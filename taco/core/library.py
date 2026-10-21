"""The household's music collection on the NAS, indexed for "play Al Green".

The NAS shares the collection over WebDAV (/music/Artist/Album/Song.flac).
The indexer walks it one folder at a time (the server refuses Depth:
infinity) and reads each song's tags through DavFile, which fetches only the
byte ranges asked for, so no song is downloaded. FLAC headers are parsed
here so cover art (often hundreds of KB) is skipped, not fetched; other
formats go through mutagen.

Runs are incremental: a file whose size and modification time haven't
changed keeps its row without a tag read. Each run builds a fresh SQLite file
and swaps it over the old one with os.replace, so lookups always see a
complete index, the old one until the new one is ready. A run whose folder
walk fails partway keeps the old index rather than lose half the collection.

Songs play by streaming the same WebDAV URLs through mpv (media.py).
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import io
import logging
import os
import random
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from .intents import normalize

log = logging.getLogger(__name__)

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}
SKIP_DIRS = {"$recycle.bin", "#recycle", "@eadir", ".appledouble"}
BLOCK = 64 * 1024
STARTUP_DELAY_S = 60
DAV = "{DAV:}"
SCHEMA = """
CREATE TABLE tracks (
  path TEXT PRIMARY KEY, size INTEGER, mtime TEXT,
  artist TEXT, album TEXT, title TEXT, track INTEGER, disc INTEGER,
  year TEXT, genre TEXT, duration REAL,
  artist_key TEXT, album_key TEXT, title_key TEXT
);
CREATE INDEX tracks_artist ON tracks(artist_key);
CREATE INDEX tracks_album ON tracks(album_key);
CREATE INDEX tracks_title ON tracks(title_key);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
COLUMNS = ("path", "size", "mtime", "artist", "album", "title", "track", "disc", "year", "genre",
           "duration", "artist_key", "album_key", "title_key")
# One-word requests that mean a radio genre, even if an album shares the name
# (Queen's "Jazz").
# Albums that "latest album" / "first album" skip.
LIVE = re.compile(r"\b(live|unplugged|in concert)\b", re.IGNORECASE)
GENRES = {"jazz", "rock", "blues", "country", "classical", "pop", "folk", "soul", "reggae",
          "news", "oldies", "bluegrass", "metal", "funk", "disco", "gospel", "hip hop", "rap"}


def key(text: str | None) -> str:
    """Comparable form of a name: 'The Rolling Stones' -> 'rolling stones'."""
    k = normalize((text or "").replace("&", " and "))
    return k.removeprefix("the ").strip()


@dataclass
class Entry:
    path: str     # relative to the share root, decoded: "Aerosmith/Toys in the Attic/01 Walk This Way.flac"
    size: int
    mtime: str


@dataclass
class Plan:
    """What to play: spoken name, kind, and the songs in order."""
    name: str
    kind: str                 # artist | album | song
    paths: list[str] = field(default_factory=list)


# --- WebDAV ------------------------------------------------------------------------

def parse_propfind(xml_text: str, root_path: str, folder: str) -> tuple[list[str], list[Entry]]:
    """A Depth: 1 PROPFIND reply -> (subfolders, audio files), as paths
    relative to the share root (root_path is the share's URL path, "/music/")."""
    folders: list[str] = []
    files: list[Entry] = []
    for resp in ET.fromstring(xml_text).iter(f"{DAV}response"):
        href = resp.findtext(f"{DAV}href") or ""
        href = unquote(urlsplit(href).path if "://" in href else href)
        rel = href.removeprefix(root_path).strip("/") if href.startswith(root_path) else None
        if rel is None or rel == folder.strip("/"):
            continue   # the folder itself
        prop = resp.find(f"{DAV}propstat/{DAV}prop")
        if prop is None:
            continue
        name = rel.rsplit("/", 1)[-1]
        if prop.find(f"{DAV}resourcetype/{DAV}collection") is not None:
            if name.lower() not in SKIP_DIRS and not name.startswith("."):
                folders.append(rel)
        elif Path(name).suffix.lower() in AUDIO_EXT and not name.startswith("."):
            files.append(Entry(rel, int(prop.findtext(f"{DAV}getcontentlength") or 0),
                               prop.findtext(f"{DAV}getlastmodified") or ""))
    return folders, files


PROPFIND_BODY = ('<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/>'
                 '<D:getcontentlength/><D:getlastmodified/></D:prop></D:propfind>')


class DavFile(io.RawIOBase):
    """A read-only, seekable view of a remote file that fetches 64 KB blocks
    with HTTP Range requests, for mutagen."""

    def __init__(self, client: httpx.Client, url: str, size: int, name: str):
        self._client, self._url, self._size, self.name = client, url, size, name
        self._pos = 0
        self._blocks: dict[int, bytes] = {}
        self.requests = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _block(self, n: int) -> bytes:
        if n not in self._blocks:
            start = n * BLOCK
            r = self._client.get(self._url, headers={"Range": f"bytes={start}-{min(start + BLOCK, self._size) - 1}"})
            r.raise_for_status()
            self.requests += 1
            self._blocks[n] = r.content if r.status_code == 206 else r.content[start:start + BLOCK]
        return self._blocks[n]

    def readinto(self, b) -> int:
        want = min(len(b), max(0, self._size - self._pos))
        got = 0
        while got < want:
            n, off = divmod(self._pos, BLOCK)
            chunk = self._block(n)[off:off + want - got]
            if not chunk:
                break
            b[got:got + len(chunk)] = chunk
            got += len(chunk)
            self._pos += len(chunk)
        return got


def _first(tags: Any, *names: str) -> str | None:
    for name in names:
        value = tags.get(name) if tags else None
        if value:
            return str(value[0] if isinstance(value, list) else value).strip() or None
    return None


def _number(text: str | None) -> int | None:
    m = re.match(r"\s*(\d+)", text or "")
    return int(m.group(1)) if m else None


def from_path(path: str) -> dict[str, Any]:
    """Artist/Album/'03 - Title.flac' -> names, for songs without tags."""
    parts = path.split("/")
    stem = Path(parts[-1]).stem
    title = re.sub(r"^\s*\d+(?:[-.]\d+)?\s*[-._]?\s*", "", stem).strip()
    return {"artist": parts[0] if len(parts) > 1 else None,
            "album": parts[1] if len(parts) > 2 else None,
            "title": title or stem, "track": _number(stem)}


def flac_tags(f: io.RawIOBase) -> tuple[dict[str, list[str]], float | None]:
    """Vorbis comments and duration from a FLAC header, seeking past every
    other metadata block (cover art, padding, seek tables)."""
    if f.read(4) != b"fLaC":
        raise ValueError("not a FLAC file")
    tags: dict[str, list[str]] = {}
    duration = None
    while True:
        header = f.read(4)
        if len(header) < 4:
            break
        last, kind, size = header[0] & 0x80, header[0] & 0x7F, int.from_bytes(header[1:], "big")
        if kind == 0:   # STREAMINFO
            info = f.read(size)
            bits = int.from_bytes(info[10:18], "big")
            rate, samples = bits >> 44, bits & ((1 << 36) - 1)
            duration = samples / rate if rate else None
        elif kind == 4:   # VORBIS_COMMENT
            data = f.read(size)
            pos = 4 + int.from_bytes(data[:4], "little")   # skip the vendor string
            count = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
            for _ in range(count):
                n = int.from_bytes(data[pos:pos + 4], "little")
                name, _, value = data[pos + 4:pos + 4 + n].decode("utf-8", "replace").partition("=")
                tags.setdefault(name.lower(), []).append(value)
                pos += 4 + n
        else:
            f.seek(size, io.SEEK_CUR)
        if last:
            break
    return tags, duration


def read_tags(client: httpx.Client, url: str, entry: Entry) -> dict[str, Any]:
    f = DavFile(client, url, entry.size, entry.path)
    if entry.path.lower().endswith(".flac"):
        tags, duration = flac_tags(f)
    else:
        import mutagen

        audio = mutagen.File(f, easy=True)
        if audio is None:
            raise ValueError("not a recognised audio file")
        tags, duration = audio.tags, getattr(audio.info, "length", None)
    return {
        "artist": _first(tags, "albumartist", "album artist", "artist"),
        "album": _first(tags, "album"),
        "title": _first(tags, "title"),
        "track": _number(_first(tags, "tracknumber")),
        "disc": _number(_first(tags, "discnumber")),
        "year": (_first(tags, "date", "year") or "")[:4] or None,
        "genre": _first(tags, "genre"),
        "duration": round(duration, 1) if duration else None,
    }


def _row(entry: Entry, tags: dict[str, Any]) -> tuple:
    guess = from_path(entry.path)
    t = {k: tags.get(k) or guess.get(k) for k in ("artist", "album", "title", "track")}
    t["title"] = t["title"] or Path(entry.path).stem
    return (entry.path, entry.size, entry.mtime, t["artist"], t["album"], t["title"], t["track"],
            tags.get("disc"), tags.get("year"), tags.get("genre"), tags.get("duration"),
            key(t["artist"]), key(t["album"]), key(t["title"]))


# --- the library ------------------------------------------------------------------

class Library:
    def __init__(self, cfg: dict[str, Any]):
        self.url = cfg["url"].rstrip("/") + "/" if cfg["url"] else ""
        self._auth = (cfg["user"], cfg["password"])
        self._verify = cfg["verify_tls"]
        self.workers = cfg["workers"]
        self.reindex_s = cfg["reindex_days"] * 86400
        self.db = Path(cfg["db"])
        self._lock = asyncio.Lock()
        self._include: Path | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def indexing(self) -> bool:
        return self._lock.locked()

    def track_url(self, path: str) -> str:
        return self.url + quote(path)

    # -- indexing --------------------------------------------------------------

    async def reindex(self) -> dict[str, Any] | None:
        """Rebuild the index in a worker thread. None if a run is already going."""
        if not self.enabled or self._lock.locked():
            return None
        async with self._lock:
            return await asyncio.to_thread(self._build)

    async def loop(self) -> None:
        """Index at startup, then every reindex_days."""
        if not self.enabled:
            return
        await asyncio.sleep(STARTUP_DELAY_S)
        while True:
            try:
                await self.reindex()
            except Exception:
                log.exception("music index failed; keeping the old one")
            while time.time() < float(self.meta().get("indexed_at", 0)) + self.reindex_s:
                await asyncio.sleep(3600)

    def _client(self) -> httpx.Client:
        return httpx.Client(auth=self._auth, verify=self._verify, timeout=30.0)

    def _build(self) -> dict[str, Any]:
        started = time.monotonic()
        root_path = urlsplit(self.url).path
        old = {row[0]: row for row in self._query("SELECT * FROM tracks")} if self.db.exists() else {}
        rows: list[tuple] = []
        failures: list[str] = []
        with self._client() as client, ThreadPoolExecutor(self.workers) as pool:
            def listing(folder: str) -> tuple[list[str], list[Entry]]:
                r = client.request("PROPFIND", self.url + quote(folder + "/" if folder else ""),
                                   headers={"Depth": "1", "Content-Type": "application/xml"},
                                   content=PROPFIND_BODY)
                r.raise_for_status()
                return parse_propfind(r.text, root_path, folder)

            def tagged(entry: Entry) -> tuple:
                try:
                    return _row(entry, read_tags(client, self.track_url(entry.path), entry))
                except Exception as e:   # unreadable file: index it by its path
                    failures.append(f"{entry.path}: {e}")
                    return _row(entry, {})

            # Walk the share breadth first, one batch of folders at a time.
            entries: list[Entry] = []
            pending = [""]
            while pending:
                results = list(pool.map(listing, pending))   # raises if any listing fails
                pending = [f for folders, _ in results for f in folders]
                entries += [e for _, files in results for e in files]
            changed = []
            for e in entries:
                prev = old.get(e.path)
                if prev and prev[1] == e.size and prev[2] == e.mtime:
                    rows.append(prev)
                else:
                    changed.append(e)
            rows += list(pool.map(tagged, changed))
        stats = {"tracks": len(rows), "read": len(changed), "failures": len(failures),
                 "seconds": round(time.monotonic() - started, 1)}
        self._write(rows, stats)
        log.info("music index: %(tracks)d songs (%(read)d read, %(failures)d unreadable) in %(seconds).0f s",
                 stats)
        for failure in failures[:10]:
            log.warning("music index: %s", failure)
        return stats

    def _write(self, rows: list[tuple], stats: dict[str, Any]) -> None:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.db.with_suffix(".db.tmp")
        tmp.unlink(missing_ok=True)
        conn = sqlite3.connect(tmp)
        try:
            conn.executescript(SCHEMA)
            conn.executemany(f"INSERT OR REPLACE INTO tracks VALUES ({','.join('?' * len(COLUMNS))})", rows)
            conn.executemany("INSERT INTO meta VALUES (?, ?)",
                             [("indexed_at", str(time.time())), *((k, str(v)) for k, v in stats.items())])
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, self.db)   # atomic: readers see the old index or the new one

    # -- lookups ---------------------------------------------------------------

    def _query(self, sql: str, args: tuple = ()) -> list[tuple]:
        if not self.db.exists():
            return []
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def meta(self) -> dict[str, str]:
        return dict(self._query("SELECT key, value FROM meta"))

    @property
    def ready(self) -> bool:
        return self.enabled and self.db.exists()

    def status(self) -> dict[str, Any]:
        meta = self.meta()
        return {"enabled": self.enabled, "indexing": self.indexing,
                "tracks": int(meta.get("tracks", 0)),
                "indexed_at": float(meta["indexed_at"]) if "indexed_at" in meta else None}

    def _songs(self, where: str, args: tuple, order: str = "disc, track, path") -> list[tuple]:
        return self._query(f"SELECT path, title, artist, album FROM tracks WHERE {where} ORDER BY {order}", args)

    def _closest(self, column: str, k: str, where: str = "1", args: tuple = ()) -> str | None:
        """The stored key in `column` that matches `k` exactly, else closely."""
        keys = [r[0] for r in self._query(f"SELECT DISTINCT {column} FROM tracks WHERE {where}", args) if r[0]]
        if k in keys:
            return k
        close = difflib.get_close_matches(k, keys, n=1, cutoff=0.85)
        return close[0] if close else None

    def albums(self, artist_key: str) -> list[dict[str, Any]]:
        """An artist's albums, oldest first (undated last), flagged if live."""
        rows = self._query("SELECT album_key, album, MIN(year), COUNT(*) FROM tracks WHERE artist_key = ? "
                           "GROUP BY album_key ORDER BY MIN(year) IS NULL, MIN(year), album", (artist_key,))
        return [{"key": k, "album": a, "year": y, "songs": n, "live": bool(LIVE.search(a or ""))}
                for k, a, y, n in rows]

    def pick_album(self, artist: str, pick: str | None = None, year: int | None = None) -> Plan | None:
        """'latest' / 'first' studio album, or the album from `year`, by `artist`.
        Live albums are skipped unless an artist has nothing else."""
        if not self.ready or not (a := self._closest("artist_key", key(artist))):
            return None
        albums = [al for al in self.albums(a) if al["year"]]
        if year:
            albums = [al for al in albums if al["year"] == str(year)]
        albums = [al for al in albums if not al["live"]] or albums
        if not albums:
            return None
        chosen = albums[-1] if pick == "latest" else albums[0]
        songs = self._songs("artist_key = ? AND album_key = ?", (a, chosen["key"]))
        return Plan(f"{songs[0][3]} by {songs[0][2]}", "album", [s[0] for s in songs])

    def mentions_artist(self, text: str) -> bool:
        """Whether `text` names an artist in the collection ("spock s beard s
        latest album"), so a request Taco couldn't work out goes to Hermes
        rather than to a radio search."""
        words = f" {key(text)} "
        return any(len(k) >= 4 and f" {k} " in words
                   for (k,) in self._query("SELECT DISTINCT artist_key FROM tracks"))

    def resolve(self, query: str, by: str | None = None, kind: str | None = None) -> Plan | None:
        """'al green' -> his songs shuffled; 'our time in eden' -> that album in
        order; 'noahs dove' -> that song. `by` narrows to an artist."""
        if not self.ready:
            return None
        q = key(query)
        if not q or (q in GENRES and not by and not kind):
            return None
        artist_k = None
        if by:
            if not (artist_k := self._closest("artist_key", key(by))):
                return None
        if kind in (None, "artist") and not by and (a := self._closest("artist_key", q)):
            songs = self._songs("artist_key = ?", (a,))
            random.shuffle(songs)
            return Plan(f"{songs[0][2]}, shuffled", "artist", [s[0] for s in songs])
        scope, scope_args = ("artist_key = ?", (artist_k,)) if artist_k else ("1", ())
        if kind in (None, "album") and (al := self._closest("album_key", q, scope, scope_args)):
            songs = self._songs(f"album_key = ? AND {scope}", (al, *scope_args))
            return Plan(f"{songs[0][3]} by {songs[0][2]}", "album", [s[0] for s in songs])
        if kind is None:
            songs = self._songs(f"title_key = ? AND {scope}", (q, *scope_args), order="path")
            if songs:
                return Plan(f"{songs[0][1]} by {songs[0][2]}", "song", [songs[0][0]])
        return None

    def suggest(self, text: str, limit: int = 5) -> list[str]:
        """Artists that `text` may be a mishearing of: 'box beard' -> Spock's
        Beard (close in spelling, or sharing a distinctive word)."""
        k = key(text)
        words = {w for w in k.split() if len(w) >= 4}
        names = dict(self._query("SELECT artist_key, MIN(artist) FROM tracks GROUP BY artist_key"))
        close = difflib.get_close_matches(k, list(names), n=limit, cutoff=0.6)
        sharing = [a for a in names if a not in close and words & set(a.split())]
        return [names[a] for a in (close + sorted(sharing, key=len))[:limit]]

    def search(self, text: str, limit: int = 10) -> dict[str, Any]:
        """Artists, albums and songs whose names contain `text`, for Hermes.
        When `text` is an artist, also all their albums, oldest first. When
        nothing matches, artists it may have been misheard for."""
        like = f"%{key(text)}%"
        out: dict[str, Any] = {}
        if a := self._closest("artist_key", key(text)):
            artist = self._query("SELECT artist FROM tracks WHERE artist_key = ? LIMIT 1", (a,))[0][0]
            out["artist_albums"] = {"artist": artist, "albums": [
                {k: v for k, v in al.items() if k != "key"} for al in self.albums(a)]}
        out |= {
            "artists": [r[0] for r in self._query(
                "SELECT artist, COUNT(*) FROM tracks WHERE artist_key LIKE ? GROUP BY artist_key LIMIT ?", (like, limit))],
            "albums": [{"album": r[0], "artist": r[1], "year": r[2]} for r in self._query(
                "SELECT album, artist, MIN(year) FROM tracks WHERE album_key LIKE ? OR artist_key LIKE ? "
                "GROUP BY album_key, artist_key LIMIT ?", (like, like, limit))],
            "songs": [{"title": r[0], "artist": r[1], "album": r[2]} for r in self._query(
                "SELECT title, artist, album FROM tracks WHERE title_key LIKE ? LIMIT ?", (like, limit))],
        }
        if not any(out.values()):
            out["did_you_mean"] = self.suggest(text)
        return out

    # -- playback ----------------------------------------------------------------

    def station(self, plan: Plan) -> dict[str, Any]:
        """What RadioPlayer.play takes for this plan."""
        return {"name": plan.name, "urls": [self.track_url(p) for p in plan.paths],
                "options": self.mpv_options(), "library": True}

    def mpv_options(self) -> list[str]:
        """mpv flags that log in to the NAS. The password goes in a private
        file mpv includes, not on the command line where `ps` shows it."""
        if self._include is None:
            run_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
            self._include = run_dir / "taco-music-auth.conf"
            token = base64.b64encode(f"{self._auth[0]}:{self._auth[1]}".encode()).decode()
            fd = os.open(self._include, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f'http-header-fields="Authorization: Basic {token}"\n')
        return [f"--include={self._include}", f"--tls-verify={'yes' if self._verify else 'no'}"]

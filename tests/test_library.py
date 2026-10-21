import asyncio
import os
import sqlite3
import struct
from urllib.parse import quote, unquote

import httpx
import pytest

from taco.core import library as lib
from taco.core.library import DavFile, Entry, Library, from_path, key, parse_propfind, read_tags

ROOT = "/music/"


def flac(tags: dict[str, str], padding: int = 300_000) -> bytes:
    """A tiny but valid FLAC header: STREAMINFO, PADDING (so the tags sit a few
    64 KB blocks in, as they do behind cover art), then VORBIS_COMMENT."""
    info = struct.pack(">HH", 4096, 4096) + b"\0" * 6
    info += ((44100 << 44) | (1 << 41) | (15 << 36) | 441000).to_bytes(8, "big") + b"\0" * 16
    vendor = b"test"
    comments = [f"{k}={v}".encode() for k, v in tags.items()]
    vc = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    vc += b"".join(struct.pack("<I", len(c)) + c for c in comments)

    def block(kind, body, last=False):
        return bytes([(0x80 if last else 0) | kind]) + len(body).to_bytes(3, "big") + body
    return b"fLaC" + block(0, info) + block(1, b"\0" * padding) + block(4, vc, last=True)


def propfind(folder: str, children: list[tuple[str, int | None, str]]) -> str:
    """children: (name, size or None for a folder, mtime)."""
    def resp(href, size, mtime):
        kind = "<D:collection/>" if size is None else ""
        length = "" if size is None else f"<D:getcontentlength>{size}</D:getcontentlength>"
        return (f"<D:response><D:href>{quote(href)}</D:href><D:propstat><D:prop>"
                f"<D:resourcetype>{kind}</D:resourcetype>{length}"
                f"<D:getlastmodified>{mtime}</D:getlastmodified></D:prop></D:propstat></D:response>")
    base = ROOT + (folder + "/" if folder else "")
    body = resp(base, None, "") + "".join(resp(base + n + ("/" if s is None else ""), s, m) for n, s, m in children)
    return f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">{body}</D:multistatus>'


class FakeNas:
    """A WebDAV server holding a small collection, counting what it's asked."""

    def __init__(self):
        self.files: dict[str, tuple[bytes, str]] = {}   # path -> (content, mtime)
        self.gets = 0
        self.fail_listing = False

    def add(self, path, tags, mtime="Mon, 01 Jan 2024 00:00:00 GMT"):
        self.files[path] = (flac(tags), mtime)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path).removeprefix(ROOT).strip("/")
        if request.method == "PROPFIND":
            if self.fail_listing and path:
                return httpx.Response(500)
            depth = len(path.split("/")) if path else 0
            kids: dict[str, tuple] = {}
            for p, (data, mtime) in self.files.items():
                parts = p.split("/")
                if p.startswith(path + "/" if path else ""):
                    name = parts[depth]
                    kids[name] = (name, len(data), mtime) if depth == len(parts) - 1 else (name, None, "")
            kids["$RECYCLE.BIN"] = ("$RECYCLE.BIN", None, "") if not path else None
            kids["cover.jpg"] = ("cover.jpg", 10, "x")
            return httpx.Response(207, text=propfind(path, [k for k in kids.values() if k]))
        data, _ = self.files[path]
        self.gets += 1
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=data[start:end + 1])


@pytest.fixture
def nas(monkeypatch):
    nas = FakeNas()
    nas.add("10,000 Maniacs/Our Time in Eden/01 - Noah's Dove.flac",
            {"ARTIST": "10,000 Maniacs", "ALBUM": "Our Time in Eden", "TITLE": "Noah's Dove",
             "TRACKNUMBER": "01", "DATE": "1992-09-29"})
    nas.add("10,000 Maniacs/Our Time in Eden/02 - These Are Days.flac",
            {"ARTIST": "10,000 Maniacs", "ALBUM": "Our Time in Eden", "TITLE": "These Are Days",
             "TRACKNUMBER": "02"})
    nas.add("Al Green/Greatest Hits/CD 1/03 Let's Stay Together.flac",
            {"ALBUMARTIST": "Al Green", "ARTIST": "Al Green", "ALBUM": "Greatest Hits",
             "TITLE": "Let's Stay Together", "TRACKNUMBER": "3", "DISCNUMBER": "1"})
    nas.add("Queen/Jazz/01 Mustapha.flac", {"ARTIST": "Queen", "ALBUM": "Jazz", "TITLE": "Mustapha"})
    monkeypatch.setattr(Library, "_client",
                        lambda self: httpx.Client(transport=httpx.MockTransport(nas.handler), auth=self._auth))
    return nas


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(lib, "STARTUP_DELAY_S", 0)
    return Library({"url": "https://nas:5001/music/", "user": "taco", "password": "pw", "verify_tls": False,
                    "reindex_days": 7, "workers": 3, "db": str(tmp_path / "music.db")})


def build(library):
    return asyncio.run(library.reindex())


def test_key_and_path_fallback():
    assert key("The Rolling Stones") == "rolling stones"
    assert key("Alison Krauss & Union Station") == "alison krauss and union station"
    assert from_path("Aerosmith/Toys in the Attic/1-03 Walk This Way.mp3") == {
        "artist": "Aerosmith", "album": "Toys in the Attic", "title": "Walk This Way", "track": 1}


def test_parse_propfind_skips_self_junk_and_recycle_bin():
    xml = propfind("Al Green", [("Greatest Hits", None, ""), ("$RECYCLE.BIN", None, ""),
                                ("x.flac", 5, "m"), ("folder.jpg", 9, "m"), (".hidden.flac", 1, "m")])
    folders, files = parse_propfind(xml, ROOT, "Al Green")
    assert folders == ["Al Green/Greatest Hits"]
    assert files == [Entry("Al Green/x.flac", 5, "m")]


def test_tags_read_with_range_requests(nas):
    path = "10,000 Maniacs/Our Time in Eden/01 - Noah's Dove.flac"
    size = len(nas.files[path][0])
    with httpx.Client(transport=httpx.MockTransport(nas.handler)) as client:
        tags = read_tags(client, "https://nas:5001/music/" + quote(path), Entry(path, size, ""))
    assert tags["title"] == "Noah's Dove" and tags["artist"] == "10,000 Maniacs"
    assert tags["track"] == 1 and tags["year"] == "1992" and tags["duration"] == 10.0
    assert size // lib.BLOCK >= 4 and nas.gets == 2   # the first block and the tags, not the padding


def test_davfile_seek_and_read(nas):
    path = next(iter(nas.files))
    data = nas.files[path][0]
    with httpx.Client(transport=httpx.MockTransport(nas.handler)) as client:
        f = DavFile(client, "https://nas:5001/music/" + quote(path), len(data), path)
        f.seek(lib.BLOCK - 3)
        assert f.read(10) == data[lib.BLOCK - 3:lib.BLOCK + 7]   # spans two blocks
        f.seek(-4, 2)
        assert f.read(100) == data[-4:]


def test_build_resolve_and_search(nas, library):
    stats = build(library)
    assert stats["tracks"] == 4 and stats["failures"] == 0
    plan = library.resolve("al green")
    assert plan.kind == "artist" and plan.name == "Al Green, shuffled"
    plan = library.resolve("our time in eden")
    assert plan.kind == "album" and plan.name == "Our Time in Eden by 10,000 Maniacs"
    assert [p.rsplit("/", 1)[1][:2] for p in plan.paths] == ["01", "02"]
    plan = library.resolve("noah s dove", by="10 000 maniacs")
    assert plan.kind == "song" and plan.paths == ["10,000 Maniacs/Our Time in Eden/01 - Noah's Dove.flac"]
    assert library.resolve("our time in eaden").kind == "album"          # misheard
    assert library.resolve("jazz") is None                                 # a genre: radio
    assert library.resolve("jazz", kind="album").name == "Jazz by Queen"
    assert library.resolve("nothing like this") is None
    assert library.search("green")["artists"] == ["Al Green"]
    station = library.station(library.resolve("al green"))
    assert station["urls"][0].startswith("https://nas:5001/music/Al%20Green/")


def test_rebuild_is_incremental(nas, library):
    build(library)
    nas.gets = 0
    stats = build(library)
    assert stats == {**stats, "tracks": 4, "read": 0} and nas.gets == 0
    nas.add("Queen/Jazz/01 Mustapha.flac", {"ARTIST": "Queen", "ALBUM": "Jazz", "TITLE": "Mustapha (remaster)"},
            mtime="Tue, 02 Jan 2024 00:00:00 GMT")
    assert build(library)["read"] == 1
    assert library.resolve("mustapha remaster").kind == "song"


def test_old_index_stays_usable_during_and_after_failed_rebuild(nas, library):
    build(library)
    reader = sqlite3.connect(f"file:{library.db}?mode=ro", uri=True)
    nas.fail_listing = True
    with pytest.raises(httpx.HTTPStatusError):
        build(library)
    assert library.status()["tracks"] == 4                # old index kept
    nas.fail_listing = False
    nas.add("New/Album/01 Song.flac", {"ARTIST": "New", "ALBUM": "Album", "TITLE": "Song"})
    build(library)
    assert reader.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 4   # old file, still readable
    assert library.status()["tracks"] == 5
    assert not library.db.with_suffix(".db.tmp").exists()


def test_one_run_at_a_time(nas, library):
    async def go():
        first = asyncio.create_task(library.reindex())
        await asyncio.sleep(0)
        assert library.indexing and await library.reindex() is None
        return await first
    assert asyncio.run(go())["tracks"] == 4


def test_mpv_auth_kept_out_of_argv(library, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    opts = library.mpv_options()
    assert "pw" not in " ".join(opts) and "--tls-verify=no" in opts
    include = tmp_path / "taco-music-auth.conf"
    assert oct(os.stat(include).st_mode & 0o777) == "0o600"
    assert "Authorization: Basic dGFjbzpwdw==" in include.read_text()

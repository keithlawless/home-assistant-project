"""Train and check the "Hey Taco" verifier from saved wake clips.

The verifier is openWakeWord's speaker-specific second stage: a logistic
regression over the same audio features the wake model scores, trained on this
household saying "Hey Taco" (positives) and on TV and other talk (negatives).
See README.md in this folder. Run on the Pi from ~/taco:

    ~/taco/.venv/bin/python wakeword/verifier/verifier.py label    # guess labels with Whisper
    ~/taco/.venv/bin/python wakeword/verifier/verifier.py train    # cross-check, then save
    ~/taco/.venv/bin/python wakeword/verifier/verifier.py report   # what the live verifier decided

Labels live in <clips_dir>/labels.csv, next to the clips and out of the repo,
because they hold transcripts of what was said at home.
"""

from __future__ import annotations

import argparse
import csv
import json
import pickle
import re
import sys
import wave
from pathlib import Path

import numpy as np

from taco.config import TACO_HOME, load_config

FRAME = 1280                  # 80 ms at 16 kHz
RATE = 16000
REQUEST_SKIP_S = 0.7          # the tail of "Hey Taco" can run past the detection
FIELDS = ["clip", "room", "score", "label", "why", "preroll", "heard"]

# "Hey Taco" as Whisper hears it: "Hey Taco", "Hey, Tato", "Hey, talk, go",
# "Hey, Chocto". Without a prompt, so Whisper isn't nudged toward the phrase.
WAKE_TEXT = re.compile(r"\bta[ck]o\b|\btato\b|\bh?ey\b[\s,.]+(t|ch)\w*", re.I)
# Requests that follow a real wake start like commands; overheard TV doesn't.
COMMAND = re.compile(
    r"^(turn|play|plate|place|stop|pause|resume|next|skip|set|start|cancel|move|"
    r"what|what's|how|when|who|is|are|add|remind|tell|show|open|volume|louder|"
    r"quieter|good|i don't hear)\b", re.I)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16)


def clip_audio(clips: Path, clip: str) -> tuple[np.ndarray, np.ndarray | None, dict]:
    """(wake-word audio, request audio or None, metadata) for kind/stem."""
    meta = json.loads((clips / f"{clip}.json").read_text())
    audio = read_wav(clips / f"{clip}.wav")
    if meta["kind"] != "wake":
        return audio, None, meta
    cut = int(meta.get("preroll_s", 2.4) * RATE)
    request = audio[cut + int(REQUEST_SKIP_S * RATE):]
    return audio[:cut], request if len(request) >= RATE else None, meta


def read_labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {row["clip"]: row for row in csv.DictReader(f)}


def write_labels(path: Path, rows: dict[str, dict]) -> None:
    with path.open("w", newline="") as f:
        out = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        out.writeheader()
        out.writerows(rows[k] for k in sorted(rows, key=lambda k: k.split("/")[1]))


def guess(meta: dict, preroll: str) -> tuple[str, str]:
    """(label, reason): pos, neg or skip (left out of training)."""
    heard = (meta.get("heard") or "").strip()
    if WAKE_TEXT.search(preroll):
        return "pos", "hey taco in preroll"
    if meta["kind"] == "wake":
        if not heard:
            return "skip", "nothing heard"
        if COMMAND.search(heard):
            return "pos", "request is a command"
        return "neg", "request is not a command"
    return ("neg", "other speech") if preroll else ("skip", "no words")


def label(args) -> None:
    from faster_whisper import WhisperModel

    path = args.clips / "labels.csv"
    rows = read_labels(path)
    todo = [f"{p.parent.name}/{p.stem}" for kind in ("wake", "near_miss")
            for p in sorted((args.clips / kind).glob("*.json"))]
    todo = [c for c in todo if c not in rows]
    if not todo:
        print(f"{path}: nothing new to label")
        return
    whisper = WhisperModel("base.en", device="cpu", compute_type="int8",
                           download_root=str(TACO_HOME / "models" / "whisper"))
    for clip in todo:
        audio, _, meta = clip_audio(args.clips, clip)
        segments, _ = whisper.transcribe(audio.astype(np.float32) / 32768, language="en",
                                         beam_size=1, without_timestamps=True)
        preroll = " ".join(s.text.strip() for s in segments).strip()
        tag, why = guess(meta, preroll)
        rows[clip] = {"clip": clip, "room": meta["room"], "score": meta.get("score", meta.get("peak")),
                      "label": tag, "why": why, "preroll": preroll, "heard": meta.get("heard", "")}
        print(f"{tag:4} {clip:45} {why:26} | {preroll[:40]:40} | {rows[clip]['heard'][:40]}")
    write_labels(path, rows)
    print(f"\n{len(todo)} new; review the label column in {path} (pos, neg or skip), then run train")


class Features:
    """Replays audio through the wake model, keeping the features of every frame."""

    def __init__(self, model: str):
        import openwakeword
        from openwakeword.model import Model

        if Path(model).suffix != ".onnx":
            openwakeword.utils.download_models(model_names=[model])
        self.oww = Model(wakeword_models=[model], inference_framework="onnx")
        self.key = next(iter(self.oww.models))

    def frames(self, audio: np.ndarray, offset: int = 0) -> tuple[np.ndarray, np.ndarray]:
        """(scores, features) for each 80 ms frame."""
        self.oww.reset()
        audio = audio[offset:]
        scores, feats = [], []
        for i in range(0, len(audio) - FRAME + 1, FRAME):
            scores.append(self.oww.predict(audio[i:i + FRAME])[self.key])
            feats.append(self.oww.preprocessor.get_features(self.oww.model_inputs[self.key])[0])
        if not feats:
            return np.zeros(0), np.zeros((0, self.oww.model_inputs[self.key], 96))
        return np.array(scores), np.array(feats)


def training_set(feat: Features, clips: Path, rows: list[dict], floor: float):
    """Feature windows and labels, plus the clip each came from (for grouping).
    Positives are the frames the live verifier would see (score >= floor),
    taken at five small offsets; negatives are every frame of other speech,
    including the household's own requests."""
    X, y, groups = [], [], []
    for n, row in enumerate(rows):
        audio, request, _ = clip_audio(clips, row["clip"])
        if row["label"] == "pos":
            for offset in (0, 256, 512, 768, 1024):
                scores, f = feat.frames(audio, offset)
                X += list(f[scores >= floor])
                y += [1] * int((scores >= floor).sum())
                groups += [n] * int((scores >= floor).sum())
            if request is not None:
                f = feat.frames(request)[1]
                X += list(f); y += [0] * len(f); groups += [n] * len(f)
        else:
            f = feat.frames(audio)[1]
            X += list(f); y += [0] * len(f); groups += [n] * len(f)
    return np.array(X), np.array(y), np.array(groups)


def candidate_score(feat: Features, model, clips: Path, clip: str, floor: float) -> float | None:
    """What the live verifier would report for this clip: its peak over the
    frames at or above the floor (None if the wake model never got there)."""
    audio = clip_audio(clips, clip)[0]
    scores, f = feat.frames(audio)
    f = f[scores >= floor]
    return float(model.predict_proba(f)[:, 1].max()) if len(f) else None


def summarize(results: list[tuple[dict, float | None]], threshold: float) -> None:
    """Per label and kind: how many the verifier would accept."""
    table: dict[tuple[str, str], list[int]] = {}
    for row, v in results:
        kind = row["clip"].split("/")[0]
        counts = table.setdefault((row["label"], kind), [0, 0])
        counts[0] += v is not None and v >= threshold
        counts[1] += 1
    names = {"pos": "real 'Hey Taco'", "neg": "TV / other speech"}
    for (tag, kind), (accepted, total) in sorted(table.items()):
        print(f"  {names[tag]:18} {kind:9} accepted {accepted:3}/{total:<3}")


def train(args) -> None:
    from openwakeword.custom_verifier_model import train_verifier_model
    from sklearn.model_selection import GroupKFold

    rows = [r for r in read_labels(args.clips / "labels.csv").values() if r["label"] in ("pos", "neg")]
    pos = sum(r["label"] == "pos" for r in rows)
    if pos < 10 or len(rows) - pos < 10:
        sys.exit(f"need at least 10 pos and 10 neg labels; have {pos} and {len(rows) - pos}")
    feat = Features(args.model)
    X, y, groups = training_set(feat, args.clips, rows, args.floor)
    print(f"{pos} positive and {len(rows) - pos} negative clips -> "
          f"{int(y.sum())} positive and {int((y == 0).sum())} negative frames")

    # Cross-check: each clip is scored by a model that never saw it.
    results = []
    for train_idx, test_idx in GroupKFold(n_splits=5).split(X, y, groups):
        model = train_verifier_model(X[train_idx], y[train_idx])
        for n in sorted(set(groups[test_idx])):
            results.append((rows[n], candidate_score(feat, model, args.clips, rows[n]["clip"], args.floor)))
    print(f"\ncross-checked at verifier threshold {args.threshold}:")
    summarize(results, args.threshold)
    for row, v in results:
        wrong = (v is not None and v >= args.threshold) != (row["label"] == "pos")
        if wrong:
            shown = "-" if v is None else f"{v:.2f}"
            print(f"  wrong: {row['label']} {row['clip']} verifier {shown} | {row['preroll'][:50]}")

    model = train_verifier_model(X, y)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("wb") as f:
        pickle.dump(model, f)
    print(f"\nsaved {args.out}")


def report(args) -> None:
    """Clips saved while the live verifier ran, against their labels."""
    rows = read_labels(args.clips / "labels.csv")
    results, unlabeled = [], 0
    for kind in ("wake", "near_miss"):
        for p in sorted((args.clips / kind).glob("*.json")):
            meta = json.loads(p.read_text())
            if "verifier" not in meta:
                continue
            row = rows.get(f"{kind}/{p.stem}")
            if row is None or row["label"] not in ("pos", "neg"):
                unlabeled += 1
                continue
            results.append((row, meta["verifier"]))
    if not results:
        sys.exit("no labeled clips with a verifier score yet; run label first")
    print(f"live verifier at threshold {args.threshold} ({unlabeled} clips unlabeled or skipped):")
    summarize(results, args.threshold)


def main() -> None:
    w = load_config()["wake"]
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["label", "train", "report"])
    parser.add_argument("--clips", type=Path, default=Path(w["clips_dir"]))
    parser.add_argument("--model", default=w["model"], help="wake word model (.onnx or built-in name)")
    beside_model = Path(w["model"]).with_name(f"{Path(w['model']).stem}_verifier.pkl")
    out = w["verifier"] if w["verifier"] not in ("", "auto") else beside_model
    parser.add_argument("--out", type=Path, default=Path(out))
    parser.add_argument("--floor", type=float, default=w["near_miss"], help="candidate score floor")
    parser.add_argument("--threshold", type=float, default=w["verifier_threshold"])
    args = parser.parse_args()
    {"label": label, "train": train, "report": report}[args.command](args)


if __name__ == "__main__":
    main()

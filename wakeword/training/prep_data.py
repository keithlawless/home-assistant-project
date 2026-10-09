"""Background audio and room impulse responses for wake word training
(mirrors openWakeWord's automatic_model_training notebook)."""
import os, subprocess
from pathlib import Path
import numpy as np, scipy.io.wavfile, datasets
from tqdm import tqdm

def save(path, arr):
    scipy.io.wavfile.write(path, 16000, (np.clip(arr, -1, 1) * 32767).astype(np.int16))

os.makedirs("mit_rirs", exist_ok=True)
if len(os.listdir("mit_rirs")) < 200:
    for row in tqdm(datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True), desc="rirs"):
        save(os.path.join("mit_rirs", row["audio"]["path"].split("/")[-1]), row["audio"]["array"])

os.makedirs("audioset", exist_ok=True)
os.makedirs("audioset_16k", exist_ok=True)
if not os.listdir("audioset_16k"):
    # The AudioSet mirror now ships parquet shards (~700 MB each) with embedded audio.
    parts = []
    for i in ("00", "01"):
        path = f"audioset/bal_train_{i}.parquet"
        if not os.path.exists(path):
            subprocess.run(["curl", "-sSfL", "-o", path,
                            f"https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/{i}.parquet"],
                           check=True)
        parts.append(path)
    # Read with pyarrow directly: the shards' feature metadata is too new for
    # the datasets version openWakeWord's trainer pins.
    import io, librosa, pyarrow.parquet as pq, soundfile as sf
    n = 0
    for path in parts:
        table = pq.read_table(path, columns=["audio"])
        for cell in tqdm(table.column("audio").to_pylist(), desc=f"audioset {path}"):
            try:
                audio, rate = sf.read(io.BytesIO(cell["bytes"]), dtype="float32", always_2d=True)
            except Exception:
                continue
            audio = librosa.resample(audio.mean(axis=1), orig_sr=rate, target_sr=16000)
            save(os.path.join("audioset_16k", f"audioset_{n:05d}.wav"), audio)
            n += 1

os.makedirs("fma", exist_ok=True)
n_clips = 3 * 3600 // 30  # 3 hours of 30-second music clips
if len(os.listdir("fma")) < n_clips:
    fma = iter(datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
               .cast_column("audio", datasets.Audio(sampling_rate=16000)))
    for _ in tqdm(range(n_clips), desc="fma"):
        row = next(fma)
        save(os.path.join("fma", row["audio"]["path"].split("/")[-1].replace(".mp3", ".wav")), row["audio"]["array"])
print("prep done:", {d: len(os.listdir(d)) for d in ("mit_rirs", "audioset_16k", "fma")})

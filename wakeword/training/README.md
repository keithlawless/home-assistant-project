# Training hey_taco.onnx on a Mac (Apple Silicon)

This is how `../hey_taco.onnx` was built on 2026-10-09, after the Colab
notebook broke on Python 3.13. Takes about 2 hours on an M2 Max (mostly
generating 66,000 synthetic clips on the CPU) and ~40 GB of disk in the work
directory, which can be deleted afterwards.

```bash
mkdir ~/taco-wakeword && cd ~/taco-wakeword
python3.11 -m venv .venv && . .venv/bin/activate
git clone https://github.com/rhasspy/piper-sample-generator
git -C piper-sample-generator checkout v2.0.0      # v3 needs numpy 2; the trainer expects v2
curl -L -o piper-sample-generator/models/en_US-libritts_r-medium.pt \
  https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt
git clone https://github.com/dscripka/openwakeword
pip install "numpy<2" "scipy<1.15" "pyarrow<16" torch==2.5.1 torchaudio==2.5.1 \
  piper-phonemize-cross webrtcvad onnxruntime onnx tqdm scikit-learn requests pyyaml \
  mutagen==1.47.0 torchinfo==1.8.0 torchmetrics==1.2.0 speechbrain==0.5.14 \
  audiomentations==0.33.0 torch-audiomentations==0.11.0 acoustics==0.2.6 \
  pronouncing==0.2.0 datasets==2.14.6 deep-phonemizer==0.0.19 soundfile librosa
pip install -e ./openwakeword --no-deps
mkdir -p openwakeword/openwakeword/resources/models
for f in embedding_model.onnx melspectrogram.onnx; do
  curl -L -o openwakeword/openwakeword/resources/models/$f \
    https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$f
done
for f in validation_set_features.npy openwakeword_features_ACAV100M_2000_hrs_16bit.npy; do  # ~17 GB
  curl -L -o $f https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/$f
done
cp <repo>/wakeword/training/{prep_data.py,hey_taco.yaml,run_training.sh} .
```

One patch to `openwakeword/openwakeword/train.py` is needed on macOS: right
after `if __name__ == '__main__':` add

```python
    import multiprocessing
    multiprocessing.set_start_method('fork', force=True)
```

(macOS spawns DataLoader workers, which can't pickle the trainer's lambdas.)

Then `python prep_data.py & ./run_training.sh`. Training ends with a harmless
`No module named 'onnx_tf'` error while converting to `.tflite`; the `.onnx`
that Taco uses has already been saved to `my_custom_model/hey_taco.onnx`.

Result: accuracy 0.74, recall 0.47 on heavily augmented test clips, 0.0 false
positives per hour on the 11-hour validation set. On clean Piper/Kokoro
voices it scores 0.92–0.96 for "Hey Taco" and ~0 for "taco Tuesday",
"tacos tonight" and "Hey Jarvis".

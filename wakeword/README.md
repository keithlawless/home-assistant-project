# Training the "Hey Taco" wake word

Taco uses [openWakeWord](https://github.com/dscripka/openWakeWord) with the
custom model in this folder, **`hey_taco.onnx`**. It was trained locally on a
Mac (see `training/README.md`) because the Colab notebook below broke on
Colab's Python 3.13. The Colab route is kept here for reference.

Training needs a GPU and a large set of background audio, so it runs in
Google Colab (free) rather than on the Pi. It takes under an hour, mostly
unattended.

## 1. Train in Colab

1. Open the openWakeWord training notebook:
   https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb
2. Sign in with a Google account. In **Runtime → Change runtime type**, choose
   a **GPU** (the free T4 is fine).
3. In the first settings cell, set the target phrase to:

   ```
   hey taco
   ```

   Keep the defaults for everything else. If the notebook offers alternate
   spellings, adding `hey tacoe` can help the synthetic voices pronounce it.
4. **Runtime → Run all**. Leave the tab open until it finishes.
5. Download the resulting **`hey_taco.onnx`** (the notebook also makes a
   `.tflite`; Taco needs the `.onnx`).

### If a cell fails with `No module named 'piper_phonemize'`

Colab's newer Python has no build of `piper-phonemize`. Add a code cell just
above the failing one and run it:

```python
import sys; print(sys.version)
!pip install piper-phonemize-cross
```

then re-run from the failing cell (**Runtime → Run cell and below**). The
replacement package has builds up to Python 3.12; if the version printed is
3.13 or newer, pick an older **Runtime version** under **Runtime → Change
runtime type** and start over with this cell added. The fix lasts only for
that Colab session. (Background: openWakeWord issue #277.)

## 2. Install it on Taco

From the Mac, in this repo:

```bash
scp ~/Downloads/hey_taco.onnx taco@192.168.4.239:/srv/taco/models/wakeword/
```

Then on the Pi, add to `/srv/taco/config.toml`:

```toml
[wake]
model = "/srv/taco/models/wakeword/hey_taco.onnx"
```

and restart the voice service:

```bash
systemctl --user restart taco-voice
```

## 3. Tune it

Custom models are usually a little less precise than the built-in ones. If
Taco misses "Hey Taco", set **Settings → Wake word sensitivity** to *High*;
if it wakes up on its own, set it to *Low*. The voice log shows each
detection with its score:

```bash
journalctl --user-unit taco-voice -f | grep -E "wake word|barge-in"
```

### Per room

Sensitivity applies to every mic. A mic that hears a TV can be made stricter
on its own in `/srv/taco/config.toml`, keyed by the mic's name:

```toml
[wake.rooms."living room"]
offset = 0.1    # added to the sensitivity threshold
patience = 2    # 80 ms frames in a row at or above it (default 1)
```

A clean "Hey Taco" from a Piper voice stays above 0.5 for about 4 frames, so
patience 2–3 keeps real wakes while dropping one-frame spikes. Restart
taco-voice after editing.

### Clips

Every wake is saved under `/srv/taco/wake_clips/wake/`: a WAV with the 2.4 s
before the detection, then the request that followed. The JSON file next to
it holds the per-frame scores and `heard`, the transcript, so TV false
triggers are easy to pick out. Near misses go in `near_miss/`. These are runs
that peaked at 0.3 or more without firing, and are usually what a missed
"Hey Taco" looks like. Each folder keeps the newest 1000 clips. Set
`[wake] save_clips = false` to stop recording. These clips are the data for
training a custom verifier or retraining the model.

The log also shows `mic '<name>': no audio for N s` when a mic or satellite
stops streaming while still connected.

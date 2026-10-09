# Training the "Hey Taco" wake word

Taco uses [openWakeWord](https://github.com/dscripka/openWakeWord). Until a
custom model exists, it listens for the built-in **"Hey Jarvis"**.

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
journalctl --user -u taco-voice -f | grep -E "wake word|barge-in"
```

# Wake word verifier

A second check on "Hey Taco", in the style of smart speakers that pair an
easily triggered first stage with a stricter second one. The wake model
(`hey_taco.onnx`) marks *candidates*: any run of scores reaching
`[wake] near_miss` (0.3). The verifier, a small logistic regression over the
same audio features, trained on this household's voices, then scores each
candidate. It learns what *your* "Hey Taco" sounds like, so it can accept a
real wake the wake model scored low, and reject TV speech the wake model
scored high.

**It decides wakes** (`[wake] verifier_enforce = true`, the default once a
verifier model exists). A candidate the verifier accepts fires, even when the
wake model scored it below the threshold, and a run it rejects never fires,
however high it scored. Each verdict is logged and saved in the clip's JSON as
`"verifier"`:

```
wake word (0.93, kitchen), verifier 0.95 accept
wake word (0.41, kitchen), verifier 0.92 accept     <- below the threshold, rescued
wake word rejected (0.92, living room), verifier 0.04 reject     <- TV
```

Set `verifier_enforce = false` in config.toml to go back to threshold and
patience alone, with the verifier only logged.

## Training

Everything runs on the Pi from `~/taco`, using the clips in
`/srv/taco/wake_clips` (see `taco/voice/clips.py`):

```bash
.venv/bin/python wakeword/verifier/verifier.py label    # Whisper guesses a label for each new clip
.venv/bin/python wakeword/verifier/verifier.py train    # cross-check, then save the model
systemctl --user restart taco-voice                       # pick up the new model
```

`label` writes `/srv/taco/wake_clips/labels.csv`. That file stays on the Pi
and out of the repo, because it holds transcripts of things said at home.
Check the `label` column before training:

- `pos`: someone said "Hey Taco". The guess is based on Whisper hearing it in
  the audio before the wake, or on the request that followed being a command.
- `neg`: TV or other talk.
- `skip`: unclear, left out. Prefer `skip` to a wrong `neg`. A real wake
  labelled `neg` teaches the verifier to reject your voice.

`train` also uses the household's own requests (the audio after each real
wake) as negatives, so the verifier learns the phrase, not just the voice.
Before saving, it scores every clip with a model trained without that clip
and prints how many real wakes and TV clips it would accept. The model is
saved as `hey_taco_verifier.pkl` beside `hey_taco.onnx`. `[wake] verifier =
"auto"` (the default) loads it from there.

Guests' voices are not in the training set, so once the verifier is enforcing,
a guest may find Taco harder to wake. Re-run `label` and `train` from time to
time: every week of use adds clips.

## Checking the live verdicts

```bash
.venv/bin/python wakeword/verifier/verifier.py label     # label the new clips
.venv/bin/python wakeword/verifier/verifier.py report    # what the live verifier decided
```

`report` only counts clips saved after the verifier was running, so these are
decisions on audio it was not trained on.

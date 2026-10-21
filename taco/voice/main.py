"""taco-voice: the always-on voice loop.

wake word -> chime -> record -> STT -> fast path or Hermes (streamed) -> TTS -> speaker

With an echo-cancelling speakerphone ([audio] barge_in = true), the wake
word is also watched while Taco speaks, so "Hey Taco" interrupts an answer.
Without one, the loop is half-duplex: the mic is ignored while Taco speaks.

Mic satellites in other rooms ([[audio.satellites]]) are listened to at the
same time, each with its own wake word model and tuning ([wake.rooms]);
whichever mic hears the wake word handles that turn. Replies always play on
the local speaker. Wakes and near misses are saved as clips (clips.py).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import httpx
import numpy as np

from taco.config import load_config, read_env_value
from taco.voices import DEFAULT_VOICE, PREVIEW_TEXT

from .alarm import AlarmPlayer
from .clips import ClipRecorder
from .audio import Microphone, NetworkMicrophone, Speaker, tone
from .core_link import CoreLink
from .hermes import HermesClient, asks_question
from .listen import record_request
from .stt import Transcriber, build_vocabulary
from .tts import EngineCache
from .wake import WakeWord

log = logging.getLogger("taco.voice")

WAKE_CHIME = tone([660, 880], duration=0.15, volume=0.4)
ANNOUNCE_CHIME = tone([523, 659, 784], duration=0.16, volume=0.4)
ERROR_REPLY = "Sorry, I'm having trouble reaching my brain right now."
WAKE_THRESHOLDS = {"low": 0.7, "medium": 0.5, "high": 0.35}


class Events:
    """Fire-and-forget state updates to taco-core for the screen.
    taco-core may not be running, so failures are ignored."""

    def __init__(self, core_url: str):
        self._url = f"{core_url.rstrip('/')}/api/voice/event"
        self._q: queue.Queue[dict] = queue.Queue(maxsize=100)
        threading.Thread(target=self._run, daemon=True, name="events").start()

    def send(self, kind: str, **data) -> None:
        try:
            self._q.put_nowait({"type": kind, **data})
        except queue.Full:
            pass

    def _run(self) -> None:
        with httpx.Client(timeout=1.0) as http:
            while True:
                event = self._q.get()
                try:
                    http.post(self._url, json=event)
                except httpx.HTTPError:
                    pass


class BargeIn:
    """While Taco speaks, watch the mic for the wake word and cut the reply
    short when it's heard. A no-op without echo cancellation, and for turns
    started from a satellite, whose mic has none."""

    def __init__(self, loop: VoiceLoop):
        self.loop = loop
        self.heard = False
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> BargeIn:
        if self.loop.cfg["audio"]["barge_in"] and self.loop.active is self.loop.listeners[0]:
            self.mic, self.wake = self.loop.active
            self.mic.flush()
            # The model's rolling window still holds the "Hey Taco" that started
            # this turn; without a reset it re-fires instantly.
            self.wake.reset()
            self._thread = threading.Thread(target=self._watch, daemon=True, name="barge-in")
            self._thread.start()
        return self

    def _watch(self) -> None:
        while not self._done.is_set():
            try:
                frame = self.mic.read(timeout=0.1)
            except queue.Empty:
                continue
            if (score := self.wake.detect(frame)) is not None:
                log.info("barge-in (%.2f)", score)
                self.heard = True
                self.loop.cancel.set()
                self.loop.speaker.stop()
                self.loop.hermes.abort()
                return

    def __exit__(self, *exc) -> None:
        self._done.set()
        if self._thread:
            self._thread.join()
        if self.heard:
            self.wake.reset()


class VoiceLoop:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        audio = cfg["audio"]
        self.ready = threading.Event()   # any mic has a new frame
        mics = [Microphone(audio["sample_rate"], audio["input_device"], audio["input_name"], self.ready)]
        mics += [
            NetworkMicrophone(s["name"], s["port"], s["token"], s.get("allow"), ready=self.ready)
            for s in audio["satellites"]
        ]
        # openWakeWord keeps a rolling window per stream, so one model per mic.
        self.listeners = [(mic, self._wake_for(mic.name)) for mic in mics]
        w = cfg["wake"]
        self.clips = ClipRecorder(w["clips_dir"], w["max_clips"]) if w["save_clips"] else None
        self._wake_clip: dict | None = None   # the current wake, until its request is heard
        self.active = self.listeners[0]   # the (mic, wake) handling the current turn
        self.speaker = Speaker(audio["output_device"])
        self.stt = Transcriber(cfg["stt"]["model"], cfg["stt"]["compute_type"],
                               build_vocabulary(None, cfg["stt"]["vocabulary"]))
        self.engines = EngineCache()
        self._tts_lock = threading.Lock()
        self.voice_id = f"{cfg['tts']['engine']}:{cfg['tts']['voice']}"
        self.tts = self.engines.get(self.voice_id)
        self.ack_audio = None
        h = cfg["hermes"]
        self.hermes = HermesClient(
            h["url"], read_env_value(h["env_file"], "API_SERVER_KEY"), h["model"],
            h["timeout_s"], h["history_ttl_s"],
        )
        self.events = Events(cfg["core"]["url"])
        self.cancel = threading.Event()   # stop button or barge-in
        self.muted = False                 # "stop listening" privacy mode
        self.announcer = Speaker(audio["output_device"])   # alerts, scheduled messages
        self.alarm = AlarmPlayer(Speaker(audio["output_device"]), self.synthesize)
        self.core = CoreLink(cfg["core"]["url"], self._on_core_message)

    def _wake_for(self, room: str) -> WakeWord:
        w = self.cfg["wake"]
        tune = w["rooms"].get(room, {})
        wake = WakeWord(w["model"], w["threshold"], tune.get("offset", 0.0), tune.get("patience", w["patience"]),
                        w["near_miss"] if w["save_clips"] else None)
        log.info("wake word %s for %s: threshold %.2f, patience %d",
                 wake.name, room, wake.threshold, wake.patience)
        return wake

    # -- voice and settings ------------------------------------------------------

    def synthesize(self, text: str):
        # The alarm thread and the conversation share the current engine.
        with self._tts_lock:
            return self.tts.synthesize(text)

    def _render_ack(self) -> None:
        ack = self.cfg["listen"]["ack_phrase"]
        self.ack_audio = self.synthesize(ack) if ack else None

    def set_voice(self, voice_id: str) -> None:
        if voice_id == self.voice_id:
            return
        try:
            engine = self.engines.get(voice_id)   # loads outside the lock; may take a moment
        except Exception:
            log.exception("could not load voice %s", voice_id)
            return
        with self._tts_lock:
            self.tts, self.voice_id = engine, voice_id
        self._render_ack()
        log.info("voice is now %s", voice_id)

    def _apply_settings(self, settings: dict[str, Any]) -> None:
        muted = bool(settings.get("mic_muted"))
        if muted != self.muted:
            self.muted = muted
            log.info("microphone %s", "muted" if muted else "listening")
        threshold = WAKE_THRESHOLDS.get(settings.get("wake_sensitivity", "medium"))
        if threshold and threshold != self.listeners[0][1].sensitivity:
            for _, wake in self.listeners:
                wake.sensitivity = threshold
            log.info("wake threshold %s", ", ".join(
                f"{mic.name} {wake.threshold:.2f}" for mic, wake in self.listeners))
        self.stt.set_vocabulary(build_vocabulary(settings.get("location"), self.cfg["stt"]["vocabulary"]))
        voice = settings.get("voice") or DEFAULT_VOICE
        if voice != self.voice_id:
            threading.Thread(target=self.set_voice, args=(voice,), daemon=True, name="voice-load").start()

    def _preview(self, voice_id: str) -> None:
        try:
            engine = self.engines.get(voice_id)
            self.speaker.play(*engine.synthesize(PREVIEW_TEXT))
        except Exception:
            log.exception("preview of %s failed", voice_id)

    def _announce(self, text: str) -> None:
        try:
            self.announcer.play(ANNOUNCE_CHIME, 22050)
            self.announcer.play(*self.synthesize(text))
            log.info("announced: %r", text)
        except Exception:
            log.exception("announcement failed")

    def _on_core_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "announce" and msg.get("text"):
            threading.Thread(target=self._announce, args=(msg["text"],), daemon=True).start()
        if kind == "notification_chime":
            self.announcer.play(ANNOUNCE_CHIME, 22050)
        if kind in ("timers", "snapshot"):
            self.alarm.update(msg.get("timers") or [])
        if kind in ("settings", "snapshot") and msg.get("settings"):
            self._apply_settings(msg["settings"])
        if kind == "voice_command":
            if msg.get("command") == "stop":
                log.info("stop requested")
                self.cancel.set()
                self.speaker.stop()
                self.hermes.abort()
            elif msg.get("command") == "preview" and msg.get("voice"):
                threading.Thread(target=self._preview, args=(msg["voice"],), daemon=True).start()

    # -- main loop -----------------------------------------------------------------

    def run(self) -> None:
        for mic, _ in self.listeners:
            try:
                mic.start()
            except OSError:
                log.exception("mic %r failed to start", mic.name)
        threading.Thread(target=self._watch_mics, daemon=True, name="mic-watch").start()
        self._render_ack()   # also warms up the TTS graph
        log.info("listening for wake word on %s (barge-in %s)",
                 ", ".join(mic.name for mic, _ in self.listeners),
                 "on" if self.cfg["audio"]["barge_in"] else "off")
        self.events.send("state", state="idle")
        while True:
            self.ready.wait()
            self.ready.clear()   # before draining, so a frame arriving mid-drain re-sets it
            if (heard := self._detect_wake()) is None:
                continue
            score, self.active = heard
            mic, wake = self.active
            log.info("wake word (%.2f, %s)", score, mic.name)
            if self.clips:
                audio, scores = wake.recent()
                self._wake_clip = {"room": mic.name, "audio": audio, "info": {
                    "score": round(score, 3), "threshold": round(wake.threshold, 3),
                    "patience": wake.patience, "preroll_s": round(len(audio) / 16000, 2),
                    "scores": scores,
                }}
            self._reset_wake()
            if self.alarm.active:
                # Like a smart speaker: saying the name silences the alarm.
                self.alarm.silence()
                self.core.stop_ringing()
            self.converse()
            self._wake_clip = None
            self._reset_wake()
            self.flush_mics()
            self.events.send("state", state="idle")

    def _detect_wake(self):
        """Feed every mic's buffered frames to its wake model. Returns
        (score, (mic, wake)) for the strongest detection, or None."""
        best = None
        for mic, wake in self.listeners:
            while (frame := mic.read_nowait()) is not None:
                if self.muted:
                    continue   # frames are still drained so unmuting starts fresh
                score = wake.detect(frame)
                if self.clips and (miss := wake.pop_near_miss()):
                    self.clips.save("near_miss", mic.name, miss.audio, {
                        "peak": round(miss.peak, 3), "threshold": round(wake.threshold, 3),
                        "frames_above": miss.frames, "patience": wake.patience,
                        "scores": miss.scores,
                    })
                if score is not None:
                    if best is None or score > best[0]:
                        best = (score, (mic, wake))
                    break   # leave the rest of this mic's audio for the request
        return best

    def _watch_mics(self) -> None:
        while True:
            time.sleep(1.0)
            for mic, _ in self.listeners:
                mic.check_stall()

    def _clip_request(self, audio: np.ndarray | None, text: str | None) -> None:
        """Save the current wake's clip with the request that followed it
        (first turn only; None = nobody spoke)."""
        clip, self._wake_clip = self._wake_clip, None
        if clip is None or not self.clips:
            return
        if audio is not None:
            clip["audio"] = np.concatenate([clip["audio"], audio])
        clip["info"]["heard"] = text
        self.clips.save("wake", clip["room"], clip["audio"], clip["info"])

    def _reset_wake(self) -> None:
        for _, wake in self.listeners:
            wake.reset()

    def flush_mics(self) -> None:
        """Drop buffered audio on every mic, e.g. Taco's own voice, which
        satellites in other rooms hear too."""
        for mic, _ in self.listeners:
            mic.flush()

    def converse(self) -> None:
        """One wake: a request, plus follow-ups while Taco keeps asking questions
        or keeps being interrupted."""
        start_timeout = self.cfg["listen"]["start_timeout_s"]
        satellite = isinstance(self.active[0], NetworkMicrophone)
        vad = self.cfg["listen"]["satellite_vad_aggressiveness" if satellite else "vad_aggressiveness"]
        while True:
            self._play_and_wait(WAKE_CHIME, 22050)
            self.events.send("state", state="listening")
            audio = record_request(
                self.active[0],
                start_timeout_s=start_timeout,
                end_silence_s=self.cfg["listen"]["end_silence_s"],
                max_seconds=self.cfg["listen"]["max_seconds"],
                aggressiveness=vad,
            )
            if audio is None:
                log.info("no speech after wake")
                self._clip_request(None, None)
                return

            self.events.send("state", state="thinking")
            t0 = time.monotonic()
            text = self.stt.transcribe(audio)
            log.info("heard (%.2fs): %r", time.monotonic() - t0, text)
            self._clip_request(audio, text)
            if not text:
                return
            self.events.send("heard", text=text)

            fast = self.core.intent(text)
            if fast.get("handled"):
                log.info("fast path (%.2fs): %r", time.monotonic() - t0, fast.get("speech"))
                reply, barged = self.say(fast.get("speech") or "") if not fast.get("silent") else ("", False)
            else:
                if self.ack_audio:
                    # Tell the user they were understood; Hermes is already
                    # being asked while this plays.
                    self.speaker.play(*self.ack_audio)
                reply, barged = self.respond(text, t0)

            if barged:
                start_timeout = self.cfg["listen"]["start_timeout_s"]
                continue  # "Hey Taco" mid-answer: listen for the new request
            # A reply that asks a question gets a follow-up turn, no wake word.
            if not asks_question(reply):
                return
            start_timeout = self.cfg["listen"]["followup_s"]

    def say(self, text: str) -> tuple[str, bool]:
        """Speak a complete reply (fast-path answers). Returns (text, barged in)."""
        if not text:
            return "", False
        self.cancel.clear()
        self.events.send("state", state="speaking")
        self.events.send("reply", text=text)
        with BargeIn(self) as barge:
            self.speaker.play(*self.synthesize(text))
            self.speaker.wait()
        self.flush_mics()
        return ("" if self.cancel.is_set() else text), barge.heard

    def respond(self, text: str, t0: float) -> tuple[str, bool]:
        """Stream a Hermes reply sentence by sentence. Returns (text, barged in)."""
        spoken: list[str] = []
        first_audio = None
        self.cancel.clear()
        with BargeIn(self) as barge:
            try:
                for sentence in self.hermes.ask(text, on_tool=lambda e: self.events.send("tool", **e)):
                    if self.cancel.is_set():
                        log.info("reply cancelled")
                        break  # closes the stream; Hermes stops generating
                    audio, rate = self.synthesize(sentence)
                    if self.cancel.is_set():
                        break
                    if first_audio is None:
                        first_audio = time.monotonic() - t0
                        log.info("first audio after %.2fs", first_audio)
                        self.events.send("state", state="speaking")
                    self.speaker.play(audio, rate)
                    spoken.append(sentence)
                    self.events.send("reply", text=" ".join(spoken))
            except (httpx.HTTPError, httpx.StreamError) as e:
                if not self.cancel.is_set():  # an abort also lands here
                    log.error("hermes request failed: %s", e)
                    self.speaker.play(*self.synthesize(ERROR_REPLY))
                    spoken = [ERROR_REPLY]
            self.speaker.wait()
        self.flush_mics()  # discard Taco's own voice picked up by the mics
        log.info("reply done in %.2fs: %r", time.monotonic() - t0, " ".join(spoken))
        # A cancelled reply never invites a follow-up question.
        return ("" if self.cancel.is_set() else " ".join(spoken)), barge.heard

    def _play_and_wait(self, audio, rate) -> None:
        self.speaker.play(audio, rate)
        self.speaker.wait()
        if isinstance(self.active[0], NetworkMicrophone):
            # A satellite's audio arrives a few hundred ms late, so the end of
            # the chime would land after the flush and pass for speech.
            time.sleep(self.cfg["listen"]["satellite_lag_s"])
        self.flush_mics()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    VoiceLoop(load_config()).run()


if __name__ == "__main__":
    main()

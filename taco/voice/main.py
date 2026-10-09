"""taco-voice: the always-on voice loop.

wake word -> chime -> record -> STT -> fast path or Hermes (streamed) -> TTS -> speaker

With an echo-cancelling speakerphone ([audio] barge_in = true), the wake
word is also watched while Taco speaks, so "Hey Taco" interrupts an answer.
Without one, the loop is half-duplex: the mic is ignored while Taco speaks.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import httpx

from taco.config import load_config, read_env_value
from taco.voices import DEFAULT_VOICE, PREVIEW_TEXT

from .alarm import AlarmPlayer
from .audio import Microphone, Speaker, tone
from .core_link import CoreLink
from .hermes import HermesClient
from .listen import record_request
from .stt import Transcriber
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
    short when it's heard. A no-op without echo cancellation."""

    def __init__(self, loop: VoiceLoop):
        self.loop = loop
        self.heard = False
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> BargeIn:
        if self.loop.cfg["audio"]["barge_in"]:
            self.loop.mic.flush()
            # The model's rolling window still holds the "Hey Taco" that started
            # this turn; without a reset it re-fires instantly.
            self.loop.wake.reset()
            self._thread = threading.Thread(target=self._watch, daemon=True, name="barge-in")
            self._thread.start()
        return self

    def _watch(self) -> None:
        while not self._done.is_set():
            try:
                frame = self.loop.mic.read(timeout=0.1)
            except queue.Empty:
                continue
            if (score := self.loop.wake.detect(frame)) is not None:
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
            self.loop.wake.reset()


class VoiceLoop:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        audio = cfg["audio"]
        self.mic = Microphone(audio["sample_rate"], audio["input_device"])
        self.speaker = Speaker(audio["output_device"])
        self.wake = WakeWord(cfg["wake"]["model"], cfg["wake"]["threshold"])
        self.stt = Transcriber(cfg["stt"]["model"], cfg["stt"]["compute_type"])
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
        if threshold and threshold != self.wake.threshold:
            self.wake.threshold = threshold
            log.info("wake threshold %.2f", threshold)
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
        self.mic.start()
        self._render_ack()   # also warms up the TTS graph
        log.info("listening for wake word (barge-in %s)", "on" if self.cfg["audio"]["barge_in"] else "off")
        self.events.send("state", state="idle")
        while True:
            frame = self.mic.read()
            if self.muted:
                continue   # frames are still drained so unmuting starts fresh
            if (score := self.wake.detect(frame)) is not None:
                log.info("wake word (%.2f)", score)
                self.wake.reset()
                if self.alarm.active:
                    # Like a smart speaker: saying the name silences the alarm.
                    self.alarm.silence()
                    self.core.stop_ringing()
                self.converse()
                self.wake.reset()
                self.mic.flush()
                self.events.send("state", state="idle")

    def converse(self) -> None:
        """One wake: a request, plus follow-ups while Taco keeps asking questions
        or keeps being interrupted."""
        start_timeout = self.cfg["listen"]["start_timeout_s"]
        while True:
            self._play_and_wait(WAKE_CHIME, 22050)
            self.events.send("state", state="listening")
            audio = record_request(
                self.mic,
                start_timeout_s=start_timeout,
                end_silence_s=self.cfg["listen"]["end_silence_s"],
                max_seconds=self.cfg["listen"]["max_seconds"],
                aggressiveness=self.cfg["listen"]["vad_aggressiveness"],
            )
            if audio is None:
                log.info("no speech after wake")
                return

            self.events.send("state", state="thinking")
            t0 = time.monotonic()
            text = self.stt.transcribe(audio)
            log.info("heard (%.2fs): %r", time.monotonic() - t0, text)
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
            # A reply that ends in a question gets a follow-up turn, no wake word.
            if not reply.rstrip().endswith("?"):
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
        self.mic.flush()
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
        self.mic.flush()  # discard Taco's own voice picked up by the mic
        log.info("reply done in %.2fs: %r", time.monotonic() - t0, " ".join(spoken))
        # A cancelled reply never invites a follow-up question.
        return ("" if self.cancel.is_set() else " ".join(spoken)), barge.heard

    def _play_and_wait(self, audio, rate) -> None:
        self.speaker.play(audio, rate)
        self.speaker.wait()
        self.mic.flush()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    VoiceLoop(load_config()).run()


if __name__ == "__main__":
    main()

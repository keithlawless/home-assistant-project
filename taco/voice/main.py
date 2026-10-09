"""taco-voice: the always-on voice loop.

wake word -> chime -> record -> STT -> Hermes (streamed) -> TTS -> speaker

Half-duplex for now: the mic is ignored while Taco speaks, because the
current directional mic has no echo cancellation. With an AEC speakerphone
this becomes barge-in (Phase 5).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import httpx

from taco.config import load_config, read_env_value

from .alarm import AlarmPlayer
from .audio import Microphone, Speaker, tone
from .core_link import CoreLink
from .hermes import HermesClient
from .listen import record_request
from .stt import Transcriber
from .tts import create_engine
from .wake import WakeWord

log = logging.getLogger("taco.voice")

WAKE_CHIME = tone([660, 880], duration=0.15, volume=0.4)
ERROR_REPLY = "Sorry, I'm having trouble reaching my brain right now."


class Events:
    """Fire-and-forget state updates to taco-core for the screen.
    taco-core may not be running (Phase 1), so failures are ignored."""

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


class VoiceLoop:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        audio = cfg["audio"]
        self.mic = Microphone(audio["sample_rate"], audio["input_device"])
        self.speaker = Speaker(audio["output_device"])
        self.wake = WakeWord(cfg["wake"]["model"], cfg["wake"]["threshold"])
        self.stt = Transcriber(cfg["stt"]["model"], cfg["stt"]["compute_type"])
        self.tts = create_engine(cfg["tts"]["engine"], cfg["tts"]["voice"])
        h = cfg["hermes"]
        self.hermes = HermesClient(
            h["url"], read_env_value(h["env_file"], "API_SERVER_KEY"), h["model"],
            h["timeout_s"], h["history_ttl_s"],
        )
        self.events = Events(cfg["core"]["url"])
        self._tts_lock = threading.Lock()
        self.cancel = threading.Event()   # set by the screen's stop button
        self.alarm = AlarmPlayer(Speaker(audio["output_device"]), self.synthesize)
        self.core = CoreLink(cfg["core"]["url"], self._on_core_message)

    def synthesize(self, text: str):
        # The alarm thread and the conversation share one TTS engine.
        with self._tts_lock:
            return self.tts.synthesize(text)

    def _on_core_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") in ("timers", "snapshot"):
            self.alarm.update(msg.get("timers") or [])
        elif msg.get("type") == "voice_command" and msg.get("command") == "stop":
            log.info("stop requested")
            self.cancel.set()
            self.speaker.stop()

    def run(self) -> None:
        self.mic.start()
        self._warm_up()
        log.info("listening for wake word")
        self.events.send("state", state="idle")
        while True:
            frame = self.mic.read()
            if (score := self.wake.detect(frame)) is not None:
                log.info("wake word (%.2f)", score)
                if self.alarm.active:
                    # Like a smart speaker: saying the name silences the alarm.
                    self.alarm.silence()
                    self.core.stop_ringing()
                self.converse()
                self.wake.reset()
                self.mic.flush()
                self.events.send("state", state="idle")

    def _warm_up(self) -> None:
        # The first synthesis loads the ONNX graph; do it before anyone is
        # waiting. The acknowledgment is pre-rendered so it plays instantly.
        ack = self.cfg["listen"]["ack_phrase"]
        self.ack_audio = self.synthesize(ack) if ack else None
        log.info("warm-up done")

    def converse(self) -> None:
        """One wake: a request, plus follow-ups while Taco keeps asking questions."""
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
                reply = self.say(fast.get("speech") or "") if not fast.get("silent") else ""
            else:
                if self.ack_audio:
                    # Tell the user they were understood; Hermes is already
                    # being asked while this plays.
                    self.speaker.play(*self.ack_audio)
                reply = self.respond(text, t0)
            # A reply that ends in a question gets a follow-up turn, no wake word.
            if not reply.rstrip().endswith("?"):
                return
            start_timeout = self.cfg["listen"]["followup_s"]

    def say(self, text: str) -> str:
        """Speak a complete reply (fast-path answers)."""
        if text:
            self.events.send("state", state="speaking")
            self.events.send("reply", text=text)
            self.speaker.play(*self.synthesize(text))
            self.speaker.wait()
            self.mic.flush()
        return text

    def respond(self, text: str, t0: float) -> str:
        spoken: list[str] = []
        first_audio = None
        self.cancel.clear()
        try:
            for sentence in self.hermes.ask(text, on_tool=lambda e: self.events.send("tool", **e)):
                if self.cancel.is_set():
                    log.info("reply cancelled")
                    break  # closes the stream; Hermes stops generating
                audio, rate = self.synthesize(sentence)
                if first_audio is None:
                    first_audio = time.monotonic() - t0
                    log.info("first audio after %.2fs", first_audio)
                    self.events.send("state", state="speaking")
                self.speaker.play(audio, rate)
                spoken.append(sentence)
                self.events.send("reply", text=" ".join(spoken))
        except httpx.HTTPError as e:
            log.error("hermes request failed: %s", e)
            audio, rate = self.synthesize(ERROR_REPLY)
            self.speaker.play(audio, rate)
            spoken = [ERROR_REPLY]
        self.speaker.wait()
        self.mic.flush()  # discard Taco's own voice picked up by the mic
        log.info("reply done in %.2fs: %r", time.monotonic() - t0, " ".join(spoken))
        # A cancelled reply never invites a follow-up question.
        return "" if self.cancel.is_set() else " ".join(spoken)

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

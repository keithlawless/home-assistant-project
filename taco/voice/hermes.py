"""Streaming client for the Hermes Agent API server (OpenAI-compatible).

Yields speakable sentences as the reply streams in, so TTS can start before
the full answer arrives. Hermes sends tool progress as separate
`hermes.tool.progress` SSE events; those go to on_tool, never to speech.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator

import httpx

# Split after sentence-ending punctuation followed by whitespace.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_MIN_SENTENCE_CHARS = 20

_MARKDOWN = [
    (re.compile(r"```.*?```", re.S), " "),          # code blocks: never read aloud
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),       # images
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),   # links -> link text
    (re.compile(r"https?://\S+"), ""),
    (re.compile(r"[*_#>]+"), ""),
    (re.compile(r"^\s*[-•]\s+", re.M), ""),
    (re.compile(r"\s+"), " "),
]


def clean_for_speech(text: str) -> str:
    for pattern, repl in _MARKDOWN:
        text = pattern.sub(repl, text)
    return text.strip()


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Return (complete sentences, remainder). Short fragments stay buffered
    so "Dr." or "1." don't become their own utterance."""
    parts = _SENTENCE_END.split(buffer)
    if len(parts) == 1:
        return [], buffer
    complete, pending = [], ""
    for part in parts[:-1]:
        pending = f"{pending} {part}".strip() if pending else part
        if len(pending) >= _MIN_SENTENCE_CHARS:
            complete.append(pending)
            pending = ""
    remainder = f"{pending} {parts[-1]}".strip() if pending else parts[-1]
    return complete, remainder


class HermesClient:
    def __init__(self, url: str, api_key: str | None, model: str = "hermes",
                 timeout_s: float = 60.0, history_ttl_s: float = 120.0):
        self.url = url.rstrip("/")
        self.model = model
        self.history_ttl_s = history_ttl_s
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(headers=headers, timeout=httpx.Timeout(timeout_s, connect=5.0))
        self._history: list[dict] = []
        self._last_turn = 0.0
        self._response: httpx.Response | None = None

    def _messages(self, text: str) -> list[dict]:
        if time.monotonic() - self._last_turn > self.history_ttl_s:
            self._history = []
        return [*self._history, {"role": "user", "content": text}]

    def ask(self, text: str, on_tool: Callable[[dict], None] | None = None) -> Iterator[str]:
        """Send one user turn; yield cleaned sentences of the reply."""
        messages = self._messages(text)
        payload = {"model": self.model, "stream": True, "messages": messages}
        full_reply, buffer, event = [], "", None

        with self._http.stream("POST", f"{self.url}/chat/completions", json=payload) as resp:
            self._response = resp
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    event = None
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if event == "hermes.tool.progress":
                    if on_tool:
                        on_tool(chunk)
                    continue
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {}).get("content")
                    if not delta:
                        continue
                    full_reply.append(delta)
                    buffer += delta
                    sentences, buffer = split_sentences(buffer)
                    for s in sentences:
                        if spoken := clean_for_speech(s):
                            yield spoken

        self._response = None
        if tail := clean_for_speech(buffer):
            yield tail
        self._history = [*messages, {"role": "assistant", "content": "".join(full_reply).strip()}]
        self._last_turn = time.monotonic()

    def abort(self) -> None:
        """Cancel the in-flight reply from another thread (stop button, barge-in).
        Closing the stream makes ask() raise instead of waiting for Hermes to
        finish a slow tool call."""
        if (resp := self._response) is not None:
            try:
                resp.close()
            except Exception:
                pass

    def reset(self) -> None:
        self._history = []

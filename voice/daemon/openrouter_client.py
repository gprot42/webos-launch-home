#!/usr/bin/env python3
"""OpenRouter chat (OpenAI-compatible SSE). Stdlib only.

Used when ai_provider=openrouter. Listening (STT) and spoken replies (TTS)
still go through xAI or Gemini when those keys are present.
"""

from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from grok_client import ChatResult, SYSTEM_PROMPT, _urlopen_retry

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-6-luna"
DEFAULT_OPENROUTER_STT_MODEL = "openai/gpt-transcribe"
OPENROUTER_MODELS = (
    "openai/gpt-6-luna",
    "openai/gpt-6-sol",
    "google/gemini-3.8-flash",
    "anthropic/claude-sonnet-5",
    "x-ai/grok-4.7",
)
# Popular OpenRouter speech-to-text models (usage ranking, Aug 2026).
OPENROUTER_STT_MODELS = (
    "openai/gpt-transcribe",
    "assemblyai/universal-3-5-pro",
    "meta/muse-voice-transcribe-1.0",
    "mistralai/voxtral-mini-transcribe",
    "nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b",
    "qwen/qwen3-asr-1.7b",
    "deepgram/nova-3",
    "x-ai/grok-stt-1.0",
    "google/chirp-3",
    "openai/whisper-large-v3-turbo",
)


def stream_openrouter_chat(
    api_key: str,
    user_text: str,
    *,
    model: str = DEFAULT_OPENROUTER_MODEL,
    on_token: Optional[Callable[[str], None]] = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> ChatResult:
    """Stream a chat answer from OpenRouter (OpenAI-compatible SSE)."""
    key = (api_key or "").strip()
    if not key:
        raise RuntimeError("OpenRouter API key is missing")
    model_use = (model or DEFAULT_OPENROUTER_MODEL).strip() or DEFAULT_OPENROUTER_MODEL
    body = {
        "model": model_use,
        "stream": True,
        "max_tokens": 256,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    }
    req = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "HTTP-Referer": "https://github.com/gprot42/webos-launch-home",
            "X-Title": "Launch Home",
            "Connection": "close",
        },
        method="POST",
    )
    answer_parts: list[str] = []
    t0 = time.time()
    first_token_logged = False
    last_err: Optional[BaseException] = None
    for attempt in range(1, 3):
        answer_parts = []
        first_token_logged = False
        try:
            print(
                "[voice] openrouter http open model=%s attempt=%d"
                % (model_use, attempt),
                flush=True,
            )
            with _urlopen_retry(req, timeout=25, retries=0) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if not delta:
                        msg = (choices[0].get("message") or {}).get("content") or ""
                        delta = msg if isinstance(msg, str) else ""
                    if delta:
                        if not first_token_logged:
                            first_token_logged = True
                            print(
                                "[voice] openrouter first token +%.0fms model=%s"
                                % ((time.time() - t0) * 1000.0, model_use),
                                flush=True,
                            )
                        answer_parts.append(delta)
                        if on_token:
                            on_token(delta)
            last_err = None
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                "OpenRouter HTTP %s: %s" % (exc.code, detail[:300])
            ) from exc
        except (TimeoutError, socket.timeout, OSError) as exc:
            last_err = exc
            print(
                "[voice] openrouter stream timeout/error attempt=%d: %s"
                % (attempt, exc),
                flush=True,
            )
            if attempt < 2 and not answer_parts:
                time.sleep(0.2)
                continue
            if answer_parts:
                break
            raise RuntimeError(
                "OpenRouter timed out — check internet and try again"
            ) from exc
    if last_err is not None and not answer_parts:
        raise RuntimeError(
            "OpenRouter timed out — check internet and try again"
        ) from last_err
    print(
        "[voice] openrouter done +%.0fms chars=%d model=%s"
        % ((time.time() - t0) * 1000.0, len("".join(answer_parts)), model_use),
        flush=True,
    )
    return ChatResult(
        text="".join(answer_parts).strip(), citations=[], used_search=False
    )


def transcribe_openrouter(
    api_key: str,
    pcm: bytes,
    *,
    model: str = DEFAULT_OPENROUTER_STT_MODEL,
    language: str = "en",
    sample_rate: int = 16000,
) -> str:
    """Speech-to-text via OpenRouter /audio/transcriptions."""
    key = (api_key or "").strip()
    if not pcm or not key:
        return ""
    from grok_client import pcm16le_to_wav

    wav = pcm16le_to_wav(pcm, sample_rate=sample_rate)
    model_use = (model or DEFAULT_OPENROUTER_STT_MODEL).strip() or DEFAULT_OPENROUTER_STT_MODEL
    body = {
        "model": model_use,
        "input_audio": {
            "data": base64.b64encode(wav).decode("ascii"),
            "format": "wav",
        },
    }
    if language:
        body["language"] = language
    req = urllib.request.Request(
        OPENROUTER_STT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/gprot42/webos-launch-home",
            "X-Title": "Launch Home",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            "OpenRouter STT HTTP %s: %s" % (exc.code, detail[:300])
        ) from exc
    text = ""
    if isinstance(data, dict):
        text = str(data.get("text") or "").strip()
    return text


class OpenRouterSttSession:
    """Batch STT via OpenRouter; same hold-then-transcribe pattern as Gemini."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_OPENROUTER_STT_MODEL,
        language: str = "en",
    ) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_OPENROUTER_STT_MODEL
        self.language = language or "en"
        self._buf = bytearray()
        self._last_partial = ""
        self._segments: list[str] = []
        self._ws = _DummyWs()

    def connect(self) -> None:
        if not self.api_key:
            raise RuntimeError("OpenRouter API key not set")

    def send_pcm(self, chunk: bytes) -> None:
        if chunk:
            self._buf.extend(chunk)

    def current_text(self) -> str:
        return (" ".join(s for s in self._segments if s) or self._last_partial).strip()

    def poll_events(self, on_partial, *, timeout: float = 0.05) -> None:
        return

    def finish(self) -> str:
        pcm = bytes(self._buf)
        self._buf.clear()
        if not pcm:
            return self.current_text()
        print(
            "[voice] openrouter STT upload bytes=%d model=%s"
            % (len(pcm), self.model),
            flush=True,
        )
        text = transcribe_openrouter(
            self.api_key,
            pcm,
            model=self.model,
            language=self.language,
        )
        if text:
            self._segments = [text]
            self._last_partial = ""
        print(
            "[voice] openrouter stt finish: result=%r"
            % (text[:120] if text else ""),
            flush=True,
        )
        return text

    def close(self) -> None:
        self._buf.clear()
        self._ws = None


class _DummyWs:
    def __init__(self) -> None:
        self._sock = object()

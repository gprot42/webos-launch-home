#!/usr/bin/env python3
"""Google Gemini STT (audio→text) and streaming chat (stdlib only).

Used when ai_provider=gemini. PCM from the Magic Remote is wrapped as WAV and
sent to Gemini 3.5 Transcribe (Interactions API) or generateContent for chat
models; chat uses streamGenerateContent (SSE).
"""

from __future__ import annotations

import base64
import json
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from grok_client import ChatResult, SYSTEM_PROMPT

GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_STT_MODEL = "gemini-3.5-transcribe"
# 3.8 Flash-Lite TTS replaces the deprecated gemini-3.1-flash-tts-preview for
# voice agents. It returns WAV (RIFF) by default; _pcm_to_playable passes it on.
GEMINI_TTS_MODEL = "gemini-3.8-flash-lite-tts"
GEMINI_TTS_VOICE = "Kore"
GEMINI_TTS_RATE = 24000
# Models offered in settings (id → label not needed server-side).
GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.5-live-translate-preview",
)
GEMINI_STT_MODELS = (
    "gemini-3.5-transcribe",
    "gemini-3.8-flash",
)
# Settings language codes → BCP-47 hints for Gemini 3.5 Transcribe.
_STT_LANG_BCP47 = {
    "en": "en-US",
    "es": "es-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-BR",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "zh": "cmn-Hans-CN",
    "vi": "vi-VN",
}


def pcm16le_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw s16le mono PCM in a minimal WAV container for Gemini."""
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,  # PCM fmt chunk size
        1,  # audio format PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )
    return header + pcm


def _http_json(
    url: str,
    body: dict,
    *,
    timeout: float = 120,
    stream: bool = False,
) -> urllib.request.addinfourl:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _extract_text_from_generate(resp: dict) -> str:
    parts_out: list[str] = []
    for cand in resp.get("candidates") or []:
        content = cand.get("content") or {}
        for part in content.get("parts") or []:
            t = part.get("text")
            if t:
                parts_out.append(t)
    return "".join(parts_out).strip()


def _is_transcribe_model(model: str) -> bool:
    name = (model or "").strip().lower()
    return name.endswith("-transcribe") or name.endswith("-transcribe-preview")


def _stt_language_codes(language: str) -> list[str]:
    raw = (language or "").strip()
    if not raw:
        return []
    if "-" in raw or "_" in raw:
        return [raw.replace("_", "-")]
    mapped = _STT_LANG_BCP47.get(raw.lower())
    return [mapped] if mapped else [raw]


def _clean_transcript(text: str) -> str:
    cleaned = (text or "").strip().strip('"').strip("'")
    if cleaned.lower() in ("", '""', "''", "(no speech)", "no speech", "n/a"):
        return ""
    return cleaned


def _extract_interaction_text(payload: dict) -> str:
    """Pull transcript text from an Interactions API response."""
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    outputs = payload.get("outputs") or payload.get("output") or []
    if isinstance(outputs, dict):
        outputs = [outputs]
    for item in outputs:
        if isinstance(item, str) and item.strip():
            parts.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        t = item.get("text") or item.get("output_text")
        if isinstance(t, str) and t.strip():
            parts.append(t.strip())
        for part in item.get("content") or item.get("parts") or []:
            if isinstance(part, str) and part.strip():
                parts.append(part.strip())
            elif isinstance(part, dict):
                pt = part.get("text")
                if isinstance(pt, str) and pt.strip():
                    parts.append(pt.strip())
    if parts:
        return " ".join(parts).strip()
    for step in payload.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for content in step.get("content") or []:
            if isinstance(content, dict):
                t = content.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
    return " ".join(parts).strip()


def _gemini_transcribe_generate(
    api_key: str,
    pcm: bytes,
    *,
    model: str,
    language: str,
    sample_rate: int,
) -> str:
    """One-shot speech-to-text via Gemini audio understanding (generateContent)."""
    wav = pcm16le_to_wav(pcm, sample_rate=sample_rate)
    b64 = base64.b64encode(wav).decode("ascii")
    lang_hint = language or "en"
    prompt = (
        "Transcribe the spoken audio to plain text. "
        f"The speech language is likely '{lang_hint}'. "
        "Return ONLY the transcript with no quotes, labels, or commentary. "
        "If there is no speech, return an empty string."
    )
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "audio/wav",
                            "data": b64,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 1024,
        },
    }
    url = f"{GEMINI_API_ROOT}/models/{model}:generateContent?key={api_key}"
    try:
        with _http_json(url, body, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gemini STT HTTP {exc.code}: {detail[:300]}") from exc
    return _clean_transcript(_extract_text_from_generate(data))


def _gemini_transcribe_interactions(
    api_key: str,
    pcm: bytes,
    *,
    model: str,
    language: str,
    sample_rate: int,
    keyterms: Optional[tuple[str, ...] | list[str]] = None,
) -> str:
    """Dedicated Gemini 3.5 Transcribe via the Interactions API."""
    wav = pcm16le_to_wav(pcm, sample_rate=sample_rate)
    b64 = base64.b64encode(wav).decode("ascii")
    transcription_config: dict = {
        "mode": {"type": "smart"},
    }
    langs = _stt_language_codes(language)
    if langs:
        transcription_config["language_codes"] = langs
    vocab = [str(t).strip() for t in (keyterms or ()) if str(t).strip()]
    if vocab:
        transcription_config["custom_vocabulary"] = vocab[:100]
    body = {
        "model": model,
        "input": [
            {
                "type": "audio",
                "data": b64,
                "mime_type": "audio/wav",
            }
        ],
        "generation_config": {
            "transcription_config": transcription_config,
        },
    }
    url = GEMINI_API_ROOT + "/interactions"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "Api-Revision": "2026-05-20",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"gemini transcribe HTTP {exc.code}: {detail[:300]}"
        ) from exc
    return _clean_transcript(
        _extract_interaction_text(payload if isinstance(payload, dict) else {})
    )


def gemini_transcribe(
    api_key: str,
    pcm: bytes,
    *,
    model: str = DEFAULT_GEMINI_STT_MODEL,
    language: str = "en",
    sample_rate: int = 16000,
    keyterms: Optional[tuple[str, ...] | list[str]] = None,
) -> str:
    """One-shot speech-to-text. Uses Gemini 3.5 Transcribe when selected."""
    if not pcm or not api_key:
        return ""
    stt_model = (model or DEFAULT_GEMINI_STT_MODEL).strip() or DEFAULT_GEMINI_STT_MODEL
    if _is_transcribe_model(stt_model):
        try:
            return _gemini_transcribe_interactions(
                api_key,
                pcm,
                model=stt_model,
                language=language,
                sample_rate=sample_rate,
                keyterms=keyterms,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "[voice] gemini 3.5 transcribe failed (%s); "
                "falling back to %s audio understanding" % (exc, DEFAULT_GEMINI_MODEL),
                flush=True,
            )
            return _gemini_transcribe_generate(
                api_key,
                pcm,
                model=DEFAULT_GEMINI_MODEL,
                language=language,
                sample_rate=sample_rate,
            )
    return _gemini_transcribe_generate(
        api_key,
        pcm,
        model=stt_model,
        language=language,
        sample_rate=sample_rate,
    )


def stream_gemini_chat(
    api_key: str,
    user_text: str,
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    on_token: Optional[Callable[[str], None]] = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> ChatResult:
    """Stream a chat answer from Gemini (SSE streamGenerateContent)."""
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_text}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 1024,
        },
    }
    url = (
        f"{GEMINI_API_ROOT}/models/{model}:streamGenerateContent"
        f"?alt=sse&key={api_key}"
    )
    answer_parts: list[str] = []
    try:
        with _http_json(url, body, timeout=120) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                text = _extract_text_from_generate(chunk)
                # streamGenerateContent often returns cumulative or delta;
                # handle both: if new text starts with joined so far, take suffix.
                if not text:
                    continue
                joined = "".join(answer_parts)
                if text.startswith(joined) and len(text) > len(joined):
                    delta = text[len(joined) :]
                    answer_parts.append(delta)
                    if on_token and delta:
                        on_token(delta)
                elif text != joined:
                    # Likely a pure delta chunk
                    answer_parts.append(text)
                    if on_token:
                        on_token(text)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gemini chat HTTP {exc.code}: {detail[:300]}") from exc

    full = "".join(answer_parts).strip()
    # Deduplicate if we accidentally doubled cumulative chunks
    if not full:
        # Non-stream fallback
        url2 = f"{GEMINI_API_ROOT}/models/{model}:generateContent?key={api_key}"
        with _http_json(url2, body, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        full = _extract_text_from_generate(data)
        if full and on_token:
            on_token(full)
    return ChatResult(text=full, citations=[], used_search=False)


def _b64_audio_bytes(obj: object) -> bytes:
    if not isinstance(obj, dict):
        return b""
    data = obj.get("data") or obj.get("audio") or ""
    if isinstance(data, dict):
        data = data.get("data") or ""
    if not isinstance(data, str) or not data.strip():
        return b""
    try:
        return base64.b64decode(data)
    except Exception:
        return b""


def _extract_interaction_pcm(payload: dict) -> bytes:
    """Pull PCM bytes from an Interactions API response."""
    direct = _b64_audio_bytes(payload.get("output_audio") or {})
    if direct:
        return direct
    outputs = payload.get("outputs") or payload.get("output") or []
    if isinstance(outputs, dict):
        outputs = [outputs]
    for item in outputs:
        if not isinstance(item, dict):
            continue
        audio = item.get("audio") or item.get("output_audio") or item
        pcm = _b64_audio_bytes(audio)
        if pcm:
            return pcm
        for part in item.get("content") or item.get("parts") or []:
            if not isinstance(part, dict):
                continue
            pcm = _b64_audio_bytes(part.get("inline_data") or part.get("inlineData") or part)
            if pcm:
                return pcm
    return b""


def _pcm_to_playable(pcm: bytes, *, sample_rate: int = GEMINI_TTS_RATE) -> bytes:
    if not pcm:
        return b""
    if pcm[:4] == b"RIFF":
        return pcm
    from grok_client import pcm16le_to_mp3, pcm16le_to_wav

    mp3 = pcm16le_to_mp3(pcm, sample_rate=sample_rate)
    if mp3:
        return mp3
    return pcm16le_to_wav(pcm, sample_rate=sample_rate)


def synthesize_gemini_speech(
    api_key: str,
    text: str,
    *,
    voice: str = GEMINI_TTS_VOICE,
) -> bytes:
    """Speak *text* with Gemini TTS (separate from the Flash answer model, which is text-only).

    Returns MP3 when ffmpeg is available, otherwise WAV. Empty on failure.
    """
    spoken = (text or "").strip()
    key = (api_key or "").strip()
    if not spoken or not key:
        return b""
    voice_name = (voice or GEMINI_TTS_VOICE).strip() or GEMINI_TTS_VOICE
    body = {
        "model": GEMINI_TTS_MODEL,
        "input": spoken,
        "response_format": {"type": "audio"},
        "generation_config": {
            "speech_config": [{"voice": voice_name}],
        },
    }
    url = GEMINI_API_ROOT + "/interactions"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
            "Api-Revision": "2026-05-20",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(
            "[voice] gemini TTS HTTP %s: %s" % (exc.code, detail[:300]),
            flush=True,
        )
        return b""
    except Exception as exc:  # noqa: BLE001
        print("[voice] gemini TTS failed: %s" % exc, flush=True)
        return b""
    pcm = _extract_interaction_pcm(payload if isinstance(payload, dict) else {})
    if not pcm:
        print("[voice] gemini TTS empty audio payload", flush=True)
        return b""
    out = _pcm_to_playable(pcm)
    print(
        "[voice] gemini TTS bytes=%d voice=%s" % (len(out), voice_name),
        flush=True,
    )
    return out


class GeminiSttSession:
    """STT session compatible with GrokSttSession interface used by the daemon.

    Buffers PCM during the hold; transcribes once on finish() via Gemini
    3.5 Transcribe (Interactions API) or generateContent for chat models.
    Does not support true live partials (no streaming STT on this path).
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_GEMINI_STT_MODEL,
        language: str = "en",
        keyterms: Optional[tuple[str, ...] | list[str]] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_GEMINI_STT_MODEL
        self.language = language or "en"
        self.keyterms = keyterms or ()
        self._buf = bytearray()
        self._last_partial = ""
        self._segments: list[str] = []
        # Compatibility with GrokSttSession checks in the daemon.
        self._ws = _DummyWs()

    def connect(self) -> None:
        # No long-lived socket; ready immediately.
        if not self.api_key:
            raise RuntimeError("Gemini API key not set")

    def send_pcm(self, chunk: bytes) -> None:
        if chunk:
            self._buf.extend(chunk)

    def current_text(self) -> str:
        return (self._joined() or self._last_partial).strip()

    def _joined(self) -> str:
        return " ".join(s for s in self._segments if s).strip()

    def poll_events(
        self,
        on_partial: Callable[[str, bool], None],
        *,
        timeout: float = 0.05,
    ) -> None:
        # No interim events on the batch Gemini path.
        return

    def finish(self) -> str:
        pcm = bytes(self._buf)
        self._buf.clear()
        if not pcm:
            return self.current_text()
        print(
            "[voice] gemini STT upload bytes=%d model=%s"
            % (len(pcm), self.model),
            flush=True,
        )
        try:
            text = gemini_transcribe(
                self.api_key,
                pcm,
                model=self.model,
                language=self.language,
                keyterms=self.keyterms,
            )
        except Exception as exc:  # noqa: BLE001
            print("[voice] gemini STT failed: %s" % exc, flush=True)
            raise
        if text:
            self._segments = [text]
            self._last_partial = ""
        print(
            "[voice] gemini stt finish: result=%r" % (text[:120] if text else ""),
            flush=True,
        )
        return text

    def close(self) -> None:
        self._buf.clear()
        self._ws = None


class _DummyWs:
    """Satisfies `stt._ws is not None and stt._ws._sock is not None` checks."""

    def __init__(self) -> None:
        self._sock = object()

#!/usr/bin/env python3
"""Grok STT WebSocket + voice realtime + chat/TTS (stdlib only)."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional
from urllib.parse import quote, urlencode

PCM_CHUNK_BYTES = 3200
STT_HOST = "api.x.ai"

XAI_CREDITS_MESSAGE = (
    "xAI API credits are used up, or this team hit its monthly spending limit. "
    "Add credits or raise the limit at console.x.ai. "
    "A SuperGrok Heavy subscription does not replace API credits for TV voice."
)


def classify_xai_http_error(text: str) -> Optional[str]:
    """Return a user-facing message for xAI HTTP failures, or None."""
    raw = str(text or "")
    if not raw:
        return None
    low = raw.lower()
    if (
        "used all available credits" in low
        or "monthly spending limit" in low
        or ("spending limit" in low and "team" in low)
        or ("credits" in low and "purchase more" in low)
    ):
        return XAI_CREDITS_MESSAGE
    # 403 body is JSON; extract error field when present.
    start = raw.find("{")
    if start >= 0:
        try:
            obj = json.loads(raw[start : raw.find("}", start) + 1])
        except Exception:
            obj = None
        if isinstance(obj, dict):
            err = str(obj.get("error") or obj.get("code") or "")
            if err:
                low_err = err.lower()
                if "credit" in low_err or "spending limit" in low_err:
                    return XAI_CREDITS_MESSAGE
    return None
STT_REST_URL = "https://api.x.ai/v1/stt"
# Latest xAI transcription model (replaces the legacy "grok-stt" id).
STT_MODEL = "grok-voice-transcribe-2.0"
CHAT_URL = "https://api.x.ai/v1/chat/completions"
RESPONSES_URL = "https://api.x.ai/v1/responses"
TTS_URL = "https://api.x.ai/v1/tts"
# Speech-to-Speech (text-in path after local STT): flagship is think-fast-2.0.
DEFAULT_VOICE_MODEL = "grok-voice-think-fast-2.0"
VOICE_OUTPUT_RATE = 24000
# webOS plays each chunk as a separate <audio> element. Emitting early crumbs
# caused missing opening words (seq skip / mid-start) and stutter. Hold all
# PCM and emit one continuous MP3 when the voice turn finishes.
VOICE_PCM_FIRST_FLUSH_BYTES = int(VOICE_OUTPUT_RATE * 60.0) * 2  # effectively final-only
VOICE_PCM_FLUSH_BYTES = int(VOICE_OUTPUT_RATE * 60.0) * 2

# LG webOS often has broken/slow IPv6 to Cloudflare (api.x.ai). Python then
# sits ~10–25s on AF_INET6 before TTS/STT works. Prefer IPv4 for x.ai hosts.
_ORIG_CREATE_CONNECTION = socket.create_connection


def _is_xai_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    h = host.lower().rstrip(".")
    return (
        h == "api.x.ai"
        or h.endswith(".x.ai")
        or h == "openrouter.ai"
        or h.endswith(".openrouter.ai")
    )


def _create_connection_prefer_ipv4(address, *args, **kwargs):
    """Like socket.create_connection, but IPv4-first for xAI hosts."""
    host = address[0] if address else None
    if not _is_xai_host(host):
        return _ORIG_CREATE_CONNECTION(address, *args, **kwargs)
    port = address[1]
    timeout = kwargs.get("timeout", args[0] if args else socket._GLOBAL_DEFAULT_TIMEOUT)
    source_address = kwargs.get(
        "source_address", args[1] if len(args) > 1 else None
    )
    last_err: Optional[BaseException] = None
    # AF_INET only first — curl IPv4 TTS is ~1s; IPv6 hangs ~10–25s on this TV.
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        last_err = exc
        infos = []
    for res in infos:
        af, socktype, proto, _canon, sa = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT and timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except OSError as exc:
            last_err = exc
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    # Last resort: original (may try IPv6).
    try:
        return _ORIG_CREATE_CONNECTION(address, *args, **kwargs)
    except Exception:
        if last_err is not None:
            raise last_err
        raise


# Patch once at import — urllib, MiniWebSocket, and http.client all use this.
socket.create_connection = _create_connection_prefer_ipv4  # type: ignore[assignment]


def _urlopen_retry(req: urllib.request.Request, *, timeout: float, retries: int = 1):
    """urlopen with one retry on 429/5xx or transient network errors."""
    last: Optional[BaseException] = None
    attempts = max(1, int(retries) + 1)
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504) or i + 1 >= attempts:
                raise
            time.sleep(0.6 * (i + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if i + 1 >= attempts:
                raise
            time.sleep(0.6 * (i + 1))
    raise RuntimeError("urlopen_retry exhausted: %s" % last)

# The 21 expressive xAI TTS voices. `iris` is the default TV assistant voice.
# Voice IDs are case-insensitive on the API side.
TTS_VOICES = (
    "carina", "zagan", "helix", "orion", "luna", "iris", "altair",
    "zenith", "perseus", "helios", "lux", "kepler", "rigel", "cosmo",
    "celeste", "ursa", "sirius", "lumen", "castor", "naksh", "atlas",
)
DEFAULT_TTS_VOICE = "iris"
# Spoken TV answers stay short — overlay and TTS both show this same text.
SYSTEM_PROMPT = (
    "You are the Launch Home voice assistant, a concise TV voice assistant powered by Grok. "
    "Limit answer length: one short sentence of about 12–24 words for ordinary "
    "facts unless the user asks for detail. Never pad with preambles or lists. "
    "Be direct and helpful. Never use markdown (no **, #, bullets, or links) — "
    "answers are spoken aloud on a TV. "
    "For poems, songs, jokes, or stories: keep them short for voice "
    "(about 4 short lines unless the user asks for longer). For jokes, always "
    "give a complete setup and punchline. Never answer with a lone insult, "
    "slur, or crude word. Start the first line right away. "
    "When asked to count or recite numbers in any language: start counting "
    "immediately in that language (un, deux… / ichi, ni…), no long preamble. "
    "Keep the list brisk for speech. "
    "For questions about your name, identity, or capabilities, answer from "
    "who you are — do not use web search. Use web search only for current "
    "events, live facts, sports scores, prices, or other things that need "
    "up-to-date information."
)

# Queries that should never hit the live web tool (pure LLM / chitchat / creative).
_NO_WEB_SEARCH_RE = re.compile(
    r"(?i)\b("
    r"your name|what(?:'s| is) your name|who are you|what are you|"
    r"who is this|what can you do|your capabilities|"
    r"hello|hi there|hey there|good (morning|afternoon|evening|night)|"
    r"how are you|thank you|thanks|"
    r"poem|poems|poetry|haiku|limerick|rhyme|rhymes|"
    r"joke|jokes|riddle|story|stories|fairy tale|"
    r"sing|song|lyrics|rap for me"
    r")\b"
)

# Only these patterns take the slower /v1/responses + web_search path in auto mode.
# Everything else uses fast /v1/chat/completions (big win for poems & chat).
_NEEDS_WEB_SEARCH_RE = re.compile(
    r"(?i)\b("
    r"news|headline|headlines|today'?s|this (morning|afternoon|evening|week|month)|"
    r"right now|current|latest|breaking|"
    r"score|scores|stock|stocks|share price|crypto|bitcoin|"
    r"who won|what happened|election|weather forecast|"
    r"search (for|the)|look up|google|find (me )?(info|information|out)|"
    r"how much (is|does|do)|price of|cost of|"
    r"when is the next|schedule for|opening hours"
    r")\b"
)


def _query_needs_web_search(text: str) -> bool:
    """True only when the slow responses+web_search path is worth it.

    Auto mode used to send nearly every query (including poems) through
    /v1/responses with tools — even when search was never invoked — which
    delayed first tokens and first TTS by seconds. Creative/chitchat and
    ordinary knowledge questions now stay on fast chat completions.
    """
    t = (text or "").strip()
    if not t:
        return False
    if _NO_WEB_SEARCH_RE.search(t):
        return False
    return bool(_NEEDS_WEB_SEARCH_RE.search(t))


_MARKDOWN_NOISE_RE = re.compile(
    r"(\*\*|__|`{1,3}|#{1,6}\s*|^\s*[-*+]\s+|^\s*\d+\.\s+)",
    re.MULTILINE,
)


def strip_for_speech(text: str) -> str:
    """Remove markdown / symbols that sound awful when read by TTS."""
    t = (text or "").strip()
    if not t:
        return ""
    t = _MARKDOWN_NOISE_RE.sub("", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)  # [label](url) -> label
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


@dataclass
class ChatResult:
    """Answer text plus where it came from (Grok's model vs. live web search)."""

    text: str
    citations: list
    used_search: bool


@dataclass
class VoiceAnswerResult:
    """Realtime voice answer: transcript + how much audio was pushed."""

    text: str
    audio_chunks: int = 0
    audio_bytes: int = 0
    citations: list = field(default_factory=list)
    used_search: bool = False


@dataclass
class SttConfig:
    api_key: str
    language: str = "en"
    keyterms: tuple[str, ...] = ()
    # 0 disables Grok's semantic end-of-turn detection. With push-to-talk the
    # button release is the turn boundary, so smart_turn only causes the last
    # word to be dropped after a micro-pause. Set >0 only for open-mic use.
    smart_turn: float = 0.0


class MiniWebSocket:
    """Minimal blocking RFC 6455 WebSocket client (no external deps)."""

    def __init__(self, host: str, path: str, headers: dict[str, str]) -> None:
        self.host = host
        self.path = path
        self.headers = headers
        self._sock: Optional[socket.socket] = None

    def connect(self) -> None:
        ctx = ssl.create_default_context()
        # TV WAN can be slow; 3s timed out mid-turn ("STT connect failed:
        # The read operation timed out") and forced a bad second pass ("Cue").
        # Keep connect snappy on TV WAN — slow TLS used to sit 8–16s and miss
        # the whole capture window. Fail fast so REST STT can take over.
        raw = socket.create_connection((self.host, 443), timeout=4.0)
        self._sock = ctx.wrap_socket(raw, server_hostname=self.host)
        self._sock.settimeout(4.0)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        hdrs = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in self.headers.items():
            hdrs.append(f"{k}: {v}")
        hdrs.append("")
        hdrs.append("")
        self._sock.sendall("\r\n".join(hdrs).encode("utf-8"))
        resp = self._read_http_response()
        if "101" not in resp.split("\r\n", 1)[0]:
            raise ConnectionError(
                "WebSocket handshake failed: %s" % resp[:1200]
            )

    def _read_http_response(self) -> str:
        assert self._sock is not None
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self._sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        header, sep, rest = bytes(data).partition(b"\r\n\r\n")
        text = header.decode("utf-8", errors="replace")
        if not sep:
            return text
        content_len = 0
        for line in text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    content_len = int(line.split(":", 1)[1].strip() or "0")
                except ValueError:
                    content_len = 0
                break
        body = bytearray(rest)
        while content_len > 0 and len(body) < content_len:
            chunk = self._sock.recv(min(4096, content_len - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if body:
            text = text + "\r\n\r\n" + body.decode("utf-8", errors="replace")
        return text

    def _masked_frame(self, opcode: int, payload: bytes) -> bytes:
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", opcode, 0x80 | length)
        else:
            header = struct.pack("!BBH", opcode, 0xFE, length)
        return header + mask + masked

    def send_binary(self, payload: bytes) -> None:
        assert self._sock is not None
        # Bound send so a stalled STT peer cannot freeze the capture worker.
        prev = self._sock.gettimeout()
        try:
            self._sock.settimeout(5.0)
            self._sock.sendall(self._masked_frame(0x82, payload))
        finally:
            try:
                self._sock.settimeout(prev)
            except OSError:
                pass

    def send_text(self, text: str) -> None:
        assert self._sock is not None
        prev = self._sock.gettimeout()
        try:
            self._sock.settimeout(5.0)
            self._sock.sendall(self._masked_frame(0x81, text.encode("utf-8")))
        finally:
            try:
                self._sock.settimeout(prev)
            except OSError:
                pass

    def recv_json(self) -> dict:
        assert self._sock is not None
        while True:
            hdr = self._recv_exact(2)
            opcode = hdr[0] & 0x0F
            masked = bool(hdr[1] & 0x80)
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask_key = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by server")
            if opcode == 0x9:
                self._pong(payload)
                continue
            if opcode in (0x1, 0x2):
                text = payload.decode("utf-8", errors="replace")
                return json.loads(text)

    def _pong(self, payload: bytes) -> None:
        assert self._sock is not None
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if len(payload) < 126:
            header = struct.pack("!BB", 0x8A, 0x80 | len(payload)) + mask
        else:
            header = struct.pack("!BBH", 0x8A, 0xFE, len(payload)) + mask
        self._sock.sendall(header + masked)

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            buf.extend(chunk)
        return bytes(buf)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def pcm16le_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Minimal WAV wrapper for raw s16le mono/stereo PCM."""
    import struct

    n = len(pcm)
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + n,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        n,
    )
    return hdr + pcm


def pcm16le_to_mp3(
    pcm: bytes,
    *,
    sample_rate: int = 24000,
    ffmpeg_path: str = "ffmpeg",
) -> bytes:
    """Encode raw s16le mono PCM to MP3 via ffmpeg (webOS plays MP3 reliably).

    Returns empty bytes if ffmpeg is missing or fails — callers should fall
    back to WAV or unary TTS.
    """
    if not pcm:
        return b""
    import shutil
    import subprocess

    bin_path = ffmpeg_path or "ffmpeg"
    if not shutil.which(bin_path) and bin_path == "ffmpeg":
        for cand in (
            "/usr/bin/ffmpeg",
            "/bin/ffmpeg",
            "/media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher/voice/bin/ffmpeg",
        ):
            if os.path.isfile(cand):
                bin_path = cand
                break
    try:
        proc = subprocess.run(
            [
                bin_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(int(sample_rate)),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-f",
                "mp3",
                "-b:a",
                "64k",
                "pipe:1",
            ],
            input=pcm,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print("[voice] pcm→mp3 failed: %s" % exc, flush=True)
        return b""
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"")[:200].decode("utf-8", errors="replace")
        if err:
            print("[voice] pcm→mp3 ffmpeg: %s" % err, flush=True)
        return b""
    return proc.stdout


def stt_transcribe_file(
    api_key: str,
    pcm: bytes,
    *,
    sample_rate: int = 16000,
    language: str = "en",
    keyterms: tuple[str, ...] = (),
) -> str:
    """Batch REST STT for a short PCM clip (fallback when live WS returns empty).

    POST multipart to /v1/stt with a WAV body — more reliable than WS for
    1–2s Magic Remote commands like "launch Netflix".
    """
    if not pcm or not api_key:
        return ""
    wav = pcm16le_to_wav(pcm, sample_rate=sample_rate)
    boundary = "----GrokSttBoundary%s" % secrets.token_hex(8)
    filename = "capture.wav"
    parts: list[bytes] = []

    def _field(name: str, value: str) -> None:
        parts.append(
            (
                "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, name, value)
            ).encode("utf-8")
        )

    _field("model", STT_MODEL)
    _field("language", language or "en")
    _field("format", "true")
    for keyterm in keyterms[:100]:
        term = str(keyterm or "").strip()
        if term:
            _field("keyterm", term[:50])
    parts.append(
        (
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; "
            "filename=\"%s\"\r\nContent-Type: audio/wav\r\n\r\n"
            % (boundary, filename)
        ).encode("utf-8")
    )
    parts.append(wav)
    parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        STT_REST_URL,
        data=body,
        headers={
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        },
        method="POST",
    )
    t0 = time.time()
    try:
        # Short timeout: voice turns must not block 15s+ waiting on STT.
        with _urlopen_retry(req, timeout=8.0, retries=0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(
            "[voice] STT REST failed +%.0fms: %s"
            % ((time.time() - t0) * 1000.0, exc),
            flush=True,
        )
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("[voice] STT REST non-json: %r" % raw[:200], flush=True)
        return ""
    # Common shapes: {"text": "..."} or {"transcript": "..."} or nested
    text = (
        data.get("text")
        or data.get("transcript")
        or data.get("result")
        or ""
    )
    if isinstance(text, dict):
        text = text.get("text") or text.get("transcript") or ""
    if not text and isinstance(data.get("segments"), list):
        text = " ".join(
            str(s.get("text") or "") for s in data["segments"] if isinstance(s, dict)
        )
    text = (text or "").strip()
    print(
        "[voice] STT REST +%.0fms result=%r keys=%s"
        % (
            (time.time() - t0) * 1000.0,
            text[:120],
            list(data.keys())[:12] if isinstance(data, dict) else type(data),
        ),
        flush=True,
    )
    return text


class GrokSttSession:
    """Live speech-to-text over Grok STT WebSocket."""

    def __init__(self, config: SttConfig) -> None:
        self.config = config
        self._ws: Optional[MiniWebSocket] = None
        self._last_partial = ""
        # Grok STT segments on pauses and emits one transcript.done per segment.
        # We must accumulate every finalized segment, not just the first.
        self._segments: list[str] = []

    def connect(self) -> None:
        params = {
            "model": STT_MODEL,
            "sample_rate": "16000",
            "encoding": "pcm",
            "interim_results": "true",
            "language": self.config.language,
        }
        if self.config.keyterms:
            params["keyterm"] = [
                str(term).strip()[:50]
                for term in self.config.keyterms[:100]
                if str(term).strip()
            ]
        if self.config.smart_turn and self.config.smart_turn > 0:
            params["smart_turn"] = str(self.config.smart_turn)
        path = "/v1/stt?" + urlencode(params, doseq=True)
        self._ws = MiniWebSocket(
            STT_HOST,
            path,
            {"Authorization": f"Bearer {self.config.api_key}"},
        )
        self._ws.connect()
        ready = self._ws.recv_json()
        if ready.get("type") != "transcript.created":
            raise RuntimeError(f"unexpected STT ready event: {ready}")

    def send_pcm(self, chunk: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("STT session not connected")
        try:
            self._ws.send_binary(chunk)
        except (TimeoutError, socket.timeout, OSError, ConnectionError) as exc:
            # Surface as ConnectionError so callers can drop the dead session.
            raise ConnectionError("STT send failed: %s" % exc) from exc

    def reconnect(self) -> None:
        """Close and open a fresh STT WebSocket (best-effort mid-session recovery)."""
        try:
            self.close()
        except Exception:
            pass
        self._last_partial = ""
        self._segments = []
        self.connect()

    def reset_for_new_utterance(self) -> None:
        """Clear local transcript state before a new KEY_VOICE press.

        Warm WS reuse otherwise keeps the previous final hypothesis
        ("Set volume to ten.") and the daemon treats it as a new command,
        force-closing the overlay before the user can speak.
        """
        self._last_partial = ""
        self._segments = []

    def drain_events(self, max_ms: float = 80.0) -> None:
        """Drop any buffered WS events without updating transcript state."""
        if self._ws is None or self._ws._sock is None:
            return
        deadline = time.time() + max(0.02, float(max_ms) / 1000.0)
        while time.time() < deadline:
            try:
                self._ws._sock.settimeout(0.02)
                self._ws.recv_json()
            except Exception:
                break
        # Ensure residual text from drained events is discarded.
        self._last_partial = ""
        self._segments = []

    def current_text(self) -> str:
        """Best transcript available right now (single live hypothesis)."""
        return self._display_text()

    def finish(self) -> str:
        if self._ws is None or self._ws._sock is None:
            return self.current_text()
        already = self.current_text()
        try:
            self._ws.send_text(json.dumps({"type": "audio.done"}))
        except Exception as exc:  # noqa: BLE001
            print("[voice] stt audio.done send failed: %s" % exc, flush=True)
            result = already
            self.close()
            return result
        assert self._ws._sock is not None
        # If streaming already produced a solid partial, only wait briefly for
        # a possible final polish — do not sit for seconds with text in hand.
        # Tight finish windows — partials usually already hold the full phrase
        # after stream-during-capture.
        if already and len(already.split()) >= 3:
            deadline = time.time() + 0.35
            quiet_needed = 0.12
        elif already:
            deadline = time.time() + 0.6
            quiet_needed = 0.18
        else:
            deadline = time.time() + 1.4
            quiet_needed = 0.25
        idle_start: Optional[float] = None
        best = already
        while time.time() < deadline:
            if self._ws is None or self._ws._sock is None:
                break
            self._ws._sock.settimeout(0.25)
            try:
                event = self._ws.recv_json()
            except (TimeoutError, socket.timeout, BlockingIOError):
                if best:
                    if idle_start is None:
                        idle_start = time.time()
                    elif time.time() - idle_start >= quiet_needed:
                        break
                continue
            except (ValueError, ConnectionError, OSError):
                break
            idle_start = None
            print("[voice] stt event: %s" % event, flush=True)
            etype = event.get("type")
            if etype in ("transcript.partial", "transcript.done"):
                text = (event.get("text") or "").strip()
                merged = self._merge_partial(text) if text else None
                if merged:
                    self._last_partial = merged
                    best = self._display_text()
                elif not text and self._last_partial and etype == "transcript.done":
                    # Empty done: keep best partial as final candidate.
                    idle_start = time.time()
            elif etype == "error":
                raise RuntimeError(event.get("message", "STT error"))
        # Single-hypothesis finish: do NOT stack mid-stream "segments" that
        # produced garbage like "However is the weather".
        result = (best or self._last_partial or "").strip()
        self._segments = [result] if result else []
        self._last_partial = ""
        self.close()
        print(
            "[voice] stt finish: result=%r"
            % (result[:120],),
            flush=True,
        )
        return result

    def _joined(self) -> str:
        return " ".join(s for s in self._segments if s).strip()

    def poll_events(
        self,
        on_partial: Callable[[str, bool], None],
        *,
        timeout: float = 0.05,
    ) -> None:
        if self._ws is None or self._ws._sock is None:
            return
        self._ws._sock.settimeout(timeout)
        try:
            event = self._ws.recv_json()
        except (TimeoutError, socket.timeout, BlockingIOError):
            return
        except (ValueError, ConnectionError, OSError):
            return
        finally:
            try:
                if self._ws is not None and self._ws._sock is not None:
                    self._ws._sock.settimeout(30)
            except Exception:
                pass
        etype = event.get("type")
        if etype == "transcript.partial":
            text = (event.get("text") or "").strip()
            if text:
                merged = self._merge_partial(text)
                if merged:
                    self._last_partial = merged
                    display = self._display_text()
                    print(
                        "[voice] stt partial: %r final=%s"
                        % (display[:120], bool(event.get("is_final"))),
                        flush=True,
                    )
                    on_partial(display, bool(event.get("is_final")))
        elif etype == "transcript.done":
            # Push-to-talk: treat "done" as an updated full hypothesis, NOT a
            # committed segment. Stacking mid-stream dones produced broken
            # transcripts ("However" + "is the weather").
            text = (event.get("text") or "").strip()
            if text:
                merged = self._merge_partial(text)
                if merged or len(text) > len(self._last_partial or ""):
                    self._last_partial = merged or text
                    display = self._display_text()
                    print(
                        "[voice] stt hypothesis: %r final=%s"
                        % (display[:120], bool(event.get("speech_final"))),
                        flush=True,
                    )
                    on_partial(display, bool(event.get("speech_final")))
        elif etype == "error":
            raise RuntimeError(event.get("message", "STT error"))

    def _display_text(self) -> str:
        # Prefer the live hypothesis alone for PTT; only append segments if we
        # ever intentionally commit multi-segment dictation (currently unused).
        if self._last_partial:
            return self._last_partial.strip()
        return self._joined()

    def _merge_partial(self, text: str) -> Optional[str]:
        """Return the hypothesis to keep, or None to ignore this update.

        xAI STT sometimes emits a full phrase then a *separate* short tail
        for the version number, e.g.::

            'What do you think about Grok?'  then  '4.5?'

        Treating the tail as a regression dropped \"4.5\". Merge version-only
        tails onto a hypothesis that already ends with Grok / model name.
        """
        prev = (self._last_partial or "").strip()
        text = (text or "").strip()
        if not text:
            return None
        if not prev:
            return text
        if text == prev:
            return text

        # Version-only continuation after "…Grok" / "…art rock" / etc.
        ver_only = re.fullmatch(
            r"(?i)\s*(4(?:\s*[.,]\s*5)?|four(?:\s*point\s*five)?|"
            r"4\s*point\s*5)\s*[?.!]*\s*",
            text,
        )
        if ver_only:
            base = re.sub(r"[?.!\s]+$", "", prev)
            if re.search(
                r"(?i)\b(?:grok|art[\s\-]*rock|artrock|gork|grock|gawk|croak)\b$",
                base,
            ):
                raw_ver = (ver_only.group(1) or "").lower()
                if (
                    "5" in raw_ver
                    or "point" in raw_ver
                    or "four" in raw_ver
                    or raw_ver.replace(" ", "") in ("4.5", "4,5")
                ):
                    merged = base + " 4.5"
                else:
                    merged = base + " 4"
                print(
                    "[voice] stt partial merged version: %r + %r -> %r"
                    % (prev[:60], text[:20], merged[:80]),
                    flush=True,
                )
                return merged

        # Full hypothesis that already contains Grok but dropped the version
        # while a prior hypothesis had it — keep the longer/versioned one.
        if re.search(r"(?i)\bgrok\s+4(?:[.,]5)?\b", prev) and re.search(
            r"(?i)\bgrok\??$", text
        ):
            return prev

        if self._accept_partial(text):
            return text
        return None

    def _accept_partial(self, text: str) -> bool:
        """Accept improved hypotheses; reject collapses of a longer good string."""
        prev = (self._last_partial or "").strip()
        if not prev:
            return True
        if text == prev:
            return True
        # If STT hallucinates the complete keyterm vocabulary after a short
        # command, keep the real hypothesis that preceded it. xAI sometimes
        # echoes comma-separated hints verbatim instead of transcribing audio.
        hint_echoes = (
            "go to sky",
            "sky tv",
            "hdmi 1",
            "hdmi 2",
            "prime video",
            "southend-on-sea",
            "mute volume",
            "set volume",
        )
        lowered = text.lower()
        if len(text) > 80 and sum(term in lowered for term in hint_echoes) >= 5:
            return False
        # Short TV commands are frequently revised from a longer but wrong
        # number ("Volumes to 30" -> final "Volume 13"). Prefer the explicit
        # final numeric command even when it is a few characters shorter.
        prev_volume = re.fullmatch(
            r"(?i)volumes?\s+(?:to\s+|at\s+|level\s+)?(\d{1,3})[.!?]?",
            prev,
        )
        new_volume = re.fullmatch(
            r"(?i)volumes?\s+(?:to\s+|at\s+|level\s+)?(\d{1,3})[.!?]?",
            text.strip(),
        )
        if prev_volume and new_volume:
            return True
        # Always accept longer text (refinement / more words).
        if len(text) >= len(prev):
            return True
        # Allow small polish (punctuation), not truncations to 1–2 garbage words.
        if len(text) >= max(12, int(len(prev) * 0.75)):
            return True
        print(
            "[voice] stt partial ignored (regression): %r -> %r"
            % (prev[:80], text[:80]),
            flush=True,
        )
        return False

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None


def _search_enabled(web_search: bool, search_mode: str) -> bool:
    """Whether to attach the server-side web_search tool."""
    if not web_search:
        return False
    mode = (search_mode or "auto").strip().lower()
    if mode in ("off", "never", "false", "0", "no"):
        return False
    # auto / always / on / true — tool is available; model decides for "auto"
    return True


def _extract_citations(obj: dict) -> list[str]:
    """Pull URL citations from a Responses API object (completed response)."""
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        u = (url or "").strip()
        if u and u not in seen:
            seen.add(u)
            found.append(u)

    cites = obj.get("citations")
    if isinstance(cites, list):
        for c in cites:
            if isinstance(c, str):
                add(c)
            elif isinstance(c, dict):
                add(str(c.get("url") or c.get("uri") or ""))

    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            for ann in content.get("annotations") or []:
                if isinstance(ann, dict) and ann.get("url"):
                    add(str(ann["url"]))
    return found


def stream_chat(
    api_key: str,
    user_text: str,
    *,
    model: str = "grok-4.7",
    on_token: Optional[Callable[[str], None]] = None,
    on_search_start: Optional[Callable[[], None]] = None,
    web_search: bool = False,
    search_mode: str = "auto",
    max_search_results: int = 8,
    search_workers: int = 10,
    search_timeout_s: float = 3.5,
) -> "ChatResult":
    """Stream answer tokens; optionally pull live web evidence first.

    Internet path (preferred):
      1. Fire many search providers/queries in parallel (``web_search`` module).
      2. Stream a normal ``/v1/chat/completions`` answer grounded on those hits.

    That is much faster than a single sequential agent ``web_search`` tool loop.
    If parallel search returns nothing, we fall back to xAI ``/v1/responses``
    with the server-side tool, then to plain chat.
    """
    use_search = _search_enabled(web_search, search_mode)
    # Even when search is enabled in settings, skip it for identity/chitchat so
    # answers are pure Grok and labeled "Grok" (not "Internet").
    if use_search and not _query_needs_web_search(user_text):
        print(
            "[voice] web_search skipped (fast chat path): %r"
            % (user_text[:80],),
            flush=True,
        )
        use_search = False
    if use_search:
        if on_search_start is not None:
            try:
                on_search_start()
            except Exception:
                pass
        # --- Fast path: multi-threaded simultaneous outbound search ---
        try:
            from web_search import format_hits_for_prompt, parallel_web_search

            bundle = parallel_web_search(
                user_text,
                max_results=max_search_results,
                workers=search_workers,
                timeout_s=search_timeout_s,
            )
            print(
                "[voice] parallel web_search hits=%d queries=%s %dms errors=%d"
                % (
                    len(bundle.hits),
                    bundle.queries[:3],
                    bundle.elapsed_ms,
                    len(bundle.errors),
                ),
                flush=True,
            )
            if bundle.hits:
                context = format_hits_for_prompt(bundle.hits)
                result = _stream_chat_with_web_context(
                    api_key,
                    user_text,
                    context,
                    model=model,
                    on_token=on_token,
                )
                cites = bundle.citations[: max(1, int(max_search_results or 8))]
                return ChatResult(
                    text=result.text,
                    citations=cites,
                    used_search=True,
                )
        except Exception as exc:  # noqa: BLE001
            print(
                "[voice] parallel web_search failed: %s — trying agent path"
                % exc,
                flush=True,
            )

        # --- Fallback: xAI server-side agent tool (sequential) ---
        try:
            result = _stream_responses_with_search(
                api_key,
                user_text,
                model=model,
                on_token=on_token,
                max_search_results=max_search_results,
            )
            # Safety net: never label self/chitchat as Internet even if the
            # model still invoked web_search.
            if not _query_needs_web_search(user_text):
                return ChatResult(
                    text=result.text, citations=[], used_search=False
                )
            # Require real citation URLs for the Internet label. A tool call
            # with no usable sources should still count as Grok.
            if result.used_search and not result.citations:
                print(
                    "[voice] web_search invoked but no citations — label Grok",
                    flush=True,
                )
                return ChatResult(
                    text=result.text, citations=[], used_search=False
                )
            return result
        except urllib.error.HTTPError as exc:
            # Fall back to plain chat if Responses/tools is unavailable.
            detail = exc.read().decode("utf-8", errors="replace")
            print(
                "[voice] web_search responses failed HTTP %s: %s — falling back"
                % (exc.code, detail[:200]),
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "[voice] web_search responses failed: %s — falling back" % exc,
                flush=True,
            )
    return _stream_chat_completions(api_key, user_text, model=model, on_token=on_token)


def _stream_chat_completions(
    api_key: str,
    user_text: str,
    *,
    model: str,
    on_token: Optional[Callable[[str], None]] = None,
    system_prompt: Optional[str] = None,
) -> "ChatResult":
    """Plain streaming chat (no live web tool)."""
    model_use = (model or "grok-4.7").strip() or "grok-4.7"
    body = {
        "model": model_use,
        "stream": True,
        # Short spoken answers; fewer tokens → faster first paint + done.
        "max_tokens": 60,
        "messages": [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    }
    req = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Connection": "close",
        },
        method="POST",
    )
    answer_parts: list[str] = []
    t0 = time.time()
    first_token_logged = False
    last_err: Optional[BaseException] = None
    # One quick retry on connect stall — do not sit 60s before failing over.
    for attempt in range(1, 3):
        answer_parts = []
        first_token_logged = False
        try:
            print(
                "[voice] chat http open model=%s attempt=%d"
                % (model_use, attempt),
                flush=True,
            )
            # 20s is enough for a 60-token spoken reply; fail faster on TV WAN.
            with _urlopen_retry(req, timeout=20, retries=0) as resp:
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
                    delta = (
                        chunk.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if delta:
                        if not first_token_logged:
                            first_token_logged = True
                            print(
                                "[voice] chat first token +%.0fms model=%s"
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
            raise RuntimeError(f"chat HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout, OSError) as exc:
            last_err = exc
            print(
                "[voice] chat stream timeout/error attempt=%d: %s"
                % (attempt, exc),
                flush=True,
            )
            if attempt < 2 and not answer_parts:
                time.sleep(0.2)
                continue
            if answer_parts:
                break
            raise RuntimeError(
                "Answer timed out — check internet and try again"
            ) from exc
    if last_err is not None and not answer_parts:
        raise RuntimeError(
            "Answer timed out — check internet and try again"
        ) from last_err
    print(
        "[voice] chat done +%.0fms chars=%d model=%s"
        % ((time.time() - t0) * 1000.0, len("".join(answer_parts)), model_use),
        flush=True,
    )
    return ChatResult(text="".join(answer_parts).strip(), citations=[], used_search=False)


def _stream_chat_with_web_context(
    api_key: str,
    user_text: str,
    sources_block: str,
    *,
    model: str,
    on_token: Optional[Callable[[str], None]] = None,
) -> "ChatResult":
    """Stream a spoken answer grounded on parallel-fetched web snippets."""
    system = (
        SYSTEM_PROMPT
        + "\n\nYou have fresh web search results below. Answer the user's "
        "question using them. Prefer the most recent and specific facts. "
        "If sources disagree, say so briefly. Do not invent URLs or quotes. "
        "Do not list sources unless asked — speak naturally for a TV.\n\n"
        "WEB RESULTS:\n"
        + (sources_block or "(no snippets)")
    )
    return _stream_chat_completions(
        api_key,
        user_text,
        model=model,
        on_token=on_token,
        system_prompt=system,
    )


def _stream_responses_with_search(
    api_key: str,
    user_text: str,
    *,
    model: str,
    on_token: Optional[Callable[[str], None]] = None,
    max_search_results: int = 5,
) -> "ChatResult":
    """Stream via /v1/responses with the server-side web_search agent tool.

    Grok decides when to search (tool_choice auto). Server runs the tool and
    continues until it has enough context, then streams the final answer text.
    """
    # Keep spoken answers clean — no [[1]](url) markdown in TTS.
    _ = max_search_results  # reserved for future tool filters
    body: dict = {
        "model": model,
        "stream": True,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "tools": [{"type": "web_search"}],
        "include": ["no_inline_citations"],
    }

    req = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    answer_parts: list[str] = []
    citations: list[str] = []
    # True only when web_search actually ran (not merely because the tool is
    # *available*). A broad match on "tool" in event types was labelling pure
    # model answers like "what is your name" as Internet.
    search_invoked = False

    with _urlopen_retry(req, timeout=180, retries=1) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                if payload == "[DONE]":
                    break
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue

            etype = event.get("type") or ""

            # Text deltas (OpenAI GA name + xAI alias).
            if etype in (
                "response.output_text.delta",
                "response.text.delta",
            ):
                delta = event.get("delta") or ""
                if isinstance(delta, dict):
                    delta = delta.get("text") or delta.get("content") or ""
                if delta:
                    answer_parts.append(str(delta))
                    if on_token:
                        on_token(str(delta))
                continue

            # Explicit web_search tool lifecycle only (not every "tool" event).
            if "web_search" in etype:
                # e.g. response.web_search_call.in_progress / completed
                search_invoked = True
                print(
                    "[voice] web_search event: %s" % etype[:80],
                    flush=True,
                )
                continue

            item = event.get("item")
            if isinstance(item, dict):
                item_type = str(item.get("type") or "")
                item_name = str(item.get("name") or "")
                if (
                    item_type in ("web_search_call", "web_search")
                    or item_name == "web_search"
                    or item_type.endswith("web_search_call")
                ):
                    search_invoked = True
                    print(
                        "[voice] web_search item: type=%s status=%s"
                        % (item_type, item.get("status")),
                        flush=True,
                    )
                    # Citations sometimes live on the completed tool item.
                    for key in ("results", "output", "content"):
                        blob = item.get(key)
                        if isinstance(blob, list):
                            for entry in blob:
                                if isinstance(entry, dict) and entry.get("url"):
                                    u = str(entry["url"]).strip()
                                    if u and u not in citations:
                                        citations.append(u)
                continue

            # Completed response carries the full citation list.
            if etype in ("response.completed", "response.done"):
                resp_obj = event.get("response") or event
                if isinstance(resp_obj, dict):
                    citations = _extract_citations(resp_obj) or citations
                    # Fallback: full output_text if we missed deltas
                    if not answer_parts:
                        for out_item in resp_obj.get("output") or []:
                            if not isinstance(out_item, dict):
                                continue
                            for content in out_item.get("content") or []:
                                if (
                                    isinstance(content, dict)
                                    and content.get("type") == "output_text"
                                    and content.get("text")
                                ):
                                    text = str(content["text"])
                                    answer_parts.append(text)
                                    if on_token:
                                        on_token(text)
                    # Detect web_search_call items in final output list.
                    for out_item in resp_obj.get("output") or []:
                        if not isinstance(out_item, dict):
                            continue
                        ot = str(out_item.get("type") or "")
                        oname = str(out_item.get("name") or "")
                        if "web_search" in ot or oname == "web_search":
                            search_invoked = True
                continue

            # Some streams put citations on the top-level event.
            if event.get("citations"):
                citations = _extract_citations(event) or citations

    # Internet label only when search ran *and* returned real source URLs.
    # search_invoked alone is not enough (model sometimes searches needlessly).
    used_search = bool(citations) and search_invoked
    text = "".join(answer_parts).strip()
    # Strip any residual inline citation markdown that slipped through.
    if "[[" in text:
        text = re.sub(r"\[\[\d+\]\]\([^)]+\)", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
    print(
        "[voice] responses web_search used=%s invoked=%s citations=%d chars=%d"
        % (used_search, search_invoked, len(citations), len(text)),
        flush=True,
    )
    return ChatResult(text=text, citations=citations, used_search=used_search)


def stream_voice_answer(
    api_key: str,
    user_text: str,
    *,
    model: str = DEFAULT_VOICE_MODEL,
    voice_id: str = DEFAULT_TTS_VOICE,
    speed: float = 1.0,
    language: str = "en",
    web_search: bool = True,
    search_mode: str = "auto",
    on_token: Optional[Callable[[str], None]] = None,
    on_audio: Optional[Callable[[bytes, int], None]] = None,
    on_search_start: Optional[Callable[[], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> VoiceAnswerResult:
    """Answer via Speech-to-Speech realtime API (text in → audio + transcript).

    Uses ``wss://api.x.ai/v1/realtime?model=…`` with
    ``grok-voice-think-fast-2.0`` by default. Local STT already produced
    ``user_text``; this replaces chat completions + unary TTS for Grok answers.

    Audio is PCM16 mono at 24 kHz, wrapped into standalone WAV chunks so the
    webOS overlay ``<audio>`` element can play them. Transcript deltas stream
    via ``on_token``; each WAV chunk is delivered via ``on_audio(wav, seq)``.
    """
    text_in = (user_text or "").strip()
    if not text_in or not api_key:
        return VoiceAnswerResult(text="")

    model = (model or DEFAULT_VOICE_MODEL).strip() or DEFAULT_VOICE_MODEL
    voice = (voice_id or DEFAULT_TTS_VOICE).strip() or DEFAULT_TTS_VOICE
    try:
        spd = float(speed)
    except (TypeError, ValueError):
        spd = 1.0
    spd = max(0.7, min(1.5, spd))

    use_search = _search_enabled(web_search, search_mode)
    if use_search and not _query_needs_web_search(text_in):
        use_search = False

    path = "/v1/realtime?model=%s&reasoning.effort=none" % quote(model, safe="-._")

    ws = MiniWebSocket(
        STT_HOST,
        path,
        {"Authorization": "Bearer %s" % api_key},
    )
    t0 = time.time()
    transcript_parts: list[str] = []
    pcm_buf = bytearray()
    audio_chunks = 0
    audio_bytes = 0
    used_search = False
    search_notified = False
    err_msg = ""
    first_audio_emitted = False

    def _stopped() -> bool:
        if should_stop is None:
            return False
        try:
            return bool(should_stop())
        except Exception:
            return False

    def _flush_pcm(*, final: bool = False) -> None:
        nonlocal audio_chunks, audio_bytes, first_audio_emitted
        if not pcm_buf or on_audio is None:
            if final:
                pcm_buf.clear()
            return
        # Keep even sample frames (16-bit mono).
        available = (len(pcm_buf) // 2) * 2
        if available <= 0:
            return
        if not final:
            # Hold until the response is done — one continuous clip from the
            # true start of the spoken answer (no missing opening words).
            return
        else:
            n = available
        chunk = bytes(pcm_buf[:n])
        del pcm_buf[:n]
        if not chunk:
            return
        audio_chunks += 1
        first_audio_emitted = True
        # Prefer MP3 for webOS <audio> (data:audio/wav often plays as silence).
        payload = pcm16le_to_mp3(chunk, sample_rate=VOICE_OUTPUT_RATE)
        if not payload:
            payload = pcm16le_to_wav(chunk, sample_rate=VOICE_OUTPUT_RATE)
        audio_bytes += len(payload)
        try:
            on_audio(payload, audio_chunks)
        except Exception as exc:  # noqa: BLE001
            print("[voice] voice on_audio failed: %s" % exc, flush=True)

    def _emit_token(delta: str) -> None:
        if not delta:
            return
        transcript_parts.append(delta)
        if on_token is not None:
            try:
                on_token(delta)
            except Exception:
                pass

    t_voice = time.time()
    try:
        ws.connect()
        print(
            "[voice] voice ws connected +%.0fms"
            % ((time.time() - t_voice) * 1000.0),
            flush=True,
        )
        if ws._sock is not None:
            ws._sock.settimeout(30.0)

        # Drain session.created / conversation.created quickly (do not stall).
        for _ in range(4):
            if _stopped():
                break
            try:
                if ws._sock is not None:
                    ws._sock.settimeout(1.2)
                ev = ws.recv_json()
            except (socket.timeout, TimeoutError, OSError):
                break
            et = str(ev.get("type") or "")
            if et == "error":
                err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
                err_msg = str(
                    (err or {}).get("message") or ev.get("message") or ev
                )[:200]
                raise RuntimeError("voice session error: %s" % err_msg)
            if et in ("session.created", "conversation.created", "session.updated"):
                continue
            break

        if ws._sock is not None:
            ws._sock.settimeout(30.0)

        # Re-state length limit in session instructions — voice models can
        # ignore a buried system rule without an explicit cap reminder.
        voice_instructions = (
            SYSTEM_PROMPT
            + " Hard limit: keep this reply under 40 words unless the user "
            "explicitly asked for a longer answer, poem, story, or count."
        )
        session: dict = {
            "voice": voice,
            "instructions": voice_instructions,
            "turn_detection": None,
            "reasoning": {"effort": "none"},
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": VOICE_OUTPUT_RATE},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": VOICE_OUTPUT_RATE},
                    "speed": spd,
                },
            },
        }
        # Bias ASR language when the API surfaces it (harmless for text-in).
        lang = (language or "en").strip()
        if lang:
            session["audio"]["input"]["transcription"] = {
                "language_hint": lang,
            }
        if use_search:
            session["tools"] = [{"type": "web_search"}]

        ws.send_text(
            json.dumps({"type": "session.update", "session": session})
        )

        # Optional ack — do not wait long; create the turn immediately after.
        try:
            if ws._sock is not None:
                ws._sock.settimeout(1.5)
            for _ in range(3):
                ev = ws.recv_json()
                et = str(ev.get("type") or "")
                if et == "session.updated":
                    break
                if et == "error":
                    err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
                    err_msg = str(
                        (err or {}).get("message") or ev.get("message") or ev
                    )[:200]
                    raise RuntimeError("voice session.update failed: %s" % err_msg)
        except (socket.timeout, TimeoutError):
            pass
        finally:
            if ws._sock is not None:
                ws._sock.settimeout(30.0)

        if _stopped():
            return VoiceAnswerResult(text="")

        ws.send_text(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": text_in},
                        ],
                    },
                }
            )
        )
        # Per-turn instructions restate the short-answer limit.
        ws.send_text(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {"instructions": voice_instructions},
                }
            )
        )
        print(
            "[voice] voice response.create +%.0fms"
            % ((time.time() - t_voice) * 1000.0),
            flush=True,
        )

        print(
            "[voice] voice realtime start model=%s voice=%s search=%s"
            % (model, voice, use_search),
            flush=True,
        )

        while not _stopped():
            try:
                ev = ws.recv_json()
            except (socket.timeout, TimeoutError) as exc:
                raise RuntimeError("voice realtime timeout") from exc
            et = str(ev.get("type") or "")

            if et == "error":
                err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
                err_msg = str(
                    (err or {}).get("message") or ev.get("message") or ev
                )[:300]
                raise RuntimeError("voice error: %s" % err_msg)

            if et in (
                "response.output_audio_transcript.delta",
                "response.audio_transcript.delta",
            ):
                _emit_token(str(ev.get("delta") or ""))
                continue

            if et in ("response.output_text.delta", "response.text.delta"):
                _emit_token(str(ev.get("delta") or ""))
                continue

            if et in (
                "response.output_audio.delta",
                "response.audio.delta",
            ):
                if on_audio is None:
                    continue
                b64 = ev.get("delta") or ev.get("audio") or ""
                if b64:
                    try:
                        pcm_buf.extend(base64.b64decode(b64))
                    except Exception as exc:  # noqa: BLE001
                        print(
                            "[voice] voice audio decode failed: %s" % exc,
                            flush=True,
                        )
                    else:
                        _flush_pcm(final=False)
                continue

            if et in (
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            ):
                # Some servers send the full transcript only at the end.
                part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                final_tr = (
                    ev.get("transcript")
                    or ev.get("text")
                    or part.get("transcript")
                )
                if final_tr and not transcript_parts:
                    _emit_token(str(final_tr))
                continue

            if et in (
                "response.output_audio.done",
                "response.audio.done",
            ):
                continue

            if et in (
                "response.function_call_arguments.delta",
                "response.mcp_call.in_progress",
                "response.mcp_call_arguments.delta",
            ):
                if not search_notified and on_search_start is not None:
                    search_notified = True
                    try:
                        on_search_start()
                    except Exception:
                        pass
                used_search = True
                continue

            if et in (
                "response.function_call_arguments.done",
                "response.mcp_call.completed",
            ):
                used_search = True
                continue

            # Server-side web_search may surface as output items without
            # function_call events — watch status strings.
            if "web_search" in et or "search" in et.lower():
                if "in_progress" in et or "searching" in et:
                    if not search_notified and on_search_start is not None:
                        search_notified = True
                        try:
                            on_search_start()
                        except Exception:
                            pass
                    used_search = True
                continue

            if et == "response.done":
                _flush_pcm(final=True)
                break

            if et == "response.output_item.done":
                item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
                itype = str(item.get("type") or "")
                if "search" in itype or itype in ("web_search_call", "function_call"):
                    used_search = True
                continue

        full_text = "".join(transcript_parts).strip()
        # Some builds only put the final transcript on response.done payload.
        if not full_text:
            # Nothing streamed — leave empty for caller fallback.
            pass

        print(
            "[voice] voice realtime done +%.0fms chars=%d chunks=%d "
            "audio_bytes=%d model=%s used_search=%s"
            % (
                (time.time() - t0) * 1000.0,
                len(full_text),
                audio_chunks,
                audio_bytes,
                model,
                used_search,
            ),
            flush=True,
        )
        return VoiceAnswerResult(
            text=full_text,
            audio_chunks=audio_chunks,
            audio_bytes=audio_bytes,
            citations=[],
            used_search=used_search,
        )
    finally:
        try:
            if not _stopped():
                # Best-effort cancel if we are tearing down mid-turn.
                pass
            ws.close()
        except Exception:
            pass


def synthesize_speech(
    api_key: str,
    text: str,
    *,
    voice_id: str = DEFAULT_TTS_VOICE,
    language: str = "en",
    speed: float = 1.0,
) -> bytes:
    """Synthesize `text` to speech via xAI TTS; return MP3 audio bytes.

    Uses the default MP3 / 24 kHz output which HTML5 <audio> plays natively.
    `speed` is the API speech-rate multiplier (0.7–1.5); preferred over
    client-side playbackRate / ffmpeg atempo on webOS.
    Raises RuntimeError on HTTP errors so callers can log and skip playback.
    """
    text = strip_for_speech(text or "")
    if not text:
        return b""
    # API cap is 15,000 chars; TV answers are short but guard anyway.
    if len(text) > 15000:
        text = text[:15000]
    try:
        spd = float(speed)
    except (TypeError, ValueError):
        spd = 1.0
    spd = max(0.7, min(1.5, spd))
    body: dict = {
        "text": text,
        "voice_id": voice_id or DEFAULT_TTS_VOICE,
        "language": language or "en",
    }
    # Bake rate into the MP3 — webOS WebKit cannot use playbackRate safely.
    if abs(spd - 1.0) >= 0.01:
        body["speed"] = spd
    req = urllib.request.Request(
        TTS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        # Keep tight — with IPv4-first, healthy TTS is ~0.8–1.5s; do not sit
        # 12s+ when the WAN path is wedged (user already sees the answer text).
        with _urlopen_retry(req, timeout=8, retries=0) as resp:
            data = resp.read()
        print(
            "[voice] TTS unary +%.0fms voice=%s bytes=%d"
            % ((time.time() - t0) * 1000.0, voice_id, len(data)),
            flush=True,
        )
        return data
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"tts HTTP {exc.code}: {detail}") from exc


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Test Grok STT + chat from PCM file")
    parser.add_argument("--pcm", required=True, help="raw PCM16 16kHz mono file")
    parser.add_argument("--api-key", default=os.environ.get("XAI_API_KEY", ""))
    parser.add_argument("--language", default="en")
    parser.add_argument("--model", default="grok-4-fast")
    parser.add_argument("--no-chat", action="store_true")
    args = parser.parse_args(argv)

    if not args.api_key:
        print("Set XAI_API_KEY or pass --api-key", file=sys.stderr)
        return 2

    pcm = Path(args.pcm).read_bytes()
    cfg = SttConfig(api_key=args.api_key, language=args.language)
    stt = GrokSttSession(cfg)
    stt.connect()
    offset = 0
    while offset < len(pcm):
        chunk = pcm[offset : offset + PCM_CHUNK_BYTES]
        stt.send_pcm(chunk)
        try:
            stt.poll_events(lambda t, f: print(f"[{'final' if f else 'partial'}] {t}"))
        except RuntimeError:
            pass
        offset += PCM_CHUNK_BYTES
    final = stt.finish()
    print(f"\nFINAL: {final}")
    if not args.no_chat and final:
        print("\nCHAT:")
        stream_chat(args.api_key, final, model=args.model, on_token=lambda t: print(t, end="", flush=True))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
#!/usr/bin/env python3
"""Launch Home voice daemon — button, audio, AI answers and the voice card."""

from __future__ import annotations

import audioop
import base64
import collections
import datetime
import errno
import json
import os
import queue
import re
import select
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional, Union

from button_watch import ButtonEvent, ButtonWatcher, VoiceButtonEvent
from gemini_client import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_STT_MODEL,
    GeminiSttSession,
    stream_gemini_chat,
    synthesize_gemini_speech,
)
from openrouter_client import (
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_OPENROUTER_STT_MODEL,
    OpenRouterSttSession,
    stream_openrouter_chat,
)
from grok_client import (
    DEFAULT_TTS_VOICE,
    DEFAULT_VOICE_MODEL,
    GrokSttSession,
    SttConfig,
    XAI_CREDITS_MESSAGE,
    _query_needs_web_search,
    _search_enabled,
    classify_xai_http_error,
    stt_transcribe_file,
    stream_chat,
    stream_voice_answer,
    strip_for_speech,
    synthesize_speech,
)
import supergrok_auth
from hidraw_stream import HidrawVoiceStream
from luna_service import LunaBridge, VOICE_RESULT_APP_IDS
from msbc_to_pcm import MsbcDecoder, PCM_CHUNK_BYTES
import config_store
import tv_control
import weather

DEFAULT_CONFIG_PATH = "/home/root/.config/launch-home-voice/config.json"
FALLBACK_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# Sentence-boundary TTS only. Soft mid-phrase cuts ("…unit of an" | "element…")
# made webOS play separate data: URI clips with audible gaps (stutter/jitter).
TTS_MIN_CHUNK_CHARS = 80
# First chunk may start after a real sentence or a long clause — not at 22 chars.
TTS_FIRST_CHUNK_CHARS = 48
# Only force a word-boundary cut when the model produces a long unpunctuated run.
TTS_FIRST_CHUNK_FORCE = 140
# No soft mid-sentence splits while streaming (0). Sentence ends only.
TTS_SOFT_CHUNK_LIMIT = 0
# Keep this list tiny — long keyterm lists made xAI echo the whole vocabulary.
# "Grok" is often misheard as "art rock" / "gork" on Magic Remote audio.
STT_KEYTERMS = (
    "Sky",
    "Southend-on-Sea",
    "Grok",
    "Grok 4.5",
    "settings",
    "open settings",
)
_STT_KEYTERM_ECHO_RE = re.compile(
    r"(?is)\bSky\b.*\bHDMI\s*1\b.*\bHDMI\s*2\b.*\bPrime Video\b.*"
    r"\bSouthend-on-Sea\b.*\bmute volume\b.*\bset volume\b"
)
# Deterministic STT fixes for product / model names (see "Art Rock 4.5" → Grok).
_STT_GROK_VERSION_RE = re.compile(
    r"(?i)\b(?:art[\s\-]*rock|artrock|gawk|gork|grock|croak|growk)\s*"
    r"(?P<ver>4(?:\s*[.,]\s*5)?|four(?:\s*point\s*five)?)\b"
)
_STT_GROK_BARE_RE = re.compile(
    r"(?i)\b(?:art[\s\-]*rock|artrock|gawk|gork|grock|croak|growk)\b"
)
# Parallel TTS workers — synthesize next sentence while current plays.
TTS_WORKERS = 2
# Most TV answers fit one continuous MP3 (no inter-clip gap on webOS).
TTS_SINGLE_SHOT_CHARS = 320


def normalize_product_transcript(text: str) -> str:
    """Correct known STT mishearings of Grok / model names.

    Confirmed on device: \"what do you think about Grok 4.5\" → \"Art Rock 4.5\".
    """
    raw = text or ""
    if not raw.strip():
        return raw

    def _ver_repl(m: re.Match) -> str:
        ver = re.sub(r"\s+", "", (m.group("ver") or "").lower())
        ver = ver.replace(",", ".")
        if ver in ("4.5", "4,5", "fourpointfive") or "point" in (
            m.group("ver") or ""
        ).lower():
            return "Grok 4.5"
        if ver.startswith("4"):
            return "Grok 4.5" if "5" in ver else "Grok 4"
        return "Grok 4.5"

    fixed = _STT_GROK_VERSION_RE.sub(_ver_repl, raw)
    fixed = _STT_GROK_BARE_RE.sub("Grok", fixed)
    # "Grok four point five" / "Grok 4 point 5"
    fixed = re.sub(
        r"(?i)\bGrok\s+four(?:\s*point\s*five)?\b",
        "Grok 4.5",
        fixed,
    )
    fixed = re.sub(
        r"(?i)\bGrok\s+4\s*point\s*5\b",
        "Grok 4.5",
        fixed,
    )
    if fixed != raw:
        print(
            "[voice] STT product correction: %r -> %r"
            % (raw[:100], fixed[:100]),
            flush=True,
        )
    return fixed


def split_sentences(text: str, *, soft: bool = False) -> tuple[list[str], str]:
    """Split streamed answer text into complete sentences + trailing remainder.

    Boundaries are ., !, ? (optionally followed by closing quotes/brackets) when
    followed by whitespace/end, or a newline. When soft=True (first TTS chunk),
    also break on word boundaries / commas once enough characters have arrived
    so speech can start mid-sentence.
    """
    chunks: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ".!?":
            j = i + 1
            while j < n and text[j] in "\"')]":
                j += 1
            if j >= n or text[j].isspace():
                chunk = text[start:j].strip()
                if chunk:
                    chunks.append(chunk)
                while j < n and text[j].isspace():
                    j += 1
                start = j
                i = j
                continue
        elif soft and ch in ",;:" and (i - start) >= TTS_FIRST_CHUNK_CHARS:
            j = i + 1
            if j >= n or text[j].isspace():
                chunk = text[start:j].strip()
                if chunk:
                    chunks.append(chunk)
                while j < n and text[j].isspace():
                    j += 1
                start = j
                i = j
                continue
        # Soft word-boundary only at FORCE length (not every ~12 chars).
        elif soft and ch.isspace() and (i - start) >= TTS_FIRST_CHUNK_FORCE:
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            chunk = text[start:i].strip()
            if chunk:
                chunks.append(chunk)
            start = j
            i = j
            continue
        elif ch == "\n":
            chunk = text[start:i].strip()
            if chunk:
                chunks.append(chunk)
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            start = j
            i = j
            continue
        i += 1
    return chunks, text[start:]


# LG systemd service that launches the native voice UI (Alexa/concierge half-view)
# on KEY_VOICE. We keep voiceinput running because it routes the Bluetooth remote
# mic into ALSA (hw:1,4); only voiceconductor drives the on-screen UI, so that is
# the one we stop. Reversible: a reboot restores it if our app is removed.
DEFAULT_NATIVE_VOICE_SERVICES = ["voiceconductor.service"]

# LG trigger_thinq watches /tmp via inotify; creating this file disables native
# setInputEvent forwarding from trigger.alexa / trigger.thinq into voiceconductor.
NATIVE_SET_INPUT_EVENT_DISABLE_FILE = "/tmp/voiceinput_disable_set_input_event"
# Launch Home's voice card (voice/overlay), installed by enable-voice.sh.
OVERLAY_APP_ID = "org.webosbrew.lounge.voice"

# Bluetooth remote mic, exposed as an ALSA capture PCM on this TV.
DEFAULT_CAPTURE_DEVICE = "plughw:1,4"
CAPTURE_RATE = 16000
CAPTURE_CHUNK_BYTES = 3200  # 100 ms of 16-bit mono @ 16 kHz

# phased: let LG startStreaming enable the BLE mic on KEY_VOICE, then arecord.
# persistent: hold arecord open from daemon start (only works if mic is always hot).
CAPTURE_MODE_PHASED = "phased"
CAPTURE_MODE_PERSISTENT = "persistent"
# self_stream: we power the remote mic ourselves via voiceinput/startStreaming
# on KEY_VOICE, with LG's setInputEvent (UI/TTS) blocked. Clean Grok-only path.
CAPTURE_MODE_SELF = "self_stream"
DEFAULT_CAPTURE_MODE = CAPTURE_MODE_PHASED
# Exact params captured from a live LG session (busctl/ls-monitor):
#   {"deviceType":"remote","keyType":"thinQtv","subscribe":true}
# Must stay subscribed for the mic to keep streaming, so we hold the luna-send
# child process open for the whole capture and kill it on release.
SELF_STREAM_URI = "luna://com.webos.service.voiceinput/startStreaming"
SELF_STREAM_STOP_URI = "luna://com.webos.service.voiceinput/stopStreaming"
SELF_STREAM_PARAMS = '{"deviceType":"remote","keyType":"thinQtv","subscribe":true}'
SELF_STREAM_STOP_PARAMS = '{"deviceType":"remote"}'
# voiceinput usually returns socketPath in the startStreaming ACK, but some
# firmwares only return subscribed/returnValue on the first line. The remote
# mic socket is always created at this conventional path.
DEFAULT_REMOTE_SOCKET_PATH = "/var/voiceinput/voiceinput_remote1.soc"
# True cold-mic (no live PCM). Quiet speech after gain can peak ~1000-3000;
# only treat near-silence as cold so we don't re-arm after a real utterance.
COLD_MIC_PEAK = 80
# Post-gain RMS that means "mic pipe is alive" (not room-noise floor).
# Keep this low so short commands spoken at button-press still arm capture
# before the user finishes ("launch Netflix" was dying with mic_hot ~3s).
DEFAULT_MIC_HOT_RMS = 80
# Post-gain RMS that counts as real user speech for endpointing / STT.
# Magic Remote speech after gain often sits at RMS 150–400 mid-phrase when
# the BLE path is weak (logs: peak~600, heard_speech never latched → no
# "launch BBC iPlayer"). Keep this modest so short app commands still fire.
DEFAULT_SPEECH_DETECT_RMS = 140
# Mic-open / BLE arm often produces a 20–80ms energy spike that is *not*
# speech. If we set heard_speech on that single frame, silence-endpoint ends
# the turn ~1–2s later with near-empty audio → empty STT → no app launch.
# Require this much continuous above-speech_rms energy before "real speech".
SPEECH_CONFIRM_SEC = 0.14
# When setInputEvent is blocked, voiceconductor never fans the startStreaming
# request out to the sound/preprocessor sub-services that actually route the
# BLE mic onto plughw:1,4. Fire them ourselves so the ALSA PCM goes hot.
SELF_STREAM_FANOUT_URIS = (
    "luna://com.webos.service.voiceinput.sound/startStreaming",
    "luna://com.webos.service.voiceinput.preprocessor/startStreaming",
)
SELF_STREAM_FANOUT_STOP_URIS = (
    "luna://com.webos.service.voiceinput.sound/stopStreaming",
    "luna://com.webos.service.voiceinput.preprocessor/stopStreaming",
)
DEFAULT_PHASED_STREAM_DELAY_SEC = 0.5
DEFAULT_SELF_STREAM_DELAY_SEC = 0.3
SELF_STREAM_DELAY_FLOOR_SEC = 0.08
DEFAULT_STREAM_READY_TIMEOUT_SEC = 3.0
DEFAULT_STREAM_HOT_PEAK = 80
LG_VOICE_SERVICES = ["voiceinput.service", "voiceconductor.service"]
VOICEINPUT_SERVICE = "voiceinput.service"
VOICECONDUCTOR_SERVICE = "voiceconductor.service"
NATIVE_VOICE_APP = "com.webos.app.voice"
VOICEAGENT_APP = "com.webos.app.voiceagent"


class AlsaCapture:
    """Persistent 16 kHz mono S16LE capture from an ALSA device via arecord.

    The Bluetooth remote mic is grabbed exclusively by LG's voice session when
    KEY_VOICE is pressed. If we open arecord only after the press, LG owns the
    real stream and we receive silence. So we open arecord ONCE at daemon start
    and keep a background reader draining it (auto-restarting if it exits), so we
    hold the device handle before LG's per-press session and receive the audio.
    Call arm() at the start of a capture window and disarm() at the end; read()
    returns queued PCM while armed.
    """

    def __init__(self, device: str) -> None:
        self.device = device
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._q: "collections.deque[bytes]" = collections.deque(maxlen=300)
        self._running = False
        self._armed = False
        self._dbg: Optional[Any] = None

    def _spawn(self) -> None:
        try:
            self._proc = subprocess.Popen(
                [
                    "arecord",
                    "-D", self.device,
                    "-f", "S16_LE",
                    "-r", str(CAPTURE_RATE),
                    "-c", "1",
                    "-t", "raw",
                    "-q",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001 - best effort
            print("[voice] arecord spawn failed: %s" % exc, flush=True)
            self._proc = None

    def start(self) -> None:
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while self._running:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
                if self._proc is None or not self._proc.stdout:
                    time.sleep(0.3)
                    continue
            data = self._proc.stdout.read(CAPTURE_CHUNK_BYTES)
            if not data:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
                time.sleep(0.2)
                continue
            if self._armed:
                self._q.append(data)
                if self._dbg is not None:
                    try:
                        self._dbg.write(data)
                    except Exception:
                        pass

    def arm(self) -> None:
        self._q.clear()
        try:
            self._dbg = open("/tmp/launch-home-voice-capture.raw", "wb")
        except Exception:
            self._dbg = None
        self._armed = True

    def disarm(self) -> None:
        self._armed = False
        if self._dbg is not None:
            try:
                self._dbg.close()
            except Exception:
                pass
            self._dbg = None

    def read(self, timeout: float = 0.6) -> bytes:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q:
                try:
                    return self._q.popleft()
                except IndexError:
                    pass
            if not self._running:
                return b""
            time.sleep(0.01)
        # No data this window: return silence so the capture loop stays alive
        # until the button is released (which flips self._active off).
        return b"\x00" * CAPTURE_CHUNK_BYTES

    def stop(self) -> None:
        self._running = False
        self.disarm()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None


class OneshotAlsaCapture:
    """Per-session arecord — opens only after startStreaming (self_stream path).

    Unlike AlsaCapture, never pre-opens at daemon start and never injects zero
    padding when the read queue is empty (which poisoned Grok STT).
    """

    def __init__(self, device: str) -> None:
        self.device = device
        self._proc: Optional[subprocess.Popen] = None
        self._dbg: Optional[Any] = None

    def start(self) -> None:
        self.stop()
        try:
            self._proc = subprocess.Popen(
                [
                    "arecord",
                    "-D",
                    self.device,
                    "-f",
                    "S16_LE",
                    "-r",
                    str(CAPTURE_RATE),
                    "-c",
                    "1",
                    "-t",
                    "raw",
                    "-q",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001 - best effort
            print("[voice] oneshot arecord spawn failed: %s" % exc, flush=True)
            self._proc = None

    def arm(self) -> None:
        try:
            self._dbg = open("/tmp/launch-home-voice-capture.raw", "wb")
        except Exception:
            self._dbg = None

    def disarm(self) -> None:
        if self._dbg is not None:
            try:
                self._dbg.close()
            except Exception:
                pass
            self._dbg = None

    def read(self, timeout: float = 0.6) -> bytes:
        if self._proc is None or self._proc.poll() is not None:
            return b""
        if not self._proc.stdout:
            return b""
        fd = self._proc.stdout.fileno()
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if not ready:
            return b""
        try:
            data = os.read(fd, CAPTURE_CHUNK_BYTES)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                return b""
            raise
        if not data:
            return b""
        if self._dbg is not None:
            try:
                self._dbg.write(data)
            except Exception:
                pass
        return data

    def stop(self) -> None:
        self.disarm()
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                if proc.stderr:
                    err = proc.stderr.read().decode("utf-8", "ignore").strip()
                    if err:
                        print("[voice] oneshot arecord stderr: %s" % err, flush=True)
            except Exception:
                pass


class SocketUnixCapture:
    """Read raw PCM straight from voiceinput's remote-mic unix socket.

    voiceinput/startStreaming returns socketPath (e.g.
    /var/voiceinput/voiceinput_remote1.soc). The BLE remote mic audio is
    delivered there as 16 kHz S16LE mono, INDEPENDENT of the ALSA/setInputEvent
    chain — so we get user speech with LG's assistant fully suppressed. Same
    interface as OneshotAlsaCapture so it is a drop-in for the capture loop.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._sock: Optional[socket.socket] = None
        self._dbg: Optional[Any] = None
        self._eof = False
        self._prebuffer: list[bytes] = []

    def start(self, wait_sec: float = 3.0) -> None:
        self.stop()
        self._eof = False
        self._prebuffer = []
        deadline = time.time() + max(0.1, float(wait_sec))
        while time.time() < deadline and not os.path.exists(self.socket_path):
            time.sleep(0.04)
        if not os.path.exists(self.socket_path):
            print(
                "[voice] socket capture: %s never appeared" % self.socket_path,
                flush=True,
            )
            self._sock = None
            return
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self.socket_path)
            s.setblocking(False)
            self._sock = s
            print(
                "[voice] socket capture connected %s" % self.socket_path,
                flush=True,
            )
        except OSError as exc:
            print("[voice] socket capture connect failed: %s" % exc, flush=True)
            self._sock = None

    def arm(self) -> None:
        try:
            self._dbg = open("/tmp/launch-home-voice-capture.raw", "wb")
        except Exception:
            self._dbg = None

    def disarm(self) -> None:
        if self._dbg is not None:
            try:
                self._dbg.close()
            except Exception:
                pass
            self._dbg = None

    def read(self, timeout: float = 0.6) -> bytes:
        if self._prebuffer:
            return self._prebuffer.pop(0)
        if self._sock is None:
            return b""
        sock = self._sock
        if sock is None:
            return b""
        ready, _, _ = select.select([sock], [], [], timeout)
        if not ready:
            return b""
        # stop() may clear _sock between select and recv (early silent close).
        sock = self._sock
        if sock is None:
            self._eof = True
            return b""
        try:
            data = sock.recv(CAPTURE_CHUNK_BYTES)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                return b""
            self._eof = True
            return b""
        except AttributeError:
            self._eof = True
            return b""
        if not data:
            self._eof = True
            return b""
        if self._dbg is not None:
            try:
                self._dbg.write(data)
            except Exception:
                pass
        return data

    def push_front(self, data: bytes) -> None:
        if data:
            self._prebuffer.insert(0, data)

    def stop(self) -> None:
        self.disarm()
        self._prebuffer = []
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def load_config(path: Optional[str] = None) -> dict[str, Any]:
    candidates = [
        path,
        os.environ.get("LH_VOICE_CONFIG"),
        DEFAULT_CONFIG_PATH,
        str(FALLBACK_CONFIG_PATH),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    required = (
                        ("capture_mode", CAPTURE_MODE_SELF),
                        ("disable_native_triggers", True),
                        ("self_stream_keep_voiceconductor", True),
                        ("use_socket_capture", True),
                        ("fresh_stream_per_press", False),
                    )
                    needs_migration = any(
                        data.get(key) != value for key, value in required
                    )
                    if str(data.get("capture_mode") or "").strip() != CAPTURE_MODE_SELF:
                        print(
                            "[voice] migrating capture_mode to self_stream",
                            flush=True,
                        )
                    for key, value in required:
                        data[key] = value
                    if needs_migration:
                        try:
                            p.write_text(
                                json.dumps(data, indent=2) + "\n",
                                encoding="utf-8",
                            )
                            print(
                                "[voice] persisted clean self_stream capture config",
                                flush=True,
                            )
                        except OSError:
                            pass
                    return data
                print(
                    "[voice] config root not an object in %s — trying next"
                    % p,
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] bad config %s: %s — trying next" % (p, exc),
                    flush=True,
                )
    # Last resort: keep daemon alive with empty config (keys missing → UI error).
    print(
        "[voice] WARNING: no valid config — starting with empty defaults",
        flush=True,
    )
    return {
        "xai_api_key": "xai-...",
        "ai_provider": "xai",
        "stt_language": "en",
        "chat_model": "grok-4.6",
        "voice_model": DEFAULT_VOICE_MODEL,
        "tts_enabled": True,
        "tts_voice": "iris",
        "tts_speed": 1.25,
        "web_search": True,
        "capture_mode": "self_stream",
        "disable_native_triggers": True,
        "self_stream_keep_voiceconductor": True,
        "use_socket_capture": True,
        "fresh_stream_per_press": False,
    }


class GrokVoiceDaemon:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.ai_provider = str(config.get("ai_provider") or "xai").strip().lower()
        if self.ai_provider not in ("xai", "gemini", "openrouter"):
            self.ai_provider = "xai"
        api_key = config.get("xai_api_key") or os.environ.get("XAI_API_KEY", "")
        if not api_key or api_key.startswith("xai-..."):
            # Stay alive without a key so the Luna service + settings UI remain
            # reachable; report the problem only when a voice session starts.
            api_key = ""
        self.api_key = api_key  # xAI key or SuperGrok access token
        self.auth_mode = supergrok_auth.normalize_auth_mode(config.get("auth_mode"))
        self._refresh_xai_bearer(log=True)
        gem_key = config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
        if not gem_key or str(gem_key).startswith("AIza..."):
            gem_key = ""
        self.gemini_api_key = gem_key
        self.gemini_model = config.get("gemini_model") or DEFAULT_GEMINI_MODEL
        self.gemini_stt_model = (
            config.get("gemini_stt_model") or DEFAULT_GEMINI_STT_MODEL
        )
        or_key = config.get("openrouter_api_key") or os.environ.get(
            "OPENROUTER_API_KEY", ""
        )
        if not or_key or str(or_key).startswith("sk-or-..."):
            or_key = ""
        self.openrouter_api_key = or_key
        self.openrouter_model = (
            config.get("openrouter_model") or DEFAULT_OPENROUTER_MODEL
        )
        self.openrouter_stt_model = (
            config.get("openrouter_stt_model") or DEFAULT_OPENROUTER_STT_MODEL
        )
        self.stt_language = config.get("stt_language", "en")
        # Software gain before STT. voiceinput can be quiet pre-AGC, but 4×
        # hard-clipped loud speech (peak=32768) and STT only heard "Molecule."
        # Default 2.0 + post-normalize targets ~14k peak without saturation.
        try:
            # Default 1.5 — 2.0–4.0 hard-clipped short commands into unintelligible
            # bursts (sample max 30k) and STT returned empty for "launch Netflix".
            self.capture_gain = float(config.get("capture_gain", 2.2) or 2.2)
        except (TypeError, ValueError):
            self.capture_gain = 2.2
        self.capture_gain = max(0.5, min(3.5, self.capture_gain))
        try:
            self.stt_target_peak = int(config.get("stt_target_peak", 14000) or 14000)
        except (TypeError, ValueError):
            self.stt_target_peak = 14000
        self.stt_target_peak = max(6000, min(24000, self.stt_target_peak))
        # Live STT during capture is the main source of wrong on-screen text
        # ("thank you", "molecule", BFD races). Default OFF: record the whole
        # utterance, then one full-buffer STT. Set stt_live_stream=true to
        # re-enable partials (less reliable on this TV).
        self.stt_live_stream = bool(config.get("stt_live_stream", False))
        # Prefer a warm startStreaming subscription across presses. Fresh arm
        # every press costs ~2–3s (stop+start+socket) and the user already
        # finished "what is a molecule" before mic_hot — STT then only sees a
        # blip ("You." / Chinese 哦). Only re-arm when the socket is actually dead.
        self.fresh_stream_per_press = bool(
            config.get("fresh_stream_per_press", False)
        )
        self.chat_model = config.get("chat_model", "grok-4.6")
        self.voice_model = (
            config.get("voice_model") or DEFAULT_VOICE_MODEL
        )
        self.tts_enabled = bool(config.get("tts_enabled", True))
        self.tts_voice = config.get("tts_voice") or DEFAULT_TTS_VOICE
        # Grok-first + internet fallback: pass xAI Live Search in "auto" mode so
        # Grok answers from its own model and only searches the web for what it
        # doesn't know. We label the answer source from the returned citations.
        # (Gemini path does not use xAI web_search.)
        self.web_search = bool(config.get("web_search", True))
        self.web_search_mode = config.get("web_search_mode", "auto")
        try:
            self.web_search_workers = int(config.get("web_search_workers", 10) or 10)
        except (TypeError, ValueError):
            self.web_search_workers = 10
        self.web_search_workers = max(2, min(20, self.web_search_workers))
        try:
            self.web_search_timeout_s = float(
                config.get("web_search_timeout_s", 3.5) or 3.5
            )
        except (TypeError, ValueError):
            self.web_search_timeout_s = 3.5
        self.web_search_timeout_s = max(1.5, min(12.0, self.web_search_timeout_s))
        try:
            self.web_search_max_results = int(
                config.get("web_search_max_results", 8) or 8
            )
        except (TypeError, ValueError):
            self.web_search_max_results = 8
        self.web_search_max_results = max(3, min(16, self.web_search_max_results))
        print(
            "[voice] ai_provider=%s xai_key=%s gemini_key=%s openrouter_key=%s "
            "web_search=%s workers=%d timeout=%.1fs"
            % (
                self.ai_provider,
                "yes" if self.api_key else "no",
                "yes" if self.gemini_api_key else "no",
                "yes" if self.openrouter_api_key else "no",
                self.web_search_mode if self.web_search else "off",
                self.web_search_workers,
                self.web_search_timeout_s,
            ),
            flush=True,
        )
        # Speech rate: applied via xAI TTS `speed` (0.7–1.5). webOS WebKit
        # cannot use <audio>.playbackRate (silent), and ffmpeg is often absent.
        try:
            # Slightly brisk default so answers feel snappy on the TV.
            self.tts_speed = float(config.get("tts_speed", 1.25) or 1.25)
        except (TypeError, ValueError):
            self.tts_speed = 1.25
        self.tts_speed = max(0.7, min(1.5, self.tts_speed))
        # Streaming-TTS state: a worker thread synthesizes queued sentence
        # chunks in order while the chat answer is still being generated.
        self._tts_queue: Optional["queue.Queue[Optional[str]]"] = None
        self._tts_thread: Optional[threading.Thread] = None
        self._tts_buffer = ""
        self._tts_pending = ""
        self._tts_seq = 0
        self._tts_first_chunk = True
        self._tts_audio_bytes = 0
        self._overlay_ready_for_audio = threading.Event()
        self._overlay_ready_for_audio.set()
        self._pending_relaunch_transcript: Optional[str] = None
        try:
            self.auto_dismiss_sec = float(
                config.get("overlay_auto_dismiss_sec", 12) or 12
            )
        except (TypeError, ValueError):
            self.auto_dismiss_sec = 12.0
        self.auto_dismiss_sec = max(4.0, min(60.0, self.auto_dismiss_sec))
        # When TTS is on, the overlay stays open until the spoken answer has
        # finished playing (the overlay reports playback completion), so the
        # fixed auto-dismiss above only bounds read-only (TTS off) sessions.
        # This is just a safety cap in case that completion signal is lost.
        self.tts_close_safety_sec = float(config.get("tts_close_safety_sec", 180))
        # Small grace after speech finishes so the last word isn't clipped and
        # the final text stays readable for a beat before the window closes.
        # How long to leave the card up after speech ends so the user can read
        # the question + answer (was 1s — felt like the app vanished instantly).
        try:
            self.tts_post_speech_sec = float(
                config.get("tts_post_speech_sec", 4.5) or 4.5
            )
        except (TypeError, ValueError):
            self.tts_post_speech_sec = 4.5
        self.tts_post_speech_sec = max(1.5, min(20.0, self.tts_post_speech_sec))
        self.hidraw_device = config.get("hidraw_device", "/dev/hidraw0")
        self.ffmpeg_path = config.get("ffmpeg_path")
        # Synthetic KEY_VOICE release: Magic Remote has no true hold. Too short
        # (1.5s) fired before mic_hot (~2.5s) and truncated "launch terminal".
        self.idle_release = float(config.get("button_idle_release_sec", 3.0))
        self.idle_release = max(3.0, min(6.0, self.idle_release))
        config["button_idle_release_sec"] = self.idle_release
        self.use_alsa_capture = bool(config.get("use_alsa_capture", True))
        # Prefer reading the BLE remote mic straight from voiceinput's unix
        # socket (proven to carry audio with setInputEvent blocked). Falls back
        # to ALSA arecord when disabled or no socketPath is returned.
        self.use_socket_capture = bool(config.get("use_socket_capture", True))
        self._stream_socket_path: Optional[str] = None
        self._press_mic_path: Optional[str] = None
        # After activateMrcu 0013/1003, LG toasts "check the connection status
        # of your Magic Controller". Do not call again until this timestamp.
        self._mrcu_skip_until = 0.0
        self.capture_device = config.get("capture_device", DEFAULT_CAPTURE_DEVICE)
        self.silence_rms = int(config.get("silence_rms", 350))
        # Real-speech gate (post-gain). Noise above silence_rms but below this
        # must NOT arm silence-endpointing or the UI sits on Listening for the
        # whole max_capture window while STT gets empty audio.
        try:
            self.speech_detect_rms = int(
                config.get("speech_detect_rms", DEFAULT_SPEECH_DETECT_RMS)
                or DEFAULT_SPEECH_DETECT_RMS
            )
        except (TypeError, ValueError):
            self.speech_detect_rms = DEFAULT_SPEECH_DETECT_RMS
        if self.speech_detect_rms > 420:
            self.speech_detect_rms = DEFAULT_SPEECH_DETECT_RMS
        self.speech_detect_rms = max(90, min(420, self.speech_detect_rms))
        try:
            self.mic_hot_rms = int(
                config.get("mic_hot_rms", DEFAULT_MIC_HOT_RMS) or DEFAULT_MIC_HOT_RMS
            )
        except (TypeError, ValueError):
            self.mic_hot_rms = DEFAULT_MIC_HOT_RMS
        if self.mic_hot_rms >= 400:
            self.mic_hot_rms = DEFAULT_MIC_HOT_RMS
        self.mic_hot_rms = max(60, min(300, self.mic_hot_rms))
        self.max_capture_sec = float(config.get("max_capture_sec", 15))
        # The Magic Remote KEY_VOICE button is momentary: webOS logs a release
        # ~0.5s after press regardless of how long it is physically held, so we
        # cannot use button release to bound capture. Instead we end the
        # utterance on our own silence detection.
        # Silence after speech: long enough for a breath mid-question, short
        # enough that room noise does not pin Listening for 10+ seconds.
        # End-of-utterance silence. Too short cuts "tell me | something funny"
        # at the breath between words (seen as 0.78s clip → STT "Tell me").
        self.end_silence_sec = float(config.get("end_silence_sec", 1.25))
        self.end_silence_sec = max(1.05, min(2.5, self.end_silence_sec))
        config["end_silence_sec"] = self.end_silence_sec
        # Do not allow silence-end until we have held long enough for a short
        # sentence ("tell me something funny" ≈ 1.5–2s of speech).
        self.min_capture_sec = float(config.get("min_capture_sec", 1.6))
        self.min_capture_sec = max(1.2, min(4.0, self.min_capture_sec))
        config["min_capture_sec"] = self.min_capture_sec
        self.speech_grace_sec = float(config.get("speech_grace_sec", 4.0))
        try:
            self.mic_hot_timeout_sec = float(
                config.get("mic_hot_timeout_sec", 5.0) or 5.0
            )
        except (TypeError, ValueError):
            self.mic_hot_timeout_sec = 5.0
        self.mic_hot_timeout_sec = max(2.0, min(12.0, self.mic_hot_timeout_sec))
        self.capture_mode = config.get("capture_mode", DEFAULT_CAPTURE_MODE)
        if self.capture_mode == CAPTURE_MODE_SELF:
            self.fresh_stream_per_press = False
            config["fresh_stream_per_press"] = False
        self.phased_stream_delay_sec = float(
            config.get("phased_stream_delay_sec", DEFAULT_PHASED_STREAM_DELAY_SEC)
        )
        self.self_stream_delay_sec = float(
            config.get(
                "self_stream_delay_sec",
                config.get("phased_stream_delay_sec", DEFAULT_SELF_STREAM_DELAY_SEC),
            )
        )
        if self.self_stream_delay_sec < SELF_STREAM_DELAY_FLOOR_SEC:
            self.self_stream_delay_sec = SELF_STREAM_DELAY_FLOOR_SEC
        self.self_stream_keep_voiceconductor = bool(
            config.get("self_stream_keep_voiceconductor", True)
        )
        self.stream_ready_timeout_sec = float(
            config.get("stream_ready_timeout_sec", DEFAULT_STREAM_READY_TIMEOUT_SEC)
        )
        self.stream_hot_peak = int(config.get("stream_hot_peak", DEFAULT_STREAM_HOT_PEAK))
        # LG's voice UI (com.webos.app.voice / voiceagent) launches a few seconds
        # AFTER the press, often after the user has released. Keep the renderer
        # killer running this long past release so the delayed UI is dismissed.
        self.renderer_kill_linger_sec = float(
            config.get("renderer_kill_linger_sec", 6.0)
        )
        self.disable_native_voice = bool(config.get("disable_native_voice", False))
        self.disable_native_triggers = bool(
            config.get("disable_native_triggers", self.disable_native_voice)
        )
        self.native_voice_services = (
            config.get("native_voice_service_ids") or list(DEFAULT_NATIVE_VOICE_SERVICES)
        )
        # Union config close-list with built-in defaults so older config.json
        # still kills new LG surfaces (e.g. beanbrowser) without a manual edit.
        from luna_service import NATIVE_AI_APPS as _NATIVE_DEFAULTS

        cfg_close = list(config.get("native_close_app_ids") or [])
        native_close_ids: list[str] = []
        for app_id in list(_NATIVE_DEFAULTS) + cfg_close:
            if app_id and app_id not in native_close_ids:
                native_close_ids.append(app_id)
        # Bean browser can appear ~6–10s after KEY_VOICE; keep polling a bit.
        close_poll = float(config.get("native_aiplatform_close_poll_sec", 12))
        if close_poll < 10:
            close_poll = 12.0
        self.luna = LunaBridge(
            # Always Launch Home's own card, whatever an old config says.
            overlay_app_id=OVERLAY_APP_ID,
            close_native=bool(config.get("close_native_aiplatform", True)),
            native_close_poll_sec=close_poll,
            native_app_ids=native_close_ids,
            close_voiceagent_during_capture=(
                self.capture_mode != CAPTURE_MODE_SELF
                and bool(config.get("close_voiceagent_during_capture", False))
            ),
        )
        print(
            "[voice] native close targets: %s" % ", ".join(native_close_ids),
            flush=True,
        )
        # Serve settings RPCs from the sandboxed overlay over the WS channel,
        # which is not subject to the webOS ACG private bus that blocks the
        # non-root app's luna-send calls.
        self.luna.config_handler = self._handle_config_rpc
        self._session_lock = threading.Lock()
        self._session_gen = 0
        self._session_cancel = threading.Event()
        self._app_handoff_gen = 0
        self._ignore_voice_until_ts = 0.0
        # Best live STT hypothesis painted during capture (worker ↔ finalize).
        self._live_best_lock = threading.Lock()
        self._live_best_text = ""
        self._live_best_gen = 0
        # Mid-capture silent TV command (app launch / mute / volume / HDMI).
        self._early_silent_lock = threading.Lock()
        self._early_silent_fired_gen = 0
        self._last_early_silent_text = ""
        self._stt_streamed_gen = 0
        self._native_ui_suppress_ts = 0.0
        self._lg_cancel_ts = 0.0
        self._active = False
        self._dismiss_timer: Optional[threading.Timer] = None
        self._capture: Optional[AlsaCapture] = None
        self._renderer_killer_stop: Optional[threading.Event] = None
        self._renderer_killer_thread: Optional[threading.Thread] = None
        self._renderer_killer_linger: Optional[threading.Timer] = None
        self._stream_proc: Optional[subprocess.Popen] = None
        self._stream_subscription_ok = False
        self._socket_session_active = False
        self._release_requested = False
        # Speaking-session close coordination: when TTS actually produced audio
        # we wait for the overlay to report playback finished before closing,
        # rather than the fixed auto-dismiss timer.
        self._awaiting_playback = False
        self._tts_spoke = False
        self._pcm_chunks: list[bytes] = []
        # Serialize STT WebSocket connects so pre-hot + capture threads never
        # open two sessions and close each other's socket (Bad file descriptor).
        self._stt_connect_lock = threading.Lock()
        self._stt_streamed = False
        self._stt_send_errors = False
        self._button = ButtonWatcher(self._on_button, idle_release_sec=self.idle_release)
        self._stream = HidrawVoiceStream(
            self.hidraw_device,
            idle_stop=self.idle_release,
            on_activity=self._button.notify_voice_activity,
        )

    def _handle_config_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Serve overlay settings RPCs over the WS channel.

        Mirrors the response shapes of luna-service/index.js so app.js can use
        the same result fields regardless of transport.
        """
        if method == "getConfig":
            return {"returnValue": True, "config": config_store.get_public_config()}
        if method == "getApiKey":
            cfg = config_store.load_config()
            xai = cfg.get("xai_api_key")
            gem = cfg.get("gemini_api_key")
            return {
                "returnValue": True,
                "xai_api_key": ""
                if config_store.is_placeholder_key(xai)
                else (xai or ""),
                "gemini_api_key": ""
                if config_store.is_placeholder_key(gem)
                else (gem or ""),
                "openrouter_api_key": ""
                if config_store.is_placeholder_key(cfg.get("openrouter_api_key"))
                else (cfg.get("openrouter_api_key") or ""),
            }
        if method == "getStatus":
            status = config_store.get_status()
            status["returnValue"] = True
            try:
                status["wsListening"] = bool(
                    status.get("wsListening")
                    or getattr(self.luna, "ws_listening", False)
                )
            except Exception:
                pass
            return status
        if method == "startSuperGrokLogin":
            info = supergrok_auth.start_login()
            return {"returnValue": True, **info}
        if method == "cancelSuperGrokLogin":
            supergrok_auth.cancel_login()
            return {"returnValue": True}
        if method == "signOutSuperGrok":
            supergrok_auth.clear_session()
            cfg = config_store.load_config()
            cfg["auth_mode"] = "API_KEY"
            config_store.write_config(cfg)
            self.auth_mode = "API_KEY"
            self._refresh_xai_bearer()
            return {"returnValue": True, "config": config_store.get_public_config()}
        if method == "importSuperGrokAuth":
            path = str((params or {}).get("path") or "").strip() or None
            info = supergrok_auth.import_cli_auth(path)
            cfg = config_store.load_config()
            cfg["auth_mode"] = "SUPERGROK_OAUTH"
            config_store.write_config(cfg)
            self.auth_mode = "SUPERGROK_OAUTH"
            self._refresh_xai_bearer()
            return {
                "returnValue": True,
                "signed_in": bool(info.get("signed_in")),
                "email": info.get("email") or "",
                "config": config_store.get_public_config(),
            }
        if method == "playbackEnded":
            # The overlay finished playing every spoken chunk; close now (plus a
            # short grace) instead of waiting on the fixed auto-dismiss timer.
            print("[voice] playbackEnded from overlay", flush=True)
            self._on_playback_ended()
            return {"returnValue": True}
        if method == "setConfig":
            params = params or {}
            old = config_store.load_config()
            _cfg, changed = config_store.apply_updates(params)
            public = config_store.get_public_config()
            if not changed:
                return {
                    "returnValue": True,
                    "saved": False,
                    "restarting": False,
                    "config": public,
                }
            # Keys that require a full process restart (capture stack / auth).
            restart_keys = {
                "ai_provider",
                "auth_mode",
                "xai_api_key",
                "gemini_api_key",
                "openrouter_api_key",
                "capture_mode",
                "capture_device",
                "hidraw_device",
                "use_socket_capture",
                "use_alsa_capture",
                "fresh_stream_per_press",
                "self_stream_keep_voiceconductor",
                "disable_native_triggers",
            }
            needs_restart = any(
                k in params and old.get(k) != _cfg.get(k) for k in restart_keys
            )
            # API key fields may be under placeholder-filtered updates.
            if (
                "xai_api_key" in params
                or "gemini_api_key" in params
                or "openrouter_api_key" in params
                or "auth_mode" in params
            ):
                needs_restart = True
            if needs_restart:
                threading.Timer(1.0, self._restart_service).start()
                return {
                    "returnValue": True,
                    "saved": True,
                    "restarting": True,
                    "config": public,
                }
            self._apply_live_config(_cfg)
            return {
                "returnValue": True,
                "saved": True,
                "restarting": False,
                "config": public,
            }
        if method == "launchApp":
            # Dev/test helper: launch a TV app by voice phrase or raw app id.
            phrase = str(params.get("phrase") or "").strip()
            app_id = str(params.get("id") or "").strip()
            if phrase:
                result = tv_control.handle_tv_command(phrase)
                if result is None:
                    return {"returnValue": False, "errorText": "not a TV command"}
                return {
                    "returnValue": bool(result.ok),
                    "spoken": result.spoken,
                    "kind": result.kind,
                    "detail": result.detail,
                }
            if app_id:
                from luna_service import _luna_send

                resp = _luna_send(
                    "luna://com.webos.applicationManager/launch",
                    {"id": app_id, "noSplash": True},
                    timeout=12.0,
                )
                return {"returnValue": bool(resp.get("returnValue")), "detail": resp}
            return {"returnValue": False, "errorText": "phrase or id required"}
        raise ValueError("unknown method: %s" % method)

    def _apply_live_config(self, cfg: dict[str, Any]) -> None:
        """Hot-apply settings that do not require reopening capture hardware."""
        try:
            self.auth_mode = supergrok_auth.normalize_auth_mode(cfg.get("auth_mode"))
            self._refresh_xai_bearer()
            self.tts_enabled = bool(cfg.get("tts_enabled", True))
            self.tts_voice = cfg.get("tts_voice") or self.tts_voice
            try:
                self.tts_speed = max(
                    0.7, min(1.5, float(cfg.get("tts_speed", self.tts_speed) or 1.0))
                )
            except (TypeError, ValueError):
                pass
            self.stt_language = str(cfg.get("stt_language") or self.stt_language or "en")
            self.chat_model = cfg.get("chat_model") or self.chat_model
            self.voice_model = (
                cfg.get("voice_model")
                or getattr(self, "voice_model", DEFAULT_VOICE_MODEL)
            )
            self.gemini_model = cfg.get("gemini_model") or getattr(
                self, "gemini_model", DEFAULT_GEMINI_MODEL
            )
            self.gemini_stt_model = cfg.get("gemini_stt_model") or getattr(
                self, "gemini_stt_model", DEFAULT_GEMINI_STT_MODEL
            )
            or_key = cfg.get("openrouter_api_key") or ""
            if or_key and not str(or_key).startswith("sk-or-...") and "•" not in str(or_key):
                self.openrouter_api_key = or_key
            self.openrouter_model = cfg.get("openrouter_model") or getattr(
                self, "openrouter_model", DEFAULT_OPENROUTER_MODEL
            )
            self.openrouter_stt_model = cfg.get("openrouter_stt_model") or getattr(
                self, "openrouter_stt_model", DEFAULT_OPENROUTER_STT_MODEL
            )
            self.web_search = bool(cfg.get("web_search", self.web_search))
            self.web_search_mode = cfg.get("web_search_mode", self.web_search_mode)
            try:
                self.web_search_workers = max(
                    2, min(20, int(cfg.get("web_search_workers", self.web_search_workers)))
                )
            except (TypeError, ValueError, AttributeError):
                pass
            try:
                self.web_search_timeout_s = max(
                    1.5,
                    min(
                        12.0,
                        float(
                            cfg.get(
                                "web_search_timeout_s",
                                getattr(self, "web_search_timeout_s", 3.5),
                            )
                        ),
                    ),
                )
            except (TypeError, ValueError, AttributeError):
                pass
            try:
                self.auto_dismiss_sec = max(
                    4.0,
                    min(
                        60.0,
                        float(
                            cfg.get("overlay_auto_dismiss_sec", self.auto_dismiss_sec)
                        ),
                    ),
                )
            except (TypeError, ValueError):
                pass
            try:
                self.tts_post_speech_sec = max(
                    1.5,
                    min(
                        20.0,
                        float(
                            cfg.get("tts_post_speech_sec", self.tts_post_speech_sec)
                        ),
                    ),
                )
            except (TypeError, ValueError):
                pass
            self.luna.close_native = bool(
                cfg.get("close_native_aiplatform", self.luna.close_native)
            )
            print(
                "[voice] live config applied (no restart) voice=%s "
                "voice_model=%s speed=%.2f lang=%s search=%s"
                % (
                    self.tts_voice,
                    getattr(self, "voice_model", DEFAULT_VOICE_MODEL),
                    self.tts_speed,
                    self.stt_language,
                    self.web_search,
                ),
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print("[voice] live config apply failed: %s" % exc, flush=True)

    @staticmethod
    def _restart_service() -> None:
        ok, output = config_store.restart_daemon()
        if not ok:
            print("[voice] daemon restart failed: %s" % output, flush=True)

    def run(self) -> None:
        self.luna.start()
        # Button watcher + native-UI suppression MUST come up before any
        # startStreaming / recovery work. KEY_VOICE during a multi-second
        # recovery previously reached LG only (no ButtonWatcher yet, disable
        # flag briefly cleared) and left com.webos.app.voice stuck bottom-left.
        if self.disable_native_triggers or self.capture_mode == CAPTURE_MODE_SELF:
            # Prevent the "LG voice recognition feature is turned off" dialog
            # (voiceconductor shows it when the option is off). Then block only
            # setInputEvent so LG does not paint its own Listening UI.
            self._pin_lg_voice_recognition_enabled()
            self._disable_native_triggers()
            # Cover startup only. Each KEY_VOICE press re-arms the closer
            # (button handler), so it no longer runs for the daemon's
            # lifetime (that cost ~30% of the TV's CPU when idle).
            self.luna.engage_voiceagent_suppression(seconds=30.0)
            threading.Thread(
                target=self._native_trigger_guard_loop,
                daemon=True,
                name="native-trigger-guard",
            ).start()
            # Keep mic on + LG NLP off. Interval 30s so a Settings toggle
            # cannot leave LG concierge exclusive for long.
            def _voice_setting_guard() -> None:
                while True:
                    time.sleep(30.0)
                    try:
                        self._pin_lg_voice_recognition_enabled()
                        self._disable_native_triggers()
                    except Exception:
                        pass

            threading.Thread(
                target=_voice_setting_guard,
                daemon=True,
                name="lg-voice-setting-guard",
            ).start()
        self._button.start()
        phased = self.capture_mode == CAPTURE_MODE_PHASED
        self_stream = self.capture_mode == CAPTURE_MODE_SELF
        if self_stream:
            # Warm stack: keep voiceconductor + voiceinput running so BLE voice
            # device stays registered. Block trigger setInputEvent only; we call
            # startStreaming ourselves on each press.
            self._disable_native_triggers()
            if self.self_stream_keep_voiceconductor:
                # Start services WITHOUT clearing the disable flag (unlike
                # _ensure_lg_voice_stack, which is for phased mode only).
                self._ensure_lg_voice_services()
                self._disable_native_triggers()
                print(
                    "[voice] capture_mode=self_stream (warm stack) — voiceconductor stays up, setInputEvent blocked",
                    flush=True,
                )
            else:
                self._stop_native_voice()
                self._ensure_voiceinput_service()
                threading.Thread(target=self._native_voice_watchdog, daemon=True).start()
                print(
                    "[voice] capture_mode=self_stream (cold) — voiceconductor stopped",
                    flush=True,
                )
                # Cold path only: reset BLE routing once. In warm-stack mode we
                # ride LG's live registration, so cycling voiceinput here would
                # only risk wedging it (error 10000).
                self._recover_voice_stack("daemon startup")
            print(
                "[voice] self_stream delay=%.2fs ready_timeout=%.1fs"
                % (self.self_stream_delay_sec, self.stream_ready_timeout_sec),
                flush=True,
            )
            if self.use_socket_capture:
                # Do NOT startStreaming at boot — remote is usually asleep
                # (error 10000) and a leftover stream leaves the mic LED on
                # with no user press. Arm only on KEY_VOICE via
                # _arm_remote_mic_socket. Keepalive only stops stale streams.
                print(
                    "[voice] mic idle at boot — arm on first KEY_VOICE only",
                    flush=True,
                )
                threading.Thread(
                    target=self._mic_keepalive_loop, daemon=True, name="mic-keepalive"
                ).start()
        elif phased:
            self._ensure_lg_voice_stack()
            print(
                "[voice] capture_mode=phased delay=%.2fs — LG stack stays up for mic enable"
                % self.phased_stream_delay_sec,
                flush=True,
            )
        else:
            if self.disable_native_triggers:
                self._disable_native_triggers()
            if self.disable_native_voice:
                self._stop_native_voice()
                threading.Thread(target=self._native_voice_watchdog, daemon=True).start()
        # Opening hidraw is best-effort: the device may not exist until the
        # Magic Remote is paired/awake. It is (re)opened lazily on capture, so
        # a failure here must not take the daemon (and Luna service) down.
        try:
            self._stream.open(wait=0.0)
        except OSError as exc:
            print(
                f"[voice] hidraw {self.hidraw_device} not ready yet: {exc}",
                file=sys.stderr,
                flush=True,
            )
        if self.use_alsa_capture and self.capture_mode == CAPTURE_MODE_PERSISTENT:
            # Persistent mode: grab ALSA before LG's per-press session (often silent
            # when the BLE mic is gated until startStreaming runs).
            self._capture = AlsaCapture(self.capture_device)
            self._capture.start()
            print(
                "[voice] persistent mic capture started dev=%s"
                % self.capture_device,
                flush=True,
            )
        # One-shot TV-control Luna health probe (Phase 2). Does not change state.
        try:
            vol, muted = tv_control._get_volume()
            print(
                "[voice] tv_control probe volume=%s muted=%s"
                % (vol, muted),
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print("[voice] tv_control probe failed: %s" % exc, flush=True)
        print("[voice] ready — press KEY_VOICE on Magic Remote", flush=True)
        try:
            self.luna.write_voice_state(False, reason="daemon_start")
        except Exception:
            pass
        # Warm installed-app cache off the critical path (free-form "open X").
        def _warm_apps() -> None:
            try:
                n = tv_control.refresh_installed_apps()
                print(
                    "[voice] installed-app cache warm count=%d" % n,
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] installed-app cache warm failed: %s" % exc,
                    flush=True,
                )

        threading.Thread(
            target=_warm_apps, daemon=True, name="warm-app-cache"
        ).start()
        # Pre-warm STT WebSocket so the first press does not pay 10–16s TLS.
        self._schedule_stt_warm(reason="boot")
        # No voice-card pre-warm here: launching the card at start-up put a
        # full-screen card over whatever was on screen (seen after every boot)
        # and nothing closed it. The first press starts the card instead.
        try:
            tick = 0
            while True:
                time.sleep(1)
                tick += 1
                # systemd WatchdogSec=180: keep-alive when NOTIFY_SOCKET is set.
                if tick % 30 == 0:
                    try:
                        from systemd import daemon as _sd  # type: ignore

                        _sd.notify("WATCHDOG=1")
                    except Exception:
                        # No python-systemd on TV: poke the notify socket manually.
                        try:
                            import os as _os
                            import socket as _sock

                            addr = _os.environ.get("NOTIFY_SOCKET")
                            if addr:
                                s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM)
                                if addr.startswith("@"):
                                    s.connect("\0" + addr[1:])
                                else:
                                    s.connect(addr)
                                s.sendall(b"WATCHDOG=1")
                                s.close()
                        except Exception:
                            pass
                # Re-assert native trigger disable file (other tools may remove it).
                if tick % 45 == 0 and (
                    self.disable_native_triggers
                    or self.capture_mode == CAPTURE_MODE_SELF
                ):
                    try:
                        self._disable_native_triggers()
                    except Exception:
                        pass
        except KeyboardInterrupt:
            print("[voice] shutting down", flush=True)
        finally:
            self._stop_renderer_killer()
            self._button.stop()
            if self._capture is not None:
                self._capture.stop()
                self._capture = None
            self._stream.close()
            self.luna.stop()

    def _ensure_voiceinput_service(self) -> None:
        try:
            # Fast path: already active — never block 10s on systemctl start
            # (that froze "launch terminal" mid-press with Reconnecting UI).
            active = (
                subprocess.run(
                    ["systemctl", "is-active", VOICEINPUT_SERVICE],
                    capture_output=True,
                    text=True,
                    timeout=2,
                ).stdout.strip()
                == "active"
            )
            if not active:
                subprocess.run(
                    ["systemctl", "start", VOICEINPUT_SERVICE],
                    check=False,
                    timeout=4,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                active = (
                    subprocess.run(
                        ["systemctl", "is-active", VOICEINPUT_SERVICE],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    ).stdout.strip()
                    == "active"
                )
            print(
                "[voice] %s: %s"
                % (VOICEINPUT_SERVICE, "active" if active else "not active"),
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort
            print(
                "[voice] could not start %s: %s" % (VOICEINPUT_SERVICE, exc),
                flush=True,
            )

    def _ensure_lg_voice_services(self) -> None:
        """Start voiceinput/voiceconductor without touching the disable flag.

        Used by self_stream warm mode: we need the services for BLE mic
        registration, but must NEVER clear
        /tmp/voiceinput_disable_set_input_event or KEY_VOICE will open LG's
        native AI popup.
        """
        for svc in LG_VOICE_SERVICES:
            try:
                subprocess.run(
                    ["systemctl", "start", svc],
                    check=False,
                    timeout=10,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                print("[voice] could not start %s: %s" % (svc, exc), flush=True)

    def _ensure_lg_voice_stack(self) -> None:
        """Let LG run startStreaming on KEY_VOICE (removes suppression flags).

        Only for capture_mode=phased, where we intentionally ride LG's own
        KEY_VOICE path. Do NOT call this from self_stream warm mode.
        """
        try:
            Path(NATIVE_SET_INPUT_EVENT_DISABLE_FILE).unlink(missing_ok=True)
            print(
                "[voice] LG voice triggers enabled (removed %s)"
                % NATIVE_SET_INPUT_EVENT_DISABLE_FILE,
                flush=True,
            )
        except OSError as exc:
            print(
                "[voice] could not remove %s: %s"
                % (NATIVE_SET_INPUT_EVENT_DISABLE_FILE, exc),
                flush=True,
            )
        self._ensure_lg_voice_services()

    def _start_renderer_killer(self) -> None:
        """Kill native voice UI renderers for the whole capture window."""
        if self._renderer_killer_linger is not None:
            self._renderer_killer_linger.cancel()
            self._renderer_killer_linger = None
        self._stop_renderer_killer()
        stop = threading.Event()
        self._renderer_killer_stop = stop

        def _loop() -> None:
            while not stop.is_set():
                if self._active:
                    # Visual-only close during live capture. cancelRecognition
                    # mid-stream kills BLE PCM (peak collapses to ~400 and STT
                    # returns empty — "launch BBC iPlayer" never matches).
                    LunaBridge.close_system_ui(VOICEAGENT_APP)
                    for app_id in (
                        "com.webos.app.assistant",
                        "com.webos.app.voice",
                        "com.webos.app.alert",
                    ):
                        LunaBridge.close_system_ui(app_id)
                # Only close SAM/browser voice-result apps after capture.
                # The SystemUI voiceagent is handled separately with a safe
                # visual-only sysuicompmgr close during the live turn.
                # Also skip while a voice turn is live: closing beanbrowser
                # drops voice card's WebSocket so answers never arrive.
                if not self._active and not getattr(
                    self.luna, "_voice_session_live", False
                ):
                    LunaBridge.close_system_ui(VOICEAGENT_APP)
                    for app_id in self.luna.native_app_ids:
                        if app_id == VOICEAGENT_APP:
                            continue
                        LunaBridge._kill_renderer(app_id)
                        self._luna_once(
                            "luna://com.webos.applicationManager/closeByAppId",
                            json.dumps({"id": app_id}),
                            timeout=3.0,
                        )
                stop.wait(0.25)

        self._renderer_killer_thread = threading.Thread(target=_loop, daemon=True)
        self._renderer_killer_thread.start()

    def _stop_renderer_killer(self) -> None:
        if self._renderer_killer_stop is not None:
            self._renderer_killer_stop.set()
            self._renderer_killer_stop = None
        if self._renderer_killer_thread is not None:
            self._renderer_killer_thread.join(timeout=1.0)
            self._renderer_killer_thread = None

    def _pin_lg_voice_recognition_enabled(self) -> None:
        """Pin Magic Remote mic path ON so BLE audio reaches voiceinput.

        Important: do **not** force ``voiceRecognition`` off. On this TV that
        starves the remote-mic PCM (socket peak~400, empty STT) even with
        startStreaming subscribed. voice card exclusivity comes from
        ``disable_native_triggers`` (setInputEvent block) + UI close hammers,
        not from turning the whole LG voice stack off.
        """
        try:
            from luna_service import _luna_send  # type: ignore
        except Exception as exc:  # noqa: BLE001
            print(
                "[voice] pin LG voice settings import failed: %s" % exc,
                flush=True,
            )
            return

        mic = ""
        try:
            cur = _luna_send(
                "luna://com.webos.settingsservice/getSystemSettings",
                {"category": "voiceframework"},
                timeout=4.0,
            )
            settings = cur.get("settings") or {}
            mic = str(settings.get("voice_mic") or "").lower()
        except Exception:
            mic = ""

        # Also read option category flags for mic-related keys.
        try:
            import json as _json

            opt = _json.load(open("/var/luna/preferences/option"))
            if not mic:
                mic = str(opt.get("voice_mic") or "").lower()
        except Exception:
            opt = {}

        need_mic = mic not in ("on", "true", "1")
        # Ensure recognition/mic stack can deliver PCM (was ON when launches worked).
        need_rec = True
        try:
            if (
                str(opt.get("voiceRecognition") or "").lower() in ("on", "true", "1")
                and opt.get("enableVoiceRecognition") is not False
                and str(opt.get("voiceRecognitionEnable") or "on").lower()
                in ("on", "true", "1")
            ):
                need_rec = False
        except Exception:
            need_rec = True

        if not need_mic and not need_rec:
            if not getattr(self, "_lg_voice_mic_ok", False):
                print(
                    "[voice] LG voice_mic + recognition already on (audio path)",
                    flush=True,
                )
            self._lg_voice_mic_ok = True
            return

        print(
            "[voice] pinning LG voice audio path (mic=%s rec_need=%s)"
            % (mic or "?", need_rec),
            flush=True,
        )
        try:
            import hashlib
            import json as _json

            path = "/var/luna/preferences/option"
            d = _json.load(open(path))
            d["voice_mic"] = "on"
            d["voiceRecognition"] = "on"
            d["voiceRecognitionEnable"] = "on"
            d["enableVoiceRecognition"] = True
            d["useVoiceRecognition"] = True
            raw = _json.dumps(d, separators=(",", ":"), ensure_ascii=False)
            open(path, "w").write(raw)
            open(path + ".md5", "w").write(
                "%s  %s\n" % (hashlib.md5(raw.encode()).hexdigest(), path)
            )
        except Exception as exc:  # noqa: BLE001
            print("[voice] pin option file failed: %s" % exc, flush=True)

        for category, settings in (
            ("voiceframework", {"voice_mic": "on"}),
            (
                "option",
                {
                    "voice_mic": "on",
                    "voiceRecognition": "on",
                    "voiceRecognitionEnable": "on",
                    "enableVoiceRecognition": True,
                    "useVoiceRecognition": True,
                },
            ),
        ):
            try:
                resp = _luna_send(
                    "luna://com.webos.settingsservice/setSystemSettings",
                    {"category": category, "settings": settings},
                    timeout=4.0,
                )
                print(
                    "[voice] pin LG %s -> %s"
                    % (category, resp.get("returnValue")),
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] pin LG %s failed: %s" % (category, exc),
                    flush=True,
                )
        self._lg_voice_mic_ok = True
        # Swallow "feature has been turned on" toast / off dialog.
        try:
            LunaBridge = type(self.luna)  # noqa: N806
            self.luna.dismiss_voice_recognition_off_dialog(seconds=5.0)
            for _ in range(15):
                LunaBridge.close_system_ui("com.webos.app.toast")
                LunaBridge.close_system_ui("com.webos.app.alert")
                time.sleep(0.08)
        except Exception:
            pass

    def _disable_native_triggers(self) -> None:
        """Disable trigger.alexa/thinq setInputEvent via LG's /tmp watch flag.

        Reversible: rm /tmp/voiceinput_disable_set_input_event
        """
        try:
            flag = Path(NATIVE_SET_INPUT_EVENT_DISABLE_FILE)
            if flag.is_file():
                return
            flag.touch()
            print(
                "[voice] native trigger setInputEvent disabled (%s)"
                % NATIVE_SET_INPUT_EVENT_DISABLE_FILE,
                flush=True,
            )
        except OSError as exc:
            print(
                "[voice] could not create %s: %s"
                % (NATIVE_SET_INPUT_EVENT_DISABLE_FILE, exc),
                flush=True,
            )

    def _native_triggers_blocked(self) -> bool:
        try:
            return Path(NATIVE_SET_INPUT_EVENT_DISABLE_FILE).is_file()
        except OSError:
            return False

    def _native_trigger_guard_loop(self) -> None:
        while True:
            try:
                if not self._native_triggers_blocked():
                    self._disable_native_triggers()
                    print(
                        "[voice] native trigger guard restored",
                        flush=True,
                    )
            except Exception:
                pass
            time.sleep(1.0)

    def _stop_native_voice(self) -> None:
        """Stop LG's native voice services so KEY_VOICE only shows our overlay.

        Reversible: a reboot (or removing our app) restores them, since we never
        mask or delete anything on disk.
        """
        for svc in self.native_voice_services:
            try:
                subprocess.run(
                    ["systemctl", "stop", svc],
                    check=False,
                    timeout=10,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                print(f"[voice] could not stop {svc}: {exc}", flush=True)
        print(
            "[voice] native voice services stopped: "
            + ", ".join(self.native_voice_services),
            flush=True,
        )

    def _native_voice_watchdog(self) -> None:
        """Keep native voice services down in case something restarts them."""
        while True:
            time.sleep(5)
            for svc in self.native_voice_services:
                try:
                    active = (
                        subprocess.run(
                            ["systemctl", "is-active", svc],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        ).stdout.strip()
                        == "active"
                    )
                except Exception:
                    active = False
                if active:
                    try:
                        subprocess.run(
                            ["systemctl", "stop", svc],
                            check=False,
                            timeout=10,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception:
                        pass

    def _start_native_streaming(self) -> bool:
        """Power the remote mic ourselves (no LG UI/TTS). Keep subscription open."""
        self._stream_subscription_ok = False
        self._stop_voiceinput_stream()
        self._ensure_voiceinput_service()
        try:
            self._stream_proc = subprocess.Popen(
                ["luna-send", "-i", SELF_STREAM_URI, SELF_STREAM_PARAMS],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            threading.Thread(
                target=self._log_stream_subscription,
                args=(self._stream_proc,),
                daemon=True,
            ).start()
            print("[voice] voiceinput/startStreaming subscribe started", flush=True)
            # ALWAYS fan-out when setInputEvent is blocked. Previously we only
            # fanned when voiceconductor was stopped — but warm-stack mode keeps
            # conductor up *and* blocks setInputEvent, so sound/preprocessor
            # never got startStreaming and PCM stayed near-silent (peak~400,
            # heard_speech=False, empty STT → "launch BBC" did nothing).
            def _fanout_when_ready() -> None:
                if self._wait_stream_subscription(2.5):
                    self._start_stream_fanout()
                elif self.disable_native_triggers or self.capture_mode == CAPTURE_MODE_SELF:
                    # Still try once — some builds ACK late but accept fan-out.
                    time.sleep(0.3)
                    self._start_stream_fanout()

            threading.Thread(
                target=_fanout_when_ready, daemon=True, name="stream-fanout"
            ).start()
            return True
        except Exception as exc:  # noqa: BLE001 - best-effort
            print("[voice] startStreaming failed: %s" % exc, flush=True)
            return False

    def _start_stream_fanout(self) -> None:
        """Force the ALSA-routing sub-services on when setInputEvent is blocked.

        voiceconductor normally fans startStreaming out to voiceinput.sound
        (plughw:1,4 routing) and voiceinput.preprocessor. With the setInputEvent
        disable flag set, that fan-out never runs and the PCM stays silent.
        These are best-effort; sub-services that reject are logged, not fatal.
        """
        print("[voice] stream fanout → sound + preprocessor", flush=True)
        for uri in SELF_STREAM_FANOUT_URIS:
            resp = self._luna_once(uri, SELF_STREAM_PARAMS, timeout=4.0)
            print(
                "[voice] fanout %s -> %s"
                % (
                    uri.rsplit("/", 2)[-2],
                    (resp or {}).get("returnValue", resp),
                ),
                flush=True,
            )

    def _stop_stream_fanout(self) -> None:
        for uri in SELF_STREAM_FANOUT_STOP_URIS:
            self._luna_once(uri, SELF_STREAM_STOP_PARAMS, timeout=4.0)

    def _log_stream_subscription(self, proc: subprocess.Popen) -> None:
        """Log first Luna response so silent subscribe failures are visible."""
        if not proc.stdout:
            return
        try:
            line = proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "ignore").strip()
            print("[voice] startStreaming response: %s" % text, flush=True)
            try:
                payload = json.loads(text)
                ok = bool(payload.get("returnValue", False))
                sp = payload.get("socketPath")
                if sp:
                    self._stream_socket_path = sp
                elif ok:
                    # Subscribed but path omitted — use the conventional remote
                    # socket so we don't treat a good stream as rejected.
                    self._stream_socket_path = (
                        self._stream_socket_path or DEFAULT_REMOTE_SOCKET_PATH
                    )
                    print(
                        "[voice] startStreaming ok without socketPath — "
                        "using %s" % self._stream_socket_path,
                        flush=True,
                    )
                self._stream_subscription_ok = ok
                if not ok:
                    err = payload.get("errorText") or payload.get("errorCode") or "unknown"
                    print("[voice] startStreaming REJECTED: %s" % err, flush=True)
            except json.JSONDecodeError:
                pass
        except Exception as exc:  # noqa: BLE001 - debug only
            print("[voice] startStreaming log error: %s" % exc, flush=True)

    def _ensure_stream_ready(self, wait_sec: float = 2.0) -> bool:
        """Wait for a successful startStreaming ACK and a usable socket path.

        Returns True when we can open the mic socket. Soft-retries once without
        a full voice-stack recovery (which triggers LG's "audio processing is
        unstable" toast and kills the utterance).
        """
        if self._wait_stream_subscription(wait_sec) and self._stream_socket_path:
            return True
        if self._stream_subscription_ok and not self._stream_socket_path:
            self._stream_socket_path = DEFAULT_REMOTE_SOCKET_PATH
            print(
                "[voice] subscription ok, defaulting socketPath=%s"
                % self._stream_socket_path,
                flush=True,
            )
            return True
        # Soft retry: re-issue startStreaming only (no systemctl restart).
        print("[voice] startStreaming not ready — soft retry", flush=True)
        self._stop_native_streaming()
        self._stream_socket_path = None
        if not self._start_native_streaming():
            return False
        if self._wait_stream_subscription(wait_sec) and self._stream_socket_path:
            return True
        if self._stream_subscription_ok:
            self._stream_socket_path = (
                self._stream_socket_path or DEFAULT_REMOTE_SOCKET_PATH
            )
            return True
        return False

    def _stop_native_streaming(self) -> None:
        # Stop fan-out first so the remote mic LED / BLE stream goes dark.
        try:
            self._stop_stream_fanout()
        except Exception:
            pass
        try:
            self._luna_once(SELF_STREAM_STOP_URI, SELF_STREAM_STOP_PARAMS, timeout=3.0)
        except Exception:
            pass
        if self._stream_proc is not None:
            try:
                self._stream_proc.terminate()
                self._stream_proc.wait(timeout=2)
            except Exception:
                try:
                    self._stream_proc.kill()
                except Exception:
                    pass
            self._stream_proc = None
        self._stream_subscription_ok = False

    def _luna_once(self, uri: str, params: str, timeout: float = 8.0) -> dict[str, Any]:
        try:
            out = subprocess.check_output(
                ["luna-send", "-n", "1", uri, params],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
            return json.loads(out.strip()) if out.strip() else {}
        except Exception as exc:  # noqa: BLE001 - best-effort
            print("[voice] luna %s failed: %s" % (uri, exc), flush=True)
            return {}

    def _cancel_lg_recognition(self, *, force: bool = False) -> None:
        """Dismiss LG NLP after our capture is done.

        Never call this while capture_active unless force=True — it tears down
        the shared voiceinput stream and empty STT follows.
        """
        if self.luna.capture_active and not force:
            return
        if self._active and not force:
            return
        now = time.time()
        if now - self._lg_cancel_ts < 0.35:
            return
        self._lg_cancel_ts = now

        def _run() -> None:
            resp = self._luna_once(
                "luna://com.webos.service.voiceconductor/cancelRecognition",
                "{}",
                timeout=0.8,
            )
            if resp:
                print(
                    "[voice] LG recognition cancelled: %s" % resp,
                    flush=True,
                )

        threading.Thread(
            target=_run,
            daemon=True,
            name="cancel-lg-recognition",
        ).start()

    def _stop_voiceinput_stream(self) -> None:
        self._stop_native_streaming()
        resp = self._luna_once(SELF_STREAM_STOP_URI, SELF_STREAM_STOP_PARAMS)
        if resp:
            print("[voice] stopStreaming: %s" % resp, flush=True)

    def _cancel_lg_voice_ui(self, *, after_capture: bool = False) -> None:
        """Dismiss native LISTENING / LG AI overlay without killing the mic stream.

        The bottom-left bar is com.webos.app.voiceagent — a SystemUIComponent
        launched via sysuicompmgr, NOT a WebAppMgr renderer. closeByAppId and
        _kill_renderer are no-ops for it.

        Cancelling LG recognition stops its STT/NLP/result app. Our independent
        voiceinput subscription remains open for voice card capture.
        """
        system_ids = (
            "com.webos.app.voiceagent",
            "com.webos.app.assistant",
            "com.webos.app.voice",
        )
        for app_id in system_ids:
            LunaBridge.close_system_ui(app_id)
        self._cancel_lg_recognition()
        try:
            self.luna.burst_dismiss_voice_ui(seconds=5.0 if after_capture else 2.5)
        except Exception:
            pass
        if self.luna.capture_active and not self.luna.close_voiceagent_during_capture:
            return
        native_ids = [
            app_id
            for app_id in self.luna.native_app_ids
            if app_id in VOICE_RESULT_APP_IDS
        ]
        for app_id in native_ids:
            LunaBridge._kill_renderer(app_id)
            threading.Thread(
                target=LunaBridge.close_native_app,
                args=(app_id,),
                daemon=True,
            ).start()
        try:
            subprocess.run(
                ["pkill", "-f", "/usr/bin/com.webos.app.voice"],
                check=False,
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _schedule_renderer_kill_linger(self) -> None:
        """Keep dismissing delayed voice UI for several seconds after release."""
        if self.renderer_kill_linger_sec <= 0:
            self._stop_renderer_killer()
            return
        if self._renderer_killer_linger is not None:
            self._renderer_killer_linger.cancel()
        timer = threading.Timer(
            self.renderer_kill_linger_sec, self._stop_renderer_killer
        )
        timer.daemon = True
        self._renderer_killer_linger = timer
        timer.start()

    def _recover_voice_stack(self, reason: str) -> None:
        """Reset voiceinput BLE routing after cold self_stream or error 10000."""
        print("[voice] voice stack recovery (%s)" % reason, flush=True)
        # Keep native UI blocked for the entire recovery — never open a window
        # where KEY_VOICE can launch LG's AI overlay.
        self._disable_native_triggers()
        self.luna.engage_voiceagent_suppression(seconds=120.0)
        self._stop_voiceinput_stream()
        self._cancel_lg_voice_ui()
        # Drop any orphaned luna-send -i subscriptions that can leave
        # voiceinput stuck returning errorCode 10000.
        try:
            subprocess.run(
                ["pkill", "-f", "luna-send.*startStreaming"],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        try:
            subprocess.run(
                ["systemctl", "restart", VOICEINPUT_SERVICE],
                check=False,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if self.self_stream_keep_voiceconductor or self.capture_mode == CAPTURE_MODE_PHASED:
                subprocess.run(
                    ["systemctl", "restart", VOICECONDUCTOR_SERVICE],
                    check=False,
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception as exc:  # noqa: BLE001 - best-effort
            print("[voice] service restart failed: %s" % exc, flush=True)
        # voiceinput needs a moment after restart before startStreaming accepts
        # subscriptions again; 0.5s was too short and left error 10000 stuck.
        time.sleep(2.5)
        # Re-assert after service restarts (they may ignore a flag that was
        # already present before they came up).
        self._disable_native_triggers()
        mrcu = self._luna_once(
            "luna://com.webos.service.mrcu/activateMrcu",
            "{}",
            timeout=12.0,
        )
        if mrcu:
            print("[voice] activateMrcu: %s" % mrcu, flush=True)
        if self.capture_mode == CAPTURE_MODE_SELF:
            self._disable_native_triggers()
        time.sleep(0.5)

    def _wait_stream_subscription(self, timeout: float = 1.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stream_subscription_ok:
                return True
            time.sleep(0.05)
        return self._stream_subscription_ok

    def _activate_mrcu(self) -> bool:
        """Wake the Magic Remote BLE path so startStreaming can create the socket.

        Returns True when Luna reports success (returnValue true).
        Failed activateMrcu (0013 / 1003) makes webOS toast
        "Please check the connection status of your Magic Controller".
        After that, skip further calls for several minutes.
        """
        now = time.time()
        skip_until = float(getattr(self, "_mrcu_skip_until", 0.0) or 0.0)
        if now < skip_until:
            return False
        mrcu = self._luna_once(
            "luna://com.webos.service.mrcu/activateMrcu",
            "{}",
            timeout=4.0,
        )
        if mrcu:
            print("[voice] activateMrcu: %s" % mrcu, flush=True)
        try:
            ok = bool(mrcu and mrcu.get("returnValue") is True)
        except Exception:
            ok = False
        if ok:
            self._mrcu_skip_until = 0.0
            return True
        code = ""
        text = ""
        if isinstance(mrcu, dict):
            code = str(mrcu.get("errorCode") or "")
            text = str(mrcu.get("errorText") or "")
        if (
            code in ("0013", "13", "1003")
            or "Cannot activate" in text
            or "not Ready" in text
        ):
            self._mrcu_skip_until = now + 300.0
            print(
                "[voice] activateMrcu disabled 300s after %s (%s) "
                "— skip Magic Controller toast" % (code or "?", text or ""),
                flush=True,
            )
        return False

    def _activate_mrcu_until_ready(self, attempts: int = 4, gap: float = 0.45) -> bool:
        """Retry activateMrcu — Magic Remote often reports 1003 (not ready) once."""
        now = time.time()
        if now < float(getattr(self, "_mrcu_skip_until", 0.0) or 0.0):
            return False
        for i in range(max(1, int(attempts))):
            if self._activate_mrcu():
                return True
            if time.time() < float(getattr(self, "_mrcu_skip_until", 0.0) or 0.0):
                return False
            time.sleep(gap * (1.0 + 0.35 * i))
        return False

    def _wait_socket_node(self, sock_path: str, timeout: float) -> bool:
        """Wait until the voiceinput unix socket node exists on disk."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sock_path and os.path.exists(sock_path):
                return True
            time.sleep(0.04)
        return bool(sock_path and os.path.exists(sock_path))

    def _mic_stream_lock(self) -> threading.Lock:
        lock = getattr(self, "_mic_arm_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._mic_arm_lock = lock
        return lock

    def _warm_socket_path(self) -> Optional[str]:
        """Return voiceinput unix socket path if the node exists on disk."""
        path = self._stream_socket_path or DEFAULT_REMOTE_SOCKET_PATH
        if path and os.path.exists(path):
            return path
        if os.path.exists(DEFAULT_REMOTE_SOCKET_PATH):
            self._stream_socket_path = DEFAULT_REMOTE_SOCKET_PATH
            return DEFAULT_REMOTE_SOCKET_PATH
        return None

    def _cold_start_streaming(self) -> Optional[str]:
        """stop+start voiceinput and return socket path (or None)."""
        print("[voice] cold startStreaming arm", flush=True)
        self._disable_native_triggers()
        self._stop_native_streaming()
        self._stream_socket_path = None
        self._activate_mrcu_until_ready(attempts=3, gap=0.35)
        if not self._start_native_streaming():
            self._activate_mrcu_until_ready(attempts=2, gap=0.4)
            self._start_native_streaming()
        # One wait only — double stop/start was burning 5–10s and thrashing
        # the remote (logs: systemctl start voiceinput timed out 10s).
        if not self._ensure_stream_ready(2.2):
            self._activate_mrcu_until_ready(attempts=2, gap=0.35)
            time.sleep(0.25)
            self._start_native_streaming()
            self._ensure_stream_ready(2.2)
        sock_path = self._stream_socket_path or DEFAULT_REMOTE_SOCKET_PATH
        if self._wait_socket_node(sock_path, 1.8):
            return sock_path
        print(
            "[voice] socket missing after ACK: %s" % sock_path,
            flush=True,
        )
        return sock_path if os.path.exists(sock_path) else None

    def _mic_keepalive_loop(self) -> None:
        """Idle mic hygiene — do NOT light the remote mic when the user is idle.

        Earlier versions cold-re-armed startStreaming every ~45s. That:
          - turned the Magic Remote mic LED back on after a turn ("mic on
            when not speaking")
          - thrashed voiceinput with error 10000 while the remote slept
          - still left the next press with near-silent PCM

        Now: only ensure services exist; never startStreaming until KEY_VOICE.
        Arm happens on press via _arm_remote_mic_socket / _cold_start_streaming.
        """
        while True:
            try:
                time.sleep(90.0)
                if self._active or self.luna.capture_active:
                    continue
                if self._session_lock.locked():
                    continue
                if self.capture_mode != CAPTURE_MODE_SELF:
                    continue
                # If a stale subscription is still holding the mic open after a
                # turn, shut it down so the remote LED goes off.
                if (
                    self._stream_proc is not None
                    and self._stream_proc.poll() is None
                    and not self._active
                ):
                    last = float(getattr(self, "_last_session_end_ts", 0.0) or 0.0)
                    if last and (time.time() - last) > 8.0:
                        print(
                            "[voice] mic idle — stopStreaming (no keepalive re-arm)",
                            flush=True,
                        )
                        with self._mic_stream_lock():
                            if not self._active and not self.luna.capture_active:
                                self._stop_native_streaming()
                                self._stream_socket_path = None
                # Soft ensure voiceinput unit is up (no stream).
                try:
                    self._ensure_voiceinput_service()
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                print("[voice] mic keepalive error: %s" % exc, flush=True)

    def _arm_remote_mic_socket(self) -> Optional[str]:
        """Arm voiceinput startStreaming and return a live socket path (or None).

        Warm path: socket node exists + luna-send subscription still running.
        Do NOT open the PCM socket for a "probe" — that steals the single
        client slot (launch terminal / molecule turns then saw peak=0).

        On cold failure (error 10000 / missing socket): one full stack recovery
        + mrcu wake + second cold arm — without yielding KEY_VOICE to LG UI.
        """
        with self._mic_stream_lock():
            self._disable_native_triggers()
            # Prefer an existing voice socket. activateMrcu is only for cold
            # start — on this TV it often fails with 0013 and pops an LG toast.
            warm = self._warm_socket_path()
            proc_ok = (
                self._stream_proc is not None
                and self._stream_proc.poll() is None
                and bool(self._stream_subscription_ok)
            )
            if warm and proc_ok:
                print(
                    "[voice] warm startStreaming reused path=%s" % warm,
                    flush=True,
                )
                self._stream_socket_path = warm
                return warm
            if warm and not proc_ok:
                print(
                    "[voice] socket file present but subscription dead — cold arm",
                    flush=True,
                )
            path = self._cold_start_streaming()
            if path and (
                self._stream_subscription_ok or os.path.exists(path)
            ):
                return path
            # Full recovery once — then second cold arm (still keep native UI blocked).
            print(
                "[voice] cold arm weak — recovering voice stack once",
                flush=True,
            )
            try:
                self.luna.burst_dismiss_voice_ui(seconds=6.0)
            except Exception:
                pass
            self._recover_voice_stack("arm after startStreaming failure")
            self._activate_mrcu_until_ready(attempts=4, gap=0.4)
            path2 = self._cold_start_streaming()
            return path2

    def _wait_for_pcm_ready(
        self,
        capture: Union[OneshotAlsaCapture, AlsaCapture],
        timeout: float,
    ) -> bool:
        """Wait until ALSA delivers non-zero PCM (not just Luna returnValue:true)."""
        deadline = time.time() + timeout
        peak = 0
        while time.time() < deadline and self._active:
            chunk = capture.read(timeout=0.15)
            if not chunk:
                continue
            try:
                level = audioop.max(chunk, 2)
            except Exception:
                level = 0
            if level > peak:
                peak = level
            if level >= self.stream_hot_peak:
                print(
                    "[voice] PCM ready peak=%d (threshold=%d)"
                    % (level, self.stream_hot_peak),
                    flush=True,
                )
                return True
        print(
            "[voice] PCM not ready within %.1fs (best peak=%d)"
            % (timeout, peak),
            flush=True,
        )
        return False

    def _kick_listen_pipeline(self) -> None:
        """Start STT + remote mic the instant KEY_VOICE is seen.

        Session setup used to close LG UI first and only then arm the mic
        (~2–5s). Short commands like "launch Netflix" finished before PCM
        or STT were live → empty transcript / "No speech detected".
        """
        def _stt() -> None:
            try:
                self._refresh_xai_bearer()
                if self._stt_is_live():
                    stt = getattr(self, "_stt", None)
                    if stt is not None and hasattr(stt, "reset_for_new_utterance"):
                        stt.reset_for_new_utterance()
                    print("[voice] STT ready at press (warm)", flush=True)
                    return
                self._ensure_stt_connected(log_ready=True)
                print("[voice] STT connected at press", flush=True)
            except Exception as exc:  # noqa: BLE001
                print("[voice] STT at press failed: %s" % exc, flush=True)

        def _mic() -> None:
            try:
                path = self._arm_remote_mic_socket()
                self._press_mic_path = path
                print(
                    "[voice] mic armed at press path=%s" % (path or "?"),
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print("[voice] mic arm at press failed: %s" % exc, flush=True)

        threading.Thread(target=_stt, daemon=True, name="stt-at-press").start()
        threading.Thread(target=_mic, daemon=True, name="mic-at-press").start()

    def _on_button(self, ev: VoiceButtonEvent) -> None:
        print("[voice] button event: %s" % ev.kind, flush=True)
        if ev.kind == ButtonEvent.PRESSED:
            if time.time() < self._ignore_voice_until_ts:
                print(
                    "[voice] duplicate press ignored during app-launch cooldown",
                    flush=True,
                )
                return
            # STT + mic first — every millisecond before "launch Netflix" ends.
            self._kick_listen_pipeline()
            # Kill LG "voice recognition is turned off?" ASAP — before session
            # setup / mic arm (that dialog blocks the screen for many seconds).
            try:
                self.luna.dismiss_voice_recognition_off_dialog(seconds=8.0)
            except Exception:
                pass
            try:
                LunaBridge.close_system_ui("com.webos.app.alert")
            except Exception:
                pass
            threading.Thread(target=self._start_session, daemon=True).start()
        else:
            # self_stream / socket capture is silence-endpointed. The Magic
            # Remote only logs KEY_VOICE clicks (no true press/release state),
            # so ButtonWatcher synthesizes RELEASE after button_idle_release_sec
            # (~1.5s). That synthetic release often fires while startStreaming
            # is still spinning up — if we call _end_capture here, _active is
            # cleared and the capture thread exits with exit=active_cleared /
            # peak=0 / "No speech detected" before any PCM arrives. Never kill
            # the session on release in this mode.
            if (
                self.capture_mode == CAPTURE_MODE_SELF
                or self._socket_session_active
                or self.use_socket_capture
            ):
                self._release_requested = True
                print(
                    "[voice] synthetic release noted — silence endpoint remains active",
                    flush=True,
                )
            else:
                threading.Thread(target=self._end_capture, daemon=True).start()

    def _start_session(self) -> None:
        turn_t0 = time.time()
        if turn_t0 < self._ignore_voice_until_ts:
            print(
                "[voice] session start ignored during app-launch cooldown",
                flush=True,
            )
            return
        if not self._session_lock.acquire(blocking=False):
            # Second press while busy: request cancel of in-flight answer path.
            # After capture ends the lock is free, so a short wait lets barge-in
            # start a fresh turn.
            self._session_cancel.set()
            print(
                "[voice] session busy — cancel requested (gen=%d)"
                % self._session_gen,
                flush=True,
            )
            if not self._session_lock.acquire(timeout=2.5):
                print("[voice] barge-in wait timed out", flush=True)
                return
            # Won the lock: fall through into a new session.
        self._session_gen += 1
        session_gen = self._session_gen
        self._session_cancel.clear()
        self._turn_t0 = turn_t0
        self._refresh_xai_bearer()
        if (
            self.capture_mode == CAPTURE_MODE_SELF
            or self.disable_native_triggers
        ) and not self._native_triggers_blocked():
            print(
                "[voice] native trigger guard missing at press — restoring",
                flush=True,
            )
            self._disable_native_triggers()
        print(
            "[voice] timing press gen=%d t=0ms" % session_gen,
            flush=True,
        )
        # Instant mic badge for WS listeners — BEFORE any Luna
        # close_system_ui / startStreaming work (those can take 0.5–2s+).
        try:
            self.luna.signal_listening_early(reason="button_press")
        except Exception as early_exc:  # noqa: BLE001
            print(
                "[voice] early listening signal failed: %s" % early_exc,
                flush=True,
            )
        now = time.time()
        if now - self._native_ui_suppress_ts >= 0.15:
            self._native_ui_suppress_ts = now
            # Instant kill of LG Listening bars + result cards on press.
            for app_id in (
                "com.webos.app.voiceagent",
                "com.webos.app.assistant",
                "com.webos.app.voice",
                "com.webos.app.alert",
            ):
                LunaBridge.close_system_ui(app_id)
            for app_id in (
                "com.webos.app.aiplatform",
                "com.webos.app.aiplatformsupport",
                "amazon.alexa.view",
                "amazon.alexapr",
                "com.webos.app.voiceweb",
            ):
                try:
                    self._luna_once(
                        "luna://com.webos.applicationManager/closeByAppId",
                        json.dumps({"id": app_id}),
                        timeout=1.0,
                    )
                except Exception:
                    pass
            # Do NOT cancelRecognition here — it races startStreaming and
            # leaves the remote mic socket near-silent for the whole turn.
            try:
                # The live-session flag keeps the watcher on for the turn;
                # this covers the press itself if no session starts.
                self.luna.engage_voiceagent_suppression(seconds=30.0)
            except Exception:
                pass
            try:
                # UI hammer only (aiplatform / voiceagent), not cancelRecognition.
                self.luna.burst_dismiss_voice_ui(seconds=10.0)
            except Exception:
                pass
            try:
                self.luna.dismiss_voice_recognition_off_dialog(seconds=6.0)
            except Exception:
                pass
        if not self._active_provider_key():
            self._active = True
            self._cancel_dismiss()
            # Launch our overlay (and close the native concierge) so the user
            # sees our app with a clear message instead of LG's aiplatform.
            self.luna.session_started()
            if self.ai_provider == "gemini":
                self._fail(
                    "No Gemini API key set — open Settings → AI Voice and Save"
                )
            elif self.ai_provider == "openrouter":
                self._fail(
                    "No OpenRouter API key set — open Settings → AI Voice and Save"
                )
            else:
                self._fail(
                    "No xAI API key set — open Settings → AI Voice and Save"
                )
            return
        if self.ai_provider == "openrouter" and not self.openrouter_api_key:
            self._active = True
            self._cancel_dismiss()
            self.luna.session_started()
            self._fail(
                "No OpenRouter API key set — open Settings → AI Voice and Save"
            )
            return
        self._active = True
        self.luna.capture_active = True
        self._cancel_dismiss()
        # Keep a warm STT WebSocket across presses when possible. Closing it
        # every press forced a cold TLS connect (~10–16s on this TV) so audio
        # never streamed during capture and the user waited for transcript.
        self._stt_send_errors = False
        self._live_best_clear(gen=session_gen)
        # No early silent until this press has streamed fresh PCM (blocks
        # stale warm-STT "Set volume to ten" from closing the card instantly).
        self._stt_streamed = False
        self._stt_streamed_gen = 0
        if self._stt_is_live():
            print("[voice] STT reusing warm session", flush=True)
            try:
                stt = getattr(self, "_stt", None)
                if stt is not None and hasattr(stt, "reset_for_new_utterance"):
                    stt.reset_for_new_utterance()
                if stt is not None and hasattr(stt, "drain_events"):
                    stt.drain_events(120.0)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] STT reset for new utterance failed: %s" % exc,
                    flush=True,
                )
        else:
            old_stt = getattr(self, "_stt", None)
            self._stt = None
            if old_stt is not None:
                try:
                    old_stt.close()
                except Exception:
                    pass
            # Connect immediately in parallel with mic arm (do not wait).
            threading.Thread(
                target=lambda: self._ensure_stt_connected(log_ready=True),
                daemon=True,
                name="stt-press-connect",
            ).start()
        try:
            # CRITICAL: arm the remote mic BEFORE/while launching UI. Logs showed
            # mic_hot at +3s after press — full questions finished before audio
            # started, STT only got "You." / "哦".
            sock_holder: dict[str, Any] = {}
            arm_thread: Optional[threading.Thread] = None
            if self.capture_mode == CAPTURE_MODE_SELF and self.use_socket_capture:
                self._socket_session_active = True
                self._release_requested = False

                def _arm_mic_early() -> None:
                    try:
                        sock_holder["path"] = self._arm_remote_mic_socket()
                    except Exception as arm_exc:  # noqa: BLE001
                        sock_holder["err"] = arm_exc
                        print(
                            "[voice] early mic arm failed: %s" % arm_exc,
                            flush=True,
                        )

                arm_thread = threading.Thread(target=_arm_mic_early, daemon=True)
                arm_thread.start()

            # Stay headless until STT looks like a question. App launches
            # ("start amazon prime") must not flash the blue Grok card.
            self.luna.session_started(show_overlay=False)
            try:
                self.luna.status("Listening…")
            except Exception:
                pass
            # Re-burst after overlay paint — LG aiplatform often launches 0.5–1.5s late.
            try:
                self.luna.burst_dismiss_voice_ui(seconds=8.0)
            except Exception:
                pass
            if self.capture_mode == CAPTURE_MODE_SELF:
                self._cancel_lg_voice_ui()
                self._start_renderer_killer()
                # Mark session ownership *before* the startStreaming wait so a
                # synthetic RELEASE during setup cannot fall through to
                # _end_capture (which would zero _active mid-setup).
                self._socket_session_active = True
                self._release_requested = False
                if arm_thread is not None:
                    arm_thread.join(timeout=4.0)
                sock_path = sock_holder.get("path")
                if not sock_path and self.use_socket_capture:
                    sock_path = self._arm_remote_mic_socket()
                elif not sock_path:
                    # Still arm BLE path even when capturing via ALSA.
                    try:
                        self._arm_remote_mic_socket()
                    except Exception:
                        pass
                time.sleep(max(0.02, min(0.12, float(self.self_stream_delay_sec))))
                if not self._active:
                    print(
                        "[voice] session aborted during stream setup",
                        flush=True,
                    )
                    self._socket_session_active = False
                    if self._session_lock.locked():
                        try:
                            self._session_lock.release()
                        except RuntimeError:
                            pass
                    return
                if getattr(self, "_capture", None) is not None:
                    self._capture.stop()
                    self._capture = None

                # Prefer the unix remote socket (proven when peak>2k with
                # speech). ALSA plughw often has high noise that is *not* the
                # Magic Remote mic — STT returns empty. Only fall back to ALSA
                # if the socket is missing/unusable.
                use_alsa = False
                sock_cap = None
                if self.use_socket_capture and sock_path:
                    if not self._stt_is_live():
                        threading.Thread(
                            target=lambda: self._ensure_stt_connected(
                                log_ready=True
                            ),
                            daemon=True,
                            name="stt-arm-connect",
                        ).start()
                    sock_cap = SocketUnixCapture(sock_path)
                    sock_cap.start(wait_sec=1.5)
                    sock_peak = 0
                    if sock_cap._sock is not None:
                        # BLE audio often ramps after ~0.5–1s; sample longer.
                        try:
                            probe = sock_cap.read(0.9)
                        except Exception:
                            probe = None
                        if probe:
                            try:
                                sock_peak = audioop.max(probe, 2)
                            except Exception:
                                sock_peak = 0
                            sock_cap.push_front(probe)
                    if sock_cap._sock is not None:
                        # Always prefer socket when connected — ALSA on this TV
                        # is not the Magic Remote mic (loud noise, empty STT).
                        use_alsa = False
                        print(
                            "[voice] using unix socket capture peak=%d path=%s"
                            % (sock_peak, sock_path),
                            flush=True,
                        )
                    else:
                        use_alsa = True
                        print(
                            "[voice] socket missing — ALSA fallback %s"
                            % self.capture_device,
                            flush=True,
                        )
                        try:
                            sock_cap.stop()
                        except Exception:
                            pass
                        sock_cap = None
                else:
                    use_alsa = True

                if not use_alsa and sock_cap is not None and sock_cap._sock is not None:
                    sock_cap.arm()
                    self._capture = sock_cap
                    self._pcm_chunks = []
                    try:
                        self.luna.status("Listening…")
                    except Exception:
                        pass
                    self._capture_thread = threading.Thread(
                        target=self._capture_audio_socket, daemon=True
                    )
                    self._capture_thread.start()
                    return  # socket session drains + finalizes on its own

                # ALSA primary path (and fallback). Must NOT leave
                # _socket_session_active True or _end_capture is ignored forever
                # when using the ALSA capture thread.
                self._socket_session_active = False
                try:
                    self.luna.status("Listening…")
                except Exception:
                    pass
                if not self._stt_is_live():
                    threading.Thread(
                        target=lambda: self._ensure_stt_connected(log_ready=True),
                        daemon=True,
                        name="stt-arm-connect",
                    ).start()
                print(
                    "[voice] ALSA capture device=%s" % self.capture_device,
                    flush=True,
                )
                self._capture = OneshotAlsaCapture(self.capture_device)
                self._capture.start()
                self._capture.arm()
                if not self._wait_for_pcm_ready(
                    self._capture, self.stream_ready_timeout_sec
                ):
                    self._recover_voice_stack("PCM still zero after startStreaming")
                    self._start_native_streaming()
                    time.sleep(self.self_stream_delay_sec)
                    if self._capture is not None:
                        self._capture.stop()
                    self._capture = OneshotAlsaCapture(self.capture_device)
                    self._capture.start()
                    self._capture.arm()
            elif self.capture_mode == CAPTURE_MODE_PHASED:
                self._ensure_lg_voice_stack()
                self._start_renderer_killer()
            self._stt = self._new_stt_session()
            self._stt.connect()
            print(
                "[voice] STT connected provider=%s; starting capture"
                % self.ai_provider,
                flush=True,
            )
            if self.use_alsa_capture:
                if self.capture_mode == CAPTURE_MODE_SELF:
                    pass  # capture already opened before STT connect
                elif self.capture_mode == CAPTURE_MODE_PHASED:
                    time.sleep(self.phased_stream_delay_sec)
                    if getattr(self, "_capture", None) is not None:
                        self._capture.stop()
                    self._capture = AlsaCapture(self.capture_device)
                    self._capture.start()
                    self._capture.arm()
                elif getattr(self, "_capture", None) is None:
                    self._capture = AlsaCapture(self.capture_device)
                    self._capture.start()
                    self._capture.arm()
                elif getattr(self, "_capture", None) is not None:
                    self._capture.arm()
                self._capture_thread = threading.Thread(
                    target=self._capture_audio_alsa, daemon=True
                )
            else:
                self._decoder = MsbcDecoder(ffmpeg_path=self.ffmpeg_path)
                self._decoder.start()
                self._capture_thread = threading.Thread(
                    target=self._capture_audio, daemon=True
                )
            self._capture_thread.start()
            self._pump_thread = threading.Thread(target=self._pump_stt_partials, daemon=True)
            self._pump_thread.start()
        except Exception as exc:
            self._socket_session_active = False
            self._active = False
            self.luna.capture_active = False
            self._fail(str(exc))

    def _capture_audio_socket(self) -> None:
        """Socket session: read the whole hold into a buffer, STT + chat.

        Runs in its own thread. Reads PCM from voiceinput's remote-mic unix
        socket from press until release (+ short drain) or max_capture, while
        STT connects in parallel. Finalizes on its own and releases the lock.
        """
        session_gen = self._session_gen
        cap = self._capture
        self._pcm_chunks = []
        total = 0
        peak = 0
        stt_flushed = False
        heard_speech = False
        speech_rms = int(self.speech_detect_rms)
        hot_rms = int(self.mic_hot_rms)
        max_bytes = int(self.max_capture_sec * 32000)  # 16k * 2 bytes
        exit_reason = "active_cleared"

        # Always stream PCM to STT *during* capture. Waiting until after
        # capture for a full-buffer re-upload costs ~3–14s on the TV (TLS +
        # re-send). Live partials paint once the hypothesis looks solid
        # (2+ words) so the user sees text before capture ends.
        live_stt = True
        # stt_live_stream=true → paint every partial (including 1-word flashes).
        paint_aggressive = bool(self.stt_live_stream)
        if not self._stt_is_live():
            threading.Thread(
                target=lambda: self._ensure_stt_connected(log_ready=True),
                daemon=True,
            ).start()

        self._stt_streamed = False
        self._stt_send_errors = False
        # live_best is cleared on press; worker updates, finalize reads (synced).
        self._live_best_clear(gen=session_gen)
        # ~15s of audio; drop newest under backpressure (keep utterance start).
        pcm_queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=200)
        stt_worker_stop = threading.Event()
        stt_worker_done = threading.Event()
        stt_reconnect_used = {"n": 0}
        stt_worker: Optional[threading.Thread] = None

        def _stt_worker() -> None:
            pending: list[bytes] = []

            def cb(text: str, is_final: bool) -> None:
                t = (text or "").strip()
                if not t:
                    return
                words = t.split()
                show = self._live_best_update(
                    t, is_final=is_final, gen=session_gen
                )
                # Launch/mute/volume/HDMI: fire headless — no card, no STT text.
                if self._try_early_silent_command(
                    show or t, gen=session_gen, is_final=is_final
                ):
                    return
                if self._looks_like_silent_command_prefix(show or t):
                    return
                # Real question → open Listening UI and paint STT.
                self._maybe_show_voice_ui_for_text(show or t)
                should_paint = (
                    paint_aggressive
                    or is_final
                    or len(words) >= 2
                    or len(t) >= 12
                )
                if should_paint and show:
                    try:
                        self.luna.transcript_partial(show, is_final=is_final)
                    except Exception:
                        pass

            def _flush_pending(stt: Any, *, allow_reconnect: bool) -> None:
                nonlocal pending
                if not pending or stt is None:
                    return
                sent_any = False
                failed = False
                # Send as ~100–200ms aggregates for more stable STT.
                blob = b"".join(pending)
                pending = []
                step = 6400  # 200ms @ 16kHz s16 mono
                for i in range(0, len(blob), step):
                    buf = blob[i : i + step]
                    try:
                        stt.send_pcm(buf)
                        sent_any = True
                    except Exception as exc:  # noqa: BLE001
                        failed = True
                        self._stt_send_errors = True
                        print(
                            "[voice] STT send failed: %s" % exc,
                            flush=True,
                        )
                        # Keep unsent tail for reconnect retry.
                        pending = [blob[i:]]
                        break
                if failed and allow_reconnect and stt_reconnect_used["n"] < 1:
                    stt_reconnect_used["n"] += 1
                    try:
                        if hasattr(stt, "reconnect"):
                            stt.reconnect()
                            print(
                                "[voice] STT reconnected mid-capture",
                                flush=True,
                            )
                            for buf in list(pending):
                                try:
                                    stt.send_pcm(buf)
                                    sent_any = True
                                except Exception as rexc:  # noqa: BLE001
                                    self._stt_send_errors = True
                                    print(
                                        "[voice] STT send failed after "
                                        "reconnect: %s" % rexc,
                                        flush=True,
                                    )
                                    break
                            else:
                                pending = []
                    except Exception as rexc:  # noqa: BLE001
                        self._stt_send_errors = True
                        print(
                            "[voice] STT reconnect failed: %s" % rexc,
                            flush=True,
                        )
                        pending = []
                elif failed:
                    # Stopping or already reconnected — drop tail; finalize
                    # will full-buffer re-STT from _pcm_chunks.
                    pending = []
                if sent_any:
                    self._stt_streamed = True
                    self._stt_streamed_gen = int(session_gen)

            try:
                while True:
                    stopping = stt_worker_stop.is_set()
                    try:
                        item = pcm_queue.get(timeout=0.05)
                        if item is not None:
                            pending.append(item)
                    except queue.Empty:
                        pass
                    stt = getattr(self, "_stt", None)
                    live = self._stt_is_live(stt)
                    if live and pending:
                        # Never reconnect after stop — that races with finish().
                        _flush_pending(stt, allow_reconnect=not stopping)
                    if live and not stopping:
                        try:
                            stt.poll_events(cb)
                        except Exception:
                            pass
                    if stopping and pcm_queue.empty():
                        # One last best-effort flush, then exit so finalize
                        # can call finish() on a stable socket.
                        stt = getattr(self, "_stt", None)
                        if pending and self._stt_is_live(stt):
                            _flush_pending(stt, allow_reconnect=False)
                        break
            finally:
                stt_worker_done.set()

        stt_worker = threading.Thread(target=_stt_worker, daemon=True)
        stt_worker.start()
        print(
            "[voice] STT mode=stream_during_capture paint_aggressive=%s"
            % paint_aggressive,
            flush=True,
        )

        try:
            # RELEASE often arrives while the mic is still arming. Track that
            # dynamically (not only at loop entry) — logs showed RELEASE after
            # connect_start but before mic_hot, snapshotted as early_release=False,
            # then snappy stop at +0.2s → 0.9s clip → empty STT on weather Qs.
            release_before_hot = bool(self._release_requested)
            if release_before_hot:
                print(
                    "[voice] release pending until mic hot (full utterance window)",
                    flush=True,
                )
            connect_start = time.time()
            # Endpoint clocks start only after the mic pipe is hot + real speech.
            # Starting them at connect made noise look like speech and left the
            # UI on Listening for many seconds with empty STT.
            mic_hot = False
            hot_start = connect_start
            capture_start = connect_start
            last_voice_ts = connect_start
            first_speech_ts = 0.0  # wall time of first real speech after hot
            cold_retry_done = False
            # Accumulated continuous speech energy (resets on quiet frames).
            speech_run_sec = 0.0
            while self._active:
                now = time.time()
                # Absolute ceiling from socket connect (includes arm-to-hot wait).
                if (now - connect_start) >= self.max_capture_sec + self.mic_hot_timeout_sec:
                    exit_reason = "max_capture"
                    break
                if total >= max_bytes:
                    exit_reason = "max_bytes"
                    break
                if cap is None or cap._sock is None:
                    exit_reason = "socket_eof"
                    break
                # Update while arming — synthetic RELEASE almost always lands here.
                if not mic_hot and self._release_requested and not release_before_hot:
                    release_before_hot = True
                    print(
                        "[voice] release during arm — keep long utterance window",
                        flush=True,
                    )

                # Until mic is hot: buffer + stream PCM (no silence endpoint).
                # Keep ~3.5s ring so "What is …" spoken as the mic wakes is kept.
                if not mic_hot:
                    # Zero bytes for >1.2s → dead pipe; one cold re-arm (no PCM
                    # probe — that steals the single client slot).
                    if (
                        not cold_retry_done
                        and total == 0
                        and (now - connect_start) >= 1.2
                    ):
                        cold_retry_done = True
                        print(
                            "[voice] no PCM on socket — mid-capture cold re-arm",
                            flush=True,
                        )
                        try:
                            cap.stop()
                        except Exception:
                            pass
                        sock_path = self._cold_start_streaming()
                        if sock_path:
                            cap = SocketUnixCapture(sock_path)
                            cap.start(wait_sec=1.5)
                            if cap._sock is not None:
                                cap.arm()
                                self._capture = cap
                                connect_start = time.time()
                                print(
                                    "[voice] mid-capture re-arm ok path=%s"
                                    % sock_path,
                                    flush=True,
                                )
                                continue
                        print(
                            "[voice] mid-capture re-arm failed",
                            flush=True,
                        )
                    if (now - connect_start) >= self.mic_hot_timeout_sec:
                        exit_reason = "no_speech"
                        break
                    chunk = cap.read(0.12)
                    if not chunk:
                        continue
                    chunk = self._pcm_apply_gain(chunk)
                    try:
                        level = audioop.rms(chunk, 2)
                    except Exception:
                        level = 0
                    if level > peak:
                        peak = level
                    self._pcm_chunks.append(chunk)
                    total += len(chunk)
                    pre_hot_max = int(3.5 * 32000)
                    while total > pre_hot_max and self._pcm_chunks:
                        dropped = self._pcm_chunks.pop(0)
                        total -= len(dropped)
                    try:
                        pcm_queue.put_nowait(chunk)
                    except queue.Full:
                        pass
                    # Any live PCM after 150ms means the pipe is open. Waiting
                    # for a loud spike delayed "launch Netflix" past the utterance.
                    if (
                        level >= hot_rms
                        or level >= speech_rms
                        or (total >= 4800 and level >= 40)
                    ):
                        mic_hot = True
                        hot_start = time.time()
                        # Endpoint clock starts at first real energy; pre-roll
                        # ring is already in _pcm_chunks for batch STT.
                        capture_start = hot_start
                        last_voice_ts = hot_start
                        # Do NOT set heard_speech from a single arming spike —
                        # BLE/socket open often blips above speech_rms once,
                        # then goes quiet; that used to silence-end mid wait.
                        speech_run_sec = 0.0
                        print(
                            "[voice] mic hot peak=%d (hot_rms=%d) — Listening"
                            % (peak, hot_rms),
                            flush=True,
                        )
                        t0 = float(getattr(self, "_turn_t0", connect_start))
                        hot_ms = (time.time() - t0) * 1000.0
                        print(
                            "[voice] timing mic_hot +%.0fms" % hot_ms,
                            flush=True,
                        )
                        try:
                            # Late arm: user often already spoke into a dead mic.
                            if hot_ms >= 1500.0:
                                self.luna.status("Speak now…")
                            else:
                                self.luna.status("Listening…")
                        except Exception:
                            pass
                    continue

                elapsed = now - capture_start
                if elapsed >= self.max_capture_sec:
                    exit_reason = "max_capture"
                    break
                # Silence endpoint only after a short-sentence window. Adaptive:
                # early in the utterance demand a longer trailing silence so a
                # breath between "tell me" and "something funny" does not cut.
                if heard_speech and elapsed >= float(self.min_capture_sec):
                    speech_age = (
                        (now - first_speech_ts) if first_speech_ts > 0 else elapsed
                    )
                    silence_need = float(self.end_silence_sec)
                    if speech_age < 1.8:
                        silence_need = max(silence_need, 1.45)
                    elif speech_age < 2.8:
                        silence_need = max(silence_need, 1.2)
                    # Incomplete command starters ("Open", "Set volume to") —
                    # user is mid-phrase; do not cut after the first word.
                    live = ""
                    try:
                        live = (self._live_best_get(gen=session_gen) or "").strip()
                    except Exception:
                        live = ""
                    if not live:
                        try:
                            stt_live = getattr(self, "_stt", None)
                            if stt_live is not None:
                                live = (stt_live.current_text() or "").strip()
                        except Exception:
                            live = ""
                    incomplete_cmd = False
                    if live:
                        try:
                            if self._looks_like_silent_command_prefix(live) and (
                                tv_control.silent_tv_command_kind(live) is None
                            ):
                                incomplete_cmd = True
                        except Exception:
                            incomplete_cmd = False
                        # Bare "open" / "launch" / "go to" without a target.
                        if re.match(
                            r"^(?:open|launch|start|go\s+to|show)\.?$",
                            live,
                            re.I,
                        ):
                            incomplete_cmd = True
                    if incomplete_cmd:
                        silence_need = max(silence_need, 2.4)
                        # Hold at least ~2.6s of speech window for "open settings".
                        if elapsed < 2.6:
                            pass  # do not silence-end yet
                        elif (now - last_voice_ts) >= silence_need:
                            exit_reason = "silence"
                            break
                    elif (now - last_voice_ts) >= silence_need:
                        exit_reason = "silence"
                        break
                # Hot mic but no real speech yet -> grace window after hot.
                if (
                    not heard_speech
                    and (now - hot_start) >= self.speech_grace_sec
                ):
                    exit_reason = "no_speech"
                    break
                # Released during arm, never got speech — fail after grace.
                if (
                    not heard_speech
                    and release_before_hot
                    and (now - hot_start) >= max(3.0, float(self.speech_grace_sec))
                ):
                    exit_reason = "no_speech"
                    break
                chunk = cap.read(0.15)
                if not chunk:
                    continue
                chunk = self._pcm_apply_gain(chunk)
                total += len(chunk)
                self._pcm_chunks.append(chunk)
                try:
                    level = audioop.rms(chunk, 2)
                except Exception:
                    level = 0
                if level > peak:
                    peak = level
                # Mid-phrase remote speech often sits BELOW speech_detect_rms
                # (and well below silence_rms). Old continue_rms used
                # silence_rms*1.15 (~400) which is *stricter* than speech_rms
                # (280) — soft words never refreshed last_voice_ts and silence
                # cut the turn after "tell me". Keep the turn open on any
                # energy clearly above the near-silence floor.
                continue_rms = max(
                    110,
                    min(int(speech_rms * 0.55), int(self.silence_rms * 0.5)),
                )
                # Estimate this chunk's duration from PCM size (16 kHz S16LE mono).
                chunk_sec = max(0.02, min(0.25, len(chunk) / 32000.0))
                if level >= speech_rms:
                    speech_run_sec += chunk_sec
                    last_voice_ts = time.time()
                    if not heard_speech and speech_run_sec >= SPEECH_CONFIRM_SEC:
                        heard_speech = True
                        first_speech_ts = last_voice_ts
                        print(
                            "[voice] speech confirmed (%.0fms run, level=%d)"
                            % (speech_run_sec * 1000.0, level),
                            flush=True,
                        )
                    elif heard_speech:
                        pass  # keep last_voice_ts refreshed
                    self._button.notify_voice_activity()
                elif heard_speech and level >= continue_rms:
                    # Quiet continuation of the same phrase ("…something funny").
                    speech_run_sec = 0.0
                    last_voice_ts = time.time()
                    self._button.notify_voice_activity()
                else:
                    # Quiet frame — break the "sustained speech" run counter.
                    speech_run_sec = 0.0
                # Stream to STT while capturing (worker buffers until connected).
                try:
                    pcm_queue.put_nowait(chunk)
                except queue.Full:
                    # Drop this newest chunk (keep earlier speech for STT).
                    pass
        finally:
            # Drain and stop the STT worker.
            stt_worker_stop.set()
            if stt_worker is not None:
                try:
                    pcm_queue.put_nowait(None)  # wake blocked get()
                except Exception:
                    pass
                stt_worker.join(timeout=3.0)
                if not stt_worker_done.is_set() or stt_worker.is_alive():
                    print(
                        "[voice] STT worker slow to exit — full-buffer finalize",
                        flush=True,
                    )
                    self._stt_send_errors = True
            stt_flushed = bool(self._stt_streamed)
            stt_errors = bool(self._stt_send_errors)
            # Always dump last capture for offline debug (16k S16LE mono).
            try:
                self._save_capture_debug(
                    list(self._pcm_chunks), peak=peak, total_bytes=total
                )
            except Exception as exc:  # noqa: BLE001
                print("[voice] capture debug save failed: %s" % exc, flush=True)
            print(
                "[voice] socket capture done bytes=%d peak=%d heard_speech=%s "
                "exit=%s streamed=%s errors=%s"
                % (total, peak, heard_speech, exit_reason, stt_flushed, stt_errors),
                flush=True,
            )
            t0 = float(getattr(self, "_turn_t0", time.time()))
            print(
                "[voice] timing capture_done +%.0fms exit=%s bytes=%d"
                % ((time.time() - t0) * 1000.0, exit_reason, total),
                flush=True,
            )
            # Quiet Magic Remote audio often never latches heard_speech (peak
            # 400–900, rms under speech_detect) but still holds a real command.
            # Force STT when we captured a usable buffer so "launch BBC…" works.
            if (
                not heard_speech
                and peak >= 180
                and total >= 32000  # ≥1s of 16kHz s16 mono
                and exit_reason in ("no_speech", "silence", "max_capture", "release")
            ):
                print(
                    "[voice] quiet-speech rescue peak=%d bytes=%d exit=%s — still STT"
                    % (peak, total, exit_reason),
                    flush=True,
                )
                heard_speech = True
                if exit_reason == "no_speech":
                    exit_reason = "quiet_rescue"
            # Leave "Listening…" as soon as the mic stops — STT finalize can
            # still take seconds and must not look like we are still recording.
            try:
                self.luna.status("Got it — thinking…" if heard_speech else "No speech…")
            except Exception:
                pass
            try:
                self.luna.listening_ended(has_audio=bool(heard_speech or peak >= speech_rms))
            except Exception:
                pass
            try:
                if heard_speech or peak >= speech_rms:
                    self.luna.status("Transcribing…")
            except Exception:
                pass
            # Self-heal only for true cold mic (near-zero peak, no speech).
            # Quiet-but-real speech often peaks 1000–3000 after modest gain —
            # re-arming there was tearing down a working stream mid-session and
            # contributing to LG's "audio processing is unstable" toast.
            if not heard_speech and peak < COLD_MIC_PEAK:
                print(
                    "[voice] cold mic (peak=%d < %d) — re-arming stream for next press"
                    % (peak, COLD_MIC_PEAK),
                    flush=True,
                )
                self._stop_native_streaming()
                self._stream_socket_path = None
            self._finalize_socket_session(
                stt_flushed,
                heard_speech=heard_speech,
                peak=peak,
                total_bytes=total,
                session_gen=session_gen,
                stt_errors=stt_errors,
            )

    def _active_provider_key(self) -> str:
        if self.ai_provider == "gemini":
            return self.gemini_api_key or ""
        if self.ai_provider == "openrouter":
            return self.openrouter_api_key or ""
        return self.api_key or ""

    def _refresh_xai_bearer(self, *, log: bool = False) -> str:
        """Resolve API key or SuperGrok OAuth token into self.api_key."""
        cfg_key = ""
        try:
            cfg_key = str((self.config or {}).get("xai_api_key") or "")
        except Exception:
            cfg_key = self.api_key or ""
        if cfg_key.startswith("xai-..."):
            cfg_key = ""
        try:
            token, mode = supergrok_auth.resolve_bearer(self.auth_mode, cfg_key)
            self.api_key = token
            self.auth_mode = mode
            if log:
                print(
                    "[voice] xAI auth mode=%s token=%s"
                    % (mode, "yes" if token else "no"),
                    flush=True,
                )
            return token
        except Exception as exc:
            if self.auth_mode == "SUPERGROK_OAUTH":
                print("[voice] SuperGrok bearer: %s" % exc, flush=True)
                # Keep any still-valid in-memory token; otherwise empty.
                if not self.api_key or str(self.api_key).startswith("xai-"):
                    self.api_key = ""
            elif not cfg_key:
                self.api_key = ""
            else:
                self.api_key = cfg_key
            return self.api_key or ""

    def _note_xai_http_error(self, exc: Any) -> Optional[str]:
        msg = classify_xai_http_error(str(exc or ""))
        if not msg:
            return None
        kind = "credits" if "credits" in msg.lower() else "auth"
        config_store.save_last_xai_error(kind, msg)
        print("[voice] xAI API error: %s" % msg, flush=True)
        return msg

    def _tts_available(self) -> bool:
        """Spoken replies: Gemini TTS on the Gemini path, otherwise xAI voices."""
        if not self.tts_enabled:
            return False
        if self.ai_provider == "gemini" and self.gemini_api_key:
            return True
        return bool(self.api_key)

    def _new_stt_session(self):
        """Create STT for the configured provider (xAI live WS or Gemini batch)."""
        self._refresh_xai_bearer()
        if self.ai_provider == "openrouter":
            return OpenRouterSttSession(
                self.openrouter_api_key,
                model=self.openrouter_stt_model,
                language=self.stt_language,
            )
        if self.ai_provider == "gemini":
            return GeminiSttSession(
                self.gemini_api_key,
                model=self.gemini_stt_model,
                language=self.stt_language,
                keyterms=STT_KEYTERMS,
            )
        return GrokSttSession(
            SttConfig(
                api_key=self.api_key,
                language=self.stt_language,
                keyterms=STT_KEYTERMS,
            )
        )

    def _stt_is_live(self, stt: Any = None) -> bool:
        """True when the STT session has an open WebSocket (or Gemini buffer)."""
        if stt is None:
            stt = getattr(self, "_stt", None)
        if stt is None:
            return False
        # Gemini buffers locally — treat as live once constructed.
        if isinstance(stt, (GeminiSttSession, OpenRouterSttSession)):
            return True
        ws = getattr(stt, "_ws", None)
        sock = getattr(ws, "_sock", None) if ws is not None else None
        return sock is not None

    def _live_best_clear(self, *, gen: Optional[int] = None) -> None:
        """Reset live STT best-text for a new press/generation."""
        with self._live_best_lock:
            self._live_best_text = ""
            if gen is not None:
                self._live_best_gen = int(gen)

    def _live_best_update(
        self, text: str, *, is_final: bool = False, gen: Optional[int] = None
    ) -> str:
        """Merge a live STT partial into the best hypothesis; return display text.

        Thread-safe: called from the STT capture worker and read from finalize.
        Keeps the longest solid line so short mid-phrase rewrites cannot erase
        a good question (and so finalize can use it if WS finish() is empty).
        """
        t = (text or "").strip()
        if not t:
            with self._live_best_lock:
                return (self._live_best_text or "").strip()
        try:
            t = normalize_product_transcript(t)
        except Exception:
            pass
        with self._live_best_lock:
            if gen is not None and int(gen) != int(self._live_best_gen or 0):
                # Stale worker from a previous press — ignore.
                return (self._live_best_text or "").strip()
            prev = (self._live_best_text or "").strip()
            if (not prev) or len(t) >= len(prev):
                self._live_best_text = t
            elif is_final and len(t) >= max(6, int(len(prev) * 0.55)):
                # Accept a slightly shorter final polish.
                self._live_best_text = t
            # Prefer longer of prev vs new when final is a short glitch.
            elif is_final and prev and len(t) < max(8, int(len(prev) * 0.55)):
                pass
            return (self._live_best_text or "").strip()

    def _live_best_get(self, *, gen: Optional[int] = None) -> str:
        """Snapshot best live transcript for finalize / catch-up."""
        with self._live_best_lock:
            if gen is not None and int(gen) != int(self._live_best_gen or 0):
                return ""
            return (self._live_best_text or "").strip()

    _EARLY_SILENT_KINDS = frozenset(
        {
            "app_launch",
            "input_switch",
            "mute",
            "unmute",
            "volume_up",
            "volume_down",
            "volume_set",
        }
    )
    _EARLY_BARE_APP_NAMES = frozenset(
        {
            "netflix",
            "youtube",
            "prime",
            "hulu",
            "spotify",
            "plex",
            "twitch",
            "peacock",
            "tubi",
            "browser",
            "terminal",
            "shell",
            "console",
            "bbc",
            "iplayer",
            "itv",
            "itvx",
        }
    )

    def _looks_like_silent_command_prefix(self, text: str) -> bool:
        """True while STT may still complete into a silent TV command."""
        t = (text or "").strip().lower()
        if not t:
            return True
        try:
            if tv_control.silent_tv_command_kind(t):
                return True
        except Exception:
            pass
        # Incomplete "set volume to …" (number not heard yet) — keep UI hidden
        # so we never open a Listening card that later becomes a black screen.
        if re.match(
            r"^(?:set|change|put|make|adjust)\s+(?:the\s+)?volume\b",
            t,
            re.I,
        ):
            return True
        if re.match(r"^volumes?\s+(?:to|at|level)\b", t, re.I):
            return True
        # Incomplete command still being spoken — keep UI hidden.
        if re.match(
            r"^(open|launch|start|run|go\s+to|play|load|show|put\s+on|"
            r"mute|unmute|volume|switch|change|hdmi|channel|turn|"
            r"sleep\s+timer|go\s+home|home\s+screen)\b",
            t,
            re.I,
        ):
            # Clear question wording → show the card.
            if re.search(
                r"\b(what|who|why|how|when|where|which|is|are|can|could|"
                r"tell|explain|define|weather)\b",
                t,
                re.I,
            ):
                return False
            return True
        return False

    def _maybe_show_voice_ui_for_text(self, text: str) -> None:
        """Open Listening/STT only for normal questions, not launch commands."""
        if self._looks_like_silent_command_prefix(text):
            return
        try:
            self.luna.ensure_voice_overlay()
        except Exception as exc:  # noqa: BLE001
            print(
                "[voice] ensure_voice_overlay failed: %s" % exc,
                flush=True,
            )

    def _try_early_silent_command(
        self, text: str, *, gen: int, is_final: bool = False
    ) -> bool:
        """If live STT is already a silent TV command, execute now and stop capture.

        Headless: no overlay, no STT paint, no wait for end-of-speech.
        """
        if gen != self._session_gen:
            return False
        # Warm STT reuse used to re-emit the *previous* final ("Set volume to
        # eleven.") on the next KEY_VOICE and force-close the Listening card
        # before the user could speak. Only arm early silent after this press
        # has streamed real PCM for a short window.
        t0 = float(getattr(self, "_turn_t0", 0.0) or 0.0)
        age = (time.time() - t0) if t0 > 0 else 0.0
        streamed_gen = int(getattr(self, "_stt_streamed_gen", 0) or 0)
        if streamed_gen != int(gen) or not bool(getattr(self, "_stt_streamed", False)):
            return False
        t = (text or "").strip()
        if not t or len(t) > 80:
            return False
        try:
            t = normalize_product_transcript(t)
        except Exception:
            pass
        # Ignore exact replay of the last silent command (stale warm STT).
        prev_silent = (getattr(self, "_last_early_silent_text", None) or "").strip()
        if prev_silent and t.lower().rstrip(".!?") == prev_silent.lower().rstrip(".!?"):
            print(
                "[voice] early silent ignored (stale replay): %r" % t[:60],
                flush=True,
            )
            return False
        try:
            kind = tv_control.silent_tv_command_kind(t)
        except Exception:
            return False
        if not kind or kind not in self._EARLY_SILENT_KINDS:
            return False
        words = t.split()
        # Age gate: complete app commands fire ASAP once PCM has streamed.
        # Keep a slightly longer guard for volume/mute so warm-STT ghosts
        # cannot re-fire the previous turn.
        min_age = 0.40 if kind == "app_launch" else 0.70
        if age < min_age:
            return False
        # Partials: require a complete-looking command so "open net" does not
        # fire. Finals and known bare app names can be one token.
        if not is_final:
            low = t.lower().strip()
            if kind == "app_launch":
                if len(words) < 2 and low not in self._EARLY_BARE_APP_NAMES:
                    return False
                # Incomplete free-form: still speaking the app name.
                if low.endswith(
                    (" open", " launch", " start", " go to", " play")
                ):
                    return False
                # "launch net" / "open you" — wait for a fuller name.
                if len(words) == 2 and len(words[1]) < 4:
                    return False
            elif kind == "volume_set":
                # Digits or spoken numbers ("ten", "twenty five").
                if not re.search(
                    r"\d|"
                    r"\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|"
                    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
                    r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
                    r"eighty|ninety|hundred|max|maximum|full|half|medium|mid)\b",
                    t,
                    re.I,
                ):
                    return False

        # Resolve under the lock so we only cancel capture when we will act.
        with self._early_silent_lock:
            if int(self._early_silent_fired_gen or 0) == int(gen):
                return False
            try:
                result = tv_control.handle_tv_command(t)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] early silent handle failed: %s" % exc,
                    flush=True,
                )
                return False
            if result is None:
                return False
            rkind = (result.kind or kind or "").strip()
            if rkind not in self._EARLY_SILENT_KINDS:
                return False
            self._early_silent_fired_gen = int(gen)
            self._last_early_silent_text = t

        print(
            "[voice] early silent %s from live STT (final=%s): %r"
            % (rkind, is_final, t[:60]),
            flush=True,
        )
        # Stop capture immediately so we do not wait for silence / STT finish.
        self._session_cancel.set()
        self._active = False
        self._socket_session_active = False
        self.luna.capture_active = False
        # Clear listeners' mic badge immediately (do not leave listening:true).
        try:
            self.luna.write_voice_state(False, reason="early_silent")
        except Exception:
            pass
        try:
            if self._capture is not None:
                self._capture.stop()
        except Exception:
            pass
        # Clear warm STT residual so the next press cannot re-fire this command.
        try:
            stt = getattr(self, "_stt", None)
            if stt is not None and hasattr(stt, "reset_for_new_utterance"):
                stt.reset_for_new_utterance()
        except Exception:
            pass

        def _run() -> None:
            t0 = float(getattr(self, "_turn_t0", time.time()))
            self._awaiting_playback = False
            self._tts_spoke = False
            self._cancel_dismiss()
            # Soft-hide only (keep warm process) unless we hand off to another app.
            try:
                if rkind != "app_launch":
                    self.luna.close_overlay(force=False)
            except Exception:
                pass
            if rkind == "app_launch":
                self._execute_app_launch(result)
            elif result.ok:
                self._execute_silent_tv_control(result)
            else:
                print(
                    "[voice] early silent %s not ok: %s"
                    % (rkind, result.detail),
                    flush=True,
                )
                try:
                    self.luna.end_silent_tv_control()
                except Exception:
                    try:
                        self.luna.close_overlay(force=False)
                    except Exception:
                        pass
            print(
                "[voice] timing answer_done +%.0fms kind=%s gen=%d early=1"
                % ((time.time() - t0) * 1000.0, rkind, gen),
                flush=True,
            )

        threading.Thread(
            target=_run, daemon=True, name="early-silent-tv"
        ).start()
        return True

    def _schedule_stt_warm(
        self, *, reason: str = "", delay_sec: float = 0.0
    ) -> None:
        """Background-connect STT for the next press (non-blocking).

        delay_sec: wait before connecting so we do not steal TV WAN bandwidth
        from the current chat/TTS request (was adding multi-second lag).
        """
        if not self._active_provider_key():
            return
        if self._stt_is_live():
            return

        def _warm() -> None:
            if delay_sec > 0:
                time.sleep(float(delay_sec))
            if self._stt_is_live():
                return
            # Do not warm mid-answer if a new press already started.
            if getattr(self, "_active", False) and reason not in (
                "boot",
                "after-answer",
            ):
                # Still allow delayed after-answer warms when idle.
                pass
            try:
                stt = self._ensure_stt_connected(log_ready=True)
                if stt is not None:
                    print(
                        "[voice] STT warm ready reason=%s"
                        % (reason or "n/a"),
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] STT warm failed reason=%s: %s"
                    % (reason or "n/a", exc),
                    flush=True,
                )

        threading.Thread(
            target=_warm, daemon=True, name="stt-warm-%s" % (reason or "x")
        ).start()

    def _ensure_stt_connected(self, *, log_ready: bool = False) -> Any:
        """Connect STT once under a lock (safe from pre-hot + capture threads)."""
        with self._stt_connect_lock:
            existing = getattr(self, "_stt", None)
            if self._stt_is_live(existing):
                if log_ready:
                    print(
                        "[voice] STT already live provider=%s"
                        % self.ai_provider,
                        flush=True,
                    )
                return existing
            t_stt = time.time()
            try:
                stt = self._new_stt_session()
                stt.connect()
                self._stt = stt
                ms = (time.time() - t_stt) * 1000.0
                print(
                    "[voice] STT connected provider=%s +%.0fms"
                    % (self.ai_provider, ms),
                    flush=True,
                )
                config_store.clear_last_xai_error()
                if log_ready:
                    t0 = float(getattr(self, "_turn_t0", time.time()))
                    print(
                        "[voice] timing stt_ready +%.0fms"
                        % ((time.time() - t0) * 1000.0),
                        flush=True,
                    )
                return stt
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] STT connect failed +%.0fms: %s"
                    % ((time.time() - t_stt) * 1000.0, exc),
                    flush=True,
                )
                self._note_xai_http_error(exc)
                # Do not clear a concurrent success that raced into self._stt.
                if getattr(self, "_stt", None) is existing:
                    self._stt = None
                return None

    def _pcm_apply_gain(self, chunk: bytes) -> bytes:
        """Apply capture_gain without hard-clipping (keeps STT intelligible).

        Soft-limit to ~12k peak — full-scale clips (30000+) made short
        commands like "launch Netflix" unintelligible to Grok STT.
        """
        if not chunk:
            return chunk
        g = float(self.capture_gain)
        if abs(g - 1.0) < 1e-3:
            return chunk
        try:
            pk = audioop.max(chunk, 2)
        except Exception:
            pk = 0
        # Soft ceiling well below s16 max so consonants stay clean.
        soft_max = 12000.0
        if pk > 0 and pk * g > soft_max:
            g = min(g, soft_max / float(pk))
        try:
            return audioop.mul(chunk, 2, g)
        except audioop.error:
            try:
                return audioop.mul(chunk, 2, max(0.5, g * 0.5))
            except audioop.error:
                return chunk

    def _pcm_normalize_for_stt(self, chunks: list[bytes]) -> list[bytes]:
        """Scale full utterance toward stt_target_peak (fix clip / quiet mic)."""
        blob = b"".join(c for c in chunks if c)
        if not blob:
            return list(chunks)
        try:
            pk = audioop.max(blob, 2)
            rms = audioop.rms(blob, 2)
        except Exception:
            return list(chunks)
        if pk <= 0:
            return list(chunks)
        target = int(self.stt_target_peak)
        if pk >= 20000:
            # Already hot/clipped — only scale *down* toward target.
            scale = target / float(pk)
            print(
                "[voice] PCM hot/clipped peak=%d rms=%d — normalize x%.2f"
                % (pk, rms, scale),
                flush=True,
            )
        elif pk < 5000:
            # Quiet Magic Remote speech often peaks 1–2k; allow strong boost
            # so STT hears "launch Netflix" (was failing at peak=1342).
            scale = min(14.0, target / float(pk))
            print(
                "[voice] PCM quiet peak=%d rms=%d — normalize x%.2f"
                % (pk, rms, scale),
                flush=True,
            )
        elif pk > int(target * 1.35) or pk < int(target * 0.45):
            scale = target / float(pk)
            print(
                "[voice] PCM peak=%d rms=%d — normalize x%.2f"
                % (pk, rms, scale),
                flush=True,
            )
        else:
            return list(chunks)
        try:
            out = audioop.mul(blob, 2, scale)
        except audioop.error:
            try:
                out = audioop.mul(blob, 2, min(scale, 0.95))
            except audioop.error:
                return list(chunks)
        step = 6400
        return [out[i : i + step] for i in range(0, len(out), step)]

    def _stt_upload_pcm(
        self,
        stt: Any,
        chunks: list[bytes],
        *,
        realtime: bool = False,
        silent: bool = False,
    ) -> bool:
        """Send buffered PCM to STT with light polling. Returns True if any sent.

        realtime=True paces ~realtime for short clips — dumping 1–2s of PCM in
        a few ms produced empty STT for "launch Netflix" on this TV.
        silent=True skips overlay partial paints (faster app-launch path).
        """
        sent = False
        if silent:
            cb = lambda text, is_final: None  # noqa: E731
        else:
            cb = lambda text, is_final: self.luna.transcript_partial(  # noqa: E731
                text, is_final
            )
        # Aggregate to ~100–200ms frames.
        blob = b"".join(b for b in chunks if b)
        if not blob:
            return False
        # Larger frames + no sleep on the bulk path: post-capture re-upload of
        # 4–5s audio used to add seconds of artificial delay.
        step = 3200 if realtime else 12800  # 100ms vs 400ms @ 16k s16 mono
        for i in range(0, len(blob), step):
            buf = blob[i : i + step]
            try:
                stt.send_pcm(buf)
                sent = True
                stt.poll_events(cb)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] STT upload send failed: %s" % exc,
                    flush=True,
                )
                break
            if realtime:
                # Mild pacing only for short commands (empty STT when dumped).
                time.sleep(max(0.01, (len(buf) / 32000.0) * 0.35))
        return sent

    def _pcm_trim_leading_silence(
        self, chunks: list[bytes], *, max_keep_sec: float = 0.0
    ) -> list[bytes]:
        """Drop only pure leading silence; keep soft speech onsets ("What…").

        Used for STT final pass so we keep the full question.
        """
        if not chunks:
            return []
        # Very low threshold — post-gain room floor is often ~50–150 RMS.
        thresh = max(80, min(int(self.silence_rms) // 2, 200))
        out: list[bytes] = []
        started = False
        # Keep a short pre-roll once speech starts so word onsets are not cut.
        pre_roll: list[bytes] = []
        pre_roll_max = int(0.35 * 32000)
        pre_roll_bytes = 0
        kept = 0
        max_bytes = int(max_keep_sec * 32000) if max_keep_sec > 0 else 0
        for buf in chunks:
            if not buf:
                continue
            try:
                level = audioop.rms(buf, 2)
            except Exception:
                level = thresh + 1
            if not started:
                if level < thresh:
                    pre_roll.append(buf)
                    pre_roll_bytes += len(buf)
                    while pre_roll_bytes > pre_roll_max and pre_roll:
                        dropped = pre_roll.pop(0)
                        pre_roll_bytes -= len(dropped)
                    continue
                started = True
                out.extend(pre_roll)
                kept += pre_roll_bytes
                pre_roll = []
                pre_roll_bytes = 0
            out.append(buf)
            kept += len(buf)
            if max_bytes and kept >= max_bytes:
                break
        return out if out else list(chunks)

    def _stt_needs_pcm_upload(self, stt: Any, stt_flushed: bool) -> bool:
        """Whether finalize should re-push _pcm_chunks into the STT session.

        Gemini buffers PCM locally during the live worker; re-uploading the same
        chunks doubles the WAV and STT latency. Only push if its buffer is empty.

        xAI live STT: if the worker already streamed successfully, do **not**
        re-push — empty current_text() before finish() is normal, and re-upload
        doubles the audio (seen as duration≈2× real length + empty transcript).
        """
        if stt is None:
            return False
        if isinstance(stt, (GeminiSttSession, OpenRouterSttSession)):
            return len(getattr(stt, "_buf", b"")) == 0 and bool(self._pcm_chunks)
        # Live xAI WebSocket: only upload if the worker never got bytes out.
        if stt_flushed:
            return False
        return bool(self._pcm_chunks)

    def _stt_connect_blocking(self, timeout: float = 4.0) -> Any:
        """Ensure a live STT session; connect now if the background thread lagged."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stt_is_live():
                return self._stt
            time.sleep(0.05)
        # Background connect failed or timed out — try once more (locked).
        return self._ensure_stt_connected(log_ready=False)

    def _save_capture_debug(
        self, chunks: list[bytes], *, peak: int = 0, total_bytes: int = 0
    ) -> None:
        """Write last utterance as raw PCM + WAV for offline STT checks."""
        blob = b"".join(c for c in chunks if c)
        if not blob:
            return
        raw_path = "/tmp/launch-home-voice-last-capture.raw"
        wav_path = "/tmp/launch-home-voice-last-capture.wav"
        with open(raw_path, "wb") as fh:
            fh.write(blob)
        # Minimal WAV header (16 kHz mono s16le).
        n = len(blob)
        import struct as _struct

        hdr = _struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + n,
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            CAPTURE_RATE,
            CAPTURE_RATE * 2,
            2,
            16,
            b"data",
            n,
        )
        with open(wav_path, "wb") as fh:
            fh.write(hdr)
            fh.write(blob)
        # Coarse energy stats (1s windows).
        win = CAPTURE_RATE * 2  # 1s of s16 mono
        parts = []
        for i in range(0, n, win):
            piece = blob[i : i + win]
            if len(piece) < 640:
                break
            try:
                parts.append(str(audioop.rms(piece, 2)))
            except Exception:
                parts.append("?")
        sec = n / float(CAPTURE_RATE * 2)
        print(
            "[voice] capture debug saved %s (%.2fs peak=%d bytes=%d rms/s=%s)"
            % (wav_path, sec, peak, total_bytes or n, ",".join(parts[:12])),
            flush=True,
        )

    @staticmethod
    def _stt_text_usable(text: str, *, language: str = "en") -> bool:
        """False for empty / CJK-hallucination when we expect English speech.

        Grok STT sometimes returns '哦' / '我' on near-silent Magic Remote clips
        even with language=en. Those must not become the user question.
        """
        t = (text or "").strip()
        if not t:
            return False
        lang = (language or "en").lower()
        if lang.startswith("en"):
            # Need at least one Latin letter or digit.
            if not re.search(r"[A-Za-z0-9]", t):
                return False
            # Reject pure punctuation / single CJK with a stray mark.
            latin = re.findall(r"[A-Za-z0-9']+", t)
            if not latin:
                return False
        return True

    def _stt_rest_transcribe(
        self, chunks: list[bytes], *, silent: bool = False, quick: bool = False
    ) -> str:
        """Fast one-shot REST STT (no WebSocket). Preferred when WS is cold.

        On this TV a cold STT WebSocket often costs 10–16s; REST for a few
        seconds of PCM typically returns in ~1–2s.
        quick=True: only the normalized candidate (used in REST/WS races so a
        hung first attempt cannot burn 16s on two sequential 8s timeouts).
        """
        if not chunks or not self.api_key:
            return ""
        if not silent:
            try:
                self.luna.status("Transcribing…")
            except Exception:
                pass
        t0 = time.time()
        text = ""
        # Try normalized first, then lightly-padded raw — over-normalize has
        # produced empty REST results on short Magic Remote clips.
        candidates: list[tuple[str, list[bytes]]] = [
            ("norm", self._pcm_normalize_for_stt(list(chunks))),
        ]
        if not quick:
            pad = b"\x00" * int(0.2 * 32000)
            raw_pad = [pad] + list(chunks) + [pad]
            candidates.append(("raw_pad", raw_pad))
        for label, use in candidates:
            blob = b"".join(c for c in use if c)
            if not blob or len(blob) < 1600:
                continue
            try:
                cand = stt_transcribe_file(
                    self.api_key,
                    blob,
                    sample_rate=CAPTURE_RATE,
                    language=self.stt_language or "en",
                    keyterms=STT_KEYTERMS,
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] STT REST %s failed: %s" % (label, exc),
                    flush=True,
                )
                continue
            cand = (cand or "").strip()
            if self._stt_text_usable(cand, language=self.stt_language):
                text = cand
                print(
                    "[voice] STT REST path +%.0fms via=%s text=%r"
                    % ((time.time() - t0) * 1000.0, label, text[:60]),
                    flush=True,
                )
                break
            if cand:
                print(
                    "[voice] STT REST discarded unusable via=%s text=%r"
                    % (label, cand[:40]),
                    flush=True,
                )
        if not text:
            print(
                "[voice] STT REST path +%.0fms text='' (all variants empty)"
                % ((time.time() - t0) * 1000.0),
                flush=True,
            )
        if text and not silent:
            try:
                self.luna.transcript_partial(text, is_final=True)
            except Exception:
                pass
        return text

    def _stt_full_buffer_transcribe(
        self,
        chunks: list[bytes],
        *,
        raw: bool = False,
        silent: bool = False,
        allow_rest: bool = True,
    ) -> str:
        """One clean STT pass over the full capture buffer (fresh session).

        silent=True: do not paint live STT on the overlay (app-launch path
        only needs the text internally — showing/speaking it slows hand-off).
        allow_rest=False: WS-only (used when REST is already racing in parallel).
        """
        if not chunks:
            return ""
        # Reuse STT pre-connected during capture when still live (avoids
        # post-capture TLS timeout that produced garbage "Cue").
        old = getattr(self, "_stt", None)
        stt = None
        if old is not None and self._stt_is_live(old):
            stt = old
            print("[voice] STT reusing pre-connected session", flush=True)
        else:
            # Cold WS is slow on TV. Prefer REST unless a parallel race already
            # owns the REST path (allow_rest=False).
            if allow_rest:
                rest = self._stt_rest_transcribe(chunks, silent=silent)
                if rest:
                    return rest
            self._stt = None
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            stt = self._ensure_stt_connected(log_ready=False)
        if stt is None:
            if allow_rest:
                return self._stt_rest_transcribe(chunks, silent=silent)
            return ""
        blob0 = b"".join(c for c in chunks if c)
        sec0 = len(blob0) / 32000.0 if blob0 else 0.0
        # Short voice commands: skip trim/normalize first (they wiped "launch
        # Netflix" into empty STT on this TV). Pad a little silence so the
        # model sees a clear end-of-utterance.
        if raw or sec0 <= 3.0:
            use = list(chunks)
            pad = b"\x00" * int(0.25 * 32000)  # 250ms silence
            if use:
                use = [pad] + use + [pad]
            # Always boost quiet short clips (peak~1–2k needs gain for STT).
            if use:
                use = self._pcm_normalize_for_stt(use)
        else:
            # Soft leading silence only — keep word onsets ("What is…").
            trimmed = self._pcm_trim_leading_silence(chunks)
            use = trimmed if trimmed else list(chunks)
            # Undo clip / quiet mic so STT sees clean levels (not ±32768 garbage).
            use = self._pcm_normalize_for_stt(use)
        nbytes = sum(len(c) for c in use)
        try:
            pk = audioop.max(b"".join(use), 2) if use else 0
            rms = audioop.rms(b"".join(use), 2) if use else 0
        except Exception:
            pk = rms = 0
        print(
            "[voice] STT full-buffer upload chunks=%d bytes=%d (%.2fs peak=%d rms=%d silent=%s)"
            % (len(use), nbytes, nbytes / 32000.0, pk, rms, silent),
            flush=True,
        )
        t_up = time.time()
        # Only skip re-upload when THIS live session already produced text.
        # A failed stream-during-capture sets _stt_streamed=True then closes the
        # socket; falling back with skip-upload sent audio.done on an empty
        # session (duration=0) and always returned '' (peak=5473 failures).
        already = (stt.current_text() or "").strip()
        same_live = old is not None and stt is old and self._stt_is_live(stt)
        need_upload = not (
            same_live
            and bool(getattr(self, "_stt_streamed", False))
            and bool(already)
        )
        if need_upload:
            self._stt_upload_pcm(
                stt, use, realtime=(sec0 <= 2.5 or raw), silent=silent
            )
        else:
            print(
                "[voice] STT skip re-upload (live text ready) early=%r"
                % (already[:60],),
                flush=True,
            )
        # Let partials settle before audio.done (batch path has no live text).
        if silent:
            cb = lambda text, is_final: None  # noqa: E731
        else:
            cb = lambda text, is_final: self.luna.transcript_partial(  # noqa: E731
                text, is_final
            )
        # Short settle: if capture already streamed, text is usually ready.
        sec = nbytes / 32000.0
        if already and len(already.split()) >= 3 and not need_upload:
            settle_deadline = time.time() + 0.45
        elif sec < 3.5:
            settle_deadline = time.time() + min(1.4, 0.5 + sec * 0.3)
        else:
            settle_deadline = time.time() + min(1.8, 0.45 + sec * 0.25)
        best = already
        last_change = time.time()
        while time.time() < settle_deadline:
            try:
                stt.poll_events(cb, timeout=0.08)
            except Exception:
                pass
            cur = (stt.current_text() or "").strip()
            if cur and len(cur) >= len(best):
                if cur != best:
                    best = cur
                    last_change = time.time()
            words = len(best.split())
            lower_best = best.lower()
            incomplete_location = bool(
                re.search(
                    r"\b(?:weather|forecast|temperature)\s*$|"
                    r"\b(?:in|at|for|near)\s*$",
                    lower_best,
                )
            )
            if best and not incomplete_location and (
                (words >= 4 and time.time() - last_change >= 0.15)
                or (sec < 2.2 and words >= 2 and time.time() - last_change >= 0.2)
            ):
                break
            time.sleep(0.01)
        try:
            text = (stt.finish() or "").strip()
        except Exception as exc:  # noqa: BLE001
            print("[voice] STT full-buffer finish failed: %s" % exc, flush=True)
            text = (stt.current_text() or best or "").strip()
        if best and (not text or (len(best) > len(text) + 4)):
            text = best
        try:
            stt.close()
        except Exception:
            pass
        self._stt = None
        # Reject CJK / non-English hallucinations when language is English.
        if not self._stt_text_usable(text, language=self.stt_language):
            if text:
                print(
                    "[voice] STT discarded unusable text=%r (lang=%s)"
                    % ((text or "")[:40], self.stt_language),
                    flush=True,
                )
            text = ""
        # Short-command / bad-WS fallback: REST file STT.
        if not (text or "").strip() and sec0 <= 6.0 and self.api_key and blob0:
            print(
                "[voice] STT WS empty/unusable — trying REST file STT (%.2fs)"
                % sec0,
                flush=True,
            )
            try:
                # Soft-limit for REST too (avoid sending full-scale clips).
                rest_chunks = self._pcm_normalize_for_stt(list(chunks))
                rest_pcm = b"".join(rest_chunks) if rest_chunks else blob0
                text = stt_transcribe_file(
                    self.api_key,
                    rest_pcm,
                    sample_rate=CAPTURE_RATE,
                    language=self.stt_language or "en",
                    keyterms=STT_KEYTERMS,
                )
                if not self._stt_text_usable(text, language=self.stt_language):
                    if text:
                        print(
                            "[voice] STT REST discarded unusable text=%r"
                            % (text or "")[:40],
                            flush=True,
                        )
                    text = ""
            except Exception as exc:  # noqa: BLE001
                print("[voice] STT REST exception: %s" % exc, flush=True)
                self._note_xai_http_error(exc)
                text = ""
        print(
            "[voice] STT full-buffer done +%.0fms text=%r"
            % ((time.time() - t_up) * 1000.0, (text or "")[:80]),
            flush=True,
        )
        return text

    def _finalize_socket_session(
        self,
        stt_flushed: bool,
        *,
        heard_speech: bool = False,
        peak: int = 0,
        total_bytes: int = 0,
        session_gen: int = 0,
        stt_errors: bool = False,
    ) -> None:
        # Capture which generation this finalize belongs to (guard late races).
        gen = int(session_gen or self._session_gen)
        if gen != self._session_gen:
            print(
                "[voice] finalize skipped before start "
                "(stale gen=%d current=%d)"
                % (gen, self._session_gen),
                flush=True,
            )
            return
        try:
            self._active = False
            self.luna.capture_active = False
            # Close this press's socket, then stop the remote mic stream so the
            # Magic Remote LED goes dark (users reported "mic back on when not
            # speaking" from leftover startStreaming + keepalive re-arms).
            if self._capture is not None:
                self._capture.stop()
                self._capture = None
            try:
                with self._mic_stream_lock():
                    self._stop_native_streaming()
                    self._stream_socket_path = None
                print(
                    "[voice] mic stopped after capture (gen=%d)" % gen,
                    flush=True,
                )
            except Exception as stop_exc:  # noqa: BLE001
                print(
                    "[voice] mic stop after capture failed: %s" % stop_exc,
                    flush=True,
                )
            self._last_session_end_ts = time.time()

            # Release the session lock once capture ends so a new press can
            # cancel / start a turn while answer/TTS still runs for this gen.
            if self._session_lock.locked():
                try:
                    self._session_lock.release()
                    print(
                        "[voice] session lock released after capture (gen=%d)"
                        % gen,
                        flush=True,
                    )
                except RuntimeError:
                    pass

            if gen != self._session_gen or self._session_cancel.is_set():
                print(
                    "[voice] finalize aborted (stale gen=%d current=%d cancel=%s)"
                    % (gen, self._session_gen, self._session_cancel.is_set()),
                    flush=True,
                )
                return
            # Mid-capture silent command already ran (app launch / mute / …).
            if int(getattr(self, "_early_silent_fired_gen", 0) or 0) == int(gen):
                print(
                    "[voice] finalize skipped (early silent command gen=%d)"
                    % gen,
                    flush=True,
                )
                return

            transcript = ""
            sec_audio = float(total_bytes) / 32000.0

            # Fast path: audio was streamed during capture → finish() only.
            # Empty stream → race REST + WS re-upload in parallel.
            has_energy = bool(
                self._pcm_chunks
                and (
                    peak >= 80
                    or heard_speech
                    or peak >= self.mic_hot_rms
                    or stt_flushed
                    or total_bytes >= 16000
                )
            )
            stt = self._stt if self._stt_is_live() else None
            early = (stt.current_text() or "").strip() if stt is not None else ""
            try:
                early = normalize_product_transcript(early) if early else early
            except Exception:
                pass
            # Synced best hypothesis from capture worker (may beat WS finish).
            live_best = self._live_best_get(gen=gen)
            try:
                live_best = (
                    normalize_product_transcript(live_best) if live_best else live_best
                )
            except Exception:
                pass
            if live_best and (
                not early or len(live_best) > len(early) + 2
            ):
                early = live_best

            # Hide STT paint for short/soft command clips and for live
            # transcripts that already resolve to silent TV actions (app
            # launch, mute, volume, HDMI) so "launch terminal" never paints
            # transcript cards. Classification only — no Luna side effects.
            silent_stt = sec_audio <= 2.0 and peak < 5500
            if not silent_stt and early:
                try:
                    if tv_control.silent_tv_command_kind(early):
                        silent_stt = True
                except Exception:
                    pass
            if not silent_stt:
                try:
                    self.luna.status("Transcribing…")
                except Exception:
                    pass
            else:
                # Stay on Listening… — do not flash Transcribing for commands.
                try:
                    self.luna.status("Listening…")
                except Exception:
                    pass

            print(
                "[voice] STT finalize "
                "(streamed=%s errors=%s sec=%.2f peak=%d early=%r live_best=%r silent=%s)"
                % (
                    stt_flushed,
                    stt_errors,
                    sec_audio,
                    peak,
                    (early or "")[:50],
                    (live_best or "")[:50],
                    silent_stt,
                ),
                flush=True,
            )
            # Stream-finish only when we already have live text. Bytes-sent with
            # empty early often means a dead/stale warm WS — finish() yields ''
            # and must not block a clean re-upload fallback.
            use_stream_finish = bool(
                has_energy
                and stt is not None
                and stt_flushed
                and not stt_errors
                and early
            )
            if use_stream_finish:
                t_fin = time.time()
                if not silent_stt:
                    try:
                        self.luna.transcript_partial(early, is_final=False)
                    except Exception:
                        pass
                try:
                    transcript = (stt.finish() or "").strip()
                except Exception as exc:  # noqa: BLE001
                    print(
                        "[voice] STT finish failed: %s" % exc, flush=True
                    )
                    transcript = (stt.current_text() or early or "").strip()
                try:
                    stt.close()
                except Exception:
                    pass
                self._stt = None
                self._stt_streamed = False
                if early and (
                    not transcript or len(early) > len(transcript) + 4
                ):
                    transcript = early
                # Prefer synced live_best if finish() collapsed a good line.
                if live_best and (
                    not transcript or len(live_best) > len(transcript) + 4
                ):
                    print(
                        "[voice] STT live_best sync kept %r over finish %r"
                        % ((live_best or "")[:60], (transcript or "")[:60]),
                        flush=True,
                    )
                    transcript = live_best
                try:
                    transcript = normalize_product_transcript(transcript)
                except Exception:
                    pass
                if not self._stt_text_usable(
                    transcript, language=self.stt_language
                ):
                    transcript = ""
                print(
                    "[voice] STT stream-finish +%.0fms text=%r"
                    % (
                        (time.time() - t_fin) * 1000.0,
                        (transcript or "")[:60],
                    ),
                    flush=True,
                )
            elif has_energy and stt is not None and stt_flushed and not early:
                # Streamed PCM but never got a hypothesis — abandon that socket.
                print(
                    "[voice] STT stream empty during capture — race REST+WS",
                    flush=True,
                )
                try:
                    stt.close()
                except Exception:
                    pass
                self._stt = None
                self._stt_streamed = False
                stt_flushed = False
            if has_energy and not (transcript or "").strip() and live_best:
                # Capture painted a good line even if WS finish was empty.
                if self._stt_text_usable(live_best, language=self.stt_language):
                    transcript = live_best
                    print(
                        "[voice] STT live_best sync used as final: %r"
                        % (transcript[:60],),
                        flush=True,
                    )
            if has_energy and not (transcript or "").strip():
                # Race REST and a fresh WS full-buffer re-upload; take the
                # first usable result (whichever returns first).
                t_fb = time.time()
                self._stt_streamed = False
                chunks_snap = list(self._pcm_chunks)
                race_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
                # Paint STT during race only for normal questions — silent
                # TV commands stay on Listening… with no transcript card.
                race_silent = bool(silent_stt)
                if not race_silent:
                    try:
                        self.luna.status("Transcribing…")
                    except Exception:
                        pass

                def _race_rest() -> None:
                    try:
                        t = self._stt_rest_transcribe(
                            chunks_snap, silent=race_silent, quick=True
                        )
                        if t:
                            race_q.put(("rest", t))
                    except Exception as exc:  # noqa: BLE001
                        print(
                            "[voice] STT race REST failed: %s" % exc,
                            flush=True,
                        )
                    finally:
                        race_q.put(("rest_done", ""))

                def _race_ws() -> None:
                    try:
                        # Ensure no half-open session shares the race.
                        old = getattr(self, "_stt", None)
                        if old is not None:
                            try:
                                old.close()
                            except Exception:
                                pass
                            self._stt = None
                        self._stt_streamed = False
                        prefer_raw = sec_audio <= 3.5 or peak >= 3000
                        t = self._stt_full_buffer_transcribe(
                            chunks_snap,
                            raw=prefer_raw,
                            silent=race_silent,
                            allow_rest=False,
                        )
                        if t:
                            race_q.put(("ws", t))
                        elif not prefer_raw:
                            t2 = self._stt_full_buffer_transcribe(
                                chunks_snap,
                                raw=True,
                                silent=race_silent,
                                allow_rest=False,
                            )
                            if t2:
                                race_q.put(("ws_raw", t2))
                    except Exception as exc:  # noqa: BLE001
                        print(
                            "[voice] STT race WS failed: %s" % exc,
                            flush=True,
                        )
                    finally:
                        race_q.put(("ws_done", ""))

                thr_rest = threading.Thread(
                    target=_race_rest, daemon=True, name="stt-race-rest"
                )
                thr_ws = threading.Thread(
                    target=_race_ws, daemon=True, name="stt-race-ws"
                )
                thr_rest.start()
                thr_ws.start()
                finished = 0
                # Cold WS on this TV often needs 8–12s; REST can also stall.
                # 10s was aborting after partials and before finish().
                deadline = time.time() + 16.0
                source = ""
                last_live_paint = ""

                def _race_live_candidate() -> str:
                    stt_live = getattr(self, "_stt", None)
                    if stt_live is None:
                        return ""
                    try:
                        cur = (stt_live.current_text() or "").strip()
                    except Exception:
                        return ""
                    if not cur:
                        return ""
                    try:
                        cur = normalize_product_transcript(cur)
                    except Exception:
                        pass
                    if not self._stt_text_usable(
                        cur, language=self.stt_language
                    ):
                        return ""
                    words = len(cur.split())
                    lower = cur.lower().rstrip(".!? ")
                    # Do not accept mid-phrase stubs ("Where is", "Go to").
                    incomplete = bool(
                        re.search(
                            r"\b(?:where|what|who|when|how|why|go to|open|"
                            r"launch|weather|forecast)\s*$|"
                            r"\b(?:in|at|for|near|on|to)\s*$",
                            lower,
                        )
                    )
                    if incomplete:
                        return ""
                    if words >= 3 or (sec_audio <= 2.5 and words >= 2):
                        return cur
                    return ""

                while finished < 2 and time.time() < deadline:
                    # Accept solid live partials mid-race (before finish()).
                    live = _race_live_candidate()
                    if live:
                        transcript = live
                        source = "ws_live"
                        print(
                            "[voice] STT race winner=%s +%.0fms text=%r"
                            % (
                                source,
                                (time.time() - t_fb) * 1000.0,
                                transcript[:60],
                            ),
                            flush=True,
                        )
                        break
                    # Paint partials even when not yet complete enough to win.
                    # Never paint silent TV commands (open settings / Netflix).
                    stt_live = getattr(self, "_stt", None)
                    if stt_live is not None:
                        try:
                            paint = (stt_live.current_text() or "").strip()
                        except Exception:
                            paint = ""
                        if paint and paint != last_live_paint:
                            last_live_paint = paint
                            skip_paint = False
                            try:
                                if (
                                    self._looks_like_silent_command_prefix(paint)
                                    or tv_control.silent_tv_command_kind(paint)
                                ):
                                    skip_paint = True
                            except Exception:
                                skip_paint = False
                            if not skip_paint:
                                try:
                                    self.luna.transcript_partial(
                                        paint, is_final=False
                                    )
                                except Exception:
                                    pass
                    try:
                        kind, val = race_q.get(timeout=0.12)
                    except queue.Empty:
                        continue
                    if kind in ("rest_done", "ws_done"):
                        finished += 1
                        continue
                    cand = (val or "").strip()
                    try:
                        cand = normalize_product_transcript(cand)
                    except Exception:
                        pass
                    if self._stt_text_usable(
                        cand, language=self.stt_language
                    ):
                        transcript = cand
                        source = kind
                        print(
                            "[voice] STT race winner=%s +%.0fms text=%r"
                            % (
                                kind,
                                (time.time() - t_fb) * 1000.0,
                                transcript[:60],
                            ),
                            flush=True,
                        )
                        break
                thr_rest.join(timeout=0.2)
                thr_ws.join(timeout=0.2)
                # Late finish may have landed after we stopped waiting.
                if not self._stt_text_usable(
                    transcript, language=self.stt_language
                ):
                    try:
                        while True:
                            kind, val = race_q.get_nowait()
                            if kind in ("rest_done", "ws_done"):
                                continue
                            cand = (val or "").strip()
                            try:
                                cand = normalize_product_transcript(cand)
                            except Exception:
                                pass
                            if self._stt_text_usable(
                                cand, language=self.stt_language
                            ):
                                transcript = cand
                                source = kind or "late"
                                print(
                                    "[voice] STT race late winner=%s "
                                    "+%.0fms text=%r"
                                    % (
                                        source,
                                        (time.time() - t_fb) * 1000.0,
                                        transcript[:60],
                                    ),
                                    flush=True,
                                )
                                break
                    except queue.Empty:
                        pass
                if not self._stt_text_usable(
                    transcript, language=self.stt_language
                ):
                    live = _race_live_candidate()
                    if live:
                        transcript = live
                        source = source or "ws_live_late"
                    else:
                        transcript = ""
                print(
                    "[voice] STT post-capture +%.0fms winner=%s text=%r"
                    % (
                        (time.time() - t_fb) * 1000.0,
                        source or "none",
                        (transcript or "")[:60],
                    ),
                    flush=True,
                )

            # A new KEY_VOICE press may start while this generation is waiting
            # for STT. Never let the older finalizer fail or clear the new turn.
            if gen != self._session_gen or self._session_cancel.is_set():
                print(
                    "[voice] STT result discarded after barge-in "
                    "(stale gen=%d current=%d cancel=%s)"
                    % (gen, self._session_gen, self._session_cancel.is_set()),
                    flush=True,
                )
                return

            if not transcript:
                last_err = config_store.load_last_xai_error()
                if last_err and last_err.get("message"):
                    msg = str(last_err["message"])
                elif total_bytes < 4000 or peak < 80:
                    msg = "No speech detected — wait for Listening, then speak"
                elif not self._active_provider_key():
                    msg = "Speech service unavailable — check internet / API key"
                elif peak < 450:
                    msg = (
                        "Mic too quiet (peak=%d) — wait for Listening, then "
                        "speak clearly into the remote"
                        % peak
                    )
                else:
                    # Quiet remote speech that STT still failed — not "too quiet".
                    msg = (
                        "Could not understand audio (peak=%d, %d bytes) — try again"
                        % (peak, total_bytes)
                    )
                self._fail(msg)
                return
            if len(transcript) > 80 and _STT_KEYTERM_ECHO_RE.search(transcript):
                print(
                    "[voice] STT keyterm echo rejected before routing: %r"
                    % transcript[:140],
                    flush=True,
                )
                self._fail("I could not understand that clearly. Please try again.")
                return
            transcript = weather.normalize_weather_transcript(transcript)
            transcript = normalize_product_transcript(transcript)
            print("[voice] transcript final: %r" % transcript, flush=True)
            t0 = float(getattr(self, "_turn_t0", time.time()))
            print(
                "[voice] timing transcript +%.0fms gen=%d text=%r"
                % ((time.time() - t0) * 1000.0, gen, (transcript or "")[:60]),
                flush=True,
            )

            # ---- Fast app launch: no STT paint, no TTS, just open the app ----
            try:
                early_tv = tv_control.handle_tv_command(transcript)
                print(
                    "[voice] tv_control match text=%r -> %s"
                    % (
                        (transcript or "")[:50],
                        None
                        if early_tv is None
                        else "%s id=%s"
                        % (
                            early_tv.kind,
                            (early_tv.detail or {}).get("id"),
                        ),
                    ),
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print("[voice] tv_control early failed: %s" % exc, flush=True)
                early_tv = None
            early_kind = (early_tv.kind or "") if early_tv is not None else ""
            silent_control_kinds = frozenset(
                {
                    "input_switch",
                    "mute",
                    "unmute",
                    "volume_up",
                    "volume_down",
                    "volume_set",
                }
            )
            if early_tv is not None and (
                early_kind == "app_launch" or early_kind in silent_control_kinds
            ):
                print(
                    "[voice] transcript used (silent %s): %r"
                    % (early_kind, (transcript or "")[:60]),
                    flush=True,
                )
                self._awaiting_playback = False
                self._tts_spoke = False
                self._cancel_dismiss()
                self._session_cancel.set()
                # Soft-hide for mute/volume (keep warm). App launch force-closes.
                if early_kind != "app_launch":
                    try:
                        self.luna.close_overlay(force=False)
                    except Exception:
                        pass
                if early_kind == "app_launch":
                    self._execute_app_launch(early_tv)
                elif early_tv.ok:
                    self._execute_silent_tv_control(early_tv)
                else:
                    # Failures: show a brief error card only.
                    self._session_cancel.clear()
                    self._active = False
                    self.luna.capture_active = False
                    try:
                        self.luna.ensure_voice_overlay()
                    except Exception:
                        pass
                    self.luna.error(
                        early_tv.spoken or "I could not complete that TV command."
                    )
                    self._schedule_dismiss(short=True)
                # Hard stop this generation so no later path can chat/reclaim.
                self._active = False
                self.luna.capture_active = False
                print(
                    "[voice] timing answer_done +%.0fms kind=%s gen=%d"
                    % ((time.time() - t0) * 1000.0, early_kind, gen),
                    flush=True,
                )
                return

            # If we already handed off to an app this press, never chat.
            if self.luna.app_handoff_active():
                print(
                    "[voice] skip chat (app handoff active) text=%r"
                    % (transcript or "")[:50],
                    flush=True,
                )
                return

            # Q&A / weather / errors need the card — open it only now.
            try:
                self.luna.ensure_voice_overlay()
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] ensure_voice_overlay before answer: %s" % exc,
                    flush=True,
                )

            # Reject pure STT garbage. Single nonsense words from bad audio
            # ("Cue", "You.", "哦") must NOT go to chat. Real short nouns
            # ("molecule", "Netflix") still answer.
            words = [
                w
                for w in re.findall(r"[A-Za-z0-9']+", transcript or "")
                if w
            ]
            _stop_only = frozenset(
                """
                a an the and or but if then than that this these those there here
                to of in on at for from by with as so yes no ok okay oh um uh
                is am are was were be been being do does did have has had
                i me my we us you he she it they them
                today tonight tomorrow please right now just well yeah yep
                cue queue que q
                """.split()
            )
            _app_words = frozenset(
                {
                    "prime",
                    "netflix",
                    "youtube",
                    "amazon",
                    "disney",
                    "spotify",
                    "hulu",
                    "max",
                    "browser",
                    "settings",
                    "home",
                }
            )
            content_words = [
                w for w in words if w.lower() not in _stop_only and len(w) > 1
            ]
            raw = (transcript or "").strip()
            only_non_latin = bool(raw) and not re.search(
                r"[A-Za-z0-9]", raw
            )
            # One short mystery word from a failed clip (Cue/You) — not a question.
            one_junk = (
                len(content_words) == 1
                and content_words[0].lower() not in _app_words
                and len(content_words[0]) <= 4
                and self._local_definition_answer(transcript) is None
                and self._local_time_answer(transcript) is None
            )
            is_garbled = (
                early_tv is None
                and not weather.is_weather_query(transcript or "")
                and gen > self._app_handoff_gen
                and not self.luna.app_handoff_active()
                and (
                    only_non_latin
                    or (not content_words and bool(words))
                    or (not words and not raw)
                    or one_junk
                )
            )
            # Incomplete open/launch with no target (STT cut after first word).
            bare_open = bool(
                re.match(
                    r"^(?:open|launch|start|go\s+to|show)\.?$",
                    (transcript or "").strip(),
                    re.I,
                )
            )
            if bare_open and early_tv is None:
                self._cancel_lg_recognition()
                print(
                    "[voice] incomplete open command: %r"
                    % (transcript or "")[:60],
                    flush=True,
                )
                self.luna.suppress_reclaim = False
                try:
                    self.luna.transcript_final(transcript or "")
                except Exception:
                    pass
                msg = (
                    "Say the full command — for example open settings, "
                    "or open Netflix."
                )
                try:
                    self.luna.answer_final(msg)
                except Exception:
                    pass
                if self._tts_available():
                    try:
                        self.luna.tts_will_speak(True)
                        if getattr(self, "_tts_queue", None) is None:
                            self._tts_begin()
                        self._tts_feed_answer_text(msg)
                        self._tts_end()
                        self.luna.tts_complete(self._tts_seq)
                    except Exception:
                        self._awaiting_playback = False
                else:
                    self._awaiting_playback = False
                self._schedule_dismiss(short=False)
                return

            if is_garbled:
                self._cancel_lg_recognition()
                print(
                    "[voice] garbled transcript — ask repeat: %r"
                    % (transcript or "")[:60],
                    flush=True,
                )
                self.luna.suppress_reclaim = False
                try:
                    self.luna.transcript_final(transcript or "")
                except Exception:
                    pass
                msg = (
                    "I didn't catch that clearly — press voice and "
                    "say your command again."
                )
                try:
                    self.luna.answer_final(msg)
                except Exception:
                    pass
                print(
                    "[voice] answer final [repeat]: %r" % msg[:120],
                    flush=True,
                )
                if self._tts_available():
                    try:
                        self.luna.tts_will_speak(True)
                        if getattr(self, "_tts_queue", None) is None:
                            self._tts_begin()
                        self._tts_feed_answer_text(msg)
                        self._tts_end()
                        self.luna.tts_complete(self._tts_seq)
                    except Exception:
                        self._awaiting_playback = False
                else:
                    self._awaiting_playback = False
                self._schedule_dismiss(short=False)
                return

            # Keep suppress_reclaim True until the answer is painted — clearing
            # it here let reclaim / re-launch flash Home between STT and answer.
            try:
                self.luna.status("Got your question…")
            except Exception:
                pass
            transcript = self.luna.transcript_final(transcript)
            self._cancel_lg_recognition()
            print("[voice] transcript used: %r" % transcript, flush=True)
            answer_t0 = time.time()
            # Immediate feedback while weather/chat may take a few seconds.
            try:
                if weather.is_weather_query(transcript):
                    self.luna.status("Looking up weather…")
                else:
                    self.luna.status("Thinking…")
            except Exception:
                pass

            # Answer immediately. Keep voiceinput startStreaming WARM so the
            # next press does not pay 2–3s stop/start (lost "what is a molecule").
            # Only dismiss LG voice chrome — do not call stopStreaming here.
            def _post_capture_cleanup() -> None:
                if gen <= self._app_handoff_gen or self.luna.app_handoff_active():
                    return
                try:
                    # Visual-only suppression keeps the LG bar down without
                    # disturbing the warm Magic Remote voiceinput stream.
                    self._cancel_lg_voice_ui(after_capture=True)
                except Exception:
                    pass

            threading.Thread(target=_post_capture_cleanup, daemon=True).start()
            if (
                gen != self._session_gen
                or self._session_cancel.is_set()
                or gen <= self._app_handoff_gen
            ):
                print(
                    "[voice] session cancelled/stale before answer (gen=%d)"
                    % gen,
                    flush=True,
                )
                if (
                    gen == self._session_gen
                    and gen > self._app_handoff_gen
                    and not self.luna.app_handoff_active()
                ):
                    self._schedule_dismiss(short=True)
                return
            # Only re-launch if the card is actually gone; re-send status after.
            if self.luna.ws_client_count() <= 0:
                if not self.luna.ensure_overlay_connected(timeout=2.0):
                    print(
                        "[voice] WARNING: answering with no overlay WS",
                        flush=True,
                    )
                else:
                    try:
                        self.luna.transcript_final(transcript)
                        if weather.is_weather_query(transcript):
                            self.luna.status("Looking up weather…")
                        else:
                            self.luna.status("Thinking…")
                    except Exception:
                        pass
            self._overlay_ready_for_audio.set()

            answer_kind = self._answer_and_speak(transcript)
            # Safe to reclaim again only after we have finished the answer path.
            try:
                self.luna.suppress_reclaim = False
            except Exception:
                pass
            t0 = float(getattr(self, "_turn_t0", answer_t0))
            print(
                "[voice] timing answer_done +%.0fms (path +%.0fms) kind=%s gen=%d"
                % (
                    (time.time() - t0) * 1000.0,
                    (time.time() - answer_t0) * 1000.0,
                    answer_kind,
                    gen,
                ),
                flush=True,
            )
            # Warm STT only after the answer uses the network (not during chat).
            self._schedule_stt_warm(reason="after-answer", delay_sec=0.5)
            if gen != self._session_gen:
                print(
                    "[voice] answer discarded (stale gen=%d current=%d)"
                    % (gen, self._session_gen),
                    flush=True,
                )
                return
            if self._session_cancel.is_set() or answer_kind == "cancelled":
                print("[voice] session cancelled during answer", flush=True)
                self._schedule_dismiss(short=True)
                return
            # Power/screen off: short dismiss. App launch already force-closed
            # the overlay in _answer_and_speak — do not re-arm sessionEnded.
            if answer_kind == "app_launch":
                pass
            elif answer_kind in ("power_off", "screen_off"):
                self._schedule_dismiss(short=True)
            else:
                # TTS may still be synthesizing more chunks after the first.
                # Wait for first audio, then arm dismiss; each later chunk and
                # _tts_end re-arm with the full byte budget so speech is not
                # cut after the opening phrase.
                if self._tts_available() and getattr(self, "_tts_fed", 0) > 0:
                    wait_tts = time.time() + 4.0
                    while (
                        not self._tts_spoke
                        and time.time() < wait_tts
                        and gen == self._session_gen
                    ):
                        time.sleep(0.05)
                    if self._tts_spoke:
                        print(
                            "[voice] first TTS audio ready before dismiss "
                            "(bytes=%d chunks=%d)"
                            % (
                                getattr(self, "_tts_audio_bytes", 0),
                                getattr(self, "_tts_seq", 0),
                            ),
                            flush=True,
                        )
                    else:
                        print(
                            "[voice] TTS still pending at dismiss "
                            "(fed=%s delivered=%s) — arming speech estimate"
                            % (
                                getattr(self, "_tts_fed", 0),
                                getattr(self, "_tts_delivered", 0),
                            ),
                            flush=True,
                        )
                        self._tts_spoke = True
                        self._awaiting_playback = True
                self._schedule_dismiss()
        except Exception as exc:  # noqa: BLE001
            if gen == self._session_gen:
                self._fail(str(exc))
            else:
                print(
                    "[voice] finalize error ignored (stale gen): %s" % exc,
                    flush=True,
                )
        finally:
            # Finalization overlaps a newer barge-in by design. Shared capture
            # state and the lock now belong to that newer generation, so an
            # older thread must not clear PCM, mark its socket inactive, or
            # release the newer turn's lock.
            if gen == self._session_gen:
                self._socket_session_active = False
                self._pcm_chunks = []
                if gen > self._app_handoff_gen and not self.luna.app_handoff_active():
                    self._cancel_lg_voice_ui()
                if (
                    self.capture_mode == CAPTURE_MODE_SELF
                    and gen > self._app_handoff_gen
                    and not self.luna.app_handoff_active()
                ):
                    self._schedule_renderer_kill_linger()
                # Lock is normally released as soon as capture ends. This only
                # covers exceptions before that release.
                if self._session_lock.locked():
                    try:
                        self._session_lock.release()
                    except RuntimeError:
                        pass

    def _capture_audio_alsa(self) -> None:
        """Read PCM from the remote mic and stop on sustained silence.

        There is no hidraw activity signal on this TV, so we detect end-of-speech
        from the audio level itself: once we have heard speech and then stay below
        the silence threshold for idle_release seconds, we finish the session.
        """
        deadline = time.time() + self.max_capture_sec
        last_loud = time.time()
        heard_speech = False
        stt_started = self.capture_mode != CAPTURE_MODE_SELF
        total = 0
        peak = 0
        self._pcm_chunks = []
        # Use same speech gate as socket path (idle_release alone was 1.5s of
        # noise-floor chatter → 30s+ "Listening" with no finalize).
        speech_rms = int(self.speech_detect_rms)
        silence_need = max(0.9, min(1.4, float(self.end_silence_sec)))
        print("[voice] alsa capture start dev=%s" % self.capture_device, flush=True)
        try:
            while self._active:
                chunk = self._capture.read()
                if not chunk:
                    if isinstance(self._capture, SocketUnixCapture):
                        if self._capture._sock is None:
                            break
                        continue
                    if isinstance(self._capture, OneshotAlsaCapture):
                        if self._capture._proc is not None and self._capture._proc.poll() is not None:
                            print(
                                "[voice] alsa capture: arecord exited (code=%s)"
                                % self._capture._proc.returncode,
                                flush=True,
                            )
                            break
                        continue
                    print("[voice] alsa capture: stream ended (arecord exited?)", flush=True)
                    break
                total += len(chunk)
                self._pcm_chunks.append(chunk)
                try:
                    level = audioop.rms(chunk, 2)
                except Exception:
                    level = 0
                if not stt_started and level >= speech_rms:
                    stt_started = True
                    print(
                        "[voice] hot audio detected — buffering for batch STT",
                        flush=True,
                    )
                if level > peak:
                    peak = level
                now = time.time()
                if level >= speech_rms:
                    heard_speech = True
                    last_loud = now
                    self._button.notify_voice_activity()
                if now >= deadline:
                    break
                # Button release: stop Listening quickly (short tail only).
                if self._release_requested:
                    if heard_speech and (now - last_loud) >= 0.28:
                        break
                    if (now - (deadline - self.max_capture_sec)) >= 0.9:
                        break
                # End after real speech + short silence (not room-noise forever).
                if heard_speech and (now - last_loud) >= silence_need:
                    break
                # No real speech after grace → stop (don't record 30s of hiss).
                if (
                    not heard_speech
                    and not self._release_requested
                    and (now - (deadline - self.max_capture_sec)) >= self.speech_grace_sec
                ):
                    break
        finally:
            print(
                "[voice] alsa capture done bytes=%d peak=%d heard_speech=%s"
                % (total, peak, heard_speech),
                flush=True,
            )
            # Kill remote mic stream so LED goes off (same as socket finalize).
            try:
                with self._mic_stream_lock():
                    self._stop_native_streaming()
                    self._stream_socket_path = None
                print("[voice] mic stopped after alsa capture", flush=True)
            except Exception:
                pass
            self._last_session_end_ts = time.time()
            try:
                self.luna.status("Got it — thinking…")
            except Exception:
                pass
            # Prefer batch finalize (same as socket path) when we have PCM.
            if self._active and self._pcm_chunks and not self.stt_live_stream:
                gen = self._session_gen
                self._active = False
                self.luna.capture_active = False
                threading.Thread(
                    target=self._finalize_socket_session,
                    kwargs={
                        "stt_flushed": False,
                        "heard_speech": heard_speech,
                        "peak": peak,
                        "total_bytes": total,
                        "session_gen": gen,
                        "stt_errors": False,
                    },
                    daemon=True,
                ).start()
            elif self._active:
                threading.Thread(target=self._end_capture, daemon=True).start()

    def _capture_audio(self) -> None:
        self._stream.start()
        try:
            for frame in self._stream.iter_msbc_frames():
                if not self._active:
                    break
                try:
                    self._decoder.feed(frame)
                except Exception:
                    break
                for chunk in self._decoder.iter_pcm_chunks(block=False):
                    if chunk:
                        self._stt.send_pcm(chunk)
        finally:
            self._stream.stop()

    def _pump_stt_partials(self) -> None:
        gen = self._session_gen

        def _on_partial(text: str, is_final: bool) -> None:
            t = (text or "").strip()
            if not t:
                return
            show = self._live_best_update(t, is_final=is_final, gen=gen)
            if self._try_early_silent_command(
                show or t, gen=gen, is_final=is_final
            ):
                return
            if self._looks_like_silent_command_prefix(show or t):
                return
            self._maybe_show_voice_ui_for_text(show or t)
            try:
                self.luna.transcript_partial(show or t, is_final=is_final)
            except Exception:
                pass

        while self._active:
            try:
                self._stt.poll_events(_on_partial)
            except Exception:
                break
            time.sleep(0.02)

    def _end_capture(self) -> None:
        if not self._active:
            return
        # Only socket-thread sessions own silence endpointing. ALSA fallback
        # under self_stream MUST finalize here — logs showed 32s ALSA capture
        # then "_end_capture ignored" forever (Listening stuck, no answer).
        if self._socket_session_active:
            self._release_requested = True
            print(
                "[voice] _end_capture ignored (socket session owns finalize)",
                flush=True,
            )
            return
        gen = self._session_gen
        self._active = False
        self.luna.capture_active = False
        if self.capture_mode == CAPTURE_MODE_SELF:
            # Keep warm stream; do not stopStreaming after ALSA fallback turn.
            self._cancel_lg_voice_ui()
        if self.capture_mode in (CAPTURE_MODE_PHASED, CAPTURE_MODE_SELF):
            # voiceagent Listening bar often appears 2–6s after press.
            self._schedule_renderer_kill_linger()
        else:
            self._stop_renderer_killer()
        if self.use_alsa_capture:
            if getattr(self, "_capture", None) is not None:
                self._capture.disarm()
                if self.capture_mode in (CAPTURE_MODE_PHASED, CAPTURE_MODE_SELF):
                    self._capture.stop()
                    self._capture = None
        else:
            self._stream.stop()
        # Free the lock once capture ends (same as socket finalize path).
        if self._session_lock.locked():
            try:
                self._session_lock.release()
                print(
                    "[voice] session lock released after alsa capture (gen=%d)"
                    % gen,
                    flush=True,
                )
            except RuntimeError:
                pass
        if gen != self._session_gen or self._session_cancel.is_set():
            print(
                "[voice] alsa finalize aborted (stale gen=%d current=%d)"
                % (gen, self._session_gen),
                flush=True,
            )
            return
        for _ in range(100):
            if hasattr(self, "_stt"):
                break
            time.sleep(0.02)
        try:
            if not self.use_alsa_capture and hasattr(self, "_decoder"):
                self._decoder.close()
            transcript = ""
            if hasattr(self, "_stt"):
                transcript = self._stt.finish()
            if not transcript:
                if gen == self._session_gen:
                    last_err = config_store.load_last_xai_error()
                    if last_err and last_err.get("message"):
                        self._fail(str(last_err["message"]))
                    else:
                        self._fail("No speech detected — hold KEY_VOICE while speaking")
                return
            if gen != self._session_gen or self._session_cancel.is_set():
                return
            transcript = self.luna.transcript_final(transcript)
            # Same continuous overlay as socket path — no close/relaunch flash.
            threading.Thread(target=self._cancel_lg_voice_ui, daemon=True).start()
            if self.luna.ws_client_count() <= 0:
                if not self.luna.ensure_overlay_connected(timeout=1.2):
                    print(
                        "[voice] WARNING: answering with no overlay WS",
                        flush=True,
                    )
            self._overlay_ready_for_audio.set()
            answer_kind = self._answer_and_speak(transcript)
            if gen != self._session_gen:
                print(
                    "[voice] alsa answer discarded (stale gen=%d)" % gen,
                    flush=True,
                )
                return
            if self._session_cancel.is_set() or answer_kind == "cancelled":
                self._schedule_dismiss(short=True)
                return
            if answer_kind == "app_launch":
                pass  # overlay already force-closed
            elif answer_kind in ("power_off", "screen_off"):
                self._schedule_dismiss(short=True)
            else:
                self._schedule_dismiss()
        except Exception as exc:
            if gen == self._session_gen:
                self._fail(str(exc))
        finally:
            if self._session_lock.locked():
                try:
                    self._session_lock.release()
                except RuntimeError:
                    pass

    def _foreground_app_id(self) -> str:
        """Best-effort current foreground app id (for seamless handoff)."""
        try:
            from luna_service import _luna_send  # type: ignore

            fg = _luna_send(
                "luna://com.webos.applicationManager/getForegroundAppInfo",
                {},
                timeout=1.2,
            )
            return str(
                fg.get("appId") or fg.get("foregroundAppId") or fg.get("id") or ""
            )
        except Exception:
            return ""

    @staticmethod
    def _native_app_log_confirmed(app_id: str, since_ts: float) -> bool:
        if app_id not in ("amazon", "amazon.html"):
            return False
        try:
            with open("/var/log/messages", "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(0, 2)
                end = handle.tell()
                handle.seek(max(0, end - 131072))
                recent = handle.read()
        except OSError:
            return False
        lifecycle_markers = (
            'SAM NL_APP_LAUNCH_BEGIN {"app_id":"amazon"',
            'SAM NL_APP_LAUNCH_BEGIN {"app_id":"amazon.html"',
            'LSM NL_VSC {"app_id":"amazon","visible":true',
            'LSM NL_VSC {"app_id":"amazon.html","visible":true',
            '"foreground_app_id":"amazon"',
            '"foreground_app_id":"amazon.html"',
        )
        for line in reversed(recent.splitlines()):
            if not any(marker in line for marker in lifecycle_markers):
                continue
            match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)Z", line)
            if not match:
                return True
            try:
                log_ts = datetime.datetime.fromisoformat(match.group(1)).timestamp()
            except ValueError:
                return True
            return log_ts >= since_ts - 1.0
        return False

    def _execute_silent_tv_control(self, tv_result: Any) -> None:
        """Close voice card immediately after a successful silent TV command."""
        self._awaiting_playback = False
        self._tts_spoke = False
        self._cancel_dismiss()
        self._session_cancel.set()
        self._active = False
        self.luna.capture_active = False
        self._app_handoff_gen = max(self._app_handoff_gen, self._session_gen)
        try:
            self._button.force_release()
        except Exception:
            pass
        self._ignore_voice_until_ts = time.time() + 2.0
        self._session_gen += 1
        try:
            self._stop_renderer_killer()
        except Exception:
            pass
        if self._renderer_killer_linger is not None:
            try:
                self._renderer_killer_linger.cancel()
            except Exception:
                pass
            self._renderer_killer_linger = None
        try:
            self.luna.end_silent_tv_control()
        except Exception as exc:  # noqa: BLE001
            print(
                "[voice] silent TV control overlay close failed: %s" % exc,
                flush=True,
            )
            try:
                self.luna.close_overlay(force=True)
            except Exception:
                pass
        if self._session_lock.locked():
            try:
                self._session_lock.release()
            except RuntimeError:
                pass
        print(
            "[voice] %s complete — voice card stopped without confirmation"
            % (getattr(tv_result, "kind", None) or "tv_control"),
            flush=True,
        )

    # System UIs that open under our fullscreen cover and often never appear as
    # SAM "foreground" while voice card is still painted (Settings, Home).
    _SYSTEM_UI_LAUNCH_IDS = frozenset(
        {
            "com.palm.app.settings",
            "com.webos.app.setting",
            "com.webos.app.home",
        }
    )

    @staticmethod
    def _fg_matches_launch(fg: str, launched_id: str, all_ids: list[str]) -> bool:
        """True if SAM FG is the target (or a settings-family sibling id)."""
        if not fg:
            return False
        candidates = [launched_id] + [str(x) for x in (all_ids or []) if x]
        for cand in candidates:
            if not cand:
                continue
            if fg == cand or cand in fg or fg in cand:
                return True
        # Settings launch id frequently differs from the visible settings shell.
        if any("setting" in (c or "").lower() for c in candidates):
            if "setting" in fg.lower():
                return True
        return False

    def _execute_app_launch(self, tv_result: Any) -> None:
        """Open an app then stop voice card with no confirmation UI.

        Silent like mute/volume: no "Opening…", no spoken confirm, no error
        card. Launch the target, close voice card, re-assert the target so Home
        does not linger. Trust a successful Luna launch (no FG wait) so brew
        apps like Terminal are not blocked by missing SAM foreground reports.
        """
        self._awaiting_playback = False
        self._tts_spoke = False
        self._cancel_dismiss()
        self._session_cancel.set()  # cancel any in-flight chat/TTS
        self._active = False
        self.luna.capture_active = False
        self._app_handoff_gen = max(self._app_handoff_gen, self._session_gen)
        try:
            self._button.force_release()
        except Exception:
            pass
        self._ignore_voice_until_ts = time.time() + 3.0
        # Invalidate any in-flight finalize/answer for this press.
        self._session_gen += 1
        # Stop native-UI killer so it cannot thrash focus back to us.
        try:
            self._stop_renderer_killer()
        except Exception:
            pass
        if self._renderer_killer_linger is not None:
            try:
                self._renderer_killer_linger.cancel()
            except Exception:
                pass
            self._renderer_killer_linger = None
        detail: dict[str, Any] = {}
        try:
            detail = dict(tv_result.detail or {})
        except Exception:
            detail = {}
        app_id = str(detail.get("id") or "")
        ids = list(detail.get("ids") or ([app_id] if app_id else []))
        launch_params = dict(detail.get("params") or {})
        if app_id == "org.webosbrew.hbchannel":
            ids = [app_id]
        pretty = str(detail.get("spoken") or getattr(tv_result, "spoken", None) or "app")
        our_id = str(getattr(self.luna, "overlay_app_id", "") or "")
        self_launch = bool(
            our_id
            and any(
                (c == our_id or our_id in str(c) or str(c) in our_id)
                for c in ([app_id] + list(ids))
                if c
            )
        )
        print(
            "[voice] app_launch silent — id=%s (launch then close, no UI)%s"
            % (app_id or "?", " self" if self_launch else ""),
            flush=True,
        )
        # Tell WS listeners (e.g. a home-screen launcher in the foreground)
        # so they can launch too: native Prime Video (`amazon`) often stays
        # behind a foreground web app when only the daemon luna-sends.
        # Not for our own app, which we open ourselves with special params.
        if not self_launch:
            try:
                self.luna._broadcast(
                    "appLaunch",
                    {
                        "id": app_id,
                        "ids": ids,
                        "spoken": pretty,
                        "params": launch_params,
                    },
                )
            except Exception:
                pass
        # Opening voice card itself: clear any Netflix-style handoff and show the
        # settings card. Do not arm the guardian that would kill us again.
        if self_launch:
            try:
                self.luna.clear_app_handoff()
            except Exception:
                pass
            self.luna.suppress_reclaim = False
            self._ignore_voice_until_ts = 0.0
            launch_params = dict(launch_params)
            # Settings UI (not voice overlay) when user says "open voice card".
            launch_params.setdefault("mode", "settings")
            try:
                r = tv_control.launch_app_id(
                    our_id or app_id,
                    spoken_name=pretty or "voice card",
                    params=launch_params,
                )
                print(
                    "[voice] self-launch %s: %s"
                    % (our_id or app_id, getattr(r, "ok", None)),
                    flush=True,
                )
            except Exception as rexc:  # noqa: BLE001
                print("[voice] self-launch failed: %s" % rexc, flush=True)
            if self._session_lock.locked():
                try:
                    self._session_lock.release()
                except RuntimeError:
                    pass
            return
        # Soft handoff only — no status paint; overlay is a brief cover.
        try:
            self.luna.prepare_app_handoff(300.0)
        except Exception:
            try:
                self.luna.suppress_reclaim = True
            except Exception:
                pass

        # Settings / Quick Settings are system overlays, not card FG apps.
        system_overlay = bool(detail.get("system_overlay")) or (
            app_id == "com.palm.app.settings"
            or app_id == "com.webos.app.quicksettings"
            or any(
                str(c) in ("com.palm.app.settings", "com.webos.app.quicksettings")
                for c in ids
                if c
            )
        )
        open_via = str(detail.get("open_via") or "")
        # Default noSplash=false so card apps (BBC iPlayer, Netflix, …) take
        # visible FG when a launcher web app is underneath. Explicit detail wins.
        no_splash = False
        if "no_splash" in detail:
            no_splash = bool(detail.get("no_splash"))
        elif system_overlay:
            no_splash = False

        # Soft-close voice card *before* launching card apps so a launcher is
        # not covering a noSplash-started process (looks like "nothing happened").
        if not system_overlay and app_id:
            try:
                self.luna.end_session_for_app_launch(soft=True)
            except Exception:
                try:
                    self.luna.close_overlay(force=True)
                except Exception:
                    pass
            time.sleep(0.2)

        # ---- Quick Settings (gear): close voice card, then QMENU key ----
        if system_overlay and open_via == "qmenu":
            print(
                "[voice] settings via QMENU — soft-close voice card then gear panel",
                flush=True,
            )
            try:
                self.luna.end_session_for_app_launch(soft=True)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] end_session before QMENU failed: %s" % exc,
                    flush=True,
                )
                try:
                    self.luna.begin_app_handoff(300.0)
                    self.luna.close_overlay(force=True)
                except Exception:
                    pass
            time.sleep(0.25)
            try:
                r = tv_control.open_quick_settings()
                launched = bool(r.ok)
                print(
                    "[voice] open_quick_settings ok=%s via=%s"
                    % (r.ok, (r.detail or {}).get("via")),
                    flush=True,
                )
            except Exception as rexc:  # noqa: BLE001
                print("[voice] open_quick_settings failed: %s" % rexc, flush=True)
                launched = False
            # One delayed re-press if first QMENU was ignored (voice card still up).
            if launched:
                def _qmenu_retry() -> None:
                    time.sleep(0.6)
                    try:
                        self.luna.close_overlay(force=True)
                    except Exception:
                        pass
                    try:
                        tv_control.open_quick_settings()
                    except Exception:
                        pass

                threading.Thread(target=_qmenu_retry, daemon=True).start()
            if self._session_lock.locked():
                try:
                    self._session_lock.release()
                except RuntimeError:
                    pass
            print(
                "[voice] app_launch complete — QMENU handoff active=%s"
                % self.luna.app_handoff_active(),
                flush=True,
            )
            try:
                old_stt = getattr(self, "_stt", None)
                self._stt = None
                self._stt_streamed = False
                if old_stt is not None:
                    try:
                        old_stt.close()
                    except Exception:
                        pass
                self._schedule_stt_warm(reason="after-app-launch", delay_sec=0.8)
            except Exception:
                pass
            return

        # ---- Full Settings app overlay ----
        if system_overlay:
            print(
                "[voice] system_overlay handoff — soft-close voice card first, "
                "then launch id=%s params=%s noSplash=%s"
                % (app_id or "?", launch_params, no_splash),
                flush=True,
            )
            try:
                self.luna.end_session_for_app_launch(soft=True)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] end_session before settings failed: %s" % exc,
                    flush=True,
                )
                try:
                    self.luna.begin_app_handoff(300.0)
                    self.luna.close_overlay(force=True)
                except Exception:
                    pass
            time.sleep(0.35)

        launched = False
        launched_id = ""
        for cand in ids[:3]:
            if not cand or cand == "com.webos.app.quicksettings":
                # Not a SAM-launchable id.
                continue
            try:
                r = tv_control.launch_app_id(
                    cand,
                    spoken_name=pretty,
                    params=launch_params,
                    no_splash=no_splash,
                )
                if r.ok:
                    launched = True
                    launched_id = cand
                    print("[voice] app_launch ok id=%s" % cand, flush=True)
                    break
            except Exception as rexc:  # noqa: BLE001
                print(
                    "[voice] app_launch failed id=%s: %s" % (cand, rexc),
                    flush=True,
                )
        if not launched:
            # Silent fail — do not paint an error card over the previous app.
            print(
                "[voice] app_launch all ids failed: %s" % ids[:5],
                flush=True,
            )
            try:
                self.luna.clear_app_handoff()
            except Exception:
                pass
            try:
                self.luna.end_silent_tv_control()
            except Exception:
                try:
                    self.luna.close_overlay(force=True)
                except Exception:
                    pass
            if self._session_lock.locked():
                try:
                    self._session_lock.release()
                except RuntimeError:
                    pass
            return

        if not system_overlay:
            # Card apps (Netflix/Prime/…): tear down voice card after launch.
            try:
                self.luna.end_session_for_app_launch()
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] end_session_for_app_launch failed: %s" % exc,
                    flush=True,
                )
                try:
                    self.luna.begin_app_handoff(300.0)
                    self.luna.close_overlay(force=True)
                except Exception:
                    pass

        # One light re-assert only (was up to 6 launches). Known card apps
        # (Netflix etc.) usually stay FG after the first launch; extra launches
        # only added latency and focus thrash.
        reassert_id = str(launched_id or (ids[0] if ids else "") or "")
        if reassert_id and reassert_id != "com.webos.app.quicksettings":
            reassert_no_splash = no_splash
            reassert_params = dict(launch_params)
            is_system = system_overlay

            def _re_foreground() -> None:
                # Two staggered re-launches: a launcher / compositor can steal
                # FG once after a voice-driven app open (BBC looked like a no-op).
                for delay in (0.25 if is_system else 0.15, 0.85):
                    time.sleep(delay)
                    try:
                        self.luna.close_overlay(force=True)
                    except Exception:
                        pass
                    try:
                        tv_control.launch_app_id(
                            reassert_id,
                            spoken_name=pretty,
                            params=reassert_params,
                            no_splash=False,
                        )
                    except Exception:
                        pass

            threading.Thread(target=_re_foreground, daemon=True).start()
        # Free session lock so the daemon is fully idle until next KEY_VOICE.
        if self._session_lock.locked():
            try:
                self._session_lock.release()
            except RuntimeError:
                pass
        print(
            "[voice] app_launch complete — voice card stopped (handoff active=%s "
            "system_overlay=%s)"
            % (self.luna.app_handoff_active(), system_overlay),
            flush=True,
        )
        # Drop any dead warm STT from the pre-launch press and re-warm so the
        # next KEY_VOICE does not stream into a stale socket.
        try:
            old_stt = getattr(self, "_stt", None)
            self._stt = None
            self._stt_streamed = False
            if old_stt is not None:
                try:
                    old_stt.close()
                except Exception:
                    pass
            self._schedule_stt_warm(reason="after-app-launch", delay_sec=0.8)
        except Exception:
            pass

    def _answer_and_speak(self, transcript: str) -> str:
        """Route transcript → TV / weather / chat, speak when useful.

        Returns a kind tag for dismiss policy: ``power_off``, ``screen_off``,
        ``tv``, ``weather``, ``chat``, etc.

        When TTS is enabled (chat path), each completed sentence is synthesized
        and pushed to the overlay as its own audio chunk while later sentences
        are still being generated.
        """
        if self._session_cancel.is_set():
            return "cancelled"
        if self.luna.app_handoff_active():
            print(
                "[voice] _answer_and_speak skipped (app handoff)",
                flush=True,
            )
            return "app_launch"
        tts_on = self._tts_available()
        # Reset per-session speaking-close state up front.
        self._tts_spoke = False
        self._awaiting_playback = tts_on
        if (
            tts_on
            and self.ai_provider == "openrouter"
            and not self.api_key
        ):
            self.luna.status("TTS needs xAI key (spoken replies use Grok voices)")

        def on_token(token: str) -> None:
            if self._session_cancel.is_set():
                return
            self.luna.answer_partial(token)

        def _show_local(
            answer: str,
            source: str,
            *,
            citations: Optional[list] = None,
            speak: bool = True,
        ) -> str:
            answer = (answer or "").strip()
            # Paint text first so the answer is on screen immediately; TTS
            # synthesis runs in parallel (no longer blocks the first paint).
            self.luna.answer_source(source, citations or [])
            self.luna.answer_partial(answer)
            self.luna.answer_final(answer)
            if speak and tts_on:
                self.luna.tts_will_speak(True)
                spoken_answer = strip_for_speech(answer)
                self._awaiting_playback = True

                def _finish_local_tts() -> None:
                    try:
                        # Don't synthesize into a missing card (silent no-op).
                        try:
                            self.luna.ensure_overlay_connected(timeout=0.8)
                        except Exception:
                            pass
                        # One continuous clip — multi-chunk local TTS stuttered
                        # on webOS between data: URI elements.
                        t_tts = time.time()
                        audio = synthesize_speech(
                            self.api_key,
                            spoken_answer,
                            voice_id=self.tts_voice,
                            language=self.stt_language,
                            speed=self.tts_speed,
                        )
                        if not audio:
                            self._awaiting_playback = False
                            self.luna.tts_complete(0)
                            return
                        self._tts_seq = 1
                        self._tts_spoke = True
                        self._tts_audio_bytes = len(audio)
                        print(
                            "[voice] local TTS single-shot +%.0fms "
                            "chars=%d bytes=%d"
                            % (
                                (time.time() - t_tts) * 1000.0,
                                len(spoken_answer),
                                len(audio),
                            ),
                            flush=True,
                        )
                        self.luna.answer_audio(
                            base64.b64encode(audio).decode("ascii"),
                            "audio/mpeg",
                            1.0,
                            seq=1,
                            text=spoken_answer,
                        )
                        self.luna.tts_complete(1)
                        try:
                            self._rearm_dismiss_for_speech()
                        except Exception:
                            pass
                    except Exception as exc:  # noqa: BLE001
                        print("[voice] local TTS failed: %s" % exc, flush=True)
                        self._awaiting_playback = False
                        self.luna.tts_complete(0)

                threading.Thread(target=_finish_local_tts, daemon=True).start()
            else:
                self._awaiting_playback = False
                self._tts_spoke = False
            return answer

        arithmetic = self._simple_arithmetic_answer(transcript)
        if arithmetic:
            print(
                "[voice] answer final [arithmetic]: %r" % arithmetic,
                flush=True,
            )
            _show_local(arithmetic, "grok")
            return "arithmetic"

        local_time = self._local_time_answer(transcript)
        if local_time:
            print(
                "[voice] answer final [time]: %r" % local_time,
                flush=True,
            )
            _show_local(local_time, "grok")
            return "time"

        identity = self._local_identity_answer(transcript)
        if identity:
            print(
                "[voice] answer final [identity]: %r" % identity,
                flush=True,
            )
            _show_local(identity, "grok")
            return "identity"

        joke = self._local_joke_answer(transcript)
        if joke:
            print(
                "[voice] answer final [joke]: %r" % joke,
                flush=True,
            )
            _show_local(joke, "grok")
            return "joke"

        definition = self._local_definition_answer(transcript)
        if definition:
            print(
                "[voice] answer final [definition]: %r" % definition,
                flush=True,
            )
            _show_local(definition, "grok")
            return "definition"

        # Phase 2 (README.ai.md §2): confident TV-control commands run as local
        # Luna actions — never guessed; unmatched utterances fall through.
        tv_result = None
        try:
            tv_result = tv_control.handle_tv_command(transcript)
        except Exception as exc:  # noqa: BLE001
            print("[voice] tv_control failed: %s" % exc, flush=True)
            tv_result = None
        if tv_result is not None:
            spoken = tv_result.spoken or "OK."
            kind = tv_result.kind or "tv"
            if not tv_result.ok:
                print(
                    "[voice] tv_control %s failed: %s"
                    % (kind, tv_result.detail),
                    flush=True,
                )
            else:
                print("[voice] tv_control %s ok" % kind, flush=True)
            print(
                "[voice] answer final [tv_control]: %r" % spoken[:200],
                flush=True,
            )
            # Power/screen off: show confirm and return immediately — no TTS
            # synthesis (TV is shutting down; waiting blocks on a dying panel).
            skip_tts = kind in ("power_off", "screen_off")
            # App launch should already be handled silently in finalize; keep a
            # safety net here with zero TTS / minimal UI.
            if kind == "app_launch":
                self._execute_app_launch(tv_result)
                return kind
            _show_local(spoken, "tv", speak=not skip_tts)
            return kind

        # Weather questions are answered from the free Open-Meteo API (no key /
        # no signup) and labelled as an internet source, since the LLM has no
        # live forecast data.
        weather_text = None
        try:
            if weather.is_weather_query(transcript):
                # Keep the same card on screen — never close for weather.
                try:
                    self.luna.status("Looking up weather…")
                except Exception:
                    pass
                # LG voice.performer often opens Google in the system browser
                # for weather phrasing — kill it without dismissing voice card.
                try:
                    self.luna._kill_lg_voice_browsers()
                except Exception:
                    pass
                try:
                    self.luna.status("Checking the forecast…")
                except Exception:
                    pass
            weather_text = weather.weather_answer(transcript)
        except Exception as exc:
            print("[voice] weather lookup failed: %s" % exc, flush=True)
        if weather_text:
            try:
                self.luna._kill_lg_voice_browsers()
            except Exception:
                pass
            try:
                self.luna.status("Got it…")
            except Exception:
                pass
            print(
                "[voice] answer final [weather]: %r" % weather_text[:200], flush=True
            )
            _show_local(
                weather_text.strip(), "internet", citations=["open-meteo.com"]
            )
            return "weather"

        if (
            self.tts_enabled
            and self.ai_provider == "openrouter"
            and not self.api_key
        ):
            print(
                "[voice] TTS skipped on openrouter path (needs xAI key for voices)",
                flush=True,
            )

        chat_t0 = time.time()
        first_token_ms: list[float] = []
        writing_status_sent = {"n": 0}

        def on_token_timed(token: str) -> None:
            if not first_token_ms:
                first_token_ms.append((time.time() - chat_t0) * 1000.0)
                print(
                    "[voice] answer first paint +%.0fms"
                    % first_token_ms[0],
                    flush=True,
                )
                if writing_status_sent["n"] == 0:
                    writing_status_sent["n"] = 1
                    try:
                        self.luna.status("Writing answer…")
                    except Exception:
                        pass
            on_token(token)

        try:
            if self.ai_provider == "gemini":
                ask_label = "Asking Gemini…"
            elif self.ai_provider == "openrouter":
                ask_label = "Asking OpenRouter…"
            else:
                ask_label = "Asking Grok…"
            self.luna.status(ask_label)
        except Exception:
            pass

        if self.ai_provider in ("gemini", "openrouter"):
            if self.ai_provider == "openrouter":
                result = stream_openrouter_chat(
                    self.openrouter_api_key,
                    transcript,
                    model=self.openrouter_model,
                    on_token=on_token_timed,
                )
                src_label = "openrouter"
                model_name = self.openrouter_model
            else:
                result = stream_gemini_chat(
                    self.gemini_api_key,
                    transcript,
                    model=self.gemini_model,
                    on_token=on_token_timed,
                )
                src_label = "gemini"
                model_name = self.gemini_model
            answer = result.text
            if not answer:
                answer = "(No response)"
            print(
                "[voice] answer final [%s, model=%s]: %r"
                % (src_label, model_name, answer[:200]),
                flush=True,
            )
            self.luna.answer_source(src_label, [])
            self.luna.answer_final(answer)
            if tts_on:
                self.luna.tts_will_speak(True)
                spoken_answer = strip_for_speech(answer)
                self._awaiting_playback = True

                def _speak_gemini_answer() -> None:
                    try:
                        audio = b""
                        mime = "audio/mpeg"
                        if self.ai_provider == "gemini" and self.gemini_api_key:
                            audio = synthesize_gemini_speech(
                                self.gemini_api_key,
                                spoken_answer,
                            )
                        if not audio and self.api_key:
                            audio = synthesize_speech(
                                self.api_key,
                                spoken_answer,
                                voice_id=self.tts_voice,
                                language=self.stt_language,
                                speed=self.tts_speed,
                            )
                        if not audio:
                            self._awaiting_playback = False
                            self.luna.tts_complete(0)
                            return
                        if audio[:4] == b"RIFF":
                            mime = "audio/wav"
                        self._tts_seq = 1
                        self._tts_spoke = True
                        self._tts_audio_bytes = len(audio)
                        self.luna.answer_audio(
                            base64.b64encode(audio).decode("ascii"),
                            mime,
                            1.0,
                            seq=1,
                            text=answer,
                        )
                        self.luna.tts_complete(1)
                    except Exception as exc:  # noqa: BLE001
                        print(
                            "[voice] gemini TTS failed: %s" % exc, flush=True
                        )
                        self._awaiting_playback = False
                        self.luna.tts_complete(0)

                threading.Thread(
                    target=_speak_gemini_answer,
                    daemon=True,
                    name="gemini-tts",
                ).start()
            return "chat"

        # xAI spoken answers: chat (fast text) + streaming MP3 TTS (reliable on
        # webOS). Voice realtime (think-fast-2.0) is used when live web search
        # is needed — and its PCM is converted to MP3 for playback.
        # Plain chat when TTS is off.
        want_search = _search_enabled(self.web_search, self.web_search_mode) and (
            self.web_search_mode == "always"
            or _query_needs_web_search(transcript or "")
        )
        voice_model = getattr(self, "voice_model", None) or DEFAULT_VOICE_MODEL
        voice_ok = False
        answer = ""
        used_search = False
        citations: list = []

        # Ensure the overlay is live before we push any audio (otherwise
        # answerAudio is broadcast to 0 clients and the user hears nothing).
        if tts_on:
            try:
                self.luna.ensure_overlay_connected(timeout=2.0)
            except Exception:
                pass

        if tts_on and want_search:
            self.luna.tts_will_speak(True)
            self._awaiting_playback = True
            self._tts_seq = 0
            self._tts_spoke = False
            self._tts_audio_bytes = 0
            voice_audio_ready = {"n": 0}

            def on_voice_audio(payload: bytes, seq: int) -> None:
                if self._session_cancel.is_set() or not payload:
                    return
                # Detect container: webOS plays MP3 data: URIs; WAV is often silent.
                if payload[:4] == b"RIFF":
                    mime = "audio/wav"
                elif payload[:3] == b"ID3" or (
                    len(payload) >= 2
                    and payload[0] == 0xFF
                    and (payload[1] & 0xE0) == 0xE0
                ):
                    mime = "audio/mpeg"
                else:
                    mime = "audio/mpeg"
                self._tts_seq = seq
                self._tts_spoke = True
                self._tts_audio_bytes = getattr(self, "_tts_audio_bytes", 0) + len(
                    payload
                )
                if voice_audio_ready["n"] == 0:
                    voice_audio_ready["n"] = 1
                    try:
                        self.luna.ensure_overlay_connected(timeout=1.0)
                    except Exception:
                        pass
                self.luna.answer_audio(
                    base64.b64encode(payload).decode("ascii"),
                    mime,
                    1.0,
                    seq=seq,
                    text="",
                )

            try:
                print(
                    "[voice] voice realtime start model=%s voice=%s search=%s q=%r"
                    % (
                        voice_model,
                        self.tts_voice,
                        want_search,
                        (transcript or "")[:50],
                    ),
                    flush=True,
                )
                voice_result = stream_voice_answer(
                    self.api_key,
                    transcript,
                    model=voice_model,
                    voice_id=self.tts_voice,
                    speed=self.tts_speed,
                    language=self.stt_language,
                    web_search=True,
                    search_mode="always",
                    on_token=on_token_timed,
                    on_audio=on_voice_audio,
                    on_search_start=lambda: self.luna.status(
                        "Searching the web…"
                    ),
                    should_stop=lambda: self._session_cancel.is_set(),
                )
                answer = (voice_result.text or "").strip()
                used_search = bool(voice_result.used_search)
                citations = list(voice_result.citations or [])
                if answer or voice_result.audio_chunks > 0:
                    voice_ok = True
                    self.luna.tts_complete(
                        int(voice_result.audio_chunks or 0)
                    )
                    if voice_result.audio_chunks <= 0:
                        self._awaiting_playback = False
                    print(
                        "[voice] voice realtime done chunks=%d chars=%d"
                        % (
                            int(voice_result.audio_chunks or 0),
                            len(answer),
                        ),
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] voice realtime failed: %s — chat+streaming TTS"
                    % exc,
                    flush=True,
                )
                voice_ok = False
                self._awaiting_playback = tts_on

        if not voice_ok:
            # Fallback: fast chat stream; if TTS is on, synthesize *while*
            # tokens arrive (first sentence speaks early) instead of waiting
            # for the full answer then one unary TTS call.
            chat_model = (self.chat_model or "grok-4.6").strip()
            if chat_model not in ("grok-4.6", "grok-4.5"):
                chat_model = "grok-4.6"
            print(
                "[voice] chat start model=%s q=%r tts=%s"
                % (chat_model, (transcript or "")[:60], tts_on),
                flush=True,
            )
            use_stream_tts = bool(tts_on)
            if use_stream_tts:
                self.luna.tts_will_speak(True)
                self._awaiting_playback = True
                # Paint tokens live. Do NOT mid-stream multi-clip TTS — webOS
                # gaps between data: URI <audio> elements sound like stutter.
                # One continuous MP3 after the full answer (see below).

            def on_token_and_maybe_tts(token: str) -> None:
                on_token_timed(token)

            result = stream_chat(
                self.api_key,
                transcript,
                model=chat_model,
                on_token=on_token_and_maybe_tts,
                on_search_start=lambda: self.luna.status("Searching the web…"),
                web_search=self.web_search,
                search_mode=self.web_search_mode,
                max_search_results=self.web_search_max_results,
                search_workers=self.web_search_workers,
                search_timeout_s=min(2.5, float(self.web_search_timeout_s or 2.5)),
            )
            answer = result.text
            used_search = bool(result.used_search)
            citations = list(result.citations or [])
            if use_stream_tts:
                spoken_answer = strip_for_speech(answer or "")
                # Synthesize on this thread immediately (no extra hop) so
                # speech starts ~1s after the last chat token, not later.
                try:
                    try:
                        self.luna.ensure_overlay_connected(timeout=0.8)
                    except Exception:
                        pass
                    if not spoken_answer or spoken_answer == "(No response)":
                        self._awaiting_playback = False
                        self.luna.tts_complete(0)
                    else:
                        t_tts = time.time()
                        audio = synthesize_speech(
                            self.api_key,
                            spoken_answer,
                            voice_id=self.tts_voice,
                            language=self.stt_language,
                            speed=self.tts_speed,
                        )
                        if not audio:
                            self._awaiting_playback = False
                            self.luna.tts_complete(0)
                        else:
                            self._tts_seq = 1
                            self._tts_spoke = True
                            self._tts_audio_bytes = len(audio)
                            print(
                                "[voice] chat TTS single-shot +%.0fms "
                                "chars=%d bytes=%d"
                                % (
                                    (time.time() - t_tts) * 1000.0,
                                    len(spoken_answer),
                                    len(audio),
                                ),
                                flush=True,
                            )
                            self.luna.answer_audio(
                                base64.b64encode(audio).decode("ascii"),
                                "audio/mpeg",
                                1.0,
                                seq=1,
                                text=spoken_answer,
                            )
                            self.luna.tts_complete(1)
                            try:
                                self._rearm_dismiss_for_speech()
                            except Exception:
                                pass
                except Exception as tts_exc:  # noqa: BLE001
                    print(
                        "[voice] chat TTS failed: %s" % tts_exc,
                        flush=True,
                    )
                    self._awaiting_playback = False
                    self.luna.tts_complete(0)

        if not answer:
            answer = "(No response)"
        source = "internet" if used_search else "grok"
        print(
            "[voice] answer final [%s, voice=%s, %d citation(s)]: %r"
            % (
                source,
                voice_ok,
                len(citations),
                answer[:200],
            ),
            flush=True,
        )
        self.luna.answer_source(source, citations)
        self.luna.answer_final(answer)
        return "chat"

    @staticmethod
    def _local_time_answer(text: str) -> Optional[str]:
        normalized = re.sub(
            r"\s+",
            " ",
            (text or "").lower().strip().rstrip("?.!"),
        )
        full_patterns = {
            "what time is it",
            "what is the time",
            "what's the time",
            "tell me the time",
            "current time",
            "the time",
            "time",
        }
        stt_fragments = {
            "is it",
            "what time",
        }
        if normalized not in full_patterns and normalized not in stt_fragments:
            return None
        now = time.localtime()
        hour = int(time.strftime("%I", now))
        minute = int(time.strftime("%M", now))
        suffix = time.strftime("%p", now).lower()
        if minute == 0:
            return "It's %d %s." % (hour, suffix)
        return "It's %d:%02d %s." % (hour, minute, suffix)

    @staticmethod
    def _local_identity_answer(text: str) -> Optional[str]:
        """Instant identity answers — no chat round-trip."""
        n = re.sub(r"\s+", " ", (text or "").lower().strip().rstrip("?.!"))
        patterns = {
            "who are you",
            "who're you",
            "what are you",
            "what's your name",
            "what is your name",
            "your name",
            "what can you do",
            "what do you do",
            "hello who are you",
            "hi who are you",
        }
        if n not in patterns and not re.match(
            r"^(?:hello|hi|hey)[, ]+(?:who|what) are you$", n
        ):
            return None
        if "name" in n:
            return "I'm the Launch Home voice assistant, powered by Grok."
        if "can you do" in n or "do you do" in n:
            return (
                "I can answer questions, look up weather, control the TV, "
                "and search the web when you need live facts."
            )
        return (
            "I'm the Launch Home voice assistant on your LG TV, powered by Grok. "
            "Ask me anything."
        )

    @staticmethod
    def _local_joke_answer(text: str) -> Optional[str]:
        """Instant canned joke for bare requests only.

        Topic jokes (\"tell me a joke about watermelons\") fall through to Grok
        so the subject is actually used. Confirmed miss: full STT was kept but
        the local path returned an unrelated skeleton joke.
        """
        n = re.sub(r"\s+", " ", (text or "").lower().strip().rstrip("?.!"))
        if not re.search(
            r"\b("
            r"joke|jokes|funny|make me laugh|something funny|"
            r"tell me a joke|say something funny|humour|humor"
            r")\b",
            n,
        ):
            return None
        # Explicit topic / "about X" → cloud (must not use a canned line).
        if re.search(
            r"\b(?:about|with|involving|featuring|regarding|on the topic)\b",
            n,
        ):
            return None
        # Any leftover content after stripping joke boilerplate → cloud.
        stripped = re.sub(
            r"\b("
            r"tell|me|a|an|some|something|say|make|please|"
            r"joke|jokes|funny|laugh|humour|humor|can|you|would|"
            r"like|to|hear|one|another|do|got|have|any|know"
            r")\b",
            " ",
            n,
        )
        stripped = re.sub(r"[^a-z0-9\s]+", " ", stripped)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if stripped:
            return None
        # Short complete jokes for TTS (setup + punchline).
        jokes = (
            "Why did the TV break up with the remote? It needed some space.",
            "I told my Wi‑Fi we needed to talk. It said, sorry, you're out of range.",
            "Why don't skeletons fight each other? They don't have the guts.",
            "What do you call a fish with no eyes? Fsh.",
            "Why did the scarecrow win an award? He was outstanding in his field.",
            "I asked the librarian if the library had books on paranoia. "
            "She whispered, they're right behind you.",
            "Parallel lines have so much in common. It's a shame they'll never meet.",
            "Why can't your nose be twelve inches long? Because then it would be a foot.",
        )
        # Stable pick per day so repeats feel intentional, not random noise.
        day = int(time.strftime("%j"))
        return jokes[(day + len(n)) % len(jokes)]

    @staticmethod
    def _local_definition_answer(text: str) -> Optional[str]:
        normalized = re.sub(
            r"\s+",
            " ",
            (text or "").lower().strip().rstrip("?.!"),
        )
        normalized = re.sub(
            r"^(?:what(?:'s| is)|define|tell me (?:what|about))\s+",
            "",
            normalized,
        ).strip()
        definitions = {
            "an atom": (
                "An atom is the smallest unit of an element, made of protons "
                "and neutrons in a nucleus with electrons around it."
            ),
            "atom": (
                "An atom is the smallest unit of an element, made of protons "
                "and neutrons in a nucleus with electrons around it."
            ),
            "a molecule": (
                "A molecule is two or more atoms chemically bonded together "
                "as the smallest unit of a substance."
            ),
            "molecule": (
                "A molecule is two or more atoms chemically bonded together "
                "as the smallest unit of a substance."
            ),
        }
        return definitions.get(normalized)

    @staticmethod
    def _simple_arithmetic_answer(text: str) -> Optional[str]:
        t = (text or "").lower().strip().rstrip("?.!")
        t = re.sub(r"^(?:what(?:'s| is)|calculate|compute)\s+", "", t).strip()
        number_words = {
            "zero": 0,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
            "thirteen": 13,
            "fourteen": 14,
            "fifteen": 15,
            "sixteen": 16,
            "seventeen": 17,
            "eighteen": 18,
            "nineteen": 19,
            "twenty": 20,
        }

        def _number(value: str) -> Optional[float]:
            value = value.strip()
            if value in number_words:
                return float(number_words[value])
            try:
                return float(value)
            except ValueError:
                return None

        m = re.fullmatch(
            r"(-?\d+(?:\.\d+)?|[a-z]+)\s+"
            r"(plus|minus|times|multiplied by|divided by|over)\s+"
            r"(-?\d+(?:\.\d+)?|[a-z]+)",
            t,
        )
        if not m:
            return None
        left = _number(m.group(1))
        right = _number(m.group(3))
        if left is None or right is None:
            return None
        op = m.group(2)
        if op == "plus":
            result = left + right
        elif op == "minus":
            result = left - right
        elif op in ("times", "multiplied by"):
            result = left * right
        else:
            if right == 0:
                return "You can't divide by zero."
            result = left / right
        if result.is_integer():
            value = str(int(result))
        else:
            value = ("%.6f" % result).rstrip("0").rstrip(".")
        return "The answer is %s." % value

    def _tts_feed_answer_text(self, answer: str) -> None:
        """Enqueue a full known answer as one or few TTS jobs (not word crumbs)."""
        text = strip_for_speech(answer or "")
        if not text:
            return
        if len(text) <= TTS_SINGLE_SHOT_CHARS:
            self._tts_feed(text)
            return
        chunks, rem = split_sentences(text, soft=False)
        for c in chunks:
            if c.strip():
                self._tts_feed(c.strip())
        if (rem or "").strip():
            self._tts_feed(rem.strip())

    def _tts_begin(self) -> None:
        self._tts_buffer = ""
        self._tts_pending = ""
        self._tts_seq = 0  # feed sequence / last result key
        self._tts_fed = 0  # chunks enqueued for synthesis
        self._tts_delivered = 0  # audio chunks actually sent to overlay
        self._tts_next_send = 1  # next seq to broadcast (ordered)
        self._tts_first_chunk = True
        self._tts_audio_bytes = 0
        self._tts_queue = queue.Queue()
        self._tts_results: dict[int, tuple[bytes, str]] = {}
        self._tts_result_lock = threading.Lock()
        self._tts_threads: list[threading.Thread] = []
        n_workers = max(1, int(TTS_WORKERS))
        for _ in range(n_workers):
            t = threading.Thread(
                target=self._tts_worker_loop, args=(self._tts_queue,), daemon=True
            )
            t.start()
            self._tts_threads.append(t)
        # Keep legacy single-thread attr for any external checks.
        self._tts_thread = self._tts_threads[0] if self._tts_threads else None

    def _tts_ingest(self, token: str) -> None:
        """Buffer streamed tokens; emit only on sentence ends (no mid-phrase cuts).

        Soft mid-phrase TTS crumbs produced separate webOS <audio> clips with
        gaps between them — speech sounded stuttered and incomplete.
        """
        self._tts_buffer += token
        soft = self._tts_fed < TTS_SOFT_CHUNK_LIMIT
        chunks, self._tts_buffer = split_sentences(self._tts_buffer, soft=soft)
        for chunk in chunks:
            self._tts_pending = (
                (self._tts_pending + " " + chunk).strip()
                if self._tts_pending
                else chunk
            )
            need = (
                TTS_FIRST_CHUNK_CHARS
                if self._tts_fed == 0
                else TTS_MIN_CHUNK_CHARS
            )
            # Prefer full sentences: feed when split_sentences already cut on .!?
            if len(self._tts_pending) >= need or (
                chunk and chunk[-1:] in ".!?"
            ):
                self._tts_feed(self._tts_pending)
                self._tts_pending = ""
                self._tts_first_chunk = False
        # Only force a word cut on a very long unpunctuated run (rare).
        if self._tts_fed == 0:
            buf = (self._tts_pending + " " + self._tts_buffer).strip()
            if len(buf) >= TTS_FIRST_CHUNK_FORCE:
                cut = buf.rfind(" ", 0, TTS_FIRST_CHUNK_FORCE + 1)
                if cut < max(16, TTS_FIRST_CHUNK_CHARS // 2):
                    cut = buf.rfind(" ")
                if cut >= max(16, TTS_FIRST_CHUNK_CHARS // 2):
                    self._tts_feed(buf[:cut].strip())
                    rest = buf[cut:].strip()
                    self._tts_pending = ""
                    self._tts_buffer = rest
                    self._tts_first_chunk = False

    def _tts_flush(self) -> None:
        tail = (self._tts_pending + " " + self._tts_buffer).strip()
        if tail and tail != "(No response)":
            self._tts_feed(tail)
        self._tts_pending = ""
        self._tts_buffer = ""

    def _tts_feed(self, text: str) -> None:
        if self._tts_queue is None:
            return
        cleaned = strip_for_speech(text)
        if not cleaned:
            return
        self._tts_fed += 1
        seq = self._tts_fed
        # Log feed time so we can see overlap with answer streaming.
        print(
            "[voice] TTS feed #%d (%d chars) %r"
            % (seq, len(cleaned), cleaned[:40]),
            flush=True,
        )
        self._tts_queue.put((seq, cleaned))

    def _tts_emit_ready_locked(self) -> None:
        """Broadcast completed chunks in order (call with _tts_result_lock)."""
        while self._tts_next_send in self._tts_results:
            audio, text = self._tts_results.pop(self._tts_next_send)
            seq = self._tts_next_send
            self._tts_next_send += 1
            if not audio:
                print(
                    "[voice] TTS chunk #%d skipped (empty/failed) %r"
                    % (seq, (text or "")[:40]),
                    flush=True,
                )
                continue
            if self._tts_delivered == 0:
                ready = self._overlay_ready_for_audio.wait(timeout=0.2)
                if not ready:
                    print(
                        "[voice] overlay not ready for first TTS — playing anyway",
                        flush=True,
                    )
            self._tts_delivered += 1
            self._tts_seq = self._tts_delivered  # tts_complete uses this count
            self._tts_audio_bytes += len(audio)
            b64 = base64.b64encode(audio).decode("ascii")
            print(
                "[voice] TTS chunk #%d voice=%s speed=%.2f bytes=%d %r"
                % (
                    self._tts_delivered,
                    self.tts_voice,
                    self.tts_speed,
                    len(audio),
                    text[:40],
                ),
                flush=True,
            )
            self.luna.answer_audio(
                b64, "audio/mpeg", 1.0, self._tts_delivered, text=text
            )
            self._tts_spoke = True
            self._awaiting_playback = True
            # Re-arm on *every* chunk — arming only on the first left the
            # dismiss timer based on "A molecule is" (~20KB) and cut off the
            # rest of the answer after ~2 words.
            try:
                self._rearm_dismiss_for_speech()
            except Exception:
                pass

    def _rearm_dismiss_for_speech(self) -> None:
        """Extend overlay lifetime as more TTS audio is delivered."""
        if self._session_cancel.is_set():
            return
        if not self._tts_spoke and self._tts_audio_bytes <= 0:
            return
        print(
            "[voice] re-arm dismiss for TTS (bytes=%d chunks=%d)"
            % (self._tts_audio_bytes, self._tts_seq),
            flush=True,
        )
        self._schedule_dismiss(short=False)

    def _tts_worker_loop(self, q: "queue.Queue") -> None:
        while True:
            item = q.get()
            if item is None:
                break
            try:
                seq, text = item
            except (TypeError, ValueError):
                seq, text = 0, str(item)
            text = (text or "").strip()
            if not text:
                continue
            audio = b""
            try:
                # Speed is baked in by the TTS API (no ffmpeg needed on TV).
                audio = synthesize_speech(
                    self.api_key,
                    text,
                    voice_id=self.tts_voice,
                    language=self.stt_language,
                    speed=self.tts_speed,
                )
            except Exception as exc:  # noqa: BLE001
                print("[voice] TTS chunk failed: %s" % exc, flush=True)
                audio = b""
            with self._tts_result_lock:
                self._tts_results[seq] = (audio or b"", text)
                self._tts_emit_ready_locked()

    def _tts_end(self) -> None:
        q = self._tts_queue
        threads = list(getattr(self, "_tts_threads", None) or [])
        if not threads and self._tts_thread is not None:
            threads = [self._tts_thread]
        if q is not None:
            for _ in threads or [None]:
                q.put(None)
        for t in threads:
            t.join(timeout=20)
        # If synthesis failed entirely, do not wait forever for playbackEnded.
        if not self._tts_spoke:
            self._awaiting_playback = False
        self._tts_queue = None
        self._tts_thread = None
        self._tts_threads = []
        # Full byte count now known — extend dismiss so multi-chunk answers
        # are not cut after the first phrase.
        try:
            if self._tts_spoke:
                self._rearm_dismiss_for_speech()
        except Exception:
            pass

    def _estimate_tts_play_sec(self) -> float:
        """Rough playback length from MP3 byte count (fallback if overlay is silent).

        xAI TTS defaults to ~128 kbps MP3. Divide by speed so faster speech
        shortens the wait. Always leave a small floor for very short answers.
        """
        nbytes = int(getattr(self, "_tts_audio_bytes", 0) or 0)
        if nbytes <= 0:
            # Still synthesizing — keep the card open long enough for a
            # multi-sentence answer to start and finish.
            fed = int(getattr(self, "_tts_fed", 0) or 0)
            return max(12.0, 6.0 + fed * 4.0)
        # bits / (128000 bits/s) / speed
        speed = max(0.7, min(1.5, float(getattr(self, "tts_speed", 1.0) or 1.0)))
        sec = (nbytes * 8.0) / (128000.0 * speed)
        # Decode/queue/start latency + gap between streamed chunks + grace.
        # Multi-chunk answers often have 0.5–1.5s synthesis gaps between lines.
        chunks = max(1, int(getattr(self, "_tts_seq", 1) or 1))
        gap_pad = min(8.0, 1.2 * max(0, chunks - 1))
        return max(5.0, min(120.0, sec + 3.5 + gap_pad + self.tts_post_speech_sec))

    def _speak_answer(self, answer: str) -> None:
        """Synthesize the answer with the configured voice and push it to the
        overlay to play. Best-effort: TTS failures never break the session."""
        if not self._tts_available():
            return
        text = (answer or "").strip()
        if not text or text == "(No response)":
            return
        try:
            audio = synthesize_speech(
                self.api_key,
                text,
                voice_id=self.tts_voice,
                language=self.stt_language,
                speed=self.tts_speed,
            )
        except Exception as exc:  # noqa: BLE001
            print("[voice] TTS failed: %s" % exc, flush=True)
            return
        if not audio:
            return
        b64 = base64.b64encode(audio).decode("ascii")
        print(
            "[voice] TTS ready voice=%s speed=%.2f bytes=%d"
            % (self.tts_voice, self.tts_speed, len(audio)),
            flush=True,
        )
        self.luna.answer_audio(b64, "audio/mpeg", 1.0, text=text)

    def _fail(self, message: str) -> None:
        print(f"[voice] error: {message}", file=sys.stderr, flush=True)
        traceback.print_exc()
        self._active = False
        self.luna.capture_active = False
        # CRITICAL: keep suppress_reclaim True. Clearing it while
        # _voice_session_live was still True let the watcher
        # "reclaim foreground (ws-lost)" re-launch the overlay after empty
        # STT / failed turns — user saw voice card pop back over the TV.
        self.luna.suppress_reclaim = True
        self._socket_session_active = False
        self._awaiting_playback = False
        self._tts_spoke = False
        self.luna.capture_active = False
        try:
            self.luna.write_voice_state(False, reason="error")
        except Exception:
            pass
        if self.luna.app_handoff_active():
            print(
                "[voice] error during app handoff (no overlay): %s" % message,
                flush=True,
            )
            if self._session_lock.locked():
                try:
                    self._session_lock.release()
                except RuntimeError:
                    pass
            return
        try:
            self.luna.ensure_voice_overlay()
        except Exception:
            pass
        try:
            self.luna.error(message)
        except Exception:
            pass
        # No live card: end immediately (nothing to read). Avoid 10s of
        # live-session reclaim risk with ws=0.
        if self.luna.ws_client_count() <= 0:
            try:
                self.luna.session_ended(force_close_sec=2.0)
            except Exception:
                pass
        else:
            # Keep the error on screen long enough to read.
            self._schedule_dismiss(short=False)
        if self._session_lock.locked():
            try:
                self._session_lock.release()
            except RuntimeError:
                pass

    def _cancel_dismiss(self) -> None:
        if self._dismiss_timer:
            self._dismiss_timer.cancel()
            self._dismiss_timer = None

    def _schedule_dismiss(self, short: bool = False) -> None:
        self._cancel_dismiss()
        self._last_session_end_ts = time.time()
        # Spoken answers: prefer overlay `playbackEnded`, but never hang the
        # window if that signal is lost (historically common on this WebKit).
        # Estimate play time from the MP3 bytes we just pushed and use that as
        # the primary close timer; playbackEnded can still close earlier.
        speaking = (not short) and self._awaiting_playback and self._tts_spoke
        if speaking:
            delay = self._estimate_tts_play_sec()
            # Hard ceiling so a bad estimate cannot pin the UI for minutes.
            delay = min(delay, max(15.0, float(self.tts_close_safety_sec)))
            print(
                "[voice] dismiss armed after speech ~%.1fs (bytes=%d chunks=%d)"
                % (delay, self._tts_audio_bytes, self._tts_seq),
                flush=True,
            )
        else:
            self._awaiting_playback = False
            delay = 4.0 if short else self.auto_dismiss_sec
            print(
                "[voice] dismiss armed in %.1fs (short=%s spoke=%s)"
                % (delay, short, self._tts_spoke),
                flush=True,
            )

        def _dismiss() -> None:
            if self._awaiting_playback:
                print(
                    "[voice] dismiss timer fired (no playbackEnded) — closing",
                    flush=True,
                )
            self._awaiting_playback = False
            self._last_session_end_ts = time.time()
            # Safety margin so Luna close does not cut trailing audio.
            self.luna.session_ended(force_close_sec=max(8.0, delay * 0.25 + 6.0))

        self._dismiss_timer = threading.Timer(delay, _dismiss)
        self._dismiss_timer.daemon = True
        self._dismiss_timer.start()

    def _on_playback_ended(self) -> None:
        """Overlay reported the spoken answer finished playing -> close now."""
        # Always honour the signal even if we already cleared the flag — a late
        # report should still tear the window down promptly.
        self._awaiting_playback = False
        self._cancel_dismiss()
        print(
            "[voice] closing after playback (grace=%.1fs)"
            % self.tts_post_speech_sec,
            flush=True,
        )

        def _dismiss() -> None:
            # Overlay should already be closing; short safety only.
            self.luna.session_ended(
                force_close_sec=max(5.0, float(self.tts_post_speech_sec) + 3.0)
            )

        self._dismiss_timer = threading.Timer(self.tts_post_speech_sec, _dismiss)
        self._dismiss_timer.daemon = True
        self._dismiss_timer.start()


_LOG_MAX_BYTES = 2_500_000


def _rotate_log(path: str) -> None:
    try:
        os.replace(path, path + ".1")
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass


def _setup_file_logging(path: str = "/tmp/launch-home-voice.log") -> None:
    """Tee stdout/stderr to a file; rotate when large so /tmp (RAM) cannot fill."""
    try:
        if os.path.isfile(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
            _rotate_log(path)
        logf = open(path, "a", buffering=1)
    except OSError:
        return
    lock = threading.Lock()
    state = {"file": logf, "size": logf.tell()}

    class _Tee:
        def __init__(self, console):
            self._console = console

        def write(self, data):
            try:
                self._console.write(data)
                self._console.flush()
            except Exception:
                pass
            with lock:
                f = state["file"]
                try:
                    f.write(data)
                    state["size"] += len(data)
                except Exception:
                    return
                # The daemon runs for days: rotate while running, not only at start.
                if state["size"] > _LOG_MAX_BYTES:
                    try:
                        f.close()
                        _rotate_log(path)
                        state["file"] = open(path, "a", buffering=1)
                        state["size"] = 0
                    except OSError:
                        pass

        def flush(self):
            try:
                self._console.flush()
            except Exception:
                pass

    sys.stdout = _Tee(sys.__stdout__)
    sys.stderr = _Tee(sys.__stderr__)
    print("[voice] === daemon start %s ===" % time.strftime("%H:%M:%S"), flush=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Launch Home voice daemon")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    # Own process group, so Launch Home's run loop (voice-run.sh) can stop the
    # daemon together with its helpers (tail, luna-send, arecord).
    try:
        os.setsid()
    except OSError:
        pass
    _setup_file_logging()
    try:
        config = load_config(args.config)
        GrokVoiceDaemon(config).run()
    except Exception as exc:
        print(f"[voice] fatal: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Config read/write for the Launch Home voice daemon.

Services the settings RPCs Launch Home's AI Voice tab sends over the local
WebSocket (ws://127.0.0.1:8678), bypassing the webOS ACG private bus that
blocks non-root luna-send calls.

API keys stay in config.json on disk and are never returned in full to
clients except via explicit getApiKey-style fields used by the settings UI
for edit-in-place (still only on the TV).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

CONFIG_DIR = os.environ.get(
    "LH_VOICE_CONFIG_DIR", "/home/root/.config/launch-home-voice"
)
CONFIG_PATH = os.environ.get(
    "LH_VOICE_CONFIG", os.path.join(CONFIG_DIR, "config.json")
)
EXAMPLE_PATH = os.environ.get(
    "LH_VOICE_CONFIG_EXAMPLE", "/media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher/voice/config.example.json"
)

PLACEHOLDER_KEY = "xai-..."
PLACEHOLDER_GEMINI = "AIza..."
PLACEHOLDER_OPENROUTER = "sk-or-..."

DEFAULTS: dict[str, Any] = {
    "ai_provider": "xai",  # xai | gemini | openrouter
    "auth_mode": "API_KEY",  # API_KEY | SUPERGROK_OAUTH
    "xai_api_key": PLACEHOLDER_KEY,
    "gemini_api_key": "",
    "gemini_model": "gemini-3.8-flash",
    "gemini_stt_model": "gemini-3.5-transcribe",
    "openrouter_api_key": "",
    "openrouter_model": "openai/gpt-6-luna",
    "openrouter_stt_model": "openai/gpt-transcribe",
    "stt_language": "en",
    "chat_model": "grok-4.7",
    "voice_model": "grok-voice-think-fast-2.0",
    "overlay_auto_dismiss_sec": 10,
    "close_native_aiplatform": True,
    "hidraw_device": "/dev/hidraw0",
    "overlay_app_id": "org.webosbrew.lounge.voice",
    "ffmpeg_path": "ffmpeg",
    "button_idle_release_sec": 3.0,
    "native_aiplatform_close_poll_sec": 12,
    "tts_enabled": True,
    "tts_voice": "iris",
    "tts_speed": 1.0,
    "web_search": True,
    "capture_mode": "self_stream",
    "disable_native_triggers": True,
    "self_stream_keep_voiceconductor": True,
    "use_socket_capture": True,
    "fresh_stream_per_press": False,
}

PUBLIC_KEYS = [
    "ai_provider",
    "auth_mode",
    "stt_language",
    "chat_model",
    "voice_model",
    "gemini_model",
    "gemini_stt_model",
    "openrouter_model",
    "openrouter_stt_model",
    "overlay_auto_dismiss_sec",
    "close_native_aiplatform",
    "tts_enabled",
    "tts_voice",
    "tts_speed",
    "web_search",
]

AI_PROVIDERS = ("xai", "gemini", "openrouter")

OPENROUTER_MODELS = [
    "openai/gpt-6-luna",
    "openai/gpt-6-sol",
    "google/gemini-3.8-flash",
    "anthropic/claude-sonnet-5",
    "x-ai/grok-4.7",
]

OPENROUTER_STT_MODELS = [
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
]

# The 21 expressive xAI TTS voices (docs.x.ai text-to-speech).
TTS_VOICES = [
    "carina", "zagan", "helix", "orion", "luna", "iris", "altair",
    "zenith", "perseus", "helios", "lux", "kepler", "rigel", "cosmo",
    "celeste", "ursa", "sirius", "lumen", "castor", "naksh", "atlas",
]

GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.5-live-translate-preview",
]

GEMINI_STT_MODELS = [
    "gemini-3.5-transcribe",
    "gemini-3.8-flash",
]

CHAT_MODELS = [
    "grok-4.7",
    "grok-4.6",
]

# Speech-to-Speech models used for xAI spoken answers (docs.x.ai voice-agent).
VOICE_MODELS = [
    "grok-voice-think-fast-2.0",
    "grok-voice-think-fast-1.0",
    "grok-voice-latest",
]

# Retired model ids remapped on load so old config.json keeps working.
_GEMINI_MODEL_ALIASES = {
    "gemini-2.0-flash": "gemini-3.8-flash",
    "gemini-2.0-flash-001": "gemini-3.8-flash",
    "gemini-2.5-flash": "gemini-3.8-flash",
    "gemini-2.5-pro": "gemini-3.1-pro-preview",
    "gemini-3.5-flash": "gemini-3.8-flash",
    "gemini-3.6-flash": "gemini-3.8-flash",
    "gemini-3-pro-preview": "gemini-3.1-pro-preview",
}

# Earlier OpenRouter picks → successors (x-ai/grok-4 is gone from OpenRouter).
_OPENROUTER_MODEL_ALIASES = {
    "openai/gpt-4o-mini": "openai/gpt-6-luna",
    "openai/gpt-4o": "openai/gpt-6-sol",
    "google/gemini-3.7-flash": "google/gemini-3.8-flash",
    "anthropic/claude-sonnet-4": "anthropic/claude-sonnet-5",
    "x-ai/grok-4": "x-ai/grok-4.7",
}

_OPENROUTER_STT_MODEL_ALIASES = {
    "openai/gpt-4o-mini-transcribe": "openai/gpt-transcribe",
    "openai/gpt-4o-transcribe": "openai/gpt-transcribe",
}

_CHAT_MODEL_ALIASES = {
    "grok-4.5": "grok-4.7",
    "grok-4.3": "grok-4.7",
    "grok-4": "grok-4.7",
    "grok-4-fast": "grok-4.7",
    "grok-3": "grok-4.7",
    "grok-3-mini": "grok-4.7",
    "grok-3-fast": "grok-4.7",
    "grok-2": "grok-4.7",
    "grok-2-latest": "grok-4.7",
}

_VOICE_MODEL_ALIASES = {
    "grok-voice-2-fast": "grok-voice-think-fast-2.0",
    "grok-voice-think-fast-2": "grok-voice-think-fast-2.0",
    "grok-voice-think-fast": "grok-voice-think-fast-2.0",
}


XAI_ERROR_PATH = "/tmp/launch-home-voice-xai-error.json"


def save_last_xai_error(kind: str, message: str) -> None:
    payload = {
        "kind": kind or "error",
        "message": message or "",
        "ts": time.time(),
    }
    try:
        tmp = XAI_ERROR_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, XAI_ERROR_PATH)
    except OSError:
        pass


def clear_last_xai_error() -> None:
    try:
        if os.path.isfile(XAI_ERROR_PATH):
            os.remove(XAI_ERROR_PATH)
    except OSError:
        pass


def load_last_xai_error() -> Optional[dict[str, Any]]:
    if not os.path.isfile(XAI_ERROR_PATH):
        return None
    try:
        with open(XAI_ERROR_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not data.get("message"):
            return None
        return {
            "kind": data.get("kind") or "error",
            "message": data.get("message") or "",
            "ts": data.get("ts") or 0,
        }
    except Exception:
        return None


def _read_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_defaults() -> dict[str, Any]:
    out = dict(DEFAULTS)
    if os.path.isfile(EXAMPLE_PATH):
        try:
            out.update(_read_json(EXAMPLE_PATH))
        except Exception as exc:  # noqa: BLE001
            print("[voice] bad example config: %s" % exc, flush=True)
    return out


def load_config() -> dict[str, Any]:
    """Load config; corrupt JSON falls back to defaults (daemon stays up)."""
    if not os.path.isfile(CONFIG_PATH):
        return load_defaults()
    merged = load_defaults()
    try:
        disk = _read_json(CONFIG_PATH)
        if not isinstance(disk, dict):
            raise ValueError("config root must be an object")
        merged.update(disk)
    except Exception as exc:  # noqa: BLE001
        print(
            "[voice] ERROR: bad config.json (%s) — using defaults; "
            "fix via settings or fix %s"
            % (exc, CONFIG_PATH),
            flush=True,
        )
        return load_defaults()
    # Drop retired Gemini models (e.g. gemini-2.0-flash).
    gm = str(merged.get("gemini_model") or "").strip()
    if gm in _GEMINI_MODEL_ALIASES:
        merged["gemini_model"] = _GEMINI_MODEL_ALIASES[gm]
    elif gm and gm not in GEMINI_MODELS:
        merged["gemini_model"] = DEFAULTS["gemini_model"]
    gstt = str(merged.get("gemini_stt_model") or "").strip()
    if not gstt:
        merged["gemini_stt_model"] = DEFAULTS["gemini_stt_model"]
    # Drop retired Grok 3 models from the settings picker.
    cm = str(merged.get("chat_model") or "").strip()
    if cm in _CHAT_MODEL_ALIASES:
        merged["chat_model"] = _CHAT_MODEL_ALIASES[cm]
    elif cm and cm not in CHAT_MODELS:
        merged["chat_model"] = DEFAULTS["chat_model"]
    vm = str(merged.get("voice_model") or "").strip()
    if vm in _VOICE_MODEL_ALIASES:
        merged["voice_model"] = _VOICE_MODEL_ALIASES[vm]
    elif vm and vm not in VOICE_MODELS:
        merged["voice_model"] = DEFAULTS["voice_model"]
    elif not vm:
        merged["voice_model"] = DEFAULTS["voice_model"]
    orm = str(merged.get("openrouter_model") or "").strip()
    if orm in _OPENROUTER_MODEL_ALIASES:
        merged["openrouter_model"] = _OPENROUTER_MODEL_ALIASES[orm]
    elif not orm:
        merged["openrouter_model"] = DEFAULTS["openrouter_model"]
    orstt = str(merged.get("openrouter_stt_model") or "").strip()
    if orstt in _OPENROUTER_STT_MODEL_ALIASES:
        merged["openrouter_stt_model"] = _OPENROUTER_STT_MODEL_ALIASES[orstt]
    elif not orstt:
        merged["openrouter_stt_model"] = DEFAULTS["openrouter_stt_model"]
    prov = str(merged.get("ai_provider") or "xai").strip().lower()
    if prov not in AI_PROVIDERS:
        merged["ai_provider"] = "xai"
    merged["capture_mode"] = "self_stream"
    merged["disable_native_triggers"] = True
    merged["self_stream_keep_voiceconductor"] = True
    merged["use_socket_capture"] = True
    merged["fresh_stream_per_press"] = False
    try:
        import supergrok_auth

        merged["auth_mode"] = supergrok_auth.normalize_auth_mode(
            merged.get("auth_mode")
        )
    except Exception:
        merged["auth_mode"] = "API_KEY"
    return merged


def mask_api_key(key: Any) -> str:
    if not key or key in (PLACEHOLDER_KEY, PLACEHOLDER_GEMINI, PLACEHOLDER_OPENROUTER):
        return ""
    key = str(key)
    if len(key) <= 8:
        return "\u2022" * 8
    return key[:4] + ("\u2022" * 8) + key[-4:]


def is_placeholder_key(key: Any) -> bool:
    return not key or key in (
        PLACEHOLDER_KEY,
        PLACEHOLDER_GEMINI,
        PLACEHOLDER_OPENROUTER,
    )


def is_masked_key(value: Any) -> bool:
    return not value or (isinstance(value, str) and "\u2022" in value)


def _ensure_config_dir() -> None:
    if not os.path.isdir(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    else:
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass


def write_config(config: dict[str, Any]) -> None:
    _ensure_config_dir()
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(config, indent=2) + "\n")
    os.replace(tmp_path, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def provider_key_configured(config: dict[str, Any] | None = None) -> bool:
    """True if the *selected* provider has a usable API key or SuperGrok session."""
    cfg = config if config is not None else load_config()
    provider = str(cfg.get("ai_provider") or "xai").lower()
    if provider == "gemini":
        return not is_placeholder_key(cfg.get("gemini_api_key"))
    if provider == "openrouter":
        return not is_placeholder_key(cfg.get("openrouter_api_key"))
    try:
        import supergrok_auth

        mode = supergrok_auth.normalize_auth_mode(cfg.get("auth_mode"))
        if mode == "SUPERGROK_OAUTH":
            info = supergrok_auth.session_info()
            if info.get("signed_in"):
                return True
    except Exception:
        pass
    return not is_placeholder_key(cfg.get("xai_api_key"))


# Labels for settings pickers. Settings clients (Launch Home's AI Voice tab, or
# any launcher that shows an AI Voice pane) render settings_options() as-is and
# keep no model lists of their own, so model updates happen only in this file.
_OPTION_LABELS = {
    "grok-4.7": "grok-4.7",
    "grok-4.6": "grok-4.6 (fallback)",
    "grok-voice-think-fast-2.0": "Voice think fast 2.0",
    "grok-voice-think-fast-1.0": "Voice think fast 1.0",
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "gemini-3.7-flash": "Gemini 3.7 Flash",
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "gemini-3.5-live-translate-preview": "Live translate preview",
    "gemini-3.5-transcribe": "Gemini 3.5 Transcribe",
    "openai/gpt-6-luna": "GPT-6 Luna",
    "openai/gpt-6-sol": "GPT-6 Sol",
    "google/gemini-3.8-flash": "Gemini 3.8 Flash",
    "anthropic/claude-sonnet-5": "Claude Sonnet 5",
    "x-ai/grok-4.7": "Grok 4.7",
    "openai/gpt-transcribe": "GPT Transcribe",
    "assemblyai/universal-3-5-pro": "AssemblyAI Universal-3.5 Pro",
    "meta/muse-voice-transcribe-1.0": "Meta Muse Voice Transcribe",
    "mistralai/voxtral-mini-transcribe": "Voxtral Mini Transcribe",
    "nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b": "Nemotron ASR 0.6B",
    "qwen/qwen3-asr-1.7b": "Qwen3 ASR 1.7B",
    "deepgram/nova-3": "Deepgram Nova-3",
    "x-ai/grok-stt-1.0": "Grok STT 1.0",
    "google/chirp-3": "Google Chirp 3",
    "openai/whisper-large-v3-turbo": "Whisper Large V3 Turbo",
}

# Speech language picker (code, label).
STT_LANGUAGES = [
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("zh", "Chinese"),
    ("vi", "Vietnamese"),
]


def settings_options() -> dict[str, list[dict[str, str]]]:
    """Picker choices for every selectable setting, first entry = default."""

    def opts(ids: list[str]) -> list[dict[str, str]]:
        return [{"value": v, "label": _OPTION_LABELS.get(v, v)} for v in ids]

    return {
        "chat_model": opts(CHAT_MODELS),
        # grok-voice-latest stays accepted on load but isn't offered.
        "voice_model": opts([m for m in VOICE_MODELS if m != "grok-voice-latest"]),
        "gemini_model": opts(GEMINI_MODELS),
        "gemini_stt_model": opts(GEMINI_STT_MODELS),
        "openrouter_model": opts(OPENROUTER_MODELS),
        "openrouter_stt_model": opts(OPENROUTER_STT_MODELS),
        "stt_language": [{"value": c, "label": label} for c, label in STT_LANGUAGES],
    }


def get_public_config() -> dict[str, Any]:
    config = load_config()
    raw_xai = config.get("xai_api_key")
    raw_gem = config.get("gemini_api_key")
    raw_or = config.get("openrouter_api_key")
    oauth_info: dict[str, Any] = {}
    auth_mode = "API_KEY"
    try:
        import supergrok_auth

        auth_mode = supergrok_auth.normalize_auth_mode(config.get("auth_mode"))
        oauth_info = supergrok_auth.session_info()
    except Exception:
        oauth_info = {"signed_in": False, "email": ""}
    out: dict[str, Any] = {
        "xai_api_key_masked": mask_api_key(raw_xai),
        "xai_api_key_full": "" if is_placeholder_key(raw_xai) else (raw_xai or ""),
        "gemini_api_key_masked": mask_api_key(raw_gem),
        "gemini_api_key_full": "" if is_placeholder_key(raw_gem) else (raw_gem or ""),
        "openrouter_api_key_masked": mask_api_key(raw_or),
        "openrouter_api_key_full": "" if is_placeholder_key(raw_or) else (raw_or or ""),
        "api_key_configured": provider_key_configured(config),
        "xai_api_key_configured": not is_placeholder_key(raw_xai),
        "gemini_api_key_configured": not is_placeholder_key(raw_gem),
        "openrouter_api_key_configured": not is_placeholder_key(raw_or),
        "auth_mode": auth_mode,
        "oauth_signed_in": bool(oauth_info.get("signed_in")),
        "oauth_email": oauth_info.get("email") or "",
        "oauth_pending": oauth_info.get("pending"),
        "oauth_error": oauth_info.get("error") or "",
        "oauth_expires_at": oauth_info.get("expires_at") or 0,
        "oauth_token_expired": bool(oauth_info.get("token_expired")),
        "last_xai_error": load_last_xai_error(),
    }
    for key in PUBLIC_KEYS:
        out[key] = config.get(key)
    out["options"] = settings_options()
    return out


def apply_updates(updates: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Apply updates. Returns (config, changed). No write if nothing changed."""
    if not isinstance(updates, dict):
        raise ValueError("Invalid config payload")

    config = load_config()
    changed = False

    for key in PUBLIC_KEYS:
        if key not in updates or updates[key] is None:
            continue
        if key == "overlay_auto_dismiss_sec":
            try:
                sec = float(updates[key])
            except (TypeError, ValueError):
                raise ValueError("overlay_auto_dismiss_sec must be between 3 and 120")
            if sec < 3 or sec > 120:
                raise ValueError("overlay_auto_dismiss_sec must be between 3 and 120")
            config[key] = sec
            changed = True
            continue
        if key == "tts_speed":
            try:
                spd = float(updates[key])
            except (TypeError, ValueError):
                raise ValueError("tts_speed must be a number")
            config[key] = max(0.7, min(1.5, spd))
            changed = True
            continue
        if key in ("close_native_aiplatform", "tts_enabled", "web_search"):
            config[key] = bool(updates[key])
            changed = True
            continue
        if key == "tts_voice":
            if str(updates[key]) not in TTS_VOICES:
                raise ValueError("tts_voice must be one of the supported voices")
            config[key] = str(updates[key])
            changed = True
            continue
        if key == "ai_provider":
            prov = str(updates[key]).strip().lower()
            if prov not in AI_PROVIDERS:
                raise ValueError("ai_provider must be 'xai', 'gemini', or 'openrouter'")
            config[key] = prov
            changed = True
            continue
        if key == "auth_mode":
            try:
                import supergrok_auth

                config[key] = supergrok_auth.normalize_auth_mode(updates[key])
            except Exception:
                config[key] = "API_KEY"
            changed = True
            continue
        if key == "gemini_model":
            model = str(updates[key]).strip()
            if not model:
                raise ValueError("gemini_model is required")
            config[key] = model
            changed = True
            continue
        if key == "gemini_stt_model":
            model = str(updates[key]).strip()
            if not model:
                raise ValueError("gemini_stt_model is required")
            if len(model) > 160:
                raise ValueError("gemini_stt_model is too long")
            config[key] = model
            changed = True
            continue
        if key == "openrouter_model":
            model = str(updates[key]).strip()
            if not model:
                raise ValueError("openrouter_model is required")
            if len(model) > 160:
                raise ValueError("openrouter_model is too long")
            config[key] = model
            changed = True
            continue
        if key == "openrouter_stt_model":
            model = str(updates[key]).strip()
            if not model:
                raise ValueError("openrouter_stt_model is required")
            if len(model) > 200:
                raise ValueError("openrouter_stt_model is too long")
            config[key] = model
            changed = True
            continue
        if key == "voice_model":
            model = str(updates[key]).strip()
            model = _VOICE_MODEL_ALIASES.get(model, model)
            if model not in VOICE_MODELS:
                raise ValueError(
                    "voice_model must be one of: " + ", ".join(VOICE_MODELS)
                )
            config[key] = model
            changed = True
            continue
        if key == "chat_model":
            model = str(updates[key]).strip()
            model = _CHAT_MODEL_ALIASES.get(model, model)
            if model not in CHAT_MODELS:
                raise ValueError(
                    "chat_model must be one of: " + ", ".join(CHAT_MODELS)
                )
            config[key] = model
            changed = True
            continue
        value = str(updates[key]).strip()
        if not value:
            raise ValueError(key + " is required")
        config[key] = value
        changed = True

    if "xai_api_key" in updates and not is_masked_key(updates["xai_api_key"]):
        api_key = str(updates["xai_api_key"]).strip()
        if not api_key:
            raise ValueError("xai_api_key is required")
        # JWT from SuperGrok / `grok login` — store as OAuth, not an API key.
        if api_key.startswith("eyJ"):
            import supergrok_auth

            supergrok_auth.save_access_token(api_key)
            config["auth_mode"] = "SUPERGROK_OAUTH"
            changed = True
        elif not api_key.startswith("xai-"):
            raise ValueError("xai_api_key should start with xai-")
        else:
            config["xai_api_key"] = api_key
            changed = True

    if "gemini_api_key" in updates and not is_masked_key(updates["gemini_api_key"]):
        gkey = str(updates["gemini_api_key"]).strip()
        if not gkey:
            raise ValueError("gemini_api_key is required")
        # Google AI Studio keys typically start with AIza
        config["gemini_api_key"] = gkey
        changed = True

    if "openrouter_api_key" in updates and not is_masked_key(
        updates["openrouter_api_key"]
    ):
        okey = str(updates["openrouter_api_key"]).strip()
        if not okey:
            raise ValueError("openrouter_api_key is required")
        config["openrouter_api_key"] = okey
        changed = True

    # Ensure the selected provider has a key after save when changing provider.
    provider = str(config.get("ai_provider") or "xai").lower()
    if provider == "gemini" and is_placeholder_key(config.get("gemini_api_key")):
        # Allow saving other fields if gemini key already missing and not in this payload
        if "gemini_api_key" in updates or "ai_provider" in updates:
            if is_placeholder_key(config.get("gemini_api_key")):
                raise ValueError("Enter a Gemini API key when using Google Gemini")
    if provider == "openrouter" and is_placeholder_key(
        config.get("openrouter_api_key")
    ):
        if "openrouter_api_key" in updates or "ai_provider" in updates:
            if is_placeholder_key(config.get("openrouter_api_key")):
                raise ValueError("Enter an OpenRouter API key from openrouter.ai")
    if provider == "xai" and is_placeholder_key(config.get("xai_api_key")):
        try:
            import supergrok_auth

            oauth_ok = (
                supergrok_auth.normalize_auth_mode(config.get("auth_mode"))
                == "SUPERGROK_OAUTH"
                and bool(supergrok_auth.session_info().get("signed_in"))
            )
        except Exception:
            oauth_ok = False
        if not oauth_ok and ("xai_api_key" in updates or "ai_provider" in updates):
            if is_placeholder_key(config.get("xai_api_key")):
                raise ValueError("Enter an xAI API key, or sign in with SuperGrok Heavy")

    if not changed:
        return config, False

    write_config(config)
    return config, True


def restart_daemon() -> tuple[bool, str]:
    """Restart by exiting: Launch Home's run loop (voice-run.sh) starts a fresh
    daemon a few seconds later. The delay lets the setConfig reply go out."""
    import os
    import threading

    threading.Timer(0.8, os._exit, args=(0,)).start()
    return True, "restarting"


def get_status() -> dict[str, Any]:
    config = load_config()
    # Answering this request means the daemon is running.
    daemon_active = True
    provider = str(config.get("ai_provider") or "xai").lower()
    # WS probe: Launch Home and the voice card need 8678 open.
    ws_ok = False
    try:
        import socket as _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(0.4)
        ws_ok = s.connect_ex(("127.0.0.1", 8678)) == 0
        s.close()
    except Exception:  # noqa: BLE001
        ws_ok = False
    oauth_info: dict[str, Any] = {}
    auth_mode = "API_KEY"
    try:
        import supergrok_auth

        auth_mode = supergrok_auth.normalize_auth_mode(config.get("auth_mode"))
        oauth_info = supergrok_auth.session_info()
    except Exception:
        oauth_info = {}
    last_err = load_last_xai_error()
    return {
        "daemonActive": daemon_active,
        "apiKeyConfigured": provider_key_configured(config),
        "aiProvider": provider,
        "authMode": auth_mode,
        "oauthSignedIn": bool(oauth_info.get("signed_in")),
        "oauthEmail": oauth_info.get("email") or "",
        "oauthPending": oauth_info.get("pending"),
        "oauthError": oauth_info.get("error") or "",
        "oauthExpiresAt": oauth_info.get("expires_at") or 0,
        "oauthTokenExpired": bool(oauth_info.get("token_expired")),
        "lastXaiError": last_err,
        "configPath": CONFIG_PATH,
        "configExists": os.path.isfile(CONFIG_PATH),
        "wsListening": ws_ok,
    }


def get_api_key() -> str:
    """Active provider's API key (for legacy callers)."""
    config = load_config()
    provider = str(config.get("ai_provider") or "xai").lower()
    if provider == "gemini":
        raw = config.get("gemini_api_key")
    elif provider == "openrouter":
        raw = config.get("openrouter_api_key")
    else:
        raw = config.get("xai_api_key")
    return "" if is_placeholder_key(raw) else (raw or "")

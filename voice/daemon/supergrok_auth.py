#!/usr/bin/env python3
"""SuperGrok / SuperGrok Heavy OIDC device-code auth (same client as Grok CLI).

Ported from macos-grok-agent `supergrok_auth.rs`. Tokens are stored under
/home/root/.config/launch-home-voice/oauth.json and used as Bearer for api.x.ai.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
SCOPE = "openid profile email offline_access api:access grok-cli:access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

CONFIG_DIR = os.environ.get(
    "LH_VOICE_CONFIG_DIR", "/home/root/.config/launch-home-voice"
)
OAUTH_PATH = os.environ.get(
    "LH_VOICE_OAUTH", os.path.join(CONFIG_DIR, "oauth.json")
)
CLI_AUTH_CANDIDATES = (
    "/home/root/.grok/auth.json",
    "/tmp/grok-auth.json",
    os.path.expanduser("~/.grok/auth.json"),
)

REFRESH_SKEW_SEC = 120
# Avoid hitting the token endpoint on every Settings poll.
_PROBE_TTL_SEC = 45.0

_lock = threading.Lock()
_pending: Optional[dict[str, Any]] = None
_poll_stop = threading.Event()
_poll_thread: Optional[threading.Thread] = None
_probe_at = 0.0
_probe_ok = True
_probe_err = ""


def normalize_auth_mode(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in (
        "SUPERGROK_OAUTH",
        "OAUTH",
        "SUPERGROK",
        "SUPERGROK_HEAVY",
        "HEAVY",
    ):
        return "SUPERGROK_OAUTH"
    return "API_KEY"


def _now() -> float:
    return time.time()


def _decode_jwt_claims(jwt: str) -> Optional[dict[str, Any]]:
    parts = (jwt or "").split(".")
    if len(parts) < 2:
        return None
    import base64

    pad = "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _email_from_jwt(jwt: str) -> Optional[str]:
    claims = _decode_jwt_claims(jwt) or {}
    for key in ("email", "preferred_username", "name"):
        val = claims.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    tier = claims.get("tier")
    if isinstance(tier, str) and tier.strip():
        return "SuperGrok (%s)" % tier.strip()
    return None


def _ensure_dir() -> None:
    if not os.path.isdir(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)


def load_session() -> Optional[dict[str, Any]]:
    if not os.path.isfile(OAUTH_PATH):
        return None
    try:
        with open(OAUTH_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return data


def _set_config_auth_mode_oauth() -> None:
    """Persist SuperGrok as the active auth mode so Settings does not snap back."""
    try:
        import config_store

        cfg = config_store.load_config()
        if normalize_auth_mode(cfg.get("auth_mode")) == "SUPERGROK_OAUTH":
            return
        cfg["auth_mode"] = "SUPERGROK_OAUTH"
        config_store.write_config(cfg)
    except Exception as exc:  # noqa: BLE001
        print("[voice] could not save SuperGrok auth_mode: %s" % exc, flush=True)


def _reset_probe(ok: Optional[bool] = None, err: str = "") -> None:
    global _probe_at, _probe_ok, _probe_err
    _probe_at = _now() if ok is not None else 0.0
    if ok is None:
        _probe_ok = True
        _probe_err = ""
    else:
        _probe_ok = bool(ok)
        _probe_err = err or ""


def save_session(session: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = OAUTH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(session, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, OAUTH_PATH)
    try:
        os.chmod(OAUTH_PATH, 0o600)
    except OSError:
        pass
    _reset_probe(True, "")
    _set_config_auth_mode_oauth()


def clear_session() -> None:
    cancel_login()
    _reset_probe(False, "")
    try:
        if os.path.isfile(OAUTH_PATH):
            os.remove(OAUTH_PATH)
    except OSError:
        pass


def _access_expires_at(session: dict[str, Any]) -> float:
    stored = 0.0
    try:
        stored = float(session.get("expires_at") or 0)
    except (TypeError, ValueError):
        stored = 0.0
    claims = _decode_jwt_claims(str(session.get("access_token") or "")) or {}
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        return float(exp)
    return stored


def _session_fresh(session: dict[str, Any], now: Optional[float] = None) -> bool:
    if not session or not session.get("access_token"):
        return False
    now = _now() if now is None else now
    exp = _access_expires_at(session)
    if exp <= 0:
        # Opaque / non-JWT token with no stored expiry — treat as live until
        # an API call fails.
        return True
    return exp > now + REFRESH_SKEW_SEC


def _pending_snapshot() -> tuple[Optional[dict[str, Any]], str]:
    with _lock:
        if not _pending:
            return None, ""
        pending = {
            "userCode": _pending.get("user_code") or "",
            "verificationUri": _pending.get("verification_uri_complete")
            or _pending.get("verification_uri")
            or "",
            "error": _pending.get("error") or "",
            "status": _pending.get("status") or "pending",
        }
        return pending, str(_pending.get("error") or "")


def _cached_refresh_probe() -> tuple[bool, str]:
    """Try get_valid_access_token(), cached so Settings polls stay cheap."""
    global _probe_at, _probe_ok, _probe_err
    now = _now()
    with _lock:
        if _probe_at and (now - _probe_at) < _PROBE_TTL_SEC:
            return _probe_ok, _probe_err
    try:
        get_valid_access_token()
        ok, err = True, ""
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc)
    with _lock:
        _probe_at = _now()
        _probe_ok = ok
        _probe_err = err
    return ok, err


def session_info() -> dict[str, Any]:
    """Return UI-facing auth state. signed_in is false once the session is dead.

    A leftover oauth.json used to count as signed-in forever, so Settings hid
    the QR and claimed success after an expired SuperGrok login.
    """
    session = load_session()
    pending, pending_err = _pending_snapshot()
    if not session:
        return {
            "signed_in": False,
            "email": "",
            "expires_at": 0,
            "token_expired": False,
            "pending": pending,
            "error": pending_err,
        }

    email = session.get("email") or _email_from_jwt(
        str(session.get("access_token") or "")
    ) or ""
    expires_at = _access_expires_at(session)
    now = _now()
    refresh = str(session.get("refresh_token") or "")
    empty = {
        "email": email,
        "expires_at": expires_at,
        "pending": pending,
    }

    if _session_fresh(session, now):
        return {
            "signed_in": True,
            "token_expired": False,
            "error": pending_err,
            **empty,
        }

    # Access is expired. During an in-flight device-code login do not probe
    # refresh — the UI should keep showing the QR.
    if pending and (pending.get("status") or "pending") == "pending":
        return {
            "signed_in": False,
            "token_expired": True,
            "error": pending_err or "Sign-in expired. Sign in again.",
            **empty,
        }

    if refresh:
        ok, err = _cached_refresh_probe()
        if ok:
            live = load_session() or session
            live_email = live.get("email") or email
            return {
                "signed_in": True,
                "email": live_email,
                "expires_at": _access_expires_at(live),
                "token_expired": False,
                "pending": pending,
                "error": pending_err,
            }
        return {
            "signed_in": False,
            "token_expired": True,
            "error": err or pending_err or "Sign-in expired. Sign in again.",
            **empty,
        }

    return {
        "signed_in": False,
        "token_expired": True,
        "error": pending_err or "Sign-in expired. Sign in again.",
        **empty,
    }


def _form_post(url: str, fields: list[tuple[str, str]], timeout: float = 20.0) -> tuple[int, str]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = str(exc)
        return int(exc.code), raw
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def request_device_code() -> dict[str, Any]:
    status, raw = _form_post(
        DEVICE_CODE_URL,
        [("client_id", CLIENT_ID), ("scope", SCOPE)],
    )
    if status < 200 or status >= 300:
        raise RuntimeError(
            "Device code request failed HTTP %s: %s" % (status, raw[:300])
        )
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("Invalid device code JSON: %s" % exc)
    device_code = str(data.get("device_code") or "")
    user_code = str(data.get("user_code") or "")
    if not device_code or not user_code:
        raise RuntimeError("Device code response missing codes")
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": str(
            data.get("verification_uri") or "https://accounts.x.ai/oauth2/device"
        ),
        "verification_uri_complete": str(data.get("verification_uri_complete") or ""),
        "expires_in": int(data.get("expires_in") or 1800),
        "interval": max(1, int(data.get("interval") or 5)),
    }


def _parse_tokens(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    access = str(data.get("access_token") or "")
    if not access:
        raise RuntimeError("Token response missing access_token")
    expires_in = int(data.get("expires_in") or 3600)
    return {
        "access_token": access,
        "refresh_token": str(data.get("refresh_token") or ""),
        "expires_at": _now() + max(60, expires_in),
        "email": _email_from_jwt(access) or "",
    }


def _persist_tokens(tokens: dict[str, Any], keep_refresh: str = "") -> dict[str, Any]:
    refresh = tokens.get("refresh_token") or keep_refresh
    session = {
        "access_token": tokens["access_token"],
        "refresh_token": refresh,
        "expires_at": tokens.get("expires_at") or (_now() + 3600),
        "email": tokens.get("email") or _email_from_jwt(tokens["access_token"]) or "",
    }
    prev = load_session()
    if prev and not session["email"]:
        session["email"] = prev.get("email") or ""
    save_session(session)
    return session


def _poll_once(device_code: str) -> tuple[str, Any]:
    status, raw = _form_post(
        TOKEN_URL,
        [
            ("grant_type", DEVICE_GRANT),
            ("device_code", device_code),
            ("client_id", CLIENT_ID),
        ],
    )
    if 200 <= status < 300:
        try:
            return "ok", _parse_tokens(raw)
        except Exception as exc:
            return "error", str(exc)
    err = ""
    try:
        obj = json.loads(raw)
        err = str(obj.get("error") or "")
        desc = str(obj.get("error_description") or err or raw[:200])
    except Exception:
        desc = raw[:200] or ("HTTP %s" % status)
    if err in ("authorization_pending", "slow_down") or "pending" in desc.lower():
        return err or "authorization_pending", desc
    if err == "access_denied":
        return "denied", desc
    if err == "expired_token":
        return "expired", desc
    return "error", desc


def _poll_loop(device_code: str, interval: int) -> None:
    wait = max(1, int(interval))
    deadline = _now() + 15 * 60
    while _now() < deadline and not _poll_stop.is_set():
        kind, payload = _poll_once(device_code)
        if kind == "ok" and isinstance(payload, dict):
            session = _persist_tokens(payload)
            with _lock:
                global _pending
                _pending = None
            print(
                "[voice] SuperGrok signed in email=%s"
                % (session.get("email") or "?"),
                flush=True,
            )
            return
        if kind == "slow_down":
            wait = min(15, wait + 2)
        elif kind in ("denied", "expired", "error"):
            with _lock:
                if _pending:
                    _pending["status"] = kind
                    _pending["error"] = str(payload)
            print("[voice] SuperGrok login %s: %s" % (kind, payload), flush=True)
            return
        _poll_stop.wait(wait)
    with _lock:
        if _pending and _pending.get("status") == "pending":
            _pending["status"] = "expired"
            _pending["error"] = "Timed out waiting for SuperGrok approval."


def start_login() -> dict[str, Any]:
    cancel_login()
    _reset_probe()
    device = request_device_code()
    with _lock:
        global _pending, _poll_thread
        _pending = {
            "device_code": device["device_code"],
            "user_code": device["user_code"],
            "verification_uri": device["verification_uri"],
            "verification_uri_complete": device["verification_uri_complete"],
            "status": "pending",
            "error": "",
        }
        _poll_stop.clear()
        _poll_thread = threading.Thread(
            target=_poll_loop,
            args=(device["device_code"], device["interval"]),
            daemon=True,
            name="supergrok-poll",
        )
        _poll_thread.start()
    _set_config_auth_mode_oauth()
    return {
        "userCode": device["user_code"],
        "verificationUri": device["verification_uri_complete"]
        or device["verification_uri"],
        "expiresIn": device["expires_in"],
    }


def cancel_login() -> None:
    global _pending, _poll_thread
    _poll_stop.set()
    with _lock:
        _pending = None
        _poll_thread = None


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    status, raw = _form_post(
        TOKEN_URL,
        [
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token),
            ("client_id", CLIENT_ID),
        ],
    )
    if status < 200 or status >= 300:
        raise RuntimeError("Token refresh failed HTTP %s: %s" % (status, raw[:300]))
    return _parse_tokens(raw)


def get_valid_access_token() -> str:
    session = load_session()
    if not session:
        raise RuntimeError(
            "SuperGrok is not signed in. Open Settings → AI Voice → Sign in with SuperGrok."
        )
    access = str(session.get("access_token") or "")
    expires_at = _access_expires_at(session)
    now = _now()
    if access and _session_fresh(session, now):
        return access
    refresh = str(session.get("refresh_token") or "")
    if not refresh:
        if access and expires_at > now:
            return access
        raise RuntimeError("SuperGrok sign-in expired. Sign in again in Settings.")
    try:
        tokens = refresh_access_token(refresh)
        updated = _persist_tokens(tokens, keep_refresh=refresh)
        return str(updated["access_token"])
    except Exception as exc:
        if access and expires_at > now:
            return access
        raise RuntimeError("SuperGrok token refresh failed: %s" % exc)


def import_cli_auth(path: Optional[str] = None) -> dict[str, Any]:
    paths = [path] if path else list(CLI_AUTH_CANDIDATES)
    last_err = "Auth file not found"
    chosen = ""
    data: Optional[dict[str, Any]] = None
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                parsed = json.load(fh)
        except Exception as exc:
            last_err = "Could not read %s: %s" % (p, exc)
            continue
        data = parsed if isinstance(parsed, dict) else None
        chosen = p
        break
    if data is None:
        raise RuntimeError(
            last_err
            + ". Copy ~/.grok/auth.json from a Mac after `grok login` to "
            "/home/root/.grok/auth.json on the TV."
        )
    token = ""
    refresh = ""
    email = ""
    if data.get("key") or data.get("access_token"):
        token = str(data.get("key") or data.get("access_token") or "").strip()
        refresh = str(data.get("refresh_token") or "")
        email = str(data.get("email") or "")
    else:
        entries = []
        for _k, val in data.items():
            if not isinstance(val, dict):
                continue
            key = str(val.get("key") or val.get("access_token") or "").strip()
            if key:
                entries.append(val)
        if not entries:
            raise RuntimeError("No access token in %s" % chosen)
        entry = entries[0]
        token = str(entry.get("key") or entry.get("access_token") or "").strip()
        refresh = str(entry.get("refresh_token") or "")
        email = str(entry.get("email") or "")
    if not token:
        raise RuntimeError("Empty token in %s" % chosen)
    claims = _decode_jwt_claims(token) or {}
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        expires_at = float(exp)
    elif refresh:
        expires_at = _now() + 30 * 24 * 3600
    else:
        # Opaque long-lived token with no exp claim — don't fake a 1h expiry.
        expires_at = 0
    session = {
        "access_token": token,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "email": email or _email_from_jwt(token) or "",
    }
    save_session(session)
    cancel_login()
    return session_info()


def save_access_token(token: str) -> dict[str, Any]:
    token = (token or "").strip()
    if not token:
        raise RuntimeError("Empty SuperGrok token")
    claims = _decode_jwt_claims(token) or {}
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        expires_at = float(exp)
    else:
        expires_at = 0
    prev = load_session() or {}
    session = {
        "access_token": token,
        "refresh_token": prev.get("refresh_token") or "",
        "expires_at": expires_at,
        "email": _email_from_jwt(token) or prev.get("email") or "",
    }
    save_session(session)
    return session_info()


def resolve_bearer(auth_mode: str, api_key: str) -> tuple[str, str]:
    """Return (bearer, mode). mode is SUPERGROK_OAUTH or API_KEY."""
    mode = normalize_auth_mode(auth_mode)
    if mode == "SUPERGROK_OAUTH":
        return get_valid_access_token(), "SUPERGROK_OAUTH"
    key = (api_key or "").strip()
    if not key or key.startswith("xai-..."):
        raise RuntimeError(
            "Add an xAI API key in Settings, or sign in with SuperGrok Heavy."
        )
    return key, "API_KEY"

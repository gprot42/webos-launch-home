#!/usr/bin/env python3
"""Phase 2 (README.ai.md §2): TV control via Luna / LS2.

Maps high-confidence voice commands to Luna calls so the TV can be controlled
without guessing. Ambiguous utterances return None so the caller falls back to
Grok chat.

High priority (implemented):
  - Power off / screen off
  - Volume up / down / set / mute / unmute
  - Channel up / down / go to channel N (best-effort)
  - Inputs: HDMI 1–4, AV, component, live TV
  - Apps: Netflix, YouTube, Home, browser, common store apps
  - Accessibility: captions / subtitles on|off (settings best-effort)

Medium priority (subset):
  - Home / settings launch
  - Sleep timer (screen off after N minutes via Timer)
"""

from __future__ import annotations

import json
import glob
import os
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Luna helpers
# ---------------------------------------------------------------------------


def _luna(uri: str, payload: Optional[dict[str, Any]] = None, timeout: float = 8.0) -> dict[str, Any]:
    """Call a Luna method via luna-send (same shape as luna_service._luna_send)."""
    # Prefer the shared helper so TV-control launches use the identical path as
    # overlay launch (which is known-good from the systemd daemon process).
    try:
        from luna_service import _luna_send as _shared_luna_send  # type: ignore

        resp = _shared_luna_send(uri, payload or {}, timeout=timeout)
        if resp:
            return resp
    except Exception:
        pass

    params = json.dumps(payload or {}, separators=(",", ":"))
    wait_ms = str(int(max(timeout, 1.0) * 1000))
    variants = (
        ["luna-send", "-f", "-n", "1", "-w", wait_ms, uri, params],
        ["luna-send", "-n", "1", "-w", wait_ms, uri, params],
        ["luna-send", "-n", "1", uri, params],
        # Some rooted firmwares only answer on the public bus for a subset of
        # methods; keep pub as a last resort.
        ["luna-send-pub", "-n", "1", "-f", "-w", wait_ms, uri, params],
        ["luna-send-pub", "-n", "1", uri, params],
    )
    last_err = ""
    saw_empty = False
    for cmd in variants:
        try:
            out = subprocess.check_output(
                cmd,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout + 2,
            )
            text = (out or "").strip()
            if not text:
                # Empty body is common for successful applicationManager/launch
                # on some webOS builds — remember and keep trying other variants.
                saw_empty = True
                last_err = "empty response from %s" % " ".join(cmd[:3])
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                start = text.find("{")
                if start >= 0:
                    return json.loads(text[start:])
                last_err = "non-json: %r" % text[:120]
        except FileNotFoundError:
            last_err = "binary missing"
            continue
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            last_err = str(exc)[:160]
            continue
    if saw_empty and "/launch" in (uri or ""):
        # applicationManager/launch often returns an empty body on success.
        # Only treat empty as soft-OK for launch methods — not volume/list/etc.
        return {"returnValue": True, "softEmpty": True}
    return {"returnValue": False, "errorText": last_err or "luna call failed"}


def _ok(resp: dict[str, Any]) -> bool:
    if not resp:
        return False
    if resp.get("returnValue") is False:
        return False
    if "errorCode" in resp and resp.get("returnValue") is not True:
        return False
    # Explicit success
    if resp.get("returnValue") is True:
        return True
    # Empty-ish success-like payloads from some firmwares
    return "errorText" not in resp


def _first_ok(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for uri, payload in calls:
        last = _luna(uri, payload)
        if _ok(last):
            return last
    return last


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class TvControlResult:
    """Outcome of a matched TV control command."""

    kind: str
    spoken: str
    ok: bool
    detail: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Intent matchers — only fire on confident phrases
# ---------------------------------------------------------------------------

_VOL_UP = re.compile(
    r"\b(?:volume\s*up|turn\s+(?:the\s+)?volume\s*up|louder|increase\s+(?:the\s+)?volume|"
    r"turn\s+it\s*up|raise\s+(?:the\s+)?volume)\b",
    re.I,
)
_VOL_DOWN = re.compile(
    r"\b(?:volume\s*down|turn\s+(?:the\s+)?volume\s*down|quieter|softer|"
    r"decrease\s+(?:the\s+)?volume|turn\s+it\s*down|lower\s+(?:the\s+)?volume)\b",
    re.I,
)
# STT often writes "ten" / "twenty five" instead of digits.
_VOL_LEVEL = (
    r"("
    r"\d{1,3}"
    r"|one\s+hundred|a\s+hundred|hundred|maximum|max|full|half|medium|mid"
    r"|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:\s*[- ]\s*(?:one|two|three|four|five|six|seven|eight|nine))?"
    r"|zero|oh|nought|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
    r")"
)
# Prefixes share a single level capture so STT variants all hit one group.
_VOL_SET = re.compile(
    r"\b(?:"
    r"(?:set|change|put|make|adjust)\s+(?:the\s+)?volume\s+(?:to\s+|at\s+|level\s+)?"
    r"|volumes?\s+(?:set\s+)?(?:to\s+|at\s+|level\s+)"
    r"|volumes?\s+(?:level\s+)?"
    r"|(?:increase|raise|decrease|lower)\s+(?:the\s+)?volume\s+to\s+"
    r"|volume\s+set\s+(?:to\s+)?"
    r"|turn\s+(?:the\s+)?volume\s+(?:to\s+|at\s+|down\s+to\s+|up\s+to\s+)"
    r")"
    + _VOL_LEVEL
    + r"\b",
    re.I,
)
_WORD_NUMBERS: dict[str, int] = {
    "zero": 0,
    "oh": 0,
    "nought": 0,
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
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "max": 100,
    "maximum": 100,
    "full": 100,
    "half": 50,
    "medium": 50,
    "mid": 50,
}


def _parse_volume_level(raw: str) -> Optional[int]:
    """Parse '10', 'ten', 'twenty five', 'one hundred' → 0–100."""
    s = re.sub(r"[\s\-]+", " ", (raw or "").strip().lower())
    if not s:
        return None
    if re.fullmatch(r"\d{1,3}", s):
        return max(0, min(100, int(s)))
    if s in _WORD_NUMBERS:
        return _WORD_NUMBERS[s]
    if s in ("one hundred", "a hundred"):
        return 100
    parts = s.split()
    if len(parts) == 2 and parts[0] in _WORD_NUMBERS and parts[1] in _WORD_NUMBERS:
        tens = _WORD_NUMBERS[parts[0]]
        ones = _WORD_NUMBERS[parts[1]]
        if 20 <= tens <= 90 and tens % 10 == 0 and 1 <= ones <= 9:
            return tens + ones
    return None
_MUTE = re.compile(
    r"\b(?:mute(?:\s+(?:the\s+)?(?:tv|volume|sound|audio))?|"
    r"mute\s+volume|silence(?:\s+(?:the\s+)?(?:tv|volume|sound))?|"
    r"turn\s+(?:the\s+)?sound\s+off)\b",
    re.I,
)
_UNMUTE = re.compile(
    r"\b(?:unmute(?:\s+(?:the\s+)?(?:tv|volume|sound|audio))?|"
    r"un[- ]mute(?:\s+(?:the\s+)?(?:volume|sound))?|"
    r"sound\s+on|turn\s+(?:the\s+)?sound\s+on)\b",
    re.I,
)

_POWER_OFF = re.compile(
    r"\b(?:turn\s+(?:the\s+)?tv\s+off|power\s+off(?:\s+the\s+tv)?|switch\s+(?:the\s+)?tv\s+off|"
    r"shut\s+(?:the\s+)?tv\s+off|turn\s+off\s+(?:the\s+)?tv)\b",
    re.I,
)
_SCREEN_OFF = re.compile(
    r"\b(?:turn\s+(?:the\s+)?screen\s+off|screen\s+off|display\s+off)\b",
    re.I,
)

_CH_UP = re.compile(r"\b(?:channel\s*up|next\s+channel)\b", re.I)
_CH_DOWN = re.compile(r"\b(?:channel\s*down|previous\s+channel|last\s+channel)\b", re.I)
_CH_SET = re.compile(
    r"\b(?:go\s+to\s+channel|switch\s+to\s+channel|channel)\s+(\d{1,4})\b",
    re.I,
)

_HOME = re.compile(r"\b(?:go\s+home|open\s+home|home\s+screen|show\s+home)\b", re.I)
_SETTINGS = re.compile(
    r"\b(?:open\s+(?:all\s+|full\s+|picture\s+|sound\s+|network\s+|general\s+)?"
    r"settings?|go\s+to\s+settings?|show\s+settings?)\b",
    re.I,
)
# Magic Remote STT often mangles "settings" → sea / seat / set / sittings.
_SETTINGS_STT_FIX = re.compile(
    r"\bopen\s+(?:the\s+)?"
    r"(?:sea|seat|set|sets|setts|settins?|sittings?|sitings?|setting|"
    r"sethings?|system\s*settings?|tv\s*settings?)\b",
    re.I,
)


def normalize_control_transcript(text: str) -> str:
    """Fix common STT mangling of TV control phrases before matching."""
    t = _normalize(text)
    if not t:
        return t
    # "open sea" / "open seat" (confirmed on device) → open settings
    t = _SETTINGS_STT_FIX.sub("open settings", t)
    # Bare "settings" with open-ish verbs already covered by _SETTINGS.
    t = re.sub(r"\bgo\s+to\s+(?:sea|seat|settins?)\b", "go to settings", t, flags=re.I)
    t = re.sub(r"\bshow\s+(?:sea|seat|settins?)\b", "show settings", t, flags=re.I)
    # STT often spells letter brands with spaces: "b b c" → "bbc", "i t v" → "itv".
    t = re.sub(r"\bb\s+b\s+c\b", "bbc", t, flags=re.I)
    t = re.sub(r"\bi\s+t\s+v\b", "itv", t, flags=re.I)
    # BBC iPlayer is mangled heavily by STT (device logs):
    #   "See i player" / "See, I play" / "P C I player" / "launch eye player"
    t = re.sub(
        r"\b(?:see|c|sea|si|pc|p\s*c|be|bee)\s*[,\.]?\s*i\s*(?:player|play|plar)\b",
        "bbc iplayer",
        t,
        flags=re.I,
    )
    t = re.sub(r"\bi\s*player\b", "iplayer", t, flags=re.I)
    # Trailing "i play" (missing 'er') when user said iPlayer.
    t = re.sub(r"\bi\s+play\b", "iplayer", t, flags=re.I)
    t = re.sub(r"\beye\s*player\b", "iplayer", t, flags=re.I)
    t = re.sub(r"\bhigh\s*player\b", "iplayer", t, flags=re.I)
    # "launch bbc" alone is enough intent when only iPlayer is installed as BBC video.
    return t

_CAPTIONS_ON = re.compile(
    r"\b(?:turn\s+on\s+(?:the\s+)?(?:subtitles?|captions?)|"
    r"(?:subtitles?|captions?)\s+on|"
    r"enable\s+(?:subtitles?|captions?)|"
    r"closed?\s*captions?\s+on)\b",
    re.I,
)
_CAPTIONS_OFF = re.compile(
    r"\b(?:turn\s+off\s+(?:the\s+)?(?:subtitles?|captions?)|"
    r"(?:subtitles?|captions?)\s+off|"
    r"disable\s+(?:subtitles?|captions?)|"
    r"closed?\s*captions?\s+off)\b",
    re.I,
)

_SLEEP = re.compile(
    r"\b(?:sleep\s+timer\s+(\d{1,3})\s*(?:min(?:ute)?s?)?|"
    r"turn\s+off\s+after\s+(\d{1,3})\s*(?:min(?:ute)?s?)?|"
    r"(?:in\s+)?(\d{1,3})\s*(?:min(?:ute)?s?)\s+(?:turn\s+off|sleep))\b",
    re.I,
)

# Input: "switch to HDMI 1", "go to HDMI2", "change input to HDMI 3"
_INPUT = re.compile(
    r"\b(?:(?:switch|change|go)\s+(?:to\s+)?(?:input\s+)?|"
    r"(?:set\s+)?input\s+(?:to\s+)?)"
    r"(hdmi\s*(\d)|av|component|live\s*tv|antenna|cable|tuner)\b",
    re.I,
)
_INPUT_BARE = re.compile(r"\b(hdmi\s*(\d)|live\s*tv)\b", re.I)
_HDMI_DIRECT = re.compile(
    r"^(?:(?:open|launch|start|run|play|go\s+to|switch|change|show|"
    r"lunch|punch|lanch|lauch|launce)\s+)?"
    r"(?:input\s+)?(?:hdmi|dmi|h\s*d\s*m\s*i)\s*[- ]?([1-4])$",
    re.I,
)
_HDMI_STT_ALIASES: dict[str, int] = {
    "am i one": 1,
    "am i 1": 1,
    "am i two": 2,
    "am i 2": 2,
    "am i three": 3,
    "am i 3": 3,
    "am i four": 4,
    "am i 4": 4,
    "my one": 1,
    "my 1": 1,
    "my two": 2,
    "my 2": 2,
    "my three": 3,
    "my 3": 3,
    "my four": 4,
    "my 4": 4,
}
_SKY = re.compile(
    r"^(?:go\s*to|switch\s+to|change\s+to|open|launch|start|show|watch)\s+"
    r"(?:sky|sky\s+tv|sky\s+box|set\s*top\s+box)$",
    re.I,
)
_SKY_STT_ALIASES = frozenset(
    {
        "to sky",
        "two sky",
    }
)
_STT_KEYTERM_ECHO_MARKERS = (
    "go to sky",
    "sky tv",
    "hdmi 1",
    "hdmi 2",
    "prime video",
    "southend-on-sea",
    "mute volume",
    "set volume",
)
_MUTE_STT_ALIASES = frozenset(
    {
        "william",
    }
)

# Apps: "open Netflix", "launch YouTube", "start Disney Plus", "launch brew".
# Group 1 = known aliases; group 2 = free-form name (any short phrase).
# Bare "prime" is intentional — "open prime" / "launch prime" == Prime Video.
_APP = re.compile(
    r"\b(?:open|launch|start|run|go\s+to|play|load|show|put\s+on|fire\s+up|bring\s+up)\s+"
    r"(?:"
    r"(netflix|youtube|you\s*tube|disney(?:\s*plus|\+)?|"
    r"prime(?:\s*video)?|amazon(?:\s*prime(?:\s*video)?)?|"
    r"hulu|max|hbo(?:\s*max)?|apple\s*tv(?:\s*plus)?|spotify|browser|web\s*browser|"
    r"live\s*tv|gallery|photos|music|plex|twitch|paramount(?:\s*plus)?|"
    r"peacock|crunchyroll|tubi|"
    r"bbc(?:\s*iplayer|\s*i\s*player|\s*sounds)?|iplayer|i\s*player|"
    r"channel\s*4|all\s*4|itv(?:x)?|channel\s*5|"
    r"brew|home\s*brew|homebrew(?:\s*channel)?|hb\s*channel|"
    r"terminal|webos\s*terminal|shell|console)"
    r"|"
    # Free-form: one to four words (e.g. "launch BBC iPlayer", "open com.foo.app")
    r"([a-z0-9][a-z0-9._+\-]{0,48}(?:\s+[a-z0-9][a-z0-9._+\-]{0,24}){0,3})"
    r")\b",
    re.I,
)
_YOUTUBE_SEARCH = re.compile(
    r"(?i)^\s*(?:open|launch|start|run|play|go\s+to|load|show|"
    r"lunch|punch|lanch|lauch|launce)\s+"
    r"(?:youtube|you\s*tube)\s+(?:and\s+)?(?:search(?:\s+for)?|find|look\s+for)\s+"
    r"(.+?)\s*$"
)
_PRIME_SEARCH = re.compile(
    r"(?i)^\s*(?:(?:open|launch|start|run|play|go\s+to|load|show|"
    r"lunch|punch|lanch|lauch|launce)\s+)?"
    r"(?:amazon\s+prime(?:\s+video)?|prime\s+video)\s+(?:and\s+)?"
    r"(?:search(?:\s+(?:for|on))?|find|look\s+for)\s+(.+?)\s*$"
)

# Known app id map (LG store / built-in ids vary by region; try several).
_APP_IDS: dict[str, list[str]] = {
    "netflix": ["netflix"],
    "youtube": ["youtube.leanback.v4", "youtube.leanback", "com.webos.app.youtubetv"],
    "disney": ["com.disney.disneyplus-prod", "disney"],
    "prime": ["amazon", "com.amazon.amazonvideo.livingroom"],
    "hulu": ["hulu"],
    "max": ["com.hbo.hbomax", "hbomax"],
    "apple": ["com.apple.appletv", "com.apple.appstore"],
    "spotify": ["spotify-beehive"],
    "browser": ["com.webos.app.browser", "com.webos.app.beanbrowser"],
    "livetv": ["com.webos.app.livetv"],
    "gallery": ["com.webos.app.photovideo", "com.webos.app.igallery"],
    "music": ["com.webos.app.music"],
    "plex": ["cdp-30", "plex"],
    "twitch": ["twitch"],
    "paramount": ["com.cbs.ca", "paramountplus"],
    "peacock": ["com.peacocktv.peacockandroid"],
    "crunchyroll": ["crunchyroll"],
    "tubi": ["com.tubitv.webos"],
    # UK free-to-air / streaming (STT often says "b b c" for BBC)
    "bbc": ["bbc.iplayer.3.0", "bbc.iplayer.lge", "bbc.iplayer"],
    "iplayer": ["bbc.iplayer.3.0", "bbc.iplayer.lge", "bbc.iplayer"],
    "bbcsounds": ["bbc.sounds.1.0", "bbc.sounds"],
    "channel4": ["com.channel4.channel4", "channel4"],
    "itv": ["com.itv.tve", "itv.player", "com.itv.itvx"],
    "channel5": ["com.channel5.my5", "channel5"],
    # webOS Homebrew Channel (common on rooted TVs)
    "brew": [
        "org.webosbrew.hbchannel",
        "com.webosbrew.hbchannel",
        "org.webosbrew.app.hbchannel",
    ],
    # Rooted brew terminal (https://github.com/gprot42/webosterminal)
    "terminal": [
        "com.github.gprot42.webosterminal",
        "com.webosbrew.terminal",
        "org.webosbrew.terminal",
    ],
}

# Cache of installed apps from listApps (id + title), refreshed periodically.
# Long TTL: listApps is 100–400ms on TV and was on the critical path for every
# "launch Netflix". Prefer static _APP_IDS for known names; cache is only for
# free-form / title matching.
_INSTALLED_CACHE: list[tuple[str, str]] = []
_INSTALLED_CACHE_TS: float = 0.0
_INSTALLED_CACHE_TTL_SEC = 600.0
_APP_MANIFEST_DIRS = (
    "/media/developer/apps/usr/palm/applications",
    "/media/cryptofs/apps/usr/palm/applications",
    "/usr/palm/applications",
)
_UNSAFE_LAUNCH_APP_IDS = {
    "com.webos.app.home",
    "com.webos.app.voice",
    "com.webos.app.voiceagent",
}
_UNSAFE_LAUNCH_ID_MARKERS = (
    "sysuicompmgr",
    "voiceagent",
    "voiceconductor",
)


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[?.!,]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def is_tv_control_query(text: str) -> bool:
    """Cheap prefilter: does this *look* like a TV control utterance?"""
    t = _normalize(text)
    if not t or len(t) > 120:
        return False
    keys = (
        "volume", "mute", "unmute", "sound", "audio", "louder", "quieter", "softer",
        "power off", "turn off", "screen off", "shut",
        "channel", "hdmi", "input", "live tv",
        "open ", "launch ", "start ", "run ", "go home", "home screen",
        "subtitle", "caption", "settings", "sleep timer",
        "terminal", "shell", "console", "brew", "netflix", "youtube",
        "prime", "amazon prime", "prime video",
        "amazon", "prime", "disney", "hulu", "spotify", "plex",
        "bbc", "iplayer", "itv", "channel 4", "channel 5", "my5",
        # STT often mangles "launch" → punch/lunch/lounge/…
        "punch ", "lunch ", "lounge ", "large ", "plunch ", "lanch ",
    )
    return any(k in t for k in keys)


def refresh_installed_apps() -> int:
    """Refresh the installed-app cache and return the number of launchable apps."""
    return sum(
        1
        for app_id, title in _list_installed_apps(force=True)
        if _safe_installed_app(app_id, title)
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _get_volume() -> tuple[Optional[int], Optional[bool]]:
    resp = _luna("luna://com.webos.service.audio/master/getVolume", {})
    vs = resp.get("volumeStatus") or resp
    vol = vs.get("volume")
    muted = vs.get("muteStatus")
    if muted is None:
        muted = vs.get("muted")
    try:
        return (int(vol) if vol is not None else None, bool(muted) if muted is not None else None)
    except (TypeError, ValueError):
        return None, None


def _volume_up(steps: int = 3) -> TvControlResult:
    vol, _ = _get_volume()
    if vol is not None:
        return _volume_set(min(100, vol + max(1, steps)), kind="volume_up")
    last = _first_ok(
        [
            ("luna://com.webos.service.audio/master/volumeUp", {}),
            ("luna://com.webos.service.audio/volumeUp", {}),
            ("luna://com.webos.audio/volumeUp", {}),
        ]
    )
    return TvControlResult("volume_up", "Volume up.", _ok(last), last)


def _volume_down(steps: int = 3) -> TvControlResult:
    vol, _ = _get_volume()
    if vol is not None:
        return _volume_set(max(0, vol - max(1, steps)), kind="volume_down")
    last = _first_ok(
        [
            ("luna://com.webos.service.audio/master/volumeDown", {}),
            ("luna://com.webos.service.audio/volumeDown", {}),
            ("luna://com.webos.audio/volumeDown", {}),
        ]
    )
    return TvControlResult("volume_down", "Volume down.", _ok(last), last)


def _volume_set(level: int, *, kind: str = "volume_set") -> TvControlResult:
    level = max(0, min(100, int(level)))
    last = _first_ok(
        [
            ("luna://com.webos.service.audio/master/setVolume", {"volume": level}),
            ("luna://com.webos.service.audio/setVolume", {"volume": level}),
            ("luna://com.webos.audio/setVolume", {"volume": level}),
        ]
    )
    return TvControlResult(
        kind,
        (
            f"Volume up. Now at {level}."
            if kind == "volume_up"
            else f"Volume down. Now at {level}."
            if kind == "volume_down"
            else f"Volume set to {level}."
        ),
        _ok(last),
        last,
    )


def _mute(mute: bool) -> TvControlResult:
    last = _first_ok(
        [
            ("luna://com.webos.service.apiadapter/audio/setMute", {"mute": mute}),
            ("luna://com.webos.service.audio/master/muteVolume", {"mute": mute}),
            ("luna://com.webos.service.audio/master/muteVolume", {"muted": mute}),
            ("luna://com.webos.service.audio/setMuted", {"muted": mute}),
            ("luna://com.webos.audio/setMuted", {"muted": mute}),
        ]
    )
    spoken = "Muted." if mute else "Unmuted."
    return TvControlResult("mute" if mute else "unmute", spoken, _ok(last), last)


def _power_off(screen_only: bool = False) -> TvControlResult:
    if screen_only:
        last = _first_ok(
            [
                ("luna://com.webos.service.tvpower/power/turnOffScreen", {}),
                ("luna://com.webos.service.tvpower/power2/turnOffScreen", {}),
            ]
        )
        return TvControlResult("screen_off", "Turning the screen off.", _ok(last), last)
    last = _first_ok(
        [
            ("luna://com.webos.service.tvpower/power/powerOff", {"reason": "remoteKey"}),
            ("luna://com.webos.service.tvpower/power2/powerOff", {"reason": "remoteKey"}),
            ("luna://com.webos.service.tvpower/power/turnOffScreen", {}),
        ]
    )
    return TvControlResult("power_off", "Powering off the TV.", _ok(last), last)


def _channel_step(up: bool) -> TvControlResult:
    if up:
        last = _first_ok(
            [
                ("luna://com.webos.service.networkinput/controls/channelUp", {}),
                ("luna://com.webos.service.tv.channel/channelUp", {}),
            ]
        )
        return TvControlResult("channel_up", "Channel up.", _ok(last), last)
    last = _first_ok(
        [
            ("luna://com.webos.service.networkinput/controls/channelDown", {}),
            ("luna://com.webos.service.tv.channel/channelDown", {}),
        ]
    )
    return TvControlResult("channel_down", "Channel down.", _ok(last), last)


def _channel_set(number: str) -> TvControlResult:
    # Best-effort: open Live TV then try openChannel by channelNumber.
    _luna("luna://com.webos.applicationManager/launch", {"id": "com.webos.app.livetv"})
    last = _first_ok(
        [
            (
                "luna://com.webos.service.apiadapter/tv/openChannel",
                {"channelNumber": str(number)},
            ),
            (
                "luna://com.webos.service.iepg/openChannel",
                {"channelNumber": str(number)},
            ),
        ]
    )
    ok = _ok(last)
    spoken = (
        f"Switching to channel {number}."
        if ok
        else f"I tried to open channel {number}, but the tuner did not accept it."
    )
    return TvControlResult("channel_set", spoken, ok, last)


def _switch_input(label: str, hdmi_n: Optional[int] = None) -> TvControlResult:
    label_l = label.lower().replace(" ", "")
    # eim input ids commonly look like HDMI_1, HDMI_2, …
    candidates: list[str] = []
    if hdmi_n is not None:
        candidates = [f"HDMI_{hdmi_n}", f"HDMI{hdmi_n}", f"hdmi_{hdmi_n}", f"COMP{hdmi_n}"]
    elif "livetv" in label_l or "antenna" in label_l or "cable" in label_l or "tuner" in label_l:
        # Live TV is an app, not an external input id.
        resp = _luna("luna://com.webos.applicationManager/launch", {"id": "com.webos.app.livetv"})
        return TvControlResult("input_livetv", "Switching to Live TV.", _ok(resp), resp)
    elif "av" in label_l:
        candidates = ["AV_1", "AV1", "COMP1", "COMPONENT"]
    elif "component" in label_l:
        candidates = ["COMP1", "COMPONENT", "COMPONENT_1"]

    # Prefer: resolve via eim list, then launch the appId for that input.
    listing = _luna("luna://com.webos.service.eim/getAllInputStatus", {})
    devices = listing.get("devices") or listing.get("inputList") or []
    if isinstance(devices, list) and devices and hdmi_n is not None:
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            did = str(dev.get("id") or dev.get("inputId") or "")
            dlabel = str(dev.get("label") or dev.get("name") or "")
            if re.search(rf"hdmi\s*{hdmi_n}\b", did + " " + dlabel, re.I):
                app_id = dev.get("appId")
                if app_id:
                    resp = _luna("luna://com.webos.applicationManager/launch", {"id": app_id})
                    return TvControlResult(
                        "input_switch",
                        f"Switching to HDMI {hdmi_n}.",
                        _ok(resp),
                        resp,
                    )
                candidates.insert(0, did)

    last: dict[str, Any] = {}
    for cid in candidates:
        last = _first_ok(
            [
                ("luna://com.webos.service.eim/getInputStatus", {"id": cid}),
            ]
        )
        app_id = last.get("appId")
        if app_id:
            resp = _luna("luna://com.webos.applicationManager/launch", {"id": app_id})
            nice = f"HDMI {hdmi_n}" if hdmi_n else label
            return TvControlResult("input_switch", f"Switching to {nice}.", _ok(resp), resp)
        last = _first_ok(
            [
                ("luna://com.webos.service.apiadapter/tv/switchInput", {"inputId": cid}),
                ("luna://com.webos.service.eim/switchInput", {"inputId": cid, "id": cid}),
            ]
        )
        if _ok(last):
            nice = f"HDMI {hdmi_n}" if hdmi_n else label
            return TvControlResult("input_switch", f"Switching to {nice}.", True, last)

    if hdmi_n is not None:
        hdmi_app_id = f"com.webos.app.hdmi{hdmi_n}"
        installed_ids = {app_id for app_id, _ in _list_installed_apps()}
        if hdmi_app_id in installed_ids:
            last = _luna(
                "luna://com.webos.applicationManager/launch",
                {"id": hdmi_app_id, "params": {}, "noSplash": True},
                timeout=4.0,
            )
            if _ok(last) or last.get("softEmpty"):
                return TvControlResult(
                    "input_switch",
                    f"Switching to HDMI {hdmi_n}.",
                    True,
                    {"id": hdmi_app_id, "resp": last},
                )

    nice = f"HDMI {hdmi_n}" if hdmi_n else label
    return TvControlResult(
        "input_switch",
        f"I could not switch to {nice}.",
        False,
        last,
    )


def _accept_set_top_box_power_prompt(delay: float = 4.4) -> None:
    def _worker() -> None:
        time.sleep(max(0.0, delay))
        ir_resp = _luna(
            "luna://com.webos.service.irdbmanager/sendIrCommand",
            {
                "deviceType": "settop",
                "keyCode": "IR_KEY_POWER",
                "buttonState": "single",
                "connectedInput": "HDMI_3",
            },
            timeout=3.0,
        )
        key_responses: list[dict[str, Any]] = []
        if not _ok(ir_resp):
            # The prompt can appear several seconds after HDMI activation.
            # Repeated ENTER is idempotent after the dialog closes and covers
            # firmware timing variance without moving focus to "No".
            for pause in (0.0, 1.2, 1.8):
                if pause:
                    time.sleep(pause)
                key_responses.append(
                    _luna(
                        "luna://com.webos.service.networkinput/sendSpecialKey",
                        {"key": "ENTER"},
                        timeout=3.0,
                    )
                )
        print(
            "[voice] Sky set-top power ir=%s enter=%s"
            % (ir_resp, key_responses),
            flush=True,
        )

    threading.Thread(target=_worker, daemon=True).start()


def _open_sky() -> TvControlResult:
    result = _switch_input("HDMI 3", 3)
    if result.ok:
        _accept_set_top_box_power_prompt()
        detail = dict(result.detail or {})
        detail.update(
            {
                "device": "sky",
                "hdmi": 3,
                "confirm_set_top_box_power": True,
            }
        )
        return TvControlResult(
            "input_switch",
            "Switching to Sky on HDMI 3.",
            True,
            detail,
        )
    return TvControlResult(
        "input_switch",
        "I could not switch to Sky on HDMI 3.",
        False,
        result.detail,
    )


def _list_installed_apps(force: bool = False) -> list[tuple[str, str]]:
    """Return [(appId, title), ...] from applicationManager/listApps."""
    global _INSTALLED_CACHE, _INSTALLED_CACHE_TS
    now = time.time()
    if (
        not force
        and _INSTALLED_CACHE
        and (now - _INSTALLED_CACHE_TS) < _INSTALLED_CACHE_TTL_SEC
    ):
        return _INSTALLED_CACHE
    apps: list[tuple[str, str]] = []
    for uri in (
        "luna://com.webos.applicationManager/listApps",
        "luna://com.webos.service.applicationManager/listApps",
        "luna://com.webos.applicationManager/dev/listApps",
    ):
        # Keep listApps off the critical path when possible; when we must call
        # it, fail fast rather than blocking voice turns for 10s.
        resp = _luna(uri, {}, timeout=4.0)
        raw = resp.get("apps") or resp.get("appList") or resp.get("installed") or []
        if not isinstance(raw, list) or not raw:
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or item.get("appId") or "").strip()
            if not aid:
                continue
            title = str(
                item.get("title")
                or item.get("appDescription")
                or item.get("name")
                or aid
            ).strip()
            apps.append((aid, title))
        if apps:
            break
    seen_ids = {app_id for app_id, _ in apps}
    for root in _APP_MANIFEST_DIRS:
        pattern = os.path.join(root, "*", "appinfo.json")
        for path in glob.glob(pattern):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    item = json.load(handle)
            except (OSError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "").strip()
            if not aid or aid in seen_ids:
                continue
            title = str(item.get("title") or item.get("name") or aid).strip()
            apps.append((aid, title))
            seen_ids.add(aid)
    _INSTALLED_CACHE = apps
    _INSTALLED_CACHE_TS = now
    if apps:
        print(
            "[voice] tv_control listApps count=%d" % len(apps),
            flush=True,
        )
    return apps


def _slug_tokens(name: str) -> list[str]:
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return [t for t in s.split() if t]


def _candidate_ids_for_name(spoken: str) -> list[str]:
    """Build plausible webOS app ids from a spoken app name."""
    raw = (spoken or "").strip()
    low = raw.lower().strip()
    # Already looks like a full app id (com.foo.bar / org.webosbrew.x / netflix)
    if re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9_\-]+)+", low):
        return [low]
    if re.fullmatch(r"[a-z][a-z0-9_\-]{1,40}", low) and "." not in low:
        # bare id like "netflix"
        base = [low]
    else:
        base = []
    tokens = _slug_tokens(raw)
    joined = "".join(tokens)
    dashed = "-".join(tokens)
    underscored = "_".join(tokens)
    candidates: list[str] = []
    for c in base + [
        joined,
        dashed,
        underscored,
        "com.%s" % joined if joined else "",
        "com.webos.app.%s" % joined if joined else "",
        "com.webosbrew.%s" % joined if joined else "",
        "org.webosbrew.%s" % joined if joined else "",
        "org.webosbrew.hbchannel" if "brew" in joined else "",
        "com.%s.app" % joined if joined else "",
    ]:
        c = (c or "").strip(".").lower()
        if c and c not in candidates:
            candidates.append(c)
    return candidates


def _fuzzy_installed_ids(spoken: str) -> list[str]:
    """Match spoken name against installed app titles/ids."""
    tokens = _slug_tokens(spoken)
    if not tokens:
        return []
    needle = "".join(tokens)
    hits: list[tuple[int, str]] = []
    for aid, title in _list_installed_apps():
        aid_l = aid.lower()
        title_l = re.sub(r"[^a-z0-9]+", "", title.lower())
        score = 0
        if needle and needle in aid_l:
            score = 100 + len(needle)
        elif needle and needle in title_l:
            score = 90 + len(needle)
        else:
            matched = sum(1 for t in tokens if t in aid_l or t in title_l)
            if matched == len(tokens) and matched > 0:
                score = 50 + matched * 10
            elif matched >= max(1, len(tokens) - 0) and len(tokens) == 1:
                score = 40
        if score > 0:
            hits.append((score, aid))
    hits.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    for _, aid in hits:
        if aid not in out:
            out.append(aid)
        if len(out) >= 8:
            break
    return out


def _safe_installed_app(app_id: str, title: str) -> bool:
    app_id_l = app_id.lower()
    title_l = title.lower()
    if app_id_l in _UNSAFE_LAUNCH_APP_IDS:
        return False
    if any(marker in app_id_l for marker in _UNSAFE_LAUNCH_ID_MARKERS):
        return False
    if title_l in {"", "system ui", "voice agent"}:
        return False
    return True


def _strip_launch_suffixes(spoken: str) -> str:
    cleaned = re.sub(
        r"\s+(?:app|application|launcher)\s*$",
        "",
        (spoken or "").strip(),
        flags=re.I,
    ).strip()
    return cleaned


def _installed_app_matches(spoken: str) -> list[tuple[str, str, int]]:
    spoken = _strip_launch_suffixes(spoken)
    tokens = _slug_tokens(spoken)
    if not tokens:
        return []
    needle = "".join(tokens)
    hits: list[tuple[int, str, str]] = []
    for app_id, title in _list_installed_apps():
        app_id_l = app_id.lower()
        if not _safe_installed_app(app_id, title):
            continue
        title_slug = "".join(_slug_tokens(title))
        id_slug = "".join(_slug_tokens(app_id_l))
        score = 0
        if needle == title_slug:
            score = 400
        elif title_slug.startswith(needle) or needle.startswith(title_slug):
            score = 330 + min(len(needle), len(title_slug))
        elif len(needle) >= 4 and needle in title_slug:
            score = 280 + len(needle)
        elif len(needle) >= 5 and needle in id_slug:
            score = 220 + len(needle)
        elif all(token in title_slug or token in id_slug for token in tokens):
            score = 180 + len(tokens)
        if score:
            hits.append((score, app_id, title))
    hits.sort(key=lambda item: (-item[0], item[2].lower(), item[1]))
    return [(app_id, title, score) for score, app_id, title in hits]


def _prefer_brand_default(
    spoken: str, matches: list[tuple[str, str, int]]
) -> Optional[tuple[str, str, int]]:
    """When bare brand matches several apps, pick a sensible default.

    Example: spoken \"bbc\" / \"b b c\" hits BBC iPlayer and BBC Sounds with
    near-equal scores — prefer video (iPlayer) unless the user said Sounds.
    """
    if not matches:
        return None
    low = re.sub(r"[^a-z0-9]+", "", (spoken or "").lower())
    if not low:
        return None
    # Explicit sounds / radio → BBC Sounds
    if "sound" in low or "radio" in low:
        for m in matches:
            if "sound" in m[0].lower() or "sound" in m[1].lower():
                return m
    # Bare BBC / iPlayer → prefer iPlayer over Sounds
    if low in ("bbc", "iplayer", "bbciplayer") or low.startswith("bbc"):
        for m in matches:
            aid_l = m[0].lower()
            title_l = m[1].lower()
            if "iplayer" in aid_l or "iplayer" in title_l or "i player" in title_l:
                return m
        # Fall through: first non-Sounds BBC app if any
        for m in matches:
            if "sound" not in m[0].lower() and "sound" not in m[1].lower():
                return m
    return None


def _launch_installed_app(spoken: str) -> Optional[TvControlResult]:
    matches = _installed_app_matches(spoken)
    if not matches:
        return None
    app_id, title, score = matches[0]
    if score < 220:
        return None
    if len(matches) > 1 and matches[1][2] >= score - 5:
        preferred = _prefer_brand_default(spoken, matches)
        if preferred is not None:
            app_id, title, score = preferred
            print(
                "[voice] tv_control installed app brand-default text=%r -> %s (%s)"
                % (spoken[:60], title, app_id),
                flush=True,
            )
        else:
            print(
                "[voice] tv_control installed app ambiguous text=%r: %s / %s"
                % (spoken[:60], title, matches[1][1]),
                flush=True,
            )
            return None
    return TvControlResult(
        "app_launch",
        f"Opening {title}.",
        True,
        {
            "id": app_id,
            "ids": [app_id],
            "deferred": True,
            "spoken": title,
            "installed": True,
        },
    )


def resolve_app_launch(name_key: str, spoken_name: str) -> tuple[str, list[str]]:
    """Return (pretty_name, ordered app ids).

    Known apps (netflix/youtube/…) use static alias IDs first so early silent
    launch never waits on listApps. Installed-title matching only runs when the
    cache is already warm, or for free-form names.
    """
    ids: list[str] = []
    static = list(_APP_IDS.get(name_key) or [])
    cache_warm = bool(
        _INSTALLED_CACHE and (time.time() - _INSTALLED_CACHE_TS) < _INSTALLED_CACHE_TTL_SEC
    )

    # Prefer warm-cache installed match when available (correct regional id).
    if cache_warm:
        installed = _launch_installed_app(spoken_name)
        if installed is not None:
            app_id = str(installed.detail.get("id") or "")
            if app_id:
                ids.append(app_id)

    for app_id in static:
        if app_id not in ids:
            ids.append(app_id)

    # Free-form only: require a confident installed-app match (may call listApps).
    if name_key not in _APP_IDS and not ids:
        installed = _launch_installed_app(spoken_name)
        if installed is not None:
            app_id = str(installed.detail.get("id") or "")
            if app_id:
                ids.append(app_id)
    return spoken_name, ids


def launch_app_id(
    app_id: str,
    *,
    spoken_name: str = "app",
    params: Optional[dict[str, Any]] = None,
    no_splash: bool = False,
) -> TvControlResult:
    """Single-shot Luna launch for a concrete app id (fast).

    Default ``no_splash=False`` so the app takes visible foreground under
    a launcher web app (noSplash=true has been observed to start apps while
    LSM-visible:false — "launch X" returns ok but the user still sees Home).
    """
    app_id = (app_id or "").strip()
    if not app_id:
        return TvControlResult(
            "app_launch",
            f"I could not open {spoken_name}.",
            False,
            {},
        )
    launch_params = dict(params or {})
    payload: dict[str, Any] = {
        "id": app_id,
        "params": launch_params,
    }
    if no_splash:
        payload["noSplash"] = True
    print(
        "[voice] tv_control launch id=%s params=%s noSplash=%s"
        % (app_id, launch_params, no_splash),
        flush=True,
    )
    # One primary URI only — softEmpty is success for /launch on this TV.
    # Short timeout: app handoff should not stall on a hung luna-send.
    last = _luna(
        "luna://com.webos.applicationManager/launch",
        payload,
        timeout=2.5,
    )
    print("[voice] tv_control launch result %s" % last, flush=True)
    if _ok(last) or last.get("softEmpty"):
        return TvControlResult(
            "app_launch",
            f"Opening {spoken_name}.",
            True,
            {"id": app_id, "params": launch_params, "resp": last},
        )
    # One fallback URI
    last = _luna(
        "luna://com.webos.service.applicationManager/launch",
        payload,
        timeout=2.5,
    )
    print("[voice] tv_control launch fallback %s" % last, flush=True)
    ok = _ok(last) or bool(last.get("softEmpty"))
    return TvControlResult(
        "app_launch",
        f"Opening {spoken_name}." if ok else f"I could not open {spoken_name}.",
        ok,
        {"id": app_id, "params": launch_params, "resp": last},
    )


def _launch_app(name_key: str, spoken_name: str, *, defer: bool = True) -> TvControlResult:
    """Resolve app ids; by default do not Luna-launch yet (daemon closes overlay first)."""
    pretty, ids = resolve_app_launch(name_key, spoken_name)
    if not ids:
        return TvControlResult(
            "app_launch",
            f"I could not open {pretty}. It may not be installed.",
            False,
            {},
        )
    # Defer actual launch so voice card can close first — avoids slow double launch
    # and overlay-on-top of Netflix.
    if defer:
        return TvControlResult(
            "app_launch",
            f"Opening {pretty}.",
            True,
            {"id": ids[0], "ids": ids, "deferred": True, "spoken": pretty},
        )
    return launch_app_id(ids[0], spoken_name=pretty)


def _captions(on: bool) -> TvControlResult:
    # Caption keys vary by region/firmware; try common ones.
    settings_variants = [
        {"category": "caption", "settings": {"captionEnable": "on" if on else "off"}},
        {"category": "caption", "settings": {"captionEnable": on}},
        {"category": "option", "settings": {"captionEnable": "on" if on else "off"}},
        {"category": "caption", "settings": {"closedCaption": "on" if on else "off"}},
    ]
    last: dict[str, Any] = {}
    for payload in settings_variants:
        last = _luna("luna://com.webos.settingsservice/setSystemSettings", payload)
        if _ok(last):
            return TvControlResult(
                "captions",
                "Subtitles on." if on else "Subtitles off.",
                True,
                last,
            )
    return TvControlResult(
        "captions",
        "I could not change the subtitle setting on this TV.",
        False,
        last,
    )


def _sleep_timer(minutes: int) -> TvControlResult:
    minutes = max(1, min(180, int(minutes)))
    # Best-effort: many firmwares expose a timer service; fall back to spoken ack.
    last = _first_ok(
        [
            (
                "luna://com.webos.service.tvpower/power/setPowerOffTimer",
                {"time": minutes * 60},
            ),
            (
                "luna://com.webos.settingsservice/setSystemSettings",
                {"category": "option", "settings": {"timerPowerOff": str(minutes)}},
            ),
        ]
    )
    if _ok(last):
        return TvControlResult(
            "sleep_timer",
            f"Sleep timer set for {minutes} minutes.",
            True,
            last,
        )
    return TvControlResult(
        "sleep_timer",
        f"I could not set a sleep timer for {minutes} minutes on this TV.",
        False,
        last,
    )


def _app_key_from_phrase(phrase: str) -> tuple[str, str]:
    p = phrase.lower().replace(" ", "")
    pl = phrase.lower().strip()
    if "netflix" in p:
        return "netflix", "Netflix"
    if "youtube" in p or "youtub" in p:
        return "youtube", "YouTube"
    if "disney" in p:
        return "disney", "Disney Plus"
    # Bare "prime" / "amazon" / "amazon prime" → Prime Video app
    if "prime" in p or "amazon" in p:
        return "prime", "Prime Video"
    if "hulu" in p:
        return "hulu", "Hulu"
    if "max" in p or "hbo" in p:
        return "max", "Max"
    if "apple" in p:
        return "apple", "Apple TV"
    if "spotify" in p:
        return "spotify", "Spotify"
    if "browser" in p:
        return "browser", "the browser"
    if (
        "terminal" in p
        or "termianl" in p
        or "termanal" in p
        or "termnal" in p
        or "shell" in p
        or "console" in p
    ):
        return "terminal", "Terminal"
    if "brew" in p or "hbchannel" in p:
        return "brew", "Homebrew Channel"
    if "livetv" in p or "live tv" in pl:
        return "livetv", "Live TV"
    if "gallery" in p or "photo" in p:
        return "gallery", "Photos"
    if "music" in p:
        return "music", "Music"
    if "plex" in p:
        return "plex", "Plex"
    if "twitch" in p:
        return "twitch", "Twitch"
    if "paramount" in p:
        return "paramount", "Paramount Plus"
    if "peacock" in p:
        return "peacock", "Peacock"
    if "crunchyroll" in p:
        return "crunchyroll", "Crunchyroll"
    if "tubi" in p:
        return "tubi", "Tubi"
    # BBC: bare "bbc" / "iplayer" / "bbc iplayer" → iPlayer; "bbc sounds" → Sounds
    if "sounds" in p and "bbc" in p:
        return "bbcsounds", "BBC Sounds"
    if "iplayer" in p or p in ("bbc", "bbciplayer"):
        return "bbc", "BBC iPlayer"
    if "channel4" in p or "all4" in p or pl in ("channel 4", "all 4"):
        return "channel4", "Channel 4"
    if "itvx" in p or p == "itv" or pl.startswith("itv "):
        return "itv", "ITVX"
    if "channel5" in p or "my5" in p or pl in ("channel 5", "my 5"):
        return "channel5", "Channel 5"
    # Free-form: pretty spoken name, key = slug for candidate generation
    pretty = re.sub(r"\s+", " ", phrase.strip())
    if pretty:
        pretty = pretty[0].upper() + pretty[1:]
    return "".join(_slug_tokens(phrase)) or pl, pretty or phrase


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def handle_tv_command(text: str) -> Optional[TvControlResult]:
    """If `text` is a confident TV-control command, execute it and return a result.

    Returns None when the utterance should be handled by weather/Grok instead.
    """
    t = normalize_control_transcript(text)
    if not t:
        return None
    if len(t) > 80 and sum(marker in t for marker in _STT_KEYTERM_ECHO_MARKERS) >= 5:
        print(
            "[voice] tv_control rejected STT keyterm echo: %r" % t[:100],
            flush=True,
        )
        return None

    if t in _MUTE_STT_ALIASES:
        print(
            "[voice] tv_control STT alias text=%r -> mute" % t,
            flush=True,
        )
        return _mute(True)

    if _SKY.match(t) or t in _SKY_STT_ALIASES:
        if t in _SKY_STT_ALIASES:
            print(
                "[voice] tv_control STT alias text=%r -> go to Sky" % t,
                flush=True,
            )
        return _open_sky()

    hdmi_direct = _HDMI_DIRECT.match(t)
    if hdmi_direct:
        return _switch_input(
            "HDMI %s" % hdmi_direct.group(1),
            int(hdmi_direct.group(1)),
        )
    hdmi_stt_number = _HDMI_STT_ALIASES.get(t)
    if hdmi_stt_number is not None:
        return _switch_input(
            "HDMI %d" % hdmi_stt_number,
            hdmi_stt_number,
        )

    youtube_search = _YOUTUBE_SEARCH.match(t)
    if youtube_search:
        query = youtube_search.group(1).strip()
        if query:
            pretty, ids = resolve_app_launch("youtube", "YouTube")
            if ids:
                params = {
                    "target": "q=%s"
                    % urllib.parse.quote(query, safe=""),
                }
                return TvControlResult(
                    "app_launch",
                    "Searching YouTube for %s." % query,
                    True,
                    {
                        "id": ids[0],
                        "ids": ids,
                        "deferred": True,
                        "spoken": pretty,
                        "params": params,
                        "action": "search",
                        "query": query,
                    },
                )

    prime_search = _PRIME_SEARCH.match(t)
    if prime_search:
        query = prime_search.group(1).strip()
        if query:
            pretty, ids = resolve_app_launch("prime", "Prime Video")
            if ids:
                params = {
                    "contentTarget": {
                        "intent": "Search",
                        "intentParam": query,
                        "languageCode": "en-GB",
                    }
                }
                return TvControlResult(
                    "app_launch",
                    "Searching Prime Video for %s." % query,
                    True,
                    {
                        "id": ids[0],
                        "ids": ids,
                        "deferred": True,
                        "spoken": pretty,
                        "params": params,
                        "action": "search",
                        "query": query,
                        "native_lifecycle": True,
                    },
                )

    if _SETTINGS.search(t):
        # "open settings" → LG Quick Settings (gear panel). That is a system
        # UI opened via QMENU; SAM launch of com.palm.app.settings often paints
        # LSM visible:true then is dismissed by compositor/guardian thrash.
        # Deep links ("picture/sound/network settings", "all settings") still
        # open the full Settings overlay with an explicit panel target.
        deep = re.search(
            r"\b(?:all|full|picture|sound|network|general)\s+settings?\b",
            t,
        )
        if not deep and not re.search(r"\b(?:picture|sound|network|general)\b", t):
            return TvControlResult(
                "app_launch",
                "Opening Settings.",
                True,
                {
                    "id": "com.webos.app.quicksettings",
                    "ids": ["com.webos.app.quicksettings"],
                    "deferred": True,
                    "spoken": "Settings",
                    "system_overlay": True,
                    "open_via": "qmenu",
                },
            )
        target = "picture"
        if re.search(r"\bsound\b", t):
            target = "sound"
        elif re.search(r"\bnetwork\b", t):
            target = "network"
        elif re.search(r"\bgeneral\b", t):
            target = "general"
        return TvControlResult(
            "app_launch",
            "Opening Settings.",
            True,
            {
                "id": "com.palm.app.settings",
                "ids": ["com.palm.app.settings"],
                "deferred": True,
                "spoken": "Settings",
                "params": {"target": target},
                "system_overlay": True,
                "no_splash": False,
                "open_via": "settings_app",
            },
        )

    # Volume/mute BEFORE app launch — "set volume to max" must not open an
    # app titled Max. App launch still runs before chat (see below).
    if _UNMUTE.search(t):
        return _mute(False)
    if _MUTE.search(t) and "unmute" not in t:
        return _mute(True)

    m = _VOL_SET.search(t)
    if m:
        raw_level = next((g for g in m.groups() if g is not None), None)
        level = _parse_volume_level(raw_level or "")
        if level is not None:
            print(
                "[voice] tv_control volume_set raw=%r level=%d text=%r"
                % (raw_level, level, t[:60]),
                flush=True,
            )
            return _volume_set(level)
    if _VOL_UP.search(t) and not _VOL_SET.search(t):
        return _volume_up()
    if _VOL_DOWN.search(t) and not _VOL_SET.search(t):
        return _volume_down()

    # App launch before other TV controls / chat. Logs showed "Amazon Prime."
    # falling through to Grok while the user expected a silent app open.
    app = _try_app_launch_from_text(t)
    if app is not None:
        return app

    if not is_tv_control_query(text):
        return None

    if _POWER_OFF.search(t):
        return _power_off(screen_only=False)
    if _SCREEN_OFF.search(t):
        return _power_off(screen_only=True)

    m = _SLEEP.search(t)
    if m:
        mins = next(g for g in m.groups() if g is not None)
        return _sleep_timer(int(mins))

    if _CAPTIONS_ON.search(t):
        return _captions(True)
    if _CAPTIONS_OFF.search(t):
        return _captions(False)

    if _CH_UP.search(t):
        return _channel_step(True)
    if _CH_DOWN.search(t):
        return _channel_step(False)
    m = _CH_SET.search(t)
    if m:
        return _channel_set(m.group(1))

    m = _INPUT.search(t) or (
        _INPUT_BARE.search(t) if re.match(r"^(switch|change|go|set)\b", t) else None
    )
    if m:
        raw = m.group(0)
        # Extract HDMI number if present
        hm = re.search(r"hdmi\s*(\d)", t, re.I)
        if hm:
            return _switch_input(raw, int(hm.group(1)))
        if re.search(r"live\s*tv|antenna|cable|tuner", t, re.I):
            return _switch_input("live tv")
        if re.search(r"\bav\b", t, re.I):
            return _switch_input("av")
        if "component" in t:
            return _switch_input("component")

    if _HOME.search(t):
        # Deferred silent handoff — open Home without voice card confirmation UI.
        return TvControlResult(
            "app_launch",
            "Opening Home.",
            True,
            {
                "id": "com.webos.app.home",
                "ids": ["com.webos.app.home"],
                "deferred": True,
                "spoken": "Home",
            },
        )

    return None


# App names we treat as "open this" when STT mangles "launch" (punch/lunch/…).
# Bare "prime" is the same app as "amazon prime" / "prime video".
_APP_NAME_RE = re.compile(
    r"\b("
    r"netflix|youtube|you\s*tube|disney(?:\s*plus|\+)?|"
    r"amazon\s*prime(?:\s*video)?|prime\s*video|amazon\s*video|"
    r"prime|amazon|"
    r"hulu|hbo(?:\s*max)?|\bmax\b|apple\s*tv|"
    r"spotify|plex|twitch|paramount(?:\s*plus)?|peacock|crunchyroll|tubi|"
    r"bbc(?:\s*iplayer|\s*i\s*player|\s*sounds)?|iplayer|"
    r"channel\s*4|all\s*4|itvx?|channel\s*5|my\s*5|"
    r"brew|home\s*brew|homebrew(?:\s*channel)?|"
    r"terminal|termianl|termanal|termnal|webos\s*terminal|shell|console|"
    r"browser"
    r")\b",
    re.I,
)
# Verbs / STT near-misses for "launch|open|start|run" — open and launch are equal.
_LAUNCH_VERB_RE = re.compile(
    r"\b(?:"
    r"open|launch|start|run|play|go\s+to|load|show|put\s+on|fire\s+up|bring\s+up|"
    r"opened|opening|launched|launching|starting|"
    r"lunch|lounge|large|last|punch|plunch|lanch|lauch|launce|ranch|"
    r"bunch|munch|flush|flash|hoping|opem|opn"
    r")\b",
    re.I,
)

# Explicit open/launch/start + Prime (and STT near-misses) → same app always.
_PRIME_LAUNCH_RE = re.compile(
    r"(?i)\b(?:"
    r"open|launch|start|run|play|go\s+to|load|show|put\s+on|"
    r"lunch|punch|lanch|lauch|launce|hoping|opened|launched"
    r")\b\s+"
    r"(?:the\s+|a\s+|an\s+)?"
    r"(?:"
    r"amazon\s*prime(?:\s*video)?|"
    r"prime\s*video|"
    r"prime|"
    r"amazon"
    r")\b",
)

# "launch terminal" / STT near-misses (termanal / lunch terminal).
_TERMINAL_LAUNCH_RE = re.compile(
    r"(?i)\b(?:"
    r"open|launch|start|run|go\s+to|load|show|"
    r"lunch|punch|lanch|lauch|launce|opened|launched"
    r")\b\s+"
    r"(?:the\s+|a\s+|an\s+)?"
    r"(?:webos\s*)?"
    r"(?:terminal|termianl|termanal|termnal|shell|console)\b",
)

# BBC iPlayer — STT often drops "BBC" or mangles to "see/pc i player".
_BBC_IPLAYER_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:open|launch|start|run|go\s+to|load|show|lunch|punch|lanch|lauch|"
    r"launce|opened|launched)\s+(?:the\s+|a\s+|an\s+)?"
    r")?"
    r"(?:"
    r"bbc(?:\s*iplayer|\s*i\s*player|\s*sounds)?|"
    r"iplayer|"
    r"i\s*player|"
    r"see\s*i\s*player|"
    r"see\s*i\s*play|"
    r"p\s*c\s*i\s*player|"
    r"pc\s*i\s*player|"
    r"eye\s*player"
    r")\b",
)


def _try_app_launch_from_text(t: str) -> Optional[TvControlResult]:
    """Match app launch even when STT says 'punch Netflix' instead of 'launch'.

    ``open prime`` and ``launch prime`` (and amazon prime / prime video) all
    open the same Prime Video app — same task, same silent handoff.
    """
    # Apply STT normalizations (b b c → bbc, i play → iplayer, …) first.
    t = normalize_control_transcript(t)
    if not t:
        return None
    words = t.split()
    # Questions about an app → chat, not launch ("what is netflix").
    if re.search(
        r"\b(what|who|why|how|when|where|tell|about|explain|define)\b", t
    ) and not _LAUNCH_VERB_RE.search(t):
        return None

    # Fast path: open/launch/start + prime (any wording) → Prime Video.
    if _PRIME_LAUNCH_RE.search(t):
        print(
            "[voice] tv_control app match prime verb text=%r -> prime"
            % (t[:60],),
            flush=True,
        )
        return _launch_app("prime", "Prime Video")

    if _TERMINAL_LAUNCH_RE.search(t):
        print(
            "[voice] tv_control app match terminal verb text=%r -> terminal"
            % (t[:60],),
            flush=True,
        )
        return _launch_app("terminal", "Terminal")

    # BBC iPlayer before generic app match (STT mangling is extreme).
    if _BBC_IPLAYER_RE.search(t):
        # "bbc sounds" → Sounds; bare bbc / iplayer → iPlayer.
        if re.search(r"\bsounds?\b", t, re.I) and re.search(r"\bbbc\b", t, re.I):
            print(
                "[voice] tv_control app match bbc sounds text=%r" % (t[:60],),
                flush=True,
            )
            return _launch_app("bbcsounds", "BBC Sounds")
        print(
            "[voice] tv_control app match bbc iplayer text=%r -> bbc"
            % (t[:60],),
            flush=True,
        )
        return _launch_app("bbc", "BBC iPlayer")

    m_app = _APP_NAME_RE.search(t)
    if not m_app:
        # Free-form "open/launch <something>"
        m = _APP.search(t)
        if m:
            phrase = (m.group(1) or m.group(2) or "").strip()
            phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.I).strip()
            if phrase and phrase.lower() not in (
                "the",
                "a",
                "an",
                "app",
                "application",
                "something",
                "it",
            ):
                installed_app = _launch_installed_app(phrase)
                if installed_app is not None:
                    print(
                        "[voice] tv_control installed app match "
                        "text=%r -> %s"
                        % (phrase[:60], installed_app.detail.get("id")),
                        flush=True,
                    )
                    return installed_app
                key, spoken = _app_key_from_phrase(phrase)
                result = _launch_app(key, spoken)
                return result if result.ok else None
        return None

    app_phrase = m_app.group(1)
    key, spoken = _app_key_from_phrase(app_phrase)

    # Explicit (or STT-mangled) launch verb + app name.
    if _LAUNCH_VERB_RE.search(t):
        print(
            "[voice] tv_control app match verb+app text=%r -> %s"
            % (t[:60], key),
            flush=True,
        )
        return _launch_app(key, spoken)

    # Bare app name only: "Netflix" / "YouTube" / "Prime"
    bare = re.sub(r"[?.!,]+$", "", t).strip()
    if bare == app_phrase.lower() or bare.replace(" ", "") == app_phrase.lower().replace(
        " ", ""
    ):
        return _launch_app(key, spoken)

    # Short "… Netflix" (≤4 words): treat as launch intent after KEY_VOICE.
    if len(words) <= 4 and m_app:
        print(
            "[voice] tv_control app match short text=%r -> %s" % (t[:60], key),
            flush=True,
        )
        return _launch_app(key, spoken)

    return None


def open_quick_settings() -> TvControlResult:
    """Open LG Quick Settings (gear) via networkinput QMENU special key."""
    # QMENU is the reliable path on this OLED; SAM has no installable app id
    # for com.webos.app.quicksettings (system UI only).
    last: dict[str, Any] = {}
    for key in ("QMENU", "MENU"):
        last = _luna(
            "luna://com.webos.service.networkinput/sendSpecialKey",
            {"key": key},
            timeout=3.0,
        )
        print(
            "[voice] tv_control sendSpecialKey %s -> %s" % (key, last),
            flush=True,
        )
        if _ok(last) or last.get("softEmpty"):
            return TvControlResult(
                "app_launch",
                "Opening Settings.",
                True,
                {"id": "com.webos.app.quicksettings", "via": key, "resp": last},
            )
    # Fallback: full Settings app with picture panel.
    return launch_app_id(
        "com.palm.app.settings",
        spoken_name="Settings",
        params={"target": "picture"},
        no_splash=False,
    )


def silent_tv_command_kind(text: str) -> Optional[str]:
    """Classify silent TV intents without executing Luna (safe for STT preview).

    Returns kind for commands that should not paint overlay status/transcript
    cards: app_launch, mute/volume, HDMI/Sky input switch. Returns None if the
    text is not a silent control (or is not TV control at all).
    """
    t = normalize_control_transcript(text)
    if not t:
        return None
    if len(t) > 80 and sum(marker in t for marker in _STT_KEYTERM_ECHO_MARKERS) >= 5:
        return None
    if t in _MUTE_STT_ALIASES:
        return "mute"
    if _UNMUTE.search(t):
        return "unmute"
    if _MUTE.search(t) and "unmute" not in t:
        return "mute"
    m = _VOL_SET.search(t)
    if m:
        raw = next((g for g in m.groups() if g is not None), None)
        if _parse_volume_level(raw or "") is not None:
            return "volume_set"
    if _VOL_UP.search(t) and not _VOL_SET.search(t):
        return "volume_up"
    if _VOL_DOWN.search(t) and not _VOL_SET.search(t):
        return "volume_down"
    if _SKY.match(t) or t in _SKY_STT_ALIASES:
        return "input_switch"
    if _HDMI_DIRECT.match(t) or _HDMI_STT_ALIASES.get(t) is not None:
        return "input_switch"
    if _YOUTUBE_SEARCH.match(t) or _PRIME_SEARCH.match(t):
        return "app_launch"
    if _SETTINGS.search(t):
        return "app_launch"
    # Fast classify known app launches without listApps / full resolve.
    if _APP_NAME_RE.search(t) and (
        _LAUNCH_VERB_RE.search(t) or len(t.split()) <= 4
    ):
        return "app_launch"
    if _PRIME_LAUNCH_RE.search(t) or _TERMINAL_LAUNCH_RE.search(t):
        return "app_launch"
    # Free-form only — may warm-cache listApps; avoid on every partial.
    if _APP.search(t) and _try_app_launch_from_text(t) is not None:
        return "app_launch"
    if not is_tv_control_query(text):
        return None
    m = _INPUT.search(t) or (
        _INPUT_BARE.search(t) if re.match(r"^(switch|change|go|set)\b", t) else None
    )
    if m:
        return "input_switch"
    if _HOME.search(t):
        return "app_launch"
    return None


def try_handle(text: str) -> Optional[str]:
    """Convenience for the daemon: return spoken confirmation text, or None."""
    result = handle_tv_command(text)
    if result is None:
        return None
    if not result.ok:
        print(
            "[voice] tv_control %s failed: %s" % (result.kind, result.detail),
            flush=True,
        )
        # Still speak the failure so the user knows we tried.
    else:
        print("[voice] tv_control %s ok" % result.kind, flush=True)
    return result.spoken

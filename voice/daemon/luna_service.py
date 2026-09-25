#!/usr/bin/env python3
"""Luna bridge: launch overlay, suppress native AI, broadcast session events."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Optional

SOCKET_PATH = "/var/run/launch-home-voice.sock"
WS_HOST = "127.0.0.1"
WS_PORT = 8678
# Name returned to a "hello" request: Launch Home only talks to a socket that
# identifies as its own voice service (never to another project on a port).
SERVICE_NAME = "launch-home-voice"
# Flag file clients may poll for an instant mic badge without waiting
# for WebSocket delivery. Written on every listening enter/exit.
VOICE_STATE_PATH = "/tmp/launch-home-voice-state.json"
# Upper bound on concurrent unix event subscribers. Only the single Luna bridge
# should ever be connected; anything beyond this is a reconnect leak, so we
# evict the oldest to protect the daemon's thread budget.
MAX_UNIX_CLIENTS = 16
# Launch Home and the voice card share the WS port; each says which it is with
# a "hello" request. Older single-client mode closed every prior socket on
# connect, which kicked a client's mic badge off right as sessionStarted was
# broadcast. Cap still limits reconnect leaks; evict oldest when over.
MAX_WS_CLIENTS = 8
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_handshake(conn: socket.socket) -> bool:
    """Perform the RFC6455 server handshake. Returns True on success."""
    try:
        conn.settimeout(5.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(1024)
            if not chunk:
                return False
            data += chunk
            if len(data) > 16384:
                return False
        key = None
        origin = ""
        for line in data.decode("latin-1").split("\r\n"):
            low = line.lower()
            if low.startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
            elif low.startswith("origin:"):
                origin = line.split(":", 1)[1].strip().lower()
        if not key:
            return False
        # Only apps on this TV (file:// or null origin) may connect. A web page
        # in the TV browser could otherwise read the API keys or change settings.
        if origin.startswith("http://") or origin.startswith("https://"):
            return False
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("latin-1")).digest()
        ).decode("latin-1")
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: " + accept + "\r\n\r\n"
        )
        conn.sendall(resp.encode("latin-1"))
        conn.settimeout(None)
        return True
    except OSError:
        return False


def _ws_frame(text: str) -> bytes:
    """Build a single unfragmented text frame (server->client, unmasked)."""
    payload = text.encode("utf-8")
    n = len(payload)
    header = bytearray()
    header.append(0x81)  # FIN + text opcode
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


def _ws_recv_exact(conn: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_read_frame(conn: socket.socket) -> Optional[tuple[int, bytes]]:
    """Read one client->server frame. Returns (opcode, payload) or None on EOF.

    Client frames are masked per RFC6455 §5.3; we XOR the payload with the
    4-byte masking key. Handles 7-bit, 16-bit and 64-bit length forms.
    """
    hdr = _ws_recv_exact(conn, 2)
    if not hdr:
        return None
    opcode = hdr[0] & 0x0F
    masked = (hdr[1] & 0x80) != 0
    length = hdr[1] & 0x7F
    if length == 126:
        ext = _ws_recv_exact(conn, 2)
        if not ext:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = _ws_recv_exact(conn, 8)
        if not ext:
            return None
        length = struct.unpack(">Q", ext)[0]
    mask = b""
    if masked:
        mask = _ws_recv_exact(conn, 4)
        if mask is None:
            return None
    payload = b""
    if length:
        payload = _ws_recv_exact(conn, length)
        if payload is None:
            return None
    if masked and mask:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload


def _ws_control_frame(opcode: int, payload: bytes = b"") -> bytes:
    """Build a control frame (pong/close). Control payloads are <=125 bytes."""
    payload = payload[:125]
    return bytes([0x80 | (opcode & 0x0F), len(payload)]) + payload


# Native voice assistants / result UIs that the TV may launch when KEY_VOICE is
# pressed. We close all of these so voice card is the only thing shown.
NATIVE_AI_APPS = [
    "com.webos.app.aiplatform",   # LG concierge / AI platform ("content you'll like")
    "com.webos.app.aiplatformsupport",  # AI platform support surface
    "amazon.alexa.view",          # Amazon Alexa (full view)
    "amazon.alexapr",             # Amazon Alexa (proactive/preview popup)
    "com.webos.app.voiceagent",   # thinQ "Listening…" bar (SystemUI, not renderer)
    "com.webos.app.assistant",    # LG assistant
    "google.assistant",           # Google Assistant voice result
    "com.webos.app.dangbei-overlay",  # regional voice-result overlay
    "com.webos.app.voice",        # native voice search app (different from voiceagent)
    "com.webos.app.voiceweb",     # voice web UI
    "com.webos.app.browser",      # classic web-search result card
    # Newer webOS: voice.performer.plugin opens Bean Browser as an OVERLAY
    # ~5–7s after KEY_VOICE (seen in SAM NL_APP_LAUNCH_BEGIN). Without this
    # the user lands in a full browser instead of voice card.
    "com.webos.app.beanbrowser",
    "com.webos.app.chromebrowser",
]

# Full-screen / card UIs that must die on every KEY_VOICE (not SystemUI bars).
NATIVE_RESULT_KILL_IDS = (
    "com.webos.app.aiplatform",
    "com.webos.app.aiplatformsupport",
    "com.webos.app.voice",
    "com.webos.app.voiceweb",
    "com.webos.app.assistant",
    "amazon.alexa.view",
    "amazon.alexapr",
    "google.assistant",
    "com.webos.app.dangbei-overlay",
)

# Closing these mid-session has been observed to drop voice card's WebSocket
# (0 clients) so answers never reach the TV. Only close browsers at session
# start (once) and after session end — never while a voice turn is live.
BROWSER_CLOSE_IDS = frozenset(
    {
        "com.webos.app.browser",
        "com.webos.app.beanbrowser",
        "com.webos.app.chromebrowser",
    }
)

# Launch Home itself: a voice turn started over it hands the screen back to it.
LAUNCHER_APP_ID = "org.webosbrew.lounge.launcher"

VOICE_RESULT_APP_IDS = frozenset(
    {
        "com.webos.app.aiplatform",
        "com.webos.app.aiplatformsupport",
        "amazon.alexa.view",
        "amazon.alexapr",
        "google.assistant",
        "com.webos.app.dangbei-overlay",
        "com.webos.app.voiceweb",
        "com.webos.app.assistant",
        "com.webos.app.voice",
    }
)

# Bottom-left "Listening…" / LG AI utterance bar — launched via sysuicompmgr,
# NOT WebAppMgr. closeByAppId and renderer kill do not dismiss it.
SYSTEM_UI_VOICE_APPS = (
    "com.webos.app.voiceagent",
    # Some firmwares surface a second SystemUI strip for AI / thinQ.
    "com.webos.app.assistant",
    # Some webOS builds paint a thinQ / AI tip via the same manager.
    "com.webos.app.voice",
    # Yes/No: "LG voice recognition feature is turned off?"
    "com.webos.app.alert",
    # Toast: "LG voice recognition feature has been turned on."
    "com.webos.app.toast",
)


def _luna_send(uri: str, payload: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
    cmd = [
        "luna-send",
        "-f",
        "-n",
        "1",
        "-w",
        str(int(timeout * 1000)),
        uri,
        json.dumps(payload, separators=(",", ":")),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, timeout=timeout + 2)
        return json.loads(out) if out.strip() else {}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


class LunaBridge:
    """Push overlay events and manage app lifecycle via Luna + Unix socket."""

    def __init__(
        self,
        overlay_app_id: str = "org.webosbrew.lounge.voice",
        *,
        close_native: bool = True,
        native_close_poll_sec: float = 3.0,
        native_app_ids: Optional[list[str]] = None,
        close_voiceagent_during_capture: bool = True,
        socket_path: str = SOCKET_PATH,
    ) -> None:
        self.overlay_app_id = overlay_app_id
        self.close_native = close_native
        self.native_close_poll_sec = native_close_poll_sec
        self.native_app_ids = native_app_ids or list(NATIVE_AI_APPS)
        self.close_voiceagent_during_capture = close_voiceagent_during_capture
        self.socket_path = socket_path
        self._server: Optional[socket.socket] = None
        self._clients: list[socket.socket] = []
        self._ws_server: Optional[socket.socket] = None
        self._ws_clients: list[socket.socket] = []
        # Role each client gave in "hello": "overlay" (the voice card) or
        # "launcher" (Launch Home). Only the card counts as a connected overlay.
        self._ws_roles: dict[socket.socket, str] = {}
        self._ws_listening = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._ws_thread: Optional[threading.Thread] = None
        # While True, the mic is live: suppress all native-app closing so
        # voiceconductor does not EOF the capture socket mid-utterance.
        self.capture_active = False
        # While True, do NOT re-launch the overlay if WS drops (mid capture/STT).
        # Reclaim during STT caused a black full-screen flash on KEY_VOICE release.
        self.suppress_reclaim = False
        # After "launch Netflix" (etc.), block all voice card re-launches until
        # the next KEY_VOICE so reclaim cannot pull our card over the app.
        self._app_handoff_until = 0.0
        # Persistent voiceagent-suppression window. The bottom-left LG
        # "Listening…" bar (com.webos.app.voiceagent) can appear anywhere from
        # press to several seconds after the answer. A single short post-release
        # poll misses it. Instead we keep a suppression window open from session
        # start until a few seconds after session end and dismiss the bar on a
        # steady cadence — but ONLY while the mic is idle (closing it during
        # live capture EOFs the voiceinput socket).
        self._suppress_deadline = 0.0
        self._suppress_lock = threading.Lock()
        # LG's own voice search can open its browser a few seconds after a
        # voice press, so browsers may be closed only in a window after a
        # press (see _arm_browser_close). Never at daemon start, never at idle,
        # and never when the user was already in the browser.
        self._browser_close_until = 0.0
        # Voice pressed while Launch Home was in front: bring it back when the
        # card closes (closing the card alone makes webOS show LG's home).
        self._return_to_launcher = False
        # Optional callback: (method: str, params: dict) -> dict. Handles
        # settings RPCs (getConfig/setConfig/getApiKey/getStatus) that the
        # sandboxed overlay app sends over the WS channel, bypassing the ACG
        # private bus that blocks its non-root luna-send calls. Set by the
        # daemon after construction. Raising in the callback yields an error
        # reply to the client.
        self.config_handler: Optional[Any] = None
        # Voice session state for late WS subscribers (app often connects after
        # sessionStarted was already broadcast during cold launch).
        self._voice_session_live = False
        self._last_transcript = ""
        self._last_answer = ""
        self._last_status = ""
        self._last_answer_source: Optional[dict[str, Any]] = None
        # Generation counter cancels stale close_overlay timers from a previous
        # turn so they cannot hide the card mid next question.
        self._session_ui_gen = 0
        self._close_timer: Optional[threading.Timer] = None

    def start(self) -> None:
        if self._server is not None:
            return
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o666)
        self._server.listen(8)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self._start_ws_server()
        threading.Thread(target=self._voiceagent_watch_loop, daemon=True).start()

    def engage_voiceagent_suppression(self, seconds: float = 30.0) -> None:
        """Open/extend the window during which the Listening bar is dismissed."""
        with self._suppress_lock:
            self._suppress_deadline = max(
                self._suppress_deadline, time.time() + seconds
            )

    def burst_dismiss_voice_ui(self, seconds: float = 8.0) -> None:
        """Hammer-close LG/Amazon voice + concierge UIs for several seconds.

        KEY_VOICE races: sysuicompmgr paints the Listening bar and SAM may
        launch aiplatform ("content you'll like") / Alexa within 0.3–2s. A
        single close loses that race; rapid closes keep voice card exclusive.
        """
        secs = max(2.0, float(seconds))

        def _close_system_ui_burst() -> None:
            end = time.time() + secs
            n = 0
            while time.time() < end:
                try:
                    for app_id in SYSTEM_UI_VOICE_APPS:
                        self.close_system_ui(app_id)
                    self.close_system_ui("com.webos.app.alert")
                    self.close_system_ui("com.webos.app.toast")
                except Exception:
                    pass
                n += 1
                time.sleep(0.06 if n < 25 else 0.18)

        def _close_native_results() -> None:
            end = time.time() + secs
            while time.time() < end:
                # Kill result cards even during capture — these are not the
                # mic path; only voiceagent close mid-capture EOFs the socket.
                try:
                    for app_id in NATIVE_RESULT_KILL_IDS:
                        _luna_send(
                            "luna://com.webos.applicationManager/closeByAppId",
                            {"id": app_id},
                            timeout=0.55,
                        )
                        _luna_send(
                            "luna://com.webos.applicationManager/close",
                            {"id": app_id},
                            timeout=0.45,
                        )
                except Exception:
                    pass
                # SystemUI bars every tick too (dual path).
                try:
                    for app_id in SYSTEM_UI_VOICE_APPS:
                        self.close_system_ui(app_id)
                except Exception:
                    pass
                time.sleep(0.28)

        threading.Thread(target=_close_system_ui_burst, daemon=True).start()
        threading.Thread(target=_close_native_results, daemon=True).start()

    def release_voiceagent_suppression(self, linger: float = 8.0) -> None:
        """Close the suppression window `linger` seconds from now.

        Replaces any longer arm: the watcher stays on while a turn is live
        (see _voiceagent_watch_loop), so after the turn only `linger` is needed
        to catch LG's late voice UI. A far-future deadline kept it closing apps
        several times a second for as long as the daemon ran.
        """
        with self._suppress_lock:
            self._suppress_deadline = time.time() + linger

    def _arm_browser_close(self, fg: str) -> None:
        """Allow closing browsers for the voice turn that starts now.

        `fg` is the app in front when the voice button was pressed: if that
        is a browser, the user was browsing, so it stays open.
        """
        if fg in BROWSER_CLOSE_IDS:
            self._browser_close_until = 0.0
        else:
            self._browser_close_until = time.time() + 90.0

    def _browsers_closable(self) -> bool:
        return time.time() < self._browser_close_until

    def _kill_lg_voice_browsers(self) -> None:
        """Force-close LG voice-search browsers (Google card from voice.performer).

        SAM logs show: voice.performer.plugin → com.webos.app.browser with
        google.com for weather-like KEY_VOICE queries. Must run during the
        whole voice turn, not only at session start.
        """
        if not self._browsers_closable():
            return
        for app_id in BROWSER_CLOSE_IDS:
            try:
                _luna_send(
                    "luna://com.webos.applicationManager/closeByAppId",
                    {"id": app_id},
                    timeout=1.2,
                )
            except Exception:
                pass

    def _dismiss_native_competitors(self) -> None:
        """Close LG browser/voice UIs without launching voice card."""
        for app_id in self.native_app_ids:
            if app_id == self.overlay_app_id:
                continue
            if app_id in SYSTEM_UI_VOICE_APPS:
                continue
            if app_id in BROWSER_CLOSE_IDS and not self._browsers_closable():
                continue
            self.close_native_app(app_id)
        for app_id in SYSTEM_UI_VOICE_APPS:
            self.close_system_ui(app_id)

    def app_handoff_active(self) -> bool:
        """True for a while after we launched Netflix/etc. — do not steal focus."""
        return time.time() < float(self._app_handoff_until or 0.0)

    def _foreground_app_id(self) -> str:
        """Best-effort current foreground app id (for focus checks)."""
        try:
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

    def prepare_app_handoff(self, seconds: float = 300.0) -> None:
        """Soft handoff: block reclaim while target app is launching.

        No overlay status text — launch commands are silent (mute/volume style).
        The card stays briefly as a cover until close; do not paint "Opening…".
        """
        secs = max(60.0, float(seconds))
        self._app_handoff_until = time.time() + secs
        self.suppress_reclaim = True
        self.capture_active = False
        self._cancel_close_timer()
        self._session_ui_gen += 1
        print(
            "[voice] app handoff prepared for %.0fs (silent, no Opening UI)"
            % secs,
            flush=True,
        )

    def begin_app_handoff(self, seconds: float = 300.0) -> None:
        """Hard handoff: drop overlay clients + guardian (call after target is up)."""
        secs = max(60.0, float(seconds))
        self._app_handoff_until = time.time() + secs
        self.suppress_reclaim = True
        self._voice_session_live = False
        self.capture_active = False
        self._cancel_close_timer()
        # Invalidate focus-retry / late session UI work from the launch press.
        self._session_ui_gen += 1
        # Drop the voice card's WS so a zombie card cannot paint chat over Prime.
        # Launch Home's own socket stays (it only shows the mic badge).
        with self._lock:
            stale = [c for c in self._ws_clients if self._ws_roles.get(c) == "overlay"]
            self._ws_clients = [c for c in self._ws_clients if c not in stale]
            for conn in stale:
                self._ws_roles.pop(conn, None)
            stale += list(self._clients)
            self._clients = []
        for conn in stale:
            try:
                conn.close()
            except OSError:
                pass
        # Guardian only for a short race window after handoff. A long window
        # made "open voice card" / home-screen launch impossible for minutes
        # (guardian kept killing the card). Reclaim is still blocked via
        # app_handoff_until for the full handoff duration until KEY_VOICE.
        guard_secs = min(12.0, max(5.0, float(secs)))
        threading.Thread(
            target=self._app_handoff_guardian_loop,
            args=(guard_secs,),
            daemon=True,
            name="app-handoff-guard",
        ).start()
        print(
            "[voice] app handoff armed for %.0fs (guardian %.0fs)"
            % (secs, guard_secs),
            flush=True,
        )

    def _app_handoff_guardian_loop(self, seconds: float) -> None:
        """Briefly force-close voice card if it steals focus right after handoff.

        Only covers the focus race when launching Netflix/Prime/etc. Does not
        run for the full handoff TTL so the user can open voice card from Home.
        Never kill during a live KEY_VOICE turn.

        Only close when SAM FG is actually voice card. Continuous closeByAppId
        hammering dismissed Settings/Quick Settings overlays (LSM visible:true
        then false within seconds).
        """
        deadline = time.time() + max(5.0, min(15.0, float(seconds)))
        app_id = self.overlay_app_id
        while time.time() < deadline and self.app_handoff_active():
            # New voice turn clears handoff in session_started; also skip if
            # a turn somehow overlaps the guard window.
            if self._voice_session_live or self.capture_active:
                time.sleep(0.5)
                continue
            try:
                fg = _luna_send(
                    "luna://com.webos.applicationManager/getForegroundAppInfo",
                    {},
                    timeout=1.5,
                )
                fg_id = str(
                    fg.get("appId") or fg.get("foregroundAppId") or fg.get("id") or ""
                )
                if fg_id == app_id or (fg_id and app_id in fg_id):
                    print(
                        "[voice] handoff guardian: voice card foreground — killing",
                        flush=True,
                    )
                    _luna_send(
                        "luna://com.webos.applicationManager/closeByAppId",
                        {"id": app_id},
                        timeout=2.0,
                    )
                    # No renderer kill here — kills can thrash WebAppMgr and
                    # hide sibling overlays (Settings) painted in the same window.
            except Exception:
                pass
            time.sleep(0.55)
        print("[voice] handoff guardian ended", flush=True)

    def clear_app_handoff(self) -> None:
        """Allow overlay again (new KEY_VOICE press)."""
        self._app_handoff_until = 0.0

    def _reclaim_foreground(self, *, reason: str = "") -> None:
        """Close LG browser/voice UIs and bring voice card back to the front.

        Only call during a live voice turn. Idle suppression must never launch
        the overlay (that was opening voice card on every deploy/daemon restart).
        Avoid re-launch when WS is already live — a second launch reloads the
        page and flashes the TV background.
        """
        if self.app_handoff_active():
            print(
                "[voice] reclaim blocked (app handoff, reason=%s)"
                % (reason or "?"),
                flush=True,
            )
            # Still kill LG bars only — never launch ourselves over Netflix.
            try:
                for app_id in SYSTEM_UI_VOICE_APPS:
                    self.close_system_ui(app_id)
            except Exception:
                pass
            return
        self._dismiss_native_competitors()
        if self.ws_client_count() > 0:
            if reason:
                print(
                    "[voice] reclaim competitors only (%s) ws=%d"
                    % (reason, self.ws_client_count()),
                    flush=True,
                )
            return
        if not self._voice_session_live:
            print(
                "[voice] reclaim skipped (no live voice session, reason=%s)"
                % (reason or "?"),
                flush=True,
            )
            return
        payload = {
            "id": self.overlay_app_id,
            "noSplash": True,
            "params": {"source": "launchhome", "mode": "voice"},
        }
        _luna_send(
            "luna://com.webos.applicationManager/launch",
            payload,
            timeout=6.0,
        )
        if reason:
            print(
                "[voice] reclaim foreground (%s) ws=%d"
                % (reason, self.ws_client_count()),
                flush=True,
            )

    def _voiceagent_watch_loop(self) -> None:
        """Suppress LG native voice UI; keep voice card front only mid voice turn.

        Between turns / at daemon start: only dismiss native bars/browsers —
        never launch the overlay (install/restart must stay silent).
        During a live voice session: close competitors and re-launch voice card
        so beanbrowser cannot steal the screen.
        """
        was_active = False
        last_reclaim = 0.0
        last_suppress = 0.0
        last_browser_kill = 0.0
        while True:
            # App handoff (post Netflix/Prime launch): voice card is fully stopped.
            # Do nothing that could steal focus — not even competitor dismiss
            # thrash (that can pop our card back in the stack on some firmwares).
            if self.app_handoff_active():
                was_active = False
                time.sleep(0.5)
                continue
            # Armed while a voice turn is live, plus a short window around it
            # (press, session end). It used to be armed for 24 h, so the
            # closes below ran every 0.35-0.8 s forever: ~30 luna-send
            # processes a second kept ls-hubd and servicemanager busy
            # (~30% of the TV's CPU) and closed LG's browser and popups.
            active = (
                self._voice_session_live
                or self.capture_active
                or time.time() < self._suppress_deadline
            )
            if active:
                if not was_active:
                    print(
                        "[voice] voice-UI watcher armed",
                        flush=True,
                    )
                    was_active = True
                now = time.time()
                n_ws = self.ws_client_count()
                if self._voice_session_live:
                    # LG voice.performer launches com.webos.app.browser (Google)
                    # ~5–8s after KEY_VOICE for weather/search-like queries.
                    # Kill browsers on a fast cadence for the whole turn so the
                    # user never lands on a Google card mid-answer.
                    if (
                        not self.capture_active
                        and (now - last_browser_kill) >= 0.35
                    ):
                        try:
                            self._kill_lg_voice_browsers()
                        except Exception:
                            pass
                        last_browser_kill = now
                    # Only reclaim when the overlay is actually gone. Periodic
                    # re-launch while WS is healthy caused webOSRelaunch churn
                    # and double clients / mid-answer focus races.
                    # NEVER reclaim during capture/STT (suppress_reclaim): logs
                    # showed ws=0 after release → reclaim → black screen while
                    # STT still running, then "Listening…" on a blank card.
                    if self.app_handoff_active():
                        # Post Netflix-launch: never pull voice card over the app.
                        if (now - last_suppress) >= 1.0:
                            try:
                                for app_id in SYSTEM_UI_VOICE_APPS:
                                    self.close_system_ui(app_id)
                            except Exception:
                                pass
                            last_suppress = now
                    elif (
                        n_ws == 0
                        and not self.capture_active
                        and not self.suppress_reclaim
                        and not self.app_handoff_active()
                        and (now - last_reclaim) >= 1.2
                    ):
                        try:
                            self._reclaim_foreground(reason="ws-lost")
                        except Exception as exc:  # noqa: BLE001
                            print(
                                "[voice] reclaim failed: %s" % exc,
                                flush=True,
                            )
                        last_reclaim = now
                    elif (
                        n_ws == 0
                        and not self.capture_active
                        and self.suppress_reclaim
                    ):
                        # Keep native bars down without relaunching voice card.
                        if (now - last_suppress) >= 0.4:
                            try:
                                for app_id in SYSTEM_UI_VOICE_APPS:
                                    self.close_system_ui(app_id)
                            except Exception:
                                pass
                            last_suppress = now
                    elif n_ws > 0 and (now - last_suppress) >= 1.0:
                        # During answer/TTS: kill Listening bar; browsers are
                        # handled by the faster last_browser_kill cadence above.
                        try:
                            for app_id in SYSTEM_UI_VOICE_APPS:
                                self.close_system_ui(app_id)
                        except Exception:
                            pass
                        last_suppress = now
                else:
                    # Idle / install / daemon start: never launch overlay.
                    # Still hammer LG/Amazon result apps so a stray KEY_VOICE
                    # never leaves "content you'll like" / Alexa on screen.
                    if (now - last_suppress) >= 0.8:
                        try:
                            self._dismiss_native_competitors()
                            for app_id in NATIVE_RESULT_KILL_IDS:
                                try:
                                    _luna_send(
                                        "luna://com.webos.applicationManager/closeByAppId",
                                        {"id": app_id},
                                        timeout=0.45,
                                    )
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        last_suppress = now
                # Always dismiss the SystemUI Listening / AI bar. Do this every
                # loop tick with a short Luna timeout so the bar cannot stick.
                for app_id in SYSTEM_UI_VOICE_APPS:
                    self.close_system_ui(app_id)
                # Mid-session: also kill aiplatform/Alexa every tick (cheap).
                if self._voice_session_live or self.capture_active:
                    for app_id in (
                        "com.webos.app.aiplatform",
                        "com.webos.app.aiplatformsupport",
                        "amazon.alexa.view",
                        "amazon.alexapr",
                    ):
                        try:
                            _luna_send(
                                "luna://com.webos.applicationManager/closeByAppId",
                                {"id": app_id},
                                timeout=0.35,
                            )
                        except Exception:
                            pass
                time.sleep(0.15 if self._voice_session_live else 0.35)
            else:
                was_active = False
                time.sleep(0.2)

    def _start_ws_server(self) -> None:
        if self._ws_server is not None:
            return
        last_err: Optional[BaseException] = None
        for attempt in range(1, 6):
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                # Drop any lingering TIME_WAIT holder when possible.
                try:
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except (AttributeError, OSError):
                    pass
                srv.bind((WS_HOST, WS_PORT))
                srv.listen(8)
                self._ws_server = srv
                self._ws_listening = True
                self._ws_thread = threading.Thread(
                    target=self._ws_accept_loop, daemon=True
                )
                self._ws_thread.start()
                print(
                    "[voice] ws server listening on %s:%d"
                    % (WS_HOST, WS_PORT),
                    flush=True,
                )
                return
            except OSError as exc:
                last_err = exc
                print(
                    "[voice] ws server bind failed (try %d/5): %s"
                    % (attempt, exc),
                    flush=True,
                )
                time.sleep(0.4 * attempt)
        self._ws_listening = False
        print(
            "[voice] ERROR: ws server unavailable after retries: %s"
            % last_err,
            flush=True,
        )

    @property
    def ws_listening(self) -> bool:
        return bool(self._ws_listening)

    def _ws_accept_loop(self) -> None:
        assert self._ws_server is not None
        while True:
            try:
                conn, _ = self._ws_server.accept()
            except OSError:
                break
            if not _ws_handshake(conn):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            # Multi-client: launchers + overlay all need live session events.
            # Evict oldest only when over the cap (reconnect leak protection).
            stale_list: list[socket.socket] = []
            with self._lock:
                self._ws_clients.append(conn)
                while len(self._ws_clients) > MAX_WS_CLIENTS:
                    stale_list.append(self._ws_clients.pop(0))
                for stale in stale_list:
                    self._ws_roles.pop(stale, None)
            for stale in stale_list:
                try:
                    stale.close()
                except OSError:
                    pass
            print(
                "[voice] ws client connected (clients=%d, closed %d old)"
                % (self.ws_total_count(), len(stale_list)),
                flush=True,
            )
            # Home/launcher open connects without a voice session. Cancel any
            # delayed voice safety-close so we do not kill the settings UI.
            # Never clear app_handoff here: warm-process WS reconnects after
            # Netflix/Settings handoff used to wipe the handoff and reclaim
            # voice card over the target app.
            if not self._voice_session_live and not self.capture_active:
                self._cancel_close_timer()
                if self.app_handoff_active():
                    print(
                        "[voice] home/settings WS attach — handoff kept "
                        "(suppress reclaim)",
                        flush=True,
                    )
                else:
                    self.suppress_reclaim = False
                    self._session_ui_gen += 1
                    print(
                        "[voice] home/settings WS attach — idle reconnect",
                        flush=True,
                    )
            # Catch-up: cold launch often connects after sessionStarted was sent
            # to 0 clients, so the UI never left Settings and never showed STT.
            threading.Thread(
                target=self._ws_catchup, args=(conn,), daemon=True
            ).start()
            threading.Thread(
                target=self._ws_client_reader, args=(conn,), daemon=True
            ).start()

    def _ws_client_reader(self, conn: socket.socket) -> None:
        """Read inbound frames from one WS client and dispatch config RPCs.

        Broadcasts (_broadcast) still push outbound events to this same conn;
        this reader only consumes client->server frames. It must not hold
        self._lock while blocked in recv, so it acquires the lock only briefly
        to send a reply frame.
        """
        try:
            while True:
                frame = _ws_read_frame(conn)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # close
                    break
                if opcode == 0x9:  # ping -> pong
                    try:
                        conn.sendall(_ws_control_frame(0xA, payload))
                    except OSError:
                        break
                    continue
                if opcode not in (0x1, 0x2):  # only text/binary carry RPCs
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._handle_ws_request(conn, msg)
        finally:
            with self._lock:
                if conn in self._ws_clients:
                    self._ws_clients.remove(conn)
                self._ws_roles.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass

    def _handle_ws_request(self, conn: socket.socket, msg: dict[str, Any]) -> None:
        req_type = msg.get("type")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if not req_type:
            return
        if req_type == "hello":
            role = str(params.get("role") or "")
            with self._lock:
                self._ws_roles[conn] = role
            reply = {
                "event": "configResult",
                "id": req_id,
                "ok": True,
                "result": {"service": SERVICE_NAME, "role": role},
            }
            with self._lock:
                try:
                    conn.sendall(_ws_frame(json.dumps(reply)))
                except OSError:
                    pass
            return
        if self.config_handler is None:
            resp = {
                "event": "configResult",
                "id": req_id,
                "ok": False,
                "error": "config handler unavailable",
            }
        else:
            try:
                result = self.config_handler(req_type, params)
                resp = {
                    "event": "configResult",
                    "id": req_id,
                    "ok": True,
                    "result": result or {},
                }
            except Exception as exc:  # noqa: BLE001
                resp = {
                    "event": "configResult",
                    "id": req_id,
                    "ok": False,
                    "error": str(exc),
                }
        frame = _ws_frame(json.dumps(resp))
        with self._lock:
            try:
                conn.sendall(frame)
            except OSError:
                pass

    def stop(self) -> None:
        if self._ws_server is not None:
            try:
                self._ws_server.close()
            except OSError:
                pass
            self._ws_server = None
        with self._lock:
            for client in self._ws_clients:
                try:
                    client.close()
                except OSError:
                    pass
            self._ws_clients.clear()
            self._ws_roles.clear()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        with self._lock:
            for client in self._clients:
                try:
                    client.close()
                except OSError:
                    pass
            self._clients.clear()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def _accept_loop(self) -> None:
        assert self._server is not None
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                break
            with self._lock:
                # Defensive cap: the Luna bridge should only ever hold one live
                # subscription. If a buggy client reconnects without closing old
                # sockets, unbounded growth would leak a reader thread per
                # connection and eventually exhaust the process thread limit
                # ("can't start new thread"), bricking the overlay. Evict the
                # oldest connections to keep the count bounded.
                while len(self._clients) >= MAX_UNIX_CLIENTS:
                    stale = self._clients.pop(0)
                    try:
                        stale.close()
                    except OSError:
                        pass
                self._clients.append(client)
            threading.Thread(
                target=self._client_reader, args=(client,), daemon=True
            ).start()

    def _client_reader(self, client: socket.socket) -> None:
        """Detect disconnect of a unix event subscriber and prune promptly.

        The Node Luna bridge subscribes but never sends data; it reconnects on
        every drop. Without reading, closed sockets only get pruned on a failed
        broadcast send, so stale entries pile up (observed 445+). Blocking on
        recv here removes each client the moment its peer closes.
        """
        try:
            while True:
                try:
                    data = client.recv(4096)
                except OSError:
                    break
                if not data:
                    break  # EOF: peer closed the connection
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            try:
                client.close()
            except OSError:
                pass

    def _ws_send(self, conn: socket.socket, event: str, payload: Optional[dict[str, Any]] = None) -> bool:
        """Send one event to a single WS client. Returns False if the socket died."""
        msg = json.dumps({"event": event, "payload": payload or {}})
        try:
            conn.sendall(_ws_frame(msg))
            return True
        except OSError:
            return False

    def _ws_catchup(self, conn: socket.socket) -> None:
        """Replay live session state so a late-connecting overlay still works.

        Uses ``sessionCatchup`` (not ``sessionStarted``) so the overlay does
        not wipe a transcript/answer already on screen when focus-retry or a
        second WebSocket attaches mid-utterance.
        """
        # Small delay so the page can attach onmessage handlers after open.
        time.sleep(0.15)
        if not self._voice_session_live and not self.capture_active:
            return
        print("[voice] ws catch-up (session live) for late client", flush=True)
        payload: dict[str, Any] = {
            "catchup": True,
            "capture_active": bool(self.capture_active),
            "transcript": self._last_transcript or "",
            "answer": self._last_answer or "",
            "status": getattr(self, "_last_status", "") or "",
        }
        if self._last_answer_source:
            payload["source"] = self._last_answer_source
        if not self._ws_send(conn, "sessionCatchup", payload):
            return
        # Re-paint status so reopened card shows "Looking up weather…" not blank.
        st = getattr(self, "_last_status", "") or ""
        if st:
            self._ws_send(conn, "status", {"text": st})

    def _broadcast(self, event: str, payload: Optional[dict[str, Any]] = None) -> None:
        msg = json.dumps({"event": event, "payload": payload or {}})
        data = (msg + "\n").encode("utf-8")
        frame = _ws_frame(msg)
        # Snapshot clients under lock, then send without holding the lock so a
        # slow/dead peer cannot stall the daemon (or other clients).
        with self._lock:
            clients = list(self._clients)
            ws_clients = list(self._ws_clients)
            if event == "answerAudio":
                # Only the voice card plays speech; Launch Home ignores it.
                ws_clients = [c for c in ws_clients if self._ws_roles.get(c) == "overlay"]
        n_clients = len(clients)
        n_ws = len(ws_clients)
        dead: list[socket.socket] = []
        ws_dead: list[socket.socket] = []
        for client in clients:
            try:
                client.sendall(data)
            except OSError:
                dead.append(client)
        for client in ws_clients:
            try:
                client.sendall(frame)
            except OSError:
                ws_dead.append(client)
        if dead or ws_dead:
            with self._lock:
                for client in dead:
                    if client in self._clients:
                        self._clients.remove(client)
                    try:
                        client.close()
                    except OSError:
                        pass
                for client in ws_dead:
                    if client in self._ws_clients:
                        self._ws_clients.remove(client)
                    self._ws_roles.pop(client, None)
                    try:
                        client.close()
                    except OSError:
                        pass
        # answerPartial is extremely chatty during streaming; log only gaps /
        # important events so /tmp/launch-home-voice.log stays readable.
        if event not in ("answerPartial", "transcriptPartial"):
            print(
                "[voice] broadcast %s -> %d client(s), %d ws"
                % (event, n_clients, n_ws),
                flush=True,
            )
        elif n_ws == 0 and n_clients == 0:
            print(
                "[voice] broadcast %s DROPPED (no clients)" % event,
                flush=True,
            )

    def _overlay_count_locked(self) -> int:
        return sum(1 for c in self._ws_clients if self._ws_roles.get(c) == "overlay")

    def ws_client_count(self) -> int:
        """Connected voice-card sockets (Launch Home's own socket excluded)."""
        with self._lock:
            return self._overlay_count_locked()

    def ws_total_count(self) -> int:
        with self._lock:
            return len(self._ws_clients)

    def ensure_overlay_connected(self, timeout: float = 3.0) -> bool:
        """Make sure at least one overlay WebSocket is live.

        Critical before answer/TTS: if the card was killed (browser close /
        focus race), re-launch and wait so we do not synthesize into the void.
        """
        if self.ws_client_count() > 0:
            return True
        if self.app_handoff_active() and not self._voice_session_live:
            print(
                "[voice] ensure_overlay blocked (app handoff)",
                flush=True,
            )
            return False
        if self.suppress_reclaim and not self._voice_session_live:
            print(
                "[voice] ensure_overlay blocked (session ended / no reclaim)",
                flush=True,
            )
            return False
        # Tell the user why the card may flash back if SAM killed us mid-turn.
        try:
            self.status("Thinking…")
        except Exception:
            pass
        print(
            "[voice] overlay WS missing — re-launching before answer",
            flush=True,
        )
        # Single launch, no focus-retry (retry races kill a just-connected WS).
        payload = {
            "id": self.overlay_app_id,
            "noSplash": True,
            "params": {"source": "launchhome", "mode": "voice"},
        }
        _luna_send(
            "luna://com.webos.applicationManager/launch", payload, timeout=12.0
        )
        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            if self.ws_client_count() > 0:
                # Give onmessage handlers a beat after open + catch-up.
                time.sleep(0.2)
                print(
                    "[voice] overlay WS restored (%d client(s))"
                    % self.ws_client_count(),
                    flush=True,
                )
                return True
            time.sleep(0.1)
        print(
            "[voice] overlay WS still missing after %.1fs" % timeout,
            flush=True,
        )
        return False

    def launch_overlay(self) -> None:
        """Bring voice card to the foreground in voice mode.

        Fast path: short luna timeout; native browser closes run in the
        background so they never block first paint. Warm process (WS still
        connected) only needs a quick relaunch for focus.
        """
        if self.app_handoff_active():
            print(
                "[voice] launch_overlay blocked (app handoff)",
                flush=True,
            )
            return
        payload = {
            "id": self.overlay_app_id,
            "noSplash": True,
            "params": {"source": "launchhome", "mode": "voice"},
        }
        # Do not block first paint on browser kills — run them after launch.
        def _close_browsers_bg() -> None:
            if not self.close_native or not self._browsers_closable():
                return
            for app_id in self.native_app_ids:
                if app_id in BROWSER_CLOSE_IDS:
                    try:
                        self.close_native_app(app_id)
                    except Exception:
                        pass

        t_launch = time.time()
        resp = _luna_send(
            "luna://com.webos.applicationManager/launch", payload, timeout=4.0
        )
        print(
            "[voice] launch_overlay(%s) +%.0fms: %s"
            % (
                self.overlay_app_id,
                (time.time() - t_launch) * 1000.0,
                resp,
            ),
            flush=True,
        )

        retry_gen = self._session_ui_gen

        def _focus_retry() -> None:
            # Faster poll: warm apps often connect in <400ms.
            for i in range(10):
                time.sleep(0.12)
                if self.app_handoff_active() or retry_gen != self._session_ui_gen:
                    return
                if self.ws_client_count() > 0:
                    print(
                        "[voice] launch_overlay focus-retry skipped "
                        "(%d ws after %.2fs)"
                        % (self.ws_client_count(), (i + 1) * 0.12),
                        flush=True,
                    )
                    return
            if self.app_handoff_active() or retry_gen != self._session_ui_gen:
                return
            r2 = _luna_send(
                "luna://com.webos.applicationManager/launch",
                payload,
                timeout=4.0,
            )
            print("[voice] launch_overlay focus-retry: %s" % r2, flush=True)

        threading.Thread(target=_focus_retry, daemon=True).start()
        threading.Thread(target=_close_browsers_bg, daemon=True).start()
        if self.close_native:
            threading.Thread(target=self._close_native_loop, daemon=True).start()

    @staticmethod
    def close_system_ui(app_id: str) -> None:
        """Dismiss a SystemUIComponent (e.g. voiceagent Listening bar).

        Documented in firmware Util.qml:
          luna://com.webos.service.sysuicompmgr/close '{"appId":"com.webos.app.voiceagent"}'

        Use a short timeout so the watch loop can fire many closes per second —
        a 5s luna-send wait let the bar stay visible for the whole call.
        """
        _luna_send(
            "luna://com.webos.service.sysuicompmgr/close",
            {"appId": app_id},
            timeout=0.25,
        )

    def dismiss_voice_recognition_off_dialog(self, seconds: float = 8.0) -> None:
        """Kill LG voice-recognition popups (alert Yes/No + "turned on" toast).

        - Alert: "LG voice recognition feature is turned off…?"
        - Toast: "LG voice recognition feature has been turned on."

        Both are SystemUI (`com.webos.app.alert` / `com.webos.app.toast`).
        """
        print(
            "[voice] dismissing LG voice popups (alert+toast) for %.1fs"
            % max(2.0, float(seconds)),
            flush=True,
        )
        targets = ("com.webos.app.alert", "com.webos.app.toast")

        def _kill_ui_procs() -> None:
            try:
                out = subprocess.check_output(
                    ["ps", "ax", "-o", "pid=,args="],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2,
                )
            except Exception:
                return
            for line in out.splitlines():
                low = line.lower()
                if not any(
                    t in low or t.replace("com.webos.", "") in low for t in targets
                ) and "app.alert" not in low and "app.toast" not in low:
                    continue
                parts = line.strip().split(None, 1)
                if not parts:
                    continue
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

        def _loop() -> None:
            end = time.time() + max(2.0, float(seconds))
            n = 0
            while time.time() < end:
                for app_id in targets:
                    try:
                        self.close_system_ui(app_id)
                    except Exception:
                        pass
                if n % 5 == 0:
                    _kill_ui_procs()
                if n % 3 == 0:
                    try:
                        fg = self._foreground_app_id()
                    except Exception:
                        fg = ""
                    if "alert" in (fg or "").lower():
                        for key in ("BACK", "DOWN", "ENTER"):
                            try:
                                _luna_send(
                                    "luna://com.webos.service.networkinput/sendSpecialKey",
                                    {"key": key},
                                    timeout=0.3,
                                )
                            except Exception:
                                pass
                n += 1
                time.sleep(0.05 if n < 50 else 0.15)

        threading.Thread(
            target=_loop, daemon=True, name="dismiss-voice-off-alert"
        ).start()

    @staticmethod
    def close_native_app(app_id: str) -> None:
        """Dismiss a real launched app/overlay (e.g. com.webos.app.voice, the
        LG voice-search / beanbrowser overlay from voice.performer.plugin).

        These are SAM-launched apps (window type OVERLAY/CARD), NOT
        sysuicompmgr SystemUIComponents, so closeByAppId is the correct API.
        Also kill the WebAppMgr renderer — some browser overlays ignore close.
        """
        resp = _luna_send(
            "luna://com.webos.applicationManager/closeByAppId",
            {"id": app_id},
            timeout=5.0,
        )
        if resp.get("returnValue") is True and "browser" in app_id:
            print("[voice] closed native app %s" % app_id, flush=True)
        LunaBridge._kill_renderer(app_id)

    @staticmethod
    def _kill_renderer(app_id: str) -> None:
        """Kill the WebAppMgr renderer for a web-overlay assistant.

        LG's Alexa/voice popups are web apps whose renderer ignores
        closeByAppId, so the only reliable way to dismiss them on a rooted
        TV is to kill the renderer process that owns the app-id.
        """
        marker = "app-id=%s" % app_id
        try:
            pids = os.listdir("/proc")
        except OSError:
            return
        for pid in pids:
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            except OSError:
                continue
            if "type=renderer" in cmd and marker in cmd:
                try:
                    os.kill(int(pid), 9)
                except OSError:
                    pass

    def _close_native_loop(self) -> None:
        deadline = time.time() + self.native_close_poll_sec
        while time.time() < deadline:
            if self.app_handoff_active():
                # App launch won — stop all native thrash that can refocus us.
                return
            # Never close native voice apps while the mic is live: closing
            # voiceagent makes voiceconductor EOF the capture socket and
            # truncates the utterance. Wait out the capture, then resume.
            # Keep pushing the deadline out while capture is active so the
            # full poll window runs AFTER release (otherwise a long hold
            # exhausts the window and the Listening bar is never dismissed).
            if self.capture_active or self._voice_session_live:
                # Defer to _voiceagent_watch_loop reclaim path while a turn
                # is live (close competitors + re-focus voice card together).
                deadline = time.time() + self.native_close_poll_sec
                time.sleep(0.25)
                continue
            for app_id in SYSTEM_UI_VOICE_APPS:
                self.close_system_ui(app_id)
            for app_id in self.native_app_ids:
                if app_id in SYSTEM_UI_VOICE_APPS:
                    continue
                if app_id in BROWSER_CLOSE_IDS and not self._browsers_closable():
                    continue
                # Card/system apps: ask the app manager to close.
                _luna_send(
                    "luna://com.webos.applicationManager/closeByAppId",
                    {"id": app_id},
                    timeout=5.0,
                )
                # Web-overlay assistants (Alexa popup): kill the renderer.
                self._kill_renderer(app_id)
            time.sleep(0.25)

    def _hand_back_to_launcher(self) -> None:
        """Put Launch Home in front before the card closes, if the turn began there.

        Closing the card alone makes webOS's last-input handler open LG's home
        instead of the app underneath. Skipped when the turn opened something
        else (an app, an input).
        """
        if not self._return_to_launcher or self.app_handoff_active():
            return
        fg = self._foreground_app_id()
        if fg and fg != self.overlay_app_id:
            return
        print("[voice] handing the screen back to Launch Home", flush=True)
        _luna_send(
            "luna://com.webos.applicationManager/launch",
            {"id": LAUNCHER_APP_ID},
            timeout=3.0,
        )

    def close_overlay(self, *, force: bool = False) -> None:
        # Never tear down the card while a voice turn is live — a delayed
        # safety timer from the *previous* turn used to fire mid-question and
        # flash the TV background, then launch_overlay opened a new panel.
        # force=True: app launch (Netflix/etc) must dismiss us immediately.
        if self._voice_session_live and not force:
            print(
                "[voice] close_overlay skipped (voice session live)",
                flush=True,
            )
            return
        # Soft close (default): hide the card but keep WebAppMgr + WS warm so
        # the next KEY_VOICE is a fast relaunch, not a cold SAM start (~1–2s).
        if not force:
            print(
                "[voice] close_overlay soft (keep process warm) id=%s"
                % self.overlay_app_id,
                flush=True,
            )
            try:
                self._broadcast("sessionEnded")
            except Exception:
                pass
            return
        print(
            "[voice] close_overlay force id=%s"
            % self.overlay_app_id,
            flush=True,
        )
        _luna_send(
            "luna://com.webos.applicationManager/closeByAppId",
            {"id": self.overlay_app_id},
            timeout=3.0,
        )
        # Never kill our own WebAppMgr renderer here. That caused a black
        # "Listening" card on the *next* press (renderer dead while SAM still
        # thought the app was up). closeByAppId is enough for app hand-off.

    def end_session_for_app_launch(self, *, soft: bool = False) -> None:
        """Fully stop voice card after launching another app (Prime/Netflix/…).

        Arms a long handoff so reclaim/launch_overlay cannot pull us back over
        the target app until the next KEY_VOICE.

        soft=True: closeByAppId only (no renderer SIGKILL). Use for system
        overlays like Settings so we do not thrash WebAppMgr mid-paint.
        """
        self._cancel_close_timer()
        self._voice_session_live = False
        self.capture_active = False
        self._session_ui_gen += 1  # invalidate any pending safety close
        # Clear mic badge flag immediately (clients may poll this file).
        try:
            self.write_voice_state(False, reason="app_handoff")
        except Exception:
            pass
        # Mark the client as closing before dropping its WebSocket so it never
        # paints "Reconnecting…" during a successful app handoff.
        try:
            self._broadcast("sessionEnded")
        except Exception:
            pass
        time.sleep(0.08)
        # 5 minutes (or until next KEY_VOICE clears handoff).
        self.begin_app_handoff(300.0)
        # Force-close the card.
        self.close_overlay(force=True)
        if not soft:
            try:
                self._kill_renderer(self.overlay_app_id)
            except Exception:
                pass
        # Second close a moment later (SAM sometimes ignores the first).
        def _double_close() -> None:
            time.sleep(0.4)
            try:
                _luna_send(
                    "luna://com.webos.applicationManager/closeByAppId",
                    {"id": self.overlay_app_id},
                    timeout=3.0,
                )
            except Exception:
                pass
            if not soft:
                try:
                    self._kill_renderer(self.overlay_app_id)
                except Exception:
                    pass

        threading.Thread(target=_double_close, daemon=True).start()

    def end_silent_tv_control(self) -> None:
        """Hide voice card after mute/volume/input.

        Must call closeByAppId — broadcasting sessionEnded alone only hides the
        DOM inside a still-foreground card, which paints as a black screen.
        Do not kill the WebAppMgr renderer (that made the next press a cold start).
        """
        self._cancel_close_timer()
        self._voice_session_live = False
        self.capture_active = False
        self.suppress_reclaim = True
        self._session_ui_gen += 1
        try:
            self.write_voice_state(False, reason="silent_tv_control")
        except Exception:
            pass
        try:
            self._broadcast("sessionEnded")
        except Exception:
            pass
        time.sleep(0.05)
        # closeByAppId removes the fullscreen card so Home/Netflix underneath
        # shows again. Renderer stays alive for a faster next KEY_VOICE.
        self.close_overlay(force=True)

    def _cancel_close_timer(self) -> None:
        t = self._close_timer
        self._close_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def write_voice_state(self, listening: bool, reason: str = "") -> None:
        """Atomic flag file for clients' mic badge (no WS required)."""
        payload = {
            "listening": bool(listening),
            "ts": time.time(),
            "reason": reason or ("listening" if listening else "idle"),
            # When the mic is down, never leave capture_active true in this
            # file — clients used that flag and kept "Listening" on.
            "capture_active": bool(self.capture_active) if listening else False,
            "session_live": bool(self._voice_session_live),
        }
        try:
            tmp = VOICE_STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, VOICE_STATE_PATH)
        except OSError as exc:
            print("[voice] voice-state write failed: %s" % exc, flush=True)

    def signal_listening_early(self, reason: str = "button_press") -> None:
        """Paint passive listeners *before* Luna/mic setup.

        KEY_VOICE → session_started used to wait on close_system_ui +
        launch_overlay (hundreds of ms to seconds). Broadcast + flag file
        first so the top-right mic badge appears on the next frame.
        """
        self._voice_session_live = True
        self.capture_active = True
        self.suppress_reclaim = True
        if not self._last_status:
            self._last_status = "Listening…"
        self.write_voice_state(True, reason=reason)
        self._broadcast("sessionStarted", {"early": True, "reason": reason})
        if self._last_status:
            self._broadcast("status", {"text": self._last_status})

    def session_started(self, *, show_overlay: bool = True) -> None:
        # Invalidate any pending safety-close from the previous turn first.
        self._session_ui_gen += 1
        self._cancel_close_timer()
        # New KEY_VOICE: allow overlay again after a prior Netflix handoff.
        self.clear_app_handoff()
        self._voice_session_live = True
        self.suppress_reclaim = True  # hold until STT/answer done
        self._last_transcript = ""
        self._last_answer = ""
        self._last_status = "Listening…"
        self._last_answer_source = None
        # Always mark listening for clients *before* any Luna work.
        self.write_voice_state(True, reason="session_started")
        self._broadcast(
            "sessionStarted",
            {"listening": True, "reason": "session_started"},
        )
        if self._last_status:
            self._broadcast("status", {"text": self._last_status})
        # Overlay launch runs after the broadcast so a launcher client is never
        # gated on applicationManager / focus races.
        if show_overlay:
            try:
                self.ensure_voice_overlay()
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] session_started overlay launch failed: %s" % exc,
                    flush=True,
                )
        else:
            print(
                "[voice] session_started: silent capture (no overlay)",
                flush=True,
            )

        def _dismiss_lg_chrome() -> None:
            try:
                # Short arm: the live-session flag keeps the watcher on for
                # the rest of the turn; session_ended then leaves 20 s.
                self.engage_voiceagent_suppression(seconds=30.0)
                for app_id in SYSTEM_UI_VOICE_APPS:
                    self.close_system_ui(app_id)
                self.burst_dismiss_voice_ui(seconds=3.0)
                self.dismiss_voice_recognition_off_dialog(seconds=6.0)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[voice] LG chrome dismiss failed: %s" % exc,
                    flush=True,
                )

        threading.Thread(
            target=_dismiss_lg_chrome, daemon=True, name="dismiss-lg-chrome"
        ).start()

    def ensure_voice_overlay(self) -> None:
        """Bring up the Listening / STT card as fast as possible."""
        if not self._voice_session_live:
            return
        if self.app_handoff_active():
            print(
                "[voice] ensure_voice_overlay blocked (app handoff)",
                flush=True,
            )
            return
        n_ws = self.ws_client_count()
        fg = self._foreground_app_id()
        self._arm_browser_close(fg)
        if fg and fg != self.overlay_app_id:
            self._return_to_launcher = fg == LAUNCHER_APP_ID
        # The voice card also shows over Launch Home: it only draws the mic
        # badge, so without the card there was no transcript or spoken answer.
        passive_home = False
        already_front = bool(
            n_ws > 0
            and fg
            and (
                fg == self.overlay_app_id
                or self.overlay_app_id in fg
                or fg in self.overlay_app_id
            )
        )
        if already_front or passive_home:
            print(
                "[voice] ensure_voice_overlay: %s (fg=%s ws=%d) — no launch"
                % (
                    "warm FG" if already_front else "passive home",
                    fg or "?",
                    n_ws,
                ),
                flush=True,
            )
            # sessionStarted already broadcast in session_started(); re-nudge
            # status only so late clients still paint Listening.
            if self._last_status:
                self._broadcast("status", {"text": self._last_status})
            return
        # Warm process in background (WS still up) — relaunch for focus only.
        if n_ws > 0:
            print(
                "[voice] ensure_voice_overlay: warm WS (fg=%s) — quick focus"
                % (fg or "?"),
                flush=True,
            )
        self.launch_overlay()
        # Overlay-only catch-up — do not tell clients the mic is live again.
        self._broadcast("sessionStarted", {"overlayOnly": True})
        if self._last_status:
            self._broadcast("status", {"text": self._last_status})
        # Brief wait only — warm apps connect in tens of ms.
        deadline = time.time() + (0.15 if n_ws > 0 else 0.35)
        while time.time() < deadline:
            with self._lock:
                if self._overlay_count_locked():
                    break
            time.sleep(0.03)

    def transcript_partial(self, text: str, is_final: bool = False) -> None:
        # Keep the longest live hypothesis for catch-up / final fallback so a
        # short mid-phrase rewrite cannot erase the user's full question.
        t = (text or "").strip()
        if t:
            prev = (self._last_transcript or "").strip()
            if (not prev) or len(t) >= len(prev) or is_final:
                if is_final and prev and len(t) < max(8, int(len(prev) * 0.55)):
                    # Short "final" partials are often STT glitches — keep prev.
                    pass
                else:
                    self._last_transcript = t
        self._broadcast(
            "transcriptPartial", {"text": text, "is_final": is_final}
        )

    def listening_ended(self, *, has_audio: bool = False) -> None:
        """Mic capture finished — overlay should leave the Listening state."""
        # Keep session_live true (answer may still stream); only mic is done.
        self.write_voice_state(False, reason="listening_ended")
        self._broadcast(
            "listeningEnded",
            {
                "has_audio": bool(has_audio),
                "transcript": self._last_transcript or "",
            },
        )

    def status(self, text: str) -> None:
        """Update the voice overlay status line (e.g. Looking up weather…)."""
        self._last_status = text or ""
        self._broadcast("status", {"text": text or ""})

    def transcript_final(self, text: str) -> str:
        """Publish final STT; returns the text used (may keep a longer live partial)."""
        t = (text or "").strip()
        prev = (self._last_transcript or "").strip()
        # Prefer the longer of final STT vs live partials when the engine
        # collapses a good long hypothesis into a short tail ("UK today.").
        if t and prev and len(t) < max(10, int(len(prev) * 0.6)):
            print(
                "[voice] transcript final shorter than live — keeping %r over %r"
                % (prev[:80], t[:80]),
                flush=True,
            )
            t = prev
        if t:
            self._last_transcript = t
        self._broadcast("transcriptFinal", {"text": t or text})
        return self._last_transcript or t or (text or "")

    def answer_partial(self, text: str) -> None:
        if text:
            # Accumulate for catch-up; final will replace with full string.
            self._last_answer = (self._last_answer or "") + text
        self._broadcast("answerPartial", {"text": text})

    def answer_final(self, text: str) -> None:
        if text:
            self._last_answer = text
        self._broadcast("answerFinal", {"text": text})

    def answer_source(self, source: str, citations=None) -> None:
        """Tell the overlay where the answer came from: Grok's model or the web.

        `source` is "grok" or "internet"; `citations` are the web sources Grok
        used (empty when it answered from its own knowledge).
        """
        payload = {"source": source, "citations": list(citations or [])}
        self._last_answer_source = payload
        self._broadcast("answerSource", payload)

    def tts_will_speak(self, enabled: bool = True) -> None:
        """Tell the overlay speech is coming so it can hold answer text until
        the first audio chunk is ready (eliminates the text-then-silence gap).
        """
        self._broadcast("ttsWillSpeak", {"enabled": bool(enabled)})

    def answer_audio(
        self,
        b64_audio: str,
        mime: str = "audio/mpeg",
        speed: float = 1.0,
        seq: int = 0,
        text: str = "",
    ) -> None:
        """Push synthesized answer audio (base64) for the overlay to play.

        `speed` becomes the overlay <audio> playbackRate (>1 speaks faster).
        `seq` orders streamed chunks (1,2,3…) so the overlay plays them in turn.
        `text` is the spoken wording for this chunk so the UI can reveal it in
        sync with playback instead of printing the whole answer first.
        """
        payload = {
            "audio": b64_audio,
            "mime": mime,
            "speed": speed,
            "seq": seq,
        }
        if text:
            payload["text"] = text
        self._broadcast("answerAudio", payload)

    def error(self, message: str) -> None:
        self._broadcast("error", {"message": message})

    def tts_complete(self, count: int) -> None:
        """Tell the overlay how many spoken chunks make up the full answer, so
        it knows when playback is truly finished (vs. a transient gap between
        streamed chunks) and can report `playbackEnded`."""
        self._broadcast("ttsComplete", {"count": count})

    def session_ended(self, *, force_close_sec: float = 12.0) -> None:
        # LG's voice.performer launches com.webos.app.voice ~10s after the press
        # (confirmed in /var/log/messages), often after our answer is already
        # shown. Keep the watcher dismissing it for a generous window.
        self.release_voiceagent_suppression(20.0)
        # Drop live-session first so the watcher cannot reclaim between this
        # and the delayed close (that was re-launching the overlay after fail).
        self._voice_session_live = False
        self.suppress_reclaim = True  # hold until next KEY_VOICE session_started
        self.capture_active = False
        self.write_voice_state(False, reason="session_ended")
        self._broadcast("sessionEnded")
        # sessionEnded only hides the DOM inside the card. If we never call
        # closeByAppId, voice card stays fullscreen FG with a blank page → black
        # screen after weather/chat (safety used to skip when warm WS attached).
        # Must be cancelable — a new KEY_VOICE bumps _session_ui_gen.
        self._cancel_close_timer()
        # Brief grace so the client can paint "session ended", then remove card.
        # force_close_sec is a long upper bound for old clients; after TTS we
        # already waited for playbackEnded + tts_post_speech_sec.
        delay = max(0.45, min(2.5, float(force_close_sec) * 0.12 + 0.35))
        gen = self._session_ui_gen
        print(
            "[voice] sessionEnded — force-dismiss card in %.1fs (gen=%d)"
            % (delay, gen),
            flush=True,
        )

        def _safety_close() -> None:
            if gen != self._session_ui_gen or self._voice_session_live:
                print(
                    "[voice] safety close skipped (stale gen or live session)",
                    flush=True,
                )
                return
            # Do not skip for warm WS — that left a black voice card after Q&A.
            # Launcher settings open bumps _session_ui_gen on idle reconnect.
            print(
                "[voice] sessionEnded force-dismiss card (ws=%d)"
                % self.ws_client_count(),
                flush=True,
            )
            self._hand_back_to_launcher()
            self.close_overlay(force=True)

        self._close_timer = threading.Timer(delay, _safety_close)
        self._close_timer.daemon = True
        self._close_timer.start()

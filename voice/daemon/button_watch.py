#!/usr/bin/env python3
"""Watch lginput2 logs for KEY_VOICE press/release events."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Optional

LOG_PATHS = ("/var/log/messages", "/var/log/lginput2.log")
BUTTON_LINE_RE = re.compile(
    r"lginput2 NL_BUTTON_CLICK\s+(\{.*\})",
    re.DOTALL,
)


class ButtonEvent(Enum):
    PRESSED = "pressed"
    RELEASED = "released"


@dataclass(frozen=True)
class VoiceButtonEvent:
    kind: ButtonEvent
    ts: float


def _parse_button_payload(line: str) -> Optional[dict]:
    match = BUTTON_LINE_RE.search(line)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def is_voice_press(line: str) -> bool:
    payload = _parse_button_payload(line)
    if not payload:
        return False
    return payload.get("button_type") == "KEY_VOICE"


def is_voice_release(line: str) -> bool:
    payload = _parse_button_payload(line)
    if not payload:
        return False
    if payload.get("button_type") != "KEY_VOICE":
        return False
    history = payload.get("button_history") or payload.get("history") or ""
    if isinstance(history, str) and "release" in history.lower():
        return True
    state = str(payload.get("state", "")).lower()
    return state in ("release", "released", "up")


class ButtonWatcher:
    """Tail system logs and emit KEY_VOICE press/release callbacks."""

    def __init__(
        self,
        on_event: Callable[[VoiceButtonEvent], None],
        *,
        log_paths: Iterable[str] = LOG_PATHS,
        idle_release_sec: float = 1.5,
    ) -> None:
        self._on_event = on_event
        self._log_paths = tuple(log_paths)
        self._idle_release_sec = idle_release_sec
        self._held = False
        self._last_press_ts = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._idle_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="button-watch", daemon=True)
        self._thread.start()
        self._idle_thread = threading.Thread(
            target=self._idle_release_loop, name="button-idle", daemon=True
        )
        self._idle_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def notify_voice_activity(self) -> None:
        """Refresh hold timer when hidraw voice packets arrive."""
        if self._held:
            self._last_press_ts = time.time()

    def force_release(self) -> None:
        if self._held:
            self._emit(ButtonEvent.RELEASED)

    def _emit(self, kind: ButtonEvent) -> None:
        if kind == ButtonEvent.PRESSED:
            if self._held:
                return
            self._held = True
            self._last_press_ts = time.time()
        else:
            if not self._held:
                return
            self._held = False
        self._on_event(VoiceButtonEvent(kind=kind, ts=time.time()))

    def _handle_line(self, line: str) -> None:
        if is_voice_release(line):
            self._emit(ButtonEvent.RELEASED)
            return
        if is_voice_press(line):
            self._emit(ButtonEvent.PRESSED)

    def _idle_release_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.1)
            if not self._held or self._idle_release_sec <= 0:
                continue
            if time.time() - self._last_press_ts >= self._idle_release_sec:
                self._emit(ButtonEvent.RELEASED)

    def _pick_log(self) -> str:
        import os

        for path in self._log_paths:
            if os.path.isfile(path):
                return path
        return self._log_paths[0]

    def _run(self) -> None:
        """Tail the system log; restart if tail exits (logrotate / crash)."""
        log_path = self._pick_log()
        while not self._stop.is_set():
            proc = subprocess.Popen(
                ["tail", "-F", "-n", "0", log_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    self._handle_line(line.rstrip("\n"))
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            # brief backoff then re-attach so KEY_VOICE never goes silent
            time.sleep(0.5)
            log_path = self._pick_log()


def main() -> int:
    import sys

    def printer(ev: VoiceButtonEvent) -> None:
        print(f"{ev.kind.value} @ {ev.ts:.3f}", flush=True)

    watcher = ButtonWatcher(printer)
    watcher.start()
    print(f"[button-watch] tailing {watcher._pick_log()} — hold KEY_VOICE", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        watcher.stop()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
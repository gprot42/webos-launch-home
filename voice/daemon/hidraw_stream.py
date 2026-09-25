#!/usr/bin/env python3
"""Stream mSBC frames from Magic Remote hidraw voice reports."""

from __future__ import annotations

import os
import select
import sys
import time
from typing import Callable, Iterable, Iterator, Optional

MSBC_SYNC = 0xAD
MSBC_FRAME_LEN = 57
MSBC_H2_SYNC = 0x01
MSBC_H2_SEQ = (0x08, 0x38, 0xC8, 0xF8)
DEFAULT_VOICE_IDS = (0xF9, 0xFB, 0xFE)
HEADER_BYTES = 2


def extract_msbc_frames(report: bytes) -> list[bytes]:
    """Return raw 57-byte mSBC frames embedded in a voice HID report."""
    out: list[bytes] = []
    n = len(report)
    i = 2
    while i + MSBC_FRAME_LEN <= n:
        if (
            report[i] == MSBC_SYNC
            and report[i + 1] == 0x00
            and report[i + 2] == 0x00
            and report[i - 2] == MSBC_H2_SYNC
            and report[i - 1] in MSBC_H2_SEQ
        ):
            out.append(report[i : i + MSBC_FRAME_LEN])
            i += MSBC_FRAME_LEN
        else:
            i += 1
    return out


def open_hidraw(device: str, wait: float = 0.0) -> int:
    deadline = time.time() + wait if wait > 0 else 0.0
    while True:
        try:
            return os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            if wait <= 0 or time.time() >= deadline:
                raise
            time.sleep(0.3)


class HidrawVoiceStream:
    """Read hidraw voice reports and yield mSBC frames while active."""

    def __init__(
        self,
        device: str = "/dev/hidraw0",
        *,
        voice_ids: Iterable[int] = DEFAULT_VOICE_IDS,
        idle_stop: float = 1.5,
        on_activity: Optional[Callable[[], None]] = None,
    ) -> None:
        self.device = device
        self.voice_ids = frozenset(voice_ids)
        self.idle_stop = idle_stop
        self._on_activity = on_activity
        self._fd: Optional[int] = None
        self._active = False
        self._last_data = 0.0

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def open(self, wait: float = 5.0) -> None:
        if self._fd is not None:
            return
        self._fd = open_hidraw(self.device, wait=wait)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def start(self) -> None:
        self._active = True
        self._last_data = time.time()

    def stop(self) -> None:
        self._active = False

    def iter_msbc_frames(self) -> Iterator[bytes]:
        """Yield mSBC frames until stopped or idle timeout after voice seen."""
        if self._fd is None:
            self.open()
        saw_voice = False
        while self._active:
            now = time.time()
            if saw_voice and self.idle_stop > 0 and (now - self._last_data) >= self.idle_stop:
                break
            ready, _, _ = select.select([self._fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(self._fd, 1024)
            except BlockingIOError:
                continue
            except OSError as exc:
                print(f"[hidraw-stream] read error: {exc}", file=sys.stderr)
                break
            if not data:
                continue
            rid = data[0]
            if rid not in self.voice_ids:
                continue
            saw_voice = True
            self._last_data = time.time()
            if self._on_activity:
                self._on_activity()
            for frame in extract_msbc_frames(data):
                yield frame


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Stream mSBC frames from hidraw")
    parser.add_argument("--device", default="/dev/hidraw0")
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args()

    stream = HidrawVoiceStream(args.device)
    stream.open(wait=10.0)
    stream.start()
    deadline = time.time() + args.seconds
    count = 0
    try:
        for _frame in stream.iter_msbc_frames():
            count += 1
            if time.time() >= deadline:
                break
    finally:
        stream.stop()
        stream.close()
    print(f"[hidraw-stream] mSBC frames={count}", file=sys.stderr)
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
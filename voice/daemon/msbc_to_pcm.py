#!/usr/bin/env python3
"""Decode mSBC frames to PCM16 mono 16 kHz chunks via ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from queue import Empty, Queue
from typing import Iterable, Iterator, Optional

PCM_CHUNK_BYTES = 3200  # 100 ms @ 16 kHz mono s16le


def find_ffmpeg(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        "/media/developer/apps/usr/palm/applications/org.webosbrew.lounge.launcher/voice/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "/bin/ffmpeg",
    ):
        if shutil.which(candidate) or __import__("os").path.isfile(candidate):
            return candidate
    raise FileNotFoundError(
        "ffmpeg not found — install on TV or bundle to voice/bin/ffmpeg"
    )


class MsbcDecoder:
    """Pipe mSBC frames into ffmpeg and read PCM16 output in fixed chunks."""

    def __init__(self, ffmpeg_path: Optional[str] = None) -> None:
        self.ffmpeg_path = find_ffmpeg(ffmpeg_path)
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._reader: Optional[threading.Thread] = None
        self._queue: Queue[Optional[bytes]] = Queue()
        self._write_err: Optional[Exception] = None

    def start(self) -> None:
        if self._proc is not None:
            return
        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "sbc",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self._proc.stdout is not None
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                chunk = self._proc.stdout.read(PCM_CHUNK_BYTES)
                if not chunk:
                    break
                self._queue.put(chunk)
        finally:
            self._queue.put(None)

    def feed(self, msbc_frame: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("decoder not started")
        try:
            self._proc.stdin.write(msbc_frame)
            self._proc.stdin.flush()
        except Exception as exc:
            self._write_err = exc
            raise

    def feed_many(self, frames: Iterable[bytes]) -> None:
        for frame in frames:
            self.feed(frame)

    def iter_pcm_chunks(self, *, block: bool = True) -> Iterator[bytes]:
        """Yield PCM chunks until decoder closes stdout."""
        while True:
            try:
                item = self._queue.get(timeout=0.2 if block else 0.0)
            except Empty:
                if self._write_err:
                    raise self._write_err
                if self._proc and self._proc.poll() is not None:
                    break
                if not block:
                    return
                continue
            if item is None:
                break
            yield item

    def close(self) -> None:
        if self._proc is None:
            return
        if self._proc.stdin:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        if self._reader:
            self._reader.join(timeout=2.0)
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        stderr = b""
        if self._proc.stderr:
            stderr = self._proc.stderr.read() or b""
        if self._proc.returncode not in (0, None) and stderr:
            print(f"[msbc-to-pcm] ffmpeg stderr: {stderr.decode(errors='replace')}", file=sys.stderr)
        self._proc = None
        self._reader = None


def decode_frames_to_pcm(
    frames: Iterable[bytes],
    *,
    ffmpeg_path: Optional[str] = None,
) -> bytes:
    """Batch-decode mSBC frames (fallback when live pipe is unavailable)."""
    decoder = MsbcDecoder(ffmpeg_path=ffmpeg_path)
    decoder.start()
    try:
        decoder.feed_many(frames)
    finally:
        decoder.close()
    out = bytearray()
    for chunk in decoder.iter_pcm_chunks():
        out.extend(chunk)
    return bytes(out)


def main() -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Decode .msbc file to raw PCM")
    parser.add_argument("input")
    parser.add_argument("-o", "--output", default="/tmp/launch-home-voice.pcm")
    parser.add_argument("--ffmpeg", default=None)
    args = parser.parse_args()

    frames = Path(args.input).read_bytes()
    frame_size = 57
    frame_list = [
        frames[i : i + frame_size] for i in range(0, len(frames) - frame_size + 1, frame_size)
    ]
    pcm = decode_frames_to_pcm(frame_list, ffmpeg_path=args.ffmpeg)
    Path(args.output).write_bytes(pcm)
    print(f"wrote {len(pcm)} bytes -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
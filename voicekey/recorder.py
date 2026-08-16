"""pw-record subprocess management: start on key-press, SIGINT on release."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
import time

log = logging.getLogger("voicekey.recorder")


class RecordingError(Exception):
    pass


class Recorder:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.path: str | None = None
        self.started: float = 0.0

    @property
    def active(self) -> bool:
        return self.proc is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.active else 0.0

    def start(self) -> None:
        assert not self.active
        fd, path = tempfile.mkstemp(prefix="voicekey-", suffix=".wav", dir="/tmp")
        os.close(fd)
        self.path = path
        try:
            self.proc = subprocess.Popen(
                ["pw-record", "--rate", "16000", "--channels", "1", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except OSError:
            self.path = None
            self._unlink(path)
            raise
        self.started = time.monotonic()
        log.info("recording -> %s", path)

    def stop(self) -> tuple[str, float]:
        """Stop recording; return (wav_path, duration). Raises RecordingError
        if pw-record died before we stopped it (no mic, PipeWire down, ...)."""
        assert self.active and self.proc is not None and self.path is not None
        proc, path = self.proc, self.path
        duration = self.elapsed
        self.proc = None
        self.path = None

        died_early = proc.poll() is not None
        if not died_early:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                died_early = True
        timed_out = False
        try:
            _, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            _, stderr = proc.communicate()
        if died_early or timed_out:
            tail = (stderr or b"").decode(errors="replace").strip()[-500:]
            self._unlink(path)
            reason = "did not stop within 3s" if timed_out else "exited early"
            raise RecordingError(
                f"pw-record {reason} (rc={proc.returncode}): {tail or 'no stderr'}"
            )
        log.info("recorded %.2fs", duration)
        return path, duration

    def abort(self) -> None:
        """Stop and discard (accidental tap, stuck key, device disconnect)."""
        if not self.active:
            return
        try:
            path, _ = self.stop()
        except RecordingError:
            return
        self._unlink(path)

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

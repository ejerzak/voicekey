"""Microphone capture. ``pw-record`` streams raw 16 kHz mono PCM to its stdout;
a reader thread hands every 100 ms frame to a callback (live recognition) and
keeps it for the final pass. Any command with the same contract can be the
source — ``voicekey.replay`` plays a WAV file at real-time pace for tests."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger("voicekey.recorder")

SAMPLE_RATE = 16000
FRAME_SAMPLES = SAMPLE_RATE // 10
PW_RECORD = [
    "pw-record", "--raw", "--format", "s16",
    "--rate", str(SAMPLE_RATE), "--channels", "1", "-",
]


class RecordingError(Exception):
    pass


class Recorder:
    def __init__(self, argv: list[str] = PW_RECORD) -> None:
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.frames: list[np.ndarray] = []
        self.started = 0.0

    @property
    def active(self) -> bool:
        return self.proc is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.active else 0.0

    @property
    def finished(self) -> bool:
        """The source exited by itself: end of a replayed file, or a failure."""
        return self.proc is not None and self.proc.poll() is not None

    def start(self, on_frame) -> None:
        assert not self.active
        self.proc = subprocess.Popen(
            self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.frames = []
        self.started = time.monotonic()
        self.thread = threading.Thread(
            target=self._pump, args=(self.proc.stdout, on_frame),
            name="recorder", daemon=True,
        )
        self.thread.start()
        log.info("recording")

    def _pump(self, stdout, on_frame) -> None:
        while data := stdout.read(FRAME_SAMPLES * 2):
            frame = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            self.frames.append(frame)
            try:
                on_frame(frame)
            except Exception:
                log.exception("live recognition failed on a frame")

    def stop(self) -> tuple[np.ndarray, float]:
        """Stop the source; return (samples, duration). Raises RecordingError
        if the source failed before we stopped it (no microphone, PipeWire down)."""
        assert self.proc is not None and self.thread is not None
        proc, thread, frames = self.proc, self.thread, self.frames
        duration = self.elapsed
        self.proc = self.thread = None
        failed = proc.poll() not in (None, 0)
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
        thread.join(3)
        if thread.is_alive():
            proc.kill()
            thread.join()
        stderr = proc.stderr.read().decode(errors="replace").strip()[-500:]
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()
        if failed:
            raise RecordingError(
                f"{self.argv[0]} exited early (rc={proc.returncode}): "
                f"{stderr or 'no stderr'}"
            )
        samples = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
        log.info("recorded %.2fs", duration)
        return samples, duration

    def abort(self) -> None:
        """Stop and discard (accidental tap, stuck key, device disconnect)."""
        if not self.active:
            return
        try:
            self.stop()
        except RecordingError:
            pass

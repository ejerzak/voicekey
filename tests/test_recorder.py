from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import wave

import numpy as np

from voicekey.recorder import FRAME_SAMPLES, Recorder, RecordingError

PYTHON = sys.executable


def _wait(predicate, timeout=5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


class RecorderTests(unittest.TestCase):
    def test_frames_reach_callback_and_stop_returns_all_samples(self):
        source = [PYTHON, "-c", (
            "import sys, numpy as np; "
            "sys.stdout.buffer.write((np.arange(4000) % 1000).astype('<i2').tobytes())"
        )]
        frames = []
        recorder = Recorder(source)
        recorder.start(frames.append)
        _wait(lambda: recorder.finished)
        samples, _duration = recorder.stop()
        self.assertEqual(len(samples), 4000)
        self.assertEqual([len(f) for f in frames], [FRAME_SAMPLES, FRAME_SAMPLES, 800])
        self.assertAlmostEqual(float(samples[999]), 999 / 32768, places=6)
        self.assertFalse(recorder.active)

    def test_source_failure_is_reported_with_stderr(self):
        source = [PYTHON, "-c", "import sys; sys.stderr.write('no microphone'); sys.exit(1)"]
        recorder = Recorder(source)
        recorder.start(lambda frame: None)
        _wait(lambda: recorder.finished)
        with self.assertRaisesRegex(RecordingError, "no microphone"):
            recorder.stop()

    def test_spawn_failure_leaves_recorder_inactive(self):
        recorder = Recorder(["/nonexistent/pw-record"])
        with self.assertRaises(OSError):
            recorder.start(lambda frame: None)
        self.assertFalse(recorder.active)

    def test_replay_source_paces_a_wav_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "half-second.wav")
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(np.zeros(8000, dtype=np.int16).tobytes())
            recorder = Recorder([PYTHON, "-m", "voicekey.replay", path])
            started = time.monotonic()
            recorder.start(lambda frame: None)
            _wait(lambda: recorder.finished)
            samples, duration = recorder.stop()
        self.assertEqual(len(samples), 8000)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertGreaterEqual(duration, 0.4)


if __name__ == "__main__":
    unittest.main()

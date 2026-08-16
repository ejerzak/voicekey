from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from voicekey.config import Config
from voicekey.daemon import Daemon, RecordingJob


class DaemonRoutingTests(unittest.TestCase):
    def setUp(self):
        self.daemon = Daemon(Config())
        self.daemon.backend = Mock()
        self.daemon.backend.transcribe.return_value = "hello"

    @patch("voicekey.daemon.notify")
    def test_agent_transcription_uses_separate_queue(self, _notify):
        job = RecordingJob("unused.wav", "agent", 1.0, None)
        self.daemon._process_recording(job)
        self.assertEqual(self.daemon.agent_prompts.get_nowait(), "hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.time.monotonic", return_value=20.0)
    def test_stale_dictation_is_copied(self, _clock, copy, _notify):
        job = RecordingJob("unused.wav", "dictate", 1.0, 7)
        self.daemon._deliver_dictation(job, "hello")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.focus.window_id", return_value=8)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_focus_change_is_copied(self, _clock, _focus, copy, _notify):
        job = RecordingJob("unused.wav", "dictate", 1.0, 7)
        self.daemon._deliver_dictation(job, "hello")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.focus.window_id", return_value=None)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_unverifiable_focus_is_copied(self, _clock, _focus, copy, _notify):
        job = RecordingJob("unused.wav", "dictate", 1.0, None)
        self.daemon._deliver_dictation(job, "hello")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    def test_full_agent_queue_recovers_prompt(self, _notify):
        for _ in range(self.daemon.agent_prompts.maxsize):
            self.daemon.agent_prompts.put_nowait("occupied")
        with patch("voicekey.daemon.recovery.save", return_value="/secure/path") as save:
            job = RecordingJob("unused.wav", "agent", 1.0, None)
            self.daemon._process_recording(job)
        save.assert_called_once_with("hello")


if __name__ == "__main__":
    unittest.main()

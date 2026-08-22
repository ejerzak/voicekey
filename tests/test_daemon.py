from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from voicekey.config import Config
from voicekey.daemon import Daemon, RecordingJob


class FakeRecorder:
    def __init__(self):
        self.active = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.active = True
        self.starts += 1

    def stop(self):
        self.active = False
        self.stops += 1
        return "unused.wav", 1.0


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

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_toggle_key_uses_two_presses_and_ignores_release(self, _focus, _notify):
        cfg = Config(dictate_toggle_key="KEY_CONFIG")
        daemon = Daemon(cfg)
        daemon.recorder = FakeRecorder()
        chord = next(
            chord
            for chord, action in daemon.actions.items()
            if action == ("dictate", "toggle")
        )
        code = next(iter(chord))

        daemon._on_key("/dev/input/event9", code, 1)
        daemon._on_key("/dev/input/event9", code, 0)
        self.assertTrue(daemon.recorder.active)
        self.assertEqual(daemon.recorder.starts, 1)
        self.assertEqual(daemon.recorder.stops, 0)

        daemon._on_key("/dev/input/event9", code, 1)
        self.assertFalse(daemon.recorder.active)
        self.assertEqual(daemon.recorder.stops, 1)
        self.assertEqual(daemon.recordings.get_nowait().action, "dictate")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_hold_key_starts_on_press_and_stops_on_release(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        chord = next(
            chord
            for chord, action in daemon.actions.items()
            if action == ("dictate", "hold")
        )
        code = next(iter(chord))

        daemon._on_key("/dev/input/event3", code, 1)
        self.assertTrue(daemon.recorder.active)
        self.assertEqual(daemon.recorder.starts, 1)

        daemon._on_key("/dev/input/event3", code, 0)
        self.assertFalse(daemon.recorder.active)
        self.assertEqual(daemon.recorder.stops, 1)
        self.assertEqual(daemon.recordings.get_nowait().action, "dictate")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_longest_matching_chord_selects_agent(self, _focus, _notify):
        cfg = Config(
            dictate_key="KEY_F23",
            agent_key="KEY_RIGHTALT+KEY_F23",
        )
        daemon = Daemon(cfg)
        daemon.recorder = FakeRecorder()

        daemon._on_key("/dev/input/event3", 100, 1)  # KEY_RIGHTALT
        daemon._on_key("/dev/input/event3", 193, 1)  # KEY_F23
        self.assertTrue(daemon.recorder.active)
        self.assertEqual(daemon.session_action, "agent")

        daemon._on_key("/dev/input/event3", 193, 0)
        self.assertFalse(daemon.recorder.active)
        self.assertEqual(daemon.recordings.get_nowait().action, "agent")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_bare_shared_key_selects_dictation(self, _focus, _notify):
        cfg = Config(
            dictate_key="KEY_F23",
            agent_key="KEY_RIGHTALT+KEY_F23",
        )
        daemon = Daemon(cfg)
        daemon.recorder = FakeRecorder()

        daemon._on_key("/dev/input/event3", 193, 1)  # KEY_F23
        self.assertEqual(daemon.session_action, "dictate")
        daemon._on_key("/dev/input/event3", 193, 0)
        self.assertEqual(daemon.recordings.get_nowait().action, "dictate")


if __name__ == "__main__":
    unittest.main()

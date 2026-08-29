from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import numpy as np

from voicekey.config import Config
from voicekey.daemon import Daemon, ImePreview, Job, NotifyPreview, Session


class FakeRecorder:
    def __init__(self):
        self.active = False
        self.starts = 0
        self.stops = 0
        self.on_frame = None
        self.elapsed = 0.0

    def start(self, on_frame):
        self.active = True
        self.starts += 1
        self.on_frame = on_frame

    def stop(self):
        self.active = False
        self.stops += 1
        return np.zeros(16000, dtype=np.float32), 1.0

    def abort(self):
        self.active = False


class FakeIme:
    def __init__(self, generation=1):
        self.generation = generation
        self.preedits = []
        self.commits = []
        self.commit_result = True

    def activation(self):
        return self.generation

    def rebind(self):
        return True

    def preedit(self, text, generation):
        self.preedits.append((text, generation))

    def commit(self, text, generation):
        self.commits.append((text, generation))
        return self.commit_result


class FakeStream:
    def __init__(self, texts):
        self.texts = list(texts)

    def feed(self, frame):
        return self.texts.pop(0)

    def finish(self):
        return self.texts.pop(0)


def _job(action, preview=None, window_id=None):
    return Job(np.zeros(16000, dtype=np.float32), action, 1.0, window_id,
               preview or NotifyPreview(action), "live")


class DaemonRoutingTests(unittest.TestCase):
    def setUp(self):
        self.daemon = Daemon(Config())
        self.daemon.backend = Mock()
        self.daemon.backend.transcribe.return_value = "hello"

    @patch("voicekey.daemon.notify")
    def test_agent_transcription_uses_separate_queue(self, _notify):
        self.daemon._process(_job("agent"))
        self.assertEqual(self.daemon.agent_prompts.get_nowait(), "hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.time.monotonic", return_value=20.0)
    def test_stale_dictation_is_copied(self, _clock, copy, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.focus.window_id", return_value=8)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_focus_change_is_copied(self, _clock, _focus, copy, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.focus.window_id", return_value=None)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_unverifiable_focus_is_copied(self, _clock, _focus, copy, _notify):
        self.daemon._deliver_dictation(_job("dictate"), "hello")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.inject", return_value="wtype")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_same_window_is_typed(self, _clock, _focus, inject, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        inject.assert_called_once_with("hello", "wtype")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.inject")
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_ime_preview_commits_in_place(self, _clock, inject, _notify):
        ime = FakeIme()
        self.daemon._deliver_dictation(_job("dictate", ImePreview(ime, 1), 7), "hello")
        self.assertEqual(ime.commits, [("hello", 1)])
        inject.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.focus.window_id", return_value=8)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_lost_ime_field_falls_back_to_focus_guard(self, _clock, _focus, copy, _notify):
        ime = FakeIme()
        ime.commit_result = False
        self.daemon._deliver_dictation(_job("dictate", ImePreview(ime, 1), 7), "hello")
        self.assertEqual(ime.preedits, [("", 1)], "stale preedit is cleared")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    def test_full_agent_queue_recovers_prompt(self, _notify):
        for _ in range(self.daemon.agent_prompts.maxsize):
            self.daemon.agent_prompts.put_nowait("occupied")
        with patch("voicekey.daemon.recovery.save", return_value="/secure/path") as save:
            self.daemon._process(_job("agent"))
        save.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    def test_empty_transcript_clears_preview(self, _notify):
        self.daemon.backend.transcribe.return_value = ""
        ime = FakeIme()
        self.daemon._process(_job("dictate", ImePreview(ime, 1)))
        self.assertEqual(ime.preedits, [("", 1)])
        self.assertTrue(self.daemon.agent_prompts.empty())


class BindingsTests(unittest.TestCase):
    def test_bindings_describe_configured_keys(self):
        daemon = Daemon(Config(dictate_toggle_key="KEY_CONFIG"))
        self.assertEqual(daemon.bindings(), [
            "KEY_F9=dictate(hold)", "KEY_F10=agent(hold)", "KEY_CONFIG=dictate(toggle)",
        ])


class SessionTests(unittest.TestCase):
    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_live_text_goes_to_the_active_field(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=3)
        daemon.streaming = Mock()
        daemon.streaming.session.return_value = FakeStream(["hel", "hello", "hello there"])
        code = next(iter(next(
            chord for chord, action in daemon.actions.items() if action == ("dictate", "hold")
        )))

        daemon._on_key("/dev/input/event3", code, 1)
        self.assertIs(daemon.session.ime, daemon.ime)
        daemon.recorder.on_frame(np.zeros(1600, dtype=np.float32))
        self.assertIsInstance(daemon.session.preview, ImePreview)
        daemon.recorder.on_frame(np.zeros(1600, dtype=np.float32))
        daemon._on_key("/dev/input/event3", code, 0)

        self.assertEqual(daemon.ime.preedits, [("hel", 3), ("hello", 3), ("hello there", 3)])
        job = daemon.jobs.get_nowait()
        self.assertEqual(job.live_text, "hello there")
        self.assertEqual(job.window_id, 7)

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_agent_and_inactive_ime_preview_in_notifications(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=None)
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertIsInstance(daemon.session.preview, NotifyPreview)
        daemon._finish()
        daemon.ime = FakeIme(generation=1)
        daemon._start("/dev/input/event3", frozenset(), "hold", "agent", "release")
        self.assertIsInstance(daemon.session.preview, NotifyPreview)

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=None)
    def test_no_focused_window_means_no_in_field_text(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=1)  # e.g. a lock screen's password field
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertIsInstance(daemon.session.preview, NotifyPreview)

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_toggle_key_uses_two_presses_and_ignores_release(self, _focus, _notify):
        daemon = Daemon(Config(dictate_toggle_key="KEY_CONFIG"))
        daemon.recorder = FakeRecorder()
        code = next(iter(next(
            chord for chord, action in daemon.actions.items() if action == ("dictate", "toggle")
        )))

        daemon._on_key("/dev/input/event9", code, 1)
        daemon._on_key("/dev/input/event9", code, 0)
        self.assertTrue(daemon.recorder.active)
        self.assertEqual((daemon.recorder.starts, daemon.recorder.stops), (1, 0))

        daemon._on_key("/dev/input/event9", code, 1)
        self.assertFalse(daemon.recorder.active)
        self.assertEqual(daemon.recorder.stops, 1)
        self.assertEqual(daemon.jobs.get_nowait().action, "dictate")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_hold_key_starts_on_press_and_stops_on_release(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        code = next(iter(next(
            chord for chord, action in daemon.actions.items() if action == ("dictate", "hold")
        )))

        daemon._on_key("/dev/input/event3", code, 1)
        self.assertTrue(daemon.recorder.active)
        daemon._on_key("/dev/input/event3", code, 0)
        self.assertFalse(daemon.recorder.active)
        self.assertEqual(daemon.jobs.get_nowait().action, "dictate")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_longest_matching_chord_selects_agent(self, _focus, _notify):
        daemon = Daemon(Config(dictate_key="KEY_F23", agent_key="KEY_RIGHTALT+KEY_F23"))
        daemon.recorder = FakeRecorder()

        daemon._on_key("/dev/input/event3", 100, 1)  # KEY_RIGHTALT
        daemon._on_key("/dev/input/event3", 193, 1)  # KEY_F23
        self.assertEqual(daemon.session.action, "agent")
        daemon._on_key("/dev/input/event3", 193, 0)
        self.assertEqual(daemon.jobs.get_nowait().action, "agent")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_bare_shared_key_selects_dictation(self, _focus, _notify):
        daemon = Daemon(Config(dictate_key="KEY_F23", agent_key="KEY_RIGHTALT+KEY_F23"))
        daemon.recorder = FakeRecorder()

        daemon._on_key("/dev/input/event3", 193, 1)  # KEY_F23
        self.assertEqual(daemon.session.action, "dictate")
        daemon._on_key("/dev/input/event3", 193, 0)
        self.assertEqual(daemon.jobs.get_nowait().action, "dictate")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    def test_stuck_key_aborts_and_clears_preview(self, _focus, _notify):
        daemon = Daemon(Config(max_seconds=5))
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        daemon.recorder.elapsed = 6.0
        daemon._on_tick()
        self.assertIsNone(daemon.session)
        self.assertEqual(daemon.ime.preedits, [("", 1)])


if __name__ == "__main__":
    unittest.main()

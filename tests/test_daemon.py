from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from voicekey import daemon as daemon_mod
from voicekey.config import Config
from voicekey.daemon import Daemon, ImePreview, Job, NotifyPreview, Session, Spacing
from voicekey.ime import ImeHung
from voicekey.focus import Focus


def _focused(window=7, app="ghostty"):
    return patch("voicekey.daemon.focus.focused", return_value=Focus(window, app))

FRAME = np.zeros(1600, dtype=np.float32)


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
    def __init__(self, generation=1, activation_delay_calls=0):
        self.generation = generation
        self.preedits = []
        self.commits = []
        self.commit_result = True
        self.rebinds = 0
        self._delay = activation_delay_calls

    def activation(self):
        if self._delay > 0:
            self._delay -= 1
            return None
        return self.generation

    def rebind(self):
        self.rebinds += 1
        return True

    def before_cursor(self):
        return None

    def preedit(self, text, generation):
        self.preedits.append((text, generation))

    def commit(self, text, generation):
        self.commits.append((text, generation))
        return self.commit_result


class FakeStream:
    def __init__(self, texts, fail=False):
        self.texts = list(texts)
        self.fail = fail

    def feed(self, frame):
        if self.fail:
            raise RuntimeError("decoder exploded")
        return self.texts.pop(0)

    def finish(self):
        return self.texts.pop(0)


def _job(action, preview=None, window_id=None):
    return Job(np.zeros(16000, dtype=np.float32), action, 1.0, window_id,
               preview or NotifyPreview(action), "live")


def _dictate_code(daemon):
    return next(iter(next(
        chord for chord, action in daemon.actions.items() if action == ("dictate", "hold")
    )))


class DeliveryTests(unittest.TestCase):
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
    @patch("voicekey.daemon.inject_mod.type_text")
    @patch("voicekey.daemon.focus.window_id", return_value=8)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_focus_change_is_copied(self, _clock, _focus, type_text, copy, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        copy.assert_called_once_with("hello")
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.type_text")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_same_window_is_typed(self, _clock, _focus, type_text, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        type_text.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.inject_mod.type_text", side_effect=RuntimeError("wtype missing"))
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_typing_failure_copies_instead_of_pasting(self, _clock, _focus, _type, copy, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.inject_mod.type_text")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_clipboard_mode_only_copies(self, _clock, _focus, type_text, copy, _notify):
        self.daemon.cfg.dictation.inject = "clipboard"
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        copy.assert_called_once_with("hello")
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.type_text")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_ime_preview_commits_in_place(self, _clock, _focus, type_text, _notify):
        ime = FakeIme()
        self.daemon._deliver_dictation(_job("dictate", ImePreview(ime, 1), 7), "hello")
        self.assertEqual(ime.commits, [("hello", 1)])
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.inject_mod.type_text")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_lost_field_is_copied_never_typed(self, _clock, _focus, type_text, copy, _notify):
        ime = FakeIme()
        ime.commit_result = False
        self.daemon._deliver_dictation(_job("dictate", ImePreview(ime, 1), 7), "hello")
        self.assertEqual(ime.preedits, [("", 1)], "stale preedit is cleared")
        copy.assert_called_once_with("hello")
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.focus.window_id", return_value=8)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_window_is_checked_before_ime_commit(self, _clock, _focus, copy, _notify):
        ime = FakeIme()
        self.daemon._deliver_dictation(_job("dictate", ImePreview(ime, 1), 7), "hello")
        self.assertEqual(ime.commits, [])
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

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.recovery.keep", side_effect=OSError("disk full"))
    @patch("voicekey.daemon.inject_mod.type_text")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_recording_log_failure_does_not_lose_text(self, _clock, _focus, type_text, _keep, _notify):
        self.daemon.cfg.recordings_dir = "/nowhere"
        self.daemon._process(_job("dictate", window_id=7))
        type_text.assert_called_once_with("hello")


class SessionTests(unittest.TestCase):
    @patch("voicekey.daemon.notify")
    @_focused()
    def test_field_is_bound_at_key_down_and_live_text_goes_there(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=3, activation_delay_calls=2)
        daemon.streaming = Mock()
        daemon.streaming.session.return_value = FakeStream(["hel", "hello", "hello there"])
        code = _dictate_code(daemon)

        daemon._on_key("/dev/input/event3", code, 1)
        self.assertEqual(daemon.ime.rebinds, 1)
        self.assertIsInstance(daemon.session.preview, ImePreview)
        self.assertEqual(daemon.session.preview.generation, 3)
        daemon.recorder.on_frame(FRAME)
        daemon.recorder.on_frame(FRAME)
        daemon._on_key("/dev/input/event3", code, 0)

        self.assertEqual(daemon.ime.preedits, [("hel", 3), ("hello", 3), ("hello there", 3)])
        job = daemon.jobs.get_nowait()
        self.assertEqual(job.live_text, "hello there")
        self.assertEqual(job.window_id, 7)

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_no_rebind_while_previous_text_is_landing(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=5)
        daemon.jobs.put_nowait(_job("dictate"))  # still being transcribed
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertEqual(daemon.ime.rebinds, 0)
        self.assertEqual(daemon.session.preview.generation, 5)

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_agent_and_inactive_field_preview_in_notifications(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=None)
        with patch.object(daemon_mod, "ACTIVATION_WAIT", 0.01):
            daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertIsInstance(daemon.session.preview, NotifyPreview)
        daemon._finish()
        daemon.jobs.get_nowait()
        daemon.ime = FakeIme(generation=1)
        daemon._start("/dev/input/event3", frozenset(), "hold", "agent", "release")
        self.assertIsInstance(daemon.session.preview, NotifyPreview)
        self.assertEqual(daemon.ime.rebinds, 0)

    @patch("voicekey.daemon.notify")
    @_focused(window=None, app=None)
    def test_no_focused_window_means_no_in_field_text(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=1)  # e.g. a lock screen's password field
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertIsInstance(daemon.session.preview, NotifyPreview)

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_live_recognizer_failure_keeps_the_recording(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.streaming = Mock()
        daemon.streaming.session.return_value = FakeStream([], fail=True)
        code = _dictate_code(daemon)
        daemon._on_key("/dev/input/event3", code, 1)
        session = daemon.session
        daemon.recorder.on_frame(FRAME)
        daemon._on_key("/dev/input/event3", code, 0)
        self.assertFalse(session.live)
        self.assertEqual(daemon.jobs.get_nowait().live_text, "")

    def test_slow_live_recognizer_drops_only_the_preview(self):
        release = threading.Event()

        class SlowStream:
            def feed(self, frame):
                release.wait(5)
                return "x"

            def finish(self):
                return "x"

        session = Session("dictate", "hold", frozenset(), "dev")
        session.attach(SlowStream())
        for _ in range(daemon_mod.OVERLOAD_FRAMES + 2):
            session.feed(FRAME)
        self.assertFalse(session.live)
        release.set()
        session.finish()  # must not block or raise

    def test_partial_decoded_during_cancel_is_discarded(self):
        started = threading.Event()
        release = threading.Event()

        class SlowStream:
            def feed(self, frame):
                started.set()
                release.wait(5)
                return "late partial"

            def finish(self):
                return "late partial"

        session = Session("dictate", "hold", frozenset(), "dev")
        ime = FakeIme()
        session.preview = ImePreview(ime, 1)
        session.attach(SlowStream())
        session.feed(FRAME)
        self.assertTrue(started.wait(2))
        session.cancel()          # e.g. the release timed out and the final text was committed
        session.preview.clear()
        release.set()
        session.decoder.join(2)
        self.assertEqual(ime.preedits, [("", 1)], "no partial after the clear")

    @patch("voicekey.daemon.notify")
    def test_previews_ignore_updates_after_commit_or_clear(self, notify):
        ime = FakeIme()
        preview = ImePreview(ime, 1)
        preview.commit("final")
        preview.update("late")
        self.assertEqual(ime.preedits, [])
        shown = NotifyPreview("dictate")
        shown.clear()
        shown.update("late")
        notify.assert_not_called()

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_stuck_decoder_disables_the_preview_until_it_exits(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.streaming = Mock()
        hang = threading.Event()
        daemon._stuck = threading.Thread(target=hang.wait, daemon=True)
        daemon._stuck.start()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        daemon.streaming.session.assert_not_called()
        self.assertFalse(daemon.session.live)
        hang.set()

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_preview_setup_failure_keeps_the_recording(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.streaming = Mock()
        daemon.streaming.session.side_effect = RuntimeError("no stream for you")
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertTrue(daemon.recorder.active)
        self.assertIsInstance(daemon.session.preview, NotifyPreview)
        daemon._finish()
        self.assertEqual(daemon.jobs.get_nowait().action, "dictate")

    @patch("voicekey.daemon.notify")
    @_focused()
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
    @_focused()
    def test_hold_key_starts_on_press_and_stops_on_release(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        code = _dictate_code(daemon)

        daemon._on_key("/dev/input/event3", code, 1)
        self.assertTrue(daemon.recorder.active)
        daemon._on_key("/dev/input/event3", code, 0)
        self.assertFalse(daemon.recorder.active)
        self.assertEqual(daemon.jobs.get_nowait().action, "dictate")

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_longest_matching_chord_selects_agent(self, _focus, _notify):
        daemon = Daemon(Config(dictate_key="KEY_F23", agent_key="KEY_RIGHTALT+KEY_F23"))
        daemon.recorder = FakeRecorder()

        daemon._on_key("/dev/input/event3", 100, 1)  # KEY_RIGHTALT
        daemon._on_key("/dev/input/event3", 193, 1)  # KEY_F23
        self.assertEqual(daemon.session.action, "agent")
        daemon._on_key("/dev/input/event3", 193, 0)
        self.assertEqual(daemon.jobs.get_nowait().action, "agent")

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_bare_shared_key_selects_dictation(self, _focus, _notify):
        daemon = Daemon(Config(dictate_key="KEY_F23", agent_key="KEY_RIGHTALT+KEY_F23"))
        daemon.recorder = FakeRecorder()

        daemon._on_key("/dev/input/event3", 193, 1)  # KEY_F23
        self.assertEqual(daemon.session.action, "dictate")
        daemon._on_key("/dev/input/event3", 193, 0)
        self.assertEqual(daemon.jobs.get_nowait().action, "dictate")

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_stuck_key_aborts_and_clears_preview(self, _focus, _notify):
        daemon = Daemon(Config(max_seconds=5))
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        daemon.recorder.elapsed = 6.0
        daemon._on_tick()
        self.assertIsNone(daemon.session)
        self.assertEqual(daemon.ime.preedits, [("", 1)])


class SpacingRuleTests(unittest.TestCase):
    def test_character_before_the_cursor_decides_when_reported(self):
        spacing = Spacing()
        self.assertEqual(spacing.owed(".", "firefox", 7), " ")
        for before in ("\n", " ", "(", "“", ""):
            self.assertEqual(spacing.owed(before, "firefox", 7), "", repr(before))

    def test_emacs_reports_nothing_useful_so_continuation_decides(self):
        spacing = Spacing()
        self.assertEqual(spacing.owed("", "emacs", 7), "")
        spacing.inserted(7, "First.", spacing.mark())
        self.assertEqual(spacing.owed("", "emacs", 7), " ")
        self.assertEqual(spacing.owed("", "firefox", 7), "", "a real empty field")

    def test_unknown_surroundings_continue_only_our_own_text_in_the_same_window(self):
        spacing = Spacing()
        self.assertEqual(spacing.owed(None, "ghostty", 7), "")
        spacing.inserted(7, "First.", spacing.mark())
        self.assertEqual(spacing.owed(None, "ghostty", 7), " ")
        self.assertEqual(spacing.owed(None, "ghostty", 8), "")
        self.assertEqual(spacing.owed(None, None, None), "")
        spacing.inserted(7, "First.\n", spacing.mark())
        self.assertEqual(spacing.owed(None, "ghostty", 7), "", "ended with a newline")

    def test_typing_in_between_hands_spacing_back_even_if_it_races_the_insertion(self):
        spacing = Spacing()
        mark = spacing.mark()
        spacing.user_typed()          # arrives after the commit, before inserted()
        spacing.inserted(7, "First.", mark)
        self.assertEqual(spacing.owed(None, "ghostty", 7), "", "the user typed last")
        spacing.inserted(7, "Again.", spacing.mark())
        spacing.user_typed()
        self.assertEqual(spacing.owed(None, "ghostty", 7), "")

    def test_landing_is_counted_per_window(self):
        spacing = Spacing()
        spacing.queued(7)
        spacing.queued(8)
        spacing.queued(7)
        self.assertEqual(spacing.predict(7, "ghostty", ""), " ")
        spacing.landed(7)  # one of the two for window 7 delivered
        self.assertEqual(spacing.predict(7, "ghostty", ""), " ", "another is still queued")
        spacing.landed(8)
        self.assertEqual(spacing.predict(8, "ghostty", ""), "")
        spacing.landed(7)
        self.assertEqual(spacing.predict(7, "ghostty", ""), "")

    def test_prediction_assumes_our_landing_text_and_settlement_uses_what_landed(self):
        spacing = Spacing()
        spacing.queued(7)
        self.assertEqual(spacing.predict(7, "ghostty", "."), " ")
        self.assertEqual(spacing.predict(8, "ghostty", ""), "", "another window: not ours")
        job = Job(np.zeros(1), "dictate", 1.0, 7, NotifyPreview("dictate"), "live",
                  "ghostty", ".", True)
        self.assertEqual(spacing.settle(job), "", "the stale '.' is ignored; nothing landed")
        spacing.inserted(7, "First.", spacing.mark())
        self.assertEqual(spacing.settle(job), " ")
        job = Job(np.zeros(1), "dictate", 1.0, 7, NotifyPreview("dictate"), "live",
                  "ghostty", "", False)
        self.assertEqual(spacing.settle(job), "", "nothing was landing: the field's own report wins")

    def test_punctuation_joins_without_a_space(self):
        self.assertEqual(daemon_mod.spaced(" ", ", however"), ", however")
        self.assertEqual(daemon_mod.spaced(" ", "next"), " next")
        self.assertEqual(daemon_mod.spaced("", "next"), "next")


class SpacingIntegrationTests(unittest.TestCase):
    def _daemon(self, before=None, window=7, app="ghostty", landing=None, queued=None):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=1)
        daemon.ime.before_cursor = lambda: before
        if landing is not None:
            daemon.spacing.queued(landing)
        if queued:
            daemon.jobs.put_nowait(_job(queued, window_id=9))
        with patch("voicekey.daemon.notify"), _focused(window, app):
            daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        return daemon

    def test_preview_and_commit_carry_the_predicted_space(self):
        daemon = self._daemon(before=".")
        daemon.session.preview.update("Second thought")
        self.assertEqual(daemon.ime.preedits, [(" Second thought", 1)])
        daemon.session.preview.commit("Second thought.")
        self.assertEqual(daemon.ime.commits, [(" Second thought.", 1)])

    def test_pending_agent_or_other_window_work_does_not_hide_the_field(self):
        daemon = self._daemon(before="", queued="agent")
        self.assertFalse(daemon.session.after_landing)
        self.assertEqual(daemon.session.before, "")
        self.assertEqual(daemon.session.prefix, "")
        daemon = self._daemon(before="", landing=9)  # a dictation landing elsewhere
        self.assertFalse(daemon.session.after_landing)
        self.assertEqual(daemon.session.prefix, "")

    def test_rapid_second_dictation_is_settled_after_the_first_lands(self):
        daemon = self._daemon(before=".", landing=7)
        self.assertTrue(daemon.session.after_landing)
        self.assertEqual(daemon.session.prefix, " ")
        daemon._finish()
        daemon.jobs.get_nowait()
        daemon.spacing.inserted(7, "First.", daemon.spacing.mark())
        ime = FakeIme()
        job = Job(np.zeros(1), "dictate", 1.0, 7, ImePreview(ime, 1), "live", "ghostty", ".", True)
        with patch("voicekey.daemon.notify"), patch("voicekey.daemon.focus.window_id", return_value=7), \
                patch("voicekey.daemon.time.monotonic", return_value=2.0):
            daemon._deliver_dictation(job, "Second.")
        self.assertEqual(ime.commits, [(" Second.", 1)])

    def test_queued_dictation_counts_as_landing_until_the_worker_is_done(self):
        daemon = self._daemon()
        daemon._finish()
        self.assertTrue(daemon.spacing.landing_in(7))
        daemon.backend = Mock()
        daemon.backend.transcribe.return_value = ""
        with patch("voicekey.daemon.notify"):
            threading.Thread(target=daemon._transcription_worker, daemon=True).start()
            daemon.jobs.join()
        self.assertFalse(daemon.spacing.landing_in(7))

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.inject_mod.type_text")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_hung_input_method_saves_the_text_and_neither_types_nor_copies(
            self, _clock, _focus, type_text, copy, _notify):
        daemon = Daemon(Config())
        preview = ImePreview(FakeIme(), 1)
        preview.ime.commit = Mock(side_effect=ImeHung("stopped responding"))
        with patch("voicekey.daemon.recovery.save", return_value="/secure/path") as save:
            daemon._deliver_dictation(_job("dictate", preview, 7), "Hello.")
        save.assert_called_once_with("Hello.")
        copy.assert_not_called()
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_a_copy_leaves_the_spacing_state_alone(self, _clock, _focus, _notify):
        daemon = Daemon(Config())
        daemon.spacing.inserted(7, "Hello.", daemon.spacing.mark())
        preview = ImePreview(FakeIme(), 1)
        preview.ime.commit_result = False
        with patch("voicekey.daemon.inject_mod.copy"):
            daemon._deliver_dictation(_job("dictate", preview, 7), "Hello.")
        self.assertEqual(daemon.spacing.owed(None, "ghostty", 7), " ")


class BindingsTests(unittest.TestCase):
    def test_bindings_describe_configured_keys(self):
        daemon = Daemon(Config(dictate_toggle_key="KEY_CONFIG"))
        self.assertEqual(daemon.bindings(), [
            "KEY_F9=dictate(hold)", "KEY_F10=agent(hold)", "KEY_CONFIG=dictate(toggle)",
        ])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import itertools
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from voicekey import daemon as daemon_mod
from voicekey import target as target_mod
from voicekey.config import Config
from voicekey.daemon import Daemon, Job, Session, Spacing
from voicekey.target import (
    ClipboardTarget, EmacsTarget, ImePreview, ImeTarget, NotifyPreview, Window, WtypeTarget,
)
from voicekey.emacs import EmacsError, EmacsTimeout, Pin
from voicekey.gate import Gate
from voicekey.ime import ImeHung
from voicekey.focus import Focus


def _focused(window=7, app="ghostty"):
    return patch("voicekey.target.focus.focused", return_value=Focus(window, app))


def _ticking(start=2.0, step=1.0):
    """A clock that advances on every read and a sleep that does not, so a
    delivery waiting for a window to come back runs out of budget at once."""
    def decorate(test):
        clock = itertools.count(start, step)
        test = patch("voicekey.target.time.sleep", new=lambda seconds: None)(test)
        test = patch("voicekey.target.time.monotonic", new=lambda: next(clock))(test)
        return patch("voicekey.daemon.time.monotonic", side_effect=lambda: next(clock))(test)
    return decorate

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
        self.surrounding = None  # (before, after) the field reports, or None
        self.showing = ""  # the preedit it had when it was deactivated
        self.replacements = []

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

    def surrounding_text(self):
        return self.surrounding

    def left_showing(self):
        return self.showing

    def preedit(self, text, generation):
        self.preedits.append((text, generation))

    def commit(self, text, generation):
        self.commits.append((text, generation))
        return self.commit_result and generation == self.generation

    def replace(self, before, text, generation):
        self.replacements.append((before, text, generation))
        return self.commit_result and generation == self.generation


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


def _ime_target(ime, generation=1, window_id=7, app="ghostty", verify=True):
    return ImeTarget(ime, generation, Window(window_id, verify), app)


def _emacs_target(preview=None, pin_id="pin-1", before=None, window_id=7):
    pinning = Mock()
    pinning.id = pin_id
    pinning.before.return_value = before
    return EmacsTarget(preview or NotifyPreview("dictate"), Window(window_id, True), "emacs", pinning)


def _job(action, target=None, window_id=None, before=None):
    """A transcribed recording as the workers see it: an agent prompt only
    shows live text; a dictation lands through its target, by default one
    that types into WINDOW_ID with wtype."""
    if target is None:
        if action == "agent":
            target = NotifyPreview(action)
        else:
            target = WtypeTarget(NotifyPreview("dictate"), Window(window_id, True), "ghostty")
    window_id = getattr(target, "window_id", window_id)
    app_id = getattr(target, "app_id", None)
    return Job(np.zeros(16000, dtype=np.float32), action, 1.0, window_id, target, "live",
               app_id, before)


def _dictate_code(daemon):
    return next(iter(next(
        chord for chord, action in daemon.actions.items() if action == ("dictate", "hold")
    )))


def _no_real_recovery_file(test: unittest.TestCase) -> None:
    """Every copy saves a transcript; tests must not touch the real one."""
    saver = patch("voicekey.daemon.recovery.save", return_value="/secure/path")
    saver.start()
    test.addCleanup(saver.stop)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        _no_real_recovery_file(self)
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
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.focus.window_id", return_value=8)
    @_ticking()
    def test_focus_change_is_copied_and_saved(self, _clock, _focus, type_text, copy, notify):
        with patch("voicekey.daemon.recovery.save", return_value="/secure/path") as save:
            self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        copy.assert_called_once_with("hello")
        type_text.assert_not_called()
        save.assert_called_once_with("hello")  # an agent's wl-copy could clobber the clipboard
        self.assertIn("/secure/path", notify.call_args.args[1])

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.focus.window_id", return_value=8)
    @_ticking()
    def test_a_copy_survives_a_failed_recovery_save(self, _clock, _focus, copy, notify):
        with patch("voicekey.daemon.recovery.save", side_effect=OSError("read-only fs")):
            self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        copy.assert_called_once_with("hello")
        self.assertIn("recovery also failed", notify.call_args.args[1])

    @patch("voicekey.daemon.notify")
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_same_window_is_typed(self, _clock, _focus, type_text, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        type_text.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.inject_mod.type_text", side_effect=RuntimeError("wtype missing"))
    @patch("voicekey.target.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_typing_failure_copies_instead_of_pasting(self, _clock, _focus, _type, copy, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_clipboard_mode_only_copies(self, _clock, _focus, type_text, copy, _notify):
        target = ClipboardTarget(NotifyPreview("dictate"), Window(7, True), "ghostty")
        self.daemon._deliver_dictation(_job("dictate", target), "hello")
        copy.assert_called_once_with("hello")
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_ime_preview_commits_in_place(self, _clock, _focus, type_text, _notify):
        ime = FakeIme()
        self.daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "hello")
        self.assertEqual(ime.commits, [("hello", 1)])
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @_ticking()
    def test_lost_field_is_copied_never_typed(self, _clock, _focus, type_text, copy, _notify):
        ime = FakeIme()
        ime.commit_result = False
        self.daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "hello")
        self.assertEqual(ime.preedits, [("", 1)], "stale preedit is cleared")
        copy.assert_called_once_with("hello")
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.focus.window_id", return_value=8)
    @_ticking()
    def test_window_is_checked_before_ime_commit(self, _clock, _focus, copy, _notify):
        ime = FakeIme()
        self.daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "hello")
        self.assertEqual(ime.commits, [])
        copy.assert_called_once_with("hello")

    @patch("voicekey.target.notify")
    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.focus.window_id", side_effect=[8, 8, 7, 7])
    @_ticking()
    def test_focus_stolen_and_returned_lands_in_the_field_again(self, _clock, _focus, copy, _notify, notify):
        # Focus went to an agent's browser mid-dictation; the field was
        # deactivated and, being a terminal, dropped the preedit. When the
        # window is focused again the field activates afresh (generation 2)
        # and the text lands there.
        ime = FakeIme(generation=2)
        self.daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "hello")
        self.assertEqual(ime.commits, [("hello", 2)])
        copy.assert_not_called()
        self.assertIn("Waiting for focus", str(notify.call_args_list))

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @_ticking()
    def test_provisional_text_the_application_kept_is_replaced(self, _clock, _focus, copy, _notify):
        # Chromium turns the preedit into real text when focus leaves; it is
        # right before the cursor on return, so the final text replaces it.
        ime = FakeIme(generation=2)
        ime.showing = " hello wor"
        ime.surrounding = ("Dear all, hello wor", "")
        self.daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "hello world")
        self.assertEqual(ime.replacements, [(len(" hello wor"), "hello world", 2)])
        self.assertEqual(ime.commits, [("hello world", 1)], "the first attempt, refused as stale")
        copy.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @_ticking()
    def test_provisional_text_kept_away_from_the_cursor_is_never_typed_over(self, _clock, _focus, copy, notify):
        ime = FakeIme(generation=2)
        ime.showing = " hello wor"
        ime.surrounding = ("Dear all, hello wor and then", "")
        self.daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "hello world")
        self.assertEqual(ime.replacements, [])
        self.assertEqual(ime.commits, [("hello world", 1)], "only the stale attempt; nothing typed over")
        copy.assert_called_once_with("hello world")
        self.assertIn("kept the live text elsewhere", str(notify.call_args))

    @patch("voicekey.target.notify")
    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.focus.window_id", return_value=8)
    @_ticking()
    def test_a_window_that_never_comes_back_is_copied_after_the_budget(self, _clock, _focus, copy, _notify, notify):
        ime = FakeIme(generation=2)
        self.daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "hello")
        self.assertEqual(ime.commits, [])
        copy.assert_called_once_with("hello")
        self.assertIn("Waiting for focus", str(notify.call_args_list), "it waited")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_without_a_verifiable_window_nothing_is_waited_for(self, _clock, copy, _notify):
        # A frozen clock: any wait would hang the test.
        ime = FakeIme(generation=2)
        self.daemon._deliver_dictation(_job("dictate", _ime_target(ime, verify=False)), "hello")
        self.assertEqual(ime.commits, [("hello", 1)], "tried once, with the field's own generation")
        copy.assert_called_once_with("hello")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.focus.window_id", side_effect=[8, 8, 7])
    @_ticking()
    def test_typing_waits_for_the_window_too(self, _clock, _focus, type_text, copy, _notify):
        self.daemon._deliver_dictation(_job("dictate", window_id=7), "hello")
        type_text.assert_called_once_with("hello")
        copy.assert_not_called()

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
        self.daemon._process(_job("dictate", _ime_target(ime)))
        self.assertEqual(ime.preedits, [("", 1)])
        self.assertTrue(self.daemon.agent_prompts.empty())

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.recovery.keep", side_effect=OSError("disk full"))
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_recording_log_failure_does_not_lose_text(self, _clock, _focus, type_text, _keep, _notify):
        self.daemon.cfg.recordings_dir = "/nowhere"
        self.assertTrue(self.daemon._process(_job("dictate", window_id=7)))
        job, text = self.daemon.deliveries.get_nowait()
        self.daemon._deliver_dictation(job, text)
        type_text.assert_called_once_with("hello")


class WorkerTests(unittest.TestCase):
    """Transcription and delivery are separate workers: on 2026-08-30 one
    hung wl-copy (15 s) held the single worker, the six dictations behind
    it aged past max_delay_seconds waiting to be transcribed, and each was
    copied and hung in turn."""

    def setUp(self):
        _no_real_recovery_file(self)
        self.daemon = Daemon(Config())
        self.daemon.backend = Mock()
        self.daemon.backend.transcribe.return_value = "hello"

    def _run_workers(self, deliver):
        with patch.object(self.daemon, "_deliver_dictation", side_effect=deliver):
            threading.Thread(target=self.daemon._transcription_worker, daemon=True).start()
            threading.Thread(target=self.daemon._delivery_worker, daemon=True).start()
            self.daemon.jobs.join()
            self.daemon.deliveries.join()

    @patch("voicekey.daemon.notify")
    def test_a_hung_delivery_does_not_delay_the_next_transcription(self, _notify):
        delivering = threading.Event()
        release = threading.Event()

        def deliver(job, text):
            delivering.set()
            release.wait(5)

        for _ in range(2):
            self.daemon.spacing.queued(7)
            self.daemon.jobs.put_nowait(_job("dictate", window_id=7))
        with patch.object(self.daemon, "_deliver_dictation", side_effect=deliver):
            threading.Thread(target=self.daemon._transcription_worker, daemon=True).start()
            threading.Thread(target=self.daemon._delivery_worker, daemon=True).start()
            self.assertTrue(delivering.wait(2))
            self.daemon.jobs.join()  # the second recording is transcribed meanwhile
            self.assertEqual(self.daemon.backend.transcribe.call_count, 2)
            self.assertTrue(self.daemon.spacing.landing_in(7), "nothing has landed yet")
            release.set()
            self.daemon.deliveries.join()
        self.assertFalse(self.daemon.spacing.landing_in(7))

    @patch("voicekey.daemon.notify")
    def test_a_delivery_that_blows_up_saves_the_text(self, _notify):
        self.daemon.spacing.queued(7)
        self.daemon.jobs.put_nowait(_job("dictate", window_id=7))
        with patch("voicekey.daemon.recovery.save", return_value="/secure/path") as save:
            self._run_workers(deliver=Mock(side_effect=RuntimeError("boom")))
        save.assert_called_once_with("hello")
        self.assertFalse(self.daemon.spacing.landing_in(7))

    @patch("voicekey.daemon.notify")
    def test_an_empty_transcript_lands_nowhere_and_says_so(self, _notify):
        self.daemon.backend.transcribe.return_value = ""
        self.daemon.spacing.queued(7)
        self.daemon.jobs.put_nowait(_job("dictate", window_id=7))
        deliver = Mock()
        self._run_workers(deliver)
        deliver.assert_not_called()
        self.assertFalse(self.daemon.spacing.landing_in(7))


class PolishStageTests(unittest.TestCase):
    """With a polish model configured, a third worker sits between
    transcription and delivery: the raw transcript is shown meanwhile and
    lands unchanged whenever the model is late, fails or is not trusted."""

    def setUp(self):
        _no_real_recovery_file(self)
        self.daemon = Daemon(Config())
        self.daemon.backend = Mock()
        self.daemon.backend.transcribe.return_value = "hello"
        self.daemon.polisher = Mock()
        self.daemon.polisher.polish.return_value = "Hello."

    def _polish(self, job):
        self.daemon.spacing.queued(job.window_id)
        self.assertTrue(self.daemon._process(job))
        self.assertTrue(self.daemon._landing(), "the gate stays held while the model works")
        threading.Thread(target=self.daemon._polish_worker, daemon=True).start()
        self.daemon.polishing.join()

    @staticmethod
    def _fresh(job):
        """Just released, so the delivery budget is whole."""
        return daemon_mod.replace(job, finished_at=time.monotonic())

    @patch("voicekey.daemon.notify")
    def test_the_polished_text_is_what_lands(self, _notify):
        job = self._fresh(_job("dictate", window_id=7))
        self._polish(job)
        self.assertEqual(self.daemon.deliveries.get_nowait(), (job, "Hello."))
        self.assertTrue(self.daemon.spacing.landing_in(7), "still owned by the delivery worker")

    @patch("voicekey.daemon.notify")
    def test_the_raw_text_is_shown_meanwhile_and_lands_when_the_model_fails(self, _notify):
        self.daemon.polisher.polish.return_value = None
        ime = FakeIme()
        job = self._fresh(_job("dictate", _ime_target(ime)))
        self._polish(job)
        self.assertEqual(ime.preedits, [("hello", 1)])
        self.assertEqual(self.daemon.deliveries.get_nowait(), (job, "hello"))

    @patch("voicekey.daemon.notify")
    def test_a_worker_bug_still_lands_the_raw_text(self, _notify):
        self.daemon.polisher.polish.side_effect = RuntimeError("bug")
        job = self._fresh(_job("dictate", window_id=7))
        self._polish(job)
        self.assertEqual(self.daemon.deliveries.get_nowait(), (job, "hello"))

    @patch("voicekey.daemon.notify")
    def test_nothing_left_after_cleanup_lands_nowhere_and_says_so(self, notify):
        self.daemon.polisher.polish.return_value = ""
        ime = FakeIme()
        self._polish(self._fresh(_job("dictate", _ime_target(ime))))
        self.assertTrue(self.daemon.deliveries.empty())
        self.assertEqual(ime.preedits[-1], ("", 1), "the preedit is cleared")
        self.assertFalse(self.daemon.spacing.landing_in(7))
        self.assertFalse(self.daemon._landing())
        self.assertIn("nothing to type", str(notify.call_args))

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.time.monotonic", return_value=8.5)
    def test_the_wait_fits_inside_the_delivery_budget(self, _clock, _notify):
        # finished_at is 1.0 and max_delay_seconds 10: 2.5 s of budget left,
        # one of which delivery needs, so the model gets 1.5 s, not 4.
        self._polish(_job("dictate", window_id=7))
        self.daemon.polisher.polish.assert_called_once_with("hello", 1.5)

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.time.monotonic", return_value=10.5)
    def test_no_budget_left_means_no_polish(self, _clock, _notify):
        job = _job("dictate", window_id=7)
        self._polish(job)
        self.daemon.polisher.polish.assert_not_called()
        self.assertEqual(self.daemon.deliveries.get_nowait(), (job, "hello"))

    @patch("voicekey.daemon.notify")
    def test_agent_prompts_and_empty_transcripts_skip_the_model(self, _notify):
        self.daemon._process(_job("agent"))
        self.assertEqual(self.daemon.agent_prompts.get_nowait(), "hello")
        self.daemon.backend.transcribe.return_value = ""
        self.assertFalse(self.daemon._process(_job("dictate", window_id=7)))
        self.assertTrue(self.daemon.polishing.empty())
        self.daemon.polisher.polish.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.recovery.keep")
    def test_the_recording_is_kept_once_with_every_transcript(self, keep, _notify):
        self.daemon.cfg.recordings_dir = "/tmp/recordings"
        job = self._fresh(_job("dictate", window_id=7))
        self._polish(job)
        keep.assert_called_once_with("/tmp/recordings", job.samples, "live", "hello", "Hello.")

    @patch("voicekey.daemon.notify")
    def test_without_a_model_the_pipeline_is_as_before(self, _notify):
        self.daemon.polisher = None
        job = _job("dictate", window_id=7)
        self.assertTrue(self.daemon._process(job))
        self.assertTrue(self.daemon.polishing.empty())
        self.assertEqual(self.daemon.deliveries.get_nowait(), (job, "hello"))


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
        self.assertIsInstance(daemon.session.target, ImeTarget)
        self.assertEqual(daemon.session.target.preview.generation, 3)
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
        self.assertEqual(daemon.session.target.preview.generation, 5)

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_agent_and_inactive_field_preview_in_notifications(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=None)
        with patch.object(target_mod, "ACTIVATION_WAIT", 0.01):
            daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertIsInstance(daemon.session.target.preview, NotifyPreview)
        daemon._finish()
        daemon.jobs.get_nowait()
        daemon.ime = FakeIme(generation=1)
        daemon._start("/dev/input/event3", frozenset(), "hold", "agent", "release")
        self.assertIsInstance(daemon.session.target, NotifyPreview)
        self.assertEqual(daemon.ime.rebinds, 0)

    @patch("voicekey.daemon.notify")
    @patch("voicekey.target.emacs_mod.pin", side_effect=lambda pin_id: Pin(pin_id, "."))
    @_focused(app="emacs")
    def test_emacs_buffer_is_pinned_at_key_down(self, _focus, pin, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertIsInstance(daemon.session.target, EmacsTarget)
        pin_id = daemon.session.target.pinning.id
        self.assertTrue(pin_id)
        pin.assert_called_once_with(pin_id)
        self.assertEqual(daemon.session.before, ".", "an idle Emacs answers in time for the preview")
        daemon._finish()
        self.assertEqual(daemon.jobs.get_nowait().target.pinning.id, pin_id)

    @patch("voicekey.daemon.notify")
    @patch("voicekey.target.emacs_mod.pin", side_effect=lambda pin_id: Pin(pin_id, "."))
    @_focused(app="emacs")
    def test_emacs_buffer_is_pinned_even_while_our_text_is_landing(self, _focus, pin, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.spacing.queued(7)
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertIsInstance(daemon.session.target, EmacsTarget)
        self.assertEqual(daemon.session.before, ".", "read anyway; settle() knows if it goes stale")

    @patch("voicekey.daemon.notify")
    @_focused(app="emacs")
    def test_a_busy_emacs_does_not_hold_up_the_key_down(self, _focus, _notify):
        # A dialog or a long command blocks Emacs's command loop; the pin
        # form is delivered and runs later, and the keyboard thread must
        # not wait for it (it used to wait up to 5 s at every key-down).
        release = threading.Event()

        def slow_pin(pin_id):
            release.wait(5)
            return Pin(pin_id, ".")

        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        with patch("voicekey.target.emacs_mod.pin", side_effect=slow_pin), \
                patch.object(daemon_mod, "PIN_WAIT", 0.01):
            started = time.monotonic()
            daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIsInstance(daemon.session.target, EmacsTarget)
            self.assertIsNone(daemon.session.before, "not known yet; settled at delivery")
            daemon._finish()
            job = daemon.jobs.get_nowait()
            release.set()  # Emacs is free again before delivery
            with patch("voicekey.target.emacs_mod.insert") as insert, \
                    patch("voicekey.daemon.time.monotonic", return_value=job.finished_at + 1.0):
                daemon._deliver_dictation(job, "Hello.")
            insert.assert_called_once_with(" Hello.", job.target.pinning.id, timeout=9.0)

    @patch("voicekey.daemon.notify")
    @patch("voicekey.target.emacs_mod.pin")
    @_focused()
    def test_other_applications_pin_nothing(self, _focus, pin, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        pin.assert_not_called()
        self.assertNotIsInstance(daemon.session.target, EmacsTarget)

    @patch("voicekey.daemon.notify")
    @_focused(window=None, app=None)
    def test_no_focused_window_means_no_in_field_text(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=1)  # e.g. a lock screen's password field
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertIsInstance(daemon.session.target.preview, NotifyPreview)

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
        session.target = _ime_target(ime)
        session.attach(SlowStream())
        session.feed(FRAME)
        self.assertTrue(started.wait(2))
        session.cancel()          # e.g. the release timed out and the final text was committed
        session.target.clear()
        release.set()
        session.decoder.join(2)
        self.assertEqual(ime.preedits, [("", 1)], "no partial after the clear")

    @patch("voicekey.daemon.notify")
    def test_previews_ignore_updates_after_commit_or_clear(self, notify):
        ime = FakeIme()
        preview = ImePreview(ime, 1)
        preview.commit("final")
        preview.show("late")
        self.assertEqual(ime.preedits, [])
        shown = NotifyPreview("dictate")
        shown.clear()
        shown.show("late")
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
        self.assertIsInstance(daemon.session.target.preview, NotifyPreview)
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
    def test_recording_past_max_seconds_is_stopped_and_transcribed(self, _focus, notify):
        # A stuck key cannot be told from a long thought until release, so
        # the recording is delivered, not discarded (ten days of use: the
        # longest recording was 68 s, 21 ran past 30 s).
        daemon = Daemon(Config(max_seconds=5))
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme()
        code = _dictate_code(daemon)
        daemon._on_key("/dev/input/event3", code, 1)
        daemon.recorder.elapsed = 6.0
        daemon._on_tick()
        self.assertIsNone(daemon.session)
        self.assertFalse(daemon.recorder.active)
        self.assertEqual(daemon.jobs.get_nowait().action, "dictate")
        self.assertEqual(daemon.ime.preedits, [], "the preview stays until the text replaces it")
        self.assertIn("stuck key", notify.call_args.args[1])
        daemon._on_key("/dev/input/event3", code, 0)  # the eventual release is nothing now
        self.assertIsNone(daemon.session)
        self.assertEqual(daemon.recorder.stops, 1)


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
                  "ghostty", "", inserts_mark=spacing.inserts_in(7))
        self.assertEqual(spacing.settle(job), "",
                         "the first landed nowhere: the field's own report (empty) holds")
        spacing.inserted(7, "First.", spacing.mark())
        self.assertEqual(spacing.settle(job), " ", "ours landed after key-down: continuation")
        spacing.user_typed()
        self.assertEqual(spacing.settle(job), "", "and the user typed since: theirs to decide")
        job = Job(np.zeros(1), "dictate", 1.0, 7, NotifyPreview("dictate"), "live",
                  "ghostty", ".", inserts_mark=spacing.inserts_in(7))
        self.assertEqual(spacing.settle(job), " ", "nothing landed since: the field's '.' wins")

    def test_a_first_dictation_that_lands_nowhere_keeps_the_field_report(self):
        # Typed "Hello.", two quick dictations, the first with no speech: the
        # second used to lose the "." it saw at key-down and join the text.
        spacing = Spacing()
        spacing.queued(7)
        job = Job(np.zeros(1), "dictate", 1.0, 7, NotifyPreview("dictate"), "live",
                  "ghostty", ".", inserts_mark=spacing.inserts_in(7))
        spacing.landed(7)  # no speech detected
        self.assertEqual(spacing.settle(job), " ")

    def test_punctuation_joins_without_a_space(self):
        self.assertEqual(target_mod.spaced(" ", ", however"), ", however")
        self.assertEqual(target_mod.spaced(" ", "next"), " next")
        self.assertEqual(target_mod.spaced("", "next"), "next")


class SpacingIntegrationTests(unittest.TestCase):
    def setUp(self):
        _no_real_recovery_file(self)

    def _daemon(self, before=None, window=7, app="ghostty", landing=None, queued=None):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.ime = FakeIme(generation=1)
        daemon.ime.before_cursor = lambda: before
        self._probe = patch("voicekey.target.emacs_mod.pin", return_value=Pin("pin-1", before))
        self._probe.start()
        self.addCleanup(self._probe.stop)
        if landing is not None:
            daemon.spacing.queued(landing)
        if queued:
            daemon.jobs.put_nowait(_job(queued, window_id=9))
        with patch("voicekey.daemon.notify"), _focused(window, app):
            daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        return daemon

    def test_preview_and_commit_carry_the_predicted_space(self):
        daemon = self._daemon(before=".")
        daemon.session.target.show("Second thought")
        self.assertEqual(daemon.ime.preedits, [(" Second thought", 1)])
        daemon._finish()
        job = daemon.jobs.get_nowait()
        with patch("voicekey.daemon.notify"), patch("voicekey.target.focus.window_id", return_value=7), \
                patch("voicekey.daemon.time.monotonic", return_value=job.finished_at + 1.0):
            daemon._deliver_dictation(job, "Second thought.")
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
        job = _job("dictate", _ime_target(ime), before=".")
        with patch("voicekey.daemon.notify"), patch("voicekey.target.focus.window_id", return_value=7), \
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
    @_focused(app="emacs")
    def test_delivery_waits_for_the_pin_even_when_our_text_was_landing(self, _focus, _notify):
        # The insert is a second emacsclient; sent before the pin's has
        # reached Emacs it would find "no pinned buffer".
        release = threading.Event()

        def slow_pin(pin_id):
            release.wait(5)
            return Pin(pin_id, ".")

        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon.spacing.queued(7)  # a dictation of ours is still landing
        with patch("voicekey.target.emacs_mod.pin", side_effect=slow_pin), \
                patch.object(daemon_mod, "PIN_WAIT", 0.01):
            daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
            self.assertTrue(daemon.session.after_landing)
            daemon._finish()
            job = daemon.jobs.get_nowait()
            inserted = threading.Event()
            with patch("voicekey.target.emacs_mod.insert",
                       side_effect=lambda *a, **k: inserted.set()) as insert, \
                    patch("voicekey.daemon.time.monotonic", return_value=job.finished_at + 1.0):
                threading.Thread(target=daemon._deliver_dictation, args=(job, "Hello."),
                                 daemon=True).start()
                self.assertFalse(inserted.wait(0.3), "not before Emacs has registered the pin")
                release.set()
                self.assertTrue(inserted.wait(2))
            insert.assert_called_once()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.emacs_mod.insert")
    def test_a_transcript_gone_stale_waiting_for_emacs_is_copied_not_inserted_late(
            self, insert, copy, _notify):
        daemon = Daemon(Config())
        daemon.cfg.dictation.max_delay_seconds = 0.5
        pinning = Mock()
        pinning.id = "pin-1"
        pinning.before.side_effect = lambda wait: time.sleep(wait)  # Emacs never answers
        target = EmacsTarget(NotifyPreview("dictate"), Window(7, True), "emacs", pinning)
        job = Job(np.zeros(1), "dictate", time.monotonic(), 7, target, "live", "emacs")
        daemon._deliver_dictation(job, "Hello.")
        insert.assert_not_called()
        copy.assert_called_once_with("Hello.")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.target.emacs_mod.insert")
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_emacs_gets_the_text_through_emacsclient_not_the_ime(self, _clock, insert, _notify):
        daemon = Daemon(Config())
        daemon.spacing.inserted(7, "First.", daemon.spacing.mark())
        ime = FakeIme()
        job = _job("dictate", _emacs_target(ImePreview(ime, 1), before="."), before=".")
        daemon._deliver_dictation(job, "Second.")
        insert.assert_called_once_with(" Second.", "pin-1", timeout=9.0)
        self.assertEqual(ime.commits, [])
        self.assertEqual(ime.preedits, [("", 1)], "the preedit is cleared first")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.emacs_mod.insert")
    @patch("voicekey.target.focus.window_id", return_value=8)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_emacs_delivery_follows_the_pinned_buffer_not_focus(self, _clock, focus, insert, copy, notify):
        # 2026-08-30: an agent's timer popped a dialog mid-dictation, focus
        # moved, and the text was copied. The buffer was pinned at key-down,
        # so focus is no reason to refuse.
        daemon = Daemon(Config())
        job = _job("dictate", _emacs_target(ImePreview(FakeIme(), 1)))
        daemon._deliver_dictation(job, "Hello.")
        insert.assert_called_once_with("Hello.", "pin-1", timeout=9.0)
        copy.assert_not_called()
        focus.assert_not_called()
        self.assertEqual(notify.call_args.args[0], "✓ Typed")

    @patch("voicekey.daemon.notify")
    @patch("voicekey.target.emacs_mod.insert")
    @patch("voicekey.daemon.time.monotonic", return_value=7.5)
    def test_emacs_has_the_rest_of_the_delay_budget_to_answer(self, clock, insert, _notify):
        daemon = Daemon(Config())
        job = _job("dictate", _emacs_target())
        daemon._deliver_dictation(job, "Hello.")
        self.assertEqual(insert.call_args.kwargs["timeout"], 3.5)
        clock.return_value = 10.9  # nearly stale: still a second for a healthy Emacs
        daemon._deliver_dictation(job, "Hello.")
        self.assertEqual(insert.call_args.kwargs["timeout"], 1.0)

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.emacs_mod.insert", side_effect=EmacsError("buffer is read-only"))
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_emacs_refusal_copies_with_the_reason(self, _clock, _insert, copy, notify):
        daemon = Daemon(Config())
        job = _job("dictate", _emacs_target(ImePreview(FakeIme(), 1)))
        with patch("voicekey.daemon.recovery.save", return_value="/secure/path"):
            daemon._deliver_dictation(job, "Hello.")
        copy.assert_called_once_with("Hello.")
        self.assertIn("read-only", notify.call_args.args[1])
        self.assertIn("/secure/path", notify.call_args.args[1])

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.emacs_mod.insert", side_effect=EmacsTimeout("Emacs did not answer within 9s"))
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_a_blocked_emacs_saves_the_text_and_neither_types_nor_copies(
            self, _clock, _insert, type_text, copy, notify):
        # The form is queued in Emacs and runs once the dialog is dismissed,
        # so a copy could be pasted on top of it.
        daemon = Daemon(Config())
        job = _job("dictate", _emacs_target(ImePreview(FakeIme(), 1)))
        with patch("voicekey.daemon.recovery.save", return_value="/secure/path") as save:
            daemon._deliver_dictation(job, "Hello.")
        save.assert_called_once_with("Hello.")
        copy.assert_not_called()
        type_text.assert_not_called()
        self.assertIn("may still appear", notify.call_args.args[1])

    @patch("voicekey.daemon.notify")
    @patch("voicekey.daemon.inject_mod.copy")
    @patch("voicekey.target.inject_mod.type_text")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @patch("voicekey.daemon.time.monotonic", return_value=2.0)
    def test_hung_input_method_saves_the_text_and_neither_types_nor_copies(
            self, _clock, _focus, type_text, copy, _notify):
        daemon = Daemon(Config())
        ime = FakeIme()
        ime.commit = Mock(side_effect=ImeHung("stopped responding"))
        with patch("voicekey.daemon.recovery.save", return_value="/secure/path") as save:
            daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "Hello.")
        save.assert_called_once_with("Hello.")
        copy.assert_not_called()
        type_text.assert_not_called()

    @patch("voicekey.daemon.notify")
    @patch("voicekey.target.focus.window_id", return_value=7)
    @_ticking()
    def test_a_copy_leaves_the_spacing_state_alone(self, _clock, _focus, _notify):
        daemon = Daemon(Config())
        daemon.spacing.inserted(7, "Hello.", daemon.spacing.mark())
        ime = FakeIme()
        ime.commit_result = False
        with patch("voicekey.daemon.inject_mod.copy"):
            daemon._deliver_dictation(_job("dictate", _ime_target(ime)), "Hello.")
        self.assertEqual(daemon.spacing.owed(None, "ghostty", 7), " ")


class AgentGateTests(unittest.TestCase):
    def _daemon(self, **config):
        daemon = Daemon(Config(**config))
        daemon.recorder = FakeRecorder()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        daemon.gate = Gate(os.path.join(directory.name, "lock"))
        daemon.gate.open()
        self.addCleanup(daemon.gate.close)
        return daemon

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_lock_is_held_from_key_down_until_the_text_has_landed(self, _focus, _notify):
        daemon = self._daemon()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertTrue(daemon.gate.held)
        daemon._finish()
        self.assertTrue(daemon.gate.held, "the transcript is still on its way")
        daemon.backend = Mock()
        daemon.backend.transcribe.return_value = ""
        threading.Thread(target=daemon._transcription_worker, daemon=True).start()
        daemon.jobs.join()
        deadline = time.monotonic() + 2
        while daemon.gate.held and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(daemon.gate.held)

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_lock_is_held_while_the_text_is_being_delivered(self, _focus, _notify):
        daemon = self._daemon()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        daemon._finish()
        daemon.backend = Mock()
        daemon.backend.transcribe.return_value = "hello"
        threading.Thread(target=daemon._transcription_worker, daemon=True).start()
        daemon.jobs.join()
        self.assertTrue(daemon.gate.held, "transcribed, not yet landed")
        with patch.object(daemon, "_deliver_dictation"):
            threading.Thread(target=daemon._delivery_worker, daemon=True).start()
            daemon.deliveries.join()
        deadline = time.monotonic() + 2
        while daemon.gate.held and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(daemon.gate.held)

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_lock_is_released_when_nothing_will_land(self, _focus, _notify):
        daemon = self._daemon()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        daemon._abort("keyboard gone")
        self.assertFalse(daemon.gate.held)
        daemon = self._daemon(min_seconds=2.0)  # FakeRecorder records 1 s: a tap
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertTrue(daemon.gate.held)
        daemon._finish()
        self.assertFalse(daemon.gate.held)

    @patch("voicekey.daemon.notify")
    @_focused()
    def test_the_gate_is_not_used_until_the_daemon_runs(self, _focus, _notify):
        daemon = Daemon(Config())
        daemon.recorder = FakeRecorder()
        daemon._start("/dev/input/event3", frozenset(), "hold", "dictate", "release")
        self.assertFalse(daemon.gate.held)


class BindingsTests(unittest.TestCase):
    def test_bindings_describe_configured_keys(self):
        daemon = Daemon(Config(dictate_toggle_key="KEY_CONFIG"))
        self.assertEqual(daemon.bindings(), [
            "KEY_F9=dictate(hold)", "KEY_F10=agent(hold)", "KEY_CONFIG=dictate(toggle)",
        ])


if __name__ == "__main__":
    unittest.main()

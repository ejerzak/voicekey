from __future__ import annotations

import subprocess
import threading
import unittest
from unittest.mock import patch

from voicekey import emacs


def _run(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class PinTests(unittest.TestCase):
    @patch("voicekey.emacs.subprocess.run", return_value=_run('"."\n'))
    def test_pin_registers_a_fresh_id_and_parses_the_character_before_point(self, run):
        first, second = emacs.pin(), emacs.pin()
        self.assertEqual(first.before, ".")
        self.assertNotEqual(first.id, second.id)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["emacsclient", "-e"])
        self.assertIn(f'"{second.id}"', argv[2])
        self.assertIn("voicekey--pins", argv[2])
        self.assertIn("(window-buffer (selected-window))", argv[2])

    @patch("voicekey.emacs.subprocess.run", return_value=_run('"\\n"\n'))
    def test_pin_newline(self, run):
        self.assertEqual(emacs.pin().before, "\n")

    @patch("voicekey.emacs.subprocess.run", return_value=_run('"."\n'))
    def test_pin_uses_the_id_it_is_given(self, run):
        self.assertEqual(emacs.pin("given").id, "given")
        self.assertIn('"given"', run.call_args.args[0][2])

    @patch("voicekey.emacs.subprocess.run", side_effect=OSError("no emacsclient"))
    def test_pin_without_emacs_still_names_the_buffer_it_would_have_pinned(self, run):
        pinned = emacs.pin()
        self.assertIsNone(pinned.before)
        self.assertTrue(pinned.id)

    @patch("voicekey.emacs.subprocess.run",
           side_effect=subprocess.TimeoutExpired(["emacsclient"], 5))
    def test_pin_survives_a_blocked_emacs(self, run):
        # The form was delivered; Emacs registers the pin once it is free,
        # so the id is kept for delivery.
        self.assertIsNone(emacs.pin().before)


class PendingPinTests(unittest.TestCase):
    def test_the_id_is_known_before_emacs_answers(self):
        release = threading.Event()

        def slow(argv, **kwargs):
            release.wait(5)
            return _run('"."\n')

        with patch("voicekey.emacs.subprocess.run", side_effect=slow) as run:
            pending = emacs.PendingPin()
            self.assertTrue(pending.id)
            self.assertIsNone(pending.before(0.05), "Emacs is busy; the preview goes without")
            release.set()
            self.assertEqual(pending.before(2.0), ".")
        self.assertIn(f'"{pending.id}"', run.call_args.args[0][2])

    @patch("voicekey.emacs.subprocess.run", side_effect=OSError("no emacsclient"))
    def test_without_emacs_the_id_still_names_the_buffer(self, run):
        pending = emacs.PendingPin()
        self.assertIsNone(pending.before(2.0))
        self.assertTrue(pending.id)


class InsertTests(unittest.TestCase):
    def test_normal_and_visual_state_end_in_normal_state_like_escape(self):
        # Dictating in normal state is usually an accident; the buffer is
        # left in the state it was found in, not in insert state.
        self.assertIn("(evil-append 1) (insert text) (evil-normal-state)", emacs.INSERT)
        visual = emacs.INSERT[emacs.INSERT.index("(eq state 'visual)"):emacs.INSERT.index("(eq state 'normal)")]
        self.assertIn("(evil-normal-state)", visual)
        self.assertIn("(t (insert text))", emacs.INSERT, "insert state stays insert")

    @patch("voicekey.emacs.subprocess.run", return_value=_run('"ok"\n'))
    def test_insert_goes_to_the_pinned_buffer_and_escapes_the_text_for_lisp(self, run):
        emacs.insert('say "hi" \\ bye', "abc123")
        form = run.call_args.args[0][2]
        self.assertIn('"say \\"hi\\" \\\\ bye"', form)
        self.assertIn('(assoc "abc123"', form)
        self.assertNotIn("(window-buffer (selected-window))", form, "delivery does not read focus")
        self.assertEqual(run.call_args.kwargs["timeout"], emacs.TIMEOUT)

    @patch("voicekey.emacs.subprocess.run", return_value=_run('"ok"\n'))
    def test_insert_waits_as_long_as_it_is_told(self, run):
        emacs.insert("x", "abc123", timeout=8.5)
        self.assertEqual(run.call_args.kwargs["timeout"], 8.5)

    @patch("voicekey.emacs.subprocess.run",
           return_value=_run("*ERROR*: buffer is read-only", returncode=1))
    def test_refusals_carry_the_reason(self, run):
        with self.assertRaisesRegex(emacs.EmacsError, "read-only"):
            emacs.insert("x", "abc123")

    @patch("voicekey.emacs.subprocess.run",
           side_effect=subprocess.TimeoutExpired(["emacsclient"], 5))
    def test_a_blocked_emacs_is_a_timeout_not_a_refusal(self, run):
        with self.assertRaises(emacs.EmacsTimeout) as caught:
            emacs.insert("x", "abc123")
        self.assertIsInstance(caught.exception, emacs.EmacsError)
        self.assertIn("did not answer", str(caught.exception))

    def test_the_forms_refuse_what_the_daemon_must_copy(self):
        for reason in ("no pinned buffer", "the buffer is gone", "buffer is read-only",
                       "an operator is pending", "blockwise selection"):
            self.assertIn(reason, emacs.INSERT)


if __name__ == "__main__":
    unittest.main()

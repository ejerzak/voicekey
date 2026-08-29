from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from voicekey import emacs


def _run(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class EmacsTests(unittest.TestCase):
    @patch("voicekey.emacs.subprocess.run", return_value=_run('"."\n'))
    def test_before_cursor_parses_the_lisp_string(self, run):
        self.assertEqual(emacs.before_cursor(), ".")
        self.assertEqual(run.call_args.args[0][:2], ["emacsclient", "-e"])

    @patch("voicekey.emacs.subprocess.run", return_value=_run('"\\n"\n'))
    def test_before_cursor_newline(self, run):
        self.assertEqual(emacs.before_cursor(), "\n")

    @patch("voicekey.emacs.subprocess.run", side_effect=OSError("no emacsclient"))
    def test_before_cursor_is_unknown_without_emacs(self, run):
        self.assertIsNone(emacs.before_cursor())

    @patch("voicekey.emacs.subprocess.run", return_value=_run('"ok"\n'))
    def test_insert_escapes_the_text_for_lisp(self, run):
        emacs.insert('say "hi" \\ bye')
        form = run.call_args.args[0][2]
        self.assertIn('"say \\"hi\\" \\\\ bye"', form)

    @patch("voicekey.emacs.subprocess.run",
           return_value=_run("*ERROR*: buffer is read-only", returncode=1))
    def test_refusals_carry_the_reason(self, run):
        with self.assertRaisesRegex(emacs.EmacsError, "read-only"):
            emacs.insert("x")


if __name__ == "__main__":
    unittest.main()

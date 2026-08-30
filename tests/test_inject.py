from __future__ import annotations

import time
import unittest

from voicekey import inject


class InjectTests(unittest.TestCase):
    def test_a_forking_clipboard_server_does_not_hold_the_call(self):
        # wl-copy exits at once but leaves a child, holding stderr, to serve
        # the clipboard; the call must return with the parent.
        started = time.monotonic()
        inject._run(["bash", "-c", "cat >/dev/null; sleep 2 & exit 0"], "text")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_failures_carry_the_exit_code_and_stderr(self):
        with self.assertRaisesRegex(inject.InjectError, r"rc=3.*boom"):
            inject._run(["bash", "-c", "echo boom >&2; exit 3"], "text")

    def test_the_text_reaches_stdin(self):
        with self.assertRaisesRegex(inject.InjectError, "hello there"):
            inject._run(["bash", "-c", "cat >&2; exit 1"], "hello there")


if __name__ == "__main__":
    unittest.main()

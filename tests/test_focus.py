from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from voicekey.focus import window_id


class FocusTests(unittest.TestCase):
    @patch("voicekey.focus.subprocess.run")
    def test_reads_niri_window_id(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"id": 42, "title": "private"}', stderr=""
        )
        self.assertEqual(window_id(), 42)

    @patch("voicekey.focus.subprocess.run")
    def test_failed_query_is_unverifiable(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no socket"
        )
        self.assertIsNone(window_id())


if __name__ == "__main__":
    unittest.main()

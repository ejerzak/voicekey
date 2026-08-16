from __future__ import annotations

import functools
import os
import tempfile
import unittest
from unittest.mock import patch

from voicekey.recorder import Recorder


class RecorderTests(unittest.TestCase):
    def test_spawn_failure_removes_temporary_wav(self):
        real_mkstemp = tempfile.mkstemp
        with tempfile.TemporaryDirectory() as directory:
            local_mkstemp = functools.partial(real_mkstemp, dir=directory)
            with patch("voicekey.recorder.tempfile.mkstemp", side_effect=local_mkstemp):
                with patch(
                    "voicekey.recorder.subprocess.Popen",
                    side_effect=FileNotFoundError("pw-record"),
                ):
                    recorder = Recorder()
                    with self.assertRaises(FileNotFoundError):
                        recorder.start()
            self.assertEqual(os.listdir(directory), [])
            self.assertIsNone(recorder.path)


if __name__ == "__main__":
    unittest.main()

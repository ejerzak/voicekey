from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from voicekey import recovery


class RecoveryTests(unittest.TestCase):
    def test_recovery_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = os.path.join(directory, "voicekey")
            path = os.path.join(state_dir, "last-recovery.txt")
            with patch.object(recovery, "STATE_DIR", state_dir):
                with patch.object(recovery, "LAST_RECOVERY", path):
                    self.assertEqual(recovery.save("secret"), path)
            self.assertEqual(stat.S_IMODE(os.stat(state_dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "secret\n")


if __name__ == "__main__":
    unittest.main()

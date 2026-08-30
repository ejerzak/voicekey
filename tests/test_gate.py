from __future__ import annotations

import fcntl
import os
import tempfile
import unittest

from voicekey.gate import Gate


def _lockable(path: str) -> bool:
    """Whether another process could take the lock right now."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


class GateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "voicekey", "lock")

    def test_held_exactly_while_busy(self):
        gate = Gate(self.path)
        gate.open()
        self.addCleanup(gate.close)
        self.assertTrue(_lockable(self.path))
        gate.settle(lambda: True)
        self.assertTrue(gate.held)
        self.assertFalse(_lockable(self.path), "agents must wait")
        gate.settle(lambda: True)  # idempotent
        gate.settle(lambda: False)
        self.assertFalse(gate.held)
        self.assertTrue(_lockable(self.path))

    def test_does_nothing_before_open(self):
        gate = Gate(self.path)
        gate.settle(lambda: True)
        self.assertFalse(gate.held)
        self.assertFalse(os.path.exists(self.path))

    def test_a_lock_held_elsewhere_is_never_waited_for(self):
        gate = Gate(self.path)
        gate.open()
        self.addCleanup(gate.close)
        other = os.open(self.path, os.O_RDWR)
        fcntl.flock(other, fcntl.LOCK_EX)
        gate.settle(lambda: True)  # returns at once
        self.assertFalse(gate.held)
        fcntl.flock(other, fcntl.LOCK_UN)
        os.close(other)
        gate.settle(lambda: True)  # tried again at the next transition
        self.assertTrue(gate.held)

    def test_the_file_is_private_and_the_lock_dies_with_the_descriptor(self):
        gate = Gate(self.path)
        gate.open()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        gate.settle(lambda: True)
        gate.close()
        self.assertTrue(_lockable(self.path))
        self.assertFalse(gate.held)


if __name__ == "__main__":
    unittest.main()

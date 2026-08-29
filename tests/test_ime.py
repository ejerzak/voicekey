from __future__ import annotations

import unittest

from voicekey.ime import InputMethod


class FakeProxy:
    def __init__(self):
        self.calls = []

    def set_preedit_string(self, text, begin, end):
        self.calls.append(("preedit", text, begin, end))

    def commit_string(self, text):
        self.calls.append(("commit_string", text))

    def commit(self, serial):
        self.calls.append(("commit", serial))


class FakeDisplay:
    def flush(self):
        pass


def _offline_input_method() -> InputMethod:
    """State machine and request logic only — no Wayland connection, no thread."""
    ime = InputMethod.__new__(InputMethod)
    ime._reset()
    ime._im = FakeProxy()
    ime._display = FakeDisplay()
    return ime


class ActivationTests(unittest.TestCase):
    def test_activation_is_applied_on_done_and_numbered(self):
        ime = _offline_input_method()
        self.assertIsNone(ime.activation())
        ime._on_activate(None)
        self.assertIsNone(ime.activation(), "not applied before done")
        ime._on_done(None)
        self.assertEqual(ime.activation(), 1)
        ime._on_deactivate(None)
        ime._on_done(None)
        self.assertIsNone(ime.activation())
        ime._on_activate(None)
        ime._on_done(None)
        self.assertEqual(ime.activation(), 2)

    def test_requests_carry_the_serial_of_done_events(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        ime._on_done(None)  # e.g. a surrounding-text update
        self.assertTrue(ime._apply(1, preedit="héllo"))
        self.assertEqual(ime._im.calls, [("preedit", "héllo", 6, 6), ("commit", 2)])

    def test_stale_generation_is_refused(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        ime._on_deactivate(None)
        ime._on_done(None)
        ime._on_activate(None)
        ime._on_done(None)
        self.assertFalse(ime._apply(1, commit="late text"))
        self.assertFalse(ime._apply(2, preedit="x") is False)
        self.assertEqual(ime._im.calls, [("preedit", "x", 1, 1), ("commit", 3)])

    def test_commit_replaces_the_preedit(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        self.assertTrue(ime._apply(1, commit="final text"))
        self.assertEqual(ime._im.calls, [
            ("preedit", "", 0, 0), ("commit_string", "final text"), ("commit", 1),
        ])


if __name__ == "__main__":
    unittest.main()

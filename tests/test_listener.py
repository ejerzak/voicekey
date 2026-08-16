from __future__ import annotations

import unittest
from unittest.mock import Mock

from evdev import ecodes

from voicekey.listener import _supports_any_key


class InputDeviceSelectionTests(unittest.TestCase):
    def test_full_keyboard_with_configured_function_key_is_selected(self):
        device = Mock()
        device.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.KEY_A, ecodes.KEY_F9]
        }
        self.assertTrue(_supports_any_key(device, {ecodes.KEY_F9}))

    def test_separate_hotkey_device_is_selected(self):
        device = Mock()
        device.capabilities.return_value = {
            ecodes.EV_KEY: [
                ecodes.KEY_CONFIG,
                ecodes.KEY_BLUETOOTH,
            ]
        }
        self.assertTrue(
            _supports_any_key(
                device,
                {ecodes.KEY_CONFIG, ecodes.KEY_BLUETOOTH},
            )
        )

    def test_unrelated_button_device_is_ignored(self):
        device = Mock()
        device.capabilities.return_value = {ecodes.EV_KEY: [ecodes.KEY_POWER]}
        self.assertFalse(_supports_any_key(device, {ecodes.KEY_F9}))


if __name__ == "__main__":
    unittest.main()

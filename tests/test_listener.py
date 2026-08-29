from __future__ import annotations

import unittest
from unittest.mock import Mock

from evdev import ecodes

from voicekey.listener import KeyboardListener, _is_keyboard, _supports_any_key


def _event(code, value, type_=ecodes.EV_KEY):
    return Mock(type=type_, code=code, value=value)


class DispatchTests(unittest.TestCase):
    def test_voice_keys_go_to_on_key_and_everything_else_is_only_activity(self):
        on_key, on_activity = Mock(), Mock()
        listener = KeyboardListener({ecodes.KEY_F9}, on_key, Mock(), Mock(), Mock(),
                                    on_activity=on_activity)
        listener.dispatch("/dev/input/event3", [
            _event(ecodes.KEY_F9, 1), _event(ecodes.KEY_F9, 2), _event(ecodes.KEY_F9, 0),
            _event(ecodes.KEY_A, 1), _event(ecodes.KEY_A, 0),
            _event(ecodes.BTN_LEFT, 1),
            _event(ecodes.REL_X, 5, type_=ecodes.EV_REL),
        ])
        self.assertEqual([c.args for c in on_key.call_args_list],
                         [("/dev/input/event3", ecodes.KEY_F9, 1),
                          ("/dev/input/event3", ecodes.KEY_F9, 0)])
        self.assertEqual(on_activity.call_count, 2, "a key and a click; no repeats, no releases")


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

    def test_any_keyboard_counts_for_activity_but_a_mouse_does_not(self):
        keyboard = Mock()
        keyboard.capabilities.return_value = {ecodes.EV_KEY: [ecodes.KEY_A, ecodes.KEY_ENTER]}
        mouse = Mock()
        mouse.capabilities.return_value = {ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT]}
        self.assertTrue(_is_keyboard(keyboard))
        self.assertFalse(_is_keyboard(mouse))

    def test_unrelated_button_device_is_ignored(self):
        device = Mock()
        device.capabilities.return_value = {ecodes.EV_KEY: [ecodes.KEY_POWER]}
        self.assertFalse(_supports_any_key(device, {ecodes.KEY_F9}))


if __name__ == "__main__":
    unittest.main()

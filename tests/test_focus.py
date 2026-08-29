from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from voicekey.focus import Focus, focused, window_id


def _run(stdout, returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class FocusTests(unittest.TestCase):
    @patch.dict("voicekey.focus.os.environ", {"NIRI_SOCKET": "/run/niri.sock"}, clear=True)
    @patch("voicekey.focus.subprocess.run")
    def test_niri(self, run):
        run.return_value = _run('{"id": 42, "app_id": "emacs", "title": "private"}')
        self.assertEqual(focused(), Focus(42, "emacs"))
        self.assertEqual(run.call_args.args[0][:2], ["niri", "msg"])

    @patch.dict("voicekey.focus.os.environ", {"SWAYSOCK": "/run/sway.sock"}, clear=True)
    @patch("voicekey.focus.subprocess.run")
    def test_sway_finds_the_focused_container_in_the_tree(self, run):
        run.return_value = _run(
            '{"type": "root", "focused": false, "nodes": [{"type": "output", "nodes": ['
            '{"type": "workspace", "focused": false, "nodes": [], "floating_nodes": ['
            '{"type": "floating_con", "id": 7, "focused": true, "app_id": "foot"}]}]}]}'
        )
        self.assertEqual(focused(), Focus(7, "foot"))
        self.assertEqual(run.call_args.args[0][:2], ["swaymsg", "-t"])

    @patch.dict("voicekey.focus.os.environ", {"HYPRLAND_INSTANCE_SIGNATURE": "abc"}, clear=True)
    @patch("voicekey.focus.subprocess.run")
    def test_hyprland_uses_the_window_address(self, run):
        run.return_value = _run('{"address": "0x55d1", "class": "firefox"}')
        self.assertEqual(focused(), Focus("0x55d1", "firefox"))
        self.assertEqual(run.call_args.args[0][:2], ["hyprctl", "-j"])

    @patch.dict("voicekey.focus.os.environ", {"XDG_CURRENT_DESKTOP": "river"}, clear=True)
    @patch("voicekey.focus.subprocess.run")
    def test_unknown_compositor_is_unverifiable_without_running_anything(self, run):
        self.assertEqual(focused(), Focus())
        run.assert_not_called()

    @patch.dict("voicekey.focus.os.environ", {"NIRI_SOCKET": "/run/niri.sock"}, clear=True)
    @patch("voicekey.focus.subprocess.run")
    def test_failed_query_is_unverifiable(self, run):
        run.return_value = _run("", returncode=1)
        self.assertIsNone(window_id())


if __name__ == "__main__":
    unittest.main()

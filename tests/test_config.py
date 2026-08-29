from __future__ import annotations

import os
import tempfile
import unittest

from voicekey.config import ConfigError, load


class ConfigTests(unittest.TestCase):
    def _load_text(self, text: str):
        fd, path = tempfile.mkstemp(suffix=".toml")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
            return load(path)
        finally:
            os.unlink(path)

    def test_example_config_loads(self):
        path = os.path.join(os.path.dirname(__file__), "..", "config.example.toml")
        cfg = load(path)
        self.assertEqual(cfg.dictation.inject, "wtype")
        self.assertEqual(cfg.agent.target, "hermes")
        self.assertEqual(cfg.agent.transport, "local")
        self.assertEqual(cfg.agent.tmux_session, "voicekey-hermes")
        self.assertTrue(os.path.isabs(cfg.agent.working_directory))

    def test_defaults_enable_live_preview_and_in_field_text(self):
        cfg = self._load_text("")
        self.assertEqual(cfg.backend.type, "parakeet")
        self.assertTrue(cfg.backend.model_dir.endswith(
            "/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming"
        ))
        self.assertTrue(cfg.streaming.model_dir.endswith("-560ms-int8-2026-04-25"))
        self.assertTrue(os.path.isabs(cfg.streaming.model_dir))
        self.assertTrue(cfg.dictation.ime)
        self.assertEqual(cfg.recordings_dir, "")

    def test_preview_can_be_disabled_and_recordings_kept(self):
        cfg = self._load_text(
            'recordings_dir = "~/voicekey-samples"\n'
            '[streaming]\nmodel_dir = ""\n[dictation]\nime = false\n'
        )
        self.assertEqual(cfg.streaming.model_dir, "")
        self.assertFalse(cfg.dictation.ime)
        self.assertEqual(cfg.recordings_dir, os.path.expanduser("~/voicekey-samples"))

    def test_removed_remote_backend_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "backend.type must be one of"):
            self._load_text('[backend]\ntype = "remote"\n')

    def test_remote_transport_requires_host_and_user(self):
        with self.assertRaisesRegex(ConfigError, "agent.remote_host"):
            self._load_text('[agent]\ntransport = "ssh-over-tailscale"\n')

    def test_remote_fields_are_rejected_for_local_transport(self):
        with self.assertRaisesRegex(ConfigError, "require agent.transport"):
            self._load_text('[agent]\nremote_host = "desktop.example"\n')

    def test_numeric_string_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "max_seconds must be a number"):
            self._load_text('max_seconds = "90"\n')

    def test_section_must_be_table(self):
        with self.assertRaisesRegex(ConfigError, r"\[backend\] must be a TOML table"):
            self._load_text('backend = "whisper"\n')

    def test_unsafe_legacy_agent_command_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown config key agent.cmd"):
            self._load_text('[agent]\ncmd = ["hermes", "-z", "{text}"]\n')

    def test_old_emacs_agent_config_is_rejected(self):
        with self.assertRaisesRegex(
            ConfigError, "unknown config key agent.default_provider"
        ):
            self._load_text('[agent]\ndefault_provider = "hermes"\n')

    def test_tmux_names_are_constrained(self):
        with self.assertRaisesRegex(ConfigError, "agent.tmux_session"):
            self._load_text('[agent]\ntmux_session = "bad/session"\n')

    def test_recording_bounds_are_ordered(self):
        with self.assertRaisesRegex(ConfigError, "min_seconds must be less"):
            self._load_text("min_seconds = 2\nmax_seconds = 1\n")

    def test_voice_keys_must_be_unique(self):
        with self.assertRaisesRegex(
            ConfigError, "configured voice key chords must differ"
        ):
            self._load_text('dictate_toggle_key = "KEY_F9"\n')

    def test_voice_chord_order_does_not_make_duplicate_binding_unique(self):
        with self.assertRaisesRegex(
            ConfigError, "configured voice key chords must differ"
        ):
            self._load_text(
                'dictate_key = "KEY_F23+KEY_RIGHTALT"\n'
                'agent_key = "KEY_RIGHTALT+KEY_F23"\n'
            )


if __name__ == "__main__":
    unittest.main()

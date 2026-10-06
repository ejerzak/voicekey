from __future__ import annotations

import os
import tempfile
import unittest

from voicekey.config import ConfigError, load


class ConfigTests(unittest.TestCase):
    def test_destination_defaults_and_legacy_migration(self):
        cfg = self._load_text('')
        self.assertEqual(cfg.persistent.destination_policy, 'pause')
        self.assertEqual(cfg.persistent.silence_seconds, 60)
        for value, expected in (('true', 'follow'), ('false', 'pin')):
            cfg = self._load_text('[persistent]\nfollow_focus = ' + value)
            self.assertEqual(cfg.persistent.destination_policy, expected)
        for policy in ('pause', 'follow', 'pin'):
            cfg = self._load_text(f'[persistent]\ndestination_policy = "{policy}"')
            self.assertEqual(cfg.persistent.destination_policy, policy)
        for text in ('destination_policy = "other"', 'destination_policy = true',
                     'follow_focus = "yes"', 'follow_focus = true\ndestination_policy = "pause"'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self._load_text('[persistent]\n' + text)

    def test_multiline_apps_require_explicit_app_ids(self):
        self.assertEqual(self._load_text('').dictation.multiline_apps, [])
        cfg = self._load_text('[dictation]\nmultiline_apps = ["example.composer"]')
        self.assertEqual(cfg.dictation.multiline_apps, ['example.composer'])
        for value in ('"browser"', '{}', '[1]', '[""]', '[true]'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self._load_text('[dictation]\nmultiline_apps = ' + value)

    def test_text_processing_config(self):
        cfg = self._load_text('[text.word_overrides]\n"hyper whisper" = "hyprwhspr"\n'
                              '[dictation]\npost_transcription_hook = "cat"\n'
                              '[agent]\npost_transcription_hook = "cat"')
        self.assertEqual(cfg.text.word_overrides, {'hyper whisper': 'hyprwhspr'})
        self.assertEqual(cfg.agent.post_transcription_hook, 'cat')
        for value in ('[]', '{ "" = "x" }', '{ name = 3 }', '{ name = "" }', '{ Name = "x", name = "y" }'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self._load_text('[text]\nword_overrides = ' + value)
        with self.assertRaises(ConfigError):
            self._load_text('[dictation]\npost_transcription_hook = 42')

    def test_app_styles_are_validated(self):
        cfg = self._load_text('[polish.app_styles]\n"org.signal.Signal" = "semi-casual"')
        self.assertEqual(cfg.polish.app_styles, {'org.signal.Signal': 'semi-casual'})
        self.assertEqual(cfg.polish.style, 'semi-formal')
        for value in ('"bad"', '[]', '{ "" = "casual" }',
                      '{ signal = 1 }', '{ signal = "academic" }'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self._load_text('[polish]\napp_styles = ' + value)

    def test_persistent_settings_and_incompatible_bindings_are_validated(self):
        cfg = self._load_text('[persistent]\nkey = "KEY_F11"\nsilence_seconds = 90\n')
        self.assertEqual(cfg.persistent.key, 'KEY_F11')
        self.assertEqual(cfg.persistent.silence_seconds, 90)
        self.assertTrue(os.path.isabs(cfg.persistent.vad_model))
        self.assertEqual(cfg.persistent.polish_min_words, 0)
        self.assertTrue(cfg.persistent.drop_filler_only)
        changed = self._load_text('[persistent]\npolish_min_words = 3\ndrop_filler_only = false')
        self.assertEqual(changed.persistent.polish_min_words, 3)
        self.assertFalse(changed.persistent.drop_filler_only)
        for text in ('[persistent]\nkey = "KEY_RIGHTMETA"',
                     '[persistent]\nsilence_seconds = 0',
                     '[persistent]\nsilence_seconds = 1',
                     '[persistent]\nmax_utterance_seconds = 1',
                     '[persistent]\npause_seconds = "slow"',
                     '[persistent]\npolish_min_words = -1',
                     '[persistent]\ndrop_filler_only = "yes"',
                     '[pipeline]\nmax_audio_seconds = "bad"',
                     '[persistent]\nunknown = true'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self._load_text(text)

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

    def test_polish_is_off_by_default_and_validated_when_on(self):
        cfg = self._load_text("")
        self.assertEqual(cfg.polish.backend, "none")
        self.assertEqual(cfg.polish.server.model_file, "")
        cfg = self._load_text(
            '[polish]\nbackend = "openai"\nurl = "http://127.0.0.1:9000/v1/"\n'
            'style = "formal"\n[polish.server]\nmodel_file = "~/x.gguf"\nthreads = 2\n'
        )
        self.assertEqual(cfg.polish.url, "http://127.0.0.1:9000/v1")
        self.assertEqual(cfg.polish.style, "formal")
        self.assertEqual(cfg.polish.server.threads, 2)
        self.assertTrue(os.path.isabs(cfg.polish.server.model_file))
        self.assertTrue(cfg.polish.server.command.endswith("/voicekey/llama.cpp/llama-server"))
        self.assertTrue(os.path.isabs(cfg.polish.server.command), "the default path is expanded")
        cfg = self._load_text('[polish.server]\ncommand = "llama-server"')
        self.assertEqual(cfg.polish.server.command, "llama-server", "a bare name stays for PATH")
        for text, message in (
            ('[polish]\nbackend = "cloud"', "polish.backend"),
            ('[polish]\nurl = "127.0.0.1:9000"', "polish.url"),
            ('[polish]\nformat = "chat"', "polish.format"),
            ('[polish]\nstyle = "academic"', "polish.style"),
            ('[polish]\nmax_wait_seconds = 0', "polish.max_wait_seconds"),
            ('[polish]\nserver = 3', "[server]"),
            ('[polish.server]\nthreads = 1.5', "polish.server.threads"),
            ('[polish.server]\ncontext = 64', "polish.server.context"),
            ('[polish]\nnope = 1', "polish.nope"),
        ):
            with self.assertRaisesRegex(ConfigError, message, msg=text):
                self._load_text(text)
        # Any style goes for our own prompt.
        self.assertEqual(self._load_text('[polish]\nformat = "instruct"\nstyle = "academic"').polish.style,
                         "academic")

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

    def test_remote_working_directory_is_resolved_on_the_remote_host(self):
        remote = ('[agent]\ntransport = "ssh-over-tailscale"\nremote_host = "desktop"\n'
                  'remote_user = "alice"\nidentity_file = "~/.ssh/id_ed25519"\n')
        cfg = self._load_text(remote)
        self.assertEqual(cfg.agent.working_directory, "~/.local/share/voicekey/hermes",
                         "not expanded here: that would be this machine's home")
        cfg = self._load_text(remote + 'working_directory = "/srv/hermes/"\n')
        self.assertEqual(cfg.agent.working_directory, "/srv/hermes")
        with self.assertRaisesRegex(ConfigError, "absolute or start with ~/"):
            self._load_text(remote + 'working_directory = "hermes"\n')
        with self.assertRaisesRegex(ConfigError, "bare home"):
            self._load_text(remote + 'working_directory = "~/"\n')
        with self.assertRaisesRegex(ConfigError, "filesystem root"):
            self._load_text(remote + 'working_directory = "//"\n')  # normpath keeps "//"
        with self.assertRaisesRegex(ConfigError, "whitespace"):
            self._load_text(remote + 'working_directory = "/srv/hermes "\n')
        self.assertTrue(os.path.isabs(self._load_text("").agent.working_directory),
                        "local: expanded as before")

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
        with self.assertRaisesRegex(ConfigError, "tap_seconds must be less"):
            self._load_text("tap_seconds = 2\nmax_seconds = 1\n")

    def test_retired_min_seconds_is_ignored(self):
        with self.assertLogs("voicekey.config", "WARNING"):
            cfg = self._load_text("min_seconds = 0.3\n")
        self.assertFalse(hasattr(cfg, "min_seconds"))

    def test_voice_keys_must_be_unique(self):
        with self.assertRaisesRegex(
            ConfigError, "configured voice key chords must differ"
        ):
            self._load_text('dictate_toggle_key = "KEY_RIGHTMETA"\n')

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

class PipelineConfigTests(unittest.TestCase):
    def test_short_polish_default_and_pipeline_limits_are_validated(self):
        from voicekey.config import Config, ConfigError, _validate
        cfg = Config()
        _validate(cfg)
        self.assertEqual(cfg.polish.min_words, 8)
        cfg.polish.min_words = -1
        with self.assertRaises(ConfigError):
            _validate(cfg)
        cfg.polish.min_words = 0
        cfg.pipeline.max_pending = 0
        with self.assertRaises(ConfigError):
            _validate(cfg)
        cfg.pipeline.max_pending = 8
        cfg.pipeline.max_audio_seconds = cfg.max_seconds - 1
        with self.assertRaises(ConfigError):
            _validate(cfg)


class TapConfigTests(unittest.TestCase):
    def test_draft_is_opt_in_with_configurable_cancel_chord(self):
        from voicekey.config import Config, ConfigError, _validate
        cfg = Config()
        self.assertFalse(cfg.persistent.draft)
        self.assertEqual(cfg.persistent.draft_cancel_key, 'KEY_ESC')
        cfg.persistent.draft = True
        cfg.persistent.draft_cancel_key = 'KEY_LEFTCTRL+KEY_ESC'
        _validate(cfg)
        cfg.persistent.draft_cancel_key = cfg.dictate_key
        with self.assertRaisesRegex(ConfigError, 'chords must differ'):
            _validate(cfg)
        cfg.persistent.draft_cancel_key = 'KEY_ESC'
        cfg.persistent.draft = 'yes'
        with self.assertRaisesRegex(ConfigError, 'persistent.draft'):
            _validate(cfg)

    def test_tap_threshold_and_batch_reservation_are_always_validated(self):
        from voicekey.config import Config, ConfigError, _validate
        for value in (0, -1, True, float('nan'), 'fast'):
            cfg = Config(tap_seconds=value)
            with self.assertRaisesRegex(ConfigError, 'tap_seconds'):
                _validate(cfg)
        cfg = Config()
        cfg.persistent.max_utterance_seconds = cfg.pipeline.max_audio_seconds
        with self.assertRaises(ConfigError):
            _validate(cfg)

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voicekey.agent_profiles import load_selection, save_selection


class AgentSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = patch.dict(os.environ, {'XDG_STATE_HOME': self.tmp.name})
        self.state.start()
        self.addCleanup(self.state.stop)
        self.path = Path(self.tmp.name) / 'voicekey' / 'agent-profile.json'
        self.profiles = {'hermes': object(), 'codex': object()}

    def test_selection_round_trips_in_machine_local_state_and_can_be_replaced(self):
        self.assertEqual(load_selection(self.profiles, 'hermes'), 'hermes')
        save_selection('codex')
        self.assertEqual(json.loads(self.path.read_text()), {'profile': 'codex'})
        self.assertEqual(load_selection(self.profiles, 'hermes'), 'codex')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        save_selection('hermes')
        self.assertEqual(load_selection(self.profiles, 'codex'), 'hermes')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_and_removed_profiles_fall_back_to_configured_default(self):
        self.path.parent.mkdir()
        for contents in ('not json', 'null', '[]', '{}', '{"profile": 4}',
                         '{"profile": "removed"}'):
            with self.subTest(contents=contents):
                self.path.write_text(contents)
                self.assertEqual(load_selection(self.profiles, 'codex'), 'codex')

    def test_failed_replace_keeps_previous_selection_and_removes_temporary_file(self):
        save_selection('hermes')
        with patch('voicekey.agent_profiles.os.replace', side_effect=OSError('read only')):
            with self.assertRaises(OSError):
                save_selection('codex')
        self.assertEqual(load_selection(self.profiles, 'codex'), 'hermes')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

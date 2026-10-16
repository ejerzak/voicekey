import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evdev import ecodes

from voicekey.control import ControlServer, request
from voicekey.config import Config
from voicekey.daemon import Daemon
from voicekey.listener import KeyboardListener
from voicekey.recovery import Journal
from tests.test_pipeline import wait_for


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'runtime'/'control.sock'
        self.server = ControlServer(self.path)
        self.server.start()
        self.addCleanup(self.server.close)

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1)
        client.connect(str(self.path))
        self.addCleanup(client.close)
        stream = client.makefile('rb')
        self.addCleanup(stream.close)
        status = json.loads(stream.readline())
        self.assertEqual(status['type'], 'status')
        self.assertEqual(status['protocol_version'], 1)
        self.assertIn('capture-client-name', status['capabilities'])
        return client, stream

    def test_status_and_private_socket(self):
        self.server.publish({'state': 'idle', 'listening': False})
        result = request('status', self.path)
        self.assertEqual(result['state'], 'idle')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_mutation_only_runs_when_controller_drains_then_acknowledges(self):
        client, stream = self.connect()
        client.sendall(b'{"command":"start","id":7}\n')
        wait_for(lambda: not self.server.commands.empty())
        handler = Mock(return_value=None)
        handler.assert_not_called()
        self.server.drain(handler)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args, ('start',))
        self.assertEqual(handler.call_args.kwargs['args'], {})
        self.assertEqual(handler.call_args.kwargs['request_id'], 7)
        while (reply := json.loads(stream.readline()))['type'] != 'reply':
            pass
        self.assertEqual(reply, {'type': 'reply', 'id': 7, 'error': None})

    def test_expired_request_cannot_start_recording_late(self):
        client, _ = self.connect()
        self.server.commands.put((client, {'command': 'start'}, time.monotonic()-1))
        handler = Mock(return_value=None)
        self.server.drain(handler)
        handler.assert_not_called()

    def test_unknown_and_oversized_input_disconnect_without_mutation(self):
        for data in (b'{"command":"shell"}\n', b'x'*9000,
                     b'{"command":"start","args":[]}\n',
                     b'{"command":"start","id":{}}\n'):
            client, stream = self.connect()
            client.sendall(data)
            # An initial status heartbeat may precede disconnect.
            while stream.readline():
                pass
        self.assertTrue(self.server.commands.empty())
        self.assertTrue(self.server.thread.is_alive())

    def test_second_server_cannot_replace_live_socket(self):
        other = ControlServer(self.path)
        with self.assertRaisesRegex(OSError, 'already in use'):
            other.start()
        self.assertEqual(request('status', self.path)['state'], 'loading')

    def test_reconnect_after_daemon_restart(self):
        self.server.close()
        self.assertFalse(self.path.exists())
        self.server = ControlServer(self.path)
        self.server.start()
        self.addCleanup(self.server.close)
        self.server.publish({'state': 'idle'})
        self.assertEqual(request('status', self.path)['state'], 'idle')

    def test_wrong_peer_uid_is_refused(self):
        with patch('voicekey.control.os.getuid', return_value=os.getuid() + 1):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(self.path))
                self.assertEqual(client.recv(1), b'')
        self.assertTrue(self.server.commands.empty())

    def test_fragmented_request_keeps_arguments_and_returns_handler_data(self):
        client, stream = self.connect()
        client.sendall(b'{"command":"capture-start","id":"capture-a","args":')
        client.sendall(b'{"seconds":5}}\n')
        wait_for(lambda: not self.server.commands.empty())
        handler = Mock(return_value={'capture_id': 'abc'})
        self.server.drain(handler)
        self.assertEqual(handler.call_args.kwargs['args'], {'seconds': 5})
        while (reply := json.loads(stream.readline()))['type'] != 'reply':
            pass
        self.assertEqual(reply, {'type': 'reply', 'id': 'capture-a', 'error': None, 'capture_id': 'abc'})

    def test_agent_tap_is_advertised_accepted_and_replies_with_what_it_did(self):
        client, stream = self.connect()
        self.assertIn('agent-tap', request('status', self.path)['capabilities'])
        client.sendall(b'{"command":"agent-tap","id":1}\n')
        wait_for(lambda: not self.server.commands.empty())
        self.server.drain(Mock(return_value={'agent': 'parked'}))
        while (reply := json.loads(stream.readline()))['type'] != 'reply':
            pass
        self.assertEqual(reply, {'type': 'reply', 'id': 1, 'error': None, 'agent': 'parked'})

    def test_draft_toggle_commands_are_advertised_and_update_socket_status(self):
        daemon = Daemon(Config(), journal=Journal(self.tmp.name + '/sessions'))
        self.addCleanup(daemon.close)
        client, stream = self.connect()
        self.assertIn('draft-toggle', request('status', self.path)['capabilities'])
        for command, enabled in (('draft-on', True), ('draft-off', False)):
            client.sendall((json.dumps({'command': command, 'id': command}) + '\n').encode())
            wait_for(lambda: not self.server.commands.empty())
            self.server.drain(daemon.command)
            while (reply := json.loads(stream.readline()))['type'] != 'reply':
                pass
            self.assertIsNone(reply['error'])
            self.server.publish(daemon.status())
            self.assertEqual(request('status', self.path)['draft_enabled'], enabled)


class DaemonControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.daemon = Daemon(Config(), journal=Journal(self.tmp.name))
        self.addCleanup(self.daemon.close)
        self.daemon.backend = Mock()
        self.daemon.vad = Mock()

    def test_draft_toggle_updates_idle_daemon_without_starting_capture(self):
        d = self.daemon
        original = dict(d.actions)
        d.cfg.persistent.destination_policy = 'follow'
        for command, enabled in (('draft-on', True), ('draft-on', True),
                                 ('draft-off', False), ('draft-off', False)):
            d.command(command)
            self.assertEqual(d.status()['draft_enabled'], enabled)
            self.assertEqual(d.cfg.persistent.destination_policy, 'follow')
            self.assertFalse(d.status()['listening'])
            self.assertIsNone(d.persistent)
        self.assertEqual(d.actions, original)
        d.model_state = 'unloaded'
        d.command('draft-on')
        self.assertEqual(d.model_state, 'unloaded')

    def test_draft_toggle_is_refused_during_capture_review_and_pending_work(self):
        d = self.daemon
        for attribute in ('persistent', 'session', 'client_capture'):
            setattr(d, attribute, Mock())
            try:
                for command in ('draft-on', 'draft-off'):
                    with self.assertRaises(ValueError):
                        d.command(command)
                self.assertFalse(d.cfg.persistent.draft)
            finally:
                setattr(d, attribute, None)
        identity = d.pipeline.ledger.admit(1)
        try:
            with self.assertRaises(ValueError):
                d.command('draft-on')
        finally:
            d.pipeline.ledger.complete(identity, 'dropped')

    def test_listener_started_with_drafts_off_can_cancel_after_enabling(self):
        d = self.daemon
        self.assertFalse(d.cfg.persistent.draft)
        listener = KeyboardListener(d._listened_keys, d._on_key, Mock(), Mock(), Mock())
        def escape():
            listener.dispatch('test', [Mock(type=ecodes.EV_KEY, code=ecodes.KEY_ESC, value=value)
                                       for value in (1, 0)])
        with patch.object(d, '_on_activity') as activity:
            escape()
            activity.assert_called_once()
        d.command('draft-on')
        d.persistent = Mock(draft=True)
        try:
            escape()
            d.persistent.request_draft_key.assert_called_once_with('cancel')
        finally:
            d.persistent = None
        d.command('draft-off')
        with patch.object(d, '_on_activity') as activity:
            escape()
            activity.assert_called_once()
        self.assertEqual(d.pressed['test'], set())

    def test_enabling_drafts_never_overwrites_an_existing_key_binding(self):
        d = self.daemon
        chord = d._draft_cancel_chord
        d.actions[chord] = ('agent', 'hold')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            d.command('draft-on')
        self.assertFalse(d.cfg.persistent.draft)
        d.command('draft-off')
        self.assertEqual(d.actions[chord], ('agent', 'hold'))

    def test_policy_changes_are_idle_only_and_do_not_touch_app_styles(self):
        d = self.daemon
        styles = dict(d.cfg.polish.app_styles)
        with patch('voicekey.daemon.focus.compositor', return_value='niri'):
            self.assertEqual(d.status()['destination_policy'], 'pause')
            d.command('pin')
            self.assertFalse(d.status()['follow_focus'])
            d.command('follow-focus')
            self.assertTrue(d.status()['follow_focus'])
            d.command('pause-on-switch')
            self.assertEqual(d.status()['destination_policy'], 'pause')
        d.persistent = Mock()
        d.persistent.stopping.is_set.return_value = False
        self.assertTrue(d.status()['listening'])
        with self.assertRaisesRegex(ValueError, 'Stop dictation'):
            d.command('pin')
        self.assertEqual(d.cfg.polish.app_styles, styles)
        d.persistent = None

    def test_status_distinguishes_initial_binding_from_a_ready_session_without_destination(self):
        d = self.daemon
        session = Mock(ready=threading.Event(), stopping=threading.Event())
        session.target.target.application_name = ''
        d.persistent = session
        self.addCleanup(setattr, d, 'persistent', None)
        self.assertTrue(d.status()['listening'])
        self.assertTrue(d.status()['binding'])
        session.ready.set()
        self.assertTrue(d.status()['listening'])
        self.assertFalse(d.status()['binding'])
        self.assertEqual(d.status()['destination_name'], '')

    def test_pause_reason_survives_cleanup_and_start_typing_is_explicit(self):
        d = self.daemon
        d.persistent = Mock(paused=True, reason='No text field detected', last_live=None, typing_fallback=True)
        d._retire_persistent()
        self.assertEqual(d.status()['state'], 'paused')
        self.assertFalse(d.status()['listening'])
        self.assertEqual(d.status()['pause_reason'], 'No text field detected')
        self.assertTrue(d.status()['can_type'])
        d._pause_reason = 'A more detailed explanation'
        self.assertTrue(d.status()['can_type'])  # UI capability is not encoded in prose.
        with patch.object(d, '_start_persistent', side_effect=lambda *a, **kw: setattr(d, 'persistent', Mock())) as start:
            d.command('start-typing')
            self.assertTrue(start.call_args.kwargs['allow_typing'])
            d.persistent = None
            d.command('start')
            self.assertFalse(start.call_args.kwargs['allow_typing'])
        d.persistent = None
        d.command('stop')
        self.assertEqual(d.status()['state'], 'idle')
        self.assertFalse(d.status()['can_type'])

    def test_clipboard_configuration_never_offers_or_starts_simulated_typing(self):
        d = self.daemon
        d.cfg.dictation.inject = 'clipboard'
        d._pause_reason, d._typing_fallback = 'No text field detected', True
        self.assertFalse(d.status()['can_type'])
        with patch.object(d, '_ensure_models') as models:
            with self.assertRaisesRegex(ValueError, 'disabled'):
                d.command('start-typing')
        models.assert_not_called()
        self.assertIsNone(d.persistent)

    def test_stop_uses_existing_session_stop_and_clears_hold_gesture(self):
        d = self.daemon
        d.persistent = Mock()
        d._gesture = ('session', 0)
        d.command('stop')
        d.persistent.request_stop.assert_called_once_with('stopped from panel')
        self.assertIsNone(d._gesture)
        d.persistent = None

    def test_start_is_idempotently_refused_while_busy(self):
        d = self.daemon
        with patch.object(d, '_start_persistent') as start:
            d.persistent = Mock()
            with self.assertRaises(ValueError):
                d.command('start')
            start.assert_not_called()
        d.persistent = None

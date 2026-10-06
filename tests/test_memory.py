"""Model lifecycle tests use owned recorders and fake desktop targets."""
import gc
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from evdev import ecodes

from voicekey.config import Config
from voicekey.daemon import Daemon
from voicekey.gate import Gate
from voicekey.recovery import Journal
from voicekey.target import NotifyPreview, Window, ImeTarget
from tests.test_persistent import ControlledRecorder
from tests.test_pipeline import FakeRecorder, wait_for


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        for module in ('daemon', 'persistent', 'pipeline'):
            patch(f'voicekey.{module}.notify').start()
        patch('voicekey.daemon.focus.compositor', return_value='none').start()
        patch('voicekey.target.focus.window_id', return_value=1).start()
        self.insert = Mock(return_value=True)
        self.bind = patch('voicekey.daemon.target_mod.bind', side_effect=lambda *a, **kw: self.binding(1)).start()
        cfg = Config()
        cfg.pipeline.shutdown_seconds = .5
        self.d = Daemon(cfg, recorder_factory=ControlledRecorder,
                        journal=Journal(self.tmp.name + '/sessions'))
        self.d.gate = Gate(self.tmp.name + '/lock')
        self.d.gate.open()
        self.install_models()
        self.d.start_workers()
        self.addCleanup(self.cleanup)

    def binding(self, window):
        ime = Mock(activation=Mock(return_value=1), before_cursor=Mock(return_value=None),
                   commit=self.insert)
        return ImeTarget(ime, 1, Window(window, True), 'terminal')

    def cleanup(self):
        self.d.close()
        if self.d._model_thread:
            self.d._model_thread.join(3)

    def install_models(self):
        self.d.backend = Mock(transcribe=Mock(return_value='Saved words.'))
        self.d.vad = Mock(speech=lambda samples: bool(np.max(np.abs(samples)) > .01))
        self.d.streaming = None

    def unload(self):
        self.d.command('free-memory')
        wait_for(lambda: self.d.model_state == 'unloaded')

    def cold_start(self, *, fail=False):
        self.unload()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def load():
            entered.set()
            release.wait(3)
            if fail:
                raise RuntimeError('broken model')
            self.install_models()
        patch.object(self.d, '_load_models', side_effect=load).start()
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 1)
        self.assertTrue(entered.wait(1))
        session = self.d.persistent
        self.assertIsNotNone(session)
        wait_for(lambda: self.bind.called)
        return session, release

    def settle(self, session):
        self.assertTrue(session.done.wait(3))
        self.d._on_tick()

    def test_unload_releases_models_and_child_but_keeps_input_method(self):
        backend = weakref.ref(self.d.backend)
        self.d.ime = Mock()
        server = self.d.polish_server = Mock()
        self.d.polisher = Mock()
        self.unload()
        gc.collect()
        self.assertIsNone(backend())
        self.assertIsNone(self.d.vad)
        self.assertIsNone(self.d.polisher)
        server.stop.assert_called_once()
        self.d.ime.close.assert_not_called()
        self.assertEqual(self.d.status()['state'], 'unloaded')
        self.assertEqual(self.d.status()['error'], '')
        self.d.command('free-memory')  # idempotent
        self.assertFalse(self.d.status()['listening'])

    def test_cold_hold_records_and_binds_before_load_and_honors_release(self):
        session, release = self.cold_start()
        self.assertTrue(session.recorder.active)
        self.assertFalse(session.ready.is_set())
        self.assertEqual(self.d.status()['models'], 'loading')
        self.assertTrue(self.d.status()['listening'])
        samples = np.ones(1600, dtype=np.float32) * .2
        session.recorder.push(samples)
        # Model loading must not block key-up or reinterpret a held key as a tap.
        self.d._gesture = (session, time.monotonic() - 1)
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 0)
        self.assertTrue(session.recorder.finished)
        release.set()
        self.settle(session)
        np.testing.assert_array_equal(self.d.backend.transcribe.call_args.args[0], samples)
        self.insert.assert_called_once()
        self.assertEqual(self.insert.call_args.args[0], 'Saved words.')
        self.bind.assert_called_once()

    def test_failed_reload_preserves_audio_and_never_inserts(self):
        session, release = self.cold_start(fail=True)
        session.recorder.push(np.ones(1600, dtype=np.float32) * .2)
        release.set()
        self.settle(session)
        self.insert.assert_not_called()
        self.assertTrue(list(Path(self.tmp.name, 'sessions').glob('*.wav')))
        self.assertIn('broken model', self.d.status()['error'])
        self.assertFalse(session.recorder.active)

    def test_free_memory_during_capture_stops_then_finishes_speech(self):
        self.d.command('start')
        session = self.d.persistent
        wait_for(session.ready.is_set)
        backend = self.d.backend
        session.recorder.push(np.ones(1600, dtype=np.float32) * .2)
        self.d.command('free-memory')
        self.assertTrue(session.recorder.finished)
        self.assertIs(self.d.backend, backend)
        self.settle(session)
        wait_for(lambda: self.d.model_state == 'unloaded')
        backend.transcribe.assert_called_once()
        self.insert.assert_called_once()

    def test_timed_out_native_call_blocks_unloading_even_with_empty_ledger(self):
        slot = self.d.pipeline._slots['transcribe']
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hung():
            entered.set()
            release.wait(3)
        from voicekey.work import WorkTimeout
        with self.assertRaises(WorkTimeout):
            slot.call(hung, time.monotonic() + .05)
        self.d.command('free-memory')
        self.assertIsNotNone(self.d.backend)
        self.assertTrue(self.d.status()['unload_pending'])
        self.assertIn('Waiting', self.d.status()['error'])
        with self.assertRaises(ValueError):
            self.d.command('start')
        release.set()
        wait_for(lambda: not slot.busy)
        self.d._on_tick()
        wait_for(lambda: self.d.model_state == 'unloaded')

    def test_cold_agent_job_waits_for_reload_inside_transcription_slot(self):
        self.unload()
        self.d.recorder = FakeRecorder()
        self.d.recorder_factory = FakeRecorder
        release = threading.Event()
        self.addCleanup(release.set)
        def load():
            release.wait(3)
            self.install_models()
        patch.object(self.d, '_load_models', side_effect=load).start()
        self.d.pipeline._send_agent = Mock()
        self.d._on_key('test', ecodes.KEY_RIGHTALT, 1)
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 1)
        self.d._gesture = (self.d.session, time.monotonic() - 1)  # a hold, not a tap
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 0)
        self.d._on_key('test', ecodes.KEY_RIGHTALT, 0)
        wait_for(lambda: self.d.pipeline._slots['transcribe'].busy)
        release.set()
        wait_for(lambda: not self.d.pipeline.ledger.busy)
        self.d.pipeline._send_agent.assert_called_once_with('Saved words.')

    def test_shutdown_during_loading_stops_capture_and_cleans_late_child(self):
        session, release = self.cold_start()
        session.recorder.push(np.ones(1600, dtype=np.float32) * .2)
        server = Mock()
        # The active callback already started; assign a late child before release.
        self.d.polish_server = server
        self.d.close()
        self.assertFalse(session.recorder.active)
        release.set()
        self.d._model_thread.join(3)
        server.stop.assert_called_once()
        self.assertIsNone(self.d.backend)

    def test_focus_switch_during_reload_keeps_audio_with_its_destination(self):
        from voicekey.focus import Focus
        from tests.test_follow import ManualWatch
        patch('voicekey.daemon.focus.compositor', return_value='niri').start()
        patch('voicekey.daemon.NiriFocusWatch', ManualWatch).start()
        destination = [1]
        patch('voicekey.target.focus.window_id', side_effect=lambda **kw: destination[0]).start()
        self.d.cfg.persistent.destination_policy = 'follow'
        self.bind.side_effect = lambda *a, **kw: self.binding(destination[0])
        session, release = self.cold_start()
        wait_for(lambda: session.watcher is not None)
        a = np.ones(1637, dtype=np.float32) * .2
        b = np.ones(2071, dtype=np.float32) * .5
        session.recorder.push(a)
        destination[0] = 2
        session.watcher.changed(Focus(2, 'terminal', 200))
        session.recorder.push(b)
        self.d.command('stop')
        release.set()
        self.settle(session)
        calls = self.d.backend.transcribe.call_args_list
        self.assertEqual(len(calls), 2)
        np.testing.assert_array_equal(calls[0].args[0], a)
        np.testing.assert_array_equal(calls[1].args[0], b)
        self.insert.assert_called_once()  # old terminal speech is recovery-only

    def test_panel_wait_is_used_only_for_initial_binding_in_follow_mode(self):
        from voicekey.focus import Focus
        from tests.test_follow import ManualWatch
        self.d.cfg.persistent.destination_policy = 'follow'
        destination = [1]
        self.bind.side_effect = lambda *a, **kw: self.binding(destination[0])
        with patch('voicekey.daemon.focus.compositor', return_value='niri'), \
                patch('voicekey.daemon.NiriFocusWatch', ManualWatch), \
                patch('voicekey.target.focus.window_id', side_effect=lambda **kw: destination[0]):
            self.d.command('start')
            session = self.d.persistent
            wait_for(session.ready.is_set)
            for identity in (2, 3):
                destination[0] = identity
                session.watcher.changed(Focus(identity, 'terminal'))
                wait_for(lambda: session.target.target.window_id == identity)
            self.d.command('stop')
            self.settle(session)
        self.assertEqual([c.kwargs['activation_wait'] for c in self.bind.call_args_list], [1.0, .2, .2])

    def test_free_memory_during_reload_finishes_capture_then_unloads(self):
        session, release = self.cold_start()
        session.recorder.push(np.ones(1600, dtype=np.float32) * .2)
        self.d.command('free-memory')
        self.assertTrue(session.recorder.finished)
        self.assertTrue(self.d.status()['unload_pending'])
        self.assertEqual(self.d.model_state, 'loading')
        with self.assertRaises(ValueError):
            self.d._ensure_models()
        release.set()
        self.settle(session)
        wait_for(lambda: self.d.model_state == 'unloaded')
        self.insert.assert_called_once()

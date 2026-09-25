import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evdev import ecodes

from voicekey.config import Config
from voicekey.daemon import Daemon
from voicekey.gate import Gate
from voicekey.recovery import Journal
from tests.test_pipeline import FakeRecorder, FakeTarget, wait_for
from tests.test_notify import queued_notifications
from voicekey.notify import notify


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config()
        self.cfg.pipeline.shutdown_seconds = 0.4
        self.daemon = Daemon(self.cfg, recorder_factory=FakeRecorder,
                             journal=Journal(self.tmp.name + '/sessions'))
        # Exercise the single-recording controller used by agent capture/replay.
        self.daemon.actions[frozenset({ecodes.KEY_RIGHTMETA})] = ('dictate', 'hold')
        self.daemon.gate = Gate(self.tmp.name + '/lock')
        self.daemon.gate.open()
        self.daemon.backend = Mock(transcribe=Mock(return_value='hello'))
        self.daemon.start_workers()
        self.addCleanup(self.daemon.close)
        self.targets = []
        def target(*args):
            obj = FakeTarget()
            self.targets.append(obj)
            return obj
        patch('voicekey.daemon.target_mod.bind', side_effect=target).start()
        patch('voicekey.daemon.notify').start()
        patch('voicekey.pipeline.notify').start()
        self.addCleanup(patch.stopall)

    def key(self, value, code=ecodes.KEY_RIGHTMETA, device='fake'):
        self.daemon._on_key(device, code, value)

    def done(self):
        wait_for(lambda: not self.daemon.pipeline.ledger.busy)

    def test_recording_processing_and_success_stay_in_status_without_popups(self):
        with queued_notifications() as pending, \
                patch('voicekey.daemon.notify', side_effect=notify), \
                patch('voicekey.pipeline.notify', side_effect=notify):
            self.key(1)
            self.assertTrue(self.daemon.status()['listening'])
            self.key(0)
            self.done()
            self.daemon.pipeline.deliveries.join()
            self.assertFalse(self.daemon.status()['listening'])
            self.assertEqual(self.targets[0].calls[0][0], 'hello')
            self.assertTrue(pending.empty())

    def test_status_during_agent_recording_describes_notification_preview(self):
        self.daemon.actions[frozenset({ecodes.KEY_F10})] = ('agent', 'hold')
        self.key(1, ecodes.KEY_F10)
        self.assertEqual(self.daemon.status()['destination'], 'agent notification')
        self.daemon.session.cancel()

    def test_hold_key_transfers_gate_ownership_through_finalization(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        recorder = self.daemon.recorder
        original = recorder.stop
        def stop():
            entered.set()
            release.wait(2)
            return original()
        recorder.stop = stop
        self.key(1)
        self.assertTrue(self.daemon.gate.held)
        session = self.daemon.session
        self.key(0)
        self.assertTrue(entered.wait(1))
        self.assertIsNone(self.daemon.session)
        self.daemon._settle_gate()  # another worker settling during this gap
        self.assertTrue(self.daemon.gate.held)
        self.assertTrue(recorder.stopping.is_set())
        release.set()
        self.done()
        self.assertFalse(self.daemon.gate.held)
        self.assertEqual(session.target.calls[0][0], 'hello')

    def test_release_timestamp_precedes_slow_finalization(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        recorder = self.daemon.recorder
        original = recorder.stop
        def stop():
            entered.set()
            release.wait(2)
            return original()
        recorder.stop = stop
        self.key(1)
        before_release = time.monotonic()
        self.key(0)
        after_release = time.monotonic()
        self.assertTrue(entered.wait(1))
        release.set()
        self.done()
        deadline = self.targets[0].calls[0][1]
        self.assertGreaterEqual(deadline, before_release + self.cfg.dictation.max_delay_seconds)
        self.assertLessEqual(deadline, after_release + self.cfg.dictation.max_delay_seconds)

    def test_toggle_ignores_release_and_stops_on_second_press(self):
        chord = frozenset({ecodes.KEY_F11})
        self.daemon.actions[chord] = ('dictate', 'toggle')
        self.key(1, ecodes.KEY_F11)
        session = self.daemon.session
        self.key(0, ecodes.KEY_F11)
        self.assertIs(self.daemon.session, session)
        self.key(1, ecodes.KEY_F11)
        self.done()
        self.assertIsNone(self.daemon.session)

    def test_another_device_or_key_cannot_release_a_hold(self):
        self.key(1)
        session = self.daemon.session
        self.key(0, device='other')
        self.key(1, ecodes.KEY_F10)
        self.assertIs(self.daemon.session, session)
        self.key(0)
        self.done()

    def test_chord_dispatch_selects_longest_match(self):
        self.key(1, ecodes.KEY_RIGHTALT)
        self.key(1, ecodes.KEY_RIGHTMETA)
        self.assertEqual(self.daemon.session.action, 'agent')
        # Exercise only control; never dispatch a real agent in a test.
        self.daemon.pipeline._send_agent = Mock()
        self.key(0, ecodes.KEY_RIGHTALT)
        self.key(0, ecodes.KEY_RIGHTMETA)
        self.done()

    def test_disconnect_and_source_exit_preserve_recordings(self):
        self.key(1)
        self.daemon._on_device_lost('fake')
        self.done()
        self.key(1)
        self.daemon.recorder.finished = True
        self.daemon._on_tick()
        self.done()
        self.assertEqual(self.daemon.backend.transcribe.call_count, 2)

    def test_recording_limit_transcribes_instead_of_discarding(self):
        self.key(1)
        self.daemon.recorder.elapsed = self.cfg.max_seconds + 1
        self.daemon._on_tick()
        self.done()
        self.assertEqual(self.targets[0].calls[0][0], 'hello')

    def test_tap_releases_gate_without_transcription(self):
        self.daemon.recorder.duration = 0.01
        self.key(1)
        self.key(0)
        self.done()
        self.assertFalse(self.daemon.gate.held)
        self.daemon.backend.transcribe.assert_not_called()

    def test_failed_preview_setup_keeps_capture(self):
        with patch('voicekey.daemon.target_mod.bind', side_effect=RuntimeError('preview')):
            self.key(1)
        self.assertIsNotNone(self.daemon.session)
        # Fallback is clipboard; patch it for the test.
        with patch('voicekey.pipeline.inject.copy') as copy:
            self.key(0)
            self.done()
        copy.assert_called_once_with('hello')

    def test_shutdown_stops_active_capture_closes_resources_and_is_idempotent(self):
        self.key(1)
        recorder = self.daemon.recorder
        self.daemon.ime = Mock()
        self.daemon.polish_server = Mock()
        self.daemon.close()
        self.assertTrue(recorder.stopping.is_set())
        self.assertFalse(self.daemon.gate.held)
        self.daemon.ime.close.assert_called_once()
        self.daemon.polish_server.stop.assert_called_once()
        self.daemon.close()
        self.key(1)
        self.assertIsNone(self.daemon.session)
        self.daemon.ime.close.assert_called_once()

    def test_keyboard_activity_resets_fallback_spacing(self):
        spacing = self.daemon.pipeline.spacing
        spacing.inserted(7, 'hello', spacing.mark())
        self.assertEqual(spacing.prefix(7), ' ')
        self.daemon._on_activity()
        self.assertEqual(spacing.prefix(7), '')

    def test_bindings_describe_configured_keys(self):
        self.assertEqual(self.daemon.bindings(), ['KEY_RIGHTMETA=dictate(tap/hold)', 'KEY_RIGHTALT+KEY_RIGHTMETA=agent(hold)'])

    def test_contended_gate_is_retried_during_capture(self):
        import fcntl
        import os
        fd = os.open(self.daemon.gate.path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            self.key(1)
            self.assertFalse(self.daemon.gate.held)
            fcntl.flock(fd, fcntl.LOCK_UN)
            self.daemon._on_tick()
            self.assertTrue(self.daemon.gate.held)
            self.key(0)
            self.done()
        finally:
            os.close(fd)

    def test_wav_replay_runs_capture_pipeline_and_fake_target_without_desktop_io(self):
        import wave
        path = str(Path(self.tmp.name) / 'sample.wav')
        with wave.open(path, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\0\0' * 8000)
        with patch('voicekey.pipeline.inject.copy') as copy:
            self.daemon.replay(path)
        self.assertEqual(self.targets[0].calls[0][0], 'hello')
        self.assertEqual(len(self.daemon.backend.transcribe.call_args.args[0]), 8000)
        copy.assert_not_called()

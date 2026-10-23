import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evdev import ecodes

from voicekey.config import AgentConfig, Config
from voicekey.daemon import Daemon
from voicekey.gate import Gate
from voicekey.pipeline import Pipeline
from voicekey.recovery import Journal
from tests.test_pipeline import FakeRecorder, FakeTarget, wait_for
from tests.test_notify import queued_notifications
from voicekey.notify import notify


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = patch.dict(os.environ, {'XDG_STATE_HOME': self.tmp.name})
        state.start()
        self.addCleanup(state.stop)
        self.cfg = Config()
        self.cfg.pipeline.shutdown_seconds = 0.4
        self.daemon = Daemon(self.cfg, recorder_factory=FakeRecorder,
                             journal=Journal(self.tmp.name + '/sessions'))
        # Exercise the single-recording controller used by agent capture/replay.
        self.daemon.actions[frozenset({ecodes.KEY_RIGHTMETA})] = ('dictate', 'hold')
        self.daemon.gate = Gate(self.tmp.name + '/lock')
        self.daemon.gate.open()
        self.daemon.backend = Mock(transcribe=Mock(return_value='hello'))
        # Never reach the real agent: the agent window is the fake target's.
        patch('voicekey.pipeline.agent.show').start()
        patch('voicekey.daemon.focus.compositor', return_value='niri').start()
        self.daemon.pipeline.focus_agent = Mock(return_value=7)
        self.daemon.pipeline.open_agent = Mock()
        self.daemon.start_workers()
        self.addCleanup(self.daemon.close)
        self.targets = []
        def target(*args, **kwargs):
            obj = FakeTarget()
            self.targets.append(obj)
            return obj
        self.fake_bind = target
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

    def test_status_during_agent_recording_describes_the_agent_window(self):
        self.daemon.actions[frozenset({ecodes.KEY_F10})] = ('agent', 'hold')
        self.key(1, ecodes.KEY_F10)
        self.assertEqual(self.daemon.status()['destination'], 'fake target')
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
        self.daemon._gesture = (self.daemon.session, time.monotonic() - 1)
        self.key(0, ecodes.KEY_RIGHTALT)
        self.key(0, ecodes.KEY_RIGHTMETA)
        self.done()

    def agent_press(self, *, held):
        self.daemon.pipeline.open_agent = Mock()
        self.key(1, ecodes.KEY_RIGHTALT)
        self.key(1, ecodes.KEY_RIGHTMETA)
        session = self.daemon.session
        self.assertEqual(session.action, 'agent')
        if not held:
            self.cfg.tap_seconds = 10  # keep the tap independent of scheduler delays
        self.daemon._gesture = (session, time.monotonic() - (1 if held else 0))
        self.key(0, ecodes.KEY_RIGHTMETA)
        self.key(0, ecodes.KEY_RIGHTALT)
        self.done()
        self.assertIsNone(self.daemon.session)
        self.assertIsNone(self.daemon._gesture)

    def test_agent_tap_raises_agent_and_discards_recording(self):
        self.agent_press(held=False)
        self.daemon.pipeline.focus_agent.assert_called_once_with(profile=self.cfg.agent)
        self.daemon.pipeline.open_agent.assert_not_called()
        self.daemon.backend.transcribe.assert_not_called()
        self.assertEqual(self.targets[0].calls, [])
        self.assertFalse(self.daemon.gate.held)

    def parking_press(self, *, held, focused):
        """Press the agent key with parking on, the agent window (7) having
        focus beforehand or not; return the park_agent mock."""
        self.cfg.agent.park_workspace = 'hermes'
        self.daemon.pipeline.park_agent = Mock()
        with patch('voicekey.daemon.focus.window_id', return_value=7 if focused else 3):
            self.agent_press(held=held)
        return self.daemon.pipeline.park_agent

    def test_agent_tap_parks_an_agent_that_already_had_focus(self):
        self.parking_press(held=False, focused=True).assert_called_once_with(7, profile=self.cfg.agent)
        self.daemon.backend.transcribe.assert_not_called()

    def test_agent_tap_only_raises_an_agent_that_lacked_focus(self):
        self.parking_press(held=False, focused=False).assert_not_called()
        self.daemon.pipeline.focus_agent.assert_called_once_with(profile=self.cfg.agent)

    def test_agent_hold_dictates_into_a_focused_agent_instead_of_parking(self):
        self.parking_press(held=True, focused=True).assert_not_called()
        self.daemon.pipeline.deliveries.join()
        self.assertEqual(self.targets[0].calls[0][0], 'hello')

    def test_without_parking_the_agent_key_never_asks_for_focus(self):
        with patch('voicekey.daemon.focus.window_id') as window_id:
            self.agent_press(held=False)
        window_id.assert_not_called()

    def test_agent_hold_types_unpolished_transcript_into_agent_window(self):
        polisher = Mock()
        self.daemon.polisher = polisher
        self.agent_press(held=True)
        self.daemon.pipeline.deliveries.join()
        self.assertEqual(self.targets[0].calls[0][0], 'hello')
        polisher.polish.assert_not_called()
        self.daemon.pipeline.open_agent.assert_not_called()

    def closed_agent_hold(self):
        """Hold the agent chord with no agent window open; return the session
        and the callback that reports the window opened."""
        self.daemon.pipeline.focus_agent.return_value = None
        self.key(1, ecodes.KEY_RIGHTALT)
        self.key(1, ecodes.KEY_RIGHTMETA)
        session = self.daemon.session
        self.assertEqual(session.target.kind, 'pending')
        self.assertTrue(self.daemon.gate.held)
        ready = self.daemon.pipeline.open_agent.call_args.kwargs['on_ready']
        return session, ready

    def release_agent(self, session, *, held=True):
        if not held:
            self.cfg.tap_seconds = 10
        self.daemon._gesture = (session, time.monotonic() - (1 if held else 0))
        self.key(0, ecodes.KEY_RIGHTMETA)
        self.key(0, ecodes.KEY_RIGHTALT)

    def test_agent_hold_without_open_terminal_listens_and_lands_once_it_opens(self):
        session, ready = self.closed_agent_hold()
        session.target.show('live words')
        ready(7, '')
        self.assertEqual(self.targets[0].shown, ['live words'])
        self.release_agent(session)
        self.done()
        self.assertEqual(self.targets[0].calls[0][0], 'hello')

    def test_agent_release_before_the_terminal_opens_waits_for_it(self):
        session, ready = self.closed_agent_hold()
        self.release_agent(session)
        wait_for(lambda: self.daemon.pipeline.opening.unfinished_tasks == 1)
        self.assertFalse(self.daemon.pipeline._slots['deliver'].busy, "waiting aside, not in delivery")
        self.assertEqual(self.targets, [])
        ready(7, '')
        self.done()
        self.assertEqual(self.targets[0].calls[0][0], 'hello')

    def configure_profiles(self):
        original = self.cfg.agent
        original.post_transcription_hook = "sed 's/^/Hermes: /'"
        alternate = AgentConfig(profile_id='codex', name='Codex',
                                app_id='dev.voicekey.Codex', terminal_title='Voicekey Codex',
                                command=['codex'], park_workspace='codex',
                                post_transcription_hook="sed 's/^/Codex: /'")
        self.cfg.agent_profiles = {'hermes': original, 'codex': alternate}
        self.daemon.agent_profiles = self.cfg.agent_profiles
        return original, alternate

    def test_selection_while_agent_opens_keeps_original_delivery_and_hook(self):
        original, alternate = self.configure_profiles()
        session, ready = self.closed_agent_hold()
        self.assertEqual(session.agent_profile, original)
        self.assertEqual(self.daemon.pipeline.open_agent.call_args.kwargs['profile'], original)
        self.assertEqual(self.daemon.command('agent-select', args={'profile': 'codex'}),
                         {'agent_profile': 'codex'})
        self.assertEqual(self.daemon.status()['agent_title'], alternate.terminal_title)
        self.assertTrue(self.daemon.status()['listening'])
        self.assertEqual(session.agent_profile.profile_id, 'hermes')
        self.release_agent(session)
        wait_for(lambda: self.daemon.pipeline.opening.unfinished_tasks == 1)
        ready(7, '')
        self.done()
        self.assertEqual(self.targets[0].calls[0][0], 'Hermes: hello')
        self.daemon.pipeline.focus_agent.return_value = 7
        self.agent_press(held=True)
        self.assertEqual(self.daemon.pipeline.focus_agent.call_args.kwargs['profile'], alternate)
        self.assertEqual(self.targets[1].calls[0][0], 'Codex: hello')

    def test_selection_between_key_down_and_tap_release_parks_original_profile(self):
        original, alternate = self.configure_profiles()
        original.park_workspace = 'hermes'
        self.daemon.pipeline.park_agent = Mock()
        with patch('voicekey.daemon.focus.window_id', return_value=7):
            self.key(1, ecodes.KEY_RIGHTALT)
            self.key(1, ecodes.KEY_RIGHTMETA)
        session = self.daemon.session
        self.daemon.command('agent-select', args={'profile': 'codex'})
        original.park_workspace = 'changed-after-key-down'
        self.release_agent(session, held=False)
        self.done()
        parked = self.daemon.pipeline.park_agent.call_args
        self.assertEqual(parked.args, (7,))
        self.assertEqual(parked.kwargs['profile'].profile_id, 'hermes')
        self.assertEqual(parked.kwargs['profile'].park_workspace, 'hermes')
        self.assertIs(self.daemon.active_agent, alternate)
        self.daemon.backend.transcribe.assert_not_called()

    def test_explicit_inactive_profile_tap_raises_parks_or_opens_without_selection(self):
        original, alternate = self.configure_profiles()
        self.daemon.pipeline.park_agent = Mock()
        for window, focused, expected in ((7, 3, 'raised'), (7, 7, 'parked'),
                                          (None, 3, 'opening')):
            with self.subTest(expected=expected), \
                    patch('voicekey.daemon.focus.window_id', return_value=focused):
                self.daemon.pipeline.focus_agent.reset_mock(return_value=False)
                self.daemon.pipeline.focus_agent.return_value = window
                self.daemon.pipeline.open_agent.reset_mock()
                self.daemon.pipeline.park_agent.reset_mock()
                result = self.daemon.command('agent-tap', args={'profile': 'codex'})
                self.assertEqual(result, {'agent': expected})
                self.daemon.pipeline.focus_agent.assert_called_once_with(profile=alternate)
                if expected == 'parked':
                    self.daemon.pipeline.park_agent.assert_called_once_with(7, profile=alternate)
                else:
                    self.daemon.pipeline.park_agent.assert_not_called()
                if expected == 'opening':
                    self.daemon.pipeline.open_agent.assert_called_once_with(profile=alternate)
                else:
                    self.daemon.pipeline.open_agent.assert_not_called()
                self.assertEqual(self.daemon.agent_profile, 'hermes')
                self.assertIs(self.daemon.active_agent, original)
                self.assertIsNone(self.daemon.session)
        self.daemon.backend.transcribe.assert_not_called()

    def test_dictation_is_not_held_behind_an_agent_window_still_opening(self):
        session, ready = self.closed_agent_hold()
        self.release_agent(session)
        wait_for(lambda: self.daemon.backend.transcribe.call_count == 1)
        self.key(1)  # dictate, while the agent window is still opening
        self.key(0)
        wait_for(lambda: self.targets and self.targets[0].calls)
        self.assertEqual(self.targets[0].calls[0][0], 'hello')
        ready(7, '')
        self.done()
        self.assertEqual(self.targets[1].calls[0][0], 'hello')

    def test_agent_tap_is_timed_by_the_keyboard_not_by_binding(self):
        """Raising and binding block the listener, so the release is read
        late; its own time still makes it a tap, which parks the agent."""
        self.cfg.agent.park_workspace = 'hermes'
        self.daemon.pipeline.park_agent = Mock()
        fake_bind = self.fake_bind
        def slow_bind(*args, **kwargs):
            time.sleep(0.3)
            return fake_bind(*args, **kwargs)
        pressed = time.monotonic()
        with patch('voicekey.daemon.focus.window_id', return_value=7), \
                patch('voicekey.daemon.target_mod.bind', side_effect=slow_bind):
            self.daemon._on_key('fake', ecodes.KEY_RIGHTALT, 1, at=pressed)
            self.daemon._on_key('fake', ecodes.KEY_RIGHTMETA, 1, at=pressed)
        self.assertGreater(time.monotonic() - pressed, self.cfg.tap_seconds)
        self.daemon._on_key('fake', ecodes.KEY_RIGHTMETA, 0, at=pressed + 0.05)
        self.daemon._on_key('fake', ecodes.KEY_RIGHTALT, 0, at=pressed + 0.06)
        self.done()
        self.daemon.pipeline.park_agent.assert_called_once_with(7, profile=self.cfg.agent)
        self.daemon.backend.transcribe.assert_not_called()

    def test_agent_tap_while_opening_never_claims_the_new_window(self):
        session, ready = self.closed_agent_hold()
        self.release_agent(session, held=False)
        self.done()
        ready(7, '')
        self.assertEqual(self.targets, [])
        self.daemon.backend.transcribe.assert_not_called()

    def test_agent_that_fails_to_open_saves_and_copies_the_transcript(self):
        session, ready = self.closed_agent_hold()
        ready(None, 'hermes not found')
        self.assertEqual(session.target.kind, 'refused')
        with patch('voicekey.pipeline.inject.copy') as copy:
            self.release_agent(session)
            self.done()
        copy.assert_called_once_with('hello')
        self.assertEqual(self.targets, [])

    def test_agent_capture_refuses_when_focus_is_not_the_agent_window(self):
        self.daemon.pipeline.focus_agent.return_value = 99
        self.key(1, ecodes.KEY_RIGHTALT)
        self.key(1, ecodes.KEY_RIGHTMETA)
        session = self.daemon.session
        self.assertEqual(session.target.kind, 'refused')
        self.assertTrue(self.targets[0].cancelled.is_set())
        session.cancel()

    def test_agent_key_needs_niri(self):
        with patch('voicekey.daemon.focus.compositor', return_value='sway'):
            self.key(1, ecodes.KEY_RIGHTALT)
            self.key(1, ecodes.KEY_RIGHTMETA)
        self.assertIsNone(self.daemon.session)
        self.daemon.pipeline.focus_agent.assert_not_called()

    def tap_command(self, *, focused, opening=False):
        """Send agent-tap with parking on, the agent window (7) having focus
        beforehand or not; return the reply and the park_agent mock."""
        self.cfg.agent.park_workspace = 'hermes'
        self.daemon.pipeline.park_agent = Mock()
        if opening:
            self.daemon.pipeline.focus_agent.return_value = None
        with patch('voicekey.daemon.focus.window_id', return_value=7 if focused else 3):
            reply = self.daemon.command('agent-tap')
        self.assertIsNone(self.daemon.session)
        self.assertFalse(self.daemon.pipeline.ledger.busy)
        return reply, self.daemon.pipeline.park_agent

    def test_agent_tap_command_raises_an_agent_that_lacked_focus_without_recording(self):
        reply, park = self.tap_command(focused=False)
        self.assertEqual(reply, {'agent': 'raised'})
        self.daemon.pipeline.focus_agent.assert_called_once_with(profile=self.cfg.agent)
        park.assert_not_called()
        self.assertEqual(self.targets, [])

    def test_agent_tap_command_parks_an_agent_that_already_had_focus(self):
        reply, park = self.tap_command(focused=True)
        self.assertEqual(reply, {'agent': 'parked'})
        park.assert_called_once_with(7, profile=self.cfg.agent)

    def test_agent_tap_command_opens_a_closed_agent(self):
        reply, park = self.tap_command(focused=False, opening=True)
        self.assertEqual(reply, {'agent': 'opening'})
        self.daemon.pipeline.open_agent.assert_called_once_with(profile=self.cfg.agent)
        park.assert_not_called()

    def test_agent_tap_command_is_refused_during_dictation(self):
        self.key(1)
        self.assertIsNotNone(self.daemon.session)
        with self.assertRaises(ValueError):
            self.daemon.command('agent-tap')
        self.daemon.pipeline.focus_agent.assert_not_called()
        self.key(0)
        self.done()

    def test_agent_tap_command_needs_niri(self):
        with patch('voicekey.daemon.focus.compositor', return_value='sway'):
            with self.assertRaises(ValueError):
                self.daemon.command('agent-tap')
        self.daemon.pipeline.focus_agent.assert_not_called()

    def test_status_names_the_agent_window(self):
        self.cfg.agent.terminal_title = 'Agent'
        self.assertEqual(self.daemon.status()['agent_title'], 'Agent')

    def test_agent_slot_serializes_opening_and_raising(self):
        pipeline = self.daemon.pipeline
        shown, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def show(cfg, **kwargs):
            shown.set()
            release.wait(2)
        with patch('voicekey.pipeline.agent.show', side_effect=show) as opened, \
                patch('voicekey.pipeline.agent.raise_terminal', return_value=7) as raised:
            Pipeline.open_agent(pipeline)
            self.assertTrue(shown.wait(1))
            # While a terminal is still opening, presses neither raise nor reopen it.
            self.assertIsNone(Pipeline.focus_agent(pipeline))
            Pipeline.open_agent(pipeline)
            release.set()
            wait_for(lambda: not pipeline._slots['agent'].busy)
            self.assertEqual(Pipeline.focus_agent(pipeline), 7)
        self.assertEqual(opened.call_count, 1)
        raised.assert_called_once()

    def test_hold_during_an_open_in_progress_is_told_the_window_afterwards(self):
        pipeline = self.daemon.pipeline
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def show(cfg, **kwargs):
            started.set()
            release.wait(2)
            return 7
        reported = []
        with patch('voicekey.pipeline.agent.show', side_effect=show) as opened:
            Pipeline.open_agent(pipeline)  # a tap began opening
            self.assertTrue(started.wait(1))
            Pipeline.open_agent(pipeline, on_ready=lambda *args: reported.append(args))
            release.set()
            wait_for(lambda: reported)
        self.assertEqual(reported, [(7, '')])
        self.assertEqual(opened.call_count, 2)  # the second only raises the open window

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
        self.assertEqual(self.daemon.bindings(), ['KEY_RIGHTMETA=dictate(tap/hold)', 'KEY_RIGHTALT+KEY_RIGHTMETA=agent(tap/hold)'])

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

    def test_agent_replay_goes_to_the_agent_window(self):
        import wave
        path = str(Path(self.tmp.name) / 'sample.wav')
        with wave.open(path, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\0\0' * 8000)
        with patch('voicekey.daemon.target_mod.bind', side_effect=self.fake_bind) as bind:
            self.daemon.replay(path, 'agent')
        self.daemon.pipeline.focus_agent.assert_called_once_with(profile=self.cfg.agent)
        self.assertIn('activation_wait', bind.call_args.kwargs, "bound as the agent window")
        self.assertEqual(self.targets[0].calls[0][0], 'hello')

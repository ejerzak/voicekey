from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from voicekey.capture import Session
from voicekey.config import Config
from voicekey.pipeline import Pipeline
from voicekey.recovery import Journal
from voicekey.target import Landing, Outcome, WtypeTarget, NotifyPreview, Window
from voicekey.notify import notify
from tests.test_notify import queued_notifications


class FakeRecorder:
    def __init__(self, duration=1):
        self.active = False
        self.finished = False
        self.elapsed = duration
        self.duration = duration
        self.samples = np.zeros(int(duration * 16000), dtype=np.float32)
        self.stopping = threading.Event()

    def start(self, on_frame):
        self.active = True
        self.on_frame = on_frame

    def request_stop(self):
        self.stopping.set()

    def stop(self):
        self.active = False
        return self.samples, self.duration


class FakeTarget:
    kind = 'fake'

    def __init__(self, outcome=Outcome.CONFIRMED):
        self.window_id = 7
        self.app_id = 'fake'
        self.preview = self
        self.closed = False
        self.cancelled = threading.Event()
        self.calls = []
        self.shown = []
        self.outcome = outcome

    def before(self, wait=0):
        return None

    def show(self, text):
        if not self.closed:
            self.shown.append(text)

    def clear(self):
        self.closed = True

    def cancel(self):
        self.cancelled.set()
        self.clear()

    def land(self, text, deadline, **kwargs):
        self.calls.append((text, deadline, kwargs))
        return Landing(self.outcome)

    def describe(self):
        return 'fake target'


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('timed out')
        time.sleep(0.005)


class StartupRecoveryTests(unittest.TestCase):
    def test_startup_reports_recovery_before_accepting_new_work_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory + '/sessions')
            journal.append('f', 'session-start')
            journal.append('a', 'segment', session_id='f', sequence=0)
            journal.append('a', 'final', final='Saved before the crash.')
            backend, polisher = Mock(), Mock()
            pipeline = Pipeline(Config(), backend=backend, polisher=polisher, journal=journal)
            with patch('voicekey.pipeline.notify') as notify:
                pipeline.start()
                self.addCleanup(pipeline.close)
            self.assertIn(str(journal.path('f', '.recovery.txt')), notify.call_args.args[1])
            backend.assert_not_called()
            polisher.assert_not_called()
            self.assertFalse(pipeline._storage_failed)
            self.assertFalse(pipeline.ledger.busy)
            identity = pipeline.admit()
            self.assertIsNotNone(identity)
            pipeline.ledger.complete(identity, 'dropped')


class PipelineTests(unittest.TestCase):
    def test_delivery_filters_after_transforms_and_journals_original_formatting(self):
        self.cfg.polish.min_words = 0
        self.polisher = Mock(polish=Mock(return_value='hello paragraph'))
        self.cfg.text.word_overrides = {'paragraph': '\n\nsecond'}
        self.cfg.dictation.post_transcription_hook = "printf 'first\\n\\tsecond\\n'"
        target = WtypeTarget(NotifyPreview('dictate'), Window(7, False), 'unknown')
        with patch('voicekey.inject._run') as run, patch('voicekey.target.notify'):
            session = self.submit(target)
            self.done()
        self.assertEqual(run.call_args.args[1], 'first second ')
        events = self.events(session.id)
        final = next(e for e in events if e['event'] == 'final')
        self.assertEqual(final['overridden'], 'hello \n\nsecond')
        self.assertEqual(final['final'], 'first\n\tsecond\n')
        self.assertEqual(next(e for e in events if e['event'] == 'delivery-attempt')['final'],
                         'first\n\tsecond\n')
        self.assertEqual(events[-1]['outcome'], 'submitted')

    def test_overrides_and_hook_follow_polish_and_are_journaled(self):
        self.cfg.polish.min_words = 0
        self.polisher = Mock()
        self.polisher.polish.return_value = 'hello hyper whisper'
        self.cfg.text.word_overrides = {'hyper whisper': 'hyprwhspr'}
        self.cfg.dictation.post_transcription_hook = "sed 's/^/prepared: /'"
        session = self.submit()
        self.done()
        self.assertEqual(session.target.calls[0][0], 'prepared: hello hyprwhspr')
        records, _ = self.journal._recovery_records(self.journal.path(session.id, '.jsonl'))
        final = next(r for r in records if r['event'] == 'final')
        self.assertEqual(final['raw'], 'hello')
        self.assertEqual(final['polished'], 'hello hyper whisper')
        self.assertEqual(final['overridden'], 'hello hyprwhspr')
        self.assertEqual(final['hook_result'], 'applied')

    def test_agent_uses_own_hook_and_types_prepared_text(self):
        self.cfg.dictation.post_transcription_hook = 'printf WRONG'
        self.cfg.agent.post_transcription_hook = r"sed 's/^/<dictation>/; s/$/<\/dictation>/'"
        self.cfg.text.word_overrides = {'hello': 'Hermes'}
        session = self.submit(action='agent')
        self.done()
        self.assertEqual(session.target.calls[0][0], '<dictation>Hermes</dictation>')

    def test_agent_capture_bypasses_polish_and_lands_in_its_target(self):
        self.cfg.polish.min_words = 0
        self.cfg.polish.app_styles = {'fake': 'formal'}
        self.polisher = Mock()
        session = self.submit(action='agent')
        self.done()
        self.polisher.polish.assert_not_called()
        self.assertEqual(session.target.calls[0][0], 'hello')
        final = next(e for e in self.events(session.id) if e['event'] == 'final')
        self.assertEqual(final['polish_result'], 'agent bypass')
        self.assertEqual(final['polish_style'], self.cfg.polish.style)
        self.assertEqual(final['final'], 'hello')
        self.assertEqual(self.events(session.id)[-1]['outcome'], 'confirmed')

    def test_hook_timeout_preserves_order_and_next_dictation(self):
        self.cfg.dictation.post_transcription_hook = 'sleep .4; printf late'
        with patch('voicekey.text.HOOK_SECONDS', .05):
            first, second = self.submit(), self.submit()
            self.done()
        self.assertEqual(first.target.calls[0][0], 'hello')
        self.assertEqual(second.target.calls[0][0], 'hello')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notice = patch('voicekey.pipeline.notify').start()
        self.copy = patch('voicekey.pipeline.inject.copy').start()
        self.addCleanup(patch.stopall)
        self.cfg = Config()
        self.cfg.pipeline.shutdown_seconds = 0.3
        self.cfg.pipeline.journal_seconds = 0.3
        self.backend = Mock(transcribe=Mock(return_value='hello'))
        self.polisher = None
        self.journal = Journal(self.tmp.name + '/sessions')
        self.pipeline = Pipeline(self.cfg, backend=lambda: self.backend,
                                 polisher=lambda: self.polisher, journal=self.journal)
        self.pipeline.start()
        self.addCleanup(self.pipeline.close)

    def submit(self, target=None, *, live='live fallback', age=0, action='dictate'):
        identity = self.pipeline.admit()
        self.assertIsNotNone(identity)
        session = Session(action, 'hold', frozenset(), 'fake', identity=identity)
        if target is not None:
            session.target = target
        else:
            session.target = FakeTarget()
        session.text = live
        self.pipeline.submit(session, FakeRecorder(), time.monotonic() - age)
        return session

    def done(self):
        wait_for(lambda: not self.pipeline.ledger.busy)

    def events(self, identity):
        return [json.loads(line) for line in self.journal.path(identity, '.jsonl').read_text().splitlines()]

    def test_audio_precedes_transcription_and_text_precedes_attempt(self):
        target = FakeTarget()
        def transcribe(samples):
            self.assertTrue(list(Path(self.journal.directory).glob('*.wav')))
            return 'hello'
        self.backend.transcribe.side_effect = transcribe
        original = target.land
        def land(text, deadline, **kwargs):
            events = self.events(session.id)
            self.assertEqual([e['event'] for e in events],
                             ['captured', 'transcribed', 'final', 'delivery-attempt'])
            return original(text, deadline, **kwargs)
        target.land = land
        session = self.submit(target)
        self.done()
        self.assertEqual(target.calls[0][0], 'hello')
        self.assertFalse(self.journal.path(session.id, '.wav').exists())
        self.assertEqual(self.events(session.id)[-1]['outcome'], 'confirmed')

    def test_refused_delivery_copies_and_leaves_a_persistent_notice(self):
        session = self.submit(FakeTarget(Outcome.REFUSED))
        self.done()
        self.copy.assert_called_once_with('hello')
        events = self.events(session.id)
        self.assertEqual(events[-1]['outcome'], 'copied')
        self.assertEqual(next(e for e in events if e['event'] == 'delivery-attempt')['target'], 'fake target')
        summary, body = self.notice.call_args.args
        self.assertEqual(summary, '📋 Copied, not inserted')
        self.assertIn(str(self.journal.path(session.id, '.txt')), body)
        self.assertTrue(self.notice.call_args.kwargs['error'])
        designed = FakeTarget(Outcome.REFUSED)
        designed.kind = 'clipboard'
        self.submit(designed)
        self.done()
        self.assertEqual(self.notice.call_args.args[0], '📋 Copied')
        self.assertNotIn('error', self.notice.call_args.kwargs)

    def test_successful_agent_capture_is_quiet_but_clipboard_recovery_notifies(self):
        with queued_notifications() as pending:
            self.notice.side_effect = notify
            self.submit(action='agent')
            self.done()
            self.pipeline.deliveries.join()
            self.assertTrue(pending.empty())
            target = FakeTarget(Outcome.REFUSED)
            target.kind = 'clipboard'
            self.submit(target)
            self.done()
            self.pipeline.deliveries.join()
            _, command = pending.get_nowait()
            self.assertIn('📋 Copied', command)
            self.copy.assert_called_once_with('hello')

    def test_short_text_skips_polish_and_threshold_is_configurable(self):
        self.polisher = Mock(polish=Mock(return_value='Hello.'))
        first = self.submit()
        self.done()
        self.polisher.polish.assert_not_called()
        self.cfg.polish.min_words = 0
        second = self.submit()
        self.done()
        self.polisher.polish.assert_called_once()
        self.assertEqual(self.polisher.polish.call_args.kwargs, {'app_id': 'fake'})
        self.assertEqual(second.target.calls[0][0], 'Hello.')

    def test_hung_polish_adopts_raw_and_late_result_never_lands(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.cfg.polish.min_words = 0
        self.cfg.polish.max_wait_seconds = 0.05
        def polish(text, wait, *, app_id=None):
            entered.set()
            release.wait(2)
            return 'late rewrite'
        self.polisher = Mock(polish=polish)
        first = self.submit()
        self.assertTrue(entered.wait(1))
        second = self.submit()
        self.done()
        self.assertEqual(first.target.calls[0][0], 'hello')
        self.assertEqual(second.target.calls[0][0], 'hello')
        release.set()
        self.pipeline._slots['polish'].join(1)
        self.assertEqual(len(first.target.calls), 1)

    def test_short_dictation_does_not_overtake_earlier_polish(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.backend.transcribe.side_effect = ['this is a long enough sentence to receive polish', 'short']
        def polish(text, wait, *, app_id=None):
            entered.set()
            release.wait(2)
            return 'polished sentence'
        self.polisher = Mock(polish=polish)
        first = self.submit()
        self.assertTrue(entered.wait(1))
        second = self.submit()
        wait_for(lambda: self.backend.transcribe.call_count == 2)
        self.assertEqual(second.target.calls, [])
        release.set()
        self.done()
        self.assertEqual([x.id for x in self.pipeline.ledger.history], [first.id, second.id])

    def test_blocked_delivery_does_not_delay_transcription(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        target = FakeTarget()
        def land(*args, **kwargs):
            entered.set()
            release.wait(2)
            return Landing(Outcome.CONFIRMED)
        target.land = land
        self.submit(target)
        self.assertTrue(entered.wait(1))
        self.submit()
        wait_for(lambda: self.backend.transcribe.call_count == 2)
        release.set()
        self.done()

    def test_failed_transcription_uses_live_and_retains_audio_when_no_live(self):
        self.backend.transcribe.side_effect = RuntimeError('native failed')
        first = self.submit()
        self.done()
        self.assertEqual(first.target.calls[0][0], 'live fallback')
        second = self.submit(live='')
        self.done()
        self.assertTrue(self.journal.path(second.id, '.wav').exists())
        self.assertEqual(self.events(second.id)[-1]['outcome'], 'saved')

    def test_expired_work_is_preserved_before_copy_without_delivery(self):
        session = self.submit(age=20)
        self.done()
        self.assertEqual(session.target.calls, [])
        self.copy.assert_called_once_with('live fallback')
        self.assertIn('live fallback', (Path(self.tmp.name) / 'last-recovery.txt').read_text())
        self.assertTrue(self.journal.path(session.id, '.wav').exists())

    def test_uncertain_delivery_never_copies_or_retries(self):
        session = self.submit(FakeTarget(Outcome.UNKNOWN))
        self.done()
        self.copy.assert_not_called()
        self.assertEqual(self.events(session.id)[-1]['outcome'], 'unknown')
        self.assertEqual(len(session.target.calls), 1)

    def test_refusals_preserve_each_transcript_when_clipboard_is_overwritten(self):
        self.backend.transcribe.side_effect = ['first', 'second']
        first = self.submit(FakeTarget(Outcome.REFUSED))
        second = self.submit(FakeTarget(Outcome.REFUSED))
        self.done()
        self.assertIn('first', self.journal.path(first.id, '.txt').read_text())
        self.assertIn('second', self.journal.path(second.id, '.txt').read_text())
        self.assertEqual(self.copy.call_count, 2)

    def test_empty_polish_result_never_discards_raw(self):
        self.cfg.polish.min_words = 0
        self.polisher = Mock(polish=Mock(return_value=''))
        session = self.submit()
        self.done()
        self.assertEqual(session.target.calls[0][0], 'hello')

    def test_empty_transcript_drops_only_after_recording_was_saved(self):
        self.backend.transcribe.return_value = ''
        session = self.submit(live='')
        self.done()
        self.assertEqual(self.events(session.id)[0]['event'], 'captured')
        self.assertEqual(self.events(session.id)[-1]['outcome'], 'dropped')
        self.assertEqual(session.target.calls, [])

    def test_admission_bounds_all_stages(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(samples):
            entered.set()
            release.wait(2)
            return 'hello'
        self.backend.transcribe.side_effect = transcribe
        self.submit()
        self.assertTrue(entered.wait(1))
        admitted = []
        while (identity := self.pipeline.admit()) is not None:
            admitted.append(identity)
        self.assertTrue(admitted)
        self.assertLessEqual(len(admitted) + 1, self.cfg.pipeline.max_pending)
        for identity in admitted:
            self.pipeline.ledger.complete(identity, 'dropped')
        release.set()
        self.done()

    def test_disk_failure_disables_new_capture_and_does_not_deliver(self):
        with patch.object(self.journal, 'capture', side_effect=OSError('disk full')):
            session = self.submit()
            self.done()
        self.assertEqual(session.target.calls, [])
        self.assertIsNone(self.pipeline.admit())

    def test_shutdown_invalidates_late_polish_and_revokes_delivery_permission(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.cfg.polish.min_words = 0
        def polish(text, wait, *, app_id=None):
            entered.set()
            release.wait(2)
            return 'late'
        self.polisher = Mock(polish=polish)
        session = self.submit()
        self.assertTrue(entered.wait(1))
        self.pipeline.close(timeout=0.03)
        release.set()
        self.pipeline._slots['polish'].join(1)
        self.assertEqual(session.target.calls, [])
        self.assertFalse(self.pipeline.ledger.busy)
        self.assertFalse(self.journal.path(session.id, '.permit').exists())
        self.assertIn('hello', self.journal.path(session.id, '.txt').read_text())

    def test_live_fallback_keeps_original_audio_after_insertion(self):
        self.backend.transcribe.side_effect = RuntimeError('failed recognizer')
        session = self.submit()
        self.done()
        self.assertEqual(session.target.calls[0][0], 'live fallback')
        self.assertTrue(self.journal.path(session.id, '.wav').exists())
        self.assertFalse(self.journal.path(session.id, '.done').exists())

    def test_storage_capacity_is_reserved_before_admission(self):
        self.journal.available = self.pipeline._disk_reservation - 1
        self.assertIsNone(self.pipeline.admit())
        self.journal.available = self.pipeline._disk_reservation
        first = self.pipeline.admit()
        self.assertIsNotNone(first)
        self.assertIsNone(self.pipeline.admit())
        self.pipeline.ledger.complete(first, 'dropped')

    def test_optional_corpus_failure_does_not_disable_dictation(self):
        self.cfg.recordings_dir = self.tmp.name + '/corpus'
        with patch('voicekey.pipeline.recovery.keep', side_effect=OSError('corpus disk full')):
            session = self.submit()
            self.done()
        self.assertEqual(session.target.calls[0][0], 'hello')
        self.assertFalse(self.pipeline._storage_failed)

    def test_full_capture_is_preserved_on_shutdown_during_finalization(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        identity = self.pipeline.admit()
        session = Session('dictate', 'hold', frozenset(), 'fake', identity=identity)
        session.target = FakeTarget()
        recorder = FakeRecorder()
        original = recorder.stop
        def stop():
            entered.set()
            release.wait(2)
            return original()
        recorder.stop = stop
        self.pipeline.submit(session, recorder, time.monotonic())
        self.assertTrue(entered.wait(1))
        self.pipeline.close(timeout=0.02)
        release.set()
        for thread in self.pipeline._threads:
            thread.join(0.3)
        self.assertTrue(self.journal.path(identity, '.wav').exists())
        self.assertEqual(session.target.calls, [])


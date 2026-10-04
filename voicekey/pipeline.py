"""Bounded hold-to-talk pipeline, independent of keys and capture sources.

The ledger owns admission and terminal state. FIFO stage queues carry each
utterance once, including utterances which skip polish, so final delivery stays
in speech order. Supervised execution slots give callers control of deadlines
without spawning replacement threads for a hung model or application.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, replace

import numpy as np

from . import agent, inject, polish, recovery, text
from .ledger import Ledger, Stage
from .notify import notify
from .recorder import RecordingError
from .spacing import Spacing
from .target import Landing, Outcome
from .work import Slot, WorkBusy, WorkTimeout

log = logging.getLogger("voicekey.pipeline")
MAX_TEXT_BYTES = 100000
# Room for all text stages and delivery intent in JSONL and readable form, even if JSON
# escapes every byte. Reserved separately from audio before capture starts.
TEXT_RESERVE = 8 * 1024 * 1024


@dataclass(frozen=True)
class Job:
    id: str
    action: str
    target: object
    samples: np.ndarray | None
    finished_at: float
    deadline: float
    live: str = ""
    raw: str = ""
    final: str = ""
    polish_deadline: float = 0.0
    failure: str = ""
    session_id: str = ""
    drop_reason: str = ""


class Pipeline:
    def __init__(self, cfg, *, backend, polisher, settled=lambda: None, journal=None, send_agent=None, notifier=None, completed=lambda *args: None):
        self.cfg = cfg
        self.notify = notifier or (lambda *args, **kwargs: notify(*args, **kwargs))
        self.backend = backend
        self.polisher = polisher
        self.completed = completed
        self.settled = settled
        limits = cfg.pipeline
        self.ledger = Ledger(limits.max_pending, limits.max_audio_seconds)
        self.journal = journal or recovery.Journal(megabytes=limits.recovery_megabytes,
                                                  history_days=limits.history_days)
        self._disk_reservation = int(cfg.max_seconds * 32000) + TEXT_RESERVE
        self.spacing = Spacing()
        self.captures, self.jobs, self.polishing, self.deliveries, self.agents = (
            queue.Queue(maxsize=limits.max_pending) for _ in range(5)
        )
        self._closed = threading.Event()
        self._accepting = True
        self._storage_failed = False
        self._stop_at = float("inf")
        self._threads = []
        self._items = {}
        self._recorders = {}
        self._session_deadlines = {}
        # Only the immediately preceding, successfully delivered batch is eligible.
        # A prepared but pending batch invalidates older context without waiting.
        self._polish_context = {}  # session -> (utterance id, bounded text, activity mark)
        self._stalled_audio = 0.0
        self._stalled_corpus_audio = 0.0
        self._items_lock = threading.Lock()
        self._slots = {name: Slot(name) for name in ("transcribe", "polish", "deliver", "agent", "corpus", "hook")}
        self._journal_slots = {name: Slot("journal-" + name) for name in
                               ("startup", "capture", "transcribe", "polish", "deliver", "agent", "close", "session")}
        self._send_agent = send_agent or (lambda text: agent.send_prompt(
            cfg.agent, text, cancelled=self._closed,
            deadline=min(self._stop_at, time.monotonic() + cfg.agent.ready_timeout)))

    def start(self, *, recover=True):
        if self._threads:
            return
        try:
            recovered = (self._journal_slots["startup"].call(self.journal.recover_interrupted,
                         time.monotonic() + self.cfg.pipeline.journal_seconds) if recover else [])
            if recovered:
                self.notify("voicekey: interrupted dictation recovered",
                       f"{len(recovered)} session(s) in {self.journal.directory}; latest: {recovered[-1]}",
                       channel="persistent", ms=0, attention=True)
            self._journal_slots["startup"].call(lambda: self.journal.prepare(self._disk_reservation),
                                                time.monotonic() + self.cfg.pipeline.journal_seconds)
        except Exception as exc:
            self._storage_error(exc)
        for name, source, handler, expected in (("finalize", self.captures, self._finalize, Stage.FINALIZING),
                                      ("transcription", self.jobs, self._transcribe, Stage.TRANSCRIBING),
                                      ("polishing", self.polishing, self._polish, Stage.POLISHING),
                                      ("delivery", self.deliveries, self._deliver, Stage.READY),
                                      ("agent-dispatch", self.agents, self._agent, Stage.READY)):
            thread = threading.Thread(target=self._consume, args=(source, handler, expected), name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def admit(self, *, audio_seconds=None, session_id="", sequence=0, gated=True):
        if not self._accepting or self._storage_failed or self._slots["transcribe"].busy and not self.ledger.gated:
            return None
        retained = self._stalled_audio if self._slots["transcribe"].busy else 0.0
        retained += self._stalled_corpus_audio if self._slots["corpus"].busy else 0.0
        seconds = self.cfg.max_seconds if audio_seconds is None else audio_seconds
        return self.ledger.admit(seconds, retained_audio=retained,
                                 storage_bytes=int(seconds * 32000) + TEXT_RESERVE,
                                 storage_available=self.journal.available, session_id=session_id,
                                 sequence=sequence, gated=gated)

    def stop_session(self, identity, deadline):
        with self._items_lock:
            self._session_deadlines[identity] = deadline

    def forget_session(self, identity):
        with self._items_lock:
            self._session_deadlines.pop(identity, None)
            self._polish_context.pop(identity, None)

    def _deadline(self, job):
        with self._items_lock:
            return min(job.deadline, self._stop_at,
                       self._session_deadlines.get(job.session_id, float("inf")))

    def expire_session(self, session_id):
        """Preserve one stopped session and revoke its remaining attempts."""
        identities = {u.id for u in self.ledger.snapshots() if u.session_id == session_id}
        with self._items_lock:
            items = [self._items[i] for i in identities if i in self._items]
            recorders = {i: self._recorders[i] for i in identities if i in self._recorders}
        for item in items:
            item.target.cancel()
        def preserve():
            for item in items:
                self.journal.revoke(item.id)
                source = recorders.get(item.id)
                if source is not None:
                    self.journal.capture(item.id, source.samples, getattr(item, "text", ""))
                    self.journal.append(item.id, "segment", session_id=session_id,
                        sequence=item.sequence, start_sample=source.start_sample,
                        end_sample=source.end_sample, reason=source.reason)
                current = self.ledger.get(item.id)
                self.journal.append(item.id, "shutdown", disposition="unknown" if current and
                                    current.stage == Stage.DELIVERING else "saved")
        self._save("session", preserve)
        for identity in identities:
            current = self.ledger.get(identity)
            self.ledger.complete(identity, "unknown" if current and current.stage == Stage.DELIVERING else "saved")
        with self._items_lock:
            for identity in identities:
                self._items.pop(identity, None)
                self._recorders.pop(identity, None)
        self.settled()

    def submit(self, session, recorder, finished_at):
        if self.ledger.transition(session.id, Stage.CAPTURING, Stage.FINALIZING):
            with self._items_lock:
                self._items[session.id] = session
                self._recorders[session.id] = recorder
            self.captures.put_nowait((session, recorder, finished_at))

    def _consume(self, source, handler, expected):
        while not self._closed.is_set():
            try:
                item = source.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                identity = item[0].id if isinstance(item, tuple) else item.id
                current = self.ledger.get(identity)
                if not self._closed.is_set() and current is not None and current.stage == expected:
                    handler(item)
            except Exception as exc:
                log.exception("pipeline stage failed")
                target = item[0].target if isinstance(item, tuple) else item.target
                target.clear()
                self.completed(identity, "failed", str(exc))
                self.ledger.complete(identity, "failed")
                with self._items_lock:
                    self._items.pop(identity, None)
                self.notify("voicekey: pipeline failed", f"{exc}; recovery is in {self.journal.directory}", error=True)
            finally:
                source.task_done()
                self.settled()
                item = None  # do not retain the previous audio while get() waits

    def _storage_error(self, exc):
        self._storage_failed = True
        self.notify("voicekey: recovery unavailable", f"{exc}; new recordings disabled until restart", error=True)

    def _save(self, lane, function):
        try:
            return self._journal_slots[lane].call(function, time.monotonic() + self.cfg.pipeline.journal_seconds)
        except Exception as exc:
            self._storage_error(exc)
            raise

    def _remember(self, job):
        with self._items_lock:
            self._items[job.id] = job

    def _complete(self, job, outcome, reason="", lane="deliver"):
        if self.ledger.get(job.id) is None:
            return
        try:
            self._save(lane, lambda: self.journal.append(job.id, "outcome", outcome=str(outcome), reason=reason,
                                                        recovery_needed=bool(job.failure)))
            try:
                self._save(lane, lambda: self.journal.prepare(self._disk_reservation))
            except Exception:
                pass  # outcome is saved; the storage error already disabled new admission
        finally:
            if job.session_id:
                job.target.completed(outcome)
            job.target.clear()
            self.completed(job.id, str(outcome), reason)
            self.ledger.complete(job.id, str(outcome))
            log.info("%s outcome=%s%s", job.id, outcome, f" ({reason})" if reason else "")
            with self._items_lock:
                self._items.pop(job.id, None)
            self.settled()

    def _finalize(self, item):
        session, recorder, finished_at = item
        failure = ""
        try:
            samples, duration = recorder.stop()
        except RecordingError as exc:
            samples, duration, failure = exc.samples, exc.duration, str(exc)
        # Discard accidental taps and explicitly cancelled client recordings.
        if getattr(session, "discard", False) or (duration < self.cfg.min_seconds and not failure
                                                    and not getattr(session, "session_id", "")):
            session.cancel()
            session.target.clear()
            self.completed(session.id, "dropped", "Capture cancelled" if getattr(session, "discard", False)
                           else "Recording shorter than min_seconds")
            self.ledger.complete(session.id, "dropped")
            with self._items_lock:
                self._items.pop(session.id, None)
                self._recorders.pop(session.id, None)
            return
        self._save("capture", lambda: self.journal.capture(session.id, samples, session.text))
        session_id = getattr(session, "session_id", "")
        if session_id:
            self._save("capture", lambda: self.journal.append(session.id, "segment", session_id=session_id,
                sequence=session.sequence, start_sample=recorder.start_sample,
                end_sample=recorder.end_sample, reason=recorder.reason))
        with self._items_lock:
            self._recorders.pop(session.id, None)
        session.finish(timeout=min(1.0, max(0, self._stop_at - time.monotonic())))
        deadline = finished_at + (self.cfg.dictation.max_delay_seconds if session.action == "dictate"
                                  else self.cfg.pipeline.transcription_seconds)
        if hasattr(session, "processing_seconds"):
            deadline = finished_at + session.processing_seconds
        if session_id:
            deadline = float("inf")  # queue age does not revoke a persistent binding
        job = Job(session.id, session.action, session.target, samples, finished_at, deadline,
                  live=session.text, failure=failure, session_id=session_id)
        if not self.ledger.transition(job.id, Stage.FINALIZING, Stage.TRANSCRIBING,
                                      audio_seconds=len(samples) / 16000, live=session.text, storage_bytes=TEXT_RESERVE):
            return
        self._remember(job)
        self.jobs.put_nowait(job)

    def _transcribe(self, job):
        if self._discard_client(job, "transcribe"):
            return
        backend = self.backend()
        failure = job.failure
        try:
            if backend is None:
                raise RuntimeError("transcription backend unavailable")
            deadline = min(self._deadline(job),
                           time.monotonic() + self.cfg.pipeline.transcription_seconds)
            raw = self._slots["transcribe"].call(lambda: backend.transcribe(job.samples), deadline)
            if not isinstance(raw, str) or len(raw.encode()) > MAX_TEXT_BYTES:
                raise ValueError("invalid or oversized transcript")
        except Exception as exc:
            if isinstance(exc, WorkTimeout) and self._slots["transcribe"].busy:
                self._stalled_audio = len(job.samples) / 16000
            raw = job.live
            failure = f"transcription failed ({exc}); using live text" if raw else f"transcription failed ({exc})"
        if self.ledger.get(job.id) is None:
            return
        transcribed_at = time.monotonic()
        log.info("%s transcribed (%d chars)", job.id, len(raw))
        model = self.cfg.backend.model_dir if self.cfg.backend.type == "parakeet" else self.cfg.backend.model
        self._save("transcribe", lambda: self.journal.append(job.id, "transcribed", raw=raw,
            live=job.live, failure=failure, action=job.action, backend=self.cfg.backend.type, model=model))
        if self._discard_client(job, "transcribe"):
            return
        if not raw:
            self._complete(job, Outcome.SAVED if failure else Outcome.DROPPED, failure, "transcribe")
            if failure:
                self.notify("voicekey: recording saved", f"{failure}; audio in {self.journal.directory}", error=True)
            return
        job = replace(job, raw=raw, failure=failure,
                      polish_deadline=min(transcribed_at + self.cfg.polish.max_wait_seconds, job.deadline - 0.15))
        if not self.cfg.recordings_dir:
            job = replace(job, samples=None)
        if not self.ledger.transition(job.id, Stage.TRANSCRIBING, Stage.POLISHING, raw=raw,
                                      audio_seconds=0 if job.samples is None else len(job.samples) / 16000):
            return
        self._remember(job)
        # Every dictation traverses this queue, including short ones, preserving order.
        self.polishing.put_nowait(job)

    def _polish(self, job):
        if self._discard_client(job, "polish"):
            return
        if hasattr(job.target, 'prepare_draft'):
            chunk = job.target.prepare_draft(job, self)
            if chunk is not None:
                self._keep_recording(job, chunk)
            if not self._discard_client(job, "polish"):
                self._complete(job, Outcome.DRAFT, job.failure, "polish")
            return
        final = job.raw
        polisher = self.polisher()
        deadline = min(job.polish_deadline, self._deadline(job))
        drop = bool(job.session_id and self.cfg.persistent.drop_filler_only and polish.filler_only(job.raw))
        minimum = self.cfg.persistent.polish_min_words if job.session_id else self.cfg.polish.min_words
        eligible = (not drop and job.action == "dictate" and polisher is not None
                    and len(polish.words(job.raw)) >= minimum)
        context = ""
        if job.session_id and eligible and self.cfg.persistent.polish_context:
            with self._items_lock:
                previous = self._polish_context.get(job.session_id)
            if previous is not None and previous[2] == self.spacing.mark() and (len(previous) < 4 or previous[3] == getattr(job.target, "context_key", None)):
                context = previous[1]
        polish_result = "below word threshold"
        if polisher is None:
            polish_result = "disabled" if self.cfg.polish.backend == "none" else "polisher unavailable"
        if job.action == "agent":
            polish_result = "agent bypass"
        if eligible:
            polish_result = "deadline reached"
        if drop:
            final = ""
            polish_result = "filler-only utterance"
        if eligible and time.monotonic() < deadline:
            job.target.show(job.raw)
            try:
                cleaned = self._slots["polish"].call(
                    # Each utterance retains the app at its destination boundary.
                    lambda: polisher.polish(job.raw, max(0, deadline - time.monotonic()),
                                           app_id=job.target.app_id, **({"context": context} if context else {})), deadline)
                context_stale = bool(context and previous[2] != self.spacing.mark())
                if context_stale:
                    cleaned = None  # typing during cleanup made the context stale
                if isinstance(cleaned, str) and cleaned.strip() and len(cleaned.encode()) <= MAX_TEXT_BYTES:
                    final = cleaned
                    note = getattr(polisher, "last_reason", None)
                    polish_result = note if isinstance(note, str) and note.startswith("applied") else "applied"
                else:
                    polish_result = "raw fallback: model unavailable or output rejected"
                    reason = ("context invalidated by keyboard activity" if context_stale
                              else getattr(polisher, "last_reason", None))
                    if isinstance(reason, str):
                        polish_result = f"raw fallback: {reason}"
            except Exception as exc:
                polish_result = f"raw fallback: {exc}"
                log.warning("polish skipped: %s", exc)
        if self.ledger.get(job.id) is None:
            return
        if self._discard_client(job, "polish"):
            return
        polished = final
        final, overridden, hook_result = self._prepare_text(job, final) if not drop else (final, final, "disabled")
        if self.ledger.get(job.id) is None:
            return
        style = self.cfg.polish.style
        # Agent prompts use a notification preview, with no application binding.
        if job.action == "dictate":
            style = self.cfg.polish.app_styles.get(job.target.app_id, style)
        self._save("polish", lambda: self.journal.append(job.id, "final", raw=job.raw, final=final,
            polished=polished, overridden=overridden, polish_result=polish_result,
            polish_style=style, polish_context=context, hook_result=hook_result, action=job.action))
        self._keep_recording(job, final)
        job = replace(job, final=final, samples=None, drop_reason="filler-only utterance" if drop else "")
        if not self.ledger.transition(job.id, Stage.POLISHING, Stage.READY, final=final,
                                      audio_seconds=0, gated=job.action == "dictate"):
            return
        if job.session_id and not drop:
            with self._items_lock:
                self._polish_context[job.session_id] = (job.id, "", self.spacing.mark(), getattr(job.target, "context_key", None))
        self._remember(job)
        if job.action == "agent" and sum(not u.gated for u in self.ledger.snapshots()) > self.cfg.pipeline.max_pending:
            self._complete(job, Outcome.SAVED, "agent backlog full", "polish")
            self.notify("voicekey: agent busy", f"prompt saved to {self.journal.path(job.id, '.txt')}", error=True)
            return
        (self.agents if job.action == "agent" else self.deliveries).put_nowait(job)

    def _keep_recording(self, job, final):
        if self.cfg.recordings_dir and job.samples is not None:
            try:
                self._slots["corpus"].call(lambda: recovery.keep(self.cfg.recordings_dir, job.samples,
                                                                job.live, job.raw, final),
                    min(job.deadline - 0.1, time.monotonic() + self.cfg.pipeline.journal_seconds))
            except Exception as exc:
                if isinstance(exc, WorkTimeout) and self._slots["corpus"].busy:
                    self._stalled_corpus_audio = len(job.samples) / 16000
                log.warning("optional recordings corpus unavailable: %s", exc)

    def _prepare_text(self, job, value):
        """Apply explicit corrections, then the action's hook, within its budget."""
        try:
            value = text.override(value, self.cfg.text.word_overrides)
        except ValueError as exc:
            log.warning("word overrides skipped: %s", exc)
        section = self.cfg.agent if job.action == "agent" else self.cfg.dictation
        if not section.post_transcription_hook:
            return value, value, "disabled"
        deadline = min(self._deadline(job) - 0.15, time.monotonic() + text.HOOK_SECONDS)
        try:
            final, reason = self._slots["hook"].call(
                lambda: text.hook(value, section.post_transcription_hook, deadline), deadline)
            return final, value, reason
        except (WorkBusy, WorkTimeout) as exc:
            reason = f"fallback: {exc}"
            log.warning("transcription hook %s", reason)
            return value, value, reason

    def _discard_client(self, job, lane):
        if not getattr(job.target, "discarded", False):
            return False
        self._complete(job, Outcome.DROPPED, "Capture cancelled", lane)
        return True

    def _deliver(self, job):
        if self._discard_client(job, "deliver"):
            return
        if job.drop_reason:
            # Preserve raw/final tiers before a deliberate drop, in queue order.
            # No insertion attempt, clipboard operation or model-authorized
            # deletion is involved; meaningful empty replies still fall back.
            self._complete(job, Outcome.DROPPED, job.drop_reason)
            return
        attempt = self.ledger.reserve(job.id)
        if attempt is None:
            return
        deadline = self._deadline(job)
        if job.session_id:
            deadline = min(deadline, time.monotonic() + self.cfg.persistent.delivery_seconds)
        self._save("deliver", lambda: self.journal.append(job.id, "delivery-attempt", attempt=attempt,
                                                          final=job.final, deadline=deadline,
                                                          target=job.target.describe()))
        job.target.permit = str(self.journal.path(job.id, ".permit"))
        deadline = min(deadline, self._deadline(job))
        mark = self.spacing.mark()
        if time.monotonic() >= deadline:
            landing = Landing(reason="dictation expired before delivery")
        else:
            prefix = self.spacing.prefix(job.target.window_id)
            try:
                landing = self._slots["deliver"].call(
                    lambda: job.target.land(job.final, deadline, operation_id=attempt, prefix=prefix), deadline)
            except WorkBusy:
                landing = Landing(reason="a previous delivery has not returned")
            except Exception as exc:
                job.target.cancel()
                landing = Landing(Outcome.UNKNOWN, f"delivery may have started: {exc}")
        if self.ledger.get(job.id) is None:
            return
        if landing.landed:
            if job.session_id:
                with self._items_lock:
                    previous = self._polish_context.get(job.session_id)
                    if previous is not None and previous[0] == job.id:
                        self._polish_context[job.session_id] = (job.id, polish.context_tail(job.final), mark, getattr(job.target, "context_key", None))
            self.spacing.inserted(job.target.window_id, job.final, mark)
            self._complete(job, landing.outcome)
            if job.failure:
                self.notify("voicekey: recording warning", f"{job.failure}; audio saved in {self.journal.directory}", ms=10000, attention=True)
            elif job.target.kind != "client":
                self.notify("✓ Inserted" if landing.outcome == Outcome.CONFIRMED else "✓ Sent to field", channel="dictate")
        elif landing.uncertain:
            if not job.session_id:
                self._save("deliver", lambda: self.journal.recover(job.id, job.final))
            self._complete(job, Outcome.UNKNOWN, landing.reason)
            self.notify("voicekey: delivery uncertain", f"{landing.reason}; inspect {self.journal.path(job.id, '.txt')}", error=True)
        else:
            # The final text and attempt were already saved before touching the clipboard.
            job.target.clear()
            outcome = Outcome.SAVED
            if not job.session_id:
                self._save("deliver", lambda: self.journal.recover(job.id, job.final))
            if not self._closed.is_set() and not job.session_id and getattr(job.target, "clipboard_fallback", True):
                try:
                    inject.copy(job.final)
                    outcome = Outcome.COPIED
                except Exception as exc:
                    log.warning("clipboard failed: %s", exc)
            self._complete(job, outcome, landing.reason)
            copied = outcome == Outcome.COPIED
            body = f"{landing.reason}; {self.journal.path(job.id, '.txt')}"
            if job.target.kind == "clipboard" or job.session_id:
                self.notify("📋 Copied" if copied else "voicekey: transcript saved", body, channel="dictate", ms=10000, attention=True)
            else:
                # A bound destination refused the text. A transient notice went
                # unnoticed in practice; this one persists until dismissed.
                self.notify("📋 Copied, not inserted" if copied else "voicekey: transcript saved, not inserted",
                            body, error=True)

    def _agent(self, job):
        attempt = self.ledger.reserve(job.id)
        if attempt is None:
            return
        self._save("agent", lambda: self.journal.append(job.id, "agent-attempt", attempt=attempt, raw=job.raw, final=job.final))
        try:
            self._slots["agent"].call(lambda: self._send_agent(job.final),
                                       min(self._stop_at, time.monotonic() + self.cfg.agent.ready_timeout))
        except Exception as exc:
            self._complete(job, Outcome.UNKNOWN, str(exc), "agent")
            self.notify("voicekey: agent delivery uncertain", str(self.journal.path(job.id, '.txt')), error=True)
        else:
            self._complete(job, Outcome.SUBMITTED, lane="agent")
            self.notify("✓ Sent to agent", channel="agent")

    def close(self, timeout=None):
        if self._closed.is_set():
            return
        self._accepting = False
        self._stop_at = time.monotonic() + (self.cfg.pipeline.shutdown_seconds if timeout is None else timeout)
        while self.ledger.busy and time.monotonic() < self._stop_at:
            time.sleep(0.02)
        self._closed.set()
        with self._items_lock:
            items = list(self._items.values())
            recorders = dict(self._recorders)
        for item in items:
            if not isinstance(item, Job):
                item.cancel()
            if hasattr(item.target, "cancel"):
                item.target.cancel()
            else:
                item.target.clear()
        # Stop every remaining source before considering journal/drain cleanup.
        for recorder in recorders.values():
            proc = getattr(recorder, "proc", None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        def preserve_remaining():
            for item in items:
                self.journal.revoke(item.id)
                recorder = recorders.get(item.id)
                if recorder is not None:
                    frames = tuple(getattr(recorder, "frames", ()))
                    samples = np.concatenate(frames) if frames else getattr(recorder, "samples", np.zeros(0, dtype=np.float32))
                    self.journal.capture(item.id, samples, getattr(item, "text", ""))
                current = self.ledger.get(item.id)
                self.journal.append(item.id, "shutdown", disposition="unknown" if current and
                                    current.stage == Stage.DELIVERING else "saved")
        try:
            self._save("close", preserve_remaining)
        except Exception:
            pass
        for item in items:
            # Known audio/text was journalled before entering a fallible stage.
            # A pending delivery intent remains unresolved, never silently retried.
            current = self.ledger.get(item.id)
            self.ledger.complete(item.id, "unknown" if current and current.stage == Stage.DELIVERING else "saved")
        with self._items_lock:
            self._items.clear()
            self._recorders.clear()
        self.settled()
        for thread in self._threads:
            thread.join(0.1)

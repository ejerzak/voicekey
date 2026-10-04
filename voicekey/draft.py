"""An editor draft: bounded rolling cleanup, durable revisions, one insertion.

Only the pipeline's polishing worker revises text. Previewing and the final
accept/cancel decision have separate locks; no editor call runs on the key thread.
"""
import threading
import time
import uuid

from . import emacs, focus, nvim, polish
from .session_target import SessionTarget, UtteranceTarget, join_text
from .target import Landing, Outcome, PinnedEditorTarget
from .work import WorkBusy

MAX_DRAFT_BYTES = 32000


class DraftUnsupported(Exception):
    """A definite capability refusal; ordinary delivery still checks its guards."""


class DraftTarget(SessionTarget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancelled = threading.Event()
        self._draft_lock = threading.Lock()
        self._text = ""
        self._cleanup_by = float("inf")
        self._through = -1
        self._preview_issue = ""
        self.warning = ""
        self._prepared = None

    def initialize(self):
        if not isinstance(self.target, PinnedEditorTarget):
            raise DraftUnsupported()
        try:
            self.target.show_draft("")  # acknowledgement also establishes the anchor
        except NotImplementedError as exc:
            raise DraftUnsupported() from exc
        except (emacs.EmacsRefused, nvim.NvimRefused) as exc:
            # Protocol 6's capability refusal is specific. Expired requests,
            # missing pins, and uncertain RPC results must never downgrade.
            if str(exc) in {"drafts require an editable text buffer", "selection or operator pending"}:
                raise DraftUnsupported() from exc
            raise
        # A refused initialization must leave the ordinary preview usable.
        self.target.preview.clear()

    def field_issue(self, deadline=None):
        if not isinstance(self.target, PinnedEditorTarget):
            return "Draft mode requires an Emacs or Neovim text buffer"
        # Rendering is best effort. A transient RPC timeout is not evidence
        # that the pin or a later insertion is invalid; insertion checks again.
        return "" if self.target.pin_valid else self.target.pin_reason or "Draft buffer pin is invalid"

    def attempt(self, identity):
        self.members.add(identity)
        return DraftUtteranceTarget(self, identity)

    def hotkey_focused(self):
        """True: original destination; False: elsewhere; None: unknown."""
        target = self.target
        current = focus.window_id(timeout=.5)
        if target.window_id is None or current is None:
            return None
        if current != target.window_id:
            return False
        identity = getattr(target, 'focus_identity', None)
        if identity is not None:  # terminal-local focus also distinguishes Neovim panes
            try:
                reply = nvim.call(identity[1], 'status', timeout=.1)
                if reply.get('pid') != identity[0] or not isinstance(reply.get('focused'), bool):
                    return None
                return reply['focused']
            except nvim.NvimError:
                return None
        return True

    def retire(self, identity):
        self.members.discard(identity)

    @property
    def text(self):
        with self._draft_lock:
            return self._text

    def hurry(self, seconds):
        """After acceptance, cap the total time left for optional cleanup."""
        with self._draft_lock:
            self._cleanup_by = min(self._cleanup_by, time.monotonic() + seconds)

    def render(self):
        if self.closed or not self._lock.acquire(blocking=False):
            return
        try:
            if self.cancelled.is_set():
                self.close()
                return
            # Read pending chunks before the prepared text. A chunk published in
            # between then appears in both reads and is filtered by `through`;
            # the reverse order could briefly drop it from the preview.
            snapshots = self.ledger.snapshots()
            with self._draft_lock:
                text, through = self._text, self._through
            pending = [u.raw or u.live for u in snapshots
                       if u.session_id == self.id and u.sequence > through]
            text = join_text((text, *pending))
            # Bound argv and editor display costs, even before a chunk is prepared.
            text = text.encode()[:MAX_DRAFT_BYTES].decode(errors="ignore")
            if text != self._last:
                try:
                    self.target.show_draft(text)
                    self._last = text
                    self._preview_issue = ""
                except Exception as exc:
                    self._preview_issue = f"Draft preview unavailable: {exc}"
        finally:
            self._lock.release()

    def prepare(self, job, pipeline):
        """Clean one chunk as ordinary mode does, then append it to the draft.

        The draft so far is read-only context for the model; earlier text is
        never revised. Hooks and word overrides run once, at acceptance.
        """
        entry = self.ledger.get(job.id)
        if entry is None or self.cancelled.is_set():
            return
        if self.failed.is_set():
            # Never advance coverage past a chunk that was not incorporated:
            # recovery lists every chunk after `through`.
            return
        with self._draft_lock:
            text, cleanup_by = self._text, self._cleanup_by
        chunk, reason = "", "filler-only utterance"
        if not (pipeline.cfg.persistent.drop_filler_only and polish.filler_only(job.raw)):
            chunk, reason = job.raw, "disabled"
            model = pipeline.polisher()
            if model is not None and len(polish.words(job.raw)) >= pipeline.cfg.persistent.polish_min_words:
                # Same budget as ordinary cleanup: a backlogged chunk whose
                # deadline has passed joins the draft raw.
                deadline = min(job.polish_deadline, pipeline._deadline(job), cleanup_by)
                context = text if pipeline.cfg.persistent.polish_context else ""
                reason = "raw: cleanup deadline reached"
                if time.monotonic() < deadline:
                    try:
                        cleaned = pipeline._slots['polish'].call(
                            lambda: model.polish(job.raw, max(0, deadline - time.monotonic()), app_id=job.target.app_id,
                                                 **({"context": context, "revise_end": True} if context else {})),
                            deadline)
                        if isinstance(cleaned, str) and cleaned.strip() and len(cleaned.encode()) <= MAX_DRAFT_BYTES:
                            note = getattr(model, "last_reason", None)
                            chunk, reason = cleaned, note if isinstance(note, str) and note.startswith("applied") else "applied"
                            # Nothing is inserted yet, so the punctuation where a
                            # pause split the text may change: only the final marks.
                            ending = getattr(model, "last_ending", None)
                            if isinstance(ending, str):
                                text = text.rstrip()
                                text, reason = (text[:len(text) - len(polish.final_marks(text))] + ending,
                                                "applied; revised the previous text's final punctuation")
                        else:
                            last = getattr(model, "last_reason", None)
                            reason = f"raw fallback: {last if isinstance(last, str) else 'model unavailable or output rejected'}"
                    except Exception as exc:
                        reason = f"raw fallback: {exc}"
        text = join_text((text, chunk))
        if len(text.encode()) > MAX_DRAFT_BYTES:
            self.failed.set()
            raise ValueError("Draft reached its 32 KB limit; pending speech is saved for recovery")
        if self.cancelled.is_set() or self.ledger.get(job.id) is None:
            return
        # Save the entire draft before publishing it. Sequence identifies the
        # chunks covered by this snapshot when reconstructing an interrupted draft.
        pipeline._save('polish', lambda: pipeline.journal.append(self.id, 'draft-update',
            draft=True, final=text, through=entry.sequence, polish_result=reason))
        pipeline._save('polish', lambda: pipeline.journal.append(job.id, 'final',
            final=chunk, raw=job.raw, draft_part=True, polish_result=reason))
        with self._draft_lock:
            self._text = text
            self._through = entry.sequence
            if job.failure:
                self.warning = job.failure
        return chunk

    def commit(self, pipeline):
        """One durable, revocable attempt, with the usual delivery supervision."""
        from .pipeline import Job
        text = self.text
        if self.cancelled.is_set() or not text:
            return Landing(Outcome.DROPPED, "Draft cancelled" if self.cancelled.is_set() else "No speech")
        deadline = time.monotonic() + pipeline.cfg.persistent.delivery_seconds
        job = Job(self.id, 'dictate', self.target, None, time.monotonic(), deadline, final=text)
        # Corrections/hooks run once, including when a definite refusal is retried.
        if self._prepared is None:
            self._prepared = pipeline._prepare_text(job, text)
            final, overridden, hook_result = self._prepared
            pipeline._save('session', lambda: pipeline.journal.append(self.id, 'final',
                draft=True, final=final, polished=text, overridden=overridden, hook_result=hook_result))
        final = self._prepared[0]
        if len(final.encode()) > MAX_DRAFT_BYTES:
            return Landing(reason="Prepared draft exceeds 32 KB; text saved for recovery")
        if self.cancelled.is_set():
            return Landing(Outcome.DROPPED, "Draft cancelled")
        operation = uuid.uuid4().hex
        pipeline._save('session', lambda: pipeline.journal.append(self.id, 'delivery-attempt',
            attempt=operation, final=final, target=self.target.describe(), deadline=deadline))
        permit = str(pipeline.journal.path(self.id, '.permit'))
        if self.cancelled.is_set():
            # A discard between the check above and creating the permit revoked
            # nothing; revoke here so no editor request can see a live permit.
            pipeline.journal.revoke(self.id)
            return Landing(Outcome.DROPPED, "Draft cancelled")
        mark = pipeline.spacing.mark()
        try:
            landing = pipeline._slots['deliver'].call(
                lambda: self.target.insert_pinned(final, deadline, operation,
                    self.target.prefix, permit, self.cancelled), deadline)
        except WorkBusy:
            landing = Landing(reason="A previous delivery is still running")
        except Exception as exc:
            self.cancelled.set()
            pipeline.journal.revoke(self.id)
            landing = Landing(Outcome.UNKNOWN, f"Draft delivery may have started: {exc}")
        pipeline._save('session', lambda: pipeline.journal.append(self.id, 'delivery-result',
            attempt=operation, outcome=str(landing.outcome), reason=landing.reason))
        pipeline.journal.revoke(self.id)
        if landing.landed:
            pipeline.spacing.inserted(self.target.window_id, final, mark)
        return landing


class DraftUtteranceTarget(UtteranceTarget):
    @property
    def discarded(self):
        return self.session.cancelled.is_set()

    def prepare_draft(self, job, pipeline):
        return self.session.prepare(job, pipeline)

    def completed(self, outcome):
        self._completed = True
        if outcome not in (Outcome.DRAFT, Outcome.DROPPED):
            self.session.failed.set()

    def clear(self):
        # A stage exception clears the target without an outcome. The chunk is
        # then missing from the draft, which must not remain acceptable.
        if not getattr(self, "_completed", False):
            self.session.failed.set()
        super().clear()

    def _land(self, *args, **kwargs):
        return Landing(reason="Draft chunks cannot be inserted individually")

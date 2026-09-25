"""Bound destinations and evidence about a single insertion attempt.

An IME activation is not a stable field handle. Losing it ends automatic
insertion for that dictation. Emacs pins a buffer after a timely acknowledgement;
wtype can verify only a window. The pipeline journals text before delivery.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum

from . import emacs, focus, inject
from .config import DictationConfig
from .delivery import UnsafeText
from .ime import ImeHung, InputMethod
from .notify import notify
from .spacing import owed, spaced

LABEL = {"dictate": "dictation", "agent": "agent"}
ACTIVATION_WAIT = 0.2


class Outcome(StrEnum):
    REFUSED = "refused"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    COPIED = "copied"
    SAVED = "saved"
    DROPPED = "dropped"
    DRAFT = "draft"


@dataclass(frozen=True)
class Landing:
    outcome: Outcome = Outcome.REFUSED
    reason: str = ""

    @property
    def landed(self):
        return self.outcome in (Outcome.CONFIRMED, Outcome.SUBMITTED)

    @property
    def uncertain(self):
        return self.outcome == Outcome.UNKNOWN


@dataclass(frozen=True)
class Capabilities:
    identity: str
    acknowledgement: bool = False
    replacement: bool = False


class NotifyPreview:
    name = "notification"

    def __init__(self, action: str):
        self.action = action
        self.closed = False
        self._last = 0.0
        self._lock = threading.Lock()

    def show(self, text: str):
        if not text:
            return
        with self._lock:
            now = time.monotonic()
            if not self.closed and now - self._last >= 0.25:
                self._last = now
                notify(f"● {LABEL[self.action]}", text, ms=60000, channel=self.action)

    def clear(self, *, timeout: float | None = None):
        with self._lock:
            self.closed = True
        return True

    def describe(self) -> str:
        return f"{LABEL[self.action]} notification"


class ImePreview:
    name = "in-field"

    def __init__(self, ime: InputMethod, generation: int, prefix: str = "", *, allow_formatting=False):
        self.ime, self.generation, self.prefix = ime, generation, prefix
        self.allow_formatting = allow_formatting
        self.owner = uuid.uuid4().hex
        self.closed = False
        self._lock = threading.Lock()
        ime.claim_preview(self.owner, allow_formatting=allow_formatting)

    def show(self, text: str):
        with self._lock:
            if not self.closed:
                self.ime.preedit(spaced(self.prefix, text), self.generation, self.owner)

    def close(self):
        with self._lock:
            self.closed = True

    def clear(self, *, timeout: float | None = None):
        with self._lock:
            self.closed = True
            self.ime.preedit("", self.generation, self.owner)
        if timeout is not None:
            return self.ime.clear_preedit(self.generation, self.owner, timeout=timeout)
        return True


class Window:
    def __init__(self, window_id, verify: bool, pid=None):
        self.id, self.verify, self.pid = window_id, verify, pid

    def focused(self, deadline: float) -> bool:
        if time.monotonic() >= deadline:
            return False
        if not self.verify:
            return True
        return self.id is not None and focus.window_id(timeout=min(0.2, deadline - time.monotonic())) == self.id


class Target:
    kind = ""
    capabilities = Capabilities("none")
    binding_refusal = ""

    def __init__(self, preview, window: Window, app_id: str | None):
        self.preview, self.window, self.app_id = preview, window, app_id
        self.cancelled = threading.Event()
        self._attempt_lock = threading.Lock()
        self._attempted = False
        self.permit: str | None = None

    @property
    def window_id(self):
        return self.window.id

    @property
    def prefix(self):
        return getattr(self.preview, "prefix", "")

    @prefix.setter
    def prefix(self, value):
        self.preview.prefix = value

    def show(self, text):
        if not self.cancelled.is_set():
            self.preview.show(text)

    def clear(self):
        self.preview.clear()

    def cancel(self):
        self.cancelled.set()
        self.clear()

    def before(self, wait=0.0):
        return None

    def describe(self) -> str:
        """The bound destination, for logs and the delivery journal."""
        return f"{self.kind or 'unbound'} target in {self.app_id or 'an unknown application'}"

    @property
    def application_name(self) -> str:
        name = (self.app_id or "").rsplit(".", 1)[-1]
        return name[:1].upper() + name[1:]

    def land(self, text: str, deadline: float, *, operation_id: str, prefix: str = "") -> Landing:
        with self._attempt_lock:
            if self._attempted:
                return Landing(Outcome.UNKNOWN, "this target already had a delivery attempt")
            if self.cancelled.is_set() or time.monotonic() >= deadline:
                return Landing(reason="delivery expired or cancelled")
            self._attempted = True
        return self._land(text, deadline, operation_id, prefix)

    def _land(self, text, deadline, operation_id, prefix):
        raise NotImplementedError


class ImeTarget(Target):
    kind = "input method"
    capabilities = Capabilities("activation")

    def __init__(self, ime, generation, window, app_id, *, allow_formatting=False):
        super().__init__(ImePreview(ime, generation, allow_formatting=allow_formatting), window, app_id)
        self.ime = ime

    def before(self, wait=0.0):
        return self.ime.before_cursor() if self.ime.activation() == self.preview.generation else None

    def _land(self, text, deadline, operation_id, prefix):
        self.preview.close()
        if not self.window.focused(deadline):
            self.clear()
            return Landing(reason="the bound window is no longer focused")
        try:
            sent = self.ime.commit(text, self.preview.generation,
                                   timeout=max(0, deadline - time.monotonic()), owner=self.preview.owner,
                                   prefix=prefix, cancelled=self.cancelled,
                                   allow_formatting=self.preview.allow_formatting)
        except UnsafeText as exc:
            self.clear()
            return Landing(reason=str(exc))
        except ImeHung as exc:
            return Landing(Outcome.UNKNOWN, str(exc))
        if sent:
            return Landing(Outcome.SUBMITTED)
        self.clear()
        return Landing(reason="the original field activation ended; check any provisional text before pasting")


class PinnedEditorTarget(Target):
    """Acknowledged editor binding, independent of compositor focus.

    Implementations acquire a pin on construction, acknowledge it through
    before(), and own preview cleanup, bounded insertion and pin release.
    Session callers may retain the pin across independent operation IDs.
    """
    pin_timeout = 0.25
    capabilities = Capabilities("buffer", acknowledgement=True)

    @property
    def pin_valid(self):
        return self.pinning.valid

    @property
    def pin_reason(self):
        return self.pinning.reason

    def before(self, wait=0.0):
        return self.pinning.before(wait)

    def show_tail(self, text, deadline):
        self.preview.show(text)

    def availability_issue(self, deadline=None):
        return ""

    def insert_pinned(self, text, deadline, operation, prefix, permit, cancelled):
        raise NotImplementedError

    def unpin(self):
        raise NotImplementedError

    def show_draft(self, text, *, timeout=.25):
        raise NotImplementedError("This editor does not support drafts")


class EmacsTarget(PinnedEditorTarget):
    kind = "emacs"
    capabilities = Capabilities("buffer", acknowledgement=True)

    def __init__(self, preview, window, app_id, pinning=None, pid=None):
        super().__init__(preview, window, app_id)
        self.pinning = pinning or emacs.PendingPin(pid)
        self._unpinned = False

    def before(self, wait=0.0):
        return self.pinning.before(wait)

    def describe(self) -> str:
        return f"emacs {self.pinning.describe()}"

    def _land(self, text, deadline, operation_id, prefix):
        self.pinning.before(max(0, deadline - time.monotonic()))
        if not self.pinning.valid:
            self.clear()
            return Landing(reason=self.pinning.reason or "Emacs did not acknowledge the original buffer in time")
        if time.monotonic() >= deadline or self.cancelled.is_set():
            self.clear()
            return Landing(reason="Emacs insertion expired or cancelled before submission")
        try:
            if not self.preview.clear(timeout=max(0, deadline - time.monotonic())):
                return Landing(reason="Emacs preview cleanup did not finish before insertion")
        except ImeHung as exc:
            # Only a cosmetic clear was attempted; no editor insertion began.
            return Landing(reason=f"Emacs preview cleanup failed: {exc}")
        if time.monotonic() >= deadline or self.cancelled.is_set():
            return Landing(reason="Emacs insertion expired or cancelled during preview cleanup")
        try:
            emacs.insert(text, self.pinning.id, timeout=max(0, deadline - time.monotonic()),
                         operation_id=operation_id, prefix=prefix, permit=self.permit)
        except emacs.EmacsRefused as exc:
            return Landing(reason=str(exc))
        except emacs.EmacsError as exc:
            return Landing(Outcome.UNKNOWN, str(exc))
        return Landing(Outcome.CONFIRMED)

    def clear(self):
        # A single dictation ends here whatever its outcome (including no speech
        # or refusal); unpinning returns the buffer to the Evil state it had.
        super().clear()
        self.unpin()

    def unpin(self):
        if self._unpinned:
            return
        self._unpinned = True
        # A pin request still in flight could otherwise land after this unpin
        # and leave its buffer in insert state.
        self.pinning.before(emacs.PIN_TIMEOUT + 0.25)
        try:
            emacs.unpin(self.pinning.id)
        except emacs.EmacsError:
            pass

    def show_draft(self, text, *, timeout=.25):
        emacs.draft(self.pinning.id, text, timeout=timeout)

    def insert_pinned(self, text, deadline, operation, prefix, permit, cancelled):
        self.pinning.before(max(0, deadline - time.monotonic()))
        if not self.pinning.valid:
            landing = Landing(reason=self.pinning.reason or "Emacs did not acknowledge the session buffer")
        else:
            preview = self.preview
            try:
                cleared = (preview.ime.clear_preedit(preview.generation, preview.owner,
                    timeout=max(0, deadline - time.monotonic())) if isinstance(preview, ImePreview) else True)
            except ImeHung:
                cleared = False
            if not cleared or cancelled.is_set():
                landing = Landing(reason="preview cleanup did not finish")
            else:
                try:
                    emacs.insert(text, self.pinning.id, timeout=max(0, deadline - time.monotonic()),
                                 operation_id=operation, prefix=prefix, permit=permit, keep_pin=True)
                    landing = Landing(Outcome.CONFIRMED)
                except emacs.EmacsRefused as exc:
                    landing = Landing(reason=str(exc))
                except emacs.EmacsError as exc:
                    landing = Landing(Outcome.UNKNOWN, str(exc))
        return landing


class WtypeTarget(Target):
    kind = "wtype"
    capabilities = Capabilities("window")

    def _land(self, text, deadline, operation_id, prefix):
        self.clear()
        if not self.window.focused(deadline) or self.cancelled.is_set() or time.monotonic() >= deadline:
            return Landing(reason="focus changed or delivery expired")
        try:
            inject.type_text(spaced(prefix, text), timeout=max(0, deadline - time.monotonic()))
        except UnsafeText as exc:
            return Landing(reason=str(exc))
        except FileNotFoundError as exc:
            return Landing(reason=f"typing could not start: {exc}")
        except Exception as exc:
            return Landing(Outcome.UNKNOWN, f"typing may be partial: {exc}")
        return Landing(Outcome.SUBMITTED)


class ClipboardTarget(Target):
    kind = "clipboard"

    def _land(self, text, deadline, operation_id, prefix):
        self.clear()
        return Landing(reason="clipboard target")


class RefusedTarget(Target):
    kind = "refused"

    @property
    def clipboard_fallback(self):
        return not self.cancelled.is_set()

    @property
    def binding_refusal(self):
        return self.reason

    def __init__(self, window, app_id, reason):
        super().__init__(NotifyPreview("dictate"), window, app_id)
        self.reason = reason

    def describe(self):
        return self.reason

    def _land(self, text, deadline, operation_id, prefix):
        return Landing(reason=self.reason)


def terminal_target(destination, window):
    """A focused Neovim gets buffer delivery; None keeps ordinary terminal typing."""
    from . import nvim
    from .nvim_target import NeovimTarget
    resolution = nvim.resolve(destination)
    if resolution.registration is not None:
        return NeovimTarget(window, destination.app_id, resolution.registration)
    if resolution.reason:
        return RefusedTarget(window, destination.app_id, resolution.reason)
    return None


def bind(ime: InputMethod | None, cfg: DictationConfig, landing: bool, *,
         activation_wait: float = ACTIVATION_WAIT) -> Target:
    """Check the window on both sides of activation acquisition.

    Emacs shares the Wayland preview. Start its buffer pin before waiting for
    the input method; final insertion remains bound to that acknowledged buffer.
    The pin names the focused window's process so that a second Emacs process
    is refused rather than bound to the server's own selected buffer.
    """
    focused = focus.focused(timeout=0.2)
    # A panel start may arrive while its popout is still giving focus back.
    # Wait only at initial binding; never retarget an existing utterance.
    deadline = time.monotonic() + activation_wait
    while focused.id is None and activation_wait > ACTIVATION_WAIT and time.monotonic() < deadline:
        time.sleep(0.1)
        focused = focus.focused(timeout=0.2)
    window = Window(focused.id, cfg.require_same_window, focused.pid)
    terminal_editor = terminal_target(focused, window)
    if terminal_editor is not None:
        if focus.focused(timeout=0.2) != focused:
            terminal_editor.cancel()
            return RefusedTarget(window, focused.app_id, "Focus changed while binding Neovim; terminal typing refused")
        return terminal_editor
    editor = (EmacsTarget(NotifyPreview("dictate"), window, focused.app_id, pid=focused.pid)
              if focused.app_id == "emacs" else None)
    generation = None
    if ime is not None:
        try:
            if landing or ime.rebind(timeout=ACTIVATION_WAIT):
                deadline = time.monotonic() + activation_wait
                while (generation := ime.activation()) is None and time.monotonic() < deadline:
                    time.sleep(0.005)
        except ImeHung:
            pass
    confirmed = focus.focused(timeout=0.2)
    stable = focused == confirmed and (focused.id is not None or not cfg.require_same_window)
    in_field = stable and generation is not None and ime.activation() == generation
    if editor is not None:
        if in_field:
            editor.preview = ImePreview(ime, generation)
        return editor
    if not stable:
        return ClipboardTarget(NotifyPreview("dictate"), window, focused.app_id)
    if in_field:
        return ImeTarget(ime, generation, window, focused.app_id,
                         allow_formatting=focused.app_id in cfg.multiline_apps)
    factory = WtypeTarget if cfg.inject == "wtype" else ClipboardTarget
    return factory(NotifyPreview("dictate"), window, focused.app_id)

"""Where a dictation's text goes, and how.

A target is bound at key-down, milliseconds after the press, and carries
everything delivery needs to put the text where it was meant to go and
nowhere else: the field's activation generation for the input method, the
window for the focus guard, the Emacs buffer pin. It shows provisional text
meanwhile — as preedit in the field when the application speaks the
input-method protocol, else in a notification — and lands the final text
once, reporting how that went so the daemon can copy or save the text
instead. The daemon never asks which kind it got.

Four kinds, chosen by ``bind()``:

- **Emacs**: the buffer of the selected window is pinned through
  ``emacsclient`` on a helper thread, and the text goes into that buffer by
  id with the gesture of its evil state, wherever focus is by then.
- **Input method**: the field active at key-down, by activation generation;
  the commit replaces the preedit in place. If focus left the window
  meanwhile, delivery waits for it to come back, within its budget, and
  reconciles what the application did with the provisional text.
- **wtype**: no field to bind; the text is typed into the focused window,
  once it is the one from key-down.
- **Clipboard**: copied, and said so.

Spacing is the daemon's: the text handed to ``land()`` already carries the
space it is owed, and the live preview's prefix is predicted at key-down."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import emacs as emacs_mod
from . import focus
from . import inject as inject_mod
from .config import DictationConfig
from .ime import ImeHung, InputMethod
from .notify import notify

log = logging.getLogger("voicekey.target")

LABEL = {"dictate": "dictation", "agent": "agent"}
PREVIEW_INTERVAL = 0.25  # seconds between notification updates
ACTIVATION_WAIT = 0.2  # seconds to wait for the focused field after binding
FOCUS_POLL = 0.25  # seconds between looks at the focused window while waiting for it to return
NO_SPACE_BEFORE = ",.;:!?)]}"  # a dictation starting like this joins the text


def spaced(prefix: str, text: str) -> str:
    """PREFIX is the space owed between the existing text and this
    dictation; it is dropped when the dictation itself starts with
    punctuation or whitespace."""
    if not text or text[0] in NO_SPACE_BEFORE or text[0].isspace():
        return text
    return prefix + text


@dataclass(frozen=True)
class Landing:
    """How landing went. Not landed and not uncertain: the daemon copies the
    text to the clipboard with REASON. Uncertain: the text may still appear
    later (a form Emacs will run when it is free, a request the compositor
    may still process), so it is saved and neither typed nor copied."""

    landed: bool = False
    reason: str = ""
    uncertain: bool = False
    summary: str = "voicekey: not typed"


LANDED = Landing(landed=True)


# --- previews ---------------------------------------------------------------

class NotifyPreview:
    """Live text in a replaceable notification."""

    name = "notification"

    def __init__(self, action: str) -> None:
        self.action = action
        self.closed = False
        self._last = 0.0

    def show(self, text: str) -> None:
        now = time.monotonic()
        if not self.closed and now - self._last >= PREVIEW_INTERVAL:
            self._last = now
            notify(f"● {LABEL[self.action]}", text, ms=60000, channel=self.action)

    def clear(self) -> None:
        self.closed = True  # the next status notification replaces it


class ImePreview:
    """Live text as preedit in the field active at key-down; commit replaces
    it in place. Both carry that field's activation generation, so nothing
    reaches a field that gained focus later, and the preview carries the
    spacing prefix predicted at key-down, so nothing jumps at commit."""

    name = "in-field"

    def __init__(self, ime: InputMethod, generation: int, prefix: str = "") -> None:
        self.ime = ime
        self.generation = generation
        self.prefix = prefix
        self.closed = False  # after clear/commit, no partial may reappear

    def show(self, text: str) -> None:
        if not self.closed:
            self.ime.preedit(spaced(self.prefix, text), self.generation)

    def clear(self) -> None:
        self.closed = True
        self.ime.preedit("", self.generation)

    def commit(self, text: str) -> bool:
        self.closed = True
        return self.ime.commit(text, self.generation)

    def replace(self, before: int, text: str) -> bool:
        """Commit in place of BEFORE bytes of real text before the cursor."""
        self.closed = True
        return self.ime.replace(before, text, self.generation)


# --- the focus guard --------------------------------------------------------

class Window:
    """The window bound at key-down, verified through the compositor when
    the config asks for it; without a verifiable window there is nothing
    to wait for."""

    def __init__(self, window_id, verify: bool) -> None:
        self.id = window_id
        self.verify = verify

    @property
    def verifiable(self) -> bool:
        return self.verify and self.id is not None

    def focused(self) -> bool:
        if not self.verify:
            return True
        return self.id is not None and focus.window_id() == self.id

    def await_(self, ready, budget: float):
        """Poll READY until it returns something, for up to BUDGET seconds."""
        if not self.verifiable:
            return None
        deadline = time.monotonic() + budget
        log.info("the window is not focused; waiting up to %.0fs for it", budget)
        notify("⏳ Waiting for focus", "the text lands once its window is focused again",
               ms=int(budget * 1000), channel="dictate")
        while True:
            result = ready()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                return None
            time.sleep(FOCUS_POLL)

    def back(self, budget: float) -> bool:
        """Focused now, or within BUDGET seconds."""
        return self.focused() or bool(self.await_(lambda: focus.window_id() == self.id or None, budget))


# --- targets ----------------------------------------------------------------

class Target:
    """Bound at key-down; shows provisional text; lands the final text once."""

    kind = ""

    def __init__(self, preview, window: Window, app_id: str | None) -> None:
        self.preview = preview
        self.window = window
        self.app_id = app_id

    @property
    def window_id(self):
        return self.window.id

    @property
    def prefix(self) -> str:
        return getattr(self.preview, "prefix", "")

    @prefix.setter
    def prefix(self, value: str) -> None:
        if isinstance(self.preview, ImePreview):
            self.preview.prefix = value

    def show(self, text: str) -> None:
        self.preview.show(text)

    def clear(self) -> None:
        self.preview.clear()

    def before(self, wait: float) -> str | None:
        """The character before the cursor, "" at the start of the text,
        None when unknown; WAIT is for targets that must ask."""
        return None

    def land(self, text: str, budget: float) -> Landing:
        raise NotImplementedError


class ImeTarget(Target):
    kind = "input method"

    def __init__(self, ime: InputMethod, generation: int, window: Window,
                 app_id: str | None) -> None:
        super().__init__(ImePreview(ime, generation), window, app_id)
        self.ime = ime
        self._before: str | None = None
        self._before_read = False

    def before(self, wait: float = 0.0) -> str | None:
        # Read once, at key-down, after the daemon has taken its spacing
        # mark; the report belongs to that activation.
        if not self._before_read:
            self._before_read = True
            self._before = self.ime.before_cursor()
        return self._before

    def land(self, text: str, budget: float) -> Landing:
        try:
            committed = self._commit(text, budget)
        except ImeHung as exc:
            # The commit may still land when the compositor recovers, so
            # neither type nor copy it: keep it where nothing can duplicate.
            return Landing(uncertain=True, summary="voicekey: input method hung",
                           reason=f"{exc}; the text may still appear when the compositor recovers")
        except _ProvisionalKept:
            return Landing(reason="the application kept the live text elsewhere; "
                           "the final text is copied instead of typed on top")
        if committed:
            log.info("committed in place")
            return LANDED
        self.preview.clear()
        return Landing(reason="the field changed; copied instead of typing")

    def _commit(self, text: str, budget: float) -> bool:
        """Commit into the field bound at key-down. If focus left its window
        meanwhile, the field was deactivated and this commit would be
        refused; so wait, within the budget, for the window to be focused
        and the field active again, and commit into that activation — after
        finding out what the application did with the provisional text it
        was showing. Kept as real text right before the cursor (Chromium
        does this), it is replaced; kept somewhere else, nothing can replace
        it and the final text is copied instead; dropped, which is what GTK
        and every terminal do, the final text is committed as usual. False
        when the field is not back within the budget."""
        if self.window.focused() and self.preview.commit(text):
            return True
        generation = self.window.await_(self._reactivated, budget)
        if generation is None:
            return False
        self.preview.generation = generation
        shown = self.ime.left_showing()
        surrounding = self.ime.surrounding_text()
        if shown and surrounding is not None:
            before, after = surrounding
            if before.endswith(shown):
                log.info("the application kept the provisional text; replacing it")
                return self.preview.replace(len(shown.encode()), text)
            if shown in before or shown in after:
                raise _ProvisionalKept()
        return self.preview.commit(text)

    def _reactivated(self) -> int | None:
        """The field's fresh activation, once its window is focused again."""
        generation = self.ime.activation()
        if generation is None or generation == self.preview.generation:
            return None
        return generation if focus.window_id() == self.window.id else None


class _ProvisionalKept(Exception):
    """The application turned the provisional text into real text, and it is
    not at the cursor now: nothing can replace it, so the final text must
    not be typed on top."""


class EmacsTarget(Target):
    """Into the buffer pinned at key-down, through Emacs itself and with the
    gesture of its current state — wherever focus went since: Emacs refuses
    only when the buffer is gone, read-only, mid-operator or blockwise
    selected. The preview is still the field's preedit (Emacs pgtk speaks
    the input-method protocol) or a notification."""

    kind = "emacs"

    def __init__(self, preview, window: Window, app_id: str | None,
                 pinning: emacs_mod.PendingPin | None = None) -> None:
        super().__init__(preview, window, app_id)
        # The buffer itself, before focus can move, on a helper thread so a
        # busy Emacs never holds up the keyboard.
        self.pinning = pinning or emacs_mod.PendingPin()

    def before(self, wait: float = 0.0) -> str | None:
        """The character before point, exact, since Emacs tells the input
        method nothing; when Emacs has answered within WAIT."""
        return self.pinning.before(wait)

    def land(self, text: str, budget: float) -> Landing:
        self.preview.clear()
        try:
            emacs_mod.insert(text, self.pinning.id, timeout=budget)
        except emacs_mod.EmacsTimeout as exc:
            # The form still runs when Emacs is free again, so neither type
            # nor copy it: keep it where nothing can duplicate.
            return Landing(uncertain=True, summary="voicekey: Emacs did not answer",
                           reason=f"{exc}; the text may still appear when Emacs is free")
        except emacs_mod.EmacsError as exc:
            return Landing(reason=f"Emacs: {exc}; copied instead")
        log.info("inserted via emacsclient")
        return LANDED


class WtypeTarget(Target):
    kind = "wtype"

    def land(self, text: str, budget: float) -> Landing:
        if not self.window.back(budget):
            return Landing(reason="focus changed; copied instead of typing")
        try:
            inject_mod.type_text(text)
        except Exception as exc:
            return Landing(reason=f"typing failed ({exc}); copied instead")
        log.info("typed via wtype")
        return LANDED


class ClipboardTarget(Target):
    kind = "clipboard"

    def land(self, text: str, budget: float) -> Landing:
        return Landing(summary="📋 Copied")


# --- binding ----------------------------------------------------------------

def bind(ime: InputMethod | None, cfg: DictationConfig, landing: bool) -> Target:
    """The target for a dictation starting now: the field first,
    milliseconds after key-down, then the window and the application.
    LANDING says a previous dictation is still on its way."""
    generation = None
    if ime is not None:
        try:
            generation = _bind_field(ime, landing)
        except ImeHung as exc:
            log.error("%s", exc)
    focused = focus.focused()
    window = Window(focused.id, cfg.require_same_window)
    # In-field text needs a real focused window: a lock screen or launcher
    # can activate the input method too, and text must never land there.
    in_field = generation is not None and (focused.id is not None or not cfg.require_same_window)
    if focused.app_id == "emacs":
        preview = ImePreview(ime, generation) if in_field else NotifyPreview("dictate")
        return EmacsTarget(preview, window, focused.app_id)
    if in_field:
        return ImeTarget(ime, generation, window, focused.app_id)
    if cfg.inject == "wtype":
        return WtypeTarget(NotifyPreview("dictate"), window, focused.app_id)
    return ClipboardTarget(NotifyPreview("dictate"), window, focused.app_id)


def _bind_field(ime: InputMethod, landing: bool) -> int | None:
    """Generation of the field active right now, or None.

    Binds afresh — the compositor drops our binding whenever another
    client touches the input method — unless a previous dictation is
    still landing: a rebind would cancel its ticket, and a binding that
    was live a second ago is trusted instead."""
    if not landing and not ime.rebind():
        return None
    deadline = time.monotonic() + ACTIVATION_WAIT
    while (generation := ime.activation()) is None and time.monotonic() < deadline:
        time.sleep(0.005)
    return generation

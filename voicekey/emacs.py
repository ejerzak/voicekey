"""Deliver dictation to Emacs through Emacs itself, not through keystrokes.

Committed text reaches an evil-mode buffer as keystrokes, so in normal or
visual state the words become commands. Instead the daemon asks Emacs, via
emacsclient, to insert the text with the typing gesture of the current
state: at point in insert state, after the cursor in normal state (``a``),
in place of the selection in visual state (``c``), and to the process in a
terminal buffer. The state is left as it was found: normal and visual
state return to normal afterwards, as Escape would. Read-only buffers,
blockwise selections and a pending operator are refused, and the caller
copies the text instead.

The buffer is pinned at key-down: one round trip registers the buffer of
the selected window under a fresh id in a voicekey-owned alist and reports
the character before point. Delivery inserts into that buffer by id, so
whatever moved focus in the meantime — an agent's emacsclient evaluation, a
dialog, another frame — cannot redirect the text. Evil state, point and
read-only-ness are buffer-local, so the buffer alone is the handle: the
text follows point within it, and never follows focus out of it.

The pin round trip runs on a helper thread (``PendingPin``): the id is
chosen here and known at once, so key handling never waits on Emacs; the
character before point arrives when Emacs answers, and delivery waits for
it only as long as its own budget allows."""

from __future__ import annotations

import ast
import logging
import secrets
import subprocess
import threading
from dataclasses import dataclass

log = logging.getLogger("voicekey.emacs")

TIMEOUT = 5.0  # seconds a healthy Emacs is given to answer
PINS = 16  # pins kept; a dictation that never lands does not leak a buffer

# Registers the buffer of the selected window under ID and returns the
# character before point (before the selection in visual state) as a Lisp
# string, "" at the start of the buffer.
PIN = """
(progn
  (defvar voicekey--pins nil "Buffers dictations are bound for: ((id . buffer) ...).")
  (with-current-buffer (window-buffer (selected-window))
    (setq voicekey--pins (cons (cons %(id)s (current-buffer))
                               (seq-take voicekey--pins %(keep)d)))
    (let ((pos (if (and (bound-and-true-p evil-local-mode) (eq evil-state 'visual))
                   evil-visual-beginning
                 (point))))
      (if (> pos (point-min)) (string (char-before pos)) ""))))
"""

# Inserts into the buffer pinned as ID and forgets the pin. A window showing
# the buffer is selected for the duration so its own point (the cursor the
# user sees) is the one used and advanced; the selection is restored after.
# Normal and visual state end back in normal state, the way Escape leaves
# them after `a text` or `c text`; insert state stays insert.
INSERT = """
(let* ((text %(text)s)
       (buffer (cdr (assoc %(id)s (bound-and-true-p voicekey--pins)))))
  (when (boundp 'voicekey--pins)
    (setq voicekey--pins (assoc-delete-all %(id)s voicekey--pins)))
  (unless buffer (error "no pinned buffer"))
  (unless (buffer-live-p buffer) (error "the buffer is gone"))
  (with-selected-window (or (get-buffer-window buffer t) (selected-window))
    (with-current-buffer buffer
      (let ((state (if (bound-and-true-p evil-local-mode) evil-state 'none)))
        (undo-boundary)
        (cond
         ((eq major-mode 'vterm-mode) (vterm-send-string text))
         ((eq major-mode 'term-mode) (term-send-raw-string text))
         (buffer-read-only (error "buffer is read-only"))
         ((eq state 'operator) (error "an operator is pending"))
         ((eq state 'visual)
          (when (eq (evil-visual-type) 'block) (error "blockwise selection"))
          (evil-change evil-visual-beginning evil-visual-end (evil-visual-type))
          (insert text)
          (evil-normal-state))
         ((eq state 'normal) (evil-append 1) (insert text) (evil-normal-state))
         (t (insert text)))
        "ok"))))
"""


class EmacsError(Exception):
    """Emacs refused or could not be reached; the message says which."""


class EmacsTimeout(EmacsError):
    """Emacs did not answer in time. The form was delivered, so it still
    runs once Emacs is free again — the caller must not send it twice."""


@dataclass(frozen=True)
class Pin:
    id: str
    before: str | None  # character before point at key-down; None if unknown


def _lisp_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _eval(form: str, timeout: float = TIMEOUT) -> str:
    """Evaluate FORM in the running Emacs; return the printed result."""
    try:
        result = subprocess.run(["emacsclient", "-e", form], capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise EmacsTimeout(f"Emacs did not answer within {timeout:.0f}s")
    except OSError as exc:
        raise EmacsError(f"emacsclient failed: {exc}")
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or output.startswith("*ERROR*"):
        raise EmacsError(output.removeprefix("*ERROR*:").strip() or "emacsclient failed")
    return output


def pin(pin_id: str | None = None) -> Pin:
    """Pin the buffer of the selected window for a later insert().

    The id is fresh whether or not Emacs answered: a form that timed out
    still runs when Emacs unblocks, so the pin may well exist by delivery."""
    pin_id = pin_id or secrets.token_hex(8)
    form = PIN % {"id": _lisp_string(pin_id), "keep": PINS - 1}
    try:
        value = ast.literal_eval(_eval(form))
    except (EmacsError, ValueError, SyntaxError) as exc:
        log.info("could not pin the Emacs buffer: %s", exc)
        return Pin(pin_id, None)
    return Pin(pin_id, value if isinstance(value, str) else None)


class PendingPin:
    """pin() on a helper thread. The id is known at once; ``before(wait)``
    gives the character before point once Emacs has answered, or None if
    it has not answered within WAIT seconds (the caller then falls back to
    what it knows about spacing). A blocked Emacs costs the key-down
    nothing: the form is delivered and runs when Emacs is free again."""

    def __init__(self) -> None:
        self.id = secrets.token_hex(8)
        self._before: str | None = None
        self._done = threading.Event()
        threading.Thread(target=self._run, name="emacs-pin", daemon=True).start()

    def _run(self) -> None:
        try:
            self._before = pin(self.id).before
        finally:
            self._done.set()

    def before(self, wait: float) -> str | None:
        self._done.wait(max(0.0, wait))
        return self._before


def insert(text: str, pin_id: str, timeout: float = TIMEOUT) -> None:
    """Insert TEXT into the buffer pinned as PIN_ID with the gesture of its
    current state; raises EmacsError, or EmacsTimeout after TIMEOUT."""
    _eval(INSERT % {"id": _lisp_string(pin_id), "text": _lisp_string(text)}, timeout)

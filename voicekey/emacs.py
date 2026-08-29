"""Deliver dictation to Emacs through Emacs itself, not through keystrokes.

Committed text reaches an evil-mode buffer as keystrokes, so in normal or
visual state the words become commands. Instead the daemon asks Emacs, via
emacsclient, to insert the text with the typing gesture of the current
state: at point in insert state, after the cursor in normal state (``a``),
in place of the selection in visual state (``c``), and to the process in a
terminal buffer. Read-only buffers, blockwise selections and a pending
operator are refused, and the caller copies the text instead."""

from __future__ import annotations

import ast
import logging
import subprocess

log = logging.getLogger("voicekey.emacs")

BUFFER = "(window-buffer (selected-window))"

# Returns the character before point (before the selection in visual state)
# as a Lisp string, "" at the start of the buffer.
PROBE = """
(with-current-buffer %(buffer)s
  (let ((pos (if (and (bound-and-true-p evil-local-mode) (eq evil-state 'visual))
                 evil-visual-beginning
               (point))))
    (if (> pos (point-min)) (string (char-before pos)) "")))
"""

INSERT = """
(with-current-buffer %(buffer)s
  (let ((text %(text)s)
        (state (if (bound-and-true-p evil-local-mode) evil-state 'none)))
    (undo-boundary)
    (cond
     ((eq major-mode 'vterm-mode) (vterm-send-string text))
     ((eq major-mode 'term-mode) (term-send-raw-string text))
     (buffer-read-only (error "buffer is read-only"))
     ((eq state 'operator) (error "an operator is pending"))
     ((eq state 'visual)
      (when (eq (evil-visual-type) 'block) (error "blockwise selection"))
      (evil-change evil-visual-beginning evil-visual-end (evil-visual-type))
      (insert text))
     ((eq state 'normal) (evil-append 1) (insert text))
     (t (insert text)))
    "ok"))
"""


class EmacsError(Exception):
    """Emacs refused or could not be reached; the message says which."""


def _lisp_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _eval(form: str) -> str:
    """Evaluate FORM in the running Emacs; return the printed result."""
    try:
        result = subprocess.run(["emacsclient", "-e", form], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EmacsError(f"emacsclient failed: {exc}")
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or output.startswith("*ERROR*"):
        raise EmacsError(output.removeprefix("*ERROR*:").strip() or "emacsclient failed")
    return output


def before_cursor(buffer: str = BUFFER) -> str | None:
    """Character before point in the current Emacs buffer; None if unknown."""
    try:
        value = ast.literal_eval(_eval(PROBE % {"buffer": buffer}))
    except (EmacsError, ValueError, SyntaxError) as exc:
        log.info("could not ask Emacs about the cursor: %s", exc)
        return None
    return value if isinstance(value, str) else None


def insert(text: str, buffer: str = BUFFER) -> None:
    """Insert TEXT with the gesture of the current state; raises EmacsError."""
    _eval(INSERT % {"buffer": buffer, "text": _lisp_string(text)})

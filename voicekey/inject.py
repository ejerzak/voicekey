"""Deliver text without the input method: type it with wtype (niri implements
the virtual-keyboard protocol) or copy it to the clipboard with wl-copy."""

from __future__ import annotations

import subprocess
import tempfile


# A wl-copy that has not returned in a few seconds will not; the caller has
# a recovery file for that case and must not hold up later deliveries (on
# 2026-08-30 one hung copy made the next six dictations late). Typing a
# long transcript with wtype legitimately takes longer.
COPY_TIMEOUT = 3.0
TYPE_TIMEOUT = 15.0


class InjectError(Exception):
    pass


def _run(argv: list[str], text: str, timeout: float = TYPE_TIMEOUT) -> None:
    # wl-copy forks a background process to serve the clipboard, and that
    # process inherits stderr. Through a pipe, waiting for EOF would wait for
    # the clipboard to change hands; a file has no EOF to wait for, and the
    # foreground process has said all it will say by the time it exits.
    with tempfile.TemporaryFile() as errors:
        result = subprocess.run(argv, input=text.encode(), stdout=subprocess.DEVNULL,
                                stderr=errors, timeout=timeout)
        if result.returncode != 0:
            errors.seek(0)
            tail = errors.read().decode(errors="replace").strip()[-300:]
            raise InjectError(f"{argv[0]} failed (rc={result.returncode}): {tail or 'no stderr'}")


def type_text(text: str) -> None:
    _run(["wtype", "-"], text, TYPE_TIMEOUT)


def copy(text: str) -> None:
    _run(["wl-copy"], text, COPY_TIMEOUT)

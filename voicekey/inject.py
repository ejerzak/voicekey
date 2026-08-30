"""Deliver text without the input method: type it with wtype (niri implements
the virtual-keyboard protocol) or copy it to the clipboard with wl-copy."""

from __future__ import annotations

import subprocess
import tempfile


class InjectError(Exception):
    pass


def _run(argv: list[str], text: str) -> None:
    # wl-copy forks a background process to serve the clipboard, and that
    # process inherits stderr. Through a pipe, waiting for EOF would wait for
    # the clipboard to change hands; a file has no EOF to wait for, and the
    # foreground process has said all it will say by the time it exits.
    with tempfile.TemporaryFile() as errors:
        result = subprocess.run(argv, input=text.encode(), stdout=subprocess.DEVNULL,
                                stderr=errors, timeout=15)
        if result.returncode != 0:
            errors.seek(0)
            tail = errors.read().decode(errors="replace").strip()[-300:]
            raise InjectError(f"{argv[0]} failed (rc={result.returncode}): {tail or 'no stderr'}")


def type_text(text: str) -> None:
    _run(["wtype", "-"], text)


def copy(text: str) -> None:
    _run(["wl-copy"], text)

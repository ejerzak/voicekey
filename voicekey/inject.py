"""Deliver text without the input method: type it with wtype (niri implements
the virtual-keyboard protocol) or copy it to the clipboard with wl-copy."""

from __future__ import annotations

import subprocess


class InjectError(Exception):
    pass


def _run(argv: list[str], text: str) -> None:
    result = subprocess.run(argv, input=text.encode(), capture_output=True, timeout=15)
    if result.returncode != 0:
        tail = result.stderr.decode(errors="replace").strip()[-300:]
        raise InjectError(f"{argv[0]} failed (rc={result.returncode}): {tail or 'no stderr'}")


def type_text(text: str) -> None:
    _run(["wtype", "-"], text)


def copy(text: str) -> None:
    _run(["wl-copy"], text)

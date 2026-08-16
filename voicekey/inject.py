"""Type the transcript into whatever has focus.

Default: wtype - (niri implements the virtual-keyboard protocol). Fallback:
wl-copy + simulated ctrl+v paste — caveat: terminals want ctrl+shift+v, which
is why direct typing is the default."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("voicekey.inject")


class InjectError(Exception):
    pass


def _run(argv: list[str], input_text: str | None = None) -> None:
    res = subprocess.run(argv, input=input_text.encode() if input_text is not None else None,
                         capture_output=True, timeout=15)
    if res.returncode != 0:
        tail = res.stderr.decode(errors="replace").strip()[-300:]
        raise InjectError(f"{argv[0]} failed (rc={res.returncode}): {tail or 'no stderr'}")


def _wtype(text: str) -> None:
    _run(["wtype", "-"], input_text=text)


def copy(text: str) -> None:
    """Copy TEXT without generating a paste keypress."""
    _run(["wl-copy"], input_text=text)


def _clipboard_paste(text: str) -> None:
    copy(text)
    _run(["wtype", "-M", "ctrl", "v", "-m", "ctrl"])


def inject(text: str, mode: str) -> str:
    """Returns the method actually used ('wtype' or 'clipboard')."""
    if mode == "wtype":
        try:
            _wtype(text)
            return "wtype"
        except (InjectError, OSError, subprocess.TimeoutExpired) as e:
            log.warning("wtype failed (%s), falling back to clipboard paste", e)
    _clipboard_paste(text)
    return "clipboard"

"""Securely retain a transcript only when delivery fails."""

from __future__ import annotations

import os

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "voicekey",
)
LAST_RECOVERY = os.path.join(STATE_DIR, "last-recovery.txt")


def save(text: str) -> str:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(LAST_RECOVERY, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.write("\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return LAST_RECOVERY

"""Tell agents when a dictation is in flight.

From key-down until the transcript has landed, voicekey holds an exclusive
flock on ``$XDG_RUNTIME_DIR/voicekey/lock``. Agent hooks take the same lock
before touching the desktop — emacsclient, wtype, wl-copy, compositor
actions — so they wait for the dictation instead of stealing focus or the
clipboard from under it. The lock is advisory and dies with the process, so
a crashed daemon can never wedge an agent; and voicekey never waits for it,
so an agent can never delay a dictation."""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from collections.abc import Callable

log = logging.getLogger("voicekey.gate")


def default_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "voicekey", "lock")


class Gate:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_path()
        self._fd: int | None = None
        self._held = False
        self._lock = threading.Lock()

    def open(self) -> None:
        """Create the lock file; until then settle() does nothing."""
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)

    @property
    def held(self) -> bool:
        return self._held

    def settle(self, busy: Callable[[], bool]) -> None:
        """Hold the lock exactly while BUSY() says so. BUSY is read under the
        gate's own lock, so of two threads settling after their own state
        change the later one wins with the current state. A lock someone
        else holds is not waited for — it is tried again next time."""
        with self._lock:
            if self._fd is None:
                return
            wanted = busy()
            if wanted == self._held:
                return
            if not wanted:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._held = False
                return
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.info("the dictation lock is held elsewhere; agents are not gated")
                return
            self._held = True

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)  # releases the lock too
                self._fd = None
                self._held = False

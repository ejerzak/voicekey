"""Desktop notifications. Every user-visible state change and every error path
goes through here — silent failure is the worst outcome. notify() never raises
and never blocks the caller: one worker thread sends them, in order."""

from __future__ import annotations

import logging
import queue
import subprocess
import threading

log = logging.getLogger("voicekey.notify")

# Independent replace ids keep an agent status update from overwriting a
# simultaneous dictation status update. Errors skip replacement and persist.
_REPLACE_IDS = {
    "dictate": "91021",
    "agent": "91022",
    "system": "91023",
}
_pending: queue.SimpleQueue = queue.SimpleQueue()
_worker: threading.Thread | None = None
_lock = threading.Lock()


def _coalesce(items: list[tuple[str | None, list[str]]]) -> list[list[str]]:
    """Errors are all sent; of replaceable notifications (channel set) only
    the newest per channel survives a backlog — a stalled notify-send must
    not replay every live-preview update afterwards."""
    errors = []
    newest: dict[str, list[str]] = {}
    for channel, cmd in items:
        if channel is None:
            errors.append(cmd)
        else:
            newest.pop(channel, None)
            newest[channel] = cmd
    return errors + list(newest.values())


def _send() -> None:
    while True:
        batch = [_pending.get()]
        while True:
            try:
                batch.append(_pending.get_nowait())
            except queue.Empty:
                break
        for cmd in _coalesce(batch):
            try:
                subprocess.run(cmd, timeout=5, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                log.warning("notify-send failed: %s", exc)


def notify(summary: str, body: str = "", *, error: bool = False,
           ms: int = 6000, channel: str = "system") -> None:
    global _worker
    cmd = ["notify-send", "-a", "voicekey"]
    key: str | None = None
    if error:
        cmd += ["-u", "critical"]
        log.error("%s: %s", summary, body)
    else:
        key = channel if channel in _REPLACE_IDS else "system"
        cmd += ["-r", _REPLACE_IDS[key], "-t", str(ms)]
    cmd.append(summary)
    if body:
        cmd.append(body)
    with _lock:
        if _worker is None:
            _worker = threading.Thread(target=_send, name="notify", daemon=True)
            _worker.start()
    _pending.put((key, cmd))

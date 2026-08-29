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


def _send() -> None:
    while True:
        cmd = _pending.get()
        try:
            subprocess.run(cmd, timeout=5, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            log.warning("notify-send failed: %s", exc)


def notify(summary: str, body: str = "", *, error: bool = False,
           ms: int = 6000, channel: str = "system") -> None:
    global _worker
    cmd = ["notify-send", "-a", "voicekey"]
    if error:
        cmd += ["-u", "critical"]
        log.error("%s: %s", summary, body)
    else:
        replace_id = _REPLACE_IDS.get(channel, _REPLACE_IDS["system"])
        cmd += ["-r", replace_id, "-t", str(ms)]
    cmd.append(summary)
    if body:
        cmd.append(body)
    with _lock:
        if _worker is None:
            _worker = threading.Thread(target=_send, name="notify", daemon=True)
            _worker.start()
    _pending.put(cmd)

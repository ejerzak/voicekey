"""Desktop notifications. Every user-visible state change and every error path
goes through here — silent failure is the worst outcome. notify() itself must
never raise."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("voicekey.notify")

# Independent replace ids keep an agent status update from overwriting a
# simultaneous dictation status update. Errors skip replacement and persist.
_REPLACE_IDS = {
    "dictate": "91021",
    "agent": "91022",
    "system": "91023",
}


def notify(summary: str, body: str = "", *, error: bool = False,
           ms: int = 6000, channel: str = "system") -> None:
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
    try:
        subprocess.run(cmd, timeout=5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning("notify-send failed: %s", e)

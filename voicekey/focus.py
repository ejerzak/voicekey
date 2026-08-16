"""Best-effort focused-window tracking through niri IPC."""

from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger("voicekey.focus")


def window_id() -> int | None:
    """Return niri's focused window id, or None when it cannot be queried."""
    try:
        result = subprocess.run(
            ["niri", "msg", "--json", "focused-window"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("niri returned invalid focused-window JSON")
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None

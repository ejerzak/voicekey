"""Best-effort focused-window tracking through niri IPC."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger("voicekey.focus")


@dataclass(frozen=True)
class Focus:
    id: int | None = None
    app_id: str | None = None


def focused() -> Focus:
    """niri's focused window, or an empty Focus when it cannot be queried."""
    try:
        result = subprocess.run(
            ["niri", "msg", "--json", "focused-window"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Focus()
    if result.returncode != 0:
        return Focus()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("niri returned invalid focused-window JSON")
        return Focus()
    if not isinstance(data, dict):
        return Focus()
    window_id = data.get("id")
    app_id = data.get("app_id")
    return Focus(
        window_id if isinstance(window_id, int) and not isinstance(window_id, bool) else None,
        app_id if isinstance(app_id, str) else None,
    )


def window_id() -> int | None:
    return focused().id

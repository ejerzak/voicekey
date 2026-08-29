"""Best-effort focused-window tracking through the compositor's IPC.

Implemented for niri, sway and Hyprland (detected by their socket variables
or XDG_CURRENT_DESKTOP). Elsewhere focus is unverifiable: set
``require_same_window = false`` to dictate without the window guard."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass

log = logging.getLogger("voicekey.focus")


@dataclass(frozen=True)
class Focus:
    id: int | str | None = None
    app_id: str | None = None


def focused() -> Focus:
    """The focused window, or an empty Focus when it cannot be queried."""
    compositor = _compositor()
    if compositor == "niri":
        data = _json(["niri", "msg", "--json", "focused-window"])
        return _focus(data.get("id"), data.get("app_id")) if isinstance(data, dict) else Focus()
    if compositor == "sway":
        node = _sway_focused(_json(["swaymsg", "-t", "get_tree"]))
        if node is None:
            return Focus()
        app_id = node.get("app_id") or (node.get("window_properties") or {}).get("class")
        return _focus(node.get("id"), app_id)
    if compositor == "hyprland":
        data = _json(["hyprctl", "-j", "activewindow"])
        return _focus(data.get("address"), data.get("class")) if isinstance(data, dict) else Focus()
    return Focus()


def window_id() -> int | str | None:
    return focused().id


def _compositor() -> str | None:
    for variable, name in (("NIRI_SOCKET", "niri"), ("SWAYSOCK", "sway"),
                           ("HYPRLAND_INSTANCE_SIGNATURE", "hyprland")):
        if os.environ.get(variable):
            return name
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    return next((name for name in ("niri", "sway", "hyprland") if name in desktop), None)


def _json(argv: list[str]):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("%s returned invalid JSON", argv[0])
        return None


def _sway_focused(node):
    if not isinstance(node, dict):
        return None
    if node.get("focused") is True and node.get("type") in ("con", "floating_con"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = _sway_focused(child)
        if found is not None:
            return found
    return None


def _focus(window_id, app_id) -> Focus:
    valid_id = (isinstance(window_id, int) and not isinstance(window_id, bool)) or (
        isinstance(window_id, str) and window_id)
    return Focus(window_id if valid_id else None, app_id if isinstance(app_id, str) else None)

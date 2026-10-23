"""Machine-local selection of a configured agent profile."""
import json
import logging
import os
from pathlib import Path
import tempfile

log = logging.getLogger("voicekey.agent_profiles")


def selection_path():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "voicekey/agent-profile.json"


def load_selection(profiles, default, path=None):
    path = Path(path) if path is not None else selection_path()
    try:
        data = json.loads(path.read_text())
        selected = data.get("profile") if isinstance(data, dict) else None
        if isinstance(selected, str) and selected in profiles:
            return selected
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("could not read agent selection: %s", exc)
    return default


def save_selection(profile_id, path=None):
    path = Path(path) if path is not None else selection_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".agent-profile-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"profile": profile_id}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

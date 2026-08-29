#!/bin/bash
# install.sh — voicekey: venv, dependencies, models, systemd user unit. Idempotent.
#
# System prerequisites (Fedora names): pipewire-utils (pw-record), wtype,
# wl-clipboard, libnotify (notify-send), gcc (evdev builds from source), uv.
# Config: ~/.config/voicekey/config.toml — created from config.example.toml if
# absent; symlink your own (per-machine) file there instead if you keep one.
# The venv lives outside the repo so binary wheels never travel with it.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HOME/.local/share/voicekey/venv"
CONFIG="$HOME/.config/voicekey/config.toml"
UNIT="$HOME/.config/systemd/user/voicekey.service"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "  Would create $VENV (python 3.12 via uv), install $HERE, download models,"
    echo "  link $UNIT -> $HERE/voicekey.service, enable voicekey.service and run --check"
    exit 0
fi

command -v uv >/dev/null || {
    echo "  uv not found — install it (dnf install uv, or https://astral.sh/uv)"; exit 1; }

if [[ ! -e "$CONFIG" ]]; then
    echo "  CONFIG: $CONFIG <- config.example.toml (edit the keys if needed)"
    mkdir -p "$(dirname "$CONFIG")"
    cp "$HERE/config.example.toml" "$CONFIG"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "  VENV: $VENV"
    uv python install 3.12
    uv venv --python 3.12 "$VENV"
fi

extra=""
if "$VENV/bin/python" - "$CONFIG" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    sys.exit(0 if tomllib.load(f).get("backend", {}).get("type") == "faster-whisper" else 1)
PY
then extra="[whisper]"; fi
echo "  INSTALL: voicekey$extra"
uv pip install --quiet --python "$VENV/bin/python" -e "$HERE$extra"

"$VENV/bin/python" -m voicekey --download

echo "  UNIT: $UNIT -> $HERE/voicekey.service"
mkdir -p "$(dirname "$UNIT")"
ln -sfnT "$HERE/voicekey.service" "$UNIT"
systemctl --user daemon-reload
systemctl --user enable voicekey.service

set +e
"$VENV/bin/python" -m voicekey --check
status=$?
set -e
case "$status" in
    0|3) ;;
    2) echo "  NOTE: no keyboard is readable. Run:  sudo usermod -aG input $USER"
       echo "        then log out and back in; the service starts with the next session." ;;
    *) echo "  ERROR: voicekey --check failed"; exit "$status" ;;
esac
if [[ "$status" != 2 ]] && systemctl --user is-active --quiet graphical-session.target; then
    systemctl --user restart voicekey.service
    echo "  voicekey.service restarted"
fi

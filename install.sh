#!/bin/bash
# install.sh — voicekey: venv, dependencies, models, systemd unit, DMS widget. Idempotent.
#
# System prerequisites (Fedora names): pipewire-utils (pw-record), wtype,
# wl-clipboard, libnotify (notify-send), gcc (evdev builds from source), uv.
# The optional polish pass needs llama.cpp, which --download fetches.
# Config: ~/.config/voicekey/config.toml — created from config.example.toml if
# absent; symlink your own (per-machine) file there instead if you keep one.
# The venv lives outside the repo so binary wheels never travel with it.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HOME/.local/share/voicekey/venv"
CONFIG="$HOME/.config/voicekey/config.toml"
UNIT="$HOME/.config/systemd/user/voicekey.service"
PLUGINS="${XDG_CONFIG_HOME:-$HOME/.config}/dms/plugins"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/dev.voicekey.Hermes.desktop"
HERMES_ICON="${HERMES_HOME:-$HOME/.hermes}/hermes-agent/apps/desktop/assets/icon-dark.png"

# The agent terminal's desktop entry, named for its app ID (TERMINAL_APP_ID in
# voicekey/agent.py): docks show Hermes as its own app, so it can be pinned,
# and launching it is an agent-key tap. Written rather than linked because
# Exec needs this venv's absolute path.
install_desktop_entry() {
    local icon=utilities-terminal
    if [[ -f "$HERMES_ICON" ]]; then icon="$HERMES_ICON"; fi
    mkdir -p "$(dirname "$DESKTOP")"
    cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Voicekey Hermes
Comment=Open or raise the Hermes terminal, like a tap of the agent key
Exec="$VENV/bin/python" -m voicekey --control agent-tap
Icon=$icon
StartupWMClass=dev.voicekey.Hermes
Terminal=false
EOF
    echo "  DESKTOP: $DESKTOP"
}

# Link contrib/dms/NAME into DMS's plugins; refuse to replace anything else.
link_plugin() {
    local link="$PLUGINS/$1" source="$HERE/contrib/dms/$1"
    if [[ -e "$link" || -L "$link" ]]; then
        if [[ ! -L "$link" || "$(readlink -f "$link")" != "$source" ]]; then
            echo "  DMS: leaving existing $link untouched; see docs/reference.md for manual setup."
            return 1
        fi
    elif ! mkdir -p "$PLUGINS" || ! ln -s "$source" "$link"; then
        echo "  DMS: could not link $link; see docs/reference.md for manual setup."
        return 1
    fi
    echo "  DMS: $link -> $source"
}

# Enable plugin ID once a scan has found it; succeed when it is loaded.
enable_plugin() {
    local reply attempt
    # IPC can succeed as a command while reporting an application-level error.
    for ((attempt = 0; attempt < 10; attempt++)); do
        if ! reply=$(timeout 3s dms ipc call plugins status "$1" 2>/dev/null); then
            return 1
        fi
        case "$reply" in
            loaded) return 0 ;;
            disabled)
                if reply=$(timeout 3s dms ipc call plugins enable "$1" 2>/dev/null) &&
                        [[ "$reply" == *PLUGIN_ENABLE_SUCCESS:* ]]; then
                    return 0
                fi
                return 1 ;;
        esac
        sleep 0.5
    done
    return 1
}

# The bar widget, and the hook that makes clicking the agent window in the
# dock an agent-key tap.
install_plugins() {
    if ! command -v dms >/dev/null; then
        echo "  DMS: not installed; skipping the optional plugins (rerun install.sh after installing DMS)."
        return
    fi
    local linked=() pending=() id reply
    if link_plugin Voicekey; then linked+=(voicekey); fi
    if link_plugin HermesSidebar; then linked+=(hermesSidebar); fi
    if ((${#linked[@]} == 0)); then
        return
    fi
    if reply=$(timeout 3s dms ipc call plugin-scan scan 2>/dev/null) && [[ "$reply" == *SCAN_TRIGGERED:* ]]; then
        for id in "${linked[@]}"; do
            if enable_plugin "$id"; then
                echo "  DMS: $id enabled."
            else
                pending+=("$id")
            fi
        done
    else
        pending=("${linked[@]}")
    fi
    if ((${#pending[@]})); then
        echo "  DMS: plugins linked, but automatic enabling was unavailable. With DMS running, use:"
        echo "       dms ipc call plugin-scan scan"
        echo "       # Once scanning finishes:"
        for id in "${pending[@]}"; do
            echo "       dms ipc call plugins enable $id"
        done
    fi
    if [[ " ${linked[*]} " == *" voicekey "* ]]; then
        echo "  DMS: add Voicekey to the bar in DMS Settings → Bar widgets."
    fi
}

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "  Would create $VENV (python 3.12 via uv), install $HERE, download models,"
    echo "  link $UNIT -> $HERE/voicekey.service, enable voicekey.service and run --check,"
    echo "  and write the Hermes terminal's desktop entry $DESKTOP"
    echo "  If dms is installed, link its plugins into $PLUGINS and enable them when DMS is running."
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
install_desktop_entry
install_plugins

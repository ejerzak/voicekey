# voicekey — global hold-to-talk voice input

Hold **F9**, speak, release → the transcript is typed into the focused field.
If transcription takes too long or niri reports that focus moved to another
window, voicekey copies the transcript instead of typing it somewhere wrong.

Hold **F10**, speak, release → the transcript is sent to a persistent Hermes
TUI. This path is independent of Emacs and of the directory containing the
focused application.

On the first F10 dispatch, voicekey:

1. starts a dedicated tmux server in a supervised systemd user unit;
2. starts `hermes --tui` in a named tmux session and a neutral working directory;
3. opens a Ghostty window attached to that session; and
4. waits for Hermes's composer to be idle and empty before submitting the prompt.

Later F10 dispatches reuse the same Hermes process and conversation. If its
Ghostty window is open, voicekey does not open another. If the window was
closed, voicekey opens a new one and reattaches it. Ghostty is only a tmux
client, so closing the window does not stop Hermes. Restarting voicekey does
not stop Hermes either.

The shared path is evdev → `pw-record` → one warm transcription backend. It
then splits: dictation is delivered immediately, while agent prompts go through
a bounded queue and separate worker. A busy Hermes therefore cannot delay
later dictation.

## Install

Hermes must already be installed as `hermes`. The default agent UI also needs
tmux, systemd-run, and Ghostty. Fedora's package list installs tmux and Ghostty;
on Debian/Ubuntu, install Ghostty separately if the distribution does not
package it.

```sh
install/install.sh 02-dnf       # or 02-apt
install/install.sh 04-symlinks  # systemd user unit and niri config
install/install.sh 07-voicekey  # venv, Python deps, model download, checks
sudo usermod -aG input "$USER"  # one-time; see security note below
# log out and back in
install/install.sh 08-services
```

Diagnostics:

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --check
journalctl --user -u voicekey
tmux -L voicekey-hermes has-session -t voicekey-hermes
systemctl --user status voicekey-voicekey-hermes-tmux.service
```

The last two commands become meaningful after the first F10 dispatch. To open
the persistent Hermes session yourself without starting another Hermes:

```sh
tmux -L voicekey-hermes attach-session -t voicekey-hermes
```

Step 07 is the only voicekey stage that downloads model files. The daemon and
`--check` load faster-whisper strictly from the local Hugging Face cache, so
they fail quickly with a repair instruction instead of waiting on the network.

`--check` exits 0 when fully ready, 2 when software/backend checks pass but no
keyboard is readable, and 1 for a configuration, dependency, or backend
failure.

## Configuration

`~/.config/voicekey/config.toml` is a symlink to
`apps/voicekey/hosts/<hostname>.toml`. The machine hostnames match their
Tailscale names: `desktop` for the desktop and `laptop` for the
laptop. Step 07 creates the symlink, falling back to a copy of
`config.example.toml` on a new host.

Backends are selected by `[backend].type`:

| backend | machine | notes |
|---|---|---|
| `faster-whisper` | desktop | large-v3-turbo on CUDA; cuBLAS/cuDNN come from pip NVIDIA wheels |
| `parakeet` | laptop | CPU via sherpa-onnx; still untested; set `model_dir` |
| `remote` | optional | configuration seam only; deliberately unimplemented |

`[agent].working_directory` is deliberately neutral. Hermes retains its own
session and memory, but a general voice command does not accidentally inherit
the focused editor's repository. `[agent].target` is the adapter seam for
future agents; the implemented target is currently only `hermes`.

`ready_timeout` controls how long an F10 transcript waits while Hermes is busy,
showing a modal, or contains a manually typed draft. Later F10 transcripts stay
behind it in the daemon's agent queue. On timeout, delivery fails closed and
the transcript is written to the recovery file.

## Behavior and safety

- Taps shorter than `min_seconds` are discarded.
- Recordings longer than `max_seconds` are aborted.
- The transcription queue is bounded; overload discards the newest recording.
- Dictation older than `dictation.max_delay_seconds` is copied, not typed.
- With `dictation.require_same_window = true`, a niri window change also causes
  copy-without-paste.
- `wtype` failure falls back to `wl-copy` plus simulated Ctrl+V. Terminal paste
  conventions differ, so direct `wtype` remains the default.
- F9/F10 are consumed by no-op niri bindings but remain visible to the raw
  evdev listener, preventing focused applications from also acting on them.
- Agent prompts are pasted only when the Hermes status is `ready` and its
  composer is empty. Voicekey will not type into an approval dialog or overwrite
  a textual draft.
- Hermes is launched as its normal interactive TUI. Voicekey never passes its
  oneshot/automatic-approval flag and never answers approval prompts.
- Hermes slash, shell, interpolation, path-drop, and path-completion syntax is
  neutralized for voice input. Control whitespace is collapsed.
- Agent transcript text goes to tmux over stdin, not command arguments or the
  journal.
- If delivery fails, the last undelivered transcript is saved mode 0600 at
  `~/.local/state/voicekey/last-recovery.txt`.
- Dictation and agent notifications use separate replacement IDs.

Membership in `input` lets every process running as the user read raw keyboard
events, not merely F9/F10. This is acceptable as an explicit first-version
tradeoff on a personal machine, not a strong security boundary. A future
version should isolate evdev access in a minimal helper that emits only the two
configured key transitions.

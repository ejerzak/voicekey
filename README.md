# VoiceKey

A global voice dictation hotkey for Linux/Wayland. Speak anywhere: browsers, chat apps, terminals and editors.

- **Hold** the hotkey to talk; **tap** it to keep listening until the next press.
- Speech is transcribed locally at each pause, with live previews where supported.
- Optional local or remote cleanup, word corrections, and an
  [agent hotkey](docs/reference.md#agent-key-optional).

Emacs and Neovim have enhanced, optional integrations: direct buffer insertion,
Evil/Neovim mode handling, and experimental **draft mode** to preview a whole
passage before accepting or discarding it. Neither editor is required.

Fedora with Niri is the tested setup.

## Requirements

- Linux with a systemd user session, Niri, PipeWire and a microphone
- DankMaterialShell for the bar widget (optional)
- `uv`, `gcc`, `pw-record`, `wtype`, `wl-copy`, `notify-send`
- Read access to keyboards (the `input` group)

The default speech models are English-only and run on the CPU; the optional
faster-whisper backend supports other languages and CUDA ([configuration](docs/reference.md#configuration)).
Live previews in application text fields need Wayland input-method support;
if you already use an input method such as fcitx, set `[dictation] ime = false`.
Sway and Hyprland are untested; GNOME and KDE are unsupported.

## Install

```sh
sudo dnf install gcc pipewire-utils wl-clipboard libnotify wtype uv
git clone https://github.com/ejerzak/voicekey.git
cd voicekey
./install.sh
sudo usermod -aG input "$USER"   # then log out and back in
```

The installer sets up Python, downloads the models, enables `voicekey.service`,
and, if DMS is installed, links its widget: add **Voicekey** in DMS Settings →
DankBar → Widgets. Keep the checkout, and rerun `./install.sh` after upgrading.

**Default hotkeys:** Right Win (`KEY_RIGHTMETA`) for dictation, Right Alt + Right Win for the agent.
For the agent chord, hold Right Alt before pressing Right Win; tap the chord to open the agent without speaking.
Change `dictate_key` / `agent_key` in the config; keys can include modifier chords.
Reserve your chosen keys in Niri so apps ignore them (defaults below):

```kdl
Super_R repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Dictation" { spawn "true"; }
Alt+Super_R repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Agent" { spawn "true"; }
```

If your layout uses Right Alt as AltGr, use `Mod5+Super_R` for the Niri binding
([Niri key bindings](https://github.com/niri-wm/niri/wiki/Configuration%3A-Key-Bindings)).

For the optional editor integrations:

- **Emacs:** run a server, with `(server-start)` or a daemon; Evil is optional.
- **Neovim:** use terminal Neovim 0.10+ and load `contrib/nvim` at startup
  ([instructions](contrib/nvim/README.md)).

Settings are in `~/.config/voicekey/config.toml` (see the
[example](config.example.toml)). After changing them:

```sh
systemctl --user restart voicekey.service
~/.local/share/voicekey/venv/bin/python -m voicekey --check
```

## Using it

Focus a text field, then hold or tap the hotkey. The widget shows where you are
dictating and has start/stop, **Free memory** (unloads the models) and
**Disable VoiceKey**.

Switching windows pauses dictation by default; the widget can instead follow the
focused window or stay with the original one (`[persistent] destination_policy`).
Listening stops after 60 seconds of silence by default (`[persistent] silence_seconds`).
Continuous dictation needs a verified text field or editor buffer; otherwise it
pauses and keeps your speech for recovery. The widget offers simulated typing
as a per-session exception.

With the Emacs/Neovim integrations, pending text stays with its original buffer.
Dictation started in normal mode enters insert mode and restores normal mode
when finished, unless you changed mode yourself.

### Draft mode

In editable Emacs and Neovim buffers, draft mode previews a passage before
inserting it. Turn it on in the widget, or set `[persistent] draft = true`.

- Press the hotkey to start. Your words appear as a preview; the buffer is untouched.
- Press it again to insert everything as one undo step, or Escape to discard
  (configurable with `draft_cancel_key`; recoverable with `--copy-last`).
- Switching away stops recording and keeps the draft. Return to accept/discard
  by hotkey, or use the widget's **Accept draft** / **Discard draft**; returning
  does not resume recording.
- Outside editable Emacs and Neovim buffers, dictation works as usual.

Details: [draft mode](docs/draft-mode.md).

## Privacy and recovery

Speech recognition is local. Cleanup is off by default; a remote cleanup
endpoint or agent receives your text. Audio and transcripts are kept under
`~/.local/state/voicekey/sessions/`. Audio is deleted after successful delivery
or draft discard; unresolved recordings remain for recovery. Successful text is
normally retained for up to seven days (`[pipeline] history_days`). These files
can contain private text.

If something does not arrive, check the destination first, then:

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --last          # show the last dictation
~/.local/share/voicekey/venv/bin/python -m voicekey --copy-last     # copy it
~/.local/share/voicekey/venv/bin/python -m voicekey --explain-last  # what happened to it
```

## More

- [Reference](docs/reference.md) · [Draft mode](docs/draft-mode.md) ·
  [Neovim plugin](contrib/nvim/README.md) · [Example config](config.example.toml) ·
  [Architecture](docs/persistent-mode-architecture.md)
- Tests: `~/.local/share/voicekey/venv/bin/python -m unittest discover -q`
  (isolated targets and private editors; never the active desktop)

MIT. See [LICENSE](LICENSE).

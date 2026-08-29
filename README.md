# voicekey

Hold-to-talk dictation for Wayland desktops. Hold a key and speak: the words
appear as you say them, inline in whatever you are typing into. Release the
key, and a second pass over the whole recording replaces them with the final
transcript. Everything runs locally on the CPU; nothing leaves the machine.

- **Live, in place.** The provisional text is drawn by the application
  itself, through the Wayland input-method protocol that CJK input uses,
  and swapped for the final text in one step. No overlay window, nothing
  typed and then deleted.
- **Accurate final text.** Streaming recognizers trade accuracy for
  latency, so the preview comes from a streaming model and the text that
  lands comes from an offline model with full context and proper
  punctuation.
- **Never in the wrong place.** Text goes only into the field that was
  active when the key went down (captured within a few milliseconds of
  the press). If that field is gone by the time the final pass lands, the
  transcript is copied to the clipboard and a notification says so.
- **Optional agent key.** A second key sends the transcript to a persistent
  [Hermes](https://hermes-agent.nousresearch.com) agent session instead.

## Requirements

- Linux with systemd and PipeWire, on a Wayland compositor that implements
  `zwp_input_method_v2`: niri, sway, Hyprland, river, labwc, Wayfire.
  GNOME and KDE implement neither this nor the virtual-keyboard protocol;
  there voicekey could only copy to the clipboard, so it is not supported.
- `uv` (installs Python 3.12 into a private venv), `gcc` (the evdev
  package builds from source), and `pw-record`, `wtype`, `wl-copy`,
  `notify-send`.
- About 1.1 GB of disk for the two models and 1.6 GB of RAM while running.
  Any recent x86_64 CPU is fine: the live recognizer uses roughly a fifth
  of one core, the final pass takes about 5 % of the recording's length.
- English speech. Both models are English-only.

Fedora: `sudo dnf install gcc pipewire-utils wl-clipboard libnotify wtype uv`

## Install

```sh
git clone https://github.com/ejerzak/voicekey.git && cd voicekey
./install.sh                     # venv, models, user service, config file
sudo usermod -aG input "$USER"   # raw keyboard access; log out and back in
```

The compositor must not act on the dictation keys itself. Under niri, add
no-op binds (with Dank Material Shell they live in
`~/.config/niri/dms/binds.kdl`):

```kdl
F9  repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Dictation" { spawn "true"; }
F10 repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Agent" { spawn "true"; }
```

The daemon starts with your next graphical session as `voicekey.service`.
Hold F9, talk, release. Settings live in `~/.config/voicekey/config.toml`,
created from [`config.example.toml`](config.example.toml); every key is
documented there. To check the setup:

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --check
```

## How it works

```
key down  ─ pw-record ─ 100 ms frames ─┬─ streaming model ─ live text ─ preedit in the focused field
                                       │                               (or a notification)
                                       └─ buffer
key up    ─ buffer ─ offline model ─ final text ─ commit in place of the preedit
                                                  (or wtype / clipboard)
```

Keys are read directly from evdev, so press and release work even though
Wayland has no global hotkeys. Audio streams from `pw-record` into a
cache-aware streaming transducer (NVIDIA Nemotron Speech Streaming 0.6B, via
sherpa-onnx) whose output only ever grows; it is shown as *preedit*
— provisional text the application renders inline but never inserts. On
release, the whole recording goes to an offline model (NVIDIA Parakeet
Unified 0.6B, via sherpa-onnx; about a point of word error rate better and
much better punctuation), and its text is committed in place of the
preedit.

voicekey registers with the compositor as *the* input method. Applications
that speak `text-input-v3` (GTK, Qt, Firefox, Chromium and Electron,
Emacs pgtk, foot, Ghostty, kitty, Alacritty, …) get the in-field
experience; anything else gets the preview in a notification and the final
text through `wtype`. Because there is one input method per seat, voicekey
cannot coexist with an IME such as fcitx — set `ime = false` to keep one.

Spacing between dictations is automatic: a dictation that continues text
gets a leading space, one that starts a line or follows an opening bracket
does not. The character before the cursor decides when the application
reports it (GTK fields, Firefox); terminals and Emacs report nothing, so
there voicekey assumes it is continuing its own previous dictation in that
window.

## Configuration

| key | meaning |
|---|---|
| `dictate_key`, `agent_key` | evdev key names or chords to hold (`KEY_F9`, `KEY_RIGHTALT+KEY_F23`) |
| `dictate_toggle_key`, `agent_toggle_key` | optional press-to-start, press-to-stop keys |
| `min_seconds`, `max_seconds` | shorter recordings are taps and discarded; longer ones are aborted (stuck key) |
| `recordings_dir` | keep the audio and both transcripts of every recording (off by default) |
| `[backend]` | final pass: `parakeet` (CPU) or `faster-whisper` (CUDA), and its model |
| `[streaming] model_dir` | live-preview model; `""` disables the preview |
| `[dictation] ime` | use the input method for preview and commit (default true) |
| `[dictation] inject` | without an input method: `wtype` (type it) or `clipboard` (copy it and say so) |
| `[dictation] max_delay_seconds` | older transcripts are copied, never typed late |
| `[dictation] require_same_window` | copy instead of typing if the focused window changed |
| `[agent]` | Hermes target, local or over SSH via Tailscale |

`install.sh` downloads the models the config names and verifies their
SHA-256 digests.

## Agent key (optional)

Hold the agent key, speak, release: the transcript goes to a persistent
Hermes TUI. On the first dispatch voicekey starts a dedicated tmux server in
a supervised systemd user unit, runs `hermes --tui` there in a neutral
working directory, opens a Ghostty window attached to it, and waits for the
composer to be idle and empty before submitting. Later dispatches reuse the
conversation; closing the window only detaches. With
`transport = "ssh-over-tailscale"`, recording and transcription stay local
and Hermes runs on another machine over OpenSSH with strict host-key
checking. Without Hermes installed, the agent key only shows a notification.

## Diagnostics

```sh
journalctl --user -u voicekey -f                                     # what each dictation did
~/.local/share/voicekey/venv/bin/python -m voicekey --check          # models, input method, keyboards
systemctl --user stop voicekey                                       # frees the input method, then:
~/.local/share/voicekey/venv/bin/python -m voicekey --replay x.wav   # dictate a 16 kHz mono WAV
```

`--check` exits 0 when ready, 2 when no keyboard is readable, 3 when only
the agent target is unavailable, 1 on a configuration, dependency or model
failure. Undelivered transcripts are saved, mode 0600, at
`~/.local/state/voicekey/last-recovery.txt`.

## Caveats

- Membership in the `input` group lets every process of your user read raw
  keyboard events. That is the tradeoff for press-and-release on Wayland;
  a minimal privileged helper would be the fix.
- The agent path infers Hermes's state from its visible TUI, so a Hermes
  update can break the "never submit into a dialog" guarantee until the
  patterns are refreshed.
- Tested on Fedora with niri. Other listed compositors follow the same
  protocols but have not been exercised.

## License

MIT. `voicekey/_input_method_v2.py` is generated from wlroots'
`input-method-unstable-v2` protocol, whose MIT notice it carries.

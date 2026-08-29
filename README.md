# voicekey — hold-to-talk dictation for Wayland

Hold a key and speak. What the recognizer hears appears *as you say it*,
inline in the field you are typing into, as provisional text. Release the key
and a second pass over the whole recording replaces it with the final
transcript. Everything runs locally on the CPU; nothing leaves the machine.
A second key sends the transcript to an agent (Hermes) instead of typing it.

```
key down  ─ pw-record ─ 100 ms frames ─┬─ streaming model ─ live text ─ preedit in the focused field
                                       │                               (or a notification)
                                       └─ buffer
key up    ─ buffer ─ offline model ─ final text ─ commit in place of the preedit
                                                  (or wtype / clipboard)
```

## How it works

- **Two models, on purpose.** Streaming recognizers trade accuracy for
  latency, so the live text comes from a cache-aware streaming transducer
  (NVIDIA Nemotron Speech Streaming 0.6B, ~7.1 % WER) and the text that lands
  comes from an offline model with full context (Parakeet Unified 0.6B,
  ~5.9 % WER, proper punctuation). Both run through sherpa-onnx as INT8 on the
  CPU: about 100 ms of compute per 560 ms of speech for the preview and
  ~0.05× real time for the final pass, so a 10 s utterance is final about
  half a second after release.
- **Provisional text is real preedit.** voicekey registers with the
  compositor as the Wayland *input method* (`zwp_input_method_v2`) — the same
  mechanism CJK input uses. Any application that speaks `text-input-v3`
  (GTK, Qt, Emacs pgtk, Firefox, foot, Ghostty, …) draws the live text inline
  itself and swaps it for the committed string; nothing is ever inserted and
  later deleted. Applications without text-input support get the preview in
  a notification and the final text via `wtype`. Because the streaming
  decoder is greedy, the live text only ever grows. voicekey takes the
  seat's input method afresh at every key-down, so it cannot coexist with
  another IME such as fcitx; set `ime = false` if you need one.
- **Focus is respected.** In-field text carries the activation of the field
  that was active at key-down; if that field is gone by the time the final
  pass lands — you clicked elsewhere, or the app blinked its text input —
  the transcript is copied to the clipboard and a notification says so.
  Nothing is ever typed into a different field. Notification-mode dictation
  gets the weaker guard niri can offer: same *window*, or copy. Pressing the
  key again while the previous text is still landing is fine; the earlier
  text lands first.
- **Spacing is automatic.** A dictation that continues existing text gets a
  leading space; one that starts a line or follows an opening bracket does
  not. The character before the cursor decides when the application
  reports it (GTK fields, Firefox); terminals and Emacs don't, so there
  voicekey assumes it is continuing its own previous dictation in that
  window. The space is shown in the preview too, so nothing jumps at
  commit.
- **Audio comes first.** Capture starts before anything else at key-down, and
  the live recognizer decodes on its own thread; if it falls behind, only
  the preview is dropped, never microphone audio for the final pass.

## Install

System packages (Fedora names): `pipewire-utils` (`pw-record`), `wtype`,
`wl-clipboard`, `libnotify`, `gcc` (evdev builds from source), `uv`.
The compositor must not act on the chosen keys itself — under niri, bind
them to no-ops:

```kdl
F9  repeat=false allow-inhibiting=false { spawn "true"; }
F10 repeat=false allow-inhibiting=false { spawn "true"; }
```

Then:

```sh
./install.sh                    # venv, deps, ~1.1 GB of models, systemd user unit
sudo usermod -aG input "$USER"  # one-time: raw keyboard access; re-login afterwards
```

`install.sh` creates `~/.config/voicekey/config.toml` from
`config.example.toml` if it doesn't exist. To keep per-machine configs in a
dotfiles repo, symlink them there before running it. The daemon runs as
`voicekey.service` in the graphical session.

Diagnostics:

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --check         # models, IME, keyboards
systemctl --user stop voicekey                                      # frees the input method, then:
~/.local/share/voicekey/venv/bin/python -m voicekey --replay x.wav  # speak a WAV into the focused field
journalctl --user -u voicekey -f
```

`--check` exits 0 when fully ready, 2 when no keyboard is readable, 3 when
only the agent target is unavailable, 1 for a configuration, dependency or
model failure. Set `recordings_dir` to keep the audio and both transcripts of
every recording — the way to compare models on your own voice.

## Configuration

`config.example.toml` lists every key with its default. The ones that matter:

| key | meaning |
|---|---|
| `dictate_key`, `agent_key` | evdev key names or chords (`KEY_RIGHTALT+KEY_F23`) to hold |
| `dictate_toggle_key`, `agent_toggle_key` | optional press-to-start, press-to-stop keys |
| `[backend]` | final pass: `parakeet` (CPU) or `faster-whisper` (CUDA) and its model |
| `[streaming] model_dir` | live-preview model; `""` disables the preview |
| `[dictation] ime` | use the input method for preview and commit (default true) |
| `[dictation] inject` | delivery without an input method: `wtype` (type it) or `clipboard` (copy it and say so) |
| `[dictation] require_same_window` | copy instead of typing if niri focus changed |
| `[agent]` | Hermes target, local or over SSH via Tailscale |

`install.sh` downloads whichever sherpa-onnx models the config names and
checks their SHA-256 digests; the known ones are listed in
`voicekey/backends.py`.

## Agent dispatch

Hold the agent key, speak, release: the transcript is sent to a persistent
Hermes TUI. On the first dispatch voicekey starts a dedicated tmux server in a
supervised systemd user unit, starts `hermes --tui` in a named session and a
neutral working directory, opens a Ghostty window attached to it, and waits
for the composer to be idle and empty before submitting. Later dispatches
reuse the same conversation; closing the window only detaches. With
`transport = "ssh-over-tailscale"` recording and transcription stay local
while Hermes runs on the remote host (standard OpenSSH server, public-key
auth, strict host-key checking; Tailscale only supplies the network path).

```sh
tmux -L voicekey-hermes attach-session -t voicekey-hermes   # open the session yourself
```

`ready_timeout` bounds how long a transcript waits for a busy Hermes; on
timeout it is written to the recovery file instead of typed into a dialog.

## Behaviour and safety

- Taps shorter than `min_seconds` are discarded; recordings longer than
  `max_seconds` are aborted (stuck key).
- Dictation older than `max_delay_seconds` is copied, not typed.
- If `wtype` fails, the text is copied and the notification says so; no
  paste keystroke is simulated (terminals read Ctrl+V as "literal next key").
- Agent prompts are pasted only when Hermes is idle with an empty composer;
  voicekey never answers approval prompts. Hermes slash, shell and path
  syntax is neutralized in voice input.
- Undelivered transcripts are saved mode 0600 at
  `~/.local/state/voicekey/last-recovery.txt`.
- Membership in `input` lets every process of the user read raw keyboard
  events — an explicit tradeoff for a personal machine, not a security
  boundary. A minimal evdev helper exposing only the two key transitions
  would be the fix.

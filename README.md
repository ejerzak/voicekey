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
  transcript is copied to the clipboard and a notification says so. In
  Emacs the buffer itself is pinned at key-down, so nothing that moves
  focus in the meantime can redirect the text.
- **Optional polish pass.** A small language model can clean the transcript
  before it lands: fillers, stutters and self-corrections go, punctuation
  and numbers are written out, nothing is added. Off by default; on a laptop
  CPU it costs about half a second per sentence, and the raw transcript
  lands unchanged whenever the model is late or not trusted.
- **Optional agent key.** A second key sends the transcript to a persistent
  [Hermes](https://hermes-agent.nousresearch.com) agent session instead.

## Requirements

- Linux with systemd and PipeWire, on a Wayland compositor that implements
  `zwp_input_method_v2`: niri, sway, Hyprland, river, labwc, Wayfire.
  The focused-window guard talks to niri, sway and Hyprland; on the others
  set `require_same_window = false` (the field-level guard still applies).
  Only those three also tell voicekey which application is focused, which
  the Emacs delivery below depends on: elsewhere Emacs gets the generic
  commit, so dictate into it in insert state only.
  GNOME and KDE implement neither this nor the virtual-keyboard protocol;
  there voicekey could only copy to the clipboard, so it is not supported.
- `uv` (installs Python 3.12 into a private venv), `gcc` (the evdev
  package builds from source), and `pw-record`, `wtype`, `wl-copy`,
  `notify-send`.
- About 1.1 GB of disk for the two models and 1.7 GB of RAM while running.
  Idle cost is nil. While you speak, the live recognizer keeps roughly one
  core busy (measured 0.75 core on a desktop i5); the final pass then
  takes about 5 % of the recording's length on four cores.
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
key up    ─ buffer ─ offline model ─ raw text ─ [polish model ─ clean text] ─ commit in place of the preedit
                                                 (optional; raw text shown      (or wtype / clipboard)
                                                  as preedit meanwhile)
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

Emacs is a special case: committed text reaches an evil-mode buffer as
keystrokes, so in normal or visual state it would become commands. When
the focused window is Emacs, voicekey therefore pins the current buffer at
key-down and, at delivery, asks Emacs through `emacsclient` to insert into
that buffer with the gesture of its state then — at point in insert state,
after the cursor in normal state (`a`), in place of the selection in visual
state (`c`), to the process in a terminal buffer — and leaves the state as
it found it: normal and visual state end back in normal, as Escape would.
Focus is not checked: an agent's `emacsclient`, a dialog or another frame
may have moved it, and the text still lands where it was meant to,
following point within that buffer but never following focus out of it.
Emacs refuses, and voicekey
copies instead, only when the buffer is gone or read-only, an operator is
pending or the selection is blockwise. If Emacs cannot answer within what
is left of `max_delay_seconds` (a GTK dialog blocks its command loop), the
transcript is saved for recovery rather than copied: the insertion still
runs once the dialog is dismissed, and a paste on top of it would double
the text. The live preview is still the preedit; Emacs also reports the
character before point, so spacing there is exact.

Spacing between dictations is automatic: a dictation that continues text
gets a leading space, one that starts a line or follows an opening bracket
does not. The character before the cursor decides when the application
reports it (GTK fields, Firefox); terminals and Emacs report nothing, so
there voicekey adds a space only when it was itself the last thing to type
in that window — a keystroke on any keyboard in between leaves spacing to
you (mouse clicks are not observed).

## Configuration

| key | meaning |
|---|---|
| `dictate_key`, `agent_key` | evdev key names or chords to hold (`KEY_F9`, `KEY_RIGHTALT+KEY_F23`) |
| `dictate_toggle_key`, `agent_toggle_key` | optional press-to-start, press-to-stop keys |
| `min_seconds`, `max_seconds` | shorter recordings are taps and discarded; longer ones are stopped and transcribed (stuck key?) |
| `recordings_dir` | keep the audio and both transcripts of every recording (off by default) |
| `[backend]` | final pass: `parakeet` (CPU) or `faster-whisper` (CUDA), and its model |
| `[streaming] model_dir` | live-preview model; `""` disables the preview |
| `[dictation] ime` | use the input method for preview and commit (default true) |
| `[dictation] inject` | without an input method: `wtype` (type it) or `clipboard` (copy it and say so) |
| `[dictation] max_delay_seconds` | older transcripts are copied, never typed late |
| `[dictation] require_same_window` | copy instead of typing if the focused window changed (Emacs is exempt: its buffer is pinned) |
| `[polish]` | third pass: `backend` (`none` or `openai`), `url`, `format` (`s1-mini` or `instruct`), `style`, `max_wait_seconds` |
| `[polish.server]` | `model_file` makes voicekey run `llama-server` itself, as a child process; `command` names which one |
| `[agent]` | Hermes target, local or over SSH via Tailscale |

`install.sh` downloads the models the config names and verifies their
SHA-256 digests.

## Polish pass (optional)

A transcript is what you said; a draft is what you meant. The polish pass
hands the offline transcript to a language model and commits what comes
back, so "so um i need to like send the the report by uh friday no wait make
that thursday" lands as "So I need to send the report by Thursday." While
the model works, the raw transcript stands in the field as preedit, so the
words are visible at the same moment as before; what moves is when they
solidify.

The default model is [S1-mini by Superwhisper](https://huggingface.co/superwhisper/s1-mini),
a 0.6B text normaliser trained for exactly this and nothing else: it will not
follow instructions, answer questions or invent content, and it runs on the
CPU (about half a second for a sentence, 1.5 s for a 500-character
paragraph, on 4 threads). To turn it on:

```toml
[polish]
backend = "openai"
[polish.server]
model_file = "~/.local/share/voicekey/s1-mini-q4_k_m.gguf"
```

then `./install.sh` (which fetches the model and a pinned llama.cpp release
build, verifying both digests) and `systemctl --user restart voicekey`.
voicekey starts `llama-server` as a child process and stops it with the
daemon. `style` picks the register (`casual`, `semi-casual`, `semi-formal`,
`formal`). A distribution's llama.cpp works too (`command = "llama-server"`
under `[polish.server]`), but check its speed: Fedora's `llama-cpp` package
is a ROCm build whose CPU path took 1.4 s for a sentence where the upstream
CPU build took 0.4 s on the same machine.

Any OpenAI-compatible chat endpoint works in place of the child server:
leave `model_file` empty and point `url` at Ollama, vLLM or a llama-server
elsewhere. With `format = "instruct"` voicekey sends its own prompt (or
yours, from `prompt_file`) for a general model that can do more, such as
LaTeX from a formula described in words.

What the model returns is judged before it is used. A reply cut off at the
token limit, one that grew beyond the input, or one where more than a
quarter of the words were never said is rejected and the raw text lands;
so does the raw text when the model does not answer within
`max_wait_seconds` (4 s by default, and always within the delivery budget).
An empty reply to a filler-only dictation means there is nothing to type.
With `recordings_dir` set, every recording keeps its live, raw and polished
texts side by side for review.

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
failure; with the polish pass on it also starts the model and runs one
sentence through it. Transcription, polish and delivery run on separate
threads, so a slow Emacs or a hung clipboard delays only the deliveries
behind it, never the transcription of the next recording; a clipboard copy
is given three seconds. The polish server's output goes to
`~/.local/state/voicekey/polish-server.log`. Every transcript that was not typed — copied to the clipboard
instead, or undeliverable — is also saved, mode 0600, at
`~/.local/state/voicekey/last-recovery.txt`, since the clipboard is one
`wl-copy` away from being overwritten.

## Agents

Coding agents drive the same desktop — `emacsclient`, `wl-copy`, `wtype`,
compositor actions — and one of them evaluating Lisp in Emacs mid-dictation
can steal focus or the clipboard. From key-down until the text has landed,
voicekey holds an exclusive `flock` on `$XDG_RUNTIME_DIR/voicekey/lock`. A
hook that takes a shared lock before such tools run, waits a bounded time
and then refuses with a reason keeps agents out of the way (for Claude Code
and Codex: `ai/shared/hooks/voicekey-lock.sh` in the config repo). The lock
is advisory, voicekey itself never waits for it, and it dies with the
daemon, so nothing can wedge.

## Caveats

- Membership in the `input` group lets every process of your user read raw
  keyboard events. That is the tradeoff for press-and-release on Wayland;
  a minimal privileged helper would be the fix.
- The agent path infers Hermes's state from its visible TUI, so a Hermes
  update can break the "never submit into a dialog" guarantee until the
  patterns are refreshed.
- Tested on Fedora with niri. The sway and Hyprland focus queries and the
  other listed compositors follow the same protocols but have not been
  exercised.

## License

MIT. `voicekey/_input_method_v2.py` is generated from wlroots'
`input-method-unstable-v2` protocol, whose MIT notice it carries.

# VoiceKey reference

For requirements and installation, see the [README](../README.md).

## How it works

```text
Hotkey down → continuous recording → live preview for the current batch
                  pauses / batch limit → offline transcript → optional polish → insert
Hotkey release after a hold / next press after a tap → stop capture and finish the tail
```

Keys are read directly from evdev, so press and release work even though
Wayland has no global hotkeys. Audio starts on key-down, before deciding
whether the gesture is a tap or a hold. A short release latches listening on;
a longer hold stops on release. Key repeats do not toggle recording.

The speech detector cuts at pauses (1.2 seconds by default), with a hard
30-second batch limit even during uninterrupted speech. The streaming model
provides provisional text; the offline model and optional cleanup finalize each
batch. Earlier batches can land while the next one records. There is no
90-second cap on the dictation session; 60 seconds of silence stops it by
default. These limits live under `[persistent]` and apply to both dictation hotkey gestures
and any additional dictation toggle keys.

voicekey registers with the compositor as *the* input method. Applications
that speak `text-input-v3` (GTK, Qt, Firefox, Chromium and Electron,
Emacs pgtk, foot, Ghostty, kitty, Alacritty, …) get the in-field
experience; other applications have no live preview and get final text through
`wtype` for ordinary replay. Persistent dictation pauses when no
verified field is available; the panel offers an explicit simulated-typing
exception. Because there is one input method per seat, voicekey cannot coexist
with an IME such as fcitx. Setting `ime = false` keeps the other IME, but generic
persistent destinations then need that explicit exception.

Emacs is a special case: committed keystrokes in Evil normal or visual
state can become commands. Voicekey instead requests a buffer pin through
`emacsclient`. Emacs must acknowledge it within 250 ms; a late or failed pin
cannot authorize insertion. The pin identifies the buffer when Emacs handles
that request, rather than claiming an atomic snapshot at physical key-down.
`emacsclient` reaches one Emacs server, so the pin request also names the
process that owns the focused window when the compositor reports it (niri,
sway and Hyprland do); a window of a second Emacs process is refused rather
than bound to whatever the server's own selected window shows. The
acknowledgement names the pinned buffer, its major mode and read-only state;
the daemon logs it, records it with each delivery attempt in the recovery
journal, and a refusal names the buffer.
Once pinned, delivery follows point within that buffer and ignores compositor
focus. Dictation behaves as typing: pinning a buffer in Evil normal state
enters insert state as `a` does (the cursor then follows the text), and the
end of the session returns to normal state as <Esc> does, unless you left
insert state yourself meanwhile. Neovim does the same with its modes. Insert
state inserts at point; visual state replaces the selection and finishes in
normal state; read-only and terminal buffers keep their state, and terminal
buffers receive process input. Spacing is computed at the actual insertion
position inside the editor transaction.

The packaged `voicekey/voicekey.el` provides those transactions. It is loaded
on demand and installs no hooks by default. Emacs uses the same Wayland
preedit preview as other supported applications. When an input-method activation
is unavailable, there is no live preview popup. The preview clear
request is flushed before the separate editor insertion is submitted, within
the delivery deadline. This orders the requests locally; the two channels
do not provide an atomic application-level transaction. Losing preview focus
does not invalidate an acknowledged buffer pin for final insertion.

Shared Wayland previews are also the default for persistent mode.
Start with dictation at one position and let pending text finish before moving
point; Emacs overlays and per-utterance insertion markers can be revisited if
actual use needs them. To bind the last buffer used through Emacs's command
loop, even when an agent changes the selected buffer, load the library in your
Emacs configuration and explicitly
enable `(voicekey-tracking-mode 1)`. This tracking behavior is tested against
a private Emacs server; it does not make an asynchronous request a historical
snapshot of point.

An expired Emacs insertion is refused inside Emacs before mutation. Each
operation has a unique ID and a revocable permission file. A duplicate request
returns its previous result. A timeout or an error after mutation begins is
reported as uncertain: inspect the field and the saved transcript before
repeating it. Buffer text edits are grouped atomically; terminal writes cannot
be rolled back.

Terminal Neovim uses the same pinned-editor interface, with a separate Lua
implementation. With the [plugin](../contrib/nvim/README.md) loaded at startup,
the normal daemon keys resolve a uniquely focused, responding Neovim under the
terminal's PID and insert through its buffer API. Neovim uses inline virtual text
at an advancing extmark, including persistent previews. It keeps that position
when the cursor moves, unlike Emacs's follow-point behavior. Every RPC has a
timeout; Lua checks expiry and the journal permit before insertion. Failed pins
or insertions never become terminal keystrokes. Unresolved audio/text stays in
recovery, and uncertain insertion is never retried automatically.

Registrations are per instance. Focus is unconfirmed until `FocusGained` or
`VimResume`; switch away and back if no initial focus event arrives. In a known
terminal, a Neovim confirming focus gets buffer delivery. Otherwise the terminal
keeps ordinary typing, unless its title shows an editor in front (`nvim …`,
`… - NVIM`), which is refused. A Neovim open elsewhere never blocks typing into
other tabs or windows. An unconfirmed Neovim without such a title, for example
one opened by `git commit`, receives ordinary typing as before. See the plugin
README for setup and the precise limits.

When a generic field loses focus, applications may keep or discard its
provisional text without reporting what happened. Voicekey stops the session
and preserves pending text instead of guessing which field or text to replace.
Check any remaining provisional text before recovering saved speech. An IME
`submitted` delivery outcome means the request was sent to the compositor; the
protocol provides no application-level insertion acknowledgement. Dictation
requires an insertion destination: clipboard-only targets cannot start it.
Failed continuous deliveries are saved for recovery without repeatedly
replacing the clipboard. Ordinary single-recording replay can still fall back
to a clipboard copy.

Spacing uses current surrounding text inside the IME operation, or the
editor's actual insertion position. Where the application reports no cursor
context, a leading space is added only when voicekey was the last thing to
type in that window. A keystroke on any keyboard resets that fallback; a
modifier alone does not. Mouse movement is not tracked.

### Formatting and unintended Return protection

Automatic delivery checks text after polish, word overrides, and hooks. Ordinary
Emacs buffers preserve paragraphs and tabs. Emacs `term`/`vterm`, simulated typing
through `wtype`, and unclassified input-method destinations replace line breaks
and tabs with spaces. Other C0/C1 control characters, including Escape, Backspace,
and DEL, refuse the entire insertion before any text is sent. Unicode letters,
emoji, and their joining characters are preserved. Line endings are normalized
to LF before applying the policy. Flattening absorbs spaces adjacent to line
breaks or tabs without changing ordinary repeated spaces.

To preserve formatting in a checked GUI composer, add its exact compositor app ID
to `[dictation] multiline_apps`. The list is empty by default. This permission
applies only when delivery actually uses the input method **and** the current
field reports the Wayland `multiline` hint with `normal` purpose. Missing hints,
terminal purpose, and `wtype` fallback still get filtering. Check a composer's
behavior with disposable text before adding it; an app ID covers the whole app,
so a browser-wide entry also trusts embedded fields such as web terminals if they
report ordinary multiline input. Field hints are application claims, not proof
that insertion cannot trigger actions.

The same policy covers continuous batches and in-field previews, including the
pending preview sent alongside a commit. Emacs's shared Wayland preview remains
single-line; its final pinned-buffer insertion preserves formatting. Emacs checks
the buffer's current mode during insertion, even if it changed after pinning.
If the current field's hints become restrictive, an already-visible multiline
preview is flattened immediately; newer queued previews remain intact. A preview
containing other controls is cleared, with its control code logged without the
dictated text. NUL is refused before launching `emacsclient`, since it cannot be
passed in a process argument.

The journal and recovery retain the original formatted transcript. Clipboard
recovery and `--copy-last` also retain it for deliberate manual use; copying
does not automatically paste. Agent dispatch and stdout output are unaffected.
Existing `inject = "wtype"` configurations gain filtering automatically. This
prevents transcript formatting from becoming Return/Tab keystrokes or terminal
newline input on restricted paths; it cannot prevent arbitrary applications
from assigning actions to ordinary characters.

## Configuration

| key | meaning |
|---|---|
| `dictate_key`, `agent_key` | dictation and agent tap/hold keys or chords (`KEY_RIGHTMETA`, `KEY_RIGHTALT+KEY_F23`) |
| `tap_seconds` | the one tap/hold boundary: a shorter press is a tap (dictation keeps listening; the agent opens), a longer one a hold (dictation stops; the recording goes to the agent). Single recordings shorter than this are dropped |
| `dictate_toggle_key`, `agent_toggle_key` | optional press-to-start, press-to-stop keys |
| `max_seconds` | single-recording limit for agent, stdout and ordinary replay |
| `recordings_dir` | keep the audio and both transcripts of every recording (off by default) |
| `[backend]` | final pass: `parakeet` (CPU) or `faster-whisper` (CUDA), and its model |
| `[streaming] model_dir` | live-preview model; `""` disables the preview |
| `[persistent]` | shared dictation engine: optional extra toggle key, speech detector, pause/silence thresholds and batch/delivery limits |
| `[dictation] ime` | use the input method for preview and commit (default true) |
| `[dictation] inject` | without an input method: `wtype` (type it) or `clipboard` (copy it and say so) |
| `[dictation] multiline_apps` | exact app IDs allowed formatting through IME in fields reporting ordinary multiline text; default `[]` |
| `[dictation] max_delay_seconds` | single-recording delivery budget; continuous dictation uses `[persistent] delivery_seconds` per insertion |
| `[dictation] require_same_window` | copy instead of typing if the focused window changed (Emacs is exempt: its buffer is pinned) |
| `[polish]` | third pass: backend, endpoint, format, style, `min_words` (8), `max_wait_seconds` (4) |
| `[pipeline]` | pending count/audio limits, transcription and shutdown deadlines, recovery quota and history retention |
| `[polish.server]` | `model_file` makes voicekey run `llama-server` itself, as a child process; `command` names which one |
| `[agent]` | persistent Hermes (local or SSH via Tailscale), or a local command |

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
a 0.6B text normaliser trained for speech cleanup. Earlier measurements were
about half a second per sentence and 1.5 s per 500-character paragraph on four
CPU threads. To turn it on:

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

Styles can be overridden per destination app with an exact, case-sensitive
app ID match. For example, under `[polish]`:

```toml
style = "semi-formal"
app_styles = { "org.signal.Signal" = "semi-casual" }
```

Unknown or unmatched apps use `style`. The app ID comes from the destination
bound to each utterance; with focus-following, new speech uses the new
destination's app ID while pending speech retains its original one. This applies to both `s1-mini`
and `instruct` formats and does not change the minimum-word thresholds or agent
mode's cleanup bypass. On niri, `niri msg -j windows` lists app IDs. Restart
voicekey after changing these settings.

Any OpenAI-compatible chat endpoint works in place of the child server:
leave `model_file` empty and point `url` at Ollama, vLLM or a llama-server
elsewhere. With `format = "instruct"` voicekey sends its own prompt (or
yours, from `prompt_file`) for a general model that can do more, such as
LaTeX from a formula described in words.

`[persistent] polish_min_words = 0` cleans every nonempty dictation batch,
including held-hotkey speech. `[polish] min_words = 8` applies to single-recording
stdout capture and ordinary replay. A skipped dictation still waits behind
earlier dictations, preserving speech order.

By default, continuous dictation supplies the previous delivered batch's ending
(up to 50 words and 800 characters) to cleanup. S1-mini cleans the combined
text using its existing prompt, and usually edits the context as well: it drops
a trailing "um", changes a capital, or punctuates where the two parts meet.
Earlier text is never reinserted or edited, so Voicekey aligns the reply's words
with the request's, discards the model's version of the context, and validates
only the new part against the current transcript. Where the context ends
without punctuation and the model put some there, the new text begins with it:
`the question I keep coming back to is` followed by `What is the cheapest
fix?` produces `, what is the cheapest fix?`. An earlier full stop cannot be withdrawn; the new
text then starts with a capital. A reply that cannot be split cleanly (a
context word after new words, one edit spanning both parts, new words filed
under the context), that copies context-only words into the new part, or that
fails the usual checks is retried once without context, within the same
timeout; the journal records `applied without context: <reason>`. Insertion
spacing remains a separate operation.

Context is confined to the same destination within a continuous session. Pending batches are not
used, and older context is skipped when the immediately preceding batch is
still awaiting delivery. Detected keyboard activity invalidates context;
mouse or programmatic cursor movement is not tracked, so this is recent
Voicekey output, not a snapshot of the editor around the current cursor.
Model output checks are conservative heuristics, not a guarantee of correct
cleanup. Both requests share the existing timeout; a timeout, or a refusal of
the retry, preserves the raw new batch. Set
`[persistent] polish_context = false` to disable it. The supplied context is
recorded as `polish_context` in the recovery journal's final event. Agent
prompts and single-recording stdout/replay do not use this context.


Empty or truncated replies, excessive growth or deletion, lost negations or
qualifications, and changes to already-written numbers trigger raw fallback.
The checks also limit novel vocabulary. They cannot establish semantic
identity: review polished paper prose, especially names and formulae. Both
raw and final text are kept in the recovery journal. `recordings_dir` retains
a separate audio/transcript corpus when configured.

The polish deadline starts when transcription finishes and includes queueing.
After `max_wait_seconds` (four seconds by default, within the delivery budget),
raw text becomes final. A hung request keeps its one execution slot; later
utterances use raw text until it returns. Late results never replace text
that has already moved on.

## Word overrides and transcription hooks

Explicit corrections work with or without polish:

```toml
[text.word_overrides]
"hyper whisper" = "hyprwhspr"
```

Matching ignores case and respects word boundaries, including for single letters.
At each position the longest matching phrase wins. Replacement strings are literal
and are never matched again. These are explicit corrections, not a spellchecker;
a personal Hunspell word list does not supply the wrong-to-right mappings.

The order is recognition → optional polish → word overrides → optional hook →
delivery. Agent prompts skip polish but receive overrides and their own hook.
Both hooks are disabled by default. Add a command to the relevant existing table:

```toml
[dictation]
post_transcription_hook = ""

[agent]
post_transcription_hook = "sed 's/^/<dictation>/; s|$|</dictation>|'"
```

A hook is a trusted shell command receiving UTF-8 text on stdin. Nonblank stdout
replaces the input verbatim, including trailing newlines. Empty or whitespace-only
output, any nonzero exit (including 77), invalid UTF-8, NUL bytes, oversized output,
or a timeout preserves the pre-hook text. Failures are logged without a popup.
Each hook gets at most five seconds, within the remaining delivery budget. A
slow hook can still exhaust that budget, causing the text to be saved instead
of inserted. Ordinary child processes are killed when the hook ends; detached
processes and external side effects cannot be undone. Hook stderr is discarded.

Raw, polished, overridden and final text are recorded before delivery. Recovery
reads the prepared text without rerunning polish, replacements or shell commands.
Restart the daemon after changing these settings.

## Last dictation and scripting

```sh
python -m voicekey --last          # latest nonempty prepared dictation, verbatim
python -m voicekey --copy-last     # copy that same text for manual paste
python -m voicekey --explain-last  # processing stages, fallback reasons, delivery outcome
```

These commands read retained journal entries without loading models or requiring
valid model configuration. They select dictations in capture order, skip agent
prompts and filler-only drops, and report when no text is available. In persistent
mode, this is the latest prepared utterance, not the whole session. Prepared text
may still be awaiting delivery; check the explanation before repeating an uncertain
insertion. Clipboard recovery does not reapply cursor-dependent spacing.

For scripts, record one utterance directly to stdout:

```sh
python -m voicekey --capture-to-stdout --seconds 10 > transcript.txt
python -m voicekey --capture-to-stdout --replay recording.wav > transcript.txt
```

Without `--seconds`, Ctrl-C finishes recording; the daemon's `max_seconds` always
caps capture. SIGTERM cancels (exit 130). The command is a thin client of the
running daemon: it loads no models, polisher, recorder or desktop integrations.
The daemon uses its existing transcription and text-processing pipeline, including
word overrides, the dictation hook and default polish style. Only prepared text
goes to stdout, without an added newline; diagnostics and the flushed
`Recording; Ctrl-C finishes, SIGTERM cancels.` line go to stderr. A flushed
`Transcribing; microphone stopped.` line marks the processing phase, including
when recording is stopped by the panel, a hotkey, or the duration limit.
Failures exit 1.

The daemon must be running and support protocol version 1; an older daemon needs
a restart after upgrading. There is no standalone fallback. The daemon's loaded
configuration applies; `--config` is rejected with this command. `--replay` sends
an absolute local WAV path for the daemon to record at real-time pace. It does not
load a second model. Captures are refused while desktop dictation or another
client capture is active, or earlier dictation is still processing.

Editor integrations can pass `--client-name Neovim` to label their destination
in daemon status and the DMS widget. This is display metadata only: it does not
select a window or change polish styles. The CLI sends the name only when the
daemon advertises support, so older version-1 daemons remain usable until restart.

`--preview` adds the daemon's live transcript on stderr while you speak, one
flushed line per revision: `Preview; "<JSON string>"`. Each line replaces the
previous one. After recording stops, a last line may carry the full raw
transcript while the polisher runs. Previews are drafts: stdout still receives
only the final prepared text, which can differ. Previews need the daemon's
streaming recognizer; while models are still loading, or while an earlier live
decoder is still running, the capture runs without them. As with
`--client-name`, the option is sent only to daemons that advertise it.

Transcripts use the daemon's normal `~/.local/state/voicekey/sessions` journal
(respecting `XDG_STATE_HOME`), so `--last` and `--copy-last` recover undelivered
client text. Disconnecting finishes and preserves the recording. Explicit
cancellation during recording discards it; cancellation after processing has
started suppresses delivery but may leave audio/text already journaled. Neither
path falls back to clipboard or window delivery. See the socket protocol below
for clients that want to connect directly.

The diagnostic `--check` and desktop `--replay` modes still load models themselves;
they are offline diagnostics, not client capture paths. Stop the service before
using them. They are retained to test model startup and desktop delivery without
changing the existing persistent/Emacs replay behaviour. The standalone scripts
in `benchmarks/` also load models for direct performance and memory measurements.

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

Tap the agent key (release within `tap_seconds`) to open Hermes without a
prompt: voicekey starts the session and terminal if needed and, on niri,
raises the terminal window. The microphone still starts at key-down, so a
hold loses nothing; a tap's audio is discarded unheard.

For a different agent, configure a local executable that accepts a prompt on
stdin. For example (replace the executable and directory with your own):

```toml
[agent]
target = "command"
transport = "local"
command = ["/absolute/path/to/agent-wrapper", "--read-stdin"]
working_directory = "~/projects/my-project"
command_timeout = 120.0
ready_timeout = 150.0
```

`command` is a nonempty array: executable first, then literal arguments. A bare
executable is resolved on PATH; executable paths and the working directory
expand `~` and become absolute when config is loaded. The directory must already
exist. Each prompt starts a fresh process in that directory. The prepared
transcript (after word overrides and the agent hook) is sent unchanged as UTF-8
on stdin, followed by EOF, without an added newline. VoiceKey performs no shell
interpolation or `{text}` substitution and never places the transcript in argv.
The wrapper must read stdin; its exit status zero means successful submission.
VoiceKey discards stdout and stderr, since either could echo private text.

`command_timeout` limits the process lifetime; `ready_timeout` is the overall
agent dispatch budget. The earlier deadline wins. Shutdown cancellation and
timeouts kill the original process group and reap the child. Members of that
group are also stopped when the command exits. Wrappers must keep all children
in the original process group: do not daemonize or create new sessions or
process groups (for example, with `setsid`, `setpgid`, or
`start_new_session=True`). Detached processes escape cleanup and may continue
running after cancellation or timeout. Wrappers must finish their work before
returning. Command mode uses no tmux, Hermes, Ghostty, or systemd; remote,
terminal, and tmux options are ignored and validated only for Hermes.
SSH transport is supported only for Hermes.

Failures, timeouts, and cancellation retain the transcript in the existing
private recovery journal and report uncertain delivery. VoiceKey does not
retry automatically: a failing process may already have acted on the prompt.
Later prompts can still be dispatched. Transcript recovery files are distinct
from application logs; the command backend logs neither transcripts nor child
output. `--check` checks executable availability and directory access without
running the configured command, and exits 3 if only the agent is unavailable.
It does not verify the command's stdin protocol or external service readiness.

## Diagnostics

```sh
journalctl --user -u voicekey -f                                     # what each dictation did
~/.local/share/voicekey/venv/bin/python -m voicekey --check          # models, input method, keyboards
systemctl --user stop voicekey                                       # frees the input method, then:
~/.local/share/voicekey/venv/bin/python -m voicekey --replay x.wav   # dictate a 16 kHz mono WAV
```

`--check` exits 0 when ready, 2 when no keyboard is readable, 3 when only
the agent target is unavailable, 1 on a configuration, dependency or model
failure; with the polish pass on it also runs one sentence through the model.
For a local polish model, the check starts a temporary server on a free
localhost port with its own temporary log, so it can run alongside the daemon.
This validates model startup and generation independently of the daemon's
configured endpoint. External polish endpoints are tested at their configured
URL. Transcription, polish and delivery have separate supervised workers;
a clipboard copy is given three seconds. The polish server's output goes to
`~/.local/state/voicekey/polish-server.log`.

## Continuous dictation and optional extra toggle

The dictation hotkey (Right Win/Super by default) uses this engine. To add another toggle key, set `[persistent] key = "KEY_F11"` (or a dedicated evdev chord), run
`python -m voicekey --download` to fetch the small Silero speech detector, and
restart the service. Reserve the key in your compositor, as for the dictation and agent hotkeys; for niri:

```kdl
F11 repeat=false allow-inhibiting=false hotkey-overlay-title="Persistent Dictation" { spawn "true"; }
```

Press once to listen continuously, and again to stop. Release does nothing;
Escape remains an ordinary editing key. The status indicator shows
whether the microphone is listening, finishing or off, with the stop reason. Automatic
startup never enables the microphone.

Speech pauses of `pause_seconds` (default 1.2) queue an utterance for
transcription and optional polish while capture continues. Pending text and
new live text share one preview, and final commits remain in speaking order.
The speech detector works independently of the optional streaming recognizer.
Persistent mode sends even short meaningful utterances through the configured
cleanup model. `[persistent] polish_min_words` controls its threshold separately
from single-recording stdout capture and ordinary replay. Whole utterances containing only recognized hesitation or
noise interjections (um, uh, er, erm, ah, eh, ach, ugh, gah, hmm, and stretched
spellings such as errrr or uhhhh) are omitted by default. Their raw text and
the drop reason remain in the journal. Mixed utterances, quoted words and
hyphenated responses such as uh-huh/uh-uh are not dropped by this rule.
Set `[persistent] drop_filler_only = false` when dictating literal interjections.
Empty model replies for meaningful text still fall back to the raw transcript.

`silence_seconds` (default 60) without detected speech turns listening fully
off; the key or panel can start another session. `max_utterance_seconds` (default 30)
forces a cut during uninterrupted speech. All three settings are configurable.

`[persistent] destination_policy` selects one of three policies:

- `"pause"` (default): leaving the starting window stops listening, including
  switching to another window of the same application. Already-recorded
  speech for Emacs or Neovim finishes through its original buffer pin, without checking
  focus. Pending text for generic destinations is kept in recovery.
  A tap starts a fresh session; returning never resumes automatically.
- `"follow"` (Niri): focus events cut captured audio. Speech before the switch
  belongs to the old destination; new speech belongs to the new one. Pending
  generic text stays in recovery even if you return before transcription ends.
  Pending editor text can finish through its original acknowledged buffer pin.
  New destinations have separate previews, polish styles and polish context.
- `"pin"`: retain the starting destination. Emacs and Neovim can receive dictation in its
  pinned buffer while you read another window. Generic destinations pause when
  their original field/window becomes unavailable.

These policies apply to both tap-to-listen and hold-to-talk. Window boundaries
follow observed compositor events and captured PCM frames (100 ms), not exact
physical-click timestamps. Pause mode uses event tracking on Niri and periodic
focus checks elsewhere, at most once a second. One missing/timed-out reply is
tolerated; two consecutive unknown replies stop capture with a tracking error.
Without events, brief away-and-back transitions between polls can go unobserved.
An acknowledged Emacs buffer without a compositor window identity remains
usable through its pin; the listening notice says window tracking is unavailable.
It cannot promise pause-on-window-switch in that case. Neovim additionally reports
terminal-local focus events to the daemon's control socket; pause/follow process
them just like window switches. This depends on terminal focus reporting (tmux
needs `focus-events on`). Other application tabs and fields remain invisible.
Emacs follows point within its pin; Neovim retains its advancing extmark.

Every persistent destination must expose a live input-method field or an
acknowledged editor buffer. Opening speech is retained for recovery if no field
is found or the editor refuses its pin. That refusal is final for the session: a late
editor acknowledgement cannot authorize insertion of its recovery text. The guard
is checked at binding, during capture
(including with a focus watcher), and before delivery. Missing or expired
fields pause listening and preserve pending speech; field loss never triggers
an automatic simulated-key fallback. Capture requires two consecutive bad
field/failure observations, 200 ms apart, before stopping; pending focus events
invalidate those observations. Brief no-window transitions in follow mode can
settle without stopping. Delivery still refuses an expired activation immediately.
A new activation does not prove it is the original field: debounce alone cannot
make tabbing between fields a supported continuous-dictation workflow.
Missing support is indistinguishable from no field, so some legitimate fields
may require an explicit exception.

After **No text field detected**, the panel offers **Use simulated typing this
session** (`--control start-typing`). Focus the intended text field before using
it. Simulated keys can invoke application shortcuts when no text field is
focused. This exception always uses pause-on-switch and ends with the session;
ordinary starts remain guarded. Clipboard-only configurations neither offer
nor accept this exception. The widget uses an explicit capability flag, so a
more detailed stop reason cannot accidentally hide or enable the button.

Legacy explicit `follow_focus = true/false` settings migrate to `"follow"`/`"pin"`.
Do not specify both the old setting and `destination_policy`. New installations
and configs that omit both default to `"pause"`. Explicit silence timeouts are
preserved; the default is now 60 seconds.

The queue uses the existing count, audio and recovery limits. A capture slot
reserves `max_utterance_seconds + 4` seconds of audio, including room for
processing lag. Long silence retains only a short lookback and does not hold
the agent coordination lock once earlier utterances are settled. Overload,
failed storage, lost keyboard or failed capture stops the microphone and
preserves available speech. There is no automatic resume after a pause.

Queued dictation batches have no insertion-age expiry. Transcription, polish and insertion calls still have finite deadlines.
Stopping signals the microphone immediately, drains for
`pipeline.shutdown_seconds` (default 10 seconds), and saves the remainder without
repeatedly overwriting the clipboard. A long queue can exceed that drain budget,
including for Emacs: its pin preserves the destination, not unlimited processing
time. Increase the budget only if real use shows unfinished queued sentences.
Session and ordered utterance IDs appear in the recovery journal. A paced WAV can exercise this path with
`--replay recording.wav --persistent`. It can perform real desktop delivery only
when a verified field or Emacs pin is available; it has no automatic typing
exception. With `ime = false`, generic destinations are rejected and captured
audio is preserved. Automated replay tests use isolated targets.

## Desktop notifications

Desktop notifications are reserved for things requiring attention. The status
indicator (DMS widget, editor UI, or a client of the control socket) handles
listening, model loading, processing and ordinary stops. Successful insertion
and agent dispatch do not show popups, and live transcript previews stay in the
input field rather than appearing as desktop notifications.

A refused action, such as pressing a dictation key while processing, gives a
brief, noncritical explanation (busy notices expire after three seconds).
Unavailable optional preview/cleanup features and a refused text field also
give noncritical warnings. Normal user stops, silence timeouts, and pauses caused
by switching windows are quiet unless speech needs recovery.

Unexpected microphone interruptions, failed or uncertain delivery, unavailable
recovery storage, and saved transcripts needing recovery still notify. Clipboard
fallback still reports that manual paste is needed. Serious failures and session
recovery notices remain visible until dismissed. These rules also apply without
the DMS widget; there is no automatic popup fallback for routine status.

## DMS bar widget

On Niri with DankMaterialShell, the optional [Voicekey widget](../contrib/dms/Voicekey)
shows a microphone icon and **Listening → destination** for the whole capture,
including silence. Automatically stopped sessions show **Off · reason**; they
never resume without another start. Muted or missing microphones show a warning. The popout has
start/stop controls and the three destination choices; keyboard gestures stay unchanged.
Policy choices are available while idle or paused and last until the daemon restarts.
For a permanent default, set `[persistent] destination_policy` in your Voicekey config.
Window policies do not distinguish tabs or panes inside one window; losing an
input-method field activation also pauses dictation.

`./install.sh` links the widget when DMS is installed and enables it if DMS is
running. Rerun it after upgrading to add the widget to an existing installation.
If DMS is stopped, it prints the commands to enable the widget later.
To install only the widget manually from this checkout:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/DankMaterialShell/plugins"
ln -s "$PWD/contrib/dms/Voicekey" "${XDG_CONFIG_HOME:-$HOME/.config}/DankMaterialShell/plugins/Voicekey"
dms ipc call plugin-scan scan
# Once scanning finishes:
dms ipc call plugins enable voicekey
```

Add **Voicekey** to your bar in DMS Settings → DankBar → Widgets. Restart
`voicekey.service` after updating the Python code. The widget reconnects if
the service restarts; it never starts recording automatically or unmutes a mic.
Opening the panel uses a shell layer, not a dictation destination. Starting
from its button closes the panel before requesting capture. Initial binding
allows up to a second for a window to return and then up to a second for its
input-method activation, without repeatedly rebinding the input method. Subsequent
follow-mode bindings use the normal 200 ms wait, regardless of how the session
started. A window change during activation still refuses delivery. If the field was slow to regain
focus, try Start listening again before resorting to simulated typing.

The same control interface is available in a terminal:

```bash
~/.local/share/voicekey/venv/bin/python -m voicekey --control status       # JSON status, no model loading
~/.local/share/voicekey/venv/bin/python -m voicekey --control start
~/.local/share/voicekey/venv/bin/python -m voicekey --control stop
~/.local/share/voicekey/venv/bin/python -m voicekey --control pause-on-switch # while idle; or: follow-focus, pin
```

The daemon publishes status and accepts these commands over a private Unix
socket at `$XDG_RUNTIME_DIR/voicekey/control.sock`. The DMS widget uses this
socket directly, so there are no polling subprocesses. Model selection and
per-app settings remain in the configuration file for this first version.

### Local control and capture protocol (version 1)

Connect a Unix stream socket to `$XDG_RUNTIME_DIR/voicekey/control.sock`
(fallback: `/run/user/<uid>/voicekey/control.sock`). The socket is mode `0600`,
its newly created directory is `0700`, and the server checks the peer UID.
There is no TCP listener. Messages are UTF-8 JSON objects, one per newline.
Keep the connection open for the entire capture and read continuously.

The first message is `{"type":"status","protocol_version":1,...}`. Status is
also broadcast on changes and approximately every two seconds. It includes
`state`, `listening`, `models`, `unload_pending`, `client_capture`, `error` and
desktop destination-policy fields. Status never contains transcript text or
capture IDs. Ignore extra fields and unrelated status messages for forward
compatibility; reject an unsupported protocol version.

Optional persistent drafts expose `draft_enabled` (runtime preference, initialized from configuration), `draft_mode`
(active session), and `draft_waiting` (prepared draft, microphone off). The latter
uses `state: "draft"`. See [draft mode](draft-mode.md) for supported buffers, ordinary fallback,
rolling cleanup, and acceptance/cancellation behavior.

`capabilities` lists optional protocol extensions (treat an absent field as an
empty list). `capture-client-name` permits `capture-start.args.client_name`;
`capture-preview` permits `capture-start.args.preview`. `draft-toggle` advertises
the idle-only `draft-on` and `draft-off` commands.
For an active client capture, `destination_name` is that supplied label, or
`Client` when unnamed; `destination` describes the socket client. Desktop
captures retain their actual window destination, such as Ghostty. A client label
does not imply that the daemon has verified the application behind the socket.

Requests have `command`, optional `id` (string or integer, echoed in replies),
and optional `args` (object, default `{}`). Use distinct IDs for outstanding
requests. Replies are `{"type":"reply","id":ID,"error":null,...}` on success,
or have a human-readable `error` string on refusal. The existing commands are
`status`, `start`, `start-typing`, `stop`, `cancel`, `draft-on`, `draft-off`,
`pause-on-switch`, `follow-focus`, `pin`
and `free-memory`; they take no arguments. `status` replies include current
status fields. `start` addresses desktop dictation. `stop` finishes the active
recording, including a client capture, and is available from any connection
without a capture ID. A client capture's result still goes to its original owner.
For persistent drafts, `stop` explicitly accepts the draft, while `cancel`
discards it. `cancel` refuses other capture types; connection-owned client
captures continue to use `capture-cancel`. `free-memory` pauses a draft without
accepting it and can unload models while the prepared draft awaits a decision.
`draft-on` and `draft-off` change the live default for subsequent persistent
sessions until daemon restart. They refuse active captures, pending processing,
and drafts awaiting a decision. Enabling drafts also enables the configured
cancellation shortcut immediately; disabling restores ordinary key handling.

A complete capture exchange looks like this (angle brackets mark example IDs):

```json
{"id":1,"command":"capture-start","args":{"seconds":30}}
{"type":"reply","id":1,"error":null,"capture_id":"<capture>"}
{"type":"capture-progress","id":1,"capture_id":"<capture>","state":"recording","models":"ready"}
{"id":2,"command":"capture-finish","args":{"capture_id":"<capture>"}}
{"type":"capture-progress","id":1,"capture_id":"<capture>","state":"transcribing"}
{"type":"reply","id":2,"error":null}
{"type":"capture-result","id":1,"capture_id":"<capture>","text":"The transcript.","error":null,"reason":null}
```

- **`capture-start`** accepts `seconds` (positive finite number, capped at the
  daemon's `max_seconds`, which is also the default), and optionally `wav`
  (absolute local path to a 16 kHz mono PCM WAV, replacing microphone input).
  When `capture-client-name` is advertised, optional `client_name` supplies a
  display label of 1–80 printable characters, with no surrounding whitespace.
  It does not set the polish app ID or alter microphone arbitration/delivery.
  When `capture-preview` is advertised, `preview: true` attaches the live
  decoder and emits `{"type":"capture-progress",...,"state":"preview","text":"..."}`
  for each revision of the live transcript, and for the raw transcript before
  polishing. Previews are best effort and never terminal; only
  `capture-result.text` is the prepared result.
  Unknown arguments are refused. The reply admits the capture and assigns its
  opaque `capture_id`; recording starts immediately afterward. A failure to open
  the recorder is a final error event. Only one capture is admitted at a time.
- **`capture-finish`** requires `args.capture_id`. It stops recording and submits
  the audio for transcription. Repeating it while that capture is processing is
  harmless. Reaching the time cap or end of the audio source finishes automatically.
- **`capture-cancel`** requires `args.capture_id`. It stops/discards recording
  and emits a final `cancelled` result. During processing, native inference may
  finish in the background, but later processing/delivery is skipped and already journaled work
  is retained. Resources remain busy until cleanup completes. Cancellation loses
  a race with an already committed result; the command then returns an error.

Only the owning connection may use `capture-finish` or `capture-cancel`;
reconnecting does not regain ownership. The global `stop` command and dictation
hotkeys can also finish a client capture. Progress and results go exclusively
to the owning connection.
All capture events echo the *start request's* `id`, plus `capture_id`, regardless
of the ID used to finish/cancel. Events can interleave with command replies
(the start reply precedes its events). Do not assume a finish/cancel reply comes
before its events. An accepted capture emits one terminal `capture-result` if
its connection remains writable. Refused starts emit only a reply.

Progress states are `loading`, `recording`, and `transcribing`. `loading` is
emitted when freed models are reloading. As with a hotkey, recording starts
while loading continues; the `recording` event also reports `models`. The
transcription stage waits for loading within its recognition budget. There is
no preview text stream. Client capture ignores follow/pause/pin window policy
and never binds to a window, editor, input method, or clipboard.

A final success has `text` (prepared transcript), `error:null`, `reason:null`.
A failure has `text:null`, a stable `error` code (`cancelled`, `no_speech`, or
`capture_failed`), and a human-readable `reason`. For example:

```json
{"type":"capture-result","id":1,"capture_id":"<capture>","text":null,"error":"capture_failed","reason":"transcription failed (transcribe)"}
```

Commands must reach the controller within two seconds and are acknowledged
without waiting for recording/transcription. That deadline does **not** apply
to an admitted capture. Recording lasts up to `seconds`; processing has the
configured `pipeline.transcription_seconds` plus `polish.max_wait_seconds` and
10 seconds for finalization, text hooks and delivery. The recognition call also
has its own `transcription_seconds` limit. A hung native call occupies its
existing worker slot; the daemon does not spawn another recognizer to replace it.

A busy microphone, active client capture, pending dictation or unavailable
recovery storage causes immediate refusal, never a queued recording. Desktop
panel starts and agent hotkeys are likewise refused during a client capture.
Dictation hotkeys finish the client capture without starting desktop dictation;
the DMS stop button and routing scripts can do the same with `stop`. Repeated
stops while processing are harmless. `free-memory` also finishes a client capture
and waits for processing before unloading. Stopping the service preserves
pending work under the normal shutdown budget.

On disconnect, recording stops and is transcribed into the regular recovery
journal. If delivery fails, `--last`/`--copy-last` can recover the prepared text;
there is no clipboard fallback. Successful delivery means the complete JSON
result was written to the socket, not that an editor inserted it. The journal
records this as `submitted`. There is no application acknowledgement or automatic
redelivery; a client that loses its connection near completion should inspect
history before inserting recovered text.

Input is limited to 8192 buffered bytes per connection; malformed JSON, unknown
commands, invalid envelope types, queue overflow, and oversized input close the
connection. Valid commands with invalid/unknown arguments receive error replies.
There are at most 16 connections and 32 queued commands. Output buffers are
bounded at 1 MiB per connection; slow readers are disconnected (normally after
three seconds). Result frames can exceed 64 KiB, up to roughly 600 KiB for a
100,000-byte transcript after JSON escaping; read through the newline. Partial
writes are buffered. On EOF, a client should report failure and offer recovery,
not silently retry capture.

### Free memory and disable

The widget's **Free memory** action stops capture, finishes or preserves pending
speech, then releases the final recognizer, streaming recognizer, speech detector,
and VoiceKey's managed cleanup server. An independently hosted cleanup endpoint
is not stopped. The input method and hotkey listener stay registered.

On the next dictation, capture and destination binding begin immediately while
models load in a background thread. The widget shows **Listening · loading models**.
Audio remains in the existing bounded buffer; key release and stop controls still
work. Focus changes retain their audio boundaries during loading. Reload failure
or timeout stops capture and preserves accepted audio for recovery.

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --control free-memory
```

The command acknowledges the request; status reports `unload_pending` and
`models` (`ready`, `loading`, `unloading`, or `unloaded`). Unloading waits for
native calls that are still running after a timeout. The widget reports this
wait; it does not claim memory has been freed. Memory release depends on the
runtime and allocator; model files remain on disk. No automatic idle timer is
currently enabled.

**Disable VoiceKey** runs `systemctl --user stop voicekey.service`. This stops
VoiceKey's capture, drains/preserves pending speech, releases its input method,
and disables its hotkeys. The widget stays available to **Enable VoiceKey** with
`systemctl --user start voicekey.service`; enabling never starts recording.
The widget confirms the service is inactive before labeling it disabled; a lost
socket alone means unavailable. These service commands run only for user actions
and status checks, not as a continuous polling loop.

Stopping applies to this login session; the installed service still starts on
next login. To change that separately, use `systemctl --user disable voicekey.service`.
This control also stops daemon-backed stdout capture. It does not mute the
microphone system-wide or stop standalone diagnostic replay or other applications.

## Recovery and limits

Each accepted recording is saved before transcription under
`~/.local/state/voicekey/sessions/`, using a unique ID. Its `.wav` contains
captured audio; `.jsonl` records stages and delivery attempts; `.txt` is a
readable transcript history. Known text is saved before delivery, including
before a clipboard attempt. `last-recovery.txt` remains a convenience copy;
a later failure does not overwrite the individual records.

Successful audio is removed after text and the outcome are saved, unless a
recording/transcription failure makes the original audio useful for recovery. Unresolved
audio and text remain for manual recovery. Successful text is retained for
up to seven days, and may be removed sooner to make room. The default quota
is 256 MB; unresolved records are never automatically deleted to meet it.
A full or unavailable recovery store disables new recordings until it is
repaired and the daemon restarted. Files are private to the user.

The pipeline admits at most eight recordings for capture/transcription/dictation,
with a separate eight-prompt limit for agent dispatch. It reserves recovery space and audio capacity before
capture (180 seconds by default, including a full `max_seconds` for a new
recording). Full admission refuses the key-down rather than capturing and
then discarding it. Recorder failure and keyboard disconnect preserve the
available samples. Single-recording captures below `tap_seconds` are discarded; a short dictation
tap starts continuous listening, and a short agent tap opens the agent.
A native model that ignores its timeout cannot spawn an unlimited succession
of replacement threads.

SIGTERM and Ctrl-C stop capture, drain pending work for `shutdown_seconds`
(default ten seconds), save unresolved work and invalidate pending targets.
Bounded resource cleanup follows; the systemd service imposes a 30-second
stop limit. On restart, mode is off and old attempts are not automatically
retried. Journaling protects completed writes against a process crash; it
does not promise recovery of an in-memory recording or unflushed data after
power loss. An uncertain delivery remains uncertain even when its text is
safely saved.

## Development

The standalone [AT-SPI probe](../contrib/accessibility/README.md) inspects application
capabilities and tests background insertion in disposable GTK/Firefox fields.
It is experimental and is not enabled as a dictation backend.

Run `python -m unittest discover -q` in the project environment. The suite
uses fake targets and a real paced WAV source, local HTTP servers, and a
private Emacs server; it never types into the current desktop. If Emacs and
Evil are available, editor transactions are also exercised in batch Emacs.
CI installs both. See [the architecture](persistent-mode-architecture.md)
and [the audit](audit-2026-09-05.md) for the persistent-mode prerequisites.

## Agents

Coding agents drive the same desktop — `emacsclient`, `wl-copy`, `wtype`,
compositor actions — and one of them evaluating Lisp in Emacs mid-dictation
can steal focus or the clipboard. From key-down until dictation is delivered
or preserved for recovery,
voicekey holds an exclusive `flock` on `$XDG_RUNTIME_DIR/voicekey/lock`. A
hook that takes a shared lock before such tools run, waits a bounded time
and then refuses with a reason can keep cooperating desktop tools out of the way. The lock
is advisory; voicekey retries acquisition without delaying capture. The
utterance owns the lock through release finalization and all pipeline stages.
Agent prompts release their gate ownership once transcription is queued for
agent dispatch; a busy agent does not lock desktop tools out for minutes.

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

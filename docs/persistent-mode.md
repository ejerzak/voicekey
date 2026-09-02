# Persistent mode: feasibility study and plan

Written 2026-09-02 after a full read of the code base (about 3,000 lines of
source, 130 passing tests) and ten days of journal history (535 recordings).
Nothing here is built yet. Section 1 is what the audit found; section 2 is
the plan for the new mode; section 3 is the order of work.

## 1. Audit

The code is in good shape: the IME generation logic, the gate, the spacing
state machine and the Emacs pinning all hold up under reading, and the test
harness (fake recorder, fake IME, fake stream) extends naturally. What
follows is what needs patching, most important first. The first three are
also prerequisites for persistent mode.

**Status (2026-09-03):** 1.1, 1.2 and 1.3 are done (delivery worker, stop
and transcribe at `max_seconds`, `PendingPin`), as are the `--check`
compositor requirement, the socket glob, and the evil-state question
(normal and visual state now end back in normal, as Escape would). A
Codex cross-audit the same day found and we fixed: spacing lost after a
first dictation that landed nowhere, the insert able to overtake the pin,
an Emacs insert able to start past `max_delay_seconds`, the remote Hermes
working directory expanded against the local home, and the remote terminal
check needing niri even with no terminal wanted. The swap experiment is
still open.

### 1.1 A stalled delivery stalls every dictation behind it

`daemon.py` runs transcription and delivery on one worker thread. A copy
through `wl-copy` may take up to 15 s (`inject.py`), an Emacs insert up to
the rest of the delay budget, a `wtype` up to 15 s. While one of those
hangs, the next recording is not even transcribed, so by the time it is, it
is older than `max_delay_seconds` and is copied too, and that copy hangs
again.

The journal shows exactly this cascade on 2026-08-30 between 01:32:57 and
01:34:29: one focus change led to a copy, `wl-copy` hung for 15 s, and the
next six dictations were each judged stale (12 s, 16 s, 26 s, 31 s old),
copied, and each copy timed out. About ninety seconds of dictation went to
the recovery file. A single `wl-copy` timeout also occurred on 2026-08-29
at 02:00:30. Why `wl-copy` (wl-clipboard 2.2.1) hangs is not established;
an agent's own `wl-copy` ran at the same moment on the 30th.

Fix: transcribe on one thread and deliver on another, so a slow delivery
never delays the next transcription; cut the copy timeout to about 3 s (a
`wl-copy` that has not returned in 3 s will not); keep the recovery-file
save as the terminal fallback. In persistent mode this matters more, since
utterances arrive every few seconds.

### 1.2 `max_seconds` discards a long recording instead of delivering it

`_on_tick` aborts a recording past 90 s on the theory of a stuck key, and
the audio is lost. In ten days the longest recording was 68 s, the 99th
percentile 46 s, and 21 recordings ran over 30 s. A thought that runs past
90 s is not rare for a professor, and a stuck key cannot be told from a
long dictation until release. Deliver what was captured (or cut at the
limit and continue) instead of discarding. Persistent mode needs this gone
anyway.

### 1.3 Key-down setup blocks the keyboard thread

`_prepare` runs on the evdev listener thread: the compositor focus query
(up to 2 s), the Emacs pin (up to 5 s), and at release the live-decoder
flush (3 s) plus the recorder stop (3 s). Audio capture starts first, so
nothing is lost, but while Emacs is busy (garbage collection, a dialog, a
long command) every key-down waits up to 5 s before the live preview
attaches, and the key-up is processed late. Pin on a helper thread and let
delivery wait for the result.

### 1.4 Smaller items

- `--check` requires the `niri` binary unconditionally (`__main__.py`),
  which contradicts the README's sway and Hyprland support; the agent
  terminal check is also niri-only.
- The daemon's model pages get swapped out while idle: 2.2 GB resident with
  0.5 GB in swap at the time of writing, peak swap 2 GB. The first dictation
  after a quiet hour pays the page-in. `MemorySwapMax=0` in the unit is the
  cheap experiment; measure before and after.
- `fix_environment` picks the Wayland socket with a glob that also matches
  `wayland-1.lock`; harmless today, wrong after a compositor crash leaves a
  stale lock file. Filter the suffix.
- Design question, not a bug: inserting in evil normal state runs
  `evil-append` and leaves the buffer in insert state. A person typing `a`
  and the text would press Escape afterwards. Decide whether to restore the
  state.
- The two service crashes on 2026-08-29 were a development bug that no
  longer exists in the code.
- Verified not a bug: Emacs escapes newlines in `emacsclient` results, so
  the character-before-point parse is safe at line starts.

## 2. The plan

### 2.1 What the mode is

A third action, started and stopped with its own key (`persistent_key`,
toggle semantics like the existing toggle keys; chords are supported, so
the laptop can use a Right-Alt combination). While it is on:

- the microphone runs continuously;
- speech is cut into utterances at pauses;
- each utterance is previewed live as preedit, transcribed by the offline
  model at the pause, optionally polished by a language model, and
  committed in place of the preedit;
- text goes only to the window that was focused when the mode was
  switched on. Within that window the cursor is free: text lands wherever
  the cursor is at each commit. When focus leaves the window, dictation
  pauses with a notification and resumes when focus returns.

Provisional versus committed is therefore decided by the pause detector:
provisional text is the live preedit within one utterance, and a commit is
the offline pass at each pause.

### 2.2 Why no rewrite

Today a key-down creates a `Session` and a key-up turns it into a `Job`
that the worker transcribes and delivers. Persistent mode keeps that
pipeline per utterance and only changes what drives it: a pause detector
plays the role of key-up followed by key-down.

```
persistent key ─ pw-record (continuous) ─ 100 ms frames ─┬─ streaming model ─ live text ─ preedit
                                                          │       │ endpoint (pause)
                                                          └─ buffer│
                                                                   ▼
                                     utterance N samples ─ offline model ─ polish (LLM) ─ commit
                                     utterance N+1 begins immediately on the same stream
```

The pause detector is already loaded: sherpa-onnx's online recognizer has
endpoint detection built in (`enable_endpoint_detection`, with three rules:
trailing silence after speech, trailing silence without speech, and a
maximum utterance length) and exposes `is_endpoint` and `reset`. The
installed version also ships a Silero voice-activity detector
(`VoiceActivityDetector`, a 2 MB model) as a fallback if the recognizer's
own endpointing proves unreliable on thinking pauses.

Concrete changes:

- `recorder.py`: a continuous mode with a "cut" that returns the frames
  since the last cut, instead of stop-and-restart.
- `Session`: rolls over to a fresh stream at each endpoint and reports the
  endpoint to the daemon from the decode thread. The offline job for
  utterance N is queued while utterance N+1 records; the job queue already
  allows that.
- `Daemon`: the `persistent` action; `max_seconds` does not apply to the
  whole session, and the endpoint rules cap a single utterance at about
  30 s so the offline pass and memory stay bounded.
- The binding logic in `_prepare` (window, app, IME generation, Emacs pin,
  character before the cursor) becomes a `Binding` object taken once at
  start and refreshed at each utterance, so moving between fields inside
  the same window works. Spacing then works unchanged.
- The gate (the flock agents wait on) is held per utterance, from first
  speech to landing, never for the whole session, or agents would be
  locked out for an hour.
- Config: a `[persistent]` table with `key`, `pause_seconds` (trailing
  silence that ends an utterance, expect to tune between 0.8 and 1.5 s),
  `max_utterance_seconds`, and the focus policy.

Latency to expect: the streaming model works in 560 ms chunks, so the
endpoint fires roughly a second after the pause begins, the offline pass
takes about 5 % of the utterance, and a local polish pass one to two
seconds. Text lands two to four seconds after a sentence ends, with the
raw live text visible in the meantime.

### 2.3 Emacs

The existing delivery (pin the buffer, insert through `emacsclient` with
the gesture of the current evil state, follow point within the buffer)
already gives "start in one section, move to another". Two additions:

- A small `voicekey.el`, shipped in the repo, that records the buffer of
  the last command the user ran through the command loop
  (`post-command-hook`) and registers each inserted region under the
  utterance id with markers. The first gives a target buffer that an
  agent's `emacsclient` evaluation cannot move, because such evaluations
  do not run through the command loop (to be verified in the first
  spike). The second lets a later pass replace an utterance's text in
  place even after the cursor has moved on, and makes "scratch that"
  possible. Without the file loaded, the current pin form keeps working.
- Two or three voice commands matched on the final transcript: "new
  paragraph", "new line", "scratch that". Nothing more clever.

### 2.4 The polish pass

A new module (`polish.py`) with the same backend seam as `backends.py`:
`none`, `ollama`, `anthropic`. It runs between the offline transcript and
delivery, per utterance, with the previous few utterances as read-only
context so sentence boundaries and terminology stay consistent. It
removes fillers, false starts and repetitions, fixes punctuation and
capitalisation, writes a described formula in LaTeX when asked, and
changes nothing else. Guard rails: the raw transcript is delivered
whenever the model errors, times out, or returns text whose length differs
from the input by more than a set fraction, so text is never lost or held
up; raw and polished text both go to `recordings_dir` for review.

Where it runs:

- Desktop, default: Ollama is already running with 9B to 14B instruct
  models on the desktop GPU (12 GB). A model of that class does this task
  well; hold it resident with `keep_alive` during a session, turn thinking
  off, expect one to two seconds per utterance. Measure before choosing.
- Laptop: no GPU, so a 9B model on the CPU is too slow per utterance.
  Route to the desktop's Ollama over Tailscale (the agent path already
  reaches that host) or use the cloud option.
- Cloud, opt-in: the Anthropic API through the official SDK, default
  model `claude-opus-5`. An hour of dictation is on the order of 50k
  input and 10k output tokens, well under a dollar. This breaks the
  README's "nothing leaves the machine" promise, so it must be an explicit
  config choice with a visible indicator, never a fallback.

A paragraph-level pass (rewrite a whole paragraph once it is complete,
using the Emacs markers) is a later step; per-utterance with context gets
most of the value and works in every application.

### 2.5 A stronger speech model

The `faster-whisper` CUDA backend is already implemented and installed in
the venv; on the desktop it is a config change. sherpa-onnx 1.13.6 also
loads NVIDIA Canary, Qwen3-ASR and Whisper. On a single clear speaker the
word-error differences between these and Parakeet are about a point, and
the audible problems in paper dictation are vocabulary (names, terms of
art) and fillers, which the polish pass and a glossary address more
directly. Recommended order: turn on `recordings_dir` for a week to build
a corpus of real dictations, compare backends on it offline, and only then
switch. Two cheap vocabulary levers: a glossary in the polish prompt, and
sherpa-onnx contextual biasing (`hotwords_file`) on the Parakeet
transducer, which needs beam search and must be timed.

### 2.6 Risks and open questions

- Thinking pauses. A philosopher pauses mid-sentence for two seconds. A
  short `pause_seconds` splits sentences; the polish pass sees fragments
  (context helps, the paragraph pass fixes). A long one adds latency to
  every commit. Expect tuning; make it a config.
- Cursor movement during preedit. Applications block cursor movement
  while preedit is showing, so moving around must happen in silence.
  Document it rather than fight it.
- Over-editing by the language model. The strict prompt, the length guard
  and the raw fallback bound the damage; the recordings corpus makes it
  auditable.
- Focus policy versus agents. Pausing when compositor focus leaves the
  window means an agent that moves focus pauses the dictation. Tolerable:
  the lock hook already holds agents while an utterance is in flight.
- Memory. Streams reset per utterance and the offline pass never sees
  more than one utterance, so nothing grows. Ollama adds 6 to 10 GB of
  GPU memory during a session.
- Triple-press on the dictation key is feasible later (taps are already
  discarded, so three within 600 ms is unambiguous), but a dedicated key
  or chord is simpler and zero-ambiguity, so start there.

## 3. Order of work

0. **Patch first**: the three items in 1.1 to 1.3, each with a test
   (the delivery cascade is not covered today). One to two days.
1. **Continuous capture and endpointing**: persistent key, `Recorder` cut,
   `Session` rollover, binding refresh per utterance, focus pause, gate per
   utterance, raw text landing. Test with `--replay` of a long WAV; the
   replay source already streams at real-time pace. Usable on its own.
2. **Emacs**: `voicekey.el` (user target buffer, marker-registered
   insertions), voice commands.
3. **Polish**: `polish.py` with Ollama, glossary, guard rails, latency
   measured on the desktop; then laptop routing and the opt-in cloud
   backend.
4. **Later**: paragraph-level polish in Emacs, backend comparison on the
   recordings corpus, hotwords.

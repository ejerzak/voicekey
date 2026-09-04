# Persistent mode: architecture

Written 2026-09-04, after the plan of 2026-09-02 (`persistent-mode.md`) and a
second full read of the code. It supersedes section 2 of that plan; sections
1 (the audit, done) and 4 (setup dependence) still stand. Nothing here is
built.

**Status (2026-09-05).** Step 6, the polish pass, was built first and on the
existing hold-to-talk pipeline: it does not depend on the mode, and using it
for a while answers whether a language-model pass is worth having before the
segmenter and ledger are built. `polish.py` (OpenAI-compatible backend, the
`s1-mini` and `instruct` formats, the judge, the child `llama-server`), a
polish worker between transcription and delivery, `[polish]` config, tests.
The default model is S1-mini by Superwhisper, a 0.6B normaliser that runs on
the laptop CPU in about half a second per sentence; its Fedora runtime is
the `llama-cpp` package. The Anthropic backend, per-mode profiles and the
prompt file's context window are still to come. Step 1, the target
extraction, was done the same day: `target.py` holds the four targets
(input method, Emacs, wtype, clipboard) behind `bind`, `show`, `before` and
`land`, the focus guard and the reconciliation of provisional text after
focus theft; `daemon.py` keeps spacing, budgets, workers and recovery. Steps
2 to 5 are unchanged.

## 1. What it is

A third mode, switched on and off by its own key. While it is on, the
microphone runs continuously; speech is cut into utterances at pauses; each
utterance passes through three tiers of text, each better and later than the
last:

| tier | source | when | shown as |
|---|---|---|---|
| live | streaming model | while speaking | provisional |
| raw | offline model over the whole utterance | about half a second after the pause | provisional |
| final | polish pass (language model) over raw, with context | one to three seconds later | committed |

Text is committed to the application once, in order, when it is final.
Committed text is never edited in a generic application; in Emacs, with
`voicekey.el` loaded, it can be replaced later (polish that arrived late, a
paragraph pass, "scratch that").

The use is dictating prose for papers, slides and email, so the final tier
is not a transcript: fillers, false starts and repetitions go; punctuation
and capitalisation are fixed; a described formula becomes LaTeX; nothing
else changes.

## 2. Guarantees

Everything below is arranged to keep these eight properties. Each has a
mechanism, and each mechanism gets a test.

1. **Nothing spoken is lost.** Every sample recorded while the mode is on
   belongs to exactly one utterance: cuts fall in silence, and an utterance
   is all the audio between two cuts, silence included. Every utterance's
   text, at every tier, is appended to a session file on disk the moment it
   is known, before delivery is attempted. Whatever the application does
   with the text, the words are on disk.
2. **Nothing lands twice, nothing lands elsewhere.** As today: text goes
   only to the window bound at switch-on (or the pinned Emacs buffer), and a
   commit names its activation generation, which the compositor refuses if
   the field has changed. When it cannot be known whether the application
   kept provisional text, voicekey does not re-type it; it says so and
   points at the session file.
3. **The audio path never blocks.** Capture, cutting and live decoding run
   ahead of everything. A slow offline pass, a hung polish server, a busy
   Emacs or a stuck clipboard delays only the tiers behind it, never the
   microphone or the next cut.
4. **In order.** Utterances commit in the order they were spoken. A later
   one may be transcribed or polished early; it lands after the earlier ones.
5. **Everything bounded.** Utterance length, pending text, the polish wait,
   the drain after switching off, silence before auto-off, absence before
   pause: each has a cap in config, and hitting a cap degrades (raw text
   lands, text is saved, the mode pauses) rather than stalls.
6. **Provisional text is cosmetic.** The live and raw tiers are a rendering
   of the ledger, never the record of it. If the preedit vanishes (focus
   theft, a keystroke, an application quirk) the ledger re-renders it.
   Correctness never depends on what the application did with provisional
   text.
7. **The microphone is never on without saying so, and never turns itself
   on.** State changes are announced; a daemon restart comes up with the
   mode off.
8. **Local by default.** The polish pass is off unless configured. A cloud
   backend is an explicit choice, visible in the status, never a fallback.

## 3. The model

```
persistent key ──▶ Controller ──▶ Session (one per switch-on)
                                     │
   pw-record (continuous) ─frames──▶ Segmenter ── cuts ──▶ Ledger ◀── stages
                              │  (VAD: cut in silence)      │ utterances, states, texts
                              └──▶ LiveDecoder ─ live text ─┘        │
                                                                     │  Transcriber (offline model)
                                                          renders ▼  │  Polisher   (LLM, bounded)
                                                           Target    │  Deliverer  (commits in order)
                                          IME | Emacs | wtype | clipboard
                                                                     │
                                                           session file (always)
```

A **session** is one switch-on to switch-off. It owns a target binding, a
ledger of utterances and a session file.

An **utterance** is the audio between two cuts and the texts derived from
it. Its state only moves forward:

    capturing → cut → transcribed → final → committed | copied | saved | dropped

with `held` as a flag on any pre-terminal state while the target is
temporarily unavailable. `final` means the polish pass returned, or was
skipped, or timed out and the raw text was adopted. `dropped` is the empty
transcript (or "scratch that" spoken alone). `copied` and `saved` are the
"not typed" outcomes of today.

Hold-to-talk becomes a session with exactly one utterance whose cut is the
key release; the toggle keys likewise. One pipeline, one delivery path, one
set of tests.

## 4. Components

### 4.1 Capture (`recorder.py`)

`pw-record` runs for the whole session. The recorder keeps the frames since
the last cut and offers `cut(at_frame)`, which returns them and starts
afresh without stopping the process; `stop()` is the final cut. The replay
source (`voicekey.replay`) already streams at real-time pace, so a long WAV
with pauses drives the whole persistent pipeline in tests.

### 4.2 Segmenter (`segmenter.py`)

Decides where cuts fall. Two implementations behind one interface:

- `KeySegmenter`: one cut at key release (hold and toggle keys), with
  today's tap and `max_seconds` rules.
- `PauseSegmenter`: Silero VAD (a 2 MB model through sherpa-onnx's
  `VoiceActivityDetector`, installed with the others under a digest) on its
  own thread, fed by a queue from the recorder. A cut is emitted once speech
  has been heard since the last cut and `pause_seconds` of silence have
  followed it. The cut point is the middle of that silence, by frame index,
  so no cut ever falls in speech and no sample is dropped: the utterance is
  everything since the previous cut, leading silence included. The offline
  model is indifferent to silence, and this is what makes guarantee 1 a
  one-line invariant. Silence with no speech since the last cut is discarded
  frame by frame, so memory stays bounded through a long pause. An
  utterance past `max_utterance_seconds` is cut at the quietest window of
  its last few seconds, else hard.

Why the VAD and not the streaming recognizer's endpoint rules (the plan's
first choice): the live preview is optional (`streaming.model_dir = ""`) and
segmentation must not be; the VAD's silence threshold is exactly the knob
wanted; it costs about a millisecond per 32 ms of audio; and its boundaries
are sample-exact. The recognizer's `is_endpoint` stays available as a second
implementation of the same interface should the VAD misjudge this
microphone.

### 4.3 Live decoder

As today (`Session._decode`): one streaming stream per utterance, frames
through a bounded queue, a fallen-behind decoder loses the preview and
nothing else. At a cut the stream is finished (its text is the utterance's
last live text) and a fresh one started. A later optimisation, not for v1:
stop feeding silence to the streaming model after the first half second of
a pause, which is most of its idle cost.

### 4.4 Ledger (`ledger.py`)

The session's ordered utterances, their states and texts, under one lock.
Pure Python, no I/O but the session file, and the most tested module. It
answers three questions:

- *What should the target show?* `render()` returns `(head, tail)`: `head`
  is the final text of the oldest uncommitted utterance when it is final,
  else `None`; `tail` is the best-known text of every other uncommitted
  utterance in order (final over raw over live), with the spacing between
  them resolved. The deliverer commits `head` and shows `tail` in one
  request; the live decoder shows `tail` alone.
- *What is pending?* For the gate, the status, and reconciliation: per
  utterance, the text last shown provisionally (`shown`).
- *Context for polish*: the last few final texts.

Every change to an utterance goes through the ledger, which appends a line
to the session file and asks the target to re-render. Renders are posted
under the ledger's lock, so their order is the ledger's order, and
coalesced, so the newest wins (as `notify.py` already does).

**Spacing.** Between utterances of one session it is deterministic: a space
unless the previous final text ends in whitespace or the next begins with
closing punctuation. Between the session's first utterance and what was
already in the field, today's `Spacing` decides (the character before the
cursor when reported, else continuation). A keystroke from the user hands
spacing back to `Spacing`, as today.

**Session file.** `~/.local/state/voicekey/sessions/<timestamp>.txt` holds
the final text, one paragraph per utterance, marked where it was not typed;
`<timestamp>.jsonl` holds every tier with timestamps and outcomes. Both are
written before any delivery attempt. The text file is what to open when
something went wrong; the JSONL is the corpus for tuning the polish prompt,
with `recordings_dir` adding the audio. Hold-to-talk keeps
`last-recovery.txt`.

### 4.5 Stages

Three workers on three queues, today's two plus one:

- **Transcriber**: the offline model over the utterance's samples gives the
  raw text. Unchanged code. If it throws, the live text is adopted as raw.
- **Polisher**: raw text plus context gives the final text, on its own
  worker so a slow model never touches transcription. Each utterance has a
  deadline (`polish.max_wait_seconds`, about 5 s from transcription); past
  it the raw text is adopted as final, and a polish result that arrives
  later is discarded, or in Emacs applied as a replacement. Section 6.
- **Deliverer**: takes the ledger's `render()` and drives the target;
  waits while the target is `held`; records outcomes. All target I/O
  happens on this thread, except provisional updates, which the target
  serialises itself (the IME loop thread; a coalescing helper thread for
  Emacs).

### 4.6 Target (`target.py`)

The binding to where text goes, extracted from today's `_prepare`, the two
preview classes and `_deliver_dictation`. One interface:

    bind()               at switch-on: field, window, app, before-cursor, Emacs buffer
    cut(utterance)       at each cut: what must be captured now (Emacs: a marker at point)
    show(tail)           provisional text; fire-and-forget
    commit(head, tail)   commit head and show tail in one request; landed / refused / unknown
    status()             active / inactive / gone
    reconcile()          on re-activation: what became of the shown text (4.7)

Implementations:

- **IME** (applications speaking text-input-v3): `commit` is one request
  carrying `commit_string(head)` and `set_preedit_string(tail)`, which the
  protocol applies atomically and in that order — how a CJK input method
  commits a word and keeps composing. What the user sees, with provisional
  text in brackets:

      The proof is short. [It follows from lemma two um so we can]      raw N + live N+1
      The proof is short. It follows from Lemma 2. [so we can assume]   N committed; N+1 live

  The generation check is unchanged. One small change in `ime.py`: `_call`
  splits into post-and-wait, so the post happens under the ledger lock and
  the wait outside it.
- **Emacs**: through `emacsclient`, using `voicekey.el` when it is loaded
  (section 5) and today's pin-and-insert forms otherwise. With `voicekey.el`
  there is no IME involvement in Emacs at all: the preview is an overlay
  Emacs draws itself, which cannot be deactivated, and one channel means no
  ordering problem between preedit and insert. Hold-to-talk gets the same.
- **wtype** and **clipboard**: final text only, live text in a
  notification; unchanged.

Emacs is chosen by `app_id` at bind, as today.

### 4.7 Focus, deactivation and reconciliation

The known issue (`known-issues.md`): when focus leaves a field showing
preedit, some applications keep the provisional text and others drop it,
and the input method is told neither. In persistent mode the field shows
provisional text most of the time, so this is handled, not hoped away.

- **Emacs**: unaffected. The buffer is the target and the overlay is
  Emacs's own; focus may go anywhere.
- **IME targets**: on `deactivate` the target is `inactive`; uncommitted
  utterances are `held` and keep accumulating, and the microphone stays on.
  Looking at a PDF while talking is the normal case for an author, and an
  agent that steals focus for two seconds must not mute a sentence. On the
  next activation in the bound window (any field of it), `reconcile()`
  reads the surrounding text the application reports:
  - it ends with the shown tail: the application kept it. Those utterances
    are marked committed as shown (unpolished), the session file says so,
    and a notification counts them. Step 7 of section 8 can replace them in
    place later.
  - it ends with the last committed text, or the tail is gone: dropped. The
    deliverer re-renders and carries on; nothing was lost, since nothing
    had been committed.
  - anything else: the user typed there. Treated as dropped, spacing handed
    back.
  - no surrounding text at all (terminals): unknowable. Utterances whose
    `shown` is non-empty are `saved` (session file, clipboard,
    notification); utterances cut while inactive were never shown and are
    delivered normally.
- **Absence**: unfocused for `unfocused_pause_seconds` (default 120) pauses
  the session: microphone off, held text saved and copied, a notification.
  Focus returning resumes it with a fresh bind. The key always means off.
- **Silence**: no speech for `idle_off_minutes` (default 10) switches the
  mode off with a notification. A forgotten microphone is a privacy problem
  and a battery problem.

`max_delay_seconds` ("older transcripts are copied, never typed late") does
not apply inside a session. The mode is on by the user's choice, so text
lands as long as it stays on and the target is alive, in order, however
late the pipeline runs. Switching off starts a drain (`drain_seconds`,
default 30): the last utterance is cut at once, the pipeline empties, and
anything still pending at the end is saved. Hold-to-talk keeps the staleness
rule.

### 4.8 Controller (`daemon.py`, shrunk)

The key handler and mode state machine that today's `_on_key`, `_start`,
`_finish` and `_on_tick` already are, made explicit:

    IDLE ──hold key down──▶ HOLD ──key up──▶ IDLE
    IDLE ──persistent key──▶ ON ──persistent key──▶ DRAINING ──drained──▶ IDLE
    ON ──window unfocused──▶ HOLDING ──focused──▶ ON
    HOLDING ──unfocused_pause_seconds──▶ PAUSED ──focused──▶ ON (rebind)
    ON | HOLDING | PAUSED ──idle_off_minutes──▶ DRAINING

While the mode is on, the hold and agent keys are ignored and logged. Later
the agent key could mark the current utterance as bound for Hermes instead
of the ledger; the segmenter makes that a small change.

The gate is held per utterance, from first speech to a terminal state. A
session of silence holds nothing, so agents are not locked out for an hour.

### 4.9 Status

Notifications only on state changes and failures: on (naming the target),
holding, paused, off (with a count and the session file). Never per
utterance: "✓ Typed" every five seconds is noise. The indicator is one
replaceable notification without expiry, plus a state file at
`$XDG_RUNTIME_DIR/voicekey/state` (`on emacs 12`, `holding`, `off`) for a
bar widget to poll.

## 5. Emacs: `voicekey.el`

Shipped in the repo, loaded by the user; everything degrades to today's
forms without it. It gives the Emacs target:

- **A session buffer** at switch-on: the buffer of the selected window,
  held by reference for the session, plus the buffer of the last command
  the user ran through the command loop (`post-command-hook`) as the
  "which buffer did the user mean" check that an agent's `emacsclient`
  evaluation cannot move. (To verify in the first spike, as the plan says.)
- **A marker per cut**, planted at point (where `a` would insert in normal
  state) when the segmenter cuts, on a helper thread so the cut never waits
  on Emacs. The utterance lands at its marker, so "finish a sentence, move
  point, start the next" works even though the sentence lands two seconds
  after point moved.
- **An overlay preview**: pending text as an overlay at each utterance's
  marker and live text at point, in a face. Not buffer text, so it survives
  focus, is never committed by accident, and cannot block the cursor.
  Updates are `emacsclient -e` calls on a coalescing helper thread (newest
  wins), so a busy Emacs delays only the preview.
- **A region per commit**: begin and end markers under the utterance id.
  This is what makes `replace(id, text)` exact after point has moved on:
  late polish, "scratch that" (delete the previous region), and the
  paragraph pass.
- **Evil**: the first commit of a session uses today's gesture rules
  (visual selection replaced, normal state ends in normal). Later commits
  insert at markers and leave the state alone. Terminal buffers get the
  text as today, without markers.

Voice commands are matched on the raw text before polish, deterministically,
so they work with polish off: "new paragraph", "new line", "scratch that".
Nothing else.

## 6. Polish (`polish.py`)

An HTTP client with two backends behind one seam, and `none`:

- `openai-compatible`: `/v1/chat/completions`, which is what Ollama,
  llama.cpp's server, vLLM and LM Studio all speak, local or over the
  tailnet. One implementation covers every local option.
- `anthropic`: the Messages API through the official SDK. An explicit
  opt-in; the README's "nothing leaves the machine" gets a stated
  exception; the status shows it.

The prompt lives in a file the user edits (`~/.config/voicekey/polish.md`,
seeded from the repo), with a style slot (`prose` by default; `slides` and
`email` later, by config or a voice command) and a glossary of names and
terms of art, the cheap vocabulary lever. The model gets the previous two or
three final utterances as read-only context and returns only the current
one. The prompt says the utterance may be a fragment cut by a thinking
pause: do not close it with a full stop unless it plainly ends a sentence,
and continue a previous fragment in lower case. The voice commands above are
already applied, and it is told so.

Guard rails, all in the polisher and all tested against a fake backend: a
request timeout; the per-utterance deadline; a length ratio (final within a
set fraction of raw, else raw lands); no empty result for non-empty input.
On any failure the raw text lands and the session file records both.

Where it runs, on this laptop: there is no GPU, and the desktop's Ollama
does not answer over the tailnet today (it listens on localhost; binding it
to the Tailscale address with the firewall limited to that interface, or an
SSH tunnel over the identity the agent path already uses, would fix that).
So the laptop's choices are the desktop when it is reachable, or the cloud.
For a two-second rewrite of one sentence with a few hundred tokens of
context, latency matters more than depth: `claude-sonnet-5` is the model to
try first, not Opus. Measure both before choosing a default.

## 7. Failure modes

| what fails | what happens |
|---|---|
| polish server down or slow | raw text lands at the deadline; session file keeps both texts |
| offline model throws | live text adopted as raw; polish still runs |
| live decoder falls behind | preview off for that utterance; nothing else |
| focus stolen mid-sentence (IME target) | held; reconciled on return; nothing typed twice |
| focus stolen (Emacs) | nothing: the buffer and markers are the target |
| application dropped the preedit | re-rendered; committed once when final |
| application kept the preedit | marked committed-unpolished; counted in a notification |
| unknowable (terminal) | shown text saved and copied, never re-typed |
| Emacs blocked by a dialog | the commit waits its budget, then the utterance is `saved`; the form runs later, so nothing is re-sent |
| session buffer killed | session pauses with a notification |
| daemon restart | mode off; the session file has everything up to the crash |
| user types mid-preedit | the application resets the preedit; the ledger re-renders; spacing handed back |
| user walks away | paused after 2 min unfocused; off after 10 min of silence |

## 8. What changes, in order

Each step leaves the suite green and hold-to-talk unchanged in behaviour;
each is one commit.

1. **Target extraction.** `target.py` from `_prepare`, `ImePreview`,
   `NotifyPreview` and `_deliver_dictation`; the daemon's if-chain becomes
   `target.commit`. A pure refactor: the tests move with the code and their
   assertions do not change.
2. **Ledger.** `Utterance` and `Ledger` replace `Job`, `Session.text` and
   the landing counters; hold-to-talk is a one-utterance session. `ime.py`
   gets post-and-wait.
3. **Continuous capture and the segmenter.** `Recorder.cut`,
   `KeySegmenter` (today's behaviour, now explicit), `PauseSegmenter` with
   the VAD, the live decoder's rollover. Tested with a fake VAD frame by
   frame, and end to end with `--replay` of a WAV with pauses into a fake
   target, asserting the exact sequence of renders.
4. **The mode.** Persistent key, the controller, the per-utterance gate,
   focus policy and reconciliation, the session file, status. Usable on its
   own with raw text landing.
5. **Emacs.** `voicekey.el`, the Emacs target on it, voice commands.
6. **Polish.** `polish.py`, the prompt file, guard rails, both backends;
   latency measured on the desktop and from the laptop.
7. **Later.** In-place replacement of committed-unpolished text in
   applications that report surrounding text (`delete_surrounding_text`,
   verified against the report first, abandoned on any mismatch); the
   paragraph pass in Emacs; the agent key during a session; VAD-gated
   feeding of the streaming model; skipping the offline pass when polish is
   on (the polisher adds punctuation anyway; measure the quality cost);
   backend comparison on the recordings corpus.

Config, under `[persistent]`: `key`, `pause_seconds` (1.0),
`max_utterance_seconds` (30), `when_unfocused` (`hold` | `mute`),
`unfocused_pause_seconds` (120), `idle_off_minutes` (10), `drain_seconds`
(30). Under `[polish]`: `backend` (`none`), `url`, `model`, `api_key_file`,
`timeout_seconds`, `max_wait_seconds`, `prompt_file`, `style`.

## 9. Decisions, and what was set aside

- **Commit once, after polish; never edit committed text in a generic
  application.** The alternative, committing raw text at the pause and
  replacing it with polished text, is exact in Emacs (markers) and only
  plausible elsewhere (`delete_surrounding_text` against a surrounding-text
  report that Chromium windows and terminals omit). One policy for v1; the
  ledger and target seams make early-commit-and-replace a local change if
  the two-second wait in Emacs turns out to matter.
- **Compound preedit** (pending raw text and live text in one provisional
  string) rather than showing only the current utterance. The gradient live,
  raw, committed is what streaming dictation looks like everywhere; the
  protocol supports it in one request; and it hides no ambiguity that the
  one-utterance version would avoid.
- **Hold while unfocused, not mute.** Mute is simpler and was the plan; it
  also mutes silently whenever an agent takes focus, which is the failure
  the known issue describes. With a ledger, holding is the same path as a
  slow polish.
- **VAD over recognizer endpointing**: section 4.2.
- **Emacs preview by overlay, not preedit**: section 4.6.
- **Two extractions before the mode**, revising the plan's "why no
  rewrite". Not a rewrite: the models, the IME connection, the Emacs forms,
  the recorder pump, the gate, the spacing rules and the workers all stay.
  What moves is the orchestration, the one part that would otherwise gain a
  second copy of every branch.
- **No staleness rule inside a session**: section 4.7.
- **Set aside**: a scratch window the user pastes from (sidesteps every
  in-application problem and defeats the point); grabbing the keyboard
  through the input-method protocol to intercept cursor keys during preedit
  (would fix cursor movement, but takes every key from every application
  while the mode is on); pipelining polish requests (measure first); a
  fallback chain of polish backends (timeouts stack, and cloud must never
  be a fallback).

## 10. Open questions

1. Where should the laptop's polish run: the desktop over the tailnet
   (Ollama must be exposed there first) or the cloud? Both will be
   supported; the laptop's default is a choice to make.
2. Should hold-to-talk get the polish pass too, behind a switch that is off
   by default? The pipeline makes it free; the question is whether short
   dictations want it.
3. The `voicekey.el` command-loop check for the user's buffer is worth a
   spike before step 5, to confirm that agent evaluations bypass
   `post-command-hook`.

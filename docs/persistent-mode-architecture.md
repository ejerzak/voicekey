# Persistent mode: architecture and implementation

Updated 2026-09-22 after implementing continuous capture, segmentation,
session previews, ordered delivery and the persistent controller on top of
the [audited foundation](audit-2026-09-05.md). Persistent listening is now
implemented. The earlier feasibility study is historical context.

**Pinned editor interface (2026-09-24).** Persistent capture now depends on
`PinnedEditorTarget` for pin acknowledgement, validity, repeated insertion,
description and release; Emacs's protocol and follow-point behavior are unchanged.
Terminal Neovim implements the second adapter using its existing server socket,
per-instance registration and a Lua extmark. Its preview is virtual text in the
buffer, not Wayland preedit. Plugin focus reports enter the same pause/follow
boundary handling as compositor events, including within a single Ghostty window.
Neovim retains an advancing insertion position rather than following point.
See [implementation and verification](neovim-integration.md) for the terminal
policy, remaining focus uncertainty and testing. Earlier implementation notes
below describe the Emacs-only starting point.

**Destination safety (2026-09-22).** The default is now pause-on-window-switch,
with follow and pin as explicit alternatives. All persistent sessions require
an input-method field or acknowledged Emacs buffer. A one-session typing
exception always pauses on window switches. Switching stops capture but lets
already-recorded Emacs speech finish through its pin, without a focus check.
Field/failure observations are debounced across two polls while delivery still
checks its original activation. Queued focus events invalidate observations.
Window queries without an event stream run at most once per second. Early
binding rejection preserves audio without touching an uninitialized detector;
healthy tail classification always uses padded 512-sample detector windows. The default silence timeout is 60 seconds. See the
[reference](reference.md#continuous-dictation-and-optional-extra-toggle) for current controls and limits;
the implementation history below records earlier decisions.

**Preview decision (2026-09-05).** Use native Wayland preedit in Emacs as well
as other supported applications. Keep the shared path as the default for
persistent mode. The initial writing workflow assumes that pending text can
finish before point moves; buffer-attached overlays, per-utterance anchors and
editing committed regions are optional later work, justified by actual use.

**Silence decision (2026-09-05).** A configurable period without detected
speech turns persistent mode fully off. Resuming requires an explicit start;
speech or a focus change cannot restart the microphone after this timeout.
Measure silence from the audio's last detected speech (or session start before
any speech), independently of transcription and insertion delays. This is a
separate, longer threshold from the pause that ends an utterance. Initial
defaults are 1.2 seconds to end an utterance and 60 seconds to turn listening
off; pending work receives bounded drain and preservation when capture stops.

**Key decision (2026-09-06).** F11 starts and stops a persistent
session. Releasing the key does nothing. Escape retains its normal editing
role. Other bindings are configurable.

## Handoff for the next session

The first working persistent mode is built. Spend the next few days using F11
for ordinary writing and record concrete problems before making more changes.
Keep Nemotron previews and Parakeet final transcription: the benchmark favors
speed over modest RAM savings. Measure other hardware separately.
The older `persistent-mode.md` and the audit's original work order are
historical context.

The implementation passes **247 Python tests**, including a batch run of
**17 Emacs editor tests** and a private-server binding test:

```sh
~/.local/share/voicekey/venv/bin/python -m unittest discover -q
```

The suite needs localhost sockets. Editor checks use batch/private Emacs,
not the user's live server. Existing hold/toggle keys retain their behavior;
the dedicated `[persistent] key` enables continuous listening. Quick dictation
skips polish below eight words; persistent mode defaults to polishing every
meaningful utterance and dropping filler-only utterances with raw text retained.

The optional continuous toggle requires a reserved compositor key and the
checksum-verified Silero model. The service starts with capture off.

The implementation entry points are:

- `recorder.py`: `AudioBuffer` retains bounded PCM with absolute sample
  indices; `CutRecording` passes an extracted utterance to the existing finalizer.
- `segment.py`: Silero classification, separate pause/idle thresholds,
  retrospective cuts, pre-roll and exact maximum-length boundaries. The
  detector is independent of the optional live recognizer.
- `persistent.py`: one continuous capture owner, decoder rollover with suffix
  replay, admission before cuts, silence disposal, stop and bounded drain.
  Each capture reservation includes four seconds for processing lag. Capture
  starts before binding, pin acknowledgement, journal I/O and VAD setup; the
  bounded buffer retains speech during setup. Failed binding aborts capture
  and drops that admission without transcription. Focus polling runs on the
  capture worker, once a second for wtype and every 200 ms for in-memory
  checks. After a cut, live decoding attaches once the
  cancelled decoder exits and replays from the new utterance's start.
- `ledger.py` and `pipeline.py`: session IDs and utterance order, per-utterance
  gate ownership, ordinary FIFO processing, and per-session expiry at stop.
  Persistent queue age does not trigger quick dictation's insertion deadline;
  each individual worker/delivery operation remains bounded.
- `session_target.py`: one session preview renders every pending utterance at
  its best tier. Delivery attempts remain single-use. Generic commits carry
  the newer provisional tail in the same protocol transaction. Emacs clears
  the preview, inserts through its retained pin, then renders the current tail.
- `voicekey.el`: protocol version 3 supports retaining and explicitly releasing
  a session pin, while each insertion keeps its own expiring operation ID. A
  pin names the focused window's process, so a second Emacs process is refused,
  and answers with a description of the bound buffer for logs and the journal.
- `daemon.py` and `config.py`: dedicated toggle binding, microphone status and
  conservative focus/failure handling. Pauses stop capture and require a new
  explicit start; there is no automatic resume in this first implementation.

A real-model replay on the desktop processed 19.87 seconds of packaged speech
and silence in 19.93 seconds, producing two ordered commits and 27 preview
updates. Commits arrived about 1.05 and 0.98 seconds after their audio cuts.
The isolated process peaked at about 1.74 GiB RSS and used 50.0 CPU-seconds
during replay. This used the actual Silero, Parakeet and streaming models,
an isolated destination, and no polish; it is not a live compositor test or
a measurement of the user's dictation quality. The replay accounted for all
317,920 samples as utterance audio or explicitly discarded silence.

## What is built

`daemon.py` handles keys, capture ownership and resource lifetime.
`capture.py` owns one optional live decoder. `ledger.py` owns an admitted
utterance's lifecycle, revision, audio reservation, gate ownership and delivery
attempt. `pipeline.py` runs bounded FIFO stages, journals before side effects,
and supervises fallible calls through `work.py`. The four delivery targets
share explicit capabilities and an outcome enum. Speech and polish adapters
remain independent of mode control.

Hold-to-talk and the existing toggle keys use the same path:

    admit → capturing → finalizing → transcribing → polishing → ready
                                                        → delivering → terminal

An ID is unique to the recording; in hold-to-talk, one session is one
utterance. Admission is bounded across capture, transcription and dictation;
agent dispatch has a separate bounded backlog so it cannot exhaust dictation
slots. Every dictation traverses the polish queue, including ones that skip
the model, so a short dictation cannot overtake an earlier long one. The default
quick-mode polish threshold is eight words; `min_words = 0` disables that
threshold. Persistent mode uses its own `polish_min_words`, default zero.
Its configurable filler-only rule produces a journalled empty final tier and
a queued drop, with no insertion attempt. Preview rendering respects that
empty final tier instead of falling back to the raw filler. Empty model replies
for content-bearing input remain rejected.

The ledger does no I/O. It returns immutable snapshots under a short lock;
workers perform journal and target operations outside that lock. A delivery
operation is reserved exactly once before its intent is journalled. Late
results cannot transition a terminal record. Completed in-memory history is
bounded. Audio is released after its last consumer, and a timed-out native
call retains one bounded slot rather than creating replacement threads.

## Delivery evidence and destination identity

| Target | Identity | Evidence after a successful call |
|---|---|---|
| Emacs | A promptly acknowledged buffer pin | Editor confirmed the operation ID |
| IME | One activation generation | Requests flushed to the compositor |
| wtype | A checked window | The typing process completed |
| Clipboard | No insertion destination | Clipboard command completed |

An IME activation is not a field handle that survives deactivation. A new
activation in the same window may belong to another field. Generic delivery
therefore refuses a changed activation and preserves/copies final text.
It does not infer that missing provisional text was dropped, or automatically
delete surrounding text after focus returns. The low-level replacement helper
requires the exact checked generation and surrounding-text revision and
refuses a mismatch at execution.

Bound, submitted, confirmed and saved are different facts. Outcomes are
`refused`, `submitted`, `confirmed`, `unknown`, `copied`, `saved` and `dropped`;
unexpected infrastructure failures are reported separately. In particular,
submitting a generic IME request does not acknowledge application insertion.
A timeout or partial failure is saved as uncertain, with no automatic retry or
clipboard fallback. The journal is recovery evidence, not proof that an
outstanding application operation cannot still execute.

The generic window check cannot atomically prevent focus changing during
`wtype`. Buffer pin acquisition is also asynchronous: without editor tracking,
it names the selected buffer when Emacs handles a request within 250 ms.
Voicekey does not claim those mechanisms guarantee an atomic key-down snapshot.

## Emacs transaction groundwork

`voicekey/voicekey.el` is packaged and loaded on demand. Loading it installs
no hooks. Pinning must receive a timely acknowledgement; failed/late pins do
not authorize insertion. Insertion carries a unique operation ID, an expiry
checked inside Emacs, and a revocable permission file. Repeated operations
return the previous result. Definite pre-mutation refusals differ from errors
after mutation began. Buffer edits are atomic; terminal writes cannot be.
Spacing is computed at the actual gesture position. Hold-to-talk follows point
within the pinned buffer, retaining its existing gesture semantics.

Emacs uses the shared Wayland preview, falling back to notifications when no
usable activation is available. Its buffer pin starts before waiting for the
IME binding. Before final insertion, its preview is closed to new updates and
the clear request is flushed within the remaining delivery budget. A stale
activation or a newer preview owner is left alone. Failed cleanup cannot
start an editor insertion. These are ordered local submissions through two
channels, not an atomic application transaction. The optional
`voicekey-tracking-mode`, enabled explicitly by the user, records the last
command-loop buffer and marker. A private-server test verifies that an
`emacsclient` evaluation which switches buffers does not run `post-command-hook`
and therefore does not redirect this tracking state. Batch tests exercise
normal, insert and visual state, operator/block refusal, narrowing, killed and
read-only buffers, cancellation, expiry, duplicate operations and hook failure.

**Buffer and point decision (reaffirmed 2026-09-05).** The persistent session
keeps its original Emacs buffer binding and follows point within that buffer.
For example: start in section 2, switch to a PDF, and continue dictating into
section 2 in the background. Return to the bound buffer and click in section
5; subsequent commits follow point there without restarting the session.
Selecting another application or another Emacs buffer does not retarget the
session. Background insertion must leave the user's selected window and
buffer alone after the operation.

Insertion uses point when each commit executes, as hold-to-talk does. The
existing workflow assumes the user lets pending text finish before moving
point: queued text is not anchored to the position where it was spoken.
Per-utterance markers are not a prerequisite.
If later editing needs them, a marker planted by a helper records point when
that helper executes, not at an earlier audio cut. The existing marker spike
verifies that insertion-type `t` preserves order at a shared position. Exact
region replacement would also need fixed start/advancing end markers and
validation against intervening edits.

## Recovery, deadlines and stopping

A unique `.wav` is written before transcription. JSONL and readable text
records retain known tiers and delivery intent before insertion or copying.
Atomic audio publication and private files protect completed writes against a
process crash. Capturing audio still in memory, and filesystem writes not
flushed to durable storage before power loss, are outside that guarantee.

Successful audio is removed after the final text and outcome are recorded.
Unresolved audio/text remain until manually recovered or removed. Successful
text is retained up to `history_days`, subject to the quota. If preservation
cannot proceed, new capture is disabled with an error; the pipeline does not
silently continue on an assumption that recovery succeeded.

At persistent session close, undelivered utterances are collected in sequence
order into `<session-id>.recovery.txt`, with a copy in `last-recovery.txt`.
Uncertain deliveries are labelled for inspection; entries without text point
to their retained audio. The session journal indexes the utterance records,
including captures preserved at drain expiry. Sessions with recovery remain
outside automatic successful-history pruning.
The final notification includes the consolidated path. Unreadable JSON lines
produce explicit inspection notices in the summary without blocking recovery
of other utterances or disabling new dictation; source records remain intact.

Before workers start, interrupted session indexes without `session-closed`
are consolidated in the same way and their recovery paths are reported.
An attempt without a recorded outcome is labelled uncertain and its permit
is revoked; recovery never retries delivery or transcription. A readable
close marker prevents repeat recovery on later starts, even when the crash
left a truncated line. Only durably indexed records can be reconstructed.

Hold-to-talk uses an absolute insertion deadline from key release, including
finalization and all queues. Transcription and polish additionally have their
own bounds. The polish deadline starts at transcription completion. A
supervisor adopts raw text at expiry while a stuck request retains its slot;
late results are discarded. Clipboard, IME, editor and agent operations have
bounded waits. A slow side effect is classified as uncertain rather than
assumed not to have happened.

The utterance retains gate ownership through finalization and delivery or
recovery. Admission reserves count and audio capacity before the microphone
starts. A contested advisory lock is retried without blocking capture. Agent
work releases its desktop gate after text is queued for dispatch, while still
counting against its separate backlog limit.

Shutdown stops accepting recordings, signals capture to stop, and drains
within the configured period. Remaining sources are stopped, recovery is
preserved, pending target permissions are revoked, and owned resources are
closed with bounded cleanup. New sessions get new IDs. Old uncertain attempts
are never automatically replayed. Agent commands also check the operation's
cancellation/deadline before each external command.

## Persistent mode contract

The user explicitly enables a continuous session with a dedicated key.
VAD-driven pauses create utterances that use the same lifecycle and delivery
path as hold-to-talk. VAD remains independent of the optional streaming model.
Commit once in order after finalization/polish; an editor with verified region
handles may support later replacement as a separate operation.

The additional contracts are:

1. **Session identity and authority.** Every event carries session ID,
   utterance ID and revision. Stop/drain revokes authority for later updates.
   Mode state is separate from target availability and stage progress. A
   pinned Emacs buffer remains available when another application gains focus.
   The session follows point movement within that buffer; switching to a
   different buffer does not change the bound destination.
2. **Sample ownership.** Cuts use absolute sample indices. Account for samples
   assigned to utterances and explicitly discarded no-speech silence. A
   retrospective pause cut needs retained suffix frames so the new live
   decoder receives samples already seen by the old decoder. Forced maximum
   cuts can split speech; they must not claim to occur only in silence.
3. **Rendering versus operations.** A preview snapshot contains every pending
   utterance at its best-known tier, including a final utterance waiting for
   its commit. Preview updates may be coalesced; insertion commands may not.
   Reserve an attempt before posting it. IME commit can carry the current
   provisional tail in the same request, preserving newer live text.
4. **Journal ordering.** Ordered events are written outside ledger locks.
   Delivery waits for the relevant journal acknowledgement. Bound outstanding
   journal events, pending text and retained audio. Failed storage or sustained
   overload pauses capture and preserves what is available.
5. **Unavailable targets.** Hold pending text while useful. Generic
   reactivation requires deliberate rebinding; it does not prove the old field
   or its provisional text survived. Unknown preedit retention remains unknown.
   A verified editor anchor supports continuing while focus moves elsewhere.
6. **Microphone policy.** Distinguish armed/capturing, holding, paused and off.
   Automatic resume is permitted only while the user has left the session
   armed, and is announced. Explicit off, the configurable no-speech timeout
   and daemon restart require an explicit start before capture can resume.
   Measure the no-speech timeout from detected speech, not text insertion.
   Target absence and no-speech limits are independent.
7. **Voice commands.** Deterministic matching precedes polish. Newline and
   paragraph insertion can use ordinary text. `scratch that` requires verified
   replacement capability; unsupported targets must report that limitation
   without deleting guessed text.
8. **Bounds and drain.** Set utterance length, pending count, audio seconds,
   pending text, silence/absence limits and drain deadline. A persistent session
   may choose a different insertion-age policy, but worker and memory limits
   remain finite. Preserve pending work at stop; saved and uncertain are not
   interchangeable outcomes.

## Next work

The [September 6 cleanup and model review](model-review-2026-09-06.md) records
the first live-test findings, the F11 cleanup changes and the promising shared
speech-model option. The requested-stop failure classification found in the
live session is repaired with a synthetic-process regression test.

The [shared-Parakeet benchmark](asr-benchmark-2026-09-06.md) is also complete.
The user accepted retaining the current Nemotron/Parakeet pair: the tested
Parakeet streaming and memory-sharing alternatives were slower, and speed
matters more than these RAM savings on their 32 GB machines. Benchmark
artifacts are isolated from production; no speech-model refactor was deployed.

1. Exercise F11 with actual paper dictation, including background Emacs
   insertion while reading a PDF and moving point after pending text lands.
   Tune pause/silence thresholds and measure latency with polish enabled.
2. Configure a dedicated laptop chord and measure its CPU, memory, latency and
   speech boundaries. The desktop replay does not establish laptop behavior.
3. Revisit editor overlays if preview continuity across focus changes matters.
   Native previews stay bound to their original activation; notification
   previews take over for the rest of an Emacs session after deactivation.
4. Add voice editing commands, per-utterance anchors, additional polish
   providers or paragraph rewriting only as actual use demonstrates need.
5. Under consideration, not built (2026-10-03): hold back each utterance's
   final `.`, `?` or `!` until the next utterance's cleanup has seen both
   sides of the pause. A 1.2 s pause often splits a sentence and the model
   ends the fragment with a full stop ("cut this kind of repetitive." /
   "Summary of the method…"), which ordinary insertion cannot withdraw. The held mark
   would be inserted at session end if there was no keyboard activity
   since, and dropped if the destination changed. Cost: the last mark
   appears only when speech resumes or the session ends, and delivery gains
   a session-end insertion. Draft mode already gets this effect from
   `revise_end`. In the 30 recorded context requests, 2 needed it.

The deterministic suite covers continuous paced WAV capture, sample ownership,
hard cuts and suffix replay, ordered commits and combined previews, idle gate
release, silence shutoff, overload, dead targets, VAD timeout, stop during drain
and late-result suppression. Batch Emacs exercises repeated insertion through
one pin, background delivery and cursor movement within the pinned buffer.
CI runs the Python suite and batch/private-server Emacs tests. Real desktop
behavior and model accuracy remain separate integration measurements.

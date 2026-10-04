# Persistent drafts (experimental)

Draft mode is opt-in. Ordinary persistent dictation still inserts each finished
utterance by default. Use **Draft mode: off/on** in the DMS widget to switch
between modes while idle, without restarting or loading models. The toggle is
disabled while recording, processing, or reviewing a draft. Accept or discard
that session first. The choice lasts until VoiceKey restarts, like the panel's
destination choices. Ordinary sessions use the selected destination policy.

For a permanent default, set these in your existing section in
`~/.config/voicekey/config.toml`, then restart `voicekey.service`:

```toml
[persistent]
draft = true
draft_cancel_key = "KEY_ESC"
```

After upgrading, restart the daemon and reload the DMS widget to expose the
toggle. The command-line equivalents are `python -m voicekey --control draft-on`
and `--control draft-off`.

The setting means **use drafts where supported**: editable Emacs and Neovim
buffers with a reliable original window identity. Browser fields, ordinary
terminals, explicitly enabled simulated typing, and unsupported editor buffers
(such as Emacs term/vterm and minibuffers) use ordinary incremental dictation.
Ordinary safeguards still apply: simulated typing requires explicit permission,
read-only buffers remain protected, and a refused editor binding never becomes
typing into its surrounding terminal. RPC timeouts and uncertain bindings do
not trigger fallback. The preference stays enabled for the next session.
Restart Neovim after updating its plugin; Emacs loads the updated integration on demand.
The Neovim `:VoiceKey` single-shot command keeps its existing behavior.

Ordinary fallback preserves tap-versus-hold behavior, including a release while
the destination is still being bound. The widget shows the actual session mode
separately from the draft preference.

## Using a draft

- Press the normal dictation key to start. In an actual draft session it is always a toggle,
  even if you hold it down: releasing the key does not stop recording.
- Text appears as a virtual preview in the original buffer. Emacs draws it at
  the insertion point, with the cursor after it; Neovim uses wrapped virtual
  lines below it. Previews do not modify buffer text, the undo history, or the
  file on disk.
- From Evil or Neovim normal mode, dictation switches to insert mode for the
  session (as `a` does) and back to normal mode when the draft is accepted or
  discarded, unless you changed mode yourself meanwhile.
- Press the dictation key again to finish processing and insert the whole draft
  once, at its original anchor. Ordinary edits before the anchor move it with
  the surrounding text. Acceptance creates one buffer insertion/undo step.
- Press Escape to discard the entire draft, including work still being
  transcribed or cleaned up. Escape also reaches the editor, where it leaves
  insert mode as usual. To use a different chord, set, for example,
  `draft_cancel_key = "KEY_LEFTCTRL+KEY_ESC"`.
- The dictation and cancellation hotkeys act only while the draft's original
  window is focused (and, for terminal Neovim, its original instance reports
  focus). Pressing either elsewhere shows a reminder without accepting or
  discarding anything. If focus cannot be verified, the draft stays pending:
  retry in its original window or use the widget controls. Unknown focus never
  authorizes Escape.
- The DMS panel offers **Accept draft** and **Discard draft**. The equivalents
  are `python -m voicekey --control stop` and `--control cancel`. These explicit
  actions can operate from another window.

Window switches, silence timeout, and **Free memory** stop the microphone but
leave the draft awaiting acceptance or cancellation. Return to the original
window to accept by hotkey, or use the widget. Drafts always stay at one
destination and pause on window switches, regardless of `destination_policy`.
You can accept a prepared draft after unloading the models.

The preview is not directly editable. You can edit other buffer text, but must
accept the draft before editing its words. Typing at the insertion point goes
into the buffer before the draft, and the draft lands after it. Normal editor
undo remains available after acceptance. Escape cannot roll back an insertion
already performed.

Acceptance waits for pending chunks to be transcribed; the daemon's shutdown
timeout does not expire a healthy draft backlog. Cleanup of chunks still queued
at acceptance shares one budget of `polish.max_wait_seconds`; after that they
join the draft uncleaned. Cancellation and daemon shutdown still have bounded
drain times. A transient preview timeout does not revoke the draft.

A definite insertion refusal (for example, a read-only buffer) retains the
draft and its anchor. Fix the destination and explicitly accept again, or
discard/recover the text. Word overrides and the hook are not run again for a
retry. Uncertain delivery is never retried automatically.

## Cleanup and limits

The existing `[polish]` backend and style apply; draft mode does not enable an
LLM or change its endpoint. Each chunk is cleaned once, exactly as in ordinary
persistent mode, with the preceding draft text as read-only context
(`persistent.polish_context`). Earlier text is never revised, so the preview is
stable and cleanup costs the same as ordinary dictation. Unlike ordinary mode,
typing elsewhere does not invalidate that context: the draft is not buffer text.
One exception: because nothing is inserted yet, the punctuation where a pause
split the text may change: the model's choice replaces the final marks of the
previous text (`.` to `,` or nothing to rejoin a sentence, nothing to `.` to end
one). The model's other edits to earlier text are discarded, as in ordinary
mode.

With cleanup disabled, the draft uses the raw transcript. Model errors and
rejected responses append the new chunk's raw text. Cleanup is subject to the
existing model's limits and cannot guarantee semantic equivalence. Word
overrides and the transcription hook run once when accepting the complete
draft, so the inserted text can differ from the preview where they apply. There
is no whole-document editorial pass in this version.

Drafts are limited to 32 KB of prepared UTF-8 text. Reaching the limit stops the
session and preserves the accumulated draft and overflow speech for recovery;
it does not insert a partial draft. Existing recovery-storage and pending-work
limits also apply. Audio is processed in bounded chunks and retained on disk
until the draft is accepted or cancelled.

## Recovery

Every prepared revision is journalled before it becomes visible. Recovery uses
the latest complete draft plus any later unfinished chunks, rather than joining
overlapping revisions. A daemon shutdown preserves the draft without accepting
it. An interrupted or refused insertion is recoverable through the normal
history and session recovery files; uncertain delivery is explicitly labelled.

`python -m voicekey --last` shows the latest prepared draft as a whole, excluding
internal chunk revisions. `--copy-last` copies that prepared text for manual
recovery. If processing was interrupted or hit a limit, inspect the session's
`.recovery.txt` as well: it can include additional chunks or references to audio
that had not yet entered the prepared draft.

Discarding (Escape, **Discard draft**, or `--control cancel`) prevents
insertion and removes the preview, but an accidental discard is recoverable:
the prepared text is written to `~/.local/state/voicekey/last-recovery.txt`
and remains the latest dictation, so `python -m voicekey --copy-last` puts it
on the clipboard. Chunks still being transcribed at that moment are dropped.
Retained history expires after `history_days`; discarding is not a
secure-erasure operation.

If any chunk fails to be processed, the draft can no longer be accepted as a
whole: recording stops and the draft plus every chunk after the last one it
contains are written to the session's `.recovery.txt`.

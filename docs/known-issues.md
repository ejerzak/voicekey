# Known issues

## Dictated control characters can trigger unintended actions

**Status:** delivery-boundary mitigation implemented (2026-09-21). Restart
the service to load it. Per-app formatting permissions are empty by default;
checking and enabling additional GUI composers remains a separate task.

Ordinary dictation must not accidentally send an unfinished message or execute
a terminal command. Previously, `inject.type_text` passed the prepared transcript
unchanged to `wtype`, which explicitly maps newlines to Return keypresses
([upstream source](https://github.com/atx/wtype/blob/master/main.c#L150-L175)).
Paragraph breaks introduced by transcription, polish, word overrides, or hooks
can therefore become submission keystrokes. Model instructions are not an
enforcement boundary. A separate investigation reported three polished
transcripts containing paragraph breaks; their `submitted` outcomes indicate
delivery, not proof that a message was sent or a command executed.

Input-method delivery commits text without generating Enter key events, avoiding
that specific mechanism in GUI composers. It is not universally safe: terminals
can interpret newline/control bytes as commands. Clipboard paste is also
application-dependent; bracketed paste can protect shell editing, but requires
terminal/application cooperation ([Bash documentation](https://www.gnu.org/software/bash/manual/html_node/Readline-Init-File-Syntax.html),
[Ghostty paste protection](https://ghostty.org/docs/config/reference#clipboard-paste-protection)).
Emacs's ordinary pinned-buffer insertion inserts text rather than invoking Return;
its `term` and `vterm` paths send process input and need terminal filtering.
The agent key types into the Hermes terminal through the same input-method path.

Observed delivery records included input-method insertion for Signal and Ghostty,
both input-method and `wtype` delivery for Firefox, and buffer insertion for Emacs.
These are observations, not permanent app classifications: availability and
focus determine the chosen path.

Implemented policy:

- Ordinary Emacs buffers preserve paragraphs and tabs. Emacs checks the current
  mode inside the insertion transaction; `term` and `vterm` flatten formatting.
- `wtype` always flattens line breaks and tabs, including existing configurations.
- Input-method delivery preserves formatting only for exact app IDs listed in
  `[dictation] multiline_apps`, and only while the field reports ordinary
  multiline input. Unknown/single-line fields and terminal purpose are restricted.
  Content-type updates are applied on Wayland `done` and checked again at delivery.
- All automatic insertion paths refuse other C0/C1 controls before sending text.
  Shared backend enforcement covers both single recordings and continuous batches,
  after polish, overrides, and hooks. A refusal does not retry the insertion.
- In-field previews remain enabled and follow the generic formatting policy;
  unsafe previews are cleared with a diagnostic identifying the control code.
  A restrictive hint change also flattens an already-visible multiline preview
  without overwriting a newer queued preview. Emacs's Wayland previews remain
  single-line while its ordinary buffer insertion preserves final formatting.
- Original transcripts remain in the journal/recovery. Clipboard recovery retains
  formatting for manual use, without automatic paste.

The Python backends share `delivery.prepare`; Emacs mirrors the small policy in
its insertion transaction. Controlled tests cover control characters, formatting,
queued field changes, preview tails, continuous batches, and terminal mode changes.
Follow-up checks cover adjacent-space flattening, preview hint downgrades, and
NUL refusal before starting `emacsclient` (a definite refusal, not uncertain delivery).

Remaining limits: field hints are application claims. Do not add an app to the
formatting list without checking its behavior; a browser entry also trusts web
terminals that advertise ordinary multiline fields. Unclassified destinations
lose automatic paragraph formatting. Manual paste still needs care in terminals,
and arbitrary applications can act on ordinary characters. Automatic clipboard
paste and stronger field-specific integrations remain possible future work.

## Headed Playwright MCP work steals compositor focus

**Status:** open on the Playwright side (observed 2026-09-04). Following
the 2026-09-05 audit, voicekey no longer treats a new activation in the same
window as proof of the original field. It preserves/copies the final transcript
and leaves potentially retained provisional text alone. Emacs uses an
acknowledged buffer pin and an expiring insertion operation. See
[audit-2026-09-05.md](audit-2026-09-05.md) and
[persistent-mode-architecture.md](persistent-mode-architecture.md).

Background LLM tasks using Playwright MCP regularly move compositor focus to
the headed browser even though the task does not need keyboard focus. This is
itself undesirable: automation should be able to manipulate a visible,
interactive browser without disrupting the application the user is currently
using. Headless mode is not an acceptable workaround because the user still
needs to log in and manipulate the browser manually.

One visible consequence is damaged Voicekey dictation when focus is stolen
between key-down and key-up. Applications handle the resulting deactivated
preedit differently: some appear to commit the provisional transcript, while
others discard it entirely. The final transcript then cannot reliably replace
the provisional text in its original field.

Preferred fix: stop Playwright MCP (or its headed-browser integration) from
requesting compositor focus during background operations while leaving the
browser visible and manually usable. Investigate whether the focus request
comes from browser creation, page or popup creation, or an explicit
`bringToFront`-style operation, and whether Playwright MCP can reuse an
existing visible browser without activating its windows. A compositor rule
that declines activation from this browser/profile may be a fallback.

Voicekey's defensive behavior is now implemented: an acknowledged Emacs buffer
pin survives compositor focus changes; generic delivery stays bound to its
original Wayland activation. A later activation cannot prove field identity,
so generic final text is preserved/copied instead of automatically rebound.
Emacs still uses shared Wayland previews, which can deactivate independently
of its pinned final insertion. Preventing the original focus theft remains
the outstanding Playwright/compositor work.

## Emacs dictation appears lost after read-only buffer refusal

**Status:** root cause found and fixed in voicekey. Restart the service to
load the fix.

Dictation in Emacs appeared to disappear instead of being inserted.
Recording and transcription succeeded, but Emacs refused final insertion
because the pinned buffer was read-only. Voicekey copied the final transcript
to the clipboard and preserved it in the recovery files. The fallback was
not apparent to the user.

Cause: the dictation was aimed at a second Emacs process, but voicekey pins
buffers through `emacsclient`, which reaches only the server process. The
pin therefore bound the server's selected buffer instead of the intended
buffer. The read-only guard prevented insertion into the wrong process;
with a writable buffer selected there, the text could have been inserted
silently into the other Emacs. Neither focus theft nor a transcription
failure was involved.

Fix: the focused window's process ID, which niri, sway and Hyprland report,
travels with the pin request, and `voicekey--pin` refuses a window owned by
another Emacs process. The pin acknowledgement now describes
the bound buffer (name, major mode, read-only state, Evil state); the daemon
logs it and journals it with each delivery attempt, and the read-only refusal
names the buffer. A refused insertion that falls back to the clipboard is
announced with a critical notification that persists until dismissed. The
read-only insertion guard is unchanged.

Remaining: a second Emacs process is refused rather than served. Open new
frames from the server Emacs (`emacsclient -c`) instead of launching another
`emacs`. Under a compositor that reports no process ID the check does not
apply, and the pin binds the server's selected buffer as before.

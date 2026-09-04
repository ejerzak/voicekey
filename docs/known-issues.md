# Known issues

## Headed Playwright MCP work steals compositor focus

**Status:** open on the Playwright side (observed 2026-09-04). The voicekey
side below was done on 2026-09-05: delivery waits, within
`max_delay_seconds`, for the dictation's window to be focused again and
commits into the field's new activation, replacing provisional text the
application kept right before the cursor, leaving alone (and copying the
final text instead) provisional text it kept elsewhere, and trusting a
field that reports no surrounding text to have dropped it. Emacs was
already immune (pinned buffer). What remains is on the Playwright side.

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

Voicekey should also be more defensive: key-down should pin the target for the
lifetime of the gesture, keeping the dictation attached to it through key-up
and final delivery regardless of intervening compositor focus changes.

Likely constraint: the generic Wayland input-method path identifies the target
by an activation generation, which becomes stale when focus leaves the field.
Investigate whether the target can be kept alive across deactivation; if not,
the safe fix may require a target-specific pinned insertion mechanism like the
one already used for Emacs.

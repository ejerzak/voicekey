"""Live text in the focused field through Wayland's input-method protocol.

The daemon registers as the compositor's input method (zwp_input_method_v2).
When an application focuses a text field that speaks text-input-v3, the
compositor *activates* us; we may then show preedit text — provisional, drawn
inline by the application, never inserted — and finally commit a string, which
replaces the preedit in place. Applications without text-input support never
activate us; callers check ``activation()`` and fall back to notifications
and wtype.

One connection, one thread. ``preedit`` is fire-and-forget, ``commit`` waits.
Every request names the activation *generation* it belongs to, so text never
lands in a field that gained focus after the recording started. If the
connection dies the object turns itself off: ``activation()`` is None and
requests are dropped, so the daemon degrades to notifications and typing."""

from __future__ import annotations

import logging
import os
import queue
import select
import threading

log = logging.getLogger("voicekey.ime")

CALL_TIMEOUT = 2.0  # seconds to wait for the loop thread before giving up

EVENTS = (
    "activate", "deactivate", "surrounding_text", "text_change_cause",
    "content_type", "done", "unavailable",
)


class ImeUnavailable(Exception):
    """No Wayland display, no input-method support, or another IME is bound."""


class InputMethod:
    def __init__(self) -> None:
        try:
            from pywayland.client import Display
            from pywayland.protocol.wayland import WlSeat

            from ._input_method_v2 import ZwpInputMethodManagerV2
        except ImportError as exc:
            raise ImeUnavailable(f"pywayland is not installed: {exc}")
        self._reset()
        try:
            self._display = Display()
            self._display.connect()
        except Exception as exc:
            raise ImeUnavailable(f"cannot connect to the Wayland display: {exc}")
        self._seat = self._manager = None

        def on_global(registry, name, interface, version):
            if interface == "wl_seat" and self._seat is None:
                self._seat = registry.bind(name, WlSeat, min(version, 7))
            elif interface == "zwp_input_method_manager_v2":
                self._manager = registry.bind(name, ZwpInputMethodManagerV2, 1)

        registry = self._display.get_registry()
        registry.dispatcher["global"] = on_global
        self._display.roundtrip()
        if self._seat is None or self._manager is None:
            self._display.disconnect()
            raise ImeUnavailable("the compositor does not offer zwp_input_method_v2")
        self._im = None
        if not self._bind():
            self._display.disconnect()
            raise ImeUnavailable("another input method is already bound")
        self._thread = threading.Thread(target=self._run, name="ime", daemon=True)
        self._thread.start()

    def _reset(self) -> None:
        self._active = False
        self._pending_active = False
        self._generation = 0
        self._serial = 0  # number of `done` events received; echoed in commit()
        self._unavailable = False
        self._dead = False
        self._closing = False
        self._commands: queue.SimpleQueue = queue.SimpleQueue()
        self._wake_r, self._wake_w = os.pipe()

    def _bind(self) -> bool:
        """(Re)create our input-method object.

        niri (smithay) keeps one input method per seat, and destroying *any*
        input-method object — even another client's stale one — silently
        drops the current instance without telling it. So the daemon rebinds
        before every recording: that reclaims the seat and guarantees the
        activation state that follows is fresh."""
        if self._im is not None:
            self._im.destroy()
        self._unavailable = False
        self._active = self._pending_active = False
        self._serial = 0  # the compositor counts `done` per object
        self._im = self._manager.get_input_method(self._seat)
        for event in EVENTS:
            self._im.dispatcher[event] = getattr(self, f"_on_{event}")
        self._display.roundtrip()
        return not self._unavailable

    # --- public, any thread ---

    def activation(self) -> int | None:
        """Generation of the current activation, or None when no field is active."""
        if self._dead or self._unavailable or not self._active:
            return None
        return self._generation

    def rebind(self) -> bool:
        """Bind afresh; the activation for a focused field follows shortly."""
        return self._call(self._bind)

    def preedit(self, text: str, generation: int) -> None:
        self._post(lambda: self._apply(generation, preedit=text))

    def commit(self, text: str, generation: int) -> bool:
        """Insert TEXT in place of the preedit. False if the field went away."""
        return self._call(lambda: self._apply(generation, commit=text))

    def _call(self, function) -> bool:
        """Run FUNCTION on the loop thread and wait for its result. A call
        that times out is cancelled, so it cannot fire later — after the
        caller has already delivered the text another way."""
        if self._dead:
            return False
        done = threading.Event()
        cancelled = threading.Event()
        result = []

        def run():
            if cancelled.is_set():
                return
            try:
                result.append(bool(function()))
            finally:
                done.set()

        self._post(run)
        if not done.wait(CALL_TIMEOUT):
            cancelled.set()
            log.warning("the input method did not respond within %.0fs", CALL_TIMEOUT)
        return bool(result and result[0])

    def close(self) -> None:
        self._closing = True
        os.write(self._wake_w, b"x")
        self._thread.join(2.0)

    # --- protocol events (loop thread); state is applied on `done` ---

    def _on_activate(self, im) -> None:
        self._pending_active = True

    def _on_deactivate(self, im) -> None:
        self._pending_active = False

    def _on_surrounding_text(self, im, text, cursor, anchor) -> None:
        pass

    def _on_text_change_cause(self, im, cause) -> None:
        pass

    def _on_content_type(self, im, hint, purpose) -> None:
        pass

    def _on_done(self, im) -> None:
        self._serial += 1
        if self._pending_active != self._active:
            self._active = self._pending_active
            if self._active:
                self._generation += 1
            log.debug("input method %s (generation %d)",
                      "active" if self._active else "inactive", self._generation)

    def _on_unavailable(self, im) -> None:
        self._unavailable = True
        self._active = self._pending_active = False
        log.warning("another client bound the input method; "
                    "in-field text is off until the next recording")

    # --- requests (loop thread) ---

    def _apply(self, generation: int, *, preedit: str | None = None,
               commit: str | None = None) -> bool:
        if self._unavailable or not self._active or self._generation != generation:
            return False
        # Text-input state is double-buffered and resets on every commit, so a
        # commit that carries no preedit request *removes* the preedit. Never
        # send an empty preedit string instead: GTK treats "" as a preedit that
        # is still present and skips preedit-end, which leaves Ghostty in its
        # composing state, swallowing every printable key.
        if commit is not None:
            self._im.commit_string(commit)
        elif preedit:
            end = len(preedit.encode())
            self._im.set_preedit_string(preedit, end, end)
        self._im.commit(self._serial)
        self._display.flush()
        return True

    def _post(self, command) -> None:
        if self._dead:
            return
        self._commands.put(command)
        os.write(self._wake_w, b"x")

    def _run(self) -> None:
        try:
            fd = self._display.get_fd()
            while not self._closing:
                self._display.flush()
                readable, _, _ = select.select([fd, self._wake_r], [], [], 1.0)
                if fd in readable:
                    self._display.dispatch(block=True)
                if self._wake_r in readable:
                    os.read(self._wake_r, 4096)
                while True:
                    try:
                        command = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        command()
                    except Exception:
                        log.exception("input-method request failed")
        except Exception:
            log.exception("input method connection failed; in-field text is off")
        finally:
            self._dead = True
            self._active = False
            try:
                self._display.disconnect()
            except Exception:
                pass

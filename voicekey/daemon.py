"""Voicekey orchestration.

Hold a key: the microphone streams into the live recognizer and the partial
text is previewed — as preedit in the focused field when its application
speaks the input-method protocol, otherwise in a notification. Release: the
whole recording gets a second, offline pass (more accurate, better
punctuation) and that text replaces the preedit. If focus left the field's
window meanwhile — an agent's browser, a dialog — the text waits, within
its delay budget, for the window to be focused again and lands in the field
then, replacing the provisional text if the application turned it into real
text; if the window does not come back in time, or the field is gone, the
text is copied to the clipboard instead — never typed somewhere else. Emacs
needs no focus for any of that: its buffer is
pinned at key-down and the text goes into it through emacsclient, whatever
is focused by then. While anything is in flight, a lock in the runtime
directory tells agents to keep their hands off the desktop. Transcription
and delivery are separate workers, so a slow Emacs, a hung clipboard or a
long wtype never delays the next recording's transcription (on 2026-08-30
one hung wl-copy made the six dictations behind it late, and each was
copied and hung in turn). With a polish model configured, a third worker
sits between them: the raw transcript replaces the live text as preedit
while the model cleans it, and the cleaned text is what lands — or the raw
text, once the model's deadline passes. Agent prompts share the pipeline up
to transcription, then go to Hermes through their own queue and worker, so
a busy agent never delays dictation."""

from __future__ import annotations

import glob
import logging
import os
import queue
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace

import numpy as np
from evdev import ecodes

from . import agent as agent_mod
from . import emacs as emacs_mod
from . import focus
from . import inject as inject_mod
from . import polish as polish_mod
from . import recovery
from .backends import BackendUnavailable, create_backend, create_streaming
from .config import Config, ConfigError, key_chord_names
from .gate import Gate
from .ime import ImeHung, ImeUnavailable, InputMethod
from .listener import KeyboardListener
from .notify import notify
from .recorder import Recorder, RecordingError

log = logging.getLogger("voicekey.daemon")

LABEL = {"dictate": "dictation", "agent": "agent"}
HOLD = "hold"
TOGGLE = "toggle"
TRANSCRIPTION_QUEUE_SIZE = 4
AGENT_QUEUE_SIZE = 8
PREVIEW_INTERVAL = 0.25  # seconds between notification updates
ACTIVATION_WAIT = 0.2  # seconds to wait for the focused field after binding
OVERLOAD_FRAMES = 30  # 3 s of audio the live recognizer may fall behind
PIN_WAIT = 0.1  # seconds the preview waits for an idle Emacs to report the character before point
FOCUS_POLL = 0.25  # seconds between looks at the focused window while waiting for it to return


def _keycode(name: str) -> int:
    code = ecodes.ecodes.get(name)
    if not isinstance(code, int):
        raise ConfigError(f"unknown key name {name!r} (want evdev names like 'KEY_F9')")
    return code


def _key_chord(value: str) -> frozenset[int]:
    return frozenset(_keycode(name) for name in key_chord_names(value))


def fix_environment() -> None:
    """Fill session variables commonly absent from a systemd user service."""
    if not os.environ.get("WAYLAND_DISPLAY"):
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        sockets = sorted(path for path in glob.glob(os.path.join(runtime, "wayland-*"))
                         if not path.endswith(".lock"))
        if sockets:
            os.environ["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
            log.info("WAYLAND_DISPLAY not set; using %s", os.environ["WAYLAND_DISPLAY"])
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = f"{local_bin}:{os.environ.get('PATH', '')}"


# --- live previews ----------------------------------------------------------

class NotifyPreview:
    """Live text in a replaceable notification."""

    def __init__(self, action: str) -> None:
        self.action = action
        self.closed = False
        self._last = 0.0

    def update(self, text: str) -> None:
        now = time.monotonic()
        if not self.closed and now - self._last >= PREVIEW_INTERVAL:
            self._last = now
            notify(f"● {LABEL[self.action]}", text, ms=60000, channel=self.action)

    def clear(self) -> None:
        self.closed = True  # the next status notification replaces it


class ImePreview:
    """Live text as preedit in the field active at key-down; commit replaces
    it in place. Both carry that field's activation generation, so nothing
    reaches a field that gained focus later, and the spacing prefix decided
    at key-down, so nothing jumps at commit."""

    def __init__(self, ime: InputMethod, generation: int, prefix: str = "") -> None:
        self.ime = ime
        self.generation = generation
        self.prefix = prefix
        self.closed = False  # after clear/commit, no partial may reappear

    def update(self, text: str) -> None:
        if not self.closed:
            self.ime.preedit(spaced(self.prefix, text), self.generation)

    def clear(self) -> None:
        self.closed = True
        self.ime.preedit("", self.generation)

    def commit(self, text: str) -> bool:
        self.closed = True
        return self.ime.commit(spaced(self.prefix, text), self.generation)

    def replace(self, before: int, text: str) -> bool:
        """Commit in place of BEFORE bytes of real text before the cursor."""
        self.closed = True
        return self.ime.replace(before, spaced(self.prefix, text), self.generation)


class ProvisionalKept(Exception):
    """The application turned the provisional text into real text, and it is
    not at the cursor now: nothing can replace it, so the final text must
    not be typed on top."""


def spaced(prefix: str, text: str) -> str:
    """PREFIX is the space owed between the existing text and this
    dictation; it is dropped when the dictation itself starts with
    punctuation or whitespace."""
    if not text or text[0] in Spacing.NO_SPACE_BEFORE or text[0].isspace():
        return text
    return prefix + text


class Spacing:
    """The space owed between what is in the field and a new dictation.

    Decided by the character before the cursor when the application reports
    it; otherwise a space is owed only when voicekey itself was the last to
    type in that window — a keystroke in between hands spacing back to the
    user. Predicted at key-down for the preview and settled at delivery,
    once any dictation queued ahead in the same window has landed."""

    NO_SPACE_AFTER = " \t\n([{\"'“‘"  # a dictation may follow these directly
    NO_SPACE_BEFORE = ",.;:!?)]}"  # a dictation starting like this joins the text
    UNTRUSTED = {"emacs"}  # reports "" whatever precedes the cursor

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._continuing: int | str | None = None  # window voicekey typed into last
        self._activity = 0  # keystrokes seen; guards inserted() against a race
        self._landing: Counter = Counter()  # dictations queued per window, undelivered
        self._inserts: Counter = Counter()  # dictations landed per window, ever

    def queued(self, window_id) -> None:
        with self._lock:
            self._landing[window_id] += 1

    def landed(self, window_id) -> None:
        with self._lock:
            self._landing[window_id] -= 1

    def landing_in(self, window_id) -> bool:
        with self._lock:
            return window_id is not None and self._landing[window_id] > 0

    def predict(self, window_id, app_id: str | None, before: str | None) -> str:
        """At key-down, for the preview."""
        if self.landing_in(window_id):
            return " "  # our own text is about to land there
        return self.owed(before, app_id, window_id)

    def inserts_in(self, window_id) -> int:
        """Take at key-down, before the character before the cursor is
        read; settle() compares it to tell whether our own text landed in
        this window in the meantime."""
        with self._lock:
            return self._inserts[window_id]

    def settle(self, job: Job) -> str:
        """At delivery. If one of our dictations landed in this window since
        key-down, the character read then is stale and continuation
        decides; otherwise the field's own report still holds — a first
        dictation that landed nowhere (no speech, copied, refused) changed
        nothing."""
        with self._lock:
            landed = self._inserts[job.window_id] > job.inserts_mark
        return self.owed(None if landed else job.before, job.app_id, job.window_id)

    def owed(self, before: str | None, app_id: str | None, window_id) -> str:
        if before is not None and not (before == "" and app_id in self.UNTRUSTED):
            return "" if before == "" or before in self.NO_SPACE_AFTER else " "
        with self._lock:
            return " " if window_id is not None and window_id == self._continuing else ""

    def mark(self) -> int:
        """Take before inserting; hand to inserted() afterwards."""
        return self._activity

    def inserted(self, window_id, text: str, mark: int) -> None:
        with self._lock:
            self._inserts[window_id] += 1
            if self._activity != mark:
                return  # the user typed meanwhile and owns the spacing now
            self._continuing = None if text[-1:].isspace() else window_id

    def user_typed(self) -> None:
        with self._lock:
            self._activity += 1
            self._continuing = None


# --- one held key -----------------------------------------------------------

class Session:
    """One held key, from press to release. Audio frames arrive on the
    recorder thread and are decoded on their own thread, so a slow live
    recognizer can only lose the preview, never microphone audio."""

    def __init__(self, action: str, behavior: str, chord: frozenset[int],
                 device: str) -> None:
        self.action = action
        self.behavior = behavior
        self.chord = chord
        self.device = device
        self.window_id: int | None = None
        self.app_id: str | None = None
        self.preview: NotifyPreview | ImePreview = NotifyPreview(action)
        # Spacing inputs, captured at key-down; see Spacing.
        self.before: str | None = None  # character before the cursor, if reported
        self.after_landing = False  # our own text was still landing in this window
        self.inserts_mark = 0  # our landings in this window at key-down; see Spacing.settle
        self.prefix = ""  # space shown in the preview; settled again at delivery
        self.pin: str | None = None  # the Emacs buffer pinned at key-down; see emacs.py
        self.pinning: emacs_mod.PendingPin | None = None  # the pin round trip, still in flight
        self.text = ""
        self.decoder: threading.Thread | None = None
        self._stream = None
        self._lock = threading.Lock()
        self._frames: queue.Queue = queue.Queue(maxsize=OVERLOAD_FRAMES)

    def attach(self, stream) -> None:
        """Start live recognition. Frames that arrived earlier reach only
        the final pass, which keeps all of them anyway."""
        self._stream = stream
        self.decoder = threading.Thread(target=self._decode, name="live-decode", daemon=True)
        self.decoder.start()

    @property
    def live(self) -> bool:
        return self._stream is not None

    @property
    def stuck(self) -> bool:
        """The decode thread outlived the session (native inference hung)."""
        return self.decoder is not None and self.decoder.is_alive()

    def feed(self, frame: np.ndarray) -> None:  # recorder thread; never blocks
        if self._stream is None:
            return
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            self._drop("live recognition fell behind; preview off for this recording")

    def _decode(self) -> None:  # decode thread
        while (frame := self._frames.get()) is not None:
            stream = self._stream
            if stream is None:
                return
            try:
                text = stream.feed(frame)
            except Exception:
                log.exception("live recognition failed; preview off for this recording")
                self._stream = None
                return
            self._show(stream, text)

    def finish(self) -> None:
        """Flush the live recognizer; returns once its text is final."""
        stream = self._stream
        if stream is None:
            return
        try:
            self._frames.put_nowait(None)
        except queue.Full:
            self._drop("live recognition fell behind at release")
            return
        self.decoder.join(3.0)
        if self.decoder.is_alive():
            self._drop("live recognition did not finish in time")
            return
        if self._stream is None:
            return  # it failed and said so
        try:
            self._show(stream, stream.finish())
        except Exception:
            log.exception("live recognition failed at release")
            self._stream = None

    def cancel(self) -> None:
        with self._lock:
            self._stream = None
        try:
            self._frames.put_nowait(None)
        except queue.Full:
            pass

    def _drop(self, reason: str) -> None:
        log.warning(reason)
        self.cancel()

    def _show(self, stream, text: str) -> None:
        with self._lock:
            if self._stream is not stream:
                return  # cancelled while decoding: this partial is stale
            if text and text != self.text:
                self.text = text
                self.preview.update(text)


@dataclass(frozen=True)
class Job:
    samples: np.ndarray
    action: str
    finished_at: float
    window_id: int | None
    preview: NotifyPreview | ImePreview
    live_text: str
    app_id: str | None = None
    before: str | None = None
    pin: str | None = None
    pinning: emacs_mod.PendingPin | None = None
    inserts_mark: int = 0


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.actions: dict[frozenset[int], tuple[str, str]] = {
            _key_chord(cfg.dictate_key): ("dictate", HOLD),
            _key_chord(cfg.agent_key): ("agent", HOLD),
        }
        if cfg.dictate_toggle_key:
            self.actions[_key_chord(cfg.dictate_toggle_key)] = ("dictate", TOGGLE)
        if cfg.agent_toggle_key:
            self.actions[_key_chord(cfg.agent_toggle_key)] = ("agent", TOGGLE)
        self.recorder = Recorder()
        self.pressed: dict[str, set[int]] = {}
        self.session: Session | None = None
        self.backend = None
        self.backend_error: str | None = None
        self.streaming = None
        self.ime: InputMethod | None = None
        self.jobs: queue.Queue[Job] = queue.Queue(maxsize=TRANSCRIPTION_QUEUE_SIZE)
        self.polishing: queue.Queue[tuple[Job, str]] = queue.Queue()  # transcribed, being cleaned
        self.deliveries: queue.Queue[tuple[Job, str]] = queue.Queue()  # transcribed, not yet landed
        self.polisher: polish_mod.Polisher | None = None
        self.polish_server: polish_mod.LlamaServer | None = None
        self.agent_prompts: queue.Queue[str] = queue.Queue(maxsize=AGENT_QUEUE_SIZE)
        self.spacing = Spacing()
        self.gate = Gate()  # held while anything is in flight; agents wait on it
        # A decode thread that never came back; the recognizer is not
        # provably safe to share with it, so no live preview until it exits.
        self._stuck: threading.Thread | None = None

    def load(self) -> None:
        """Load both models and register as the input method; each failure
        is reported and degrades the daemon rather than stopping it."""
        try:
            self.backend = create_backend(self.cfg.backend, self.cfg.language)
        except BackendUnavailable as exc:
            self.backend_error = str(exc)
        except Exception as exc:
            self.backend_error = f"{type(exc).__name__}: {exc}"
            log.exception("transcription backend failed to load")
        if self.backend_error:
            notify("voicekey: transcription unavailable", self.backend_error, error=True)
        try:
            self.streaming = create_streaming(self.cfg.streaming)
        except BackendUnavailable as exc:
            notify("voicekey: live preview unavailable", str(exc), error=True)
        except Exception as exc:
            log.exception("streaming backend failed to load")
            notify("voicekey: live preview unavailable", f"{type(exc).__name__}: {exc}", error=True)
        if self.cfg.polish.backend != "none":
            try:
                self.polish_server = polish_mod.start_server(self.cfg.polish)
                self.polisher = polish_mod.create_polisher(self.cfg.polish, self.polish_server)
            except polish_mod.PolishError as exc:
                notify("voicekey: polish unavailable", f"{exc}; transcripts land unpolished", error=True)
            except Exception as exc:
                log.exception("polish failed to start")
                notify("voicekey: polish unavailable", f"{type(exc).__name__}: {exc}", error=True)
        if self.cfg.dictation.ime:
            try:
                self.ime = InputMethod()
            except ImeUnavailable as exc:
                log.info("no in-field preview: %s", exc)
            except Exception:
                log.exception("input method failed to start; previews use notifications")

    def start_workers(self) -> None:
        for name, target in (("transcription", self._transcription_worker),
                             ("polish", self._polish_worker),
                             ("delivery", self._delivery_worker),
                             ("agent-dispatch", self._agent_worker)):
            threading.Thread(target=target, daemon=True, name=name).start()

    def close(self) -> None:
        """Stop what the daemon started; called on the way out."""
        if self.polish_server is not None:
            self.polish_server.stop()

    def run(self) -> None:
        fix_environment()
        self.gate.open()
        self.load()
        self.start_workers()
        listener = KeyboardListener(
            keycodes=set().union(*self.actions),
            on_key=self._on_key,
            on_device_lost=self._on_device_lost,
            on_tick=self._on_tick,
            on_no_access=lambda msg: notify(
                "voicekey: no keyboard access", msg, error=True
            ),
            on_activity=self._on_activity,
        )
        log.info("listening: %s", ", ".join(self.bindings()))
        listener.run()

    def bindings(self) -> list[str]:
        """Human-readable key bindings, e.g. ['KEY_F9=dictate(hold)', ...]."""
        return [
            f"{key}={action}({behavior})"
            for key, (action, behavior) in (
                (self.cfg.dictate_key, ("dictate", HOLD)),
                (self.cfg.agent_key, ("agent", HOLD)),
                (self.cfg.dictate_toggle_key, ("dictate", TOGGLE)),
                (self.cfg.agent_toggle_key, ("agent", TOGGLE)),
            )
            if key
        ]

    def replay(self, path: str, action: str = "dictate") -> None:
        """Run one WAV file through the whole pipeline as if it were spoken."""
        self.gate.open()
        self.recorder = Recorder([sys.executable, "-m", "voicekey.replay", path])
        self._start("replay", frozenset(), HOLD, action, "replaying")
        while self.session is not None and not self.recorder.finished:
            time.sleep(0.05)
        if self.session is not None:
            self._finish()
        self.jobs.join()
        self.polishing.join()
        self.deliveries.join()
        if action == "agent":
            self.agent_prompts.join()

    # --- key handling (listener thread) ---

    def _on_key(self, device: str, code: int, value: int) -> None:
        pressed = self.pressed.setdefault(device, set())
        session = self.session
        if value == 0:
            if (session is not None and session.behavior == HOLD
                    and device == session.device and code in session.chord):
                self._finish()
            pressed.discard(code)
            return

        pressed.add(code)
        matches = [
            (chord, action)
            for chord, action in self.actions.items()
            if code in chord and chord <= pressed
        ]
        if not matches:
            return
        longest = max(len(chord) for chord, _action in matches)
        matches = [item for item in matches if len(item[0]) == longest]
        if len(matches) != 1:
            log.warning("ignoring ambiguous key chords for pressed keys %s", pressed)
            return
        chord, (action, behavior) = matches[0]
        if session is not None:
            if (behavior == TOGGLE and chord == session.chord
                    and device == session.device):
                self._finish()
            else:
                log.debug("ignoring %s press: already recording", action)
            return
        instruction = "press again to stop" if behavior == TOGGLE else "release to stop"
        self._start(device, chord, behavior, action, instruction)

    def _start(self, device: str, chord: frozenset[int], behavior: str,
               action: str, stop_instruction: str) -> None:
        session = Session(action, behavior, chord, device)
        try:
            self.recorder.start(session.feed)  # capture first; everything else can wait
        except OSError as exc:
            notify("voicekey: recording failed", str(exc), error=True)
            return
        self.session = session
        self._settle_gate()
        try:
            self._prepare(session)
        except Exception as exc:  # the recording and its final pass survive
            log.exception("could not set up the live preview")
            session.cancel()
            notify("voicekey: live preview unavailable", f"{type(exc).__name__}: {exc}", error=True)
        log.info("%s: %s preview", action,
                 "in-field" if isinstance(session.preview, ImePreview) else "notification")
        notify(f"● Recording ({LABEL[action]})", stop_instruction, ms=60000, channel=action)

    def _prepare(self, session: Session) -> None:
        """Bind the field first, milliseconds after key-down, then the rest;
        text will go only to the field active now."""
        dictate = session.action == "dictate"
        generation = None
        if dictate and self.ime is not None:
            try:
                generation = self._bind_field()
            except ImeHung as exc:
                log.error("%s", exc)
        if dictate:
            focused = focus.focused()
            session.window_id, session.app_id = focused.id, focused.app_id
            session.after_landing = self.spacing.landing_in(session.window_id)
            # Before the character before the cursor is read, so a landing
            # that changes it is known at delivery (Spacing.settle).
            session.inserts_mark = self.spacing.inserts_in(session.window_id)
        # In-field text needs a real focused window: a lock screen or launcher
        # can activate the input method too, and text must never land there.
        if generation is not None and (
                session.window_id is not None or not self.cfg.dictation.require_same_window):
            session.preview = ImePreview(self.ime, generation)
            session.before = self.ime.before_cursor()
        if dictate and session.app_id == "emacs":
            # The buffer itself, before focus can move, on a helper thread so
            # a busy Emacs never holds up the keyboard; and the character
            # before point, exact, since Emacs tells the IME nothing — for
            # the preview when it arrives at once, else settled at delivery.
            session.pinning = emacs_mod.PendingPin()
            session.pin = session.pinning.id
            session.before = session.pinning.before(PIN_WAIT)
        if self.streaming is not None:
            if self._stuck is not None and self._stuck.is_alive():
                log.warning("a previous live decoder is still running; no preview")
            else:
                session.attach(self.streaming.session())
        if dictate:
            session.prefix = self.spacing.predict(session.window_id, session.app_id, session.before)
            if isinstance(session.preview, ImePreview):
                session.preview.prefix = session.prefix

    def _bind_field(self) -> int | None:
        """Generation of the field active right now, or None.

        Binds afresh — the compositor drops our binding whenever another
        client touches the input method — unless a previous dictation is
        still landing: a rebind would cancel its ticket, and a binding that
        was live a second ago is trusted instead."""
        if not self._landing() and not self.ime.rebind():
            return None
        deadline = time.monotonic() + ACTIVATION_WAIT
        while (generation := self.ime.activation()) is None and time.monotonic() < deadline:
            time.sleep(0.005)
        return generation

    def _finish(self) -> None:
        session, self.session = self.session, None
        try:
            self._queue(session)
        finally:
            self._settle_gate()

    def _queue(self, session: Session) -> None:
        try:
            samples, duration = self.recorder.stop()
        except RecordingError as exc:
            session.cancel()
            session.preview.clear()
            notify("voicekey: recording failed", str(exc), error=True)
            return
        if duration < self.cfg.min_seconds:
            log.info("discarded %.2fs tap", duration)
            session.cancel()
            session.preview.clear()
            notify("voicekey", "cancelled (tap)", ms=1000, channel=session.action)
            return
        session.finish()
        self._note_stuck(session)
        job = Job(samples, session.action, time.monotonic(), session.window_id,
                  session.preview, session.text, session.app_id, session.before,
                  session.pin, session.pinning, session.inserts_mark)
        if job.action == "dictate":
            self.spacing.queued(job.window_id)  # before the worker can deliver it
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            if job.action == "dictate":
                self.spacing.landed(job.window_id)
            session.preview.clear()
            notify("voicekey: busy",
                   "too many recordings queued; newest recording discarded", error=True)

    def _abort(self, message: str) -> None:
        session, self.session = self.session, None
        session.cancel()
        self._note_stuck(session)
        self.recorder.abort()
        session.preview.clear()
        notify("voicekey", message, error=True)
        self._settle_gate()

    def _note_stuck(self, session: Session) -> None:
        if session.stuck:
            self._stuck = session.decoder

    def _on_activity(self) -> None:
        self.spacing.user_typed()

    def _settle_gate(self) -> None:
        """After every transition: agents wait while anything is in flight."""
        self.gate.settle(lambda: self.session is not None or self._landing())

    def _landing(self) -> bool:
        """Some recording is still being transcribed, polished or delivered."""
        return (self.jobs.unfinished_tasks > 0 or self.polishing.unfinished_tasks > 0
                or self.deliveries.unfinished_tasks > 0)

    def _on_device_lost(self, device: str) -> None:
        self.pressed.pop(device, None)
        if self.session is not None and device == self.session.device:
            self._abort("recording aborted (keyboard disconnected)")

    def _on_tick(self) -> None:
        if self.session is not None and self.recorder.elapsed > self.cfg.max_seconds:
            # A stuck key cannot be told from a long thought until release,
            # so the recording is stopped and transcribed, never discarded.
            log.warning("recording stopped at %.0fs (stuck key?)", self.cfg.max_seconds)
            notify("voicekey", f"recording stopped at {self.cfg.max_seconds:.0f}s (stuck key?); "
                   "transcribing what was said", ms=10000)
            self._finish()

    # --- workers ---

    def _transcription_worker(self) -> None:
        while True:
            job = self.jobs.get()
            handed_over = False
            try:
                handed_over = self._process(job)
            except Exception as exc:
                log.exception("transcription failed")
                job.preview.clear()
                notify("voicekey: error", f"{type(exc).__name__}: {exc}", error=True)
            finally:
                if job.action == "dictate" and not handed_over:
                    self.spacing.landed(job.window_id)
                self.jobs.task_done()
                self._settle_gate()

    def _process(self, job: Job) -> bool:
        """Transcribe JOB and pass the text on. True when a dictation went to
        the delivery worker, which then owns its landing."""
        if self.backend is None:
            job.preview.clear()
            notify("voicekey: transcription unavailable",
                   self.backend_error or "no backend", error=True)
            return False
        notify("⋯ Transcribing", ms=30000, channel=job.action)
        text = self.backend.transcribe(job.samples)
        polishing = bool(text) and job.action == "dictate" and self.polisher is not None
        if not polishing:
            self._keep(job, text)  # else kept once the polished text is known too
        if not text:
            job.preview.clear()
            notify("voicekey", "no speech detected", channel=job.action)
            return False
        log.info("transcribed %s prompt (%d chars)", job.action, len(text))
        if polishing:
            self.polishing.put((job, text))  # before task_done: the gate must see no gap
            return True
        if job.action == "dictate":
            self.deliveries.put((job, text))
            return True
        job.preview.clear()
        try:
            self.agent_prompts.put_nowait(text)
        except queue.Full:
            self._delivery_failed("voicekey: agent busy", "too many agent prompts queued", text)
            return False
        notify("→ Agent", "prompt queued", ms=10000, channel="agent")
        return False

    def _keep(self, job: Job, text: str, polished: str | None = None) -> None:
        if not self.cfg.recordings_dir:
            return
        try:
            recovery.keep(self.cfg.recordings_dir, job.samples, job.live_text, text, polished)
        except Exception as exc:
            log.warning("could not keep the recording: %s", exc)

    def _polish_worker(self) -> None:
        """The third pass, on its own thread so a slow model never delays a
        transcription. The raw transcript is shown as preedit meanwhile, and
        lands as it is when the model is late, fails, or is not trusted."""
        while True:
            job, text = self.polishing.get()
            final = text
            try:
                job.preview.update(text)
                notify("⋯ Polishing", ms=30000, channel="dictate")
                # Within the delivery budget, with a second left for landing.
                wait = min(self.cfg.polish.max_wait_seconds, self._budget(job) - 1.0)
                cleaned = self.polisher.polish(text, wait) if wait > 0 else None
                if cleaned is not None:
                    final = cleaned
            except Exception:
                log.exception("polish failed")
            finally:
                self._keep(job, text, final)
                if final:
                    self.deliveries.put((job, final))  # before task_done: no gap for the gate
                else:
                    job.preview.clear()
                    self.spacing.landed(job.window_id)
                    notify("voicekey", "nothing to type after cleanup", channel="dictate")
                self.polishing.task_done()
                self._settle_gate()

    def _delivery_worker(self) -> None:
        """Landing on its own thread: a slow Emacs, a hung wl-copy or a long
        wtype delays only the deliveries behind it, never a transcription."""
        while True:
            job, text = self.deliveries.get()
            try:
                self._deliver_dictation(job, text)
            except Exception as exc:
                log.exception("delivery failed")
                job.preview.clear()
                self._delivery_failed("voicekey: delivery failed",
                                      f"{type(exc).__name__}: {exc}", text)
            finally:
                self.spacing.landed(job.window_id)
                self.deliveries.task_done()
                self._settle_gate()

    def _deliver_dictation(self, job: Job, text: str) -> None:
        age = time.monotonic() - job.finished_at
        if age > self.cfg.dictation.max_delay_seconds:
            job.preview.clear()
            self._copy_instead(text, f"dictation was {age:.1f}s old; copied instead of typing")
            return
        if job.app_id == "emacs":
            # Into the buffer pinned at key-down, through Emacs itself and
            # with the gesture of its current state — wherever focus went
            # since: Emacs refuses only when the buffer is gone, read-only,
            # mid-operator or blockwise selected. It has the rest of the
            # delay budget to answer, since a dialog can hold it up.
            job.preview.clear()
            if job.pinning is not None:
                # The pin must be registered before the insert is sent, and
                # its report of the character before point is wanted if it
                # was not in by key-down. Emacs has the rest of the delay
                # budget for that; a transcript that goes stale waiting is
                # copied like any other, never inserted late.
                before = job.pinning.before(self._budget(job))
                if job.before is None:
                    job = replace(job, before=before)
                age = time.monotonic() - job.finished_at
                if age > self.cfg.dictation.max_delay_seconds:
                    self._copy_instead(text, f"dictation was {age:.1f}s old; copied instead of typing")
                    return
            prefix = self.spacing.settle(job)
            mark = self.spacing.mark()
            try:
                emacs_mod.insert(spaced(prefix, text), job.pin or "", timeout=self._budget(job))
            except emacs_mod.EmacsTimeout as exc:
                # The form still runs when Emacs is free again, so neither
                # type nor copy it: keep it where nothing can duplicate.
                self._delivery_failed("voicekey: Emacs did not answer",
                                      f"{exc}; the text may still appear when Emacs is free", text)
                return
            except emacs_mod.EmacsError as exc:
                self._copy_instead(text, f"Emacs: {exc}; copied instead")
                return
            log.info("inserted via emacsclient")
            self._inserted(job, text, mark)
            return
        prefix = self.spacing.settle(job)
        mark = self.spacing.mark()
        if isinstance(job.preview, ImePreview):
            job.preview.prefix = prefix
            try:
                committed = self._commit_in_field(job, text)
            except ImeHung as exc:
                # The commit may still land when the compositor recovers, so
                # neither type nor copy it: keep it where nothing can duplicate.
                self._delivery_failed("voicekey: input method hung",
                                      f"{exc}; the text may still appear when the compositor recovers", text)
                return
            except ProvisionalKept:
                self._copy_instead(text, "the application kept the live text elsewhere; "
                                   "the final text is copied instead of typed on top")
                return
            if committed:
                log.info("committed in place")
                self._inserted(job, text, mark)
                return
            job.preview.clear()
            self._copy_instead(text, "the field changed; copied instead of typing")
            return
        if not self._window_back(job):
            self._copy_instead(text, "focus changed; copied instead of typing")
            return
        if self.cfg.dictation.inject == "wtype":
            try:
                inject_mod.type_text(spaced(prefix, text))
            except Exception as exc:
                self._copy_instead(text, f"typing failed ({exc}); copied instead")
                return
            log.info("typed via wtype")
            self._inserted(job, text, mark)
            return
        self._copy_instead(text, "", summary="📋 Copied")

    def _budget(self, job: Job) -> float:
        """What is left of the delay budget, and at least a second for a
        healthy Emacs to answer."""
        return max(1.0, self.cfg.dictation.max_delay_seconds - (time.monotonic() - job.finished_at))

    def _inserted(self, job: Job, text: str, mark: int) -> None:
        self.spacing.inserted(job.window_id, text, mark)
        notify("✓ Typed", channel="dictate")

    def _same_window(self, job: Job) -> bool:
        if not self.cfg.dictation.require_same_window:
            return True
        return job.window_id is not None and focus.window_id() == job.window_id

    def _commit_in_field(self, job: Job, text: str) -> bool:
        """Commit into the field bound at key-down. If focus left its window
        meanwhile, the field was deactivated and this commit would be
        refused; so wait, within the delivery budget, for the window to be
        focused and the field active again, and commit into that activation
        — after finding out what the application did with the provisional
        text it was showing. Kept as real text right before the cursor
        (Chromium does this), it is replaced; kept somewhere else, nothing
        can replace it and the final text is copied instead; dropped, which
        is what GTK and every terminal do, the final text is committed as
        usual. False when the field is not back within the budget."""
        if self._same_window(job) and job.preview.commit(text):
            return True
        generation = self._reactivation(job)
        if generation is None:
            return False
        job.preview.generation = generation
        shown = job.preview.ime.left_showing()
        surrounding = job.preview.ime.surrounding_text()
        if shown and surrounding is not None:
            before, after = surrounding
            if before.endswith(shown):
                log.info("the application kept the provisional text; replacing it")
                return job.preview.replace(len(shown.encode()), text)
            if shown in before or shown in after:
                raise ProvisionalKept()
        return job.preview.commit(text)

    def _reactivation(self, job: Job) -> int | None:
        """The field's activation once its window is focused again, or None
        when that does not happen within the delivery budget."""
        def back():
            generation = job.preview.ime.activation()
            if generation is None or generation == job.preview.generation:
                return None
            return generation if focus.window_id() == job.window_id else None
        return self._await_window(job, back)

    def _window_back(self, job: Job) -> bool:
        """The window is focused, now or within the delivery budget."""
        if self._same_window(job):
            return True
        return bool(self._await_window(job, lambda: focus.window_id() == job.window_id or None))

    def _await_window(self, job: Job, ready):
        """Poll READY until it returns something, within what is left of the
        delay budget; nothing to wait for without a verifiable window."""
        if not self.cfg.dictation.require_same_window or job.window_id is None:
            return None
        budget = self._budget(job)
        deadline = time.monotonic() + budget
        log.info("the window is not focused; waiting up to %.0fs for it", budget)
        notify("⏳ Waiting for focus", "the text lands once its window is focused again",
               ms=int(budget * 1000), channel="dictate")
        while True:
            result = ready()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                return None
            time.sleep(FOCUS_POLL)

    def _copy_instead(self, text: str, reason: str, *, summary: str = "voicekey: not typed") -> None:
        """The clipboard is one wl-copy away from being overwritten, so the
        transcript is saved to a file as well."""
        log.info("not typed: %s", reason or "copied")
        try:
            inject_mod.copy(text)
        except Exception as exc:
            self._delivery_failed("voicekey: clipboard failed", str(exc), text)
            return
        notify(summary, _with_recovery(reason, text), ms=10000, channel="dictate")

    def _agent_worker(self) -> None:
        while True:
            text = self.agent_prompts.get()
            try:
                target = agent_mod.send_prompt(self.cfg.agent, text)
            except agent_mod.AgentError as exc:
                self._delivery_failed("voicekey: agent dispatch failed", str(exc), text)
            except Exception as exc:
                log.exception("unexpected agent dispatch failure")
                self._delivery_failed("voicekey: agent dispatch failed",
                                      f"{type(exc).__name__}: {exc}", text)
            else:
                notify("✓ Sent to agent", target, ms=10000, channel="agent")
            finally:
                self.agent_prompts.task_done()

    @staticmethod
    def _delivery_failed(summary: str, detail: str, text: str) -> None:
        notify(summary, _with_recovery(detail, text), error=True)


def _with_recovery(detail: str, text: str) -> str:
    """DETAIL plus where the transcript was saved."""
    try:
        saved = f"Transcript saved to {recovery.save(text)}"
    except OSError as exc:
        saved = f"Transcript recovery also failed: {exc}"
    return f"{detail}\n{saved}" if detail else saved

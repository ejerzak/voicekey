"""Voicekey orchestration.

Hold a key: the microphone streams into the live recognizer and the partial
text is previewed — as preedit in the focused field when its application
speaks the input-method protocol, otherwise in a notification. Release: the
whole recording gets a second, offline pass (more accurate, better
punctuation) and that text replaces the preedit. If the field that was active
at key-down is gone by then, the text is copied to the clipboard instead —
never typed somewhere else. Agent prompts share the pipeline up to
transcription, then go to Hermes through their own queue and worker, so a
busy agent never delays dictation."""

from __future__ import annotations

import glob
import logging
import os
import queue
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass

import numpy as np
from evdev import ecodes

from . import agent as agent_mod
from . import emacs as emacs_mod
from . import focus
from . import inject as inject_mod
from . import recovery
from .backends import BackendUnavailable, create_backend, create_streaming
from .config import Config, ConfigError, key_chord_names
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
        sockets = sorted(glob.glob(os.path.join(runtime, "wayland-*")))
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

    def settle(self, job: Job) -> str:
        """At delivery: whatever was landing has landed by now."""
        before = None if job.after_landing else job.before
        return self.owed(before, job.app_id, job.window_id)

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
        self.prefix = ""  # space shown in the preview; settled again at delivery
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
    after_landing: bool = False


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
        self.agent_prompts: queue.Queue[str] = queue.Queue(maxsize=AGENT_QUEUE_SIZE)
        self.spacing = Spacing()
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
        if self.cfg.dictation.ime:
            try:
                self.ime = InputMethod()
            except ImeUnavailable as exc:
                log.info("no in-field preview: %s", exc)
            except Exception:
                log.exception("input method failed to start; previews use notifications")

    def start_workers(self) -> None:
        for name, target in (("transcription", self._transcription_worker),
                             ("agent-dispatch", self._agent_worker)):
            threading.Thread(target=target, daemon=True, name=name).start()

    def run(self) -> None:
        fix_environment()
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
        self.recorder = Recorder([sys.executable, "-m", "voicekey.replay", path])
        self._start("replay", frozenset(), HOLD, action, "replaying")
        while self.session is not None and not self.recorder.finished:
            time.sleep(0.05)
        if self.session is not None:
            self._finish()
        self.jobs.join()
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
        # In-field text needs a real focused window: a lock screen or launcher
        # can activate the input method too, and text must never land there.
        if generation is not None and (
                session.window_id is not None or not self.cfg.dictation.require_same_window):
            session.preview = ImePreview(self.ime, generation)
            if not session.after_landing:  # else stale: the landing text changes it
                session.before = self.ime.before_cursor()
        if dictate and session.app_id == "emacs" and not session.after_landing:
            session.before = emacs_mod.before_cursor()  # exact; Emacs tells the IME nothing
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
        if self.jobs.unfinished_tasks == 0 and not self.ime.rebind():
            return None
        deadline = time.monotonic() + ACTIVATION_WAIT
        while (generation := self.ime.activation()) is None and time.monotonic() < deadline:
            time.sleep(0.005)
        return generation

    def _finish(self) -> None:
        session, self.session = self.session, None
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
                  session.after_landing)
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

    def _note_stuck(self, session: Session) -> None:
        if session.stuck:
            self._stuck = session.decoder

    def _on_activity(self) -> None:
        self.spacing.user_typed()

    def _on_device_lost(self, device: str) -> None:
        self.pressed.pop(device, None)
        if self.session is not None and device == self.session.device:
            self._abort("recording aborted (keyboard disconnected)")

    def _on_tick(self) -> None:
        if self.session is not None and self.recorder.elapsed > self.cfg.max_seconds:
            self._abort(f"recording discarded: exceeded {self.cfg.max_seconds:.0f}s — stuck key?")

    # --- workers ---

    def _transcription_worker(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                self._process(job)
            except Exception as exc:
                log.exception("transcription failed")
                job.preview.clear()
                notify("voicekey: error", f"{type(exc).__name__}: {exc}", error=True)
            finally:
                if job.action == "dictate":
                    self.spacing.landed(job.window_id)
                self.jobs.task_done()

    def _process(self, job: Job) -> None:
        if self.backend is None:
            job.preview.clear()
            notify("voicekey: transcription unavailable",
                   self.backend_error or "no backend", error=True)
            return
        notify("⋯ Transcribing", ms=30000, channel=job.action)
        text = self.backend.transcribe(job.samples)
        if self.cfg.recordings_dir:
            try:
                recovery.keep(self.cfg.recordings_dir, job.samples, job.live_text, text)
            except Exception as exc:
                log.warning("could not keep the recording: %s", exc)
        if not text:
            job.preview.clear()
            notify("voicekey", "no speech detected", channel=job.action)
            return
        log.info("transcribed %s prompt (%d chars)", job.action, len(text))
        if job.action == "dictate":
            self._deliver_dictation(job, text)
            return
        job.preview.clear()
        try:
            self.agent_prompts.put_nowait(text)
        except queue.Full:
            self._delivery_failed("voicekey: agent busy", "too many agent prompts queued", text)
            return
        notify("→ Agent", "prompt queued", ms=10000, channel="agent")

    def _deliver_dictation(self, job: Job, text: str) -> None:
        age = time.monotonic() - job.finished_at
        if age > self.cfg.dictation.max_delay_seconds:
            job.preview.clear()
            self._copy_instead(text, f"dictation was {age:.1f}s old; copied instead of typing")
            return
        prefix = self.spacing.settle(job)
        mark = self.spacing.mark()
        if job.app_id == "emacs":
            # Through Emacs itself, with the gesture of its current state, so
            # dictation never becomes commands in normal or visual state.
            if not self._same_window(job):
                job.preview.clear()
                self._copy_instead(text, "focus changed; copied instead of typing")
                return
            job.preview.clear()
            try:
                emacs_mod.insert(spaced(prefix, text))
            except emacs_mod.EmacsError as exc:
                self._copy_instead(text, f"Emacs: {exc}; copied instead")
                return
            log.info("inserted via emacsclient")
            self._inserted(job, text, mark)
            return
        if isinstance(job.preview, ImePreview):
            job.preview.prefix = prefix
            try:
                committed = self._same_window(job) and job.preview.commit(text)
            except ImeHung as exc:
                # The commit may still land when the compositor recovers, so
                # neither type nor copy it: keep it where nothing can duplicate.
                self._delivery_failed("voicekey: input method hung",
                                      f"{exc}; the text may still appear when the compositor recovers", text)
                return
            if committed:
                log.info("committed in place")
                self._inserted(job, text, mark)
                return
            job.preview.clear()
            self._copy_instead(text, "the field changed; copied instead of typing")
            return
        if not self._same_window(job):
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

    def _inserted(self, job: Job, text: str, mark: int) -> None:
        self.spacing.inserted(job.window_id, text, mark)
        notify("✓ Typed", channel="dictate")

    def _same_window(self, job: Job) -> bool:
        if not self.cfg.dictation.require_same_window:
            return True
        return job.window_id is not None and focus.window_id() == job.window_id

    def _copy_instead(self, text: str, reason: str, *, summary: str = "voicekey: not typed") -> None:
        log.info("not typed: %s", reason or "copied")
        try:
            inject_mod.copy(text)
        except Exception as exc:
            self._delivery_failed("voicekey: clipboard failed", str(exc), text)
            return
        notify(summary, reason, ms=10000, channel="dictate")

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
        try:
            body = f"{detail}\nTranscript saved to {recovery.save(text)}"
        except OSError as exc:
            body = f"{detail}\nTranscript recovery also failed: {exc}"
        notify(summary, body, error=True)

"""Voicekey orchestration.

Hold a key: the microphone streams into the live recognizer and the partial
text is previewed — as preedit in the focused field when its application
speaks the input-method protocol, otherwise in a notification. Release: the
whole recording gets a second, offline pass (more accurate, better punctuation)
and that text replaces the preedit, or is typed or copied when there is no
IME target. Agent prompts share the pipeline up to transcription, then go to
Hermes through their own queue and worker, so a busy agent never delays
dictation."""

from __future__ import annotations

import glob
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np
from evdev import ecodes

from . import agent as agent_mod
from . import focus
from . import inject as inject_mod
from . import recovery
from .backends import BackendUnavailable, create_backend, create_streaming
from .config import Config, ConfigError, key_chord_names
from .ime import ImeUnavailable, InputMethod
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
        self._last = 0.0

    def update(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last >= PREVIEW_INTERVAL:
            self._last = now
            notify(f"● {LABEL[self.action]}", text, ms=60000, channel=self.action)

    def clear(self) -> None:
        pass  # the next status notification replaces it

    def commit(self, text: str) -> bool:
        return False


class ImePreview:
    """Live text as preedit in the focused field; commit replaces it in place."""

    def __init__(self, ime: InputMethod, generation: int) -> None:
        self.ime = ime
        self.generation = generation

    def update(self, text: str) -> None:
        self.ime.preedit(text, self.generation)

    def clear(self) -> None:
        self.ime.preedit("", self.generation)

    def commit(self, text: str) -> bool:
        return self.ime.commit(text, self.generation)


# --- one held key -----------------------------------------------------------

@dataclass
class Session:
    action: str
    behavior: str
    chord: frozenset[int]
    device: str
    window_id: int | None
    preview: NotifyPreview | ImePreview
    stream: object | None
    text: str = ""

    def feed(self, frame: np.ndarray) -> None:  # recorder thread
        if self.stream is not None:
            self._show(self.stream.feed(frame))

    def finish(self) -> None:
        if self.stream is not None:
            self._show(self.stream.finish())

    def _show(self, text: str) -> None:
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
        window_id = (
            focus.window_id()
            if action == "dictate" and self.cfg.dictation.require_same_window
            else None
        )
        # In-field text needs a real focused window: a lock screen or launcher
        # can activate the input method too, and text must never land there.
        in_field = (
            self.ime is not None and action == "dictate"
            and (window_id is not None or not self.cfg.dictation.require_same_window)
        )
        generation = self.ime.activation() if in_field else None
        preview = (
            ImePreview(self.ime, generation) if generation is not None
            else NotifyPreview(action)
        )
        stream = self.streaming.session() if self.streaming else None
        session = Session(action, behavior, chord, device, window_id, preview, stream)
        try:
            self.recorder.start(session.feed)
        except OSError as exc:
            notify("voicekey: recording failed", str(exc), error=True)
            return
        self.session = session
        log.info("%s: %s preview", action,
                 "in-field" if isinstance(preview, ImePreview) else "notification")
        notify(f"● Recording ({LABEL[action]})", stop_instruction, ms=60000, channel=action)

    def _finish(self) -> None:
        session, self.session = self.session, None
        try:
            samples, duration = self.recorder.stop()
        except RecordingError as exc:
            session.preview.clear()
            notify("voicekey: recording failed", str(exc), error=True)
            return
        if duration < self.cfg.min_seconds:
            log.info("discarded %.2fs tap", duration)
            session.preview.clear()
            notify("voicekey", "cancelled (tap)", ms=1000, channel=session.action)
            return
        session.finish()
        job = Job(samples, session.action, time.monotonic(), session.window_id,
                  session.preview, session.text)
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            session.preview.clear()
            notify("voicekey: busy",
                   "too many recordings queued; newest recording discarded", error=True)

    def _abort(self, message: str) -> None:
        session, self.session = self.session, None
        self.recorder.abort()
        session.preview.clear()
        notify("voicekey", message, error=True)

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
            recovery.keep(self.cfg.recordings_dir, job.samples, job.live_text, text)
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
        if job.preview.commit(text):
            log.info("committed in place")
            notify("✓ Typed", channel="dictate")
            return
        job.preview.clear()
        if self.cfg.dictation.require_same_window:
            current = focus.window_id()
            if job.window_id is None or current is None:
                self._copy_instead(text, "could not verify focus; copied instead of typing")
                return
            if current != job.window_id:
                self._copy_instead(text, "focus changed; copied instead of typing")
                return
        try:
            method = inject_mod.inject(text, self.cfg.dictation.inject)
        except Exception as exc:
            self._delivery_failed("voicekey: typing failed", str(exc), text)
            return
        suffix = (
            " via clipboard paste"
            if method == "clipboard" and self.cfg.dictation.inject != "clipboard"
            else ""
        )
        log.info("typed via %s", method)
        notify("✓ Typed" + suffix, channel="dictate")

    def _copy_instead(self, text: str, reason: str) -> None:
        log.info("not typed: %s", reason)
        try:
            inject_mod.copy(text)
        except Exception as exc:
            self._delivery_failed("voicekey: clipboard failed", str(exc), text)
            return
        notify("voicekey: not typed", reason, ms=10000, channel="dictate")

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

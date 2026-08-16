"""Voicekey orchestration.

Recording and transcription are serialized because the speech backend is a
single warm model.  Dictation delivery and agent dispatch diverge immediately
after transcription, so a slow agent can never delay later dictation.
"""

from __future__ import annotations

import glob
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

from evdev import ecodes

from . import agent as agent_mod
from . import focus
from . import inject as inject_mod
from . import recovery
from .backends import BackendUnavailable, create_backend
from .config import Config, ConfigError
from .listener import KeyboardListener
from .notify import notify
from .recorder import Recorder, RecordingError

log = logging.getLogger("voicekey.daemon")

ACTION_LABEL = {"dictate": "dictation", "agent": "agent"}
HOLD = "hold"
TOGGLE = "toggle"
TRANSCRIPTION_QUEUE_SIZE = 4
AGENT_QUEUE_SIZE = 8


@dataclass(frozen=True)
class RecordingJob:
    wav: str
    action: str
    finished_at: float
    window_id: int | None


def _keycode(name: str) -> int:
    code = ecodes.ecodes.get(name)
    if not isinstance(code, int):
        raise ConfigError(f"unknown key name {name!r} (want evdev names like 'KEY_F9')")
    return code


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


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.actions: dict[int, tuple[str, str]] = {
            _keycode(cfg.dictate_key): ("dictate", HOLD),
            _keycode(cfg.agent_key): ("agent", HOLD),
        }
        if cfg.dictate_toggle_key:
            self.actions[_keycode(cfg.dictate_toggle_key)] = ("dictate", TOGGLE)
        if cfg.agent_toggle_key:
            self.actions[_keycode(cfg.agent_toggle_key)] = ("agent", TOGGLE)
        self.recorder = Recorder()
        self.session_action: str | None = None
        self.session_code: int | None = None
        self.session_device: str | None = None
        self.session_window_id: int | None = None
        self.backend = None
        self.backend_error: str | None = None
        self.recordings: queue.Queue[RecordingJob] = queue.Queue(
            maxsize=TRANSCRIPTION_QUEUE_SIZE
        )
        self.agent_prompts: queue.Queue[str] = queue.Queue(maxsize=AGENT_QUEUE_SIZE)

    def load_backend(self) -> None:
        try:
            self.backend = create_backend(self.cfg.backend, self.cfg.language)
        except BackendUnavailable as exc:
            self.backend_error = str(exc)
            notify("voicekey: transcription unavailable", str(exc), error=True)
        except Exception as exc:
            self.backend_error = f"{type(exc).__name__}: {exc}"
            log.exception("transcription backend failed to load")
            notify(
                "voicekey: transcription unavailable", self.backend_error, error=True
            )

    def run(self) -> None:
        fix_environment()
        self.load_backend()
        threading.Thread(
            target=self._transcription_worker, daemon=True, name="transcription"
        ).start()
        threading.Thread(
            target=self._agent_worker, daemon=True, name="agent-dispatch"
        ).start()
        listener = KeyboardListener(
            keycodes=set(self.actions),
            on_key=self._on_key,
            on_device_lost=self._on_device_lost,
            on_tick=self._on_tick,
            on_no_access=lambda msg: notify(
                "voicekey: no keyboard access", msg, error=True
            ),
        )
        descriptions = [
            f"{self.cfg.dictate_key}=dictate(hold)",
            f"{self.cfg.agent_key}=agent(hold)",
        ]
        if self.cfg.dictate_toggle_key:
            descriptions.append(f"{self.cfg.dictate_toggle_key}=dictate(toggle)")
        if self.cfg.agent_toggle_key:
            descriptions.append(f"{self.cfg.agent_toggle_key}=agent(toggle)")
        log.info("listening: %s", ", ".join(descriptions))
        listener.run()

    # Listener callbacks run on the main thread.

    def _on_key(self, device_path: str, code: int, value: int) -> None:
        action, behavior = self.actions[code]
        if behavior == TOGGLE:
            if value == 0:
                return
            if self.recorder.active:
                if code == self.session_code and device_path == self.session_device:
                    self._finish()
                else:
                    log.debug("ignoring %s toggle: already recording", action)
                return
            self._start(device_path, code, action, "press again to stop")
            return

        if value == 1:
            if self.recorder.active:
                log.debug("ignoring %s press: already recording", action)
                return
            self._start(device_path, code, action, "release to stop")
        else:
            if (
                not self.recorder.active
                or code != self.session_code
                or device_path != self.session_device
            ):
                return
            self._finish()

    def _start(
        self, device_path: str, code: int, action: str, stop_instruction: str
    ) -> None:
        try:
            self.recorder.start()
        except OSError as exc:
            notify("voicekey: recording failed", str(exc), error=True)
            return
        self.session_action = action
        self.session_code = code
        self.session_device = device_path
        self.session_window_id = (
            focus.window_id()
            if action == "dictate" and self.cfg.dictation.require_same_window
            else None
        )
        notify(
            f"● Recording ({ACTION_LABEL[action]})",
            stop_instruction,
            ms=60000,
            channel=action,
        )

    def _clear_session(self) -> tuple[str | None, int | None]:
        action, window_id = self.session_action, self.session_window_id
        self.session_action = None
        self.session_code = None
        self.session_device = None
        self.session_window_id = None
        return action, window_id

    def _finish(self) -> None:
        action, window_id = self._clear_session()
        try:
            wav, duration = self.recorder.stop()
        except RecordingError as exc:
            notify("voicekey: recording failed", str(exc), error=True)
            return
        if action not in ACTION_LABEL:
            Recorder._unlink(wav)
            notify("voicekey: internal error", "recording action was lost", error=True)
            return
        if duration < self.cfg.min_seconds:
            log.info("discarded %.2fs tap", duration)
            notify("voicekey", "cancelled (tap)", ms=1000, channel=action)
            Recorder._unlink(wav)
            return
        job = RecordingJob(wav, action, time.monotonic(), window_id)
        try:
            self.recordings.put_nowait(job)
        except queue.Full:
            Recorder._unlink(wav)
            notify(
                "voicekey: busy",
                "too many recordings queued; newest recording discarded",
                error=True,
            )

    def _on_device_lost(self, device_path: str) -> None:
        if self.recorder.active and device_path == self.session_device:
            self._clear_session()
            self.recorder.abort()
            notify("voicekey", "recording aborted (keyboard disconnected)", error=True)

    def _on_tick(self) -> None:
        if self.recorder.active and self.recorder.elapsed > self.cfg.max_seconds:
            self._clear_session()
            self.recorder.abort()
            notify(
                "voicekey: recording discarded",
                f"exceeded {self.cfg.max_seconds:.0f}s — stuck key?",
                error=True,
            )

    # Worker threads.

    def _transcription_worker(self) -> None:
        while True:
            job = self.recordings.get()
            try:
                self._process_recording(job)
            except Exception as exc:
                log.exception("transcription dispatch failed")
                notify(
                    "voicekey: error", f"{type(exc).__name__}: {exc}", error=True
                )
            finally:
                Recorder._unlink(job.wav)

    def _process_recording(self, job: RecordingJob) -> None:
        if self.backend is None:
            notify(
                "voicekey: transcription unavailable",
                self.backend_error or "no backend",
                error=True,
            )
            return
        notify("⋯ Transcribing", ms=30000, channel=job.action)
        try:
            text = self.backend.transcribe(job.wav)
        except Exception as exc:
            notify(
                "voicekey: transcription failed",
                f"{type(exc).__name__}: {exc}",
                error=True,
            )
            return
        if not text:
            notify("voicekey", "no speech detected", channel=job.action)
            return
        log.info("transcribed %s prompt (%d chars)", job.action, len(text))
        if job.action == "dictate":
            self._deliver_dictation(job, text)
        else:
            try:
                self.agent_prompts.put_nowait(text)
            except queue.Full:
                self._delivery_failed(
                    "voicekey: agent busy",
                    "too many agent prompts queued",
                    text,
                )
                return
            notify("→ Agent", "prompt queued", ms=10000, channel="agent")

    def _deliver_dictation(self, job: RecordingJob, text: str) -> None:
        age = time.monotonic() - job.finished_at
        if age > self.cfg.dictation.max_delay_seconds:
            self._copy_instead(
                text,
                f"dictation was {age:.1f}s old; copied instead of typing",
            )
            return
        if self.cfg.dictation.require_same_window:
            current_window_id = focus.window_id()
            if job.window_id is None or current_window_id is None:
                self._copy_instead(
                    text, "could not verify focus; copied instead of typing"
                )
                return
            if current_window_id != job.window_id:
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
        notify("✓ Typed" + suffix, channel="dictate")

    def _copy_instead(self, text: str, reason: str) -> None:
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
                continue
            except Exception as exc:
                log.exception("unexpected agent dispatch failure")
                self._delivery_failed(
                    "voicekey: agent dispatch failed",
                    f"{type(exc).__name__}: {exc}",
                    text,
                )
                continue
            notify("✓ Sent to agent", target, ms=10000, channel="agent")

    @staticmethod
    def _delivery_failed(summary: str, detail: str, text: str) -> None:
        try:
            path = recovery.save(text)
            body = f"{detail}\nTranscript saved to {path}"
        except OSError as exc:
            body = f"{detail}\nTranscript recovery also failed: {exc}"
        notify(summary, body, error=True)

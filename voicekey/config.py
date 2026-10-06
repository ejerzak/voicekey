"""Load and validate the per-host voicekey TOML configuration."""

from __future__ import annotations

import logging
import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from typing import Any

log = logging.getLogger("voicekey.config")

DEFAULT_PATH = os.path.expanduser("~/.config/voicekey/config.toml")
MODELS_DIR = "~/.local/share/voicekey"
BACKEND_TYPES = {"faster-whisper", "parakeet"}
POLISH_BACKENDS = {"none", "openai"}
POLISH_FORMATS = {"s1-mini", "instruct"}
S1_MINI_STYLES = ("casual", "semi-casual", "semi-formal", "formal")
AGENT_TARGETS = {"hermes", "command"}
AGENT_TRANSPORTS = {"local", "ssh-over-tailscale"}
TMUX_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
REMOTE_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"
)
REMOTE_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class ConfigError(Exception):
    pass


def key_chord_names(value: str) -> tuple[str, ...]:
    """Return the evdev key names in a ``+``-separated key chord."""
    names = tuple(name.strip() for name in value.split("+"))
    if not names or any(not name for name in names):
        raise ConfigError(f"invalid key chord {value!r}")
    if len(names) != len(set(names)):
        raise ConfigError(f"key chord contains a duplicate key: {value!r}")
    return names


@dataclass
class BackendConfig:
    """Offline model for the final pass."""

    type: str = "parakeet"
    model_dir: str = (
        f"{MODELS_DIR}/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming"
    )
    # faster-whisper only
    model: str = "large-v3-turbo"
    device: str = "auto"
    compute_type: str = "default"


@dataclass
class StreamingConfig:
    """Streaming model for the live preview; an empty model_dir disables it."""

    model_dir: str = (
        f"{MODELS_DIR}/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
    )


@dataclass
class PersistentConfig:
    destination_policy: str = "pause"  # pause on window switch, follow, or pin
    draft: bool = False  # editor-only draft; insert once on explicit acceptance
    draft_cancel_key: str = "KEY_ESC"
    key: str = ""  # optional additional dictation toggle chord
    vad_model: str = f"{MODELS_DIR}/silero_vad.onnx"
    pause_seconds: float = 1.2
    silence_seconds: float = 60.0
    max_utterance_seconds: float = 30.0
    pre_roll_seconds: float = 0.3
    delivery_seconds: float = 5.0
    polish_min_words: int = 0
    polish_context: bool = True
    drop_filler_only: bool = True


@dataclass
class DictationConfig:
    ime: bool = True
    inject: str = "wtype"
    max_delay_seconds: float = 10.0
    require_same_window: bool = True
    post_transcription_hook: str = ""
    multiline_apps: list[str] = field(default_factory=list)


@dataclass
class TextConfig:
    word_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class PolishServerConfig:
    """llama-server run by voicekey itself, as a child process, when
    ``model_file`` names a GGUF; otherwise ``[polish] url`` must point at a
    server someone else runs (Ollama, a llama-server on another host)."""

    command: str = f"{MODELS_DIR}/llama.cpp/llama-server"  # install.sh fetches it; or "llama-server" from PATH
    model_file: str = ""
    threads: int = 4  # 4 is the knee on a laptop CPU; more contends with the recognizers
    context: int = 2048  # room for a paragraph and its reply; the KV cache costs 115 MB per 1024


@dataclass
class PolishConfig:
    """Third pass: a language model cleans the transcript before it lands.
    Off by default; bounded by ``max_wait_seconds``, past which the raw
    transcript lands unchanged."""

    backend: str = "none"  # "none" | "openai" (any OpenAI-compatible chat endpoint)
    url: str = "http://127.0.0.1:8642/v1"
    model: str = "s1-mini"  # the name sent in requests; llama-server ignores it, Ollama needs it
    format: str = "s1-mini"  # "s1-mini" (its trained prompt) | "instruct" (our prompt, any model)
    style: str = "semi-formal"
    app_styles: dict[str, str] = field(default_factory=dict)  # exact destination app IDs
    prompt_file: str = ""  # instruct: a file replacing the built-in prompt
    api_key_file: str = ""  # for a server that is not voicekey's own child
    timeout_seconds: float = 10.0  # one request
    max_wait_seconds: float = 4.0  # past this the raw transcript lands
    min_words: int = 8  # short corrections land without a language-model round trip
    server: PolishServerConfig = field(default_factory=PolishServerConfig)


@dataclass
class PipelineConfig:
    max_pending: int = 8
    max_audio_seconds: float = 180.0
    transcription_seconds: float = 30.0
    shutdown_seconds: float = 10.0
    journal_seconds: float = 3.0
    recovery_megabytes: int = 256
    history_days: int = 7


@dataclass
class AgentConfig:
    """Persistent Hermes or a local command accepting transcripts on stdin."""

    target: str = "hermes"
    command: list[str] = field(default_factory=list)
    transport: str = "local"
    remote_host: str = ""
    remote_user: str = ""
    identity_file: str = ""
    tmux_socket: str = "voicekey-hermes"
    tmux_session: str = "voicekey-hermes"
    working_directory: str = "~/.local/share/voicekey/hermes"
    terminal: str = "ghostty"
    terminal_title: str = "Voicekey Hermes"
    open_terminal: bool = True
    command_timeout: float = 10.0
    ready_timeout: float = 300.0
    post_transcription_hook: str = ""


@dataclass
class Config:
    dictate_key: str = "KEY_RIGHTMETA"
    agent_key: str = "KEY_RIGHTALT+KEY_RIGHTMETA"
    dictate_toggle_key: str = ""
    agent_toggle_key: str = ""
    language: str = "en"
    tap_seconds: float = 0.25
    max_seconds: float = 90.0
    recordings_dir: str = ""
    backend: BackendConfig = field(default_factory=BackendConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    persistent: PersistentConfig = field(default_factory=PersistentConfig)
    dictation: DictationConfig = field(default_factory=DictationConfig)
    polish: PolishConfig = field(default_factory=PolishConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    text: TextConfig = field(default_factory=TextConfig)


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.pop(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a TOML table")
    return value


def _apply(obj: object, data: dict[str, Any], section: str) -> None:
    known = {item.name for item in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"unknown config key {section}{key}")
        setattr(obj, key, value)


def _string(name: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ConfigError(f"{name} must be {qualifier}")
    return value


def _path(name: str, value: Any) -> str:
    value = _string(name, value, allow_empty=True)
    return os.path.abspath(os.path.expanduser(value)) if value else ""


def _number(name: str, value: Any, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ConfigError(f"{name} must be finite and >= {minimum:g}")
    return result


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _integer(name: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{name} must be an integer >= {minimum}")
    return value


def _validate_polish(cfg: PolishConfig) -> None:
    cfg.backend = _string("polish.backend", cfg.backend)
    if cfg.backend not in POLISH_BACKENDS:
        raise ConfigError(
            f"polish.backend must be one of {', '.join(sorted(POLISH_BACKENDS))}, "
            f"got {cfg.backend!r}"
        )
    cfg.url = _string("polish.url", cfg.url).rstrip("/")
    if not cfg.url.startswith(("http://", "https://")):
        raise ConfigError("polish.url must start with http:// or https://")
    cfg.model = _string("polish.model", cfg.model)
    cfg.format = _string("polish.format", cfg.format)
    if cfg.format not in POLISH_FORMATS:
        raise ConfigError(
            f"polish.format must be one of {', '.join(sorted(POLISH_FORMATS))}, "
            f"got {cfg.format!r}"
        )
    cfg.style = _string("polish.style", cfg.style)
    if cfg.format == "s1-mini" and cfg.style not in S1_MINI_STYLES:
        raise ConfigError(
            f"polish.style must be one of {', '.join(S1_MINI_STYLES)} for the "
            f"s1-mini format, got {cfg.style!r}"
        )
    cfg.prompt_file = _path("polish.prompt_file", cfg.prompt_file)
    if not isinstance(cfg.app_styles, dict):
        raise ConfigError("polish.app_styles must be a table of app IDs to styles")
    for app_id, style in cfg.app_styles.items():
        _string("polish.app_styles app ID", app_id)
        _string(f"polish.app_styles.{app_id}", style)
        if cfg.format == "s1-mini" and style not in S1_MINI_STYLES:
            raise ConfigError(f"polish.app_styles.{app_id} must be one of {', '.join(S1_MINI_STYLES)}")
    cfg.api_key_file = _path("polish.api_key_file", cfg.api_key_file)
    cfg.timeout_seconds = _number("polish.timeout_seconds", cfg.timeout_seconds, minimum=0.1)
    cfg.max_wait_seconds = _number("polish.max_wait_seconds", cfg.max_wait_seconds, minimum=0.1)
    cfg.min_words = _integer("polish.min_words", cfg.min_words)
    command = _string("polish.server.command", cfg.server.command)
    # A bare name is looked up on PATH; a path may start with ~.
    cfg.server.command = _path("polish.server.command", command) if "/" in command else command
    cfg.server.model_file = _path("polish.server.model_file", cfg.server.model_file)
    cfg.server.threads = _integer("polish.server.threads", cfg.server.threads, minimum=1)
    cfg.server.context = _integer("polish.server.context", cfg.server.context, minimum=512)


def _validate(cfg: Config) -> None:
    cfg.dictate_key = _string("dictate_key", cfg.dictate_key)
    cfg.agent_key = _string("agent_key", cfg.agent_key)
    cfg.dictate_toggle_key = _string(
        "dictate_toggle_key", cfg.dictate_toggle_key, allow_empty=True
    )
    cfg.agent_toggle_key = _string(
        "agent_toggle_key", cfg.agent_toggle_key, allow_empty=True
    )
    cfg.persistent.key = _string("persistent.key", cfg.persistent.key, allow_empty=True)
    cfg.persistent.draft = _boolean("persistent.draft", cfg.persistent.draft)
    cfg.persistent.draft_cancel_key = _string("persistent.draft_cancel_key", cfg.persistent.draft_cancel_key)
    key_chord_names(cfg.persistent.draft_cancel_key)
    cfg.language = _string("language", cfg.language, allow_empty=True)
    cfg.tap_seconds = _number("tap_seconds", cfg.tap_seconds, minimum=0.01)
    cfg.max_seconds = _number("max_seconds", cfg.max_seconds, minimum=0.1)
    if cfg.tap_seconds >= cfg.max_seconds:
        raise ConfigError("tap_seconds must be less than max_seconds")
    cfg.recordings_dir = _path("recordings_dir", cfg.recordings_dir)
    configured_keys = [
        frozenset(key_chord_names(key))
        for key in (
            cfg.dictate_key,
            cfg.agent_key,
            cfg.dictate_toggle_key,
            cfg.agent_toggle_key,
            cfg.persistent.key,
            cfg.persistent.draft_cancel_key if cfg.persistent.draft else "",
        )
        if key
    ]
    if len(configured_keys) != len(set(configured_keys)):
        raise ConfigError("configured voice key chords must differ")

    cfg.backend.type = _string("backend.type", cfg.backend.type)
    if cfg.backend.type not in BACKEND_TYPES:
        raise ConfigError(
            f"backend.type must be one of {', '.join(sorted(BACKEND_TYPES))}, "
            f"got {cfg.backend.type!r}"
        )
    cfg.backend.model_dir = _path("backend.model_dir", cfg.backend.model_dir)
    for name in ("model", "device", "compute_type"):
        setattr(
            cfg.backend,
            name,
            _string(f"backend.{name}", getattr(cfg.backend, name), allow_empty=True),
        )
    cfg.streaming.model_dir = _path("streaming.model_dir", cfg.streaming.model_dir)
    if cfg.persistent.destination_policy not in ("pause", "follow", "pin"):
        raise ConfigError("persistent.destination_policy must be 'pause', 'follow', or 'pin'")
    cfg.persistent.vad_model = _path("persistent.vad_model", cfg.persistent.vad_model)
    cfg.persistent.polish_context = _boolean("persistent.polish_context", cfg.persistent.polish_context)
    cfg.persistent.polish_min_words = _integer("persistent.polish_min_words", cfg.persistent.polish_min_words)
    cfg.persistent.drop_filler_only = _boolean("persistent.drop_filler_only", cfg.persistent.drop_filler_only)
    for name in ("pause_seconds", "silence_seconds", "max_utterance_seconds", "pre_roll_seconds", "delivery_seconds"):
        setattr(cfg.persistent, name, _number(f"persistent.{name}", getattr(cfg.persistent, name), minimum=0.1))
    p = cfg.persistent
    if not p.pre_roll_seconds < p.pause_seconds < p.max_utterance_seconds:
        raise ConfigError("persistent requires pre_roll_seconds < pause_seconds < max_utterance_seconds")
    if p.silence_seconds <= p.pause_seconds:
        raise ConfigError("persistent.silence_seconds must exceed pause_seconds")

    cfg.dictation.ime = _boolean("dictation.ime", cfg.dictation.ime)
    cfg.dictation.inject = _string("dictation.inject", cfg.dictation.inject)
    if cfg.dictation.inject not in ("wtype", "clipboard"):
        raise ConfigError(
            "dictation.inject must be 'wtype' or 'clipboard', "
            f"got {cfg.dictation.inject!r}"
        )
    cfg.dictation.max_delay_seconds = _number(
        "dictation.max_delay_seconds", cfg.dictation.max_delay_seconds, minimum=0.1
    )
    cfg.dictation.require_same_window = _boolean(
        "dictation.require_same_window", cfg.dictation.require_same_window
    )
    if not isinstance(cfg.dictation.multiline_apps, list):
        raise ConfigError("dictation.multiline_apps must be a list of exact app IDs")
    for app in cfg.dictation.multiline_apps:
        _string("dictation.multiline_apps entry", app)

    _validate_polish(cfg.polish)

    if not isinstance(cfg.text.word_overrides, dict):
        raise ConfigError("text.word_overrides must be a table of phrases to replacements")
    seen = set()
    for phrase, replacement in cfg.text.word_overrides.items():
        _string("text.word_overrides phrase", phrase)
        _string(f"text.word_overrides.{phrase}", replacement)
        if phrase.lower() in seen:
            raise ConfigError("text.word_overrides phrases must differ ignoring case")
        seen.add(phrase.lower())
    for name in ("dictation", "agent"):
        section = getattr(cfg, name)
        _string(f"{name}.post_transcription_hook", section.post_transcription_hook, allow_empty=True)

    for name in ("max_pending", "recovery_megabytes", "history_days"):
        setattr(cfg.pipeline, name, _integer(f"pipeline.{name}", getattr(cfg.pipeline, name), minimum=1))
    for name in ("max_audio_seconds", "transcription_seconds", "shutdown_seconds", "journal_seconds"):
        setattr(cfg.pipeline, name, _number(f"pipeline.{name}", getattr(cfg.pipeline, name), minimum=0.1))
    if cfg.pipeline.max_audio_seconds < cfg.max_seconds:
        raise ConfigError("pipeline.max_audio_seconds must be at least max_seconds")
    if p.max_utterance_seconds + 4 > cfg.pipeline.max_audio_seconds:
        raise ConfigError("pipeline.max_audio_seconds must allow a persistent utterance plus 4 seconds")

    cfg.agent.target = _string("agent.target", cfg.agent.target)
    if cfg.agent.target not in AGENT_TARGETS:
        raise ConfigError(
            f"agent.target must be one of {', '.join(sorted(AGENT_TARGETS))}, "
            f"got {cfg.agent.target!r}"
        )
    cfg.agent.transport = _string("agent.transport", cfg.agent.transport)
    if not isinstance(cfg.agent.command, list) or any(
        not isinstance(arg, str) or "\0" in arg for arg in cfg.agent.command
    ):
        raise ConfigError("agent.command must be an array of strings without NUL bytes")
    if cfg.agent.command:
        executable = _string("agent.command executable", cfg.agent.command[0])
        cfg.agent.command[0] = _path("agent.command executable", executable) if "/" in executable else executable
    if cfg.agent.target == "command":
        if not cfg.agent.command:
            raise ConfigError("agent.command requires an executable and optional arguments")
        if cfg.agent.transport != "local":
            raise ConfigError("agent.target = 'command' requires agent.transport = 'local'")
    if cfg.agent.transport not in AGENT_TRANSPORTS:
        raise ConfigError(
            "agent.transport must be one of "
            f"{', '.join(sorted(AGENT_TRANSPORTS))}, "
            f"got {cfg.agent.transport!r}"
        )
    if cfg.agent.target == "hermes":
        cfg.agent.remote_host = _string(
            "agent.remote_host", cfg.agent.remote_host, allow_empty=True
        )
        cfg.agent.remote_user = _string(
            "agent.remote_user", cfg.agent.remote_user, allow_empty=True
        )
        cfg.agent.identity_file = _string(
            "agent.identity_file", cfg.agent.identity_file, allow_empty=True
        )
        if cfg.agent.transport == "ssh-over-tailscale":
            if not REMOTE_HOST_RE.fullmatch(cfg.agent.remote_host):
                raise ConfigError(
                    "agent.remote_host must be a MagicDNS name for "
                    "ssh-over-tailscale"
                )
            if not REMOTE_USER_RE.fullmatch(cfg.agent.remote_user):
                raise ConfigError(
                    "agent.remote_user must be a Linux user name for "
                    "ssh-over-tailscale"
                )
            if not cfg.agent.identity_file:
                raise ConfigError(
                    "agent.identity_file is required for ssh-over-tailscale"
                )
            cfg.agent.identity_file = os.path.abspath(
                os.path.expanduser(cfg.agent.identity_file)
            )
        elif cfg.agent.remote_host or cfg.agent.remote_user or cfg.agent.identity_file:
            raise ConfigError(
                "agent remote fields require "
                "agent.transport = 'ssh-over-tailscale'"
            )
        for name in ("tmux_socket", "tmux_session"):
            value = _string(f"agent.{name}", getattr(cfg.agent, name))
            if not TMUX_NAME_RE.fullmatch(value):
                raise ConfigError(
                    f"agent.{name} must contain 1-48 letters, digits, '_' or '-'"
                )
            setattr(cfg.agent, name, value)
    working_directory = _string("agent.working_directory", cfg.agent.working_directory)
    if "\0" in working_directory:
        raise ConfigError("agent.working_directory may not contain NUL bytes")
    if working_directory != working_directory.strip():
        raise ConfigError("agent.working_directory may not begin or end with whitespace")
    if cfg.agent.transport == "ssh-over-tailscale":
        # Resolved on the remote host, where `~/` is the remote user's home;
        # expanding it here would name this machine's.
        working_directory = os.path.normpath(working_directory)
        if working_directory == "~":
            raise ConfigError("agent.working_directory may not be a bare home")
        if not (working_directory.startswith("~/") or os.path.isabs(working_directory)):
            raise ConfigError(
                "agent.working_directory must be absolute or start with ~/ "
                "for ssh-over-tailscale"
            )
    else:
        working_directory = os.path.abspath(os.path.expanduser(working_directory))
    if not working_directory.strip(os.path.sep):  # "/", and "//", which normpath keeps
        raise ConfigError("agent.working_directory may not be the filesystem root")
    cfg.agent.working_directory = working_directory
    if cfg.agent.target == "hermes":
        cfg.agent.terminal = _string("agent.terminal", cfg.agent.terminal)
        if cfg.agent.terminal != "ghostty":
            raise ConfigError("agent.terminal currently supports only 'ghostty'")
        cfg.agent.terminal_title = _string(
            "agent.terminal_title", cfg.agent.terminal_title
        )
        cfg.agent.open_terminal = _boolean(
            "agent.open_terminal", cfg.agent.open_terminal
        )
    cfg.agent.command_timeout = _number(
        "agent.command_timeout", cfg.agent.command_timeout, minimum=0.1
    )
    cfg.agent.ready_timeout = _number(
        "agent.ready_timeout", cfg.agent.ready_timeout, minimum=1.0
    )


def load(path: str | None = None) -> Config:
    path = os.path.expanduser(path or DEFAULT_PATH)
    cfg = Config()
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(
            f"no config at {path} — run install.sh, or copy config.example.toml there"
        )
    except PermissionError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}")

    if not isinstance(data, dict):
        raise ConfigError("config root must be a TOML table")
    for section, target in (
        ("backend", cfg.backend),
        ("streaming", cfg.streaming),
        ("persistent", cfg.persistent),
        ("dictation", cfg.dictation),
        ("agent", cfg.agent),
        ("pipeline", cfg.pipeline),
        ("text", cfg.text),
    ):
        values = _table(data, section)
        if section == "persistent" and "follow_focus" in values:
            legacy = _boolean("persistent.follow_focus", values.pop("follow_focus"))
            if "destination_policy" in values:
                raise ConfigError("use persistent.destination_policy instead of follow_focus, not both")
            values["destination_policy"] = "follow" if legacy else "pin"
        _apply(target, values, f"{section}.")
    polish = _table(data, "polish")
    _apply(cfg.polish.server, _table(polish, "server"), "polish.server.")
    _apply(cfg.polish, polish, "polish.")
    if "min_seconds" in data:
        data.pop("min_seconds")
        log.warning("min_seconds is retired and ignored; tap_seconds also bounds the shortest recording")
    _apply(cfg, data, "")
    _validate(cfg)
    return cfg

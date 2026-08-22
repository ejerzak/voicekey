"""Load and validate the per-host voicekey TOML configuration."""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from typing import Any

DEFAULT_PATH = os.path.expanduser("~/.config/voicekey/config.toml")
BACKEND_TYPES = {"faster-whisper", "parakeet", "remote"}
AGENT_TARGETS = {"hermes"}
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
    type: str = "faster-whisper"
    model: str = "large-v3-turbo"
    device: str = "auto"
    compute_type: str = "default"
    model_dir: str = ""
    url: str = ""


@dataclass
class DictationConfig:
    inject: str = "wtype"
    max_delay_seconds: float = 10.0
    require_same_window: bool = True


@dataclass
class AgentConfig:
    """Persistent Hermes target; future targets implement the same seam."""

    target: str = "hermes"
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


@dataclass
class Config:
    dictate_key: str = "KEY_F9"
    agent_key: str = "KEY_F10"
    dictate_toggle_key: str = ""
    agent_toggle_key: str = ""
    language: str = "en"
    min_seconds: float = 0.3
    max_seconds: float = 90.0
    backend: BackendConfig = field(default_factory=BackendConfig)
    dictation: DictationConfig = field(default_factory=DictationConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


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


def _validate(cfg: Config) -> None:
    cfg.dictate_key = _string("dictate_key", cfg.dictate_key)
    cfg.agent_key = _string("agent_key", cfg.agent_key)
    cfg.dictate_toggle_key = _string(
        "dictate_toggle_key", cfg.dictate_toggle_key, allow_empty=True
    )
    cfg.agent_toggle_key = _string(
        "agent_toggle_key", cfg.agent_toggle_key, allow_empty=True
    )
    cfg.language = _string("language", cfg.language, allow_empty=True)
    cfg.min_seconds = _number("min_seconds", cfg.min_seconds)
    cfg.max_seconds = _number("max_seconds", cfg.max_seconds, minimum=0.1)
    if cfg.min_seconds >= cfg.max_seconds:
        raise ConfigError("min_seconds must be less than max_seconds")
    configured_keys = [
        frozenset(key_chord_names(key))
        for key in (
            cfg.dictate_key,
            cfg.agent_key,
            cfg.dictate_toggle_key,
            cfg.agent_toggle_key,
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
    for name in ("model", "device", "compute_type", "model_dir", "url"):
        setattr(
            cfg.backend,
            name,
            _string(f"backend.{name}", getattr(cfg.backend, name), allow_empty=True),
        )

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

    cfg.agent.target = _string("agent.target", cfg.agent.target)
    if cfg.agent.target not in AGENT_TARGETS:
        raise ConfigError(
            f"agent.target must be one of {', '.join(sorted(AGENT_TARGETS))}, "
            f"got {cfg.agent.target!r}"
        )
    cfg.agent.transport = _string("agent.transport", cfg.agent.transport)
    if cfg.agent.transport not in AGENT_TRANSPORTS:
        raise ConfigError(
            "agent.transport must be one of "
            f"{', '.join(sorted(AGENT_TRANSPORTS))}, "
            f"got {cfg.agent.transport!r}"
        )
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
    working_directory = os.path.abspath(
        os.path.expanduser(
            _string("agent.working_directory", cfg.agent.working_directory)
        )
    )
    if working_directory == os.path.sep:
        raise ConfigError("agent.working_directory may not be the filesystem root")
    cfg.agent.working_directory = working_directory
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
            f"no config at {path} — run install step 07-voicekey, or copy "
            "apps/voicekey/config.example.toml there"
        )
    except PermissionError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}")

    if not isinstance(data, dict):
        raise ConfigError("config root must be a TOML table")
    backend = _table(data, "backend")
    dictation = _table(data, "dictation")
    agent = _table(data, "agent")
    _apply(cfg.backend, backend, "backend.")
    _apply(cfg.dictation, dictation, "dictation.")
    _apply(cfg.agent, agent, "agent.")
    _apply(cfg, data, "")
    _validate(cfg)
    return cfg

"""Dispatch voice prompts to a persistent Hermes TUI.

Hermes runs in a dedicated tmux server supervised by a transient systemd user
unit.  Ghostty is only a client: closing its window detaches from tmux without
stopping Hermes.  Prompt text enters tmux through stdin, never argv or logs.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .config import AgentConfig

log = logging.getLogger("voicekey.agent")

_EMPTY_PLACEHOLDERS = ("Ask me anything", 'Try "')
_PROMPT_ONLY_RE = re.compile(r"^\s*(?:[A-Za-z0-9_-]+\s+)?[❯>$#›»→]\s*$")
_READY_RE = re.compile(r"(?:^|[─\s])ready(?:\s|│|$)", re.MULTILINE)
_POLL_SECONDS = 0.2


class AgentError(Exception):
    pass


def _require(command: str) -> str:
    path = shutil.which(command)
    if path is None:
        raise AgentError(
            f"{command} not found — install it and rerun install step 07-voicekey"
        )
    return path


def _run(
    argv: list[str],
    *,
    timeout: float,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise AgentError(f"command timed out after {timeout:.0f}s: {argv[0]}")
    except OSError as exc:
        raise AgentError(f"could not run {argv[0]}: {exc}")
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise AgentError(
            f"{os.path.basename(argv[0])} failed: {detail or 'no output'}"
        )
    return result


def _tmux(
    cfg: AgentConfig,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [_require("tmux"), "-L", cfg.tmux_socket, *args],
        timeout=cfg.command_timeout,
        input_text=input_text,
        check=check,
    )


def _server_running(cfg: AgentConfig) -> bool:
    result = _tmux(cfg, "show-options", "-gv", "exit-empty", check=False)
    return result.returncode == 0


def _start_server(cfg: AgentConfig) -> None:
    """Start a foreground tmux server in a systemd-owned transient unit."""
    systemd_run = _require("systemd-run")
    tmux = _require("tmux")
    unit = f"voicekey-{cfg.tmux_socket}-tmux"
    result = _run(
        [
            systemd_run,
            "--user",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--description=Voicekey persistent Hermes tmux server",
            "--property=Restart=always",
            "--property=RestartSec=2s",
            "--",
            tmux,
            "-L",
            cfg.tmux_socket,
            "-f",
            "/dev/null",
            "-D",
        ],
        timeout=cfg.command_timeout,
        check=False,
    )
    if result.returncode != 0 and "already exists" not in result.stderr.lower():
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise AgentError(f"systemd-run failed: {detail or 'no output'}")

    deadline = time.monotonic() + cfg.command_timeout
    while time.monotonic() < deadline:
        if _server_running(cfg):
            return
        time.sleep(_POLL_SECONDS)
    raise AgentError("the dedicated Hermes tmux server did not become ready")


def _ensure_server(cfg: AgentConfig) -> None:
    if not _server_running(cfg):
        _start_server(cfg)


def _workspace(cfg: AgentConfig) -> str:
    path = Path(cfg.working_directory)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentError(f"cannot create Hermes working directory {path}: {exc}")
    if not path.is_dir():
        raise AgentError(f"Hermes working directory is not a directory: {path}")
    return str(path)


def _session_target(cfg: AgentConfig) -> str:
    # This dedicated server contains only voicekey's validated session name.
    # Do not use tmux's documented '=' exact-match prefix here: tmux 3.7b
    # accepts it for has-session but rejects it for session set-option and
    # resolves it incorrectly for some target-pane commands.
    return cfg.tmux_session


def _pane_target(cfg: AgentConfig) -> str:
    return f"{cfg.tmux_session}:0.0"


def _ensure_session(cfg: AgentConfig) -> bool:
    """Ensure Hermes is running; return True when a new session was created."""
    _ensure_server(cfg)
    target = _session_target(cfg)
    exists = _tmux(cfg, "has-session", "-t", target, check=False)
    created = exists.returncode != 0
    if created:
        hermes = _require("hermes")
        command = shlex.join([hermes, "--tui"])
        _tmux(
            cfg,
            "new-session",
            "-d",
            "-s",
            cfg.tmux_session,
            "-c",
            _workspace(cfg),
            command,
        )
        log.info("started persistent Hermes session %s", cfg.tmux_session)

    # Ignore user tmux defaults that would destroy the session on detach, and
    # leave the full terminal to Hermes rather than displaying a tmux status bar.
    _tmux(cfg, "set-option", "-t", target, "destroy-unattached", "off")
    _tmux(cfg, "set-option", "-t", target, "remain-on-exit", "off")
    _tmux(cfg, "set-option", "-g", "status", "off")
    return created


def _ensure_terminal(cfg: AgentConfig) -> bool:
    """Open Ghostty only when the Hermes tmux session has no client."""
    target = _session_target(cfg)
    if _session_has_client(cfg):
        return False

    if not cfg.open_terminal:
        return False
    if cfg.terminal != "ghostty":
        raise AgentError(f"unsupported agent terminal: {cfg.terminal}")

    systemd_run = _require("systemd-run")
    terminal = _require(cfg.terminal)
    tmux = _require("tmux")
    terminal_class = f"voicekey-{cfg.tmux_session}"
    environment = [
        f"--setenv={name}={os.environ[name]}"
        for name in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR")
        if os.environ.get(name)
    ]
    _run(
        [
            systemd_run,
            "--user",
            "--quiet",
            "--collect",
            "--description=Voicekey Hermes terminal",
            *environment,
            "--",
            terminal,
            f"--title={cfg.terminal_title}",
            f"--class={terminal_class}",
            "--gtk-single-instance=false",
            "-e",
            tmux,
            "-L",
            cfg.tmux_socket,
            "attach-session",
            "-t",
            target,
        ],
        timeout=cfg.command_timeout,
    )

    deadline = time.monotonic() + cfg.command_timeout
    while time.monotonic() < deadline:
        if _session_has_client(cfg):
            log.info("opened terminal for Hermes session %s", cfg.tmux_session)
            return True
        time.sleep(_POLL_SECONDS)
    raise AgentError("Ghostty opened but did not attach to the Hermes tmux session")


def _session_has_client(cfg: AgentConfig) -> bool:
    clients = _tmux(
        cfg,
        "list-clients",
        "-t",
        _session_target(cfg),
        "-F",
        "#{client_pid}",
        check=False,
    )
    return clients.returncode == 0 and bool(clients.stdout.strip())


def _empty_composer(screen: str) -> bool:
    """Recognize Hermes's idle, empty composer without parsing private text."""
    if not _READY_RE.search(screen):
        return False
    # The composer and an optional bottom status bar occupy the final few rows.
    # Looking farther back risks mistaking quoted response text for a prompt.
    tail = screen.splitlines()[-5:]
    return any(marker in "\n".join(tail) for marker in _EMPTY_PLACEHOLDERS) or any(
        _PROMPT_ONLY_RE.fullmatch(line) for line in tail
    )


def _wait_for_empty_composer(cfg: AgentConfig) -> None:
    deadline = time.monotonic() + cfg.ready_timeout
    while True:
        if _tmux(
            cfg, "has-session", "-t", _session_target(cfg), check=False
        ).returncode != 0:
            raise AgentError("Hermes exited before the prompt could be delivered")
        screen = _tmux(
            cfg, "capture-pane", "-p", "-t", _pane_target(cfg)
        ).stdout
        if _empty_composer(screen):
            return
        if time.monotonic() >= deadline:
            raise AgentError(
                "Hermes did not reach an idle, empty composer within "
                f"{cfg.ready_timeout:.0f}s; it may be running, awaiting input, "
                "or contain a textual draft"
            )
        time.sleep(_POLL_SECONDS)


def _wait_for_composer_text(cfg: AgentConfig) -> None:
    """Wait for Hermes's asynchronous bracketed-paste handler to commit."""
    deadline = time.monotonic() + cfg.command_timeout
    while time.monotonic() < deadline:
        screen = _tmux(
            cfg, "capture-pane", "-p", "-t", _pane_target(cfg)
        ).stdout
        if not _empty_composer(screen):
            return
        time.sleep(_POLL_SECONDS)
    raise AgentError("Hermes did not place the voice prompt in its composer")


def _wait_for_submission_started(cfg: AgentConfig) -> None:
    """Confirm Enter cleared the composer or moved Hermes out of ready state."""
    deadline = time.monotonic() + cfg.command_timeout
    while time.monotonic() < deadline:
        screen = _tmux(
            cfg, "capture-pane", "-p", "-t", _pane_target(cfg)
        ).stdout
        if _empty_composer(screen) or not _READY_RE.search(screen):
            return
        time.sleep(_POLL_SECONDS)
    raise AgentError("Hermes did not submit the voice prompt")


def _safe_prompt(text: str) -> str:
    # Speech transcripts should be one logical line. Collapsing whitespace
    # removes terminal control characters rather than injecting them into a PTY.
    prompt = " ".join(text.split())
    if not prompt:
        raise AgentError("agent transcript was empty")
    # Hermes handles leading / and ! as local slash/shell commands and treats
    # absolute paths as file drops. Give those transcripts an ordinary prose
    # prefix. It also executes `{!...}` interpolation anywhere in a message.
    if prompt[0] in "/!":
        prompt = "Voice request: " + prompt
    prompt = prompt.replace("{!", "{ !")
    # A trailing space prevents Hermes's path completer from intercepting the
    # Enter intended to submit. It is semantically inert agent input.
    return prompt + " "


def _paste_prompt(cfg: AgentConfig, text: str) -> None:
    buffer_name = "voicekey-prompt"
    prompt = _safe_prompt(text)
    _tmux(
        cfg,
        "load-buffer",
        "-b",
        buffer_name,
        "-",
        input_text=prompt,
    )
    _tmux(
        cfg,
        "paste-buffer",
        "-p",
        "-d",
        "-b",
        buffer_name,
        "-t",
        _pane_target(cfg),
    )
    _wait_for_composer_text(cfg)
    _tmux(cfg, "send-keys", "-t", _pane_target(cfg), "Enter")
    _wait_for_submission_started(cfg)


def send_prompt(cfg: AgentConfig, text: str) -> str:
    """Queue TEXT in persistent Hermes and return the user-visible target."""
    if cfg.target != "hermes":
        raise AgentError(f"unsupported agent target: {cfg.target}")
    _ensure_session(cfg)
    _ensure_terminal(cfg)
    _wait_for_empty_composer(cfg)
    _paste_prompt(cfg, text)
    log.info(
        "queued agent prompt (%d chars) in Hermes session %s",
        len(text),
        cfg.tmux_session,
    )
    return f"Hermes — {cfg.tmux_session}"

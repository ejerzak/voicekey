"""Dispatch voice prompts to persistent Hermes or a local stdin command.

Hermes runs in a dedicated tmux server supervised by a transient systemd user
unit.  Ghostty is only a client: closing its window detaches from tmux without
stopping Hermes.  Prompt text enters tmux through stdin, never argv or logs.
"""

from __future__ import annotations

import json
import contextvars
import logging
import os
import re
import shlex
import shutil
import signal
import selectors
import subprocess
import time
from pathlib import Path

from . import focus
from .config import AgentConfig

log = logging.getLogger("voicekey.agent")

_EMPTY_PLACEHOLDERS = ("Ask me anything", 'Try "')
_PROMPT_ONLY_RE = re.compile(r"^\s*(?:[A-Za-z0-9_-]+\s+)?[❯>$#›»→]\s*$")
_READY_RE = re.compile(r"(?:^|[─\s])ready(?:\s|│|$)", re.MULTILINE)
_POLL_SECONDS = 0.2
_operation = contextvars.ContextVar("voicekey_agent_operation", default=(float("inf"), None))


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
    deadline, cancelled = _operation.get()
    remaining = deadline - time.monotonic()
    if remaining <= 0 or (cancelled is not None and cancelled.is_set()):
        raise AgentError("agent operation expired or cancelled")
    timeout = min(timeout, remaining)
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


def _remote(cfg: AgentConfig) -> bool:
    return cfg.transport == "ssh-over-tailscale"


def _remote_destination(cfg: AgentConfig) -> str:
    return f"{cfg.remote_user}@{cfg.remote_host}"


def _ssh_argv(cfg: AgentConfig, *, allocate_tty: bool = False) -> list[str]:
    """Build strict OpenSSH-over-Tailscale arguments, bypassing system config."""
    ssh = _require("ssh")
    tailscale = _require("tailscale")
    known_hosts = Path(os.path.expanduser("~/.ssh/known_hosts"))
    argv = [
        ssh,
        "-F",
        "/dev/null",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        os.path.expanduser(cfg.identity_file),
        "-o",
        f"ProxyCommand={tailscale} nc %h %p",
    ]
    if allocate_tty:
        argv.append("-tt")
    argv.append(_remote_destination(cfg))
    return argv


def _remote_run(
    cfg: AgentConfig,
    argv: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a noninteractive OpenSSH command over the Tailscale network."""
    return _run(
        [*_ssh_argv(cfg), shlex.join(argv)],
        timeout=cfg.command_timeout,
        input_text=input_text,
        check=check,
    )


def _target_executable(cfg: AgentConfig, command: str) -> str:
    return command if _remote(cfg) else _require(command)


def _target_run(
    cfg: AgentConfig,
    argv: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if _remote(cfg):
        return _remote_run(cfg, argv, input_text=input_text, check=check)
    return _run(
        argv,
        timeout=cfg.command_timeout,
        input_text=input_text,
        check=check,
    )


def _remote_executable(cfg: AgentConfig, command: str) -> str:
    """Resolve a remote user-installed command through its login environment."""
    result = _remote_run(
        cfg,
        ["sh", "-lc", 'command -v "$1"', "voicekey", command],
        check=False,
    )
    path = result.stdout.strip().splitlines()
    if result.returncode != 0 or not path or not path[-1].startswith("/"):
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise AgentError(
            f"{command} not found on {_remote_destination(cfg)}"
            + (f": {detail}" if detail else "")
        )
    return path[-1]


def check_target(cfg: AgentConfig) -> str | None:
    """Check availability; never execute the configured command backend."""
    if cfg.target == "command":
        if not cfg.command or shutil.which(cfg.command[0]) is None:
            return "agent command executable not found or not executable"
        if not Path(cfg.working_directory).is_dir():
            return "agent command working directory does not exist or is not a directory"
        if not os.access(cfg.working_directory, os.X_OK):
            return "agent command working directory is not accessible"
        return None
    if not _remote(cfg):
        return None
    try:
        result = _remote_run(
            cfg,
            [
                "sh",
                "-lc",
                "for cmd in tmux systemd-run hermes; do "
                'command -v "$cmd" >/dev/null || { '
                'printf "missing remote command: %s\\n" "$cmd" >&2; '
                "exit 127; }; done",
            ],
            check=False,
        )
    except AgentError as exc:
        return str(exc)
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout).strip()[-500:] or (
        f"could not reach {_remote_destination(cfg)}"
    )


def _tmux(
    cfg: AgentConfig,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _target_run(
        cfg,
        [_target_executable(cfg, "tmux"), "-L", cfg.tmux_socket, *args],
        input_text=input_text,
        check=check,
    )


def _server_running(cfg: AgentConfig) -> bool:
    result = _tmux(cfg, "show-options", "-gv", "exit-empty", check=False)
    return result.returncode == 0


def _start_server(cfg: AgentConfig) -> None:
    """Start a foreground tmux server in a systemd-owned transient unit."""
    systemd_run = _target_executable(cfg, "systemd-run")
    tmux = _target_executable(cfg, "tmux")
    unit = f"voicekey-{cfg.tmux_socket}-tmux"
    result = _target_run(
        cfg,
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


def _remote_workspace(cfg: AgentConfig) -> str:
    """Create the working directory on the remote host and return its
    absolute path there. A `~/` path is the remote user's home, resolved by
    the remote shell; a local expansion would name this machine's home."""
    if cfg.working_directory.startswith("~/"):
        script = 'd="$HOME/$1" && mkdir -p -- "$d" && test -d "$d" && printf %s "$d"'
        argument = cfg.working_directory[2:]
    else:
        script = 'mkdir -p -- "$1" && test -d "$1" && printf %s "$1"'
        argument = cfg.working_directory
    result = _target_run(cfg, ["sh", "-c", script, "voicekey", argument], check=False)
    path = result.stdout.strip()
    if result.returncode != 0 or not path.startswith("/"):
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise AgentError(
            f"cannot create remote Hermes working directory {cfg.working_directory}: "
            f"{detail or 'no output'}"
        )
    return path


def _workspace(cfg: AgentConfig) -> str:
    if _remote(cfg):
        return _remote_workspace(cfg)
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
        hermes = (
            _remote_executable(cfg, "hermes")
            if _remote(cfg)
            else _require("hermes")
        )
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
    """Open one local Ghostty attached to the configured Hermes session."""
    target = _session_target(cfg)
    if not cfg.open_terminal:
        return False  # nothing to open, so nothing to ask the compositor
    if _remote(cfg):
        if _terminal_window_open(cfg):
            return False
    elif _session_has_client(cfg):
        return False

    if cfg.terminal != "ghostty":
        raise AgentError(f"unsupported agent terminal: {cfg.terminal}")

    systemd_run = _require("systemd-run")
    terminal = _require(cfg.terminal)
    environment = [
        f"--setenv={name}={os.environ[name]}"
        for name in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR")
        if os.environ.get(name)
    ]
    baseline_clients = _client_count(cfg) if _remote(cfg) else 0
    if _remote(cfg):
        known_hosts = Path(os.path.expanduser("~/.ssh/known_hosts"))
        try:
            known_hosts_ready = (
                known_hosts.is_file() and known_hosts.stat().st_size > 0
            )
        except OSError:
            known_hosts_ready = False
        if not known_hosts_ready:
            raise AgentError(
                "OpenSSH known_hosts is empty; verify and record the host key "
                f"for {cfg.remote_host} before using remote Voicekey"
            )
        attach_command = [
            *_ssh_argv(cfg, allocate_tty=True),
            shlex.join(
                ["tmux", "-L", cfg.tmux_socket, "attach-session", "-t", target]
            ),
        ]
    else:
        attach_command = [
            _require("tmux"),
            "-L",
            cfg.tmux_socket,
            "attach-session",
            "-t",
            target,
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
            "--gtk-single-instance=false",
            "-e",
            *attach_command,
        ],
        timeout=cfg.command_timeout,
    )

    deadline = time.monotonic() + cfg.command_timeout
    while time.monotonic() < deadline:
        client_attached = _client_count(cfg) > baseline_clients
        window_open = not _remote(cfg) or _terminal_window_open(cfg)
        if client_attached and window_open:
            log.info("opened terminal for Hermes session %s", cfg.tmux_session)
            return True
        time.sleep(_POLL_SECONDS)
    raise AgentError("Ghostty opened but did not attach to the Hermes tmux session")


def _terminal_window_open(cfg: AgentConfig) -> bool:
    """Whether the terminal attached to the remote session is open, asked of
    the local compositor rather than of remote tmux, whose clients may sit
    on other machines. niri lists windows with titles; elsewhere the tmux
    client count is the best available answer."""
    if focus.compositor() != "niri":
        return _session_has_client(cfg)
    return _terminal_window_id(cfg) is not None


def _terminal_window_id(cfg: AgentConfig) -> int | None:
    """The niri id of the local terminal showing the session, by title."""
    result = _run(
        [_require("niri"), "msg", "--json", "windows"],
        timeout=cfg.command_timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise AgentError(f"could not query niri windows: {detail or 'no output'}")
    try:
        windows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AgentError(f"niri returned invalid window data: {exc}")
    if not isinstance(windows, list):
        raise AgentError("niri returned invalid window data: expected a list")
    return next(
        (
            window.get("id")
            for window in windows
            if isinstance(window, dict)
            and window.get("app_id") == "com.mitchellh.ghostty"
            and window.get("title") == cfg.terminal_title
            and isinstance(window.get("id"), int)
        ),
        None,
    )


def _focus_terminal(cfg: AgentConfig) -> bool:
    """Raise the session's terminal; only niri exposes windows to do so."""
    if not cfg.open_terminal or focus.compositor() != "niri":
        return False
    window = _terminal_window_id(cfg)
    if window is None:
        return False
    _run(
        [_require("niri"), "msg", "action", "focus-window", "--id", str(window)],
        timeout=cfg.command_timeout,
    )
    return True


def _client_count(cfg: AgentConfig) -> int:
    clients = _tmux(
        cfg,
        "list-clients",
        "-t",
        _session_target(cfg),
        "-F",
        "#{client_pid}",
        check=False,
    )
    if clients.returncode != 0:
        return 0
    return len([line for line in clients.stdout.splitlines() if line.strip()])


def _session_has_client(cfg: AgentConfig) -> bool:
    return _client_count(cfg) > 0


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


def _send_command(cfg: AgentConfig, text: str, *, cancelled=None, deadline=None) -> str:
    """Send exact UTF-8 text via stdin; child output may contain private text."""
    if cfg.transport != "local":
        raise AgentError("command target requires local transport")
    if not text.strip():
        raise AgentError("agent transcript was empty")
    expires = min(time.monotonic() + cfg.command_timeout,
                  deadline if deadline is not None else time.monotonic() + cfg.ready_timeout)

    def check_operation():
        if cancelled is not None and cancelled.is_set():
            raise AgentError("agent command cancelled")
        if time.monotonic() >= expires:
            raise AgentError("agent command timed out")

    check_operation()
    error = check_target(cfg)
    if error:
        raise AgentError(error)
    process = None
    try:
        check_operation()
        process = subprocess.Popen(
            cfg.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd=cfg.working_directory,
            start_new_session=True,
        )
        payload = memoryview(text.encode("utf-8"))
        os.set_blocking(process.stdin.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while payload:
                check_operation()
                if not selector.select(0.05):
                    continue
                try:
                    written = os.write(process.stdin.fileno(), payload[:4096])
                except BlockingIOError:
                    continue
                except BrokenPipeError:
                    raise AgentError("agent command closed stdin before receiving the transcript") from None
                payload = payload[written:]
        process.stdin.close()
        while True:
            check_operation()
            try:
                process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
        check_operation()
        if process.returncode != 0:
            raise AgentError(f"agent command exited with status {process.returncode}")
    except (OSError, ValueError, UnicodeError):
        # Do not surface child output, argv, or exception data containing input.
        raise AgentError("could not run agent command") from None
    finally:
        if process is not None:
            # Stop members of the original group even if the child has exited.
            # Wrappers must not detach children into new groups or sessions.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if process.stdin is not None:
                process.stdin.close()
    log.info("agent command completed")
    return "Command"


def show(cfg: AgentConfig, *, cancelled=None, deadline=None, focus_only=False) -> None:
    """Start Hermes and its terminal if needed and bring it forward, without a
    prompt. FOCUS_ONLY skips the session and terminal setup, for when a prompt
    dispatch already owns them."""
    if cfg.target != "hermes":
        raise AgentError("the command agent target has no window to open")
    token = _operation.set((deadline if deadline is not None else time.monotonic() + cfg.ready_timeout,
                            cancelled))
    try:
        if not focus_only:
            _ensure_session(cfg)
            _ensure_terminal(cfg)
        _focus_terminal(cfg)
    finally:
        _operation.reset(token)


def send_prompt(cfg: AgentConfig, text: str, *, cancelled=None, deadline=None) -> str:
    """Dispatch TEXT and return the user-visible target."""
    if cfg.target == "command":
        return _send_command(cfg, text, cancelled=cancelled, deadline=deadline)
    if cfg.target != "hermes":
        raise AgentError(f"unsupported agent target: {cfg.target}")
    token = _operation.set((deadline if deadline is not None else time.monotonic() + cfg.ready_timeout,
                            cancelled))
    try:
        _ensure_session(cfg)
        _ensure_terminal(cfg)
        _wait_for_empty_composer(cfg)
        _paste_prompt(cfg, text)
    finally:
        _operation.reset(token)
    log.info(
        "queued agent prompt (%d chars) in Hermes session %s",
        len(text),
        cfg.tmux_session,
    )
    return f"Hermes — {cfg.tmux_session}"

from __future__ import annotations

import argparse
import logging
import math
import shutil
import signal
import sys


def main() -> int:
    parser = argparse.ArgumentParser("voicekey")
    from .control import CAPTURE_COMMANDS, COMMANDS
    parser.add_argument("--control", choices=[c for c in COMMANDS if c not in CAPTURE_COMMANDS],
                        help="query or control the running daemon")
    parser.add_argument("--config", help="config path (default ~/.config/voicekey/config.toml)")
    parser.add_argument("--check", action="store_true",
                        help="load config, models and the input method; list keyboards; exit")
    parser.add_argument("--download", action="store_true",
                        help="fetch model weights (install step, not first keypress)")
    parser.add_argument("--replay", metavar="WAV",
                        help="run a 16 kHz mono WAV through the pipeline as if spoken "
                             "(stop voicekey.service first: only one input method can be bound)")
    parser.add_argument("--agent", action="store_true",
                        help="with --replay: process the transcript as an agent prompt (no polish)")
    parser.add_argument("--persistent", action="store_true", help="with --replay: segment and deliver a continuous session")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--last", action="store_true", help="print the latest prepared dictation, verbatim")
    commands.add_argument("--copy-last", action="store_true", help="copy the latest prepared dictation")
    commands.add_argument("--explain-last", action="store_true", help="show processing and delivery of the latest dictation")
    commands.add_argument("--capture-to-stdout", action="store_true", help="record until Ctrl-C and write text to stdout; no desktop delivery")
    parser.add_argument("--seconds", type=float, help="with --capture-to-stdout: stop after this many seconds (capped by max_seconds)")
    parser.add_argument("--client-name", help="with --capture-to-stdout: application name shown in daemon status")
    parser.add_argument("--preview", action="store_true",
                        help='with --capture-to-stdout: print live transcripts on stderr as Preview; "<json string>"')
    args = parser.parse_args()
    if args.control:
        if any((args.check, args.download, args.replay, args.agent, args.persistent,
                args.last, args.copy_last, args.explain_last, args.capture_to_stdout,
                args.seconds is not None, args.client_name is not None, args.preview, args.config)):
            parser.error("--control cannot be combined with other commands or --config")
        import json
        from .control import request
        try:
            result = request(args.control)
        except (OSError, ValueError) as exc:
            print(f"Voicekey control unavailable: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 1 if result.get("error") else 0
    history = args.last or args.copy_last or args.explain_last
    if (history or args.capture_to_stdout) and (args.check or args.download or args.agent or args.persistent):
        parser.error("history and stdout commands cannot be combined with --check, --download, --agent or --persistent")
    if history and args.replay:
        parser.error("history commands cannot be combined with --replay")
    if args.seconds is not None and (not args.capture_to_stdout or not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error("--seconds requires --capture-to-stdout and a positive finite duration")
    if args.client_name is not None and (not args.capture_to_stdout or not args.client_name
            or len(args.client_name) > 80 or not args.client_name.isprintable()
            or args.client_name != args.client_name.strip()):
        parser.error("--client-name requires --capture-to-stdout and 1–80 printable characters without surrounding whitespace")
    if args.preview and not args.capture_to_stdout:
        parser.error("--preview requires --capture-to-stdout")
    if args.persistent and (not args.replay or args.agent):
        parser.error("--persistent requires --replay and cannot be combined with --agent")

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    if history:
        from .history import show
        return show(copy=args.copy_last, diagnostic=args.explain_last)

    if args.capture_to_stdout:
        if args.config:
            parser.error("--capture-to-stdout uses the running daemon configuration; --config is not supported")
        from .stdout import capture
        return capture(wav=args.replay, seconds=args.seconds, client_name=args.client_name,
                       preview=args.preview)

    from .config import ConfigError, load
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    if args.download:
        from .backends import predownload
        try:
            predownload(cfg.backend, cfg.streaming, cfg.polish, cfg.persistent)
        except Exception as exc:
            print(f"model download failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.check:
        return check(cfg)

    from .daemon import Daemon, fix_environment
    daemon = Daemon(cfg)
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    previous_sigterm = signal.signal(signal.SIGTERM, stop)
    try:
        if args.replay:
            fix_environment()
            daemon.load()
            if args.persistent and daemon.vad is None:
                from .segment import SpeechDetector
                daemon.vad = SpeechDetector(cfg.persistent.vad_model)
            daemon.start_workers()
            daemon.replay(args.replay, "persistent" if args.persistent else "agent" if args.agent else "dictate")
        else:
            daemon.run()
    except KeyboardInterrupt:
        pass
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            daemon.close()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


def check(cfg) -> int:
    """Exit 0 when fully ready, 2 when no keyboard is readable, 3 when only
    the agent target is unavailable, 1 for a config, dependency or model failure."""
    from evdev import InputDevice, ecodes

    from . import focus
    from .backends import BackendUnavailable, create_backend, create_streaming
    from .config import key_chord_names
    from .daemon import fix_environment
    from .ime import ImeUnavailable, InputMethod
    from .listener import _supports_any_key, all_event_devices
    from .polish import PolishError, diagnostic_polisher

    fix_environment()
    key_names = (cfg.dictate_key, cfg.agent_key, cfg.dictate_toggle_key, cfg.agent_toggle_key, cfg.persistent.key)
    chord_key_names = {
        name for chord in key_names if chord for name in key_chord_names(chord)
    }
    unknown_keys = sorted(
        name for name in chord_key_names if not isinstance(ecodes.ecodes.get(name), int)
    )
    if unknown_keys:
        print(f"config error: unknown key name(s): {', '.join(unknown_keys)}", file=sys.stderr)
        return 1
    keycodes = {ecodes.ecodes[name] for name in chord_key_names}

    print(f"keys: dictate={cfg.dictate_key} agent={cfg.agent_key} inject={cfg.dictation.inject}")
    if cfg.dictate_toggle_key or cfg.agent_toggle_key:
        print(f"toggle keys: dictate={cfg.dictate_toggle_key or '(none)'} "
              f"agent={cfg.agent_toggle_key or '(none)'}")
    if cfg.persistent.key:
        print(f"persistent key: {cfg.persistent.key}; silence shutoff: {cfg.persistent.silence_seconds:g}s")
    print(f"agent: hermes transport={cfg.agent.transport} "
          f"tmux session={cfg.agent.tmux_session} cwd={cfg.agent.working_directory}")
    if cfg.agent.transport == "ssh-over-tailscale":
        print(f"agent remote: {cfg.agent.remote_user}@{cfg.agent.remote_host}")

    required = {"notify-send", "pw-record", "wl-copy"}
    if cfg.dictation.inject == "wtype":
        required.add("wtype")
    if cfg.polish.backend != "none" and cfg.polish.server.model_file:
        required.add(cfg.polish.server.command)
    compositor = focus.compositor()
    focus_tool = {"niri": "niri", "sway": "swaymsg", "hyprland": "hyprctl"}.get(compositor)
    if focus_tool:
        print(f"compositor: {compositor}")
        required.add(focus_tool)
    elif cfg.dictation.require_same_window:
        print("WARNING: no niri, sway or Hyprland detected: the focused window cannot be "
              "verified, so dictations will be copied, not typed — set "
              "dictation.require_same_window = false", file=sys.stderr)
    agent_required = {"systemd-run"}
    if cfg.agent.transport == "ssh-over-tailscale":
        agent_required.update({"ssh", "tailscale"})
    else:
        agent_required.update({"hermes", "tmux"})
    if cfg.agent.open_terminal:
        agent_required.add(cfg.agent.terminal)
    if compositor != "niri":
        print("WARNING: the agent key needs niri to find the agent's window", file=sys.stderr)
    missing = sorted(command for command in required if shutil.which(command) is None)
    agent_missing = sorted(command for command in agent_required if shutil.which(command) is None)
    for command in sorted(required - set(missing)):
        print(f"command: {command}: OK")
    for command in sorted(agent_required - set(agent_missing)):
        print(f"agent command: {command}: OK")
    if missing:
        print(f"missing command(s): {', '.join(missing)}", file=sys.stderr)
    if agent_missing:
        print("WARNING: agent target unavailable; missing command(s): "
              f"{', '.join(agent_missing)}", file=sys.stderr)
    agent_error = None
    if not agent_missing:
        from .agent import check_target
        agent_error = check_target(cfg.agent)
        if agent_error:
            print("WARNING: agent target unavailable: " + agent_error, file=sys.stderr)
        elif cfg.agent.transport == "ssh-over-tailscale":
            print("agent remote target: OK")

    key_devices, denied = [], 0
    for path in sorted(all_event_devices()):
        try:
            dev = InputDevice(path)
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        try:
            if _supports_any_key(dev, keycodes):
                key_devices.append(f"{path} ({dev.name})")
        except OSError:
            pass
        finally:
            dev.close()
    for device in key_devices:
        print(f"key device: {device}")
    if denied:
        print(f"WARNING: {denied} input device(s) not readable — "
              "add user to 'input' group and re-login", file=sys.stderr)
    if not key_devices:
        print("WARNING: no readable devices provide the configured keys", file=sys.stderr)

    print(f"backend: {cfg.backend.type} ... loading")
    try:
        create_backend(cfg.backend, cfg.language)
        print("backend: OK")
        if cfg.streaming.model_dir:
            print("streaming: loading")
            create_streaming(cfg.streaming)
            print("streaming: OK")
        else:
            print("streaming: disabled (no live preview)")
        from .segment import SpeechDetector
        SpeechDetector(cfg.persistent.vad_model)
        print("dictation speech detector: OK")
    except BackendUnavailable as exc:
        print(f"backend unavailable: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"backend failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if cfg.dictation.ime:
        try:
            InputMethod().close()
            print("input method: OK (live text goes into the focused field)")
        except ImeUnavailable as exc:
            print(f"input method: unavailable ({exc}); no in-field preview")
    if cfg.polish.backend == "none":
        print("polish: off")
    else:
        endpoint = "a temporary localhost port" if cfg.polish.server.model_file else cfg.polish.url
        print(f"polish: {cfg.polish.format} via {endpoint} (model {cfg.polish.model}, "
              f"style {cfg.polish.style}, up to {cfg.polish.max_wait_seconds:g}s)")
        try:
            with diagnostic_polisher(cfg.polish) as polisher:
                cleaned = polisher.polish("so um this is uh a test of the the polish pass", 15.0)
            if cleaned is None:
                print("WARNING: polish: the test model did not answer "
                      "(see diagnostics above)", file=sys.stderr)
            else:
                print(f"polish: OK ({cleaned!r})")
        except PolishError as exc:
            print(f"WARNING: polish unavailable: {exc}", file=sys.stderr)
    if missing:
        return 1
    if not key_devices:
        return 2
    return 3 if agent_missing or agent_error else 0


if __name__ == "__main__":
    sys.exit(main())

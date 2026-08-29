from __future__ import annotations

import argparse
import logging
import shutil
import sys


def main() -> int:
    parser = argparse.ArgumentParser("voicekey")
    parser.add_argument("--config", help="config path (default ~/.config/voicekey/config.toml)")
    parser.add_argument("--check", action="store_true",
                        help="load config, models and the input method; list keyboards; exit")
    parser.add_argument("--download", action="store_true",
                        help="fetch model weights (install step, not first keypress)")
    parser.add_argument("--replay", metavar="WAV",
                        help="run a 16 kHz mono WAV through the pipeline as if spoken "
                             "(stop voicekey.service first: only one input method can be bound)")
    parser.add_argument("--agent", action="store_true",
                        help="with --replay: send the transcript to the agent instead of typing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    from .config import ConfigError, load
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    if args.download:
        from .backends import predownload
        try:
            predownload(cfg.backend, cfg.streaming)
        except Exception as exc:
            print(f"model download failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.check:
        return check(cfg)

    from .daemon import Daemon, fix_environment
    daemon = Daemon(cfg)
    try:
        if args.replay:
            fix_environment()
            daemon.load()
            daemon.start_workers()
            daemon.replay(args.replay, "agent" if args.agent else "dictate")
        else:
            daemon.run()
    except KeyboardInterrupt:
        pass
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    return 0


def check(cfg) -> int:
    """Exit 0 when fully ready, 2 when no keyboard is readable, 3 when only
    the agent target is unavailable, 1 for a config, dependency or model failure."""
    from evdev import InputDevice, ecodes

    from .backends import BackendUnavailable, create_backend, create_streaming
    from .config import key_chord_names
    from .daemon import fix_environment
    from .ime import ImeUnavailable, InputMethod
    from .listener import _supports_any_key, all_event_devices

    fix_environment()
    key_names = (cfg.dictate_key, cfg.agent_key, cfg.dictate_toggle_key, cfg.agent_toggle_key)
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
    print(f"agent: {cfg.agent.target} transport={cfg.agent.transport} "
          f"tmux session={cfg.agent.tmux_session} cwd={cfg.agent.working_directory}")
    if cfg.agent.transport == "ssh-over-tailscale":
        print(f"agent remote: {cfg.agent.remote_user}@{cfg.agent.remote_host}")

    required = {"niri", "notify-send", "pw-record", "wl-copy"}
    if cfg.dictation.inject == "wtype":
        required.add("wtype")
    agent_required = {"systemd-run"}
    if cfg.agent.transport == "ssh-over-tailscale":
        agent_required.update({"niri", "ssh", "tailscale"})
    else:
        agent_required.update({"hermes", "tmux"})
    if cfg.agent.open_terminal:
        agent_required.add(cfg.agent.terminal)
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
            print(f"input method: unavailable ({exc}); previews use notifications")
    if missing:
        return 1
    if not key_devices:
        return 2
    return 3 if agent_missing or agent_error else 0


if __name__ == "__main__":
    sys.exit(main())

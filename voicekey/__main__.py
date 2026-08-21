from __future__ import annotations

import argparse
import logging
import shutil
import sys


def main() -> int:
    parser = argparse.ArgumentParser("voicekey")
    parser.add_argument("--config", help="config path (default ~/.config/voicekey/config.toml)")
    parser.add_argument("--check", action="store_true",
                        help="load config + backend, list keyboards, exit")
    parser.add_argument("--download", action="store_true",
                        help="pre-fetch model weights (install step, not first keypress)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    from .config import ConfigError, load
    try:
        cfg = load(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    if args.download:
        from .backends import predownload
        try:
            predownload(cfg.backend)
        except Exception as e:
            print(f"model download failed: {e}", file=sys.stderr)
            return 1
        return 0

    if args.check:
        return check(cfg)

    from .daemon import Daemon
    try:
        Daemon(cfg).run()
    except KeyboardInterrupt:
        pass
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    return 0


def check(cfg) -> int:
    from evdev import InputDevice, ecodes

    from .backends import BackendUnavailable, create_backend
    from .listener import _supports_any_key, all_event_devices

    key_names = (
        cfg.dictate_key,
        cfg.agent_key,
        cfg.dictate_toggle_key,
        cfg.agent_toggle_key,
    )
    keycodes = {
        ecodes.ecodes[name] for name in key_names
        if isinstance(ecodes.ecodes.get(name), int)
    }

    print(
        f"keys: dictate={cfg.dictate_key} agent={cfg.agent_key} "
        f"inject={cfg.dictation.inject}"
    )
    if cfg.dictate_toggle_key or cfg.agent_toggle_key:
        print(
            f"toggle keys: dictate={cfg.dictate_toggle_key or '(none)'} "
            f"agent={cfg.agent_toggle_key or '(none)'}"
        )
    print(
        f"agent: {cfg.agent.target} transport={cfg.agent.transport} "
        f"tmux session={cfg.agent.tmux_session} "
        f"cwd={cfg.agent.working_directory}"
    )
    if cfg.agent.transport == "ssh-over-tailscale":
        print(
            f"agent remote: {cfg.agent.remote_user}@{cfg.agent.remote_host}"
        )

    required = {
        "niri",
        "notify-send",
        "pw-record",
        "wl-copy",
    }
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
    agent_missing = sorted(
        command for command in agent_required if shutil.which(command) is None
    )
    for command in sorted(required - set(missing)):
        print(f"command: {command}: OK")
    for command in sorted(agent_required - set(agent_missing)):
        print(f"agent command: {command}: OK")
    if missing:
        print(f"missing command(s): {', '.join(missing)}", file=sys.stderr)
    if agent_missing:
        print(
            "WARNING: agent target unavailable; missing command(s): "
            f"{', '.join(agent_missing)}",
            file=sys.stderr,
        )
    agent_error = None
    if not agent_missing:
        from .agent import check_target

        agent_error = check_target(cfg.agent)
        if agent_error:
            print(
                "WARNING: agent target unavailable: " + agent_error,
                file=sys.stderr,
            )
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
    except BackendUnavailable as e:
        print(f"backend unavailable: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"backend failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("backend: OK")
    if missing:
        return 1
    if not key_devices:
        return 2
    return 3 if agent_missing or agent_error else 0


if __name__ == "__main__":
    sys.exit(main())

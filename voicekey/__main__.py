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
    from evdev import InputDevice

    from .backends import BackendUnavailable, create_backend
    from .listener import _is_keyboard, all_event_devices

    print(
        f"keys: dictate={cfg.dictate_key} agent={cfg.agent_key} "
        f"inject={cfg.dictation.inject}"
    )
    print(
        f"agent: {cfg.agent.target} via tmux session={cfg.agent.tmux_session} "
        f"cwd={cfg.agent.working_directory}"
    )

    required = {
        "niri",
        "notify-send",
        "pw-record",
        "wl-copy",
    }
    if cfg.dictation.inject == "wtype":
        required.add("wtype")
    agent_required = {"hermes", "systemd-run", "tmux"}
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

    keyboards, denied = [], 0
    for path in sorted(all_event_devices()):
        try:
            dev = InputDevice(path)
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        try:
            if _is_keyboard(dev):
                keyboards.append(f"{path} ({dev.name})")
        except OSError:
            pass
        finally:
            dev.close()
    for k in keyboards:
        print(f"keyboard: {k}")
    if denied:
        print(f"WARNING: {denied} input device(s) not readable — "
              "add user to 'input' group and re-login", file=sys.stderr)
    if not keyboards:
        print("WARNING: no readable keyboards found", file=sys.stderr)

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
    if not keyboards:
        return 2
    return 3 if agent_missing else 0


if __name__ == "__main__":
    sys.exit(main())

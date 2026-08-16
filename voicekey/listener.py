"""evdev listener across all devices that provide the configured keys.

No EVIOCGRAB — the compositor still sees the held keys, so the bound keys must
be inert in niri and apps (see desktop/niri/binds.kdl).

Handles: full keyboards, separate laptop hotkey devices, hotplug (periodic
rescan — docks, BT), device disconnect mid-hold, and the no-permission case
(user not in `input` group) by reporting once and retrying, so the daemon
degrades gracefully instead of crash-looping."""

from __future__ import annotations

import glob
import logging
import select
import time

from evdev import InputDevice, ecodes

log = logging.getLogger("voicekey.listener")

RESCAN_INTERVAL = 2.0
NO_ACCESS_RETRY = 10.0


def _supports_any_key(dev: InputDevice, keycodes: set[int]) -> bool:
    keys = dev.capabilities().get(ecodes.EV_KEY, [])
    return not keycodes.isdisjoint(keys)


def all_event_devices() -> set[str]:
    """NOT evdev.list_devices(): that silently drops devices the user lacks
    read+write access to, which would make the no-permission case (user not in
    'input' group) indistinguishable from 'no keyboards'."""
    return set(glob.glob("/dev/input/event*"))


class KeyboardListener:
    """Callbacks:
      on_key(device_path, keycode, value)  — value 1=press, 0=release (repeats
                                             are filtered out here)
      on_device_lost(device_path)          — device vanished (may hold a key)
      on_tick()                            — every loop iteration (~1s max)
      on_no_access(message)                — no readable key devices (once per outage)
    """

    def __init__(self, keycodes: set[int], on_key, on_device_lost, on_tick,
                 on_no_access) -> None:
        self.keycodes = keycodes
        self.on_key = on_key
        self.on_device_lost = on_device_lost
        self.on_tick = on_tick
        self.on_no_access = on_no_access
        self.devices: dict[str, InputDevice] = {}
        self._last_rescan = 0.0
        self._no_access_reported = False

    def _rescan(self) -> None:
        self._last_rescan = time.monotonic()
        seen_paths = all_event_devices()
        for path in list(self.devices):
            if path not in seen_paths:
                self._drop(path)
        denied = 0
        for path in seen_paths - self.devices.keys():
            try:
                dev = InputDevice(path)
            except PermissionError:
                denied += 1
                continue
            except OSError:
                continue
            try:
                is_relevant = _supports_any_key(dev, self.keycodes)
            except OSError:
                dev.close()
                continue
            if is_relevant:
                self.devices[path] = dev
                log.info("watching %s (%s)", path, dev.name)
            else:
                dev.close()
        if not self.devices:
            if denied and not self._no_access_reported:
                self._no_access_reported = True
                self.on_no_access(
                    f"no readable devices for configured keys "
                    f"({denied} device(s) denied) — is "
                    "the user in the 'input' group? "
                    "Fix: sudo usermod -aG input $USER, then re-login."
                )
        else:
            self._no_access_reported = False

    def _drop(self, path: str) -> None:
        dev = self.devices.pop(path, None)
        if dev is None:
            return
        log.info("lost %s (%s)", path, dev.name)
        try:
            dev.close()
        except OSError:
            pass
        self.on_device_lost(path)

    def run(self) -> None:
        self._rescan()
        while True:
            if not self.devices:
                time.sleep(NO_ACCESS_RETRY)
                self._rescan()
                self.on_tick()
                continue
            try:
                readable, _, _ = select.select(
                    list(self.devices.values()), [], [], 1.0
                )
            except (OSError, ValueError):
                log.warning("keyboard select failed; reopening input devices")
                for path in list(self.devices):
                    self._drop(path)
                self._rescan()
                self.on_tick()
                continue
            if time.monotonic() - self._last_rescan > RESCAN_INTERVAL:
                self._rescan()
            for dev in readable:
                if dev.path not in self.devices:
                    continue
                try:
                    events = list(dev.read())
                except OSError:
                    self._drop(dev.path)
                    continue
                for ev in events:
                    if (ev.type == ecodes.EV_KEY and ev.code in self.keycodes
                            and ev.value in (0, 1)):  # ignore repeats (2)
                        self.on_key(dev.path, ev.code, ev.value)
            self.on_tick()

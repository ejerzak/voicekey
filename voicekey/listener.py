"""evdev listener across the devices that provide the configured keys — and
every other keyboard, only for the fact that a key was pressed (spacing).

No EVIOCGRAB — the compositor still sees the held keys, so the bound keys must
be inert in the compositor and apps (see README "Install").

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


TYPING_KEYS = {ecodes.KEY_A, ecodes.KEY_SPACE, ecodes.KEY_ENTER}
# A modifier on its own types nothing, so it is not activity. A dedicated
# dictation key may come with phantom modifiers (the Copilot key reports
# Left Meta + Left Shift + F23), which would otherwise hand spacing back to
# the user before every dictation.
MODIFIERS = {
    ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT, ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL,
    ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT, ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
    ecodes.KEY_CAPSLOCK, ecodes.KEY_NUMLOCK, ecodes.KEY_SCROLLLOCK, ecodes.KEY_FN,
}


def _supports_any_key(dev: InputDevice, keycodes: set[int]) -> bool:
    keys = dev.capabilities().get(ecodes.EV_KEY, [])
    return not keycodes.isdisjoint(keys)


def _is_keyboard(dev: InputDevice) -> bool:
    return _supports_any_key(dev, TYPING_KEYS)


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
      on_activity()                        — any other key or button was pressed,
                                             modifiers aside (the fact only; never which one)
    """

    def __init__(self, keycodes: set[int], on_key, on_device_lost, on_tick,
                 on_no_access, on_activity=None) -> None:
        self.keycodes = keycodes
        self.on_key = on_key
        self.on_device_lost = on_device_lost
        self.on_tick = on_tick
        self.on_no_access = on_no_access
        self.on_activity = on_activity
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
                is_relevant = _supports_any_key(dev, self.keycodes) or _is_keyboard(dev)
            except OSError:
                dev.close()
                continue
            if is_relevant:
                self.devices[path] = dev
                log.info("watching %s (%s)", path, dev.name)
            else:
                dev.close()
        voice_devices = [
            dev for dev in self.devices.values()
            if _supports_any_key(dev, self.keycodes)
        ]
        if not voice_devices:
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

    def _read(self, dev: InputDevice):
        """The pending events of DEV, or None once it is gone. The fd is
        non-blocking, so a device reported readable with nothing to read is
        a spurious wakeup, not a disconnect."""
        try:
            return list(dev.read())
        except BlockingIOError:
            return []
        except OSError:
            self._drop(dev.path)
            return None

    def dispatch(self, device_path: str, events) -> None:
        for ev in events:
            if ev.type != ecodes.EV_KEY:
                continue
            if ev.code in self.keycodes:
                if ev.value in (0, 1):  # ignore repeats (2)
                    self.on_key(device_path, ev.code, ev.value)
            elif ev.value == 1 and ev.code not in MODIFIERS and self.on_activity is not None:
                self.on_activity()

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
                events = self._read(dev)
                if events is None:
                    continue
                self.dispatch(dev.path, events)
            self.on_tick()

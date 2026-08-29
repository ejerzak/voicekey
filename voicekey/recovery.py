"""Keep transcripts that could not be delivered, and (opt-in) recordings."""

from __future__ import annotations

import os
import time
import wave

import numpy as np

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "voicekey",
)
LAST_RECOVERY = os.path.join(STATE_DIR, "last-recovery.txt")


def save(text: str) -> str:
    """Retain an undelivered transcript, mode 0600, and return its path."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(LAST_RECOVERY, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.write("\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return LAST_RECOVERY


def keep(directory: str, samples: np.ndarray, live_text: str, text: str) -> str:
    """Debug aid (``recordings_dir``): store the audio and both transcripts
    of a recording so models can be compared on real speech later."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    base = os.path.join(directory, stamp)
    with wave.open(base + ".wav", "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    with open(base + ".txt", "w", encoding="utf-8") as handle:
        handle.write(f"live:  {live_text}\nfinal: {text}\n")
    return base

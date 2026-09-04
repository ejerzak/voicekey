"""Keep transcripts that could not be delivered, and (opt-in) recordings."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
import wave

import numpy as np

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "voicekey",
)
LAST_RECOVERY = os.path.join(STATE_DIR, "last-recovery.txt")


_saving = threading.Lock()


def save(text: str) -> str:
    """Retain an undelivered transcript, mode 0600, and return its path.

    Written whole to a private temporary file and renamed into place, under
    a lock: the delivery and transcription workers can both fail at once,
    and the file must always hold one complete transcript."""
    with _saving:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=".last-recovery-", dir=STATE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(fd, 0o600)
                handle.write(text)
                handle.write("\n")
            os.replace(temporary, LAST_RECOVERY)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    return LAST_RECOVERY


def keep(directory: str, samples: np.ndarray, live_text: str, text: str,
         polished: str | None = None) -> str:
    """Debug aid (``recordings_dir``): store the audio and every transcript
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
        if polished is not None:
            handle.write(f"polished: {polished}\n")
    return base

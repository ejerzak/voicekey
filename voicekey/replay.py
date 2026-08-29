"""pw-record stand-in: stream a 16 kHz mono WAV to stdout at real-time pace.

    python -m voicekey.replay recording.wav

Exits 0 at the end of the file or on SIGINT, like a microphone that stops."""

from __future__ import annotations

import sys
import time
import wave

from .recorder import FRAME_SAMPLES, SAMPLE_RATE


def main(path: str) -> int:
    with wave.open(path, "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            print(f"{path}: need 16 kHz mono 16-bit PCM", file=sys.stderr)
            return 1
        pcm = wav.readframes(wav.getnframes())
    out = sys.stdout.buffer
    step = FRAME_SAMPLES * 2
    started = time.monotonic()
    try:
        for offset in range(0, len(pcm), step):
            due = started + offset / (2 * SAMPLE_RATE)
            time.sleep(max(0.0, due - time.monotonic()))
            out.write(pcm[offset:offset + step])
            out.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

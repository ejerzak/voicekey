"""One recording's optional live decoder. The recorder callback only enqueues."""
from __future__ import annotations

import logging
import queue
import threading

from .target import NotifyPreview

log = logging.getLogger("voicekey.capture")
OVERLOAD_FRAMES = 30


class Session:
    def __init__(self, action, behavior, chord, device, *, identity="", on_text=None):
        self.id = identity
        self.action, self.behavior, self.chord, self.device = action, behavior, chord, device
        self.target = NotifyPreview(action)
        self.text = ""
        self.agent_profile = None
        self.decoder = None
        self._stream = None
        self._frames = queue.Queue(maxsize=OVERLOAD_FRAMES)
        self._cancelled = threading.Event()
        self._accepting = False
        self._on_text = on_text

    @property
    def live(self):
        return self._accepting and not self._cancelled.is_set()

    @property
    def stuck(self):
        return self.decoder is not None and self.decoder.is_alive()

    def attach(self, factory):
        self._accepting = True
        self.decoder = threading.Thread(target=self._decode, args=(factory,), name="live-decode", daemon=True)
        self.decoder.start()

    def feed(self, frame):
        if not self.live:
            return
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            # No preview/ledger locks, subprocesses or logging on the audio path.
            self._cancelled.set()
            self._accepting = False

    def _decode(self, factory):
        try:
            stream = factory() if callable(factory) else factory
            self._stream = stream
            while not self._cancelled.is_set():
                frame = self._frames.get()
                if frame is None:
                    self._show(stream.finish())
                    return
                self._show(stream.feed(frame))
        except Exception:
            log.exception("live recognition failed; recording is preserved")
        finally:
            self._accepting = False
            self._stream = None

    def _show(self, text):
        if self._cancelled.is_set() or not text or text == self.text or len(text.encode()) > 100000:
            return
        self.text = text
        if self._on_text is not None and not self._on_text(self.id, text):
            return
        self.target.show(text)

    def finish(self, timeout=1.0):
        if self.decoder is None:
            return
        try:
            self._frames.put_nowait(None)
        except queue.Full:
            self.cancel()
        self.decoder.join(max(0, timeout))
        if self.decoder.is_alive():
            self.cancel()
            log.warning("live decoder did not finish; using the available preview")

    def cancel(self):
        self._cancelled.set()
        self._accepting = False
        try:
            self._frames.put_nowait(None)
        except queue.Full:
            pass

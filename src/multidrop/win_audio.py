"""Windows: play what this PC plays on an AirPlay speaker.

The default output is recorded with WASAPI loopback (soundcard) and streamed
with raop.RaopSender on the service's event loop. The PC's own speakers keep
playing; AirPlay runs about 2 s behind.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable

from multidrop import raop

log = logging.getLogger(__name__)


def _capture(stop: threading.Event, on_pcm: Callable[[bytes], None]) -> None:
    """Loopback of the default output as 44.1 kHz int16 stereo, until `stop`."""
    import warnings

    import numpy as np
    import soundcard as sc
    warnings.filterwarnings("ignore", module="soundcard")     # "data discontinuity" on silence
    mic = sc.get_microphone(id=str(sc.default_speaker().name), include_loopback=True)
    with mic.recorder(samplerate=raop.RATE, channels=2) as rec:
        while not stop.is_set():
            data = rec.record(numframes=None)
            if len(data):
                on_pcm((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())
            else:
                time.sleep(0.01)


class AirPlayAudio:
    """Receivers found on the network and the one stream at a time."""

    def __init__(self, loop: asyncio.AbstractEventLoop, capture=_capture,
                 on_change: Callable[[], None] = lambda: None):
        self.loop, self._capture_fn, self.on_change = loop, capture, on_change
        self.receivers: list[raop.Receiver] = []
        self.current: raop.Receiver | None = None
        self.volume = 0.75
        self._sender: raop.RaopSender | None = None
        self._stop_capture: threading.Event | None = None
        self._lock = threading.Lock()

    def _run(self, coro, timeout: float = 20):
        """Run `coro` on the service loop; on timeout it is cancelled there too,
        so a receiver that stops answering leaves no half-started stream."""
        return asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(coro, timeout), self.loop).result(timeout + 5)

    def scan(self, timeout: float = 3.0) -> None:
        self.receivers = self._run(raop.discover(timeout))
        self.on_change()

    def play(self, receiver: raop.Receiver) -> None:
        """Stream to `receiver` (stops a running stream first); raises
        raop.RaopError/OSError when the receiver refuses or is unreachable."""
        with self._lock:
            self._stop_locked()
            sender = raop.RaopSender(receiver.host, receiver.port, receiver.encryption)
            self._run(sender.start())
            try:
                self._run(sender.set_volume(self.volume))
            except raop.RaopError as e:         # some receivers have fixed volume
                log.info("volume: %s", e)
            self._sender, self.current = sender, receiver
            stop = self._stop_capture = threading.Event()

            def feed(pcm: bytes) -> None:
                self.loop.call_soon_threadsafe(sender.feed, pcm)

            def capture() -> None:
                try:
                    self._capture_fn(stop, feed)
                except Exception as e:
                    log.warning("audio capture ended: %s", e)
            threading.Thread(target=capture, name="airplay-capture", daemon=True).start()
        self.on_change()

    def set_volume(self, level: float) -> None:
        self.volume = level
        if self._sender is not None:
            self._run(self._sender.set_volume(level))
        self.on_change()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
        self.on_change()

    def _stop_locked(self) -> None:
        if self._stop_capture is not None:
            self._stop_capture.set()
            self._stop_capture = None
        if self._sender is not None:
            try:
                self._run(self._sender.stop(), 10)
            except Exception as e:
                log.debug("stopping AirPlay: %s", e)
            self._sender = None
        self.current = None

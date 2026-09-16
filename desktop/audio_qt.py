"""Qt-backed PCM output: the device that *is* the preview's clock.

Kept in ``desktop/`` rather than ``preview/`` so the preview engine stays
importable - and unit-testable - without Qt, and so the packaged headless CLI
keeps excluding PySide6.

Two behaviours of the Qt sink are load-bearing here, and both were measured
before this file was written:

``processedUSecs()`` is **played** audio, not buffered audio
    Feeding 0.6 s of PCM and reading the counter gives exactly 600000 us.  That is
    what makes it a clock: the timeline cannot lead the sound, and a device with
    nothing to play simply does not advance.
``reset()`` discards the buffer *and* re-zeroes the counter
    It also returns the sink to the stopped state, so a seek must flush and then
    start again.  Flushing is what stops the previous position from being heard
    after the timeline has already moved - the failure the acceptance calls
    "不混旧音" - and re-zeroing is why the session re-anchors the clock on every
    seek instead of accumulating.
"""
import threading

from PySide6 import QtMultimedia

from preview.clock import DEFAULT_BUFFER_FRAMES

__all__ = ['QtAudioOutput']


class QtAudioOutput:
    """A push-mode :class:`QAudioSink` exposing the preview's ``AudioOutput``.

    The sink object is never released while this wrapper lives, and every device
    call takes a lock.  That is deliberate: the preview's mixer thread reads
    ``frames_played`` continuously, and dropping the last Python reference to a
    ``QAudioSink`` lets Qt delete the C++ object underneath that thread, which
    surfaces as ``RuntimeError: Internal C++ object ... already deleted`` - a crash
    at teardown, i.e. at the one moment a caller is trying to report what happened.
    """

    def __init__(self, rate=48000, channels=1, buffer_frames=DEFAULT_BUFFER_FRAMES,
                 device=None):
        self.rate = int(rate)
        self.channels = int(channels)
        self.buffer_frames = int(buffer_frames)
        self.format = QtMultimedia.QAudioFormat()
        self.format.setSampleRate(self.rate)
        self.format.setChannelCount(self.channels)
        self.format.setSampleFormat(QtMultimedia.QAudioFormat.Int16)
        self._device = device
        self._sink = QtMultimedia.QAudioSink(self.format, device) if device is not None \
            else QtMultimedia.QAudioSink(self.format)
        self._sink.setBufferSize(self.buffer_frames * 2 * self.channels)
        self._stream = None
        self._lock = threading.Lock()
        self._closed = False
        self.starts = 0
        self.flushes = 0
        self.underflows = 0
        self._last_played = 0

    # -- AudioOutput -----------------------------------------------------
    def start(self):
        with self._lock:
            if self._closed:
                return self
            self._stream = self._sink.start()
            self.starts += 1
        return self

    def write(self, pcm):
        with self._lock:
            if self._closed or self._stream is None:
                return 0
            free = self._sink.bytesFree()
            if free <= 0:
                self.underflows += 1
                return 0
            if len(pcm) > free:
                # Never hand Qt a block it would truncate: half a PCM block is a click.
                pcm = pcm[:free - free % 2]
            if not pcm:
                self.underflows += 1
                return 0
            return self._stream.write(pcm)

    def free_frames(self):
        with self._lock:
            if self._closed or self._sink is None:
                return 0
            return max(0, self._sink.bytesFree() // (2 * self.channels))

    def frames_played(self):
        """Frames the device has played; after :meth:`close`, the last reading.

        A session's diagnostics must stay readable after teardown - that is the
        moment a caller reports what happened - so closing the device does not
        erase the clock, it freezes it at its final value.
        """
        with self._lock:
            if self._closed or self._sink is None:
                return self._last_played
            self._last_played = int(self._sink.processedUSecs() * self.rate // 1_000_000)
            return self._last_played

    def flush(self):
        with self._lock:
            self.flushes += 1
            if self._closed or self._sink is None:
                return
            # reset() drops what has not been heard, re-zeroes processedUSecs()
            # and stops the device; the caller must start it again.
            self._sink.reset()
            self._stream = None

    def stop(self):
        with self._lock:
            if self._closed or self._sink is None:
                return
            self._sink.stop()
            self._stream = None

    def close(self):
        with self._lock:
            if self._sink is not None:
                if not self._closed:
                    self._last_played = int(
                        self._sink.processedUSecs() * self.rate // 1_000_000)
                try:
                    self._sink.stop()
                except RuntimeError:
                    pass
            self._closed = True
            self._stream = None
            # self._sink is intentionally kept referenced: see the class docstring.

    # -- diagnostics -----------------------------------------------------
    def report(self):
        with self._lock:
            return {'rate': self.rate, 'buffer_frames': self.buffer_frames,
                    'starts': self.starts, 'flushes': self.flushes,
                    'underflows': self.underflows, 'closed': self._closed,
                    'last_played_frames': self._last_played,
                    'state': None if (self._sink is None or self._closed)
                             else str(self._sink.state())}

    @staticmethod
    def available():
        """Whether this machine has an output device at all.

        A machine with no sound card must still be able to preview - the caller
        falls back to :class:`preview.clock.NullAudioOutput` and the picture still
        plays - so this is a question, not a failure.
        """
        return bool(QtMultimedia.QMediaDevices.audioOutputs())

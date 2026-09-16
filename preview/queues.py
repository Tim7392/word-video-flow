"""Bounded hand-off between a producer thread and the presentation thread.

Both edges of a preview are bounded on purpose, and they need *different*
overflow rules:

video frames - **drop the oldest**
    The picture must show what is happening now.  If decoding fell behind, the
    useful frames are the newest ones; keeping the backlog would make the
    preview play the past in slow motion and then have to skip anyway.  Dropping
    is recorded, never hidden, so the UI can say the preview is degraded instead
    of pretending it kept up.
audio - **never drop, block the producer instead**
    A dropped PCM block is an audible click, and there is no way to hide it.  The
    device buffer is the natural back-pressure, so the mixer waits for room
    rather than overwriting sound.

Both rules need the same two guarantees, which is why this is one class:

* **Bounded.**  A `maxlen` is required, not defaulted to unlimited: an unbounded
  frame queue is how a preview turns a slow decoder into an out-of-memory crash
  on an 8 GB machine.
* **Never an unbounded wait.**  Every blocking call takes a timeout and returns
  ``None``/``False`` instead of hanging, and :meth:`close` wakes every waiter.
  A closed preview therefore tears down in finite time even if a decoder is
  wedged - the failure mode the role rules single out ("不能无限 join 线程卡 UI").
"""
import threading
from collections import deque

__all__ = ['BoundedQueue', 'DROP_OLDEST', 'BLOCK', 'QueueClosed']

#: Overflow rules.
DROP_OLDEST = 'drop-oldest'
BLOCK = 'block'


class QueueClosed(Exception):
    """Raised by :meth:`BoundedQueue.put_nowait` after :meth:`BoundedQueue.close`."""


class BoundedQueue:
    """A fixed-capacity queue with an explicit overflow rule and a close switch."""

    def __init__(self, maxlen, overflow=DROP_OLDEST, name=''):
        if maxlen <= 0:
            raise ValueError('BoundedQueue needs a positive maxlen')
        if overflow not in (DROP_OLDEST, BLOCK):
            raise ValueError('unknown overflow rule %r' % (overflow,))
        self.maxlen = int(maxlen)
        self.overflow = overflow
        self.name = name
        self._items = deque()
        self._condition = threading.Condition()
        self._closed = False
        self.dropped = 0
        self.high_water = 0
        self._put_waits = 0

    # -- introspection ---------------------------------------------------
    def __len__(self):
        with self._condition:
            return len(self._items)

    @property
    def closed(self):
        with self._condition:
            return self._closed

    @property
    def full(self):
        with self._condition:
            return len(self._items) >= self.maxlen

    def stats(self):
        """Snapshot for the UI/diagnostics; never holds the lock while read."""
        with self._condition:
            return {'name': self.name, 'depth': len(self._items), 'maxlen': self.maxlen,
                    'dropped': self.dropped, 'high_water': self.high_water,
                    'overflow': self.overflow, 'closed': self._closed,
                    'put_waits': self._put_waits}

    # -- writing ---------------------------------------------------------
    def put_nowait(self, item):
        """Enqueue without waiting; returns False when a blocking queue is full."""
        with self._condition:
            if self._closed:
                raise QueueClosed(self.name)
            if len(self._items) >= self.maxlen:
                if self.overflow == BLOCK:
                    return False
                self._items.popleft()
                self.dropped += 1
            self._items.append(item)
            self.high_water = max(self.high_water, len(self._items))
            self._condition.notify()
            return True

    def put(self, item, timeout=None):
        """Enqueue, waiting at most ``timeout`` seconds for room.

        Returns True when the item was stored, False on timeout, and raises
        :class:`QueueClosed` when the queue was closed - so a producer loop
        always has a way to end.
        """
        with self._condition:
            if self._closed:
                raise QueueClosed(self.name)
            if self.overflow == BLOCK and len(self._items) >= self.maxlen:
                self._put_waits += 1
                if not self._condition.wait_for(
                        lambda: self._closed or len(self._items) < self.maxlen,
                        timeout=timeout):
                    return False
                if self._closed:
                    raise QueueClosed(self.name)
            return self.put_nowait(item)

    # -- reading ---------------------------------------------------------
    def get(self, timeout=None):
        """Take the oldest item; ``None`` when closed-and-empty or on timeout.

        Every removal notifies, because removing is what makes room: without that
        a blocking producer would sit out its whole timeout while space was
        already free, which for the PCM path means dropouts that look like a slow
        disk.
        """
        with self._condition:
            if not self._items:
                if self._closed:
                    return None
                if not self._condition.wait_for(
                        lambda: self._closed or self._items, timeout=timeout):
                    return None
                if not self._items:
                    return None
            item = self._items.popleft()
            self._condition.notify()
            return item

    def peek(self):
        with self._condition:
            return self._items[0] if self._items else None

    def take_newest(self):
        """Drop the backlog and return the newest item (``None`` when empty).

        The presentation path uses this to catch up after a long UI stall: the
        frames it skipped are counted as dropped, not silently forgotten.
        """
        with self._condition:
            if not self._items:
                return None
            newest = self._items[-1]
            self.dropped += len(self._items) - 1
            self._items.clear()
            self._condition.notify_all()
            return newest

    # -- lifecycle -------------------------------------------------------
    def clear(self):
        """Discard everything held; used when a seek makes it all stale."""
        with self._condition:
            count = len(self._items)
            self._items.clear()
            self.dropped += count
            self._condition.notify_all()
            return count

    def close(self):
        """Refuse new items and wake every waiter."""
        with self._condition:
            self._closed = True
            count = len(self._items)
            self._condition.notify_all()
            return count

    def reopen(self):
        with self._condition:
            self._closed = False

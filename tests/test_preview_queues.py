"""Bounded hand-off without unbounded waits: the rule that keeps teardown finite."""
import threading
import time

import pytest

from preview.queues import BLOCK, DROP_OLDEST, BoundedQueue, QueueClosed


def test_a_queue_requires_a_bound():
    with pytest.raises(ValueError):
        BoundedQueue(0)
    with pytest.raises(ValueError):
        BoundedQueue(-5)


def test_drop_oldest_keeps_the_newest_picture_and_counts_the_loss():
    """A preview that fell behind must show now, not the past in slow motion."""
    queue = BoundedQueue(maxlen=3, overflow=DROP_OLDEST)
    for index in range(10):
        queue.put_nowait(index)
    assert len(queue) == 3
    assert [queue.get(timeout=0) for _ in range(3)] == [7, 8, 9]
    assert queue.dropped == 7
    assert queue.stats()['high_water'] == 3


def test_block_policy_refuses_rather_than_overwriting_sound():
    """A dropped PCM block is an audible click, so this policy never drops."""
    queue = BoundedQueue(maxlen=2, overflow=BLOCK)
    assert queue.put_nowait(1) is True
    assert queue.put_nowait(2) is True
    assert queue.put_nowait(3) is False
    assert len(queue) == 2
    assert queue.dropped == 0
    assert [queue.get(timeout=0) for _ in range(2)] == [1, 2]


def test_a_blocked_put_gives_up_instead_of_waiting_forever():
    queue = BoundedQueue(maxlen=1, overflow=BLOCK)
    queue.put_nowait('only')
    started = time.monotonic()
    assert queue.put('second', timeout=0.05) is False
    elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 2.0
    assert queue.stats()['put_waits'] == 1


def test_a_blocked_put_is_released_by_a_get():
    queue = BoundedQueue(maxlen=1, overflow=BLOCK)
    queue.put_nowait('first')
    result = []

    def waiter():
        result.append(queue.put('second', timeout=5.0))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.05)
    assert queue.get(timeout=0) == 'first'
    thread.join(timeout=2.0)
    assert result == [True]


def test_close_wakes_a_blocked_reader_without_waiting_for_the_timeout():
    """Teardown must not depend on a worker noticing politely."""
    queue = BoundedQueue(maxlen=1)
    outcome = []

    def reader():
        started = time.monotonic()
        outcome.append((queue.get(timeout=30.0), time.monotonic() - started))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.05)
    queue.close()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    item, elapsed = outcome[0]
    assert item is None
    assert elapsed < 2.0


def test_close_wakes_a_blocked_writer_and_refuses_new_items():
    queue = BoundedQueue(maxlen=1, overflow=BLOCK)
    queue.put_nowait('held')
    errors = []

    def writer():
        try:
            queue.put('later', timeout=30.0)
        except QueueClosed:
            errors.append('closed')

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.05)
    queue.close()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert errors == ['closed']
    with pytest.raises(QueueClosed):
        queue.put_nowait('after close')


def test_a_closed_queue_still_drains_what_it_already_holds():
    """Frames already decoded are still valid; only new ones are refused."""
    queue = BoundedQueue(maxlen=4)
    queue.put_nowait('a')
    queue.put_nowait('b')
    queue.close()
    assert queue.get(timeout=0) == 'a'
    assert queue.get(timeout=0) == 'b'
    assert queue.get(timeout=0) is None


def test_take_newest_drops_the_backlog_and_counts_it():
    queue = BoundedQueue(maxlen=8)
    for index in range(5):
        queue.put_nowait(index)
    assert queue.take_newest() == 4
    assert len(queue) == 0
    assert queue.dropped == 4
    assert queue.take_newest() is None


def test_clear_counts_what_a_seek_threw_away():
    queue = BoundedQueue(maxlen=8)
    for index in range(4):
        queue.put_nowait(index)
    assert queue.clear() == 4
    assert len(queue) == 0
    assert queue.dropped == 4

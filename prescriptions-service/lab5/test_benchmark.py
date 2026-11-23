import threading
import time
import unittest
from array import array

from lab5.benchmark import (
    FifoRWLock,
    GlobalMutex,
    RingBuffer,
    _threaded_run,
    nearest_rank_percentiles,
    parse_args,
)


class RingBufferTests(unittest.TestCase):
    def test_overwrite_and_last_order(self) -> None:
        ring = RingBuffer(4, GlobalMutex())
        for value in range(7):
            ring.append((1, value))
        _, latest = ring.last(16)
        self.assertEqual([event[0] for event in latest], [6, 5, 4, 3])
        self.assertTrue(ring.validate_invariants()["valid"])

    def test_read_does_not_change_state(self) -> None:
        ring = RingBuffer(8, FifoRWLock())
        for value in range(5):
            ring.append((1, value))
        before = ring.state_fingerprint()
        ring.last(16)
        self.assertEqual(before, ring.state_fingerprint())

    def test_fifo_rwlock_allows_reader_batch_and_blocks_writer(self) -> None:
        lock = FifoRWLock()
        first_reader_ready = threading.Event()
        second_reader_ready = threading.Event()
        release_readers = threading.Event()
        writer_entered = threading.Event()

        def reader(ready: threading.Event) -> None:
            lock.acquire_read()
            ready.set()
            release_readers.wait(timeout=2)
            lock.release_read()

        def writer() -> None:
            lock.acquire_write()
            writer_entered.set()
            lock.release_write()

        first = threading.Thread(target=reader, args=(first_reader_ready,))
        second = threading.Thread(target=reader, args=(second_reader_ready,))
        first.start()
        self.assertTrue(first_reader_ready.wait(timeout=1))
        second.start()
        self.assertTrue(second_reader_ready.wait(timeout=1))
        writing = threading.Thread(target=writer)
        writing.start()
        time.sleep(0.02)
        self.assertFalse(writer_entered.is_set())
        release_readers.set()
        first.join(timeout=1)
        second.join(timeout=1)
        writing.join(timeout=1)
        self.assertTrue(writer_entered.is_set())

    def test_fifo_rwlock_does_not_let_late_reader_barge_past_writer(self) -> None:
        lock = FifoRWLock()
        lock.acquire_read()
        order: list[str] = []

        def writer() -> None:
            lock.acquire_write()
            order.append("writer")
            lock.release_write()

        def late_reader() -> None:
            lock.acquire_read()
            order.append("reader")
            lock.release_read()

        writing = threading.Thread(target=writer)
        writing.start()
        deadline = time.monotonic() + 1
        while lock._next_ticket < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(lock._next_ticket, 2)
        reading = threading.Thread(target=late_reader)
        reading.start()
        lock.release_read()
        writing.join(timeout=1)
        reading.join(timeout=1)
        self.assertEqual(order, ["writer", "reader"])

    def test_nearest_rank_percentiles(self) -> None:
        p50, p99 = nearest_rank_percentiles(array("Q", range(1, 101)))
        self.assertEqual(p50, 0.05)
        self.assertEqual(p99, 0.099)

    def test_cli_selects_lock_and_phase(self) -> None:
        args = parse_args(["--mode", "global", "--phase", "threaded"])
        self.assertEqual(args.mode, "global")
        self.assertEqual(args.phase, "threaded")


class BenchmarkSmokeTests(unittest.TestCase):
    def test_short_threaded_runs_preserve_invariants(self) -> None:
        for mode in ("global", "rwlock"):
            result = _threaded_run(
                mode=mode,
                capacity=64,
                thread_count=6,
                warmup_s=0.05,
                duration_s=0.10,
                repeat=1,
            )
            self.assertTrue(result["invariants"]["valid"])
            self.assertTrue(result["invariants"]["read_is_pure"])
            self.assertGreater(result["roles"]["write"]["operations"], 0)
            self.assertGreater(result["roles"]["read"]["operations"], 0)


if __name__ == "__main__":
    unittest.main()

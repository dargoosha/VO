from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


Role = Literal["write", "read"]


class ReadWriteLock(Protocol):
    policy: str

    def acquire_read(self) -> None: ...

    def release_read(self) -> None: ...

    def acquire_write(self) -> None: ...

    def release_write(self) -> None: ...


class GlobalMutex:
    """One mutex shared by every read and write operation."""

    policy = "single global mutex"

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire_read(self) -> None:
        self._lock.acquire()

    def release_read(self) -> None:
        self._lock.release()

    def acquire_write(self) -> None:
        self._lock.acquire()

    def release_write(self) -> None:
        self._lock.release()


class FifoRWLock:
    """A starvation-free FIFO readers-writer lock built on Condition.

    Every acquisition gets a monotonically increasing ticket. Requests start
    in ticket order. Consecutive readers may be active together; a writer waits
    until all earlier readers have left. This bounds waiting for both roles.
    """

    policy = "FIFO tickets; consecutive readers share a phase; no barging"

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._next_ticket = 0
        self._serving_ticket = 0
        self._active_readers = 0
        self._active_writer = False

    def _ticket(self) -> int:
        ticket = self._next_ticket
        self._next_ticket += 1
        return ticket

    def acquire_read(self) -> None:
        with self._condition:
            ticket = self._ticket()
            while ticket != self._serving_ticket or self._active_writer:
                self._condition.wait()
            self._active_readers += 1
            self._serving_ticket += 1
            self._condition.notify_all()

    def release_read(self) -> None:
        with self._condition:
            if self._active_readers <= 0:
                raise RuntimeError("release_read without acquire_read")
            self._active_readers -= 1
            if self._active_readers == 0:
                self._condition.notify_all()

    def acquire_write(self) -> None:
        with self._condition:
            ticket = self._ticket()
            while (
                ticket != self._serving_ticket
                or self._active_writer
                or self._active_readers > 0
            ):
                self._condition.wait()
            self._active_writer = True
            self._serving_ticket += 1
            self._condition.notify_all()

    def release_write(self) -> None:
        with self._condition:
            if not self._active_writer:
                raise RuntimeError("release_write without acquire_write")
            self._active_writer = False
            self._condition.notify_all()


@dataclass(frozen=True)
class OperationTiming:
    duration_ns: int
    wait_ns: int
    hold_ns: int


class RingBuffer:
    """Fixed-capacity ring buffer whose public operations own synchronization."""

    def __init__(self, capacity: int, lock: ReadWriteLock) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.lock = lock
        self._slots: list[tuple[int, tuple[int, int]] | None] = [None] * capacity
        self._write_index = 0
        self._size = 0
        self._next_sequence = 0
        self._total_appends = 0

    def append(self, payload: tuple[int, int]) -> tuple[OperationTiming, int]:
        call_ns = time.perf_counter_ns()
        self.lock.acquire_write()
        acquired_ns = time.perf_counter_ns()
        try:
            sequence = self._next_sequence
            self._next_sequence += 1
            self._slots[self._write_index] = (sequence, payload)
            self._write_index = (self._write_index + 1) % self.capacity
            if self._size < self.capacity:
                self._size += 1
            self._total_appends += 1
            release_ns = time.perf_counter_ns()
        finally:
            self.lock.release_write()
        return_ns = time.perf_counter_ns()
        return (
            OperationTiming(
                duration_ns=return_ns - call_ns,
                wait_ns=acquired_ns - call_ns,
                hold_ns=release_ns - acquired_ns,
            ),
            sequence,
        )

    def last(
        self, limit: int = 16
    ) -> tuple[OperationTiming, tuple[tuple[int, tuple[int, int]], ...]]:
        if limit < 0:
            raise ValueError("limit must not be negative")
        call_ns = time.perf_counter_ns()
        self.lock.acquire_read()
        acquired_ns = time.perf_counter_ns()
        try:
            result: list[tuple[int, tuple[int, int]]] = []
            take = min(limit, self._size)
            for offset in range(take):
                index = (self._write_index - 1 - offset) % self.capacity
                event = self._slots[index]
                if event is None:
                    raise AssertionError("occupied ring slot is empty")
                result.append(event)
            release_ns = time.perf_counter_ns()
        finally:
            self.lock.release_read()
        return_ns = time.perf_counter_ns()
        return (
            OperationTiming(
                duration_ns=return_ns - call_ns,
                wait_ns=acquired_ns - call_ns,
                hold_ns=release_ns - acquired_ns,
            ),
            tuple(result),
        )

    def state_fingerprint(self) -> tuple[Any, ...]:
        return (
            self._write_index,
            self._size,
            self._next_sequence,
            self._total_appends,
            tuple(self._slots),
        )

    def validate_invariants(self) -> dict[str, Any]:
        if not 0 <= self._size <= self.capacity:
            raise AssertionError("ring size is outside capacity")
        if not 0 <= self._write_index < self.capacity:
            raise AssertionError("write index is outside ring")
        if self._next_sequence != self._total_appends:
            raise AssertionError("sequence and append counters diverged")

        start = (self._write_index - self._size) % self.capacity
        chronological: list[tuple[int, tuple[int, int]]] = []
        for offset in range(self._size):
            event = self._slots[(start + offset) % self.capacity]
            if event is None:
                raise AssertionError("occupied ring slot is empty")
            chronological.append(event)

        sequences = [event[0] for event in chronological]
        if any(left >= right for left, right in zip(sequences, sequences[1:])):
            raise AssertionError("stored event numbers are not strictly increasing")
        if sequences:
            expected_first = self._next_sequence - self._size
            if sequences[0] != expected_first or sequences[-1] != self._next_sequence - 1:
                raise AssertionError("ring does not contain the latest contiguous events")

        before_read = self.state_fingerprint()
        _, latest = self.last(16)
        after_read = self.state_fingerprint()
        latest_sequences = [event[0] for event in latest]
        descending = all(
            left > right for left, right in zip(latest_sequences, latest_sequences[1:])
        )
        if len(latest) > 16 or not descending:
            raise AssertionError("last(16) violates ordering or size")
        if before_read != after_read:
            raise AssertionError("read operation changed ring state")

        return {
            "capacity": self.capacity,
            "size": self._size,
            "total_appends_including_warmup": self._total_appends,
            "latest_count": len(latest),
            "latest_strictly_descending": descending,
            "read_is_pure": before_read == after_read,
            "valid": True,
        }


@dataclass
class ThreadMeasurement:
    thread_id: int
    role: Role
    count: int = 0
    wait_sum_ns: int = 0
    max_wait_ns: int = 0
    hold_sum_ns: int = 0
    durations_ns: array = field(default_factory=lambda: array("Q"))

    def add(self, timing: OperationTiming) -> None:
        self.count += 1
        self.wait_sum_ns += timing.wait_ns
        self.max_wait_ns = max(self.max_wait_ns, timing.wait_ns)
        self.hold_sum_ns += timing.hold_ns
        self.durations_ns.append(timing.duration_ns)


def _select_kth(values: array, k: int) -> int:
    """Select zero-based kth value in-place without expanding to Python ints."""

    if not 0 <= k < len(values):
        raise IndexError(k)
    left = 0
    right = len(values) - 1
    while True:
        if left == right:
            return int(values[left])
        pivot = int(values[(left + right) // 2])
        i = left
        j = right
        while i <= j:
            while int(values[i]) < pivot:
                i += 1
            while int(values[j]) > pivot:
                j -= 1
            if i <= j:
                values[i], values[j] = values[j], values[i]
                i += 1
                j -= 1
        if k <= j:
            right = j
        elif k >= i:
            left = i
        else:
            return int(values[k])


def nearest_rank_percentiles(values: array) -> tuple[float, float]:
    if not values:
        return math.nan, math.nan
    n = len(values)
    p50_index = math.ceil(0.50 * n) - 1
    p99_index = math.ceil(0.99 * n) - 1
    p50_ns = _select_kth(values, p50_index)
    p99_ns = _select_kth(values, p99_index)
    return p50_ns / 1_000.0, p99_ns / 1_000.0


def _role_summary(
    measurements: list[ThreadMeasurement], role: Role, duration_s: float
) -> dict[str, Any]:
    selected = [item for item in measurements if item.role == role]
    counts = [item.count for item in selected]
    combined = array("Q")
    for item in selected:
        combined.extend(item.durations_ns)
        item.durations_ns = array("Q")
    duration_samples = len(combined)
    duration_sum_ns = sum(combined)
    p50_us, p99_us = nearest_rank_percentiles(combined)
    operations = sum(counts)
    wait_sum_ns = sum(item.wait_sum_ns for item in selected)
    hold_sum_ns = sum(item.hold_sum_ns for item in selected)
    mean_count = statistics.fmean(counts) if counts else math.nan
    cv = (
        statistics.pstdev(counts) / mean_count
        if counts and mean_count > 0 and len(counts) > 1
        else 0.0
    )
    return {
        "threads": len(selected),
        "operations": operations,
        "duration_samples": duration_samples,
        "duration_sum_ns": duration_sum_ns,
        "wait_sum_ns": wait_sum_ns,
        "hold_sum_ns": hold_sum_ns,
        "throughput_ops_s": operations / duration_s,
        "per_thread_ops_s": operations / duration_s / len(selected),
        "p50_us": p50_us,
        "p99_us": p99_us,
        "tail_ratio": p99_us / p50_us if p50_us > 0 else math.nan,
        "max_wait_ms": max((item.max_wait_ns for item in selected), default=0)
        / 1_000_000.0,
        "wait_fraction": wait_sum_ns / 1_000_000_000.0 / (len(selected) * duration_s),
        "cv": cv,
        "mean_hold_us": hold_sum_ns / operations / 1_000.0 if operations else math.nan,
    }


def _new_lock(mode: str) -> ReadWriteLock:
    if mode == "global":
        return GlobalMutex()
    if mode == "rwlock":
        return FifoRWLock()
    raise ValueError(f"unknown mode: {mode}")


def _threaded_run(
    *,
    mode: str,
    capacity: int,
    thread_count: int,
    warmup_s: float,
    duration_s: float,
    repeat: int,
) -> dict[str, Any]:
    lock = _new_lock(mode)
    ring = RingBuffer(capacity, lock)
    ready = threading.Barrier(thread_count + 1)
    start_event = threading.Event()
    schedule: dict[str, int] = {}
    measurements = [
        ThreadMeasurement(
            thread_id=thread_id,
            role="write" if thread_id % 2 == 1 else "read",
        )
        for thread_id in range(1, thread_count + 1)
    ]
    errors: list[str] = []
    error_lock = threading.Lock()

    def worker(measurement: ThreadMeasurement) -> None:
        local_counter = 0
        try:
            ready.wait()
            start_event.wait()
            warmup_end_ns = schedule["warmup_end_ns"]
            measurement_end_ns = schedule["measurement_end_ns"]

            while time.perf_counter_ns() < warmup_end_ns:
                if measurement.role == "write":
                    payload = (measurement.thread_id, local_counter)
                    local_counter += 1
                    ring.append(payload)
                else:
                    limit = 16
                    ring.last(limit)

            while time.perf_counter_ns() < measurement_end_ns:
                if measurement.role == "write":
                    payload = (measurement.thread_id, local_counter)
                    local_counter += 1
                    timing, _ = ring.append(payload)
                else:
                    limit = 16
                    timing, _ = ring.last(limit)
                measurement.add(timing)
        except BaseException as exc:  # preserve worker failures for the main thread
            with error_lock:
                errors.append(
                    f"thread {measurement.thread_id} ({measurement.role}): "
                    f"{type(exc).__name__}: {exc}"
                )

    threads = [
        threading.Thread(
            target=worker,
            args=(measurement,),
            name=f"lab5-{measurement.role}-{measurement.thread_id}",
        )
        for measurement in measurements
    ]
    for thread in threads:
        thread.start()
    ready.wait()
    start_ns = time.perf_counter_ns()
    schedule["warmup_end_ns"] = start_ns + round(warmup_s * 1_000_000_000)
    schedule["measurement_end_ns"] = schedule["warmup_end_ns"] + round(
        duration_s * 1_000_000_000
    )
    start_event.set()
    for thread in threads:
        thread.join()
    finished_ns = time.perf_counter_ns()
    if errors:
        raise RuntimeError("; ".join(errors))

    invariants = ring.validate_invariants()
    write = _role_summary(measurements, "write", duration_s)
    read = _role_summary(measurements, "read", duration_s)
    total_operations = write["operations"] + read["operations"]
    return {
        "kind": "threaded",
        "mode": mode,
        "lock_policy": lock.policy,
        "repeat": repeat,
        "warmup_s": warmup_s,
        "duration_s": duration_s,
        "scheduled_wall_s": warmup_s + duration_s,
        "observed_wall_s": (finished_ns - start_ns) / 1_000_000_000.0,
        "thread_count": thread_count,
        "writer_threads": (thread_count + 1) // 2,
        "reader_threads": thread_count // 2,
        "roles": {"write": write, "read": read},
        "total": {
            "operations": total_operations,
            "throughput_ops_s": total_operations / duration_s,
        },
        "thread_counts": [
            {
                "thread_id": item.thread_id,
                "role": item.role,
                "operations": item.count,
            }
            for item in measurements
        ],
        "invariants": invariants,
    }


def _interleaved_pattern(writers: int, readers: int) -> tuple[Role, ...]:
    pattern: list[Role] = []
    used_writers = 0
    used_readers = 0
    while used_writers < writers or used_readers < readers:
        if used_writers < writers:
            pattern.append("write")
            used_writers += 1
        if used_readers < readers:
            pattern.append("read")
            used_readers += 1
    return tuple(pattern)


def _print_run_summary(run: dict[str, Any]) -> None:
    write = run["roles"]["write"]
    read = run["roles"]["read"]
    print(
        "result "
        f"kind={run['kind']} mode={run['mode']} repeat={run['repeat']} "
        f"X_w={write['throughput_ops_s']:.3f} ops/s "
        f"X_r={read['throughput_ops_s']:.3f} ops/s "
        f"X={run['total']['throughput_ops_s']:.3f} ops/s",
        flush=True,
    )
    print(
        "latency "
        f"write_p50={write['p50_us']:.3f} us "
        f"write_p99={write['p99_us']:.3f} us "
        f"read_p50={read['p50_us']:.3f} us "
        f"read_p99={read['p99_us']:.3f} us",
        flush=True,
    )
    print(
        "waiting "
        f"write_max={write['max_wait_ms']:.6f} ms "
        f"w_w={write['wait_fraction']:.9f} "
        f"w_r={read['wait_fraction']:.9f}",
        flush=True,
    )
    if run["kind"] == "threaded":
        counts = ", ".join(
            f"t{item['thread_id']}:{item['role']}={item['operations']}"
            for item in run["thread_counts"]
        )
        print(f"thread_counts {counts}", flush=True)
    invariants = run["invariants"]
    print(
        "invariants "
        f"valid={invariants['valid']} "
        f"read_is_pure={invariants['read_is_pure']} "
        f"latest_strictly_descending={invariants['latest_strictly_descending']}",
        flush=True,
    )


def _baseline_run(
    *,
    mode: str,
    capacity: int,
    writer_weight: int,
    reader_weight: int,
    warmup_s: float,
    duration_s: float,
    repeat: int,
) -> dict[str, Any]:
    lock = _new_lock(mode)
    ring = RingBuffer(capacity, lock)
    pattern = _interleaved_pattern(writer_weight, reader_weight)
    measurements = {
        "write": ThreadMeasurement(1, "write"),
        "read": ThreadMeasurement(1, "read"),
    }
    local_counter = 0
    pattern_index = 0
    start_ns = time.perf_counter_ns()
    warmup_end_ns = start_ns + round(warmup_s * 1_000_000_000)
    measurement_end_ns = warmup_end_ns + round(duration_s * 1_000_000_000)

    while time.perf_counter_ns() < warmup_end_ns:
        role = pattern[pattern_index % len(pattern)]
        pattern_index += 1
        if role == "write":
            payload = (1, local_counter)
            local_counter += 1
            ring.append(payload)
        else:
            limit = 16
            ring.last(limit)

    while time.perf_counter_ns() < measurement_end_ns:
        role = pattern[pattern_index % len(pattern)]
        pattern_index += 1
        if role == "write":
            payload = (1, local_counter)
            local_counter += 1
            timing, _ = ring.append(payload)
        else:
            limit = 16
            timing, _ = ring.last(limit)
        measurements[role].add(timing)

    finished_ns = time.perf_counter_ns()
    invariants = ring.validate_invariants()
    write = _role_summary([measurements["write"]], "write", duration_s)
    read = _role_summary([measurements["read"]], "read", duration_s)
    total_operations = write["operations"] + read["operations"]
    return {
        "kind": "baseline_x1",
        "mode": mode,
        "lock_policy": lock.policy,
        "repeat": repeat,
        "warmup_s": warmup_s,
        "duration_s": duration_s,
        "scheduled_wall_s": warmup_s + duration_s,
        "observed_wall_s": (finished_ns - start_ns) / 1_000_000_000.0,
        "pattern": list(pattern),
        "roles": {"write": write, "read": read},
        "total": {
            "operations": total_operations,
            "throughput_ops_s": total_operations / duration_s,
        },
        "invariants": invariants,
    }


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    if args.threads < 2:
        raise ValueError("the laboratory needs at least one writer and one reader")
    if args.capacity <= 0:
        raise ValueError("capacity must be positive")
    if args.warmup < 0:
        raise ValueError("warmup must not be negative")
    if args.duration <= 0:
        raise ValueError("duration must be positive")
    if args.repeats <= 0:
        raise ValueError("repeats must be positive")
    modes = ("global", "rwlock") if args.mode == "both" else (args.mode,)
    phases = ("baseline", "threaded") if args.phase == "both" else (args.phase,)
    writers = (args.threads + 1) // 2
    readers = args.threads // 2
    results: dict[str, Any] = {
        "schema_version": 2,
        "laboratory": 5,
        "variant": 6,
        "structure": "C6 ring buffer of latest service events",
        "parameters": {
            "capacity": args.capacity,
            "thread_count": args.threads,
            "writer_threads": writers,
            "reader_threads": readers,
            "warmup_s": args.warmup,
            "duration_s": args.duration,
            "repeats": args.repeats,
            "read_limit": 16,
            "mode_selection": args.mode,
            "phase_selection": args.phase,
        },
        "environment": {
            "captured_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "processor_identifier": os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
            "logical_cores": os.cpu_count(),
        },
        "lock_policies": {
            "global": GlobalMutex.policy,
            "rwlock": FifoRWLock.policy,
        },
        "baseline_runs": [],
        "threaded_runs": [],
    }

    if "baseline" in phases:
        for mode in modes:
            for repeat in range(1, args.repeats + 1):
                print(f"baseline mode={mode} repeat={repeat}/{args.repeats}", flush=True)
                run = _baseline_run(
                    mode=mode,
                    capacity=args.capacity,
                    writer_weight=writers,
                    reader_weight=readers,
                    warmup_s=args.warmup,
                    duration_s=args.duration,
                    repeat=repeat,
                )
                results["baseline_runs"].append(run)
                _print_run_summary(run)

    if "threaded" in phases:
        for mode in modes:
            for repeat in range(1, args.repeats + 1):
                print(f"threaded mode={mode} repeat={repeat}/{args.repeats}", flush=True)
                run = _threaded_run(
                    mode=mode,
                    capacity=args.capacity,
                    thread_count=args.threads,
                    warmup_s=args.warmup,
                    duration_s=args.duration,
                    repeat=repeat,
                )
                results["threaded_runs"].append(run)
                _print_run_summary(run)

    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Laboratory 5 benchmark for variant 6 (C6 ring buffer)."
    )
    parser.add_argument(
        "--output", type=Path, default=Path("results/lab5/raw_results.json")
    )
    parser.add_argument("--capacity", type=int, default=1024)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--warmup", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--mode",
        choices=("global", "rwlock", "both"),
        default="both",
        help="Synchronization mode selected for this launch.",
    )
    parser.add_argument(
        "--phase",
        choices=("baseline", "threaded", "both"),
        default="both",
        help="Run the X_1 baseline, the n-thread experiment, or both.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    result = run_experiment(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"saved {args.output}")


if __name__ == "__main__":
    main()

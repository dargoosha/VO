"""Open-loop load generator for laboratory work 3.

The generator owns a fixed set of HTTP/1.1 keep-alive connections. Each
connection is represented by one worker and is recreated after a configured
number of requests. Client latency starts at the planned arrival time, so it
includes the generator queue as required for an open-loop test.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiohttp


@dataclass(frozen=True)
class Segment:
    index: int
    name: str
    duration_seconds: int
    target_rps: float
    begin_second: int
    summary_window_seconds: int

    @property
    def end_second(self) -> int:
        return self.begin_second + self.duration_seconds

    @property
    def window_start_second(self) -> int:
        return self.end_second - self.summary_window_seconds


@dataclass(frozen=True)
class WorkItem:
    request_id: int
    planned_monotonic: float
    planned_unix: float
    segment_index: int
    record_id: int


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def rounded(value: float) -> float | str:
    return "" if math.isnan(value) else round(value, 3)


def load_ids(path: str) -> list[int]:
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(values, list) or not values:
        raise ValueError("IDs file must contain a non-empty JSON array")
    return [int(value) for value in values]


def parse_profile(path: str) -> list[tuple[str, int, float]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("Profile must be a non-empty JSON array")
    result: list[tuple[str, int, float]] = []
    for item in raw:
        name = str(item["name"])
        duration = int(item["duration_seconds"])
        rate = float(item["target_rps"])
        if duration < 1 or rate <= 0:
            raise ValueError("Profile duration and target_rps must be positive")
        result.append((name, duration, rate))
    return result


def build_segments(args: argparse.Namespace) -> list[Segment]:
    segments: list[Segment] = []
    begin = 0
    if args.command == "steps":
        if args.warmup_seconds:
            segments.append(
                Segment(0, "warmup", args.warmup_seconds, args.start_rps, 0,
                        args.warmup_seconds)
            )
            begin = args.warmup_seconds
        for step in range(args.max_steps):
            rate = args.start_rps + step * args.step_rps
            segments.append(
                Segment(step + 1, f"step_{step + 1}", args.step_seconds,
                        rate, begin, args.window_seconds)
            )
            begin += args.step_seconds
    else:
        for index, (name, duration, rate) in enumerate(
                parse_profile(args.profile_file), start=1):
            segments.append(Segment(index, name, duration, rate, begin, duration))
            begin += duration
    return segments


def new_second_bucket() -> dict[str, Any]:
    return {
        "scheduled": 0,
        "enqueued": 0,
        "started": 0,
        "completed": 0,
        "ok": 0,
        "non2xx": 0,
        "connection_errors": 0,
        "timeouts": 0,
        "queue_rejected": 0,
        "cancelled": 0,
        "client_ms": [],
        "request_ms": [],
        "queue_ms": [],
        "pods": Counter(),
        "queue_depth_end": 0,
        "inflight_end": 0,
    }


async def execute(args: argparse.Namespace) -> None:
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    ids = load_ids(args.ids)
    segments = build_segments(args)
    base_url = args.url.rstrip("/")
    request_base = base_url + "/prescriptions/"
    health_url = base_url + "/livez"
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    queue: asyncio.Queue[WorkItem | None] = asyncio.Queue()
    stats: defaultdict[int, dict[str, Any]] = defaultdict(new_second_bucket)
    request_rows: list[dict[str, Any]] = []
    error_counts: Counter[tuple[int, int, str, str, str]] = Counter()
    worker_ready: asyncio.Queue[int] = asyncio.Queue()
    start_event = asyncio.Event()
    stop_sampler = asyncio.Event()
    active_requests = 0
    peak_pending = 0
    peak_queue_depth = 0
    sessions_opened = 0
    sessions_rotated = 0
    connections_established = 0
    start_monotonic = 0.0
    start_unix = 0.0
    request_counter = 0

    def elapsed_second(now: float | None = None) -> int:
        moment = time.monotonic() if now is None else now
        return max(0, int(moment - start_monotonic))

    def segment_by_index(index: int) -> Segment:
        return next(segment for segment in segments if segment.index == index)

    async def make_session() -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(
            limit=1,
            limit_per_host=1,
            keepalive_timeout=args.keepalive_timeout,
            ttl_dns_cache=300,
        )
        return aiohttp.ClientSession(connector=connector, timeout=timeout)

    async def worker(worker_id: int) -> None:
        nonlocal active_requests, sessions_opened, sessions_rotated, connections_established
        session = await make_session()
        sessions_opened += 1
        try:
            async with session.get(health_url) as response:
                await response.read()
                if response.status >= 400:
                    raise RuntimeError(f"connection warm-up returned HTTP {response.status}")
            connections_established += 1
            await worker_ready.put(worker_id)
            await start_event.wait()
            requests_on_connection = 0
            connection_generation = 1
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                active_requests += 1
                started_monotonic = time.monotonic()
                started_unix = time.time()
                sent_second = elapsed_second(started_monotonic)
                stats[sent_second]["started"] += 1
                outcome = "connection_errors"
                status = ""
                pod = "unknown"
                exception_type = ""
                exception_message = ""
                try:
                    endpoint = request_base + str(item.record_id)
                    async with session.get(endpoint) as response:
                        await response.read()
                        status = response.status
                        pod = response.headers.get("X-Pod", "unknown")
                    outcome = "ok" if 200 <= int(status) < 300 else "non2xx"
                except asyncio.TimeoutError as exc:
                    outcome = "timeouts"
                    exception_type = type(exc).__name__
                    exception_message = str(exc)[:240]
                except (aiohttp.ClientError, OSError) as exc:
                    outcome = "connection_errors"
                    exception_type = type(exc).__name__
                    exception_message = str(exc)[:240]
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                finally:
                    completed_monotonic = time.monotonic()
                    completed_unix = time.time()
                    completed_second = elapsed_second(completed_monotonic)
                    client_ms = max(0.0, (completed_monotonic - item.planned_monotonic) * 1000)
                    request_ms = max(0.0, (completed_monotonic - started_monotonic) * 1000)
                    queue_ms = max(0.0, (started_monotonic - item.planned_monotonic) * 1000)
                    bucket = stats[completed_second]
                    bucket["completed"] += 1
                    bucket[outcome] += 1
                    bucket["client_ms"].append(client_ms)
                    bucket["request_ms"].append(request_ms)
                    bucket["queue_ms"].append(queue_ms)
                    if outcome == "ok":
                        bucket["pods"][pod] += 1
                    if exception_type:
                        error_counts[(completed_second, item.segment_index, outcome,
                                      exception_type, exception_message)] += 1
                    request_rows.append({
                        "request_id": item.request_id,
                        "segment_index": item.segment_index,
                        "segment_name": segment_by_index(item.segment_index).name,
                        "connection_id": worker_id,
                        "connection_generation": connection_generation,
                        "planned_unix": round(item.planned_unix, 6),
                        "started_unix": round(started_unix, 6),
                        "completed_unix": round(completed_unix, 6),
                        "queue_ms": round(queue_ms, 3),
                        "request_ms": round(request_ms, 3),
                        "client_ms": round(client_ms, 3),
                        "status": status,
                        "outcome": outcome,
                        "pod": pod,
                    })
                    active_requests -= 1
                    requests_on_connection += 1
                    queue.task_done()
                if requests_on_connection >= args.rotate_after:
                    await session.close()
                    session = await make_session()
                    sessions_opened += 1
                    sessions_rotated += 1
                    async with session.get(health_url) as response:
                        await response.read()
                        if response.status >= 400:
                            raise RuntimeError(
                                f"rotated connection warm-up returned HTTP {response.status}"
                            )
                    connections_established += 1
                    requests_on_connection = 0
                    connection_generation += 1
        finally:
            await session.close()

    async def sampler() -> None:
        nonlocal peak_pending, peak_queue_depth
        next_tick = start_monotonic + 1
        while not stop_sampler.is_set():
            delay = next_tick - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop_sampler.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass
            second = elapsed_second(next_tick) - 1
            second = max(0, second)
            depth = queue.qsize()
            stats[second]["queue_depth_end"] = depth
            stats[second]["inflight_end"] = active_requests
            peak_queue_depth = max(peak_queue_depth, depth)
            peak_pending = max(peak_pending, depth + active_requests)
            next_tick += 1

    workers = [asyncio.create_task(worker(i + 1)) for i in range(args.connections)]
    for _ in workers:
        await asyncio.wait_for(worker_ready.get(), timeout=args.timeout + 10)
    start_monotonic = time.monotonic()
    start_unix = time.time()
    start_event.set()
    sampler_task = asyncio.create_task(sampler())
    below_target_history: list[bool] = []
    executed_segments: list[Segment] = []
    stop_reason = "profile_complete" if args.command == "profile" else "max_steps"

    async def produce(segment: Segment) -> None:
        nonlocal request_counter, peak_pending, peak_queue_depth
        count = int(segment.duration_seconds * segment.target_rps)
        for index in range(count):
            planned = start_monotonic + segment.begin_second + index / segment.target_rps
            # Windows' event loop can wake a short sleep slightly early. Keep
            # waiting until the planned open-loop arrival.
            while (delay := planned - time.monotonic()) > 0:
                await asyncio.sleep(delay)
            planned_second = max(0, int(planned - start_monotonic))
            stats[planned_second]["scheduled"] += 1
            pending = queue.qsize() + active_requests
            peak_pending = max(peak_pending, pending)
            peak_queue_depth = max(peak_queue_depth, queue.qsize())
            request_counter += 1
            if pending >= args.max_pending:
                stats[planned_second]["queue_rejected"] += 1
                continue
            item = WorkItem(
                request_id=request_counter,
                planned_monotonic=planned,
                planned_unix=start_unix + segment.begin_second + index / segment.target_rps,
                segment_index=segment.index,
                record_id=ids[(request_counter - 1) % len(ids)],
            )
            queue.put_nowait(item)
            stats[planned_second]["enqueued"] += 1

    try:
        for segment in segments:
            executed_segments.append(segment)
            await produce(segment)
            boundary = start_monotonic + segment.end_second
            if time.monotonic() < boundary:
                await asyncio.sleep(boundary - time.monotonic())
            if args.command == "steps" and segment.name != "warmup":
                successes = sum(
                    stats[second]["ok"]
                    for second in range(segment.window_start_second, segment.end_second)
                )
                achieved = successes / segment.summary_window_seconds
                below_target_history.append(achieved < 0.9 * segment.target_rps)
                if args.max_rps and segment.target_rps >= args.max_rps:
                    stop_reason = "max_rps"
                    break
                if len(below_target_history) >= 2 and all(below_target_history[-2:]):
                    stop_reason = "two_below_90"
                    break
    finally:
        profile_end_unix = time.time()
        try:
            await asyncio.wait_for(queue.join(), timeout=args.drain_timeout)
        except asyncio.TimeoutError:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is not None:
                    stats[elapsed_second()]["cancelled"] += 1
                queue.task_done()
            try:
                await asyncio.wait_for(queue.join(), timeout=args.timeout + 1)
            except asyncio.TimeoutError:
                for task in workers:
                    task.cancel()
        for _ in workers:
            queue.put_nowait(None)
        await asyncio.gather(*workers, return_exceptions=True)
        stop_sampler.set()
        await sampler_task
    finish_unix = time.time()

    seconds_path = output.with_name(output.name + "_seconds.csv")
    steps_path = output.with_name(output.name + "_steps.csv")
    requests_path = output.with_name(output.name + "_requests.csv")
    pods_path = output.with_name(output.name + "_pods_seconds.csv")
    errors_path = output.with_name(output.name + "_errors.csv")
    meta_path = output.with_name(output.name + "_meta.json")

    active_segments = executed_segments
    last_nominal_second = max((s.end_second for s in active_segments), default=0)
    last_second = max(max(stats, default=-1) + 1, last_nominal_second)

    def segment_at(second: int) -> Segment | None:
        for segment in active_segments:
            if segment.begin_second <= second < segment.end_second:
                return segment
        return None

    second_fields = [
        "unix_second", "elapsed_second", "segment_index", "segment_name",
        "segment_second", "target_rps", "scheduled_rps", "enqueued_rps",
        "started_rps", "sent_rps", "completed_rps", "success_rps", "http_2xx",
        "http_non2xx", "connection_errors", "timeouts", "queue_rejected",
        "cancelled", "inflight_end", "queue_depth_end", "p50_client_ms",
        "p95_client_ms", "p99_client_ms", "p99_ms", "p50_request_ms",
        "p95_request_ms", "p99_request_ms", "p50_queue_ms", "p95_queue_ms",
        "p99_queue_ms",
    ]
    with seconds_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=second_fields)
        writer.writeheader()
        for second in range(last_second):
            bucket = stats[second]
            segment = segment_at(second)
            client_p99 = percentile(bucket["client_ms"], 0.99)
            writer.writerow({
                "unix_second": int(start_unix + second),
                "elapsed_second": second,
                "segment_index": segment.index if segment else "",
                "segment_name": segment.name if segment else "drain",
                "segment_second": second - segment.begin_second if segment else "",
                "target_rps": segment.target_rps if segment else 0,
                "scheduled_rps": bucket["scheduled"],
                "enqueued_rps": bucket["enqueued"],
                "started_rps": bucket["started"],
                "sent_rps": bucket["started"],
                "completed_rps": bucket["completed"],
                "success_rps": bucket["ok"],
                "http_2xx": bucket["ok"],
                "http_non2xx": bucket["non2xx"],
                "connection_errors": bucket["connection_errors"],
                "timeouts": bucket["timeouts"],
                "queue_rejected": bucket["queue_rejected"],
                "cancelled": bucket["cancelled"],
                "inflight_end": bucket["inflight_end"],
                "queue_depth_end": bucket["queue_depth_end"],
                "p50_client_ms": rounded(percentile(bucket["client_ms"], 0.50)),
                "p95_client_ms": rounded(percentile(bucket["client_ms"], 0.95)),
                "p99_client_ms": rounded(client_p99),
                "p99_ms": rounded(client_p99),
                "p50_request_ms": rounded(percentile(bucket["request_ms"], 0.50)),
                "p95_request_ms": rounded(percentile(bucket["request_ms"], 0.95)),
                "p99_request_ms": rounded(percentile(bucket["request_ms"], 0.99)),
                "p50_queue_ms": rounded(percentile(bucket["queue_ms"], 0.50)),
                "p95_queue_ms": rounded(percentile(bucket["queue_ms"], 0.95)),
                "p99_queue_ms": rounded(percentile(bucket["queue_ms"], 0.99)),
            })

    summary_rows: list[dict[str, Any]] = []
    for segment in active_segments:
        window_start = segment.window_start_second
        window_end = segment.end_second
        seconds = range(window_start, window_end)
        success_values = [stats[second]["ok"] for second in seconds]
        client_values = [value for second in seconds for value in stats[second]["client_ms"]]
        pod_counts = Counter()
        for second in seconds:
            pod_counts.update(stats[second]["pods"])
        window_length = max(1, segment.summary_window_seconds)
        success_rps = sum(success_values) / window_length
        summary_rows.append({
            "segment_index": segment.index,
            "segment_name": segment.name,
            "duration_seconds": segment.duration_seconds,
            "target_rps": segment.target_rps,
            "window_start_second": window_start,
            "window_end_second": window_end,
            "scheduled_rps": round(sum(stats[s]["scheduled"] for s in seconds) / window_length, 3),
            "started_rps": round(sum(stats[s]["started"] for s in seconds) / window_length, 3),
            "success_rps": round(success_rps, 3),
            "std_rps": round(statistics.pstdev(success_values), 3) if len(success_values) > 1 else 0,
            "attainment_ratio": round(success_rps / segment.target_rps, 6),
            "p50_client_ms": rounded(percentile(client_values, 0.50)),
            "p95_client_ms": rounded(percentile(client_values, 0.95)),
            "p99_client_ms": rounded(percentile(client_values, 0.99)),
            "http_non2xx": sum(stats[s]["non2xx"] for s in seconds),
            "connection_errors": sum(stats[s]["connection_errors"] for s in seconds),
            "timeouts": sum(stats[s]["timeouts"] for s in seconds),
            "queue_rejected": sum(stats[s]["queue_rejected"] for s in seconds),
            "pod_2xx_json": json.dumps(dict(sorted(pod_counts.items())), ensure_ascii=False),
        })
    with steps_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    request_fields = [
        "request_id", "segment_index", "segment_name", "connection_id",
        "connection_generation", "planned_unix", "started_unix", "completed_unix",
        "queue_ms", "request_ms", "client_ms", "status", "outcome", "pod",
    ]
    with requests_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=request_fields)
        writer.writeheader()
        writer.writerows(sorted(request_rows, key=lambda row: row["request_id"]))

    pod_fields = [
        "unix_second", "elapsed_second", "segment_index", "segment_name", "pod",
        "completed_rps",
    ]
    with pods_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=pod_fields)
        writer.writeheader()
        for second in range(last_second):
            segment = segment_at(second)
            for pod, count in sorted(stats[second]["pods"].items()):
                writer.writerow({
                    "unix_second": int(start_unix + second),
                    "elapsed_second": second,
                    "segment_index": segment.index if segment else "",
                    "segment_name": segment.name if segment else "drain",
                    "pod": pod,
                    "completed_rps": count,
                })

    error_fields = [
        "unix_second", "elapsed_second", "segment_index", "error_kind",
        "exception_type", "message", "count",
    ]
    with errors_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=error_fields)
        writer.writeheader()
        for key, count in sorted(error_counts.items()):
            second, segment_index, kind, exception_type, message = key
            writer.writerow({
                "unix_second": int(start_unix + second),
                "elapsed_second": second,
                "segment_index": segment_index,
                "error_kind": kind,
                "exception_type": exception_type,
                "message": message,
                "count": count,
            })

    measured_rows = [row for row in summary_rows if row["segment_name"] != "warmup"]
    peak_row = max(measured_rows, key=lambda row: row["success_rps"]) if measured_rows else None
    final_row = measured_rows[-1] if measured_rows else None
    capacity_reached = bool(final_row and final_row["attainment_ratio"] < 0.9)
    totals = {
        key: sum(stats[second][key] for second in stats)
        for key in ["scheduled", "enqueued", "started", "completed", "ok", "non2xx",
                    "connection_errors", "timeouts", "queue_rejected", "cancelled"]
    }
    meta = {
        "command": args.command,
        "base_url": base_url,
        "replicas": args.replicas,
        "connections": args.connections,
        "expected_connections_8R": 24,
        "rotate_after": args.rotate_after,
        "timeout_seconds": args.timeout,
        "drain_timeout_seconds": args.drain_timeout,
        "max_pending": args.max_pending,
        "unix_start": start_unix,
        "unix_profile_end": profile_end_unix,
        "unix_finish": finish_unix,
        "segments": [asdict(segment) for segment in active_segments],
        "totals": totals,
        "peak_pending": peak_pending,
        "peak_queue_depth": peak_queue_depth,
        "sessions_opened": sessions_opened,
        "sessions_rotated": sessions_rotated,
        "tcp_connections_established": connections_established,
        "stop_reason": stop_reason,
        "capacity_reached": capacity_reached if args.command == "steps" else None,
        "eligible_for_usl": capacity_reached if args.command == "steps" else None,
        "X_peak": peak_row["success_rps"] if peak_row else None,
        "X_std_rps": peak_row["std_rps"] if peak_row else None,
        "X_segment": peak_row["segment_name"] if peak_row else None,
        "final_attainment_ratio": final_row["attainment_ratio"] if final_row else None,
        "files": [str(path) for path in [seconds_path, steps_path, requests_path,
                                          pods_path, errors_path]],
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"saved": meta["files"] + [str(meta_path)], "summary": meta},
                     ensure_ascii=False, indent=2), flush=True)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--ids", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--replicas", type=int, required=True)
    parser.add_argument("--connections", type=int, default=24)
    parser.add_argument("--rotate-after", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--drain-timeout", type=float, default=10.0)
    parser.add_argument("--max-pending", type=int, default=3000)
    parser.add_argument("--keepalive-timeout", type=float, default=60.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    steps = subparsers.add_parser("steps", help="Run the fixed-replica step profile")
    add_common_arguments(steps)
    steps.add_argument("--warmup-seconds", type=int, default=10)
    steps.add_argument("--start-rps", type=float, default=200)
    steps.add_argument("--step-rps", type=float, default=200)
    steps.add_argument("--step-seconds", type=int, default=30)
    steps.add_argument("--window-seconds", type=int, default=20)
    steps.add_argument("--max-steps", type=int, default=15)
    steps.add_argument("--max-rps", type=float, default=0)
    profile = subparsers.add_parser("profile", help="Run a JSON segment profile")
    add_common_arguments(profile)
    profile.add_argument("--profile-file", required=True)
    args = parser.parse_args()
    if args.connections < 1 or args.replicas < 1 or args.rotate_after < 1:
        parser.error("replicas, connections and rotate-after must be positive")
    if args.connections != 24:
        parser.error("lab 3 requires 8*R=24 connections; use --connections 24")
    if args.command == "steps":
        if args.step_seconds != 30 or args.window_seconds != 20:
            parser.error("lab 3 requires 30-second steps and a 20-second summary window")
        if args.window_seconds > args.step_seconds:
            parser.error("window-seconds cannot exceed step-seconds")
    return args


def main() -> None:
    asyncio.run(execute(parse_args()))


if __name__ == "__main__":
    main()

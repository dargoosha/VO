import argparse
import asyncio
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import aiohttp


def percentile(values, percent):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * percent / 100
    lo, hi = math.floor(position), math.ceil(position)
    return round(values[lo] + (values[hi] - values[lo]) * (position - lo), 3)


async def run(args):
    successful_completions_in_window = 0
    ids = None
    if args.path_template == '/prescriptions/{id}':
        ids = json.loads(Path(args.ids).read_text(encoding='utf-8'))
        if not isinstance(ids, list) or not ids:
            raise ValueError('IDs file must contain a non-empty JSON array.')

    loop = asyncio.get_running_loop()
    total_seconds = args.warmup + args.duration
    count = math.ceil(args.rps * total_seconds)
    start = loop.time() + 0.2
    measurement_start = start + args.warmup
    measurement_end = measurement_start + args.duration
    results = []
    statuses = Counter()
    pending = set()
    peak_inflight = 0
    inflight = 0
    requests_started = 0
    errors = []
    completions_during_measurement = 0
    generated_during_measurement = 0

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def one_request(i, scheduled_at):
            nonlocal inflight, peak_inflight, requests_started
            nonlocal completions_during_measurement
            nonlocal successful_completions_in_window
            phase = 'measurement' if scheduled_at >= measurement_start else 'warmup'
            path = args.path_template.replace('{id}', str(ids[i % len(ids)])) if ids else args.path_template
            url = args.base_url.rstrip('/') + path
            begun = loop.time()
            inflight += 1
            peak_inflight = max(peak_inflight, inflight)
            requests_started += 1
            status = 'exception'
            error_message = None
            try:
                async with session.get(url) as response:
                    status = str(response.status)
                    await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                error_message = f'{type(exc).__name__}: {exc}'
            finally:
                finished = loop.time()
                inflight -= 1
                if (
                        phase == 'measurement'
                        and measurement_start <= finished < measurement_end
                ):
                    completions_during_measurement += 1
                    if status.startswith("2"):
                        successful_completions_in_window += 1
                if phase == 'measurement':
                    statuses[status] += 1
                    results.append({
                        'status': status,
                        'latency_ms': (finished - scheduled_at) * 1000,
                        'service_elapsed_ms': (finished - begun) * 1000,
                        'finished_at_s': finished - start,
                    })
                    if error_message and len(errors) < 10:
                        errors.append(error_message)

        for i in range(count):
            scheduled = start + i / args.rps
            delay = scheduled - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if scheduled >= measurement_end:
                break
            if scheduled >= measurement_start:
                generated_during_measurement += 1
            task = asyncio.create_task(one_request(i, scheduled))
            pending.add(task)
            task.add_done_callback(pending.discard)

        if pending:
            await asyncio.gather(*pending)

    values = [r['latency_ms'] for r in results]
    success_values = [r['latency_ms'] for r in results if r['status'].startswith('2')]
    bad = sum(n for status, n in statuses.items() if not status.startswith('2'))
    report = {
        "successful_completions_in_window": successful_completions_in_window,
        "achieved_success_rps": round(
            successful_completions_in_window / args.duration, 3
        ),
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'base_url': args.base_url,
        'path_template': args.path_template,
        'target_rps': args.rps,
        'warmup_seconds': args.warmup,
        'measurement_seconds': args.duration,
        'scheduled_measurement_requests': generated_during_measurement,
        'measurement_requests_completed_eventually': len(results),
        'completions_within_measurement_window': completions_during_measurement,
        'achieved_rps_within_window': round(completions_during_measurement / args.duration, 2),
        'non_2xx_percent': round(bad * 100 / len(results), 3) if results else None,
        'latency_ms_p50': percentile(values, 50),
        'latency_ms_p95': percentile(values, 95),
        'latency_ms_p99': percentile(values, 99),
        'latency_success_ms_p50': percentile(success_values, 50),
        'latency_success_ms_p95': percentile(success_values, 95),
        'latency_success_ms_p99': percentile(success_values, 99),
        'latency_avg_ms': round(statistics.mean(values), 3) if values else None,
        'peak_inflight': peak_inflight,
        'http_status_counts': dict(statuses),
        'error_samples': errors,
        'notes': 'Latency measured from scheduled arrival to completion; phase selected by scheduled arrival. Achieved RPS counts completions inside the fixed 30s measurement window. Percentiles include all completed measurement-phase requests, including those finishing afterward.',
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"Target: {args.rps} RPS | achieved: {report['achieved_rps_within_window']} RPS")
    print(f"Scheduled: {generated_during_measurement} | eventual completions: {len(results)} | completed in-window: {completions_during_measurement}")
    print(f"p50/p95/p99: {report['latency_ms_p50']} / {report['latency_ms_p95']} / {report['latency_ms_p99']} ms")
    print(f"Non-2xx: {report['non_2xx_percent']}% | status counts: {dict(statuses)} | peak in-flight: {peak_inflight}")
    print(f'Result: {path.resolve()}')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base-url', default='http://localhost:8080')
    p.add_argument('--path-template', default='/prescriptions/{id}')
    p.add_argument('--ids', default='results/ids.json')
    p.add_argument('--rps', type=int, required=True)
    p.add_argument('--warmup', type=int, default=10)
    p.add_argument('--duration', type=int, default=30)
    p.add_argument('--timeout', type=float, default=10)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    if args.rps <= 0 or args.duration <= 0 or args.warmup < 0:
        p.error('rps and duration must be positive; warmup must be non-negative')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()

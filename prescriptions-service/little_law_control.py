"""Little's law verification: discrete counts from independently recorded starts/ends.
Run: python little_law_control.py --rps 100 --warmup 10 --duration 30 --output results/little_control.json
Requires aiohttp. Tests GET /prescriptions/{id} with existing results/ids.json.
"""
import argparse
import asyncio
import bisect
import json
import math
import random
import statistics
from collections import Counter
from pathlib import Path
import aiohttp


def roundnum(x):
    return round(x, 5) if x is not None else None


async def run(args):
    ids = json.loads(Path(args.ids).read_text(encoding='utf-8'))
    if not isinstance(ids, list) or not ids:
        raise ValueError('ids.json must contain an array of existing prescription IDs')
    loop = asyncio.get_running_loop()
    start = loop.time() + 0.5
    window_start = start + args.warmup
    window_end = window_start + args.duration
    all_starts, all_finishes, records = [], [], []
    tasks = []
    statuses = Counter()
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=args.connector_limit)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def request(i, scheduled):
            # START is actual entry into the client-side HTTP operation, not nominal schedule.
            begin = loop.time()
            all_starts.append(begin)
            status = 'exception'
            try:
                url = args.base_url.rstrip('/') + '/prescriptions/' + str(ids[i % len(ids)])
                async with session.get(url) as resp:
                    status = str(resp.status)
                    await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                status = 'exception:' + type(exc).__name__
            finally:
                finish = loop.time()
                all_finishes.append(finish)
                if window_start <= begin < window_end:
                    records.append((begin, finish, status))
                    statuses[status] += 1

        total = math.ceil((args.warmup + args.duration) * args.rps)
        for i in range(total):
            scheduled = start + i / args.rps
            if scheduled >= window_end:
                break
            if scheduled > loop.time():
                await asyncio.sleep(scheduled - loop.time())
            tasks.append(asyncio.create_task(request(i, scheduled)))
        await asyncio.gather(*tasks)

    starts = sorted(all_starts)
    finishes = sorted(all_finishes)

    def concurrency_at(t):
        # Two counters independently incremented on actual start and finish.
        return bisect.bisect_right(starts, t) - bisect.bisect_right(finishes, t)

    def regular_samples(step, phase):
        n = math.floor((args.duration - phase) / step)
        times = (window_start + phase + k * step for k in range(n))
        return [concurrency_at(t) for t in times]

    # At 100 RPS arrivals happen every 10ms. A single 10ms sampling phase can alias.
    # Report phase sensitivity rather than silently selecting the most favourable phase.
    step = 0.01
    samples_phase0 = regular_samples(step, 0.0)
    samples_phase5 = regular_samples(step, 0.005)
    samples_1ms = regular_samples(0.001, 0.0005)
    # Randomized stratified sampling: one observation in each 10ms bin.
    rng = random.Random(6146)
    jittered = [concurrency_at(window_start + k * step + rng.random() * step)
                for k in range(int(args.duration / step))]

    successful = [r for r in records if r[2].startswith('2')]
    success_fin_window = [r for r in records if r[2].startswith('2') and r[1] < window_end]
    all_fin_window = [f for f in finishes if window_start <= f < window_end]
    mean_W = statistics.mean((end - begin for begin, end, _ in successful)) if successful else None
    lambda_success = len(success_fin_window) / args.duration
    Little = lambda_success * mean_W if mean_W is not None else None

    def deviation(measured):
        return abs(measured - Little) / Little * 100 if Little and measured is not None else None

    phase0 = statistics.mean(samples_phase0)
    phase5 = statistics.mean(samples_phase5)
    ms1 = statistics.mean(samples_1ms)
    jitter = statistics.mean(jittered)
    result = {
        'config': {'rps': args.rps, 'warmup_s': args.warmup, 'duration_s': args.duration,
                   'timeout_s': args.timeout, 'connector_limit': args.connector_limit,
                   'base_url': args.base_url},
        'measurement_requests_started': len(records),
        'measurement_successes_eventually': len(successful),
        'measurement_successes_within_window': len(success_fin_window),
        'departures_any_phase_within_window': len(all_fin_window),
        'error_fraction_percent': roundnum(100 * (len(records)-len(successful)) / len(records)) if records else None,
        'lambda_success_per_s': roundnum(lambda_success),
        'mean_W_success_ms_actual_start_to_finish': roundnum(mean_W * 1000) if mean_W is not None else None,
        'lambda_times_W': roundnum(Little),
        'L_regular_10ms_phase_0': roundnum(phase0),
        'L_regular_10ms_phase_5ms': roundnum(phase5),
        'L_regular_1ms': roundnum(ms1),
        'L_stratified_10ms_random_phase': roundnum(jitter),
        'sample_counts': {'phase0':len(samples_phase0),'phase5':len(samples_phase5),
                          '1ms':len(samples_1ms),'random':len(jittered)},
        'deviation_1ms_pct': roundnum(deviation(ms1)),
        'deviation_random_phase_pct': roundnum(deviation(jitter)),
        'status_counts': dict(statuses),
        'interpretation': 'L counts actual started-minus-finished requests at fixed times; W measures actual start-to-finish elapsed time. Window edges, client scheduling, timing jitter and errors can affect equality.'
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print('Success RPS:',result['lambda_success_per_s'],'| mean W:',result['mean_W_success_ms_actual_start_to_finish'],'ms')
    print('lambda*W:',result['lambda_times_W'],'| L at 1ms:',result['L_regular_1ms'],
          '| deviation:',result['deviation_1ms_pct'],'%')
    print('L 10ms phases 0ms/5ms:',result['L_regular_10ms_phase_0'],result['L_regular_10ms_phase_5ms'])
    print('L randomized-10ms:',result['L_stratified_10ms_random_phase'],
          '| deviation:',result['deviation_random_phase_pct'],'%')
    print('Counts:', result['sample_counts'], '| errors:',result['error_fraction_percent'],'%')
    print('Saved:',output.resolve())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://localhost:8080')
    parser.add_argument('--ids',default='results/ids.json')
    parser.add_argument('--rps',type=int,default=100)
    parser.add_argument('--warmup',type=float,default=10)
    parser.add_argument('--duration',type=float,default=30)
    parser.add_argument('--timeout',type=float,default=10)
    parser.add_argument('--connector-limit',type=int,default=0)
    parser.add_argument('--output',default='results/little_control.json')
    a=parser.parse_args()
    if a.rps<=0 or a.duration<=0 or a.warmup<0:parser.error('invalid time/rps values')
    asyncio.run(run(a))

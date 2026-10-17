"""Additional open/closed-loop tests for prescriptions-service Lab 1.
Requires: pip install aiohttp. Run from the prescriptions-service directory.
"""
import argparse
import asyncio
import json
import math
import statistics
from collections import Counter
from pathlib import Path
import aiohttp


def pctl(vals, q):
    if not vals:
        return None
    a = sorted(vals)
    x = (len(a)-1)*q/100
    low = int(x)
    high = min(low+1, len(a)-1)
    return round(a[low] + (a[high]-a[low])*(x-low), 3)


async def run(cfg):
    ids = json.loads(Path(cfg.ids).read_text(encoding='utf-8'))
    if not ids:
        raise ValueError('results/ids.json is empty; run python seed.py first.')
    loop = asyncio.get_running_loop()
    start = loop.time() + .5
    measure_start = start + cfg.warmup
    end = measure_start + cfg.duration
    active = 0
    inflight_samples = []
    peak = 0
    active_area = 0.
    last_change = measure_start
    measurements = []
    completed_in_window = 0
    successes_in_window = 0
    statuses = Counter()
    errors = []
    planned = 0
    tasks = []
    next_id = 0
    timeout = aiohttp.ClientTimeout(total=cfg.timeout)
    async def sample_inflight():
        await asyncio.sleep(max(0, measure_start - loop.time()))

        while loop.time() < end:
            inflight_samples.append(active)
            await asyncio.sleep(0.01)

    def update_area(now):
        nonlocal active_area, last_change
        stop = min(now, end)
        if stop > last_change:
            active_area += active * (stop-last_change)
            last_change = stop

    connector = aiohttp.TCPConnector(
        limit=cfg.connector_limit,
        limit_per_host=cfg.connector_limit
    )
    async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector
    ) as sess:
        async def request(idx, scheduled=None):
            nonlocal active, peak, completed_in_window, successes_in_window
            begin = loop.time()
            update_area(begin)
            active += 1
            peak = max(peak, active)
            status = 'exception'
            err = None
            url = cfg.base_url.rstrip('/') + '/prescriptions/' + str(ids[idx % len(ids)])
            try:
                async with sess.get(url) as res:
                    status = str(res.status)
                    await res.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
                err = type(ex).__name__ + ': ' + str(ex)
            finally:
                finish = loop.time()
                update_area(finish)
                active -= 1
                # Only requests arriving in measurement phase enter latency sample
                if scheduled is None:
                    belongs = begin >= measure_start and begin < end
                    ref = begin
                else:
                    belongs = scheduled >= measure_start and scheduled < end
                    ref = scheduled
                if belongs:
                    measurements.append((finish-ref, finish-begin, status))
                    statuses[status] += 1
                    if err and len(errors)<8:
                        errors.append(err)
                if measure_start <= finish < end:
                    completed_in_window += 1
                    if status.startswith('2'):
                        successes_in_window += 1

        async def closed_worker(worker):
            n = worker
            while True:
                now = loop.time()
                if now >= end:
                    break
                if now < start:
                    await asyncio.sleep(start-now)
                if loop.time() >= end:
                    break
                await request(n)
                n += cfg.clients

        sampler_task = asyncio.create_task(sample_inflight())

        if cfg.mode == 'open':
            amount = math.ceil(cfg.rps * (cfg.warmup + cfg.duration))
            for i in range(amount):
                scheduled = start + i / cfg.rps
                if scheduled >= end:
                    break
                delay = scheduled - loop.time()
                if delay>0:
                    await asyncio.sleep(delay)
                if scheduled >= measure_start:
                    planned += 1
                tasks.append(asyncio.create_task(request(i, scheduled)))
            if tasks:
                await asyncio.gather(*tasks)
        else:
            await asyncio.gather(*(closed_worker(i) for i in range(cfg.clients)))
        await sampler_task
    update_area(loop.time())
    latencies = [lat*1000 for lat,_,_ in measurements]
    succeeded = [elapsed*1000 for _,elapsed,status in measurements if status.startswith('2')]
    mean_inflight = active_area / cfg.duration
    mean_inflight_sampled = (
        statistics.mean(inflight_samples) if inflight_samples else 0.0
    )
    achieved = successes_in_window / cfg.duration
    mean_w = statistics.mean(succeeded)/1000 if succeeded else 0
    little_calc = achieved * mean_w
    result = dict(mode=cfg.mode, target_rps=cfg.rps if cfg.mode=='open' else None,
                  clients=cfg.clients if cfg.mode=='closed' else None,
                  warmup_seconds=cfg.warmup, measurement_seconds=cfg.duration,
                  planned_measurement_requests=planned if cfg.mode=='open' else None,
                  measurement_samples=len(measurements), completed_in_window=completed_in_window,
                  successful_completions_in_window=successes_in_window,
                  achieved_success_rps=round(achieved,3),
                  p50_ms=pctl(latencies,50),p95_ms=pctl(latencies,95),p99_ms=pctl(latencies,99),
                  p95_success_ms=pctl(succeeded,95),
                  non_2xx_percent=round(100*sum(n for s,n in statuses.items() if not s.startswith('2'))/len(measurements),3) if measurements else None,
                  mean_latency_success_ms=round(mean_w*1000,3),
                  mean_inflight=round(mean_inflight,4),
                  mean_inflight_sampled=round(mean_inflight_sampled,4),
                  sampling_interval_ms=10, sampling_count=len(inflight_samples),
                  peak_inflight=peak,
                  little_lambda_W=round(little_calc,4),
                  little_deviation_percent=round(100*abs(mean_inflight-little_calc)/little_calc,3) if little_calc>0 else None,
                  little_deviation_sampled_percent=round(100*abs(mean_inflight_sampled-little_calc)/little_calc,3) if little_calc>0 else None,
                  status_counts=dict(statuses),error_samples=errors,
                  notes='Little approximation compares measurement-window mean concurrency with achieved successful completions/sec times mean latency for measurement-phase arrivals. Boundary discrepancies and exceptions may affect equality.')
    dest = Path(cfg.output)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(f"Mode={cfg.mode} | achieved success RPS={result['achieved_success_rps']} | p95={result['p95_ms']} ms | non-2xx={result['non_2xx_percent']}%")
    print(f"Mean L sampled every 10 ms={result['mean_inflight_sampled']} ({result['sampling_count']} samples); lambda*W={result['little_lambda_W']}; deviation={result['little_deviation_sampled_percent']}% | peak={peak}")
    print(f"Saved: {dest.resolve()}")


def main():
    a=argparse.ArgumentParser()
    a.add_argument('--mode',choices=['open','closed'],required=True)
    a.add_argument('--rps',type=int,default=100)
    a.add_argument('--clients',type=int,default=10)
    a.add_argument('--warmup',type=float,default=10)
    a.add_argument('--duration',type=float,default=30)
    a.add_argument('--timeout',type=float,default=10)
    a.add_argument('--base-url',default='http://localhost:8080')
    a.add_argument('--ids',default='results/ids.json')
    a.add_argument('--output',required=True)
    a.add_argument("--connector-limit",type=int,default=0)
    c=a.parse_args()
    if c.rps<=0 or c.clients<=0 or c.duration<=0 or c.warmup<0:
        a.error('Bad numeric arguments')
    asyncio.run(run(c))

if __name__=='__main__':
    main()

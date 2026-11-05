"""Lab 2 open-loop step generator. Install: python -m pip install aiohttp.
Raw seconds CSV and step summaries CSV. Python 3.10+.
"""
import argparse
import asyncio
import csv
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import aiohttp


def percentile(data, q):
    if not data:
        return float('nan')
    s = sorted(data)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


async def run(args):
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    seconds_file = out.with_name(out.stem + '_seconds.csv')
    steps_file = out.with_name(out.stem + '_steps.csv')
    meta_file = out.with_name(out.stem + '_meta.json')
    ids = [1]
    if args.ids:
        ids = json.loads(Path(args.ids).read_text(encoding='utf-8'))
        if not ids:
            raise ValueError('IDs file is empty')
    if args.mode == 'web' and not args.ids:
        raise ValueError('--ids results/ids.json is required for web mode')
    url = args.url.rstrip('/')
    if args.mode == 'noop':
        url += '/noop'
    if args.mode == 'web':
        url += '/prescriptions/'
    stats = defaultdict(lambda: {
        'scheduled': 0,
        'sent': 0,
        'wire_sent': 0,
        'ok': 0,
        'cohort_ok': 0,
        'http_status_non2xx': 0,
        'connection_errors': 0,
        'timeouts': 0,
        'lat': [],
    })
    pending = set()
    start = time.monotonic()
    absolute_start = time.time()
    current_step = 0
    peak_pending = 0

    trace_config = aiohttp.TraceConfig()

    async def on_request_headers_sent(session, trace_config_ctx, params):
        """Count a request only when aiohttp actually writes its headers."""
        request_state = trace_config_ctx.trace_request_ctx
        if request_state is not None and not request_state['wire_sent']:
            send_sec = int(time.monotonic() - start)
            stats[send_sec]['wire_sent'] += 1
            request_state['wire_sent'] = True
            request_state['send_sec'] = send_sec

    trace_config.on_request_headers_sent.append(on_request_headers_sent)

    async def hit(planned, rid):
        endpoint = url if args.mode == 'noop' else url + str(rid)
        attempt_sec = int(time.monotonic() - start)
        stats[attempt_sec]['sent'] += 1
        request_state = {
            'wire_sent': False,
            'send_sec': None,
            'attempt_sec': attempt_sec,
        }
        cancelled = False
        try:
            async with session.get(
                endpoint,
                trace_request_ctx=request_state,
            ) as res:
                await res.read()
                code = res.status
            kind = 'ok' if 200 <= code < 300 else 'http_status_non2xx'
        except asyncio.TimeoutError:
            kind = 'timeouts'
        except asyncio.CancelledError:
            kind = 'timeouts'
            cancelled = True
        except (aiohttp.ClientError, OSError):
            kind = 'connection_errors'
        done = time.monotonic()
        completion_bucket = stats[int(done - start)]
        completion_bucket['lat'].append(max(0., (done - planned) * 1000))
        cohort_sec = request_state['attempt_sec']
        cohort_bucket = stats[cohort_sec]
        if kind == 'ok':
            completion_bucket['ok'] += 1
            cohort_bucket['cohort_ok'] += 1
        else:
            cohort_bucket[kind] += 1
        if cancelled:
            raise asyncio.CancelledError

    def schedule_task(planned, i):
        nonlocal peak_pending
        t = asyncio.create_task(hit(planned, ids[i % len(ids)]))
        pending.add(t)
        peak_pending = max(peak_pending, len(pending))
        t.add_done_callback(pending.discard)

    step_summaries = []
    history = []
    step_records = []
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(
        limit=args.connection_limit,
        limit_per_host=args.connection_limit,
        ttl_dns_cache=300,
        keepalive_timeout=30
    )

    def summarize_step(step, rate, begin):
        window_start = begin + 5
        window_end = begin + args.step_seconds
        seconds = range(window_start, window_end)
        window = [stats[s]['ok'] for s in seconds]
        success_rps = sum(window) / len(window)
        std = statistics.pstdev(window) if len(window) > 1 else 0.
        lat = [v for sec in seconds for v in stats[sec]['lat']]
        status_non2xx = sum(stats[s]['http_status_non2xx'] for s in seconds)
        conn_err = sum(stats[s]['connection_errors'] for s in seconds)
        timeouts = sum(stats[s]['timeouts'] for s in seconds)
        sent_count = sum(stats[s]['sent'] for s in seconds)
        wire_sent_count = sum(stats[s]['wire_sent'] for s in seconds)
        send = sent_count / len(window)
        wire_send = wire_sent_count / len(window)
        http_non2xx = status_non2xx + timeouts
        failed = status_non2xx + timeouts + conn_err
        return dict(
            step=step + 1,
            target_rps=rate,
            success_rps=round(success_rps, 3),
            std_rps=round(std, 3),
            send_rps=round(send, 3),
            wire_sent_rps=round(wire_send, 3),
            p50_ms=round(percentile(lat, .50), 3),
            p95_ms=round(percentile(lat, .95), 3),
            p99_ms=round(percentile(lat, .99), 3),
            http_non2xx=http_non2xx,
            http_status_non2xx=status_non2xx,
            connection_errors=conn_err,
            timeouts=timeouts,
            error_fraction=round(failed / sent_count, 6) if sent_count else float('nan'),
        )

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trace_configs=[trace_config],
    ) as session:
        async def produce(duration, rate, begin):
            # Absolute scheduling: do not wait for previous requests.
            count = int(duration * rate)
            for j in range(count):
                planned = start + begin + j / rate
                delay = planned - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                sec = int(planned - start)
                stats[sec]['scheduled'] += 1
                schedule_task(planned, j)

        await produce(args.warmup, args.start_rps, 0)
        measured_steps = []
        for step in range(args.max_steps):
            current_step = step
            rate = args.start_rps + step * args.step_rps
            begin = args.warmup + step * args.step_seconds
            await produce(args.step_seconds, rate, begin)
            # Do not pause between steps; arrivals must remain continuous.
            await asyncio.sleep(0)
            measured_steps.append((step, rate, begin))
            row = summarize_step(step, rate, begin)
            print('STEP', row, flush=True)
            history.append(row['success_rps'] < .9 * rate)
            if args.max_rps and rate >= args.max_rps:
                break
            if len(history) >= 2 and history[-1] and history[-2]:
                print('STOP: two consecutive steps below 90% target', flush=True)
                break
        if pending:
            print('Draining pending tasks (<= timeout)', flush=True)
            await asyncio.wait(pending, timeout=args.timeout + 1)
            for task in list(pending):
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    step_records = [summarize_step(*spec) for spec in measured_steps]
    for row in step_records:
        print('FINAL STEP', row, flush=True)

    last_sec = max(stats, default=-1)
    def step_at(sec):
        if sec < args.warmup:
            return 0
        return min(len(measured_steps), 1 + (sec - args.warmup) // args.step_seconds)
    def rate_at(sec):
        step = step_at(sec)
        return args.start_rps if step == 0 else args.start_rps + (step - 1) * args.step_rps
    with seconds_file.open('w', newline='', encoding='utf-8') as f:
        fields = ['unix_second', 'elapsed_second', 'step', 'target_rps',
                  'scheduled_rps', 'sent_rps', 'wire_sent_rps',
                  'success_rps', 'http_2xx',
                  'http_non2xx', 'http_status_non2xx', 'connection_errors',
                  'timeouts', 'error_fraction', 'p50_ms', 'p95_ms', 'p99_ms']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for sec in range(last_sec+1):
            z=stats[sec]
            w.writerow(dict(unix_second=int(absolute_start+sec),elapsed_second=sec,step=step_at(sec),target_rps=rate_at(sec),
                scheduled_rps=z['scheduled'],sent_rps=z['sent'],wire_sent_rps=z['wire_sent'],
                success_rps=z['ok'],http_2xx=z['ok'],
                http_non2xx=z['http_status_non2xx']+z['timeouts'],
                http_status_non2xx=z['http_status_non2xx'],
                connection_errors=z['connection_errors'],timeouts=z['timeouts'],
                error_fraction=round((z['http_status_non2xx']+z['timeouts']+z['connection_errors'])/z['sent'],6) if z['sent'] else '',
                p50_ms=round(percentile(z['lat'],.50),3),p95_ms=round(percentile(z['lat'],.95),3),p99_ms=round(percentile(z['lat'],.99),3)))
    with steps_file.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(step_records[0]));w.writeheader();w.writerows(step_records)
    meta={'mode':args.mode,'base_url':args.url,'unix_start':absolute_start,'warmup':args.warmup,'step_seconds':args.step_seconds,
          'first_step_rps':args.start_rps,'increment_rps':args.step_rps,'timeout':args.timeout,
          'connection_limit':args.connection_limit,
          'peak_pending':peak_pending,'X_peak':max(x['success_rps'] for x in step_records),'X_step':max(step_records,key=lambda x:x['success_rps']),
          'steps':len(step_records),'last_step_target':step_records[-1]['target_rps'],'last_step_success':step_records[-1]['success_rps']}
    meta_file.write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
    print('SAVED:', seconds_file,steps_file,meta_file,flush=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--mode',choices=['web','noop'],required=True)
    p.add_argument('--url',default='http://localhost:8080')
    p.add_argument('--ids')
    p.add_argument('--output',required=True)
    p.add_argument('--start-rps',type=int,default=200)
    p.add_argument('--step-rps',type=int,default=200)
    p.add_argument('--warmup',type=int,default=10)
    p.add_argument('--step-seconds',type=int,default=20)
    p.add_argument('--timeout',type=float,default=5)
    p.add_argument('--max-steps',type=int,default=15)
    p.add_argument('--max-rps',type=int,default=0,help='Stop once this target step is measured. 0 disables.')
    p.add_argument('--connection-limit',type=int,default=0,
                   help='aiohttp connector limit; 0 means unlimited (default).')
    args=p.parse_args()
    if (args.start_rps<1 or args.step_rps<1 or args.warmup<0 or
            args.step_seconds<6 or args.max_steps<1 or args.connection_limit<0):
        p.error('Invalid positive step settings')
    asyncio.run(run(args))

if __name__=='__main__':
    main()

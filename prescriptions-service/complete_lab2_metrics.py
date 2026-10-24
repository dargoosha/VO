"""Lab 2: retrieve historical peak CPU/in-progress and aligned p99 timelines.
Run inside prescriptions-service while local Prometheus is running.
Only reads existing inputs; writes new CSV/PNG. Never modifies REPORT2.md.
Requires matplotlib: python -m pip install matplotlib
"""
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'results' / 'lab2'
FIG = ROOT / 'images' / 'lab2'
P99 = ('histogram_quantile(0.99, sum by (le) '
       '(rate(http_request_duration_seconds_bucket'
       '{method="GET",path="/prescriptions/{id}"}[15s])))')
CPU = 'rate(process_cpu_seconds_total{job="web"}[15s])'
INFLIGHT = 'http_requests_in_progress{job="web"}'


def query_range(host, expr, start, end, step=5):
    params = urlencode({'query': expr, 'start': f'{start:.3f}',
                        'end': f'{end:.3f}', 'step': str(step)})
    with urlopen(host.rstrip('/') + '/api/v1/query_range?' + params, timeout=35) as response:
        result = json.load(response)
    if result.get('status') != 'success':
        raise RuntimeError('Prometheus query failed: ' + str(result))
    return result['data']['result']


def points(series):
    for s in series:
        instance = s['metric'].get('instance', 'all')
        for t, value in s['values']:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                yield instance, float(t), number


def mean(values):
    return sum(values) / len(values) if values else None


def render_number(x, digits=5):
    return '' if x is None else f'{x:.{digits}f}'


def read_csv(name):
    with (OUT / name).open(newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, columns):
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def run(host):
    OUT.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        raise SystemExit('Install matplotlib: python -m pip install matplotlib')

    # First check TSDB on a known past interval. Current queries cannot reconstruct old runs.
    meta = {}
    for n in range(1, 5):
        path = OUT / f'web{n}_final_meta.json'
        meta[n] = json.loads(path.read_text(encoding='utf-8-sig'))
    if not query_range(host, 'up{job="web"}', float(meta[1]['unix_start']) + 10,
                       float(meta[1]['unix_start']) + 35, 5):
        raise SystemExit('No Prometheus history at first final run: check retention / running Prometheus volume.')

    instance_rows = []
    summary_rows = []
    for n in range(1, 5):
        m = meta[n]
        peak = m['X_step']
        step = int(peak['step'])
        begin = float(m['unix_start']) + float(m.get('warmup', 10)) + (step - 1) * float(m.get('step_seconds', 20))
        # The same final 15 seconds as the client's step summary (20s step, omit first 5s).
        start = begin + 5
        end = begin + float(m.get('step_seconds', 20)) - .01
        groups = {}
        for metric_name, promql in (('cpu_cores', CPU), ('in_progress', INFLIGHT)):
            result = query_range(host, promql, start, end, 5)
            for instance, t, value in points(result):
                groups.setdefault(instance, {}).setdefault(metric_name, []).append(value)
        usable = [(instance, data) for instance, data in groups.items() if 'cpu_cores' in data and 'in_progress' in data]
        # Filter stale processes using rate(http_requests_total) during THIS N's peak window.
        # DNS/instance addresses can be reused across runs; records reflect only historical queries.
        for instance, data in sorted(usable):
            instance_rows.append({'n': n, 'peak_step': step, 'target_rps': peak['target_rps'],
                                  'instance': instance, 'cpu_cores_mean': render_number(mean(data['cpu_cores'])),
                                  'in_progress_mean': render_number(mean(data['in_progress'])),
                                  'cpu_samples': len(data['cpu_cores']),
                                  'in_progress_samples': len(data['in_progress'])})
        av_cpu = mean([mean(d['cpu_cores']) for _, d in usable])
        av_inflight = mean([mean(d['in_progress']) for _, d in usable])
        summary_rows.append({'n': n, 'peak_step': step, 'target_rps': peak['target_rps'],
                             'X_rps': peak['success_rps'], 'instances_with_data': len(usable),
                             'cpu_cores_per_instance': render_number(av_cpu),
                             'cpu_percent_one_core_per_instance': render_number(av_cpu * 100 if av_cpu is not None else None, 2),
                             'in_progress_per_instance': render_number(av_inflight)})
        print(f'N={n}: peak step={step}, CPU={render_number(av_cpu)}, in-progress={render_number(av_inflight)}, instances={len(usable)}')
        if len(usable) != n:
            print(f'  WARNING: expected {n} instance series, got {len(usable)}. Inspect per-instance CSV; do not present missing data as 0.')

    write_csv(OUT / 'peak_prometheus_per_instance.csv', instance_rows,
              ['n','peak_step','target_rps','instance','cpu_cores_mean','in_progress_mean','cpu_samples','in_progress_samples'])
    write_csv(OUT / 'peak_prometheus_summary.csv', summary_rows,
              ['n','peak_step','target_rps','X_rps','instances_with_data','cpu_cores_per_instance','cpu_percent_one_core_per_instance','in_progress_per_instance'])

    timeline = []
    for n in (1, 4):
        m = meta[n]
        start = float(m['unix_start'])
        finish = start + float(m.get('warmup',10)) + int(m['steps']) * float(m.get('step_seconds',20))
        series = query_range(host, P99, start + 5, finish - .1, 5)
        server = [(t, v * 1000) for _,t,v in points(series)]
        if not server:
            print(f'WARNING: no historical server p99 for N={n}.')
        client = read_csv(f'web{n}_final_seconds.csv')
        records = []
        for row in client:
            try:
                t = float(row['unix_second'])
                if t >= finish or t < start: continue
                p99 = float(row['p99_ms'])
                p99 = p99 if math.isfinite(p99) else None
                records.append((t, p99, float(row['target_rps']), float(row['success_rps'])))
            except (ValueError, TypeError, KeyError):
                continue
        for t, p in server:
            timeline.append({'n': n, 'source': 'server', 'unix_time': render_number(t,3), 'p99_ms': render_number(p,3)})
        for t, p99, _, _ in records:
            timeline.append({'n': n, 'source': 'client', 'unix_time': render_number(t,3), 'p99_ms': render_number(p99,3)})
        fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        if records:
            axes[0].plot([(t-start) for t,p,_,_ in records if p is not None],
                         [p for _,p,_,_ in records if p is not None],
                         label='Client p99 (per-second)', linewidth=1.4)
            axes[1].plot([t-start for t,_,_,_ in records], [target for _,_,target,_ in records],
                         label='Target RPS', linestyle='--')
            axes[1].plot([t-start for t,_,_,_ in records], [actual for _,_,_,actual in records],
                         label='Completed 2xx RPS')
        if server:
            axes[0].plot([t-start for t,_ in server], [v for _,v in server],
                         label='Server p99 (15s window, 5s step)', linewidth=1.5)
        axes[0].set_ylabel('p99 (ms)')
        axes[1].set_ylabel('Requests/s')
        axes[1].set_xlabel('Seconds since test start (shared time axis)')
        for ax in axes:
            ax.grid(alpha=.2)
            ax.legend(loc='upper left', fontsize=9)
        fig.suptitle(f'Lab 2: aligned client/server p99 and request rate, N={n}')
        fig.tight_layout()
        filename = FIG / f'39_aligned_p99_n{n}.png' if n == 1 else FIG / '40_aligned_p99_n4.png'
        fig.savefig(filename, dpi=165)
        plt.close(fig)
        print('Saved:', filename.relative_to(ROOT), f'(server data points: {len(server)})')

    write_csv(OUT / 'aligned_p99_timeline.csv', timeline, ['n','source','unix_time','p99_ms'])
    print('Saved: results/lab2/peak_prometheus_summary.csv')
    print('Saved: results/lab2/peak_prometheus_per_instance.csv')
    print('Saved: results/lab2/aligned_p99_timeline.csv')
    print('NOTICE: Means are 5s Prometheus samples averaged over the client\'s 15s peak window, not an integral over all requests.')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--prometheus', default='http://localhost:9090')
    run(p.parse_args().prometheus)

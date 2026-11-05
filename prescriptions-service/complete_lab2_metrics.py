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
REQUEST_RATE = ('sum by (instance) (rate(http_requests_total'
                '{job="web",path="/prescriptions/{id}"}[15s]))')


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


def run(host, series_template, output_prefix, figure_n1, figure_n4):
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
        path = OUT / f'{series_template.format(n=n)}_meta.json'
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

    per_instance_path = OUT / f'{output_prefix}_peak_prometheus_per_instance.csv'
    summary_path = OUT / f'{output_prefix}_peak_prometheus_summary.csv'
    write_csv(per_instance_path, instance_rows,
              ['n','peak_step','target_rps','instance','cpu_cores_mean','in_progress_mean','cpu_samples','in_progress_samples'])
    write_csv(summary_path, summary_rows,
              ['n','peak_step','target_rps','X_rps','instances_with_data','cpu_cores_per_instance','cpu_percent_one_core_per_instance','in_progress_per_instance'])

    timeline = []
    run_metrics = []
    for n in (1, 4):
        m = meta[n]
        start = float(m['unix_start'])
        finish = start + float(m.get('warmup',10)) + int(m['steps']) * float(m.get('step_seconds',20))
        series = query_range(host, P99, start + 5, finish - .1, 5)
        server = [(t, v * 1000) for _,t,v in points(series)]
        if not server:
            print(f'WARNING: no historical server p99 for N={n}.')
        client = read_csv(f'{series_template.format(n=n)}_seconds.csv')
        records = []
        last_target = (float(m.get('first_step_rps', 200)) +
                       (int(m['steps']) - 1) * float(m.get('increment_rps', 200)))
        for row in client:
            try:
                t = float(row['unix_second'])
                if t >= finish or t < start: continue
                p99 = float(row['p99_ms'])
                p99 = p99 if math.isfinite(p99) else None
                records.append((t, p99, min(float(row['target_rps']), last_target),
                                float(row['success_rps'])))
            except (ValueError, TypeError, KeyError):
                continue
        for t, p in server:
            timeline.append({'n': n, 'source': 'server', 'unix_time': render_number(t,3), 'p99_ms': render_number(p,3)})
        for t, p99, _, _ in records:
            timeline.append({'n': n, 'source': 'client', 'unix_time': render_number(t,3), 'p99_ms': render_number(p99,3)})
        per_instance = {}
        for metric_name, promql, scale in (
                ('completed_rps', REQUEST_RATE, 1),
                ('cpu_percent_one_core', CPU, 100),
                ('in_progress', INFLIGHT, 1)):
            result = query_range(host, promql, start + 5, finish - .1, 5)
            for instance, t, value in points(result):
                scaled = value * scale
                per_instance.setdefault(instance, {}).setdefault(metric_name, []).append((t, scaled))
                run_metrics.append({'n': n, 'metric': metric_name, 'instance': instance,
                                    'unix_time': render_number(t,3), 'value': render_number(scaled,5)})

        fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
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
        metric_ax = axes[2]
        cpu_ax = metric_ax.twinx()
        inflight_ax = metric_ax.twinx()
        inflight_ax.spines['right'].set_position(('outward', 60))
        colors = plt.cm.tab10.colors
        for index, (instance, values) in enumerate(sorted(per_instance.items())):
            color = colors[index % len(colors)]
            short = instance.split(':')[0]
            for metric_name, axis, style in (
                    ('completed_rps', metric_ax, '-'),
                    ('cpu_percent_one_core', cpu_ax, '--'),
                    ('in_progress', inflight_ax, ':')):
                rows = values.get(metric_name, [])
                if rows:
                    axis.plot([t-start for t,_ in rows], [v for _,v in rows],
                              color=color, linestyle=style, linewidth=1.2,
                              label=f'{short} {metric_name}')
        metric_ax.set_ylabel('Completed RPS')
        cpu_ax.set_ylabel('CPU, % of one core')
        inflight_ax.set_ylabel('In progress')
        handles, labels = [], []
        for axis in (metric_ax, cpu_ax, inflight_ax):
            axis_handles, axis_labels = axis.get_legend_handles_labels()
            handles.extend(axis_handles); labels.extend(axis_labels)
        if handles:
            metric_ax.legend(handles, labels, loc='upper left', fontsize=7, ncol=3)
        axes[2].set_xlabel('Seconds since test start (shared time axis)')
        for ax in (axes[0], axes[1], metric_ax):
            ax.grid(alpha=.2)
        axes[0].legend(loc='upper left', fontsize=9)
        axes[1].legend(loc='upper left', fontsize=9)
        axes[0].set_title(f'Lab 2: aligned client/server p99 and request rate, N={n}')
        fig.tight_layout()
        filename = FIG / (figure_n1 if n == 1 else figure_n4)
        fig.savefig(filename, dpi=165)
        plt.close(fig)
        print('Saved:', filename.relative_to(ROOT), f'(server data points: {len(server)})')

    p99_path = OUT / f'{output_prefix}_aligned_p99_timeline.csv'
    run_metrics_path = OUT / f'{output_prefix}_aligned_run_metrics.csv'
    write_csv(p99_path, timeline, ['n','source','unix_time','p99_ms'])
    write_csv(run_metrics_path, run_metrics, ['n','metric','instance','unix_time','value'])
    print('Saved:', summary_path)
    print('Saved:', per_instance_path)
    print('Saved:', p99_path)
    print('Saved:', run_metrics_path)
    print('NOTICE: Means are 5s Prometheus samples averaged over the client\'s 15s peak window, not an integral over all requests.')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--prometheus', default='http://localhost:9090')
    p.add_argument('--series-template', default='web{n}_final')
    p.add_argument('--output-prefix', default='lab2')
    p.add_argument('--figure-n1', default='39_aligned_p99_n1.png')
    p.add_argument('--figure-n4', default='40_aligned_p99_n4.png')
    args = p.parse_args()
    run(args.prometheus, args.series_template, args.output_prefix,
        args.figure_n1, args.figure_n4)

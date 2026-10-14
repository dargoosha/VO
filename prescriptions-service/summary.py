"""Summarize 3 repetitions per RPS and instance count using medians."""
import json
import statistics
from pathlib import Path

root = Path('results')
fields = ['achieved_rps_within_window', 'latency_ms_p50', 'latency_ms_p95', 'latency_ms_p99', 'non_2xx_percent']
print('instances,target_rps,achieved_rps,p50_ms,p95_ms,p99_ms,non_2xx_percent,repeats_found')
for instances in (1, 2):
    for rps in (10, 100, 500):
        rows = []
        for repetition in (1, 2, 3):
            f = root / f'web{instances}_{rps}_{repetition}.json'
            if f.exists():
                rows.append(json.loads(f.read_text(encoding='utf-8')))
        values = []
        for field in fields:
            nums = [row[field] for row in rows if row.get(field) is not None]
            values.append(str(round(statistics.median(nums), 3)) if nums else '—')
        print(','.join([str(instances), str(rps), *values, str(len(rows))]))

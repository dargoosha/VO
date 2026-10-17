"""Print medians for the six load-test configurations from results/*.json."""
import json
import statistics
from pathlib import Path
p=Path('results')
print('web,target,success_rps,total_rps,p50_ms,p95_ms,p99_ms,error_pct,repeats')
for web in (1,2):
    for target in (10,100,500):
        rows=[]
        for i in (1,2,3):
            names=([f'web{web}_500_fixed_{i}.json'] if target==500 else [])+[f'web{web}_{target}_{i}.json']
            f=next((p/name for name in names if (p/name).is_file()), None)
            if f: rows.append(json.loads(f.read_text(encoding='utf-8')))
        def med(k,default=None):
            vals=[r[k] for r in rows if r.get(k) is not None]
            return round(statistics.median(vals),3) if vals else default
        values=[web,target,med('achieved_success_rps',med('achieved_rps_within_window') if target in (10,100) else None),med('achieved_rps_within_window'),med('latency_ms_p50'),med('latency_ms_p95'),med('latency_ms_p99'),med('non_2xx_percent'),len(rows)]
        print(','.join(str(x) if x is not None else 'NA' for x in values))

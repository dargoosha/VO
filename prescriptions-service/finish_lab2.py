"""Recover historical server p99/M4 from local Prometheus and capture synchronized Docker stats.
Python standard library only. Run from prescriptions-service directory.
"""
import argparse, csv, json, math, re, subprocess, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

BUCKETS=[0.002,0.005,0.01,0.02,0.05,0.1,0.2,0.5,1.,2.]
OUT=Path('results/lab2')

def api_query(expr, at, host):
    url=host.rstrip('/')+'/api/v1/query?'+urlencode({'query':expr,'time':f'{at:.3f}'})
    try:
        with urlopen(url,timeout=20) as response: body=json.load(response)
    except Exception as ex: raise RuntimeError(f'Prometheus unreachable or query failed: {ex}')
    if body.get('status')!='success': raise RuntimeError(str(body))
    return body['data']['result']

def instant(expr,t,host):
    rows=api_query(expr,t,host)
    if not rows:return None
    try:
        val=float(rows[0]['value'][1]);return val if math.isfinite(val) else None
    except (KeyError,ValueError,IndexError):return None

def histogram(t,host,window=15):
    # Sum increases for a fixed historical 15-second window; not 'now'.
    selector='method="GET",path="/prescriptions/{id}"'
    expr=f'sum by (le) (increase(http_request_duration_seconds_bucket{{{selector}}}[{window}s]))'
    rows=api_query(expr,t,host)
    vals={}
    for r in rows:
        try:
            le=r['metric']['le']; v=float(r['value'][1]);
            if math.isfinite(v):vals[le]=max(0,v)
        except (KeyError,ValueError):pass
    points=[]
    for b in BUCKETS:
        names=[f'{b:g}',str(b)]
        v=next((vals[n] for n in names if n in vals),None)
        if v is None:return None,vals
        points.append((b,v))
    total=vals.get('+Inf')
    if total is None or total<=0:return None,vals
    rank=.99*total
    previous_x=0.;previous_n=0.
    for bound,cum in points:
        if rank <= cum:
            q=bound if cum<=previous_n else previous_x+(bound-previous_x)*(rank-previous_n)/(cum-previous_n)
            return {'p99':q,'lower':previous_x,'upper':bound,'error_width':bound-previous_x,'count':total,'buckets':vals},vals
        previous_x,previous_n=bound,cum
    return {'p99':2.,'lower':2.,'upper':None,'error_width':None,'count':total,'buckets':vals,'beyond':True},vals

def meta_paths():
    return [OUT/f'web{n}_final_meta.json' for n in range(1,5)]

def fetch(args):
    missing=[str(p) for p in meta_paths() if not p.exists()]
    if missing:raise SystemExit('Missing metadata: '+', '.join(missing))
    results=[]; history_ok=True
    for n,mp in enumerate(meta_paths(),start=1):
        meta=json.loads(mp.read_text(encoding='utf-8-sig'))
        start=float(meta['unix_start'])+float(meta.get('warmup',10));duration=float(meta.get('step_seconds',20))
        steps=int(meta.get('steps',0))
        if not steps:
            steps_csv=OUT/f'web{n}_final_steps.csv'
            with steps_csv.open(encoding='utf-8-sig',newline='') as f:steps=sum(1 for _ in csv.DictReader(f))
        for k in range(1,steps+1):
            # End of this 20-second step, minus one second to avoid scrape boundary jitter.
            eval_time=start+k*duration-1
            h,_=histogram(eval_time,args.prometheus,15)
            if h is None:history_ok=False
            results.append({'n':n,'step':k,'target_rps':int(200*k),'unix_time':round(eval_time,3),
                            'p99_server_ms':round(h['p99']*1000,3) if h else None,
                            'bucket_low_ms':round(h['lower']*1000,3) if h else None,
                            'bucket_high_ms':round(h['upper']*1000,3) if h and h['upper'] is not None else None,
                            'max_error_ms':round(h['error_width']*1000,3) if h and h['error_width'] is not None else None,
                            'samples_est':round(h['count'],2) if h else None,'cumulative':h['buckets'] if h else None})
    reference=next((r for r in results if r['n']==1 and r['step']==1 and r['p99_server_ms'] is not None),None)
    threshold=None
    if reference:
        p=reference['p99_server_ms']/1000
        threshold=min(BUCKETS,key=lambda b:(abs(b-p),-b))
    for r in results:
        if threshold is not None and r['cumulative'] and r['samples_est']:
            labels=(f'{threshold:g}',str(threshold));v=next((r['cumulative'][lab] for lab in labels if lab in r['cumulative']),None)
            r['m4']=round(v/r['samples_est'],6) if v is not None else None
        else:r['m4']=None
    fields=['n','step','target_rps','unix_time','p99_server_ms','bucket_low_ms','bucket_high_ms','max_error_ms','samples_est','m4']
    OUT.mkdir(parents=True,exist_ok=True)
    with (OUT/'server_metrics.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({key:r[key] for key in fields} for r in results)
    (OUT/'server_metrics.json').write_text(json.dumps({'threshold_seconds':threshold,'data':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Historical windows checked: {len(results)}; successful: {sum(r["p99_server_ms"] is not None for r in results)}')
    print('M4 threshold:',f'{threshold*1000:g} ms' if threshold else 'NOT DETERMINABLE')
    print('Saved: results/lab2/server_metrics.csv and server_metrics.json')
    if not history_ok:print('WARNING: missing historical histogram data in Prometheus for some intervals. No zeroes invented.')
    write_report(results,threshold)

def write_report(rows,threshold):
    path=Path('REPORT2.md')
    if not path.exists():print('REPORT2.md not found: skipping report update');return
    report=path.read_text(encoding='utf-8')
    # Preserve original report for rollback.
    backup=Path('REPORT2.before_metrics.md')
    if not backup.exists():backup.write_text(report,encoding='utf-8')
    summary=['## 9. Серверний p99: історичні вимірювання Prometheus','',
    'p99 обчислено для останніх 15 секунд кожної сходинки з історичних приростів накопичувальних бакетів. Величини в таблиці — оцінки гістограми; максимальна похибка обмежена шириною бакета (за винятком останнього +Inf).', '',
    '| N | Сходинка, RPS | p99 сервера, мс | Бакет, мс | Максимальна похибка, мс |', '|---:|---:|---:|---|---:|']
    for r in rows:
        p=f'{r["p99_server_ms"]:.3f}' if r['p99_server_ms'] is not None else 'немає історичних даних'
        bk=f'{r["bucket_low_ms"]:g}–{r["bucket_high_ms"]:g}' if r['bucket_high_ms'] is not None else ('понад 2000' if r['bucket_low_ms'] is not None else '—')
        er=f'{r["max_error_ms"]:g}' if r['max_error_ms'] is not None else '—'
        summary.append(f'| {r["n"]} | {r["target_rps"]} | {p} | {bk} | {er} |')
    summary.extend(['','Для клієнтських p99 використано `webN_final_steps.csv`. Серверні та клієнтські перцентилі вимірюють різні ділянки шляху запиту.',''])
    m=['## 10. Додаткова метрика М4','']
    if threshold is None:
        m.append('Історичні дані першої сходинки N=1 недоступні або неповні. Поріг М4 не визначався, отже числову М4 не підмінено здогадом. Для відновлення метрики потрібен новий прогін за працюючого Prometheus.')
    else:
        m.extend([f'Поріг **T={threshold*1000:g} мс** обрано як найближчу межу Г2 до серверного p99 на першій сходинці N=1. Частка M4 = приріст накопичувального бакета `le="{threshold:g}"` / приріст бакета `+Inf`.', '',
        '| N | Інтенсивність, RPS | M4, частка | M4, % |','|---:|---:|---:|---:|'])
        for r in rows:
            if r['n'] in (1,4):
                v=r['m4'];m.append(f'| {r["n"]} | {r["target_rps"]} | {v:.5f} | {v*100:.2f} |' if v is not None else f'| {r["n"]} | {r["target_rps"]} | — | — |')
    m.append('')
    report=re.sub(r'## 9\. .*?(?=\n## 10\.)','\n'.join(summary).rstrip()+'\n',report,flags=re.S)
    report=re.sub(r'## 10\. .*?(?=\n## 11\.)','\n'.join(m).rstrip()+'\n',report,flags=re.S)
    path.write_text(report,encoding='utf-8')
    print('Updated REPORT2.md sections 9 and 10 (backup: REPORT2.before_metrics.md)')

def capture(args):
    OUT.mkdir(parents=True,exist_ok=True)
    filename=OUT/'web4_sync_docker_stats.csv'
    result_path=OUT/'web4_sync'
    command=[sys.executable,'step_load_test.py','--mode','web','--ids','results/ids.json','--output',str(result_path),'--max-rps','2600']
    fields=['sample_unix','name','cpu_percent','mem_usage','mem_percent','net_io','block_io','pids']
    stop=threading.Event()
    def sampler():
        with filename.open('w',newline='',encoding='utf-8') as output:
            w=csv.DictWriter(output,fieldnames=fields);w.writeheader()
            while not stop.is_set():
                try:
                    proc=subprocess.run(['docker','stats','--no-stream','--format','{{json .}}'],capture_output=True,text=True,timeout=8)
                    when=time.time()
                    if proc.returncode==0:
                        for line in proc.stdout.splitlines():
                            try:
                                item=json.loads(line)
                                w.writerow(dict(sample_unix=f'{when:.3f}',name=item.get('Name'),cpu_percent=item.get('CPUPerc'),mem_usage=item.get('MemUsage'),mem_percent=item.get('MemPerc'),net_io=item.get('NetIO'),block_io=item.get('BlockIO'),pids=item.get('PIDs')))
                            except json.JSONDecodeError:pass
                        output.flush()
                    else:print('docker stats error:',proc.stderr.strip(),file=sys.stderr)
                except Exception as exc:print('Docker stats sample failed:',exc,file=sys.stderr)
                stop.wait(1)
    print('Capturing Docker CPU/RAM synchronized with a NEW N=4 experiment.')
    print('Do not change Compose scale while this command runs.')
    thread=threading.Thread(target=sampler,daemon=True);thread.start()
    try:
        with (OUT/'web4_sync_console.txt').open('w',encoding='utf-8') as log:
            proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            for line in proc.stdout:
                print(line,end='');log.write(line);log.flush()
            code=proc.wait()
    finally:
        stop.set();thread.join(timeout=12)
    print('Saved',filename,'and web4_sync_* (CSV / JSON)')
    meta_path=OUT/'web4_sync_meta.json'
    if meta_path.exists():
        meta=json.loads(meta_path.read_text(encoding='utf-8-sig'))
        peak=meta.get('X_step',{}).get('step')
        if peak:
            start=float(meta['unix_start'])+float(meta.get('warmup',10))+(int(peak)-1)*float(meta.get('step_seconds',20))+5
            end=float(meta['unix_start'])+float(meta.get('warmup',10))+int(peak)*float(meta.get('step_seconds',20))
            with filename.open(encoding='utf-8') as f:
                matched=[r for r in csv.DictReader(f) if start<=float(r['sample_unix'])<end]
            with (OUT/'web4_sync_peak_stats.csv').open('w',newline='',encoding='utf-8') as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(matched)
            print(f'X(4) step={peak}, last 15s: {datetime.fromtimestamp(start,timezone.utc).isoformat()} to {datetime.fromtimestamp(end,timezone.utc).isoformat()}')
            print(f'Synchronized Docker resource samples: {len(matched)}; saved web4_sync_peak_stats.csv')
            if not matched:print('WARNING: no matching samples, check Docker Desktop response times.')
    if code:raise SystemExit(code)

if __name__=='__main__':
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='action',required=True)
    q=sub.add_parser('historical');q.add_argument('--prometheus',default='http://localhost:9090')
    sub.add_parser('capture-web4')
    a=p.parse_args()
    if a.action=='historical':fetch(a)
    else:capture(a)

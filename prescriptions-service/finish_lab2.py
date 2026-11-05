"""Recover historical p99/M4 and capture synchronized Docker/generator stats.
Historical mode uses the standard library; capture mode also requires psutil.
Run from prescriptions-service directory.
"""
import argparse, csv, json, math, subprocess, sys, threading, time
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
    selector='job="web",method="GET",path="/prescriptions/{id}"'
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

def meta_paths(series_template):
    return [OUT/f'{series_template.format(n=n)}_meta.json' for n in range(1,5)]

def fetch(args):
    paths=meta_paths(args.series_template)
    missing=[str(p) for p in paths if not p.exists()]
    if missing:raise SystemExit('Missing metadata: '+', '.join(missing))
    results=[]; history_ok=True
    for n,mp in enumerate(paths,start=1):
        meta=json.loads(mp.read_text(encoding='utf-8-sig'))
        start=float(meta['unix_start'])+float(meta.get('warmup',10));duration=float(meta.get('step_seconds',20))
        steps=int(meta.get('steps',0))
        if not steps:
            steps_csv=OUT/f'{args.series_template.format(n=n)}_steps.csv'
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
            r['m4']=round(min(1.,max(0.,v/r['samples_est'])),6) if v is not None else None
        else:r['m4']=None
    fields=['n','step','target_rps','unix_time','p99_server_ms','bucket_low_ms','bucket_high_ms','max_error_ms','samples_est','m4']
    OUT.mkdir(parents=True,exist_ok=True)
    csv_path=OUT/f'{args.output_prefix}.csv'
    json_path=OUT/f'{args.output_prefix}.json'
    with csv_path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({key:r[key] for key in fields} for r in results)
    json_path.write_text(json.dumps({'threshold_seconds':threshold,'data':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Historical windows checked: {len(results)}; successful: {sum(r["p99_server_ms"] is not None for r in results)}')
    print('M4 threshold:',f'{threshold*1000:g} ms' if threshold else 'NOT DETERMINABLE')
    print('Saved:',csv_path,'and',json_path)
    if not history_ok:print('WARNING: missing historical histogram data in Prometheus for some intervals. No zeroes invented.')

def capture(args):
    try:
        import psutil
    except ImportError:
        raise SystemExit('capture-web4 requires psutil: python -m pip install psutil')
    OUT.mkdir(parents=True,exist_ok=True)
    prefix=args.prefix
    filename=OUT/f'{prefix}_docker_stats.csv'
    generator_filename=OUT/f'{prefix}_generator_resources.csv'
    result_path=OUT/prefix
    command=[sys.executable,'step_load_test.py','--mode','web','--ids','results/ids.json','--output',str(result_path),'--max-rps','2600']
    fields=['sample_unix','name','cpu_percent','mem_usage','mem_percent','net_io','block_io','pids']
    generator_fields=['sample_unix','cpu_percent_one_core','ram_mb']
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
    resource_thread=None
    try:
        with (OUT/f'{prefix}_console.txt').open('w',encoding='utf-8') as log:
            proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            process=psutil.Process(proc.pid)
            process.cpu_percent(None)
            def resource_sampler():
                with generator_filename.open('w',newline='',encoding='utf-8') as output:
                    w=csv.DictWriter(output,fieldnames=generator_fields);w.writeheader()
                    while not stop.wait(1):
                        try:
                            w.writerow({'sample_unix':f'{time.time():.3f}',
                                        'cpu_percent_one_core':f'{process.cpu_percent(None):.3f}',
                                        'ram_mb':f'{process.memory_info().rss/1024/1024:.3f}'})
                            output.flush()
                        except (psutil.NoSuchProcess,psutil.AccessDenied):
                            break
            resource_thread=threading.Thread(target=resource_sampler,daemon=True);resource_thread.start()
            for line in proc.stdout:
                print(line,end='');log.write(line);log.flush()
            code=proc.wait()
    finally:
        stop.set();thread.join(timeout=12)
        if resource_thread:resource_thread.join(timeout=12)
    print('Saved',filename,generator_filename,'and',f'{prefix}_* (CSV / JSON)')
    meta_path=OUT/f'{prefix}_meta.json'
    if meta_path.exists():
        meta=json.loads(meta_path.read_text(encoding='utf-8-sig'))
        peak=meta.get('X_step',{}).get('step')
        if peak:
            start=float(meta['unix_start'])+float(meta.get('warmup',10))+(int(peak)-1)*float(meta.get('step_seconds',20))+5
            end=float(meta['unix_start'])+float(meta.get('warmup',10))+int(peak)*float(meta.get('step_seconds',20))
            with filename.open(encoding='utf-8') as f:
                matched=[r for r in csv.DictReader(f) if start<=float(r['sample_unix'])<end]
            peak_stats_path=OUT/f'{prefix}_peak_stats.csv'
            with peak_stats_path.open('w',newline='',encoding='utf-8') as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(matched)
            print(f'X(4) step={peak}, last 15s: {datetime.fromtimestamp(start,timezone.utc).isoformat()} to {datetime.fromtimestamp(end,timezone.utc).isoformat()}')
            print(f'Synchronized Docker resource samples: {len(matched)}; saved {peak_stats_path.name}')
            if not matched:print('WARNING: no matching samples, check Docker Desktop response times.')
            with generator_filename.open(encoding='utf-8') as f:
                generator_rows=[r for r in csv.DictReader(f) if start<=float(r['sample_unix'])<end]
            generator_peak_path=OUT/f'{prefix}_peak_generator_resources.csv'
            with generator_peak_path.open('w',newline='',encoding='utf-8') as f:
                w=csv.DictWriter(f,fieldnames=generator_fields);w.writeheader();w.writerows(generator_rows)
            if generator_rows:
                cpu=sum(float(r['cpu_percent_one_core']) for r in generator_rows)/len(generator_rows)
                ram=sum(float(r['ram_mb']) for r in generator_rows)/len(generator_rows)
                print(f'Generator X(4) samples: {len(generator_rows)}; CPU mean={cpu:.3f}% of one core; RAM mean={ram:.3f} MiB')
            else:print('WARNING: no matching generator resource samples.')
    if code:raise SystemExit(code)

if __name__=='__main__':
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='action',required=True)
    q=sub.add_parser('historical');q.add_argument('--prometheus',default='http://localhost:9090')
    q.add_argument('--series-template',default='web{n}_final')
    q.add_argument('--output-prefix',default='server_metrics')
    c=sub.add_parser('capture-web4');c.add_argument('--prefix',default='web4_sync')
    a=p.parse_args()
    if a.action=='historical':fetch(a)
    else:capture(a)

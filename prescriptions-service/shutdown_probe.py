"""Test SIGTERM under steady HTTP load with docker stop at t=10s.
Requires: Docker CLI, aiohttp, running two web replicas.
"""
import argparse
import asyncio
import csv
import json
import subprocess
import time
from pathlib import Path
import aiohttp

async def main(args):
    results=[]
    ids=json.loads(Path(args.ids).read_text(encoding='utf-8'))
    if not ids:
        raise SystemExit('IDs file is empty')
    stop_result={}
    started=time.monotonic()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5),connector=aiohttp.TCPConnector(limit=0)) as session:
        async def one(planned,index):
            wait=max(0,planned-time.monotonic())
            if wait:await asyncio.sleep(wait)
            actual=time.monotonic()-started
            try:
                endpoint=f'{args.url.rstrip("/")}/prescriptions/{ids[index % len(ids)]}'
                async with session.get(endpoint) as res:
                    await res.read(); status=res.status; err=''
            except Exception as e:
                status=0;err=type(e).__name__
            results.append((round(actual,3),status,err))
        async def stopper():
            await asyncio.sleep(10)
            print('Stopping:',args.container,flush=True)
            p=await asyncio.create_subprocess_exec('docker','stop',args.container,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            output,error=await p.communicate()
            stop_result['docker_stop_returncode']=p.returncode
            inspect=await asyncio.create_subprocess_exec(
                'docker','inspect','--format','{{.State.ExitCode}}',args.container,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            inspect_out,inspect_err=await inspect.communicate()
            try:stop_result['container_exit_code']=int(inspect_out.decode().strip())
            except ValueError:stop_result['container_exit_code']=None
            print('docker stop:',p.returncode,output.decode(errors='replace'),error.decode(errors='replace'),flush=True)
        stopping=asyncio.create_task(stopper())
        tasks=[]
        for i in range(args.duration*args.rps):
            when=started+i/args.rps
            delay=when-time.monotonic()
            if delay>0:await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(when,i)))
        await asyncio.gather(*tasks)
        await stopping
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(['elapsed_send_second','status','error']);w.writerows(results)
    around=[x for x in results if 5<=x[0]<15]
    bad=[x for x in around if not 200<=x[1]<300]
    print(f'Total={len(results)}; ten-second window [5,15): requests={len(around)}, non2xx_or_errors={len(bad)}',flush=True)
    if bad:print('First errors:',bad[:10])
    meta={
        'endpoint':'/prescriptions/{id}',
        'ids_file':args.ids,
        'rps':args.rps,
        'duration_seconds':args.duration,
        'control_window':'[5,15)',
        'control_window_requests':len(around),
        'control_window_non2xx_or_errors':len(bad),
        **stop_result,
    }
    out.with_name(out.stem+'_meta.json').write_text(
        json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Saved:',out)

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--container',required=True)
    p.add_argument('--url',default='http://localhost:8080')
    p.add_argument('--ids',default='results/ids.json')
    p.add_argument('--rps',type=int,default=50)
    p.add_argument('--duration',type=int,default=25)
    p.add_argument('--output',default='results/lab2/shutdown.csv')
    asyncio.run(main(p.parse_args()))

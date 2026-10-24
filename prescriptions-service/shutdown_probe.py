"""Test SIGTERM under steady HTTP load with docker stop at t=10s.
Requires: Docker CLI, aiohttp, running two web replicas.
"""
import argparse
import asyncio
import csv
import subprocess
import time
from pathlib import Path
import aiohttp

async def main(args):
    results=[]
    started=time.monotonic()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5),connector=aiohttp.TCPConnector(limit=0)) as session:
        async def one(planned):
            wait=max(0,planned-time.monotonic())
            if wait:await asyncio.sleep(wait)
            actual=time.monotonic()-started
            try:
                async with session.get('http://localhost:8080/livez') as res:
                    await res.read(); status=res.status; err=''
            except Exception as e:
                status=0;err=type(e).__name__
            results.append((round(actual,3),status,err))
        async def stopper():
            await asyncio.sleep(10)
            print('Stopping:',args.container,flush=True)
            p=await asyncio.create_subprocess_exec('docker','stop',args.container,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            output,error=await p.communicate()
            print('docker stop:',p.returncode,output.decode(errors='replace'),error.decode(errors='replace'),flush=True)
        stopping=asyncio.create_task(stopper())
        tasks=[]
        for i in range(args.duration*args.rps):
            when=started+i/args.rps
            delay=when-time.monotonic()
            if delay>0:await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(when)))
        await asyncio.gather(*tasks)
        await stopping
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(['elapsed_send_second','status','error']);w.writerows(results)
    around=[x for x in results if 5<=x[0]<15]
    bad=[x for x in around if not 200<=x[1]<300]
    print(f'Total={len(results)}; ten-second window [5,15): requests={len(around)}, non2xx_or_errors={len(bad)}',flush=True)
    if bad:print('First errors:',bad[:10])
    print('Saved:',out)

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--container',required=True)
    p.add_argument('--rps',type=int,default=50)
    p.add_argument('--duration',type=int,default=25)
    p.add_argument('--output',default='results/lab2/shutdown.csv')
    asyncio.run(main(p.parse_args()))

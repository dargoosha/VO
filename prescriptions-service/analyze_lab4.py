"""Аналіз фактичних результатів лабораторної роботи 4 і побудова графіків."""
from __future__ import annotations
import csv, json, math, statistics
from pathlib import Path
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parent; RES=ROOT/'results'/'lab4'; IMG=ROOT/'images'/'lab4'; IMG.mkdir(parents=True,exist_ok=True)
def j(name): return json.loads((RES/name).read_text(encoding='utf-8-sig'))
def rows(name):
    with (RES/name).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def fit_usl(cap):
    lam=cap[1]; obs=[]
    for n,x in sorted(cap.items()):
        if n>1:
            c=x/lam;obs.append((n-1,n*(n-1),n/c-1))
    suu=sum(u*u for u,v,y in obs);suv=sum(u*v for u,v,y in obs);svv=sum(v*v for u,v,y in obs);suy=sum(u*y for u,v,y in obs);svy=sum(v*y for u,v,y in obs);d=suu*svv-suv*suv
    sr=(svv*suy-suv*svy)/d;kr=(suu*svy-suv*suy)/d
    if sr<0: s=0.;k=max(0.,svy/svv)
    elif kr<0: k=0.;s=max(0.,suy/suu)
    else:s,k=sr,kr
    star=math.sqrt((1-s)/k) if k>0 and s<1 else None
    return {'lambda':lam,'sigma':s,'kappa':k,'w_star':star,'sigma_unconstrained':sr,'kappa_unconstrained':kr}
def active_stats(w):
    vals=[int(r['completed']) for r in rows(f'step6_w{w}_seconds.csv') if int(r['completed'])>0]
    return statistics.pstdev(vals) if len(vals)>1 else 0.
step6={int(r['workers']):r for r in rows('step6_summary.csv')}
# The methodology defines X(W) as the number completed in the final 30 seconds.
capacity={w:float(step6[w]['X_30s']) for w in (1,2,3)}
# 400 / active_seconds is retained only as a supplementary diagnostic.
active_capacity={w:400/float(step6[w]['active_seconds']) for w in (1,2,3)}
model=fit_usl(capacity);model['capacity_x30']=capacity;model['relative_capacity']={w:x/capacity[1] for w,x in capacity.items()};model['active_capacity_supplementary']=active_capacity
d=j('step2_duration_prometheus.json')['data']['result'];dur={x['metric']['step']:float(x['value'][1]) for x in d};model['t_io_seconds']=dur['fetch']+dur['store'];model['t_cpu_seconds']=dur['process'];model['x1_theoretical']=1/(model['t_io_seconds']+model['t_cpu_seconds']+0.05)
b10=j('step10_batch10_w3_summary.json');model['batch10_x3_30s']=b10['X_30s'];model['batch10_x3_active_supplementary']=400/b10['active_seconds'];model['batch10_claim_p99_seconds']=b10['claim_p99_seconds'];model['batch1_claim_p99_seconds']=j('step6_w3_summary.json')['claim_p99_seconds']
theory=[]
for name in ('step9_baseline','step9_poison'):
    s=j(name+'_summary.json');n=s['created']-s['poison_count']
    for metric,observed,p in (('attempts_gt_1',s['attempts_gt_1_nonpoison'],.15),('failed',s['failed_nonpoison'],.15**3)):
        fraction=observed/n;se=math.sqrt(p*(1-p)/n)
        theory.append({'series':name,'metric':metric,'n':n,'observed':observed,'observed_fraction':fraction,'expected_fraction':p,'difference_percentage_points':100*(fraction-p),'expected_95_low':max(0,p-1.96*se),'expected_95_high':min(1,p+1.96*se)})
model['step9_theory_comparison']=theory
(RES/'analysis_summary.json').write_text(json.dumps(model,ensure_ascii=False,indent=2),encoding='utf-8')
with (RES/'capacity_summary.csv').open('w',encoding='utf-8',newline='') as f:
    wr=csv.writer(f);wr.writerow(['workers','X_30s','std_30s','C_30s','X_active_supplementary','std_active_supplementary']);[wr.writerow([w,capacity[w],float(step6[w]['std_30s']),capacity[w]/capacity[1],active_capacity[w],active_stats(w)]) for w in (1,2,3)]
with (RES/'step9_theory_comparison.csv').open('w',encoding='utf-8',newline='') as f:
    wr=csv.DictWriter(f,fieldnames=list(theory[0]));wr.writeheader();wr.writerows(theory)

plt.style.use('seaborn-v0_8-whitegrid')
fig,ax=plt.subplots(figsize=(8,5));ws=[1,2,3];ys=[capacity[w] for w in ws];err=[float(step6[w]['std_30s']) for w in ws];ax.errorbar(ws,ys,yerr=err,fmt='o',capsize=5,label='X за останні 30 с')
grid=[1+i/50 for i in range(101)];curve=[model['lambda']*w/(1+model['sigma']*(w-1)+model['kappa']*w*(w-1)) for w in grid];ax.plot(grid,curve,label='USL за X_30');ax.plot(ws,[active_capacity[w] for w in ws],'--s',label='400 / active_seconds (додатково)');ax.set(xlabel='Кількість воркерів W',ylabel='Задач/с',title='Пропускна здатність пулу');ax.set_xticks(ws);ax.legend();fig.tight_layout();fig.savefig(IMG/'01_capacity_usl.png',dpi=170);plt.close(fig)
for name,title,out in [('step7_steady','Сталий режим 0,7 X_30(3)','02_little_queue.png'),('step7_overload','Профіль 1,3 X_30(3)','03_overload_queue.png')]:
    rr=rows(name+'_queue.csv');t0=int(rr[0]['unix_second']);x=[int(r['unix_second'])-t0 for r in rr];fig,ax=plt.subplots(figsize=(9,4.8));ax.plot(x,[int(r['queued']) for r in rr],label='queued');ax.plot(x,[int(r['running']) for r in rr],label='running');ax.set(xlabel='Час, с',ylabel='Кількість задач',title=title);ax.legend();fig.tight_layout();fig.savefig(IMG/out,dpi=170);plt.close(fig)
fig,axs=plt.subplots(2,1,figsize=(10,8),sharex=False)
for ax,name,label in [(axs[0],'step9_baseline','baseline'),(axs[1],'step9_poison','Ф3: 10 seed=0')]:
    rr=rows(name+'_timeline.csv');t=[int(r['unix_second']) for r in rr];t0=t[0];x=[v-t0 for v in t];done=[int(r['done']) for r in rr];rate=[0.]+[(done[i]-done[i-1])/(t[i]-t[i-1]) for i in range(1,len(rr))]
    q=ax.plot(x,[int(r['queued']) for r in rr],label='queued',color='tab:blue');run=ax.plot(x,[int(r['running']) for r in rr],label='running',color='tab:orange');ax2=ax.twinx();completed=ax2.plot(x,rate,label='completed/с',color='tab:green');restarts=ax2.plot(x,[float(r['restarts']) for r in rr],':',label='restarts',color='tab:red',alpha=.75)
    if name=='step9_poison':
        poison=[r for r in rows('step9_poison_created.csv') if r['poison']=='1']
        if poison:
            px=float(poison[0]['started_unix'])-t0;mark=ax.axvline(px,color='black',ls='--',label='момент Ф3')
    ax.set_title(label);ax.set_ylabel('Кількість задач');ax2.set_ylabel('Завершено за секунду / рестарти');lines=q+run+completed+restarts+([mark] if name=='step9_poison' and poison else []);ax.legend(lines,[v.get_label() for v in lines],loc='upper left',ncol=2)
axs[-1].set_xlabel('Час від початку спостереження, с');fig.suptitle('FAIL_RATE=0,15: стан черги та виконання');fig.tight_layout();fig.savefig(IMG/'04_fail_rate.png',dpi=170);plt.close(fig)
fig,ax=plt.subplots(figsize=(8,4.8));labels=['batch 1','batch 10'];pos=[0,1];width=.36;ax.bar([v-width/2 for v in pos],[capacity[3],b10['X_30s']],width,label='X за останні 30 с');ax.bar([v+width/2 for v in pos],[active_capacity[3],model['batch10_x3_active_supplementary']],width,label='400 / active_seconds (додатково)');ax.set_xticks(pos,labels);ax.set(ylabel='Задач/с',title='Д4: пакетне захоплення');ax.legend();fig.tight_layout();fig.savefig(IMG/'05_batch_capacity.png',dpi=170);plt.close(fig)
rr=rows('step9_poison_attempts.csv');fig,ax=plt.subplots(figsize=(7,4.5));ax.bar([r['attempts'] for r in rr],[int(r['count']) for r in rr]);ax.set(xlabel='attempts',ylabel='Кількість задач',title='Ф3: розподіл задач за attempts після повного drain');fig.tight_layout();fig.savefig(IMG/'06_poison_attempts.png',dpi=170);plt.close(fig)
print(json.dumps(model,ensure_ascii=False,indent=2))

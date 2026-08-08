"""Deterministic Study 2 analysis.

Pre-specified headline: test whether decomposition wins under a recall-weighted
service-selection loss function.  This was specified before subgroup inspection:
macro answers expected one-run performance; micro pools individual service
decisions and therefore excludes the three multi-agent budget deaths that
produced no decisions.  Outputs are confined to results/analysis_study2.
"""
from __future__ import annotations
import csv, glob, json, math
from collections import Counter, defaultdict
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np
import yaml
from scipy.stats import fisher_exact, t

ROOT=Path(__file__).resolve().parent.parent; RUNS=ROOT/'results/runs_study2'; OUT=ROOT/'results/analysis_study2'
SERVICES=('postgres','nginx','redis','rabbitmq')
def mean(x): return float(np.mean(x)) if x else None
def rows(): return [json.loads(Path(p).read_text()) for p in glob.glob(str(RUNS/'**/*.json'),recursive=True)]
def write(name, rows_):
    if not rows_: return
    with (OUT/name).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows_[0]));w.writeheader();w.writerows(rows_)
def fbeta(p,r,b): return 0 if not p+r else (1+b*b)*p*r/(b*b*p+r)
def role(sid): return yaml.safe_load((ROOT/'benchmark/scenarios_study2'/f'{sid}.yaml').read_text())['derivation']['coverage'].split('-')[0]
def paired(rs,key):
    vals=[]
    for sid in sorted({r['scenario_id'] for r in rs}):
        get=lambda r: r['tokens_used'] if key=='tokens_used' else r['scores'].get(key,0)
        a=[get(r) for r in rs if r['scenario_id']==sid and r['architecture']=='single']; b=[get(r) for r in rs if r['scenario_id']==sid and r['architecture']=='multi']; vals.append(mean(b)-mean(a))
    d=np.array(vals); se=np.std(d,ddof=1)/math.sqrt(8); crit=t.ppf(.975,7)
    return dict(metric=key,mean_delta=float(d.mean()),sd=float(d.std(ddof=1)),ci_low=float(d.mean()-crit*se),ci_high=float(d.mean()+crit*se),t=float(d.mean()/se if se else 0),df=7)
def main():
    OUT.mkdir(parents=True,exist_ok=True); rs=rows(); assert len(rs)==48
    assert {r['git_commit'] for r in rs}=={'98dca8bdcaad1ca91d0d36a549391f101324bbac'}
    assert {r['model'] for r in rs}=={'gpt-4.1-nano-2025-04-14'}
    cells=Counter((r['scenario_id'],r['architecture']) for r in rs); assert set(cells.values())=={3}
    per=[]; conf=[]; summary={"warning":"All runs record git_dirty=true; untracked AGENTS.md was present. No tracked source differed from 98dca8b.","n":48}
    micro={}
    for arm in ('single','multi'):
        x=[r for r in rs if r['architecture']==arm]; fin=[r for r in x if r['termination_reason']=='finalised'];
        macro={k:mean([r['scores'].get(k,0) for r in x]) for k in ('selection_exact_match','selection_precision','selection_recall','selection_f1')}
        c=Counter(d['verdict'] for r in x for d in r['scores'].get('selection_detail',[])); tp,fp,fn=c['true_positive'],c['false_positive'],c['false_negative']; p=tp/(tp+fp); rec=tp/(tp+fn)
        micro[arm]=dict(TP=tp,FP=fp,FN=fn,precision=p,recall=rec,F1=fbeta(p,rec,1),F05=fbeta(p,rec,.5),F2=fbeta(p,rec,2),zero_decision_runs=len(x)-len({r['run_id'] for r in x if r['scores'].get('selection_detail')}))
        summary[arm]=dict(n=len(x),completion=len(fin)/len(x),macro=macro,micro=micro[arm],mean_tokens=mean([r['tokens_used'] for r in x]),mean_wall_clock_s=mean([r['wall_clock_s'] for r in x]),correctness_conditional=mean([r['scores']['correctness'] for r in fin if 'correctness' in r['scores']]),parameters_checked_mean=mean([r['scores']['parameters_checked'] for r in fin if 'parameters_checked' in r['scores']]))
        for sid in sorted({r['scenario_id'] for r in x}):
            z=[r for r in x if r['scenario_id']==sid]; f=[r for r in z if r['termination_reason']=='finalised']; per.append(dict(scenario_id=sid,role=role(sid),architecture=arm,n=len(z),completion=mean([r['termination_reason']=='finalised' for r in z]),exact=mean([r['scores'].get('selection_exact_match',0) for r in z]),precision=mean([r['scores'].get('selection_precision',0) for r in z]),recall=mean([r['scores'].get('selection_recall',0) for r in z]),f1=mean([r['scores'].get('selection_f1',0) for r in z]),correctness=mean([r['scores']['correctness'] for r in f if 'correctness' in r['scores']]),parameters_checked=mean([r['scores']['parameters_checked'] for r in f if 'parameters_checked' in r['scores']]),cis_actionable=mean([r['scores']['cis_pass_rate_actionable'] for r in f if 'cis_pass_rate_actionable' in r['scores']]),tokens=mean([r['tokens_used'] for r in z]),wall_clock_s=mean([r['wall_clock_s'] for r in z])))
        for ro in ('minimal','distractor','inferred','straightforward'):
            for svc in SERVICES:
                cc=Counter(d['verdict'] for r in x if role(r['scenario_id'])==ro for d in r['scores'].get('selection_detail',[]) if d['service']==svc); conf.append(dict(architecture=arm,role=ro,service=svc,TP=cc['true_positive'],FP=cc['false_positive'],FN=cc['false_negative'],TN=cc['true_negative']))
    # paired tests and crossover
    summary['paired']=[paired(rs,k) for k in ('selection_f1','selection_precision','selection_recall','tokens_used')]
    summary['fisher']=dict(exact_p=fisher_exact([[5,19],[7,17]]).pvalue,completion_p=fisher_exact([[24,0],[21,3]]).pvalue,token_ratio=summary['multi']['mean_tokens']/summary['single']['mean_tokens'])
    grid=np.linspace(.1,5,491); diff=np.array([fbeta(micro['multi']['precision'],micro['multi']['recall'],b)-fbeta(micro['single']['precision'],micro['single']['recall'],b) for b in grid]); cross=float(grid[np.argmin(abs(diff))]); summary['fbeta_crossover_beta']=cross
    write('table1_per_scenario_arm.csv',per); write('table2_confusion_by_role.csv',conf); write('table3_paired_tests.csv',summary['paired'])
    write('table4_failure_taxonomy.csv',[dict(scenario_id=r['scenario_id'],architecture=r['architecture'],termination_reason=r['termination_reason'],tokens_used=r['tokens_used'],per_agent_tokens=json.dumps(r.get('per_agent_tokens',{})),last_activity=(r.get('history') or [{}])[-1].get('summary',''),no_spec=r.get('final_spec') is None) for r in rs if r['termination_reason']!='finalised'])
    study1=list(csv.DictReader((ROOT/'results/analysis/table0_pooled_descriptives.csv').open()))
    cross=[dict(study='Study 1',architecture=x[''],completion=x['n_finalised']+'/'+x['n'],mean_tokens=x['tok_mean'],token_ratio='1.88x',correctness_note='NOT COMPARABLE') for x in study1]+[dict(study='Study 2',architecture=a,completion=f"{sum(r['termination_reason']=='finalised' for r in rs if r['architecture']==a)}/24",mean_tokens=summary[a]['mean_tokens'],token_ratio=f"{summary['fisher']['token_ratio']:.2f}x",correctness_note='NOT COMPARABLE') for a in ('single','multi')]
    write('table5_cross_study.csv',cross)
    with (OUT/'summary.json').open('w') as f: json.dump(summary,f,indent=2)
    # figures
    plt.style.use('seaborn-v0_8-whitegrid'); colors={'single':'#4C72B0','multi':'#DD8452'}
    fig,ax=plt.subplots(figsize=(8,4));
    for arm in ('single','multi'): ax.plot(grid,[fbeta(micro[arm]['precision'],micro[arm]['recall'],b) for b in grid],label=arm,color=colors[arm],lw=2)
    ax.axvline(summary['fbeta_crossover_beta'],color='black',ls='--');ax.set(xlabel='beta',ylabel='micro F-beta',title='Recall-weighted loss crossover');ax.legend();fig.tight_layout();fig.savefig(OUT/'fig2_fbeta_crossover.png',dpi=200);plt.close(fig)
    fig,axs=plt.subplots(1,2,figsize=(10,4),sharey=True)
    for ax,arm in zip(axs,('single','multi')):
        q=Counter(d['verdict'] for r in rs if r['architecture']==arm for d in r['scores'].get('selection_detail',[])); bottom=0
        for v,c in zip(('true_positive','false_positive','false_negative','true_negative'),('#55A868','#C44E52','#8172B3','#999999')): ax.bar([arm],[q[v]],bottom=bottom,label=v if arm=='single' else None,color=c);bottom+=q[v]
        ax.set_title(arm)
    axs[0].legend(fontsize=8);fig.tight_layout();fig.savefig(OUT/'fig1_selection_confusion.png',dpi=200);plt.close(fig)
    fig,ax=plt.subplots(figsize=(8,4)); roles=['minimal','distractor','inferred','straightforward'];
    for i,a in enumerate(('single','multi')): ax.bar(np.arange(4)+i*.35,[mean([r['scores'].get('selection_f1',0) for r in rs if r['architecture']==a and role(r['scenario_id'])==ro]) for ro in roles],.35,label=a,color=colors[a])
    ax.set_xticks(np.arange(4)+.175,roles);ax.set_ylabel('macro selection F1');ax.legend();fig.tight_layout();fig.savefig(OUT/'fig3_f1_by_role.png',dpi=200);plt.close(fig)
    fig,ax=plt.subplots(figsize=(6,4)); ax.bar(['Study 1','Study 2'],[1.88,summary['fisher']['token_ratio']],color='#4C72B0');ax.set_ylabel('multi / single token ratio');fig.tight_layout();fig.savefig(OUT/'fig4_token_ratio.png',dpi=200);plt.close(fig)
    print(json.dumps(summary,indent=2))
if __name__=='__main__': main()

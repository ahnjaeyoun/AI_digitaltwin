import csv, random, math, sys
random.seed(42)
INP=r'C:\pipes_press\dataset\realtime_input_1000cycles_normal_relief_mixed.csv'
rows=list(csv.DictReader(open(INP,encoding='utf-8-sig')))
from collections import defaultdict, OrderedDict
cyc=OrderedDict(); ctype=defaultdict(set)
for r in rows: ctype[r['cycle_id']].add(r['cycle_type'])
for r in rows:
    if ctype[r['cycle_id']]!={'normal'}: continue
    cyc.setdefault(r['cycle_id'],[]).append(r)
templates=list(cyc.values())
print('normal templates:',len(templates),file=sys.stderr)
def gv(r,k):
    try: return float(r[k])
    except: return 0.0
# 학습용 준균형 구성(2026-07-22 복원 — 90:10 검증셋 역할은 gen_trajectories.py twin 세트가 대체).
# 이 설정+seed 42가 dataset/scenarios.csv(145만 행)의 원천이다.
COUNTS={'normal':30000,'pump_wear':7000,'suction_clog':7000,'line_clog':7000,'valve_stuck':7000,
        'valve_int_leak':7000,'seal_leak':7000,'ext_leak':7000,'relief_early':7000,'relief_stuck':7000,'overheat':7000}
SEV=['initial','moderate','severe']
MECH={
 'suction_clog':  {'mult':[10,22,40]},
 'line_clog':     {'mult':[7,16,30],'rough':[15,45,90]},
 'valve_stuck':   {'frac':[0.65,0.42,0.15]},
 'overheat':      {'add':[14,24,34]},
 'pump_wear':     {'scale':[0.97,0.94,0.91]},
 'seal_leak':     {'K':[3e-12,6e-12,1.1e-11]},
 'ext_leak':      {'K':[3e-12,6e-12,1.1e-11]},
 'valve_int_leak':{'K':[3e-12,6e-12,1.1e-11]},
 'relief_early':  {'cap':[225,208,190]},
 'relief_stuck':  {'tadd':[8,14,20],'rmul':[1.03,1.05,1.07]},
}
EXTNODES=['N_CYL_CAP','N_VALVE_A','N_VALVE_P']
VALVE_BY_MODE={'downstroke':'V_DIR_PA','upstroke':'V_DIR_PB'}
FIELDS=['row_id','cycle_id','cycle_second','mode','target_pressure','target_chamber','temperature_c','pump_rpm',
        'cmd_pa','cmd_pb','cmd_at','cmd_bt','v_pa','v_pb','v_at','v_bt','v_relief_open','v_relief_set',
        'loss_cell','loss_k_mult','rough_mult','pump_head_scale','leak_from','leak_to','leak_K','load_cap',
        'fault_class','severity_level','fault_severity','onset_type','is_anomaly','gt_fault_location','gt_leak_mode']
import os
OUTDIR=os.path.join(os.path.dirname(os.path.abspath(__file__)))
out=open(os.path.join(OUTDIR,'scenarios.csv'),'w',newline=''); w=csv.DictWriter(out,fieldnames=FIELDS); w.writeheader()
gcid=0
for cls,n in COUNTS.items():
    for _ in range(n):
        gcid+=1; tmpl=random.choice(templates); nrow=len(tmpl)
        tj=random.uniform(0.97,1.03); tempj=random.gauss(0,1.0); rpmj=random.uniform(0.98,1.02)
        is_anom=0 if cls=='normal' else 1
        sev_i=random.randrange(3) if is_anom else -1
        sev_level=SEV[sev_i] if is_anom else 'none'
        onset=random.choice(['abrupt','gradual']) if is_anom else 'normal'
        loc='';leak_mode=''
        for si,r in enumerate(tmpl):
            mode=r['System.active_mode']
            fp=(0.25+0.75*si/max(1,nrow-1)) if onset=='gradual' else 1.0
            tgt=gv(r,'Press.target_pressure_bar_g')*tj; temp=gv(r,'Fluid.temperature_c')+tempj; rpm=gv(r,'Cell.PUMP_01.rpm')*rpmj
            cmd_pa=gv(r,'Cell.V_DIR_PA.opening_percent'); cmd_pb=gv(r,'Cell.V_DIR_PB.opening_percent')
            cmd_at=gv(r,'Cell.V_DIR_AT.opening_percent'); cmd_bt=gv(r,'Cell.V_DIR_BT.opening_percent')
            v_pa,v_pb,v_at,v_bt=cmd_pa,cmd_pb,cmd_at,cmd_bt; v_rset=250.0; v_ropen=0.0
            loss_cell='';loss_mult=1.0;rough_mult=1.0;phs=1.0;lf='';lt='';lk=0.0;lcap=1e9
            if is_anom:
                m=MECH.get(cls,{})
                if cls=='suction_clog': loss_cell='C_SUCTION'; loss_mult=1+(m['mult'][sev_i]-1)*fp; loc='C_SUCTION'
                elif cls=='line_clog': loss_cell='C_PRESSURE'; loss_mult=1+(m['mult'][sev_i]-1)*fp; rough_mult=1+(m['rough'][sev_i]-1)*fp; loc='C_PRESSURE'
                elif cls=='valve_stuck':
                    av=VALVE_BY_MODE.get(mode); loc=av or 'V_DIR_PA'; fr=1-(1-m['frac'][sev_i])*fp
                    if mode=='downstroke': v_pa=cmd_pa*fr
                    elif mode=='upstroke': v_pb=cmd_pb*fr
                elif cls=='overheat': temp=temp+m['add'][sev_i]*fp; loc='Fluid'
                elif cls=='pump_wear': phs=1-(1-m['scale'][sev_i])*fp; loc='PUMP_01'
                elif cls=='seal_leak': lf='N_CYL_CAP';lt='N_CYL_ROD';lk=m['K'][sev_i]*fp; loc='N_CYL_CAP-ROD'; leak_mode='internal'
                elif cls=='ext_leak':
                    if si==0: loc=random.choice(EXTNODES)
                    lf=loc;lt='';lk=m['K'][sev_i]*fp; leak_mode='external'
                elif cls=='valve_int_leak': lf='N_VALVE_P';lt='N_VALVE_T';lk=m['K'][sev_i]*fp; loc=VALVE_BY_MODE.get(mode,'V_DIR_PA'); leak_mode='valve_internal'
                elif cls=='relief_early': cap=m['cap'][sev_i]; cap=tgt-(tgt-cap)*fp; lcap=cap; v_rset=cap; loc='V_RELIEF'
                elif cls=='relief_stuck': tgt=tgt+m['tadd'][sev_i]*fp; rpm=rpm*(1+(m['rmul'][sev_i]-1)*fp); v_rset=999; loc='V_RELIEF'
            w.writerow({'row_id':f'{gcid}_{si}','cycle_id':gcid,'cycle_second':si,'mode':mode,
                'target_pressure':round(tgt,3),'target_chamber':r['Press.target_chamber'],'temperature_c':round(temp,3),
                'pump_rpm':int(rpm),'cmd_pa':round(cmd_pa,3),'cmd_pb':round(cmd_pb,3),'cmd_at':round(cmd_at,3),'cmd_bt':round(cmd_bt,3),
                'v_pa':round(v_pa,3),'v_pb':round(v_pb,3),'v_at':round(v_at,3),'v_bt':round(v_bt,3),
                'v_relief_open':v_ropen,'v_relief_set':round(v_rset,3),'loss_cell':loss_cell,'loss_k_mult':round(loss_mult,4),
                'rough_mult':round(rough_mult,4),'pump_head_scale':round(phs,4),'leak_from':lf,'leak_to':lt,'leak_K':lk,'load_cap':lcap,
                'fault_class':cls,'severity_level':sev_level,'fault_severity':round(fp if is_anom else 0,3),
                'onset_type':onset,'is_anomaly':is_anom,'gt_fault_location':loc,'gt_leak_mode':leak_mode})
out.close(); print('total cycles:',gcid,file=sys.stderr)

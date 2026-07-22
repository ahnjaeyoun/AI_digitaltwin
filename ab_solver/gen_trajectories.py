"""
예지보전 통합 데이터셋용 궤적(trajectory) 시나리오 생성기.
설계: docs/예지보전_통합데이터셋_설계.md (2026-07-22 확정)

gen_scenarios.py(독립 사이클)와 달리 한 궤적 = 한 설비의 시간순 사이클 묶음이며,
gradual 고장의 심각도가 "사이클 간"에 0→1로 램프된다(기존은 사이클 내부 초 단위 램프).

- 사이클 15초 고정: downstroke 6s / pressure_hold 3s / upstroke 6s. 실측 정상 템플릿에는
  hold 3초가 없으므로(1~2초뿐) (down 6, up 6)인 템플릿의 hold 행을 3행으로 반복 확장해 합성.
- 라벨 정책(확정): onset 이전 사이클은 fault_class='normal'. health_stage / onset_cycle(관측) /
  failure_cycle / cycles_to_failure는 여기서 만들지 않고, 솔버 실행 후
  scripts/trajectory_labels.py가 관리도(Western Electric) 판정으로 계산·트리밍한다.
- gradual: 건강 prefix(궤적의 10~40%) → onset부터 severe 목표 파라미터까지 선형/지수 램프.
  abrupt: prefix 후 목표 심각도(3단계 랜덤)로 계단 — severe면 그 사이클로 종료(즉시 고장),
  initial/moderate면 10~30사이클 유지 후 고장 미도달 종료(censored).
- gen.cpp는 헤더 이름으로 컬럼을 찾고 모르는 컬럼은 무시하므로, 궤적 메타 컬럼
  (trajectory_id, cycle_in_traj, gt_*)을 표준 32컬럼 뒤에 추가해도 안전하다.

출력: scenarios_traj_pilot.csv (gen_static.exe 입력, 파일럿 ~5만 행)
"""
import csv, math, os, random, sys

# ---------------- 세트별 프로필 (실행: python gen_trajectories.py [pilot|train|test|twin]) ----------------
# 파일럿: 캘리브레이션·검증 전용(소규모·짧은 램프). 본생성 3세트는 2026-07-22 확정 표준안:
#  train/test = 정상 30% : 고장 70%(궤적 수 기준), twin = 정상 95% : 고장 5%(사이클 수 기준, 배포 시뮬레이션용)
PROFILES = dict(
    pilot=dict(seed=100, n_normal=17, n_grad=2, n_abr=2, ramp_range=(30, 80),
               normal_len=(50, 100), out='scenarios_traj_pilot.csv'),
    train=dict(seed=101, n_normal=129, n_grad=15, n_abr=15, ramp_range=(50, 400),
               normal_len=(200, 500), out='scenarios_traj_train.csv'),
    test=dict(seed=102, n_normal=43, n_grad=5, n_abr=5, ramp_range=(50, 400),
              normal_len=(200, 500), out='scenarios_traj_test.csv'),
    twin=dict(seed=103, n_normal=190, n_grad=1, n_abr=1, ramp_range=(50, 400),
              normal_len=(200, 500), out='scenarios_traj_twin.csv'),
)
COMMON = dict(
    prefix_frac=(0.10, 0.40),   # gradual: 건강 prefix가 전체 궤적에서 차지하는 비율
    abrupt_prefix=(10, 30),     # abrupt: 건강 prefix 사이클 수
    abrupt_hold=(10, 30),       # abrupt initial/moderate: 고장 상태 유지 사이클 수
)
CFG = dict(COMMON, **PROFILES[sys.argv[1] if len(sys.argv) > 1 else 'pilot'])

INP = r'C:\pipes_press\dataset\realtime_input_1000cycles_normal_relief_mixed.csv'
CYCLE_SECONDS = 15  # 6 + 3 + 6 고정

FAULTS = ['pump_wear', 'suction_clog', 'line_clog', 'valve_stuck', 'valve_int_leak',
          'seal_leak', 'ext_leak', 'relief_early', 'relief_stuck', 'overheat']
SEV = ['initial', 'moderate', 'severe']
MECH = {  # gen_scenarios.py와 동일 캘리브레이션 (severe = index 2 가 램프의 s=1 목표)
    'suction_clog':  {'mult': [10, 22, 40]},
    'line_clog':     {'mult': [7, 16, 30], 'rough': [15, 45, 90]},
    'valve_stuck':   {'frac': [0.65, 0.42, 0.15]},
    'overheat':      {'add': [14, 24, 34]},
    'pump_wear':     {'scale': [0.97, 0.94, 0.91]},
    'seal_leak':     {'K': [3e-12, 6e-12, 1.1e-11]},
    'ext_leak':      {'K': [3e-12, 6e-12, 1.1e-11]},
    'valve_int_leak': {'K': [3e-12, 6e-12, 1.1e-11]},
    'relief_early':  {'cap': [225, 208, 190]},
    'relief_stuck':  {'tadd': [8, 14, 20], 'rmul': [1.03, 1.05, 1.07]},
}
EXTNODES = ['N_CYL_CAP', 'N_VALVE_A', 'N_VALVE_P']
VALVE_BY_MODE = {'downstroke': 'V_DIR_PA', 'upstroke': 'V_DIR_PB'}

FIELDS = ['row_id', 'cycle_id', 'cycle_second', 'mode', 'target_pressure', 'target_chamber',
          'temperature_c', 'pump_rpm', 'cmd_pa', 'cmd_pb', 'cmd_at', 'cmd_bt',
          'v_pa', 'v_pb', 'v_at', 'v_bt', 'v_relief_open', 'v_relief_set',
          'loss_cell', 'loss_k_mult', 'rough_mult', 'pump_head_scale',
          'leak_from', 'leak_to', 'leak_K', 'load_cap',
          'fault_class', 'severity_level', 'fault_severity', 'onset_type', 'is_anomaly',
          'gt_fault_location', 'gt_leak_mode',
          # ---- 궤적 메타 (gen.cpp는 무시, 라벨링/감사용) ----
          'trajectory_id', 'cycle_in_traj', 'gt_traj_class', 'gt_inject_onset',
          'gt_planned_failure', 'gt_curve']


def gv(r, k):
    try:
        return float(r[k])
    except Exception:
        return 0.0


def load_templates():
    """(down 6, up 6)인 정상 사이클을 골라 hold를 3행으로 확장, 15행 표준 사이클 목록 반환."""
    rows = list(csv.DictReader(open(INP, encoding='utf-8-sig')))
    from collections import defaultdict, OrderedDict
    ctype = defaultdict(set)
    for r in rows:
        ctype[r['cycle_id']].add(r['cycle_type'])
    cyc = OrderedDict()
    for r in rows:
        if ctype[r['cycle_id']] != {'normal'}:
            continue
        cyc.setdefault(r['cycle_id'], []).append(r)
    templates = []
    for rs in cyc.values():
        d = [r for r in rs if r['System.active_mode'] == 'downstroke']
        h = [r for r in rs if r['System.active_mode'] == 'pressure_hold']
        u = [r for r in rs if r['System.active_mode'] == 'upstroke']
        if len(d) == 6 and len(u) == 6 and len(h) >= 1:
            templates.append(d + (h * 3)[:3] + u)  # hold 반복 확장 → 15행
    return templates


def apply_fault(cls, idx, fp, mode, tgt, temp, rpm, cmd, loc_state):
    """gen_scenarios.py의 주입 공식과 동일하되 fp가 사이클 간 연속값.
    idx: MECH 심각도 인덱스(gradual은 2=severe 목표, abrupt는 계단 레벨).
    반환: (주입 필드 dict, tgt, temp, rpm, loc, leak_mode)"""
    m = MECH.get(cls, {})
    v_pa, v_pb = cmd['pa'], cmd['pb']
    f = dict(loss_cell='', loss_k_mult=1.0, rough_mult=1.0, pump_head_scale=1.0,
             leak_from='', leak_to='', leak_K=0.0, load_cap=1e9, v_relief_set=250.0)
    loc, leak_mode = '', ''
    if cls == 'suction_clog':
        f['loss_cell'] = 'C_SUCTION'; f['loss_k_mult'] = 1 + (m['mult'][idx] - 1) * fp; loc = 'C_SUCTION'
    elif cls == 'line_clog':
        f['loss_cell'] = 'C_PRESSURE'; f['loss_k_mult'] = 1 + (m['mult'][idx] - 1) * fp
        f['rough_mult'] = 1 + (m['rough'][idx] - 1) * fp; loc = 'C_PRESSURE'
    elif cls == 'valve_stuck':
        av = VALVE_BY_MODE.get(mode); loc = av or 'V_DIR_PA'
        fr = 1 - (1 - m['frac'][idx]) * fp
        if mode == 'downstroke':
            v_pa = cmd['pa'] * fr
        elif mode == 'upstroke':
            v_pb = cmd['pb'] * fr
    elif cls == 'overheat':
        temp = temp + m['add'][idx] * fp; loc = 'Fluid'
    elif cls == 'pump_wear':
        f['pump_head_scale'] = 1 - (1 - m['scale'][idx]) * fp; loc = 'PUMP_01'
    elif cls == 'seal_leak':
        f['leak_from'] = 'N_CYL_CAP'; f['leak_to'] = 'N_CYL_ROD'; f['leak_K'] = m['K'][idx] * fp
        loc = 'N_CYL_CAP-ROD'; leak_mode = 'internal'
    elif cls == 'ext_leak':
        f['leak_from'] = loc_state['ext_node']; f['leak_to'] = ''; f['leak_K'] = m['K'][idx] * fp
        loc = loc_state['ext_node']; leak_mode = 'external'
    elif cls == 'valve_int_leak':
        f['leak_from'] = 'N_VALVE_P'; f['leak_to'] = 'N_VALVE_T'; f['leak_K'] = m['K'][idx] * fp
        loc = VALVE_BY_MODE.get(mode, 'V_DIR_PA'); leak_mode = 'valve_internal'
    elif cls == 'relief_early':
        cap = m['cap'][idx]; cap = tgt - (tgt - cap) * fp
        f['load_cap'] = cap; f['v_relief_set'] = cap; loc = 'V_RELIEF'
    elif cls == 'relief_stuck':
        tgt = tgt + m['tadd'][idx] * fp; rpm = rpm * (1 + (m['rmul'][idx] - 1) * fp)
        f['v_relief_set'] = 999; loc = 'V_RELIEF'
    return f, tgt, temp, rpm, v_pa, v_pb, loc, leak_mode


def sev_curve(k, R, curve):
    """onset 후 k번째 사이클(1..R)의 심각도 s∈(0,1]."""
    s = k / R
    if curve == 'exp':
        return (math.exp(3 * s) - 1) / (math.exp(3) - 1)
    return s


def plan_trajectories(rng):
    """궤적 계획 목록 생성: (cls, onset_type, curve, prefix, n_fault_cycles, abrupt_level)"""
    plans = []
    for _ in range(CFG['n_normal']):
        T = rng.randint(*CFG['normal_len'])
        plans.append(dict(cls='normal', onset='', curve='', prefix=T, nf=0, lvl=-1))
    for cls in FAULTS:
        for _ in range(CFG['n_grad']):
            R = rng.randint(*CFG['ramp_range'])
            fr = rng.uniform(*CFG['prefix_frac'])
            H = max(3, round(R * fr / (1 - fr)))
            curve = rng.choice(['linear', 'exp'])
            plans.append(dict(cls=cls, onset='gradual', curve=curve, prefix=H, nf=R, lvl=2))
        for _ in range(CFG['n_abr']):
            H = rng.randint(*CFG['abrupt_prefix'])
            lvl = rng.randrange(3)
            nf = 1 if lvl == 2 else rng.randint(*CFG['abrupt_hold'])  # severe=즉시 고장 종료
            plans.append(dict(cls=cls, onset='abrupt', curve='', prefix=H, nf=nf, lvl=lvl))
    rng.shuffle(plans)
    return plans


def main():
    rng = random.Random(CFG['seed'])
    templates = load_templates()
    print(f'templates (6/3/6 synthesized): {len(templates)}', file=sys.stderr)

    outdir = os.path.dirname(os.path.abspath(__file__))
    out = open(os.path.join(outdir, CFG['out']), 'w', newline='')
    w = csv.DictWriter(out, fieldnames=FIELDS)
    w.writeheader()

    plans = plan_trajectories(rng)
    gcid = 0
    n_rows = 0
    for tid, p in enumerate(plans, start=1):
        tmpl = rng.choice(templates)          # 궤적당 템플릿 고정(같은 설비·같은 작업)
        ext_node = rng.choice(EXTNODES)       # ext_leak 누설 위치는 궤적당 고정
        T = p['prefix'] + p['nf']
        inject_onset = p['prefix'] + 1 if p['nf'] else ''
        planned_fail = p['prefix'] + p['nf'] if p['onset'] == 'gradual' else ''
        for k in range(1, T + 1):
            gcid += 1
            tj = rng.uniform(0.97, 1.03); tempj = rng.gauss(0, 1.0); rpmj = rng.uniform(0.98, 1.02)
            faulty = p['nf'] and k > p['prefix']
            if faulty:
                sv = sev_curve(k - p['prefix'], p['nf'], p['curve']) if p['onset'] == 'gradual' else 1.0
                idx = p['lvl']
            for si, r in enumerate(tmpl):
                mode = 'downstroke' if si < 6 else ('pressure_hold' if si < 9 else 'upstroke')
                tgt = gv(r, 'Press.target_pressure_bar_g') * tj
                temp = gv(r, 'Fluid.temperature_c') + tempj
                rpm = gv(r, 'Cell.PUMP_01.rpm') * rpmj
                cmd = {c: gv(r, f'Cell.V_DIR_{c.upper()}.opening_percent') for c in ('pa', 'pb', 'at', 'bt')}
                row = dict(row_id=f'{gcid}_{si}', cycle_id=gcid, cycle_second=si, mode=mode,
                           target_chamber=r['Press.target_chamber'],
                           cmd_pa=round(cmd['pa'], 3), cmd_pb=round(cmd['pb'], 3),
                           cmd_at=round(cmd['at'], 3), cmd_bt=round(cmd['bt'], 3),
                           v_at=round(cmd['at'], 3), v_bt=round(cmd['bt'], 3),
                           v_relief_open=0.0,
                           trajectory_id=tid, cycle_in_traj=k, gt_traj_class=p['cls'],
                           gt_inject_onset=inject_onset, gt_planned_failure=planned_fail,
                           gt_curve=p['curve'])
                if faulty:
                    f, tgt, temp, rpm, v_pa, v_pb, loc, leak_mode = apply_fault(
                        p['cls'], idx, sv, mode, tgt, temp, rpm, cmd, {'ext_node': ext_node})
                    lvl3 = SEV[idx] if p['onset'] == 'abrupt' else SEV[min(2, int(sv * 3))]
                    row.update(f, v_pa=round(v_pa, 3), v_pb=round(v_pb, 3),
                               fault_class=p['cls'], severity_level=lvl3,
                               fault_severity=round(sv, 4), onset_type=p['onset'], is_anomaly=1,
                               gt_fault_location=loc, gt_leak_mode=leak_mode)
                else:  # 건강 구간·정상 궤적: onset 전 normal 라벨(확정 설계)
                    row.update(v_pa=round(cmd['pa'], 3), v_pb=round(cmd['pb'], 3),
                               v_relief_set=250.0, loss_cell='', loss_k_mult=1.0, rough_mult=1.0,
                               pump_head_scale=1.0, leak_from='', leak_to='', leak_K=0.0,
                               load_cap=1e9, fault_class='normal', severity_level='none',
                               fault_severity=0, onset_type='normal', is_anomaly=0,
                               gt_fault_location='', gt_leak_mode='')
                row['target_pressure'] = round(tgt, 3)
                row['temperature_c'] = round(temp, 3)
                row['pump_rpm'] = int(rpm)
                row['loss_k_mult'] = round(row['loss_k_mult'], 4)
                row['rough_mult'] = round(row['rough_mult'], 4)
                row['pump_head_scale'] = round(row['pump_head_scale'], 4)
                if row['v_relief_set'] != 999:
                    row['v_relief_set'] = round(row['v_relief_set'], 3)
                w.writerow(row)
                n_rows += 1
    out.close()
    print(f'trajectories: {len(plans)}, cycles: {gcid}, rows: {n_rows}', file=sys.stderr)


if __name__ == '__main__':
    main()

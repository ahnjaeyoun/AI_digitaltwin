"""
궤적 데이터셋의 관리도 기반 라벨 산출 + 파일럿 정합성 검증 리포트.
설계: docs/예지보전_통합데이터셋_설계.md — health_stage/onset_cycle/failure_cycle/cycles_to_failure는
관측 신호 기준으로 판정한다(주입 파라미터 기준 아님).

1차 파일럿에서 확인된 이 솔버의 특성(문서 §6 참고)을 반영한 최종 구조:
 - 솔버가 매초 준정적이라 hold 압력 감쇠가 존재하지 않음 → 감시 신호에서 hold_decay 제외.
 - 목표압(±3%)·RPM(±2%) 지터가 유량·압력 σ를 지배 → 지령값(hold 목표압, rpm) OLS 회귀로
   정규화한 잔차 신호를 사용(σ 15~20배 감소, 지령은 관측 가능하므로 누수 아님).
 - 리턴유량차·목표압오차는 정상에서 σ≈0(영분산) → σ 대신 물리 임계 매핑(docx §5.1 원칙).
 - 통일 프레임: 클래스별 감시 신호에 대해 initial/moderate/severe "주입 수준"에서의 신호값을
   데이터에서 자동 캘리브레이션(docx 표 B의 심각도 임계표 재산출에 해당)하고, 이를
   z 상당값 2/3/6에 매핑한 z_eq 척도로 관리도 판정:
     onset_cycle  = WE 규칙(1점 z_eq≥3, 또는 연속 3점 중 2점 ≥2) 최초 발동 (raw z_eq)
     failure_cycle= z_eq≥6 (relief_stuck은 CAP≥250bar 물리한계 OR) 최초 도달 → 이후 트리밍
     health_stage = 인과적 rolling median(3) z_eq 기준 <2 normal / <3 initial / <6 moderate / ≥6 severe
     degradation_score = smoothed z_eq

파일럿 실행: python trajectory_labels.py → 캘리브레이션 표 + 검증 리포트 + 사이클 라벨 CSV
"""
import sys

import numpy as np
import pandas as pd

SCEN_PATH = r"C:\pipes_press\ab_solver\scenarios_traj_pilot.csv"
SOLVER_PATH = r"C:\pipes_press\dataset\gen_output_traj_pilot.csv"
CYCLE_LABELS_OUT = r"C:\pipes_press\dataset\traj_pilot_cycle_labels.csv"

RETURN_AREA = 3.14159265 * 0.008 ** 2  # C_RETURN 직경 16mm → 유속→L/min 환산

# 클래스 → (감시 신호, 방향). *_r = 지령(tgt_hold, rpm) 회귀 정규화 잔차
CLASS_SIG = {
    'pump_wear':      ('flow_per_rpm_r',    'low'),
    'suction_clog':   ('down_min_pump_in_r', 'low'),
    'line_clog':      ('down_pump_out_r',   'high'),
    'valve_stuck':    ('up_flow_r',         'low'),
    'valve_int_leak': ('down_max_tgt_err',  'high'),
    'seal_leak':      ('down_max_tgt_err',  'high'),
    'ext_leak':       ('frd_max',           'high'),
    'relief_early':   ('down_max_tgt_err',  'high'),
    'relief_stuck':   ('down_pump_out_r',   'high'),
    'overheat':       ('mean_temp',         'high'),
}
NORM_SIGS = ['down_mean_flow', 'flow_per_rpm', 'down_pump_out', 'up_flow', 'down_min_pump_in']

# 심각도 3단계 주입 수준의 sv 등가값(=severe 효과 대비 비율, MECH 파라미터에서 유도).
# initial 수준이 사실상 무효과인 relief_early는 0 → 캘리브레이션에서 검출 하한으로 대체.
SV_EQ = {
    'suction_clog': (0.231, 0.538), 'line_clog': (0.207, 0.517), 'valve_stuck': (0.412, 0.682),
    'overheat': (0.412, 0.706), 'pump_wear': (0.333, 0.667), 'seal_leak': (0.273, 0.545),
    'ext_leak': (0.273, 0.545), 'valve_int_leak': (0.273, 0.545),
    'relief_early': (0.0, 0.47), 'relief_stuck': (0.4, 0.7),
}


def cycle_signals(df):
    """행(초) 단위 조인 데이터 → 사이클 단위 감시 신호 테이블."""
    df = df.copy()
    df['return_flow'] = df['vel_C_RETURN'] * RETURN_AREA * 60000
    df['flow_ret_diff'] = df['calculated_flow_rate_L_min'] - df['return_flow']
    d = df[df['mode'] == 'downstroke']
    h = df[df['mode'] == 'pressure_hold']
    u = df[df['mode'] == 'upstroke']

    meta = df.groupby('cycle_id').agg(
        trajectory_id=('trajectory_id', 'first'), cycle_in_traj=('cycle_in_traj', 'first'),
        gt_traj_class=('gt_traj_class', 'first'), gt_inject_onset=('gt_inject_onset', 'first'),
        gt_planned_failure=('gt_planned_failure', 'first'), gt_curve=('gt_curve', 'first'),
        onset_type=('onset_type', 'last'), fault_severity=('fault_severity', 'max'),
        mean_temp=('temperature_c', 'mean'), max_cap_p=('N_CYL_CAP_p', 'max'),
        frd_max=('flow_ret_diff', 'max'))
    gd = d.groupby('cycle_id').agg(
        down_mean_flow=('calculated_flow_rate_L_min', 'mean'),
        down_min_pump_in=('N_PUMP_IN_p', 'min'), down_pump_out=('N_PUMP_OUT_p', 'mean'),
        down_max_tgt_err=('target_pressure_error_bar', 'max'), rpm=('pump_rpm', 'mean'))
    gh = h.groupby('cycle_id').agg(tgt_hold=('target_pressure', 'mean'))
    gu = u.groupby('cycle_id').agg(up_flow=('calculated_flow_rate_L_min', 'mean'))
    c = meta.join(gd).join(gh).join(gu)
    c['flow_per_rpm'] = c['down_mean_flow'] / c['rpm'] * 1000
    return c.reset_index()


def add_normalized(cyc, coefs=None):
    """정상 궤적 사이클로 OLS(절편+tgt_hold+rpm) 적합 → 전체에 잔차 컬럼 *_r 추가.
    coefs를 주면(train에서 저장한 값) 재적합 없이 그대로 적용 — 세트 간 동일 기준 유지."""
    Xa = np.column_stack([np.ones(len(cyc)), cyc['tgt_hold'], cyc['rpm']])
    if coefs is None:
        nm = cyc[cyc['gt_traj_class'] == 'normal']
        Xn = np.column_stack([np.ones(len(nm)), nm['tgt_hold'], nm['rpm']])
        coefs = {}
        for s in NORM_SIGS:
            b, *_ = np.linalg.lstsq(Xn, nm[s], rcond=None)
            coefs[s] = list(b)
    for s in NORM_SIGS:
        cyc[s + '_r'] = cyc[s] - Xa @ np.asarray(coefs[s])
    return cyc, coefs


def calibrate(cyc):
    """클래스별 심각도 임계표(v0/v_init/v_mod/v_sev) 자동 산출 — docx 표 B 재산출에 해당.
    v0 = 정상 중앙값, v_init/v_mod = 해당 주입 수준(sv 등가) 부근 gradual 사이클 중앙값,
    v_sev = sv≥0.85 사이클의 보수적 분위수(고장 방향 하위 10%) → 램프 말미에 안정적으로 도달."""
    nm = cyc[cyc['gt_traj_class'] == 'normal']
    calib = {}
    for cls, (sig, direction) in CLASS_SIG.items():
        g = cyc[(cyc['gt_traj_class'] == cls) & (cyc['fault_severity'] > 0)]
        v0 = nm[sig].median()
        lo_q, hi_q = (0.9, 0.1) if direction == 'low' else (0.1, 0.9)
        sev = g[g['fault_severity'] >= 0.85][sig]
        v_sev = sev.quantile(lo_q)

        def level_val(sv_eq):
            band = g[(g['fault_severity'] >= sv_eq - 0.08) & (g['fault_severity'] <= sv_eq + 0.08)]
            return band[sig].median() if len(band) >= 5 else np.nan
        v_i, v_m = level_val(SV_EQ[cls][0]), level_val(SV_EQ[cls][1])
        sgn = -1 if direction == 'low' else 1
        d_sev = sgn * (v_sev - v0)
        d_i = sgn * (v_i - v0) if v_i == v_i else np.nan
        d_m = sgn * (v_m - v0) if v_m == v_m else np.nan
        # 검출 하한: 정상 분포 극단(99.9pct 이탈폭)의 2배보다 작은 임계는 무의미 → 하한 적용
        # (1.5배로는 relief_early의 initial 임계가 정상 노이즈 상한과 겹쳐 prefix 오경보 발생 확인)
        noise = max(sgn * (nm[sig].quantile(0.999 if sgn > 0 else 0.001) - v0), 0) * 2.0
        d_i = max(d_i if d_i == d_i else 0.10 * d_sev, noise, 1e-9)
        d_m = max(d_m if d_m == d_m else 0.45 * d_sev, d_i * 1.25)
        d_sev = max(d_sev, d_m * 1.25)
        calib[cls] = dict(sig=sig, dir=direction, v0=v0, d=[d_i, d_m, d_sev])
    return calib


def z_eq(cyc, calib):
    """캘리브레이션 임계를 z 상당값 [2,3,6]에 매핑한 연속 점수(고장 방향)."""
    out = {}
    for cls, c in calib.items():
        sgn = -1 if c['dir'] == 'low' else 1
        t = sgn * (cyc[c['sig']] - c['v0'])
        d_i, d_m, d_s = c['d']
        v = np.interp(t, [0, d_i, d_m, d_s], [0, 2, 3, 6])
        v = v + np.maximum(0, t - d_s) * (3 / max(d_s - d_m, 1e-9))  # 6 초과 선형 연장
        out[cls] = v
    return pd.DataFrame(out, index=cyc.index)


def we_onset(z):
    """WE 규칙: 1점 z≥3 또는 연속 3점 중 2점 z≥2 최초 발동 인덱스(0기준)."""
    z = np.asarray(z)
    for i in range(len(z)):
        if z[i] >= 3 or (z[max(0, i - 2):i + 1] >= 2).sum() >= 2:
            return i
    return None


def smooth(z):
    return pd.Series(z).rolling(3, min_periods=1).median().to_numpy()  # 인과적


def stage_of(z):
    return np.select([z >= 6, z >= 3, z >= 2], ['severe', 'moderate', 'initial'], default='normal')


def label_trajectories(cyc, calib):
    zdf = z_eq(cyc, calib)
    out, traj_rows = [], []
    for tid, g in cyc.groupby('trajectory_id'):
        g = g.sort_values('cycle_in_traj')
        cls = g['gt_traj_class'].iloc[0]
        z = (zdf.loc[g.index, cls] if cls != 'normal' else zdf.loc[g.index].max(axis=1)).to_numpy()
        zs = smooth(z)
        onset_i = we_onset(z)
        fail_mask = z >= 6
        if cls == 'relief_stuck':
            fail_mask |= g['max_cap_p'].to_numpy() >= 250
        fail_i = int(np.argmax(fail_mask)) if fail_mask.any() else None
        keep = len(g) if fail_i is None else fail_i + 1  # failure_cycle에서 궤적 종료(트리밍)
        gg = g.iloc[:keep].copy()
        gg['degradation_score'] = zs[:keep]
        gg['health_stage'] = stage_of(zs[:keep])
        gg['onset_cycle'] = (onset_i + 1) if onset_i is not None and onset_i < keep else np.nan
        gg['failure_cycle'] = (fail_i + 1) if fail_i is not None else np.nan
        is_gradual = (g['onset_type'] == 'gradual').any()
        gg['cycles_to_failure'] = (fail_i + 1) - gg['cycle_in_traj'] if (fail_i is not None and is_gradual) else np.nan
        out.append(gg)
        traj_rows.append(dict(
            trajectory_id=tid, cls=cls,
            onset_type=g['onset_type'].iloc[-1] if cls != 'normal' else 'normal',
            curve=g['gt_curve'].iloc[0], n_cycles=len(g), kept=keep,
            inject_onset=pd.to_numeric(g['gt_inject_onset'].iloc[0], errors='coerce'),
            planned_failure=pd.to_numeric(g['gt_planned_failure'].iloc[0], errors='coerce'),
            obs_onset=(onset_i + 1) if onset_i is not None else np.nan,
            obs_failure=(fail_i + 1) if fail_i is not None else np.nan,
            max_z=float(np.max(z)) if len(z) else np.nan))
    return pd.concat(out, ignore_index=True), pd.DataFrame(traj_rows)


def report(cyc_labeled, traj, calib, n_norm_cycles):
    pd.set_option('display.width', 200)
    print('=' * 70)
    print(f'[1] 클래스별 심각도 임계표 (정상 {n_norm_cycles}사이클 + gradual 궤적 자동 캘리브레이션)')
    print(f'  {"class":15s} {"signal":20s} {"v0(정상)":>10s} {"initial(z2)":>11s} {"moderate(z3)":>12s} {"severe(z6)":>11s}')
    for cls, c in calib.items():
        sgn = -1 if c['dir'] == 'low' else 1
        vi, vm, vs = (c['v0'] + sgn * d for d in c['d'])
        print(f'  {cls:15s} {c["sig"]:20s} {c["v0"]:10.4f} {vi:11.4f} {vm:12.4f} {vs:11.4f}')

    print('\n[2] 정상 전용 궤적 오경보 (전 클래스 z_eq 최대 기준)')
    nt = traj[traj['cls'] == 'normal']
    print(f"  궤적 {len(nt)}개 중 WE 발동 {nt['obs_onset'].notna().sum()}개, "
          f"6σ(가짜 고장) {nt['obs_failure'].notna().sum()}개, max z_eq={nt['max_z'].max():.2f}")
    ncyc = cyc_labeled[cyc_labeled['gt_traj_class'] == 'normal']
    print(f'  정상 사이클 health_stage 분포: {ncyc["health_stage"].value_counts().to_dict()}')

    print('\n[3] gradual 궤적: 관측 onset/failure vs 주입 계획')
    gt = traj[traj['onset_type'] == 'gradual'].copy()
    gt['detect_delay'] = gt['obs_onset'] - gt['inject_onset']
    gt['fail_vs_plan'] = gt['obs_failure'] - gt['planned_failure']
    for cls, g in gt.groupby('cls'):
        print(f"  {cls:15s} n={len(g)}  onset지연: {g['detect_delay'].tolist()}  "
              f"6σ도달 {g['obs_failure'].notna().sum()}/{len(g)}  (도달-계획): {g['fail_vs_plan'].tolist()}  "
              f"max_z={g['max_z'].round(1).tolist()}")

    print('\n[4] abrupt 궤적')
    at = traj[traj['onset_type'] == 'abrupt'].copy()
    at['detect_delay'] = at['obs_onset'] - at['inject_onset']
    for cls, g in at.groupby('cls'):
        print(f"  {cls:15s} n={len(g)}  onset지연: {g['detect_delay'].tolist()}  "
              f"6σ도달: {g['obs_failure'].notna().tolist()}  max_z={g['max_z'].round(1).tolist()}")

    print('\n[5] health_stage 단조 진행성 (gradual 궤적, smoothed 기준 역행 비율)')
    order = {'normal': 0, 'initial': 1, 'moderate': 2, 'severe': 3}
    grad_ids = traj.loc[traj['onset_type'] == 'gradual', 'trajectory_id']
    regress = total = 0
    for tid in grad_ids:
        s = cyc_labeled[cyc_labeled['trajectory_id'] == tid].sort_values('cycle_in_traj')['health_stage'].map(order)
        dif = s.diff().dropna()
        regress += int((dif < 0).sum()); total += len(dif)
    print(f'  역행 {regress}/{total} ({regress / total * 100:.1f}%)')

    n_ctf = cyc_labeled['cycles_to_failure'].notna().sum()
    print(f'\n[6] RUL 라벨 커버리지: cycles_to_failure 값 있는 사이클 {n_ctf:,}개 '
          f'(gradual & 6σ 도달 궤적), censored(정상/abrupt/미도달) {len(cyc_labeled) - n_ctf:,}개')


def main():
    scen = pd.read_csv(SCEN_PATH)
    sol = pd.read_csv(SOLVER_PATH)
    df = scen.merge(sol.drop(columns=['active_mode']), on='row_id', validate='one_to_one')
    assert len(df) == len(scen), '조인 행 수 불일치'
    cyc = cycle_signals(df)
    cyc, _ = add_normalized(cyc)
    calib = calibrate(cyc)
    n_norm = (cyc['gt_traj_class'] == 'normal').sum()
    cyc_labeled, traj = label_trajectories(cyc, calib)
    report(cyc_labeled, traj, calib, n_norm)
    cyc_labeled.to_csv(CYCLE_LABELS_OUT, index=False)
    print(f'\n사이클 라벨 저장 -> {CYCLE_LABELS_OUT} ({len(cyc_labeled):,}사이클, 트리밍 전 {len(cyc):,})')


if __name__ == '__main__':
    main()

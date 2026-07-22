"""
궤적형(예지보전 통합) 최종 데이터셋 빌더 — docs/예지보전_통합데이터셋_설계.md §3/§5-3 구현.

scenarios_traj_*.csv(생성기) + gen_output_traj_*.csv(솔버)를 row_id로 조인해
final_training_dataset_v4와 동일한 48컬럼 스키마 + 궤적/예지보전 컬럼 10개를 붙인다:
  trajectory_id, cycle_in_traj, timestamp_sec, cycle_duration_sec(=15),
  health_stage, degradation_score, onset_cycle(관측), failure_cycle, cycles_to_failure,
  fault_component

라벨 판정(관리도)·트리밍은 scripts/trajectory_labels.py의 로직을 그대로 재사용한다.
캘리브레이션(지령 회귀 계수 + 클래스별 심각도 임계표)은 train 세트에서 산출해 JSON으로 저장하고,
test/twin 세트는 그 파일을 로드해 적용한다 — 세 세트의 라벨 정의를 동일하게 유지하기 위함.

severity_level은 설계 확정대로 주입 샘플링 값이 아니라 health_stage에서 유도해 덮어쓴다
(normal→none). fault_class/is_anomaly는 주입(물리) 기준 그대로 — 주입~관측 onset 사이는
"fault_class는 고장, health_stage는 normal"인 잠복 구간이 될 수 있다(정직한 라벨).

사용:
  python build_trajectory_dataset.py --set train   # 캘리브레이션 산출+저장
  python build_trajectory_dataset.py --set test    # train 캘리브레이션 적용
  python build_trajectory_dataset.py --set twin
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import trajectory_labels as TL

SCEN_TPL = r"C:\pipes_press\ab_solver\scenarios_traj_{s}.csv"
SOLVER_TPL = r"C:\pipes_press\dataset\gen_output_traj_{s}.csv"
OUT_TPL = r"C:\pipes_press\dataset\traj_dataset_{s}.csv"
CALIB_PATH = r"C:\pipes_press\models\traj_label_calibration.json"

CYCLE_DURATION_SEC = 15  # 6/3/6 고정 사이클 (설계 확정)

# 시나리오 컬럼 → final 스키마 (build_dataset.load_command_source와 동일 규칙 + 밸브 실제개도)
SCEN_RENAME = {
    "cmd_pa": "valve_cmd_pa", "cmd_pb": "valve_cmd_pb", "cmd_at": "valve_cmd_at", "cmd_bt": "valve_cmd_bt",
    "v_pa": "valve_pos_pa", "v_pb": "valve_pos_pb", "v_at": "valve_pos_at", "v_bt": "valve_pos_bt",
    "v_relief_set": "relief_set_pressure_bar", "target_pressure": "target_pressure_bar",
}
# gen_output(축약 컬럼명) → final 스키마 (enrich_dataset_v3.REFRESH_MAP과 동일 계열)
SOLVER_RENAME = {
    "active_mode": "cycle_phase",
    "calculated_flow_rate_L_min": "flow_rate_L_min",
    "N_PUMP_IN_p": "pump_in_pressure_bar", "N_PUMP_OUT_p": "pump_out_pressure_bar",
    "N_CYL_CAP_p": "cyl_cap_pressure_bar", "N_CYL_ROD_p": "cyl_rod_pressure_bar",
    "max_active_velocity": "max_velocity_m_s",
    "N_VALVE_P_p": "gt_N_VALVE_P_p", "N_VALVE_A_p": "gt_N_VALVE_A_p",
    "N_VALVE_B_p": "gt_N_VALVE_B_p", "N_VALVE_T_p": "gt_N_VALVE_T_p",
    "Kv_V_DIR_PA": "gt_Kv_V_DIR_PA", "Kv_V_DIR_PB": "gt_Kv_V_DIR_PB",
    "Kv_V_DIR_AT": "gt_Kv_V_DIR_AT", "Kv_V_DIR_BT": "gt_Kv_V_DIR_BT", "Kv_V_RELIEF": "gt_Kv_V_RELIEF",
    "loss_C_SUCTION": "gt_loss_C_SUCTION", "loss_C_PRESSURE": "gt_loss_C_PRESSURE",
    "loss_C_A_LINE": "gt_loss_C_A_LINE", "loss_C_B_LINE": "gt_loss_C_B_LINE",
    "loss_C_RETURN": "gt_loss_C_RETURN",
}

# fault_component 매핑 (설계 §3). ext_leak은 누설 위치(gt_fault_location)에 따라 분기.
COMPONENT_BY_CLASS = {
    "normal": "none", "pump_wear": "pump", "suction_clog": "suction_filter",
    "line_clog": "pressure_line", "valve_stuck": "direction_valve", "valve_int_leak": "direction_valve",
    "seal_leak": "cylinder", "relief_early": "relief_valve", "relief_stuck": "relief_valve",
    "overheat": "hydraulic_oil",
}
EXT_LEAK_COMPONENT = {"N_CYL_CAP": "cylinder", "N_VALVE_A": "valve_port", "N_VALVE_P": "pump_line"}

TRAJ_COLUMNS = [
    "trajectory_id", "cycle_in_traj", "timestamp_sec", "cycle_duration_sec",
    "health_stage", "degradation_score", "onset_cycle", "failure_cycle", "cycles_to_failure",
    "fault_component",
    # 감사(audit) 전용 — 궤적이 어떤 고장으로 계획됐는지(prefix 구간 포함), 주입 시작 사이클
    "gt_traj_class", "gt_inject_onset",
]
STAGE_TO_SEVERITY = {"normal": "none", "initial": "initial", "moderate": "moderate", "severe": "severe"}


def build(set_name):
    scen = pd.read_csv(SCEN_TPL.format(s=set_name))
    sol = pd.read_csv(SOLVER_TPL.format(s=set_name))
    assert (sol["status"] == "ok").all(), "솔버 실패 행 존재"
    df = scen.merge(sol, on="row_id", validate="one_to_one")
    assert len(df) == len(scen) == len(sol), "조인 행 수 불일치"

    # --- 관리도 라벨 판정 (trajectory_labels 재사용) ---
    cyc = TL.cycle_signals(df)
    if set_name in ("train", "pilot"):  # pilot은 코드 검증용(캘리브레이션 저장 안 함)
        cyc, coefs = TL.add_normalized(cyc)
        calib = TL.calibrate(cyc)
        if set_name == "train":
            Path(CALIB_PATH).write_text(json.dumps(
                {"coefs": {k: [float(x) for x in v] for k, v in coefs.items()},
                 "calib": {k: dict(v, v0=float(v["v0"]), d=[float(x) for x in v["d"]]) for k, v in calib.items()}},
                ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"캘리브레이션 저장 -> {CALIB_PATH}")
    else:
        saved = json.loads(Path(CALIB_PATH).read_text(encoding="utf-8"))
        cyc, _ = TL.add_normalized(cyc, coefs=saved["coefs"])
        calib = saved["calib"]
    cyc_labeled, traj = TL.label_trajectories(cyc, calib)
    n_norm = (cyc["gt_traj_class"] == "normal").sum()
    TL.report(cyc_labeled, traj, calib, n_norm)

    # --- 행(초) 단위 final 스키마 조립 ---
    keep_cycles = cyc_labeled[["cycle_id", "health_stage", "degradation_score",
                               "onset_cycle", "failure_cycle", "cycles_to_failure"]]
    df = df.merge(keep_cycles, on="cycle_id", how="inner")  # failure 이후 사이클 트리밍
    df = df.rename(columns=SCEN_RENAME).rename(columns=SOLVER_RENAME)

    area_m2 = np.pi * (0.016 / 2) ** 2
    df["return_flow_L_min"] = df["vel_C_RETURN"] * area_m2 * 60000
    df["timestamp_sec"] = (df["cycle_in_traj"] - 1) * CYCLE_DURATION_SEC + df["cycle_second"]
    df["cycle_duration_sec"] = CYCLE_DURATION_SEC
    df["severity_level"] = df["health_stage"].map(STAGE_TO_SEVERITY)  # 설계: 관측 기준으로 유도
    df["fault_component"] = df["fault_class"].map(COMPONENT_BY_CLASS)
    ext = df["fault_class"] == "ext_leak"
    df.loc[ext, "fault_component"] = df.loc[ext, "gt_fault_location"].map(EXT_LEAK_COMPONENT)

    from build_dataset import FINAL_SCHEMA_COLUMNS
    out_cols = FINAL_SCHEMA_COLUMNS + TRAJ_COLUMNS
    missing = [c for c in out_cols if c not in df.columns]
    assert not missing, f"누락 컬럼: {missing}"
    df = df.sort_values(["trajectory_id", "cycle_in_traj", "cycle_second"])[out_cols]

    out = OUT_TPL.format(s=set_name)
    df.to_csv(out, index=False)
    ratio = (df["fault_class"] == "normal").mean()
    print(f"\n저장 -> {out}: {len(df):,}행 x {len(df.columns)}열, "
          f"궤적 {df['trajectory_id'].nunique()}개, normal 행 비율 {ratio:.1%}, "
          f"RUL 라벨 행 {df['cycles_to_failure'].notna().sum():,}개")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set", required=True, choices=["train", "test", "twin", "pilot"])
    args = ap.parse_args()
    build(args.set)

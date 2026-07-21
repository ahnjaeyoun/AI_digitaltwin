"""
raw digital-twin 출력(명령 소스 + solver_output) -> final_training_dataset.csv 스키마로 병합.

사용 예:
    python build_dataset.py --cmd input_timeseries.csv --solver solver_output_timeseries.csv --out merged.csv
    python build_dataset.py --cmd scenarios.csv        --solver solver_output_timeseries.csv --out merged.csv
"""
import argparse

import numpy as np
import pandas as pd

FLUID_DENSITY_KG_M3 = 860.0
SPECIFIC_GRAVITY = FLUID_DENSITY_KG_M3 / 1000.0
VALVE_KV_MAX_M3_H = {"pa": 0.5, "pb": 0.5, "at": 0.5, "bt": 0.5}  # network.json 고정 스펙
C_RETURN_DIAMETER_M = 0.016  # network.json C_RETURN 배관 직경 (리턴라인 유량 환산용)

LABEL_COLS = [
    "fault_class", "severity_level", "fault_severity", "onset_type", "is_anomaly",
    "gt_leak_mode", "gt_fault_location",
]
LABEL_DEFAULTS = {
    "fault_class": "normal", "severity_level": "none", "fault_severity": 0.0,
    "onset_type": "normal", "is_anomaly": 0, "gt_leak_mode": np.nan, "gt_fault_location": np.nan,
}

FINAL_SCHEMA_COLUMNS = [
    "cycle_id", "cycle_second", "cycle_phase", "target_chamber", "temperature_c", "pump_rpm",
    "valve_cmd_pa", "valve_pos_pa", "valve_cmd_pb", "valve_pos_pb",
    "valve_cmd_at", "valve_pos_at", "valve_cmd_bt", "valve_pos_bt",
    "flow_rate_L_min", "return_flow_L_min", "pump_in_pressure_bar", "pump_out_pressure_bar",
    "cyl_cap_pressure_bar", "cyl_rod_pressure_bar", "pump_delta_pressure_bar",
    "pump_head_m", "hydraulic_power_kW", "max_velocity_m_s", "target_pressure_error_bar",
    "relief_set_pressure_bar", "target_pressure_bar",
    "gt_N_VALVE_P_p", "gt_N_VALVE_A_p", "gt_N_VALVE_B_p", "gt_N_VALVE_T_p",
    "gt_Kv_V_DIR_PA", "gt_Kv_V_DIR_PB", "gt_Kv_V_DIR_AT", "gt_Kv_V_DIR_BT", "gt_Kv_V_RELIEF",
    "gt_loss_C_SUCTION", "gt_loss_C_PRESSURE", "gt_loss_C_A_LINE", "gt_loss_C_B_LINE", "gt_loss_C_RETURN",
    "gt_leak_mode", "gt_fault_location",
    "fault_class", "severity_level", "fault_severity", "onset_type", "is_anomaly",
]

ENGINEERED_COLUMNS = [
    "feat_valve_dev_pa", "feat_valve_dev_pb", "feat_valve_dev_at", "feat_valve_dev_bt",
    "feat_valve_dev_max_pa", "feat_valve_dev_max_pb", "feat_valve_dev_max_at", "feat_valve_dev_max_bt",
    "feat_flow_per_rpm", "feat_cap_rod_corr",
    "feat_hold_pressure_decay", "feat_hold_pressure_decay_ffill", "feat_mass_balance_resid",
    "feat_target_pressure_error_max", "feat_downstroke_error_max", "feat_upstroke_error_max",
    "feat_up_down_error_diff", "feat_flow_return_diff",
]


def load_command_source(path):
    """명령/라벨 소스: gen_scenarios.py의 scenario 파일 또는 raw input_timeseries 파일."""
    df = pd.read_csv(path, encoding="utf-8-sig")

    if "cmd_pa" in df.columns:  # gen_scenarios.py scenario 스키마
        df = df.rename(columns={
            "cmd_pa": "valve_cmd_pa", "cmd_pb": "valve_cmd_pb",
            "cmd_at": "valve_cmd_at", "cmd_bt": "valve_cmd_bt",
            "v_relief_set": "relief_set_pressure_bar", "target_pressure": "target_pressure_bar",
        })
    elif "Cell.V_DIR_PA.opening_percent" in df.columns:  # raw input_timeseries 스키마
        df = df.rename(columns={
            "Press.target_chamber": "target_chamber",
            "Fluid.temperature_c": "temperature_c",
            "Cell.PUMP_01.rpm": "pump_rpm",
            "Cell.V_DIR_PA.opening_percent": "valve_cmd_pa",
            "Cell.V_DIR_PB.opening_percent": "valve_cmd_pb",
            "Cell.V_DIR_AT.opening_percent": "valve_cmd_at",
            "Cell.V_DIR_BT.opening_percent": "valve_cmd_bt",
            "Cell.V_RELIEF.set_pressure_bar_g": "relief_set_pressure_bar",
            "Press.target_pressure_bar_g": "target_pressure_bar",
        })
    else:
        raise ValueError(f"인식할 수 없는 명령 소스 스키마: {path}")

    base = ["cycle_id", "cycle_second", "target_chamber", "temperature_c", "pump_rpm",
            "valve_cmd_pa", "valve_cmd_pb", "valve_cmd_at", "valve_cmd_bt",
            "relief_set_pressure_bar", "target_pressure_bar"]
    present_labels = [c for c in LABEL_COLS if c in df.columns]
    return df[base + present_labels]


def load_solver_output(path):
    """solver_output_timeseries 스키마 -> final 스키마 컬럼명으로 정규화."""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df = df.drop(columns=["cycle_phase"])  # 한글 원시 phase 라벨 (active_mode가 영문으로 대체)
    df = df.rename(columns={
        "active_mode": "cycle_phase",
        "calculated_flow_rate_L_min": "flow_rate_L_min",
        "N_PUMP_IN_pressure_bar_g": "pump_in_pressure_bar",
        "N_PUMP_OUT_pressure_bar_g": "pump_out_pressure_bar",
        "N_CYL_CAP_pressure_bar_g": "cyl_cap_pressure_bar",
        "N_CYL_ROD_pressure_bar_g": "cyl_rod_pressure_bar",
        "max_active_velocity_m_s": "max_velocity_m_s",
        "V_DIR_PA_opening_percent": "valve_pos_pa",
        "V_DIR_PB_opening_percent": "valve_pos_pb",
        "V_DIR_AT_opening_percent": "valve_pos_at",
        "V_DIR_BT_opening_percent": "valve_pos_bt",
        "N_VALVE_P_pressure_bar_g": "gt_N_VALVE_P_p",
        "N_VALVE_A_pressure_bar_g": "gt_N_VALVE_A_p",
        "N_VALVE_B_pressure_bar_g": "gt_N_VALVE_B_p",
        "N_VALVE_T_pressure_bar_g": "gt_N_VALVE_T_p",
        "V_DIR_PA_Kv_effective_m3_h": "gt_Kv_V_DIR_PA",
        "V_DIR_PB_Kv_effective_m3_h": "gt_Kv_V_DIR_PB",
        "V_DIR_AT_Kv_effective_m3_h": "gt_Kv_V_DIR_AT",
        "V_DIR_BT_Kv_effective_m3_h": "gt_Kv_V_DIR_BT",
        "V_RELIEF_Kv_effective_m3_h": "gt_Kv_V_RELIEF",
        "C_SUCTION_pressure_loss_bar": "gt_loss_C_SUCTION",
        "C_PRESSURE_pressure_loss_bar": "gt_loss_C_PRESSURE",
        "C_A_LINE_pressure_loss_bar": "gt_loss_C_A_LINE",
        "C_B_LINE_pressure_loss_bar": "gt_loss_C_B_LINE",
        "C_RETURN_pressure_loss_bar": "gt_loss_C_RETURN",
    })
    # 리턴라인 유량 (2026-07-16 gen.cpp 수정: leak_to_가 비어있는(외부누설) 경우만 리턴경로에
    # 누설분을 뺀 유량이 반영되도록 고침 -> ext_leak만 flow_rate_L_min과 벌어짐, 다른 클래스는 그대로).
    area_m2 = np.pi * (C_RETURN_DIAMETER_M / 2) ** 2
    df["return_flow_L_min"] = df["C_RETURN_velocity_m_s"] * area_m2 * 60000
    keep = [
        "cycle_id", "cycle_second", "cycle_phase",
        "valve_pos_pa", "valve_pos_pb", "valve_pos_at", "valve_pos_bt",
        "flow_rate_L_min", "return_flow_L_min", "pump_in_pressure_bar", "pump_out_pressure_bar",
        "cyl_cap_pressure_bar", "cyl_rod_pressure_bar", "pump_delta_pressure_bar",
        "pump_head_m", "hydraulic_power_kW", "max_velocity_m_s", "target_pressure_error_bar",
        "gt_N_VALVE_P_p", "gt_N_VALVE_A_p", "gt_N_VALVE_B_p", "gt_N_VALVE_T_p",
        "gt_Kv_V_DIR_PA", "gt_Kv_V_DIR_PB", "gt_Kv_V_DIR_AT", "gt_Kv_V_DIR_BT", "gt_Kv_V_RELIEF",
        "gt_loss_C_SUCTION", "gt_loss_C_PRESSURE", "gt_loss_C_A_LINE", "gt_loss_C_B_LINE", "gt_loss_C_RETURN",
    ]
    return df[keep]


def merge_sources(cmd_df, solver_df):
    merged = cmd_df.merge(solver_df, on=["cycle_id", "cycle_second"], how="inner", validate="one_to_one")
    if len(merged) != len(cmd_df) or len(merged) != len(solver_df):
        raise ValueError(
            f"merge 후 행 수 불일치: cmd={len(cmd_df)} solver={len(solver_df)} merged={len(merged)} "
            "-> cycle_id/cycle_second 조인 키가 두 파일에서 어긋남"
        )
    return merged


def apply_label_defaults(df):
    df = df.copy()
    for col, default in LABEL_DEFAULTS.items():
        if col not in df.columns:
            df[col] = default
    return df


def add_engineered_features(df):
    df = df.sort_values(["cycle_id", "cycle_second"]).reset_index(drop=True)

    for v in ["pa", "pb", "at", "bt"]:
        df[f"feat_valve_dev_{v}"] = df[f"valve_cmd_{v}"] - df[f"valve_pos_{v}"]
        # 위상 전환(예: downstroke->보압) 후 해당 밸브가 닫히면 편차 신호가 사라지므로,
        # "이번 사이클 들어 지금까지 관측된 최대 편차"를 누적으로 기억해 계속 들고 간다.
        df[f"feat_valve_dev_max_{v}"] = (
            df[f"feat_valve_dev_{v}"].abs().groupby(df["cycle_id"]).cummax()
        )

    df["feat_flow_per_rpm"] = df["flow_rate_L_min"] / df["pump_rpm"].replace(0, np.nan)

    df["feat_cap_rod_corr"] = (
        df.groupby("cycle_id", group_keys=False)
        .apply(lambda c: c["cyl_cap_pressure_bar"].expanding(min_periods=3).corr(c["cyl_rod_pressure_bar"]),
               include_groups=False)
        .reset_index(level=0, drop=True)
    )

    df["feat_hold_pressure_decay"] = np.nan
    hold_mask = (df["cycle_phase"] == "pressure_hold").to_numpy()
    hold = df.loc[hold_mask, ["cycle_id", "cycle_second", "cyl_cap_pressure_bar"]]
    g = hold.groupby("cycle_id")
    t0_val = g["cyl_cap_pressure_bar"].transform("first")
    t0_sec = g["cycle_second"].transform("first")
    elapsed = (hold["cycle_second"] - t0_sec).replace(0, np.nan)
    df.loc[hold.index, "feat_hold_pressure_decay"] = (hold["cyl_cap_pressure_bar"] - t0_val) / elapsed
    # 보압 구간이 끝나면(upstroke 등) 값이 사라지므로, 직전에 관측된 감쇠율을 그대로 들고 간다.
    df["feat_hold_pressure_decay_ffill"] = df.groupby("cycle_id")["feat_hold_pressure_decay"].ffill()

    # relief_early는 목표압이 낮춰진 릴리프 설정압에 근접하는 짧은 구간(주로 downstroke 중반)에만
    # target_pressure_error가 반짝 나타나고 그 외 구간은 정상과 물리적으로 구별이 안 된다.
    # (음수 없음, min=0 확인됨) 누적 최댓값으로 신호를 사이클 끝까지 들고 간다.
    df["feat_target_pressure_error_max"] = (
        df["target_pressure_error_bar"].groupby(df["cycle_id"]).cummax()
    )

    # ext_leak(누설위치가 downstroke측 P/A 경로에 고정) vs valve_int_leak(활성 밸브를 그때그때 따라다님)
    # 구분용: downstroke/upstroke phase별로 target_pressure_error의 누적 최댓값을 따로 추적.
    # ext_leak은 downstroke만 높고 upstroke는 뚝 떨어지는 비대칭, valve_int_leak은 양쪽 다 높게 유지(실측 확인됨).
    for phase in ["downstroke", "upstroke"]:
        mask = (df["cycle_phase"] == phase).to_numpy()
        sub = df.loc[mask, ["cycle_id", "target_pressure_error_bar"]]
        colname = f"feat_{phase}_error_max"
        df[colname] = np.nan
        df.loc[sub.index, colname] = sub.groupby("cycle_id")["target_pressure_error_bar"].cummax()
        df[colname] = df.groupby("cycle_id")[colname].ffill()

    # 트리가 downstroke/upstroke 최댓값을 알아서 조합하길 기대하기보다, 비대칭성 자체를 직접 계산해서 준다.
    df["feat_up_down_error_diff"] = df["feat_downstroke_error_max"] - df["feat_upstroke_error_max"]

    # 활성 라인(cap->PA, rod->PB)의 근사 질량수지 잔차. 밸브 오리피스식(gen.cpp의
    # delta_p_bar = SG*(Q_m3h/Kv_eff)^2)을 측정 가능한 압력만으로 역산한 1차 근사치이며,
    # 개별 밸브 양단이 아닌 펌프->실린더 전체 경로 손실을 뭉뚱그린 값이라 정밀 검증 필요.
    is_cap = df["target_chamber"] == "cap"
    active_kv = np.where(is_cap, VALVE_KV_MAX_M3_H["pa"], VALVE_KV_MAX_M3_H["pb"])
    active_pos = np.where(is_cap, df["valve_pos_pa"], df["valve_pos_pb"])
    cyl_pressure = np.where(is_cap, df["cyl_cap_pressure_bar"], df["cyl_rod_pressure_bar"])
    dp_bar = (df["pump_out_pressure_bar"] - cyl_pressure).clip(lower=0)
    kv_eff = active_kv * (active_pos / 100.0)
    expected_q_m3h = np.where(kv_eff > 0, kv_eff * np.sqrt(dp_bar / SPECIFIC_GRAVITY), 0.0)
    expected_q_lmin = expected_q_m3h * 1000.0 / 60.0
    df["feat_mass_balance_resid"] = df["flow_rate_L_min"] - expected_q_lmin

    # 실제 리턴라인 유량 기반 질량수지 (2026-07-16 gen.cpp 수정 이후 데이터셋에만 존재).
    # ext_leak는 누설분만큼 펌프유량 > 리턴유량으로 벌어지고, 나머지 클래스는 거의 0(정합성 확인됨).
    if "return_flow_L_min" in df.columns:
        df["feat_flow_return_diff"] = df["flow_rate_L_min"] - df["return_flow_L_min"]

    return df


def build(cmd_path, solver_path, out_path):
    cmd_df = load_command_source(cmd_path)
    solver_df = load_solver_output(solver_path)
    merged = merge_sources(cmd_df, solver_df)
    merged = apply_label_defaults(merged)
    merged = add_engineered_features(merged)
    merged = merged[FINAL_SCHEMA_COLUMNS + ENGINEERED_COLUMNS]
    merged.to_csv(out_path, index=False)
    print(f"{len(merged)}행, {len(merged.columns)}열 -> {out_path}")
    return merged


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cmd", required=True, help="input_timeseries.csv 또는 gen_scenarios.py scenario 파일")
    ap.add_argument("--solver", required=True, help="solver_output_timeseries.csv")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    build(args.cmd, args.solver, args.out)

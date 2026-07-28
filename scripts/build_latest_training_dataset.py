"""현재 gen_static.exe 출력으로 별도의 최신 학습 데이터 CSV를 만든다.

기존 ``final_training_dataset_v3.csv``는 수정하지 않는다. 이 모듈은 최신 솔버
출력(gen_output_latest.csv)과 동일 순서의 시나리오(scenarios.csv)를 청크 단위로
대조한 뒤, 모델 입력용 센서값과 학습 정답값을 하나의 새 CSV에 저장한다.
"""

import argparse
from math import pi
from pathlib import Path

import pandas as pd


# network.json에 정의된 리턴 배관 내경이다. 솔버는 속도(m/s)를 출력하므로,
# 단면적을 곱한 뒤 L/min으로 환산해야 리턴 유량 센서값이 된다.
C_RETURN_DIAMETER_M = 0.016


# 최종 학습 파일의 열 순서는 기존 v3와 호환되도록 유지한다. gt_ 접두어 열은
# 솔버 검증용 내부 정답이며 모델 입력에는 사용하지 않는다.
FINAL_COLUMNS = [
    "cycle_id", "cycle_second", "cycle_phase", "target_chamber", "temperature_c", "pump_rpm",
    "valve_cmd_pa", "valve_pos_pa", "valve_cmd_pb", "valve_pos_pb",
    "valve_cmd_at", "valve_pos_at", "valve_cmd_bt", "valve_pos_bt",
    "flow_rate_L_min", "return_flow_L_min", "pump_in_pressure_bar", "pump_out_pressure_bar",
    "cyl_cap_pressure_bar", "cyl_rod_pressure_bar", "pump_delta_pressure_bar",
    "pump_head_m", "hydraulic_power_kW", "max_velocity_m_s", "target_pressure_error_bar",
    "gt_N_VALVE_P_p", "gt_N_VALVE_A_p", "gt_N_VALVE_B_p", "gt_N_VALVE_T_p",
    "gt_Kv_V_DIR_PA", "gt_Kv_V_DIR_PB", "gt_Kv_V_DIR_AT", "gt_Kv_V_DIR_BT", "gt_Kv_V_RELIEF",
    "gt_loss_C_SUCTION", "gt_loss_C_PRESSURE", "gt_loss_C_A_LINE", "gt_loss_C_B_LINE", "gt_loss_C_RETURN",
    "gt_leak_mode", "gt_fault_location", "fault_class", "severity_level", "fault_severity",
    "onset_type", "is_anomaly", "target_pressure_bar", "relief_set_pressure_bar",
]

SCENARIO_COLUMNS = [
    "row_id", "cycle_id", "cycle_second", "target_chamber", "temperature_c", "pump_rpm",
    "cmd_pa", "cmd_pb", "cmd_at", "cmd_bt", "v_pa", "v_pb", "v_at", "v_bt",
    "target_pressure", "v_relief_set", "fault_class", "severity_level", "fault_severity",
    "onset_type", "is_anomaly", "gt_fault_location", "gt_leak_mode",
]

SOLVER_COLUMNS = [
    "row_id", "active_mode", "calculated_flow_rate_L_min", "hydraulic_power_kW", "pump_head_m",
    "pump_delta_pressure_bar", "target_pressure_error_bar", "N_PUMP_IN_p", "N_PUMP_OUT_p",
    "N_VALVE_P_p", "N_VALVE_A_p", "N_VALVE_B_p", "N_VALVE_T_p", "N_CYL_CAP_p", "N_CYL_ROD_p",
    "vel_C_RETURN", "max_active_velocity", "loss_C_SUCTION", "loss_C_PRESSURE", "loss_C_A_LINE",
    "loss_C_B_LINE", "loss_C_RETURN", "Kv_V_DIR_PA", "Kv_V_DIR_PB", "Kv_V_DIR_AT", "Kv_V_DIR_BT",
    "Kv_V_RELIEF",
]


def _detect_csv_encoding(path: Path) -> str:
    """PowerShell 리디렉션의 UTF-16 출력과 일반 UTF-8 CSV를 모두 읽는다."""
    with path.open("rb") as source:
        bom = source.read(3)
    if bom.startswith(b"\xff\xfe") or bom.startswith(b"\xfe\xff"):
        return "utf-16"
    return "utf-8-sig"


def _build_chunk(scenarios: pd.DataFrame, solver: pd.DataFrame) -> pd.DataFrame:
    """같은 row_id 청크 두 개를 최신 최종 학습 스키마로 변환한다."""
    if len(scenarios) != len(solver):
        raise ValueError(f"시나리오와 솔버 청크 행 수 불일치: {len(scenarios)} != {len(solver)}")
    if not scenarios["row_id"].equals(solver["row_id"]):
        raise ValueError("row_id 순서 또는 값이 일치하지 않아 안전하게 병합할 수 없습니다.")

    area_m2 = pi * (C_RETURN_DIAMETER_M / 2) ** 2
    result = pd.DataFrame({
        "cycle_id": scenarios["cycle_id"],
        "cycle_second": scenarios["cycle_second"],
        "cycle_phase": solver["active_mode"],
        "target_chamber": scenarios["target_chamber"],
        "temperature_c": scenarios["temperature_c"],
        "pump_rpm": scenarios["pump_rpm"],
        "valve_cmd_pa": scenarios["cmd_pa"], "valve_pos_pa": scenarios["v_pa"],
        "valve_cmd_pb": scenarios["cmd_pb"], "valve_pos_pb": scenarios["v_pb"],
        "valve_cmd_at": scenarios["cmd_at"], "valve_pos_at": scenarios["v_at"],
        "valve_cmd_bt": scenarios["cmd_bt"], "valve_pos_bt": scenarios["v_bt"],
        "flow_rate_L_min": solver["calculated_flow_rate_L_min"],
        "return_flow_L_min": solver["vel_C_RETURN"] * area_m2 * 60000.0,
        "pump_in_pressure_bar": solver["N_PUMP_IN_p"],
        "pump_out_pressure_bar": solver["N_PUMP_OUT_p"],
        "cyl_cap_pressure_bar": solver["N_CYL_CAP_p"],
        "cyl_rod_pressure_bar": solver["N_CYL_ROD_p"],
        "pump_delta_pressure_bar": solver["pump_delta_pressure_bar"],
        "pump_head_m": solver["pump_head_m"],
        "hydraulic_power_kW": solver["hydraulic_power_kW"],
        "max_velocity_m_s": solver["max_active_velocity"],
        "target_pressure_error_bar": solver["target_pressure_error_bar"],
        "gt_N_VALVE_P_p": solver["N_VALVE_P_p"], "gt_N_VALVE_A_p": solver["N_VALVE_A_p"],
        "gt_N_VALVE_B_p": solver["N_VALVE_B_p"], "gt_N_VALVE_T_p": solver["N_VALVE_T_p"],
        "gt_Kv_V_DIR_PA": solver["Kv_V_DIR_PA"], "gt_Kv_V_DIR_PB": solver["Kv_V_DIR_PB"],
        "gt_Kv_V_DIR_AT": solver["Kv_V_DIR_AT"], "gt_Kv_V_DIR_BT": solver["Kv_V_DIR_BT"],
        "gt_Kv_V_RELIEF": solver["Kv_V_RELIEF"],
        "gt_loss_C_SUCTION": solver["loss_C_SUCTION"], "gt_loss_C_PRESSURE": solver["loss_C_PRESSURE"],
        "gt_loss_C_A_LINE": solver["loss_C_A_LINE"], "gt_loss_C_B_LINE": solver["loss_C_B_LINE"],
        "gt_loss_C_RETURN": solver["loss_C_RETURN"],
        "gt_leak_mode": scenarios["gt_leak_mode"], "gt_fault_location": scenarios["gt_fault_location"],
        "fault_class": scenarios["fault_class"], "severity_level": scenarios["severity_level"],
        "fault_severity": scenarios["fault_severity"], "onset_type": scenarios["onset_type"],
        "is_anomaly": scenarios["is_anomaly"],
        "target_pressure_bar": scenarios["target_pressure"], "relief_set_pressure_bar": scenarios["v_relief_set"],
    })
    return result[FINAL_COLUMNS]


def build_latest_training_dataset(
    scenarios_path: Path,
    solver_path: Path,
    output_path: Path,
    chunksize: int = 200_000,
) -> int:
    """최신 솔버 출력과 시나리오를 대조해 새 최종 학습 CSV를 만들고 행 수를 반환한다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"기존 파일을 보호하기 위해 덮어쓰지 않습니다: {output_path}")

    total_rows = 0
    wrote_header = False
    with (
        pd.read_csv(
            scenarios_path,
            usecols=SCENARIO_COLUMNS,
            chunksize=chunksize,
            encoding=_detect_csv_encoding(scenarios_path),
            # scenarios.csv도 대용량 usecols 조합에서 동일한 C 파서 오류가 나므로
            # 솔버 출력과 같은 파서를 사용해 행 단위 정합성 검사를 유지한다.
            engine="python",
        ) as scenario_chunks,
        pd.read_csv(
            solver_path,
            usecols=SOLVER_COLUMNS,
            chunksize=chunksize,
            encoding=_detect_csv_encoding(solver_path),
            # 145만 행 최신 솔버 CSV에서 pandas C 파서의 usecols 내부 오류가 재현된다.
            # Python 파서는 전체 행·38열 구조를 끝까지 정상적으로 읽는 것을 확인했다.
            engine="python",
        ) as solver_chunks,
    ):
        for scenarios, solver in zip(scenario_chunks, solver_chunks, strict=True):
            result = _build_chunk(scenarios, solver)
            result.to_csv(output_path, index=False, mode="a", header=not wrote_header)
            wrote_header = True
            total_rows += len(result)

    if not wrote_header:
        raise ValueError("입력 파일에 데이터 행이 없습니다.")
    return total_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="최신 솔버 출력으로 별도의 최종 학습 CSV를 생성합니다.")
    parser.add_argument("--scenarios", type=Path, default=Path("dataset/scenarios.csv"))
    parser.add_argument("--solver", type=Path, default=Path("dataset/gen_output_latest.csv"))
    parser.add_argument("--out", type=Path, default=Path("dataset/final_training_dataset_latest.csv"))
    args = parser.parse_args()
    rows = build_latest_training_dataset(args.scenarios, args.solver, args.out)
    print(f"저장 완료: {args.out} ({rows:,}행, {len(FINAL_COLUMNS)}열)")


if __name__ == "__main__":
    main()

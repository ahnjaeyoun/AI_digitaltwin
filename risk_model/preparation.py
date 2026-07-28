"""원본 입력과 기존 솔버 출력을 검증·결합하고 모델 입력으로 변환합니다."""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import (
    CATEGORICAL_ENCODINGS,
    FEATURE_COLUMNS,
    NORMAL_TRAINING_PATH,
    REALTIME_INPUT_PATH,
    SOLVER_OUTPUT_PATH,
)


def convert_features(frame: pd.DataFrame) -> np.ndarray:
    """문자 운전 상태를 숫자로 바꾸고, 결측·무한값을 검사합니다."""
    missing = [column for column in FEATURE_COLUMNS if column not in frame]
    if missing:
        raise ValueError(f"필요한 모델 입력 열이 없습니다: {', '.join(missing)}")
    converted = pd.DataFrame(index=frame.index)
    for column in FEATURE_COLUMNS:
        values = frame[column].map(CATEGORICAL_ENCODINGS[column]) if column in CATEGORICAL_ENCODINGS else pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any():
            raise ValueError(f"{column} 열에 결측값 또는 지원하지 않는 값이 있습니다.")
        converted[column] = values
    result = converted.to_numpy(dtype=np.float32)
    if not np.isfinite(result).all():
        raise ValueError("모델 입력에 무한값이 있습니다.")
    return result


def _require_columns(frame: pd.DataFrame, required: tuple[str, ...], label: str) -> None:
    """검증에 필요한 열이 빠졌다면 어떤 파일에서 빠졌는지 알려줍니다."""
    missing = [column for column in required if column not in frame]
    if missing:
        raise ValueError(f"{label}에 필요한 열이 없습니다: {', '.join(missing)}")


def _validate_alignment(source: pd.DataFrame, solver: pd.DataFrame) -> None:
    """원본 한 행과 솔버 한 행이 정확히 같은 시점을 나타내는지 검사합니다."""
    source_keys = ("sample_index", "cycle_id", "timestamp")
    solver_keys = (
        "input_row_index",
        "cycle_id",
        "timestamp",
        "status",
        "solver_warning",
        "contains_inf_or_nan",
    )
    _require_columns(source, source_keys, "원본 입력")
    _require_columns(solver, solver_keys, "솔버 출력")

    if len(source) != len(solver):
        raise ValueError(
            f"원본 입력과 솔버 출력의 행 수가 다릅니다: {len(source)}행, {len(solver)}행"
        )

    source_row = pd.to_numeric(source["sample_index"], errors="raise").to_numpy()
    solver_row = pd.to_numeric(solver["input_row_index"], errors="raise").to_numpy()
    if not np.array_equal(source_row, solver_row):
        raise ValueError("sample_index와 input_row_index가 일치하지 않습니다.")

    source_cycle = pd.to_numeric(source["cycle_id"], errors="raise").to_numpy()
    solver_cycle = pd.to_numeric(solver["cycle_id"], errors="raise").to_numpy()
    if not np.array_equal(source_cycle, solver_cycle):
        raise ValueError("원본 입력과 솔버 출력의 cycle_id가 일치하지 않습니다.")

    source_time = source["timestamp"].astype(str).to_numpy()
    solver_time = solver["timestamp"].astype(str).to_numpy()
    if not np.array_equal(source_time, solver_time):
        raise ValueError("원본 입력과 솔버 출력의 timestamp가 일치하지 않습니다.")

    if not solver["status"].astype(str).str.lower().eq("ok").all():
        raise ValueError("솔버 출력 status에 ok가 아닌 행이 있습니다.")
    # solver_warning은 목표 압력과 계산 부하 압력의 편차가 0이 아닐 때도
    # 표시됩니다. 계산 자체가 성공했고 유한한 값이라면 정상 운전의 작은
    # 압력 편차도 학습해야 하므로, 이 열은 확인 정보로만 남기고 행을 버리지 않습니다.
    if not solver["contains_inf_or_nan"].astype(str).str.lower().eq("no").all():
        raise ValueError("솔버 출력 contains_inf_or_nan에 오류 행이 있습니다.")


def _combine(source: pd.DataFrame, solver: pd.DataFrame) -> pd.DataFrame:
    """검증이 끝난 두 표에서 기존 모델 입력 26개를 같은 순서로 만듭니다."""
    _validate_alignment(source, solver)
    source = source.reset_index(drop=True)
    solver = solver.reset_index(drop=True)

    source_required = (
        "cycle_second",
        "cycle_phase",
        "Press.target_chamber",
        "Fluid.temperature_c",
        "Cell.PUMP_01.rpm",
        "Cell.V_DIR_PA.opening_percent",
        "Cell.V_DIR_PB.opening_percent",
        "Cell.V_DIR_AT.opening_percent",
        "Cell.V_DIR_BT.opening_percent",
        "Press.target_pressure_bar_g",
        "Cell.V_RELIEF.set_pressure_bar_g",
    )
    solver_required = (
        "calculated_flow_rate_L_min",
        "N_PUMP_IN_pressure_bar_g",
        "N_PUMP_OUT_pressure_bar_g",
        "N_CYL_CAP_pressure_bar_g",
        "N_CYL_ROD_pressure_bar_g",
        "pump_delta_pressure_bar",
        "pump_head_m",
        "hydraulic_power_kW",
        "max_active_velocity_m_s",
        "target_pressure_error_bar",
    )
    _require_columns(source, source_required, "원본 입력")
    _require_columns(solver, solver_required, "솔버 출력")

    rows = pd.DataFrame(
        {
            "cycle_second": source["cycle_second"],
            "cycle_phase": source["cycle_phase"],
            "target_chamber": source["Press.target_chamber"],
            "temperature_c": source["Fluid.temperature_c"],
            "pump_rpm": source["Cell.PUMP_01.rpm"],
            "valve_cmd_pa": source["Cell.V_DIR_PA.opening_percent"],
            "valve_pos_pa": source["Cell.V_DIR_PA.opening_percent"],
            "valve_cmd_pb": source["Cell.V_DIR_PB.opening_percent"],
            "valve_pos_pb": source["Cell.V_DIR_PB.opening_percent"],
            "valve_cmd_at": source["Cell.V_DIR_AT.opening_percent"],
            "valve_pos_at": source["Cell.V_DIR_AT.opening_percent"],
            "valve_cmd_bt": source["Cell.V_DIR_BT.opening_percent"],
            "valve_pos_bt": source["Cell.V_DIR_BT.opening_percent"],
            "flow_rate_L_min": solver["calculated_flow_rate_L_min"],
            "return_flow_L_min": solver["calculated_flow_rate_L_min"],
            "pump_in_pressure_bar": solver["N_PUMP_IN_pressure_bar_g"],
            "pump_out_pressure_bar": solver["N_PUMP_OUT_pressure_bar_g"],
            "cyl_cap_pressure_bar": solver["N_CYL_CAP_pressure_bar_g"],
            "cyl_rod_pressure_bar": solver["N_CYL_ROD_pressure_bar_g"],
            "pump_delta_pressure_bar": solver["pump_delta_pressure_bar"],
            "pump_head_m": solver["pump_head_m"],
            "hydraulic_power_kW": solver["hydraulic_power_kW"],
            "max_velocity_m_s": solver["max_active_velocity_m_s"],
            "target_pressure_error_bar": solver["target_pressure_error_bar"],
            "target_pressure_bar": source["Press.target_pressure_bar_g"],
            "relief_set_pressure_bar": source["Cell.V_RELIEF.set_pressure_bar_g"],
        }
    )
    return rows.loc[:, FEATURE_COLUMNS]


def prepare_training_data() -> tuple[pd.DataFrame, pd.DataFrame, np.ndarray]:
    """기존 원본·솔버 CSV를 결합하여 정상 학습 CSV와 숫자 배열을 만듭니다."""
    source = pd.read_csv(REALTIME_INPUT_PATH, encoding="utf-8-sig")
    solver = pd.read_csv(SOLVER_OUTPUT_PATH, encoding="utf-8-sig")
    rows = _combine(source, solver)
    training = rows.copy()
    training.insert(0, "fault_class", "normal")
    training.insert(0, "cycle_id", source["cycle_id"].astype(int))
    training.insert(0, "trajectory_id", "normal_3000cycles")
    training.to_csv(NORMAL_TRAINING_PATH, index=False, encoding="utf-8-sig")
    return source, rows, convert_features(training)

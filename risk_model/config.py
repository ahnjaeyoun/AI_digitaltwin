"""위험도 모델에서 한곳에서만 바꾸는 경로와 기본 설정입니다."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIRECTORY = PROJECT_ROOT / "dataset"
OUTPUT_DIRECTORY = Path(__file__).resolve().parent / "outputs"

# 이미 C++ 솔버 계산이 끝난 3,000사이클 정상 데이터와 기존 오류 검증 데이터입니다.
REALTIME_INPUT_PATH = DATASET_DIRECTORY / "realtime_input_3000cycles_normal_only.csv"
SOLVER_OUTPUT_PATH = DATASET_DIRECTORY / "realtime_solver_output_3000cycles_normal_only.csv"
NORMAL_TRAINING_PATH = DATASET_DIRECTORY / "realtime_training_3000cycles_normal_only.csv"
FAULT_TEST_PATH = DATASET_DIRECTORY / "traj_dataset_test.csv"

# 도커에서는 이 파일 하나만 읽으면 되도록 모델 가중치와 정규화 기준을 함께 저장합니다.
MODEL_PATH = OUTPUT_DIRECTORY / "risk_model.pt"
EVALUATION_PATH = OUTPUT_DIRECTORY / "risk_evaluation_results.json"
RISK_RESULT_PATH = OUTPUT_DIRECTORY / "realtime_risk_3000cycles_normal_only.csv"

# 학습과 실시간 위험도 계산에 공통으로 사용하는 센서·솔버 입력 26개입니다.
FEATURE_COLUMNS = (
    "cycle_second", "cycle_phase", "target_chamber", "temperature_c", "pump_rpm",
    "valve_cmd_pa", "valve_pos_pa", "valve_cmd_pb", "valve_pos_pb",
    "valve_cmd_at", "valve_pos_at", "valve_cmd_bt", "valve_pos_bt",
    "flow_rate_L_min", "return_flow_L_min", "pump_in_pressure_bar",
    "pump_out_pressure_bar", "cyl_cap_pressure_bar", "cyl_rod_pressure_bar",
    "pump_delta_pressure_bar", "pump_head_m", "hydraulic_power_kW",
    "max_velocity_m_s", "target_pressure_error_bar", "target_pressure_bar",
    "relief_set_pressure_bar",
)

CATEGORICAL_ENCODINGS = {
    "cycle_phase": {"downstroke": 0.0, "pressure_hold": 1.0, "upstroke": 2.0},
    "target_chamber": {"cap": 0.0, "rod": 1.0},
}

MAX_SEQUENCE_ROWS = 150
SEQUENCE_LENGTHS = tuple(range(1, MAX_SEQUENCE_ROWS + 1))
LENGTH_BUCKETS = ((1, 14), (15, 59), (60, 149), (150, 150))
TRAIN_SEQUENCE_STRIDE = 100
CALIBRATION_SEQUENCE_STRIDE = 20

LSTM_HIDDEN_SIZE = 64
LSTM_LATENT_SIZE = 32
TRAIN_EPOCHS = 15
TRAIN_BATCH_SIZE = 128
RANDOM_SEED = 42
RISK_ALERT_THRESHOLD_PERCENT = 60.0

#!/usr/bin/env python3
"""Evaluate predictive-maintenance risk from live Solver MQTT state messages.

Pipeline:
    hydraulic-press/unity/state (raw input + Solver result)
        -> 26 risk-model features
        -> per-line rolling window (1..150 rows)
        -> LSTM autoencoder reconstruction risk
        -> hydraulic-press/risk/result

The saved ``risk_model.pt`` contains the LSTM weights, StandardScaler, feature
order, and normal p95/p99 reconstruction-error calibrations for every supported
window length.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import signal
import socket
import sys
import threading
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import paho.mqtt.client as mqtt
import torch
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence

from inference_timescale_store import InferenceTimescaleStore


APP_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL = APP_DIR / "risk_model.pt"
DEFAULT_SETTINGS = APP_DIR / "mqtt_settings.json"
DEFAULT_SOURCE_TOPIC = "hydraulic-press/unity/state"
DEFAULT_OUTPUT_TOPIC = "hydraulic-press/risk/result"
DEFAULT_ALERT_THRESHOLD_PERCENT = 60.0
CRITICAL_THRESHOLD_PERCENT = 80.0

PHASE_ENCODING = {
    "downstroke": 0.0,
    "하강": 0.0,
    "pressure_hold": 1.0,
    "hold": 1.0,
    "압력유지": 1.0,
    "upstroke": 2.0,
    "상승": 2.0,
}

CHAMBER_ENCODING = {
    "cap": 0.0,
    "rod": 1.0,
}


def load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig") as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError(f"MQTT 설정 최상위 값은 객체여야 합니다: {path}")
    return document


def parse_args() -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", type=Path, default=DEFAULT_SETTINGS)
    pre_args, _ = pre_parser.parse_known_args()
    settings = load_settings(pre_args.config.expanduser().resolve())
    broker = settings.get("broker", {})
    topics = settings.get("topics", {})
    risk = settings.get("risk", {})
    timescaledb = settings.get("timescaledb", {})
    if not isinstance(broker, dict):
        broker = {}
    if not isinstance(topics, dict):
        topics = {}
    if not isinstance(risk, dict):
        risk = {}
    if not isinstance(timescaledb, dict):
        timescaledb = {}

    parser = argparse.ArgumentParser(
        description="Solver MQTT 상태를 구독하고 예지보전 위험도를 발행합니다."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument(
        "--host",
        default=os.getenv("MQTT_HOST", broker.get("host", "localhost")),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MQTT_PORT", broker.get("port", 1883))),
    )
    parser.add_argument(
        "--source-topic",
        default=os.getenv(
            "MQTT_SOLVER_STATE_TOPIC",
            risk.get("source_topic", topics.get("unity", DEFAULT_SOURCE_TOPIC)),
        ),
    )
    parser.add_argument(
        "--output-topic",
        default=os.getenv(
            "MQTT_RISK_TOPIC",
            risk.get("output_topic", topics.get("risk", DEFAULT_OUTPUT_TOPIC)),
        ),
    )
    parser.add_argument(
        "--qos",
        type=int,
        choices=(0, 1, 2),
        default=int(risk.get("qos", broker.get("qos", 1))),
    )
    parser.add_argument(
        "--retain",
        action="store_true",
        default=bool(risk.get("retain", False)),
    )
    parser.add_argument(
        "--keepalive",
        type=int,
        default=int(broker.get("keepalive", 60)),
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument(
        "--client-id",
        default=f"press-risk-{socket.gethostname()}-{os.getpid()}",
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument(
        "--alert-threshold",
        type=float,
        default=float(
            risk.get("alert_threshold_percent", DEFAULT_ALERT_THRESHOLD_PERCENT)
        ),
    )
    parser.add_argument(
        "--log-every",
        type=int,
        default=int(risk.get("log_every", 1)),
    )
    parser.add_argument(
        "--timescale-dsn",
        default=os.getenv("TIMESCALE_DSN", ""),
        help="추론 결과를 저장할 TimescaleDB 연결 문자열",
    )
    parser.add_argument(
        "--timescale-schema",
        default=os.getenv(
            "TIMESCALE_SCHEMA",
            str(timescaledb.get("schema", "public")),
        ),
    )
    parser.add_argument(
        "--timescale-required",
        action="store_true",
        default=bool(timescaledb.get("required", False)),
    )
    parser.add_argument(
        "--no-timescale-initialize",
        action="store_false",
        dest="timescale_initialize",
        default=bool(timescaledb.get("initialize", True)),
    )
    parser.add_argument(
        "--timescale-connect-timeout",
        type=int,
        default=int(timescaledb.get("connect_timeout", 5)),
    )
    parser.add_argument(
        "--timescale-flush-timeout",
        type=float,
        default=float(timescaledb.get("flush_timeout", 30.0)),
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        help="MQTT 대신 solver_state JSON 또는 JSONL을 읽는 오프라인 검증 모드",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="오프라인 위험도 결과를 JSONL로 저장",
    )
    return parser.parse_args()


def finite_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 값이 숫자가 아닙니다: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 값이 유한수가 아닙니다: {value!r}")
    return number


def first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def normalize_phase(message: dict[str, Any], input_row: dict[str, Any]) -> tuple[str, float]:
    candidates = (
        message.get("cycle_phase"),
        message.get("active_mode"),
        input_row.get("cycle_phase"),
        input_row.get("System.active_mode"),
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        lowered = text.lower()
        if lowered in PHASE_ENCODING:
            return lowered, PHASE_ENCODING[lowered]
        if text in PHASE_ENCODING:
            return text, PHASE_ENCODING[text]
    raise ValueError(f"지원하지 않는 cycle_phase입니다: {candidates!r}")


def normalize_chamber(input_row: dict[str, Any], result_row: dict[str, Any]) -> tuple[str, float]:
    value = first_value(
        input_row.get("Press.target_chamber"),
        result_row.get("Press_target_chamber"),
    )
    chamber = str(value or "").strip().lower()
    if chamber not in CHAMBER_ENCODING:
        raise ValueError(f"지원하지 않는 target_chamber입니다: {value!r}")
    return chamber, CHAMBER_ENCODING[chamber]


def contains_invalid_solver_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    return normalized in {"yes", "true", "1"}


def calibrated_risk_score(
    normalized_excess_ratio: float,
    alert_threshold_percent: float,
) -> float:
    """Map normal-error excess to a smooth 0..100 score.

    The original training helper clipped every error at or above p99 directly
    to 100, which made materially different high-risk states indistinguishable.
    This curve preserves the original alert boundary: when the excess ratio is
    ``alert_threshold / 100``, the displayed score is exactly that threshold.
    Beyond p99 it approaches 100 gradually instead of hard-clipping.
    """
    ratio = max(0.0, float(normalized_excess_ratio))
    threshold_fraction = float(
        np.clip(alert_threshold_percent / 100.0, 1e-6, 1.0 - 1e-6)
    )
    curve_rate = -math.log(1.0 - threshold_fraction) / threshold_fraction
    return float(np.clip((1.0 - math.exp(-curve_rate * ratio)) * 100.0, 0.0, 100.0))


class LSTMAutoencoder(nn.Module):
    """Architecture used when the bundled checkpoint was trained."""

    def __init__(self, input_size: int, hidden_size: int, latent_size: int):
        super().__init__()
        self.encoder = nn.LSTM(input_size, hidden_size, batch_first=True)
        self.to_latent = nn.Linear(hidden_size, latent_size)
        self.decoder = nn.LSTM(latent_size, hidden_size, batch_first=True)
        self.output = nn.Linear(hidden_size, input_size)

    def forward(self, values: torch.Tensor, lengths: torch.Tensor) -> torch.Tensor:
        packed = pack_padded_sequence(
            values,
            lengths.cpu(),
            batch_first=True,
            enforce_sorted=False,
        )
        _, (hidden, _) = self.encoder(packed)
        latent = self.to_latent(hidden[-1])
        repeated = latent.unsqueeze(1).expand(-1, values.size(1), -1)
        decoded, _ = self.decoder(repeated)
        return self.output(decoded)


class RiskRuntime:
    """Loads the checkpoint and evaluates one already ordered sequence."""

    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path.expanduser().resolve()
        if not self.model_path.is_file():
            raise FileNotFoundError(f"위험도 모델이 없습니다: {self.model_path}")

        # This project-owned checkpoint includes an sklearn StandardScaler, so
        # weights_only=False is required. Do not use this loader for untrusted files.
        artifact = torch.load(
            self.model_path,
            map_location="cpu",
            weights_only=False,
        )
        if artifact.get("model_type") != "lstm_autoencoder":
            raise ValueError("LSTM Autoencoder 위험도 모델 파일이 아닙니다")

        self.columns = tuple(str(column) for column in artifact["columns"])
        self.max_sequence_rows = int(artifact["max_sequence_rows"])
        self.scaler = artifact["scaler"]
        self.calibrations = {
            int(length): {
                "p95": float(values["p95"]),
                "p99": float(values["p99"]),
            }
            for length, values in artifact["calibrations"].items()
        }
        self.network = LSTMAutoencoder(
            input_size=int(artifact["input_size"]),
            hidden_size=int(artifact["hidden_size"]),
            latent_size=int(artifact["latent_size"]),
        )
        self.network.load_state_dict(artifact["state"])
        self.network.eval()

        if len(self.columns) != int(artifact["input_size"]):
            raise ValueError("체크포인트 columns와 input_size가 일치하지 않습니다")
        missing_calibrations = [
            length
            for length in range(1, self.max_sequence_rows + 1)
            if length not in self.calibrations
        ]
        if missing_calibrations:
            raise ValueError(
                f"시퀀스 길이별 보정값이 없습니다: {missing_calibrations[:5]}"
            )

    def make_feature_row(
        self,
        message: dict[str, Any],
    ) -> tuple[np.ndarray, dict[str, Any]]:
        input_row = message.get("input")
        result_row = message.get("result")
        if not isinstance(input_row, dict) or not isinstance(result_row, dict):
            raise ValueError("input 또는 result 객체가 없습니다")

        status = str(result_row.get("status", "")).strip().lower()
        if status != "ok":
            raise ValueError(f"Solver status가 ok가 아닙니다: {status!r}")
        if contains_invalid_solver_value(result_row.get("contains_inf_or_nan")):
            raise ValueError("Solver 결과에 NaN 또는 무한대가 있습니다")

        phase_name, phase_value = normalize_phase(message, input_row)
        chamber_name, chamber_value = normalize_chamber(input_row, result_row)
        flow = finite_float(
            result_row.get("calculated_flow_rate_L_min"),
            "calculated_flow_rate_L_min",
        )
        valve_pa = finite_float(
            input_row.get("Cell.V_DIR_PA.opening_percent"),
            "Cell.V_DIR_PA.opening_percent",
        )
        valve_pb = finite_float(
            input_row.get("Cell.V_DIR_PB.opening_percent"),
            "Cell.V_DIR_PB.opening_percent",
        )
        valve_at = finite_float(
            input_row.get("Cell.V_DIR_AT.opening_percent"),
            "Cell.V_DIR_AT.opening_percent",
        )
        valve_bt = finite_float(
            input_row.get("Cell.V_DIR_BT.opening_percent"),
            "Cell.V_DIR_BT.opening_percent",
        )

        features = {
            "cycle_second": finite_float(
                first_value(message.get("cycle_second"), input_row.get("cycle_second")),
                "cycle_second",
            ),
            "cycle_phase": phase_value,
            "target_chamber": chamber_value,
            "temperature_c": finite_float(
                input_row.get("Fluid.temperature_c"),
                "Fluid.temperature_c",
            ),
            "pump_rpm": finite_float(
                first_value(
                    result_row.get("pump_rpm"),
                    input_row.get("Cell.PUMP_01.rpm"),
                ),
                "pump_rpm",
            ),
            "valve_cmd_pa": valve_pa,
            "valve_pos_pa": valve_pa,
            "valve_cmd_pb": valve_pb,
            "valve_pos_pb": valve_pb,
            "valve_cmd_at": valve_at,
            "valve_pos_at": valve_at,
            "valve_cmd_bt": valve_bt,
            "valve_pos_bt": valve_bt,
            "flow_rate_L_min": flow,
            "return_flow_L_min": flow,
            "pump_in_pressure_bar": finite_float(
                result_row.get("N_PUMP_IN_pressure_bar_g"),
                "N_PUMP_IN_pressure_bar_g",
            ),
            "pump_out_pressure_bar": finite_float(
                result_row.get("N_PUMP_OUT_pressure_bar_g"),
                "N_PUMP_OUT_pressure_bar_g",
            ),
            "cyl_cap_pressure_bar": finite_float(
                result_row.get("N_CYL_CAP_pressure_bar_g"),
                "N_CYL_CAP_pressure_bar_g",
            ),
            "cyl_rod_pressure_bar": finite_float(
                result_row.get("N_CYL_ROD_pressure_bar_g"),
                "N_CYL_ROD_pressure_bar_g",
            ),
            "pump_delta_pressure_bar": finite_float(
                result_row.get("pump_delta_pressure_bar"),
                "pump_delta_pressure_bar",
            ),
            "pump_head_m": finite_float(
                result_row.get("pump_head_m"),
                "pump_head_m",
            ),
            "hydraulic_power_kW": finite_float(
                result_row.get("hydraulic_power_kW"),
                "hydraulic_power_kW",
            ),
            "max_velocity_m_s": finite_float(
                result_row.get("max_active_velocity_m_s"),
                "max_active_velocity_m_s",
            ),
            "target_pressure_error_bar": finite_float(
                result_row.get("target_pressure_error_bar"),
                "target_pressure_error_bar",
            ),
            "target_pressure_bar": finite_float(
                first_value(
                    result_row.get("Press_target_pressure_bar_g"),
                    input_row.get("Press.target_pressure_bar_g"),
                ),
                "target_pressure_bar",
            ),
            "relief_set_pressure_bar": finite_float(
                input_row.get("Cell.V_RELIEF.set_pressure_bar_g"),
                "Cell.V_RELIEF.set_pressure_bar_g",
            ),
        }
        missing = [column for column in self.columns if column not in features]
        if missing:
            raise ValueError(f"모델 입력 매핑이 없습니다: {', '.join(missing)}")

        row = np.asarray([features[column] for column in self.columns], dtype=np.float32)
        if row.shape != (len(self.columns),) or not np.isfinite(row).all():
            raise ValueError("모델 입력 행에 유효하지 않은 값이 있습니다")
        return row, {
            "phase_name": phase_name,
            "chamber_name": chamber_name,
        }

    def evaluate(self, rows: deque[np.ndarray]) -> dict[str, float | int]:
        values = np.asarray(rows, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != len(self.columns):
            raise ValueError(
                f"모델 입력은 [행, {len(self.columns)}] 배열이어야 합니다"
            )

        scaled = self.scaler.transform(values).astype(np.float32)
        batch = torch.as_tensor(scaled, dtype=torch.float32).unsqueeze(0)
        lengths = torch.as_tensor([len(values)], dtype=torch.long)
        with torch.no_grad():
            rebuilt = self.network(batch, lengths)
            reconstruction_error = float(torch.mean((rebuilt - batch) ** 2).item())

        calibration = self.calibrations[len(values)]
        p95 = calibration["p95"]
        p99 = calibration["p99"]
        denominator = max(p99 - p95, 1e-12)
        normalized_excess_ratio = (reconstruction_error - p95) / denominator
        return {
            "normalized_excess_ratio": normalized_excess_ratio,
            "linear_score_percent": float(
                np.clip(normalized_excess_ratio * 100.0, 0.0, 100.0)
            ),
            "reconstruction_error": reconstruction_error,
            "window_rows": len(values),
            "history_coverage_percent": len(values) / self.max_sequence_rows * 100.0,
            "p95": p95,
            "p99": p99,
        }


def maintenance_summary(
    score_percent: float,
    history_coverage_percent: float,
    alert_threshold_percent: float,
) -> dict[str, str]:
    if score_percent >= CRITICAL_THRESHOLD_PERCENT:
        return {
            "level": "critical",
            "status": "즉시 점검",
            "recommendation": (
                "정상 시계열 대비 위험도가 매우 높습니다. 안전 조건을 확인하고 "
                "펌프·밸브·압력 계통을 우선 점검하세요."
            ),
        }
    if score_percent >= alert_threshold_percent:
        return {
            "level": "caution",
            "status": "점검 권장",
            "recommendation": (
                "위험도 알람 기준을 초과했습니다. 추세를 확인하고 계획 정비를 "
                "준비하세요."
            ),
        }
    if history_coverage_percent < 100.0:
        return {
            "level": "normal",
            "status": "이력 축적 중",
            "recommendation": (
                "현재 위험도는 정상 범위입니다. 150행 이력이 확보될 때까지 "
                "계속 모니터링하세요."
            ),
        }
    return {
        "level": "normal",
        "status": "정상",
        "recommendation": "현재 위험도는 정상 범위입니다.",
    }


class RiskPipeline:
    """Maintains independent rolling histories for every run and line."""

    def __init__(
        self,
        runtime: RiskRuntime,
        alert_threshold_percent: float,
        max_groups: int = 64,
    ) -> None:
        self.runtime = runtime
        self.alert_threshold_percent = float(alert_threshold_percent)
        self.max_groups = max_groups
        self.histories: OrderedDict[
            tuple[str, str], deque[np.ndarray]
        ] = OrderedDict()
        self.seen_message_ids: set[str] = set()
        self.seen_order: deque[str] = deque()

    def process(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("schema") != "hydraulic-press.unity.v1":
            raise ValueError(f"지원하지 않는 source schema: {message.get('schema')!r}")
        if message.get("type") != "solver_state":
            raise ValueError(f"지원하지 않는 source type: {message.get('type')!r}")

        source_message_id = str(message.get("message_id", "")).strip()
        if source_message_id and source_message_id in self.seen_message_ids:
            return None

        run_id = str(message.get("run_id", "unknown"))
        line_number = int(message.get("line_number", 7))
        if not 1 <= line_number <= 16:
            raise ValueError(f"line_number는 1~16이어야 합니다: {line_number}")
        row, context = self.runtime.make_feature_row(message)

        history_key = (run_id, str(line_number))
        history = self.histories.get(history_key)
        if history is None:
            history = deque(maxlen=self.runtime.max_sequence_rows)
            self.histories[history_key] = history
        else:
            self.histories.move_to_end(history_key)
        history.append(row)
        while len(self.histories) > self.max_groups:
            self.histories.popitem(last=False)

        risk = self.runtime.evaluate(history)
        normalized_excess_ratio = float(risk["normalized_excess_ratio"])
        score_percent = calibrated_risk_score(
            normalized_excess_ratio,
            self.alert_threshold_percent,
        )
        alert = normalized_excess_ratio >= self.alert_threshold_percent / 100.0
        maintenance = maintenance_summary(
            score_percent,
            float(risk["history_coverage_percent"]),
            self.alert_threshold_percent,
        )

        if source_message_id:
            self.seen_message_ids.add(source_message_id)
            self.seen_order.append(source_message_id)
            if len(self.seen_order) > 5000:
                expired = self.seen_order.popleft()
                self.seen_message_ids.discard(expired)

        input_row_index = message.get("input_row_index", "")
        cycle_id = message.get("cycle_id", message.get("input", {}).get("cycle_id", ""))
        return {
            "schema": "hydraulic-press.risk.v1",
            "type": "risk_inference",
            "run_id": run_id,
            "message_id": f"{run_id}:risk:{line_number}:{input_row_index}",
            "source_message_id": source_message_id,
            "input_row_index": input_row_index,
            "published_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "line_number": line_number,
            "cycle_id": cycle_id,
            "cycle_second": message.get(
                "cycle_second",
                message.get("input", {}).get("cycle_second", 0),
            ),
            "cycle_phase": message.get("cycle_phase", context["phase_name"]),
            "status": maintenance["level"],
            "model": {
                "model_type": "lstm_autoencoder",
                "input_size": len(self.runtime.columns),
                "max_sequence_rows": self.runtime.max_sequence_rows,
                "alert_threshold_percent": self.alert_threshold_percent,
            },
            "risk": {
                "score_percent": round(score_percent, 4),
                "linear_score_percent": round(
                    float(risk["linear_score_percent"]),
                    4,
                ),
                "reconstruction_error": round(
                    float(risk["reconstruction_error"]),
                    8,
                ),
                "alert": alert,
                "window_rows": int(risk["window_rows"]),
                "history_coverage_percent": round(
                    float(risk["history_coverage_percent"]),
                    2,
                ),
                "p95": round(float(risk["p95"]), 8),
                "p99": round(float(risk["p99"]), 8),
            },
            "maintenance": maintenance,
        }


def read_offline_messages(path: Path) -> list[dict[str, Any]]:
    text = path.expanduser().resolve().read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith("["):
        loaded = json.loads(text)
        if not isinstance(loaded, list):
            raise ValueError("오프라인 JSON 배열 형식이 올바르지 않습니다")
        return loaded
    if "\n" in text.strip():
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return [json.loads(text)]


def run_offline(args: argparse.Namespace, pipeline: RiskPipeline) -> int:
    assert args.input_json is not None
    results = [
        result
        for message in read_offline_messages(args.input_json)
        if (result := pipeline.process(message)) is not None
    ]
    output_text = "".join(
        json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n"
        for result in results
    )
    if args.output_json:
        output_path = args.output_json.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text, encoding="utf-8")
        print(f"오프라인 위험도 결과: {output_path} ({len(results)}건)")
    else:
        print(output_text, end="")
    return 0


def run_mqtt(
    args: argparse.Namespace,
    pipeline: RiskPipeline,
    store: InferenceTimescaleStore | None = None,
) -> int:
    if args.source_topic == args.output_topic:
        raise SystemExit("source topic과 output topic은 달라야 합니다")

    inbox: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
    connected = threading.Event()
    connection_finished = threading.Event()
    connection_error: list[str] = []
    stop_event = threading.Event()
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
        protocol=mqtt.MQTTv311,
    )

    def on_connect(
        active_client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        del userdata, flags, properties
        connection_finished.set()
        if reason_code != 0:
            connection_error.append(str(reason_code))
            return
        result, _ = active_client.subscribe(args.source_topic, qos=args.qos)
        if result != mqtt.MQTT_ERR_SUCCESS:
            connection_error.append(mqtt.error_string(result))
            return
        connected.set()
        print(
            f"MQTT 구독 완료: 주제={args.source_topic!r}, QoS={args.qos}",
            flush=True,
        )

    def on_message(
        active_client: mqtt.Client,
        userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        del active_client, userdata
        try:
            inbox.put(
                json.loads(message.payload.decode("utf-8")),
                timeout=1.0,
            )
        except Exception as exc:
            print(f"잘못된 Solver 상태 메시지를 폐기했습니다: {exc}", file=sys.stderr)

    client.on_connect = on_connect
    client.on_message = on_message

    def request_stop(signum: int, frame: Any) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    print(f"MQTT 연결 중: mqtt://{args.host}:{args.port} ...", flush=True)
    client.connect_async(args.host, args.port, keepalive=args.keepalive)
    client.loop_start()
    try:
        if not connection_finished.wait(args.connect_timeout):
            raise TimeoutError("MQTT 연결 시간이 초과되었습니다")
        if not connected.wait(args.connect_timeout):
            reason = connection_error[0] if connection_error else "unknown"
            raise ConnectionError(f"MQTT 연결 또는 구독 실패: {reason}")

        processed = 0
        while not stop_event.is_set():
            try:
                message = inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = pipeline.process(message)
                if result is None:
                    continue
                encoded = json.dumps(
                    result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                publish = client.publish(
                    args.output_topic,
                    encoded,
                    qos=args.qos,
                    retain=args.retain,
                )
                if publish.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError(f"MQTT 발행 실패: {mqtt.error_string(publish.rc)}")
                publish.wait_for_publish(timeout=10.0)
                if not publish.is_published():
                    raise TimeoutError("위험도 결과 발행 확인 시간이 초과되었습니다")
                if store is not None:
                    store.add_result(result)
                processed += 1
                if processed == 1 or processed % max(1, args.log_every) == 0:
                    risk = result["risk"]
                    print(
                        "[RISK_송신완료] "
                        f"송신행={processed} "
                        f"라인={result['line_number']} "
                        f"사이클ID={result['cycle_id']} "
                        f"risk={risk['score_percent']:.2f}% "
                        f"window={risk['window_rows']}/"
                        f"{result['model']['max_sequence_rows']} "
                        f"status={result['status']} "
                        f"주제={args.output_topic!r}",
                        flush=True,
                    )
            except Exception as exc:
                print(f"위험도 처리 실패: {exc}", file=sys.stderr, flush=True)
        return 0
    finally:
        if store is not None:
            store.drain(args.timescale_flush_timeout)
            store.close()
        client.disconnect()
        client.loop_stop()


def main() -> int:
    args = parse_args()
    if not 0.0 <= args.alert_threshold <= 100.0:
        raise SystemExit("--alert-threshold는 0~100이어야 합니다")
    runtime = RiskRuntime(args.model)
    pipeline = RiskPipeline(runtime, args.alert_threshold)
    print(
        "위험도 모델 로드 완료: "
        f"{runtime.model_path}, features={len(runtime.columns)}, "
        f"max_window={runtime.max_sequence_rows}, "
        f"alert={args.alert_threshold:.1f}%",
        flush=True,
    )
    if args.input_json:
        return run_offline(args, pipeline)
    store = None
    if args.timescale_dsn.strip():
        store = InferenceTimescaleStore(
            args.timescale_dsn,
            "risk",
            args.timescale_schema,
            required=args.timescale_required,
            initialize=args.timescale_initialize,
            connect_timeout=args.timescale_connect_timeout,
            log_every=args.log_every,
        )
        store.start()
    return run_mqtt(args, pipeline, store)


if __name__ == "__main__":
    raise SystemExit(main())

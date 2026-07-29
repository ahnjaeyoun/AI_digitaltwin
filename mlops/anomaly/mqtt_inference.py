#!/usr/bin/env python3
"""Run the standalone press autoencoder on live Solver MQTT state messages.

Pipeline:
    hydraulic-press/unity/state (raw input + Solver result)
        -> 48-value model input
        -> TensorFlow SavedModel inference
        -> hydraulic-press/anomaly/result

The source topic name is kept for compatibility with the existing Solver. Unity
continues to consume that state topic for live measurements and also subscribes
to the anomaly result topic for the analysis dashboard.
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

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import numpy as np
import paho.mqtt.client as mqtt
import tensorflow as tf

from inference_timescale_store import InferenceTimescaleStore


APP_DIR = Path(__file__).resolve().parent
MODEL_DIR = APP_DIR / "models"
DEFAULT_MODEL = MODEL_DIR / "press_ae_standalone_savedmodel"
DEFAULT_IO = MODEL_DIR / "press_ae_standalone_io.json"
DEFAULT_SETTINGS = APP_DIR / "mqtt_settings.json"
DEFAULT_SOURCE_TOPIC = "hydraulic-press/unity/state"
DEFAULT_OUTPUT_TOPIC = "hydraulic-press/anomaly/result"

PHASE_INDEX = {
    "downstroke": 0.0,
    "하강": 0.0,
    "pressure_hold": 1.0,
    "hold": 1.0,
    "압력유지": 1.0,
    "upstroke": 2.0,
    "상승": 2.0,
}

FAULT_LABELS = {
    "normal": "정상",
    "pump_wear": "펌프 마모",
    "valve_stuck": "방향제어 밸브 고착",
    "overheat": "유압유 과열",
    "line_clog": "압력 라인 막힘",
    "suction_clog": "흡입 라인 막힘",
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
    anomaly = settings.get("anomaly", {})
    timescaledb = settings.get("timescaledb", {})
    if not isinstance(broker, dict):
        broker = {}
    if not isinstance(topics, dict):
        topics = {}
    if not isinstance(anomaly, dict):
        anomaly = {}
    if not isinstance(timescaledb, dict):
        timescaledb = {}

    parser = argparse.ArgumentParser(
        description="Solver MQTT 상태를 구독하고 AE 이상탐지 결과를 발행합니다."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", broker.get("host", "localhost")))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", broker.get("port", 1883))))
    parser.add_argument(
        "--source-topic",
        default=os.getenv(
            "MQTT_SOLVER_STATE_TOPIC",
            anomaly.get("source_topic", topics.get("unity", DEFAULT_SOURCE_TOPIC)),
        ),
    )
    parser.add_argument(
        "--output-topic",
        default=os.getenv(
            "MQTT_ANOMALY_TOPIC",
            anomaly.get("output_topic", topics.get("anomaly", DEFAULT_OUTPUT_TOPIC)),
        ),
    )
    parser.add_argument("--qos", type=int, choices=(0, 1, 2), default=int(anomaly.get("qos", 1)))
    parser.add_argument("--retain", action="store_true", default=bool(anomaly.get("retain", False)))
    parser.add_argument("--keepalive", type=int, default=int(broker.get("keepalive", 60)))
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument(
        "--client-id",
        default=f"press-anomaly-{socket.gethostname()}-{os.getpid()}",
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--io-config", type=Path, default=DEFAULT_IO)
    parser.add_argument("--log-every", type=int, default=int(anomaly.get("log_every", 1)))
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
        help="MQTT 대신 solver_state JSON 또는 JSONL 파일을 읽는 오프라인 검증 모드",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="오프라인 검증 결과를 JSONL 파일로 저장",
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


def read_float(source: dict[str, Any], name: str, fallback: float = 0.0) -> float:
    value = source.get(name)
    if value in (None, ""):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def normalize_phase(message: dict[str, Any], input_row: dict[str, Any]) -> tuple[str, float]:
    candidates = (
        message.get("cycle_phase"),
        message.get("active_mode"),
        input_row.get("cycle_phase"),
        input_row.get("System.active_mode"),
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text in PHASE_INDEX:
            return text, PHASE_INDEX[text]
        lowered = text.lower()
        if lowered in PHASE_INDEX:
            return lowered, PHASE_INDEX[lowered]
    raise ValueError(f"알 수 없는 cycle phase입니다: {candidates!r}")


class CycleAccumulator:
    """Tracks row-level anomaly decisions without double-counting QoS retries."""

    def __init__(self, k_of_n: int, max_cycles: int = 512) -> None:
        self.k_of_n = k_of_n
        self.max_cycles = max_cycles
        self.cycles: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()

    def add(
        self,
        run_id: str,
        line_number: Any,
        cycle_id: Any,
        input_row_index: Any,
        is_anomaly: bool,
        score: float,
    ) -> dict[str, Any]:
        key = (run_id, str(line_number), str(cycle_id))
        state = self.cycles.get(key)
        if state is None:
            state = {
                "seen_rows": set(),
                "row_count": 0,
                "anomaly_rows": 0,
                "max_score": 0.0,
            }
            self.cycles[key] = state
        else:
            self.cycles.move_to_end(key)

        row_key = str(input_row_index)
        if row_key not in state["seen_rows"]:
            state["seen_rows"].add(row_key)
            state["row_count"] += 1
            if is_anomaly:
                state["anomaly_rows"] += 1
            state["max_score"] = max(float(state["max_score"]), score)

        while len(self.cycles) > self.max_cycles:
            self.cycles.popitem(last=False)

        return {
            "row_count": int(state["row_count"]),
            "anomaly_rows": int(state["anomaly_rows"]),
            "k_of_n": self.k_of_n,
            "cycle_is_anomaly": int(state["anomaly_rows"]) >= self.k_of_n,
            "max_score": float(state["max_score"]),
        }


class StandaloneAutoencoder:
    def __init__(self, model_path: Path, io_path: Path) -> None:
        self.model_path = model_path.expanduser().resolve()
        self.io_path = io_path.expanduser().resolve()
        if not self.model_path.is_dir():
            raise FileNotFoundError(self.model_path)
        if not self.io_path.is_file():
            raise FileNotFoundError(self.io_path)

        with self.io_path.open("r", encoding="utf-8") as file:
            self.io = json.load(file)
        self.raw_columns = list(self.io["raw_columns"])
        self.threshold = float(self.io["threshold"])
        self.k_of_n = int(self.io["k_of_n"])
        self.model = tf.saved_model.load(str(self.model_path))
        if not hasattr(self.model, "serve"):
            raise AttributeError("SavedModel에 serve 함수가 없습니다")

    def make_input(self, message: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
        input_row = message.get("input")
        result_row = message.get("result")
        if not isinstance(input_row, dict) or not isinstance(result_row, dict):
            raise ValueError("solver_state 메시지에 input/result 객체가 필요합니다")

        source = {**input_row, **result_row}
        missing = [name for name in self.raw_columns if name not in source]
        if missing:
            raise ValueError(f"모델 입력 컬럼이 부족합니다: {missing}")

        raw = [finite_float(source[name], name) for name in self.raw_columns]
        phase_name, phase_index = normalize_phase(message, input_row)
        chamber_name = str(
            input_row.get(
                "Press.target_chamber",
                result_row.get("Press_target_chamber", ""),
            )
        ).strip().lower()
        if chamber_name not in {"cap", "rod"}:
            raise ValueError(f"알 수 없는 target chamber입니다: {chamber_name!r}")

        vector = np.asarray(
            [*raw, phase_index, 1.0 if chamber_name == "cap" else 0.0],
            dtype=np.float32,
        ).reshape(1, -1)
        if vector.shape != (1, 48):
            raise ValueError(f"모델 입력 shape 오류: {vector.shape}")
        return vector, {
            "phase_name": phase_name,
            "phase_index": phase_index,
            "chamber_name": chamber_name,
            "input": input_row,
            "result": result_row,
            "source": source,
        }

    def infer(self, vector: np.ndarray) -> tuple[float, bool, float]:
        outputs = self.model.serve(tf.constant(vector, dtype=tf.float32))
        if isinstance(outputs, dict):
            recon = outputs["output_0"]
            flag = outputs["output_1"]
            score = outputs["output_2"]
        else:
            recon, flag, score = outputs

        recon_error = float(np.asarray(recon).reshape(-1)[0])
        is_anomaly = bool(float(np.asarray(flag).reshape(-1)[0]) > 0.5)
        anomaly_score = float(np.asarray(score).reshape(-1)[0])
        if not all(math.isfinite(value) for value in (recon_error, anomaly_score)):
            raise ValueError("모델 출력에 NaN 또는 Inf가 있습니다")
        if is_anomaly != (recon_error > self.threshold):
            raise ValueError("모델 is_anomaly와 저장된 threshold 판정이 일치하지 않습니다")
        return recon_error, is_anomaly, anomaly_score


def valve_pattern_deviation(input_row: dict[str, Any], phase_index: float) -> tuple[float, str]:
    names = {
        "PA": "Cell.V_DIR_PA.opening_percent",
        "PB": "Cell.V_DIR_PB.opening_percent",
        "AT": "Cell.V_DIR_AT.opening_percent",
        "BT": "Cell.V_DIR_BT.opening_percent",
    }
    values = {key: read_float(input_row, name) for key, name in names.items()}
    if phase_index == 0.0:
        expected = {"PA": 76.2, "PB": 0.0, "AT": 0.0, "BT": 76.1}
    elif phase_index == 2.0:
        expected = {"PA": 0.0, "PB": 76.2, "AT": 76.4, "BT": 0.0}
    else:
        expected = {"PA": 0.0, "PB": 0.0, "AT": 0.0, "BT": 0.0}
    worst = max(values, key=lambda key: abs(values[key] - expected[key]))
    return abs(values[worst] - expected[worst]), worst


def analyze_cause(
    context: dict[str, Any],
    is_anomaly: bool,
    score: float,
) -> dict[str, Any]:
    input_row = context["input"]
    result_row = context["result"]
    source = context["source"]
    phase_index = float(context["phase_index"])
    temperature = read_float(input_row, "Fluid.temperature_c")
    suction_loss = read_float(result_row, "C_SUCTION_pressure_loss_bar")
    pressure_loss = read_float(result_row, "C_PRESSURE_pressure_loss_bar")
    flow = read_float(result_row, "calculated_flow_rate_L_min")
    pump_head = read_float(result_row, "pump_head_m")
    valve_deviation, valve_name = valve_pattern_deviation(input_row, phase_index)

    candidates: list[dict[str, Any]] = []
    if temperature >= 48.0:
        candidates.append(
            {
                "code": "overheat",
                "label": "유압유 과열",
                "confidence": min(0.99, 0.70 + (temperature - 48.0) / 40.0),
                "evidence": f"유압유 온도 {temperature:.1f} °C",
                "recommendation": "냉각기, 오일 상태와 연속 운전 부하를 점검하세요.",
            }
        )
    if valve_deviation >= 15.0:
        candidates.append(
            {
                "code": "valve_stuck",
                "label": f"방향제어 밸브 {valve_name} 동작 이상",
                "confidence": min(0.98, 0.65 + valve_deviation / 120.0),
                "evidence": f"공정 단계 기준 밸브 개도 편차 {valve_deviation:.1f} %p",
                "recommendation": "밸브 명령·피드백, 스풀 고착과 솔레노이드를 점검하세요.",
            }
        )
    if suction_loss >= 0.04:
        candidates.append(
            {
                "code": "suction_clog",
                "label": "흡입 라인 막힘 가능성",
                "confidence": min(0.95, 0.65 + suction_loss * 3.0),
                "evidence": f"흡입 라인 압력손실 {suction_loss:.3f} bar",
                "recommendation": "흡입 필터, 탱크 유면과 펌프 흡입 배관을 점검하세요.",
            }
        )
    if pressure_loss >= 2.0:
        candidates.append(
            {
                "code": "line_clog",
                "label": "압력 라인 막힘 가능성",
                "confidence": min(0.95, 0.60 + pressure_loss / 20.0),
                "evidence": f"압력 라인 압력손실 {pressure_loss:.3f} bar",
                "recommendation": "압력 배관, 필터와 국부 손실 구간을 점검하세요.",
            }
        )

    if is_anomaly and not candidates:
        candidates.append(
            {
                "code": "hydraulic_performance",
                "label": "펌프 성능 또는 복합 유압계통 이상",
                "confidence": min(0.90, 0.55 + math.log10(max(score, 1.0)) * 0.08),
                "evidence": f"유량 {flow:.2f} L/min, 펌프 헤드 {pump_head:.1f} m",
                "recommendation": "펌프 효율, 토출압력, 유량 센서와 솔버 파라미터를 함께 점검하세요.",
            }
        )

    candidates.sort(key=lambda item: float(item["confidence"]), reverse=True)
    if candidates:
        primary = candidates[0]
    else:
        primary = {
            "code": "normal",
            "label": "정상 운전 패턴",
            "confidence": max(0.5, min(0.99, 1.0 - score)),
            "evidence": "재구성 오차가 모델 임계값 이하입니다.",
            "recommendation": "현재 운전 상태를 계속 모니터링하세요.",
        }

    validation_code = str(source.get("fault_class", "")).strip()
    return {
        "likely_cause_code": primary["code"],
        "likely_cause": primary["label"],
        "confidence": round(float(primary["confidence"]), 4),
        "evidence": primary["evidence"],
        "recommendation": primary["recommendation"],
        "candidates": candidates[:3],
        "validation_label_code": validation_code,
        "validation_label": FAULT_LABELS.get(validation_code, validation_code),
    }


class InferencePipeline:
    def __init__(self, model: StandaloneAutoencoder) -> None:
        self.model = model
        self.cycles = CycleAccumulator(model.k_of_n)
        self.seen_message_ids: set[str] = set()
        self.seen_order: deque[str] = deque()

    def process(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("schema") != "hydraulic-press.unity.v1":
            raise ValueError(f"지원하지 않는 source schema: {message.get('schema')!r}")
        if message.get("type") != "solver_state":
            raise ValueError(f"지원하지 않는 source type: {message.get('type')!r}")

        source_message_id = str(message.get("message_id", ""))
        if source_message_id and source_message_id in self.seen_message_ids:
            return None

        vector, context = self.model.make_input(message)
        recon_error, is_anomaly, score = self.model.infer(vector)
        run_id = str(message.get("run_id", "unknown"))
        line_number = message.get(
            "line_number",
            context["input"].get("line_number", 7),
        )
        cycle_id = message.get("cycle_id", context["input"].get("cycle_id", ""))
        input_row_index = message.get("input_row_index", "")
        cycle = self.cycles.add(
            run_id,
            line_number,
            cycle_id,
            input_row_index,
            is_anomaly,
            score,
        )
        status = (
            "anomaly"
            if cycle["cycle_is_anomaly"]
            else "warning"
            if is_anomaly
            else "normal"
        )
        analysis = analyze_cause(context, is_anomaly, score)

        if source_message_id:
            self.seen_message_ids.add(source_message_id)
            self.seen_order.append(source_message_id)
            if len(self.seen_order) > 5000:
                expired = self.seen_order.popleft()
                self.seen_message_ids.discard(expired)

        return {
            "schema": "hydraulic-press.anomaly.v1",
            "type": "anomaly_inference",
            "run_id": run_id,
            "message_id": f"{run_id}:anomaly:{line_number}:{input_row_index}",
            "source_message_id": source_message_id,
            "input_row_index": input_row_index,
            "published_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "line_number": line_number,
            "cycle_id": cycle_id,
            "cycle_phase": message.get("cycle_phase", context["phase_name"]),
            "status": status,
            "model": {
                "name": self.model.model_path.name,
                "input_shape": [None, 48],
                "threshold": self.model.threshold,
                "k_of_n": self.model.k_of_n,
            },
            "inference": {
                "recon_error": recon_error,
                "is_anomaly": is_anomaly,
                "score": score,
            },
            "cycle": cycle,
            "analysis": analysis,
        }


def run_offline(args: argparse.Namespace, pipeline: InferencePipeline) -> int:
    assert args.input_json is not None
    text = args.input_json.expanduser().resolve().read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    messages: list[dict[str, Any]]
    if stripped.startswith("["):
        loaded = json.loads(text)
        if not isinstance(loaded, list):
            raise ValueError("오프라인 JSON 배열 형식이 올바르지 않습니다")
        messages = loaded
    elif "\n" in text.strip():
        messages = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        messages = [json.loads(text)]

    encoded_results: list[str] = []
    for message in messages:
        result = pipeline.process(message)
        if result is not None:
            encoded_results.append(json.dumps(result, ensure_ascii=False, allow_nan=False))
    output_text = "\n".join(encoded_results) + ("\n" if encoded_results else "")
    if args.output_json:
        output_path = args.output_json.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text, encoding="utf-8")
        print(f"오프라인 추론 결과: {output_path} ({len(encoded_results)}건)")
    else:
        print(output_text, end="")
    return 0


def run_mqtt(
    args: argparse.Namespace,
    pipeline: InferencePipeline,
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
            document = json.loads(message.payload.decode("utf-8"))
            inbox.put(document, timeout=1.0)
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
                    raise TimeoutError("이상탐지 결과 발행 확인 시간이 초과되었습니다")
                if store is not None:
                    store.add_result(result)
                processed += 1
                if processed == 1 or processed % max(1, args.log_every) == 0:
                    print(
                        "[ANOMALY_송신완료] "
                        f"송신행={processed} "
                        f"라인={result['line_number']} "
                        f"사이클ID={result['cycle_id']} "
                        f"score={result['inference']['score']:.4f} "
                        f"status={result['status']} "
                        f"주제={args.output_topic!r}",
                        flush=True,
                    )
            except Exception as exc:
                print(f"이상탐지 처리 실패: {exc}", file=sys.stderr, flush=True)
        return 0
    finally:
        if store is not None:
            store.drain(args.timescale_flush_timeout)
            store.close()
        client.disconnect()
        client.loop_stop()


def main() -> int:
    args = parse_args()
    runtime = StandaloneAutoencoder(args.model, args.io_config)
    pipeline = InferencePipeline(runtime)
    print(
        "AE 모델 로드 완료: "
        f"{runtime.model_path}, threshold={runtime.threshold:.9f}, "
        f"k_of_n={runtime.k_of_n}",
        flush=True,
    )
    if args.input_json:
        return run_offline(args, pipeline)
    store = None
    if args.timescale_dsn.strip():
        store = InferenceTimescaleStore(
            args.timescale_dsn,
            "anomaly",
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

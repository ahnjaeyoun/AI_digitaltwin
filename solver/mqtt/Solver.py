#!/usr/bin/env python3
"""MQTT 입력 행을 수신하여 C++ 실시간 유압 Solver에 전달합니다."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from TimescaleStore import SolverResultTail, TimescaleStore

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover - 로컬 설치 상태에 따라 달라짐
    raise SystemExit(
        "paho-mqtt가 설치되어 있지 않습니다. 프로젝트 루트에서 실행하세요: "
        "py -m pip install -r mqtt\\requirements.txt"
    ) from exc


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
PROJECT_DIR = ROOT / "realtime_solver_project"
DEFAULT_TOPIC = "hydraulic-press/solver/input"
DEFAULT_UNITY_TOPIC = "hydraulic-press/unity/state"
DEFAULT_EXE = PROJECT_DIR / "x64" / "Debug" / "Project1.exe"
DEFAULT_CONFIG = ROOT / "mqtt_settings.json"


def environment_value(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                registry_value, _ = winreg.QueryValueEx(key, name)
            return str(registry_value)
        except (FileNotFoundError, OSError):
            pass
    return default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} 값은 정수여야 합니다: {value!r}") from exc


def load_mqtt_settings(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig") as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError(f"MQTT 설정의 최상위 값은 객체여야 합니다: {path}")
    broker = document.get("broker", {})
    topics = document.get("topics", {})
    role = document.get("solver", {})
    timescaledb = document.get("timescaledb", {})
    unity = document.get("unity", {})
    if not all(
        isinstance(item, dict) for item in (broker, topics, role, timescaledb, unity)
    ):
        raise ValueError(f"MQTT 설정 구성이 올바르지 않습니다: {path}")
    return {
        **broker,
        "topic": topics.get("input", DEFAULT_TOPIC),
        "unity_topic": topics.get("unity", DEFAULT_UNITY_TOPIC),
        "unity": unity,
        "timescaledb": timescaledb,
        **role,
    }


def parse_args() -> argparse.Namespace:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    config_args, _ = config_parser.parse_known_args()
    settings = load_mqtt_settings(config_args.config.expanduser().resolve())
    timescale_settings = settings.get("timescaledb", {})
    if not isinstance(timescale_settings, dict):
        timescale_settings = {}
    unity_settings = settings.get("unity", {})
    if not isinstance(unity_settings, dict):
        unity_settings = {}

    parser = argparse.ArgumentParser(
        description="CSV 행 MQTT 메시지를 구독하고 Project1.exe를 실시간 모드로 실행합니다."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", str(settings.get("host", "localhost"))))
    parser.add_argument("--port", type=int, default=env_int("MQTT_PORT", int(settings.get("port", 1883))))
    parser.add_argument("--topic", default=os.getenv("MQTT_INPUT_TOPIC", str(settings.get("topic", DEFAULT_TOPIC))))
    parser.add_argument("--qos", type=int, choices=(0, 1, 2), default=int(settings.get("qos", 1)))
    parser.add_argument("--client-id", default=f"press-solver-{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--tls", action="store_true", default=bool(settings.get("tls", False)))
    parser.add_argument("--ca-file", type=Path, default=settings.get("ca_file"))
    parser.add_argument("--keepalive", type=int, default=int(settings.get("keepalive", 60)))
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--run-id", help="지정한 Publisher 실행ID만 수신")
    parser.add_argument(
        "--unity-topic",
        default=os.getenv(
            "MQTT_UNITY_TOPIC",
            str(settings.get("unity_topic", DEFAULT_UNITY_TOPIC)),
        ),
        help="입력값과 Solver 결과값을 함께 발행할 Unity MQTT 주제",
    )
    parser.add_argument(
        "--unity-qos",
        type=int,
        choices=(0, 1, 2),
        default=int(unity_settings.get("qos", settings.get("qos", 1))),
        help="Unity 상태 메시지 QoS(기본값: Broker QoS)",
    )
    parser.add_argument(
        "--unity-retain",
        action="store_true",
        default=bool(unity_settings.get("retain", False)),
        help="Broker가 가장 최근 Unity 상태 메시지를 보관",
    )
    parser.add_argument(
        "--no-unity",
        action="store_false",
        dest="unity_enabled",
        default=bool(unity_settings.get("enabled", True)),
        help="Unity MQTT 상태 발행 비활성화",
    )
    parser.add_argument("--solver-exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument(
        "--input-spool", type=Path, default=PROJECT_DIR / "mqtt_realtime_input.csv"
    )
    parser.add_argument(
        "--output", type=Path, default=PROJECT_DIR / "realtime_solver_output.csv"
    )
    parser.add_argument("--poll-ms", type=int, default=100)
    parser.add_argument(
        "--log-every",
        type=int,
        default=1,
        help="N행마다 수신 확인 표시(기본값: 모든 행)",
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=120.0,
        help="종료 메시지 후 Solver 완료를 기다릴 최대 시간(초)",
    )
    parser.add_argument(
        "--timescale-dsn",
        default=environment_value(
            "TIMESCALE_DSN", str(timescale_settings.get("dsn", ""))
        ),
        help="TimescaleDB 접속 문자열. TIMESCALE_DSN 환경 변수 사용 가능",
    )
    parser.add_argument(
        "--timescale-schema",
        default=environment_value(
            "TIMESCALE_SCHEMA", str(timescale_settings.get("schema", "public"))
        ),
        help="TimescaleDB 테이블 스키마(기본값: public)",
    )
    parser.add_argument(
        "--timescale-required",
        action="store_true",
        default=bool(timescale_settings.get("required", False)),
        help="DB 저장 실패 시 Solver도 오류로 종료",
    )
    parser.add_argument(
        "--no-timescale-init",
        action="store_false",
        dest="timescale_init",
        default=bool(timescale_settings.get("initialize", True)),
        help="TimescaleDB 확장과 hypertable 자동 생성을 수행하지 않음",
    )
    parser.add_argument(
        "--timescale-connect-timeout",
        type=int,
        default=int(timescale_settings.get("connect_timeout", 5)),
        help="TimescaleDB 연결 제한 시간(초)",
    )
    parser.add_argument(
        "--timescale-flush-timeout",
        type=float,
        default=float(timescale_settings.get("flush_timeout", 30.0)),
        help="종료 시 DB 저장 대기열을 비울 최대 시간(초)",
    )
    return parser.parse_args()


def json_typed_value(name: str, value: Any) -> Any:
    """CSV 문자열을 Unity에서 바로 사용하기 쉬운 JSON 자료형으로 변환합니다."""

    if value is None or not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    boolean_names = {
        "solver_warning",
        "contains_inf_or_nan",
        "Cell.PUMP_01.is_running",
    }
    if name in boolean_names and lowered in {"yes", "no"}:
        return lowered == "yes"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
        return number if math.isfinite(number) else text
    except ValueError:
        return value


def json_typed_row(row: dict[str, Any]) -> dict[str, Any]:
    """입력 또는 결과 한 행 전체를 JSON 숫자·불리언 형식으로 정규화합니다."""

    return {name: json_typed_value(name, value) for name, value in row.items()}


def publish_unity_state(
    client: mqtt.Client,
    *,
    topic: str,
    qos: int,
    retain: bool,
    run_id: str,
    input_row: dict[str, Any],
    result_row: dict[str, Any],
) -> int:
    """동일 입력행과 Solver 결과행을 하나의 Unity 상태 메시지로 발행합니다."""

    try:
        input_row_index = int(str(result_row.get("input_row_index", "")).strip())
    except ValueError as exc:
        raise ValueError("Solver 결과에 올바른 input_row_index가 없습니다") from exc

    payload = {
        "schema": "hydraulic-press.unity.v1",
        "type": "solver_state",
        "run_id": run_id,
        "message_id": f"{run_id}:unity:{input_row_index}",
        "input_row_index": input_row_index,
        "published_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "cycle_id": json_typed_value("cycle_id", result_row.get("cycle_id", "")),
        "cycle_phase": result_row.get("cycle_phase", ""),
        "active_mode": result_row.get("active_mode", ""),
        "input": json_typed_row(input_row),
        "result": json_typed_row(result_row),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    publish_result = client.publish(
        topic,
        encoded,
        qos=qos,
        retain=retain,
    )
    if publish_result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(
            f"Unity MQTT 송신 실패: {mqtt.error_string(publish_result.rc)}"
        )
    publish_result.wait_for_publish(timeout=10.0)
    if not publish_result.is_published():
        raise TimeoutError("Unity MQTT 송신 확인 시간이 초과되었습니다")
    return input_row_index


class CsvSpool:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.columns: list[str] | None = None
        self.file: Any = None
        self.writer: csv.DictWriter | None = None
        self.rows_written = 0

    def open(self, columns: list[str]) -> None:
        clean_columns = [str(column) for column in columns if str(column)]
        if not clean_columns:
            raise ValueError("MQTT 메시지에 CSV 열 정보가 없습니다")
        if len(set(clean_columns)) != len(clean_columns):
            raise ValueError("MQTT CSV 열에 중복 항목이 있습니다")
        if self.columns is not None:
            if clean_columns != self.columns:
                raise ValueError("실행 중 CSV 열 순서가 변경되었습니다")
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("w", encoding="utf-8-sig", newline="", buffering=1)
        self.columns = clean_columns
        self.writer = csv.DictWriter(self.file, fieldnames=clean_columns, extrasaction="raise")
        self.writer.writeheader()
        self.file.flush()

    def append(self, row: dict[str, Any], columns: list[str] | None) -> None:
        if self.columns is None:
            self.open(columns or list(row.keys()))
        assert self.columns is not None and self.writer is not None and self.file is not None

        unexpected = set(row) - set(self.columns)
        if unexpected:
            raise ValueError(f"행에 예상하지 못한 열이 있습니다: {sorted(unexpected)}")
        normalized = {}
        for column in self.columns:
            value = row.get(column, "")
            if value is None:
                value = ""
            elif isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            normalized[column] = value
        self.writer.writerow(normalized)
        self.file.flush()
        self.rows_written += 1

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


def start_solver(args: argparse.Namespace) -> subprocess.Popen[str]:
    executable = args.solver_exe.expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(
            f"Solver 실행 파일이 없습니다: {executable}\n"
            "realtime_solver_project를 x64/Debug로 빌드하거나 --solver-exe를 지정하세요."
        )

    input_path = args.input_spool.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    input_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.unlink(missing_ok=True)
    output_path.unlink(missing_ok=True)

    command = [
        str(executable),
        "--realtime",
        str(input_path),
        str(output_path),
        str(max(100, args.poll_ms)),
    ]
    print("Solver 시작:", subprocess.list2cmdline(command))
    process = subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def relay_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            print(f"[CXX] {line}", end="", flush=True)

    threading.Thread(target=relay_output, name="solver-output", daemon=True).start()
    return process


def stop_solver(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def count_output_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            return max(0, sum(1 for _ in csv.reader(file)) - 1)
    except (OSError, csv.Error):
        return 0


def wait_until_drained(
    process: subprocess.Popen[str],
    output_path: Path,
    expected_rows: int,
    timeout: float,
    on_poll: Callable[[], None] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_count = -1
    while time.monotonic() < deadline:
        if on_poll is not None:
            on_poll()
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"Solver가 예기치 않게 종료되었습니다. 종료 코드: {return_code}")
        count = count_output_rows(output_path)
        if count != last_count:
            print(f"Solver 출력 대기 중: {count}/{expected_rows}행")
            last_count = count
        if count >= expected_rows:
            return
        time.sleep(0.2)
    raise TimeoutError(
        f"Solver가 {timeout:g}초 안에 모든 행을 처리하지 못했습니다 "
        f"({count_output_rows(output_path)}/{expected_rows})"
    )


def make_client(
    args: argparse.Namespace,
    connected: threading.Event,
    connection_finished: threading.Event,
    connection_error: list[str],
    inbox: queue.Queue[dict[str, Any]],
) -> mqtt.Client:
    """MQTT 수신용 클라이언트와 연결·메시지 콜백을 구성합니다.

    Paho MQTT의 콜백은 네트워크 처리 스레드에서 실행됩니다. 콜백 안에서
    Solver 계산이나 파일 저장을 직접 수행하면 다음 MQTT 패킷 처리가 지연될 수
    있으므로, 수신 콜백은 JSON 해석까지만 하고 payload를 inbox에 전달합니다.
    실제 검증과 저장은 main()의 수신 처리 반복문에서 순서대로 수행합니다.
    """

    # MQTT 클라이언트 객체를 생성합니다.
    # VERSION2는 Paho 2.x 콜백 인자 형식을 사용한다는 의미입니다.
    # client_id는 Broker가 각 Solver 연결을 구분하는 고유 식별자입니다.
    # MQTTv311은 현재 Mosquitto와 주고받을 MQTT 프로토콜 버전입니다.
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
        protocol=mqtt.MQTTv311,
    )

    def on_connect(
        client: mqtt.Client,
        userdata: object,
        flags: object,
        reason_code: object,
        properties: object,
    ) -> None:
        """Broker가 연결 응답(CONNACK)을 보냈을 때 Paho가 호출합니다."""

        # 현재 수신 처리에서는 userdata, 연결 flags, MQTT 5 properties를 사용하지 않습니다.
        # 사용하지 않는 인자를 명시적으로 삭제하여 의도를 분명하게 표시합니다.
        del userdata, flags, properties

        # reason_code가 0이 아니면 Broker가 연결을 거부했거나 연결에 실패한 것입니다.
        if reason_code != 0:
            # main()이 최종 오류 원인을 출력할 수 있도록 문자열로 보관합니다.
            connection_error.append(str(reason_code))
            # 즉시 확인할 수 있도록 표준 오류에도 연결 거부 사유를 출력합니다.
            print(f"MQTT 연결 거부: {reason_code}", file=sys.stderr)
            # 연결 시도가 끝났음을 main() 대기 스레드에 알립니다.
            connection_finished.set()
            # 연결에 실패했으므로 topic 구독을 요청하지 않고 콜백을 종료합니다.
            return

        # 연결 성공 후 유압 입력 topic 구독을 Broker에 요청합니다.
        # qos=args.qos는 Publisher와 동일한 전달 수준(기본 QoS 1)을 사용합니다.
        result, _ = client.subscribe(args.topic, qos=args.qos)

        # subscribe() 호출 자체가 Paho에 정상 등록되지 않았다면 오류를 출력합니다.
        if result != mqtt.MQTT_ERR_SUCCESS:
            print(f"MQTT 구독 실패: {mqtt.error_string(result)}", file=sys.stderr)
            # 구독 실패 상태에서는 connected 이벤트를 설정하지 않습니다.
            return

        # MQTT 연결과 구독 요청 등록이 완료됐음을 main()에 알립니다.
        connected.set()
        # main()의 연결 제한 시간 대기를 해제합니다.
        connection_finished.set()
        # Visual Studio 실행기는 이 문구를 감지한 후 Publisher를 시작합니다.
        print(f"MQTT 구독 완료: 주제={args.topic!r}, QoS={args.qos}")

    def on_message(
        client: mqtt.Client,
        userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        """구독 topic에 메시지가 도착할 때마다 Paho 네트워크 스레드가 호출합니다."""

        # 이 콜백에서는 client와 userdata를 사용하지 않고 수신 message만 처리합니다.
        del client, userdata
        try:
            # MQTT payload는 bytes이므로 먼저 UTF-8 문자열로 변환합니다.
            # 변환한 문자열을 다시 Python JSON 값으로 해석합니다.
            payload = json.loads(message.payload.decode("utf-8"))

            # 프로젝트 메시지의 최상위 JSON 형식은 반드시 객체(dict)여야 합니다.
            # 배열, 문자열, 숫자 등이 오면 이후 필드 접근이 불가능하므로 폐기합니다.
            if not isinstance(payload, dict):
                raise ValueError("JSON 최상위 값은 객체여야 합니다")

            # 정상 JSON 객체를 thread-safe queue에 즉시 넣습니다.
            # 콜백에서는 계산하지 않고 main() 반복문이 도착 순서대로 꺼내 처리합니다.
            inbox.put_nowait(payload)

        # UTF-8 오류, JSON 문법 오류, 형식 오류, queue 포화는 한 메시지의 오류로 처리합니다.
        # 수신기 전체를 종료하지 않고 해당 메시지만 폐기하여 다음 메시지를 계속 받습니다.
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, queue.Full) as exc:
            print(f"잘못된 MQTT 메시지를 폐기했습니다: {exc}", file=sys.stderr)

    # 위에서 정의한 연결 콜백을 Paho 클라이언트에 등록합니다.
    client.on_connect = on_connect
    # 위에서 정의한 메시지 수신 콜백을 Paho 클라이언트에 등록합니다.
    client.on_message = on_message

    # --tls가 지정된 경우에만 MQTT 자체 TLS를 활성화합니다.
    # 현재 기본 구성은 Tailscale 터널 암호화를 사용하므로 이 분기는 실행되지 않습니다.
    if args.tls:
        # CA 파일을 지정했으면 해당 인증서를 사용하고, 없으면 시스템 기본 CA를 사용합니다.
        client.tls_set(ca_certs=str(args.ca_file) if args.ca_file else None)

    # 콜백과 선택적 TLS 설정을 모두 적용한 수신 클라이언트를 반환합니다.
    return client


def main() -> int:
    args = parse_args()
    if args.poll_ms < 1:
        raise SystemExit("--poll-ms 값은 1 이상이어야 합니다")
    if args.log_every < 1:
        raise SystemExit("--log-every 값은 1 이상이어야 합니다")
    if args.drain_timeout <= 0:
        raise SystemExit("--drain-timeout 값은 0보다 커야 합니다")
    if args.timescale_connect_timeout < 1:
        raise SystemExit("--timescale-connect-timeout 값은 1 이상이어야 합니다")
    if args.timescale_flush_timeout < 0:
        raise SystemExit("--timescale-flush-timeout 값은 0 이상이어야 합니다")
    if args.unity_enabled and not args.unity_topic.strip():
        raise SystemExit("Unity MQTT 주제는 비어 있을 수 없습니다")
    if args.unity_enabled and args.unity_topic == args.topic:
        raise SystemExit("Unity MQTT 주제는 Solver 입력 주제와 달라야 합니다")

    # Ctrl+C 또는 종료 신호를 받으면 수신 반복문을 빠져나가기 위한 이벤트입니다.
    stop_event = threading.Event()
    # Broker 연결과 topic 구독 요청이 성공했음을 표시하는 이벤트입니다.
    connected = threading.Event()
    # 연결 시도가 성공 또는 실패로 끝났음을 표시하여 제한 시간 대기를 해제합니다.
    connection_finished = threading.Event()
    # on_connect 콜백에서 받은 연결 오류 코드를 main()으로 전달하는 목록입니다.
    connection_error: list[str] = []
    # MQTT 콜백 스레드와 main() 처리 스레드 사이에서 payload를 안전하게 전달합니다.
    inbox: queue.Queue[dict[str, Any]] = queue.Queue()
    # 유효한 data 메시지를 C++ Solver가 감시하는 실시간 입력 CSV에 기록합니다.
    spool = CsvSpool(args.input_spool)

    # 아래 객체들은 실제 기능이 활성화된 뒤 할당되며 finally에서 안전하게 정리합니다.
    process: subprocess.Popen[str] | None = None
    timescale: TimescaleStore | None = None
    result_tail: SolverResultTail | None = None

    # 위 이벤트와 queue를 사용하는 MQTT 수신 클라이언트를 생성합니다.
    client = make_client(args, connected, connection_finished, connection_error, inbox)

    # --run-id가 있으면 해당 Publisher 실행만 받고, 없으면 첫 실행ID를 자동 수락합니다.
    active_run = args.run_id
    # QoS 1 재전송으로 같은 data 메시지가 다시 올 때 중복 처리를 막는 집합입니다.
    seen_message_ids: set[str] = set()
    # C++ 결과의 input_row_index와 같은 번호로 원본 입력행을 보관합니다.
    pending_unity_inputs: dict[int, dict[str, Any]] = {}
    # Broker가 확인한 Unity 상태 메시지 수를 누적합니다.
    unity_sent = 0

    def poll_solver_results(run_id: str) -> int:
        """새 C++ 결과를 DB에 저장하고 동일 입력값과 함께 Unity로 발행합니다."""

        nonlocal unity_sent
        if result_tail is None:
            return 0

        def send_result_to_unity(result_row: dict[str, Any]) -> None:
            nonlocal unity_sent
            if not args.unity_enabled:
                return
            try:
                input_row_index = int(
                    str(result_row.get("input_row_index", "")).strip()
                )
            except ValueError as exc:
                raise ValueError(
                    "Unity 전송 대상 Solver 결과에 input_row_index가 없습니다"
                ) from exc
            input_row = pending_unity_inputs.pop(input_row_index, None)
            if input_row is None:
                raise RuntimeError(
                    f"Unity 전송용 입력행을 찾지 못했습니다: 입력행={input_row_index}"
                )
            publish_unity_state(
                client,
                topic=args.unity_topic,
                qos=args.unity_qos,
                retain=args.unity_retain,
                run_id=run_id,
                input_row=input_row,
                result_row=result_row,
            )
            unity_sent += 1
            if unity_sent == 1 or unity_sent % args.log_every == 0:
                print(
                    "[UNITY_송신완료] "
                    f"송신행={unity_sent} "
                    f"입력행={input_row_index} "
                    f"사이클ID={result_row.get('cycle_id', '')} "
                    f"주제={args.unity_topic!r}",
                    flush=True,
                )

        added = result_tail.poll(
            timescale,
            run_id,
            send_result_to_unity if args.unity_enabled else None,
        )
        if timescale is not None:
            timescale.flush_pending()
        return added

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        if args.timescale_dsn:
            timescale = TimescaleStore(
                args.timescale_dsn,
                args.timescale_schema,
                required=args.timescale_required,
                initialize=args.timescale_init,
                connect_timeout=args.timescale_connect_timeout,
                log_every=args.log_every,
            )
            timescale.start()
        else:
            print(
                "TimescaleDB 저장 비활성화: TIMESCALE_DSN을 설정하면 자동으로 활성화됩니다."
            )

        # DB 저장 또는 Unity 전송 중 하나라도 사용하면 C++ 결과 CSV의 새 행을 추적합니다.
        if timescale is not None or args.unity_enabled:
            result_tail = SolverResultTail(args.output)
        if args.unity_enabled:
            print(
                "Unity MQTT 전송 활성화: "
                f"주제={args.unity_topic!r}, QoS={args.unity_qos}, "
                f"retain={args.unity_retain}"
            )

        # MQTT data 메시지가 입력 CSV에 추가되는 즉시 처리하도록 C++ Solver를 먼저 시작합니다.
        process = start_solver(args)

        # 설정 파일 또는 실행 인수에서 읽은 Broker 주소를 사용자에게 표시합니다.
        print(f"MQTT 연결 중: mqtt://{args.host}:{args.port} ...")

        # Broker에 TCP/MQTT 연결을 시작합니다. 결과는 on_connect 콜백으로 전달됩니다.
        client.connect(args.host, args.port, args.keepalive)

        # 별도 Paho 네트워크 스레드를 시작하여 수신 패킷과 콜백을 계속 처리합니다.
        client.loop_start()

        # 연결과 구독 요청이 제한 시간 안에 끝나지 않으면 무한 대기를 막기 위해 실패 처리합니다.
        if not connection_finished.wait(args.connect_timeout):
            raise TimeoutError("MQTT 연결 또는 구독 시간이 초과되었습니다")

        # 연결 시도는 끝났지만 connected가 설정되지 않았다면 실패 원인을 보고합니다.
        if not connected.is_set():
            # on_connect가 저장한 최신 오류가 없으면 일반 원인 문구를 사용합니다.
            reason = connection_error[-1] if connection_error else "알 수 없는 원인"
            raise ConnectionError(f"MQTT 연결 실패: {reason}")

        # 종료 신호 또는 end 메시지를 받을 때까지 MQTT 메시지를 순서대로 처리합니다.
        while not stop_event.is_set():
            # MQTT는 살아 있어도 C++ Solver가 비정상 종료했다면 결과를 만들 수 없으므로 중단합니다.
            if process.poll() is not None:
                raise RuntimeError(f"Solver가 예기치 않게 종료되었습니다. 종료 코드: {process.returncode}")

            # 새 C++ 결과를 감지해 TimescaleDB와 Unity에 각각 전달합니다.
            if result_tail is not None and active_run:
                poll_solver_results(active_run)

            try:
                # 콜백이 inbox에 넣은 다음 payload를 최대 0.2초 기다려 꺼냅니다.
                # 짧은 timeout 덕분에 메시지가 없어도 종료 신호와 Solver 상태를 계속 확인합니다.
                payload = inbox.get(timeout=0.2)
            except queue.Empty:
                # 0.2초 동안 메시지가 없으면 오류가 아니므로 반복문의 처음으로 돌아갑니다.
                continue

            # 이 프로젝트가 정의한 MQTT JSON 스키마만 처리합니다.
            if payload.get("schema") != "hydraulic-press.input.v1":
                print("알 수 없는 스키마의 메시지를 무시했습니다", file=sys.stderr)
                continue

            # type은 start/data/end 중 어떤 처리 분기로 보낼지 결정합니다.
            message_type = payload.get("type")

            # run_id는 한 번의 Publisher 실행에 속한 메시지들을 같은 묶음으로 식별합니다.
            run_id = str(payload.get("run_id", ""))

            # 실행ID가 없으면 다른 실행과 구분할 수 없으므로 메시지를 폐기합니다.
            if not run_id:
                print("실행ID가 없는 메시지를 무시했습니다", file=sys.stderr)
                continue

            # --run-id를 지정하지 않았다면 최초 start 또는 data 메시지의 실행ID를 수락합니다.
            # data도 허용하므로 Solver가 start 직후 늦게 구독한 경우에도 처리를 시작할 수 있습니다.
            if active_run is None and message_type in ("start", "data"):
                active_run = run_id
                print(f"Publisher 실행ID 수락: {active_run}")

            # 현재 처리 중인 실행과 다른 run_id는 여러 Publisher 데이터가 섞이지 않도록 무시합니다.
            if run_id != active_run:
                print(f"다른 실행ID의 메시지를 무시했습니다: {run_id}", file=sys.stderr)
                continue

            # start 메시지는 실행에 사용할 CSV 열 순서와 예상 행 수를 전달합니다.
            if message_type == "start":
                # Publisher가 보낸 원본 CSV 열 목록을 가져옵니다.
                columns = payload.get("columns")

                # 열 목록이 list가 아니면 안정적인 CSV 헤더를 만들 수 없으므로 실행을 실패 처리합니다.
                if not isinstance(columns, list):
                    raise ValueError("시작 메시지에 CSV 열 목록이 없습니다")

                # 실시간 입력 CSV를 열고 전달받은 열 목록으로 헤더를 작성합니다.
                spool.open(columns)

                # Publisher가 예고한 전체 행 수를 표시하여 실행 규모를 확인할 수 있게 합니다.
                print(f"실행 시작: Publisher가 {payload.get('row_count', '?')}행을 예고했습니다")

                # start 처리가 끝났으므로 다음 MQTT 메시지를 기다립니다.
                continue

            # data 메시지는 원본 CSV 한 행과 그 행의 식별 정보를 전달합니다.
            if message_type == "data":
                # QoS 재전송 중복을 구분할 고유 message_id를 문자열로 읽습니다.
                message_id = str(payload.get("message_id", ""))

                # ID가 없으면 중복 여부를 보장할 수 없으므로 해당 행을 폐기합니다.
                if not message_id:
                    print("메시지ID가 없는 데이터 메시지를 무시했습니다", file=sys.stderr)
                    continue

                # 이미 정상 처리한 ID이면 CSV와 DB에 같은 행이 두 번 들어가지 않도록 무시합니다.
                if message_id in seen_message_ids:
                    print(f"중복 메시지를 무시했습니다: 메시지ID={message_id}", file=sys.stderr)
                    continue

                # data 필드에는 원본 CSV 한 행이 JSON 객체 형태로 들어 있습니다.
                row = payload.get("data")

                # columns는 start 메시지를 놓친 경우에도 CSV 헤더를 복원할 수 있는 선택 필드입니다.
                columns = payload.get("columns")

                # 실제 행 데이터가 객체가 아니면 CSV 열과 값을 매핑할 수 없으므로 폐기합니다.
                if not isinstance(row, dict):
                    print(f"메시지 {message_id} 무시: data 값이 객체가 아닙니다", file=sys.stderr)
                    continue

                # columns가 들어 있다면 반드시 list여야 하며, 잘못된 형식은 폐기합니다.
                if columns is not None and not isinstance(columns, list):
                    print(f"메시지 {message_id} 무시: columns 값이 목록이 아닙니다", file=sys.stderr)
                    continue

                # 검증된 한 행을 C++ Solver가 감시하는 실시간 입력 CSV에 즉시 추가합니다.
                spool.append(row, columns)

                # 동일 번호의 C++ 결과가 나오면 입력값과 결합할 수 있도록 원본 행을 보관합니다.
                if args.unity_enabled:
                    pending_unity_inputs[spool.rows_written] = dict(row)

                # CSV 기록까지 성공한 뒤 ID를 집합에 넣어 이후 같은 메시지를 중복으로 판단합니다.
                seen_message_ids.add(message_id)

                # TimescaleDB 기능이 활성화된 경우 원본 payload 전체를 원천 테이블에 저장합니다.
                if timescale is not None:
                    timescale.add_raw(
                        mqtt_topic=args.topic,
                        payload=payload,
                        row=row,
                    )

                # 첫 행은 항상 로그를 남기고, 이후에는 --log-every 간격으로 수신 완료를 표시합니다.
                if (
                    spool.rows_written == 1
                    or spool.rows_written % args.log_every == 0
                ):
                    print(
                        "[MQTT_수신완료] "
                        f"수신행={spool.rows_written} "
                        f"순번={payload.get('sequence', '')} "
                        f"사이클ID={row.get('cycle_id', '')} "
                        f"시각={row.get('timestamp', '')} "
                        f"메시지ID={message_id} "
                        f"입력파일={spool.path}",
                        flush=True,
                    )

                # data 메시지 처리가 완료됐으므로 다음 MQTT 메시지를 기다립니다.
                continue

            # end 메시지는 Publisher가 이번 실행의 모든 data 메시지를 보냈다는 의미입니다.
            if message_type == "end":
                # completed/aborted 등의 영문 상태 값을 문자열로 정규화합니다.
                status_value = str(payload.get("status", "unknown"))

                # 사용자 로그에서는 주요 상태를 한글로 표시합니다.
                status_text = {
                    "completed": "완료",
                    "aborted": "중단",
                    "unknown": "알 수 없음",
                }.get(status_value, status_value)

                # 현재까지 입력 CSV에 기록한 실제 수신행 수와 종료 상태를 표시합니다.
                print(
                    f"실행 종료 메시지 수신: 상태={status_text}, "
                    f"수신행={spool.rows_written}"
                )

                # 파일 버퍼를 닫아 마지막 입력행까지 C++ Solver가 읽을 수 있도록 확정합니다.
                spool.close()

                # C++ 결과 CSV 행 수가 수신행 수와 같아질 때까지 기다립니다.
                # 대기 중에도 on_poll을 통해 새 결과를 DB와 Unity에 계속 전달합니다.
                wait_until_drained(
                    process,
                    args.output.expanduser().resolve(),
                    spool.rows_written,
                    args.drain_timeout,
                    on_poll=(
                        (lambda: poll_solver_results(active_run or run_id))
                        if result_tail is not None
                        else None
                    ),
                )

                # 출력 행 수 확인 직후 생성된 마지막 결과가 있으면 한 번 더 처리합니다.
                if result_tail is not None:
                    poll_solver_results(active_run or run_id)

                # DB가 활성화된 경우 마지막 결과 행과 메모리 대기열까지 마무리합니다.
                if timescale is not None and result_tail is not None:
                    # 설정된 제한 시간 동안 아직 저장되지 않은 DB 레코드를 모두 재시도합니다.
                    if not timescale.drain(args.timescale_flush_timeout):
                        # DB 필수 모드에서는 미저장 데이터가 하나라도 남으면 실행 실패로 처리합니다.
                        if args.timescale_required:
                            raise RuntimeError("TimescaleDB 미저장 데이터가 남아 있습니다")

                    # 원천 데이터와 Solver 결과가 각각 몇 건 저장됐는지 최종 표시합니다.
                    print(
                        "TimescaleDB 저장 완료: "
                        f"원천데이터={timescale.raw_stored}건, "
                        f"Solver결과={timescale.results_stored}건"
                    )

                # Unity 전송이 켜져 있으면 입력행과 결과행의 최종 송신 건수를 확인합니다.
                if args.unity_enabled:
                    if pending_unity_inputs:
                        raise RuntimeError(
                            "Unity로 전송하지 못한 입력행이 남아 있습니다: "
                            f"{len(pending_unity_inputs)}건"
                        )
                    print(f"Unity MQTT 전송 완료: 입력+결과={unity_sent}건")

                # 입력행 수와 최종 결과 파일을 출력하여 전체 처리 완료를 확인합니다.
                print(f"Solver 전체 완료: {spool.rows_written}행 -> {args.output.resolve()}")

                # Publisher가 completed로 끝났으면 성공(0), 그 외 상태면 실패(1)를 반환합니다.
                return 0 if payload.get("status") == "completed" else 1

            # 정의되지 않은 type은 수신기 전체를 중단하지 않고 해당 메시지만 무시합니다.
            print(f"알 수 없는 메시지 유형을 무시했습니다: {message_type!r}", file=sys.stderr)

        print("사용자 요청으로 중지합니다.")
        return 130
    except Exception as exc:
        print(f"수신기 오류: {exc}", file=sys.stderr)
        return 1
    finally:
        # 예외 또는 사용자 중단 시에도 입력 CSV 파일 핸들을 반드시 닫습니다.
        spool.close()

        # 결과 추적기가 열려 있으면 남은 C++ 결과를 DB와 Unity에 마지막으로 반영합니다.
        if result_tail is not None:
            try:
                if process is not None and active_run:
                    poll_solver_results(active_run)
            except Exception as exc:
                print(f"Solver 결과 최종 처리 오류: {exc}", file=sys.stderr)
            result_tail.close()

        # DB 저장기가 활성화됐다면 남은 대기열을 재시도한 후 연결을 닫습니다.
        if timescale is not None:
            try:
                timescale.drain(args.timescale_flush_timeout)
            except Exception as exc:
                print(f"TimescaleDB 종료 저장 오류: {exc}", file=sys.stderr)
            timescale.close()

        # Broker에 정상 연결되지 못한 경우에도 정리 단계가 실패하지 않도록 예외를 무시합니다.
        try:
            client.disconnect()
        except Exception:
            pass

        # Paho 네트워크 처리 스레드를 정지하고 종료될 때까지 기다립니다.
        client.loop_stop()

        # C++ Solver가 시작된 상태라면 종료 신호를 보내고 프로세스를 정리합니다.
        if process is not None:
            stop_solver(process)

        # 실행 중 생성한 임시 유압 네트워크 파일을 제거합니다.
        (PROJECT_DIR / "network.runtime.json").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())

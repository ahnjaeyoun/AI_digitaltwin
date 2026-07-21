#!/usr/bin/env python3
"""유압 프레스 CSV 행을 실시간 주기로 MQTT에 송신합니다."""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

import paho.mqtt.client as mqtt



SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DEFAULT_CSV = ROOT / "solver_input_1000cycles.csv"
DEFAULT_TOPIC = "hydraulic-press/solver/input"
DEFAULT_CONFIG = ROOT / "mqtt_settings.json"


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
    role = document.get("publisher", {})
    if not all(isinstance(item, dict) for item in (broker, topics, role)):
        raise ValueError(f"MQTT 설정 구성이 올바르지 않습니다: {path}")
    return {**broker, "topic": topics.get("input", DEFAULT_TOPIC), **role}


def parse_args() -> argparse.Namespace:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    config_args, _ = config_parser.parse_known_args()
    settings = load_mqtt_settings(config_args.config.expanduser().resolve())

    parser = argparse.ArgumentParser(
        description="Solver 호환 CSV의 각 행을 MQTT 메시지 하나로 송신합니다."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="입력 CSV 경로")
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", str(settings.get("host", "localhost"))))
    parser.add_argument("--port", type=int, default=env_int("MQTT_PORT", int(settings.get("port", 1883))))
    parser.add_argument("--topic", default=os.getenv("MQTT_INPUT_TOPIC", str(settings.get("topic", DEFAULT_TOPIC))))
    parser.add_argument("--qos", type=int, choices=(0, 1, 2), default=int(settings.get("qos", 1)))
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="행 사이의 대기 시간(초). 대기 없는 시험은 0",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="cycle_id와 timestamp를 연속으로 유지하면서 CSV 반복",
    )
    parser.add_argument("--limit", type=int, help="송신할 최대 데이터 행 수")
    parser.add_argument(
        "--log-every",
        type=int,
        default=1,
        help="N행마다 송신 확인 표시(기본값: 모든 행)",
    )
    parser.add_argument("--run-id", default=uuid.uuid4().hex)
    parser.add_argument("--client-id", default=f"press-publisher-{socket.gethostname()}-{os.getpid()}")
    parser.add_argument(
        "--tls", action="store_true", default=bool(settings.get("tls", False)), help="TLS 사용"
    )
    parser.add_argument(
        "--ca-file", type=Path, default=settings.get("ca_file"), help="--tls에서 사용할 CA 인증서"
    )
    parser.add_argument("--keepalive", type=int, default=int(settings.get("keepalive", 60)))
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    args = parser.parse_args()

    if args.interval < 0:
        parser.error("--interval 값은 0 이상이어야 합니다")
    if args.repeat < 1:
        parser.error("--repeat 값은 1 이상이어야 합니다")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 값은 1 이상이어야 합니다")
    if args.log_every < 1:
        parser.error("--log-every 값은 1 이상이어야 합니다")
    return args


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """송신할 CSV의 헤더와 모든 데이터 행을 읽고 Solver 호환성을 검사합니다."""

    # 상대 경로와 사용자 홈 기호를 실제 절대 경로로 변환합니다.
    path = path.expanduser().resolve()

    # 지정한 입력 파일이 없으면 Broker 연결 전에 명확한 오류로 종료합니다.
    if not path.is_file():
        raise FileNotFoundError(f"CSV 파일이 없습니다: {path}")

    # utf-8-sig는 Excel 등에서 저장한 UTF-8 BOM이 있어도 첫 헤더를 정상 인식합니다.
    # newline=""은 csv 모듈이 줄바꿈을 직접 처리하도록 하여 빈 행 생성을 방지합니다.
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        # DictReader는 각 CSV 행을 {열 이름: 값} 형태로 변환합니다.
        reader = csv.DictReader(file)
        # 원본 열 순서를 start/data 메시지에 전달하기 위해 헤더 목록을 보관합니다.
        columns = reader.fieldnames or []
        # 파일이 송신 도중 변경돼도 한 실행의 데이터가 일관되도록 모든 행을 메모리에 읽습니다.
        rows = [dict(row) for row in reader]

    # 헤더가 없으면 Solver가 실시간 입력 CSV를 만들 수 없으므로 오류 처리합니다.
    if not columns:
        raise ValueError(f"CSV 헤더가 없습니다: {path}")

    # 데이터 행이 하나도 없으면 start/end만 보내는 잘못된 실행을 막습니다.
    if not rows:
        raise ValueError(f"CSV 데이터 행이 없습니다: {path}")

    # Solver 계산에 반드시 필요한 의미별 열 그룹을 정의합니다.
    # 이름이 변경된 구형 CSV도 허용하도록 일부 항목은 두 가지 이름을 지원합니다.
    required_groups = (
        ("cycle_id",),
        ("System.active_mode", "active_mode"),
        ("Press.target_pressure_bar_g", "Press_target_pressure_bar_g"),
    )

    # 각 그룹에서 하나의 열도 발견되지 않은 경우 사람이 읽기 쉬운 누락 목록을 만듭니다.
    missing = [" or ".join(group) for group in required_groups if not any(c in columns for c in group)]

    # 필수 입력이 빠진 파일은 MQTT로 보내기 전에 차단합니다.
    if missing:
        raise ValueError("CSV가 Solver 형식과 호환되지 않습니다. 누락 항목: " + ", ".join(missing))

    # 검증된 헤더와 행 목록을 main()에 반환합니다.
    return columns, rows


def parse_timestamp(value: str) -> datetime | None:
    """CSV timestamp 문자열을 datetime으로 변환하고 잘못된 값은 None으로 반환합니다."""

    # 빈 문자열은 변환할 시각이 없다는 의미입니다.
    if not value:
        return None
    try:
        # ISO 형식(예: 2026-07-09 13:00:00)을 Python datetime으로 변환합니다.
        return datetime.fromisoformat(value)
    except ValueError:
        # 반복 송신 보정에 실패해도 원본 값을 그대로 보낼 수 있도록 None을 반환합니다.
        return None


def replay_metadata(rows: list[dict[str, str]]) -> tuple[int, int, timedelta]:
    """CSV 반복 재생 시 ID와 timestamp를 이어 붙이는 데 필요한 간격을 계산합니다."""

    # 한 번의 CSV에서 사용한 가장 큰 cycle_id를 계산하기 위한 목록입니다.
    cycle_ids = []
    # sample_index 열이 있는 CSV의 가장 큰 인덱스를 계산하기 위한 목록입니다.
    sample_indices = []

    # 모든 원본 행에서 숫자로 변환 가능한 ID만 수집합니다.
    for row in rows:
        try:
            cycle_ids.append(int(row.get("cycle_id", "")))
        except ValueError:
            # 숫자가 아닌 cycle_id는 반복 보정에서 제외하고 원본 값으로 송신합니다.
            pass
        try:
            sample_indices.append(int(row.get("sample_index", "")))
        except ValueError:
            # sample_index가 없거나 숫자가 아니면 별도 보정을 하지 않습니다.
            pass

    # 첫 행과 마지막 행의 timestamp를 읽어 한 번 재생하는 시간 범위를 계산합니다.
    first_time = parse_timestamp(rows[0].get("timestamp", ""))
    last_time = parse_timestamp(rows[-1].get("timestamp", ""))

    # 정상 시간 범위이면 마지막 샘플 다음 1초부터 다음 반복이 시작되도록 1초를 더합니다.
    if first_time is not None and last_time is not None and last_time >= first_time:
        timestamp_span = last_time - first_time + timedelta(seconds=1)
    else:
        # timestamp를 해석할 수 없으면 행당 1초라고 가정한 길이를 대체값으로 사용합니다.
        timestamp_span = timedelta(seconds=len(rows))

    # 비어 있는 ID 목록은 0을 사용하여 반복 보정이 원본 값을 바꾸지 않게 합니다.
    return max(cycle_ids, default=0), max(sample_indices, default=0), timestamp_span


def iter_replayed_rows(
    rows: list[dict[str, str]], repeat: int
) -> Iterator[tuple[int, dict[str, str]]]:
    """원본 CSV를 repeat 횟수만큼 이어 붙인 행을 순서대로 생성합니다."""

    # 반복마다 더할 cycle/sample ID 간격과 timestamp 간격을 한 번만 계산합니다.
    cycle_span, sample_span, timestamp_span = replay_metadata(rows)

    # repeat_index는 0부터 시작하며 0번째 반복은 원본 값을 그대로 사용합니다.
    for repeat_index in range(repeat):
        # 현재 반복 구간이 원본보다 얼마나 뒤의 시각인지 계산합니다.
        time_shift = timestamp_span * repeat_index

        # 원본 CSV 행 순서를 유지하여 하나씩 생성합니다.
        for source_row in rows:
            # 원본 rows를 변경하지 않도록 각 행의 얕은 복사본을 만듭니다.
            row = dict(source_row)

            # 두 번째 반복부터 cycle_id, sample_index, timestamp를 연속 값으로 보정합니다.
            if repeat_index:
                try:
                    row["cycle_id"] = str(int(row["cycle_id"]) + cycle_span * repeat_index)
                except (KeyError, ValueError):
                    # cycle_id가 없거나 숫자가 아니면 원본 값을 유지합니다.
                    pass
                if "sample_index" in row:
                    try:
                        row["sample_index"] = str(
                            int(row["sample_index"]) + sample_span * repeat_index
                        )
                    except ValueError:
                        # 숫자가 아닌 sample_index는 원본 값을 유지합니다.
                        pass

                # timestamp를 해석할 수 있을 때만 현재 반복의 시간 간격을 더합니다.
                timestamp = parse_timestamp(row.get("timestamp", ""))
                if timestamp is not None:
                    row["timestamp"] = (timestamp + time_shift).strftime("%Y-%m-%d %H:%M:%S")

            # 메모리에 전체 반복 결과를 만들지 않고 한 행씩 main()에 전달합니다.
            yield repeat_index, row


def make_client(
    args: argparse.Namespace,
    connected: threading.Event,
    connection_finished: threading.Event,
    connection_error: list[str],
) -> mqtt.Client:
    """MQTT 송신용 클라이언트와 Broker 연결 결과 콜백을 구성합니다."""

    # MQTT 클라이언트 객체를 생성합니다.
    # VERSION2는 Paho 2.x 콜백 형식을 사용하며 MQTTv311은 Broker 통신 버전입니다.
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
        """Broker의 연결 응답(CONNACK)이 도착하면 Paho 네트워크 스레드가 호출합니다."""

        # 송신 연결 확인에는 아래 콜백 인자들이 필요하지 않으므로 명시적으로 삭제합니다.
        del client, userdata, flags, properties

        # MQTT reason code 0은 Broker 연결이 정상적으로 수락됐다는 의미입니다.
        if reason_code == 0:
            # main()이 데이터 송신을 시작할 수 있도록 연결 성공 이벤트를 설정합니다.
            connected.set()
        else:
            # 연결 실패 코드를 main()이 최종 오류 메시지에 사용할 수 있도록 보관합니다.
            connection_error.append(str(reason_code))
            # 사용자가 즉시 확인할 수 있도록 표준 오류에 연결 거부 사유를 출력합니다.
            print(f"MQTT 연결 거부: {reason_code}", file=sys.stderr)

        # 성공과 실패 모두 연결 시도가 끝났으므로 main()의 제한 시간 대기를 해제합니다.
        connection_finished.set()

    # 위에서 정의한 연결 콜백을 Paho 클라이언트에 등록합니다.
    client.on_connect = on_connect

    # --tls가 지정된 경우에만 MQTT 자체 TLS를 활성화합니다.
    # 현재 기본 구성은 Tailscale 터널 암호화를 사용하므로 이 분기는 실행되지 않습니다.
    if args.tls:
        # CA 파일을 지정했으면 해당 인증서를 사용하고, 없으면 시스템 기본 CA를 사용합니다.
        client.tls_set(ca_certs=str(args.ca_file) if args.ca_file else None)

    # 연결 콜백과 선택적 TLS 설정이 적용된 송신 클라이언트를 반환합니다.
    return client


def publish_json(client: mqtt.Client, topic: str, qos: int, payload: dict[str, object]) -> None:
    """Python 객체를 UTF-8 JSON으로 변환해 송신하고 Broker 확인까지 기다립니다."""

    # ensure_ascii=False는 한글을 \uXXXX가 아닌 UTF-8 문자 그대로 JSON에 기록합니다.
    # 간결한 separators를 사용하여 네트워크로 보내는 메시지 크기를 줄입니다.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # JSON 문자열을 UTF-8 bytes로 바꿔 지정 topic에 발행합니다.
    # retain=False이므로 Broker는 이 메시지를 새 구독자에게 보관·재전송하지 않습니다.
    result = client.publish(topic, encoded.encode("utf-8"), qos=qos, retain=False)

    # publish 요청을 Paho 전송 대기열에 넣지 못했다면 즉시 송신 실패로 처리합니다.
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT 송신 실패: {mqtt.error_string(result.rc)}")

    # 기본 QoS 1에서는 Broker의 PUBACK이 도착할 때까지 최대 10초 기다립니다.
    # 이 대기 덕분에 송신 완료 로그는 단순 호출 시점이 아니라 Broker 확인 이후 출력됩니다.
    result.wait_for_publish(timeout=10.0)

    # 제한 시간 후에도 발행 완료 상태가 아니면 메시지 유실 가능성이 있어 실행을 중단합니다.
    if not result.is_published():
        raise TimeoutError("MQTT 송신 확인 시간이 초과되었습니다")


def main() -> int:
    # 명령행 인수와 mqtt_settings.json의 Broker 설정을 읽습니다.
    args = parse_args()

    # 입력 CSV 전체를 읽고 헤더 및 Solver 필수 열을 검증합니다.
    columns, rows = read_csv(args.csv)

    # start 메시지에 원본 파일명을 넣기 위해 실제 입력 경로를 계산합니다.
    source_path = args.csv.expanduser().resolve()

    # --repeat를 포함한 전체 송신 예정 행 수를 계산합니다.
    total = len(rows) * args.repeat

    # --limit이 있으면 전체 반복 행 수와 제한값 중 작은 값을 실제 송신량으로 사용합니다.
    if args.limit is not None:
        total = min(total, args.limit)

    # on_connect 콜백이 Broker 연결 성공을 main()에 알리는 이벤트입니다.
    connected = threading.Event()
    # 연결 성공 또는 실패가 확정됐을 때 연결 제한 시간 대기를 해제하는 이벤트입니다.
    connection_finished = threading.Event()
    # on_connect에서 받은 연결 오류 코드를 main()으로 전달하는 목록입니다.
    connection_error: list[str] = []

    # 위 이벤트들을 사용하는 MQTT Publisher 클라이언트를 생성합니다.
    client = make_client(args, connected, connection_finished, connection_error)

    # 모든 data 메시지를 정상 발행했는지 기록하며 end 메시지의 status에 사용합니다.
    completed = False
    # Broker가 확인한 data 메시지 수를 누적합니다.
    sent = 0

    try:
        # 사용자가 현재 접속 대상을 확인할 수 있도록 Broker 주소를 출력합니다.
        print(f"MQTT 연결 중: mqtt://{args.host}:{args.port} ...")

        # Broker에 TCP/MQTT 연결을 시작하며 최종 결과는 on_connect 콜백으로 전달됩니다.
        client.connect(args.host, args.port, args.keepalive)

        # 별도 Paho 네트워크 스레드를 시작해 연결 응답과 PUBACK을 계속 처리합니다.
        client.loop_start()

        # 설정된 제한 시간 동안 on_connect 결과가 오지 않으면 무한 대기를 막고 종료합니다.
        if not connection_finished.wait(args.connect_timeout):
            raise TimeoutError("MQTT 연결 시간이 초과되었습니다")

        # 연결 시도는 끝났지만 connected가 설정되지 않았다면 Broker 연결 실패입니다.
        if not connected.is_set():
            # 콜백에 저장된 최신 오류가 없으면 일반 원인 문구를 사용합니다.
            reason = connection_error[-1] if connection_error else "알 수 없는 원인"
            raise ConnectionError(f"MQTT 연결 실패: {reason}")

        # 첫 메시지로 start를 보내 Solver가 실행ID, CSV 열 순서, 예상 행 수를 준비하게 합니다.
        publish_json(
            client,
            args.topic,
            args.qos,
            {
                "schema": "hydraulic-press.input.v1",
                "type": "start",
                "run_id": args.run_id,
                "source_csv": source_path.name,
                "columns": columns,
                "row_count": total,
            },
        )

        # start 메시지가 Broker에서 확인된 후 실행 규모와 송신 간격을 표시합니다.
        print(
            f"{args.topic!r} 주제로 {total}행 송신 시작 "
            f"(실행ID={args.run_id}, 송신간격={args.interval:g}초)"
        )

        # 첫 행 기준 단조 시계를 저장합니다. 시스템 시간이 변경돼도 일정한 간격을 유지합니다.
        started_at = time.monotonic()

        # 원본 또는 반복 보정된 CSV 행을 한 행씩 순서대로 가져옵니다.
        for _, row in iter_replayed_rows(rows, args.repeat):
            # --limit에 도달하면 남은 원본/반복 행은 송신하지 않고 반복문을 종료합니다.
            if sent >= total:
                break

            # 누적 오차를 줄이기 위해 '이전 송신 후 interval'이 아니라 절대 목표 시각을 계산합니다.
            target_time = started_at + sent * args.interval

            # 현재 시각에서 다음 행의 목표 송신 시각까지 남은 시간을 계산합니다.
            remaining = target_time - time.monotonic()

            # 목표 시각보다 이른 경우에만 대기하며 --interval 0이면 즉시 다음 행을 보냅니다.
            if remaining > 0:
                time.sleep(remaining)

            # sequence는 사람이 읽기 쉽도록 1부터 시작하는 현재 데이터 메시지 순번입니다.
            sequence = sent + 1

            # CSV 한 행을 data 메시지 하나로 만들어 Broker에 발행하고 PUBACK을 기다립니다.
            publish_json(
                client,
                args.topic,
                args.qos,
                {
                    "schema": "hydraulic-press.input.v1",
                    "type": "data",
                    "run_id": args.run_id,
                    "message_id": f"{args.run_id}:{sequence}",
                    "sequence": sequence,
                    "sent_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "columns": columns,
                    "data": row,
                },
            )

            # Broker 확인까지 끝난 sequence만 정상 송신 수로 반영합니다.
            sent = sequence

            # 첫 행, --log-every 간격, 마지막 행은 항상 송신 완료 로그를 표시합니다.
            if sent == 1 or sent % args.log_every == 0 or sent == total:
                print(
                    "[MQTT_송신완료] "
                    f"순번={sent}/{total} "
                    f"사이클ID={row.get('cycle_id', '')} "
                    f"시각={row.get('timestamp', '')} "
                    f"메시지ID={args.run_id}:{sent}",
                    flush=True,
                )

        # 계획한 모든 data 메시지를 보낸 경우 end 상태를 completed로 만들기 위해 표시합니다.
        completed = True
        # 정상 종료 코드를 반환하지만 finally가 먼저 실행되어 end 메시지를 발행합니다.
        return 0
    except KeyboardInterrupt:
        # Ctrl+C가 입력되면 aborted end 메시지를 보낼 수 있도록 completed=False를 유지합니다.
        print("사용자 요청으로 중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:
        # 연결, JSON 변환, PUBACK 대기 등 모든 송신 오류를 한글 로그로 표시합니다.
        print(f"Publisher 오류: {exc}", file=sys.stderr)
        return 1
    finally:
        # Broker 연결이 성공한 경우에만 이번 실행을 닫는 end 메시지를 시도합니다.
        if connected.is_set():
            try:
                # completed는 전체 송신 성공 여부이며 sent는 실제 PUBACK을 받은 행 수입니다.
                publish_json(
                    client,
                    args.topic,
                    args.qos,
                    {
                        "schema": "hydraulic-press.input.v1",
                        "type": "end",
                        "run_id": args.run_id,
                        "row_count": sent,
                        "status": "completed" if completed else "aborted",
                    },
                )
            except Exception as exc:
                # 본래 오류나 종료 코드를 덮지 않고 종료 메시지 실패만 별도로 기록합니다.
                print(f"종료 메시지를 송신하지 못했습니다: {exc}", file=sys.stderr)

        # MQTT DISCONNECT 패킷을 전송하여 Broker에 정상 연결 종료를 알립니다.
        client.disconnect()

        # Paho 네트워크 스레드를 정지하고 남은 콜백 처리를 마무리합니다.
        client.loop_stop()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Visual Studio에서 Broker, Solver, Publisher를 안전한 순서로 시작합니다."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


MQTT_DIR = Path(__file__).resolve().parent
ROOT = MQTT_DIR.parent
SETTINGS_FILE = ROOT / "mqtt_settings.json"
BROKER_EXE = Path(os.getenv("MOSQUITTO_EXE", r"C:\Program Files\mosquitto\mosquitto.exe"))
BROKER_CONFIG = MQTT_DIR / "mosquitto-tailscale.generated.conf"
SOLVER_SCRIPT = MQTT_DIR / "Solver.py"
PUBLISHER_SCRIPT = MQTT_DIR / "Publisher.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="전체 MQTT Solver 실행 절차를 시작합니다.")
    parser.add_argument("--limit", type=int, default=10, help="Visual Studio 간단 시험에 사용할 행 수")
    parser.add_argument("--interval", type=float, default=0.0, help="Publisher 행 사이의 대기 시간(초)")
    parser.add_argument("--full", action="store_true", help="Publisher 기본값으로 전체 행 송신")
    parser.add_argument("--broker-timeout", type=float, default=15.0)
    parser.add_argument("--solver-ready-timeout", type=float, default=20.0)
    parser.add_argument("--solver-finish-timeout", type=float, default=180.0)
    return parser.parse_args()


def load_endpoint() -> tuple[str, int]:
    with SETTINGS_FILE.open("r", encoding="utf-8-sig") as file:
        settings = json.load(file)
    broker = settings["broker"]
    return str(broker["host"]), int(broker["port"])


def require_file(path: Path, description: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{description} 파일이 없습니다: {path}")


def port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_broker(
    host: str, port: int, process: subprocess.Popen[str] | None, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_open(host, port):
            return
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Mosquitto가 종료되었습니다. 종료 코드: {process.returncode}")
        time.sleep(0.2)
    raise TimeoutError(f"MQTT broker가 {timeout:g}초 안에 {host}:{port}를 열지 못했습니다")


def relay_output(
    process: subprocess.Popen[str], prefix: str, ready: threading.Event | None = None
) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        print(f"[{prefix}] {line}", end="", flush=True)
        if ready is not None and "MQTT 구독 완료:" in line:
            ready.set()


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def main() -> int:
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit 값은 1 이상이어야 합니다")
    if args.interval < 0:
        raise SystemExit("--interval 값은 0 이상이어야 합니다")

    require_file(SETTINGS_FILE, "MQTT 설정")
    require_file(SOLVER_SCRIPT, "MQTT Solver")
    require_file(PUBLISHER_SCRIPT, "MQTT Publisher")
    host, port = load_endpoint()

    common_environment = os.environ.copy()
    common_environment["PYTHONUTF8"] = "1"
    common_environment["PYTHONUNBUFFERED"] = "1"

    broker_process: subprocess.Popen[str] | None = None
    solver_process: subprocess.Popen[str] | None = None
    broker_owned = False

    try:
        print(f"[실행기] MQTT 접속 주소: {host}:{port}", flush=True)
        if port_is_open(host, port):
            print("[실행기] Broker에 이미 연결할 수 있어 실행 중인 Broker를 사용합니다.", flush=True)
        else:
            require_file(BROKER_EXE, "Mosquitto 실행")
            require_file(
                BROKER_CONFIG,
                "생성된 Mosquitto 설정(먼저 Setup-MqttBroker.ps1 실행)",
            )
            print("[실행기] Tailscale Mosquitto Broker 시작 중...", flush=True)
            broker_process = subprocess.Popen(
                [str(BROKER_EXE), "-c", str(BROKER_CONFIG), "-v"],
                cwd=MQTT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            broker_owned = True
            threading.Thread(
                target=relay_output,
                args=(broker_process, "브로커"),
                name="broker-output",
                daemon=True,
            ).start()
            wait_for_broker(host, port, broker_process, args.broker_timeout)
            print("[실행기] Broker 준비 완료.", flush=True)

        solver_ready = threading.Event()
        print("[실행기] MQTT Solver 시작 중...", flush=True)
        solver_process = subprocess.Popen(
            [sys.executable, "-u", str(SOLVER_SCRIPT)],
            cwd=ROOT,
            env=common_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(
            target=relay_output,
            args=(solver_process, "Solver", solver_ready),
            name="solver-output",
            daemon=True,
        ).start()
        if not solver_ready.wait(args.solver_ready_timeout):
            if solver_process.poll() is not None:
                raise RuntimeError(f"MQTT Solver가 종료되었습니다. 종료 코드: {solver_process.returncode}")
            raise TimeoutError("제한 시간 안에 MQTT Solver 구독이 완료되지 않았습니다")

        publisher_command = [sys.executable, "-u", str(PUBLISHER_SCRIPT)]
        if not args.full:
            publisher_command.extend(["--limit", str(args.limit), "--interval", str(args.interval)])
        print("[실행기] Solver 구독 완료. Publisher를 시작합니다...", flush=True)
        publisher_result = subprocess.run(
            publisher_command,
            cwd=ROOT,
            env=common_environment,
            check=False,
        )
        if publisher_result.returncode != 0:
            raise RuntimeError(f"Publisher가 종료되었습니다. 종료 코드: {publisher_result.returncode}")

        timeout = None if args.full else args.solver_finish_timeout
        try:
            solver_result = solver_process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Publisher 종료 메시지 후에도 Solver 계산이 끝나지 않았습니다") from exc
        if solver_result != 0:
            raise RuntimeError(f"MQTT Solver가 종료되었습니다. 종료 코드: {solver_result}")

        print("[실행기] MQTT 전체 처리가 정상적으로 완료되었습니다.", flush=True)
        print(f"[실행기] 결과 파일: {ROOT / 'realtime_solver_project' / 'realtime_solver_output.csv'}")
        return 0
    except KeyboardInterrupt:
        print("\n[실행기] 사용자 요청으로 중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"[실행기] 오류: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop_process(solver_process)
        if broker_owned:
            stop_process(broker_process)


if __name__ == "__main__":
    raise SystemExit(main())

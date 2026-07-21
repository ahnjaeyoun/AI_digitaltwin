"""MQTT 원천데이터와 Solver 결과를 TimescaleDB에 저장합니다."""

from __future__ import annotations

import csv
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _event_time(value: object) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now().astimezone()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _integer(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _boolean(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


@dataclass
class PendingRecord:
    kind: str
    values: dict[str, Any]
    description: str
    ordinal: int


class TimescaleStore:
    """연결 장애 시 메모리에 대기시킨 뒤 재연결하여 순서대로 저장합니다."""

    def __init__(
        self,
        dsn: str,
        schema: str = "public",
        *,
        required: bool = False,
        initialize: bool = True,
        connect_timeout: int = 5,
        retry_seconds: float = 5.0,
        log_every: int = 1,
    ) -> None:
        self.dsn = dsn
        self.schema = schema
        self.required = required
        self.initialize = initialize
        self.connect_timeout = max(1, connect_timeout)
        self.retry_seconds = max(0.2, retry_seconds)
        self.log_every = max(1, log_every)
        self.connection: Any = None
        self.pending: deque[PendingRecord] = deque()
        self.next_retry_at = 0.0
        self.last_error = ""
        self.raw_stored = 0
        self.results_stored = 0
        self._psycopg: Any = None
        self._sql: Any = None
        self._jsonb: Any = None

    def start(self) -> bool:
        try:
            self._connect()
            return True
        except Exception as exc:
            self.close()
            self.next_retry_at = time.monotonic() + self.retry_seconds
            print(
                f"[TIMESCALEDB_연결대기] 오류={exc}",
                file=sys.stderr,
                flush=True,
            )
            if self.required:
                raise RuntimeError(f"TimescaleDB 연결 실패: {exc}") from exc
            return False

    def _load_driver(self) -> None:
        if self._psycopg is not None:
            return
        try:
            import psycopg
            from psycopg import sql
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError(
                "psycopg가 설치되어 있지 않습니다. "
                "py -m pip install -r mqtt\\requirements.txt를 실행하세요."
            ) from exc
        self._psycopg = psycopg
        self._sql = sql
        self._jsonb = Jsonb

    def _connect(self) -> None:
        if self.connection is not None and not self.connection.closed:
            return
        self._load_driver()
        self.connection = self._psycopg.connect(
            self.dsn,
            autocommit=True,
            connect_timeout=self.connect_timeout,
        )
        if self.initialize:
            self._initialize_schema()
        print(
            f"[TIMESCALEDB_연결완료] 스키마={self.schema}",
            flush=True,
        )

    def _initialize_schema(self) -> None:
        assert self.connection is not None
        sql = self._sql
        raw_table = sql.Identifier(self.schema, "press_raw_data")
        result_table = sql.Identifier(self.schema, "press_solver_results")

        self.connection.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        self.connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
        )
        self.connection.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    event_time TIMESTAMPTZ NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    run_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sequence BIGINT,
                    mqtt_topic TEXT NOT NULL,
                    cycle_id TEXT,
                    cycle_phase TEXT,
                    active_mode TEXT,
                    payload JSONB NOT NULL,
                    PRIMARY KEY (event_time, message_id)
                )
                """
            ).format(raw_table)
        )
        self.connection.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    event_time TIMESTAMPTZ NOT NULL,
                    stored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    run_id TEXT NOT NULL,
                    input_row_index BIGINT NOT NULL,
                    cycle_id TEXT,
                    cycle_phase TEXT,
                    active_mode TEXT,
                    calculated_flow_rate_l_min DOUBLE PRECISION,
                    target_pressure_bar_g DOUBLE PRECISION,
                    target_pressure_error_bar DOUBLE PRECISION,
                    hydraulic_power_kw DOUBLE PRECISION,
                    solver_warning BOOLEAN,
                    status TEXT,
                    result JSONB NOT NULL,
                    PRIMARY KEY (event_time, run_id, input_row_index)
                )
                """
            ).format(result_table)
        )
        for table_name in ("press_raw_data", "press_solver_results"):
            qualified_name = f"{self.schema}.{table_name}"
            self.connection.execute(
                sql.SQL(
                    "SELECT create_hypertable({}, by_range('event_time'), "
                    "if_not_exists => TRUE)"
                ).format(sql.Literal(qualified_name))
            )

    def add_raw(
        self,
        *,
        mqtt_topic: str,
        payload: dict[str, Any],
        row: dict[str, Any],
    ) -> None:
        sequence = _integer(payload.get("sequence"))
        message_id = str(payload.get("message_id", ""))
        record = PendingRecord(
            kind="raw",
            values={
                "event_time": _event_time(row.get("timestamp")),
                "run_id": str(payload.get("run_id", "")),
                "message_id": message_id,
                "sequence": sequence,
                "mqtt_topic": mqtt_topic,
                "cycle_id": str(row.get("cycle_id", "")),
                "cycle_phase": str(row.get("cycle_phase", "")),
                "active_mode": str(
                    row.get("System.active_mode", row.get("active_mode", ""))
                ),
                "payload": payload,
            },
            description=f"종류=원천데이터 메시지ID={message_id}",
            ordinal=sequence or 0,
        )
        self.pending.append(record)
        self.flush_pending()

    def add_result(self, *, run_id: str, row: dict[str, Any]) -> None:
        input_row_index = _integer(row.get("input_row_index"))
        if input_row_index is None:
            raise ValueError("Solver 결과에 input_row_index가 없습니다")
        record = PendingRecord(
            kind="result",
            values={
                "event_time": _event_time(row.get("timestamp")),
                "run_id": run_id,
                "input_row_index": input_row_index,
                "cycle_id": str(row.get("cycle_id", "")),
                "cycle_phase": str(row.get("cycle_phase", "")),
                "active_mode": str(row.get("active_mode", "")),
                "calculated_flow_rate_l_min": _number(
                    row.get("calculated_flow_rate_L_min")
                ),
                "target_pressure_bar_g": _number(
                    row.get("Press_target_pressure_bar_g", row.get("target_pressure_bar_g"))
                ),
                "target_pressure_error_bar": _number(
                    row.get("target_pressure_error_bar")
                ),
                "hydraulic_power_kw": _number(row.get("hydraulic_power_kW")),
                "solver_warning": _boolean(row.get("solver_warning")),
                "status": str(row.get("status", "")),
                "result": row,
            },
            description=f"종류=Solver결과 입력행={input_row_index + 1}",
            ordinal=input_row_index + 1,
        )
        self.pending.append(record)
        self.flush_pending()

    def _insert(self, record: PendingRecord) -> None:
        assert self.connection is not None
        sql = self._sql
        if record.kind == "raw":
            table = sql.Identifier(self.schema, "press_raw_data")
            values = record.values
            self.connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        event_time, run_id, message_id, sequence, mqtt_topic,
                        cycle_id, cycle_phase, active_mode, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_time, message_id) DO NOTHING
                    """
                ).format(table),
                (
                    values["event_time"],
                    values["run_id"],
                    values["message_id"],
                    values["sequence"],
                    values["mqtt_topic"],
                    values["cycle_id"],
                    values["cycle_phase"],
                    values["active_mode"],
                    self._jsonb(values["payload"]),
                ),
            )
            self.raw_stored += 1
        else:
            table = sql.Identifier(self.schema, "press_solver_results")
            values = record.values
            self.connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        event_time, run_id, input_row_index, cycle_id,
                        cycle_phase, active_mode, calculated_flow_rate_l_min,
                        target_pressure_bar_g, target_pressure_error_bar,
                        hydraulic_power_kw, solver_warning, status, result
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_time, run_id, input_row_index) DO NOTHING
                    """
                ).format(table),
                (
                    values["event_time"],
                    values["run_id"],
                    values["input_row_index"],
                    values["cycle_id"],
                    values["cycle_phase"],
                    values["active_mode"],
                    values["calculated_flow_rate_l_min"],
                    values["target_pressure_bar_g"],
                    values["target_pressure_error_bar"],
                    values["hydraulic_power_kw"],
                    values["solver_warning"],
                    values["status"],
                    self._jsonb(values["result"]),
                ),
            )
            self.results_stored += 1

        if record.ordinal == 1 or record.ordinal % self.log_every == 0:
            print(f"[TIMESCALEDB_저장완료] {record.description}", flush=True)

    def flush_pending(self, *, force: bool = False) -> int:
        if not self.pending:
            return 0
        if not force and time.monotonic() < self.next_retry_at:
            return 0
        stored = 0
        try:
            self._connect()
            while self.pending:
                self._insert(self.pending[0])
                self.pending.popleft()
                stored += 1
            self.last_error = ""
            return stored
        except Exception as exc:
            self.close()
            self.next_retry_at = time.monotonic() + self.retry_seconds
            message = str(exc)
            if message != self.last_error:
                print(
                    "[TIMESCALEDB_저장대기] "
                    f"대기건수={len(self.pending)} 오류={message}",
                    file=sys.stderr,
                    flush=True,
                )
                self.last_error = message
            if self.required:
                raise RuntimeError(f"TimescaleDB 저장 실패: {exc}") from exc
            return stored

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self.pending and time.monotonic() < deadline:
            self.next_retry_at = 0.0
            self.flush_pending(force=True)
            if self.pending:
                time.sleep(min(self.retry_seconds, 1.0))
        if self.pending:
            print(
                f"[TIMESCALEDB_미저장] 종료 시 남은 건수={len(self.pending)}",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None


class SolverResultTail:
    """Solver 출력 CSV에 새로 추가되는 행만 순서대로 읽습니다."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.file: Any = None
        self.reader: csv.DictReader | None = None
        self.rows_read = 0

    def poll(
        self,
        store: TimescaleStore | None,
        run_id: str,
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> int:
        """새 결과 행을 읽어 선택적으로 DB 저장 및 외부 콜백에 전달합니다."""

        if self.file is None:
            if not self.path.is_file():
                return 0
            self.file = self.path.open("r", encoding="utf-8-sig", newline="")
            self.reader = csv.DictReader(self.file)
        assert self.reader is not None
        added = 0
        for row in self.reader:
            result = dict(row)
            if store is not None:
                store.add_result(run_id=run_id, row=result)
            if on_result is not None:
                on_result(result)
            self.rows_read += 1
            added += 1
        return added

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
            self.reader = None

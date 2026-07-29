"""Persist anomaly and risk inference result messages in TimescaleDB."""

from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any


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


@dataclass
class PendingInference:
    values: dict[str, Any]
    description: str
    ordinal: int


class InferenceTimescaleStore:
    """Store inference results, retrying transient database failures in order."""

    def __init__(
        self,
        dsn: str,
        result_kind: str,
        schema: str = "public",
        *,
        required: bool = False,
        initialize: bool = True,
        connect_timeout: int = 5,
        retry_seconds: float = 5.0,
        log_every: int = 1,
    ) -> None:
        if result_kind not in {"anomaly", "risk"}:
            raise ValueError(f"지원하지 않는 추론 결과 종류입니다: {result_kind}")
        self.dsn = dsn
        self.result_kind = result_kind
        self.schema = schema
        self.required = required
        self.initialize = initialize
        self.connect_timeout = max(1, connect_timeout)
        self.retry_seconds = max(0.2, retry_seconds)
        self.log_every = max(1, log_every)
        self.connection: Any = None
        self.pending: deque[PendingInference] = deque()
        self.next_retry_at = 0.0
        self.last_error = ""
        self.stored = 0
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
                f"[TIMESCALEDB_연결대기] 종류={self.result_kind} 오류={exc}",
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
                "psycopg가 설치되어 있지 않습니다. requirements.txt를 설치하세요."
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
            f"[TIMESCALEDB_연결완료] 종류={self.result_kind} 스키마={self.schema}",
            flush=True,
        )

    def _initialize_schema(self) -> None:
        assert self.connection is not None
        sql = self._sql
        anomaly_table = sql.Identifier(self.schema, "press_anomaly_results")
        risk_table = sql.Identifier(self.schema, "press_risk_results")

        self.connection.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        self.connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(self.schema)
            )
        )
        self.connection.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    event_time TIMESTAMPTZ NOT NULL,
                    stored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    run_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    source_message_id TEXT,
                    input_row_index BIGINT,
                    line_number INTEGER NOT NULL,
                    cycle_id TEXT,
                    cycle_phase TEXT,
                    status TEXT,
                    reconstruction_error DOUBLE PRECISION,
                    anomaly_score DOUBLE PRECISION,
                    is_anomaly BOOLEAN,
                    cycle_is_anomaly BOOLEAN,
                    likely_cause TEXT,
                    confidence DOUBLE PRECISION,
                    result JSONB NOT NULL,
                    PRIMARY KEY (event_time, message_id)
                )
                """
            ).format(anomaly_table)
        )
        self.connection.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    event_time TIMESTAMPTZ NOT NULL,
                    stored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    run_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    source_message_id TEXT,
                    input_row_index BIGINT,
                    line_number INTEGER NOT NULL,
                    cycle_id TEXT,
                    cycle_phase TEXT,
                    status TEXT,
                    score_percent DOUBLE PRECISION,
                    linear_score_percent DOUBLE PRECISION,
                    reconstruction_error DOUBLE PRECISION,
                    alert BOOLEAN,
                    window_rows INTEGER,
                    history_coverage_percent DOUBLE PRECISION,
                    p95 DOUBLE PRECISION,
                    p99 DOUBLE PRECISION,
                    maintenance_status TEXT,
                    recommendation TEXT,
                    result JSONB NOT NULL,
                    PRIMARY KEY (event_time, message_id)
                )
                """
            ).format(risk_table)
        )
        for table_name in ("press_anomaly_results", "press_risk_results"):
            qualified_name = f"{self.schema}.{table_name}"
            self.connection.execute(
                sql.SQL(
                    "SELECT create_hypertable({}, by_range('event_time'), "
                    "if_not_exists => TRUE)"
                ).format(sql.Literal(qualified_name))
            )

    def add_result(self, result: dict[str, Any]) -> None:
        message_id = str(result.get("message_id", "")).strip()
        if not message_id:
            raise ValueError("추론 결과에 message_id가 없습니다")
        ordinal = _integer(result.get("input_row_index")) or 0
        record = PendingInference(
            values={
                "event_time": _event_time(result.get("published_at")),
                "run_id": str(result.get("run_id", "")),
                "message_id": message_id,
                "source_message_id": str(result.get("source_message_id", "")),
                "input_row_index": _integer(result.get("input_row_index")),
                "line_number": _integer(result.get("line_number")) or 0,
                "cycle_id": str(result.get("cycle_id", "")),
                "cycle_phase": str(result.get("cycle_phase", "")),
                "status": str(result.get("status", "")),
                "result": result,
            },
            description=(
                f"종류={self.result_kind} 메시지ID={message_id} "
                f"라인={result.get('line_number')}"
            ),
            ordinal=ordinal + 1,
        )
        self.pending.append(record)
        self.flush_pending()

    def _insert(self, record: PendingInference) -> None:
        assert self.connection is not None
        if self.result_kind == "anomaly":
            self._insert_anomaly(record.values)
        else:
            self._insert_risk(record.values)
        self.stored += 1
        if record.ordinal == 1 or record.ordinal % self.log_every == 0:
            print(f"[TIMESCALEDB_저장완료] {record.description}", flush=True)

    def _insert_anomaly(self, values: dict[str, Any]) -> None:
        inference = values["result"].get("inference", {})
        cycle = values["result"].get("cycle", {})
        analysis = values["result"].get("analysis", {})
        table = self._sql.Identifier(self.schema, "press_anomaly_results")
        self.connection.execute(
            self._sql.SQL(
                """
                INSERT INTO {} (
                    event_time, run_id, message_id, source_message_id,
                    input_row_index, line_number, cycle_id, cycle_phase, status,
                    reconstruction_error, anomaly_score, is_anomaly,
                    cycle_is_anomaly, likely_cause, confidence, result
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (event_time, message_id) DO NOTHING
                """
            ).format(table),
            (
                values["event_time"],
                values["run_id"],
                values["message_id"],
                values["source_message_id"],
                values["input_row_index"],
                values["line_number"],
                values["cycle_id"],
                values["cycle_phase"],
                values["status"],
                _number(inference.get("recon_error")),
                _number(inference.get("score")),
                inference.get("is_anomaly"),
                cycle.get("cycle_is_anomaly"),
                str(analysis.get("likely_cause", "")),
                _number(analysis.get("confidence")),
                self._jsonb(values["result"]),
            ),
        )

    def _insert_risk(self, values: dict[str, Any]) -> None:
        risk = values["result"].get("risk", {})
        maintenance = values["result"].get("maintenance", {})
        table = self._sql.Identifier(self.schema, "press_risk_results")
        self.connection.execute(
            self._sql.SQL(
                """
                INSERT INTO {} (
                    event_time, run_id, message_id, source_message_id,
                    input_row_index, line_number, cycle_id, cycle_phase, status,
                    score_percent, linear_score_percent, reconstruction_error,
                    alert, window_rows, history_coverage_percent, p95, p99,
                    maintenance_status, recommendation, result
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (event_time, message_id) DO NOTHING
                """
            ).format(table),
            (
                values["event_time"],
                values["run_id"],
                values["message_id"],
                values["source_message_id"],
                values["input_row_index"],
                values["line_number"],
                values["cycle_id"],
                values["cycle_phase"],
                values["status"],
                _number(risk.get("score_percent")),
                _number(risk.get("linear_score_percent")),
                _number(risk.get("reconstruction_error")),
                risk.get("alert"),
                _integer(risk.get("window_rows")),
                _number(risk.get("history_coverage_percent")),
                _number(risk.get("p95")),
                _number(risk.get("p99")),
                str(maintenance.get("status", maintenance.get("level", ""))),
                str(maintenance.get("recommendation", "")),
                self._jsonb(values["result"]),
            ),
        )

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
                    f"종류={self.result_kind} 대기건수={len(self.pending)} "
                    f"오류={message}",
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
                f"[TIMESCALEDB_미저장] 종류={self.result_kind} "
                f"종료 시 남은 건수={len(self.pending)}",
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

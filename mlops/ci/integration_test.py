"""TimescaleDB에 전체 디지털 트윈 파이프라인 결과가 저장됐는지 검사합니다."""

from __future__ import annotations

import math
import os
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

import psycopg
from psycopg import sql


REQUIRED_TABLES = (
    "press_raw_data",
    "press_solver_results",
    "press_anomaly_results",
    "press_risk_results",
)


def write_junit(
    report_path: Path,
    *,
    elapsed: float,
    counts: dict[str, int] | None = None,
    failure: str | None = None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    suite = ET.Element(
        "testsuite",
        {
            "name": "digital-twin-compose-integration",
            "tests": "1",
            "failures": "1" if failure else "0",
            "errors": "0",
            "time": f"{elapsed:.3f}",
        },
    )
    case = ET.SubElement(
        suite,
        "testcase",
        {
            "classname": "pipeline.timescaledb",
            "name": "all_pipeline_results_are_persisted",
            "time": f"{elapsed:.3f}",
        },
    )
    if failure:
        node = ET.SubElement(case, "failure", {"message": failure.splitlines()[0]})
        node.text = failure
    else:
        output = ET.SubElement(case, "system-out")
        output.text = "\n".join(
            f"{table}={count}" for table, count in sorted((counts or {}).items())
        )
    ET.ElementTree(suite).write(report_path, encoding="utf-8", xml_declaration=True)


def table_counts(connection: psycopg.Connection, schema: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name in REQUIRED_TABLES:
        query = sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(schema),
            sql.Identifier(table_name),
        )
        counts[table_name] = int(connection.execute(query).fetchone()[0])
    return counts


def assert_solver_values_are_finite(
    connection: psycopg.Connection, schema: str
) -> None:
    query = sql.SQL(
        """
        SELECT calculated_flow_rate_l_min,
               target_pressure_bar_g,
               target_pressure_error_bar,
               hydraulic_power_kw
        FROM {}.{}
        ORDER BY event_time DESC
        LIMIT 100
        """
    ).format(
        sql.Identifier(schema),
        sql.Identifier("press_solver_results"),
    )
    for row_index, row in enumerate(connection.execute(query), start=1):
        for column_index, value in enumerate(row, start=1):
            if value is not None and not math.isfinite(float(value)):
                raise AssertionError(
                    "Solver 결과에 유한하지 않은 값이 있습니다: "
                    f"row={row_index}, column={column_index}, value={value}"
                )


def run() -> dict[str, int]:
    timeout = float(os.getenv("TEST_TIMEOUT_SECONDS", "240"))
    schema = os.getenv("TIMESCALE_SCHEMA", "public")
    deadline = time.monotonic() + timeout
    last_error = ""
    last_counts: dict[str, int] = {}

    while time.monotonic() < deadline:
        try:
            with psycopg.connect(connect_timeout=5) as connection:
                last_counts = table_counts(connection, schema)
                if all(count > 0 for count in last_counts.values()):
                    assert_solver_values_are_finite(connection, schema)
                    return last_counts
                last_error = f"결과 대기 중: {last_counts}"
        except (psycopg.Error, KeyError) as exc:
            last_error = str(exc)
        print(f"[CI] {last_error}", flush=True)
        time.sleep(2)

    raise TimeoutError(
        f"{timeout:.0f}초 안에 모든 결과가 저장되지 않았습니다. "
        f"마지막 건수={last_counts}, 마지막 오류={last_error}"
    )


def main() -> int:
    started = time.monotonic()
    report_path = Path(
        os.getenv("TEST_REPORT_PATH", "/reports/integration.xml")
    )
    try:
        counts = run()
    except Exception as exc:
        failure = "".join(traceback.format_exception(exc))
        write_junit(
            report_path,
            elapsed=time.monotonic() - started,
            failure=failure,
        )
        print(failure, file=sys.stderr, flush=True)
        return 1

    elapsed = time.monotonic() - started
    write_junit(report_path, elapsed=elapsed, counts=counts)
    print(f"[CI PASS] TimescaleDB 결과 건수={counts}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

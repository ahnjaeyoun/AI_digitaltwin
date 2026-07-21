-- TimescaleDB 관리자 계정으로 최초 한 번 실행할 수 있는 수동 초기화 스크립트입니다.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS public.press_raw_data (
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
);

CREATE TABLE IF NOT EXISTS public.press_solver_results (
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
);

SELECT create_hypertable(
    'public.press_raw_data',
    by_range('event_time'),
    if_not_exists => TRUE
);

SELECT create_hypertable(
    'public.press_solver_results',
    by_range('event_time'),
    if_not_exists => TRUE
);

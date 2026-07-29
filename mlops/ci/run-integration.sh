#!/bin/sh
set -eu

: "${COMPOSE_PROJECT_NAME:?COMPOSE_PROJECT_NAME must be set}"
: "${TIMESCALE_PASSWORD:?TIMESCALE_PASSWORD must be set}"

compose_files="-f compose.yaml -f compose.ci.yaml"

mkdir -p reports
chmod 0777 reports

docker compose ${compose_files} config --quiet
docker compose ${compose_files} up -d --wait broker timescaledb
docker compose ${compose_files} up -d raw-solver anomaly risk
docker compose ${compose_files} --profile simulation run --rm raw-publisher
docker compose ${compose_files} run --rm ci-tester

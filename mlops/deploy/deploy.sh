#!/bin/sh
set -eu

: "${REGISTRY_PREFIX:?REGISTRY_PREFIX must be set}"
: "${IMAGE_TAG:?IMAGE_TAG must be set}"

if [ ! -f .env ]; then
    echo "배포용 .env 파일이 없습니다." >&2
    exit 1
fi

compose_files="-f compose.yaml -f compose.production.yaml"

docker compose --env-file .env ${compose_files} config --quiet
docker compose --env-file .env ${compose_files} pull \
    broker timescaledb raw-solver anomaly risk
docker compose --env-file .env ${compose_files} up \
    -d --no-build --remove-orphans --wait --wait-timeout 180 \
    broker timescaledb raw-solver anomaly risk
docker compose --env-file .env ${compose_files} ps

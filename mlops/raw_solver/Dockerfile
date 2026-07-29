FROM gcc:12-bookworm AS solver-builder

WORKDIR /build

COPY realtime_solver_project/main.cpp realtime_solver_project/json.hpp ./

RUN g++ \
    -std=c++20 \
    -O2 \
    -pthread \
    main.cpp \
    -o hydraulic_solver


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY mqtt/requirements.txt ./mqtt/requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r mqtt/requirements.txt

COPY mqtt/Publisher.py mqtt/Solver.py mqtt/TimescaleStore.py ./mqtt/
COPY mqtt_settings.json ./
COPY data/input_gradual.csv ./data/input_gradual.csv
COPY realtime_solver_project/network.json ./realtime_solver_project/network.json
COPY --from=solver-builder /build/hydraulic_solver ./realtime_solver_project/hydraulic_solver

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "mqtt/Solver.py"]

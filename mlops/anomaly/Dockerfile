FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TF_CPP_MIN_LOG_LEVEL=2

WORKDIR /app

COPY requirements.txt ./

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY mqtt_inference.py inference_timescale_store.py mqtt_settings.json ./
COPY models/press_ae_standalone_io.json ./models/press_ae_standalone_io.json
COPY models/press_ae_standalone_savedmodel ./models/press_ae_standalone_savedmodel

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "mqtt_inference.py"]

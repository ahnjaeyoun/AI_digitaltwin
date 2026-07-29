FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /test

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY integration_test.py ./

RUN useradd --create-home --uid 10001 testuser \
    && mkdir -p /reports \
    && chown -R testuser:testuser /test /reports

USER testuser

CMD ["python", "integration_test.py"]

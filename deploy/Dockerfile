FROM python:3.13.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

RUN groupadd --system gateway && useradd --system --gid gateway --home /app gateway

WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --requirement requirements.lock
COPY --chown=gateway:gateway gateway ./gateway

USER gateway
EXPOSE 8320

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8320/ready', timeout=2)"

ENTRYPOINT ["python", "-m", "gateway"]

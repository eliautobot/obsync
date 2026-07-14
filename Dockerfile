FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OBSYNC_DATA_DIR=/data \
    OBSYNC_VAULT_PATH=/vault \
    OBSYNC_HOST=0.0.0.0 \
    OBSYNC_PORT=7769

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install ".[ocr]"

RUN groupadd --gid 1000 obsync \
    && useradd --uid 1000 --gid obsync --create-home obsync \
    && mkdir -p /data /vault \
    && chown -R obsync:obsync /data /vault

USER obsync
EXPOSE 7769
VOLUME ["/data", "/vault"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:7769/api/v1/health || exit 1

ENTRYPOINT ["obsync"]
CMD ["server"]


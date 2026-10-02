# Smart Guided Troubleshooting Engine — production image
# BASE_IMAGE can point at a registry mirror (e.g. mirror.gcr.io/library/python:3.11-slim).
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBEDDING_CACHE_DIR=/opt/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Optional: trust an extra CA (builds behind a TLS-inspecting corporate proxy). No-op by default.
ARG EXTRA_CA_CERT_B64=""
RUN if [ -n "$EXTRA_CA_CERT_B64" ]; then \
      echo "$EXTRA_CA_CERT_B64" | base64 -d > /usr/local/share/ca-certificates/extra-ca.crt && update-ca-certificates; \
    fi

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the local embedding model into the image: the container needs no model download at start-up.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(model_name=os.environ['EMBEDDING_MODEL'], cache_dir='/opt/models')"

COPY schema.py ./
COPY app ./app
COPY scripts ./scripts
COPY data ./data
COPY artifacts ./artifacts

# Build the persisted BM25/dense indexes at image build time (no per-request or per-start rebuild).
RUN python scripts/build_indexes.py > /tmp/index_report.json && cat /tmp/index_report.json

RUN useradd --uid 10001 --create-home appuser && chown -R appuser:appuser /app /opt/models
USER appuser

EXPOSE 8000
# /health returns 200 when catalog, indexes, embedding model and cache are ready (and the LLM, when a key is set).
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header"]

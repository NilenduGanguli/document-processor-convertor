FROM python:3.12-slim

# curl for the container healthcheck only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY dpc ./dpc
RUN pip install --no-cache-dir .

# The SPA; api.py serves it with an index.html fallback (CWD-relative in the container).
COPY frontend/dist ./frontend/dist

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8300/health || exit 1

CMD ["uvicorn", "dpc.api:app", "--host", "0.0.0.0", "--port", "8300"]

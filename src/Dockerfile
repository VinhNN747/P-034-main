# ---- Stage 1: Build ----
FROM python:3.11-slim AS builder

WORKDIR /app

ARG REQUIREMENTS_FILE=requirements-backend.txt
COPY requirements*.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --timeout 90 --retries 3 --no-cache-dir -r ${REQUIREMENTS_FILE}

# ---- Stage 2: Production ----
FROM python:3.11-slim

WORKDIR /app

# The virtualenv is readable by the non-root runtime user.
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Security: run as non-root user
RUN useradd -m appuser

# Copy application code
COPY . ./src/

# Create data directory with correct ownership
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "src.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

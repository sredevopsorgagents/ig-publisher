# syntax=docker/dockerfile:1.6

# --- Stage 1: Builder ---
FROM docker.io/python:3.12-slim AS builder

WORKDIR /build

# Create a virtual environment to isolate dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

# --- Stage 2: Runtime ---
FROM docker.io/python:3.12-slim AS runtime

# Security: Create a non-root user and group
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy the virtual environment from the builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY ig_publisher.py .

# Create a directory for mounting mounted files and set ownership
RUN mkdir /mounted && chown appuser:appuser /mounted

# Drop privileges
USER appuser

# Entrypoint handles the script execution, CMD handles default arguments
ENTRYPOINT ["python", "ig_publisher.py"]
CMD ["--help"]
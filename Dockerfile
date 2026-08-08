# Build a virtualenv using the appropriate Debian release
# * Install gcc and libc6-dev to compile C Python modules
# * In the virtualenv: Update pip setuputils and wheel to support building new packages
FROM python:3.13-slim-trixie AS build

RUN apt-get update && \
    apt-get install --no-install-recommends --yes gcc libc6-dev git && \
    # Symlink the distroless path for python: /usr/bin/python to the build 
    # image path: /usr/local/bin/python to ensure the runtime image's venv has
    # the right python paths.
    ln -s /usr/local/bin/python /usr/bin/python && \
    /usr/bin/python -m venv /venv && \
    /venv/bin/pip install --upgrade pip setuptools wheel

# Build the virtualenv as a separate step: Only re-execute this step when requirements.txt changes
FROM build AS build-venv
COPY requirements.txt /requirements.txt
RUN /venv/bin/pip install --disable-pip-version-check -r /requirements.txt

# Create upload directory with proper permissions
RUN mkdir -p /tmp/ig-uploads && chmod 755 /tmp/ig-uploads


# Runtime stage - use distroless for minimal attack surface
FROM gcr.io/distroless/python3-debian13

# Copy virtual environment from build stage
COPY --from=build-venv /venv /venv

# Copy application files with proper ownership
COPY --chown=nonroot:nonroot --chmod=755 main.py /app/main.py
COPY --chown=nonroot:nonroot --chmod=644 index.html /app/index.html

# Set working directory
WORKDIR /app

# Create non-root user (distroless images have 'nonroot' user)
USER nonroot

# Expose port
EXPOSE 8000

# Health check for container orchestration
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["/venv/bin/python3", "-c", "import httpx; httpx.get('http://localhost:8000/health', timeout=5).raise_for_status()"] || exit 1

# Run the application
ENTRYPOINT ["/venv/bin/python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
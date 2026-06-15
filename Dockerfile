# Build a virtualenv using the appropriate Debian release
# * Install gcc and libc6-dev to compile C Python modules
# * In the virtualenv: Update pip setuputils and wheel to support building new packages
FROM python:3.14-slim-trixie AS build
RUN apt-get update && \
    apt-get install --no-install-suggests --no-install-recommends --yes gcc libc6-dev && \
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
RUN mkdir -p /tmp/ig-uploads


FROM gcr.io/distroless/python3-debian13

COPY --from=build-venv /venv /venv
COPY --from=build-venv --chown=nonroot:nonroot --chmod=755 /tmp/ig-uploads /tmp/ig-uploads
WORKDIR /app
COPY main.py /app
COPY index.html /app
EXPOSE 8000
ENTRYPOINT ["/venv/bin/python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
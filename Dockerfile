FROM docker.io/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY index.html .

# Create temp directory for uploads and set permissions for non-root user
RUN mkdir -p /tmp/ig-uploads && \
    useradd -m appuser && \
    chown -R appuser:appuser /app /tmp/ig-uploads

USER appuser

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
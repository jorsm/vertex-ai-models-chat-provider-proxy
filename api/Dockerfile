# Multi-stage Dockerfile for AI Companion Proxy (Python 3.14)
FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application modules and static assets
COPY src/ .
COPY assets/ ./assets/

# Run as non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Serve the Flask WSGI application with Gunicorn.
CMD ["gunicorn", "--bind=0.0.0.0:8080", "--workers=1", "--worker-class=gthread", "--threads=8", "--timeout=0", "--access-logfile=-", "--error-logfile=-", "main:app"]

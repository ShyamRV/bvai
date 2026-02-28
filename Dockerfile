# BankVoiceAI — Dockerfile
# Python 3.12 slim for smallest image size (~150MB)

FROM python:3.12-slim

# Security: non-root user
RUN groupadd -r bankvoice && useradd -r -g bankvoice bankvoice

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create logs directory
RUN mkdir -p /app/logs && chown -R bankvoice:bankvoice /app

USER bankvoice

EXPOSE 8000

# Uvicorn: 2 workers, optimized for voice webhook latency
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "asyncio", \
     "--log-level", "info", \
     "--access-log"]

# Python 3.11 slim
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System packages
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
 && rm -rf /var/lib/apt/lists/*

# Copy project
COPY . /app

# Install Python packages
RUN pip install --no-cache-dir \
    telethon==1.36.0 \
    PySocks==1.7.1 \
    aiohttp

# Create non-root user and writable data directory
RUN useradd -m appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

# Start bot
CMD ["python", "-u", "bot.py"]
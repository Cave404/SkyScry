FROM python:3.12-slim

LABEL org.opencontainers.image.title="SkyScry" \
      org.opencontainers.image.description="Open-source 433.92 MHz microclimate telemetry and weather ingestion daemon with WebUI" \
      org.opencontainers.image.version="0.0.6" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install rtl-433 and libusb runtime dependencies for RTL-SDR hardware access
RUN apt-get update && apt-get install -y --no-install-recommends \
        rtl-433 \
        libusb-1.0-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for optimal layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application source and configuration templates
COPY config/ /app/config/
COPY src/ /app/src/
COPY run.py /app/run.py

# Ensure persistent volume mount directories exist
RUN mkdir -p /app/data /app/config

EXPOSE 8080

CMD ["python", "run.py"]


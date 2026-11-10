FROM python:3.12-slim

LABEL org.opencontainers.image.title="SkyScry" \
      org.opencontainers.image.description="Open-source 433.92 MHz microclimate telemetry and weather ingestion daemon with WebUI" \
      org.opencontainers.image.version="0.0.25" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTO_UPDATE_ON_START=true

WORKDIR /app

# Install rtl-433 (Debian bookworm build includes -DENABLE_DETACH_KERNEL_DRIVER),
# libusb-1.0-0, usbutils (lsusb), procps (pkill), kmod (modprobe), git, and ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
        rtl-433 \
        rtl-sdr \
        libusb-1.0-0 \
        usbutils \
        procps \
        kmod \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for optimal layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy application source, configuration templates, and smart entrypoint
COPY config/ /app/config/
COPY src/ /app/src/
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY run.py /app/run.py

# Normalize CRLF line endings (if built on Windows) and mark entrypoint executable
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh && \
    chmod +x /app/docker/entrypoint.sh && \
    mkdir -p /app/data /app/config

EXPOSE 8080

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "run.py"]

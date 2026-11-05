#!/usr/bin/env bash
# ============================================================================
# SkyScry v0.0.8 - Smart Linux / TrueNAS SCALE Container Entrypoint
# ============================================================================
set -e

echo "============================================================================"
echo "  SkyScry v0.0.8 - Container Startup & Hardware Pre-Flight"
echo "============================================================================"

cd /app

# 1. Ensure persistent volume directories exist and flush any existing SQLite WAL
mkdir -p /app/data /app/config
if [ -f "/app/data/skyscry.db" ]; then
    python -c "import sqlite3; c=sqlite3.connect('/app/data/skyscry.db', timeout=5.0); c.execute('PRAGMA wal_checkpoint(TRUNCATE);'); c.close()" 2>/dev/null || true
fi

# 2. Optional Git & Python Dependency Self-Update on Container Restart
AUTO_UPDATE="${AUTO_UPDATE_ON_START:-true}"
if [ "${AUTO_UPDATE}" = "true" ] || [ "${AUTO_UPDATE}" = "1" ]; then
    if [ -d "/app/.git" ] && command -v git >/dev/null 2>&1; then
        echo "[ENTRYPOINT] AUTO_UPDATE_ON_START=true: Checking GitHub (origin/main) for updates..."
        git config --global --add safe.directory /app 2>/dev/null || true
        if git pull --rebase --autostash origin main 2>/dev/null; then
            echo "[ENTRYPOINT] Git repository synchronized with origin/main."
        else
            echo "[ENTRYPOINT] Git pull skipped or offline; continuing with existing image build."
        fi
    fi

    if [ -f "/app/requirements.txt" ]; then
        echo "[ENTRYPOINT] Verifying Python dependencies..."
        pip install -r /app/requirements.txt --quiet 2>/dev/null || true
    fi
else
    echo "[ENTRYPOINT] AUTO_UPDATE_ON_START=${AUTO_UPDATE}: Skipping container startup Git pull."
fi

# 3. Unload conflicting Linux DVB-T TV tuner kernel modules if privileged
if command -v modprobe >/dev/null 2>&1; then
    modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2830 2>/dev/null || true
fi

# 4. Inspect /dev/bus/usb passthrough for RTL-SDR hardware
if [ -d "/dev/bus/usb" ]; then
    echo "[ENTRYPOINT] USB bus (/dev/bus/usb) detected inside container."
    if command -v lsusb >/dev/null 2>&1; then
        RTL_DEV="$(lsusb | grep -i -E '0bda:(2838|2832)|Realtek|RTL2838|RTL2832' || true)"
        if [ -n "${RTL_DEV}" ]; then
            echo "[ENTRYPOINT] Found RTL-SDR USB Receiver: ${RTL_DEV}"
        else
            echo "[ENTRYPOINT] Notice: No 0bda:2838/2832 USB device listed by lsusb yet."
        fi
    fi
else
    echo "[ENTRYPOINT] WARNING: /dev/bus/usb is not mounted. Ensure devices: ['/dev/bus/usb:/dev/bus/usb'] or privileged: true is configured."
fi

echo "[ENTRYPOINT] Launching SkyScry daemon..."
exec "$@"

# TrueNAS SCALE & Cloudflare Tunnel Deployment Guide (`v0.0.9`)

This guide walks through deploying **SkyScry (`v0.0.9`)** on **TrueNAS SCALE** (Electric Eel `24.10+` or Dragonfish) as a self-updating Docker Compose application with direct **NooElec / RTL2832U RTL-SDR USB passthrough**, automated **Watchtower** GHCR updates, and zero-port HTTPS access via **Cloudflare Tunnel (`cloudflared`)**.

---

## 1. Plug in the RTL-SDR Dongle & Prevent Linux DVB-T Conflicts

1. Plug your **NooElec / RTL2832U 433.92 MHz USB receiver** directly into a USB port on your TrueNAS SCALE server.
2. Although SkyScry's [`docker/entrypoint.sh`](../docker/entrypoint.sh) automatically runs `modprobe -r dvb_usb_rtl28xxu rtl2832` inside the privileged container on startup, you can also add a 1-line **Post-Init Script** on TrueNAS so the host kernel never claims the dongle as a TV tuner across reboots:
   - In the TrueNAS UI, navigate to **System -> Advanced Settings -> Init/Shutdown Scripts -> Add**.
   - **Description:** `Unload DVB-T kernel driver for RTL-SDR`
   - **Type:** `Command`
   - **Command:** `modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2830 || true`
   - **When:** `Post Init`
   - Click **Save**.

---

## 2. Create the Persistent Dataset on TrueNAS

Create a dataset (or subdirectories inside your existing `appdata` / `apps` dataset) to persist your SQLite WAL database (`skyscry.db`) and configuration across container updates:

1. In TrueNAS SCALE, go to **Datasets**, select your pool (e.g., `/mnt/pool/appdata` or `/mnt/tank/apps`), and click **Add Dataset** named `skyscry`.
2. Open the TrueNAS **System -> Shell** and create the `data` and `config` folders:
   ```bash
   mkdir -p /mnt/pool/appdata/skyscry/data /mnt/pool/appdata/skyscry/config
   ```
   *(Replace `/mnt/pool/appdata` with your actual pool/dataset path, such as `/mnt/tank/apps`.)*

---

## 3. Deploy via TrueNAS SCALE "Install via YAML"

TrueNAS SCALE (`24.10+`) supports native Docker Compose stacks directly in the UI:

1. Navigate to **Apps -> Discover Apps**.
2. Click the **Three Dots (`⋮`)** menu in the top-right corner and select **Install via YAML**.
3. Set **Application Name** to:
   ```text
   skyscry
   ```
4. Copy the contents of [`docker-compose.truenas.yml`](../docker-compose.truenas.yml) (shown below) into the **Custom Config** YAML editor:

```yaml
services:
  skyscry:
    image: ghcr.io/cave404/skyscry:latest
    pull_policy: always
    container_name: skyscry
    restart: unless-stopped
    privileged: true
    devices:
      - "/dev/bus/usb:/dev/bus/usb"
    ports:
      - "8080:8080"
    volumes:
      - "/mnt/pool/appdata/skyscry/data:/app/data"
      - "/mnt/pool/appdata/skyscry/config:/app/config"
    environment:
      - TZ=UTC
      - AUTO_UPDATE_ON_START=true
      - LATITUDE=
      - LONGITUDE=
      - ADMIN_PIN=
      - READ_ONLY_PUBLIC=false
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
    networks:
      - skyscry_net

  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: skyscry-cloudflared
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      - TZ=America/Chicago
      - TUNNEL_TOKEN=PASTE_YOUR_CLOUDFLARE_TUNNEL_TOKEN_HERE
    depends_on:
      - skyscry
    networks:
      - skyscry_net

  watchtower:
    image: containrrr/watchtower:latest
    container_name: skyscry-watchtower
    restart: unless-stopped
    command: --cleanup --label-enable --interval 3600
    environment:
      - TZ=America/Chicago
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    networks:
      - skyscry_net

networks:
  skyscry_net:
    driver: bridge
```

5. **Customize Paths & Coordinates:**
   - Update `/mnt/pool/appdata/skyscry/...` to match your TrueNAS dataset path.
   - Update `LATITUDE` and `LONGITUDE` for your station's regional Open-Meteo barometric pressure (`inHg`/`hPa`) and 48-hour backfill.
   - *(Optional)* Set `ADMIN_PIN=1234` so external visitors via Cloudflare can view all live dashboards and historical charts, while changing sensor aliases or running maintenance actions prompts for your PIN.
6. Click **Save**. TrueNAS will pull `ghcr.io/cave404/skyscry:latest` and start the stack.
7. Verify local LAN access at **`http://<truenas-ip>:8080`**.

---

## 4. Configure Cloudflare Tunnel (`cloudflared`) for Zero-Port HTTPS Access

Because `skyscry` and `cloudflared` share the internal `skyscry_net` Docker bridge network, you do **not** need to forward any ports on your router:

1. Open the **[Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/)** and go to **Networks -> Tunnels**.
2. Click **Create a tunnel** -> select **Cloudflared** -> name it `skyscry-truenas` -> click **Save tunnel**.
3. Copy the **Tunnel Token** string (starts with `eyJ...`) and paste it into `TUNNEL_TOKEN=` in your TrueNAS YAML stack.
4. In the Cloudflare Tunnel **Public Hostname** tab, click **Add a public hostname**:
   - **Subdomain:** `weather` (or `skyscry`)
   - **Domain:** `yourdomain.com`
   - **Service Type:** `HTTP`
   - **URL:** `skyscry:8080`
5. Click **Save hostname**.
6. Your live SkyScry 433.92 MHz weather station dashboard is now accessible worldwide over HTTPS (`https://weather.yourdomain.com`) with automatic SSL certificates and ProxyFix support!

---

## 5. How Automated Updates Work on TrueNAS

SkyScry (`v0.0.8`) provides two layers of zero-touch updates on TrueNAS SCALE:
1. **GitHub Actions + Watchtower:** Every commit pushed to `main` on GitHub triggers [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml) to build and push a fresh `ghcr.io/cave404/skyscry:latest` image. The bundled `watchtower` service checks GHCR hourly (`--interval 3600`) and automatically replaces the container whenever a new image is published.
2. **Container Restart Pull (`AUTO_UPDATE_ON_START=true`):** Simply clicking **Restart** on the `skyscry` app in TrueNAS triggers [`docker/entrypoint.sh`](../docker/entrypoint.sh) to pull the latest `origin/main` commits and verify `requirements.txt` before starting `run.py`.


# Configuration Guide

This project requires several secrets and configuration values that should NOT be committed to git.

## Wiring Diagrams

### RC Car (Raspberry Pi Zero 2W)

```
                              ┌─────────────────────────────────────┐
                              │       Raspberry Pi Zero 2W          │
                              │                                     │
     ┌──────────────┐         │  3.3V ●────────────────┐           │
     │   BNO055     │         │   GND ●──────────────┐ │           │
     │    IMU       │         │  GPIO2 (SDA) ●───┐   │ │           │
     │              │         │  GPIO3 (SCL) ●─┐ │   │ │           │
     │  VIN ●──────────────────────────────────┼─┼───┘ │           │
     │  GND ●──────────────────────────────────┼─┼─────┘           │
     │  SDA ●──────────────────────────────────┘ │                 │
     │  SCL ●────────────────────────────────────┘                 │
     └──────────────┘         │                                     │
                              │                                     │
     ┌──────────────┐         │  GPIO22 ●────────────────────┐     │
     │ Hall Sensor  │         │                              │     │
     │   (A3144)    │         │                              │     │
     │              │         │                              │     │
     │  VCC ●─────────────── 3.3V                            │     │
     │  GND ●─────────────── GND                             │     │
     │  OUT ●────────────────────────────────────────────────┘     │
     │      │                 │                  (10kΩ pull-up     │
     │     ┌┴┐                │                   to 3.3V)         │
     │     │ │ 10kΩ           │                                     │
     │     └┬┘                │                                     │
     │      ●─────────────── 3.3V                                  │
     └──────────────┘         │                                     │
                              │                                     │
     ┌──────────────┐         │  GPIO26 ●───────────────┐          │
     │  Headlight   │         │                         │          │
     │   MOSFET     │         │                         ▼          │
     │  (IRLZ44N)   │         │                    ┌────┴────┐     │
     │              │         │              Gate─►│ IRLZ44N │     │
     │              │         │                    │  MOSFET │     │
     │              │         │       Headlight ◄──┤ Drain   │     │
     │              │         │       12V+         │         │     │
     │              │         │       GND ─────────┤ Source  │     │
     └──────────────┘         │                    └─────────┘     │
                              │                                     │
     ┌──────────────┐         │  /dev/serial0 (UART)               │
     │ GPS Module   │         │  GPIO14 (TX) ●                     │
     │  (VK2828U7)  │         │  GPIO15 (RX) ●───────────┐         │
     │              │         │                          │         │
     │  VCC ●─────────────── 3.3V (or 5V if module needs)│         │
     │  GND ●─────────────── GND                         │         │
     │  TX  ●────────────────────────────────────────────┘         │
     │  RX  ● (not connected, Pi only receives)                    │
     └──────────────┘         │                                     │
                              │                                     │
     ┌──────────────┐         │  CSI Ribbon Cable                  │
     │  Camera      │         │      ┌─────┐                       │
     │  Module 3    ├─────────┼──────┤ CAM │                       │
     │   (Wide)     │         │      └─────┘                       │
     └──────────────┘         │                                     │
                              │                                     │
                              │  5V Power from ARRMA ESC           │
                              │  5V ●────────── ESC 5V out         │
                              │  GND ●───────── ESC GND            │
                              └─────────────────────────────────────┘

    Hall Sensor Placement: Mount magnet on wheel hub, sensor on chassis
    facing the magnet path. Triggers once per wheel rotation.
```

### Transmitter (ESP32-C3 Super Mini + MCP4728 DAC)

```
                              ┌─────────────────────────────────────┐
                              │      ESP32-C3 Super Mini            │
                              │                                     │
     ┌──────────────┐         │  3.3V ●────────────────┐           │
     │   MCP4728    │         │   GND ●──────────────┐ │           │
     │  12-bit DAC  │         │  GPIO8 (SDA) ●───┐   │ │           │
     │              │         │  GPIO9 (SCL) ●─┐ │   │ │           │
     │  VDD ●──────────────────────────────────┼─┼───┘ │           │
     │  GND ●──────────────────────────────────┼─┼─────┘           │
     │  SDA ●──────────────────────────────────┘ │                 │
     │  SCL ●────────────────────────────────────┘                 │
     │              │         │                                     │
     │  VOUTA ●─────┼─────────┼────► Throttle (TX joystick pot)    │
     │  VOUTB ●─────┼─────────┼────► Steering (TX joystick pot)    │
     │  VOUTC ●     │         │     (unused)                       │
     │  VOUTD ●     │         │     (unused)                       │
     │  LDAC  ●─────┼─────────┼──── GND (tie low for immediate)    │
     │  VREF  ●     │         │     (internal reference used)      │
     └──────────────┘         │                                     │
                              │                                     │
                              │  Power: USB or 3.3V                │
                              │  (from TX battery via regulator)   │
                              └─────────────────────────────────────┘

    ┌───────────────────────────────────────────────────────────────┐
    │                    ARRMA Transmitter Mod                      │
    ├───────────────────────────────────────────────────────────────┤
    │                                                               │
    │   Original Joystick Potentiometer:                           │
    │                                                               │
    │      3.3V ─────●───────●                                     │
    │                │       │                                      │
    │               ┌┴┐     ┌┴┐                                    │
    │               │▼│     │▼│  ← Potentiometers                  │
    │               └┬┘     └┬┘                                    │
    │                │       │                                      │
    │      GND ─────●───────●                                      │
    │                │       │                                      │
    │             Throttle Steering                                │
    │              Wiper    Wiper                                  │
    │                │       │                                      │
    │   ─ ─ ─ ─ ─ ─ ┼ ─ ─ ─ ┼ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─    │
    │    DISCONNECT │       │  ORIGINAL WIPERS                     │
    │   ─ ─ ─ ─ ─ ─ ┼ ─ ─ ─ ┼ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─    │
    │                │       │                                      │
    │    Connect    ┌┴┐     ┌┴┐                                    │
    │    DAC ──────►│ │     │ │◄────── DAC                         │
    │    VOUTA      └─┘     └─┘       VOUTB                        │
    │                │       │                                      │
    │             To TX    To TX                                   │
    │             MCU      MCU                                     │
    │           (Throttle) (Steering)                              │
    │                                                               │
    └───────────────────────────────────────────────────────────────┘

    Voltage Ranges (with ESP32 VDD ~3.12V):
    ┌───────────┬──────────────────────────────┬─────────┐
    │ Control   │ Range                        │ Neutral │
    ├───────────┼──────────────────────────────┼─────────┤
    │ Throttle  │ 1.20V (reverse) → 2.82V (fwd)│ 1.69V   │
    │ Steering  │ 0.22V (right) → 3.05V (left) │ 1.66V   │
    └───────────┴──────────────────────────────┴─────────┘
```

### Alternative: ESP32-WROOM Pinout

If using ESP32-WROOM instead of ESP32-C3:

```
    ┌────────────────────────────┐
    │     ESP32-WROOM-32         │
    │                            │
    │  GPIO21 (SDA) ●──── MCP4728 SDA
    │  GPIO22 (SCL) ●──── MCP4728 SCL
    │  3.3V         ●──── MCP4728 VDD
    │  GND          ●──── MCP4728 GND
    │                            │
    └────────────────────────────┘
```

---

## Quick Setup

### 1. ESP32 (Arduino)

Copy the example config and fill in your WiFi credentials:

```bash
cp main/config.h.example main/config.h
```

Edit `main/config.h`:

```cpp
#define WIFI_SSID "your-wifi-ssid"
#define WIFI_PASSWORD "your-wifi-password"
```

### 2. Cloudflare Worker

Set your TURN credentials as secrets:

```bash
cd arrma-relay
wrangler secret put TURN_KEY_ID
# Enter your Cloudflare TURN key ID

wrangler secret put TURN_KEY_API_TOKEN
# Enter your Cloudflare TURN API token
```

Get these from: Cloudflare Dashboard → Calls → TURN Keys → Create/View Key

### 3. Browser (Frontend URLs)

Copy the example config:

```bash
cp arrma-relay/public/config.js.example arrma-relay/public/config.js
```

Edit `arrma-relay/public/config.js`:

```javascript
window.WORKER_URL = "https://your-app.workers.dev";
window.CAMERA_WHEP_URL = "https://cam.yourdomain.com/cam/whep";
window.CONTROL_URL = "https://control.yourdomain.com";

// YouTube Restreamer (optional, for YouTube Live streaming)
window.RESTREAMER_URL = "https://your-restreamer.fly.dev";
window.RESTREAMER_SECRET = "your-restreamer-control-secret";
```

### 4. Raspberry Pi

#### WiFi Setup

The Pi must connect to the **same WiFi network as the ESP32** (typically a mobile hotspot on the car).

**Option A: During OS imaging** - Use Raspberry Pi Imager and configure WiFi in the settings (recommended).

**Option B: NetworkManager (Pi OS Bookworm and newer)**

Modern Raspberry Pi OS uses NetworkManager. Use `nmcli` commands:

```bash
# List available networks
sudo nmcli device wifi list

# Connect to a network
sudo nmcli device wifi connect 'your-wifi-ssid' password 'your-wifi-password'

# List saved connections
nmcli connection show

# Delete a saved connection
sudo nmcli connection delete 'old-network-name'
```

Or use a config file:

```bash
# Copy example to Pi
scp pi-scripts/wifi.nmconnection.example pi@your-pi:/tmp/

# On the Pi - install and set permissions
sudo cp /tmp/wifi.nmconnection.example /etc/NetworkManager/system-connections/YourNetwork.nmconnection
sudo nano /etc/NetworkManager/system-connections/YourNetwork.nmconnection  # Edit SSID and password
sudo chmod 600 /etc/NetworkManager/system-connections/YourNetwork.nmconnection
sudo nmcli connection reload
sudo nmcli connection up YourNetwork
```

**Option C: wpa_supplicant (Pi OS Bullseye and older)**

For older Pi OS versions that use wpa_supplicant directly:

```bash
# Copy from this repo to Pi's boot partition (headless setup)
cp pi-scripts/wpa_supplicant.conf.example /Volumes/boot/wpa_supplicant.conf
nano /Volumes/boot/wpa_supplicant.conf  # Edit with your credentials
```

Or edit directly on the Pi:

```bash
sudo nano /etc/wpa_supplicant/wpa_supplicant.conf
sudo wpa_cli -i wlan0 reconfigure
```

#### Environment Variables

Create environment file on the Pi:

```bash
# On your Pi
cat > ~/.env << 'EOF'
TOKEN_SECRET=your-secret-key-here
TURN_KEY_ID=your-turn-key-id
TURN_KEY_API_TOKEN=your-turn-api-token
EOF
chmod 600 ~/.env
```

The systemd service will automatically load this file.

#### Install Python Dependencies

```bash
sudo apt update && sudo apt install -y python3-pip python3-smbus i2c-tools
pip3 install aiortc aiohttp pyserial pynmea2 smbus2
```

#### Enable I2C and Serial (for IMU and GPS)

```bash
sudo raspi-config
# Interface Options → I2C → Enable
# Interface Options → Serial Port → No login shell, Yes hardware enabled
sudo reboot

# Verify I2C devices (BNO055 should show at 0x28)
i2cdetect -y 1
```

#### MediaMTX Setup

```bash
# Download MediaMTX for ARM64
wget https://github.com/bluenviron/mediamtx/releases/latest/download/mediamtx_vX.X.X_linux_arm64v8.tar.gz
tar -xzf mediamtx_*.tar.gz
sudo mv mediamtx /usr/local/bin/
sudo mv mediamtx.yml /etc/

# Copy and edit config
sudo cp ~/pi-scripts/mediamtx.yml.example /etc/mediamtx.yml
sudo nano /etc/mediamtx.yml

# Create systemd service
sudo tee /etc/systemd/system/mediamtx.service << 'EOF'
[Unit]
Description=MediaMTX
After=network.target

[Service]
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx.yml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
```

#### Control Relay Service Setup

```bash
# Copy scripts to Pi
scp pi-scripts/*.py pi@your-pi:~/
scp pi-scripts/control-relay.service pi@your-pi:~/

# On Pi: Install service
sudo mv ~/control-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now control-relay

# Check status
sudo systemctl status control-relay
journalctl -u control-relay -f
```

### 5. Token Generator

Set the secret when generating tokens:

```bash
TOKEN_SECRET="your-secret-key-here" node generate-token.js 60
```

**Important:** The `TOKEN_SECRET` must be the same in:

- `generate-token.js` (via environment variable)
- Pi's `~/.env` file

## File Locations

| Secret           | Location                       | Git Status     |
| ---------------- | ------------------------------ | -------------- |
| WiFi credentials | `main/config.h`                | ❌ Ignored     |
| TURN credentials | Wrangler secrets               | ✅ Not in repo |
| Frontend URLs    | `arrma-relay/public/config.js` | ❌ Ignored     |
| Token secret     | Pi `~/.env`                    | ✅ Not in repo |

## YouTube Restreamer Setup (Optional)

The restreamer allows streaming the car's video feed to YouTube Live. It runs on Fly.io and auto-scales to zero when not in use.

### Deploy to Fly.io

```bash
cd restreamer

# Create the app (first time only)
fly launch --no-deploy --name your-restreamer-name

# Set secrets
fly secrets set CAM_WHEP_URL="https://cam.yourdomain.com/cam/whep"
fly secrets set YOUTUBE_STREAM_KEY="your-youtube-stream-key"
fly secrets set CONTROL_SECRET="$(openssl rand -hex 16)"

# Deploy
fly deploy

# Get your control secret for config.js
fly ssh console -C "printenv CONTROL_SECRET"
```

### Configuration

| Secret             | Description                                |
| ------------------ | ------------------------------------------ |
| CAM_WHEP_URL       | Your camera's WHEP endpoint                |
| YOUTUBE_STREAM_KEY | From YouTube Studio → Go Live → Stream key |
| CONTROL_SECRET     | Random secret for start/stop auth          |

Add the restreamer URL and secret to `arrma-relay/public/config.js`:

```javascript
window.RESTREAMER_URL = "https://your-restreamer.fly.dev";
window.RESTREAMER_SECRET = "your-control-secret";
```

## Cloudflare Tunnel Setup

You'll need two tunnels on your Pi:

1. **Camera tunnel** - Points to MediaMTX WHEP endpoint
2. **Control tunnel** - Points to the control relay

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/

# Login and create tunnels
cloudflared tunnel login
cloudflared tunnel create camera
cloudflared tunnel create control

# Configure in ~/.cloudflared/config.yml
```

Example `~/.cloudflared/config.yml`:

```yaml
tunnel: <your-tunnel-id>
credentials-file: /home/pi/.cloudflared/<your-tunnel-id>.json
ingress:
  - hostname: cam.yourdomain.com
    service: http://localhost:8889
  - hostname: control.yourdomain.com
    service: http://localhost:8890
  - service: http_status:404
```

Create cloudflared service:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

---

## Hardware Calibration

### DAC Voltage Calibration (ARRMA Transmitter)

The MCP4728 DAC outputs 0-VDD volts. With low TX batteries (~5.5V input), the ESP32 VDD drops to ~3.12V, affecting neutral voltages.

**Calibration procedure:**

1. Power ESP32 from transmitter (not USB)
2. Measure actual VDD with multimeter
3. Adjust neutral voltages in `main/main.ino`:

```cpp
// Calibrate these based on your VDD
static const float THR_V_NEU = 1.69f;  // Throttle neutral
static const float STR_V_CTR = 1.66f;  // Steering center
```

**Expected ranges:**

| Control  | Min (reverse/right) | Neutral | Max (forward/left) |
| -------- | ------------------- | ------- | ------------------ |
| Throttle | 1.20V               | 1.69V   | 2.82V              |
| Steering | 0.22V               | 1.66V   | 3.05V              |

### BNO055 IMU Calibration

The BNO055 auto-calibrates but benefits from an initial calibration routine:

1. **Gyroscope**: Keep sensor still for 3-5 seconds
2. **Accelerometer**: Place sensor in 6 positions (each axis up/down)
3. **Magnetometer**: Move sensor in figure-8 pattern

Calibration status is shown in the debug overlay (C key):

- 0 = uncalibrated, 3 = fully calibrated
- Format: SYS/GYR/ACC/MAG (e.g., "3/3/2/3")

### Hall Sensor Wheel Calibration

Update wheel diameter in `pi-scripts/control-relay.py`:

```python
WHEEL_DIAMETER_MM = 118  # Measure your actual wheel diameter
```

The hall sensor should trigger once per wheel rotation. Mount the magnet on the wheel hub and position the sensor 2-3mm from the magnet path.

---

## Troubleshooting

### ESP32 Issues

| Problem               | Solution                                                           |
| --------------------- | ------------------------------------------------------------------ |
| ESP32 not discovered  | Check WiFi credentials, verify same network as Pi                  |
| DAC not detected      | Check I2C wiring, verify address with `i2cdetect` (should be 0x60) |
| Controls jittery      | Check DAC connections, ensure good ground, reduce I2C speed        |
| Car drifts at neutral | Recalibrate neutral voltages with current battery voltage          |

### Raspberry Pi Issues

| Problem               | Solution                                                             |
| --------------------- | -------------------------------------------------------------------- |
| No video              | Check `systemctl status mediamtx`, verify camera connected           |
| Control relay crashes | Check `journalctl -u control-relay -f`, verify Python dependencies   |
| IMU not detected      | Run `i2cdetect -y 1`, should show 0x28; check wiring                 |
| GPS no fix            | Move to open sky, check serial connection at `/dev/serial0`          |
| High latency          | Check WiFi signal, ensure 5GHz if possible, verify TURN not relaying |

### Connection Issues

| Problem                   | Solution                                                  |
| ------------------------- | --------------------------------------------------------- |
| Token rejected            | Verify TOKEN_SECRET matches between generator and Pi      |
| Video connects but no FPV | Check Cloudflare Tunnel status, verify WHEP endpoint      |
| Controls delayed          | Check if using TURN relay (should be P2P), verify network |
| Auto-reconnect failing    | Check token expiry, Pi control-relay service status       |

### Debug Commands

```bash
# Check services
sudo systemctl status mediamtx control-relay cloudflared

# View logs
journalctl -u control-relay -f
journalctl -u mediamtx -f

# Test ESP32 beacon
nc -ul 4211

# Test camera
libcamera-hello --list-cameras
curl http://localhost:8889/cam/whep

# Test I2C devices
i2cdetect -y 1

# Test GPS
cat /dev/serial0
```

# Configuration Guide

This project requires several secrets and configuration values that should NOT be committed to git.

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
```

### 4. Raspberry Pi

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

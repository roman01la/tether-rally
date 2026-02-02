#!/bin/bash
# Fetch Cloudflare TURN credentials and update MediaMTX config
# Run this via cron every 12 hours
#
# Required environment variables (set in /etc/environment or systemd):
#   TURN_KEY_ID - Cloudflare TURN key ID
#   TURN_KEY_API_TOKEN - Cloudflare TURN API token

# Check required env vars
if [ -z "$TURN_KEY_ID" ] || [ -z "$TURN_KEY_API_TOKEN" ]; then
  echo "Error: TURN_KEY_ID and TURN_KEY_API_TOKEN must be set"
  echo "Set them in /etc/environment or pass them directly"
  exit 1
fi

MEDIAMTX_CONFIG="/home/pi/mediamtx.yml"

# Fetch credentials from Cloudflare
RESPONSE=$(curl -s -X POST \
  "https://rtc.live.cloudflare.com/v1/turn/keys/${TURN_KEY_ID}/credentials/generate-ice-servers" \
  -H "Authorization: Bearer ${TURN_KEY_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"ttl": 86400}')

# Extract username and credential (they're the same for all TURN URLs)
USERNAME=$(echo "$RESPONSE" | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4)
CREDENTIAL=$(echo "$RESPONSE" | grep -o '"credential":"[^"]*"' | head -1 | cut -d'"' -f4)

if [ -z "$USERNAME" ] || [ -z "$CREDENTIAL" ]; then
  echo "Failed to get TURN credentials"
  exit 1
fi

echo "Got TURN credentials, username: ${USERNAME:0:20}..."

# Create MediaMTX config with TURN credentials
cat > "$MEDIAMTX_CONFIG" << EOF
# MediaMTX configuration with Cloudflare TURN

writeQueueSize: 128

# Enable REST API for recording control
api: yes
apiAddress: 127.0.0.1:9997

# Recording defaults (recording is started/stopped via API during races)
recordPath: /home/pi/recordings/%path_%Y-%m-%d_%H-%M-%S
recordFormat: fmp4
recordPartDuration: 2s
recordSegmentDuration: 1h

webrtcICEServers2:
  - url: stun:stun.cloudflare.com:3478
  - url: turn:turn.cloudflare.com:3478?transport=udp
    username: "${USERNAME}"
    password: "${CREDENTIAL}"
  - url: turn:turn.cloudflare.com:3478?transport=tcp
    username: "${USERNAME}"
    password: "${CREDENTIAL}"
  - url: turns:turn.cloudflare.com:443?transport=tcp
    username: "${USERNAME}"
    password: "${CREDENTIAL}"

paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 1280
    rpiCameraHeight: 720
    rpiCameraFPS: 60
    rpiCameraBitrate: 3000000
    rpiCameraCodec: hardwareH264
    rpiCameraIDRPeriod: 15
    rpiCameraHardwareH264Profile: baseline
    rpiCameraDenoise: "off"
    rpiCameraEV: 1
    rpiCameraHDR: false
    rpiCameraAfMode: manual
    rpiCameraLensPosition: 0.1

  # back:
  #   source: rtsp://192.168.0.18/ch0
  #   sourceOnDemand: true
EOF

echo "Updated MediaMTX config"

# Restart MediaMTX if it's running as a service
if systemctl is-active --quiet mediamtx; then
  sudo systemctl restart mediamtx
  echo "Restarted MediaMTX service"
fi

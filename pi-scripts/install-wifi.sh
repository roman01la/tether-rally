#!/bin/bash
# Install WiFi configuration to Raspberry Pi
# Supports both NetworkManager (Pi OS Bookworm+) and wpa_supplicant (older)
#
# Usage: ./install-wifi.sh [pi-hostname]
#   Example: ./install-wifi.sh arrma-pi2w.local

set -e

PI_HOST="${1:-arrma-pi2w.local}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing WiFi config to $PI_HOST..."

# Check which config files exist locally
HAS_NMCONNECTION=false
HAS_WPA=false

if [ -f "$SCRIPT_DIR/wifi.nmconnection" ]; then
    HAS_NMCONNECTION=true
    echo "Found: wifi.nmconnection"
fi

if [ -f "$SCRIPT_DIR/wpa_supplicant.conf" ]; then
    HAS_WPA=true
    echo "Found: wpa_supplicant.conf"
fi

if [ "$HAS_NMCONNECTION" = false ] && [ "$HAS_WPA" = false ]; then
    echo "Error: No WiFi config files found!"
    echo "Create wifi.nmconnection (from wifi.nmconnection.example) or"
    echo "wpa_supplicant.conf (from wpa_supplicant.conf.example)"
    exit 1
fi

# Copy files to Pi
echo ""
echo "Copying files to Pi..."

if [ "$HAS_NMCONNECTION" = true ]; then
    # Extract network name from the config file
    NETWORK_NAME=$(grep "^id=" "$SCRIPT_DIR/wifi.nmconnection" | cut -d= -f2)
    if [ -z "$NETWORK_NAME" ]; then
        NETWORK_NAME="wifi"
    fi
    echo "  -> NetworkManager config for '$NETWORK_NAME'"
    scp "$SCRIPT_DIR/wifi.nmconnection" "pi@$PI_HOST:/tmp/wifi.nmconnection"
fi

if [ "$HAS_WPA" = true ]; then
    echo "  -> wpa_supplicant.conf"
    scp "$SCRIPT_DIR/wpa_supplicant.conf" "pi@$PI_HOST:/tmp/wpa_supplicant.conf"
fi

# Install on Pi with proper permissions
echo ""
echo "Installing on Pi..."

ssh "pi@$PI_HOST" bash -s "$HAS_NMCONNECTION" "$HAS_WPA" "$NETWORK_NAME" << 'ENDSSH'
HAS_NMCONNECTION=$1
HAS_WPA=$2
NETWORK_NAME=$3

if [ "$HAS_NMCONNECTION" = true ]; then
    echo "Installing NetworkManager config..."
    sudo cp /tmp/wifi.nmconnection "/etc/NetworkManager/system-connections/${NETWORK_NAME}.nmconnection"
    sudo chmod 600 "/etc/NetworkManager/system-connections/${NETWORK_NAME}.nmconnection"
    sudo chown root:root "/etc/NetworkManager/system-connections/${NETWORK_NAME}.nmconnection"
    rm /tmp/wifi.nmconnection
    
    # Reload NetworkManager if running
    if systemctl is-active --quiet NetworkManager; then
        sudo nmcli connection reload
        echo "NetworkManager reloaded. Connect with: sudo nmcli connection up '$NETWORK_NAME'"
    fi
fi

if [ "$HAS_WPA" = true ]; then
    echo "Installing wpa_supplicant.conf..."
    sudo cp /tmp/wpa_supplicant.conf /etc/wpa_supplicant/wpa_supplicant.conf
    sudo chmod 644 /etc/wpa_supplicant/wpa_supplicant.conf
    rm /tmp/wpa_supplicant.conf
    
    # Reconfigure if wpa_supplicant is managing wifi (not NetworkManager)
    if ! systemctl is-active --quiet NetworkManager; then
        sudo wpa_cli -i wlan0 reconfigure 2>/dev/null || true
        echo "wpa_supplicant reconfigured"
    fi
fi

echo ""
echo "Current WiFi status:"
if command -v nmcli &> /dev/null; then
    nmcli connection show --active | grep wifi || echo "  (not connected via NetworkManager)"
else
    wpa_cli -i wlan0 status 2>/dev/null | grep ssid || echo "  (not connected)"
fi
ENDSSH

echo ""
echo "Done! WiFi config installed on $PI_HOST"

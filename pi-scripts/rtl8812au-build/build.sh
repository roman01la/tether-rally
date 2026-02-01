#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="${1:-.}"

echo "Building rtl88xxau driver for RPi kernel 6.12.62+rpt-rpi-v8..."

docker build --platform linux/arm64 -t rtl88xxau-builder "$SCRIPT_DIR"

echo "Extracting module to: $OUTPUT_DIR"
docker run --rm --platform linux/arm64 -v "$OUTPUT_DIR:/host" rtl88xxau-builder

echo ""
echo "Done! Copy 88XXau.ko to your Pi and install:"
echo "  scp 88XXau.ko pi@<ip>:~/"
echo "  ssh pi@<ip> 'sudo cp 88XXau.ko /lib/modules/\$(uname -r)/kernel/drivers/net/wireless/ && sudo depmod && sudo modprobe 88XXau'"

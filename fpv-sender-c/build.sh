#!/bin/bash
#
# Build script for fpv-sender-c on Raspberry Pi
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"

# Create build directory
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# Configure
cmake "$SCRIPT_DIR" -DCMAKE_BUILD_TYPE=Release

# Build
make -j$(nproc)

echo ""
echo "Build complete: $BUILD_DIR/fpv-sender"
echo ""
echo "Usage:"
echo "  ./fpv-sender -p <receiver_ip>:5000 -w 1280 -h 720 -f 60 -b 2000"

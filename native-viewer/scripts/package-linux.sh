#!/bin/bash
# Create Linux package for ARRMA Viewer
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"
APP_NAME="arrma-viewer"
VERSION="1.0.0"

# Architecture detection
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
    GO2RTC_ARCH="arm64"
elif [ "$ARCH" = "armv7l" ]; then
    GO2RTC_ARCH="arm"
else
    GO2RTC_ARCH="amd64"
fi

GO2RTC_VERSION="1.9.4"
GO2RTC_URL="https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${GO2RTC_ARCH}"

echo "Building ARRMA Viewer for Linux ($ARCH)..."

# Check dependencies
echo "Checking dependencies..."
MISSING_DEPS=""
for pkg in libglfw3-dev libavformat-dev libavcodec-dev libavutil-dev libswscale-dev; do
    if ! dpkg -s "$pkg" &> /dev/null; then
        MISSING_DEPS="$MISSING_DEPS $pkg"
    fi
done

if [ -n "$MISSING_DEPS" ]; then
    echo "Missing dependencies:$MISSING_DEPS"
    echo "Install with: sudo apt install$MISSING_DEPS"
    exit 1
fi

# Build the app
cd "$PROJECT_DIR"
mkdir -p build
cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j$(nproc)

# Create package directory
PACKAGE_DIR="$BUILD_DIR/${APP_NAME}-${VERSION}-linux-${ARCH}"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

# Copy executable
cp native-viewer "$PACKAGE_DIR/$APP_NAME"

# Download go2rtc if not present
if [ ! -f "$BUILD_DIR/go2rtc" ]; then
    echo "Downloading go2rtc ${GO2RTC_VERSION}..."
    curl -L -o "$BUILD_DIR/go2rtc" "$GO2RTC_URL"
    chmod +x "$BUILD_DIR/go2rtc"
fi

# Copy go2rtc
cp "$BUILD_DIR/go2rtc" "$PACKAGE_DIR/go2rtc"

# Create launcher script
cat > "$PACKAGE_DIR/run.sh" << 'EOF'
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
./arrma-viewer "$@"
EOF
chmod +x "$PACKAGE_DIR/run.sh"

# Create README
cat > "$PACKAGE_DIR/README.txt" << EOF
ARRMA Viewer ${VERSION}
=====================

A low-latency video viewer for ARRMA remote control.

Usage:
  ./run.sh                    Start with GUI prompt for WHEP URL
  ./run.sh --whep <url>       Connect to specific WHEP URL
  ./run.sh --rtsp <url>       Connect directly to RTSP URL
  ./run.sh --help             Show all options

Controls:
  F / F11   Toggle fullscreen
  S         Toggle stats overlay
  Q / Esc   Quit

Requirements:
  - OpenGL 3.3 compatible GPU
  - FFmpeg libraries (libavformat, libavcodec, libavutil, libswscale)
  - GLFW 3.x

Install dependencies on Debian/Ubuntu:
  sudo apt install libglfw3 libavformat59 libavcodec59 libavutil57 libswscale6

Configuration is saved to: ~/.config/arrma-viewer/config.json
EOF

# Create .desktop file for desktop integration
cat > "$PACKAGE_DIR/${APP_NAME}.desktop" << EOF
[Desktop Entry]
Type=Application
Name=ARRMA Viewer
Comment=Low-latency video viewer for ARRMA remote control
Exec=${APP_NAME}
Terminal=false
Categories=Video;AudioVideo;
EOF

# Create tarball
echo "Creating tarball..."
cd "$BUILD_DIR"
TARBALL="${APP_NAME}-${VERSION}-linux-${ARCH}.tar.gz"
tar -czvf "$TARBALL" "$(basename "$PACKAGE_DIR")"

echo ""
echo "=== Build Complete ==="
echo "Package directory: $PACKAGE_DIR"
echo "Tarball: $BUILD_DIR/$TARBALL"
echo ""
echo "To install system-wide:"
echo "  sudo cp $PACKAGE_DIR/$APP_NAME /usr/local/bin/"
echo "  sudo cp $PACKAGE_DIR/go2rtc /usr/local/bin/"
echo ""
echo "Done!"

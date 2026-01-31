#!/bin/bash
# Create macOS .app bundle for ARRMA Viewer
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"
APP_NAME="ARRMA Viewer"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"
VERSION="1.0.0"

# Architecture detection
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    GO2RTC_ARCH="arm64"
else
    GO2RTC_ARCH="amd64"
fi

GO2RTC_VERSION="1.9.4"
GO2RTC_URL="https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_darwin_${GO2RTC_ARCH}"

echo "Building ARRMA Viewer for macOS ($ARCH)..."

# Build the app
cd "$PROJECT_DIR"
mkdir -p build
cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j$(sysctl -n hw.ncpu)

# Create app bundle structure
echo "Creating app bundle..."
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Copy executable
cp native-viewer "$APP_BUNDLE/Contents/MacOS/ARRMA Viewer"

# Download go2rtc if not present
if [ ! -f "$BUILD_DIR/go2rtc" ]; then
    echo "Downloading go2rtc ${GO2RTC_VERSION}..."
    curl -L -o "$BUILD_DIR/go2rtc" "$GO2RTC_URL"
    chmod +x "$BUILD_DIR/go2rtc"
fi

# Copy go2rtc to bundle
cp "$BUILD_DIR/go2rtc" "$APP_BUNDLE/Contents/Resources/go2rtc"

# Create Info.plist
cat > "$APP_BUNDLE/Contents/Info.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>ARRMA Viewer</string>
    <key>CFBundleDisplayName</key>
    <string>ARRMA Viewer</string>
    <key>CFBundleIdentifier</key>
    <string>com.arrma.viewer</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>ARRMA Viewer</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key>
    <true/>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.video</string>
</dict>
</plist>
EOF

# Create a simple icon (placeholder - you can replace with a real .icns file)
# For now we'll skip the icon

echo ""
echo "=== Build Complete ==="
echo "App bundle: $APP_BUNDLE"
echo ""
echo "To install, drag '$APP_NAME.app' to /Applications"
echo ""

# Create DMG (optional)
if command -v create-dmg &> /dev/null; then
    echo "Creating DMG..."
    DMG_NAME="ARRMA-Viewer-${VERSION}-${ARCH}.dmg"
    rm -f "$BUILD_DIR/$DMG_NAME"
    create-dmg \
        --volname "ARRMA Viewer" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "ARRMA Viewer.app" 150 200 \
        --app-drop-link 450 200 \
        "$BUILD_DIR/$DMG_NAME" \
        "$APP_BUNDLE"
    echo "DMG: $BUILD_DIR/$DMG_NAME"
else
    echo "Note: Install 'create-dmg' (brew install create-dmg) to create a DMG installer"
fi

echo ""
echo "Done!"

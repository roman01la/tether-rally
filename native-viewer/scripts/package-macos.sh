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
        --icon "ARRMA Viewer.app" 150 190 \
        --app-drop-link 450 190 \
        "$BUILD_DIR/$DMG_NAME" \
        "$APP_BUNDLE"
    echo "DMG created: $BUILD_DIR/$DMG_NAME"
fi

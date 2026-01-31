# Native WebRTC Viewer

A low-latency native video viewer for Tether Rally using GLFW and libwebrtc.

## Features

- **Zero-copy rendering**: Direct GPU texture upload from decoded frames
- **Hardware decoding**: VideoToolbox on macOS for H.264 hardware acceleration
- **Minimal latency**: No jitter buffer, immediate frame display
- **WHEP signaling**: Connects to MediaMTX WebRTC streams

## Dependencies (macOS)

```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install build tools and dependencies
brew install cmake ninja glfw curl
```

## libwebrtc Setup

This project requires libwebrtc for full WebRTC functionality. Without it, the viewer builds in **stub mode** which demonstrates the architecture but cannot receive real video streams.

### Option 1: Use prebuilt binaries

Prebuilt libwebrtc binaries for macOS can be difficult to find. Some options:
- Search GitHub for "libwebrtc prebuilt macos"
- Check the [aspect-build](https://github.com/aspect-build) organization for build tooling
- Build from source (recommended for reliability)

Extract and set `LIBWEBRTC_DIR` when building.

### Option 2: Build from source

```bash
# Install depot_tools
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
export PATH="$PWD/depot_tools:$PATH"

# Fetch WebRTC
mkdir webrtc && cd webrtc
fetch --nohooks webrtc
gclient sync

# Build for macOS
cd src
gn gen out/Release --args='is_debug=false rtc_include_tests=false target_os="mac" target_cpu="arm64"'
ninja -C out/Release
```

## Building

```bash
cd native-viewer
mkdir build && cd build

# Without libwebrtc (stub mode - for development)
cmake .. -G Ninja
ninja

# With libwebrtc (full functionality)
cmake .. -G Ninja -DLIBWEBRTC_DIR=/path/to/libwebrtc
ninja
```

## Usage

```bash
# Connect to local MediaMTX
./native-viewer --url "http://localhost:8889/cam/whep"

# Connect to remote server with TURN
./native-viewer --url "https://cam.example.com/cam/whep" \
                --turn-url "turn:turn.example.com:3478" \
                --turn-user "user" \
                --turn-pass "password"
```

## Command Line Options

| Option | Description |
|--------|-------------|
| `--url` | WHEP endpoint URL (required) |
| `--turn-url` | TURN server URL |
| `--turn-user` | TURN username |
| `--turn-pass` | TURN password |
| `--width` | Window width (default: 1280) |
| `--height` | Window height (default: 720) |
| `--fullscreen` | Start in fullscreen mode |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Native Viewer                            │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │    WHEP     │    │  libwebrtc  │    │ VideoToolbox│     │
│  │  Signaling  │───►│   WebRTC    │───►│   Decoder   │     │
│  │   (cURL)    │    │   Stack     │    │   (macOS)   │     │
│  └─────────────┘    └─────────────┘    └──────┬──────┘     │
│                                               │             │
│                                               ▼             │
│                          ┌─────────────────────────┐        │
│                          │    GLFW + OpenGL        │        │
│                          │    (YUV→RGB shader)     │        │
│                          └─────────────────────────┘        │
└─────────────────────────────────────────────────────────────┘
```

## Low Latency Design

1. **No jitter buffer**: Frames are displayed immediately upon decode
2. **Hardware decoding**: VideoToolbox provides GPU-accelerated H.264 decoding
3. **Zero-copy path**: Decoded NV12 frames go directly to GPU texture
4. **Minimal buffering**: Single frame buffer for display
5. **VSync disabled**: Optional tearing for lowest possible latency
6. **WebRTC optimizations**:
   - Receiver-side bandwidth estimation hints
   - Minimal transport latency

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `F` | Toggle fullscreen |
| `Esc` | Exit |
| `S` | Show stats overlay |

## Stub Mode

When built without libwebrtc (`BUILD_STUB` defined), the viewer:
- Performs WHEP signaling (sends offer, receives answer)
- Simulates video frames with a test pattern
- Demonstrates the rendering pipeline

This is useful for development and testing the UI/rendering without a full WebRTC stack.

## License

MIT License - See LICENSE in root directory

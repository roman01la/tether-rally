# ARRMA Remote Viewer

A low-latency native video viewer for ARRMA RC car remote control using FFmpeg and go2rtc.

## Features

- **WHEP to RTSP proxy**: Uses go2rtc to convert WHEP streams to local RTSP
- **Low-latency decoding**: FFmpeg with optimized settings for minimal delay
- **Simple UI**: GLFW window with stats overlay
- **Auto-configuration**: Saves WHEP URL, prompts on first run
- **Bundled go2rtc**: macOS app includes go2rtc binary

## Dependencies (macOS)

```bash
brew install cmake glfw ffmpeg
```

## Building

```bash
cd native-viewer
mkdir build && cd build
cmake ..
make
```

## Running

```bash
./native-viewer
```

On first run, you'll be prompted to enter your WHEP URL. The URL is saved for future runs.

### Command Line Options

```bash
./native-viewer --whep <url>     # Override saved WHEP URL
./native-viewer --rtsp <url>     # Direct RTSP (bypass go2rtc)
./native-viewer --fullscreen     # Start fullscreen
./native-viewer --reset          # Clear saved configuration
```

## Packaging macOS App

```bash
./scripts/package-macos.sh
```

Creates `build/ARRMA Viewer.app` with bundled go2rtc binary.

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                      ARRMA Viewer                              │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│   WHEP URL ──► go2rtc ──► RTSP ──► FFmpeg ──► OpenGL          │
│               (proxy)    localhost   (decode)   (render)       │
│                                                                │
│   Remote         Local           H.264         RGB             │
│   WebRTC         RTSP            frames        texture         │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### Components

| Component  | Purpose                                   |
| ---------- | ----------------------------------------- |
| **go2rtc** | Converts WHEP WebRTC stream to local RTSP |
| **FFmpeg** | RTSP client + H.264 software decoder      |
| **GLFW**   | Cross-platform windowing                  |
| **OpenGL** | Video rendering with YUV→RGB conversion   |

## Keyboard Shortcuts

| Key            | Action               |
| -------------- | -------------------- |
| `F` / `F11`    | Toggle fullscreen    |
| `S`            | Toggle stats overlay |
| `Q` / `Escape` | Quit                 |

## Configuration

Settings are saved to:

- **macOS**: `~/Library/Application Support/ARRMA Viewer/config.json`
- **Linux**: `~/.config/arrma-viewer/config.json`

## License

MIT License - See LICENSE in root directory

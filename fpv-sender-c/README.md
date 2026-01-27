# FPV Sender (C)

Low-latency H.264 video streaming from Raspberry Pi camera using direct V4L2 access.

## Why C Instead of Go?

The original Go-based fpv-sender used `rpicam-vid` via stdout pipe. This architecture
has a fundamental bottleneck: the pipe cannot sustain 60fps due to:

1. **Context switch overhead** - Every read from the pipe involves a context switch
2. **Buffering latency** - Pipe buffering adds 10-30ms of latency
3. **Memory copies** - Data is copied multiple times (rpicam → kernel → Go)
4. **GC pauses** - Go's garbage collector can cause frame drops

The C implementation eliminates these issues by:

1. **Direct V4L2 access** - No subprocess, no pipes
2. **mmap'd buffers** - Zero-copy frame handling
3. **Hardware encoder** - Uses bcm2835-codec directly via V4L2 M2M
4. **Deterministic latency** - No garbage collector

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                        fpv-sender-c                            │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐ │
│  │  Camera  │───▶│ Encoder  │───▶│  Sender  │───▶│   UDP    │ │
│  │ (V4L2)   │    │ (V4L2 M2M)│    │(fragment)│    │ (socket) │ │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘ │
│                                                                 │
│  /dev/video0     /dev/video11                                  │
│  (camera)        (bcm2835-codec)                               │
└────────────────────────────────────────────────────────────────┘
```

## Building on Raspberry Pi

```bash
cd fpv-sender-c
./build.sh
```

Requirements:

- CMake 3.16+
- GCC with C11 support
- Linux kernel with V4L2 support

## Usage

```bash
# Basic usage - send to receiver at 192.168.1.100:5000
./build/fpv-sender -p 192.168.1.100:5000

# Full options
./build/fpv-sender \
    -w 1280 -h 720 \     # Resolution (default: 1280x720)
    -f 60 \               # FPS (default: 60)
    -b 2000 \             # Bitrate in kbps (default: 2000)
    -i 30 \               # IDR interval in frames (default: 30)
    -p host:port \        # Peer address (required)
    -l 5001 \             # Local port (default: 5001)
    -s stun.example.com \ # STUN server for NAT traversal (optional)
    -v                    # Verbose output
```

## Protocol Compatibility

This sender uses the same protocol as the Go version (see FPV_PLAN.md):

- **VIDEO_FRAGMENT (0x01)** - H.264 data fragments, max 1200 bytes payload
- **KEEPALIVE (0x02)** - Connection heartbeat, every 1 second
- **IDR_REQUEST (0x03)** - Request keyframe (from receiver)
- **PROBE (0x04)** - RTT measurement

## V4L2 Device Paths

On Raspberry Pi:

- `/dev/video0` - Camera device
- `/dev/video11` - H.264 encoder (bcm2835-codec)
- `/dev/video31` - Alternate encoder path

## Latency Comparison

| Component      | Go (pipe) | C (V4L2)  |
| -------------- | --------- | --------- |
| Camera capture | ~5ms      | ~1ms      |
| Pipe transfer  | ~15ms     | N/A       |
| H.264 encode   | ~8ms      | ~8ms      |
| UDP send       | ~1ms      | ~1ms      |
| **Total**      | **~29ms** | **~10ms** |

The C implementation reduces sender-side latency by approximately 19ms.

# RC Car Racing Tournament Platform

## Project Overview

A web-based platform where users can remotely control a real RC car over the internet, compete in timed races, and participate in tournaments. The system streams live FPV video from the car while allowing real-time control through a browser interface.

## Core Features

1. **Real-time RC Car Control** ✅ (Implemented)

   - WebRTC DataChannel binary protocol (PING/CTRL/PONG/RACE/STATUS/CONFIG/KICK/TELEM commands)
   - Direct P2P connection via Cloudflare TURN (10-15ms RTT)
   - Pi relay: DataChannel → UDP → ESP32
   - ESP32: Dual-core FreeRTOS (UDP on Core 0, Control on Core 1)
   - 200 Hz output loop with EMA smoothing + slew rate limiting
   - **External 12-bit DAC (MCP4728)** for clean analog output (I2C)
   - Hot-plug DAC support (ESP32 connects to WiFi first, retries DAC)
   - Touch controls (dual-zone: throttle left, steering right)
   - Keyboard controls (WASD / Arrow keys) with smooth interpolation
   - Configurable throttle limits (10-50% via admin, ESP32 hard limit 50%)
   - Safety limits enforced on ESP32 (not browser)
   - Latency measurement with EMA smoothing
   - **Auto-reconnect on connection loss** with exponential backoff
   - **Race state sync on reconnect** (RACE_RESUME command)
   - **FPV auto-reconnect** when video stream drops
   - **GPS telemetry** (position, speed, heading) broadcast at 10Hz

2. **Security & Access Control** ✅ (Implemented)

   - HMAC-SHA256 signed time-limited tokens
   - Token generator script (`generate-token.js`) + admin web UI generator
   - Newer tokens automatically invalidate older ones
   - Pi relay validates tokens (not Cloudflare Worker)
   - LocalStorage persistence for session recovery
   - No shared passwords or third-party auth needed
   - **Admin kick with token revocation** (persistent, last 10 revoked)
   - **Basic auth protected admin page** (/admin.html)

3. **Racing Game UI** ✅ (Implemented)

   - Full-screen FPV video background
   - Glassmorphism control zones at bottom corners
   - HUD overlay with status, values, latency
   - Video stats overlay (resolution, fps, bitrate, RTT)
   - Controls disabled until video connects
   - Loading spinner while video connecting
   - Active touch feedback
   - Mobile-optimized layout
   - **Track map overlay** with live car position (GPS-based)
   - **Speed display** in km/h from GPS

4. **FPV Video Streaming** ✅ (Implemented)

   - Raspberry Pi Zero 2W + Camera Module 3
   - MediaMTX for WebRTC streaming
   - Cloudflare Tunnel for internet access
   - Cloudflare TURN for NAT traversal
   - 720p @ 2Mbps @ 30fps H.264
   - ~100-300ms end-to-end latency

5. **Admin Dashboard** ✅ (Implemented)

   - Race state management (idle → countdown → racing)
   - Start race with 3-second countdown (3-2-1-GO!)
   - Stop race command
   - Live status: ESP32 connection, player connected, video feed, player ready
   - Throttle limit control (10-50% slider)
   - Kick player with token revocation
   - Token generator with configurable duration (15min - 24h)
   - Real-time race timer (mm:ss.ms format)
   - Player must click "Ready" after video connects before race can start
   - Video status reported from browser to Pi
   - **YouTube Live streaming** (start/stop from admin dashboard)

6. **YouTube Restreamer** ✅ (Implemented)

   - Fly.io hosted service (auto-scales to zero when idle)
   - Consumes WHEP video stream from car
   - Re-encodes to RTMP for YouTube Live
   - **Live telemetry overlay** (race time, throttle, steering via FFmpeg drawtext)
   - Telemetry received via separate WebRTC DataChannel from Pi
   - Admin dashboard controls (Go Live / Stop buttons)
   - Live indicator with status polling
   - Bearer token authentication for control endpoints
   - Process exits on stop (Fly.io machine auto-stops)

7. **Local Recording on Pi** 🔲 (Future)

   - High-quality 720p50 @ 8Mbps recording
   - Telemetry logging to JSONL for offline rendering
   - Post-processing scripts for telemetry overlay burn-in

8. **Tournament System** 🔲 (Planned)

   - User registration and queue management
   - Timed race sessions
   - Leaderboard and rankings
   - Support for 10+ participants per tournament

7. **Track Timing System** 🔲 (Planned)
   - Start/finish line detection
   - Lap timing with millisecond precision
   - Automatic race state management

## Current Implementation Details

### Files Structure

```
arrma-remote/
├── index.html              # Web UI (backup copy)
├── generate-token.js       # Token generator (Node.js/Bun)
├── PLAN.md                 # This document
├── SETUP.md                # Configuration/deployment guide
├── .gitignore              # Excludes secret files
├── main/
│   ├── main.ino            # ESP32 firmware (UDP receiver, FreeRTOS)
│   ├── config.h            # WiFi credentials (gitignored)
│   └── config.h.example    # Template for config.h
├── arrma-relay/
│   ├── src/index.ts        # Cloudflare Workers (static + TURN + admin auth + token gen)
│   ├── public/
│   │   ├── index.html      # Player UI (served by Workers)
│   │   ├── admin.html      # Admin dashboard (basic auth protected)
│   │   ├── config.js       # URL configuration (gitignored)
│   │   └── config.js.example # Template for config.js
│   ├── wrangler.jsonc      # Cloudflare Workers config
│   └── package.json
├── pi-scripts/
│   ├── control-relay.py    # WebRTC DataChannel → UDP relay + race management
│   ├── control-relay.service # systemd service for relay
│   ├── deploy.sh           # Quick deploy script to Pi
│   ├── .env                 # Pi secrets (gitignored, on Pi only)
│   ├── .env.example         # Template for Pi .env
│   └── update-turn-credentials.sh  # TURN credential refresh script
└── restreamer/
    ├── main.go             # Go HTTP server for YouTube restreaming
    ├── Dockerfile          # Multi-stage build (Go + MediaMTX + FFmpeg)
    ├── fly.toml            # Fly.io deployment config
    ├── go.mod              # Go module
    └── README.md           # Restreamer documentation
```

### Secrets Management

All secrets are externalized for open-source compatibility:

| Secret           | Location           | Notes                             |
| ---------------- | ------------------ | --------------------------------- |
| WiFi credentials | `main/config.h`    | Gitignored, copy from `.example`  |
| TURN credentials | Cloudflare secrets | `wrangler secret put TURN_KEY_ID` |
| Frontend URLs    | `public/config.js` | Gitignored, copy from `.example`  |
| Token secret     | Pi `~/.env`        | `TOKEN_SECRET=...` (no `export`)  |

### Binary Protocol

| Command | Byte | Payload                            | Description                                        |
| ------- | ---- | ---------------------------------- | -------------------------------------------------- |
| PING    | 0x00 | seq(2) + timestamp(4)              | Latency measurement                                |
| CTRL    | 0x01 | seq(2) + throttle(2) + steering(2) | Control values (-32767 to 32767)                   |
| PONG    | 0x02 | timestamp(4)                       | Response to PING (from ESP32)                      |
| RACE    | 0x03 | sub-cmd(1)                         | Race commands (START=0x01, STOP=0x02, RESUME=0x03) |
| STATUS  | 0x04 | sub-cmd(1) + value(1)              | Browser→Pi: VIDEO=0x01, READY=0x02                 |
| CONFIG  | 0x05 | type(1) + value(4)                 | Pi→Browser: throttle limit            |
| KICK    | 0x06 | -                                  | Pi→Browser: you have been kicked      |
| TELEM   | 0x07 | race_time(4) + throttle(2) + steering(2) + lat(4) + lon(4) + speed(2) + heading(2) + fix(1) | Pi→Clients: telemetry + GPS (10Hz, 24 bytes) |

Packet format: `seq(uint16 LE) + cmd(uint8) + payload`

### Token Format

24 hex characters: `TTTTTTTTSSSSSSSSSSSSSSSS`

- First 8 chars: Unix timestamp (expiry time)
- Last 16 chars: HMAC-SHA256 signature (first 8 bytes)

**Generation:** `TOKEN_SECRET="your-secret" node generate-token.js [minutes]` (default: 60 min)

**Validation:** Done by Pi relay (not Cloudflare Worker)

1. Token not expired
2. Signature matches HMAC of expiry timestamp
3. Token expiry >= active token expiry (newer wins)

### Voltage Mapping (ARRMA)

| Control  | Voltage Range                | Neutral |
| -------- | ---------------------------- | ------- |
| Throttle | 1.20V (back) → 2.82V (fwd)   | 1.69V   |
| Steering | 0.22V (right) → 3.05V (left) | 1.66V   |

ESP32 DAC: Pin 25 (throttle), Pin 26 (steering)
Note: Neutral voltages calibrated for ESP32 VDD ~3.12V (low TX batteries)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              USER (Browser)                                 │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                         Tether Rally Web UI                           │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │  │
│  │  │   Controls  │  │  FPV Video  │  │     HUD     │  │ Video Stats │  │  │
│  │  │ Touch/Keys  │  │   WebRTC    │  │   Latency   │  │ res/fps/bps │  │  │
│  │  └──────┬──────┘  └──────┬──────┘  └─────────────┘  └─────────────┘  │  │
│  └─────────┼────────────────┼────────────────────────────────────────────┘  │
└────────────┼────────────────┼───────────────────────────────────────────────┘
             │                │
             │ DataChannel    │ WebRTC (WHEP)
             │ (50Hz CTRL)    │ (720p H.264)
             ▼                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                           CLOUDFLARE EDGE                                  │
│  ┌──────────────────────────┐     ┌──────────────────────────────────────┐ │
│  │    Workers (Static +     │     │         Cloudflare Tunnel            │ │
│  │    TURN Credentials)     │     │    control.yourdomain.com             │ │
│  │  ┌────────────────────┐  │     │  ┌────────────────────────────────┐  │ │
│  │  │  /turn-credentials │  │     │  │   HTTP → Pi:8890 (relay)       │  │ │
│  │  │  Returns TURN auth │  │     │  │   + Pi:8889 (WHEP)             │  │ │
│  │  └────────────────────┘  │     │  └────────────────────────────────┘  │ │
│  └──────────────────────────┘     └──────────────────────────────────────┘ │
│               │                   ┌──────────────────────────────────────┐ │
│               │                   │         Cloudflare TURN              │ │
│               │                   │     turn.cloudflare.com              │ │
│               │                   │  ┌────────────────────────────────┐  │ │
│               │                   │  │   NAT Traversal for WebRTC    │  │ │
│               │                   │  │   P2P DataChannel + Video     │  │ │
│               │                   │  └────────────────────────────────┘  │ │
│               │                   └──────────────────────────────────────┘ │
└───────────────┼────────────────────────────────────────────────────────────┘
                │
           Internet (4G/5G via iPhone Hotspot)
                │
┌───────────────┼────────────────────────────────────────────────────────────┐
│               │              RC CAR (On-board)                             │
│  ┌────────────┼─────────────┐      ┌─────────────────────────────────────┐ │
│  │   ESP32    │             │      │     Raspberry Pi Zero 2W            │ │
│  │            │  UDP 4210   │◄─────│  ┌───────────────────────────────┐  │ │
│  │  ┌─────────▼─────────┐   │      │  │   control-relay.py            │  │ │
│  │  │  UDP Receive Task │   │      │  │   - WebRTC DataChannel server │  │ │
│  │  │    (Core 0)       │   │      │  │   - Token validation          │  │ │
│  │  │  → target_thr/str │   │      │  │   - UDP forward to ESP32      │  │ │
│  │  └─────────┬─────────┘   │      │  │   - PING/PONG via Pi          │  │ │
│  │            │ shared      │      │  └───────────────────────────────┘  │ │
│  │  ┌─────────▼─────────┐   │      │  ┌───────────────────────────────┐  │ │
│  │  │ Control Loop      │   │      │  │   Camera Module 3 (Wide)      │  │ │
│  │  │    (Core 1)       │   │      │  │   720p @ 30fps                │  │ │
│  │  │  - 200 Hz output  │   │      │  └───────────────┬───────────────┘  │ │
│  │  │  - EMA smoothing  │   │      │                  │                  │ │
│  │  │  - Slew limiting  │   │      │  ┌───────────────▼───────────────┐  │ │
│  │  │  - Staged timeout │   │      │  │        MediaMTX               │  │ │
│  │  └─────────┬─────────┘   │      │  │   - H.264 HW encode           │  │ │
│  │            │             │      │  │   - WebRTC server             │  │ │
│  │  ┌─────────▼─────────┐   │      │  │   - WHEP endpoint             │  │ │
│  │  │    DAC Output     │   │      │  └───────────────────────────────┘  │ │
│  │  │  Pin 25: Throttle │   │      │  ┌───────────────────────────────┐  │ │
│  │  │  Pin 26: Steering │   │      │  │      cloudflared              │  │ │
│  │  └─────────┬─────────┘   │      │  │   (Tunnel to Cloudflare)      │  │ │
│  └────────────┼─────────────┘      │  └───────────────────────────────┘  │ │
│               │                    └─────────────────────────────────────┘ │
│               ▼                                                            │
│  ┌───────────────────────────────────────────────────────────────────────┐ │
│  │                      ARRMA RC Car Receiver                            │ │
│  │              Throttle Input (1.20V-2.82V, neutral 1.69V)              │ │
│  │              Steering Input (0.22V-3.05V, neutral 1.66V)              │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────┘
```

### Data Flow Summary

| Path                         | Protocol                    | Rate       | Latency    |
| ---------------------------- | --------------------------- | ---------- | ---------- |
| Controls: Browser → Pi → ESP | DataChannel → UDP (7 bytes) | 50 Hz      | ~10-15ms   |
| Latency Ping: Browser ↔ ESP  | DataChannel ↔ UDP           | 2 Hz       | measured   |
| Video: Pi → Browser          | WebRTC H.264                | 30 fps     | ~100-300ms |
| WHEP Signaling: Browser ↔ Pi | HTTPS (via Tunnel)          | On-connect | -          |
| TURN Relay: Pi ↔ Browser     | UDP (via Cloudflare)        | Continuous | ~5-10ms    |

---

## Implementation Phases

### Phase 1: Video Streaming ✅ (Implemented)

#### Hardware

- Raspberry Pi Zero 2W + Camera Module 3 (Wide)
- Total cost: ~$50

#### Streaming Stack

1. **Camera → Raspberry Pi** - Capture via `libcamera` (rpiCamera source)
2. **Encode** - Hardware H.264 via Pi GPU
3. **Stream** - MediaMTX serves WebRTC directly
4. **NAT Traversal** - Cloudflare TURN (1TB free/month)
5. **Internet Access** - Cloudflare Tunnel (tunnels WHEP endpoint)
6. **Browser** - Native WebRTC via WHEP protocol

#### Configuration

**MediaMTX (`~/mediamtx.yml` on Pi):**

```yaml
paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 1280
    rpiCameraHeight: 720
    rpiCameraFPS: 30
    rpiCameraBitrate: 2000000
```

**Cloudflare Tunnel (`~/.cloudflared/config.yml` on Pi):**

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /home/pi/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: cam.yourdomain.com
    service: http://localhost:8889
  - service: http_status:404
```

#### Services (auto-start on boot)

- `mediamtx.service` - Camera streaming
- `cloudflared.service` - Tunnel to internet

#### Tasks

- [x] Set up Raspberry Pi with camera
- [x] Configure low-latency streaming pipeline (MediaMTX)
- [x] Set up Cloudflare Tunnel for WHEP signaling
- [x] Set up Cloudflare TURN for WebRTC NAT traversal
- [x] Integrate video player into web UI
- [x] Add video stats overlay (resolution, fps, bitrate, latency)
- [x] Block controls until video connected
- [x] Add loading spinner while connecting
- [x] Auto-start services on Pi boot

---

### Phase 2: Cloud Infrastructure ✅ (Implemented)

#### WebRTC DataChannel Control Relay

The browser establishes a WebRTC DataChannel directly to the Pi (via Cloudflare TURN).
The Pi relays control commands to the ESP32 via UDP on the local network.

**Architecture:**

```
Browser <--DataChannel (P2P via TURN)--> Pi <--UDP--> ESP32
                      │
              Token validation on Pi
```

**Files:**

```
arrma-relay/
├── src/index.ts       # Cloudflare Workers (static files + TURN credentials)
├── public/index.html  # Web UI
├── wrangler.jsonc     # Cloudflare Workers config
└── package.json

pi-scripts/
├── control-relay.py   # WebRTC DataChannel → UDP relay (aiortc)
└── control-relay.service # systemd service
```

**Endpoints:**

- Cloudflare Worker: `/turn-credentials` - Returns Cloudflare TURN auth
- Pi (via Tunnel): `POST /control/offer?token=...` - WebRTC signaling
- Pi (via Tunnel): `GET /control/health` - ESP32 discovery status

**Key features:**

- Direct P2P connection (10-15ms RTT vs 100-200ms with WS relay)
- Token validation with HMAC-SHA256 on Pi
- DataChannel: `ordered: false, maxRetransmits: 0` (UDP-like)
- ESP32 beacon discovery (broadcasts "ARRMA" on UDP 4211)
- No Durable Objects needed (stateless Worker)

**ESP32 changes (main.ino):**

- Changed from WebSocket client to UDP server (port 4210)
- FreeRTOS dual-core: UDP receive (Core 0), Control loop (Core 1)
- 200 Hz output with EMA smoothing + slew rate limiting
- Staged timeout: 80ms hold → 250ms neutral
- Beacon broadcast for Pi discovery

**Pi relay (control-relay.py):**

- aiortc for WebRTC DataChannel
- Forwards binary packets to ESP32 via UDP
- Forwards PONG from ESP32 back to browser (latency = browser↔ESP32)
- Runs as systemd service with FIFO scheduling

**Deployment:**

```bash
# 1. Deploy Cloudflare Worker
cd arrma-relay
npm run deploy

# 2. Set Worker secrets (TURN credentials from Cloudflare dashboard)
npx wrangler secret put TURN_KEY_ID
npx wrangler secret put TURN_KEY_API_TOKEN

# 3. Deploy to Pi
scp pi-scripts/control-relay.py pi@<your-pi-hostname>:~/
scp pi-scripts/control-relay.service pi@<your-pi-hostname>:~/

# 4. On Pi: Create .env with TOKEN_SECRET (must match generate-token.js)
echo "TOKEN_SECRET=your-secret-key" > ~/.env
chmod 600 ~/.env

# 5. Install service
sudo mv ~/control-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now control-relay

# 6. Generate token for access
TOKEN_SECRET="your-secret-key" node generate-token.js 60
```

#### Components

1. **WebRTC DataChannel Relay** ✅ (Pi + Cloudflare TURN)

   - P2P connection via TURN for NAT traversal
   - Authenticates users via HMAC token
   - Routes controls to ESP32 via UDP
   - ~10-15ms control latency

2. **Tournament Service** 🔲 (Planned)

   - Manages race queue
   - Tracks timing
   - Updates leaderboard

3. **Database** (PostgreSQL)

   - Users, tournaments, race results
   - Leaderboard history

4. **Web App** (React/Next.js)
   - Control interface
   - Tournament registration
   - Live leaderboard

#### Tasks

- [x] Set up Cloudflare Workers for static files + TURN
- [x] Implement WebRTC DataChannel relay on Pi
- [x] ESP32 UDP server with beacon discovery
- [x] Token-based authentication on Pi
- [ ] Set up PostgreSQL database (for tournament)
- [ ] Create database schema (for tournament)

---

### Phase 3: Tournament System (Week 3-4)

#### Race Flow

```
1. Tournament Created (Admin)
         ↓
2. Registration Opens (10 slots)
         ↓
3. Tournament Starts
         ↓
4. For each participant:
   a. "Get Ready" countdown (10s)
   b. Control enabled
   c. Start line detection → Timer starts
   d. Finish line detection → Timer stops
   e. Control disabled
   f. Car returns to start (manual/auto)
         ↓
5. All participants done
         ↓
6. Final leaderboard displayed
         ↓
7. Winner announced
```

#### Timing System Options

**Option A: Computer Vision (Software)**

- Camera detects car crossing line
- Pros: No extra hardware
- Cons: Less precise, depends on video latency

**Option B: IR Sensors (Hardware)**

- IR beam break at start/finish
- Pros: Precise (millisecond), reliable
- Cons: Extra hardware, wiring

**Option C: RFID/NFC**

- Tag on car, readers at lines
- Pros: Very reliable
- Cons: More expensive, complex

**Recommended: IR Sensors**

- Arduino/ESP32 with IR break-beam sensors
- Sends timing events to cloud server
- ~1ms precision

#### Database Schema

```sql
-- Users
CREATE TABLE users (
  id UUID PRIMARY KEY,
  username VARCHAR(50) UNIQUE,
  email VARCHAR(255) UNIQUE,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Tournaments
CREATE TABLE tournaments (
  id UUID PRIMARY KEY,
  name VARCHAR(100),
  status VARCHAR(20), -- 'registration', 'active', 'completed'
  max_participants INT DEFAULT 10,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Registrations
CREATE TABLE registrations (
  id UUID PRIMARY KEY,
  tournament_id UUID REFERENCES tournaments(id),
  user_id UUID REFERENCES users(id),
  position INT, -- queue position
  registered_at TIMESTAMP DEFAULT NOW()
);

-- Race Results
CREATE TABLE race_results (
  id UUID PRIMARY KEY,
  tournament_id UUID REFERENCES tournaments(id),
  user_id UUID REFERENCES users(id),
  time_ms INT, -- race time in milliseconds
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  status VARCHAR(20) -- 'completed', 'dnf', 'disqualified'
);
```

#### Tasks

- [ ] Design tournament state machine
- [ ] Implement registration API
- [ ] Build race queue management
- [ ] Set up timing hardware (IR sensors)
- [ ] Implement timing event handling
- [ ] Create leaderboard API

---

### Phase 4: Web Application (Week 4-5)

#### Pages

1. **Home** - Active tournaments, join/spectate
2. **Tournament Lobby** - Participants, queue, countdown
3. **Race View** - Video + controls (for active racer)
4. **Spectator View** - Video + leaderboard (for others)
5. **Results** - Final standings, replays

#### UI Components

- [ ] Video player (WebRTC/HLS)
- [ ] Control interface (existing, adapt)
- [ ] Tournament registration form
- [ ] Race queue display
- [ ] Live leaderboard
- [ ] Countdown timer
- [ ] Race timer display
- [ ] Connection status indicator

#### Tasks

- [ ] Set up Next.js project
- [ ] Implement authentication (NextAuth/Clerk)
- [ ] Build tournament pages
- [ ] Integrate video player
- [ ] Integrate control interface
- [ ] Add real-time updates (WebSocket)
- [ ] Mobile-responsive design

---

### Phase 5: Polish & Launch (Week 5-6)

#### Features

- [ ] Spectator mode with commentator view
- [ ] Race replays (record video segments)
- [ ] User profiles and stats
- [ ] Tournament history
- [ ] Social sharing (results, clips)

#### Operations

- [ ] Monitoring and alerts
- [ ] Rate limiting and abuse prevention
- [ ] Backup and recovery
- [ ] Documentation

#### Testing

- [ ] Load testing (multiple concurrent users)
- [ ] Latency testing (various network conditions)
- [ ] Edge cases (disconnects, timeouts)

---

## Technology Stack

| Component     | Technology                  | Reason                             |
| ------------- | --------------------------- | ---------------------------------- |
| RC Control    | ESP32 + UDP                 | Low latency, simple protocol       |
| Video Capture | Raspberry Pi + Camera       | Hardware encoding, flexible        |
| Video Server  | MediaMTX                    | Open source, WebRTC support        |
| Control Relay | Pi + aiortc                 | WebRTC DataChannel to UDP bridge   |
| Backend       | Cloudflare Workers          | Serverless, global edge            |
| Database      | PostgreSQL                  | Reliable, good for relational data |
| Frontend      | Vanilla JS (single HTML)    | No build step, simple deployment   |
| Hosting       | Cloudflare Workers + Tunnel | Free tier, low latency             |
| Auth          | HMAC tokens                 | Simple, no third-party deps        |
| Real-time     | WebRTC DataChannel          | P2P, ~10-15ms latency              |

---

## Hardware Bill of Materials

| Item                       | Purpose           | Est. Cost |
| -------------------------- | ----------------- | --------- |
| ESP32                      | Car control       | $10       |
| Raspberry Pi Zero 2W       | Video streaming   | $15       |
| Pi Camera Module           | FPV capture       | $25       |
| IR Break-beam sensors (x2) | Timing            | $15       |
| Arduino Nano               | Timing controller | $5        |
| Power bank                 | Pi power on car   | $20       |
| **Total**                  |                   | **~$90**  |

---

## Risk Assessment

| Risk                | Impact           | Mitigation                          |
| ------------------- | ---------------- | ----------------------------------- |
| High video latency  | Poor UX          | Test multiple streaming solutions   |
| Network instability | Lost control     | Implement timeout safety, auto-stop |
| User cheating       | Unfair results   | Server-side timing only             |
| Hardware failure    | Tournament stops | Have backup components              |
| Cloud costs         | Budget overrun   | Start small, monitor usage          |

---

## MVP Scope

For initial launch, focus on:

1. ✅ Basic car control (done)
2. ✅ Token-based access control (done)
3. ✅ Racing game UI (done)
4. ✅ Session persistence (done)
5. ✅ Video streaming over internet (done)
6. ✅ Controls blocked until video ready (done)
7. 🔲 Simple user queue (no auth, just tokens)
8. 🔲 Manual timing (admin starts/stops timer)
9. 🔲 Basic leaderboard

This allows testing the core concept before building full tournament system.

---

## Next Steps

1. **Current**: System is fully functional for single-user remote control
2. **Next**: Build basic queue system for multiple users
3. **Then**: Add manual timing (admin starts/stops timer)
4. **Later**: Implement IR sensor timing for precision
5. **Future**: Full tournament system with leaderboards

---

## Completed Work Log

- [x] ESP32 UDP server with binary protocol
- [x] DAC voltage control for throttle/steering
- [x] Ping/pong latency measurement
- [x] Auto-neutral on disconnect (staged: 80ms hold → 250ms neutral)
- [x] WiFi auto-reconnect
- [x] WiFi power save disabled for low latency
- [x] Touch controls with dual-zone layout
- [x] Relative touch positioning
- [x] Keyboard support (WASD + arrows)
- [x] Throttle limiting (asymmetric: 25% forward, 20% backward)
- [x] Safety limits enforced on ESP32 (not browser)
- [x] Slider visual decoupled from output limit
- [x] HMAC-SHA256 token authentication
- [x] Token generator script
- [x] Racing game UI with FPV video
- [x] HUD overlay (status, latency, values)
- [x] LocalStorage token persistence
- [x] Cloudflare Workers for static files + TURN credentials
- [x] Raspberry Pi Zero 2W + Camera Module 3 setup
- [x] MediaMTX WebRTC streaming
- [x] Cloudflare Tunnel for camera WHEP + control relay
- [x] Cloudflare TURN for WebRTC NAT traversal
- [x] Video stats overlay (resolution, fps, bitrate, RTT)
- [x] Controls disabled until video connects
- [x] Auto-start MediaMTX and Tunnel on Pi boot
- [x] TURN credentials refresh script for Pi
- [x] **WebRTC DataChannel control relay (Pi)**
- [x] **Direct P2P connection (10-15ms RTT)**
- [x] **ESP32 FreeRTOS dual-core (UDP Core 0, Control Core 1)**
- [x] **200 Hz output loop with EMA smoothing**
- [x] **Slew rate limiting (8.0/sec max change)**
- [x] **Staged timeout (80ms hold, 250ms neutral)**
- [x] **Removed Durable Objects (not needed)**
- [x] **ESP32 beacon discovery for Pi**
- [x] **50Hz control loop from browser**
- [x] visibilitychange handler (immediate neutral on tab hide)
- [x] Increased deadband to 5% (DAC noise filtering)
- [x] **Auto-reconnect on connection loss (exponential backoff)**
- [x] **FPV video auto-reconnect**
- [x] **Proper disconnection UI state (controls disabled)**
- [x] **Open source preparation (secrets externalized)**
- [x] **SETUP.md deployment guide**
- [x] **Admin dashboard (admin.html) with basic auth**
- [x] **Race state management (idle → countdown → racing)**
- [x] **Admin kick player with token revocation**
- [x] **Persistent revoked tokens (file-based, last 10)**
- [x] **Smooth keyboard steering interpolation**
- [x] **Video status reporting from browser to Pi**
- [x] **Throttle limit control from admin (10-50% range)**
- [x] **ESP32 hard throttle limit raised to 50% forward, 30% backward**
- [x] **CMD_KICK notification to browser on kick**
- [x] **Player "Ready" button (must click before admin can start race)**
- [x] **Admin token generator (web UI)**
- [x] **Deploy script for Pi (deploy.sh)**
- [x] **YouTube restreamer on Fly.io (WHEP → RTMP)**
- [x] **Admin YouTube streaming controls (Go Live / Stop)**

---

## Known Issues

- **Voltage calibration varies with battery**: ESP32 VDD drops when TX batteries are low (~5.5V input → 3.12V VDD). Neutral voltages need recalibration. Current values: THR_V_NEU=1.69V, STR_V_CTR=1.66V.
- **4G modem WiFi range**: Limited coverage radius when using 4G modem as hotspot. Car can drive out of range causing connection drops. Auto-reconnect helps but driving back into range is required.

---

## Resolved Issues

- ~~**Control stuttering**~~: Fixed by implementing proper control loop architecture:
  - Separated UDP receive (Core 0) from control output (Core 1)
  - 200 Hz output loop with EMA smoothing (α=0.25)
  - Slew rate limiting prevents sudden jumps
  - Staged timeout (80ms hold, 250ms neutral) handles brief packet gaps
- ~~**High latency (100-200ms)**~~: Fixed by switching from WebSocket relay to WebRTC DataChannel. Now 10-15ms RTT.
- ~~**WiFi sleep vs DAC noise tradeoff**~~: WiFi sleep now disabled (`WiFi.setSleep(false)` + `esp_wifi_set_ps(WIFI_PS_NONE)`). DAC noise handled by EMA smoothing.
- ~~**iPhone hotspot AP isolation**~~: Not a problem - Pi and ESP32 communicate directly on local network, browser connects via TURN.

---

## Open Questions

- [ ] Where will the track be located? (affects internet connectivity)
- [ ] How will the car return to start between racers?
- [ ] What happens if the car crashes/flips?
- [ ] Do we need multiple camera angles?
- [ ] Should spectators see driver's control inputs?
- [ ] Time limit per race attempt?
- [x] What's the optimal throttle limit for safe testing? → 25%
- [x] Which video streaming solution gives best latency/quality tradeoff? → MediaMTX + WebRTC
- [x] What's causing intermittent control stuttering? → Fixed with dual-core + EMA + 200Hz loop

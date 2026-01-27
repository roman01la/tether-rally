Alright — here’s a **comprehensive, engineering-style specification** for an **ultra-low-latency FPV video system** with:

- **True P2P**
- **No relay/TURN**
- **UDP hole punching (ICE-lite-ish, but custom)**
- **H.264 over UDP**
- **Raspberry Pi Zero 2W (libcamera) → macOS desktop app (VideoToolbox)**

This is intentionally “ruthless”: newest frame wins, drop aggressively, recover via frequent IDR.

---

# 1. Project summary

## 1.1 Objective

Create a minimal FPV video link that sends **H.264** from a Raspberry Pi over the public internet (4G uplink) to a macOS desktop receiver with **minimum end-to-end latency**.

## 1.2 Core principles

- **No retransmissions**
- **No jitter buffer**
- **No smoothing**
- **Drop old frames everywhere**
- **Fast recovery** via frequent keyframes

## 1.3 Scope

In scope:

- P2P NAT traversal via UDP hole punching
- H.264 encoding on Pi
- UDP packetization / reassembly
- H.264 decode + render on macOS
- Telemetry / stats for latency & loss

Out of scope:

- Any relay server (TURN/SFU/CDN)
- Controls, audio, recording, multi-viewer

---

# 2. System architecture

## 2.1 Components

### A) Pi Sender (Raspberry Pi Zero 2W)

- Captures camera frames via **libcamera**
- Encodes **H.264 Annex B**
- Packetizes frames into UDP datagrams
- Performs NAT traversal handshake
- Sends keepalives to maintain NAT mapping
- Responds to “IDR request” (optional but recommended)

### B) macOS Receiver (native desktop app)

- Performs NAT traversal handshake
- Receives UDP datagrams
- Reassembles H.264 Access Units (AUs)
- Decodes using **VideoToolbox**
- Renders using **Metal** (or lowest-latency path you choose)
- Implements “latest-frame wins” policy

### C) Signaling Server (NOT a relay)

Purpose: exchange connection metadata only.

- Used for exchanging:
  - public endpoints discovered via STUN (`srflx`)
  - session tokens / keys
  - negotiation state

- Must never forward video payload.

This can be a tiny HTTPS/WebSocket service.

### D) STUN Server(s)

- Public STUN for NAT discovery (can use well-known STUN servers)
- May be multiple for robustness

---

# 3. Network model and assumptions

## 3.1 NAT types supported (best effort)

This system can work when at least one of the following holds:

- Both sides are on “reasonable” NATs that allow UDP hole punching
- One side has a publicly reachable UDP endpoint (rare on 4G)

## 3.2 Expected failure modes (by design, since no relay)

This system **will fail** in some real-world networks, including:

- Symmetric NAT on one or both sides (common on some cellular carriers)
- Strict firewall policies blocking UDP
- Carrier policies that rotate mappings aggressively

When hole punching fails, the session cannot be established (no fallback).

---

# 4. Video encoding requirements (Pi)

## 4.1 Codec

- H.264
- Format: **Annex B** (start-code NAL units)

## 4.2 Rate / resolution targets (initial)

- 2 Mbps, 60 fps target
- Recommended starting res for 4G: **960×540 @ 60fps** (then tune upward if stable)

## 4.3 Keyframe strategy (critical for lossy transport)

- IDR interval: **0.5s** (every 30 frames at 60fps) initially
- Repeat SPS/PPS with every IDR (“inline headers”)

Rationale:

- UDP loss is inevitable; fast IDR = fast recovery.

## 4.4 Encoder latency constraints

- No B-frames (if configurable)
- Avoid lookahead / large buffers
- Prefer baseline/constrained baseline profile

---

# 5. Transport protocol (UDP payload)

## 5.1 Packet size / MTU strategy

- Avoid IP fragmentation.
- UDP payload size target: **≤ 1200 bytes** (excluding UDP/IP headers)
- A single video frame (AU) will usually require multiple packets.

## 5.2 Framing unit

- The unit of “displayable time” is an **Access Unit (AU)**.
- Sender packetizes each AU into fragments.

## 5.3 Datagram message format

All UDP datagrams start with:

### 5.3.1 Common header

- `u8  msg_type`
- `u8  version` (1)
- `u16 header_len` (bytes)
- `u32 session_id` (random per session)

### 5.3.2 VIDEO_FRAGMENT (msg_type = 0x01)

Fields:

- `u32 frame_id` (monotonic, wraps ok)
- `u16 frag_index` (0..frag_count-1)
- `u16 frag_count`
- `u32 ts_ms` (monotonic timestamp at sender)
- `u8  flags` (bit0=keyframe, bit1=contains_spspps)
- `u16 payload_len`
- `bytes payload` (raw AU fragment bytes, Annex B)

### 5.3.3 KEEPALIVE (msg_type = 0x02)

- `u32 ts_ms`
- Used to keep NAT mapping alive (and optional RTT measurement).

### 5.3.4 IDR_REQUEST (msg_type = 0x03) — optional but recommended

- `u32 seq`
- `u8  reason` (startup / decode_error / loss / user)
- Sent receiver→sender to trigger immediate IDR.

---

# 6. Receiver policies (FPV behavior)

## 6.1 Reassembly model

Receiver maintains a small map of in-flight frames:

- key: `frame_id`
- value: fragment bitmap + buffers + first_seen_time

## 6.2 Drop policy (hard realtime)

- If an AU is incomplete after **FRAME_TIMEOUT_MS** (default 20ms), drop it.
- If `frame_id_new` arrives and `frame_id_old < frame_id_new`, drop older incomplete frames immediately.
- Cap memory: max N frames in-flight (e.g. 4). Drop oldest beyond cap.

## 6.3 Decode policy

- Decode only the most recent complete AU available.
- If decoder errors / corruption:
  - drop delta frames until next keyframe
  - send `IDR_REQUEST` (if supported)

## 6.4 Render policy

- Maintain a single `latestDecodedFrame`
- Render loop always draws newest and discards previous
- Never queue more than one frame for display

---

# 7. NAT traversal specification (No relay)

This is “custom ICE-lite-ish”. Only the minimum to hole punch.

## 7.1 Roles

- Pi = **Offerer**
- macOS = **Answerer**

## 7.2 Discovery

Both peers perform STUN binding to discover:

- `srflx_ip:srflx_port` (public mapped address)
- NAT mapping lifetime estimation (optional)

## 7.3 Candidate exchange via signaling

Each peer sends to signaling server:

- `session_id`
- `srflx endpoint`
- optionally `local endpoint` (LAN candidate for same-network optimization)
- connection nonce / keys (security)

Signaling server returns the other peer’s endpoints.

## 7.4 Punching algorithm (simultaneous open)

Once both sides know each other’s srflx endpoint:

Each side repeatedly sends small UDP “probe” packets to the other side:

- Send probes at 50–100 Hz for up to **PUNCH_WINDOW_MS** (e.g. 3000ms)
- Probes include `session_id` and a random nonce
- On receiving a valid probe from expected peer:
  - mark path “validated”
  - immediately start sending KEEPALIVE every ~1–2s
  - begin video transmission

## 7.5 Path validation rules

- Accept packets only matching current `session_id`
- Must confirm liveness in both directions:
  - receiver sees sender packets
  - sender sees receiver packets (probes/keepalives)

## 7.6 NAT keepalive

To keep the hole open:

- Send KEEPALIVE every **1s** initially (cellular NATs can be aggressive)
- If stable, back off to 2s (optional)

## 7.7 Reconnect strategy

If no packets received for **TIMEOUT_MS** (e.g. 2–3s):

- re-run STUN discovery
- re-exchange candidates via signaling
- re-run punching window

---

# 8. Security model (optional, but recommended)

You can run it insecure for a lab demo, but over the internet you’ll eventually want protection.

## 8.1 Threats

- Spoofed UDP packets (DoS / garbage injection)
- Session hijacking (if someone guesses endpoints)

## 8.2 Lightweight authentication (minimal overhead)

- Each packet includes a short authentication tag, e.g.:
  - `HMAC-SHA256(session_key, header+payload)` truncated to 8–16 bytes

- Session key negotiated via signaling (over HTTPS) using random tokens.

This adds small CPU cost but prevents random injection.

Encryption is optional (adds more overhead). If you want encryption later, consider a Noise-based handshake and AEAD (still doable).

---

# 9. Performance targets and instrumentation

## 9.1 Metrics (must log)

- packets received/sec
- packet loss estimate (reassembly misses)
- frames completed/sec
- frames dropped/sec (timeout / superseded)
- decoder errors
- time from frame complete → decoded → rendered
- “frame age at render” (receiver clock – sender timestamp, with rough offset)

## 9.2 Latency measurement method

- Sender includes `ts_ms` per frame (monotonic)
- Receiver estimates offset using periodic keepalive ping-pong
- Report:
  - one-way “frame age” at render (approx)
  - plus local pipeline timings (exact)

---

# 10. Test plan

## 10.1 Phase 1: Local lab

- Both peers on same LAN (no NAT complexity)
- Validate packetization, decode, render, drop policies

## 10.2 Phase 2: NAT punching

- Put Pi behind home router NAT, receiver on different network
- Validate STUN + punching

## 10.3 Phase 3: Real 4G

- Pi behind 4G modem
- Attempt multiple sessions at different times/days
- Record success rate of P2P establishment (expect variability)

## 10.4 Phase 4: Stress

- Increase motion / scene complexity
- Validate recovery speed after induced packet loss
- Tune:
  - IDR interval (30 vs 60)
  - resolution
  - bitrate

---

# 11. Deployment / operational notes

## 11.1 Signaling server requirements

- HTTPS/WSS reachable from both peers
- Must store no video
- Minimal endpoints:
  - `POST /offer`
  - `POST /answer`
  - `GET /session/<id>` or WS room

## 11.2 STUN servers

- Use multiple to reduce dependency
- Cache working STUN choice per network

---

# 12. Known limitations (explicit)

- **No relay = not guaranteed to connect.**
- Symmetric NAT and some 4G carrier NATs will break P2P.
- Over lossy networks, video will show artifacts; recovery depends on IDR cadence.

---

## Default configuration (recommended starting defaults)

- Resolution: **1280×720**
- FPS: **60**
- Bitrate: **2 Mbps**
- IDR period: **30 frames**
- SPS/PPS inline: **enabled**
- UDP payload: **≤1200 bytes**
- Frame assembly timeout: **20ms**
- Keepalive: **1s**

---

Awesome — here are the two add-ons:

1. **Packet format appendix** (exact byte layouts, endianness, offsets)
2. **State machines** for both peers (punching + streaming lifecycle)

I’m going to be very explicit and “wire-format-y” so you can implement it without interpretation drift.

---

# Appendix A — Wire format

## A.0 Conventions

- **Byte order:** Big-endian (“network order”) for all multi-byte integers.
- **Alignment:** Packed, no padding.
- **Datagram MTU target:** total UDP payload ≤ **1200 bytes**.
- **Session:** A single run of negotiation + streaming identified by `session_id`.

---

## A.1 Common UDP header (all message types)

| Offset | Size | Type | Name                      | Notes                                      |
| -----: | ---: | ---- | ------------------------- | ------------------------------------------ |
|      0 |    1 | u8   | `msg_type`                | See A.2                                    |
|      1 |    1 | u8   | `version`                 | = 1                                        |
|      2 |    2 | u16  | `header_len`              | Total header bytes including Common header |
|      4 |    4 | u32  | `session_id`              | Random per session                         |
|      8 |    … |      | `type_specific_header...` | Starts immediately                         |

**Validation rules**

- Drop packet if `version != 1`.
- Drop packet if `header_len < 8` or `header_len > packet_len`.
- Drop packet if `session_id != current_session_id` (except during punching when you may accept pending session IDs; see state machine).

---

## A.2 Message types

| msg_type | Meaning        | Direction                             |
| -------: | -------------- | ------------------------------------- |
|     0x01 | VIDEO_FRAGMENT | Pi → macOS                            |
|     0x02 | KEEPALIVE      | both                                  |
|     0x03 | IDR_REQUEST    | macOS → Pi (optional but recommended) |
|     0x04 | PROBE          | both (punching)                       |
|     0x05 | HELLO          | both (optional capability exchange)   |

---

## A.3 VIDEO_FRAGMENT (msg_type = 0x01)

### Header layout

Common header (8 bytes) +:

| Offset | Size | Type  | Name          | Notes                          |
| -----: | ---: | ----- | ------------- | ------------------------------ |
|      8 |    4 | u32   | `stream_id`   | = 1 for v1                     |
|     12 |    4 | u32   | `frame_id`    | Monotonic; wraps ok            |
|     16 |    2 | u16   | `frag_index`  | 0..frag_count-1                |
|     18 |    2 | u16   | `frag_count`  | ≥ 1                            |
|     20 |    4 | u32   | `ts_ms`       | Sender monotonic ms            |
|     24 |    1 | u8    | `flags`       | bit0=keyframe, bit1=has_spspps |
|     25 |    1 | u8    | `codec`       | 1 = H264                       |
|     26 |    2 | u16   | `payload_len` | bytes                          |
|     28 |    N | bytes | `payload`     | raw fragment bytes             |

So: `header_len` for VIDEO_FRAGMENT is **28**.

### Flags (u8)

- bit0 `KEY`: this AU is a keyframe (IDR)
- bit1 `SPSPPS`: SPS/PPS are included somewhere in this AU (recommended true for keyframes when using inline headers)
- bits 2–7 reserved (0)

### Validation rules

- Drop if `codec != 1`.
- Drop if `frag_count == 0` or `frag_index >= frag_count`.
- Drop if `payload_len != packet_len - header_len`.
- Drop if packet_len > 1200 (soft rule; log).

---

## A.4 KEEPALIVE (msg_type = 0x02)

Common header +:

| Offset | Size | Type | Name         | Notes                                                                  |
| -----: | ---: | ---- | ------------ | ---------------------------------------------------------------------- |
|      8 |    4 | u32  | `ts_ms`      | Sender monotonic ms                                                    |
|     12 |    4 | u32  | `seq`        | Monotonic sequence                                                     |
|     16 |    4 | u32  | `echo_ts_ms` | For ping-pong RTT/offset; sender copies last seen remote ts_ms, else 0 |

`header_len` = **20**.

Usage:

- During steady state, each side sends KEEPALIVE every **1000 ms**.
- Receiver may compute RTT by matching `echo_ts_ms`.

---

## A.5 IDR_REQUEST (msg_type = 0x03) (optional but recommended)

Common header +:

| Offset | Size | Type  | Name       | Notes                                  |
| -----: | ---: | ----- | ---------- | -------------------------------------- |
|      8 |    4 | u32   | `seq`      | Monotonic                              |
|     12 |    4 | u32   | `ts_ms`    | Receiver monotonic ms                  |
|     16 |    1 | u8    | `reason`   | 1=startup,2=decode_error,3=loss,4=user |
|     17 |    3 | bytes | `reserved` | 0                                      |

`header_len` = **20**.

Policy:

- Send on startup right after punch success.
- Send after decode error or if no successful decode for X ms (e.g., 500ms).

---

## A.6 PROBE (msg_type = 0x04) — punching connectivity check

Common header +:

| Offset | Size | Type | Name        | Notes              |
| -----: | ---: | ---- | ----------- | ------------------ |
|      8 |    4 | u32  | `ts_ms`     | Monotonic ms       |
|     12 |    4 | u32  | `probe_seq` | Monotonic          |
|     16 |    8 | u64  | `nonce`     | Random per session |
|     24 |    1 | u8   | `role`      | 1=PI, 2=MAC        |
|     25 |    1 | u8   | `flags`     | bit0=ack_requested |
|     26 |    2 | u16  | `reserved`  | 0                  |

`header_len` = **28**.

Rules:

- During punching, both sides send PROBE to candidate endpoints at **50–100 Hz**.
- When you receive a valid PROBE, you may respond immediately with:
  - another PROBE (normal send loop continues), and/or
  - a KEEPALIVE echoing timestamps.

- A “path validated” condition: each side must receive ≥1 valid PROBE from the other after it started probing.

---

## A.7 HELLO (msg_type = 0x05) — optional capabilities exchange

Useful to convey codec profile string / resolution. Since you’re Annex B, you can live without it, but it helps.

Common header +:

| Offset | Size | Type  | Name                  |                                     |
| -----: | ---: | ----- | --------------------- | ----------------------------------- |
|      8 |    2 | u16   | `width`               |                                     |
|     10 |    2 | u16   | `height`              |                                     |
|     12 |    2 | u16   | `fps_x10`             | fps \* 10 (e.g., 600)               |
|     14 |    4 | u32   | `bitrate_bps`         |                                     |
|     18 |    1 | u8    | `avc_profile`         | e.g. 66 baseline, 77 main, 100 high |
|     19 |    1 | u8    | `avc_level`           | e.g. 31 = level 3.1                 |
|     20 |    4 | u32   | `idr_interval_frames` |                                     |
|     24 |    8 | bytes | `reserved`            |                                     |

`header_len` = **32**.

---

## A.8 Frame reassembly structure (receiver internal)

Not on wire, but defines behavior.

For each `frame_id`:

- `frag_count` (u16)
- bitmap `received[frag_count]`
- `bufs[frag_count]` pointers/lengths
- `first_seen_ms` (receiver monotonic)
- `is_keyframe` (from flags)
- `sender_ts_ms`

Completion:

- if all fragments received → concatenate in order into AU buffer
- AU buffer is fed to decoder immediately (subject to drop rules)

---

# Appendix B — State machines

I’ll define **two state machines**:

- **B.1 Session negotiation & hole punching** (both peers)
- **B.2 Streaming pipeline behavior** (sender + receiver)

I’ll use a consistent set of timers and thresholds so implementations match.

---

## B.0 Shared timers and constants (defaults)

- `PUNCH_WINDOW_MS = 3000`
- `PROBE_INTERVAL_MS = 10` (100 Hz) or 20ms (50 Hz)
- `KEEPALIVE_INTERVAL_MS = 1000`
- `SESSION_IDLE_TIMEOUT_MS = 3000` (no packets received)
- `FRAME_TIMEOUT_MS = 20`
- `MAX_INFLIGHT_FRAMES = 4`

---

## B.1 Hole punching / session lifecycle

### States (both peers)

1. `IDLE`
2. `SIGNALING_CONNECT`
3. `STUN_GATHER`
4. `EXCHANGE_CANDIDATES`
5. `PUNCHING`
6. `CONNECTED`
7. `STREAMING`
8. `RECONNECTING`
9. `FAILED`

---

### B.1.1 IDLE → SIGNALING_CONNECT

**Entry:** app starts or user initiates session
**Action:** connect to signaling server (HTTPS/WSS)
**Exit:** on signaling ready

---

### B.1.2 SIGNALING_CONNECT → STUN_GATHER

**Action:** start STUN binding discovery:

- allocate local UDP socket on chosen local port
- perform STUN to 1–3 STUN servers
- record:
  - `local_endpoint` (ip:port)
  - `srflx_endpoint` (public mapped ip:port)

**Exit:** when srflx is acquired (or timeout → FAILED)

---

### B.1.3 STUN_GATHER → EXCHANGE_CANDIDATES

**Action:** send to signaling server:

- `session_id`
- `nonce` (u64)
- `role` (PI or MAC)
- candidates:
  - srflx endpoint
  - optional local endpoint (LAN optimization)

- optional HELLO caps

**Exit:** when you receive the remote peer’s candidates + nonce

---

### B.1.4 EXCHANGE_CANDIDATES → PUNCHING

**Action on entry:**

- Build a candidate list to try in priority order:
  1. remote local endpoint (if both are in same LAN / private range heuristic)
  2. remote srflx endpoint

- Start timer `punch_deadline = now + PUNCH_WINDOW_MS`

- Start periodic send loop: every `PROBE_INTERVAL_MS`:
  - send PROBE to each remote candidate endpoint

**Receive handler in PUNCHING:**

- On valid PROBE with matching `session_id` and expected `nonce`:
  - record `peer_endpoint = src_addr` (important: use the actual source address you observed)
  - mark `rx_probe_ok = true`

- On valid KEEPALIVE:
  - record liveness; may mark `rx_probe_ok = true` as well

**Punch success condition:**

- You have sent probes, and you have received ≥1 valid probe/keepalive from remote (`rx_probe_ok`)
- Optional stricter: you also observed remote receiving you (implicit; you can require a KEEPALIVE echo)

**Exit:**

- If success before deadline → `CONNECTED`
- If deadline exceeded → `FAILED`

---

### B.1.5 CONNECTED → STREAMING

**Actions:**

- Lock in `peer_endpoint` as the destination for all traffic.
- Start KEEPALIVE timer (send every 1s).
- Exchange HELLO if not already.
- Receiver sends IDR_REQUEST immediately (recommended).

**Exit:**

- Sender: starts sending video fragments continuously
- Receiver: enters streaming receive pipeline

---

### B.1.6 STREAMING → RECONNECTING

**Trigger:**

- No packets received (any type) for `SESSION_IDLE_TIMEOUT_MS`
- Or repeated decode failures + no keyframes for too long
- Or local socket error

**Actions:**

- Stop video send/receive loops
- Go back to STUN_GATHER (mappings may have changed)
- Notify signaling server of reconnect attempt (new `session_id` recommended)

---

### B.1.7 RECONNECTING → PUNCHING

Same as earlier but with fresh `session_id` and `nonce`.

---

## B.2 Streaming behavior

### B.2.1 Pi Sender (STREAMING)

**Primary loop:**

- Acquire encoded AU (Annex B) per frame.
- Determine if keyframe (IDR) — encoder may expose this; otherwise parse NALs for type 5 if you must.
- Compute number of fragments `frag_count = ceil(AU_len / payload_max)`
- For each fragment:
  - fill VIDEO_FRAGMENT header
  - send UDP datagram immediately

**Constraints:**

- Do not queue AUs in user space.
- If send fails/backpressure occurs at socket level:
  - drop the remainder of the current frame and continue to next AU
  - (optional) set a flag to encourage next IDR sooner

**On receiving IDR_REQUEST:**

- Trigger encoder to emit IDR ASAP (if supported)
- Otherwise ignore; periodic IDR will handle recovery.

---

### B.2.2 macOS Receiver (STREAMING)

#### Receive thread (UDP)

On VIDEO_FRAGMENT:

- If `frame_id` < `newest_seen_frame_id - 1` (wrap-aware compare):
  - drop (too old)

- Maintain inflight map; if new `frame_id` is newer:
  - optionally drop incomplete older frames immediately

- Add fragment to assembly
- If complete:
  - finalize AU buffer
  - mark `latest_complete_AU = this` (overwrite previous complete AU if any)

**Timeout enforcement (every few ms):**

- For each inflight assembly:
  - if `now - first_seen_ms > FRAME_TIMEOUT_MS`: drop assembly

- Ensure `inflight_count <= MAX_INFLIGHT_FRAMES`:
  - drop oldest (by frame_id or first_seen)

#### Decode thread

- If `latest_complete_AU` exists:
  - if decoder is “waiting for keyframe” and AU is delta: drop AU
  - submit AU to VideoToolbox
  - clear `latest_complete_AU`

On decode output:

- Replace `latest_decoded_frame` with newest; release previous immediately

On decode error:

- set `need_keyframe = true`
- send IDR_REQUEST
- drop all delta AUs until keyframe arrives

#### Render loop

- At display cadence:
  - draw `latest_decoded_frame` immediately
  - do not buffer

---

# Appendix C — Wrap-around comparisons (important)

`frame_id` is u32 and wraps. Use serial number arithmetic:

Define:

- `is_newer(a, b)` if `(int32)(a - b) > 0`
- `is_older(a, b)` if `(int32)(a - b) < 0`

This is standard RTP-style logic.

---

# Appendix D — Signaling server API (minimal)

Not a relay; just exchanges JSON blobs.

## D.1 Create session

`POST /session`

- response: `{ session_id, role_token_pi, role_token_mac }`

## D.2 Publish candidates

`POST /session/{session_id}/candidates`
Body:

```json
{
  "role": "pi" | "mac",
  "nonce": "u64-as-string",
  "srflx": {"ip":"x.x.x.x","port":12345},
  "local": {"ip":"192.168.1.10","port":50000},
  "hello": {"w":960,"h":540,"fps":60,"bitrate":2000000,"idr":30}
}
```

## D.3 Subscribe / wait for remote candidates

Either:

- `GET /session/{session_id}/remote?role=pi`
- or WSS room that pushes updates

Security:

- token per role; session expires quickly

---

Perfect — here are the two extra pieces:

1. **A formal “no-queue” rule list** (so latency never silently grows)
2. **Socket buffer sizing + OS tuning recommendations** (macOS + Pi/Linux)

---

# Appendix E — “No-queue” rules (hard constraints)

These are _design invariants_. If you violate them, your system turns into “smooth but delayed streaming” instead of FPV.

## E.1 Global invariants

**E1. Latest wins everywhere.**
At every stage, you may hold at most:

- a small number of _in-flight_ partial frames (for reassembly)
- one _latest complete AU_
- one _latest decoded frame_

**E2. Dropping is always preferred to waiting.**
If any stage would block or build backlog, drop older work.

**E3. Never let OS queues turn loss into latency.**
If the kernel buffer accumulates packets faster than you process, you must:

- process faster, or
- reduce buffer capacity so packets drop earlier

**E4. The system must be stable under overload.**
When bitrate spikes or CPU dips, latency must stay bounded; quality is allowed to degrade.

---

## E.2 Sender-side (Pi) no-queue rules

**S1. Encoder output is the only “truth.”**
Don’t create an application-level queue of AUs. You pull AUs and immediately send fragments.

**S2. If send() fails / blocks / backpressures, you drop.**
You are not allowed to wait for the network to “catch up.”

- If you detect congestion, drop the remainder of that AU and continue.

**S3. No retransmission, no ACK-based recovery.**
Do not implement per-packet retransmit. Recovery is via keyframes.

**S4. Keyframe policy is “cheap recovery.”**
Short IDR interval and inline SPS/PPS are part of the no-queue philosophy.

---

## E.3 Receiver-side no-queue rules

### UDP receive / kernel boundary

**R1. The UDP socket receive buffer is not a queue.**
It’s a “loss absorber” with a strict cap. If you see backlog, shrink it until latency doesn’t grow.

**R2. Never copy packets into an unbounded structure.**
No `std::vector` that grows forever. Any packet storage must have a hard upper bound.

### Reassembly

**R3. In-flight frames are bounded.**

- `MAX_INFLIGHT_FRAMES` is a hard cap (default 4).
- On arrival of a newer `frame_id`, incomplete older frames may be dropped immediately.

**R4. In-flight frames have a hard time budget.**

- `FRAME_TIMEOUT_MS` (default 20ms) is a hard deadline.
- Missed fragments → drop the whole frame when it times out.

**R5. Complete AU storage is a single-slot.**

- `latest_complete_AU` is overwritten by newer frames.
- You never maintain a FIFO of complete frames.

### Decode

**R6. Decode input is also single-slot.**

- If decoder is still processing and a newer AU arrives:
  - prefer dropping the older AU (don’t queue).

**R7. Decoder output is single-slot.**

- Keep only `latest_decoded_frame`.
- On new output, release the previous frame immediately.

**R8. “Waiting for keyframe” is explicit.**

- If decode errors → enter `need_keyframe` mode.
- Drop all deltas until next keyframe.

### Render

**R9. Render only the newest frame.**

- Render loop always uses the latest decoded frame.
- Never build a present queue to “smooth” motion.

---

## E.4 Telemetry rules (to catch accidental buffering)

You must continuously compute:

- `rx_socket_queue_hint` (see Appendix F)
- `frame_age_at_render` (approx)
- `inflight_frames`
- `latest_complete_AU_age`

**Fail-fast condition:**
If `latest_complete_AU_age` or `frame_age_at_render` steadily increases while bitrate is constant, you have a queue somewhere. Trigger an alert in logs.

---

# Appendix F — Socket buffers and OS tuning (macOS + Pi/Linux)

The goal is to prevent the kernel from becoming a multi-100ms “hidden jitter buffer.”

## F.1 UDP packet sizing reminder

- Keep UDP payload ≤ **1200 bytes** (your own fragmentation).
- This reduces the chance of IP fragmentation and makes loss “smaller.”

---

## F.2 Receiver (macOS) socket buffer sizing

### Principle

For FPV, you want the kernel buffer to hold on the order of **a few milliseconds** of packets, not hundreds.

At 2 Mbps:

- 2,000,000 bits/s = 250,000 bytes/s
- That’s ~250 bytes/ms
  If your UDP payload is ~1200 bytes, that’s ~0.2 packets/ms.

So:

- A 256 KB socket buffer could represent ~1 second of data _in theory_ (depending on packetization and overhead).
  You do **not** want that.

### Recommended starting values

- `SO_RCVBUF`: start around **64 KB** on macOS receiver.
- If you see frequent drops but latency is stable, increase to **128 KB**.
- If latency grows under load, decrease.

Also set:

- `SO_REUSEADDR` (optional)
- Consider `SO_TIMESTAMP` / `SO_TIMESTAMP_MONOTONIC` equivalents for packet timing if you want (platform-specific)

### How to detect kernel queueing (practical method)

You can estimate if you’re reading too slowly by tracking:

- `packets_received_per_second`
- time between reads
- and whether “old frame_ids” are arriving

**Strong signal of kernel queueing:**
You observe a burst of packets with very old `frame_id` values after a temporary stall (e.g., a 50ms hitch). That means they were queued instead of dropped.

If you want a more direct hint:

- `ioctl(FIONREAD)` tells how many bytes are waiting in the socket buffer.
- If it’s consistently large, you’re queueing.

**Rule:** if `FIONREAD` corresponds to > ~10–20ms of data repeatedly, reduce `SO_RCVBUF`.

---

## F.3 Sender (Pi/Linux) socket buffer sizing

Pi sender can keep modest buffers too:

- `SO_SNDBUF`: 64–256 KB is fine.
- If `sendto()` blocks or errors, you drop frames rather than buffering.

On Linux you can also set:

- `SO_PRIORITY` (optional)
- `IP_TOS` / DSCP (optional; won’t help much over mobile networks unless your carrier honors it)

---

## F.4 Kernel-level limits (Pi/Linux)

Linux has caps like:

- `net.core.rmem_max`, `net.core.wmem_max`
- `net.core.netdev_max_backlog`

For FPV, don’t crank these up globally unless you know why — bigger buffers increase “hidden latency.” If anything, you might keep them reasonable.

---

## F.5 Nagle’s algorithm / TCP stuff

Not relevant (UDP). Mentioned only to avoid rabbit holes.

---

## F.6 Threading priorities (macOS)

Latency spikes often come from scheduling.

- Give your UDP receive + decode threads higher priority than UI fluff.
- Make sure your render loop can run consistently.
  (Exact API is implementation detail, but the spec requirement is: avoid long stalls.)

---

# Appendix G — Hard caps checklist (copy/paste)

These are the _must-have_ caps in code:

- `MAX_INFLIGHT_FRAMES = 4`
- `FRAME_TIMEOUT_MS = 20`
- `latest_complete_AU` = single slot (overwrite)
- `latest_decoded_frame` = single slot (overwrite)
- `SO_RCVBUF = 64KB (start)`
- UDP payload size ≤ 1200 bytes

---

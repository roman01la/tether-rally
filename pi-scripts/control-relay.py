#!/usr/bin/env python3
"""
WebRTC DataChannel to UDP Relay for RC Control

Receives control commands via WebRTC DataChannel from browser,
forwards them to ESP32 via UDP on local network.

Also provides an admin interface for race management.

Dependencies:
    pip3 install aiortc aiohttp pyserial pynmea2

Usage:
    TOKEN_SECRET="your-secret" python3 control-relay.py
"""

import asyncio
import struct
import socket
import hmac
import hashlib
import time
import logging
import os
import serial
import pynmea2
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ----- Configuration -----

# ESP32 target (discovered via beacon)
ESP32_IP = None
ESP32_PORT = 4210
BEACON_PORT = 4211

# Control relay HTTP port (exposed via Cloudflare Tunnel)
HTTP_PORT = 8890

# GPS configuration
GPS_PORT = '/dev/serial0'
GPS_BAUD = 9600  # Try 38400 if 9600 doesn't work

# Token authentication (must match generate-token.js)
# Set via environment variable: export TOKEN_SECRET="your-secret-key"
TOKEN_SECRET = os.environ.get('TOKEN_SECRET', 'change-me-in-production')

# TURN credentials (loaded from mediamtx config)
TURN_USERNAME = ''
TURN_CREDENTIAL = ''

# Protocol commands
CMD_PING = 0x00
CMD_CTRL = 0x01
CMD_PONG = 0x02
CMD_RACE = 0x03  # Race commands (start countdown, etc.)
CMD_STATUS = 0x04  # Browser -> Pi status updates
CMD_CONFIG = 0x05  # Pi -> Browser config updates (throttle limit, turbo mode, etc.)
CMD_KICK = 0x06    # Pi -> Browser: you have been kicked
CMD_TELEM = 0x07   # Pi -> Clients: telemetry broadcast
CMD_TURBO = 0x08   # Turbo mode toggle (sent to ESP32)

# Race sub-commands (sent as payload after CMD_RACE)
RACE_START_COUNTDOWN = 0x01
RACE_STOP = 0x02

# Status sub-commands (browser -> Pi)
STATUS_VIDEO = 0x01
STATUS_READY = 0x02

# ----- State -----

# UDP socket for sending to ESP32
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp_sock.setblocking(False)

# Active peer connections and data channels
pc = None
control_channel = None  # Primary browser control channel
data_channels = []  # All connected data channels (for telemetry broadcast)
video_connected = False  # Reported by browser
player_ready = False     # Player clicked Ready button

# Current telemetry state (updated by control messages)
current_throttle = 0  # Last received throttle value
current_steering = 0  # Last received steering value
telemetry_task = None  # Asyncio task for telemetry broadcast

# GPS state
gps_lat = 0.0       # Latitude in degrees
gps_lon = 0.0       # Longitude in degrees  
gps_speed = 0.0     # Speed in km/h
gps_heading = 0.0   # Heading/track in degrees
gps_fix = False     # Has valid GPS fix
gps_task = None     # Asyncio task for GPS reading

# Race state: "idle" (controls blocked), "countdown" (controls blocked), "racing" (controls allowed)
race_state = "idle"
race_start_time = None  # Unix timestamp when race started (after countdown)
countdown_task = None  # Asyncio task for countdown timer

# Throttle limit (0.0 to 0.5 max normal, 0.75 turbo - ESP32 enforces hard limit)
throttle_limit = 0.25  # Default 25%
turbo_mode = False     # Turbo mode: increases limits (fwd 75%, back 50%)

# Revoked tokens (persisted to file, keeps last 10)
REVOKED_TOKENS_FILE = '/home/pi/revoked_tokens.txt'
revoked_tokens = []  # List to maintain order
current_player_token = None  # Track current player's token for kick functionality

def load_revoked_tokens():
    """Load revoked tokens from file on startup"""
    global revoked_tokens
    try:
        with open(REVOKED_TOKENS_FILE, 'r') as f:
            revoked_tokens = [line.strip() for line in f if line.strip()]
            logger.info(f"Loaded {len(revoked_tokens)} revoked tokens from file")
    except FileNotFoundError:
        revoked_tokens = []
        logger.info("No revoked tokens file found, starting fresh")
    except Exception as e:
        logger.warning(f"Error loading revoked tokens: {e}")
        revoked_tokens = []

def save_revoked_tokens():
    """Save revoked tokens to file (keep last 10)"""
    try:
        with open(REVOKED_TOKENS_FILE, 'w') as f:
            for token in revoked_tokens[-10:]:
                f.write(token + '\n')
    except Exception as e:
        logger.warning(f"Error saving revoked tokens: {e}")

def revoke_token(token: str):
    """Add token to revoked list and persist"""
    global revoked_tokens
    if token not in revoked_tokens:
        revoked_tokens.append(token)
        # Keep only last 10
        if len(revoked_tokens) > 10:
            revoked_tokens = revoked_tokens[-10:]
        save_revoked_tokens()
        logger.info(f"Revoked token: {token[:8]}... (total: {len(revoked_tokens)})")

# ----- Telemetry Broadcast -----

def broadcast_telemetry():
    """Broadcast telemetry to all connected data channels"""
    global data_channels, race_state, race_start_time, current_throttle, current_steering
    global gps_lat, gps_lon, gps_speed, gps_heading, gps_fix
    
    # Calculate race time in milliseconds
    if race_state == "racing" and race_start_time:
        race_time_ms = int((time.time() - race_start_time) * 1000)
    else:
        race_time_ms = 0
    
    # Scale GPS values for transmission:
    # lat/lon: multiply by 1e7 to preserve 7 decimal places as int32
    # speed: multiply by 100 to preserve 2 decimal places as int16 (max 655.35 km/h)
    # heading: multiply by 100 to preserve 2 decimal places as uint16 (0-360.00)
    lat_scaled = int(gps_lat * 1e7)
    lon_scaled = int(gps_lon * 1e7)
    speed_scaled = int(gps_speed * 100)
    heading_scaled = int(gps_heading * 100)
    
    # Format: seq(2) + cmd(1) + race_time(4) + throttle(2) + steering(2) + 
    #         lat(4) + lon(4) + speed(2) + heading(2) + fix(1) = 24 bytes
    message = struct.pack('<HBIhh iiHHB', 
        0, CMD_TELEM, race_time_ms, current_throttle, current_steering,
        lat_scaled, lon_scaled, speed_scaled, heading_scaled, 1 if gps_fix else 0
    )
    
    # Send to all connected data channels
    for channel in data_channels[:]:  # Copy list to avoid mutation during iteration
        try:
            if channel.readyState == "open":
                channel.send(message)
        except Exception as e:
            logger.warning(f"Error sending telemetry: {e}")


async def gps_reader_loop():
    """Read GPS data from serial port in background"""
    global gps_lat, gps_lon, gps_speed, gps_heading, gps_fix
    
    ser = None
    while True:
        try:
            if ser is None:
                ser = serial.Serial(GPS_PORT, GPS_BAUD, timeout=1)
                logger.info(f"GPS serial port opened: {GPS_PORT} @ {GPS_BAUD}")
            
            # Read line (blocking, but with timeout)
            line = await asyncio.get_event_loop().run_in_executor(None, ser.readline)
            
            if not line:
                continue
                
            try:
                line = line.decode('ascii', errors='ignore').strip()
                if not line.startswith('$'):
                    continue
                    
                msg = pynmea2.parse(line)
                sentence_type = msg.sentence_type  # 'GGA', 'RMC', 'VTG', etc.
                
                # GGA - position fix (handles both $GPGGA and $GNGGA)
                if sentence_type == 'GGA':
                    if msg.latitude and msg.longitude:
                        gps_lat = msg.latitude
                        gps_lon = msg.longitude
                        gps_fix = msg.gps_qual > 0
                
                # RMC - recommended minimum (has speed and heading)
                elif sentence_type == 'RMC':
                    if msg.status == 'A':  # Active/valid
                        gps_fix = True
                        if msg.latitude and msg.longitude:
                            gps_lat = msg.latitude
                            gps_lon = msg.longitude
                        if msg.spd_over_grnd:
                            # Convert knots to km/h
                            gps_speed = msg.spd_over_grnd * 1.852
                        if msg.true_course:
                            gps_heading = msg.true_course
                    else:
                        gps_fix = False
                
                # VTG - track and speed
                elif sentence_type == 'VTG':
                    if hasattr(msg, 'spd_over_grnd_kmph') and msg.spd_over_grnd_kmph:
                        gps_speed = msg.spd_over_grnd_kmph
                    if hasattr(msg, 'true_track') and msg.true_track:
                        gps_heading = msg.true_track
                        
            except pynmea2.ParseError:
                pass  # Ignore malformed sentences
                
        except serial.SerialException as e:
            logger.warning(f"GPS serial error: {e}, retrying in 5s...")
            if ser:
                ser.close()
                ser = None
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"GPS error: {e}")
            await asyncio.sleep(1)

async def telemetry_broadcast_loop():
    """Broadcast telemetry at 10Hz"""
    while True:
        if race_state == "racing":
            broadcast_telemetry()
        await asyncio.sleep(0.1)  # 10Hz

# ----- Token Validation -----

def validate_token(token: str) -> bool:
    """Validate HMAC-SHA256 signed token (same as Cloudflare relay)"""
    if not token or len(token) != 24:
        return False
    
    # Check if token is revoked
    if token in revoked_tokens:
        logger.warning("Token is revoked")
        return False
    
    expiry_hex = token[:8]
    signature = token[8:]
    
    try:
        expiry = int(expiry_hex, 16)
    except ValueError:
        return False
    
    # Check expiry
    if time.time() > expiry:
        logger.warning(f"Token expired: {expiry} < {time.time()}")
        return False
    
    # Verify HMAC signature
    expected = hmac.new(
        TOKEN_SECRET.encode(),
        expiry_hex.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    
    if not hmac.compare_digest(signature, expected):
        logger.warning("Token signature mismatch")
        return False
    
    return True

# ----- TURN Credentials -----

def load_turn_credentials():
    """Load TURN credentials from mediamtx config"""
    global TURN_USERNAME, TURN_CREDENTIAL
    try:
        import yaml
        with open('/home/pi/mediamtx.yml', 'r') as f:
            config = yaml.safe_load(f)
            TURN_USERNAME = config.get('webrtcICEServers', [{}])[0].get('username', '')
            TURN_CREDENTIAL = config.get('webrtcICEServers', [{}])[0].get('password', '')
            if TURN_USERNAME:
                logger.info("Loaded TURN credentials from mediamtx.yml")
    except Exception as e:
        logger.warning(f"Could not load TURN credentials: {e}")

# ----- ESP32 Communication -----

def forward_to_esp32(message: bytes):
    """Forward control message to ESP32 via UDP (message already includes seq from browser)"""
    global ESP32_IP
    
    if ESP32_IP is None:
        logger.warning("ESP32 IP not discovered yet")
        return
    
    try:
        udp_sock.sendto(message, (ESP32_IP, ESP32_PORT))
    except Exception as e:
        logger.error(f"UDP send error: {e}")

async def discover_esp32():
    """Listen for ESP32 beacon broadcasts"""
    global ESP32_IP
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(('', BEACON_PORT))
    sock.setblocking(False)
    
    logger.info(f"Listening for ESP32 beacon on port {BEACON_PORT}")
    
    loop = asyncio.get_event_loop()
    
    while True:
        try:
            data, addr = await loop.sock_recvfrom(sock, 1024)
            if data == b'ARRMA':
                new_ip = addr[0]
                if ESP32_IP != new_ip:
                    ESP32_IP = new_ip
                    logger.info(f"Discovered ESP32 at {ESP32_IP}")
        except BlockingIOError:
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Beacon error: {e}")
            await asyncio.sleep(1)

# ----- WebRTC Signaling -----

async def handle_offer(request):
    """Handle WebRTC signaling (WHIP-like POST with SDP offer)"""
    global pc, control_channel, current_player_token
    
    # Validate token
    token = request.query.get('token', '')
    if not validate_token(token):
        logger.warning(f"Invalid token attempt")
        return web.Response(status=401, text='Invalid or expired token')
    
    logger.info("Token validated, processing WebRTC offer")
    current_player_token = token  # Track for kick functionality
    
    # Close existing connection
    if pc:
        logger.info("Closing existing peer connection")
        await pc.close()
        pc = None
        control_channel = None
    
    # Configure ICE servers
    ice_servers = []
    
    # Add Cloudflare TURN if credentials available
    if TURN_USERNAME and TURN_CREDENTIAL:
        ice_servers.append(RTCIceServer(
            urls=["turn:turn.cloudflare.com:3478?transport=udp"],
            username=TURN_USERNAME,
            credential=TURN_CREDENTIAL
        ))
        logger.info("Using Cloudflare TURN")
    
    # Add STUN fallback
    ice_servers.append(RTCIceServer(urls=["stun:stun.l.google.com:19302"]))
    
    config = RTCConfiguration(iceServers=ice_servers)
    pc = RTCPeerConnection(configuration=config)
    
    @pc.on("datachannel")
    def on_datachannel(channel):
        global control_channel, data_channels
        control_channel = channel
        data_channels.append(channel)  # Track for telemetry broadcast
        logger.info(f"DataChannel '{channel.label}' opened (total: {len(data_channels)})")
        
        # Send current config to new client
        send_config()
        
        # Send current race state (for reconnection during race)
        send_race_state()
        
        ctrl_count = [0]  # Use list to allow mutation in nested function
        
        @channel.on("message")
        def on_message(message):
            global race_state, current_throttle, current_steering
            # New packet format: seq(2) + cmd(1) + payload
            if isinstance(message, bytes) and len(message) >= 3:
                seq = struct.unpack('<H', message[0:2])[0]
                cmd = message[2]
                
                if cmd == CMD_PING:  # PING - echo back as PONG
                    pong = message[0:2] + bytes([CMD_PONG]) + message[3:]  # Keep seq, change cmd to PONG
                    channel.send(pong)
                elif cmd == CMD_CTRL:  # CTRL - forward to ESP32 only if racing
                    # Update telemetry state (throttle/steering)
                    if len(message) >= 7:
                        current_throttle, current_steering = struct.unpack('<hh', message[3:7])
                    
                    if race_state == "racing":
                        ctrl_count[0] += 1
                        forward_to_esp32(message)
                    # else: silently drop control commands (race not active)
                elif cmd == CMD_STATUS:  # STATUS - browser reporting state
                    global video_connected, player_ready
                    if len(message) >= 5:
                        sub_cmd = message[3]
                        value = message[4] == 1
                        if sub_cmd == STATUS_VIDEO:
                            video_connected = value
                            logger.info(f"Video status: {'connected' if video_connected else 'disconnected'}")
                        elif sub_cmd == STATUS_READY:
                            player_ready = value
                            logger.info(f"Player ready: {player_ready}")
                elif cmd == CMD_TURBO:  # TURBO - player toggling turbo mode
                    global turbo_mode, throttle_limit
                    if len(message) >= 4:
                        new_turbo = message[3] == 1
                        turbo_mode = new_turbo
                        logger.info(f"Turbo mode set by player: {turbo_mode}")
                        
                        # If turning off turbo and throttle exceeds normal limit, clamp it
                        if not turbo_mode and throttle_limit > 0.5:
                            throttle_limit = 0.5
                            logger.info(f"Throttle limit clamped to {throttle_limit} (turbo off)")
                        
                        # Forward to ESP32
                        send_turbo_to_esp32()
                        
                        # Send updated config back to confirm
                        send_config()
        
        @channel.on("close")
        def on_close():
            global control_channel, data_channels
            if channel in data_channels:
                data_channels.remove(channel)
            if control_channel == channel:
                control_channel = None
            logger.info(f"DataChannel '{channel.label}' closed (remaining: {len(data_channels)})")
            logger.info(f"DataChannel '{channel.label}' closed")
    
    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        global pc, control_channel, video_connected, player_ready
        logger.info(f"Connection state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            logger.info("Connection lost, cleaning up")
            control_channel = None
            video_connected = False
            player_ready = False
            if pc.connectionState != "closed":
                await pc.close()
            pc = None
    
    # Parse offer from browser
    offer_sdp = await request.text()
    offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
    await pc.setRemoteDescription(offer)
    
    # Create answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    
    # Wait for ICE gathering to complete
    logger.info("Waiting for ICE gathering...")
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)
    logger.info("ICE gathering complete")
    
    return web.Response(
        text=pc.localDescription.sdp,
        content_type="application/sdp",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    )

async def handle_options(request):
    """Handle CORS preflight requests"""
    return web.Response(
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    )

# ----- Telemetry Subscriber Endpoint -----

# Track telemetry subscriber connections (separate from main control)
telemetry_subscribers = []  # List of (pc, datachannel) tuples

async def handle_telemetry_offer(request):
    """Handle WebRTC signaling for telemetry subscribers (read-only, doesn't kick browser)"""
    global telemetry_subscribers
    
    # Validate token
    token = request.query.get('token', '')
    if not validate_token(token):
        logger.warning(f"Invalid token attempt for telemetry")
        return web.Response(status=401, text='Invalid or expired token')
    
    logger.info("Telemetry subscriber connecting...")
    
    # Configure ICE servers (same as main connection)
    ice_servers = []
    if TURN_USERNAME and TURN_CREDENTIAL:
        ice_servers.append(RTCIceServer(
            urls=["turn:turn.cloudflare.com:3478?transport=udp"],
            username=TURN_USERNAME,
            credential=TURN_CREDENTIAL
        ))
    ice_servers.append(RTCIceServer(urls=["stun:stun.l.google.com:19302"]))
    
    config = RTCConfiguration(iceServers=ice_servers)
    sub_pc = RTCPeerConnection(configuration=config)
    sub_channel = None
    
    @sub_pc.on("datachannel")
    def on_datachannel(channel):
        nonlocal sub_channel
        global data_channels
        sub_channel = channel
        data_channels.append(channel)  # Add to broadcast list
        logger.info(f"Telemetry subscriber DataChannel '{channel.label}' opened (total subscribers: {len(data_channels)})")
        
        @channel.on("close")
        def on_close():
            global data_channels
            if channel in data_channels:
                data_channels.remove(channel)
            logger.info(f"Telemetry subscriber DataChannel closed (remaining: {len(data_channels)})")
    
    @sub_pc.on("connectionstatechange")
    async def on_connectionstatechange():
        nonlocal sub_channel
        logger.info(f"Telemetry subscriber connection state: {sub_pc.connectionState}")
        if sub_pc.connectionState in ("failed", "closed", "disconnected"):
            # Clean up this subscriber
            if sub_channel and sub_channel in data_channels:
                data_channels.remove(sub_channel)
            # Remove from subscribers list
            telemetry_subscribers[:] = [(p, c) for p, c in telemetry_subscribers if p != sub_pc]
            if sub_pc.connectionState != "closed":
                await sub_pc.close()
    
    # Parse offer
    offer_sdp = await request.text()
    offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
    await sub_pc.setRemoteDescription(offer)
    
    # Create answer
    answer = await sub_pc.createAnswer()
    await sub_pc.setLocalDescription(answer)
    
    # Wait for ICE gathering
    logger.info("Telemetry subscriber: waiting for ICE gathering...")
    while sub_pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)
    logger.info("Telemetry subscriber: ICE gathering complete")
    
    # Track this subscriber
    telemetry_subscribers.append((sub_pc, sub_channel))
    
    return web.Response(
        text=sub_pc.localDescription.sdp,
        content_type="application/sdp",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    )

# ----- Health Check -----

async def handle_health(request):
    """Health check endpoint"""
    return web.json_response(
        {
            "status": "ok",
            "esp32_ip": ESP32_IP,
            "connected": pc is not None and pc.connectionState == "connected",
            "channel_open": control_channel is not None and control_channel.readyState == "open",
            "video_connected": video_connected,
            "player_ready": player_ready,
            "throttle_limit": throttle_limit,
            "turbo_mode": turbo_mode,
            "gps": {
                "fix": gps_fix,
                "lat": gps_lat,
                "lon": gps_lon,
                "speed_kmh": round(gps_speed, 2),
                "heading": round(gps_heading, 1)
            }
        },
        headers={"Access-Control-Allow-Origin": "*"}
    )

# ----- Admin Interface -----

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

def send_race_command(sub_cmd: int, payload: bytes = b''):
    """Send a race command to the connected browser client"""
    global control_channel
    
    if control_channel is None or control_channel.readyState != "open":
        logger.warning("Cannot send race command: no active DataChannel")
        return False
    
    # Format: seq(2) + cmd(1) + sub_cmd(1) + payload
    # Use seq=0 for server-initiated messages
    message = struct.pack('<HBB', 0, CMD_RACE, sub_cmd) + payload
    control_channel.send(message)
    logger.info(f"Sent race command: sub_cmd={sub_cmd}")
    return True

def send_config():
    """Send current config (throttle limit, turbo mode) to browser"""
    global control_channel, throttle_limit, turbo_mode
    
    if control_channel is None or control_channel.readyState != "open":
        return False
    
    # Format: seq(2) + cmd(1) + throttle_limit(2 as int16 scaled) + turbo(1)
    # Scale 0.0-1.0 to 0-32767
    thr_scaled = int(throttle_limit * 32767)
    message = struct.pack('<HBhB', 0, CMD_CONFIG, thr_scaled, 1 if turbo_mode else 0)
    control_channel.send(message)
    logger.info(f"Sent config: throttle_limit={throttle_limit}, turbo={turbo_mode}")
    return True

def send_turbo_to_esp32():
    """Send turbo mode command to ESP32 via UDP"""
    global ESP32_IP, turbo_mode
    
    if ESP32_IP is None:
        logger.warning("Cannot send turbo to ESP32: IP not discovered")
        return False
    
    # Format: seq(2) + cmd(1) + turbo(1)
    message = struct.pack('<HBB', 0, CMD_TURBO, 1 if turbo_mode else 0)
    try:
        udp_sock.sendto(message, (ESP32_IP, ESP32_PORT))
        logger.info(f"Sent turbo mode to ESP32: {turbo_mode}")
        return True
    except Exception as e:
        logger.error(f"Failed to send turbo to ESP32: {e}")
        return False

def send_race_state():
    """Send current race state to browser (for reconnection)"""
    global control_channel, race_state
    
    if control_channel is None or control_channel.readyState != "open":
        return False
    
    # If race is in progress, tell the browser
    if race_state == "racing":
        # Send RACE_START_COUNTDOWN followed immediately by implicit "racing" 
        # Actually, let's add a new sub-command for "already racing"
        RACE_RESUME = 0x03  # New: resume into racing state immediately
        message = struct.pack('<HBB', 0, CMD_RACE, RACE_RESUME)
        control_channel.send(message)
        logger.info("Sent race resume command")
        return True
    elif race_state == "countdown":
        send_race_command(RACE_START_COUNTDOWN)
        return True
    
    return False

async def countdown_to_racing():
    """Wait 3 seconds then enable controls"""
    global race_state, race_start_time
    await asyncio.sleep(3.0)
    race_state = "racing"
    race_start_time = time.time()
    logger.info("Race started - controls enabled")

async def handle_start_race(request):
    """Admin endpoint to start race countdown"""
    global race_state, countdown_task
    
    if race_state != "idle":
        return web.json_response({"success": False, "error": "Race already in progress"}, status=400, headers=CORS_HEADERS)
    
    if send_race_command(RACE_START_COUNTDOWN):
        race_state = "countdown"
        logger.info("Race countdown started - controls disabled")
        
        # Schedule transition to racing state after 3 seconds
        countdown_task = asyncio.create_task(countdown_to_racing())
        return web.json_response({"success": True}, headers=CORS_HEADERS)
    else:
        return web.json_response({"success": False, "error": "No player connected"}, status=400, headers=CORS_HEADERS)

async def handle_stop_race(request):
    """Admin endpoint to stop race"""
    global race_state, race_start_time, countdown_task
    
    # Cancel countdown if in progress
    if countdown_task and not countdown_task.done():
        countdown_task.cancel()
        countdown_task = None
    
    race_state = "idle"
    race_start_time = None
    logger.info("Race stopped - controls disabled")
    
    send_race_command(RACE_STOP)
    return web.json_response({"success": True}, headers=CORS_HEADERS)

def send_kick_command():
    """Send kick notification to browser before disconnecting"""
    global control_channel
    
    if control_channel is None or control_channel.readyState != "open":
        return False
    
    # Format: seq(2) + cmd(1) = 3 bytes
    message = struct.pack('<HB', 0, CMD_KICK)
    control_channel.send(message)
    logger.info("Sent kick command to browser")
    return True

async def handle_kick_player(request):
    """Admin endpoint to kick player and revoke their token"""
    global pc, control_channel, current_player_token, race_state, countdown_task
    
    if not pc or not control_channel:
        return web.json_response({"success": False, "error": "No player connected"}, status=400, headers=CORS_HEADERS)
    
    # Revoke the token so they can't reconnect with it
    if current_player_token:
        revoke_token(current_player_token)
        current_player_token = None
    
    # Stop any active race
    if countdown_task and not countdown_task.done():
        countdown_task.cancel()
        countdown_task = None
    race_state = "idle"
    
    # Send kick command to browser first (so it can stop video and show message)
    send_kick_command()
    
    # Give browser a moment to receive the kick command
    await asyncio.sleep(0.1)
    
    # Close the connection
    if pc:
        await pc.close()
        pc = None
        control_channel = None
    
    logger.info("Player kicked and token revoked")
    return web.json_response({"success": True}, headers=CORS_HEADERS)

async def handle_set_throttle(request):
    """Admin endpoint to set throttle limit"""
    global throttle_limit, turbo_mode
    
    try:
        body = await request.json()
        new_limit = float(body.get('limit', 0.25))
        # Clamp to valid range based on turbo mode
        # Normal: max 0.5, Turbo: max 0.75 (ESP32 enforces hard limits)
        max_limit = 0.75 if turbo_mode else 0.5
        throttle_limit = max(0.1, min(max_limit, new_limit))
        logger.info(f"Throttle limit set to {throttle_limit}")
        
        # Send to connected browser immediately
        send_config()
        
        return web.json_response({"success": True, "throttle_limit": throttle_limit}, headers=CORS_HEADERS)
    except Exception as e:
        logger.error(f"Error setting throttle: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=400, headers=CORS_HEADERS)

async def handle_set_turbo(request):
    """Admin endpoint to toggle turbo mode"""
    global turbo_mode, throttle_limit
    
    try:
        body = await request.json()
        new_turbo = bool(body.get('enabled', False))
        turbo_mode = new_turbo
        logger.info(f"Turbo mode set to {turbo_mode}")
        
        # If turning off turbo and throttle exceeds normal limit, clamp it
        if not turbo_mode and throttle_limit > 0.5:
            throttle_limit = 0.5
            logger.info(f"Throttle limit clamped to {throttle_limit} (turbo off)")
        
        # Send turbo mode to ESP32
        send_turbo_to_esp32()
        
        # Send updated config to browser
        send_config()
        
        return web.json_response({"success": True, "turbo_mode": turbo_mode, "throttle_limit": throttle_limit}, headers=CORS_HEADERS)
    except Exception as e:
        logger.error(f"Error setting turbo: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=400, headers=CORS_HEADERS)

# ----- Main -----

async def main():
    global telemetry_task, gps_task
    
    # Load revoked tokens from file
    load_revoked_tokens()
    
    # Load TURN credentials from mediamtx config
    load_turn_credentials()
    
    # Start ESP32 beacon discovery
    asyncio.create_task(discover_esp32())
    
    # Start GPS reader loop
    gps_task = asyncio.create_task(gps_reader_loop())
    
    # Start telemetry broadcast loop (10Hz)
    telemetry_task = asyncio.create_task(telemetry_broadcast_loop())
    
    # Set up HTTP server for WebRTC signaling
    app = web.Application()
    app.router.add_post("/control/offer", handle_offer)
    app.router.add_options("/control/offer", handle_options)
    app.router.add_get("/control/health", handle_health)
    
    # Telemetry subscriber endpoint (for restreamer, doesn't kick browser)
    app.router.add_post("/telemetry/offer", handle_telemetry_offer)
    app.router.add_options("/telemetry/offer", handle_options)
    
    # Admin API routes (page served from Cloudflare)
    app.router.add_post("/admin/start-race", handle_start_race)
    app.router.add_options("/admin/start-race", handle_options)
    app.router.add_post("/admin/stop-race", handle_stop_race)
    app.router.add_options("/admin/stop-race", handle_options)
    app.router.add_post("/admin/kick-player", handle_kick_player)
    app.router.add_options("/admin/kick-player", handle_options)
    app.router.add_post("/admin/set-throttle", handle_set_throttle)
    app.router.add_options("/admin/set-throttle", handle_options)
    app.router.add_post("/admin/set-turbo", handle_set_turbo)
    app.router.add_options("/admin/set-turbo", handle_options)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    
    logger.info(f"Control relay listening on port {HTTP_PORT}")
    logger.info(f"Endpoints:")
    logger.info(f"  POST /control/offer?token=...   - WebRTC signaling (browser)")
    logger.info(f"  POST /telemetry/offer?token=... - Telemetry subscriber (restreamer)")
    logger.info(f"  GET  /control/health            - Health check")
    logger.info(f"  POST /admin/start-race          - Start race countdown")
    logger.info(f"  POST /admin/stop-race           - Stop race")
    logger.info(f"  POST /admin/kick-player         - Kick player & revoke token")
    
    # Keep running
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")

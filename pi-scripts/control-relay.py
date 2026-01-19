#!/usr/bin/env python3
"""
WebRTC DataChannel to UDP Relay for RC Control

Receives control commands via WebRTC DataChannel from browser,
forwards them to ESP32 via UDP on local network.

Also provides an admin interface for race management.

Dependencies:
    pip3 install aiortc aiohttp

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

# Race sub-commands (sent as payload after CMD_RACE)
RACE_START_COUNTDOWN = 0x01
RACE_STOP = 0x02

# ----- State -----

# UDP socket for sending to ESP32
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp_sock.setblocking(False)

# Active peer connection
pc = None
control_channel = None

# Race state
race_start_time = None  # Unix timestamp when race started (after countdown)

# ----- Token Validation -----

def validate_token(token: str) -> bool:
    """Validate HMAC-SHA256 signed token (same as Cloudflare relay)"""
    if not token or len(token) != 24:
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
    global pc, control_channel
    
    # Validate token
    token = request.query.get('token', '')
    if not validate_token(token):
        logger.warning(f"Invalid token attempt")
        return web.Response(status=401, text='Invalid or expired token')
    
    logger.info("Token validated, processing WebRTC offer")
    
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
        global control_channel
        control_channel = channel
        logger.info(f"DataChannel '{channel.label}' opened")
        
        ctrl_count = [0]  # Use list to allow mutation in nested function
        
        @channel.on("message")
        def on_message(message):
            # New packet format: seq(2) + cmd(1) + payload
            if isinstance(message, bytes) and len(message) >= 3:
                seq = struct.unpack('<H', message[0:2])[0]
                cmd = message[2]
                
                if cmd == CMD_PING:  # PING - echo back as PONG
                    pong = message[0:2] + bytes([CMD_PONG]) + message[3:]  # Keep seq, change cmd to PONG
                    channel.send(pong)
                elif cmd == CMD_CTRL:  # CTRL - forward to ESP32
                    ctrl_count[0] += 1
                    forward_to_esp32(message)
        
        @channel.on("close")
        def on_close():
            global control_channel
            control_channel = None
            logger.info(f"DataChannel '{channel.label}' closed")
    
    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info(f"Connection state: {pc.connectionState}")
        if pc.connectionState == "failed":
            await pc.close()
    
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

async def handle_health(request):
    """Health check endpoint"""
    return web.json_response(
        {
            "status": "ok",
            "esp32_ip": ESP32_IP,
            "connected": pc is not None and pc.connectionState == "connected",
            "channel_open": control_channel is not None and control_channel.readyState == "open"
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

async def handle_start_race(request):
    """Admin endpoint to start race countdown"""
    global race_start_time
    
    if send_race_command(RACE_START_COUNTDOWN):
        # Record when race will start (after 3 second countdown)
        race_start_time = time.time() + 3.0
        return web.json_response({"success": True, "race_start_time": race_start_time}, headers=CORS_HEADERS)
    else:
        return web.json_response({"success": False, "error": "No player connected"}, status=400, headers=CORS_HEADERS)

async def handle_stop_race(request):
    """Admin endpoint to stop race"""
    global race_start_time
    
    send_race_command(RACE_STOP)
    race_start_time = None
    return web.json_response({"success": True}, headers=CORS_HEADERS)

# ----- Main -----

async def main():
    # Load TURN credentials from mediamtx config
    load_turn_credentials()
    
    # Start ESP32 beacon discovery
    asyncio.create_task(discover_esp32())
    
    # Set up HTTP server for WebRTC signaling
    app = web.Application()
    app.router.add_post("/control/offer", handle_offer)
    app.router.add_options("/control/offer", handle_options)
    app.router.add_get("/control/health", handle_health)
    
    # Admin API routes (page served from Cloudflare)
    app.router.add_post("/admin/start-race", handle_start_race)
    app.router.add_options("/admin/start-race", handle_options)
    app.router.add_post("/admin/stop-race", handle_stop_race)
    app.router.add_options("/admin/stop-race", handle_options)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    
    logger.info(f"Control relay listening on port {HTTP_PORT}")
    logger.info(f"Endpoints:")
    logger.info(f"  POST /control/offer?token=... - WebRTC signaling")
    logger.info(f"  GET  /control/health          - Health check")
    logger.info(f"  POST /admin/start-race        - Start race countdown")
    logger.info(f"  POST /admin/stop-race         - Stop race")
    
    # Keep running
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")

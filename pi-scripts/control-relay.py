#!/usr/bin/env python3
"""
WebRTC DataChannel to UDP Relay for RC Control

Receives control commands via WebRTC DataChannel from browser,
forwards them to ESP32 via UDP on local network.

Also provides an admin interface for race management.

Configuration:
    Tuning parameters loaded from car profile (pi-scripts/profiles/*.ini)
    Set CAR_PROFILE environment variable to select profile:
        export CAR_PROFILE=badlands_4kg
    
Dependencies:
    pip3 install aiortc aiohttp pyserial pynmea2

Usage:
    CAR_PROFILE=badlands_4kg TOKEN_SECRET="your-secret" python3 control-relay.py
"""

import asyncio
import struct
import socket
import hmac
import hashlib
import time
import logging
import os
import math
import re
import json
import subprocess
import serial
import pynmea2
import RPi.GPIO as GPIO
from aiohttp import web, ClientSession
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from bno055_reader import BNO055
from hall_rpm import HallRPM
from car_config import get_config
from low_speed_traction import LowSpeedTractionManager
from yaw_rate_controller import YawRateController
from slip_angle_watchdog import SlipAngleWatchdog
from steering_shaper import SteeringShaper
from abs_controller import ABSController, ThrottleStateTracker
from hill_hold import HillHold
from coast_control import CoastControl
from surface_adaptation import SurfaceAdaptation
from direction_estimator import DirectionEstimator
from stun_client import discover_endpoint

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ----- MediaMTX Recording Control -----

MEDIAMTX_API_URL = "http://127.0.0.1:9997"
recording_active = False

async def start_recording():
    """Start MediaMTX recording via REST API."""
    global recording_active
    
    try:
        async with ClientSession() as session:
            async with session.patch(
                f"{MEDIAMTX_API_URL}/v3/config/paths/patch/cam",
                json={"record": True},
                timeout=5
            ) as resp:
                if resp.status == 200:
                    recording_active = True
                    logger.info("Recording started")
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Failed to start recording: {resp.status} - {error}")
                    return False
    except Exception as e:
        logger.error(f"Error starting recording: {e}")
        return False

async def stop_recording():
    """Stop MediaMTX recording via REST API."""
    global recording_active
    
    if not recording_active:
        return True
    
    try:
        async with ClientSession() as session:
            async with session.patch(
                f"{MEDIAMTX_API_URL}/v3/config/paths/patch/cam",
                json={"record": False},
                timeout=5
            ) as resp:
                if resp.status == 200:
                    recording_active = False
                    logger.info("Recording stopped")
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Failed to stop recording: {resp.status} - {error}")
                    return False
    except Exception as e:
        logger.error(f"Error stopping recording: {e}")
        return False

# ----- Telemetry File Logging -----

telemetry_log_file = None
telemetry_log_path = None

def start_telemetry_log():
    """Start logging telemetry to JSON lines file alongside video recording."""
    global telemetry_log_file, telemetry_log_path
    
    if telemetry_log_file:
        return  # Already logging
    
    # Create filename with timestamp matching MediaMTX recording naming
    timestamp = time.strftime('%Y-%m-%d_%H-%M-%S')
    telemetry_log_path = f"/home/pi/recordings/telemetry_{timestamp}.jsonl"
    
    try:
        telemetry_log_file = open(telemetry_log_path, 'w')
        logger.info(f"Telemetry logging started: {telemetry_log_path}")
    except Exception as e:
        logger.error(f"Failed to start telemetry logging: {e}")
        telemetry_log_file = None
        telemetry_log_path = None

def stop_telemetry_log():
    """Stop telemetry file logging."""
    global telemetry_log_file, telemetry_log_path
    
    if telemetry_log_file:
        try:
            telemetry_log_file.close()
            logger.info(f"Telemetry logging stopped: {telemetry_log_path}")
        except Exception as e:
            logger.error(f"Error closing telemetry log: {e}")
        telemetry_log_file = None
        telemetry_log_path = None

def log_telemetry_frame():
    """Write current telemetry frame to log file (called at 10Hz)."""
    global telemetry_log_file
    global race_state, race_start_time, current_throttle, current_steering
    global gps_lat, gps_lon, gps_speed, gps_heading, gps_fix
    global imu_heading, imu_yaw_rate, imu_lateral_accel, blended_heading
    global fused_speed, wheel_speed, wheel_distance
    global traction_ctrl, stability_ctrl, abs_ctrl
    global traction_enabled, stability_enabled, abs_enabled
    
    if not telemetry_log_file:
        return
    
    # Calculate race time
    if race_state == "racing" and race_start_time:
        race_time_ms = int((time.time() - race_start_time) * 1000)
    else:
        race_time_ms = 0
    
    # Build telemetry frame
    frame = {
        "t": race_time_ms,  # Race time in ms
        "ts": time.time(),  # Unix timestamp for syncing with video
        "throttle": current_throttle,
        "steering": current_steering,
        "gps": {
            "lat": gps_lat,
            "lon": gps_lon,
            "speed": gps_speed,
            "heading": gps_heading,
            "fix": gps_fix
        },
        "imu": {
            "heading": blended_heading,
            "yaw_rate": imu_yaw_rate,
            "lateral_accel": imu_lateral_accel
        },
        "speed": {
            "fused": fused_speed,
            "wheel": wheel_speed,
            "gps": gps_speed
        },
        "wheel_distance": wheel_distance
    }
    
    # Add controller states if available
    if traction_ctrl and traction_enabled:
        status = traction_ctrl.get_status()
        frame["traction"] = {
            "slip_detected": status['slip_detected'],
            "throttle_mult": status['throttle_multiplier']
        }
    
    if stability_ctrl and stability_enabled:
        frame["stability"] = {
            "intervention": stability_ctrl.intervention_type,
            "yaw_error": stability_ctrl.yaw_error
        }
    
    if abs_ctrl and abs_enabled:
        status = abs_ctrl.get_status()
        frame["abs"] = {
            "active": status['active'],
            "phase": status['phase']
        }
    
    try:
        telemetry_log_file.write(json.dumps(frame) + '\n')
        # Flush periodically to ensure data is written (every frame for safety)
        telemetry_log_file.flush()
    except Exception as e:
        logger.warning(f"Error writing telemetry log: {e}")

# ----- Configuration -----

# ESP32 target (discovered via beacon)
ESP32_IP = None
ESP32_PORT = 4210
BEACON_PORT = 4211

# Pi WiFi signal monitoring
PI_WIFI_RSSI = 0  # WiFi signal strength from Pi's interface (dBm, -100 to 0)
PI_WIFI_LQ = 0    # WiFi link quality (0-100%)
PI_WIFI_INTERFACE = None  # Detected wireless interface name (e.g., 'wlan0')

# Control relay HTTP port (exposed via Cloudflare Tunnel)
HTTP_PORT = 8890

# GPS configuration
GPS_PORT = '/dev/serial0'
GPS_BAUD = 9600  # Try 38400 if 9600 doesn't work

# Token authentication (must match generate-token.js)
# Set via environment variable: export TOKEN_SECRET="your-secret-key"
TOKEN_SECRET = os.environ.get('TOKEN_SECRET', 'change-me-in-production')

# Admin password for /admin/* endpoints (must match ADMIN_PASSWORD in Cloudflare Worker)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')

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
CMD_TRACTION = 0x09 # Traction control toggle (browser -> Pi)
CMD_STABILITY = 0x0A # Stability control toggle (browser -> Pi)
CMD_DEBUG_TELEM = 0x0B # Pi -> Clients: debug telemetry (stability systems)
CMD_HEADLIGHT = 0x0C # Headlight toggle (browser -> Pi)
CMD_EXTENDED_TELEM = 0x0D # Pi -> Clients: extended controller telemetry (ABS, Hill Hold, etc.)
CMD_ABS = 0x0E # ABS toggle (browser -> Pi)
CMD_HILL_HOLD = 0x0F # Hill hold toggle (browser -> Pi)
CMD_COAST = 0x10 # Coast control toggle (browser -> Pi)
CMD_SURFACE_ADAPT = 0x11 # Surface adaptation toggle (browser -> Pi)

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

def is_connected():
    """Check if any client is connected via data channel"""
    return len(data_channels) > 0
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

# IMU (BNO055) state
imu_heading = 0.0        # BNO055 fused heading (degrees, 0=North)
imu_yaw_rate = 0.0       # Gyro Z rotation rate (deg/sec)
imu_forward_accel = 0.0  # Linear acceleration forward (m/s², gravity-free)
imu_lateral_accel = 0.0  # Linear acceleration lateral (m/s², positive = right)
imu_pitch = 0.0          # Pitch angle (degrees, positive = nose up)
imu_calibration = {'sys': 0, 'gyr': 0, 'acc': 0, 'mag': 0}
imu_valid = False        # BNO055 connected and reading
blended_heading = 0.0    # Final heading (blended IMU + GPS)
imu_task = None          # Asyncio task for IMU reading

# Traction control (unified low-speed traction manager)
traction_ctrl = None     # LowSpeedTractionManager instance
traction_enabled = False # Admin toggle for traction control

# Yaw-rate stability control
stability_ctrl = None    # YawRateController instance
stability_enabled = False # Admin toggle for stability control

# Slip angle watchdog (shares enable state with stability control)
slip_watchdog = None     # SlipAngleWatchdog instance

# Steering shaper (latency-aware steering, shares enable with stability)
steering_shaper = None   # SteeringShaper instance

# New vehicle dynamics controllers
abs_ctrl = None          # ABSController instance
throttle_tracker = None  # ThrottleStateTracker for ESC state machine
hill_hold_ctrl = None    # HillHold instance
coast_ctrl = None        # CoastControl instance
surface_adapt = None     # SurfaceAdaptation instance
direction_est = None     # DirectionEstimator instance

# Direction estimation state
signed_speed = 0.0       # Signed speed from direction estimator (km/h)

# New controller enable flags
abs_enabled = False          # ABS toggle
hill_hold_enabled = False    # Hill hold toggle
coast_enabled = False        # Coast control toggle
surface_adapt_enabled = False # Surface adaptation toggle

# ----- UDP Hole Punch State -----

# UDP socket for hole punching (receives RTP from client, forwards to them)
hole_punch_sock = None
hole_punch_port = 5004  # Local port for hole punching

# Public endpoint (discovered via STUN)
public_ip = None
public_port = None
nat_type_symmetric = False

# Active hole punch sessions: {(client_ip, client_port): last_activity_time}
active_punch_clients = {}

# Cached SPS/PPS from RTSP DESCRIBE (base64 encoded, comma-separated)
cached_sprop_parameter_sets = ""

# STUN refresh interval (NAT mappings typically timeout in 30-60s)
STUN_REFRESH_INTERVAL = 20  # seconds
stun_refresh_task = None

# RTP forwarding state
rtp_forwarder = None  # RTSPForwarder instance (legacy, kept for fallback)
fec_sender_process = None  # Native FEC sender subprocess
fec_sender_target = None  # (client_ip, client_port) the FEC sender is targeting
USE_FEC_SENDER = True  # Use native FEC sender instead of RTSP forwarder
FEC_SENDER_PATH = "/home/pi/rtp-fec-sender/build/rtp-fec-sender"
MEDIAMTX_RTSP_URL = "rtsp://127.0.0.1:8554/cam"
RTP_LOCAL_PORT = 5006  # Local port for receiving RTP from MediaMTX

# Load car configuration
_cfg = get_config()

# Heading blend parameters (from config)
SPEED_THRESHOLD_LOW = _cfg.get_float('heading_blend', 'imu_only_speed_kmh')
SPEED_THRESHOLD_HIGH = _cfg.get_float('heading_blend', 'gps_blend_speed_kmh')
HEADING_SMOOTHING = _cfg.get_float('heading_blend', 'heading_smooth_alpha')

# Hall sensor (wheel RPM) state
HALL_GPIO_PIN = 22           # BCM GPIO pin for Hall sensor
WHEEL_DIAMETER_MM = _cfg.get_int('vehicle', 'wheel_diameter_mm')

# Headlight control
HEADLIGHT_GPIO_PIN = 26      # BCM GPIO pin for headlight MOSFET (IRLZ44N)
WHEEL_CIRCUMFERENCE = (WHEEL_DIAMETER_MM * 3.14159) / 1000  # Wheel circumference in meters
hall_sensor = None           # HallRPM instance
wheel_rpm = 0.0              # Current wheel RPM
wheel_speed = 0.0            # Speed from wheel (km/h)
fused_speed = 0.0            # Final fused speed (km/h)
wheel_distance = 0.0         # Total distance from wheel (meters)
race_start_pulse_count = 0   # Pulse count at race start (for distance reset)

# Speed fusion parameters (from config)
SPEED_FUSION_ALPHA = _cfg.get_float('speed_fusion', 'fusion_alpha')
IMU_SPEED_INTEGRATE_RATE = _cfg.get_float('speed_fusion', 'imu_integrate_rate')
GPS_DRIFT_CORRECTION_ALPHA = _cfg.get_float('speed_fusion', 'gps_drift_correction_alpha')
GPS_DRIFT_CORRECTION_MIN_SPEED = _cfg.get_float('speed_fusion', 'gps_drift_correction_min_speed_kmh')
WHEELSPIN_DETECT_RATIO = _cfg.get_float('speed_fusion', 'wheelspin_detect_ratio')
WHEELSPIN_DETECT_TIME = _cfg.get_float('speed_fusion', 'wheelspin_detect_time_s')
WHEELSPIN_MAX_FUSED_RATIO = _cfg.get_float('speed_fusion', 'wheelspin_max_fused_ratio')

# Speed fusion state
imu_integrated_speed = 0.0   # Speed from IMU forward accel integration (km/h)
last_speed_fusion_time = 0.0
wheelspin_start_time = 0.0   # When wheelspin was first suspected
wheelspin_active = False     # Currently detecting wheelspin

# Stationary decay (prevent IMU drift when stopped)
STATIONARY_TIMEOUT = _cfg.get_float('speed_fusion', 'stationary_timeout_s')
STATIONARY_DECAY_RATE = _cfg.get_float('speed_fusion', 'stationary_decay_rate')
IMU_ACCEL_NOISE_THRESHOLD = _cfg.get_float('speed_fusion', 'imu_accel_noise_threshold')
wheel_stopped_since = 0.0    # Timestamp when wheel stopped

# IMU mount offset (from config)
IMU_MOUNT_OFFSET = _cfg.get_float('heading_blend', 'imu_mount_offset_deg')

# Race state: "idle" (controls blocked), "countdown" (controls blocked), "racing" (controls allowed)
race_state = "idle"
race_start_time = None  # Unix timestamp when race started (after countdown)
countdown_task = None  # Asyncio task for countdown timer

turbo_mode = False     # Turbo mode: increases limits (ESP32 enforces hard limits)
headlight_on = False   # Headlight state (controlled via GPIO 26)

# ----- WiFi Signal Monitoring -----

def get_wireless_interface():
    """Detect the active wireless interface (the one with a connection/ESSID).
    With multiple adapters, we need to find the one actually connected.
    Returns interface name (e.g., 'wlan0') or None if not found.
    """
    # Get all wireless interfaces from /proc/net/wireless
    interfaces = []
    try:
        with open('/proc/net/wireless', 'r') as f:
            lines = f.readlines()
            # Skip header lines (first 2 lines)
            for line in lines[2:]:
                # Format: "wlan0: 0000  ..."
                parts = line.strip().split(':')
                if len(parts) >= 1:
                    interface = parts[0].strip()
                    if interface:
                        interfaces.append(interface)
    except Exception as e:
        logger.debug(f"Error reading /proc/net/wireless: {e}")
    
    # Fallback: try common interface names
    if not interfaces:
        for iface in ['wlan0', 'wlan1', 'wlp2s0', 'wlp3s0']:
            try:
                result = subprocess.run(['iwconfig', iface], capture_output=True, text=True, timeout=2)
                if 'no wireless extensions' not in result.stderr:
                    interfaces.append(iface)
            except Exception:
                pass
    
    if not interfaces:
        return None
    
    # If only one interface, use it
    if len(interfaces) == 1:
        return interfaces[0]
    
    # Multiple interfaces: find the one that's actually connected (has ESSID)
    for iface in interfaces:
        try:
            result = subprocess.run(['iwconfig', iface], capture_output=True, text=True, timeout=2)
            output = result.stdout
            # Check if interface has an ESSID (connected to a network)
            # Connected: ESSID:"NetworkName"
            # Not connected: ESSID:off/any
            if 'ESSID:"' in output and 'ESSID:off' not in output:
                logger.info(f"Found active wireless interface: {iface} (connected)")
                return iface
        except Exception:
            pass
    
    # No connected interface found, return first one as fallback
    logger.warning(f"No connected wireless interface found, using {interfaces[0]}")
    return interfaces[0]

def get_wifi_signal():
    """Get WiFi signal strength and link quality from Pi's wireless interface.
    Updates global PI_WIFI_RSSI and PI_WIFI_LQ.
    """
    global PI_WIFI_RSSI, PI_WIFI_LQ, PI_WIFI_INTERFACE
    
    # Detect interface on first call
    if PI_WIFI_INTERFACE is None:
        PI_WIFI_INTERFACE = get_wireless_interface()
        if PI_WIFI_INTERFACE:
            logger.info(f"Detected wireless interface: {PI_WIFI_INTERFACE}")
        else:
            logger.warning("No wireless interface detected, WiFi monitoring disabled")
            return
    
    if not PI_WIFI_INTERFACE:
        return
    
    try:
        # Use iwconfig to get wireless stats
        result = subprocess.run(
            ['iwconfig', PI_WIFI_INTERFACE],
            capture_output=True,
            text=True,
            timeout=2
        )
        output = result.stdout
        
        for line in output.split('\n'):
            if 'Link Quality' in line:
                # Format: "Link Quality=XX/YY  Signal level=-XX dBm"
                lq_pattern = r'Link Quality[=:](\d+)/(\d+)'
                match = re.search(lq_pattern, line)
                if match:
                    current = int(match.group(1))
                    maximum = int(match.group(2))
                    PI_WIFI_LQ = int((current / maximum) * 100) if maximum > 0 else 0
                
                # Also try to get signal level
                signal_pattern = r'Signal level[=:](-?\d+)\s*dBm'
                match = re.search(signal_pattern, line)
                if match:
                    PI_WIFI_RSSI = int(match.group(1))
    except Exception as e:
        logger.debug(f"Error getting WiFi signal: {e}")

async def wifi_monitor_loop():
    """Background task to monitor Pi's WiFi signal at 1Hz"""
    while True:
        try:
            get_wifi_signal()
        except Exception as e:
            logger.warning(f"WiFi monitor error: {e}")
        await asyncio.sleep(1)  # Update every 1 second

# Revoked tokens (persisted to file, keeps last 10)
REVOKED_TOKENS_FILE = '/home/pi/revoked_tokens.txt'
revoked_tokens = []  # List to maintain order
current_player_token = None  # Track current player's token for kick functionality

# Rate limiting for WebRTC offer endpoints (IP -> list of timestamps)
# Limits: 5 requests per minute per IP
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 5
rate_limit_tracker = {}  # IP -> [timestamp1, timestamp2, ...]

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
    global imu_heading, imu_calibration, imu_yaw_rate, imu_lateral_accel, blended_heading
    global fused_speed
    global slip_watchdog, stability_enabled
    
    # Blend heading before sending
    blend_heading()
    
    # Fuse GPS + wheel speed
    fuse_speed()
    
    # Update slip watchdog (uses IMU lateral accel + yaw rate, no GPS dependency)
    # Now runs at telemetry rate (10Hz), but could be moved to IMU loop for faster response
    if slip_watchdog and stability_enabled:
        slip_watchdog.update(
            lateral_accel=imu_lateral_accel,
            yaw_rate=imu_yaw_rate,
            speed=fused_speed,
            throttle_input=current_throttle
        )
    
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
    speed_scaled = int(fused_speed * 100)  # Use fused speed instead of raw GPS
    gps_heading_scaled = int(gps_heading * 100)
    
    # Scale IMU values
    imu_heading_scaled = int(blended_heading * 100)  # Send blended as "IMU" heading
    yaw_rate_scaled = int(max(-327.67, min(327.67, imu_yaw_rate)) * 100)  # Clamp to int16 range
    
    # Pack calibration into 1 byte: SSGGAABB (sys, gyr, acc, mag - 2 bits each)
    cal = imu_calibration
    cal_packed = ((cal['sys'] & 0x03) << 6) | ((cal['gyr'] & 0x03) << 4) | \
                 ((cal['acc'] & 0x03) << 2) | (cal['mag'] & 0x03)
    
    # Wheel distance in centimeters (uint32, max ~42km)
    wheel_distance_cm = int(wheel_distance * 100)
    
    # Format: seq(2) + cmd(1) + race_time(4) + throttle(2) + steering(2) + 
    #         lat(4) + lon(4) + speed(2) + gps_heading(2) + fix(1) +
    #         imu_heading(2) + calibration(1) + yaw_rate(2) + wheel_dist(4) = 33 bytes
    message = struct.pack('<HBIhh iiHHB HBh I', 
        0, CMD_TELEM, race_time_ms, current_throttle, current_steering,
        lat_scaled, lon_scaled, speed_scaled, gps_heading_scaled, 1 if gps_fix else 0,
        imu_heading_scaled, cal_packed, yaw_rate_scaled, wheel_distance_cm
    )
    
    # Send to all connected data channels
    for channel in data_channels[:]:  # Copy list to avoid mutation during iteration
        try:
            if channel.readyState == "open":
                channel.send(message)
        except Exception as e:
            logger.warning(f"Error sending telemetry: {e}")
    
    # Log telemetry to file if recording
    log_telemetry_frame()


def broadcast_debug_telemetry():
    """Broadcast debug telemetry for stability systems (10Hz)"""
    global data_channels, traction_ctrl, stability_ctrl, slip_watchdog, steering_shaper
    global traction_enabled, stability_enabled
    
    # Get status from each system
    # Traction Control: slip_detected(1), slip_reason(1), throttle_mult(1), wheel_accel(2), vehicle_accel(2), slip_ratio(2)
    tc_slip_detected = 0
    tc_slip_reason = 0  # 0=none, 1=launch, 2=transition, 3=cruise
    tc_throttle_mult = 100  # 0-100 percent
    tc_wheel_accel = 0  # *10, signed int16
    tc_vehicle_accel = 0  # *10, signed int16
    tc_slip_ratio = 0  # *100, signed int16
    
    # Always send sensor data for debugging, even when disabled
    # LowSpeedTractionManager has: _slip_detected, _phase, slip_ratio, wheel_accel, vehicle_accel
    if traction_ctrl:
        status = traction_ctrl.get_status()
        tc_slip_detected = 1 if (traction_enabled and status['slip_detected']) else 0
        # Encode phase as reason: 1=launch, 2=transition, 3=cruise
        phase_map = {'launch': 1, 'transition': 2, 'cruise': 3}
        tc_slip_reason = phase_map.get(status['phase'], 0) if traction_enabled else 0
        tc_throttle_mult = int(status['throttle_multiplier'] * 100) if traction_enabled else 100
        tc_wheel_accel = int(max(-3276.7, min(3276.7, status['wheel_accel'])) * 10)
        tc_vehicle_accel = int(max(-3276.7, min(3276.7, status['vehicle_accel'])) * 10)
        tc_slip_ratio = int(max(-327.67, min(327.67, status['slip_ratio'])) * 100)
    
    # Yaw Rate Controller: intervention_type(1), throttle_mult(1), virtual_brake(2), yaw_desired(2), yaw_actual(2), yaw_error(2)
    yrc_intervention = 0  # 0=none, 1=oversteer, 2=understeer
    yrc_throttle_mult = 100
    yrc_virtual_brake = 0
    yrc_yaw_desired = 0  # *10, signed int16
    yrc_yaw_actual = 0   # *10, signed int16
    yrc_yaw_error = 0    # *10, signed int16
    
    # Always send sensor data for debugging, even when disabled
    if stability_ctrl:
        if stability_enabled and stability_ctrl.intervention_type == "oversteer":
            yrc_intervention = 1
        elif stability_enabled and stability_ctrl.intervention_type == "understeer":
            yrc_intervention = 2
        yrc_throttle_mult = int(stability_ctrl.get_throttle_multiplier() * 100) if stability_enabled else 100
        yrc_virtual_brake = stability_ctrl.get_virtual_brake() if stability_enabled else 0
        yrc_yaw_desired = int(max(-3276.7, min(3276.7, stability_ctrl.yaw_rate_desired)) * 10)
        yrc_yaw_actual = int(max(-3276.7, min(3276.7, stability_ctrl.yaw_rate_actual)) * 10)
        yrc_yaw_error = int(max(-3276.7, min(3276.7, stability_ctrl.yaw_error)) * 10)
    
    # Slip Angle Watchdog: slip_angle(2), intervention_active(1), throttle_mult(1)
    saw_slip_angle = 0   # *10, signed int16
    saw_intervention = 0
    saw_throttle_mult = 100
    
    # Always send sensor data for debugging, even when disabled
    if slip_watchdog:
        saw_slip_angle = int(max(-1800, min(1800, slip_watchdog.slip_angle)) * 10)
        saw_intervention = 1 if (stability_enabled and slip_watchdog.intervention_active) else 0
        saw_throttle_mult = int(slip_watchdog.get_throttle_multiplier() * 100) if stability_enabled else 100
    
    # Steering Shaper: steering_limit(1), rate_limited(1), counter_steer_active(1), counter_steer_amount(2)
    ss_steering_limit = 100  # 0-100 percent
    ss_rate_limited = 0
    ss_counter_steer = 0
    ss_counter_amount = 0  # signed int16
    
    if steering_shaper and stability_enabled:
        ss_steering_limit = int(steering_shaper.steering_limit * 100)
        ss_rate_limited = 1 if steering_shaper.rate_limited else 0
        ss_counter_steer = 1 if steering_shaper.counter_steer_active else 0
        ss_counter_amount = steering_shaper.counter_steer_amount
    
    # Pack debug telemetry
    # Format: seq(2) + cmd(1) + 
    #   TC: slip_detected(1) + slip_reason(1) + throttle_mult(1) + wheel_accel(2) + vehicle_accel(2) + slip_ratio(2) = 9 bytes
    #   YRC: intervention(1) + throttle_mult(1) + virtual_brake(2) + yaw_desired(2) + yaw_actual(2) + yaw_error(2) = 10 bytes
    #   SAW: slip_angle(2) + intervention(1) + throttle_mult(1) = 4 bytes
    #   SS: steering_limit(1) + rate_limited(1) + counter_steer(1) + counter_amount(2) = 5 bytes
    # Total: 3 + 9 + 10 + 4 + 5 = 31 bytes
    
    message = struct.pack('<HB BBB hhh B B H hhh h BB BB Bh',
        0, CMD_DEBUG_TELEM,
        # Traction Control (9 bytes)
        tc_slip_detected, tc_slip_reason, tc_throttle_mult,
        tc_wheel_accel, tc_vehicle_accel, tc_slip_ratio,
        # Yaw Rate Controller (10 bytes)
        yrc_intervention, yrc_throttle_mult, yrc_virtual_brake,
        yrc_yaw_desired, yrc_yaw_actual, yrc_yaw_error,
        # Slip Angle Watchdog (4 bytes)
        saw_slip_angle,
        saw_intervention, saw_throttle_mult,
        # Steering Shaper (5 bytes)
        ss_steering_limit, ss_rate_limited,
        ss_counter_steer, ss_counter_amount
    )
    
    # Send to all connected data channels
    for channel in data_channels[:]:
        try:
            if channel.readyState == "open":
                channel.send(message)
        except Exception as e:
            logger.warning(f"Error sending debug telemetry: {e}")


def broadcast_extended_telemetry():
    """Broadcast extended controller telemetry at 5Hz (ABS, Hill Hold, Coast, Surface, WiFi)"""
    global data_channels
    global abs_ctrl, abs_enabled, throttle_tracker
    global hill_hold_ctrl, hill_hold_enabled
    global coast_ctrl, coast_enabled
    global surface_adapt, surface_adapt_enabled
    global imu_pitch
    global PI_WIFI_RSSI, PI_WIFI_LQ
    
    # ABS Controller: active(1), direction(1), phase(1), slip_ratio(2), esc_state(1) = 6 bytes
    abs_active = 0
    abs_direction = 0  # 0=stopped, 1=forward, 2=backward
    abs_phase = 0      # 0=none, 1=apply, 2=release
    abs_slip_ratio = 0  # *100, signed int16
    abs_esc_state = 0  # 0=neutral, 1=braking, 2=reverse_armed, 3=reversing
    
    if abs_ctrl:
        status = abs_ctrl.get_status()
        abs_active = 1 if (abs_enabled and status['active']) else 0
        abs_direction = {'stopped': 0, 'forward': 1, 'backward': 2}.get(status['direction'], 0)
        abs_phase = {'none': 0, 'apply': 1, 'release': 2}.get(status['phase'], 0)
        abs_slip_ratio = int(max(-327.67, min(327.67, status['slip_ratio'])) * 100)
    
    if throttle_tracker:
        esc_map = {'neutral': 0, 'braking': 1, 'reverse_armed': 2, 'reversing': 3}
        abs_esc_state = esc_map.get(throttle_tracker.get_state(), 0)
    
    # Hill Hold: active(1), hold_force(2), blend(1), pitch(2) = 6 bytes
    hh_active = 0
    hh_hold_force = 0   # signed int16
    hh_blend = 100      # 0-100 percent
    hh_pitch = 0        # *10, signed int16
    
    if hill_hold_ctrl:
        status = hill_hold_ctrl.get_status()
        hh_active = 1 if (hill_hold_enabled and status['active']) else 0
        hh_hold_force = status['hold_force']
        hh_blend = int(status['blend_factor'] * 100)
        hh_pitch = int(max(-1800, min(1800, imu_pitch)) * 10)
    
    # Coast Control: active(1), injection(2) = 3 bytes
    coast_active = 0
    coast_injection = 0  # int16
    
    if coast_ctrl:
        status = coast_ctrl.get_status()
        coast_active = 1 if (coast_enabled and status['active']) else 0
        coast_injection = status['injection']
    
    # Surface Adaptation: grip(2), multiplier(2), measuring(1) = 5 bytes
    surf_grip = 70       # *100, grip coefficient (default 0.7)
    surf_multiplier = 100  # *100, threshold multiplier
    surf_measuring = 0
    
    if surface_adapt:
        status = surface_adapt.get_status()
        surf_grip = int(max(0, min(200, status['estimated_grip'])) * 100)
        surf_multiplier = int(max(0, min(500, status['threshold_multiplier'])) * 100)
        surf_measuring = 1 if (surface_adapt_enabled and status['measurement_active']) else 0
    
    # WiFi Signal: rssi(1), link_quality(1) = 2 bytes
    # RSSI: Pi's WiFi signal strength in dBm (-100 to 0, clamped to -128 to 0)
    # Link Quality: Pi's local WiFi link quality (0-100%) - for diagnosing car connectivity
    # Note: Client-side connection quality (control/video LQ) is calculated in the browser
    wifi_rssi = max(-128, min(0, PI_WIFI_RSSI))  # Clamp to signed byte range
    wifi_lq = PI_WIFI_LQ  # Pi's local WiFi quality
    
    # Pack extended telemetry
    # Format: seq(2) + cmd(1) + 
    #   ABS: active(1) + direction(1) + phase(1) + slip_ratio(2) + esc_state(1) = 6 bytes
    #   HH: active(1) + hold_force(2) + blend(1) + pitch(2) = 6 bytes
    #   Coast: active(1) + injection(2) = 3 bytes
    #   Surface: grip(2) + multiplier(2) + measuring(1) = 5 bytes
    #   WiFi: rssi(1) + link_quality(1) = 2 bytes
    # Total: 3 + 6 + 6 + 3 + 5 + 2 = 25 bytes
    
    message = struct.pack('<HB BBBhB BhBh Bh HHB bB',
        0, CMD_EXTENDED_TELEM,
        # ABS (6 bytes)
        abs_active, abs_direction, abs_phase, abs_slip_ratio, abs_esc_state,
        # Hill Hold (6 bytes)
        hh_active, hh_hold_force, hh_blend, hh_pitch,
        # Coast Control (3 bytes)
        coast_active, coast_injection,
        # Surface Adaptation (5 bytes)
        surf_grip, surf_multiplier, surf_measuring,
        # WiFi Signal (2 bytes)
        wifi_rssi, wifi_lq
    )
    
    # Send to all connected data channels
    for channel in data_channels[:]:
        try:
            if channel.readyState == "open":
                channel.send(message)
        except Exception as e:
            logger.warning(f"Error sending extended telemetry: {e}")


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
    """Broadcast telemetry at 10Hz, extended telemetry at 5Hz"""
    extended_counter = 0
    while True:
        try:
            if race_state == "racing":
                broadcast_telemetry()
                broadcast_debug_telemetry()
                
                # Extended telemetry at 5Hz (every other cycle)
                extended_counter += 1
                if extended_counter >= 2:
                    broadcast_extended_telemetry()
                    extended_counter = 0
        except Exception as e:
            logger.error(f"Telemetry broadcast error: {e}", exc_info=True)
        await asyncio.sleep(0.1)  # 10Hz


# ----- IMU (BNO055) Reading -----

# Calibration file path
IMU_CALIBRATION_FILE = '/home/pi/bno055_calibration.bin'

def load_imu_calibration() -> bytes | None:
    """Load saved calibration data from file"""
    try:
        with open(IMU_CALIBRATION_FILE, 'rb') as f:
            data = f.read()
            if len(data) == 22:
                logger.info("Loaded IMU calibration from file")
                return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Failed to load IMU calibration: {e}")
    return None

def save_imu_calibration(data: bytes):
    """Save calibration data to file"""
    try:
        with open(IMU_CALIBRATION_FILE, 'wb') as f:
            f.write(data)
        logger.info("Saved IMU calibration to file")
    except Exception as e:
        logger.warning(f"Failed to save IMU calibration: {e}")

async def imu_reader_loop():
    """Read BNO055 heading and acceleration at 20Hz"""
    global imu_heading, imu_yaw_rate, imu_forward_accel, imu_lateral_accel, imu_pitch, imu_calibration, imu_valid
    global traction_ctrl, traction_enabled
    global stability_ctrl, stability_enabled
    global abs_ctrl, abs_enabled, throttle_tracker
    global hill_hold_ctrl, hill_hold_enabled
    global surface_adapt, surface_adapt_enabled
    global direction_est, signed_speed
    
    # Load saved calibration BEFORE init
    saved_cal = load_imu_calibration()
    
    bno = BNO055()
    # Pass calibration data to init so it's written before switching to NDOF mode
    if not await bno.init(calibration_data=saved_cal):
        logger.warning("BNO055 not available, using GPS heading only")
        return
    
    if saved_cal:
        logger.info("IMU calibration restored from file")
    
    logger.info("BNO055 IMU reader started (20Hz)")
    
    # Track calibration state for auto-save
    last_cal_save_time = 0
    calibration_saved = saved_cal is not None
    
    while True:
        try:
            heading = bno.read_heading()
            if heading is not None:
                # Apply mount offset and normalize to 0-360
                imu_heading = (heading + IMU_MOUNT_OFFSET) % 360.0
                imu_valid = True
            
            yaw_rate = bno.read_yaw_rate()
            if yaw_rate is not None:
                # BNO055 mounted upside-down, Z axis reversed, so negate
                # Result: positive = CCW (left turn), negative = CW (right turn)
                imu_yaw_rate = -yaw_rate
            
            # Read linear acceleration for traction control and slip detection
            lin_accel = bno.read_linear_acceleration()
            if lin_accel is not None:
                # BNO055 mounted with Y axis forward, X axis right
                # Y axis: positive = forward acceleration
                # X axis: positive = rightward acceleration (lateral)
                # Note: BNO055 is upside-down, so X axis is negated
                imu_forward_accel = lin_accel[1]
                imu_lateral_accel = -lin_accel[0]  # Negate for upside-down mount
            
            # Read pitch for hill hold (upside-down mount transforms pitch)
            pitch = bno.read_pitch()
            if pitch is not None:
                # For upside-down mount: convert ±180° (flat) to 0°
                # Formula: actual = sign(pitch) * (180 - abs(pitch))
                if pitch >= 0:
                    imu_pitch = 180 - pitch
                else:
                    imu_pitch = -180 - pitch
            
            imu_calibration = bno.read_calibration()
            
            # Get grip multiplier from surface adaptation (if enabled)
            grip_multiplier = 1.0
            if surface_adapt and surface_adapt_enabled and imu_valid:
                surface_adapt.update(
                    lateral_accel=imu_lateral_accel,
                    speed=fused_speed,
                    steering=current_steering
                )
                grip_multiplier = surface_adapt.get_traction_threshold_multiplier()
            
            # Update traction control (at IMU rate for responsiveness)
            # Always update for sensor monitoring, even when disabled
            if traction_ctrl and imu_valid:
                traction_ctrl.update(
                    wheel_speed=wheel_speed,
                    ground_speed=fused_speed,
                    imu_forward_accel=imu_forward_accel,
                    yaw_rate=imu_yaw_rate,
                    throttle_input=current_throttle,
                    grip_multiplier=grip_multiplier,
                    gps_valid=gps_fix
                )
            
            # Update yaw-rate stability control (at IMU rate for fast reaction)
            # Always update for sensor monitoring, even when disabled
            if stability_ctrl and imu_valid:
                stability_ctrl.update(
                    yaw_rate=imu_yaw_rate,
                    speed=fused_speed,
                    steering_input=current_steering
                )
            
            # Update direction estimator FIRST (at IMU rate for fast response)
            # This provides signed speed and direction to ABS and other systems
            direction = "stopped"
            if direction_est and imu_valid:
                signed_speed = direction_est.update(
                    imu_accel=imu_forward_accel,
                    wheel_speed_magnitude=wheel_speed,
                    throttle=current_throttle,
                    steering=current_steering,
                    yaw_rate=imu_yaw_rate
                )
                direction = direction_est.get_direction()
            
            # Update ABS controller sensor state (at IMU rate for consistent timing)
            # This keeps slip ratio and direction detection up-to-date between control messages
            if abs_ctrl and imu_valid:
                abs_ctrl.update_sensors(
                    wheel_speed=wheel_speed,
                    vehicle_speed=fused_speed,
                    imu_forward_accel=imu_forward_accel,
                    grip_multiplier=grip_multiplier,
                    direction_override=direction
                )
            
            # Auto-save calibration when fully calibrated (all 3s)
            # Only save once per session to avoid wear
            import time
            now = time.time()
            if (not calibration_saved and 
                imu_calibration['sys'] == 3 and 
                imu_calibration['gyr'] == 3 and 
                imu_calibration['acc'] >= 1 and  # acc can be hard to get to 3
                imu_calibration['mag'] == 3 and
                now - last_cal_save_time > 10):  # Rate limit
                
                cal_data = bno.read_calibration_data()
                if cal_data:
                    save_imu_calibration(cal_data)
                    calibration_saved = True
                last_cal_save_time = now
            
        except Exception as e:
            logger.error(f"IMU error: {e}")
            imu_valid = False
        
        await asyncio.sleep(0.05)  # 20Hz


def blend_heading():
    """Blend IMU and GPS heading based on speed"""
    global blended_heading, imu_heading, gps_heading, fused_speed, imu_valid
    
    if not imu_valid:
        # No IMU - use GPS heading directly
        blended_heading = gps_heading
        return
    
    if fused_speed < SPEED_THRESHOLD_LOW:
        # Very slow/stopped - trust IMU completely
        target = imu_heading
    elif fused_speed > SPEED_THRESHOLD_HIGH:
        # Moving fast - blend heavily toward GPS (true motion direction)
        blend_factor = 0.8  # 80% GPS, 20% IMU
        target = blend_angles(imu_heading, gps_heading, blend_factor)
    else:
        # Transitional speed - linear blend
        t = (fused_speed - SPEED_THRESHOLD_LOW) / (SPEED_THRESHOLD_HIGH - SPEED_THRESHOLD_LOW)
        target = blend_angles(imu_heading, gps_heading, t * 0.8)
    
    # Smooth the heading change (handles wrap-around)
    blended_heading = smooth_angle(blended_heading, target, HEADING_SMOOTHING)


def blend_angles(a1: float, a2: float, t: float) -> float:
    """Blend two angles, handling wrap-around. t=0 returns a1, t=1 returns a2"""
    a1_rad = math.radians(a1)
    a2_rad = math.radians(a2)
    
    # Use vector interpolation to handle wrap
    x = (1-t) * math.cos(a1_rad) + t * math.cos(a2_rad)
    y = (1-t) * math.sin(a1_rad) + t * math.sin(a2_rad)
    
    return math.degrees(math.atan2(y, x)) % 360


def smooth_angle(current: float, target: float, alpha: float) -> float:
    """Low-pass filter for angles with wrap-around handling"""
    # Calculate shortest angular difference
    diff = math.atan2(
        math.sin(math.radians(target - current)),
        math.cos(math.radians(target - current))
    )
    return (current + alpha * math.degrees(diff)) % 360


# ----- Speed Fusion (GPS + Wheel RPM) -----

def update_wheel_speed():
    """Update wheel speed and distance from Hall sensor"""
    global wheel_rpm, wheel_speed, wheel_distance, hall_sensor, race_start_pulse_count
    
    if hall_sensor is None:
        wheel_rpm = 0.0
        wheel_speed = 0.0
        wheel_distance = 0.0
        return
    
    wheel_rpm = hall_sensor.get_rpm()
    # Convert RPM to km/h: (RPM * circumference_m * 60) / 1000
    # RPM * circumference = m/min, * 60 = m/h, / 1000 = km/h
    wheel_speed = (wheel_rpm * WHEEL_CIRCUMFERENCE * 60) / 1000
    
    # Calculate distance traveled since race start
    pulses_since_start = hall_sensor.get_pulse_count() - race_start_pulse_count
    wheel_distance = pulses_since_start * WHEEL_CIRCUMFERENCE


def fuse_speed():
    """
    Improved speed fusion: IMU-primary with GPS drift correction.
    
    Strategy (Priority 2 & 3):
    1. Primary: Wheel speed (real-time, no latency)
    2. Short-term dynamics: IMU forward acceleration integration
    3. Long-term drift correction: GPS (only for slow correction, not real-time)
    4. Wheelspin detection: Cap speed if wheel >> GPS for sustained period
    
    This avoids GPS latency issues while maintaining accuracy over time.
    """
    global fused_speed, wheel_speed, gps_speed, gps_fix
    global imu_integrated_speed, last_speed_fusion_time
    global wheelspin_start_time, wheelspin_active
    global imu_forward_accel
    
    now = time.time()
    dt = now - last_speed_fusion_time if last_speed_fusion_time > 0 else 0.02
    dt = max(0.001, min(0.1, dt))  # Clamp dt
    last_speed_fusion_time = now
    
    # Update wheel speed from sensor
    update_wheel_speed()
    
    # === Step 1: Integrate IMU forward acceleration ===
    # This gives us fast response to acceleration/braking
    # Only integrate when connected to prevent drift accumulation during disconnect
    if is_connected():
        # Convert m/s² to km/h change: (m/s² * dt) * 3.6 = km/h
        accel_delta_kmh = imu_forward_accel * dt * 3.6
        imu_integrated_speed += accel_delta_kmh
        imu_integrated_speed = max(0, imu_integrated_speed)  # Can't go negative
    
    # === Step 2: Blend IMU integration with wheel speed ===
    # Wheel speed is our primary source (real-time)
    # IMU integration helps during rapid changes
    global wheel_stopped_since
    
    if wheel_speed > 0.5:
        # Wheel is turning - use wheel as primary, IMU for smoothing
        # This helps when wheel has momentary dropouts
        primary_speed = wheel_speed * 0.7 + imu_integrated_speed * 0.3
        wheel_stopped_since = 0  # Reset stationary timer
    else:
        # Wheel stopped or very slow
        # Track how long wheel has been stopped
        if wheel_stopped_since == 0:
            wheel_stopped_since = now
        
        stationary_duration = now - wheel_stopped_since
        
        # If stopped for > 3 seconds with no significant acceleration, decay speed
        if stationary_duration > STATIONARY_TIMEOUT and abs(imu_forward_accel) < IMU_ACCEL_NOISE_THRESHOLD:
            # Decay IMU integrated speed toward zero
            decay_factor = STATIONARY_DECAY_RATE * dt
            imu_integrated_speed *= max(0, 1 - decay_factor)
            if imu_integrated_speed < 0.5:
                imu_integrated_speed = 0
        
        primary_speed = imu_integrated_speed
    
    # === Step 3: Wheelspin detection (Priority 3) ===
    # If wheel speed >> GPS speed for sustained period, likely wheelspin
    if gps_fix and gps_speed > GPS_DRIFT_CORRECTION_MIN_SPEED:
        wheel_to_gps_ratio = wheel_speed / max(gps_speed, 0.1)
        
        if wheel_to_gps_ratio > WHEELSPIN_DETECT_RATIO:
            # Possible wheelspin
            if not wheelspin_active:
                if wheelspin_start_time == 0:
                    wheelspin_start_time = now
                elif now - wheelspin_start_time > WHEELSPIN_DETECT_TIME:
                    wheelspin_active = True
        else:
            # No wheelspin signature
            wheelspin_start_time = 0
            wheelspin_active = False
    else:
        # GPS not reliable enough for wheelspin detection
        wheelspin_start_time = 0
        wheelspin_active = False
    
    # Cap speed during suspected wheelspin
    if wheelspin_active and gps_fix:
        max_reasonable_speed = gps_speed * WHEELSPIN_MAX_FUSED_RATIO
        primary_speed = min(primary_speed, max_reasonable_speed)
    
    # === Step 4: GPS drift correction (slow, long-term only) ===
    # GPS is used ONLY for slow drift correction, not real-time tracking
    # This prevents GPS latency from affecting control systems
    if gps_fix and gps_speed > GPS_DRIFT_CORRECTION_MIN_SPEED:
        # Very slowly blend toward GPS to prevent long-term drift
        # This is 5% per cycle at 10Hz = ~0.5% per second correction
        drift_error = gps_speed - primary_speed
        primary_speed += GPS_DRIFT_CORRECTION_ALPHA * drift_error
        
        # Also slowly correct IMU integrated speed to prevent runaway
        imu_integrated_speed += GPS_DRIFT_CORRECTION_ALPHA * drift_error
    
    # === Step 5: Smooth final output ===
    fused_speed = fused_speed + SPEED_FUSION_ALPHA * (primary_speed - fused_speed)
    fused_speed = max(0, fused_speed)

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

# ----- Stability Intervention Logging -----

# Rate-limit logging to avoid spam (log at most every 500ms when active)
_last_intervention_log = 0
_intervention_active = False

def log_stability_interventions(orig_throttle, new_throttle, orig_steering, new_steering):
    """Log stability interventions for tuning. Rate-limited to avoid spam."""
    global _last_intervention_log, _intervention_active
    global stability_ctrl, slip_watchdog, steering_shaper, traction_ctrl
    global fused_speed, imu_yaw_rate, blended_heading, gps_heading
    
    now = time.time()
    throttle_cut = orig_throttle - new_throttle
    steering_change = new_steering - orig_steering
    
    # Check if any system is actively intervening
    intervening = False
    reasons = []
    
    if stability_ctrl and stability_ctrl.intervention_type != "none":
        intervening = True
        reasons.append(f"yaw:{stability_ctrl.intervention_type}(err={stability_ctrl.yaw_error:.0f}°/s)")
    
    if slip_watchdog and slip_watchdog.intervention_active:
        intervening = True
        reasons.append(f"slip:{slip_watchdog.slip_angle:.0f}°")
    
    # LowSpeedTractionManager uses get_status() to check intervention
    if traction_ctrl:
        status = traction_ctrl.get_status()
        if status['slip_detected']:
            intervening = True
            reasons.append(f"traction:{status['phase']}(slip={status['slip_ratio']:.0%})")
    
    if steering_shaper:
        if steering_shaper.rate_limited:
            reasons.append("steer:rate")
        if steering_shaper.counter_steer_active:
            reasons.append(f"steer:assist({steering_shaper.counter_steer_amount})")
    
    # Log when intervention starts, ends, or periodically while active
    if intervening:
        if not _intervention_active or (now - _last_intervention_log) > 0.5:
            reason_str = ", ".join(reasons) if reasons else "unknown"
            logger.info(
                f"STABILITY: thr {orig_throttle}→{new_throttle} (-{throttle_cut}), "
                f"str {orig_steering}→{new_steering}, "
                f"spd={fused_speed:.1f}km/h, yaw={imu_yaw_rate:.0f}°/s, "
                f"reason=[{reason_str}]"
            )
            _last_intervention_log = now
        _intervention_active = True
    elif _intervention_active:
        # Log when intervention ends
        logger.info("STABILITY: intervention ended, full control restored")
        _intervention_active = False

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
    
    loop = asyncio.get_running_loop()
    
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
    
    # Rate limiting
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip):
        logger.warning(f"Rate limit exceeded for {client_ip}")
        return web.Response(status=429, text='Too many requests', headers=CORS_HEADERS)
    
    # Validate token
    token = request.query.get('token', '')
    if not validate_token(token):
        logger.warning(f"Invalid token attempt from {client_ip}")
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
        global imu_integrated_speed, fused_speed, wheel_speed
        
        # Reset speed variables on reconnect (when first client connects)
        was_disconnected = len(data_channels) == 0
        control_channel = channel
        data_channels.append(channel)  # Track for telemetry broadcast
        
        if was_disconnected:
            imu_integrated_speed = 0.0
            fused_speed = 0.0
            wheel_speed = 0.0
            logger.info("Speed reset: reconnect")
        
        logger.info(f"DataChannel '{channel.label}' opened (total: {len(data_channels)})")
        
        # Send current config to new client
        send_config()
        
        # Send current race state (for reconnection during race)
        send_race_state()
        
        ctrl_count = [0]  # Use list to allow mutation in nested function
        
        @channel.on("message")
        def on_message(message):
            global race_state, current_throttle, current_steering
            global traction_ctrl, traction_enabled
            global stability_ctrl, stability_enabled
            global abs_ctrl, abs_enabled, throttle_tracker
            global hill_hold_ctrl, hill_hold_enabled
            global coast_ctrl, coast_enabled
            global surface_adapt, surface_adapt_enabled
            global video_connected, player_ready, turbo_mode
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
                        limited_throttle = current_throttle
                        shaped_steering = current_steering
                        
                        # Update ESC state tracker for ABS (pass forward accel for direction hint)
                        esc_state = "neutral"
                        if throttle_tracker:
                            esc_state = throttle_tracker.update(
                                current_throttle, fused_speed, imu_forward_accel
                            )
                        
                        # Get grip multiplier from surface adaptation
                        grip_multiplier = 1.0
                        if surface_adapt and surface_adapt_enabled:
                            grip_multiplier = surface_adapt.get_traction_threshold_multiplier()
                        
                        # === CONTROLLER CHAIN ===
                        # Order: SteeringShaper → HillHold → LowSpeedTraction → 
                        #        Stability → SlipWatchdog → ABS → CoastControl
                        
                        # 1. Apply steering shaper if enabled (latency-aware steering)
                        if steering_shaper and stability_enabled:
                            shaped_steering = steering_shaper.update(
                                steering_input=current_steering,
                                speed=fused_speed,
                                yaw_rate=imu_yaw_rate
                            )
                        
                        # 2. Apply hill hold if enabled (holds car on slopes)
                        if hill_hold_ctrl and hill_hold_enabled:
                            limited_throttle = hill_hold_ctrl.update(
                                pitch_deg=imu_pitch,
                                speed_kmh=fused_speed,
                                throttle_input=limited_throttle,
                                timestamp=time.time()
                            )
                        
                        # 3. Apply traction control if enabled (wheelspin prevention)
                        if traction_ctrl and traction_enabled and limited_throttle > 0:
                            limited_throttle = traction_ctrl.apply_to_throttle(
                                limited_throttle,
                                yaw_rate=imu_yaw_rate,
                                grip_multiplier=grip_multiplier
                            )
                        
                        # 4. Apply stability control if enabled (yaw-rate limiting)
                        if stability_ctrl and stability_enabled and limited_throttle > 0:
                            limited_throttle = stability_ctrl.apply_to_throttle(limited_throttle)
                        
                        # 5. Apply slip angle watchdog if enabled (drift/slide recovery)
                        if slip_watchdog and stability_enabled and limited_throttle > 0:
                            limited_throttle = slip_watchdog.apply_to_throttle(limited_throttle)
                        
                        # 6. Apply ABS if enabled (prevents wheel lockup during braking)
                        if abs_ctrl and abs_enabled and limited_throttle < 0:
                            limited_throttle = abs_ctrl.update(
                                wheel_speed=wheel_speed,
                                vehicle_speed=fused_speed,
                                imu_forward_accel=imu_forward_accel,
                                throttle_input=limited_throttle,
                                esc_state=esc_state,
                                timestamp_ms=int(time.time() * 1000)
                            )
                        
                        # 7. Apply coast control if enabled (smooths throttle release)
                        if coast_ctrl and coast_enabled:
                            limited_throttle = coast_ctrl.update(
                                throttle_input=limited_throttle,
                                speed_kmh=fused_speed,
                                timestamp=time.time()
                            )
                        
                        # Log interventions for tuning (rate-limited to avoid spam)
                        log_stability_interventions(
                            current_throttle, limited_throttle,
                            current_steering, shaped_steering
                        )
                        
                        # Repack if throttle or steering was modified
                        if limited_throttle != current_throttle or shaped_steering != current_steering:
                            message = struct.pack('<HBhh', seq, CMD_CTRL, limited_throttle, shaped_steering)
                        
                        forward_to_esp32(message)
                    # else: silently drop control commands (race not active)
                elif cmd == CMD_STATUS:  # STATUS - browser reporting state
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
                    if race_state != "racing":
                        pass  # Ignore car controls before race starts
                    elif len(message) >= 4:
                        new_turbo = message[3] == 1
                        turbo_mode = new_turbo
                        logger.info(f"Turbo mode set by player: {turbo_mode}")
                        
                        # Forward to ESP32
                        send_turbo_to_esp32()
                        
                        # Send updated config back to confirm
                        send_config()
                elif cmd == CMD_TRACTION:  # TRACTION - player toggling traction control
                    if race_state != "racing":
                        pass  # Ignore car controls before race starts
                    elif len(message) >= 4:
                        traction_enabled = message[3] == 1
                        if traction_ctrl:
                            traction_ctrl.enabled = traction_enabled
                            if not traction_enabled:
                                traction_ctrl.reset()  # Clear any active slip state
                        logger.info(f"Traction control set by player: {traction_enabled}")
                        # Send updated config back to confirm
                        send_config()
                elif cmd == CMD_STABILITY:  # STABILITY - player toggling yaw-rate control
                    if race_state != "racing":
                        pass  # Ignore car controls before race starts
                    elif len(message) >= 4:
                        stability_enabled = message[3] == 1
                        if stability_ctrl:
                            stability_ctrl.enabled = stability_enabled
                            if not stability_enabled:
                                stability_ctrl.reset()  # Clear any active intervention
                        if slip_watchdog:
                            slip_watchdog.enabled = stability_enabled
                            if not stability_enabled:
                                slip_watchdog.reset()
                        if steering_shaper:
                            steering_shaper.enabled = stability_enabled
                            if not stability_enabled:
                                steering_shaper.reset()
                        logger.info(f"Stability control set by player: {stability_enabled}")
                        # Send updated config back to confirm
                        send_config()
                elif cmd == CMD_HEADLIGHT:  # HEADLIGHT - player toggling headlights
                    if race_state != "racing":
                        pass  # Ignore car controls before race starts
                    elif len(message) >= 4:
                        global headlight_on
                        headlight_on = message[3] == 1
                        GPIO.output(HEADLIGHT_GPIO_PIN, GPIO.HIGH if headlight_on else GPIO.LOW)
                        logger.info(f"Headlight set by player: {'ON' if headlight_on else 'OFF'}")
                elif cmd == CMD_ABS:  # ABS - player toggling ABS
                    if race_state != "racing":
                        pass  # Ignore car controls before race starts
                    elif len(message) >= 4:
                        abs_enabled = message[3] == 1
                        if abs_ctrl:
                            abs_ctrl.enabled = abs_enabled
                            if not abs_enabled:
                                abs_ctrl.reset()
                        if throttle_tracker and not abs_enabled:
                            throttle_tracker.reset()
                        logger.info(f"ABS set by player: {abs_enabled}")
                        send_config()
                elif cmd == CMD_HILL_HOLD:  # HILL_HOLD - player toggling hill hold
                    if race_state != "racing":
                        pass  # Ignore car controls before race starts
                    elif len(message) >= 4:
                        hill_hold_enabled = message[3] == 1
                        if hill_hold_ctrl:
                            hill_hold_ctrl.enabled = hill_hold_enabled
                            if not hill_hold_enabled:
                                hill_hold_ctrl.reset()
                        logger.info(f"Hill hold set by player: {hill_hold_enabled}")
                        send_config()
                elif cmd == CMD_COAST:  # COAST - player toggling coast control
                    if race_state != "racing":
                        pass  # Ignore car controls before race starts
                    elif len(message) >= 4:
                        coast_enabled = message[3] == 1
                        if coast_ctrl:
                            coast_ctrl.enabled = coast_enabled
                            if not coast_enabled:
                                coast_ctrl.reset()
                        logger.info(f"Coast control set by player: {coast_enabled}")
                        send_config()
                elif cmd == CMD_SURFACE_ADAPT:  # SURFACE_ADAPT - player toggling surface adaptation
                    if race_state != "racing":
                        pass  # Ignore car controls before race starts
                    elif len(message) >= 4:
                        surface_adapt_enabled = message[3] == 1
                        if surface_adapt:
                            surface_adapt.enabled = surface_adapt_enabled
                            if not surface_adapt_enabled:
                                surface_adapt.reset()
                        logger.info(f"Surface adaptation set by player: {surface_adapt_enabled}")
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
    
    # Rate limiting
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip):
        logger.warning(f"Rate limit exceeded for telemetry from {client_ip}")
        return web.Response(status=429, text='Too many requests', headers=CORS_HEADERS)
    
    # Validate token
    token = request.query.get('token', '')
    if not validate_token(token):
        logger.warning(f"Invalid token attempt for telemetry from {client_ip}")
        return web.Response(status=401, text='Invalid or expired token')
    
    logger.info(f"Telemetry subscriber connecting from {client_ip}...")
    
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
    """Health check endpoint - requires valid token to access detailed info"""
    # Validate token for detailed health info
    token = request.query.get('token', '')
    has_valid_token = validate_token(token)
    
    # Basic health response (always available, no sensitive data)
    if not has_valid_token:
        return web.json_response(
            {
                "status": "ok",
                "connected": pc is not None and pc.connectionState == "connected",
            },
            headers=CORS_HEADERS
        )
    
    # Full health response (requires valid token)
    # Get traction control status
    tc_status = traction_ctrl.get_status() if traction_ctrl else {"enabled": False}
    
    # Get direction estimator status
    dir_status = direction_est.get_status() if direction_est else {
        "direction": "stopped", "signed_speed": 0.0, "confidence": 0.0
    }
    
    return web.json_response(
        {
            "status": "ok",
            "connected": pc is not None and pc.connectionState == "connected",
            "channel_open": control_channel is not None and control_channel.readyState == "open",
            "video_connected": video_connected,
            "player_ready": player_ready,
            "turbo_mode": turbo_mode,
            "speed": {
                "fused_kmh": round(fused_speed, 2),
                "gps_kmh": round(gps_speed, 2),
                "wheel_kmh": round(wheel_speed, 2),
                "wheel_rpm": round(wheel_rpm, 1),
                "wheel_distance_m": round(wheel_distance, 2),
                "signed_kmh": round(dir_status['signed_speed'], 2),
                "direction": dir_status['direction']
            },
            "gps": {
                "fix": gps_fix,
                "lat": gps_lat,
                "lon": gps_lon,
                "heading": round(gps_heading, 1)
            },
            "imu": {
                "valid": imu_valid,
                "heading": round(imu_heading, 1),
                "blended_heading": round(blended_heading, 1),
                "yaw_rate": round(imu_yaw_rate, 1),
                "forward_accel": round(imu_forward_accel, 2),
                "lateral_accel": round(imu_lateral_accel, 2),
                "calibration": imu_calibration
            },
            "traction_control": tc_status,
            "direction_estimator": dir_status
        },
        headers={"Access-Control-Allow-Origin": "*"}
    )

# ----- Admin Interface -----

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Admin-Password",
}

def check_admin_auth(request) -> bool:
    """Validate admin password from X-Admin-Password header.
    Returns True if authenticated, False otherwise.
    """
    if not ADMIN_PASSWORD:
        logger.warning("ADMIN_PASSWORD not configured - admin endpoints unprotected!")
        return True  # Allow if not configured (for backwards compatibility during rollout)
    
    password = request.headers.get('X-Admin-Password', '')
    return hmac.compare_digest(password, ADMIN_PASSWORD)

def check_rate_limit(client_ip: str) -> bool:
    """Check if client IP is within rate limit for offer endpoints.
    Returns True if allowed, False if rate limited.
    """
    global rate_limit_tracker
    now = time.time()
    
    # Clean up old entries
    if client_ip in rate_limit_tracker:
        rate_limit_tracker[client_ip] = [
            ts for ts in rate_limit_tracker[client_ip] 
            if now - ts < RATE_LIMIT_WINDOW
        ]
    else:
        rate_limit_tracker[client_ip] = []
    
    # Check limit
    if len(rate_limit_tracker[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    
    # Record this request
    rate_limit_tracker[client_ip].append(now)
    return True

def get_client_ip(request) -> str:
    """Extract client IP from request, handling X-Forwarded-For from Cloudflare Tunnel."""
    # Cloudflare adds CF-Connecting-IP header
    cf_ip = request.headers.get('CF-Connecting-IP')
    if cf_ip:
        return cf_ip
    
    # Standard X-Forwarded-For (first IP is original client)
    xff = request.headers.get('X-Forwarded-For')
    if xff:
        return xff.split(',')[0].strip()
    
    # Fallback to peer IP
    return request.remote or 'unknown'

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
    """Send current config (turbo mode, traction control, stability control, etc.) to browser"""
    global control_channel, turbo_mode, traction_enabled, stability_enabled
    global abs_enabled, hill_hold_enabled, coast_enabled, surface_adapt_enabled
    
    if control_channel is None or control_channel.readyState != "open":
        return False
    
    # Format: seq(2) + cmd(1) + reserved(1) + turbo(1) + traction(1) + stability(1) + 
    #         abs(1) + hill_hold(1) + coast(1) + surface_adapt(1) = 11 bytes
    message = struct.pack('<HBbBBBBBBB', 0, CMD_CONFIG, 0, 
                          1 if turbo_mode else 0, 
                          1 if traction_enabled else 0,
                          1 if stability_enabled else 0,
                          1 if abs_enabled else 0,
                          1 if hill_hold_enabled else 0,
                          1 if coast_enabled else 0,
                          1 if surface_adapt_enabled else 0)
    control_channel.send(message)
    logger.info(f"Sent config: turbo={turbo_mode}, traction={traction_enabled}, stability={stability_enabled}, abs={abs_enabled}, hill_hold={hill_hold_enabled}, coast={coast_enabled}, surface_adapt={surface_adapt_enabled}")
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
    global race_state, race_start_time, race_start_pulse_count, hall_sensor
    global imu_integrated_speed, fused_speed, wheel_speed
    await asyncio.sleep(3.0)
    race_state = "racing"
    race_start_time = time.time()
    # Reset wheel distance tracking
    if hall_sensor:
        race_start_pulse_count = hall_sensor.get_pulse_count()
    # Reset speed variables for clean slate each race
    imu_integrated_speed = 0.0
    fused_speed = 0.0
    wheel_speed = 0.0
    logger.info("Speed reset: race start")
    logger.info("Race started - controls enabled")
    
    # Start recording and telemetry logging
    start_telemetry_log()
    await start_recording()

async def handle_start_race(request):
    """Admin endpoint to start race countdown"""
    global race_state, countdown_task
    
    # Check admin authentication
    if not check_admin_auth(request):
        return web.json_response({"success": False, "error": "Unauthorized"}, status=401, headers=CORS_HEADERS)
    
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
    
    # Check admin authentication
    if not check_admin_auth(request):
        return web.json_response({"success": False, "error": "Unauthorized"}, status=401, headers=CORS_HEADERS)
    
    # Cancel countdown if in progress
    if countdown_task and not countdown_task.done():
        countdown_task.cancel()
        countdown_task = None
    
    # Stop recording and telemetry logging if active
    stop_telemetry_log()
    await stop_recording()
    
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
    
    # Check admin authentication
    if not check_admin_auth(request):
        return web.json_response({"success": False, "error": "Unauthorized"}, status=401, headers=CORS_HEADERS)
    
    if not pc or not control_channel:
        return web.json_response({"success": False, "error": "No player connected"}, status=400, headers=CORS_HEADERS)
    
    # Revoke the token so they can't reconnect with it
    if current_player_token:
        revoke_token(current_player_token)
        current_player_token = None
    
    # Stop any active race, recording, and telemetry logging
    if countdown_task and not countdown_task.done():
        countdown_task.cancel()
        countdown_task = None
    stop_telemetry_log()
    await stop_recording()
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

async def handle_set_turbo(request):
    """Admin endpoint to toggle turbo mode"""
    global turbo_mode
    
    # Check admin authentication
    if not check_admin_auth(request):
        return web.json_response({"success": False, "error": "Unauthorized"}, status=401, headers=CORS_HEADERS)
    
    try:
        body = await request.json()
        new_turbo = bool(body.get('enabled', False))
        turbo_mode = new_turbo
        logger.info(f"Turbo mode set to {turbo_mode}")
        
        # Send turbo mode to ESP32
        send_turbo_to_esp32()
        
        # Send updated config to browser
        send_config()
        
        return web.json_response({"success": True, "turbo_mode": turbo_mode}, headers=CORS_HEADERS)
    except Exception as e:
        logger.error(f"Error setting turbo: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=400, headers=CORS_HEADERS)

async def handle_set_traction_control(request):
    """Admin endpoint to toggle traction control"""
    global traction_enabled, traction_ctrl
    
    # Check admin authentication
    if not check_admin_auth(request):
        return web.json_response({"success": False, "error": "Unauthorized"}, status=401, headers=CORS_HEADERS)
    
    try:
        body = await request.json()
        traction_enabled = bool(body.get('enabled', False))
        
        # Reset traction control state when toggling
        if traction_ctrl:
            traction_ctrl.reset()
            traction_ctrl.enabled = traction_enabled
        
        logger.info(f"Traction control set to {traction_enabled}")
        return web.json_response({
            "success": True, 
            "traction_enabled": traction_enabled
        }, headers=CORS_HEADERS)
    except Exception as e:
        logger.error(f"Error setting traction control: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=400, headers=CORS_HEADERS)

# ----- UDP Hole Punch -----

async def init_hole_punch_socket():
    """Initialize UDP socket for hole punching and discover public endpoint."""
    global hole_punch_sock, public_ip, public_port, nat_type_symmetric
    
    # Create UDP socket
    hole_punch_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    hole_punch_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    hole_punch_sock.bind(('0.0.0.0', hole_punch_port))
    hole_punch_sock.setblocking(False)
    
    logger.info(f"Hole punch socket bound to port {hole_punch_port}")
    
    # Discover public endpoint via STUN
    try:
        endpoint = await discover_endpoint(hole_punch_sock)
        public_ip = endpoint['ip']
        public_port = endpoint['port']
        nat_type_symmetric = endpoint['is_symmetric']
        
        logger.info(f"Public endpoint: {public_ip}:{public_port} (symmetric={nat_type_symmetric})")
        
        if nat_type_symmetric:
            logger.warning("Symmetric NAT detected - hole punching may not work!")
            
    except Exception as e:
        logger.error(f"STUN discovery failed: {e}")
        raise


async def stun_refresh_loop():
    """Periodically refresh STUN binding to keep NAT mapping alive."""
    global public_ip, public_port, nat_type_symmetric, hole_punch_sock
    
    while True:
        await asyncio.sleep(STUN_REFRESH_INTERVAL)
        
        if not hole_punch_sock:
            continue
            
        try:
            endpoint = await discover_endpoint(hole_punch_sock)
            
            # Check if endpoint changed (NAT rebinding)
            if endpoint['ip'] != public_ip or endpoint['port'] != public_port:
                logger.warning(
                    f"Public endpoint changed: {public_ip}:{public_port} -> "
                    f"{endpoint['ip']}:{endpoint['port']}"
                )
            
            public_ip = endpoint['ip']
            public_port = endpoint['port']
            nat_type_symmetric = endpoint['is_symmetric']
            
        except Exception as e:
            logger.error(f"STUN refresh failed: {e}")


async def get_mediamtx_sdp_info() -> dict:
    """Fetch codec info from MediaMTX API."""
    try:
        async with ClientSession() as session:
            async with session.get(
                f"{MEDIAMTX_API_URL}/v3/paths/get/cam",
                timeout=5
            ) as resp:
                if resp.status != 200:
                    logger.error(f"MediaMTX API error: {resp.status}")
                    return None
                
                data = await resp.json()
                
                # Extract relevant codec info from source
                source = data.get('source', {})
                tracks = source.get('tracks', [])
                
                # Find H264 video track
                for track in tracks:
                    if track.get('codec', '').upper() == 'H264':
                        return {
                            'codec': 'H264',
                            'width': track.get('width', 1280),
                            'height': track.get('height', 720),
                            'fps': track.get('fps', 60),
                            # SPS/PPS in base64 if available
                            'spropParameterSets': track.get('spropParameterSets', ''),
                        }
                
                # Fallback to default values if track info not found
                logger.warning("H264 track not found in MediaMTX, using defaults")
                return {
                    'codec': 'H264',
                    'width': 1280,
                    'height': 720,
                    'fps': 60,
                    'spropParameterSets': '',
                }
                
    except Exception as e:
        logger.error(f"Failed to get MediaMTX info: {e}")
        return None


async def send_punch_packets(client_ip: str, client_port: int, count: int = 5):
    """Send hole punch packets to client to open NAT pinhole."""
    global hole_punch_sock
    
    if not hole_punch_sock:
        logger.error("send_punch_packets: hole_punch_sock is None!")
        return
    
    logger.info(f"Sending {count} punch packets to {client_ip}:{client_port}")
    
    for i in range(count):
        try:
            # Send a small punch packet (just a marker byte)
            hole_punch_sock.sendto(b'\x00', (client_ip, client_port))
            logger.info(f"Sent punch packet {i+1}/{count} to {client_ip}:{client_port}")
        except Exception as e:
            logger.error(f"Failed to send punch packet: {e}")
        
        if i < count - 1:
            await asyncio.sleep(0.1)  # 100ms between packets
    
    logger.info(f"Finished sending punch packets to {client_ip}:{client_port}")


async def handle_stream_punch(request):
    """Handle hole punch request from IWA client.
    
    Client sends: {clientIp, clientPort}
    Server responds: {piIp, piPort, sdpInfo, natType}
    Before responding, starts sending punch packets to client.
    """
    global active_punch_clients
    
    # Validate token
    token = request.query.get('token', '')
    if not validate_token(token):
        logger.warning("Invalid token for stream punch")
        return web.json_response(
            {"error": "Invalid or expired token"},
            status=401,
            headers=CORS_HEADERS
        )
    
    # Check if hole punch socket is ready
    if not hole_punch_sock or not public_ip:
        return web.json_response(
            {"error": "Hole punch not initialized"},
            status=503,
            headers=CORS_HEADERS
        )
    
    try:
        body = await request.json()
        client_ip = body.get('clientIp')
        client_port = body.get('clientPort')
        
        if not client_ip or not client_port:
            return web.json_response(
                {"error": "Missing clientIp or clientPort"},
                status=400,
                headers=CORS_HEADERS
            )
        
        client_port = int(client_port)
        
        logger.info(f"Hole punch request from {client_ip}:{client_port}")
        
        # Remove any existing entries from the same IP (client reconnected with new port)
        old_entries = [k for k in active_punch_clients if k[0] == client_ip]
        for old_entry in old_entries:
            del active_punch_clients[old_entry]
            logger.info(f"[HolePunch] Removed old entry for same IP: {old_entry}")
        
        # Track this client
        active_punch_clients[(client_ip, client_port)] = time.time()
        logger.info(f"[HolePunch] Added client {client_ip}:{client_port} to active_punch_clients, total: {len(active_punch_clients)}")
        
        # IMPORTANT: Send punch packets BEFORE starting FEC sender
        # This opens the NAT pinhole while we still have the original socket
        # that was used for STUN discovery (same NAT mapping)
        if hole_punch_sock:
            await send_punch_packets(client_ip, client_port, count=3)
        
        # Now start FEC sender (this will close hole_punch_sock)
        await ensure_rtsp_forwarder()
        
        # Build sdp_info with cached sprop-parameter-sets from RTSP DESCRIBE
        sdp_info = {
            'codec': 'H264',
            'width': 1280,
            'height': 720,
            'fps': 60,
            'spropParameterSets': cached_sprop_parameter_sets,
        }
        
        if cached_sprop_parameter_sets:
            logger.info(f"Sending sprop-parameter-sets to client: {cached_sprop_parameter_sets[:50]}...")
        else:
            logger.warning("No sprop-parameter-sets cached, client will configure from stream")
        
        return web.json_response({
            'piIp': public_ip,
            'piPort': public_port,
            'sdpInfo': sdp_info,
            'natType': 'symmetric' if nat_type_symmetric else 'cone',
        }, headers=CORS_HEADERS)
        
    except Exception as e:
        logger.error(f"Stream punch error: {e}")
        return web.json_response(
            {"error": str(e)},
            status=500,
            headers=CORS_HEADERS
        )


async def handle_stream_status(request):
    """Return current hole punch status (public endpoint, NAT type)."""
    return web.json_response({
        'ready': hole_punch_sock is not None and public_ip is not None,
        'piIp': public_ip,
        'piPort': public_port,
        'natType': 'symmetric' if nat_type_symmetric else 'cone',
    }, headers=CORS_HEADERS)


async def handle_stream_stop(request):
    """Handle stream stop request from IWA client.
    
    This cleans up the FEC sender and re-opens the hole punch socket
    so a new connection can be established.
    """
    global active_punch_clients
    
    # Validate token
    token = request.query.get('token', '')
    if not validate_token(token):
        logger.warning("Invalid token for stream stop")
        return web.json_response(
            {"error": "Invalid or expired token"},
            status=401,
            headers=CORS_HEADERS
        )
    
    try:
        # Get client info from request body (optional)
        try:
            body = await request.json()
            client_ip = body.get('clientIp')
            client_port = body.get('clientPort')
            if client_ip and client_port:
                client_key = (client_ip, int(client_port))
                if client_key in active_punch_clients:
                    del active_punch_clients[client_key]
                    logger.info(f"Removed client {client_key} from active list")
        except:
            pass  # Body is optional
        
        # Clear all clients and stop FEC sender
        active_punch_clients.clear()
        logger.info("Cleared all punch clients")
        
        if USE_FEC_SENDER:
            await stop_fec_sender()
            await reopen_hole_punch_socket()
            logger.info("FEC sender stopped, socket re-opened")
        elif rtp_forwarder and rtp_forwarder.running:
            await rtp_forwarder.stop()
            logger.info("RTP forwarder stopped")
        
        return web.json_response({
            'success': True,
            'ready': hole_punch_sock is not None,
        }, headers=CORS_HEADERS)
        
    except Exception as e:
        logger.error(f"Stream stop error: {e}")
        return web.json_response(
            {"error": str(e)},
            status=500,
            headers=CORS_HEADERS
        )


class RTSPForwarder:
    """RTSP client that forwards RTP packets to hole-punched clients.
    
    Connects to MediaMTX RTSP on demand, receives RTP, forwards to clients.
    Automatically stops when no clients remain.
    """
    
    def __init__(self, rtsp_url: str, rtp_port: int):
        self.rtsp_url = rtsp_url
        self.rtp_port = rtp_port
        self.reader = None
        self.writer = None
        self.session_id = None
        self.cseq = 0
        self.running = False
        self.rtp_sock = None
        self.forward_task = None
        self.server_rtp_port = None
        
    async def start(self):
        """Start RTSP session and begin forwarding RTP."""
        if self.running:
            return
            
        logger.info(f"Starting RTSP forwarder to {self.rtsp_url}")
        
        try:
            # Parse RTSP URL
            url = self.rtsp_url
            if url.startswith('rtsp://'):
                url = url[7:]
            host_port, path = url.split('/', 1) if '/' in url else (url, '')
            if ':' in host_port:
                host, port = host_port.split(':')
                port = int(port)
            else:
                host, port = host_port, 554
            
            # Connect TCP
            self.reader, self.writer = await asyncio.open_connection(host, port)
            logger.info(f"RTSP connected to {host}:{port}")
            
            # Create RTP receive socket
            self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.rtp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.rtp_sock.bind(('0.0.0.0', self.rtp_port))
            self.rtp_sock.setblocking(False)
            logger.info(f"RTP socket bound to port {self.rtp_port}")
            
            # RTSP handshake
            await self._describe()
            await self._setup()
            await self._play()
            
            self.running = True
            
            # Start RTP forwarding loop
            self.forward_task = asyncio.create_task(self._forward_loop())
            logger.info("RTSP forwarder started")
            
        except Exception as e:
            logger.error(f"RTSP forwarder start failed: {e}")
            await self.stop()
            raise
    
    async def stop(self):
        """Stop RTSP session and RTP forwarding."""
        if not self.running and not self.writer:
            return
            
        logger.info("Stopping RTSP forwarder")
        self.running = False
        
        # Cancel forward task
        if self.forward_task:
            self.forward_task.cancel()
            try:
                await self.forward_task
            except asyncio.CancelledError:
                pass
            self.forward_task = None
        
        # Send TEARDOWN
        if self.writer and self.session_id:
            try:
                await self._send_request('TEARDOWN', self.rtsp_url)
            except:
                pass
        
        # Close connections
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except:
                pass
            self.writer = None
            self.reader = None
        
        if self.rtp_sock:
            self.rtp_sock.close()
            self.rtp_sock = None
            
        self.session_id = None
        logger.info("RTSP forwarder stopped")
    
    async def _send_request(self, method: str, url: str, extra_headers: dict = None) -> tuple:
        """Send RTSP request and wait for response."""
        self.cseq += 1
        
        headers = [
            f"{method} {url} RTSP/1.0",
            f"CSeq: {self.cseq}",
            "User-Agent: PiRelay/1.0",
        ]
        
        if self.session_id:
            headers.append(f"Session: {self.session_id}")
        
        if extra_headers:
            for k, v in extra_headers.items():
                headers.append(f"{k}: {v}")
        
        request = '\r\n'.join(headers) + '\r\n\r\n'
        self.writer.write(request.encode())
        await self.writer.drain()
        
        # Read response
        response_lines = []
        content_length = 0
        
        while True:
            line = await self.reader.readline()
            line = line.decode().rstrip('\r\n')
            if not line:
                break
            response_lines.append(line)
            if line.lower().startswith('content-length:'):
                content_length = int(line.split(':')[1].strip())
        
        # Read body if present
        body = ''
        if content_length > 0:
            body = (await self.reader.read(content_length)).decode()
        
        # Parse status
        status_line = response_lines[0] if response_lines else ''
        status_code = 0
        if 'RTSP/1.0' in status_line:
            parts = status_line.split(' ')
            if len(parts) >= 2:
                status_code = int(parts[1])
        
        # Parse headers into dict
        headers_dict = {}
        for line in response_lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers_dict[k.strip().lower()] = v.strip()
        
        # Extract session ID
        if 'session' in headers_dict:
            self.session_id = headers_dict['session'].split(';')[0]
        
        return status_code, headers_dict, body
    
    async def _describe(self):
        """DESCRIBE request to get SDP."""
        global cached_sprop_parameter_sets
        
        status, headers, body = await self._send_request(
            'DESCRIBE', self.rtsp_url,
            {'Accept': 'application/sdp'}
        )
        
        if status != 200:
            raise Exception(f"DESCRIBE failed: {status}")
        
        # Parse track control and sprop-parameter-sets from SDP
        self.track_url = None
        for line in body.split('\n'):
            line = line.strip()
            if line.startswith('a=control:') and 'track' in line.lower():
                control = line[10:]
                if control.startswith('rtsp://'):
                    self.track_url = control
                else:
                    # Relative URL
                    base = self.rtsp_url.rstrip('/') + '/'
                    self.track_url = base + control
            # Extract sprop-parameter-sets (SPS/PPS in base64)
            elif 'sprop-parameter-sets=' in line:
                # Format: a=fmtp:96 ... sprop-parameter-sets=<base64>,<base64> ...
                match = re.search(r'sprop-parameter-sets=([A-Za-z0-9+/=,]+)', line)
                if match:
                    cached_sprop_parameter_sets = match.group(1)
                    logger.info(f"Cached sprop-parameter-sets from SDP: {cached_sprop_parameter_sets[:50]}...")
        
        if not self.track_url:
            # Default to trackID=0
            self.track_url = self.rtsp_url.rstrip('/') + '/trackID=0'
        
        logger.info(f"RTSP track URL: {self.track_url}")
    
    async def _setup(self):
        """SETUP request to configure RTP transport."""
        status, headers, body = await self._send_request(
            'SETUP', self.track_url,
            {'Transport': f'RTP/AVP;unicast;client_port={self.rtp_port}-{self.rtp_port + 1}'}
        )
        
        if status != 200:
            raise Exception(f"SETUP failed: {status}")
        
        # Parse server_port from Transport header
        transport = headers.get('transport', '')
        match = re.search(r'server_port=(\d+)', transport)
        self.server_rtp_port = int(match.group(1)) if match else 8000
        
        logger.info(f"RTSP session: {self.session_id}, server RTP port: {self.server_rtp_port}")
    
    async def _play(self):
        """PLAY request to start streaming."""
        status, headers, body = await self._send_request(
            'PLAY', self.rtsp_url,
            {'Range': 'npt=0.000-'}
        )
        
        if status != 200:
            raise Exception(f"PLAY failed: {status}")
        
        logger.info("RTSP PLAY started")
    
    async def _forward_loop(self):
        """Receive RTP and forward to hole-punched clients."""
        global active_punch_clients, hole_punch_sock
        
        loop = asyncio.get_running_loop()
        packets_forwarded = 0
        last_log_time = time.time()
        first_packet_sent = {}  # Track first packet per client for logging
        
        while self.running:
            try:
                # Receive RTP packet
                data = await asyncio.wait_for(
                    loop.sock_recv(self.rtp_sock, 1500),
                    timeout=5.0
                )
                
                if not data or not hole_punch_sock:
                    continue
                
                # Clean up stale clients (no activity for 30s)
                now = time.time()
                stale_clients = [
                    client for client, last_time in active_punch_clients.items()
                    if now - last_time > 30
                ]
                for client in stale_clients:
                    del active_punch_clients[client]
                    logger.info(f"Removed stale client: {client}")
                
                # Forward to all active clients
                for (client_ip, client_port) in list(active_punch_clients.keys()):
                    try:
                        hole_punch_sock.sendto(data, (client_ip, client_port))
                        packets_forwarded += 1
                        # Log first RTP packet to each client
                        client_key = (client_ip, client_port)
                        if client_key not in first_packet_sent:
                            first_packet_sent[client_key] = True
                            logger.info(f"[RTP] First packet sent to {client_ip}:{client_port}, size={len(data)}")
                    except Exception as e:
                        logger.warning(f"Failed to forward RTP to {client_ip}:{client_port}: {e}")
                
                # Log stats every 10 seconds
                if now - last_log_time > 10:
                    logger.info(f"[RTP] Forwarded {packets_forwarded} packets to {len(active_punch_clients)} clients: {list(active_punch_clients.keys())}")
                    packets_forwarded = 0
                    last_log_time = now
                
                # Stop if no clients
                if not active_punch_clients:
                    logger.info("No active clients, stopping RTSP forwarder")
                    asyncio.create_task(self.stop())
                    break
                    
            except asyncio.TimeoutError:
                # Check if we should stop (no clients)
                if not active_punch_clients:
                    logger.info("No active clients, stopping RTSP forwarder")
                    asyncio.create_task(self.stop())
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"RTP forward error: {e}")
                await asyncio.sleep(0.1)


async def start_fec_sender(client_ip: str, client_port: int):
    """Start native FEC sender process for a client.
    
    The FEC sender uses GStreamer to capture video directly from libcamera,
    applies Reed-Solomon FEC encoding, and sends UDP packets to the client.
    
    IMPORTANT: We must bind to the same port used for hole punching so
    packets go through the NAT pinhole.
    """
    global fec_sender_process, fec_sender_target, hole_punch_sock
    
    # Kill any existing sender
    await stop_fec_sender()
    
    if not os.path.exists(FEC_SENDER_PATH):
        logger.error(f"FEC sender not found: {FEC_SENDER_PATH}")
        return False
    
    # Close hole punch socket so FEC sender can bind to same port
    if hole_punch_sock:
        try:
            hole_punch_sock.close()
            logger.info("Closed hole punch socket for FEC sender")
        except Exception as e:
            logger.warning(f"Error closing hole punch socket: {e}")
        hole_punch_sock = None
    
    # Small delay to ensure socket is released by OS
    await asyncio.sleep(0.1)
    
    try:
        logger.info(f"Starting FEC sender: {FEC_SENDER_PATH} {client_ip} {client_port} {hole_punch_port}")
        fec_sender_process = await asyncio.create_subprocess_exec(
            FEC_SENDER_PATH,
            client_ip,
            str(client_port),
            str(hole_punch_port),  # Source port for NAT traversal
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        # Track target client
        fec_sender_target = (client_ip, client_port)
        
        # Start log reader tasks
        asyncio.create_task(_read_fec_sender_output(fec_sender_process.stdout, "FEC-OUT"))
        asyncio.create_task(_read_fec_sender_output(fec_sender_process.stderr, "FEC-ERR"))
        
        logger.info(f"FEC sender started with PID {fec_sender_process.pid}")
        
        # Check if process exited immediately (indicates startup failure)
        await asyncio.sleep(0.2)
        if fec_sender_process.returncode is not None:
            logger.error(f"FEC sender exited immediately with code {fec_sender_process.returncode}")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to start FEC sender: {e}")
        return False


async def _read_fec_sender_output(stream, prefix: str):
    """Read and log FEC sender output."""
    while True:
        line = await stream.readline()
        if not line:
            break
        logger.info(f"[{prefix}] {line.decode().rstrip()}")


async def stop_fec_sender():
    """Stop the FEC sender process. Does NOT re-open hole punch socket."""
    global fec_sender_process, fec_sender_target, hole_punch_sock
    
    if fec_sender_process is None:
        return
    
    # Capture reference locally to avoid race with concurrent calls
    proc = fec_sender_process
    fec_sender_process = None
    fec_sender_target = None
    
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        logger.info("FEC sender stopped")
    except Exception as e:
        logger.warning(f"Error stopping FEC sender: {e}")


async def reopen_hole_punch_socket():
    """Re-open hole punch socket after FEC sender stops."""
    global hole_punch_sock, hole_punch_port
    
    if hole_punch_sock is not None:
        return  # Already open
    
    try:
        hole_punch_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        hole_punch_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        hole_punch_sock.bind(('0.0.0.0', hole_punch_port))
        hole_punch_sock.setblocking(False)
        logger.info(f"Re-opened hole punch socket on port {hole_punch_port}")
    except Exception as e:
        logger.error(f"Failed to re-open hole punch socket: {e}")


async def ensure_video_sender():
    """Start video sender (FEC or RTSP) if not running and clients exist."""
    global rtp_forwarder, fec_sender_process, fec_sender_target, active_punch_clients
    
    if not active_punch_clients:
        return
    
    # Get the first active client (FEC sender only supports one client currently)
    client = next(iter(active_punch_clients.keys()))
    client_ip, client_port = client
    
    if USE_FEC_SENDER:
        # Check if FEC sender needs (re)start:
        # 1. Not running at all
        # 2. Process exited
        # 3. Target client changed (reconnect with new port)
        needs_restart = (
            fec_sender_process is None or 
            fec_sender_process.returncode is not None or
            fec_sender_target != (client_ip, client_port)
        )
        
        if needs_restart:
            if fec_sender_target and fec_sender_target != (client_ip, client_port):
                logger.info(f"Client changed from {fec_sender_target} to {(client_ip, client_port)}, restarting FEC sender")
            await start_fec_sender(client_ip, client_port)
    else:
        # Fallback to RTSP forwarder (no FEC)
        if rtp_forwarder is None:
            rtp_forwarder = RTSPForwarder(MEDIAMTX_RTSP_URL, RTP_LOCAL_PORT)
        
        if not rtp_forwarder.running:
            try:
                await rtp_forwarder.start()
            except Exception as e:
                logger.error(f"Failed to start RTSP forwarder: {e}")


async def ensure_rtsp_forwarder():
    """Start RTSP forwarder if not running and clients exist.
    
    DEPRECATED: Use ensure_video_sender() instead.
    This is kept for backward compatibility.
    """
    await ensure_video_sender()


async def hole_punch_receiver_loop():
    """Listen for incoming packets on hole punch socket.
    
    When we receive a packet from a client, it confirms the hole is punched.
    We update their last activity time.
    
    IMPORTANT: Filter out STUN server responses - they use the same socket!
    """
    global active_punch_clients, hole_punch_sock
    
    loop = asyncio.get_running_loop()
    
    # Resolve STUN server IPs to filter them out
    # These respond to our STUN queries on the same socket
    stun_server_ips = set()
    for hostname, port in [("stun.cloudflare.com", 3478), ("stun.l.google.com", 19302)]:
        try:
            import socket as sock_module
            ips = sock_module.getaddrinfo(hostname, port, sock_module.AF_INET)
            for ip_info in ips:
                stun_server_ips.add(ip_info[4][0])
        except Exception as e:
            logger.warning(f"Failed to resolve STUN server {hostname}: {e}")
    logger.info(f"[HolePunch] STUN server IPs to filter: {stun_server_ips}")
    
    while True:
        if not hole_punch_sock:
            await asyncio.sleep(1)
            continue
        
        try:
            # Use wait_for with timeout instead of blocking recv
            data, addr = await asyncio.wait_for(
                loop.sock_recvfrom(hole_punch_sock, 1500),
                timeout=3.0  # 3 second timeout
            )
            client_ip, client_port = addr
            
            # Filter out STUN server responses - they're not real clients!
            if client_ip in stun_server_ips:
                logger.debug(f"[HolePunch] Ignoring STUN response from {client_ip}:{client_port}")
                continue
            
            # Log ALL incoming packets for debugging
            logger.debug(f"[HolePunch] Received packet from {client_ip}:{client_port}, size={len(data)}")
            
            # Update activity time (confirms hole is punched)
            is_new_client = (client_ip, client_port) not in active_punch_clients
            active_punch_clients[(client_ip, client_port)] = time.time()
            
            if is_new_client:
                logger.info(f"[HolePunch] NEW client confirmed: {client_ip}:{client_port}, total clients: {len(active_punch_clients)}")
                # Start RTSP forwarder if not running
                await ensure_rtsp_forwarder()
            else:
                # Log keepalive packets (less frequently)
                logger.debug(f"[HolePunch] Keepalive from {client_ip}:{client_port}")
                
        except asyncio.TimeoutError:
            # Normal - no packets received, continue waiting
            # Also check for stale clients periodically
            await cleanup_stale_clients()
        except BlockingIOError:
            await asyncio.sleep(0.001)
        except Exception as e:
            logger.error(f"Hole punch receiver error: {e}")
            await asyncio.sleep(0.1)


async def cleanup_stale_clients():
    """Remove stale clients and stop video sender if no clients remain."""
    global active_punch_clients, fec_sender_process, rtp_forwarder
    
    if not active_punch_clients:
        return
    
    now = time.time()
    stale_clients = [
        client for client, last_time in active_punch_clients.items()
        if now - last_time > 30
    ]
    
    for client in stale_clients:
        del active_punch_clients[client]
        logger.info(f"Removed stale client: {client}")
    
    # Stop video sender if no clients remain
    if not active_punch_clients:
        logger.info("No active clients, stopping video sender")
        if USE_FEC_SENDER:
            await stop_fec_sender()
            await reopen_hole_punch_socket()
        elif rtp_forwarder and rtp_forwarder.running:
            await rtp_forwarder.stop()


# ----- Main -----

async def main():
    global telemetry_task, gps_task, imu_task, hall_sensor, traction_ctrl
    global abs_ctrl, throttle_tracker, hill_hold_ctrl, coast_ctrl, surface_adapt
    
    # Load revoked tokens from file
    load_revoked_tokens()
    
    # Load TURN credentials from mediamtx config
    load_turn_credentials()
    
    # Setup headlight GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(HEADLIGHT_GPIO_PIN, GPIO.OUT)
    GPIO.output(HEADLIGHT_GPIO_PIN, GPIO.LOW)  # Start with headlights off
    logger.info(f"Headlight GPIO {HEADLIGHT_GPIO_PIN} initialized")
    
    # Start ESP32 beacon discovery
    asyncio.create_task(discover_esp32())
    
    # Start Hall sensor (wheel RPM)
    hall_sensor = HallRPM(gpio_pin=HALL_GPIO_PIN, magnets_per_rev=1, timeout=1.0)
    if hall_sensor.start():
        logger.info(f"Hall sensor started on GPIO {HALL_GPIO_PIN}")
    else:
        logger.warning("Hall sensor not available, using GPS speed only")
        hall_sensor = None
    
    # Initialize traction control - unified low-speed traction manager (disabled by default)
    traction_ctrl = LowSpeedTractionManager()
    traction_ctrl.enabled = False  # Must be enabled via admin API
    logger.info("Low-speed traction manager initialized (disabled by default)")
    
    # Initialize yaw-rate stability control (disabled by default)
    # Wheelbase: ARRMA Big Rock 3S ≈ 320mm
    global stability_ctrl, slip_watchdog, steering_shaper
    stability_ctrl = YawRateController(wheelbase_m=0.32)
    stability_ctrl.enabled = False  # Must be enabled via admin API
    logger.info("Stability control initialized (disabled by default)")
    
    # Initialize slip angle watchdog (shares enable with stability control)
    slip_watchdog = SlipAngleWatchdog()
    slip_watchdog.enabled = False
    logger.info("Slip angle watchdog initialized (disabled by default)")
    
    # Initialize steering shaper (shares enable with stability control)
    steering_shaper = SteeringShaper()
    steering_shaper.enabled = False
    logger.info("Steering shaper initialized (disabled by default)")
    
    # Initialize ABS controller (disabled by default)
    abs_ctrl = ABSController()
    abs_ctrl.enabled = False
    throttle_tracker = ThrottleStateTracker()
    logger.info("ABS controller initialized (disabled by default)")
    
    # Initialize hill hold (disabled by default)
    hill_hold_ctrl = HillHold()
    hill_hold_ctrl.enabled = False
    logger.info("Hill hold initialized (disabled by default)")
    
    # Initialize coast control (disabled by default)
    coast_ctrl = CoastControl()
    coast_ctrl.enabled = False
    logger.info("Coast control initialized (disabled by default)")
    
    # Initialize surface adaptation (disabled by default)
    surface_adapt = SurfaceAdaptation()
    surface_adapt.enabled = False
    logger.info("Surface adaptation initialized (disabled by default)")
    
    # Initialize direction estimator (always active when IMU is valid)
    direction_est = DirectionEstimator()
    logger.info("Direction estimator initialized")
    
    # Initialize UDP hole punch system
    try:
        await init_hole_punch_socket()
        # Start STUN refresh loop
        stun_refresh_task = asyncio.create_task(stun_refresh_loop())
        # Hole punch receiver and RTP forward will be started AFTER HTTP server
        logger.info("UDP hole punch socket initialized (tasks start after HTTP server)")
    except Exception as e:
        logger.warning(f"UDP hole punch initialization failed: {e}")
        logger.warning("Hole punch streaming will not be available")
    
    # Start GPS reader loop
    gps_task = asyncio.create_task(gps_reader_loop())
    
    # Start IMU (BNO055) reader loop
    imu_task = asyncio.create_task(imu_reader_loop())
    
    # Start WiFi/Internet quality monitor loop (1Hz)
    wifi_task = asyncio.create_task(wifi_monitor_loop())
    
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
    app.router.add_post("/admin/set-turbo", handle_set_turbo)
    app.router.add_options("/admin/set-turbo", handle_options)
    app.router.add_post("/admin/set-traction", handle_set_traction_control)
    app.router.add_options("/admin/set-traction", handle_options)
    
    # UDP hole punch routes (for IWA client direct streaming)
    app.router.add_post("/stream/punch", handle_stream_punch)
    app.router.add_options("/stream/punch", handle_options)
    app.router.add_get("/stream/status", handle_stream_status)
    app.router.add_post("/stream/stop", handle_stream_stop)
    app.router.add_options("/stream/stop", handle_options)
    
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
    logger.info(f"  POST /admin/set-traction        - Toggle traction control")
    logger.info(f"  POST /stream/punch?token=...    - UDP hole punch (IWA client)")
    logger.info(f"  GET  /stream/status             - Hole punch status")
    
    # Now start hole punch background tasks (after HTTP server is up)
    if hole_punch_sock:
        asyncio.create_task(hole_punch_receiver_loop())
        logger.info("Hole punch receiver started (RTSP forwarder starts on-demand)")
    
    # Keep running
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")

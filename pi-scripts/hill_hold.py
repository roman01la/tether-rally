#!/usr/bin/env python3
"""
Hill Hold / Incline Assist for ARRMA Big Rock 3S

Prevents rollback on slopes when driving over the internet with latency.
On slopes, the car rolls backward when throttle is released - the driver
over latency can't compensate quickly enough.

Uses IMU pitch angle to detect incline and automatically applies
counter-throttle to hold position.

Release Strategy (Hybrid):
1. IMMEDIATE: Strong throttle (>300) releases instantly
2. BLEND_UP: Accelerating uphill blends out quickly
3. BLEND_DOWN: Going downhill blends out slowly (controlled descent)
4. TIMEOUT: Auto-release after 30 seconds if no input

Note: Negative throttle = REVERSE, not brake (RC car ESC behavior)
- Positive pitch = nose up = apply positive throttle to hold
- Negative pitch = nose down = apply negative throttle to hold

Usage:
    from hill_hold import HillHold
    
    hill_hold = HillHold()
    
    # In control loop (20-50 Hz):
    modified_throttle = hill_hold.update(
        pitch_deg=imu_pitch,           # degrees from BNO055
        speed_kmh=fused_speed,         # km/h
        throttle_input=throttle,       # driver input
        timestamp=time.time()
    )
"""

import time
from car_config import get_config


class HillHold:
    """
    Hill hold with intelligent release strategy.
    
    Release modes:
    1. IMMEDIATE: Driver applies strong throttle = instant release
    2. GRADUAL_UP: Accelerating uphill = quick blend to driver control
    3. GRADUAL_DOWN: Going downhill = slow blend for controlled descent
    4. TIMEOUT: No input for extended period = auto-release
    """
    
    def __init__(self):
        # Load config from car profile
        cfg = get_config()
        
        # === Detection Thresholds ===
        self.PITCH_THRESHOLD_DEG = cfg.get_float('hill_hold', 'pitch_threshold_deg')
        self.SPEED_THRESHOLD_KMH = cfg.get_float('hill_hold', 'speed_threshold_kmh')
        # Config uses 0-1000 range, actual throttle is -32767 to 32767
        THROTTLE_SCALE = 32767 / 1000
        self.THROTTLE_DEADZONE = int(cfg.get_int('hill_hold', 'throttle_deadzone') * THROTTLE_SCALE)
        
        # === Hold Parameters ===
        self.HOLD_STRENGTH = int(cfg.get_int('hill_hold', 'hold_strength') * THROTTLE_SCALE)
        self.MAX_HOLD_FORCE = int(cfg.get_int('hill_hold', 'max_hold_force') * THROTTLE_SCALE)
        
        # === Release Parameters ===
        self.IMMEDIATE_RELEASE_THRESHOLD = int(cfg.get_int('hill_hold', 'immediate_release_threshold') * THROTTLE_SCALE)
        self.BLEND_RATE = cfg.get_float('hill_hold', 'blend_rate')
        self.TIMEOUT_SECONDS = cfg.get_float('hill_hold', 'timeout_s')
        
        # Settling time: car must be stationary for this long before hill hold activates
        # This prevents false activation from chassis tilt during acceleration
        self.SETTLING_TIME_S = cfg.get_float('hill_hold', 'settling_time_s', default=0.5)
        
        # === State ===
        self._active = False
        self._hold_force = 0                # Current hold force being applied
        self._blend_factor = 1.0            # 1.0 = full hold, 0.0 = driver control
        self._activation_time = 0.0
        self._pitch_at_activation = 0.0
        self._prev_time = time.time()
        self._stationary_since = None       # When car became stationary with neutral throttle
        
        # Diagnostics
        self.current_pitch = 0.0
        
        # Enable/disable
        self.enabled = True
    
    def _calculate_hold_force(self, pitch_deg: float) -> int:
        """
        Calculate required throttle to hold position on incline.
        
        Positive pitch = nose up = need positive (forward) throttle to hold
        Negative pitch = nose down = need negative (reverse) throttle to hold
        
        Args:
            pitch_deg: Pitch angle in degrees (positive = nose up)
        
        Returns:
            Throttle force to apply (-MAX to +MAX)
        """
        force = int(pitch_deg * self.HOLD_STRENGTH)
        return max(-self.MAX_HOLD_FORCE, min(self.MAX_HOLD_FORCE, force))
    
    def _should_activate(self, pitch_deg: float, speed_kmh: float, 
                         throttle_input: int, timestamp: float) -> bool:
        """
        Determine if hill hold should engage.
        
        Conditions:
        - On a significant incline
        - Nearly stopped
        - Throttle released (driver not actively controlling)
        - Stationary for settling time (prevents false trigger from chassis tilt)
        """
        stationary = abs(speed_kmh) < self.SPEED_THRESHOLD_KMH
        throttle_neutral = abs(throttle_input) < self.THROTTLE_DEADZONE
        on_incline = abs(pitch_deg) > self.PITCH_THRESHOLD_DEG
        
        # Track when car became stationary with neutral throttle
        if stationary and throttle_neutral:
            if self._stationary_since is None:
                self._stationary_since = timestamp
        else:
            self._stationary_since = None
            return False
        
        # Require settling time before activation
        # This filters out chassis pitch from acceleration/deceleration
        settled = (timestamp - self._stationary_since) >= self.SETTLING_TIME_S
        
        return on_incline and settled
    
    def _determine_release_mode(self, throttle_input: int, 
                                pitch_deg: float) -> str:
        """
        Determine how to release hill hold based on driver input.
        
        Args:
            throttle_input: Current driver throttle
            pitch_deg: Pitch angle at activation
        
        Returns:
            Release mode: "hold", "immediate", "blend_up", "blend_down"
        """
        if abs(throttle_input) < self.THROTTLE_DEADZONE:
            return "hold"
        
        # Strong input - immediate release regardless of direction
        if abs(throttle_input) > self.IMMEDIATE_RELEASE_THRESHOLD:
            return "immediate"
        
        # Check if throttle is fighting the hill or going with it
        throttle_direction = 1 if throttle_input > 0 else -1
        hill_direction = 1 if pitch_deg > 0 else -1  # Positive pitch = uphill
        
        # Driver wants to go uphill (fighting gravity)
        going_uphill = (throttle_direction == hill_direction)
        
        if going_uphill:
            # Driver is accelerating uphill - blend out quickly
            return "blend_up"
        else:
            # Driver is going downhill - blend out slowly (controlled descent)
            return "blend_down"
    
    def update(self, 
               pitch_deg: float,         # IMU pitch (positive = nose up)
               speed_kmh: float,         # Vehicle speed (km/h)
               throttle_input: int,      # Driver throttle (-32767 to 32767 or -1000 to 1000)
               timestamp: float = None) -> int:
        """
        Process throttle through hill hold system.
        
        Args:
            pitch_deg: Pitch angle from IMU (positive = nose up)
            speed_kmh: Vehicle speed (km/h)
            throttle_input: Driver throttle command
            timestamp: Current time (defaults to time.time())
        
        Returns:
            Modified throttle (may include hold force)
        """
        if timestamp is None:
            timestamp = time.time()
        
        if not self.enabled:
            self._active = False
            return throttle_input
        
        # Update timing
        dt = timestamp - self._prev_time
        self._prev_time = timestamp
        
        # Store for diagnostics
        self.current_pitch = pitch_deg
        
        # Check for activation
        if not self._active:
            if self._should_activate(pitch_deg, speed_kmh, throttle_input, timestamp):
                self._active = True
                self._blend_factor = 1.0
                self._activation_time = timestamp
                self._pitch_at_activation = pitch_deg
                self._hold_force = self._calculate_hold_force(pitch_deg)
                
            # Not active - pass through
            return throttle_input
        
        # === ACTIVE HILL HOLD ===
        
        # Check timeout
        if timestamp - self._activation_time > self.TIMEOUT_SECONDS:
            self._active = False
            return throttle_input
        
        # Check if we've started moving (driver overcame hold)
        if abs(speed_kmh) > self.SPEED_THRESHOLD_KMH * 2:
            self._active = False
            return throttle_input
        
        # Determine release mode based on driver input
        release_mode = self._determine_release_mode(throttle_input, self._pitch_at_activation)
        
        if release_mode == "immediate":
            # === IMMEDIATE RELEASE ===
            self._active = False
            return throttle_input
            
        elif release_mode == "hold":
            # === MAINTAIN HOLD ===
            return self._hold_force
            
        elif release_mode == "blend_up":
            # === BLEND OUT QUICKLY (going uphill) ===
            self._blend_factor = max(0, self._blend_factor - self.BLEND_RATE * 2)
            
        elif release_mode == "blend_down":
            # === BLEND OUT SLOWLY (controlled descent) ===
            self._blend_factor = max(0, self._blend_factor - self.BLEND_RATE * 0.5)
        
        # Check if blend complete
        if self._blend_factor <= 0:
            self._active = False
            return throttle_input
        
        # === BLENDED OUTPUT ===
        # Combine hold force with driver input based on blend factor
        blended = int(
            self._hold_force * self._blend_factor + 
            throttle_input * (1 - self._blend_factor)
        )
        
        # Clamp to valid range
        return max(-32767, min(32767, blended))
    
    def get_status(self) -> dict:
        """Get diagnostic status for telemetry."""
        return {
            "enabled": self.enabled,
            "active": self._active,
            "hold_force": self._hold_force,
            "blend_factor": round(self._blend_factor, 2),
            "pitch_at_activation": round(self._pitch_at_activation, 1),
            "current_pitch": round(self.current_pitch, 1),
        }
    
    def reset(self):
        """Reset state (call when race ends or connection resets)."""
        self._active = False
        self._hold_force = 0
        self._blend_factor = 1.0
        self._activation_time = 0.0
        self._pitch_at_activation = 0.0


# === Test / Demo ===

if __name__ == "__main__":
    hill_hold = HillHold()
    
    print("Hill Hold Simulation")
    print("=" * 50)
    
    # Simulate uphill scenario
    print("\nScenario: Stopped on 15° uphill")
    print("-" * 30)
    
    # Initial state - stopped on hill, no throttle
    for i in range(5):
        result = hill_hold.update(
            pitch_deg=15.0,
            speed_kmh=0.5,
            throttle_input=0,
            timestamp=time.time() + i * 0.1
        )
        status = hill_hold.get_status()
        print(f"  Throttle: 0 -> {result}, Active: {status['active']}, Hold: {status['hold_force']}")
    
    print("\n  Driver applies +200 throttle (uphill):")
    for i in range(10):
        result = hill_hold.update(
            pitch_deg=15.0,
            speed_kmh=0.5,
            throttle_input=200,
            timestamp=time.time() + 0.5 + i * 0.1
        )
        status = hill_hold.get_status()
        print(f"    Blend: {status['blend_factor']:.2f}, Output: {result}")
    
    hill_hold.reset()
    
    # Simulate downhill scenario
    print("\nScenario: Stopped on -10° downhill (nose down)")
    print("-" * 30)
    
    for i in range(5):
        result = hill_hold.update(
            pitch_deg=-10.0,
            speed_kmh=0.3,
            throttle_input=0,
            timestamp=time.time() + i * 0.1
        )
        status = hill_hold.get_status()
        print(f"  Throttle: 0 -> {result}, Active: {status['active']}, Hold: {status['hold_force']}")
    
    print("\n  Driver applies -150 throttle (downhill/reverse):")
    for i in range(15):
        result = hill_hold.update(
            pitch_deg=-10.0,
            speed_kmh=0.3,
            throttle_input=-150,
            timestamp=time.time() + 0.5 + i * 0.1
        )
        status = hill_hold.get_status()
        if i % 3 == 0:
            print(f"    Blend: {status['blend_factor']:.2f}, Output: {result}")

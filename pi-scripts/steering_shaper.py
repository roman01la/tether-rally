#!/usr/bin/env python3
"""
Latency-Aware Steering Shaper for ARRMA Big Rock 3S

Makes the car drivable over 100-200ms internet latency by:
1. Speed-based steering limit: reduces max steering at higher speeds
2. Rate limiting: prevents snap-oversteer from delayed human input
3. Counter-steer assist: gentle "save" when driver releases but car still rotating

This is the "biggest win" for remote driving - it prevents the delayed
human input from causing sudden, violent steering changes that the driver
can't react to in time.

Usage:
    from steering_shaper import SteeringShaper

    shaper = SteeringShaper()

    # In control loop (50Hz):
    shaped_steering = shaper.update(
        steering_input=raw_steering,  # -1000 to 1000
        speed=fused_speed,            # km/h
        yaw_rate=imu_yaw_rate         # deg/s
    )
"""

import time
import math


class SteeringShaper:
    """
    Latency-aware steering processor for remote driving.
    
    Smooths and limits steering to compensate for human reaction delay.
    """

    def __init__(self):
        # === Speed-Based Steering Limit ===
        
        # Steering range at low speed (fraction of full, 0-1)
        self.steering_limit_low_speed = 1.0    # 100% at low speed
        
        # Steering range at high speed (fraction of full, 0-1)
        self.steering_limit_high_speed = 0.5   # 50% at high speed (was 40%, more fun)
        
        # Speed thresholds for interpolation (km/h)
        self.limit_speed_low = 8.0    # Below this: full steering (raised)
        self.limit_speed_high = 40.0  # Above this: minimum steering (raised)
        
        # === Rate Limiting ===
        
        # Max steering change per second (in steering units, full range is ~65534)
        # At 50Hz, this is max_rate / 50 per update
        # 300000/sec = full sweep in ~0.2s - very responsive
        self.max_steering_rate = 300000  # units/sec (faster for responsive feel)
        
        # Faster rate limit for returning toward center (feels more natural)
        self.max_center_rate = 400000    # units/sec (faster return to center)
        
        # === Counter-Steer Assist ===
        
        # Enable/disable counter-steer assist
        self.counter_steer_enabled = True
        
        # Minimum yaw rate (deg/s) to trigger assist
        self.counter_steer_yaw_threshold = 20.0
        
        # Assist strength (0-1, fraction of "ideal" counter-steer)
        # 0.10 = 10% assist, very subtle - just a hint
        self.counter_steer_strength = 0.10
        
        # Only assist when input is near neutral (abs < this)
        self.neutral_threshold = 5000  # ~15% of full int16 range
        
        # Minimum speed for counter-steer assist
        self.counter_steer_min_speed = 10.0  # km/h (raised)
        
        # Max counter-steer assist (prevents over-correction)
        self.max_counter_steer = 5000  # ~15% of full int16 range
        
        # === Smoothing ===
        
        # Light smoothing on output (reduces jitter)
        self.output_smoothing = 0.7  # Higher = more responsive
        
        # === State ===
        
        self._prev_output = 0.0
        self._prev_time = time.time()
        
        # Diagnostics
        self.steering_limit = 1.0
        self.rate_limited = False
        self.counter_steer_active = False
        self.counter_steer_amount = 0
        
        # Enable/disable
        self.enabled = True

    def update(self,
               steering_input: int,   # -1000 to 1000
               speed: float,          # km/h
               yaw_rate: float) -> int:  # deg/s
        """
        Process steering input with latency-aware shaping.
        
        Args:
            steering_input: Raw steering command (-1000 to 1000)
            speed: Vehicle speed (km/h)
            yaw_rate: Gyro Z yaw rate (deg/s, positive = CCW)
        
        Returns:
            Shaped steering command (-1000 to 1000)
        """
        if not self.enabled:
            return steering_input
        
        now = time.time()
        dt = now - self._prev_time
        self._prev_time = now
        dt = max(0.001, min(0.1, dt))  # Clamp dt
        
        # Start with raw input as float
        steering = float(steering_input)
        
        # 1. Apply speed-based steering limit
        steering = self._apply_speed_limit(steering, speed)
        
        # 2. Add counter-steer assist (before rate limiting)
        steering = self._apply_counter_steer_assist(steering, speed, yaw_rate)
        
        # 3. Apply rate limiting
        steering = self._apply_rate_limit(steering, dt)
        
        # 4. Light output smoothing
        steering = self._prev_output + self.output_smoothing * (steering - self._prev_output)
        self._prev_output = steering
        
        # Clamp and return as int (full int16 range)
        return int(max(-32767, min(32767, steering)))

    def _apply_speed_limit(self, steering: float, speed: float) -> float:
        """
        Reduce steering range at higher speeds.
        """
        if speed <= self.limit_speed_low:
            limit = self.steering_limit_low_speed
        elif speed >= self.limit_speed_high:
            limit = self.steering_limit_high_speed
        else:
            # Linear interpolation
            t = (speed - self.limit_speed_low) / (self.limit_speed_high - self.limit_speed_low)
            limit = self.steering_limit_low_speed + t * (self.steering_limit_high_speed - self.steering_limit_low_speed)
        
        self.steering_limit = limit
        
        # Scale steering by limit
        return steering * limit

    def _apply_counter_steer_assist(self, steering: float, speed: float, yaw_rate: float) -> float:
        """
        Gently assist counter-steer when driver releases but car still rotating.
        """
        self.counter_steer_active = False
        self.counter_steer_amount = 0
        
        if not self.counter_steer_enabled:
            return steering
        
        # Only assist at speed
        if speed < self.counter_steer_min_speed:
            return steering
        
        # Only assist when input is near neutral
        if abs(steering) > self.neutral_threshold:
            return steering
        
        # Only assist when car is rotating significantly
        if abs(yaw_rate) < self.counter_steer_yaw_threshold:
            return steering
        
        # Calculate assist: steer opposite to rotation
        # yaw_rate > 0 (CCW) → steer right (positive) to counter
        # yaw_rate < 0 (CW) → steer left (negative) to counter
        
        # Scale assist by rotation rate (more rotation = more assist)
        yaw_factor = min(1.0, abs(yaw_rate) / 60.0)  # Full assist at 60 deg/s
        
        assist = -yaw_rate * self.counter_steer_strength * yaw_factor * 10
        
        # Clamp assist
        assist = max(-self.max_counter_steer, min(self.max_counter_steer, assist))
        
        self.counter_steer_active = True
        self.counter_steer_amount = int(assist)
        
        return steering + assist

    def _apply_rate_limit(self, steering: float, dt: float) -> float:
        """
        Limit how fast steering can change.
        """
        self.rate_limited = False
        
        delta = steering - self._prev_output
        
        # Use faster rate when returning toward center
        going_to_center = abs(steering) < abs(self._prev_output)
        max_rate = self.max_center_rate if going_to_center else self.max_steering_rate
        
        max_delta = max_rate * dt
        
        if abs(delta) > max_delta:
            self.rate_limited = True
            if delta > 0:
                steering = self._prev_output + max_delta
            else:
                steering = self._prev_output - max_delta
        
        return steering

    def get_status(self) -> dict:
        """Get diagnostic status."""
        return {
            "enabled": self.enabled,
            "steering_limit": round(self.steering_limit, 2),
            "rate_limited": self.rate_limited,
            "counter_steer_active": self.counter_steer_active,
            "counter_steer_amount": self.counter_steer_amount,
        }

    def reset(self):
        """Reset state."""
        self._prev_output = 0.0
        self.rate_limited = False
        self.counter_steer_active = False
        self.counter_steer_amount = 0


# === Test / Demo ===

if __name__ == "__main__":
    shaper = SteeringShaper()

    print("Steering Shaper Simulation")
    print("=" * 60)

    # Test 1: Speed-based limit
    print("\n1. Speed-based steering limit:")
    for speed in [0, 10, 20, 30, 40]:
        result = shaper.update(steering_input=1000, speed=speed, yaw_rate=0)
        print(f"   Speed {speed:2d} km/h, input=1000 → output={result} (limit={shaper.steering_limit:.2f})")
        shaper.reset()

    # Test 2: Rate limiting
    print("\n2. Rate limiting (sudden input):")
    shaper.reset()
    print("   Sudden full-left at 20 km/h:")
    for i in range(10):
        result = shaper.update(steering_input=-1000, speed=20, yaw_rate=0)
        print(f"   Frame {i+1}: output={result:5d}, rate_limited={shaper.rate_limited}")

    # Test 3: Counter-steer assist
    print("\n3. Counter-steer assist (neutral input, car rotating):")
    shaper.reset()
    shaper._prev_output = 0  # Ensure starting from center
    print("   Input=0, car rotating CCW at 40°/s:")
    for i in range(5):
        result = shaper.update(steering_input=0, speed=20, yaw_rate=40)
        status = shaper.get_status()
        print(f"   Frame {i+1}: output={result:4d}, assist={status['counter_steer_amount']:4d}, active={status['counter_steer_active']}")

    # Test 4: Combined scenario - high speed turn release
    print("\n4. High-speed turn release (realistic scenario):")
    shaper.reset()
    print("   Setup: turning right at 30 km/h, then release:")
    # First, establish a right turn
    for i in range(10):
        shaper.update(steering_input=500, speed=30, yaw_rate=-30)
    print(f"   After turning: output={shaper._prev_output:.0f}")
    
    # Now release (car still rotating)
    print("   Release to neutral (car still rotating -40°/s):")
    for i in range(10):
        result = shaper.update(steering_input=0, speed=30, yaw_rate=-40)
        status = shaper.get_status()
        print(f"   Frame {i+1}: output={result:4d}, assist={status['counter_steer_amount']:4d}")

#!/usr/bin/env python3
"""
Slip Angle Watchdog for ARRMA Big Rock 3S

Detects when the car is sliding/drifting by comparing:
- heading_imu: where the car is POINTING (mag/gyro fused)
- course_gps: where the car is MOVING (GPS track angle)

When these differ significantly for a sustained period, the car is sliding.
This watchdog gently reduces throttle to help regain grip.

Design Philosophy:
- "Feel-good stability, not a nanny"
- Only kicks in when clearly sliding (high slip angle for sustained time)
- Gentle intervention: gradual throttle reduction, not hard cut
- Fast recovery when grip is regained
- Disabled at low speeds (GPS course unreliable)

Works alongside YawRateController:
- YawRateController: fast reaction to rotation errors (10-20ms)
- SlipAngleWatchdog: catches sustained drift/slide (100-300ms)

Usage:
    from slip_angle_watchdog import SlipAngleWatchdog

    saw = SlipAngleWatchdog()

    # In control loop (10-20Hz is fine, doesn't need IMU rate):
    saw.update(
        heading_imu=blended_heading,  # degrees, 0=North
        course_gps=gps_heading,       # degrees, 0=North (track angle)
        speed=fused_speed,            # km/h
        throttle_input=throttle       # -1000 to 1000
    )

    # Get throttle multiplier
    limited_throttle = int(throttle * saw.get_throttle_multiplier())
"""

import time
import math


class SlipAngleWatchdog:
    """
    Slip angle watchdog for drift/slide detection.
    
    Compares IMU heading (where car points) vs GPS course (where car moves).
    Large difference = sliding. Sustained sliding = reduce throttle.
    """

    def __init__(self):
        # === Thresholds ===
        
        # Minimum speed (km/h) for watchdog to be active
        # GPS course is unreliable below this
        self.min_speed_kmh = 8.0
        
        # Slip angle threshold (degrees) to trigger watchdog
        # Below this, car is considered "on track"
        self.slip_angle_threshold = 35.0  # Raised - allow more drift before intervening
        
        # Time (seconds) slip must persist before intervention
        # Prevents reacting to brief transients - longer = more fun
        self.slip_duration_threshold = 0.25  # 250ms - allow quick slides
        
        # Minimum throttle to consider intervention
        # No point cutting throttle if already coasting
        self.min_throttle_for_intervention = 300  # Raised threshold
        
        # === Throttle Control ===
        
        # Target throttle multiplier during slip recovery
        # Not zero - we want "grip recovery" not "emergency stop"
        self.recovery_target = 0.6  # 60% throttle during recovery (more power)
        
        # How fast to reduce throttle (per update, ~10Hz assumed)
        # Soft ramp: 0.04/update = 0.4/sec = 400ms to reach target
        self.reduction_rate = 0.04  # 4% per cycle - smooth ramp
        
        # How fast to recover when slip ends - fast recovery feels better
        self.recovery_rate = 0.08   # 8% per cycle - quick recovery
        
        # Minimum multiplier (floor)
        self.min_multiplier = 0.5   # 50% floor - keep some power
        
        # === State ===
        
        self._throttle_multiplier = 1.0
        self._slip_start_time = None  # When slip angle first exceeded threshold
        self._intervention_active = False
        self._prev_time = time.time()
        
        # Smoothed slip angle (reduces noise)
        self._slip_angle_smooth = 0.0
        self.smoothing_alpha = 0.3
        
        # Diagnostics
        self.slip_angle = 0.0
        self.slip_duration = 0.0
        self.intervention_active = False
        
        # Enable/disable
        self.enabled = True

    def update(self,
               heading_imu: float,    # degrees, 0=North (where car points)
               course_gps: float,     # degrees, 0=North (where car moves)
               speed: float,          # km/h
               throttle_input: int):  # -1000 to 1000
        """
        Update watchdog state. Call at 10-20Hz.
        
        Args:
            heading_imu: IMU/mag fused heading (degrees, 0=North)
            course_gps: GPS track angle (degrees, 0=North)
            speed: Vehicle speed (km/h)
            throttle_input: Current throttle command
        """
        now = time.time()
        self._prev_time = now
        
        # Calculate slip angle (angular difference, handling wrap-around)
        raw_slip = self._angle_diff(heading_imu, course_gps)
        
        # Smooth the slip angle
        self._slip_angle_smooth += self.smoothing_alpha * (raw_slip - self._slip_angle_smooth)
        self.slip_angle = self._slip_angle_smooth
        
        # Check conditions for intervention
        speed_ok = speed >= self.min_speed_kmh
        throttle_ok = throttle_input >= self.min_throttle_for_intervention
        slip_high = abs(self.slip_angle) > self.slip_angle_threshold
        
        if speed_ok and throttle_ok and slip_high:
            # Slip detected
            if self._slip_start_time is None:
                self._slip_start_time = now
            
            self.slip_duration = now - self._slip_start_time
            
            # Check if sustained long enough
            if self.slip_duration >= self.slip_duration_threshold:
                self._apply_intervention()
        else:
            # No slip or conditions not met
            self._slip_start_time = None
            self.slip_duration = 0.0
            self._recover()
        
        self.intervention_active = self._intervention_active

    def _angle_diff(self, a: float, b: float) -> float:
        """
        Calculate signed angular difference (a - b), handling wrap-around.
        Result is in range [-180, 180].
        """
        diff = a - b
        # Normalize to [-180, 180]
        while diff > 180:
            diff -= 360
        while diff < -180:
            diff += 360
        return diff

    def _apply_intervention(self):
        """Gradually reduce throttle toward recovery target."""
        self._intervention_active = True
        
        target = max(self.min_multiplier, self.recovery_target)
        
        if self._throttle_multiplier > target:
            self._throttle_multiplier = max(
                target,
                self._throttle_multiplier - self.reduction_rate
            )

    def _recover(self):
        """Gradually restore throttle when slip ends."""
        if not self._intervention_active:
            self._throttle_multiplier = 1.0
            return
        
        self._throttle_multiplier = min(1.0, self._throttle_multiplier + self.recovery_rate)
        
        if self._throttle_multiplier >= 1.0:
            self._intervention_active = False

    def get_throttle_multiplier(self) -> float:
        """Get current throttle multiplier (0.0 to 1.0)."""
        if not self.enabled:
            return 1.0
        return self._throttle_multiplier

    def apply_to_throttle(self, throttle: int) -> int:
        """
        Apply watchdog to throttle command.
        Only affects positive (forward) throttle.
        """
        if not self.enabled or throttle <= 0:
            return throttle
        return int(throttle * self._throttle_multiplier)

    def get_status(self) -> dict:
        """Get diagnostic status."""
        return {
            "enabled": self.enabled,
            "slip_angle": round(self.slip_angle, 1),
            "slip_duration": round(self.slip_duration * 1000),  # ms
            "intervention_active": self.intervention_active,
            "throttle_multiplier": round(self._throttle_multiplier, 2),
        }

    def reset(self):
        """Reset state."""
        self._throttle_multiplier = 1.0
        self._slip_start_time = None
        self._intervention_active = False
        self._slip_angle_smooth = 0.0
        self.slip_angle = 0.0
        self.slip_duration = 0.0
        self.intervention_active = False


# === Test / Demo ===

if __name__ == "__main__":
    import random

    saw = SlipAngleWatchdog()

    print("Slip Angle Watchdog Simulation")
    print("=" * 60)

    # Simulate scenarios
    scenarios = [
        ("Straight driving, aligned",
         {"heading": 90.0, "course": 90.0, "speed": 25.0, "throttle": 600}),
        ("Slight angle (normal cornering)",
         {"heading": 95.0, "course": 85.0, "speed": 20.0, "throttle": 500}),
        ("Moderate drift (power slide)",
         {"heading": 120.0, "course": 85.0, "speed": 22.0, "throttle": 700}),
        ("Heavy drift (almost sideways)",
         {"heading": 150.0, "course": 90.0, "speed": 18.0, "throttle": 800}),
        ("Spin out (going backwards-ish)",
         {"heading": 270.0, "course": 90.0, "speed": 15.0, "throttle": 500}),
        ("Low speed (watchdog inactive)",
         {"heading": 120.0, "course": 80.0, "speed": 5.0, "throttle": 600}),
    ]

    for name, params in scenarios:
        print(f"\n{name}:")
        print(f"  Heading: {params['heading']}°, Course: {params['course']}°, "
              f"Speed: {params['speed']} km/h")

        # Run enough iterations to exceed duration threshold
        for i in range(25):  # ~2.5 seconds at 10Hz
            saw.update(
                heading_imu=params["heading"] + random.uniform(-2, 2),
                course_gps=params["course"] + random.uniform(-2, 2),
                speed=params["speed"],
                throttle_input=params["throttle"],
            )
            # Simulate 10Hz
            import time as t
            t.sleep(0.01)  # Speed up for test

        status = saw.get_status()
        print(f"  Slip angle: {status['slip_angle']:.1f}°")
        print(f"  Slip duration: {status['slip_duration']}ms")
        print(f"  Intervention: {status['intervention_active']}")
        print(f"  Throttle mult: {status['throttle_multiplier']}")

        # Reset for next scenario
        saw.reset()

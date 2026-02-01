#!/usr/bin/env python3
"""
Slip Watchdog for ARRMA Big Rock 3S (IMU-Based)

Detects when the car is sliding/drifting using pure IMU data:
- lateral_accel: measured lateral acceleration from accelerometer (m/s²)
- yaw_rate: rotation rate from gyroscope (deg/s)
- speed: vehicle speed (km/h)

Slip detection principle:
    Expected lateral acceleration during a turn: a_lat_expected = v * ω
    (where v = velocity, ω = yaw rate in rad/s)
    
    If actual lateral acceleration significantly exceeds expected,
    the rear is sliding out (oversteer/drift).

This approach is MORE RELIABLE than GPS-based slip detection because:
- Works at any speed (no minimum speed threshold for GPS course)
- No GPS latency or course "hunting" issues
- Direct measurement of actual vehicle dynamics
- 50Hz update rate vs GPS's 10Hz

Design Philosophy:
- "Feel-good stability, not a nanny"
- Only kicks in when clearly sliding (high slip for sustained time)
- Gentle intervention: gradual throttle reduction, not hard cut
- Fast recovery when grip is regained

Works alongside YawRateController:
- YawRateController: fast reaction to rotation errors (10-20ms)
- SlipWatchdog: catches sustained excessive lateral acceleration (100-300ms)

Usage:
    from slip_angle_watchdog import SlipAngleWatchdog

    saw = SlipAngleWatchdog()

    # In control loop (20-50Hz for best results):
    saw.update(
        lateral_accel=imu_lateral_accel,  # m/s² (positive = right)
        yaw_rate=imu_yaw_rate,            # deg/s (positive = CCW/left)
        speed=fused_speed,                # km/h
        throttle_input=throttle           # -1000 to 1000
    )

    # Get throttle multiplier
    limited_throttle = int(throttle * saw.get_throttle_multiplier())
"""

import time
import math
from car_config import get_config


class SlipAngleWatchdog:
    """
    IMU-based slip watchdog for drift/slide detection.
    
    Compares expected lateral acceleration (from speed × yaw rate)
    vs actual lateral acceleration from IMU.
    
    Large excess lateral acceleration = sliding.
    Sustained sliding = reduce throttle.
    """

    def __init__(self):
        # Load config from car profile
        cfg = get_config()
        
        # === Thresholds ===
        self.min_speed_kmh = cfg.get_float('slip_angle_watchdog', 'min_speed_kmh')
        self.lateral_excess_threshold = cfg.get_float('slip_angle_watchdog', 'lateral_excess_threshold')
        self.slip_duration_threshold = cfg.get_float('slip_angle_watchdog', 'slip_duration_threshold_s')
        self.min_throttle_for_intervention = cfg.get_int('slip_angle_watchdog', 'min_throttle_for_intervention')
        
        # === Throttle Control ===
        self.recovery_target = cfg.get_float('slip_angle_watchdog', 'recovery_target')
        self.reduction_rate = cfg.get_float('slip_angle_watchdog', 'reduction_rate')
        self.recovery_rate = cfg.get_float('slip_angle_watchdog', 'recovery_rate')
        self.min_multiplier = cfg.get_float('slip_angle_watchdog', 'min_multiplier')
        
        # === State ===
        
        self._throttle_multiplier = 1.0
        self._slip_start_time = None  # When slip first detected
        self._intervention_active = False
        self._prev_time = time.time()
        
        # Smoothed values (reduces noise)
        self._lateral_excess_smooth = 0.0
        self.smoothing_alpha = cfg.get_float('slip_angle_watchdog', 'smoothing_alpha')
        
        # Diagnostics
        self.lateral_excess = 0.0      # Excess lateral accel (m/s²)
        self.expected_lateral = 0.0    # Expected lateral accel (m/s²)
        self.actual_lateral = 0.0      # Actual lateral accel (m/s²)
        self.slip_detected = False     # Currently detecting slip
        self.slip_duration = 0.0       # How long slip has been detected
        self.intervention_active = False
        
        # Legacy compatibility (slip_angle now represents excess in different units)
        self.slip_angle = 0.0  # Will store lateral_excess for backward compat
        
        # Enable/disable
        self.enabled = True

    def update(self,
               lateral_accel: float,   # m/s² from accelerometer (positive = right)
               yaw_rate: float,        # deg/s from gyro (positive = CCW/left turn)
               speed: float,           # km/h
               throttle_input: int):   # -1000 to 1000
        """
        Update watchdog state. Call at 20-50Hz.
        
        Args:
            lateral_accel: Lateral acceleration from IMU (m/s², positive = right)
            yaw_rate: Yaw rate from gyro (deg/s, positive = left/CCW)
            speed: Vehicle speed (km/h)
            throttle_input: Current throttle command
        """
        now = time.time()
        self._prev_time = now
        
        # Store actual lateral for diagnostics
        self.actual_lateral = lateral_accel
        
        # Calculate expected lateral acceleration from circular motion
        # a_lat = v * ω (where v is in m/s, ω is in rad/s)
        # Convert units: speed km/h -> m/s, yaw_rate deg/s -> rad/s
        v_ms = speed / 3.6
        omega_rad = math.radians(yaw_rate)
        
        # Expected lateral acceleration magnitude
        # Note: Sign convention - turning left (positive yaw) creates rightward lateral accel
        # So expected lateral has opposite sign to yaw rate
        self.expected_lateral = abs(v_ms * omega_rad)
        
        # Calculate excess: how much more lateral accel than expected
        # Use absolute values - we care about magnitude of slip, not direction
        # Excess = |actual| - |expected|
        raw_excess = abs(lateral_accel) - self.expected_lateral
        
        # Smooth the excess value
        self._lateral_excess_smooth += self.smoothing_alpha * (raw_excess - self._lateral_excess_smooth)
        self.lateral_excess = self._lateral_excess_smooth
        
        # For backward compatibility, store as "slip_angle" (scaled for display)
        # Convert m/s² excess to pseudo-degrees (rough approximation for UI)
        self.slip_angle = self.lateral_excess * 10  # ~10 deg per m/s² excess
        
        # Check conditions for intervention
        speed_ok = speed >= self.min_speed_kmh
        throttle_ok = throttle_input >= self.min_throttle_for_intervention
        slip_high = self.lateral_excess > self.lateral_excess_threshold
        
        self.slip_detected = speed_ok and slip_high
        
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
            "lateral_excess": round(self.lateral_excess, 2),
            "expected_lateral": round(self.expected_lateral, 2),
            "actual_lateral": round(self.actual_lateral, 2),
            "slip_detected": self.slip_detected,
            "slip_duration": round(self.slip_duration * 1000),  # ms
            "intervention_active": self.intervention_active,
            "throttle_multiplier": round(self._throttle_multiplier, 2),
            # Legacy field for backward compatibility
            "slip_angle": round(self.slip_angle, 1),
        }

    def reset(self):
        """Reset state."""
        self._throttle_multiplier = 1.0
        self._slip_start_time = None
        self._intervention_active = False
        self._lateral_excess_smooth = 0.0
        self.lateral_excess = 0.0
        self.expected_lateral = 0.0
        self.actual_lateral = 0.0
        self.slip_detected = False
        self.slip_duration = 0.0
        self.intervention_active = False
        self.slip_angle = 0.0


# === Test / Demo ===

if __name__ == "__main__":
    import random

    saw = SlipAngleWatchdog()

    print("IMU-Based Slip Watchdog Simulation")
    print("=" * 60)

    # Simulate scenarios
    # lateral_accel: m/s² (positive = right)
    # yaw_rate: deg/s (positive = left turn)
    # speed: km/h
    scenarios = [
        ("Straight driving (no lateral accel)",
         {"lateral": 0.2, "yaw": 0.0, "speed": 25.0, "throttle": 600}),
        
        ("Normal cornering (lateral matches yaw)",
         # At 20 km/h (5.56 m/s), turning at 30 deg/s (0.52 rad/s)
         # Expected lateral = 5.56 * 0.52 = 2.9 m/s²
         {"lateral": 3.0, "yaw": 30.0, "speed": 20.0, "throttle": 500}),
        
        ("Power slide (excess lateral accel)",
         # Same turn rate, but actual lateral is much higher
         {"lateral": 7.0, "yaw": 30.0, "speed": 20.0, "throttle": 700}),
        
        ("Heavy drift (very high excess)",
         {"lateral": 10.0, "yaw": 25.0, "speed": 18.0, "throttle": 800}),
        
        ("Spin out (high lateral, low speed)",
         {"lateral": 8.0, "yaw": 60.0, "speed": 12.0, "throttle": 500}),
        
        ("Low speed (watchdog less sensitive)",
         {"lateral": 5.0, "yaw": 20.0, "speed": 4.0, "throttle": 600}),
    ]

    for name, params in scenarios:
        print(f"\n{name}:")
        print(f"  Lateral: {params['lateral']:.1f} m/s², Yaw: {params['yaw']:.1f} deg/s, "
              f"Speed: {params['speed']} km/h")

        # Run enough iterations to exceed duration threshold
        for i in range(20):  # ~1 second at 20Hz
            saw.update(
                lateral_accel=params["lateral"] + random.uniform(-0.3, 0.3),
                yaw_rate=params["yaw"] + random.uniform(-2, 2),
                speed=params["speed"],
                throttle_input=params["throttle"],
            )
            # Simulate 20Hz
            import time as t
            t.sleep(0.01)  # Speed up for test

        status = saw.get_status()
        print(f"  Expected lateral: {status['expected_lateral']:.2f} m/s²")
        print(f"  Actual lateral: {status['actual_lateral']:.2f} m/s²")
        print(f"  Lateral excess: {status['lateral_excess']:.2f} m/s²")
        print(f"  Slip detected: {status['slip_detected']}")
        print(f"  Slip duration: {status['slip_duration']}ms")
        print(f"  Intervention: {status['intervention_active']}")
        print(f"  Throttle mult: {status['throttle_multiplier']}")

        # Reset for next scenario
        saw.reset()

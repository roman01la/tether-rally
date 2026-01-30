#!/usr/bin/env python3
"""
Traction Control System for ARRMA Big Rock 3S

Tuned for 1:10 scale monster truck with:
- Spektrum Firma 3660 3200Kv brushless motor (~500W peak)
- 4WD drivetrain, 10.83:1 final drive
- dBoots RAGNAROK MT tires (118mm diameter)
- ~2.84 kg weight
- SAFETY LIMITED TO 50% THROTTLE (~25 mph / 40 km/h max)

Design Philosophy:
- This is a BASHER truck, not a race car - keep it FUN
- Allow controlled wheelspin for launches and powerslides
- Only intervene on severe, uncontrollable wheelspin
- Gentle throttle reduction (max 35% cut), fast recovery
- Disable in tight turns to allow donuts/drifts

Slip Detection (IMU-primary, no GPS real-time dependency):
1. PRIMARY: Acceleration mismatch - wheel accelerates but vehicle doesn't
2. SECONDARY: Sustained speed divergence - wheel speed diverges from IMU-estimated ground speed

NOTE: GPS is NOT used for real-time slip detection due to 300-500ms latency.
Ground speed is estimated from IMU forward acceleration integration.

Control action: soft throttle reduction with gradual recovery.

Usage:
    from traction_control import TractionControl
    
    tc = TractionControl()
    
    # In control loop (50-100Hz):
    tc.update(
        imu_forward_accel=accel_x,  # m/s² (positive = forward)
        imu_yaw_rate=yaw_rate,      # deg/s
        wheel_speed=wheel_kmh,       # km/h from hall sensor
        gps_speed=gps_kmh,           # km/h from GPS (for slow drift correction only)
        gps_valid=has_fix,
        throttle_input=throttle      # -1000 to 1000
    )
    
    # Get throttle multiplier (0.0 to 1.0)
    limited_throttle = throttle * tc.get_throttle_multiplier()
"""

import time
import math


class TractionControl:
    """
    Traction control for ARRMA Big Rock 3S monster truck.
    
    Tuned for fun bashing while preventing uncontrollable wheelspin.
    
    Architecture (IMU-primary, GPS-free real-time):
    1. Ground speed estimator: IMU forward accel integration (no GPS latency)
    2. Slip detection: acceleration mismatch (primary) + speed divergence (secondary)
    3. Control: gentle throttle limiting (max 35% cut) with fast recovery
    
    GPS is only used for very slow drift correction of the ground speed estimate,
    not for real-time slip detection.
    """
    
    def __init__(self):
        # === Tuning Parameters ===
        # Tuned for ARRMA Big Rock 3S (1:10 monster truck, 3200Kv brushless)
        # NOTE: Speed is hard-limited to 50% for safety (~25 mph max)
        # Goal: Prevent uncontrollable wheelspin while keeping driving FUN
        
        # Ground speed estimator (IMU-based, GPS for slow drift correction only)
        self.GPS_DRIFT_CORRECTION_ALPHA = 0.01  # Very slow GPS correction (1% per update)
        self.GPS_DRIFT_CORRECTION_MIN_SPEED = 5.0  # m/s - only correct above this
        
        # Slip detection thresholds
        # At 50% power limit, wheel acceleration is ~half of full power
        self.WHEEL_ACCEL_THRESHOLD = 3.0   # m/s² - wheel accel above this = potential slip
        self.VEHICLE_ACCEL_THRESHOLD = 1.0 # m/s² - if vehicle accel below this during wheel accel = slip
        self.MIN_THROTTLE_FOR_SLIP = 13000 # Minimum throttle (out of 32767) to consider slip (~40%)
        self.YAW_RATE_THRESHOLD = 90.0     # deg/s - in tight turns/donuts, disable slip detection
        
        # Speed divergence detection (secondary method, replaces GPS slip ratio)
        # If wheel speed exceeds estimated ground speed significantly = slip
        self.SPEED_DIVERGENCE_THRESHOLD = 0.4  # 40% divergence triggers
        self.SPEED_DIVERGENCE_MIN_SPEED = 3.0  # m/s - only use above this speed
        
        # Throttle control - GENTLE intervention to keep it fun
        self.THROTTLE_CUT_RATE = 0.08      # How much to cut per slip detection (8% per cycle)
        self.THROTTLE_MIN_MULTIPLIER = 0.70 # Never cut below 70% - at 50% limit, this = 35% total
        self.THROTTLE_RECOVERY_RATE = 0.006 # Recovery per update (~0.3/s at 50Hz)
        self.THROTTLE_FAST_RECOVERY_RATE = 0.02 # Faster recovery when clearly no slip
        
        # Smoothing
        self.ACCEL_SMOOTHING = 0.3         # Exponential smoothing for accelerations
        self.SPEED_SMOOTHING = 0.2         # Smoothing for speed estimates
        
        # === State ===
        
        # Ground speed estimate (m/s)
        self.estimated_ground_speed = 0.0
        
        # Previous values for derivative calculation
        self._prev_wheel_speed = 0.0       # m/s
        self._prev_time = time.time()
        
        # Smoothed accelerations
        self._wheel_accel_smooth = 0.0     # m/s²
        self._vehicle_accel_smooth = 0.0   # m/s²
        
        # Throttle control state
        self._throttle_multiplier = 1.0
        self._slip_active = False
        self._slip_start_time = 0.0
        
        # Diagnostics
        self.slip_detected = False
        self.slip_reason = ""
        self.wheel_accel = 0.0
        self.vehicle_accel = 0.0
        self.slip_ratio = 0.0
        
        # Enable/disable
        self.enabled = True
    
    def update(self, 
               imu_forward_accel: float,  # m/s² (positive = forward acceleration)
               imu_yaw_rate: float,       # deg/s
               wheel_speed: float,        # km/h
               gps_speed: float,          # km/h
               gps_valid: bool,
               throttle_input: int):      # -1000 to 1000
        """
        Update traction control state. Call at 50-100Hz.
        
        Args:
            imu_forward_accel: Forward acceleration from IMU (m/s², positive = accelerating)
            imu_yaw_rate: Yaw rate from gyro (deg/s)
            wheel_speed: Wheel speed from hall sensor (km/h)
            gps_speed: GPS speed (km/h)
            gps_valid: Whether GPS has valid fix
            throttle_input: Current throttle command (-1000 to 1000)
        """
        now = time.time()
        dt = now - self._prev_time
        self._prev_time = now
        
        # Clamp dt to avoid crazy values on first call or lag spikes
        dt = max(0.001, min(0.1, dt))
        
        # Convert speeds to m/s for calculations
        wheel_speed_ms = wheel_speed / 3.6
        gps_speed_ms = gps_speed / 3.6
        
        # === 1. Update ground speed estimate (complementary filter) ===
        self._update_ground_speed_estimate(imu_forward_accel, gps_speed_ms, gps_valid, dt)
        
        # === 2. Calculate wheel acceleration ===
        wheel_accel_raw = (wheel_speed_ms - self._prev_wheel_speed) / dt
        self._prev_wheel_speed = wheel_speed_ms
        
        # Smooth the accelerations
        self._wheel_accel_smooth += self.ACCEL_SMOOTHING * (wheel_accel_raw - self._wheel_accel_smooth)
        self._vehicle_accel_smooth += self.ACCEL_SMOOTHING * (imu_forward_accel - self._vehicle_accel_smooth)
        
        # Store for diagnostics
        self.wheel_accel = self._wheel_accel_smooth
        self.vehicle_accel = self._vehicle_accel_smooth
        
        # === 3. Slip detection ===
        self.slip_detected, self.slip_reason = self._detect_slip(
            wheel_speed_ms, gps_speed_ms, gps_valid,
            abs(imu_yaw_rate), throttle_input
        )
        
        # === 4. Update throttle multiplier ===
        self._update_throttle_control(throttle_input)
    
    def _update_ground_speed_estimate(self, imu_accel: float, gps_speed_ms: float, 
                                       gps_valid: bool, dt: float):
        """
        IMU-primary ground speed estimation.
        
        Uses IMU forward acceleration integration as primary source.
        GPS is used only for very slow drift correction (not real-time).
        This avoids GPS latency affecting slip detection.
        """
        # Integrate IMU acceleration (primary, fast response)
        self.estimated_ground_speed += imu_accel * dt
        
        # Prevent negative speed (we don't track reverse well)
        self.estimated_ground_speed = max(0, self.estimated_ground_speed)
        
        # Very slow GPS drift correction (only when GPS is reliable)
        # This prevents long-term drift without introducing GPS latency
        if gps_valid and gps_speed_ms > self.GPS_DRIFT_CORRECTION_MIN_SPEED:
            # 1% correction per cycle - very slow, won't affect real-time response
            drift_error = gps_speed_ms - self.estimated_ground_speed
            self.estimated_ground_speed += self.GPS_DRIFT_CORRECTION_ALPHA * drift_error
        
        # Sanity check: ground speed can't be way higher than wheel speed
        # (unless coasting in neutral with wheels not spinning)
        # This helps reset after wheelspin events
        if self._prev_wheel_speed > 1.0:  # Wheel is turning
            max_reasonable = self._prev_wheel_speed * 1.1  # Allow 10% margin
            self.estimated_ground_speed = min(self.estimated_ground_speed, max_reasonable)
    
    def _detect_slip(self, wheel_speed_ms: float, gps_speed_ms: float, gps_valid: bool,
                     yaw_rate_abs: float, throttle: int) -> tuple[bool, str]:
        """
        Detect wheel slip using IMU-based methods (no GPS real-time dependency).
        
        Methods:
        1. PRIMARY: Acceleration mismatch - wheel accelerates but vehicle doesn't
        2. SECONDARY: Speed divergence - wheel speed >> IMU-estimated ground speed
        
        Returns:
            (slip_detected, reason_string)
        """
        # Only check for slip when accelerating (forward or reverse)
        if abs(throttle) < self.MIN_THROTTLE_FOR_SLIP:
            return False, "throttle_low"
        
        # Reduce sensitivity in tight turns (yaw rate high)
        in_turn = yaw_rate_abs > self.YAW_RATE_THRESHOLD
        
        # === Method 1: Acceleration mismatch (primary, fast) ===
        # Wheel accelerates fast but vehicle doesn't = wheelspin
        wheel_accel_high = self._wheel_accel_smooth > self.WHEEL_ACCEL_THRESHOLD
        vehicle_accel_low = self._vehicle_accel_smooth < self.VEHICLE_ACCEL_THRESHOLD
        
        if wheel_accel_high and vehicle_accel_low and not in_turn:
            return True, f"accel_mismatch(w={self._wheel_accel_smooth:.1f},v={self._vehicle_accel_smooth:.1f})"
        
        # === Method 2: Speed divergence (secondary, IMU-based) ===
        # If wheel speed significantly exceeds IMU-estimated ground speed = slip
        # This replaces the old GPS-based slip ratio method
        if self.estimated_ground_speed > self.SPEED_DIVERGENCE_MIN_SPEED:
            # Calculate divergence: (wheel - ground) / ground
            divergence = (wheel_speed_ms - self.estimated_ground_speed) / self.estimated_ground_speed
            self.slip_ratio = divergence  # Store for diagnostics (reusing field name)
            
            # Higher threshold in turns (allow more slip)
            threshold = self.SPEED_DIVERGENCE_THRESHOLD * (1.5 if in_turn else 1.0)
            
            if divergence > threshold:
                return True, f"speed_divergence({divergence:.2f}>{threshold:.2f})"
        else:
            self.slip_ratio = 0.0
        
        return False, "none"
    
    def _update_throttle_control(self, throttle_input: int):
        """
        Update throttle multiplier based on slip state.
        Soft cut on slip, gradual recovery when clear.
        """
        if not self.enabled:
            self._throttle_multiplier = 1.0
            return
        
        if self.slip_detected:
            # Cut throttle
            self._throttle_multiplier = max(
                self.THROTTLE_MIN_MULTIPLIER,
                self._throttle_multiplier - self.THROTTLE_CUT_RATE
            )
            self._slip_active = True
            self._slip_start_time = time.time()
        else:
            # Recovery
            if self._slip_active:
                # Still in recovery mode
                time_since_slip = time.time() - self._slip_start_time
                
                # Fast recovery if clearly no slip for a while
                if time_since_slip > 0.3 and self._wheel_accel_smooth < 2.0:
                    recovery_rate = self.THROTTLE_FAST_RECOVERY_RATE
                else:
                    recovery_rate = self.THROTTLE_RECOVERY_RATE
                
                self._throttle_multiplier = min(1.0, self._throttle_multiplier + recovery_rate)
                
                # Exit recovery mode when fully recovered
                if self._throttle_multiplier >= 1.0:
                    self._slip_active = False
            else:
                self._throttle_multiplier = 1.0
    
    def get_throttle_multiplier(self) -> float:
        """
        Get current throttle multiplier (0.0 to 1.0).
        Multiply your throttle command by this value.
        """
        if not self.enabled:
            return 1.0
        return self._throttle_multiplier
    
    def apply_to_throttle(self, throttle: int) -> int:
        """
        Apply traction control to throttle command.
        Only affects positive throttle (forward acceleration).
        
        Args:
            throttle: Raw throttle (-1000 to 1000)
        
        Returns:
            Limited throttle (-1000 to 1000)
        """
        if not self.enabled or throttle <= 0:
            return throttle
        
        return int(throttle * self._throttle_multiplier)
    
    def get_status(self) -> dict:
        """Get diagnostic status for debugging/display."""
        return {
            "enabled": self.enabled,
            "slip_detected": self.slip_detected,
            "slip_reason": self.slip_reason,
            "slip_active": self._slip_active,
            "throttle_multiplier": round(self._throttle_multiplier, 2),
            "estimated_ground_speed_kmh": round(self.estimated_ground_speed * 3.6, 1),
            "wheel_accel": round(self.wheel_accel, 2),
            "vehicle_accel": round(self.vehicle_accel, 2),
            "slip_ratio": round(self.slip_ratio, 3)
        }
    
    def reset(self):
        """Reset state (call when race ends or connection resets)."""
        self.estimated_ground_speed = 0.0
        self._prev_wheel_speed = 0.0
        self._wheel_accel_smooth = 0.0
        self._vehicle_accel_smooth = 0.0
        self._throttle_multiplier = 1.0
        self._slip_active = False
        self.slip_detected = False
        self.slip_ratio = 0.0


# === Test / Demo ===

if __name__ == "__main__":
    import random
    
    tc = TractionControl()
    
    print("Traction Control Simulation")
    print("=" * 50)
    
    # Simulate some scenarios
    scenarios = [
        ("Normal acceleration", 
         {"imu_forward_accel": 2.0, "wheel_speed": 15.0, "gps_speed": 14.0, "throttle": 500}),
        ("Wheelspin on launch",
         {"imu_forward_accel": 1.0, "wheel_speed": 30.0, "gps_speed": 5.0, "throttle": 800}),
        ("High speed stable",
         {"imu_forward_accel": 0.5, "wheel_speed": 40.0, "gps_speed": 39.0, "throttle": 600}),
        ("Spin on corner exit",
         {"imu_forward_accel": 0.5, "wheel_speed": 25.0, "gps_speed": 15.0, "throttle": 700}),
    ]
    
    for name, params in scenarios:
        print(f"\n{name}:")
        
        # Run for a few iterations
        for i in range(10):
            tc.update(
                imu_forward_accel=params["imu_forward_accel"] + random.uniform(-0.2, 0.2),
                imu_yaw_rate=random.uniform(-10, 10),
                wheel_speed=params["wheel_speed"],
                gps_speed=params["gps_speed"],
                gps_valid=True,
                throttle_input=params["throttle"]
            )
        
        status = tc.get_status()
        print(f"  Slip: {status['slip_detected']} ({status['slip_reason']})")
        print(f"  Throttle mult: {status['throttle_multiplier']}")
        print(f"  Wheel accel: {status['wheel_accel']:.1f} m/s²")
        print(f"  Vehicle accel: {status['vehicle_accel']:.1f} m/s²")
        
        # Reset for next scenario
        tc.reset()

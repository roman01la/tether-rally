#!/usr/bin/env python3
"""
Unified Low-Speed Traction Manager for ARRMA Big Rock 3S

Replaces separate Launch Control + Traction Control with a unified system
that smoothly transitions between proactive (launch) and reactive (cruise)
slip management.

Phases:
1. LAUNCH (0-5 km/h): Proactive slip targeting for optimal acceleration
   - Target 10% slip ratio for best traction
   - Gradually ramp throttle as grip builds
   
2. TRANSITION (5-15 km/h): Gradual handoff between strategies
   - Blends launch and cruise behaviors
   - No sudden changes in control feel
   
3. CRUISE (>15 km/h): Reactive slip detection and correction
   - Only intervene when slip exceeds threshold (20%)
   - Fast cut, gradual recovery

Surface Adaptation Integration:
- Accepts grip_multiplier from SurfaceAdaptation
- Scales slip thresholds based on estimated surface grip
- Lower grip = more aggressive intervention

Usage:
    from low_speed_traction import LowSpeedTractionManager
    
    traction = LowSpeedTractionManager()
    
    # In control loop (20-50 Hz):
    traction.update(
        wheel_speed=wheel_kmh,
        ground_speed=fused_speed,
        imu_forward_accel=accel_y,
        yaw_rate=imu_yaw_rate,
        throttle_input=throttle,
        grip_multiplier=surface.get_traction_threshold_multiplier()
    )
    
    limited_throttle = traction.apply_to_throttle(throttle)
"""

import time
import math
from car_config import get_config


class LowSpeedTractionManager:
    """
    Unified controller for low-speed traction management.
    
    Phases:
    1. LAUNCH (0-5 km/h): Proactive slip targeting for optimal acceleration
    2. TRANSITION (5-15 km/h): Gradual handoff to reactive mode
    3. CRUISE (>15 km/h): Pure reactive traction control
    
    This eliminates the LC/TC conflict by treating them as a single
    continuous control strategy with speed-dependent behavior.
    """
    
    # Scale factor: config uses 0-1000 range, actual throttle is -32767 to 32767
    THROTTLE_SCALE = 32767 / 1000
    
    def __init__(self):
        # Load config from car profile
        cfg = get_config()
        
        # === Phase Boundaries ===
        self.LAUNCH_PHASE_END = cfg.get_float('low_speed_traction', 'launch_phase_end_kmh')
        self.TRANSITION_PHASE_END = cfg.get_float('low_speed_traction', 'transition_phase_end_kmh')
        
        # === Launch Phase Parameters (proactive) ===
        # Note: Config values are in 0-1000 range, scale to actual throttle range
        self.LAUNCH_TARGET_SLIP = cfg.get_float('low_speed_traction', 'launch_target_slip')
        self.LAUNCH_SLIP_TOLERANCE = cfg.get_float('low_speed_traction', 'launch_slip_tolerance')
        self.LAUNCH_MAX_THROTTLE_RATE = int(cfg.get_int('low_speed_traction', 'launch_max_throttle_rate') * self.THROTTLE_SCALE)
        self.LAUNCH_THROTTLE_CEILING = int(cfg.get_int('low_speed_traction', 'launch_throttle_ceiling') * self.THROTTLE_SCALE)
        self.LAUNCH_SLIP_HIGH_CUT = cfg.get_float('low_speed_traction', 'launch_slip_high_cut')
        
        # === Cruise Phase Parameters (reactive) ===
        self.CRUISE_SLIP_THRESHOLD = cfg.get_float('low_speed_traction', 'cruise_slip_threshold')
        self.CRUISE_THROTTLE_CUT_RATE = cfg.get_float('low_speed_traction', 'cruise_throttle_cut_rate')
        self.CRUISE_RECOVERY_RATE = cfg.get_float('low_speed_traction', 'cruise_recovery_rate')
        self.CRUISE_MIN_MULTIPLIER = cfg.get_float('low_speed_traction', 'cruise_min_multiplier')
        
        # === Shared Parameters ===
        self.MIN_THROTTLE_FOR_SLIP = int(cfg.get_int('low_speed_traction', 'min_throttle_for_slip') * self.THROTTLE_SCALE)
        self.YAW_RATE_THRESHOLD = cfg.get_float('low_speed_traction', 'yaw_rate_threshold')
        self.ACCEL_SMOOTHING = cfg.get_float('low_speed_traction', 'accel_smoothing')
        
        # === Ground Speed Estimation (IMU-primary) ===
        self.GPS_DRIFT_CORRECTION_ALPHA = cfg.get_float('low_speed_traction', 'gps_drift_correction_alpha')
        self.GPS_DRIFT_CORRECTION_MIN_SPEED = cfg.get_float('low_speed_traction', 'gps_drift_correction_min_speed_kmh')
        
        # === State ===
        self._phase = "launch"
        self._throttle_multiplier = 1.0
        self._launch_throttle_target = 0
        self._slip_detected = False
        self._prev_time = time.time()
        
        # Ground speed estimation
        self._estimated_ground_speed = 0.0   # m/s
        self._prev_wheel_speed = 0.0         # m/s
        
        # Smoothed values
        self._wheel_accel_smooth = 0.0
        self._vehicle_accel_smooth = 0.0
        
        # Diagnostics
        self.slip_ratio = 0.0
        self.wheel_accel = 0.0
        self.vehicle_accel = 0.0
        self.current_slip_threshold = 0.0
        
        # Enable/disable
        self.enabled = True
    
    def _calculate_slip_ratio(self, wheel_speed_ms: float, 
                               ground_speed_ms: float) -> float:
        """
        Calculate wheel slip ratio.
        
        Positive = wheel spinning faster than ground (acceleration slip)
        
        Args:
            wheel_speed_ms: Wheel speed in m/s
            ground_speed_ms: Ground speed in m/s
        
        Returns:
            Slip ratio (0.0 = no slip, 0.1 = 10% slip, etc.)
        """
        if ground_speed_ms < 0.5:
            # At very low speed, use absolute difference normalized by wheel speed
            if wheel_speed_ms < 0.1:
                return 0.0
            return (wheel_speed_ms - ground_speed_ms) / max(wheel_speed_ms, 1.0)
        return (wheel_speed_ms - ground_speed_ms) / ground_speed_ms
    
    def _determine_phase(self, ground_speed_kmh: float) -> str:
        """Determine current control phase based on speed."""
        if ground_speed_kmh < self.LAUNCH_PHASE_END:
            return "launch"
        elif ground_speed_kmh < self.TRANSITION_PHASE_END:
            return "transition"
        else:
            return "cruise"
    
    def _update_ground_speed_estimate(self, imu_accel: float, gps_speed_ms: float,
                                       gps_valid: bool, dt: float):
        """
        IMU-primary ground speed estimation.
        
        Uses IMU forward acceleration integration as primary source.
        GPS is used only for very slow drift correction.
        """
        # Integrate IMU acceleration
        self._estimated_ground_speed += imu_accel * dt
        
        # Prevent negative speed
        self._estimated_ground_speed = max(0, self._estimated_ground_speed)
        
        # Very slow GPS drift correction
        if gps_valid and gps_speed_ms > self.GPS_DRIFT_CORRECTION_MIN_SPEED / 3.6:
            drift_error = gps_speed_ms - self._estimated_ground_speed
            self._estimated_ground_speed += self.GPS_DRIFT_CORRECTION_ALPHA * drift_error
        
        # Cap estimated speed to reasonable value based on wheel speed
        if self._prev_wheel_speed > 1.0:
            max_reasonable = self._prev_wheel_speed * 1.1
            self._estimated_ground_speed = min(self._estimated_ground_speed, max_reasonable)
    
    def _launch_control(self, throttle_input: int, slip_ratio: float,
                        grip_multiplier: float) -> int:
        """
        Launch phase: Proactive slip targeting.
        
        Gradually increase throttle while maintaining target slip.
        """
        # Only active on positive throttle
        if throttle_input <= 0:
            self._launch_throttle_target = 0
            return throttle_input
        
        # Adjust target slip based on grip
        adjusted_target = self.LAUNCH_TARGET_SLIP * grip_multiplier
        
        # Ramp up throttle target
        self._launch_throttle_target = min(
            self._launch_throttle_target + self.LAUNCH_MAX_THROTTLE_RATE,
            min(throttle_input, self.LAUNCH_THROTTLE_CEILING)
        )
        
        # Adjust based on current slip
        if slip_ratio > adjusted_target * 1.3:
            # Too much slip - back off significantly
            self._slip_detected = True
            return int(self._launch_throttle_target * self.LAUNCH_SLIP_HIGH_CUT)
        elif slip_ratio > adjusted_target * 1.1:
            # Slightly over target - hold steady
            self._slip_detected = True
            return self._launch_throttle_target
        else:
            # At or below target - continue ramp
            self._slip_detected = False
            return self._launch_throttle_target
    
    def _cruise_control(self, throttle_input: int, slip_ratio: float,
                        yaw_rate_abs: float, grip_multiplier: float) -> int:
        """
        Cruise phase: Reactive slip detection and correction.
        """
        if throttle_input <= 0:
            self._throttle_multiplier = 1.0
            self._slip_detected = False
            return throttle_input
        
        # Adjust threshold based on grip and turn state
        turn_factor = 1.5 if yaw_rate_abs > self.YAW_RATE_THRESHOLD else 1.0
        adjusted_threshold = self.CRUISE_SLIP_THRESHOLD * grip_multiplier * turn_factor
        self.current_slip_threshold = adjusted_threshold
        
        if slip_ratio > adjusted_threshold:
            # Slip detected - cut throttle
            self._slip_detected = True
            self._throttle_multiplier = max(
                self.CRUISE_MIN_MULTIPLIER,
                self._throttle_multiplier - self.CRUISE_THROTTLE_CUT_RATE
            )
        else:
            # No slip - recover
            self._slip_detected = False
            self._throttle_multiplier = min(
                1.0,
                self._throttle_multiplier + self.CRUISE_RECOVERY_RATE
            )
        
        return int(throttle_input * self._throttle_multiplier)
    
    def _transition_control(self, throttle_input: int, slip_ratio: float,
                            ground_speed_kmh: float, yaw_rate_abs: float,
                            grip_multiplier: float) -> int:
        """
        Transition phase: Blend between launch and cruise strategies.
        
        Smoothly interpolates behavior to prevent sudden changes.
        """
        # Calculate blend factor (0 = full launch, 1 = full cruise)
        blend = (ground_speed_kmh - self.LAUNCH_PHASE_END) / \
                (self.TRANSITION_PHASE_END - self.LAUNCH_PHASE_END)
        blend = max(0, min(1, blend))
        
        # Get outputs from both strategies
        launch_output = self._launch_control(throttle_input, slip_ratio, grip_multiplier)
        cruise_output = self._cruise_control(throttle_input, slip_ratio, yaw_rate_abs, grip_multiplier)
        
        # Blend them
        return int(launch_output * (1 - blend) + cruise_output * blend)
    
    def update(self,
               wheel_speed: float,        # km/h from hall sensor
               ground_speed: float,       # km/h (fused speed or GPS)
               imu_forward_accel: float,  # m/s²
               yaw_rate: float,           # deg/s
               throttle_input: int,       # -32767 to 32767 or -1000 to 1000
               grip_multiplier: float = 1.0,  # from SurfaceAdaptation
               gps_valid: bool = True):
        """
        Update traction control state. Call at 20-50 Hz.
        
        Args:
            wheel_speed: Wheel speed from hall sensor (km/h)
            ground_speed: Fused ground speed (km/h)
            imu_forward_accel: Forward acceleration from IMU (m/s²)
            yaw_rate: Yaw rate from gyro (deg/s)
            throttle_input: Current throttle command
            grip_multiplier: Surface grip multiplier (1.0 = normal, >1 = slippery)
            gps_valid: Whether GPS has valid fix
        """
        if not self.enabled:
            self._throttle_multiplier = 1.0
            self._slip_detected = False
            return
        
        now = time.time()
        dt = now - self._prev_time
        self._prev_time = now
        
        # Clamp dt
        dt = max(0.001, min(0.1, dt))
        
        # Convert to m/s
        wheel_speed_ms = wheel_speed / 3.6
        ground_speed_ms = ground_speed / 3.6
        gps_speed_ms = ground_speed / 3.6  # Use fused speed as GPS proxy
        
        # Update ground speed estimate
        self._update_ground_speed_estimate(imu_forward_accel, gps_speed_ms, gps_valid, dt)
        
        # Calculate wheel acceleration
        wheel_accel_raw = (wheel_speed_ms - self._prev_wheel_speed) / dt
        self._prev_wheel_speed = wheel_speed_ms
        
        # Smooth accelerations
        self._wheel_accel_smooth += self.ACCEL_SMOOTHING * (wheel_accel_raw - self._wheel_accel_smooth)
        self._vehicle_accel_smooth += self.ACCEL_SMOOTHING * (imu_forward_accel - self._vehicle_accel_smooth)
        
        # Store for diagnostics
        self.wheel_accel = self._wheel_accel_smooth
        self.vehicle_accel = self._vehicle_accel_smooth
        
        # Use estimated ground speed for slip calculation (not wheel speed)
        # This is more accurate since we integrate IMU data
        ground_speed_for_slip = self._estimated_ground_speed
        
        # Calculate slip ratio
        self.slip_ratio = self._calculate_slip_ratio(wheel_speed_ms, ground_speed_for_slip)
        
        # Determine phase
        self._phase = self._determine_phase(ground_speed)
    
    def apply_to_throttle(self, throttle: int, yaw_rate: float = 0.0,
                          grip_multiplier: float = 1.0) -> int:
        """
        Apply traction control to throttle command.
        
        Only affects positive throttle (forward acceleration).
        
        Args:
            throttle: Raw throttle (-32767 to 32767 or -1000 to 1000)
            yaw_rate: Current yaw rate (deg/s) for turn detection
            grip_multiplier: Surface grip multiplier
        
        Returns:
            Limited throttle
        """
        if not self.enabled or throttle <= 0:
            return throttle
        
        # Check minimum throttle
        if throttle < self.MIN_THROTTLE_FOR_SLIP:
            return throttle
        
        yaw_rate_abs = abs(yaw_rate)
        
        # Apply phase-appropriate control
        if self._phase == "launch":
            return self._launch_control(throttle, self.slip_ratio, grip_multiplier)
        elif self._phase == "transition":
            return self._transition_control(
                throttle, self.slip_ratio, 
                self._estimated_ground_speed * 3.6,  # Convert back to km/h
                yaw_rate_abs, grip_multiplier
            )
        else:  # cruise
            return self._cruise_control(throttle, self.slip_ratio, yaw_rate_abs, grip_multiplier)
    
    def get_throttle_multiplier(self) -> float:
        """
        Get current throttle multiplier (0.0 to 1.0).
        For cruise phase only; launch phase modifies throttle directly.
        """
        if not self.enabled:
            return 1.0
        return self._throttle_multiplier
    
    def get_status(self) -> dict:
        """Get diagnostic status for telemetry."""
        return {
            "enabled": self.enabled,
            "phase": self._phase,
            "slip_detected": self._slip_detected,
            "slip_ratio": round(self.slip_ratio, 3),
            "throttle_multiplier": round(self._throttle_multiplier, 2),
            "launch_target": self._launch_throttle_target,
            "estimated_speed_kmh": round(self._estimated_ground_speed * 3.6, 1),
            "wheel_accel": round(self.wheel_accel, 2),
            "vehicle_accel": round(self.vehicle_accel, 2),
            "current_threshold": round(self.current_slip_threshold, 3),
        }
    
    def reset(self):
        """Reset state (call when race ends or connection resets)."""
        self._phase = "launch"
        self._throttle_multiplier = 1.0
        self._launch_throttle_target = 0
        self._slip_detected = False
        self._estimated_ground_speed = 0.0
        self._prev_wheel_speed = 0.0
        self._wheel_accel_smooth = 0.0
        self._vehicle_accel_smooth = 0.0
        self.slip_ratio = 0.0


# === Test / Demo ===

if __name__ == "__main__":
    import random
    
    traction = LowSpeedTractionManager()
    
    print("Low Speed Traction Manager Simulation")
    print("=" * 50)
    
    scenarios = [
        ("Launch from standstill (controlled slip)", {
            "wheel_speed": 8.0, "ground_speed": 2.0,
            "imu_accel": 3.0, "yaw_rate": 5.0, "throttle": 800
        }),
        ("Transition phase (moderate speed)", {
            "wheel_speed": 15.0, "ground_speed": 10.0,
            "imu_accel": 1.5, "yaw_rate": 10.0, "throttle": 600
        }),
        ("Cruise with wheelspin", {
            "wheel_speed": 35.0, "ground_speed": 20.0,
            "imu_accel": 0.5, "yaw_rate": 5.0, "throttle": 700
        }),
        ("Cruise stable (no intervention)", {
            "wheel_speed": 32.0, "ground_speed": 30.0,
            "imu_accel": 0.3, "yaw_rate": 5.0, "throttle": 500
        }),
    ]
    
    for name, params in scenarios:
        print(f"\n{name}:")
        
        # Simulate several cycles
        for i in range(10):
            traction.update(
                wheel_speed=params["wheel_speed"] + random.uniform(-0.5, 0.5),
                ground_speed=params["ground_speed"] + random.uniform(-0.3, 0.3),
                imu_forward_accel=params["imu_accel"] + random.uniform(-0.1, 0.1),
                yaw_rate=params["yaw_rate"],
                throttle_input=params["throttle"],
                grip_multiplier=1.0
            )
        
        result = traction.apply_to_throttle(
            params["throttle"],
            yaw_rate=params["yaw_rate"],
            grip_multiplier=1.0
        )
        
        status = traction.get_status()
        print(f"  Phase: {status['phase']}")
        print(f"  Slip Ratio: {status['slip_ratio']:.1%}")
        print(f"  Slip Detected: {status['slip_detected']}")
        print(f"  Throttle: {params['throttle']} -> {result}")
        print(f"  Multiplier: {status['throttle_multiplier']}")
        
        # Reset for next scenario
        traction.reset()

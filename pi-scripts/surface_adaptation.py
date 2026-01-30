#!/usr/bin/env python3
"""
Surface Adaptation for ARRMA Big Rock 3S

Estimates current surface grip from driving behavior and adjusts
traction/stability control thresholds accordingly.

Different surfaces have different grip levels:
- Asphalt: High grip (μ ≈ 0.8-1.0)
- Concrete: Medium-high grip (μ ≈ 0.7-0.9)
- Grass: Low grip (μ ≈ 0.3-0.5)
- Dirt/Gravel: Variable (μ ≈ 0.4-0.7)

Fixed traction control thresholds don't work well across surfaces.
This adaptive system learns the current grip level and scales
the slip thresholds for other controllers.

Measurement method:
- During turns (speed > 10 km/h, |steering| > 200), measure lateral accel
- Compare actual lateral accel to expected (based on speed and steering)
- Higher ratio = grippier surface

Output:
- get_traction_threshold_multiplier(): Returns 1.0 for normal grip,
  >1.0 for low grip (more aggressive intervention),
  <1.0 for high grip (less aggressive intervention)

Usage:
    from surface_adaptation import SurfaceAdaptation
    
    surface = SurfaceAdaptation()
    
    # In control loop (20 Hz):
    surface.update(
        lateral_accel=imu_lateral,  # m/s²
        speed=fused_speed,          # km/h
        steering=steering_input     # -32767 to 32767
    )
    
    # Use in traction control:
    grip_mult = surface.get_traction_threshold_multiplier()
    adjusted_threshold = base_threshold * grip_mult
"""

import time
import math


class SurfaceAdaptation:
    """
    Estimates current surface grip from driving behavior.
    
    Monitors:
    - Lateral acceleration during turns (high = grippy)
    - Longitudinal acceleration under throttle
    - Slip ratio during acceleration
    
    Adjusts traction/stability control aggressiveness accordingly.
    
    Phase 1: Only affects slip thresholds via get_traction_threshold_multiplier()
    """
    
    def __init__(self):
        # === Measurement Conditions ===
        self.MIN_SPEED_KMH = 10.0           # Only measure above this speed
        self.MIN_STEERING = 200             # Minimum steering input (out of 1000 or 32767)
        self.MIN_SAMPLES = 10               # Minimum samples before outputting estimate
        
        # === Vehicle Parameters ===
        # ARRMA Big Rock 3S approximate parameters
        self.WHEELBASE_M = 0.32             # Wheelbase in meters
        self.MAX_STEERING_ANGLE_DEG = 30.0  # Max steering angle at full lock
        
        # === Grip Estimation ===
        self.DEFAULT_GRIP = 0.7             # Starting assumption (medium grip)
        self.GRIP_SMOOTHING = 0.05          # EMA alpha for grip updates (slow)
        self.GRIP_MIN = 0.2                 # Minimum estimated grip
        self.GRIP_MAX = 1.2                 # Maximum estimated grip (>1 = very grippy)
        self.HISTORY_SIZE = 50              # Rolling average window
        
        # === State ===
        self._estimated_grip = self.DEFAULT_GRIP
        self._grip_history = []
        self._prev_time = time.time()
        self._measurement_active = False
        
        # Smoothed inputs
        self._lateral_accel_smooth = 0.0
        self._speed_smooth = 0.0
        
        # Diagnostics
        self.last_expected_accel = 0.0
        self.last_actual_accel = 0.0
        self.last_grip_sample = 0.0
        self.sample_count = 0
        
        # Enable/disable
        self.enabled = True
    
    def _steering_to_angle_rad(self, steering_input: int) -> float:
        """
        Convert steering input to approximate front wheel angle in radians.
        
        Args:
            steering_input: Steering command (-32767 to 32767 or -1000 to 1000)
        
        Returns:
            Approximate steering angle in radians
        """
        # Normalize to -1.0 to 1.0
        max_input = 32767 if abs(steering_input) > 1000 else 1000
        normalized = steering_input / max_input
        
        # Convert to radians
        angle_deg = normalized * self.MAX_STEERING_ANGLE_DEG
        return math.radians(angle_deg)
    
    def _calculate_expected_lateral_accel(self, speed_kmh: float, 
                                           steering_input: int) -> float:
        """
        Calculate expected lateral acceleration based on bicycle model.
        
        Uses kinematic relationship: a_lat = v² / R
        Where R is the turning radius from the bicycle model.
        
        Args:
            speed_kmh: Vehicle speed in km/h
            steering_input: Steering command
        
        Returns:
            Expected lateral acceleration in m/s² (assuming perfect grip)
        """
        # Convert speed to m/s
        speed_ms = speed_kmh / 3.6
        
        # Get steering angle
        delta = self._steering_to_angle_rad(steering_input)
        
        # Avoid division by zero for straight driving
        if abs(delta) < 0.01:  # ~0.5 degrees
            return 0.0
        
        # Bicycle model: turning radius R = L / tan(delta)
        # For small angles, tan(delta) ≈ delta, but use actual tan
        try:
            R = self.WHEELBASE_M / math.tan(abs(delta))
        except:
            return 0.0
        
        # Lateral acceleration: a = v² / R
        if R < 0.1:  # Prevent unrealistic values
            R = 0.1
        
        expected_accel = (speed_ms ** 2) / R
        
        return expected_accel
    
    def _update_grip_estimate(self, actual_accel: float, expected_accel: float):
        """
        Update grip estimate based on measured vs expected lateral accel.
        
        Args:
            actual_accel: Measured lateral acceleration (m/s², absolute)
            expected_accel: Expected lateral acceleration (m/s², from model)
        """
        if expected_accel < 0.5:  # Too small to measure reliably
            return
        
        # Grip ratio = actual / expected
        # If actual ≈ expected, grip ≈ 1.0 (good grip)
        # If actual < expected, grip < 1.0 (slippery)
        grip_sample = abs(actual_accel) / expected_accel
        
        # Clamp to reasonable range
        grip_sample = max(self.GRIP_MIN, min(self.GRIP_MAX, grip_sample))
        
        # Store for diagnostics
        self.last_grip_sample = grip_sample
        self.sample_count += 1
        
        # Add to history
        self._grip_history.append(grip_sample)
        if len(self._grip_history) > self.HISTORY_SIZE:
            self._grip_history.pop(0)
        
        # Update estimate (rolling average)
        if len(self._grip_history) >= self.MIN_SAMPLES:
            avg_grip = sum(self._grip_history) / len(self._grip_history)
            # Smooth update
            self._estimated_grip += self.GRIP_SMOOTHING * (avg_grip - self._estimated_grip)
    
    def update(self,
               lateral_accel: float,    # m/s² (positive = right)
               speed: float,            # km/h
               steering: int):          # -32767 to 32767 or -1000 to 1000
        """
        Update surface grip estimate. Call at 10-20 Hz.
        
        Args:
            lateral_accel: Lateral acceleration from IMU (m/s², positive = right)
            speed: Vehicle speed (km/h)
            steering: Steering command
        """
        if not self.enabled:
            return
        
        now = time.time()
        dt = now - self._prev_time
        self._prev_time = now
        
        # Smooth inputs
        alpha = 0.3
        self._lateral_accel_smooth += alpha * (lateral_accel - self._lateral_accel_smooth)
        self._speed_smooth += alpha * (speed - self._speed_smooth)
        
        # Check if conditions are right for measurement
        # Need: decent speed, significant steering, stable turn
        speed_ok = self._speed_smooth > self.MIN_SPEED_KMH
        steering_ok = abs(steering) > self.MIN_STEERING
        
        self._measurement_active = speed_ok and steering_ok
        
        if not self._measurement_active:
            return
        
        # Calculate expected lateral acceleration
        expected = self._calculate_expected_lateral_accel(self._speed_smooth, steering)
        actual = abs(self._lateral_accel_smooth)
        
        # Store for diagnostics
        self.last_expected_accel = expected
        self.last_actual_accel = actual
        
        # Update grip estimate
        self._update_grip_estimate(actual, expected)
    
    def get_estimated_grip(self) -> float:
        """
        Get current grip estimate.
        
        Returns:
            Grip coefficient (0.2-1.2, where 1.0 = normal grip)
        """
        return self._estimated_grip
    
    def get_traction_threshold_multiplier(self) -> float:
        """
        Get multiplier for traction control slip thresholds.
        
        Lower grip = higher multiplier = more aggressive intervention.
        
        Returns:
            Multiplier (0.8-3.3) to apply to slip thresholds
        """
        if not self.enabled:
            return 1.0
        
        # Invert grip: lower grip = higher multiplier
        # Clamp grip to avoid extreme values
        clamped_grip = max(0.3, self._estimated_grip)
        
        # Multiplier = 1 / grip
        # At grip 1.0 → multiplier 1.0
        # At grip 0.5 → multiplier 2.0
        # At grip 0.3 → multiplier 3.3
        return 1.0 / clamped_grip
    
    def get_status(self) -> dict:
        """Get diagnostic status for telemetry."""
        return {
            "enabled": self.enabled,
            "estimated_grip": round(self._estimated_grip, 2),
            "threshold_multiplier": round(self.get_traction_threshold_multiplier(), 2),
            "measurement_active": self._measurement_active,
            "sample_count": self.sample_count,
            "history_size": len(self._grip_history),
            "last_expected": round(self.last_expected_accel, 2),
            "last_actual": round(self.last_actual_accel, 2),
            "last_grip_sample": round(self.last_grip_sample, 2),
        }
    
    def reset(self):
        """Reset state (call when race ends or conditions change significantly)."""
        self._estimated_grip = self.DEFAULT_GRIP
        self._grip_history = []
        self._lateral_accel_smooth = 0.0
        self._speed_smooth = 0.0
        self.sample_count = 0
        self.last_expected_accel = 0.0
        self.last_actual_accel = 0.0
        self.last_grip_sample = 0.0


# === Test / Demo ===

if __name__ == "__main__":
    import random
    
    surface = SurfaceAdaptation()
    
    print("Surface Adaptation Simulation")
    print("=" * 50)
    
    # Simulate different surfaces
    surfaces = [
        ("Asphalt (high grip)", 0.95),
        ("Concrete (medium grip)", 0.7),
        ("Grass (low grip)", 0.4),
    ]
    
    for surface_name, grip_factor in surfaces:
        print(f"\n{surface_name}:")
        print("-" * 30)
        
        surface.reset()
        
        # Simulate driving on this surface
        for i in range(100):
            # Simulated turn at 15 km/h with 500 steering
            speed = 15.0 + random.uniform(-1, 1)
            steering = 500
            
            # Expected lateral accel for this speed/steering
            expected = surface._calculate_expected_lateral_accel(speed, steering)
            
            # Actual accel is scaled by surface grip + noise
            actual = expected * grip_factor + random.uniform(-0.2, 0.2)
            
            surface.update(
                lateral_accel=actual,
                speed=speed,
                steering=steering
            )
        
        status = surface.get_status()
        print(f"  Estimated Grip: {status['estimated_grip']:.2f} (actual: {grip_factor})")
        print(f"  Threshold Multiplier: {status['threshold_multiplier']:.2f}")
        print(f"  Samples: {status['sample_count']}")
    
    # Show how multiplier affects thresholds
    print("\n" + "=" * 50)
    print("Threshold Scaling Example:")
    print("-" * 30)
    base_threshold = 0.20  # 20% slip threshold
    
    for grip in [1.0, 0.7, 0.5, 0.3]:
        mult = 1.0 / grip
        adjusted = base_threshold * mult
        print(f"  Grip {grip:.1f}: {base_threshold:.0%} × {mult:.1f} = {adjusted:.0%} threshold")

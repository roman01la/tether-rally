#!/usr/bin/env python3
"""
Direction Estimator for ARRMA Big Rock 3S

Estimates vehicle direction (forward/backward/stopped) using sensor fusion
without a second hall sensor. Combines:

1. IMU forward acceleration integration (primary - fast response)
2. Throttle command intent (for seeding direction from standstill)
3. Yaw rate + steering correlation (validation when turning)
4. Wheel speed magnitude (bounding)

The key insight is that steering geometry reverses when going backward:
- Steering right + moving forward → yaw rate negative (clockwise)
- Steering right + moving backward → yaw rate positive (counter-clockwise)

This allows us to validate/correct the IMU-integrated direction when turning.

Usage:
    from direction_estimator import DirectionEstimator
    
    dir_est = DirectionEstimator()
    
    # In IMU loop (20-50 Hz):
    signed_speed = dir_est.update(
        imu_accel=imu_forward_accel,
        wheel_speed_magnitude=wheel_speed,
        throttle=current_throttle,
        steering=current_steering,
        yaw_rate=imu_yaw_rate,
        dt=0.05
    )
    
    direction = dir_est.get_direction()  # "forward", "backward", "stopped"
"""

import time
from car_config import get_config


class DirectionEstimator:
    """
    Fuses IMU acceleration, throttle intent, and yaw-steering correlation
    to estimate vehicle direction without a directional wheel sensor.
    """
    
    def __init__(self):
        # Load config from car profile
        cfg = get_config()
        
        # Throttle scale: config uses 0-1000 range, actual is -32767 to 32767
        THROTTLE_SCALE = 32767 / 1000
        
        # === Seeding thresholds (from standstill) ===
        self._throttle_seed_threshold = int(
            cfg.get_float('direction_estimator', 'throttle_seed_threshold') * THROTTLE_SCALE
        )
        self._accel_confirm_threshold = cfg.get_float(
            'direction_estimator', 'accel_confirm_threshold'
        )
        
        # === Speed thresholds ===
        self._stopped_threshold = cfg.get_float(
            'direction_estimator', 'stopped_threshold_ms'
        )
        self._yaw_validation_min_speed = cfg.get_float(
            'direction_estimator', 'yaw_validation_min_speed_ms'
        )
        
        # === Yaw-steering correlation thresholds ===
        self._min_steering_for_validation = cfg.get_int(
            'direction_estimator', 'min_steering_for_validation'
        )
        self._min_yaw_rate_for_validation = cfg.get_float(
            'direction_estimator', 'min_yaw_rate_for_validation'
        )
        self._yaw_correction_min_speed = cfg.get_float(
            'direction_estimator', 'yaw_correction_min_speed_ms'
        )
        self._yaw_correction_min_yaw_rate = cfg.get_float(
            'direction_estimator', 'yaw_correction_min_yaw_rate'
        )
        
        # === Drift correction ===
        self._stationary_decay_rate = cfg.get_float(
            'direction_estimator', 'stationary_decay_rate'
        )
        self._stationary_accel_threshold = cfg.get_float(
            'direction_estimator', 'stationary_accel_threshold'
        )
        self._stationary_throttle_threshold = int(
            cfg.get_float('direction_estimator', 'stationary_throttle_threshold') * THROTTLE_SCALE
        )
        
        # === IMU bias estimation ===
        self._bias_learning_rate = cfg.get_float(
            'direction_estimator', 'bias_learning_rate'
        )
        
        # === Confidence tracking ===
        self._confidence_decay_on_disagreement = cfg.get_float(
            'direction_estimator', 'confidence_decay_on_disagreement'
        )
        self._confidence_decay_when_stationary = cfg.get_float(
            'direction_estimator', 'confidence_decay_when_stationary'
        )
        self._confidence_growth_rate = cfg.get_float(
            'direction_estimator', 'confidence_growth_rate'
        )
        
        # === State ===
        self._signed_speed = 0.0  # Integrated velocity (m/s, signed)
        self._direction = "stopped"
        self._last_update_time = time.time()
        self._direction_confidence = 0.0  # 0.0 to 1.0
        self._accel_bias_estimate = 0.0  # Estimated accelerometer bias (m/s²)
        
        # Diagnostics
        self.signed_speed_kmh = 0.0
        self.direction = "stopped"
        self.confidence = 0.0
        self.yaw_validation_active = False
        self.yaw_agrees = True
        self.accel_bias = 0.0
    
    def update(self, imu_accel: float, wheel_speed_magnitude: float,
               throttle: int, steering: int, yaw_rate: float,
               dt: float = None) -> float:
        """
        Update direction estimate using sensor fusion.
        
        Args:
            imu_accel: Forward acceleration from IMU (m/s², positive = forward)
            wheel_speed_magnitude: Wheel speed magnitude (km/h, always positive)
            throttle: Throttle command (-32767 to 32767)
            steering: Steering command (-32767 to 32767, negative = left)
            yaw_rate: Yaw rate from gyro (deg/s, positive = CCW/left turn)
            dt: Time delta (seconds). If None, calculated from last call.
        
        Returns:
            Signed speed estimate (km/h, positive = forward, negative = backward)
        """
        # Calculate dt if not provided
        now = time.time()
        if dt is None:
            dt = now - self._last_update_time
        self._last_update_time = now
        dt = max(0.001, min(0.1, dt))  # Clamp to reasonable range
        
        # Convert wheel speed to m/s for internal calculations
        wheel_speed_ms = wheel_speed_magnitude / 3.6
        
        # === Step 1: Update IMU bias estimate (high-pass filter) ===
        # Slowly track the mean acceleration as bias
        # This removes DC offset from accelerometer
        self._accel_bias_estimate += self._bias_learning_rate * (imu_accel - self._accel_bias_estimate)
        corrected_accel = imu_accel - self._accel_bias_estimate
        self.accel_bias = self._accel_bias_estimate
        
        # === Step 2: Integrate bias-corrected IMU acceleration ===
        self._signed_speed += corrected_accel * dt
        
        # === Step 3: Bound by wheel speed magnitude ===
        # The integrated speed magnitude shouldn't exceed what the wheel reports
        if abs(self._signed_speed) > wheel_speed_ms:
            # Preserve sign, limit magnitude
            self._signed_speed = (wheel_speed_ms * 
                                  (1.0 if self._signed_speed > 0 else -1.0))
        
        # === Step 4: Seed direction from standstill ===
        # When nearly stopped, use throttle + acceleration to determine initial direction
        if abs(self._signed_speed) < 0.5 and wheel_speed_ms < 0.5:
            if throttle > self._throttle_seed_threshold and imu_accel > self._accel_confirm_threshold:
                # Commanding forward and accelerating forward
                self._signed_speed = 0.3  # Seed with small forward velocity
                self._direction_confidence = 0.6
            elif throttle < -self._throttle_seed_threshold and imu_accel < -self._accel_confirm_threshold:
                # Commanding backward and accelerating backward
                self._signed_speed = -0.3  # Seed with small backward velocity
                self._direction_confidence = 0.6
        
        # === Step 5: Yaw-steering correlation validation ===
        # When turning, check if yaw direction matches expected for current direction
        self.yaw_validation_active = False
        self.yaw_agrees = True
        
        if (abs(steering) > self._min_steering_for_validation and 
            abs(yaw_rate) > self._min_yaw_rate_for_validation and
            wheel_speed_ms > self._yaw_validation_min_speed):
            
            self.yaw_validation_active = True
            
            # Expected yaw direction when moving forward:
            # - Steering right (positive) → yaw negative (clockwise from above)
            # - Steering left (negative) → yaw positive (counter-clockwise from above)
            # So for forward motion: sign(steering) should be opposite to sign(yaw_rate)
            
            expected_yaw_sign_forward = -1 if steering > 0 else 1
            actual_yaw_sign = 1 if yaw_rate > 0 else -1
            
            yaw_says_forward = (actual_yaw_sign == expected_yaw_sign_forward)
            imu_says_forward = self._signed_speed > 0
            
            if yaw_says_forward != imu_says_forward:
                # Disagreement between IMU integration and yaw-steering correlation
                self.yaw_agrees = False
                
                # Decay confidence on disagreement
                self._direction_confidence *= self._confidence_decay_on_disagreement
                
                # Trust yaw correlation when:
                # 1. We have meaningful speed
                # 2. The disagreement is clear (not borderline)
                if (wheel_speed_ms > self._yaw_correction_min_speed and 
                    abs(yaw_rate) > self._yaw_correction_min_yaw_rate):
                    # Flip direction
                    self._signed_speed = -self._signed_speed
                    self._direction_confidence = 0.8
        
        # === Step 6: Stationary drift correction ===
        # When wheel stopped and throttle released, decay integrated speed toward zero
        if (wheel_speed_ms < 0.3 and 
            abs(throttle) < self._stationary_throttle_threshold and
            abs(imu_accel) < self._stationary_accel_threshold):
            self._signed_speed *= self._stationary_decay_rate
            # Also decay confidence when stationary
            self._direction_confidence *= self._confidence_decay_when_stationary
            if abs(self._signed_speed) < 0.1:
                self._signed_speed = 0.0
        
        # === Step 7: Update direction state ===
        if abs(self._signed_speed) < self._stopped_threshold:
            self._direction = "stopped"
        elif self._signed_speed > 0:
            self._direction = "forward"
        else:
            self._direction = "backward"
        
        # Update confidence based on speed (only grow when moving with agreement)
        if wheel_speed_ms > 2.0 and self.yaw_agrees:
            self._direction_confidence = min(
                1.0, 
                self._direction_confidence + self._confidence_growth_rate
            )
        
        # Update diagnostics
        self.signed_speed_kmh = self._signed_speed * 3.6
        self.direction = self._direction
        self.confidence = self._direction_confidence
        
        return self.signed_speed_kmh
    
    def get_direction(self) -> str:
        """
        Get current direction estimate.
        
        Returns:
            "forward", "backward", or "stopped"
        """
        return self._direction
    
    def get_signed_speed(self) -> float:
        """
        Get signed speed estimate in km/h.
        
        Returns:
            Speed in km/h (positive = forward, negative = backward)
        """
        return self.signed_speed_kmh
    
    def get_status(self) -> dict:
        """
        Get full status for telemetry/debugging.
        
        Returns:
            Dictionary with direction estimation state
        """
        return {
            'direction': self._direction,
            'signed_speed': self.signed_speed_kmh,
            'confidence': self._direction_confidence,
            'yaw_validation_active': self.yaw_validation_active,
            'yaw_agrees': self.yaw_agrees,
            'accel_bias': self.accel_bias
        }
    
    def reset(self):
        """Reset estimator state (e.g., when car is known to be stopped)."""
        self._signed_speed = 0.0
        self._direction = "stopped"
        self._direction_confidence = 0.0
        # Note: Don't reset accel_bias - it should persist across resets
        self.signed_speed_kmh = 0.0
        self.direction = "stopped"
        self.yaw_validation_active = False
        self.yaw_agrees = True

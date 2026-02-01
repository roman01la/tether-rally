#!/usr/bin/env python3
"""
Anti-lock Braking System (ABS) for ARRMA Big Rock 3S

Prevents wheel lockup during braking when driving over the internet with latency.
The driver can't feel wheel lockup, so locked wheels = no steering control = crash.

Key insight for RC cars:
- Negative throttle while moving forward = BRAKING
- Negative throttle while stopped or moving backward = REVERSING
- ABS should ONLY intervene during forward braking, never during reversing

ESC State Machine:
1. Moving forward + negative throttle = BRAKE
2. Stopped + release throttle = NEUTRAL
3. Stopped + negative throttle = REVERSE (some ESCs require neutral first)

Usage:
    from abs_controller import ABSController, ThrottleStateTracker
    
    abs_ctrl = ABSController()
    throttle_tracker = ThrottleStateTracker()
    
    # In control loop (20-50 Hz):
    esc_state = throttle_tracker.update(throttle_input, vehicle_speed)
    
    modified_throttle = abs_ctrl.update(
        wheel_speed=wheel_kmh,
        vehicle_speed=fused_speed,
        imu_forward_accel=accel_y,
        throttle_input=throttle,
        esc_state=esc_state,
        timestamp_ms=int(time.time() * 1000)
    )

Configuration:
    All tuning parameters loaded from car profile (pi-scripts/profiles/*.ini)
    Set CAR_PROFILE environment variable to select profile.
"""

import time
from car_config import get_config


class ThrottleStateTracker:
    """
    Tracks ESC brake/reverse state machine.
    
    Typical ESC behavior:
    1. Moving forward + negative throttle = BRAKE
    2. Stopped + release throttle = NEUTRAL
    3. Stopped + negative throttle = REVERSE
    
    Some ESCs require throttle to return to neutral before reverse engages.
    """
    
    def __init__(self):
        self._state = "neutral"  # "neutral", "braking", "reverse_armed", "reversing"
        self._was_moving_forward = False
        self._throttle_returned_to_neutral = True
        
    def update(self, throttle_input: int, vehicle_speed: float) -> str:
        """
        Update ESC state based on throttle and speed.
        
        Args:
            throttle_input: Throttle command (-32767 to 32767 or -1000 to 1000)
            vehicle_speed: Vehicle speed in km/h (positive = forward)
        
        Returns:
            Current ESC state interpretation: "neutral", "braking", "reverse_armed", "reversing"
        """
        # Thresholds (with some hysteresis)
        moving_forward = vehicle_speed > 2.0
        moving_backward = vehicle_speed < -2.0
        stopped = abs(vehicle_speed) <= 2.0
        throttle_neutral = abs(throttle_input) < 50
        throttle_negative = throttle_input < -100
        
        if moving_forward:
            self._was_moving_forward = True
            if throttle_negative:
                self._state = "braking"
            else:
                self._state = "neutral"
                
        elif stopped:
            if throttle_neutral:
                self._throttle_returned_to_neutral = True
                self._state = "neutral"
            elif throttle_negative:
                if self._was_moving_forward and not self._throttle_returned_to_neutral:
                    # Still braking to a stop
                    self._state = "braking"
                else:
                    # Throttle was released, now reverse
                    self._state = "reverse_armed"
                    self._was_moving_forward = False
                    
        elif moving_backward:
            self._was_moving_forward = False
            self._throttle_returned_to_neutral = False
            self._state = "reversing"
            
        return self._state
    
    def get_state(self) -> str:
        """Get current ESC state."""
        return self._state
    
    def reset(self):
        """Reset state machine."""
        self._state = "neutral"
        self._was_moving_forward = False
        self._throttle_returned_to_neutral = True


class ABSController:
    """
    ABS for RC car with proper direction awareness.
    
    Key insight: Negative throttle while moving forward = braking
                 Negative throttle while stopped/moving backward = reversing
    
    ABS should ONLY intervene during forward braking, never during:
    - Intentional reversing
    - Stationary state
    - Already moving backward
    
    Detection: Uses wheel speed vs vehicle speed (IMU-integrated) slip ratio.
    Intervention: Pulse modulation of brake input.
    """
    
    def __init__(self):
        # Load config from car profile
        cfg = get_config()
        
        # === Tuning Parameters ===
        
        # Slip detection
        self.SLIP_THRESHOLD = cfg.get_float('abs', 'slip_threshold')
        self.MIN_SPEED_KMH = cfg.get_float('abs', 'min_speed_kmh')
        self.MIN_BRAKE_INPUT = cfg.get_int('abs', 'min_brake_input')
        
        # Direction detection
        self.DIRECTION_HYSTERESIS = cfg.get_float('abs', 'direction_hysteresis_kmh')
        self.ACCEL_DIRECTION_THRESHOLD = cfg.get_float('abs', 'accel_direction_threshold')
        
        # ABS cycling
        self.CYCLE_TIME_MS = cfg.get_int('abs', 'cycle_time_ms')
        self.BRAKE_APPLY_RATIO = cfg.get_float('abs', 'brake_apply_ratio')
        self.BRAKE_RELEASE_RATIO = cfg.get_float('abs', 'brake_release_ratio')
        
        # === State ===
        self._vehicle_direction = "stopped"  # "forward", "backward", "stopped"
        self._last_cycle_time = 0
        self._abs_phase = "apply"            # "apply" or "release"
        self._intervention_active = False
        self._prev_time = time.time()
        
        # Diagnostics
        self.slip_ratio = 0.0
        self.wheel_locked = False
        
        # Enable/disable
        self.enabled = True
    
    def _determine_direction(self, vehicle_speed: float, imu_forward_accel: float) -> str:
        """
        Determine vehicle direction using speed and acceleration.
        
        Uses hysteresis to prevent rapid flapping between states.
        IMU forward accel helps detect direction during speed transitions.
        """
        if abs(vehicle_speed) < self.DIRECTION_HYSTERESIS:
            # Very slow - use acceleration to predict intent
            if imu_forward_accel > self.ACCEL_DIRECTION_THRESHOLD:
                return "forward"
            elif imu_forward_accel < -self.ACCEL_DIRECTION_THRESHOLD:
                return "backward"
            return "stopped"
        elif vehicle_speed > self.DIRECTION_HYSTERESIS:
            return "forward"
        else:
            return "backward"
    
    def _detect_wheel_lockup(self, wheel_speed: float, vehicle_speed: float) -> bool:
        """
        Detect wheel lockup during braking.
        
        Lockup occurs when wheel speed drops significantly below vehicle speed.
        
        Args:
            wheel_speed: Wheel speed in km/h
            vehicle_speed: Vehicle speed in km/h
        
        Returns:
            True if wheel is locking up
        """
        if vehicle_speed < self.MIN_SPEED_KMH:
            self.slip_ratio = 0.0
            return False
            
        # Slip ratio: (vehicle - wheel) / vehicle
        # Positive slip = wheel slower than vehicle = braking slip
        self.slip_ratio = (vehicle_speed - wheel_speed) / max(vehicle_speed, 0.1)
        
        return self.slip_ratio > self.SLIP_THRESHOLD
    
    def update(self, 
               wheel_speed: float,      # km/h from hall sensor
               vehicle_speed: float,    # km/h from IMU/GPS fusion
               imu_forward_accel: float,# m/s² (positive = forward accel)
               throttle_input: int,     # -32767 to 32767 or -1000 to 1000
               esc_state: str,          # from ThrottleStateTracker
               timestamp_ms: int) -> int:
        """
        Process throttle through ABS.
        
        Args:
            wheel_speed: Wheel speed from hall sensor (km/h)
            vehicle_speed: Fused vehicle speed (km/h)
            imu_forward_accel: Forward acceleration from IMU (m/s²)
            throttle_input: Throttle command (negative = brake/reverse)
            esc_state: Current ESC state from ThrottleStateTracker
            timestamp_ms: Current timestamp in milliseconds
        
        Returns:
            Modified throttle value (may pulse brake pressure)
        """
        if not self.enabled:
            self._intervention_active = False
            return throttle_input
        
        # Update timing
        now = time.time()
        dt = now - self._prev_time
        self._prev_time = now
        
        # Update direction state
        self._vehicle_direction = self._determine_direction(vehicle_speed, imu_forward_accel)
        
        # === CRITICAL: Only activate ABS when braking while moving forward ===
        # Use ESC state to determine if we're actually braking (not reversing)
        
        is_braking_while_forward = (
            self._vehicle_direction == "forward" and
            esc_state == "braking" and
            throttle_input < -self.MIN_BRAKE_INPUT and
            vehicle_speed > self.MIN_SPEED_KMH
        )
        
        if not is_braking_while_forward:
            # Not a braking situation - pass through unchanged
            # This allows normal reversing without ABS interference
            self._intervention_active = False
            self.wheel_locked = False
            return throttle_input
        
        # Check for wheel lockup
        self.wheel_locked = self._detect_wheel_lockup(wheel_speed, vehicle_speed)
        
        if not self.wheel_locked:
            # No lockup - apply full braking
            self._intervention_active = False
            return throttle_input
        
        # === ABS INTERVENTION ===
        self._intervention_active = True
        
        # Pulse modulation - alternate between apply and release
        time_in_cycle = timestamp_ms - self._last_cycle_time
        
        if time_in_cycle >= self.CYCLE_TIME_MS:
            self._last_cycle_time = timestamp_ms
            self._abs_phase = "release" if self._abs_phase == "apply" else "apply"
        
        if self._abs_phase == "apply":
            # Apply reduced brake pressure
            return int(throttle_input * self.BRAKE_APPLY_RATIO)
        else:
            # Release brake briefly to let wheel spin up
            # Don't go to zero - maintain some retardation
            return int(throttle_input * self.BRAKE_RELEASE_RATIO)
    
    def get_throttle_multiplier(self) -> float:
        """
        Get current brake pressure multiplier for diagnostics.
        Returns 1.0 when not intervening, <1.0 when pulsing.
        """
        if not self._intervention_active:
            return 1.0
        if self._abs_phase == "apply":
            return self.BRAKE_APPLY_RATIO
        return self.BRAKE_RELEASE_RATIO
    
    def get_status(self) -> dict:
        """Get diagnostic status for telemetry."""
        return {
            "enabled": self.enabled,
            "active": self._intervention_active,
            "direction": self._vehicle_direction,
            "phase": self._abs_phase if self._intervention_active else "none",
            "slip_ratio": round(self.slip_ratio, 3),
            "wheel_locked": self.wheel_locked,
        }
    
    def reset(self):
        """Reset state (call when race ends or connection resets)."""
        self._vehicle_direction = "stopped"
        self._last_cycle_time = 0
        self._abs_phase = "apply"
        self._intervention_active = False
        self.slip_ratio = 0.0
        self.wheel_locked = False


# === Test / Demo ===

if __name__ == "__main__":
    abs_ctrl = ABSController()
    throttle_tracker = ThrottleStateTracker()
    
    print("ABS Controller Simulation")
    print("=" * 50)
    
    scenarios = [
        ("Normal braking (no lockup)", {
            "wheel_speed": 18.0, "vehicle_speed": 20.0, 
            "imu_accel": -2.0, "throttle": -500
        }),
        ("Wheel lockup during braking", {
            "wheel_speed": 5.0, "vehicle_speed": 25.0,
            "imu_accel": -3.0, "throttle": -800
        }),
        ("Intentional reverse (should NOT trigger)", {
            "wheel_speed": 5.0, "vehicle_speed": -5.0,
            "imu_accel": -1.0, "throttle": -500
        }),
        ("Low speed braking (ABS disabled)", {
            "wheel_speed": 1.0, "vehicle_speed": 2.5,
            "imu_accel": -1.0, "throttle": -400
        }),
    ]
    
    for name, params in scenarios:
        print(f"\n{name}:")
        
        # Simulate several cycles
        for i in range(10):
            esc_state = throttle_tracker.update(
                params["throttle"], 
                params["vehicle_speed"]
            )
            
            result = abs_ctrl.update(
                wheel_speed=params["wheel_speed"],
                vehicle_speed=params["vehicle_speed"],
                imu_forward_accel=params["imu_accel"],
                throttle_input=params["throttle"],
                esc_state=esc_state,
                timestamp_ms=int(time.time() * 1000) + i * 10
            )
        
        status = abs_ctrl.get_status()
        print(f"  ESC State: {throttle_tracker.get_state()}")
        print(f"  ABS Active: {status['active']}")
        print(f"  Direction: {status['direction']}")
        print(f"  Slip Ratio: {status['slip_ratio']:.2%}")
        print(f"  Input: {params['throttle']} -> Output: {result}")
        
        # Reset for next scenario
        abs_ctrl.reset()
        throttle_tracker.reset()

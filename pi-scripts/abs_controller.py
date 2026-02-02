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
    
    Edge case handling:
    - Uses IMU forward acceleration as direction hint when speed is ambiguous
    - Prevents ABS from triggering when intentionally reversing
    """
    
    def __init__(self):
        self._state = "neutral"  # "neutral", "braking", "reverse_armed", "reversing"
        self._was_moving_forward = False
        self._throttle_returned_to_neutral = True
        self._last_forward_accel = 0.0  # Direction hint from IMU
        
        # Config uses 0-1000 range, actual throttle is -32767 to 32767
        # Scale thresholds accordingly
        THROTTLE_SCALE = 32767 / 1000
        self._throttle_neutral_threshold = int(50 * THROTTLE_SCALE)   # ~1638
        self._throttle_negative_threshold = int(-100 * THROTTLE_SCALE)  # ~-3277
        
        # Acceleration threshold for direction hint (m/s²)
        self._accel_direction_threshold = 0.5
        
    def update(self, throttle_input: int, vehicle_speed: float,
               forward_accel: float = 0.0) -> str:
        """
        Update ESC state based on throttle, speed, and acceleration.
        
        Args:
            throttle_input: Throttle command (-32767 to 32767 or -1000 to 1000)
            vehicle_speed: Vehicle speed in km/h (positive = forward)
            forward_accel: IMU forward acceleration (m/s², positive = accelerating forward)
                          Used as direction hint when speed is ambiguous
        
        Returns:
            Current ESC state interpretation: "neutral", "braking", "reverse_armed", "reversing"
        """
        self._last_forward_accel = forward_accel
        
        # Thresholds (with some hysteresis)
        moving_forward = vehicle_speed > 2.0
        moving_backward = vehicle_speed < -2.0
        stopped = abs(vehicle_speed) <= 2.0
        throttle_neutral = abs(throttle_input) < self._throttle_neutral_threshold
        throttle_negative = throttle_input < self._throttle_negative_threshold
        
        if moving_forward:
            self._was_moving_forward = True
            if throttle_negative:
                self._state = "braking"
            else:
                self._state = "neutral"
                
        elif stopped:
            if throttle_neutral:
                self._throttle_returned_to_neutral = True
                # Clear forward memory when stopped and throttle neutral - the driver
                # has completed any braking maneuver and is ready to reverse
                self._was_moving_forward = False
                self._state = "neutral"
            elif throttle_negative:
                if self._was_moving_forward and not self._throttle_returned_to_neutral:
                    # Still braking to a stop - but check acceleration hint
                    # If accelerating backward (negative accel with negative throttle),
                    # the driver is likely trying to reverse, not brake
                    if forward_accel < -self._accel_direction_threshold:
                        # IMU shows backward acceleration - treat as reverse intent
                        self._state = "reverse_armed"
                        self._was_moving_forward = False
                    else:
                        # Decelerating or stationary - still braking
                        self._state = "braking"
                else:
                    # Throttle was released, now reverse
                    self._state = "reverse_armed"
                    self._was_moving_forward = False
                    
        elif moving_backward:
            self._was_moving_forward = False
            # Note: Don't touch _throttle_returned_to_neutral here - it should only
            # be reset when throttle returns to neutral. Setting it to False here
            # caused ABS to stay stuck in "braking" when trying to drive forward
            # after reversing, because the "stopped + negative throttle" logic
            # thought we were still braking from a previous forward motion.
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
        self._last_forward_accel = 0.0


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
    
    Surface adaptation: Slip threshold scales with grip_multiplier from
    SurfaceAdaptation - lower grip = higher threshold (more tolerant).
    """
    
    def __init__(self):
        # Load config from car profile
        cfg = get_config()
        
        # === Tuning Parameters ===
        # Config uses 0-1000 range, actual throttle is -32767 to 32767
        THROTTLE_SCALE = 32767 / 1000
        
        # Slip detection
        self._base_slip_threshold = cfg.get_float('abs', 'slip_threshold')
        self.MIN_SPEED_KMH = cfg.get_float('abs', 'min_speed_kmh')
        self.MIN_BRAKE_INPUT = int(cfg.get_int('abs', 'min_brake_input') * THROTTLE_SCALE)
        
        # Slip ratio smoothing (low-pass filter to reduce sensor noise)
        self.SLIP_SMOOTHING_ALPHA = 0.3  # 0.0 = no smoothing, 1.0 = no update
        
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
        
        # Smoothed sensor state (updated at IMU rate via update_sensors)
        self._smoothed_slip_ratio = 0.0
        self._current_wheel_speed = 0.0
        self._current_vehicle_speed = 0.0
        self._current_forward_accel = 0.0
        self._current_grip_multiplier = 1.0
        
        # Diagnostics
        self.slip_ratio = 0.0           # Raw slip ratio
        self.effective_threshold = self._base_slip_threshold
        self.wheel_locked = False
        
        # Enable/disable
        self.enabled = True
    
    def update_sensors(self, wheel_speed: float, vehicle_speed: float,
                       imu_forward_accel: float, grip_multiplier: float = 1.0,
                       direction_override: str = None):
        """
        Update sensor state at IMU rate (20 Hz) for consistent timing.
        
        Called from IMU loop to keep slip ratio and direction detection
        up-to-date between control message arrivals.
        
        Args:
            wheel_speed: Wheel speed from hall sensor (km/h)
            vehicle_speed: Fused vehicle speed (km/h)
            imu_forward_accel: Forward acceleration from IMU (m/s²)
            grip_multiplier: Surface grip multiplier from SurfaceAdaptation
            direction_override: Optional direction from DirectionEstimator
                               ("forward", "backward", "stopped")
                               If provided, overrides internal direction detection
        """
        self._current_wheel_speed = wheel_speed
        self._current_vehicle_speed = vehicle_speed
        self._current_forward_accel = imu_forward_accel
        self._current_grip_multiplier = grip_multiplier
        
        # Update direction - prefer external direction estimator if provided
        if direction_override is not None:
            self._vehicle_direction = direction_override
        else:
            self._vehicle_direction = self._determine_direction(vehicle_speed, imu_forward_accel)
        
        # Update smoothed slip ratio (only when moving forward fast enough)
        if vehicle_speed > self.MIN_SPEED_KMH:
            raw_slip = (vehicle_speed - wheel_speed) / max(vehicle_speed, 0.1)
            # Low-pass filter: smoothed = alpha * old + (1-alpha) * new
            self._smoothed_slip_ratio = (
                self.SLIP_SMOOTHING_ALPHA * self._smoothed_slip_ratio +
                (1.0 - self.SLIP_SMOOTHING_ALPHA) * raw_slip
            )
        else:
            # Reset smoothed slip when slow/stopped
            self._smoothed_slip_ratio = 0.0
    
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
    
    def _detect_wheel_lockup(self, wheel_speed: float, vehicle_speed: float,
                              grip_multiplier: float = 1.0) -> bool:
        """
        Detect wheel lockup during braking using smoothed slip ratio.
        
        Lockup occurs when wheel speed drops significantly below vehicle speed.
        Threshold adapts to surface grip - lower grip = higher threshold.
        
        Args:
            wheel_speed: Wheel speed in km/h
            vehicle_speed: Vehicle speed in km/h
            grip_multiplier: Surface grip multiplier (1.0 = nominal)
        
        Returns:
            True if wheel is locking up
        """
        if vehicle_speed < self.MIN_SPEED_KMH:
            self.slip_ratio = 0.0
            self.effective_threshold = self._base_slip_threshold
            return False
        
        # Use smoothed slip ratio (updated at IMU rate)
        # Also compute raw for diagnostics
        raw_slip = (vehicle_speed - wheel_speed) / max(vehicle_speed, 0.1)
        self.slip_ratio = raw_slip
        
        # Adapt threshold based on surface grip
        # Lower grip (grip_mult > 1) = increase threshold (more tolerant of slip)
        # Higher grip (grip_mult < 1) = decrease threshold (less tolerant)
        self.effective_threshold = self._base_slip_threshold * grip_multiplier
        
        # Use smoothed slip ratio for lockup detection to reduce false triggers
        return self._smoothed_slip_ratio > self.effective_threshold
    
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
            self.slip_ratio = 0.0  # Clear slip ratio when not braking
            self._abs_phase = "apply"  # Reset phase for next intervention
            return throttle_input
        
        # Check for wheel lockup (uses smoothed slip ratio and grip adaptation)
        self.wheel_locked = self._detect_wheel_lockup(
            wheel_speed, vehicle_speed, self._current_grip_multiplier
        )
        
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
            "slip_ratio_smoothed": round(self._smoothed_slip_ratio, 3),
            "effective_threshold": round(self.effective_threshold, 3),
            "grip_multiplier": round(self._current_grip_multiplier, 2),
            "wheel_locked": self.wheel_locked,
        }
    
    def reset(self):
        """Reset state (call when race ends or connection resets)."""
        self._vehicle_direction = "stopped"
        self._last_cycle_time = 0
        self._abs_phase = "apply"
        self._intervention_active = False
        self.slip_ratio = 0.0
        self._smoothed_slip_ratio = 0.0
        self.effective_threshold = self._base_slip_threshold
        self._current_grip_multiplier = 1.0
        self.wheel_locked = False


# === Test / Demo ===

if __name__ == "__main__":
    import sys
    
    def run_unit_tests():
        """Run comprehensive unit tests for ABS system."""
        print("=" * 60)
        print("ABS Controller Unit Tests")
        print("=" * 60)
        
        passed = 0
        failed = 0
        
        def test(name, condition, details=""):
            nonlocal passed, failed
            if condition:
                print(f"  ✓ {name}")
                passed += 1
            else:
                print(f"  ✗ {name}")
                if details:
                    print(f"    {details}")
                failed += 1
        
        # === ThrottleStateTracker Tests ===
        print("\n[ThrottleStateTracker Tests]")
        
        # Throttle values need to exceed thresholds:
        # neutral threshold: ~1638, negative threshold: ~-3277
        BRAKE_THROTTLE = -5000   # Well below negative threshold
        ACCEL_THROTTLE = 5000    # Well above neutral threshold
        
        # Test 1: Forward motion + braking = braking state
        tracker = ThrottleStateTracker()
        state = tracker.update(BRAKE_THROTTLE, 20.0, -2.0)  # braking while forward
        test("Forward braking detected", state == "braking", f"got: {state}")
        tracker.reset()
        
        # Test 2: Stopped + neutral throttle = neutral
        tracker = ThrottleStateTracker()
        state = tracker.update(0, 0.5, 0.0)
        test("Stopped neutral detected", state == "neutral", f"got: {state}")
        tracker.reset()
        
        # Test 3: Reverse intent (stopped + backward accel + negative throttle)
        tracker = ThrottleStateTracker()
        tracker.update(BRAKE_THROTTLE, 15.0, -2.0)  # First, brake from forward
        tracker.update(BRAKE_THROTTLE, 1.0, -1.5)   # Now stopped with backward accel
        state = tracker.get_state()
        test("Reverse intent detected via accel", state == "reverse_armed", f"got: {state}")
        tracker.reset()
        
        # Test 4: Rapid direction change (forward -> stop -> reverse)
        tracker = ThrottleStateTracker()
        tracker.update(ACCEL_THROTTLE, 15.0, 2.0)    # Forward
        tracker.update(BRAKE_THROTTLE, 10.0, -3.0)   # Braking
        tracker.update(BRAKE_THROTTLE, 1.0, 0.0)     # Stopped, still braking
        tracker.update(0, 0.5, 0.0)                  # Neutral
        tracker.update(BRAKE_THROTTLE, 0.5, -1.0)    # Reverse intent
        state = tracker.get_state()
        test("Direction change sequence", state == "reverse_armed", f"got: {state}")
        tracker.reset()
        
        # Test 5: Moving backward = reversing
        tracker = ThrottleStateTracker()
        state = tracker.update(BRAKE_THROTTLE, -5.0, -1.0)
        test("Backward motion = reversing", state == "reversing", f"got: {state}")
        tracker.reset()
        
        # === ABSController Tests ===
        print("\n[ABSController Tests]")
        
        abs_ctrl = ABSController()
        tracker = ThrottleStateTracker()
        
        # Use properly scaled throttle values (config range 0-1000 scaled to 0-32767)
        BRAKE_THROTTLE = -15000  # Well below MIN_BRAKE_INPUT (~3277)
        ACCEL_THROTTLE = 15000
        
        # Test 6: No intervention when not braking
        abs_ctrl.reset()
        tracker.reset()
        esc_state = tracker.update(ACCEL_THROTTLE, 20.0, 2.0)  # Accelerating forward
        result = abs_ctrl.update(10.0, 20.0, 2.0, ACCEL_THROTTLE, esc_state, 1000)
        test("No intervention when accelerating", result == ACCEL_THROTTLE and not abs_ctrl.get_status()['active'])
        
        # Test 7: No intervention for normal braking (no lockup)
        abs_ctrl.reset()
        tracker.reset()
        # Prime sensor state
        for i in range(5):
            abs_ctrl.update_sensors(18.0, 20.0, -2.0, 1.0)
        esc_state = tracker.update(BRAKE_THROTTLE, 20.0, -2.0)
        result = abs_ctrl.update(18.0, 20.0, -2.0, BRAKE_THROTTLE, esc_state, 1000)
        test("No intervention for mild braking", result == BRAKE_THROTTLE, f"got: {result}")
        
        # Test 8: Intervention on wheel lockup
        abs_ctrl.reset()
        tracker.reset()
        # Prime the smoothed slip ratio with multiple updates (locked wheel)
        for i in range(10):
            abs_ctrl.update_sensors(2.0, 25.0, -3.0, 1.0)  # Severe lockup
        esc_state = tracker.update(BRAKE_THROTTLE, 25.0, -3.0)
        result = abs_ctrl.update(2.0, 25.0, -3.0, BRAKE_THROTTLE, esc_state, 1000)
        status = abs_ctrl.get_status()
        test("ABS activates on lockup", status['active'], 
             f"slip: {status['slip_ratio']:.2%}, smoothed: {status['slip_ratio_smoothed']:.2%}, thresh: {status['effective_threshold']:.2%}")
        
        # Test 9: No intervention when reversing
        abs_ctrl.reset()
        tracker.reset()
        esc_state = tracker.update(BRAKE_THROTTLE, -5.0, -1.0)  # Reversing
        result = abs_ctrl.update(5.0, -5.0, -1.0, BRAKE_THROTTLE, esc_state, 1000)
        test("No intervention when reversing", result == BRAKE_THROTTLE and not abs_ctrl.get_status()['active'])
        
        # Test 10: ABS disabled at low speed
        abs_ctrl.reset()
        tracker.reset()
        esc_state = tracker.update(BRAKE_THROTTLE, 2.0, -1.0)
        result = abs_ctrl.update(0.5, 2.0, -1.0, BRAKE_THROTTLE, esc_state, 1000)
        test("ABS disabled at low speed", result == BRAKE_THROTTLE)
        
        # Test 11: Slip ratio smoothing reduces noise sensitivity
        abs_ctrl.reset()
        tracker.reset()
        # Simulate noisy sensor with oscillating slip
        abs_ctrl.update_sensors(20.0, 25.0, -2.0, 1.0)  # Low slip
        abs_ctrl.update_sensors(5.0, 25.0, -3.0, 1.0)   # High slip spike
        abs_ctrl.update_sensors(19.0, 25.0, -2.0, 1.0)  # Low slip
        smoothed = abs_ctrl._smoothed_slip_ratio
        raw_would_be = (25.0 - 19.0) / 25.0
        test("Smoothing reduces noise", smoothed > raw_would_be, f"smoothed: {smoothed:.3f}")
        
        # Test 12: Surface adaptation scales threshold
        abs_ctrl.reset()
        tracker.reset()
        base_threshold = abs_ctrl._base_slip_threshold
        # Prime sensors with grip multiplier
        for i in range(5):
            abs_ctrl.update_sensors(10.0, 25.0, -2.0, 1.5)  # Low grip (mult > 1)
        # Trigger detection to update effective_threshold
        esc_state = tracker.update(BRAKE_THROTTLE, 25.0, -3.0)
        abs_ctrl.update(10.0, 25.0, -3.0, BRAKE_THROTTLE, esc_state, 1000)
        effective = abs_ctrl.effective_threshold
        test("Grip adaptation scales threshold", 
             abs(effective - base_threshold * 1.5) < 0.01,
             f"expected: {base_threshold * 1.5:.3f}, got: {effective:.3f}")
        
        # Test 13: Sensor dropout handling (wheel speed = 0 suddenly)
        abs_ctrl.reset()
        tracker.reset()
        abs_ctrl.update_sensors(20.0, 25.0, -2.0, 1.0)  # Normal
        # Should trigger lockup detection after priming
        for i in range(10):
            abs_ctrl.update_sensors(0.0, 25.0, -2.0, 1.0)  # Dropout (or real lockup)
        esc_state = tracker.update(BRAKE_THROTTLE, 25.0, -3.0)
        result = abs_ctrl.update(0.0, 25.0, -3.0, BRAKE_THROTTLE, esc_state, 1000)
        test("Handles sensor dropout/lockup", abs_ctrl.get_status()['active'],
             f"smoothed slip: {abs_ctrl._smoothed_slip_ratio:.2%}")
        
        # Test 14: ABS phase cycling
        abs_ctrl.reset()
        tracker.reset()
        # Prime slip ratio with lockup condition
        for i in range(10):
            abs_ctrl.update_sensors(2.0, 25.0, -3.0, 1.0)  # Severe lockup
        esc_state = tracker.update(BRAKE_THROTTLE, 25.0, -3.0)
        
        phases = []
        for i in range(10):
            abs_ctrl.update(2.0, 25.0, -3.0, BRAKE_THROTTLE, esc_state, 1000 + i * 60)
            phases.append(abs_ctrl._abs_phase)
        
        phase_changes = sum(1 for i in range(1, len(phases)) if phases[i] != phases[i-1])
        test("ABS cycles phases", phase_changes >= 2, f"phase changes: {phase_changes}, phases: {phases}")
        
        # Test 15: Reset clears all state
        abs_ctrl._intervention_active = True
        abs_ctrl._smoothed_slip_ratio = 0.5
        abs_ctrl._abs_phase = "release"
        abs_ctrl.reset()
        test("Reset clears state", 
             not abs_ctrl._intervention_active and 
             abs_ctrl._smoothed_slip_ratio == 0.0 and
             abs_ctrl._abs_phase == "apply")
        
        # Summary
        print("\n" + "=" * 60)
        print(f"Tests: {passed + failed} | Passed: {passed} | Failed: {failed}")
        print("=" * 60)
        
        return failed == 0
    
    def run_demo():
        """Run interactive demo scenarios."""
        abs_ctrl = ABSController()
        throttle_tracker = ThrottleStateTracker()
        
        print("ABS Controller Demo")
        print("=" * 50)
        
        # Use properly scaled throttle values (config range 0-1000 scaled to 0-32767)
        BRAKE = -15000  # Heavy braking
        LIGHT_BRAKE = -5000  # Light braking
        
        scenarios = [
            ("Normal braking (no lockup)", {
                "wheel_speed": 18.0, "vehicle_speed": 20.0, 
                "imu_accel": -2.0, "throttle": LIGHT_BRAKE
            }),
            ("Wheel lockup during braking", {
                "wheel_speed": 2.0, "vehicle_speed": 25.0,
                "imu_accel": -3.0, "throttle": BRAKE
            }),
            ("Intentional reverse (should NOT trigger)", {
                "wheel_speed": 5.0, "vehicle_speed": -5.0,
                "imu_accel": -1.0, "throttle": LIGHT_BRAKE
            }),
            ("Low speed braking (ABS disabled)", {
                "wheel_speed": 1.0, "vehicle_speed": 2.5,
                "imu_accel": -1.0, "throttle": LIGHT_BRAKE
            }),
            ("Surface adaptation - low grip", {
                "wheel_speed": 10.0, "vehicle_speed": 25.0,
                "imu_accel": -2.0, "throttle": BRAKE,
                "grip_mult": 1.5  # Low grip surface
            }),
        ]
        
        for name, params in scenarios:
            print(f"\n{name}:")
            
            grip_mult = params.get("grip_mult", 1.0)
            
            # Simulate sensor updates at IMU rate (more iterations for smoothing)
            for i in range(10):
                abs_ctrl.update_sensors(
                    params["wheel_speed"],
                    params["vehicle_speed"],
                    params["imu_accel"],
                    grip_mult
                )
            
            # Simulate several control cycles
            for i in range(10):
                esc_state = throttle_tracker.update(
                    params["throttle"], 
                    params["vehicle_speed"],
                    params["imu_accel"]
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
            print(f"  Slip Ratio: {status['slip_ratio']:.2%} (smoothed: {status['slip_ratio_smoothed']:.2%})")
            print(f"  Effective Threshold: {status['effective_threshold']:.2%} (grip mult: {status['grip_multiplier']:.2f})")
            print(f"  Input: {params['throttle']} -> Output: {result}")
            
            # Reset for next scenario
            abs_ctrl.reset()
            throttle_tracker.reset()
    
    # Run tests if --test flag, otherwise run demo
    if "--test" in sys.argv:
        success = run_unit_tests()
        sys.exit(0 if success else 1)
    else:
        run_demo()

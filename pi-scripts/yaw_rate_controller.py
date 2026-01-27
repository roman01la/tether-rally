#!/usr/bin/env python3
"""
Yaw-Rate Stability Controller (ESC-lite) for ARRMA Big Rock 3S

Prevents uncontrollable oversteer and understeer when driving over the internet
with 100–200ms latency. Reacts locally in 10–20ms using gyro Z.

Implements a simplified bicycle-model yaw-rate controller:
    r_des ≈ v / L * tan(δ)

When actual yaw rate differs significantly from desired:
- Oversteer (|r| > |r_des|): cut throttle aggressively
- Understeer (|r| < |r_des|): cut throttle mildly so front tires regain grip

Architecture:
- Runs at IMU rate (20–50 Hz) for fast local corrections
- Works in tandem with TractionControl (traction handles wheelspin,
  this handles vehicle rotation)

Usage:
    from yaw_rate_controller import YawRateController

    yrc = YawRateController(wheelbase_m=0.32)

    # In control loop (20–50 Hz):
    yrc.update(
        yaw_rate=imu_yaw_rate,       # deg/s from gyro Z
        speed=fused_speed,            # km/h
        steering_input=steering,      # -1000 to 1000
    )

    # Get outputs
    throttle_mult = yrc.get_throttle_multiplier()  # 0.0–1.0
    virtual_brake = yrc.get_virtual_brake()        # 0–1000 (brake intensity)

    limited_throttle = int(throttle * throttle_mult)
"""

import time
import math


class YawRateController:
    """
    Yaw-rate stability controller for remote-controlled vehicles.

    Designed for internet-latency scenarios where the human driver
    cannot react fast enough to oversteer/understeer events.
    """

    def __init__(self, wheelbase_m: float = 0.32):
        """
        Initialize yaw-rate controller.

        Args:
            wheelbase_m: Vehicle wheelbase in meters (wheel center to wheel center).
                         ARRMA Big Rock 3S wheelbase ≈ 320mm = 0.32m
        """
        # === Vehicle Parameters ===
        self.wheelbase = wheelbase_m

        # Max steering angle (degrees) at full servo deflection (±1000)
        # Typical 1:10 scale: ~30–35 degrees
        self.max_steering_angle_deg = 30.0

        # Understeer coefficient: real yaw rate vs kinematic ideal
        # Bicycle model assumes perfect grip; real cars slip.
        # Monster truck on loose surface: ~0.3–0.4
        # Grippy track: ~0.5–0.7
        self.grip_factor = 0.35

        # === Thresholds & Gains ===

        # Minimum speed (km/h) for stability control to engage
        # Below this, turning in place or very slow maneuvers - don't intervene
        self.min_speed_kmh = 5.0

        # Yaw error thresholds (deg/s)
        # Error = r_des - r_actual
        # Positive error: understeering (car not rotating enough)
        # Negative error: oversteering (car rotating too much)
        self.oversteer_threshold = 25.0   # deg/s over desired = oversteer
        self.understeer_threshold = 15.0  # deg/s under desired = understeer

        # Throttle cut rates (per update cycle)
        self.oversteer_cut_rate = 0.15      # Aggressive cut on oversteer
        self.understeer_cut_rate = 0.05     # Mild cut on understeer

        # Minimum throttle multiplier (never cut below this)
        self.min_throttle_mult = 0.30       # 30% is floor

        # Recovery rate (per update)
        self.recovery_rate = 0.03           # Gradual recovery when stable
        self.fast_recovery_rate = 0.08      # Faster when clearly stable

        # Virtual brake: additional braking on severe oversteer
        self.virtual_brake_enabled = True
        self.virtual_brake_threshold = 50.0  # deg/s error triggers brake
        self.max_virtual_brake = 400         # Max brake command (0-1000)

        # Smoothing for yaw rate (reduce noise)
        self.yaw_rate_smoothing = 0.4       # EMA alpha

        # === State ===
        self._throttle_multiplier = 1.0
        self._virtual_brake = 0
        self._yaw_rate_smooth = 0.0
        self._prev_time = time.time()
        self._intervention_active = False
        self._intervention_start = 0.0

        # Diagnostics
        self.yaw_rate_desired = 0.0
        self.yaw_rate_actual = 0.0
        self.yaw_error = 0.0
        self.intervention_type = "none"  # "oversteer", "understeer", "none"

        # Enable/disable
        self.enabled = True

    def update(self,
               yaw_rate: float,       # deg/s from gyro Z (positive = counterclockwise)
               speed: float,          # km/h (fused speed)
               steering_input: int):  # -1000 to 1000 (negative = left, positive = right)
        """
        Update controller state. Call at IMU rate (20–50 Hz).

        Args:
            yaw_rate: Actual yaw rate from gyro Z (deg/s, positive = CCW when viewed from above)
            speed: Vehicle speed (km/h)
            steering_input: Steering command (-1000 to 1000)
        """
        now = time.time()
        dt = now - self._prev_time
        self._prev_time = now

        # Clamp dt
        dt = max(0.001, min(0.1, dt))

        # Invert yaw rate: BNO055 is mounted upside-down (Z axis reversed)
        yaw_rate = -yaw_rate

        # Smooth yaw rate
        self._yaw_rate_smooth += self.yaw_rate_smoothing * (yaw_rate - self._yaw_rate_smooth)
        self.yaw_rate_actual = self._yaw_rate_smooth

        # Calculate desired yaw rate from steering and speed
        self.yaw_rate_desired = self._compute_desired_yaw_rate(speed, steering_input)

        # Compute yaw error
        # Positive = understeering (car turning less than commanded)
        # Negative = oversteering (car turning more than commanded)
        self.yaw_error = self.yaw_rate_desired - self.yaw_rate_actual

        # Determine intervention
        self._update_intervention(speed)

    def _compute_desired_yaw_rate(self, speed_kmh: float, steering: int) -> float:
        """
        Compute desired yaw rate using bicycle model.

        r_des = v / L * tan(δ)

        Args:
            speed_kmh: Speed in km/h
            steering: Steering command (-1000 to 1000)

        Returns:
            Desired yaw rate in deg/s
        """
        # Convert speed to m/s
        v = speed_kmh / 3.6

        # Convert steering command to angle
        # steering: -1000 (full left) to +1000 (full right)
        # For bicycle model, positive steering = right turn = negative yaw rate (clockwise)
        # But we'll keep signs consistent with gyro convention
        delta_deg = (steering / 1000.0) * self.max_steering_angle_deg
        delta_rad = math.radians(delta_deg)

        # Bicycle model: r = v / L * tan(delta)
        # At very small angles, tan(delta) ≈ delta, but we use exact tan
        if abs(delta_rad) < 0.001:
            return 0.0

        tan_delta = math.tan(delta_rad)

        # Yaw rate in rad/s
        r_rad_s = (v / self.wheelbase) * tan_delta

        # Convert to deg/s
        # Sign convention: positive steering (right) → negative yaw (clockwise when viewed from above)
        # Gyro Z typically: positive = counterclockwise
        # So positive steering → negative desired yaw rate
        r_deg_s = -math.degrees(r_rad_s)

        # Apply grip factor: real cars don't achieve kinematic yaw rate
        # due to tire slip (understeer gradient)
        r_deg_s *= self.grip_factor

        return r_deg_s

    def _update_intervention(self, speed_kmh: float):
        """
        Determine and apply stability intervention.
        """
        # Reset virtual brake each cycle
        self._virtual_brake = 0

        # Below minimum speed: no intervention
        if speed_kmh < self.min_speed_kmh:
            self.intervention_type = "none"
            self._recover()
            return

        # Check for oversteer/understeer
        # Use absolute error relative to desired direction
        abs_error = abs(self.yaw_error)
        abs_desired = abs(self.yaw_rate_desired)
        abs_actual = abs(self.yaw_rate_actual)

        # Oversteer: actual rotation exceeds desired
        # This happens when |r_actual| > |r_desired| in the same direction
        # Or when car rotates opposite to intended
        is_oversteer = False
        is_understeer = False

        if abs_desired < 5.0:
            # Driver wants to go straight-ish
            # Any significant rotation is oversteer
            if abs_actual > self.oversteer_threshold:
                is_oversteer = True
        else:
            # Driver is turning
            # Same sign: compare magnitudes
            same_direction = (self.yaw_rate_desired * self.yaw_rate_actual) > 0

            if same_direction:
                if abs_actual > abs_desired + self.oversteer_threshold:
                    is_oversteer = True
                elif abs_actual < abs_desired - self.understeer_threshold:
                    is_understeer = True
            else:
                # Opposite direction: definitely oversteer (spinning out)
                if abs_actual > 10.0:  # Some minimum threshold
                    is_oversteer = True

        # Apply intervention
        if is_oversteer:
            self.intervention_type = "oversteer"
            self._apply_oversteer_correction()
        elif is_understeer:
            self.intervention_type = "understeer"
            self._apply_understeer_correction()
        else:
            self.intervention_type = "none"
            self._recover()

    def _apply_oversteer_correction(self):
        """
        Aggressively cut throttle on oversteer.
        Optionally apply virtual brake.
        """
        self._intervention_active = True
        self._intervention_start = time.time()

        # Cut throttle
        self._throttle_multiplier = max(
            self.min_throttle_mult,
            self._throttle_multiplier - self.oversteer_cut_rate
        )

        # Virtual brake on severe oversteer
        if self.virtual_brake_enabled:
            oversteer_severity = abs(self.yaw_rate_actual) - abs(self.yaw_rate_desired)
            if oversteer_severity > self.virtual_brake_threshold:
                # Scale brake with severity
                brake_factor = min(1.0, (oversteer_severity - self.virtual_brake_threshold) / 50.0)
                self._virtual_brake = int(brake_factor * self.max_virtual_brake)

    def _apply_understeer_correction(self):
        """
        Mildly cut throttle on understeer to let front tires regain grip.
        """
        self._intervention_active = True
        self._intervention_start = time.time()

        self._throttle_multiplier = max(
            self.min_throttle_mult,
            self._throttle_multiplier - self.understeer_cut_rate
        )

    def _recover(self):
        """
        Gradually restore throttle when stable.
        """
        if not self._intervention_active:
            self._throttle_multiplier = 1.0
            return

        time_since_intervention = time.time() - self._intervention_start

        # Fast recovery if stable for a while
        if time_since_intervention > 0.2 and abs(self.yaw_error) < 10.0:
            rate = self.fast_recovery_rate
        else:
            rate = self.recovery_rate

        self._throttle_multiplier = min(1.0, self._throttle_multiplier + rate)

        if self._throttle_multiplier >= 1.0:
            self._intervention_active = False

    def get_throttle_multiplier(self) -> float:
        """
        Get throttle multiplier (0.0 to 1.0).
        Multiply throttle by this value.
        """
        if not self.enabled:
            return 1.0
        return self._throttle_multiplier

    def get_virtual_brake(self) -> int:
        """
        Get virtual brake command (0 to max_virtual_brake).
        Use this to apply braking on severe oversteer.
        
        Returns:
            Brake intensity (0–1000 scale, but capped at max_virtual_brake)
        """
        if not self.enabled:
            return 0
        return self._virtual_brake

    def apply_to_throttle(self, throttle: int) -> int:
        """
        Apply stability control to throttle.
        Only affects positive (forward) throttle.

        Args:
            throttle: Raw throttle (-1000 to 1000)

        Returns:
            Limited throttle
        """
        if not self.enabled or throttle <= 0:
            return throttle

        return int(throttle * self._throttle_multiplier)

    def get_status(self) -> dict:
        """Get diagnostic status."""
        return {
            "enabled": self.enabled,
            "intervention_type": self.intervention_type,
            "intervention_active": self._intervention_active,
            "throttle_multiplier": round(self._throttle_multiplier, 2),
            "virtual_brake": self._virtual_brake,
            "yaw_rate_desired": round(self.yaw_rate_desired, 1),
            "yaw_rate_actual": round(self.yaw_rate_actual, 1),
            "yaw_error": round(self.yaw_error, 1),
        }

    def reset(self):
        """Reset state (call on race end or reconnect)."""
        self._throttle_multiplier = 1.0
        self._virtual_brake = 0
        self._yaw_rate_smooth = 0.0
        self._intervention_active = False
        self.intervention_type = "none"
        self.yaw_error = 0.0


# === Test / Demo ===

if __name__ == "__main__":
    import random

    yrc = YawRateController(wheelbase_m=0.35)

    print("Yaw-Rate Controller Simulation")
    print("=" * 60)

    # Simulate scenarios
    # Sign convention (after sensor negation):
    #   steer > 0 (right) → desired yaw < 0 (clockwise)
    #   steer < 0 (left)  → desired yaw > 0 (counterclockwise)
    # Raw gyro input is negated in update() for upside-down mount,
    # so input yaw_rate here should be the RAW sensor value.
    # 
    # For a right turn (steer=+300): desired ≈ -38°/s
    # If car is stable, raw gyro reads +38°/s → after negation = -38°/s ✓
    scenarios = [
        ("Straight driving, stable",
         {"yaw_rate": -2.0, "speed": 20.0, "steering": 0}),  # tiny drift, raw=-2 → actual=+2
        ("Gentle right turn, stable",
         {"yaw_rate": 35.0, "speed": 15.0, "steering": 300}),  # raw=+35 → actual=-35, matches desired≈-38
        ("Oversteer: spin on corner exit (right turn)",
         {"yaw_rate": 90.0, "speed": 20.0, "steering": 200}),  # raw=+90 → actual=-90, desired≈-33 (too fast!)
        ("Understeer: pushing wide (right turn)",
         {"yaw_rate": 10.0, "speed": 25.0, "steering": 500}),  # raw=+10 → actual=-10, desired≈-107 (not turning enough)
        ("Severe oversteer: 180 spin",
         {"yaw_rate": 150.0, "speed": 18.0, "steering": 100}),  # raw=+150 → actual=-150, desired≈-15 (way too fast!)
        ("High speed straight, slight wobble",
         {"yaw_rate": -5.0, "speed": 35.0, "steering": 0}),  # raw=-5 → actual=+5
    ]

    for name, params in scenarios:
        print(f"\n{name}:")
        print(f"  Input: yaw={params['yaw_rate']}°/s, speed={params['speed']}km/h, steer={params['steering']}")

        # Run a few iterations
        for _ in range(15):
            yrc.update(
                yaw_rate=params["yaw_rate"] + random.uniform(-3, 3),
                speed=params["speed"],
                steering_input=params["steering"],
            )

        status = yrc.get_status()
        print(f"  Desired yaw: {status['yaw_rate_desired']:.1f}°/s")
        print(f"  Actual yaw:  {status['yaw_rate_actual']:.1f}°/s")
        print(f"  Yaw error:   {status['yaw_error']:.1f}°/s")
        print(f"  Intervention: {status['intervention_type']}")
        print(f"  Throttle mult: {status['throttle_multiplier']}")
        print(f"  Virtual brake: {status['virtual_brake']}")

        # Reset for next scenario
        yrc.reset()

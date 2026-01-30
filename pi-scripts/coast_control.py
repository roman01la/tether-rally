#!/usr/bin/env python3
"""
Coast Control / Regenerative Braking Simulation for ARRMA Big Rock 3S

Smooths the transition from throttle to coast on brushless RC cars.
When lifting throttle, brushless ESCs have aggressive drag braking
which feels jarring over latency. This controller adds a brief
counter-throttle to smooth the transition.

Behavior:
- When throttle drops from >100 to <50, start coast phase
- During coast phase (0.3s), inject decaying positive throttle
- This counteracts the ESC's drag brake for smoother deceleration

Usage:
    from coast_control import CoastControl
    
    coast = CoastControl()
    
    # In control loop:
    modified_throttle = coast.update(
        throttle_input=throttle,
        timestamp=time.time()
    )
"""

import time


class CoastControl:
    """
    Smooths the transition from throttle to coast.
    
    Instead of instant drag brake when throttle released,
    gradually reduce speed over configurable time.
    """
    
    def __init__(self):
        # === Tuning Parameters ===
        
        # Release detection
        self.RELEASE_FROM_THRESHOLD = 100   # Throttle was above this
        self.RELEASE_TO_THRESHOLD = 50      # Throttle dropped below this
        
        # Coast phase
        self.COAST_DURATION = 0.3           # Seconds of coast assist
        self.COAST_INITIAL_THROTTLE = 100   # Initial counter-throttle
        
        # Minimum speed for coast assist (don't assist at very low speed)
        self.MIN_SPEED_KMH = 5.0
        
        # === State ===
        self._last_throttle = 0
        self._release_time = 0.0
        self._coast_active = False
        self._prev_time = time.time()
        
        # Current coast injection value (for diagnostics)
        self.coast_injection = 0
        
        # Enable/disable
        self.enabled = True
    
    def _detect_release(self, throttle_input: int) -> bool:
        """
        Detect throttle release event.
        
        Returns True on the frame when throttle drops from
        above RELEASE_FROM to below RELEASE_TO.
        """
        was_throttling = self._last_throttle > self.RELEASE_FROM_THRESHOLD
        now_released = throttle_input < self.RELEASE_TO_THRESHOLD
        
        return was_throttling and now_released
    
    def _calculate_coast_injection(self, time_since_release: float) -> int:
        """
        Calculate counter-throttle to inject during coast phase.
        
        Linear decay from COAST_INITIAL to 0 over COAST_DURATION.
        """
        if time_since_release >= self.COAST_DURATION:
            return 0
        
        # Linear decay
        progress = time_since_release / self.COAST_DURATION
        injection = int(self.COAST_INITIAL_THROTTLE * (1 - progress))
        
        return max(0, injection)
    
    def update(self, 
               throttle_input: int,      # Driver throttle (-32767 to 32767 or -1000 to 1000)
               speed_kmh: float = None,  # Optional: current speed for min speed check
               timestamp: float = None) -> int:
        """
        Process throttle through coast control.
        
        Args:
            throttle_input: Driver throttle command
            speed_kmh: Current vehicle speed (optional, for min speed check)
            timestamp: Current time (defaults to time.time())
        
        Returns:
            Modified throttle (may include coast injection)
        """
        if timestamp is None:
            timestamp = time.time()
        
        if not self.enabled:
            self._last_throttle = throttle_input
            self.coast_injection = 0
            return throttle_input
        
        # Check for release event
        if self._detect_release(throttle_input):
            # Only activate if above minimum speed
            if speed_kmh is None or speed_kmh > self.MIN_SPEED_KMH:
                self._coast_active = True
                self._release_time = timestamp
        
        # Update last throttle for next frame
        self._last_throttle = throttle_input
        
        # If not coasting, pass through
        if not self._coast_active:
            self.coast_injection = 0
            return throttle_input
        
        # Calculate time since release
        time_since_release = timestamp - self._release_time
        
        # Check if coast phase is over
        if time_since_release >= self.COAST_DURATION:
            self._coast_active = False
            self.coast_injection = 0
            return throttle_input
        
        # Check if driver is actively throttling again
        if throttle_input > self.RELEASE_TO_THRESHOLD:
            self._coast_active = False
            self.coast_injection = 0
            return throttle_input
        
        # Check if driver is actively braking/reversing
        if throttle_input < -self.RELEASE_TO_THRESHOLD:
            self._coast_active = False
            self.coast_injection = 0
            return throttle_input
        
        # === COAST INJECTION ===
        self.coast_injection = self._calculate_coast_injection(time_since_release)
        
        # Add coast injection to throttle
        # If driver is at 0, we inject positive throttle
        # If driver is slightly negative, we might reduce braking effect
        modified = throttle_input + self.coast_injection
        
        # Don't let it go above the initial coast throttle
        modified = min(modified, self.COAST_INITIAL_THROTTLE)
        
        return modified
    
    def get_status(self) -> dict:
        """Get diagnostic status for telemetry."""
        return {
            "enabled": self.enabled,
            "active": self._coast_active,
            "injection": self.coast_injection,
            "last_throttle": self._last_throttle,
        }
    
    def reset(self):
        """Reset state (call when race ends or connection resets)."""
        self._last_throttle = 0
        self._release_time = 0.0
        self._coast_active = False
        self.coast_injection = 0


# === Test / Demo ===

if __name__ == "__main__":
    coast = CoastControl()
    
    print("Coast Control Simulation")
    print("=" * 50)
    
    # Simulate throttle release
    print("\nScenario: Throttle release from 500 to 0")
    print("-" * 30)
    
    # Ramp up throttle
    for throttle in [0, 200, 400, 500]:
        result = coast.update(throttle_input=throttle, speed_kmh=20.0, timestamp=time.time())
        print(f"  Throttle {throttle:4d} -> {result:4d} (coast: {coast.coast_injection})")
        time.sleep(0.05)
    
    print("\n  Release throttle:")
    
    # Release throttle and observe coast
    base_time = time.time()
    for i in range(15):
        t = base_time + i * 0.05
        result = coast.update(throttle_input=0, speed_kmh=20.0, timestamp=t)
        status = coast.get_status()
        print(f"  t={i*50:3d}ms: Input 0 -> Output {result:3d} (active: {status['active']}, injection: {status['injection']})")
    
    coast.reset()
    
    # Simulate throttle release then brake
    print("\n\nScenario: Release then immediate brake")
    print("-" * 30)
    
    # Throttle up
    coast.update(throttle_input=500, speed_kmh=20.0, timestamp=time.time())
    
    # Release and immediately brake
    t = time.time()
    result = coast.update(throttle_input=0, speed_kmh=20.0, timestamp=t)
    print(f"  Release: 0 -> {result} (coast active: {coast.get_status()['active']})")
    
    t += 0.05
    result = coast.update(throttle_input=-300, speed_kmh=20.0, timestamp=t)
    print(f"  Brake: -300 -> {result} (coast active: {coast.get_status()['active']})")

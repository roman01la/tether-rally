# Stability System Testing Guide

Testing scenarios for the ARRMA Big Rock 3S stability control systems.

## Systems Overview

| System                        | Purpose                                  | Key Metrics                               |
| ----------------------------- | ---------------------------------------- | ----------------------------------------- |
| **Traction Control (TC)**     | Prevents wheelspin on acceleration       | Slip Detected, Phase, Wheel/Vehicle Accel |
| **Yaw Rate Controller (ESC)** | Catches oversteer/understeer             | Intervention type, Desired vs Actual yaw  |
| **ABS**                       | Anti-lock braking                        | ESC State, Direction, Phase, Slip Ratio   |
| **Hill Hold**                 | Holds car on inclines                    | Pitch, Hold Force, Blend                  |
| **Coast Control**             | Prevents rollback during coasting        | Active, Injection amount                  |
| **Surface Adaptation**        | Adjusts for grip level                   | Grip estimate, Multiplier, Measuring      |
| **Slip Angle Watchdog**       | Monitors drift angle                     | Heading vs Course angle                   |
| **Steering Shaper**           | Limits steering at speed, smooths inputs | Limit %, Rate Limited, Counter-Steer      |

---

## Test Scenarios

### 1. Traction Control - Wheelspin Detection

**Goal:** Trigger "Slip Detected: yes"

**Setup:**

- Find loose surface (dirt, gravel, grass)
- Start from standstill

**Procedure:**

1. Hold car stationary
2. Apply full throttle suddenly (slam it)
3. Watch debug panel during wheel spin-up

**Expected:**

- Wheel Accel spikes > 3 m/s²
- Vehicle Accel stays low < 1 m/s²
- Slip Detected: **yes**
- Phase: `launch` → `transition` → `cruise`
- Multiplier drops (down to 70%)
- TC bar in pipeline turns orange/smaller

**Status:** ❌ Not yet verified

---

### 2. Yaw Rate (ESC) - Oversteer

**Goal:** Trigger "OVERSTEER" intervention

**Setup:**

- Open area
- Some speed

**Procedure:**

1. Drive forward or reverse
2. Apply full steering while throttling
3. Let the rear end swing out (oversteer/spin)

**Expected:**

- Intervention: **OVERSTEER**
- Actual yaw opposite sign or much larger than Desired
- Error > 50°/s
- Multiplier drops (saw 77%, 92%)
- Virtual Brake may engage
- ESC bar turns orange

**Status:** ✅ Verified working

---

### 3. Yaw Rate (ESC) - Understeer

**Goal:** Trigger "UNDERSTEER" intervention

**Setup:**

- Corner or turn area
- Build up some speed

**Procedure:**

1. Drive at moderate speed
2. Enter a corner with steering input
3. Keep throttle on (don't lift)
4. Car should push wide (understeer/plow)

**Expected:**

- Intervention: **UNDERSTEER**
- Actual yaw rate less than Desired (car not turning as much as commanded)
- Error shows car under-rotating
- Multiplier reduces slightly

**Status:** ❌ Not yet verified

---

### 4. Slip Angle - Sustained Drift

**Goal:** Trigger Slip Angle intervention

**Setup:**

- Open area
- Slippery surface helps

**Procedure:**

1. Build up speed
2. Initiate a drift/slide
3. Hold it — try to maintain high slip angle
4. Need angle > 15° at speed for intervention

**Expected:**

- Slip Angle gauge shows > 15°
- Intervention: **yes**
- Multiplier drops below 100%
- Slip bar in pipeline changes

**Status:** ⚠️ Saw 37° angle but no intervention (may need more speed/sustained)

---

### 5. Steering Shaper - High Speed Limiting

**Goal:** See steering limit drop significantly at speed

**Setup:**

- Long straight area
- Build up speed

**Procedure:**

1. Drive fast in a straight line
2. At high speed, apply steering
3. Watch the "Limit" percentage

**Expected:**

- Limit drops well below 96%
- Higher speed = lower limit
- Prevents snap oversteer at speed

**Status:** ⚠️ Saw 96-97%, need higher speed test

---

### 6. Steering Shaper - Rate Limiting

**Goal:** Trigger "Rate Limited: yes"

**Setup:**

- Any driving situation

**Procedure:**

1. Make very sudden, jerky steering inputs
2. Snap the steering from side to side quickly

**Expected:**

- Rate Limited: **yes**
- Steering feels smoothed/delayed
- Prevents latency-induced oscillations

**Status:** ✅ Verified working (fixed by increasing limits)

---

### 7. Steering Shaper - Counter-Steer Detection

**Goal:** Trigger "Counter-Steer: yes"

**Setup:**

- Open area

**Procedure:**

1. Initiate a drift/oversteer
2. Counter-steer to catch it (steer opposite to the slide)
3. System should detect you're correcting

**Expected:**

- Counter-Steer: **yes**
- Assist Amount shows value
- System may assist your correction

**Status:** ❌ Not yet verified

---

### 8. ABS - Wheel Lockup Prevention

**Goal:** Trigger ABS intervention during braking

**Setup:**

- Open area with some speed
- Smooth surface (asphalt/concrete)

**Procedure:**

1. Drive forward at moderate speed
2. Apply full reverse throttle (brake hard)
3. Watch debug panel for ABS activity

**Expected:**

- ESC State: `N` → `BRK` → `ARM` → `REV`
- ABS badge lights up during lockup
- Phase cycles: **APPLY** ↔ **RELEASE**
- Slip ratio spikes then recovers
- Braking feels modulated, not locked

**Status:** ❌ Not yet verified

---

### 9. Hill Hold - Incline Detection

**Goal:** Trigger hill hold on a slope

**Setup:**

- Find an incline (driveway, ramp, hill)
- Car pointing uphill

**Procedure:**

1. Drive partway up the incline
2. Release throttle completely
3. Watch pitch gauge and HOLD badge

**Expected:**

- Pitch shows positive angle (e.g., +8°)
- HOLD badge activates
- Hold Force bar shows positive (forward force)
- Car holds position, doesn't roll back
- Blend shows throttle handoff when resuming

**Status:** ❌ Not yet verified

---

### 10. Coast Control - Rollback Prevention

**Goal:** Trigger coast injection

**Setup:**

- Slight incline or resistance situation
- After acceleration

**Procedure:**

1. Accelerate forward
2. Release throttle (coast)
3. Watch for injection bar in debug panel

**Expected:**

- Coast Active: **ACTIVE**
- Injection shows small positive value
- Prevents unwanted deceleration/rollback
- Smooth coasting behavior

**Status:** ❌ Not yet verified

---

### 11. Surface Adaptation - Grip Estimation

**Goal:** See grip estimate change with surface

**Setup:**

- Drive on multiple surfaces (asphalt, dirt, grass)

**Procedure:**

1. Drive on high-grip surface (asphalt)
2. Note grip reading (~0.8-1.0)
3. Transition to low-grip surface (gravel, grass)
4. Watch measuring dot and grip bar

**Expected:**

- Measuring dot pulses when sampling
- Grip value changes with surface (lower on loose)
- Multiplier adjusts TC/ESC thresholds
- Low grip → higher multiplier (more sensitive)

**Status:** ❌ Not yet verified

---

## Debug Panel Reference

### Throttle Pipeline Bars

```
Input → TC → ESC → Slip → Final
```

- Green = 100% (no intervention)
- Orange/Red = reduced (intervention active)
- Smaller bar = more throttle cut

### Yaw Rate Indicator

```
[-------|Desired|----Actual----]
```

- Blue = Desired (from steering input)
- Orange = Actual (from IMU)
- Far apart = oversteer/understeer

### Slip Angle Gauge

- Needle shows angle between heading and course
- Red zone = high slip angle
- 0° = perfect tracking

---

## Tuning Notes

Current thresholds (can be adjusted):

**Traction Control:**

- Wheel Accel threshold: 3.0 m/s² (lowered for testing)
- Vehicle Accel threshold: 1.0 m/s² (lowered for testing)
- Min throttle: 13000 (~40% of int16)

**Yaw Rate (ESC):**

- Uses steering input to calculate expected yaw rate
- Compares to actual IMU yaw rate

**Slip Angle:**

- Intervention threshold: 15°
- Requires GPS speed for heading vs course

**Steering Shaper:**

- Max steering rate: 300,000 units/sec
- Max center rate: 400,000 units/sec
- Speed-based limiting curve

---

## Test Log

| Date       | Scenario      | Result   | Notes                         |
| ---------- | ------------- | -------- | ----------------------------- |
| 2026-01-28 | ESC Oversteer | ✅ Pass  | 77% multiplier, 244°/s error  |
| 2026-01-28 | ESC Oversteer | ✅ Pass  | 92% multiplier, -284°/s error |
| 2026-01-28 | Rate Limiting | ✅ Fixed | Increased rate limits         |
|            |               |          |                               |

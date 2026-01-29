#!/usr/bin/env python3
"""
Hall Effect Sensor RPM Reader

Calculates wheel RPM using a Hall effect sensor triggered by a magnet
on the wheel. Uses GPIO interrupts for accurate pulse detection.

Usage:
    from hall_rpm import HallRPM
    
    rpm_sensor = HallRPM(gpio_pin=22, magnets_per_rev=1)
    rpm_sensor.start()
    
    # Read current RPM
    rpm = rpm_sensor.get_rpm()
    
    # Cleanup
    rpm_sensor.stop()
"""

import time
import threading
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None


class HallRPM:
    """
    Hall effect sensor RPM calculator using GPIO interrupts.
    
    Args:
        gpio_pin: BCM GPIO pin number (default: 22)
        magnets_per_rev: Number of magnets on the wheel (default: 1)
        timeout: Seconds without pulse before RPM is considered 0 (default: 0.5)
        debounce_ms: Software debounce time in milliseconds (default: 5)
    """
    
    def __init__(self, gpio_pin: int = 22, magnets_per_rev: int = 1, 
                 timeout: float = 0.5, debounce_ms: int = 5):
        self.gpio_pin = gpio_pin
        self.magnets_per_rev = magnets_per_rev
        self.timeout = timeout
        self.debounce_ms = debounce_ms
        
        # State
        self._last_pulse_time = 0.0
        self._pulse_interval = 0.0  # Time between last two pulses
        self._pulse_count = 0
        self._lock = threading.Lock()
        self._running = False
        
    def start(self) -> bool:
        """Initialize GPIO and start listening for pulses. Returns True on success."""
        if GPIO is None:
            print("RPi.GPIO not available")
            return False
            
        if self._running:
            return True
            
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            
            # Use falling edge detection (sensor pulls low when magnet passes)
            GPIO.add_event_detect(
                self.gpio_pin, 
                GPIO.FALLING, 
                callback=self._pulse_callback,
                bouncetime=self.debounce_ms
            )
            
            self._running = True
            self._last_pulse_time = time.time()
            print(f"Hall RPM sensor started on GPIO {self.gpio_pin}")
            return True
            
        except Exception as e:
            print(f"Failed to start Hall RPM sensor: {e}")
            return False
    
    def stop(self):
        """Stop listening and cleanup GPIO."""
        if self._running:
            try:
                GPIO.remove_event_detect(self.gpio_pin)
                GPIO.cleanup(self.gpio_pin)
            except Exception:
                pass
            self._running = False
            print("Hall RPM sensor stopped")
    
    def _pulse_callback(self, channel):
        """Called on each magnet pass (falling edge)."""
        now = time.time()
        
        with self._lock:
            if self._last_pulse_time > 0:
                self._pulse_interval = now - self._last_pulse_time
            self._last_pulse_time = now
            self._pulse_count += 1
    
    def get_rpm(self) -> float:
        """
        Get current RPM based on pulse interval.
        Returns 0 if no recent pulses (wheel stopped).
        """
        with self._lock:
            # Check if wheel has stopped (no pulse within timeout)
            if time.time() - self._last_pulse_time > self.timeout:
                return 0.0
            
            # Need at least one interval measurement
            if self._pulse_interval <= 0:
                return 0.0
            
            # Calculate RPM: (60 seconds / interval) / magnets_per_rev
            # interval is time for one magnet pass
            rpm = (60.0 / self._pulse_interval) / self.magnets_per_rev
            return rpm
    
    def get_pulse_count(self) -> int:
        """Get total pulse count since start."""
        with self._lock:
            return self._pulse_count
    
    def reset_pulse_count(self):
        """Reset the pulse counter to zero."""
        with self._lock:
            self._pulse_count = 0
    
    def get_stats(self) -> dict:
        """Get all sensor statistics."""
        with self._lock:
            now = time.time()
            time_since_pulse = now - self._last_pulse_time if self._last_pulse_time > 0 else float('inf')
            
            return {
                'rpm': self.get_rpm(),
                'pulse_count': self._pulse_count,
                'pulse_interval_ms': self._pulse_interval * 1000 if self._pulse_interval > 0 else 0,
                'time_since_pulse': time_since_pulse,
                'running': self._running
            }
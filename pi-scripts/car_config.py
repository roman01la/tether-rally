#!/usr/bin/env python3
"""
Car Configuration Loader

Loads car-specific tuning parameters from INI profile files.
Profile selection via CAR_PROFILE environment variable.

Usage:
    export CAR_PROFILE=badlands_4kg
    python control-relay.py
    
    # Or in systemd service:
    Environment=CAR_PROFILE=badlands_4kg

Profile files are located in pi-scripts/profiles/{CAR_PROFILE}.ini
All parameters must be present - missing values cause startup error.
"""

import os
import configparser
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Singleton config instance
_config: Optional['CarConfig'] = None


class CarConfigError(Exception):
    """Raised when car configuration is invalid or missing."""
    pass


class CarConfig:
    """
    Car configuration loaded from INI profile.
    
    Provides typed getters for all tunable parameters.
    Validates that all required parameters are present.
    """
    
    # Required sections and their required keys
    REQUIRED_SCHEMA = {
        'vehicle': [
            'wheel_diameter_mm',
            'weight_kg',
            'wheelbase_m',
            'max_steering_angle_deg',
        ],
        'heading_blend': [
            'imu_only_speed_kmh',
            'gps_blend_speed_kmh',
            'heading_smooth_alpha',
            'imu_mount_offset_deg',
        ],
        'speed_fusion': [
            'fusion_alpha',
            'imu_integrate_rate',
            'gps_drift_correction_alpha',
            'gps_drift_correction_min_speed_kmh',
            'wheelspin_detect_ratio',
            'wheelspin_detect_time_s',
            'wheelspin_max_fused_ratio',
            'stationary_timeout_s',
            'stationary_decay_rate',
            'imu_accel_noise_threshold',
        ],
        'low_speed_traction': [
            'launch_phase_end_kmh',
            'transition_phase_end_kmh',
            'launch_target_slip',
            'launch_slip_tolerance',
            'launch_max_throttle_rate',
            'launch_throttle_ceiling',
            'launch_slip_high_cut',
            'cruise_slip_threshold',
            'cruise_throttle_cut_rate',
            'cruise_recovery_rate',
            'cruise_min_multiplier',
            'min_throttle_for_slip',
            'yaw_rate_threshold',
            'accel_smoothing',
            'gps_drift_correction_alpha',
            'gps_drift_correction_min_speed_kmh',
        ],
        'yaw_rate_controller': [
            'grip_factor',
            'min_speed_kmh',
            'oversteer_threshold',
            'understeer_threshold',
            'oversteer_cut_rate',
            'understeer_cut_rate',
            'min_throttle_mult',
            'recovery_rate',
            'fast_recovery_rate',
            'virtual_brake_enabled',
            'virtual_brake_threshold',
            'max_virtual_brake',
            'yaw_smoothing',
        ],
        'slip_angle_watchdog': [
            'min_speed_kmh',
            'lateral_excess_threshold',
            'slip_duration_threshold_s',
            'min_throttle_for_intervention',
            'recovery_target',
            'reduction_rate',
            'recovery_rate',
            'min_multiplier',
            'smoothing_alpha',
        ],
        'surface_adaptation': [
            'min_speed_kmh',
            'min_steering',
            'min_samples',
            'default_grip',
            'grip_smoothing',
            'grip_min',
            'grip_max',
            'history_size',
        ],
        'hill_hold': [
            'pitch_threshold_deg',
            'speed_threshold_kmh',
            'throttle_deadzone',
            'hold_strength',
            'max_hold_force',
            'immediate_release_threshold',
            'blend_rate',
            'timeout_s',
        ],
        'abs': [
            'slip_threshold',
            'min_speed_kmh',
            'min_brake_input',
            'direction_hysteresis_kmh',
            'accel_direction_threshold',
            'cycle_time_ms',
            'brake_apply_ratio',
            'brake_release_ratio',
        ],
        'coast_control': [
            'release_threshold_high',
            'release_threshold_low',
            'coast_duration_s',
            'coast_throttle',
            'min_speed_kmh',
        ],
        'steering_shaper': [
            'max_steering_ratio',
            'min_steering_ratio',
            'low_speed_kmh',
            'high_speed_kmh',
            'max_rate',
            'center_rate',
            'counter_steer_enabled',
            'counter_steer_min_yaw',
            'counter_steer_strength',
            'counter_steer_max_input',
            'counter_steer_min_speed_kmh',
            'counter_steer_max_amount',
            'smoothing_alpha',
        ],
    }
    
    def __init__(self, profile_path: Path):
        """Load and validate configuration from INI file."""
        # Disable interpolation to allow % in comments
        # Enable inline comments with # character
        self._config = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=('#',)
        )
        self._profile_path = profile_path
        
        if not profile_path.exists():
            raise CarConfigError(f"Profile not found: {profile_path}")
        
        self._config.read(profile_path)
        self._validate()
        
        logger.info(f"Loaded car profile: {profile_path.stem}")
    
    def _validate(self):
        """Validate all required sections and keys are present."""
        missing = []
        
        for section, keys in self.REQUIRED_SCHEMA.items():
            if section not in self._config:
                missing.append(f"[{section}] section")
                continue
            
            for key in keys:
                if key not in self._config[section]:
                    missing.append(f"[{section}].{key}")
        
        if missing:
            raise CarConfigError(
                f"Profile {self._profile_path.name} missing required values:\n  " +
                "\n  ".join(missing)
            )
    
    def _get(self, section: str, key: str) -> str:
        """Get raw string value."""
        return self._config[section][key]
    
    def get_float(self, section: str, key: str) -> float:
        """Get float value."""
        try:
            return self._config.getfloat(section, key)
        except ValueError as e:
            raise CarConfigError(f"[{section}].{key} must be a number: {e}")
    
    def get_int(self, section: str, key: str) -> int:
        """Get integer value."""
        try:
            return self._config.getint(section, key)
        except ValueError as e:
            raise CarConfigError(f"[{section}].{key} must be an integer: {e}")
    
    def get_bool(self, section: str, key: str) -> bool:
        """Get boolean value (true/false, yes/no, 1/0)."""
        try:
            return self._config.getboolean(section, key)
        except ValueError as e:
            raise CarConfigError(f"[{section}].{key} must be a boolean (true/false): {e}")
    
    # === Convenience Properties ===
    
    # Vehicle
    @property
    def wheel_diameter_mm(self) -> int:
        return self.get_int('vehicle', 'wheel_diameter_mm')
    
    @property
    def weight_kg(self) -> float:
        return self.get_float('vehicle', 'weight_kg')
    
    @property
    def wheelbase_m(self) -> float:
        return self.get_float('vehicle', 'wheelbase_m')
    
    @property
    def max_steering_angle_deg(self) -> float:
        return self.get_float('vehicle', 'max_steering_angle_deg')


def get_config() -> CarConfig:
    """
    Get the singleton car configuration.
    
    Loads from profiles/{CAR_PROFILE}.ini on first call.
    Raises CarConfigError if CAR_PROFILE not set or profile invalid.
    """
    global _config
    
    if _config is None:
        profile_name = os.environ.get('CAR_PROFILE')
        
        if not profile_name:
            raise CarConfigError(
                "CAR_PROFILE environment variable not set.\n"
                "Set it to the name of a profile in pi-scripts/profiles/\n"
                "Example: export CAR_PROFILE=badlands_4kg"
            )
        
        # Find profiles directory relative to this file
        script_dir = Path(__file__).parent
        profile_path = script_dir / 'profiles' / f'{profile_name}.ini'
        
        _config = CarConfig(profile_path)
    
    return _config


def reload_config(profile_name: str = None) -> CarConfig:
    """
    Force reload of configuration.
    
    Args:
        profile_name: Optional profile name to load. If not provided,
                      uses CAR_PROFILE environment variable.
    
    Returns:
        Reloaded CarConfig instance.
    """
    global _config
    _config = None
    
    if profile_name:
        os.environ['CAR_PROFILE'] = profile_name
    
    return get_config()

#!/usr/bin/env python3
"""
BNO055 I2C reader for heading/calibration data.
Runs as async task alongside GPS reader.

Test standalone:
    python3 bno055_reader.py
"""

import asyncio
import struct
import logging

logger = logging.getLogger(__name__)

# BNO055 I2C address (0x28 default, 0x29 if ADR pin pulled high)
BNO055_ADDRESS = 0x28

# Registers
REG_CHIP_ID = 0x00          # Should read 0xA0
REG_PAGE_ID = 0x07          # Page selection
REG_OPR_MODE = 0x3D         # Operation mode
REG_PWR_MODE = 0x3E         # Power mode
REG_SYS_TRIGGER = 0x3F      # System trigger
REG_UNIT_SEL = 0x3B         # Unit selection
REG_CALIB_STAT = 0x35       # Calibration status

# Data registers (page 0)
REG_EUL_HEADING_LSB = 0x1A  # Euler heading (2 bytes, LSB first)
REG_EUL_ROLL_LSB = 0x1C     # Euler roll (2 bytes)
REG_EUL_PITCH_LSB = 0x1E    # Euler pitch (2 bytes)
REG_GYR_DATA_Z_LSB = 0x18   # Gyro Z (yaw rate, 2 bytes)
REG_LIA_DATA_X_LSB = 0x28   # Linear acceleration X (2 bytes, gravity-free)
REG_LIA_DATA_Y_LSB = 0x2A   # Linear acceleration Y (2 bytes)
REG_LIA_DATA_Z_LSB = 0x2C   # Linear acceleration Z (2 bytes)

# Calibration offset registers (22 bytes total, must be in CONFIG mode to write)
REG_ACC_OFFSET_X_LSB = 0x55  # Accel offsets (6 bytes)
REG_MAG_OFFSET_X_LSB = 0x5B  # Mag offsets (6 bytes)
REG_GYR_OFFSET_X_LSB = 0x61  # Gyro offsets (6 bytes)
REG_ACC_RADIUS_LSB = 0x67    # Accel radius (2 bytes)
REG_MAG_RADIUS_LSB = 0x69    # Mag radius (2 bytes)
CALIBRATION_DATA_SIZE = 22

# Operation modes
OPR_MODE_CONFIG = 0x00      # Configuration mode
OPR_MODE_NDOF = 0x0C        # 9-DOF fusion (uses magnetometer)
OPR_MODE_IMUPLUS = 0x08     # 6-DOF fusion (gyro + accel, no mag)

# Power modes
PWR_MODE_NORMAL = 0x00


class BNO055:
    """BNO055 9-DOF IMU driver for heading/orientation data"""
    
    def __init__(self, i2c_bus: int = 1, address: int = BNO055_ADDRESS):
        self.bus_num = i2c_bus
        self.address = address
        self.bus = None
        self._initialized = False
    
    async def init(self, calibration_data: bytes = None) -> bool:
        """
        Initialize BNO055 in NDOF mode (full 9-DOF fusion with magnetometer).
        
        Args:
            calibration_data: Optional 22-byte calibration data to restore.
                              Must be passed during init for proper restore.
        """
        try:
            import smbus2
            self.bus = smbus2.SMBus(self.bus_num)
            
            # Check chip ID
            chip_id = self.bus.read_byte_data(self.address, REG_CHIP_ID)
            if chip_id != 0xA0:
                logger.error(f"BNO055 chip ID mismatch: {chip_id:#x} (expected 0xA0)")
                return False
            
            logger.info(f"BNO055 chip ID verified: {chip_id:#x}")
            
            # Switch to config mode first
            self.bus.write_byte_data(self.address, REG_OPR_MODE, OPR_MODE_CONFIG)
            await asyncio.sleep(0.025)  # Wait for mode switch
            
            # Select page 0 (default, but be explicit)
            self.bus.write_byte_data(self.address, REG_PAGE_ID, 0)
            
            # Set units: degrees (not radians), Celsius, m/s²
            # Bit 0: acceleration (0=m/s², 1=mg)
            # Bit 1: gyro (0=dps, 1=rps)  
            # Bit 2: euler (0=degrees, 1=radians)
            # Bit 4: temperature (0=Celsius, 1=Fahrenheit)
            # Bit 7: orientation (0=Windows, 1=Android)
            self.bus.write_byte_data(self.address, REG_UNIT_SEL, 0x00)
            
            # Normal power mode
            self.bus.write_byte_data(self.address, REG_PWR_MODE, PWR_MODE_NORMAL)
            await asyncio.sleep(0.01)
            
            # Restore calibration data BEFORE switching to NDOF mode
            if calibration_data and len(calibration_data) == CALIBRATION_DATA_SIZE:
                self._write_calibration_offsets(calibration_data)
                logger.info("Calibration offsets written during init")
            
            # Switch to NDOF mode (full fusion with magnetometer)
            self.bus.write_byte_data(self.address, REG_OPR_MODE, OPR_MODE_NDOF)
            await asyncio.sleep(0.02)  # Wait for mode switch
            
            self._initialized = True
            logger.info("BNO055 initialized in NDOF mode (9-DOF fusion)")
            return True
            
        except ImportError:
            logger.error("smbus2 not installed. Run: pip3 install smbus2")
            return False
        except Exception as e:
            logger.error(f"BNO055 init failed: {e}")
            return False
    
    def read_heading(self) -> float | None:
        """
        Read fused Euler heading (0-360 degrees, 0=North, clockwise).
        Returns None on error.
        """
        if not self._initialized or not self.bus:
            return None
        try:
            # Read 2 bytes: LSB, MSB
            data = self.bus.read_i2c_block_data(self.address, REG_EUL_HEADING_LSB, 2)
            # BNO055 outputs heading as signed 16-bit in 1/16 degree units
            heading_raw = struct.unpack('<h', bytes(data))[0]
            heading = heading_raw / 16.0
            # Normalize to 0-360
            return heading % 360.0
        except Exception as e:
            logger.warning(f"BNO055 heading read error: {e}")
            return None
    
    def read_euler(self) -> tuple[float, float, float] | None:
        """
        Read all Euler angles: (heading, roll, pitch) in degrees.
        Returns None on error.
        """
        if not self._initialized or not self.bus:
            return None
        try:
            # Read 6 bytes: heading(2), roll(2), pitch(2)
            data = self.bus.read_i2c_block_data(self.address, REG_EUL_HEADING_LSB, 6)
            heading_raw, roll_raw, pitch_raw = struct.unpack('<hhh', bytes(data))
            return (
                (heading_raw / 16.0) % 360.0,
                roll_raw / 16.0,
                pitch_raw / 16.0
            )
        except Exception as e:
            logger.warning(f"BNO055 euler read error: {e}")
            return None
    
    def read_pitch(self) -> float | None:
        """
        Read pitch angle (nose up/down) in degrees.
        
        Positive = nose up (climbing)
        Negative = nose down (descending)
        
        Note: When IMU is mounted upside-down, pitch sign may need to be negated
        in the calling code (see control-relay.py IMU_MOUNT_OFFSET handling).
        
        Returns None on error.
        """
        if not self._initialized or not self.bus:
            return None
        try:
            # Read pitch from euler angles (offset 0x1E, 2 bytes)
            data = self.bus.read_i2c_block_data(self.address, REG_EUL_PITCH_LSB, 2)
            # BNO055 outputs pitch as signed 16-bit in 1/16 degree units
            pitch_raw = struct.unpack('<h', bytes(data))[0]
            return pitch_raw / 16.0
        except Exception as e:
            logger.warning(f"BNO055 pitch read error: {e}")
            return None
    
    def read_roll(self) -> float | None:
        """
        Read roll angle (tilt left/right) in degrees.
        
        Positive = roll right
        Negative = roll left
        
        Returns None on error.
        """
        if not self._initialized or not self.bus:
            return None
        try:
            # Read roll from euler angles (offset 0x1C, 2 bytes)
            data = self.bus.read_i2c_block_data(self.address, REG_EUL_ROLL_LSB, 2)
            # BNO055 outputs roll as signed 16-bit in 1/16 degree units
            roll_raw = struct.unpack('<h', bytes(data))[0]
            return roll_raw / 16.0
        except Exception as e:
            logger.warning(f"BNO055 roll read error: {e}")
            return None
    
    def read_yaw_rate(self) -> float | None:
        """
        Read gyro Z axis (yaw rate) in degrees per second.
        Positive = clockwise rotation when viewed from above.
        Returns None on error.
        """
        if not self._initialized or not self.bus:
            return None
        try:
            data = self.bus.read_i2c_block_data(self.address, REG_GYR_DATA_Z_LSB, 2)
            # BNO055 outputs gyro in 1/16 dps units (when in degree mode)
            yaw_rate_raw = struct.unpack('<h', bytes(data))[0]
            return yaw_rate_raw / 16.0
        except Exception as e:
            logger.warning(f"BNO055 yaw rate read error: {e}")
            return None
    
    def read_linear_acceleration(self) -> tuple[float, float, float] | None:
        """
        Read linear acceleration (gravity-compensated) in m/s².
        Returns (x, y, z) where:
        - X = forward/backward (positive = forward)
        - Y = left/right (positive = right)
        - Z = up/down (positive = up)
        
        Note: Actual axis mapping depends on IMU mounting orientation.
        Returns None on error.
        """
        if not self._initialized or not self.bus:
            return None
        try:
            # Read 6 bytes: X(2), Y(2), Z(2)
            data = self.bus.read_i2c_block_data(self.address, REG_LIA_DATA_X_LSB, 6)
            # BNO055 outputs acceleration in 1/100 m/s² units
            x_raw, y_raw, z_raw = struct.unpack('<hhh', bytes(data))
            return (
                x_raw / 100.0,
                y_raw / 100.0,
                z_raw / 100.0
            )
        except Exception as e:
            logger.warning(f"BNO055 linear accel read error: {e}")
            return None

    def read_calibration(self) -> dict:
        """
        Read calibration status for each subsystem.
        Returns dict with keys: sys, gyr, acc, mag
        Values are 0-3 (0=uncalibrated, 3=fully calibrated)
        """
        if not self._initialized or not self.bus:
            return {'sys': 0, 'gyr': 0, 'acc': 0, 'mag': 0}
        try:
            stat = self.bus.read_byte_data(self.address, REG_CALIB_STAT)
            return {
                'sys': (stat >> 6) & 0x03,
                'gyr': (stat >> 4) & 0x03,
                'acc': (stat >> 2) & 0x03,
                'mag': stat & 0x03
            }
        except Exception as e:
            logger.warning(f"BNO055 calibration read error: {e}")
            return {'sys': 0, 'gyr': 0, 'acc': 0, 'mag': 0}
    
    def is_calibrated(self) -> bool:
        """Check if magnetometer is reasonably calibrated (≥2)"""
        cal = self.read_calibration()
        return cal['mag'] >= 2 and cal['gyr'] >= 2
    
    def _write_calibration_offsets(self, data: bytes):
        """
        Internal: Write calibration offsets while already in CONFIG mode.
        Called during init() before switching to NDOF.
        """
        # Write calibration data in chunks (smbus2 has write limits)
        # Accel offsets (6 bytes)
        self.bus.write_i2c_block_data(self.address, REG_ACC_OFFSET_X_LSB, list(data[0:6]))
        # Mag offsets (6 bytes)
        self.bus.write_i2c_block_data(self.address, REG_MAG_OFFSET_X_LSB, list(data[6:12]))
        # Gyro offsets (6 bytes)
        self.bus.write_i2c_block_data(self.address, REG_GYR_OFFSET_X_LSB, list(data[12:18]))
        # Accel radius (2 bytes)
        self.bus.write_i2c_block_data(self.address, REG_ACC_RADIUS_LSB, list(data[18:20]))
        # Mag radius (2 bytes)
        self.bus.write_i2c_block_data(self.address, REG_MAG_RADIUS_LSB, list(data[20:22]))
    
    def read_calibration_data(self) -> bytes | None:
        """
        Read raw calibration offset data (22 bytes).
        Must be called when sensor is calibrated.
        Returns bytes or None on error.
        """
        if not self._initialized or not self.bus:
            return None
        try:
            # Must switch to CONFIG mode to read calibration offsets
            self.bus.write_byte_data(self.address, REG_OPR_MODE, OPR_MODE_CONFIG)
            import time
            time.sleep(0.025)
            
            # Read all 22 bytes of calibration data
            data = self.bus.read_i2c_block_data(self.address, REG_ACC_OFFSET_X_LSB, CALIBRATION_DATA_SIZE)
            
            # Switch back to NDOF mode
            self.bus.write_byte_data(self.address, REG_OPR_MODE, OPR_MODE_NDOF)
            time.sleep(0.02)
            
            return bytes(data)
        except Exception as e:
            logger.error(f"Failed to read calibration data: {e}")
            return None
    
    def write_calibration_data(self, data: bytes) -> bool:
        """
        Write calibration offset data (22 bytes) to sensor.
        NOTE: Prefer passing calibration_data to init() instead.
        This method switches modes which may cause issues.
        Returns True on success.
        """
        if not self._initialized or not self.bus:
            return False
        if len(data) != CALIBRATION_DATA_SIZE:
            logger.error(f"Invalid calibration data size: {len(data)} (expected {CALIBRATION_DATA_SIZE})")
            return False
        try:
            # Must switch to CONFIG mode to write calibration offsets
            self.bus.write_byte_data(self.address, REG_OPR_MODE, OPR_MODE_CONFIG)
            import time
            time.sleep(0.025)
            
            self._write_calibration_offsets(data)
            
            # Switch back to NDOF mode
            self.bus.write_byte_data(self.address, REG_OPR_MODE, OPR_MODE_NDOF)
            time.sleep(0.02)
            
            logger.info("Calibration data restored successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to write calibration data: {e}")
            return False
    
    def close(self):
        """Close I2C bus"""
        if self.bus:
            self.bus.close()
            self.bus = None
            self._initialized = False


# ----- Standalone test -----

async def test_bno055():
    """Test BNO055 readings"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    bno = BNO055()
    
    if not await bno.init():
        print("Failed to initialize BNO055")
        return
    
    print("\nBNO055 initialized! Reading data at 10Hz...")
    print("Rotate the sensor to see heading change.")
    print("Move in figure-8 pattern to calibrate magnetometer.")
    print("Press Ctrl+C to stop.\n")
    
    try:
        while True:
            heading = bno.read_heading()
            yaw_rate = bno.read_yaw_rate()
            cal = bno.read_calibration()
            
            if heading is not None:
                # Simple compass direction
                directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
                idx = int((heading + 22.5) / 45) % 8
                direction = directions[idx]
                
                print(f"Heading: {heading:6.1f}° ({direction:2s}) | "
                      f"Yaw rate: {yaw_rate:+7.1f}°/s | "
                      f"Cal: SYS={cal['sys']} GYR={cal['gyr']} ACC={cal['acc']} MAG={cal['mag']}")
            else:
                print("Read error")
            
            await asyncio.sleep(0.1)
            
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        bno.close()


if __name__ == "__main__":
    asyncio.run(test_bno055())

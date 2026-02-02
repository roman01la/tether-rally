#!/bin/bash
set -e  # Exit on any error

# Deploy Python files
scp pi-scripts/control-relay.py pi-scripts/bno055_reader.py \
    pi-scripts/hall_rpm.py pi-scripts/low_speed_traction.py \
    pi-scripts/yaw_rate_controller.py pi-scripts/slip_angle_watchdog.py \
    pi-scripts/steering_shaper.py pi-scripts/abs_controller.py \
    pi-scripts/hill_hold.py pi-scripts/coast_control.py \
    pi-scripts/surface_adaptation.py pi-scripts/car_config.py \
    pi-scripts/direction_estimator.py \
    pi@arrma-pi2w.local:/home/pi/

# Deploy profile configs and create recordings directory
ssh pi@arrma-pi2w.local 'mkdir -p /home/pi/profiles /home/pi/recordings'
scp pi-scripts/profiles/*.ini pi@arrma-pi2w.local:/home/pi/profiles/

# Deploy and reload systemd service (in case it's updated)
scp pi-scripts/control-relay.service pi@arrma-pi2w.local:/tmp/
ssh pi@arrma-pi2w.local 'sudo cp /tmp/control-relay.service /etc/systemd/system/ && sudo systemctl daemon-reload'

# Disable WiFi power saving for low latency (persistent across reboots)
ssh pi@arrma-pi2w.local 'sudo iw wlan0 set power_save off; echo "options 8812au rtw_power_mgnt=0 rtw_enusbss=0" | sudo tee /etc/modprobe.d/8812au.conf > /dev/null 2>&1 || true'

# Restart service and show logs
ssh pi@arrma-pi2w.local 'sudo systemctl restart control-relay && journalctl -u control-relay -n 10 --no-pager'

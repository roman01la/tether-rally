#!/bin/bash

scp pi-scripts/control-relay.py pi-scripts/bno055_reader.py \
    pi-scripts/hall_rpm.py pi-scripts/low_speed_traction.py \
    pi-scripts/yaw_rate_controller.py pi-scripts/slip_angle_watchdog.py \
    pi-scripts/steering_shaper.py pi-scripts/abs_controller.py \
    pi-scripts/hill_hold.py pi-scripts/coast_control.py \
    pi-scripts/surface_adaptation.py \
    pi@arrma-pi2w.local:/home/pi/ \
    && ssh pi@arrma-pi2w.local 'sudo systemctl restart control-relay && journalctl -u control-relay -n 5 --no-pager'
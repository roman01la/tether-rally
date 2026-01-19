#!/bin/bash

scp pi-scripts/control-relay.py pi@arrma-pi2w.local:/home/pi/ && ssh pi@arrma-pi2w.local 'sudo systemctl restart control-relay && journalctl -u control-relay -n 5 --no-pager'
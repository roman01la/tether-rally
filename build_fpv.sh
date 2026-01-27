#!/bin/bash

cd /Users/romanliutikov/projects/arrma-remote/fpv-sender && \
GOOS=linux GOARCH=arm64 go build -o fpv-sender-arm64 . && scp fpv-sender-arm64 pi@arrma-pi2w.local:/home/pi/fpv-sender-arm64

cd /Users/romanliutikov/projects/arrma-remote/fpv-receiver/build && make
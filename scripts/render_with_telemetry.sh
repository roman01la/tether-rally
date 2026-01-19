#!/bin/bash
# Render high-quality video with telemetry overlay
# Usage: ./render_with_telemetry.sh recording.h264 recording_telemetry.jsonl

set -e

if [ $# -lt 2 ]; then
    echo "Usage: $0 <video.h264> <telemetry.jsonl> [output.mp4]"
    echo ""
    echo "Example:"
    echo "  $0 20260119_163000.h264 20260119_163000_telemetry.jsonl"
    exit 1
fi

VIDEO_FILE="$1"
TELEM_FILE="$2"
OUTPUT_FILE="${3:-${VIDEO_FILE%.h264}_with_telemetry.mp4}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASS_FILE="/tmp/telemetry_overlay.ass"
MP4_FILE="/tmp/temp_video.mp4"

echo "=== Render with Telemetry Overlay ==="
echo "Input video: $VIDEO_FILE"
echo "Telemetry:   $TELEM_FILE"
echo "Output:      $OUTPUT_FILE"
echo ""

# Step 1: Generate ASS subtitle from telemetry
echo "[1/3] Generating ASS subtitles from telemetry..."
python3 "$SCRIPT_DIR/generate_ass.py" "$TELEM_FILE" "$ASS_FILE"

# Step 2: Remux H.264 to MP4 (no re-encode)
echo "[2/3] Remuxing H.264 to MP4..."
ffmpeg -y -hide_banner -loglevel warning \
    -framerate 50 -i "$VIDEO_FILE" \
    -c copy "$MP4_FILE"

# Step 3: Render with telemetry overlay
echo "[3/3] Rendering with telemetry overlay..."
ffmpeg -y -hide_banner -loglevel warning -stats \
    -i "$MP4_FILE" \
    -vf "subtitles=$ASS_FILE" \
    -c:v libx264 -crf 18 -preset slow \
    "$OUTPUT_FILE"

# Cleanup
rm -f "$ASS_FILE" "$MP4_FILE"

echo ""
echo "Done! Output: $OUTPUT_FILE"
ls -lh "$OUTPUT_FILE"

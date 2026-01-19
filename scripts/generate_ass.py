#!/usr/bin/env python3
"""
Generate ASS subtitle file from telemetry JSONL log.

This allows burning telemetry overlay into high-quality recordings
during post-processing, using FFmpeg's subtitles filter.

Usage:
    python3 generate_ass.py recording_telemetry.jsonl telemetry.ass

Then render with:
    ffmpeg -framerate 50 -i recording.h264 -c copy recording.mp4
    ffmpeg -i recording.mp4 -vf "subtitles=telemetry.ass" -c:v libx264 -crf 18 -preset slow output.mp4
"""

import json
import sys
import argparse


def ms_to_ass_time(ms: int) -> str:
    """Convert milliseconds to ASS time format H:MM:SS.cc"""
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    cs = (ms % 1000) // 10  # ASS uses centiseconds
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def generate_ass(jsonl_path: str, output_path: str, font_name: str = "DejaVu Sans Mono"):
    """Convert telemetry JSONL to ASS subtitle file."""
    
    header = f"""[Script Info]
Title: Telemetry Overlay
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Telemetry,{font_name},24,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,3,2,0,1,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    samples = []
    
    # Read telemetry samples
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                # Only process telemetry samples (not events)
                if 'race_time_ms' in data:
                    samples.append(data)
            except json.JSONDecodeError as e:
                print(f"Warning: Skipping invalid JSON line: {e}", file=sys.stderr)
                continue
    
    if not samples:
        print("Error: No telemetry samples found in JSONL file", file=sys.stderr)
        sys.exit(1)
    
    print(f"Processing {len(samples)} telemetry samples...", file=sys.stderr)
    
    events = []
    
    for i, sample in enumerate(samples):
        start_ms = sample['t_mono_ms']
        # End time is start of next sample, or +100ms for last sample
        end_ms = samples[i + 1]['t_mono_ms'] if i + 1 < len(samples) else start_ms + 100
        
        # Format race time
        race_ms = sample['race_time_ms']
        mins = race_ms // 60000
        secs = (race_ms % 60000) // 1000
        ms = race_ms % 1000
        
        # Convert throttle/steering to percentage
        throttle = sample.get('throttle', 0)
        steering = sample.get('steering', 0)
        thr_pct = int(throttle / 32767 * 100) if throttle else 0
        str_pct = int(steering / 32767 * 100) if steering else 0
        
        # Format telemetry text
        text = f"TIME {mins:02d}:{secs:02d}.{ms:03d}  THR {thr_pct}%  STR {str_pct}%"
        
        events.append(
            f"Dialogue: 0,{ms_to_ass_time(start_ms)},{ms_to_ass_time(end_ms)},"
            f"Telemetry,,0,0,0,,{text}"
        )
    
    # Write ASS file
    with open(output_path, 'w') as f:
        f.write(header)
        f.write('\n'.join(events))
        f.write('\n')
    
    print(f"Generated {output_path} with {len(events)} subtitle events", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description='Generate ASS subtitle file from telemetry JSONL log'
    )
    parser.add_argument('input', help='Input JSONL telemetry file')
    parser.add_argument('output', help='Output ASS subtitle file')
    parser.add_argument('--font', default='DejaVu Sans Mono',
                        help='Font name to use in ASS file (default: DejaVu Sans Mono)')
    
    args = parser.parse_args()
    
    generate_ass(args.input, args.output, args.font)


if __name__ == "__main__":
    main()

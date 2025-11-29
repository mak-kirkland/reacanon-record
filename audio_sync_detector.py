#!/usr/bin/env python3
"""
audio_sync_detector.py
--------------
Analyzes an audio/video file to find the timestamp of the loudest transient (clap).
Outputs the timestamp (in seconds) to stdout.

Author: Slav Basharov
Co-author: Michael Kirkland
"""

import sys
import numpy as np
import ffmpeg

# --- CONFIGURATION ---
FFMPEG_BINARY = "/usr/bin/ffmpeg"
SAMPLE_RATE = 16000
SCAN_DURATION = 15.0  # Only scan the first 15 seconds for a clap

def extract_audio_segment(path, duration):
    """
    Decodes the first `duration` seconds of an audio/video file
    into a mono 16kHz float32 numpy array.
    """
    try:
        out, _ = (
            ffmpeg
            .input(path, t=duration)
            .output("pipe:", format="f32le", ac=1, ar=str(SAMPLE_RATE))
            .run(cmd=FFMPEG_BINARY, capture_stdout=True, capture_stderr=True)
        )
        return np.frombuffer(out, np.float32)
    except ffmpeg.Error as e:
        # Print ffmpeg errors to stderr so they don't corrupt the timestamp output
        print(f"FFmpeg Error: {e.stderr.decode()}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return None

def find_transient_peak(audio_data):
    """
    Finds the time of the maximum amplitude envelope peak.
    Returns timestamp in seconds.
    """
    if audio_data is None or len(audio_data) == 0:
        return None

    # Calculate envelope (Absolute value)
    # We smooth it slightly with a 5ms window to avoid single-sample noise
    window_size = int(0.005 * SAMPLE_RATE)
    envelope = np.abs(audio_data)

    # Simple moving average for smoothing
    kernel = np.ones(window_size) / window_size
    smoothed_env = np.convolve(envelope, kernel, mode='same')

    # Find the index of the maximum value
    peak_index = np.argmax(smoothed_env)

    # Convert index to seconds
    return peak_index / SAMPLE_RATE

def main():
    if len(sys.argv) < 2:
        print("Usage: detect_clap.py <file_path>", file=sys.stderr)
        sys.exit(1)

    file_path = sys.argv[1]

    # 1. Extract Audio
    audio_data = extract_audio_segment(file_path, SCAN_DURATION)
    if audio_data is None:
        sys.exit(1)

    # 2. Find Peak
    timestamp = find_transient_peak(audio_data)

    if timestamp is not None:
        # success: print ONLY the number
        print(f"{timestamp:.4f}")
        sys.exit(0)
    else:
        print("Could not detect audio.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

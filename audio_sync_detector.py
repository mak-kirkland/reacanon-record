#!/usr/bin/env python3
"""
audio_sync_detector.py
--------------
Analyzes two audio files to find the synchronization offset based on
the loudest transient (clap) in each file.

Algorithm:
1. Extract audio from Reference and Target.
2. Find the timestamp of the maximum peak in each file.
3. Calculate offset = Target_Peak - Reference_Peak.

Usage: python3 audio_sync_detector.py <reference_file> <target_file>

Author: Slav Basharov
Co-author: Michael Kirkland
"""

import sys
import numpy as np
import ffmpeg

# --- CONFIGURATION ---
FFMPEG_BINARY = "/usr/bin/ffmpeg"
SAMPLE_RATE = 48000
SCAN_DURATION = 15.0    # Only scan the first 15 seconds for a clap

def extract_audio_segment(path, duration):
    """
    Decodes the first `duration` seconds of an audio/video file
    into a mono float32 numpy array.
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
    envelope = np.abs(audio_data)

    # We smooth it slightly with a 5ms window to avoid single-sample noise
    window_size = int(0.005 * SAMPLE_RATE)
    kernel = np.ones(window_size) / window_size
    smoothed_env = np.convolve(envelope, kernel, mode='same')

    # Find the index of the maximum value
    peak_index = np.argmax(smoothed_env)

    # Convert index to seconds
    return peak_index / SAMPLE_RATE

def main():
    if len(sys.argv) < 3:
        print("Usage: audio_sync_detector.py <ref_file> <target_file>", file=sys.stderr)
        sys.exit(1)

    ref_path = sys.argv[1]
    tgt_path = sys.argv[2]

    # 1. Extract Audio
    ref_audio = extract_audio_segment(ref_path, SCAN_DURATION)
    tgt_audio = extract_audio_segment(tgt_path, SCAN_DURATION)

    if ref_audio is None or tgt_audio is None:
        print("Error reading audio files", file=sys.stderr)
        sys.exit(1)

    # 2. Find Peaks
    ref_peak = find_transient_peak(ref_audio)
    tgt_peak = find_transient_peak(tgt_audio)

    if ref_peak is not None and tgt_peak is not None:
        # Calculate Offset
        # If Target Peak is at 5s and Ref is at 4s, Target is +1s late.
        # Offset = 5 - 4 = 1.
        offset = tgt_peak - ref_peak

        print(f"{offset:.6f}")
        sys.exit(0)
    else:
        print("Could not detect peaks.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

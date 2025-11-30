#!/usr/bin/env python3
"""
audio_sync_detector.py
--------------
Analyzes two audio files to find the synchronization offset using
Cross-Correlation of the spectral energy envelopes.

1. High-pass filters audio to remove camera handling rumble.
2. Uses Amplitude Envelopes to ignore Phase inversion issues.
3. Uses Cross-Correlation to find the best fit across the whole file,
   not just the single loudest moment.

Usage: python3 audio_sync_detector.py <reference_file> <target_file>

Author: Slav Basharov
Co-author: Michael Kirkland
"""

import sys
import numpy as np
import ffmpeg
from scipy import signal

# --- CONFIGURATION ---
FFMPEG_BINARY = "/usr/bin/ffmpeg"
SAMPLE_RATE = 48000
SCAN_DURATION = 60.0    # Scan duration in seconds
# Frequencies below this will be ignored (removes wind/handling rumble)
HIPASS_FREQ = 300

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

def preprocess_signal(data):
    """
    Prepares the audio for comparison by normalizing, filtering,
    and extracting the envelope.
    """
    # 1. Normalize to -1.0 to 1.0 (avoids volume differences affecting score)
    if np.max(np.abs(data)) > 0:
        data = data / np.max(np.abs(data))

    # 2. High-Pass Filter
    # Removes low frequency rumble (handling noise) that confuses sync.
    # We only care about the sharp 'crack' of the clap.
    sos = signal.butter(4, HIPASS_FREQ, 'hp', fs=SAMPLE_RATE, output='sos')
    filtered = signal.sosfilt(sos, data)

    # 3. Envelope Extraction (Absolute Value)
    # This solves PHASE issues. If one mic is inverted, the raw correlation
    # would be negative. Comparing absolute energy ignores phase.
    envelope = np.abs(filtered)

    return envelope

def find_offset_correlation(ref_audio, tgt_audio):
    """
    Calculates the time offset using FFT-based Cross-Correlation.
    Returns offset in seconds.
    Positive offset = Target is LATE (needs to be moved earlier).
    """
    # Preprocess both signals to make them "comparable"
    ref_env = preprocess_signal(ref_audio)
    tgt_env = preprocess_signal(tgt_audio)

    # Calculate Cross-Correlation
    # mode='full' computes the correlation at every possible overlap
    correlation = signal.correlate(ref_env, tgt_env, mode='full', method='fft')

    # Calculate the lag axis (time shifts)
    lags = signal.correlation_lags(len(ref_env), len(tgt_env), mode='full')

    # Find the lag with the maximum correlation score
    max_corr_index = np.argmax(correlation)
    lag_samples = lags[max_corr_index]

    # Convert samples to seconds
    # Note: If Target is "behind" Reference, lag is positive.
    # However, signal.correlate(in1, in2) standard implies in1 is reference.
    # If lag is positive, it means in2 (Target) must be shifted RIGHT to match in1.
    # Therefore Target is EARLY.

    # Let's double check the sign convention:
    # If Lag is +100 samples. It means Ref[x] matches Tgt[x+100].
    # So Tgt is "ahead" or "later" in the array.
    # We want the offset to move Tgt to Ref.

    return -lag_samples / SAMPLE_RATE

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

    # Check for empty audio
    if len(ref_audio) == 0 or len(tgt_audio) == 0:
        print("Error: Audio track is empty.", file=sys.stderr)
        sys.exit(1)

    # 2. Calculate Offset
    try:
        offset = find_offset_correlation(ref_audio, tgt_audio)

        # Output result to stdout
        print(f"{offset:.6f}")
        sys.exit(0)

    except Exception as e:
        print(f"Sync calculation failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

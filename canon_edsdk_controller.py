#!/usr/bin/env python3
"""
canon_edsdk_controller.py
---------------------------------
Headless script to control Canon cameras via EDSDK.
Features:
- Robust recording start/stop with retry logic.
- Automatic file download via event callbacks.
- File-based IPC for Stop/Cancel commands and Logging.
- Real-time progress reporting to stdout.
- Clean session shutdown to prevent camera UI hangs.
- "Zombie" state protection (Force unlock on connect).
- Verify & delete: Checks size and runs FFmpeg integrity check before deleting from card.

Author: Slav Basharov
Co-author: Michael Kirkland
"""

import sys
import os
import time
import threading
import signal
import tempfile
import shutil
import subprocess
from ctypes import byref, c_void_p, c_uint32, c_bool

# Import everything from C wrapper
from canon_edsdk_defs import *

# ==============================================================================
# GLOBALS & STATE
# ==============================================================================
TEMP_DIR = tempfile.gettempdir()
PID_FILE = os.path.join(TEMP_DIR, "canon_edsdk_controller.pid")

# IPC Files
LOG_FILE = os.path.join(TEMP_DIR, "canon_edsdk_controller.log")
CMD_SAVE = os.path.join(TEMP_DIR, "canon_edsdk_cmd_save")
CMD_CANCEL = os.path.join(TEMP_DIR, "canon_edsdk_cmd_cancel")

_download_queue = []
_stop_event = threading.Event()
_should_download = True

def log(msg):
    """Writes log messages to a shared file for REAPER to tail."""
    # Always print to stdout (helpful for debugging if running manually)
    print(msg)
    sys.stdout.flush()

    # Append to log file for REAPER
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"LOG:{msg}\n")
    except Exception:
        pass # Don't crash if disk is busy

def send_result(path):
    """Writes the final result path to the log file."""
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"RESULT:{path}\n")
    except Exception:
        pass

def verify_video_integrity(filepath):
    """
    Returns True if the video file is valid.
    Uses FFmpeg to check for container errors without decoding the whole stream.
    """
    ffmpeg_bin = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

    # 1. Check if file exists and has size
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        return False

    # 2. Run FFmpeg error check
    # -v error: Only print errors
    # -i ...: Input file
    # -f null -: Output to nowhere
    try:
        cmd = [ffmpeg_bin, "-v", "error", "-i", filepath, "-f", "null", "-"]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # If return code is 0, the file structure is valid
        if result.returncode == 0:
            return True
        else:
            log(f"Integrity Check Failed: {result.stderr.decode()}")
            return False
    except Exception as e:
        log(f"Verification Error: {e}")
        return False

# ==============================================================================
# CALLBACK HANDLERS
# ==============================================================================
@EdsObjectEventHandler
def on_object_event(event, inRef, context):
    """Called by camera when a file is created (kEdsObjectEvent_DirItemCreated)."""
    if event == kEdsObjectEvent_DirItemCreated:
        log("New file detected on camera.")

        # Retain reference so it doesn't vanish before we download it
        if inRef:
            sdk.lib.EdsRetain(inRef)
            _download_queue.append(inRef)

    else:
        # We must release references for events we don't care about
        if inRef:
            sdk.lib.EdsRelease(inRef)
    return 0

@EdsProgressCallback
def on_progress(percent, context, cancel):
    """Called during file download to report progress."""
    log(f"Progress: {percent}%")
    return 0

# ==============================================================================
# CAMERA SESSION CLASS
# ==============================================================================
class CameraSession:
    def __init__(self):
        self.cam = None
        self.is_connected = False

    def __enter__(self):
        """Context manager entry: Connect and Setup."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: Cleanup."""
        self.close()

    def connect(self):
        """Initializes SDK, finds first camera, opens session."""
        if sdk.lib.EdsInitializeSDK() != 0: raise RuntimeError("SDK Init failed")

        # Get Camera List
        cam_list = c_void_p()
        sdk.lib.EdsGetCameraList(byref(cam_list))

        count = c_uint32(0)
        sdk.lib.EdsGetChildCount(cam_list, byref(count))

        if count.value == 0:
            sdk.lib.EdsRelease(cam_list)
            raise RuntimeError("No cameras detected.")

        # Get First Camera
        self.cam = c_void_p()
        sdk.lib.EdsGetChildAtIndex(cam_list, 0, byref(self.cam))
        sdk.lib.EdsRelease(cam_list)

        # Open Session
        if sdk.lib.EdsOpenSession(self.cam) != 0: raise RuntimeError("OpenSession failed")

        self.is_connected = True
        time.sleep(1.0) # Warmup

        # --- ZOMBIE PROTECTION ---
        # Force unlock immediately to rescue camera from previous crashes
        self._force_unlock()

        # Register Event Handler
        # Keep a reference to prevent garbage collection of the callback
        self.handler_ref = on_object_event
        sdk.lib.EdsSetObjectEventHandler(self.cam, kEdsObjectEvent_All, self.handler_ref, None)

    def _force_unlock(self):
        """Attempts to clear UI locks from previous sessions."""
        sdk.lib.EdsSendStatusCommand(self.cam, kEdsCameraStatusCommand_UIUnLock, 0)
        sdk.lib.EdsSendStatusCommand(self.cam, kEdsCameraStatusCommand_ExitDirectTransfer, 0)

    def _set_prop(self, prop_id, value):
        val = c_uint32(value)
        return sdk.lib.EdsSetPropertyData(self.cam, prop_id, 0, 4, byref(val))

    def _retry_action(self, prop_id, value, retries=5, desc="Command"):
        """Retry camera commands with exponential backoff."""
        for i in range(retries):
            err = self._set_prop(prop_id, value)
            if err == EDS_ERR_OK: return

            # If busy, pump events and wait
            if err in (EDS_ERR_NOT_READY, EDS_ERR_DEVICE_BUSY):
                sdk.lib.EdsGetEvent() # Pump events
                time.sleep(0.1 * (i + 1)) # Backoff: 0.1s, 0.2s, 0.3s...
            else:
                log(f"Warning: {desc} error {hex(err)}")
                break

        if err != EDS_ERR_OK and desc == "Start Record":
             raise RuntimeError(f"Failed to start recording (Err: {hex(err)})")

    def setup_recording(self):
        """Configures SaveTo and wakes up LiveView."""
        # Save to Camera SD Card
        self._set_prop(kEdsPropID_SaveTo, kEdsSaveTo_Camera)

        # Wake up Live View (Required for 70D video trigger)
        self._set_prop(kEdsPropID_Evf_OutputDevice, kEdsEvfOutputDevice_TFT)
        time.sleep(1.0)

    def start_record(self):
        """Sends Record Start command with Retry logic."""
        self._retry_action(kEdsPropID_Record, EDS_RECORD_START, 5, "Start Record")
        log("Recording started.")

    def stop_record(self):
        """Sends Record Stop command with Retry logic."""
        log("Stopping recording...")
        # Check specifically for "Not Ready" error
        self._retry_action(kEdsPropID_Record, EDS_RECORD_STOP, 10, "Stop Record")

    def download_pending_files(self, dest_dir):
        """Downloads all files in the queue."""
        for item_ref in _download_queue:
            try:
                self._download_single(item_ref, dest_dir)
            finally:
                sdk.lib.EdsRelease(item_ref) # Release the retained reference

    def _download_single(self, dir_item, dest_dir):
        info = EdsDirectoryItemInfo()
        sdk.lib.EdsGetDirectoryItemInfo(dir_item, byref(info))
        filename = info.szFileName.decode("utf-8")
        save_path = os.path.join(dest_dir, filename)

        log(f"Downloading {filename} ({info.size} bytes)...")

        stream = c_void_p()
        if sdk.lib.EdsCreateFileStream(save_path.encode("utf-8"), 1, 2, byref(stream)) != 0:
            log("File create failed.")
            return

        try:
            # Set Progress Callback
            self.progress_ref = on_progress
            sdk.lib.EdsSetProgressCallback(stream, self.progress_ref, 2, None)
            sdk.lib.EdsDownload(dir_item, info.size, stream)
            sdk.lib.EdsDownloadComplete(dir_item)

        finally:
            sdk.lib.EdsRelease(stream)

        # --- ROBUST VERIFICATION LOGIC ---

        # 1. Size Verification
        local_size = os.path.getsize(save_path)
        if local_size != info.size:
            log(f"CRITICAL: Size mismatch! Camera: {info.size}, Local: {local_size}")
            # Do NOT delete from camera. Do NOT send result.
            return

        # 2. Content Verification (FFmpeg)
        log("Verifying file integrity...")
        if verify_video_integrity(save_path):
            log(f"Verified. Deleting from card...")

            # 3. Safe Delete
            err = sdk.lib.EdsDeleteDirectoryItem(dir_item)
            if err == EDS_ERR_OK:
                log("File deleted from camera.")
            else:
                log(f"Warning: Delete failed (Err {hex(err)})")

            # Only send result if we are confident the file is good
            log(f"Saved: {save_path}")
            send_result(save_path)
        else:
            log("CRITICAL: File corrupted. NOT deleting from camera.")
            # Do NOT send result.

    def close(self):
        """Clean shutdown to release Camera UI."""
        log("Cleaning up session...")
        if self.is_connected:
            # Crucial: Force UI Unlock
            self._force_unlock()
            time.sleep(0.2)
            sdk.lib.EdsCloseSession(self.cam)
            sdk.lib.EdsRelease(self.cam)
        sdk.lib.EdsTerminateSDK()
        log("Done.")

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def main():
    if len(sys.argv) < 3:
        print("Usage: python3 canon_edsdk_controller.py <path> <duration>")
        return

    dest_folder, duration = sys.argv[1], float(sys.argv[2])

    global _should_download

    # Initialize Log (Clear previous run)
    with open(LOG_FILE, 'w') as f:
        f.write("")

    # Handle Standard Signals as backup
    def sig_handler(sig, frame):
        global _should_download
        print(f"\nSignal {sig} received. Stopping...")
        if sig == signal.SIGTERM: _should_download = False
        _stop_event.set()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        # Write PID to file for REAPER discovery
        with open(PID_FILE, 'w') as f:
            f.write(f"{os.getpid()}")

        print(f"Controller started (PID {os.getpid()})")
        sys.stdout.flush()

        with CameraSession() as session:
            session.setup_recording()
            session.start_record()

            # Recording Loop (Main Running State)
            start_time = time.time()
            while not _stop_event.is_set():
                if time.time() - start_time >= duration:
                    log("Duration reached.")
                    break

                # IPC: Check for Command Files
                if os.path.exists(CMD_SAVE):
                    log("Command received: SAVE")
                    _should_download = True
                    _stop_event.set()
                    try: os.remove(CMD_SAVE)
                    except: pass

                elif os.path.exists(CMD_CANCEL):
                    log("Command received: CANCEL")
                    _should_download = False
                    _stop_event.set()
                    try: os.remove(CMD_CANCEL)
                    except: pass

                # Keep the SDK event queue moving
                sdk.lib.EdsGetEvent()

                # Short sleep is fine here, we are just waiting for user input/timer
                time.sleep(0.1)

            # Teardown Sequence
            session.stop_record()

            # CRITICAL: Always wait for file generation (buffer flush)
            # even if we plan to discard it. Prevents camera hangs.
            log("Waiting for camera buffer flush...")

            wait_start = time.time()
            file_ready = False

            # Poll for the file event (Fast Polling)
            while time.time() - wait_start < 15.0:
                sdk.lib.EdsGetEvent()
                if _download_queue: # Check if file appeared
                    file_ready = True
                    break

                # Sleep very briefly to keep CPU usage low but reaction fast
                time.sleep(0.05)

            if _should_download and file_ready:
                session.download_pending_files(dest_folder)
            elif not file_ready:
                log("Warning: Timed out waiting for file generation.")
            else:
                log("Skipping download (Cancelled).")
                # Release items we aren't downloading
                for item in _download_queue: sdk.lib.EdsRelease(item)

    except Exception as e:
        log(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Remove discovery file
        try: os.remove(PID_FILE)
        except: pass

        # Force exit to ensure USB driver releases fully (Prevents Zombie processes)
        time.sleep(0.5)
        sys.exit(0)

if __name__ == "__main__":
    main()

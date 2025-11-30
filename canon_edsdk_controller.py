#!/usr/bin/env python3
"""
canon_edsdk_controller.py
---------------------------------
Headless script to control Canon cameras via EDSDK on Linux.
Features:
- Robust recording start/stop with retry logic.
- Automatic file download via event callbacks.
- Real-time progress reporting to stdout for REAPER integration.
- Clean session shutdown to prevent camera UI hangs.
- "Zombie" state protection (Force unlock on connect).

Author: Slav Basharov
Co-author: Michael Kirkland
"""

import sys
import os
import time
import threading
import signal
from ctypes import *

# ==============================================================================
# CONFIGURATION & CONSTANTS
# ==============================================================================
LIB_PATHS = ["libEDSDK.so", "./libEDSDK.so", "../libEDSDK.so", "/usr/local/lib/libEDSDK.so"]
RESULT_FILE = "/tmp/camera_result.txt"

# EDSDK Constants
EDS_ERR_OK = 0x00000000
EDS_ERR_DEVICE_BUSY = 0x00000080
EDS_ERR_NOT_READY = 0x00002019 # Not in recordable state

# Property IDs
kEdsPropID_SaveTo = 0x0000000B
kEdsPropID_Record = 0x00000510
kEdsPropID_Evf_OutputDevice = 0x00000500

# Values
kEdsSaveTo_Camera = 1
kEdsEvfOutputDevice_TFT = 1
EDS_RECORD_START = 4
EDS_RECORD_STOP = 0

# Commands
kEdsCameraStatusCommand_UIUnLock = 1
kEdsCameraStatusCommand_ExitDirectTransfer = 3

# Events
kEdsObjectEvent_All = 0x00000200
kEdsObjectEvent_DirItemCreated = 0x00000204

# Structures
class EdsDirectoryItemInfo(Structure):
    _fields_ = [
        ("size", c_uint64), ("isFolder", c_uint32), ("groupID", c_uint32),
        ("option", c_uint32), ("szFileName", c_char * 256),
        ("format", c_uint32), ("dateTime", c_uint32),
    ]

# ==============================================================================
# EDSDK WRAPPER CLASS
# ==============================================================================
class EdsdkWrapper:
    """Handles loading the shared library and defining C function signatures."""
    def __init__(self):
        self.lib = self._load_library()
        self._define_prototypes()
        self.Retain = getattr(self.lib, "EdsRetain") # Shortcut for internal use

    def _load_library(self):
        for path in LIB_PATHS:
            try:
                return CDLL(path)
            except OSError:
                continue
        print(f"❌ Error: Could not load libEDSDK.so. Checked: {LIB_PATHS}")
        sys.exit(1)

    def _define_prototypes(self):
        # Helper to reduce boilerplate
        def proto(name, res, args):
            f = getattr(self.lib, name)
            f.restype, f.argtypes = res, args

        proto("EdsInitializeSDK", c_int32, [])
        proto("EdsTerminateSDK", c_int32, [])
        proto("EdsGetCameraList", c_int32, [POINTER(c_void_p)])
        proto("EdsGetChildCount", c_int32, [c_void_p, POINTER(c_uint32)])
        proto("EdsGetChildAtIndex", c_int32, [c_void_p, c_uint32, POINTER(c_void_p)])
        proto("EdsOpenSession", c_int32, [c_void_p])
        proto("EdsCloseSession", c_int32, [c_void_p])
        proto("EdsRelease", c_int32, [c_void_p])
        proto("EdsSetPropertyData", c_int32, [c_void_p, c_uint32, c_int32, c_uint32, c_void_p])
        proto("EdsSendStatusCommand", c_int32, [c_void_p, c_uint32, c_int32])
        proto("EdsSetObjectEventHandler", c_int32, [c_void_p, c_uint32, c_void_p, c_void_p])
        proto("EdsGetDirectoryItemInfo", c_int32, [c_void_p, POINTER(EdsDirectoryItemInfo)])
        proto("EdsCreateFileStream", c_int32, [c_char_p, c_uint32, c_uint32, POINTER(c_void_p)])
        proto("EdsDownload", c_int32, [c_void_p, c_uint64, c_void_p])
        proto("EdsDownloadComplete", c_int32, [c_void_p])
        proto("EdsGetEvent", c_int32, [])
        proto("EdsSetProgressCallback", c_int32, [c_void_p, c_void_p, c_int32, c_void_p])

sdk = EdsdkWrapper()

# ==============================================================================
# CALLBACK HANDLERS
# ==============================================================================
# Global queues are necessary because C-callbacks don't handle class instance methods well
_download_queue = []
_download_event = threading.Event()

@CFUNCTYPE(c_int32, c_uint32, c_void_p, c_void_p)
def on_object_event(event, inRef, context):
    """Called by camera when a file is created."""
    if event == kEdsObjectEvent_DirItemCreated:
        print("New file detected on camera.")
        sys.stdout.flush()
        # Retain reference so it doesn't vanish before we download (or discard) it
        if inRef:
            sdk.Retain(inRef)
            _download_queue.append(inRef)
        _download_event.set()

    else:
        # Only release non-download events
        if inRef:
            sdk.lib.EdsRelease(inRef)
    return 0

@CFUNCTYPE(c_int32, c_uint32, c_void_p, POINTER(c_bool))
def on_progress(percent, context, cancel):
    """Called during file download."""
    print(f"Progress: {percent}%")
    sys.stdout.flush()
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
        sdk.lib.EdsSetObjectEventHandler(self.cam, kEdsObjectEvent_All, on_object_event, None)

    def _force_unlock(self):
        """Attempts to clear UI locks from previous sessions."""
        sdk.lib.EdsSendStatusCommand(self.cam, kEdsCameraStatusCommand_UIUnLock, 0)
        sdk.lib.EdsSendStatusCommand(self.cam, kEdsCameraStatusCommand_ExitDirectTransfer, 0)

    def _set_prop(self, prop_id, value):
        val = c_uint32(value)
        return sdk.lib.EdsSetPropertyData(self.cam, prop_id, 0, 4, byref(val))

    def _retry_action(self, prop_id, value, retries=5, desc="Command"):
        """Helper to retry camera commands if device is busy."""
        for _ in range(retries):
            err = self._set_prop(prop_id, value)
            if err == EDS_ERR_OK: return

            # If busy, pump events and wait
            if err in (EDS_ERR_NOT_READY, EDS_ERR_DEVICE_BUSY):
                sdk.lib.EdsGetEvent()
                time.sleep(0.5)
            else:
                print(f"Warning: {desc} error {hex(err)}")
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
        print("Recording started.")
        sys.stdout.flush()

    def stop_record(self):
        """Sends Record Stop command with Retry logic."""
        print("Stopping recording...")
        sys.stdout.flush()
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

        print(f"Downloading {filename} ({info.size} bytes)...")
        sys.stdout.flush()

        stream = c_void_p()
        if sdk.lib.EdsCreateFileStream(save_path.encode("utf-8"), 1, 2, byref(stream)) != 0:
            print("File create failed.")
            return

        try:
            # Set Progress Callback
            sdk.lib.EdsSetProgressCallback(stream, on_progress, 2, None)
            sdk.lib.EdsDownload(dir_item, info.size, stream)
            sdk.lib.EdsDownloadComplete(dir_item)

            print(f"Saved: {save_path}")
            sys.stdout.flush()

            # Notify REAPER
            with open(RESULT_FILE, "w") as f: f.write(save_path)
        finally:
            sdk.lib.EdsRelease(stream)

    def close(self):
        """Clean shutdown to release Camera UI."""
        print("Cleaning up session...")
        sys.stdout.flush()
        if self.is_connected:
            # Crucial: Force UI Unlock
            self._force_unlock()
            time.sleep(0.5)

            sdk.lib.EdsCloseSession(self.cam)
            sdk.lib.EdsRelease(self.cam)
        sdk.lib.EdsTerminateSDK()
        print("Done.")
        sys.stdout.flush()

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def main():
    if len(sys.argv) < 3:
        print("Usage: python3 canon_record.py <path> <duration>")
        return

    dest_folder, duration = sys.argv[1], float(sys.argv[2])

    # Clean previous state
    if os.path.exists(RESULT_FILE): os.remove(RESULT_FILE)

    # Event for Ctrl+C handling
    stop_event = threading.Event()
    should_download = True

    def sig_handler(sig, frame):
        nonlocal should_download
        print(f"\nSignal {sig} received. Stopping...")
        if sig == signal.SIGTERM: should_download = False
        stop_event.set()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        with CameraSession() as session:
            session.setup_recording()
            session.start_record()

            # Recording Loop
            start_time = time.time()
            while not stop_event.is_set():
                if time.time() - start_time >= duration:
                    print("Duration reached.")
                    break
                # Pump EDSDK event loop
                sdk.lib.EdsGetEvent()
                time.sleep(0.1)

            # Teardown Sequence
            session.stop_record()

            # CRITICAL: Always wait for file generation (buffer flush)
            # even if we plan to discard it. Prevents camera hangs.
            print("Waiting for camera buffer flush...")
            sys.stdout.flush()

            # Wait for file event (max 15s)
            wait_s = time.time()
            while time.time() - wait_s < 15.0:
                sdk.lib.EdsGetEvent()
                if _download_event.is_set(): break
                time.sleep(0.1)

            if should_download:
                session.download_pending_files(dest_folder)
            else:
                print("Skipping download (Cancelled).")
                # Release items we aren't downloading
                for item in _download_queue: sdk.lib.EdsRelease(item)

    except Exception as e:
        print(f"Error: {e}")
        sys.stdout.flush()
    finally:
        # Force exit to ensure USB driver releases fully (Prevents Zombie processes)
        time.sleep(0.5)
        sys.exit(0)

if __name__ == "__main__":
    main()

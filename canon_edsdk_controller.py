#!/usr/bin/env python3
"""
canon_edsdk_controller.py
---------------------------------
Headless script to control Canon cameras via EDSDK.
Features:
- Robust recording start/stop with retry logic.
- Automatic file download via event callbacks.
- Socket-based IPC for instant Stop/Cancel commands.
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
import tempfile
import socket
import platform
from ctypes import byref, c_void_p, c_uint32, c_bool

# Import everything from C wrapper
from edsdk_defs import *

# ==============================================================================
# GLOBALS & STATE
# ==============================================================================
TEMP_DIR = tempfile.gettempdir()
PID_FILE = os.path.join(TEMP_DIR, "camera_script.pid")

_download_queue = []
_download_event = threading.Event()
_stop_event = threading.Event()
_should_download = True
_client_socket = None
_socket_lock = threading.Lock()

def log(msg):
    """Sends log messages back to REAPER via socket if connected."""
    global _client_socket
    # Always print to stdout (helpful for debugging if running manually)
    print(msg)
    sys.stdout.flush()

    with _socket_lock:
        if _client_socket:
            try:
                _client_socket.sendall(f"LOG:{msg}\n".encode('utf-8'))
            except:
                pass # If client disconnected, just ignore

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

        # Signal the main thread that file is ready
        _download_event.set()

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
# IPC SERVER
# ==============================================================================
def start_ipc_server():
    """Starts a socket server on a random port to listen for Stop/Cancel."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('127.0.0.1', 0)) # Bind to port 0 (OS assigns free port)
    port = server.getsockname()[1]
    server.listen(1)

    def listener():
        global _should_download, _client_socket
        # Wait for a connection (Blocking is fine in this thread)
        while not _stop_event.is_set():
            try:
                conn, addr = server.accept()

                # Disable Nagle's algorithm to ensure logs are sent immediately
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                with _socket_lock:
                    _client_socket = conn

                # Receive command
                data = conn.recv(1024).decode('utf-8').strip()
                if "SAVE" in data:
                    log("Received command: SAVE")
                    _should_download = True
                    _stop_event.set()
                elif "CANCEL" in data:
                    log("Received command: CANCEL")
                    _should_download = False
                    _stop_event.set()

                # We keep the connection open to stream logs back
                # The main thread will use _client_socket to send RESULT
            except Exception as e:
                print(f"IPC Error: {e}")

    t = threading.Thread(target=listener, daemon=True)
    t.start()
    return port

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

            log(f"Saved: {save_path}")

            # Notify REAPER via Socket
            with _socket_lock:
                if _client_socket:
                    try:
                        _client_socket.sendall(f"RESULT:{save_path}\n".encode('utf-8'))
                    except: pass
        finally:
            sdk.lib.EdsRelease(stream)

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

        with _socket_lock:
            if _client_socket:
                _client_socket.close()

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def main():
    if len(sys.argv) < 3:
        print("Usage: python3 canon_edsdk_controller.py <path> <duration>")
        return

    dest_folder, duration = sys.argv[1], float(sys.argv[2])

    global _should_download

    # Handle Standard Signals as backup
    def sig_handler(sig, frame):
        global _should_download
        print(f"\nSignal {sig} received. Stopping...")
        if sig == signal.SIGTERM: _should_download = False
        _stop_event.set()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        # Start IPC Server
        port = start_ipc_server()

        # Write PID and PORT to file for REAPER discovery
        with open(PID_FILE, 'w') as f:
            f.write(f"{os.getpid()}\n{port}")

        print(f"IPC Server listening on port {port}")
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
                if _download_event.is_set():
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

"""
REAPER Camera Trigger
----------------------------------------------
Integrates external Python recording script with REAPER.
Includes automatic Audio/Video synchronization via clap detection.

Structure:
- CameraProcess: Handles the background recording script.
- Sync Logic: Handles the logic for aligning items.
- REAPER Helpers: Wrapper for REAPER API calls.

Author: Slav Basharov
Co-author: Michael Kirkland
"""

import os
import sys
import subprocess
import signal
import shutil
import tempfile
import platform
import socket
import time
from reaper_python import *

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
# --- CROSS-PLATFORM PYTHON DETECTION ---
# Windows usually provides 'python', while Unix/Mac prefers 'python3'
PYTHON_EXEC = shutil.which("python3") or shutil.which("python")

if not PYTHON_EXEC:
    RPR_ShowMessageBox("Could not find Python! Please install Python 3.", "Error", 0)
    sys.exit(1)

# --- DYNAMIC PATH SETUP ---
# REAPER does not support __file__. We must construct the path relative
# to REAPER's resource directory.
try:
    # RPR_GetResourcePath() returns the base config folder (e.g. ~/.config/REAPER)
    # We assume this script lives in <ResourcePath>/Scripts/reacanon-record
    reaper_res_path = RPR_GetResourcePath()
    BASE_DIR = os.path.join(reaper_res_path, "Scripts", "reacanon-record")

    # Verify it exists
    if not os.path.exists(BASE_DIR):
        RPR_ShowConsoleMsg(
            f"\n[FATAL ERROR] Script folder not found!\n"
            f"The script expects to be located at:\n{BASE_DIR}\n\n"
            f"Please create this folder and move the python scripts there.\n"
        )
        # Exit immediately prevents the rest of the script from crashing with NameErrors
        sys.exit(1)

except Exception as e:
    # Allow sys.exit() to work if called above
    if isinstance(e, SystemExit):
        raise

    RPR_ShowConsoleMsg(f"\n[Error] Failed to determine script path: {e}\n")
    sys.exit(1)

RECORD_SCRIPT = os.path.join(BASE_DIR, "canon_edsdk_controller.py")
AUDIO_SYNC_SCRIPT   = os.path.join(BASE_DIR, "audio_sync_detector.py")

# Files
# We use tempfile to get the correct path for Windows/Mac/Linux
TEMP_DIR = tempfile.gettempdir()
PID_FILE    = os.path.join(TEMP_DIR, "camera_script.pid")
RESULT_FILE = os.path.join(TEMP_DIR, "camera_result.txt")
LOG_FILE    = os.path.join(TEMP_DIR, "camera_log.txt")

# ==============================================================================
# 2. REAPER HELPER FUNCTIONS
# ==============================================================================
def console_msg(m):
    RPR_ShowConsoleMsg(str(m) + "\n")

def get_project_path():
    path = RPR_GetProjectPath("", 512)[0]
    if not path:
        # Fallback for unsaved projects
        path = os.path.join(BASE_DIR, "recordings")
        if not os.path.exists(path): os.makedirs(path)
    return path

def get_track_by_name(name, create=False):
    """Finds a track by name. Returns MediaTrack* or None."""
    count = RPR_CountTracks(0)
    for i in range(count):
        trk = RPR_GetTrack(0, i)
        if RPR_GetSetMediaTrackInfo_String(trk, "P_NAME", "", False)[3] == name:
            return trk

    if create:
        RPR_Main_OnCommand(40702, 0) # Insert new track
        trk = RPR_GetTrack(0, count)   # It's at the end
        RPR_GetSetMediaTrackInfo_String(trk, "P_NAME", name, True)
        RPR_SetMediaTrackInfo_Value(trk, "D_VOL", 0.0) # Default to silent
        return trk
    return None

def insert_video(filepath, track_name="Video"):
    """Inserts video file onto a specific track at 0.0."""
    if not os.path.exists(filepath): return None

    # 1. Deselect everything (Critical to protect audio items)
    RPR_Main_OnCommand(40289, 0) # Unselect all items
    RPR_Main_OnCommand(40297, 0) # Unselect all tracks

    # 2. Select Target Track
    trk = get_track_by_name(track_name, create=True)
    RPR_SetTrackSelected(trk, 1)

    # 3. Insert
    RPR_SetEditCurPos(0.0, False, False)
    RPR_InsertMedia(filepath, 0) # Mode 0 = Add to selected track

    # Return the new item
    # InsertMedia selects the new item, so we grab the selection
    item = RPR_GetSelectedMediaItem(0, 0)

    # Force position to 0.0 and correct track (Safety)
    if item:
        RPR_SetMediaItemInfo_Value(item, "D_POSITION", 0.0)
        RPR_MoveMediaItemToTrack(item, trk)
    return item

def get_last_audio_item():
    """
    Heuristic: Finds the selected audio item.
    (REAPER selects newly recorded items by default).
    Ignores the 'Video' track.
    """
    vid_trk = get_track_by_name("Video")
    for i in range(RPR_CountSelectedMediaItems(0)):
        item = RPR_GetSelectedMediaItem(0, i)
        # Skip if it's on the video track
        if RPR_GetMediaItem_Track(item) != vid_trk:
            return item
    return None

def get_source_file(item):
    take = RPR_GetActiveTake(item)
    return RPR_GetMediaSourceFileName(RPR_GetMediaItemTake_Source(take), "", 512)[1] if take else None

# ==============================================================================
# 3. SYNC LOGIC
# ==============================================================================
def detect_offset(ref_path, target_path):
    """Runs the python sync detector and returns float offset in seconds."""
    try:
        cmd = [PYTHON_EXEC, AUDIO_SYNC_SCRIPT, ref_path, target_path]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return float(res.stdout.strip())
        else:
            console_msg(f"[Sync Error] {res.stderr}")
            return None
    except Exception as e:
        console_msg(f"[Sync] Exception: {e}")
        return None

def run_synchronization(audio_item, video_item, video_path):
    """Aligns video item to match audio item based on clap."""
    console_msg("--- Starting Auto-Sync ---")

    # 1. Get Audio File
    audio_path = get_source_file(audio_item)
    if not audio_path or not os.path.exists(audio_path):
        console_msg("❌ Reference audio file not found.")
        return

    console_msg(f"Comparing:\nRef: {os.path.basename(audio_path)}\nTgt: {os.path.basename(video_path)}")

    # 2. Calculate Offset
    # offset is positive if Target is LATE relative to Ref
    offset = detect_offset(audio_path, video_path)

    if offset is None:
        console_msg("❌ Sync failed.")
        return

    console_msg(f"Calculated Offset: {offset:.4f}s")

    # 3. Move Video Item
    # If target is late (+0.5s), we must move it LEFT (-0.5s) to align.
    audio_pos = RPR_GetMediaItemInfo_Value(audio_item, "D_POSITION")
    new_video_pos = audio_pos - offset

    RPR_SetMediaItemInfo_Value(video_item, "D_POSITION", new_video_pos)
    RPR_UpdateArrange()

    console_msg(f"✅ Synced! Video moved to {new_video_pos:.3f}s")

# ==============================================================================
# 4. CAMERA PROCESS CONTROLLER
# ==============================================================================
class CameraProcess:
    """Manages the background recording process and monitoring loop."""
    pid = 0
    port = 0
    log_cursor = 0
    timeout_counter = 0
    audio_item_ref = None # Store reference to audio item before we deselect it
    is_cancelling = False # Track cancel state

    @staticmethod
    def get_info():
        """Reads PID and Port from the lock file."""
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f:
                    lines = f.read().strip().split('\n')
                    if len(lines) >= 2:
                        return int(lines[0]), int(lines[1])
                    elif len(lines) == 1:
                        return int(lines[0]), None
            except: pass
        return None, None

    @staticmethod
    def cleanup():
        # Clean up all temp files
        for f in [PID_FILE, RESULT_FILE]:
            if os.path.exists(f):
                try: os.remove(f)
                except: pass

    @staticmethod
    def start():
        # Clean up stale files first to prevent false "Active" states
        CameraProcess.cleanup()
        CameraProcess.is_cancelling = False

        # Env setup
        env = os.environ.copy()

        # Add local dir to path for .so/.dll loading
        if platform.system() == "Windows":
             env["PATH"] = f"{BASE_DIR};{env.get('PATH','')}"
        else:
             env["LD_LIBRARY_PATH"] = f"{BASE_DIR}:{env.get('LD_LIBRARY_PATH','')}"

        try:
            log_f = open(LOG_FILE, "w")

            # Use DETACHED_PROCESS on Windows to hide console, standard fork elsewhere
            creation_flags = 0
            if platform.system() == "Windows":
                creation_flags = 0x00000008 # DETACHED_PROCESS

            proc = subprocess.Popen(
                [PYTHON_EXEC, "-u", RECORD_SCRIPT, get_project_path(), "3600"],
                cwd=BASE_DIR, env=env, stdout=log_f, stderr=subprocess.STDOUT,
                creationflags=creation_flags
            )

            # We don't write the PID file here anymore.
            # We wait for the CHILD to write it (indicating it bound the port).

            console_msg(f"[Camera] Launching (PID {proc.pid})... waiting for connection...")

            # Wait for child to initialize and write port (max 5s)
            start_wait = time.time()
            success = False
            while time.time() - start_wait < 5.0:
                pid, port = CameraProcess.get_info()
                if pid and port:
                    success = True
                    console_msg(f"[Camera] Connected on Port {port}")
                    break
                time.sleep(0.1)

            if not success:
                console_msg("[Error] Camera script failed to initialize (No Port detected).")
                try: proc.kill()
                except: pass
                return

            RPR_Main_OnCommand(1013, 0) # Record REAPER
            console_msg(f"[Camera] Started (PID {proc.pid})")

        except Exception as e:
            console_msg(f"Start failed: {e}")

    @staticmethod
    def stop(save=True):
        pid, port = CameraProcess.get_info()
        if not pid: return

        # 1. Stop REAPER
        RPR_Main_OnCommand(1016, 0)

        # 2. Capture Audio Item (It's selected right now!)
        CameraProcess.audio_item_ref = get_last_audio_item()
        CameraProcess.is_cancelling = not save

        # 3. Send Command via Socket (Robust IPC)
        msg = "SAVE" if save else "CANCEL"
        if save: console_msg("[Camera] Stopping & Downloading...")
        else: console_msg("[Camera] Cancelling...")

        try:
            if port:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect(('127.0.0.1', port))
                    s.sendall(msg.encode('utf-8'))
            else:
                # Fallback if port wasn't written for some reason
                console_msg("[Camera] Warning: No port found. Using force kill.")
                os.kill(pid, signal.SIGTERM)
        except Exception as e:
            console_msg(f"[Camera] IPC Error: {e}")
            # Ensure it dies if socket fails
            try: os.kill(pid, signal.SIGTERM)
            except: pass

        # 4. Start Monitoring Loop
        CameraProcess.pid = pid
        CameraProcess.log_cursor = 0
        CameraProcess.monitor_loop()

    @staticmethod
    def finish_import():
        """Helper: imports result file if it exists and performs sync."""
        if not os.path.exists(RESULT_FILE): return

        with open(RESULT_FILE, 'r') as f: vid_path = f.read().strip()

        # Import Video
        vid_item = insert_video(vid_path)
        if vid_item:
            console_msg(f"[Camera] Imported.")

            # Perform Sync if we have the audio item
            if CameraProcess.audio_item_ref:
                run_synchronization(CameraProcess.audio_item_ref, vid_item, vid_path)
            else:
                console_msg("[Sync] Skipped: No audio recording found.")

            # Move cursor to end of recording
            v_end = RPR_GetMediaItemInfo_Value(vid_item, "D_POSITION") + \
                    RPR_GetMediaItemInfo_Value(vid_item, "D_LENGTH")
            RPR_SetEditCurPos(v_end, True, False)

    @staticmethod
    def monitor_loop():
        """Recursive loop to check process status and stream logs."""
        # --- Stream Logs ---
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, 'r') as f:
                    f.seek(CameraProcess.log_cursor)
                    data = f.read()
                    if data:
                        RPR_ShowConsoleMsg(data)
                        CameraProcess.log_cursor = f.tell()
                        CameraProcess.timeout_counter = 0

                        # Trust "Done." to exit immediately (Avoids Zombie process wait)
                        if "Done." in data:
                            if not CameraProcess.is_cancelling: CameraProcess.finish_import()
                            else: console_msg("[Camera] Cancelled successfully.")
                            CameraProcess.cleanup()
                            return # DONE
            except: pass

        # --- Check Process ---
        try:
            os.kill(CameraProcess.pid, 0)
        except OSError:
            # Process died unexpectedly
            if not CameraProcess.is_cancelling: CameraProcess.finish_import()
            else: console_msg("[Camera] Cancelled successfully.")
            CameraProcess.cleanup()
            return

        # --- Timeout ---
        CameraProcess.timeout_counter += 1
        if CameraProcess.timeout_counter > 900: # ~30s
            console_msg("\n[Camera] Timeout (Force Killing).")
            try: os.kill(CameraProcess.pid, signal.SIGTERM)
            except: pass
            CameraProcess.cleanup()
            return

        RPR_defer("CameraProcess.monitor_loop()")

# ==============================================================================
# 5. ENTRY POINT
# ==============================================================================
def main():
    pid, port = CameraProcess.get_info()

    # Check if actually running
    is_running = False
    if pid:
        # Check if actually running
        try:
            os.kill(pid, 0)
            is_running = True
        except OSError:
            # Stale PID file
            CameraProcess.cleanup()

    if is_running:
        choice = RPR_ShowMessageBox("Recording active.\n\nYes = Save & Sync\nNo = Cancel", "Camera", 3)
        if choice == 6: CameraProcess.stop(save=True)
        elif choice == 7: CameraProcess.stop(save=False)
    else:
        CameraProcess.start()

if __name__ == "__main__":
    main()

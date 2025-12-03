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
import time
import ctypes
from reaper_python import *

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
# --- CROSS-PLATFORM PYTHON DETECTION ---
# Windows usually provides 'python', while Unix/Mac prefers 'python3'.
# On Windows, 'python3' often points to the 'App Execution Alias' (WindowsApps),
# which fails when called from scripts. We prioritize 'python' on Windows.

if platform.system() == "Windows":
    PYTHON_EXEC = shutil.which("python") or shutil.which("python3")
elif platform.system() == "Darwin":
    CANDIDATES = [
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.13",
        "/usr/local/bin/python3",
        shutil.which("python3"),
        shutil.which("python"),
    ]
    PYTHON_EXEC = next((p for p in CANDIDATES if p and os.path.exists(p)), None)
else:
    PYTHON_EXEC = shutil.which("python3") or shutil.which("python")

# Fallback: Check if the found python is the Windows Store stub
if PYTHON_EXEC and "WindowsApps" in PYTHON_EXEC and platform.system() == "Windows":
    # Try to find a better one if the first one was the stub
    # This happens if 'python' and 'python3' BOTH point to the stub
    # We try to guess standard install paths
    potential_paths = [
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs", "Python", "Python313", "python.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs", "Python", "Python312", "python.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs", "Python", "Python311", "python.exe"),
        "C:\\Python313\\python.exe",
        "C:\\Python312\\python.exe",
        "C:\\Python311\\python.exe",
    ]
    for p in potential_paths:
        if os.path.exists(p):
            PYTHON_EXEC = p
            break

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
TEMP_DIR = tempfile.gettempdir()
PID_FILE = os.path.join(TEMP_DIR, "canon_edsdk_controller.pid")
LOG_FILE = os.path.join(TEMP_DIR, "canon_edsdk_controller.log")

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

def check_ffmpeg_installed():
    """Checks if ffmpeg is available for sync."""
    # Check PATH
    if shutil.which("ffmpeg"): return True

    # Check Script Dir (Using BASE_DIR instead of __file__)
    if os.path.exists(os.path.join(BASE_DIR, "ffmpeg.exe")): return True

    return False

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

    # 1. Check dependency
    if not check_ffmpeg_installed():
        console_msg("[Sync] Warning: FFmpeg not found. Install FFmpeg to enable auto-sync.")
        return

    console_msg("--- Starting Auto-Sync ---")

    # 2. Get Audio File
    audio_path = get_source_file(audio_item)
    if not audio_path or not os.path.exists(audio_path):
        console_msg("Reference audio file not found.")
        return

    console_msg(f"Comparing:\nRef: {os.path.basename(audio_path)}\nTgt: {os.path.basename(video_path)}")

    # 3. Calculate Offset
    offset = detect_offset(audio_path, video_path)

    if offset is None:
        console_msg("Sync failed (Could not detect clap match).")
        return

    console_msg(f"Calculated Offset: {offset:.4f}s")

    # 4. Move Video Item
    audio_pos = RPR_GetMediaItemInfo_Value(audio_item, "D_POSITION")
    new_video_pos = audio_pos - offset

    RPR_SetMediaItemInfo_Value(video_item, "D_POSITION", new_video_pos)
    RPR_UpdateArrange()

    console_msg(f"Synced! Video moved to {new_video_pos:.3f}s")

# ==============================================================================
# 4. CAMERA PROCESS CONTROLLER
# ==============================================================================
class CameraProcess:
    """Manages the background recording process."""

    save_mode = True
    audio_item_ref = None # Store reference to audio item before we deselect it
    last_log_pos = 0

    @staticmethod
    def get_pid():
        """Reads PID from the lock file."""
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f:
                    content = f.read().strip()
                    if content:
                        return int(content)
            except: pass
        return None

    @staticmethod
    def start():
        # Clean up stale file
        if os.path.exists(PID_FILE):
            try: os.remove(PID_FILE)
            except: pass

        # Reset log tracking
        CameraProcess.last_log_pos = 0

        # Env setup
        env = os.environ.copy()

        # Add local dir to path for .so/.dll loading
        if platform.system() == "Windows":
             env["PATH"] = f"{BASE_DIR};{env.get('PATH','')}"
        else:
             env["LD_LIBRARY_PATH"] = f"{BASE_DIR}:{env.get('LD_LIBRARY_PATH','')}"

        try:
            console_msg(f"Using Python: {PYTHON_EXEC}")

            # Use DETACHED_PROCESS on Windows to hide console
            creation_flags = 0x00000008 if platform.system() == "Windows" else 0

            # DEBUG: Capture stderr to see why it crashes
            proc = subprocess.Popen(
                [PYTHON_EXEC, "-u", RECORD_SCRIPT, get_project_path(), "3600"],
                cwd=BASE_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=creation_flags
            )

            console_msg(f"[Camera] Launching (PID {proc.pid})...")

            # Wait for child to initialize and write PID (max 5s)
            start_wait = time.time()
            success = False
            while time.time() - start_wait < 5.0:
                # Check if process died
                if proc.poll() is not None:
                    break

                pid = CameraProcess.get_pid()
                if pid:
                    success = True
                    break
                time.sleep(0.1)

            if not success:
                # If we are here, process either died or timed out
                console_msg("[Error] Camera script failed to initialize.")

                # Check for error output
                if proc.poll() is not None:
                    # Process died, read stderr
                    err_out = proc.stderr.read().decode('utf-8', errors='ignore')
                    if err_out:
                        console_msg(f"\n--- CONTROLLER CRASH LOG ---\n{err_out}\n----------------------------")
                    else:
                        console_msg("[Error] Process died silently.")
                else:
                    console_msg("[Error] Process timed out (no PID file).")
                    try: proc.kill()
                    except: pass

                return

            RPR_Main_OnCommand(1013, 0) # Record REAPER
            console_msg(f"[Camera] Started.")
            # Exit immediately to avoid "Terminate Instance" dialog
            # The controller script runs in background

        except Exception as e:
            console_msg(f"Start failed: {e}")

    @staticmethod
    def stop(save=True):
        pid = CameraProcess.get_pid()
        if not pid: return

        # 1. Stop REAPER Audio Immediately
        RPR_Main_OnCommand(1016, 0)

        # 2. Capture Audio Item
        CameraProcess.audio_item_ref = get_last_audio_item()
        CameraProcess.save_mode = save

        # 3. Send Command via File
        filename = "canon_edsdk_cmd_save" if save else "canon_edsdk_cmd_cancel"
        cmd_path = os.path.join(TEMP_DIR, filename)

        if save: console_msg("[Camera] Stopping & Downloading...")
        else: console_msg("[Camera] Cancelling...")

        try:
            # "Touch" the command file to signal the controller
            with open(cmd_path, 'w') as f:
                pass

            # 4. Start Non-Blocking Monitor Loop
            # This allows REAPER GUI to refresh while downloading
            RPR_defer("CameraProcess.monitor_download_loop()")

        except Exception as e:
            console_msg(f"[Camera] Error: {e}")

    @staticmethod
    def monitor_download_loop():
        """Reads log file without blocking the REAPER UI."""

        # Check liveness first (so we know if this is the last run)
        pid_alive = os.path.exists(PID_FILE)

        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    # Seek to where we last read
                    f.seek(CameraProcess.last_log_pos)
                    new_data = f.read()
                    CameraProcess.last_log_pos = f.tell()

                    if new_data:
                        for line in new_data.split('\n'):
                            line = line.strip()
                            if not line: continue

                            if line.startswith("LOG:"):
                                RPR_ShowConsoleMsg(line[4:] + "\n")
                            elif line.startswith("RESULT:"):
                                path = line[7:].strip()
                                if CameraProcess.save_mode:
                                    CameraProcess.finish_import(path, CameraProcess.audio_item_ref)

                                CameraProcess.cleanup()
                                return
            except Exception:
                pass # File busy or locking issue, retry next tick

        # If the process is dead (PID file gone), we stop monitoring.
        # This handles the "Cancel" case (where no RESULT is sent)
        # or a crash where the controller exited.
        if not pid_alive:
             CameraProcess.cleanup()
             return

        # Keep running loop in next cycle
        RPR_defer("CameraProcess.monitor_download_loop()")

    @staticmethod
    def cleanup():
        # Cleanup files
        if os.path.exists(PID_FILE):
            try: os.remove(PID_FILE)
            except: pass

    @staticmethod
    def finish_import(vid_path, audio_item):
        if not os.path.exists(vid_path): return

        vid_item = insert_video(vid_path)
        if vid_item:
            console_msg(f"[Camera] Imported.")
            if audio_item:
                run_synchronization(audio_item, vid_item, vid_path)
            else:
                console_msg("[Sync] Skipped: No audio recording found.")

# ==============================================================================
# 5. ENTRY POINT
# ==============================================================================
def is_process_running_win(pid):
    """
    Checks if a process is running using Windows Kernel32 API directly.
    Bypasses os.kill/tasklist to avoid SystemError/WinError 6 on Python 3.13+.
    """
    try:
        # Constants from Windows API
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        STILL_ACTIVE = 259

        # Open the process with limited rights (enough to query status)
        h_process = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
            False, pid
        )

        if not h_process:
            return False

        # Get exit code
        exit_code = ctypes.c_ulong()
        success = ctypes.windll.kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(h_process)

        if not success:
            return False

        # If exit code is 259 (STILL_ACTIVE), it's running
        return exit_code.value == STILL_ACTIVE

    except Exception:
        return False

def main():
    pid = CameraProcess.get_pid()
    is_running = False

    # 1. Determine if running
    if pid:
        if platform.system() == "Windows":
            is_running = is_process_running_win(pid)
        else:
            # Fallback for Mac/Linux
            try:
                os.kill(pid, 0)
                is_running = True
            except OSError:
                is_running = False

    # 2. Logic
    if is_running:
        # Process is alive: Show Stop/Cancel Dialog
        choice = RPR_ShowMessageBox("Recording active.\n\nYes = Save & Sync\nNo = Cancel", "Camera", 3)

        if choice == 6: # Yes
            CameraProcess.stop(save=True)
        else: # No (7), Cancel (2), or Closed (-1) -> Stop without saving
            CameraProcess.stop(save=False)

    else:
        # Process is dead or not found
        if pid:
            # If a PID file existed but process was dead, check logs to see why it crashed
            console_msg(f"[Warning] Previous process {pid} not found (Crashed?). Starting new session...")
            if os.path.exists(LOG_FILE):
                try:
                    with open(LOG_FILE, 'r') as f:
                        lines = f.readlines()
                        if lines:
                            console_msg(f"--- Last Log Lines ---\n{''.join(lines[-3:])}----------------------")
                except: pass

        CameraProcess.start()

if __name__ == "__main__":
    main()

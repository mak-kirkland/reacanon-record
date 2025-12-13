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
    RPR_ShowMessageBox("Could not find Python! Please install Python 3.", "ERROR", 0)
    sys.exit(1)

# ==============================================================================
# 2. LOGGING & HELPERS
# ==============================================================================
def log(msg, context="System", level="INFO"):
    """
    Consistent logging format.
    Args:
        msg (str): The message to log.
        context (str): The system component (e.g., "System", "Camera", "Sync").
        level (str): Log level ("INFO", "ERROR", "WARNING"). Defaults to "INFO".
    """
    # Simplified format string
    prefix = f"[{context}]"
    if level != "INFO":
        prefix += f" [{level}]"

    RPR_ShowConsoleMsg(f"{prefix} {msg}\n")

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
        log(
            f"FATAL: Script folder not found!\n"
            f"Expected location: {BASE_DIR}\n"
            f"Please create this folder and move the python scripts there.",
            "System", "ERROR"
        )
        # Exit immediately prevents the rest of the script from crashing with NameErrors
        sys.exit(1)

except Exception as e:
    # Allow sys.exit() to work if called above
    if isinstance(e, SystemExit):
        raise

    log(f"Failed to determine script path: {e}", "System", "ERROR")
    sys.exit(1)

RECORD_SCRIPT = os.path.join(BASE_DIR, "canon_edsdk_controller.py")
AUDIO_SYNC_SCRIPT   = os.path.join(BASE_DIR, "audio_sync_detector.py")

# Files
TEMP_DIR = tempfile.gettempdir()
PID_FILE = os.path.join(TEMP_DIR, "canon_edsdk_controller.pid")
LOG_FILE = os.path.join(TEMP_DIR, "canon_edsdk_controller.log")

FFMPEG_PATHS_MACOS = [
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/usr/homebrew/bin",
    BASE_DIR,
]

def get_path_env():
    """Returns system PATH that is necessary for the Python process to find ffmpeg"""
    # Env setup
    env = os.environ.copy()

    # Add local dir to path for .so/.dll loading
    if platform.system() == "Windows":
            env["PATH"] = f"{BASE_DIR};{env.get('PATH','')}"
    elif platform.system() == "Darwin":  # macOS
        # Fix PATH since REAPER.app strips it
        extra_paths = [
            "/usr/local/bin",        # Intel brew (older Macs)
            "/opt/homebrew/bin",     # Apple Silicon brew
            "/usr/homebrew/bin",     # custom brew prefix (your case)
            BASE_DIR,                # local libs/binaries
        ]
        env["PATH"] = ":".join([env.get("PATH", "")] + extra_paths)
    else:
        env["LD_LIBRARY_PATH"] = f"{BASE_DIR}:{env.get('LD_LIBRARY_PATH','')}"
    return env

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

    path = ":".join(FFMPEG_PATHS_MACOS) if platform.system() == "Darwin" else None
    # Check PATH
    if shutil.which(
        "ffmpeg", 
        path=path):
        return True

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
        res = subprocess.run(cmd, capture_output=True, text=True, env=get_path_env())
        if res.returncode == 0:
            return float(res.stdout.strip())
        else:
            # Child script usually prints to stderr
            err_msg = res.stderr.strip() or "Unknown error"
            log(err_msg, "Sync", "ERROR")
            return None
    except Exception as e:
        log(f"Exception: {e}", "Sync", "ERROR")
        return None

def run_synchronization(audio_item, video_item, video_path):
    """Aligns video item to match audio item based on clap."""

    # 1. Check dependency
    if not check_ffmpeg_installed():
        log("FFmpeg not found. Install FFmpeg to enable auto-sync.", "Sync", "WARNING")
        return

    log("Starting Auto-Sync...", "Sync")
    log(f"Comparing: '{get_source_file(audio_item)}' to '{os.path.basename(video_path)}'", "Sync")

    # 2. Get Audio File
    audio_path = get_source_file(audio_item)
    if not audio_path or not os.path.exists(audio_path):
        log("Reference audio file not found on selected track.", "Sync", "ERROR")
        return

    # 3. Calculate Offset
    offset = detect_offset(audio_path, video_path)

    if offset is None:
        log("Failed to detect clap match.", "Sync", "WARNING")
        return

    # 4. Move Video Item
    audio_pos = RPR_GetMediaItemInfo_Value(audio_item, "D_POSITION")
    new_video_pos = audio_pos - offset

    RPR_SetMediaItemInfo_Value(video_item, "D_POSITION", new_video_pos)
    RPR_UpdateArrange()

    log(f"Success! Video moved to {new_video_pos:.3f}s (Offset: {offset:.4f}s)", "Sync")

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

        try:
            log(f"Using Python: {PYTHON_EXEC}", "System")

            # Use DETACHED_PROCESS on Windows to hide console
            creation_flags = 0x00000008 if platform.system() == "Windows" else 0

            # DEBUG: Capture stderr to see why it crashes
            proc = subprocess.Popen(
                [PYTHON_EXEC, "-u", RECORD_SCRIPT, get_project_path(), "3600"],
                cwd=BASE_DIR, env=get_path_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=creation_flags
            )

            log(f"Launching camera process (PID {proc.pid})...", "System")

            # Wait for child to initialize and write PID (max 5s)
            start_wait = time.time()
            success = False
            while time.time() - start_wait < 5.0:
                # Check if process died
                if proc.poll() is not None:
                    break

                if CameraProcess.get_pid():
                    success = True
                    break
                time.sleep(0.1)

            if not success:
                # If we are here, process either died or timed out
                log("Camera script failed to initialize.", "System", "ERROR")

                # Check for error output
                if proc.poll() is not None:
                    # Process died, read stderr
                    err_out = proc.stderr.read().decode('utf-8', errors='ignore')
                    if err_out:
                        log(f"\n--- CONTROLLER CRASH LOG ---\n{err_out}\n----------------------------", "System", "ERROR")
                    else:
                        log("Process died silently.", "System", "ERROR")
                else:
                    log("Process timed out (no PID file).", "System", "ERROR")
                    try: proc.kill()
                    except: pass

                return

            RPR_Main_OnCommand(1013, 0) # Record REAPER
            log("Started.", "System")
            # Exit immediately to avoid "Terminate Instance" dialog
            # The controller script runs in background

        except Exception as e:
            log(f"Start failed: {e}", "System", "ERROR")

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

        if save: log("Stopping & Downloading...", "System")
        else: log("Cancelling recording...", "System")

        try:
            # "Touch" the command file to signal the controller
            with open(cmd_path, 'w') as f:
                pass

            # 4. Start Non-Blocking Monitor Loop
            # This allows REAPER GUI to refresh while downloading
            RPR_defer("CameraProcess.monitor_download_loop()")

        except Exception as e:
            log(f"IPC Error: {e}", "System", "ERROR")

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
                            if not line.strip(): continue

                            # Robust parsing using partition (safe against missing colons)
                            # Expects: TYPE:MESSAGE
                            msg_type, sep, payload = line.partition(":")

                            if not sep: continue # Malformed line, skip

                            if msg_type == "LOG":
                                # Controller logs no longer contain [Camera] prefix
                                # We add it here by using the "Camera" context
                                log(payload, "Camera")

                            elif msg_type == "RESULT":
                                path = payload.strip()
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
            log("Video imported to timeline.", "System")
            if audio_item:
                run_synchronization(audio_item, vid_item, vid_path)
            else:
                log("No audio recording found. Skipping sync.", "Sync", "WARNING")

        log("Done.", "System")

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
            log(f"Cleaning up previous session (PID {pid})...", "System", "WARNING")
            if os.path.exists(LOG_FILE):
                try:
                    with open(LOG_FILE, 'r') as f:
                        lines = f.readlines()
                        if lines:
                             log(f"\n--- Last Log Lines ---\n{''.join(lines[-3:])}----------------------", "System", "WARNING")
                except: pass

        CameraProcess.start()

if __name__ == "__main__":
    main()

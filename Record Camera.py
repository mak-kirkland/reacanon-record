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
import subprocess
import signal
from reaper_python import *

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
PYTHON_EXEC = "/usr/bin/python3"
BASE_DIR    = "/home/michael/audio/camera-script"
RECORD_SCRIPT = os.path.join(BASE_DIR, "canon_edsdk_controller.py")
CLAP_SCRIPT   = os.path.join(BASE_DIR, "audio_sync_detector.py")

# Files
PID_FILE    = "/tmp/camera_script.pid"
RESULT_FILE = "/tmp/camera_result.txt"
LOG_FILE    = "/tmp/camera_log.txt"

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
def detect_clap(filepath):
    """Runs the python clap detector and returns float timestamp."""
    try:
        cmd = [PYTHON_EXEC, CLAP_SCRIPT, filepath]
        res = subprocess.run(cmd, capture_output=True, text=True)
        return float(res.stdout.strip()) if res.returncode == 0 else None
    except Exception as e:
        console_msg(f"[Sync] Error: {e}")
        return None

def run_synchronization(audio_item, video_item, video_path):
    """Aligns video item to match audio item based on clap."""
    console_msg("--- Starting Auto-Sync ---")

    # 1. Get Audio Clap
    audio_path = get_source_file(audio_item)
    console_msg(f"Analyzing Audio: {os.path.basename(audio_path)}")
    t_audio_clap = detect_clap(audio_path)

    if t_audio_clap is None:
        console_msg("❌ Could not find clap in audio.")
        return

    # 2. Get Video Clap
    console_msg(f"Analyzing Video: {os.path.basename(video_path)}")
    t_video_clap = detect_clap(video_path)

    if t_video_clap is None:
        console_msg("❌ Could not find clap in video.")
        return

    # 3. Calculate Offset
    # Absolute Time of Clap = Audio_Start + Audio_Clap_Offset
    audio_start = RPR_GetMediaItemInfo_Value(audio_item, "D_POSITION")
    abs_clap_time = audio_start + t_audio_clap

    # New Video Position = Absolute_Clap_Time - Video_Clap_Offset
    new_video_pos = abs_clap_time - t_video_clap
    RPR_SetMediaItemInfo_Value(video_item, "D_POSITION", new_video_pos)

    # 4. Add Marker
    RPR_AddProjectMarker(0, False, abs_clap_time, 0, "Sync Point", -1)
    RPR_UpdateArrange()
    console_msg(f"✅ Synced! Video moved to {new_video_pos:.3f}s")

# ==============================================================================
# 4. CAMERA PROCESS CONTROLLER
# ==============================================================================
class CameraProcess:
    """Manages the background recording process and monitoring loop."""
    pid = 0
    log_cursor = 0
    timeout_counter = 0
    audio_item_ref = None # Store reference to audio item before we deselect it
    is_cancelling = False # Track cancel state

    @staticmethod
    def get_pid():
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f: return int(f.read().strip())
            except: pass
        return None

    @staticmethod
    def cleanup():
        if os.path.exists(PID_FILE): os.remove(PID_FILE)
        if os.path.exists(RESULT_FILE): os.remove(RESULT_FILE)

    @staticmethod
    def start():
        # Clean up stale files first to prevent false "Active" states
        CameraProcess.cleanup()
        CameraProcess.is_cancelling = False

        # Env setup
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = f"{BASE_DIR}:{env.get('LD_LIBRARY_PATH','')}"

        try:
            log_f = open(LOG_FILE, "w")
            proc = subprocess.Popen(
                [PYTHON_EXEC, "-u", RECORD_SCRIPT, get_project_path(), "3600"],
                cwd=BASE_DIR, env=env, stdout=log_f, stderr=subprocess.STDOUT
            )
            with open(PID_FILE, 'w') as f: f.write(str(proc.pid))

            RPR_Main_OnCommand(1013, 0) # Record
            console_msg(f"[Camera] Started (PID {proc.pid})")
        except Exception as e:
            console_msg(f"Start failed: {e}")

    @staticmethod
    def stop(save=True):
        pid = CameraProcess.get_pid()
        if not pid: return

        # 1. Stop REAPER
        RPR_Main_OnCommand(1016, 0)

        # 2. Capture Audio Item (It's selected right now!)
        CameraProcess.audio_item_ref = get_last_audio_item()
        CameraProcess.is_cancelling = not save

        # 3. Signal Camera Script
        try:
            if save:
                console_msg("[Camera] Stopping & Downloading...")
                os.kill(pid, signal.SIGINT) # Stop & Download
            else:
                console_msg("[Camera] Cancelling (Waiting for camera flush)...")
                # Use SIGTERM to allow script to clean up and discard file gracefully
                os.kill(pid, signal.SIGTERM)
        except OSError: pass

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
            try: os.kill(CameraProcess.pid, signal.SIGKILL)
            except: pass
            CameraProcess.cleanup()
            return

        RPR_defer("CameraProcess.monitor_loop()")

# ==============================================================================
# 5. ENTRY POINT
# ==============================================================================
def main():
    pid = CameraProcess.get_pid()
    if pid:
        # Check if actually running
        try:
            os.kill(pid, 0)
            choice = RPR_ShowMessageBox("Recording active.\n\nYes = Save & Sync\nNo = Cancel", "Camera", 3)
            if choice == 6: CameraProcess.stop(save=True)
            elif choice == 7: CameraProcess.stop(save=False)
        except OSError:
            # Stale PID file found, clean up and restart
            CameraProcess.cleanup()
            CameraProcess.start()
    else:
        CameraProcess.start()

if __name__ == "__main__":
    main()

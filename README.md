# ReaCanon Record: Canon DSLR Integration for REAPER

This toolset integrates Canon DSLRs directly into REAPER, treating the camera as a native media source. It handles recording triggers, automatic file downloading, and **sample-accurate audio/video synchronization** using a clap/slate.

## Features
- **Remote Trigger:** Start/Stop camera recording directly from REAPER.
- **Auto-Import:** Automatically downloads the video file to your project after recording.
- **Auto-Sync:** Uses FFT-based audio cross-correlation to align the camera video with your high-quality audio recording (requires a clap/slate).
- **Background Processing:** Keeps REAPER responsive while the camera is recording.

---

## 1. Prerequisites

### Hardware
- Canon DSLR/Mirrorless Camera supported by EDSDK.
- USB Cable.

### System Dependencies
- **Python 3.6+**
- **FFmpeg** installed on the system path.

### Python Libraries
Install the required scientific libraries for the sync algorithm:

```bash
pip3 install numpy scipy ffmpeg-python
```

### Canon SDK Library
You need the compiled Canon EDSDK binaries for your operating system. These can be obtained from the Canon Developer Community or other compatible sources.

**Windows:**

1. Locate `EDSDK.dll` and `EdsImage.dll` (usually found in the `Dll` folder of the SDK).

2. Place both files inside this script folder.

**macOS:**

1. Locate `EDSDK.framework`.

2. Install it to `/Library/Frameworks/` OR place the `EDSDK.framework` folder inside this script folder.

**Linux:**

1. Locate `libEDSDK.so`.

2. Place `libEDSDK.so` inside this script folder (or in `/usr/local/lib/`).

---

## 2. Installation

To ensure the dynamic pathing works correctly, you **must** use the standard REAPER directory structure.

1. Locate your REAPER resource path (usually `~/.config/REAPER/`).
2. Navigate to the `Scripts` folder.
3. Create a folder named exactly `reacanon-record`.
4. Copy all `.py` files and your OS-specific library files into this folder.

**Target Path Structure:**
```text
.../REAPER/Scripts/reacanon-record/
├── Record Camera.py            (Main REAPER Action)
├── canon_edsdk_controller.py   (Background Service)
├── edsdk_defs.py               (C-Types Wrapper)
├── audio_sync_detector.py      (Sync Logic)
└── libEDSDK.so                 (Required Library)
```

---

## 3. Usage in REAPER

1. **Load the Script:**
   - Open REAPER.
   - Open the **Actions List** (`?`).
   - Click **New Action** > **Load ReaScript...**
   - Select `Record Camera.py`.

2. **Run the Recording:**
   - Ensure your Camera is connected via USB and awake.
   - Run the **Record Camera.py** action (double-click or assign a shortcut).
   - *Status:* A dialog box will appear saying "Recording active".

3. **The Workflow:**
   - **Clap / Slate:** Make a loud clap visible to the camera and audible to your microphones.
   - **Record:** Perform your take.
   - **Stop:** When finished, click **Yes (Save & Sync)** on the dialog box.

4. **Result:**
   - The script will stop the camera.
   - It will download the video file.
   - It will analyze the audio waveforms.
   - It will insert the video onto a "Video" track and slide it to align perfectly with your audio.

---

## 4. Troubleshooting

### "Script folder not found!"
The script expects to live in `Scripts/reacanon-record/`. If you see this error, ensure the folder name matches exactly and is located inside the REAPER resource path.

### "Sync failed"
- Ensure `ffmpeg` is installed and accessible in your terminal (`which ffmpeg`).
- Ensure the clap was loud enough.
- The sync scan duration is set to **60 seconds**. Ensure you clap within the first minute of recording.

### Camera not recording / "Zombie" process
If the script crashes or the USB cable is pulled, the camera might remain "Locked" by the driver.
- Disconnect USB.
- Turn off Camera.
- Re-run the script (it includes a "Force Unlock" mechanism on startup).

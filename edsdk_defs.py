#!/usr/bin/env python3
"""
edsdk_defs.py
---------------------------------
Low-level wrapper for Canon EDSDK.
Contains all ctypes definitions, constants, and structure mappings.

This isolates the C-interface complexity from the main logic script.

Author: Slav Basharov
Co-author: Michael Kirkland
"""

import sys
from ctypes import *

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Paths where we might find the compiled EDSDK shared library
LIB_PATHS = [
    "libEDSDK.so",
    "./libEDSDK.so",
    "../libEDSDK.so",
    "/usr/local/lib/libEDSDK.so"
]

# ==============================================================================
# CONSTANTS & FLAGS
# ==============================================================================
# Error Codes
EDS_ERR_OK = 0x00000000
EDS_ERR_DEVICE_BUSY = 0x00000080
EDS_ERR_NOT_READY = 0x00002019  # Camera is not in a recordable state

# Property IDs (What we want to change/read)
kEdsPropID_SaveTo = 0x0000000B
kEdsPropID_Record = 0x00000510
kEdsPropID_Evf_OutputDevice = 0x00000500

# Property Values
kEdsSaveTo_Camera = 1
kEdsEvfOutputDevice_TFT = 1  # Transfer output to screen (LiveView/Video)
EDS_RECORD_START = 4
EDS_RECORD_STOP = 0

# Camera Commands
kEdsCameraStatusCommand_UIUnLock = 1        # Release UI lock
kEdsCameraStatusCommand_ExitDirectTransfer = 3

# Events
kEdsObjectEvent_All = 0x00000200
kEdsObjectEvent_DirItemCreated = 0x00000204 # New file created on card

# ==============================================================================
# C STRUCTURES
# ==============================================================================
class EdsDirectoryItemInfo(Structure):
    """
    Represents metadata for a file stored on the camera.
    Maps to C struct: EdsDirectoryItemInfo
    """
    _fields_ = [
        ("size", c_uint64),        # File size in bytes
        ("isFolder", c_uint32),    # Boolean flag
        ("groupID", c_uint32),
        ("option", c_uint32),
        ("szFileName", c_char * 256), # Filename string
        ("format", c_uint32),
        ("dateTime", c_uint32),
    ]

# ==============================================================================
# CALLBACK TYPES
# ==============================================================================
# Defines the signature for Python functions that can be called by C code.
# Format: CFUNCTYPE(ReturnType, Arg1Type, Arg2Type, ...)

# Callback for object events (File creation, etc.)
EdsObjectEventHandler = CFUNCTYPE(c_int32, c_uint32, c_void_p, c_void_p)

# Callback for file download progress
EdsProgressCallback = CFUNCTYPE(c_int32, c_uint32, c_void_p, POINTER(c_bool))

# ==============================================================================
# LIBRARY WRAPPER
# ==============================================================================
class EdsdkWrapper:
    """
    Manages loading the Shared Object (.so) and defining C function prototypes.
    """
    def __init__(self):
        self.lib = self._load_library()
        self._define_prototypes()
        # Helper shortcut for retaining objects (preventing garbage collection)
        self.Retain = getattr(self.lib, "EdsRetain")
        self.Release = getattr(self.lib, "EdsRelease")

    def _load_library(self):
        """Attempts to load libEDSDK.so from known paths."""
        for path in LIB_PATHS:
            try:
                return CDLL(path)
            except OSError:
                continue
        print(f"❌ Error: Could not load libEDSDK.so. Checked: {LIB_PATHS}")
        sys.exit(1)

    def _define_prototypes(self):
        """Defines input/output types for C functions to prevent segfaults."""
        def proto(name, res, args):
            try:
                f = getattr(self.lib, name)
                f.restype = res
                f.argtypes = args
            except AttributeError:
                print(f"⚠️ Warning: Function {name} not found in library.")

        # SDK Lifecycle
        proto("EdsInitializeSDK", c_int32, [])
        proto("EdsTerminateSDK", c_int32, [])

        # Session Management
        proto("EdsGetCameraList", c_int32, [POINTER(c_void_p)])
        proto("EdsGetChildCount", c_int32, [c_void_p, POINTER(c_uint32)])
        proto("EdsGetChildAtIndex", c_int32, [c_void_p, c_uint32, POINTER(c_void_p)])
        proto("EdsOpenSession", c_int32, [c_void_p])
        proto("EdsCloseSession", c_int32, [c_void_p])
        proto("EdsRelease", c_int32, [c_void_p])
        proto("EdsRetain", c_int32, [c_void_p])

        # Properties & Commands
        proto("EdsSetPropertyData", c_int32, [c_void_p, c_uint32, c_int32, c_uint32, c_void_p])
        proto("EdsSendStatusCommand", c_int32, [c_void_p, c_uint32, c_int32])

        # Events & Files
        proto("EdsSetObjectEventHandler", c_int32, [c_void_p, c_uint32, c_void_p, c_void_p])
        proto("EdsGetDirectoryItemInfo", c_int32, [c_void_p, POINTER(EdsDirectoryItemInfo)])
        proto("EdsCreateFileStream", c_int32, [c_char_p, c_uint32, c_uint32, POINTER(c_void_p)])
        proto("EdsDownload", c_int32, [c_void_p, c_uint64, c_void_p])
        proto("EdsDownloadComplete", c_int32, [c_void_p])
        proto("EdsGetEvent", c_int32, [])
        proto("EdsSetProgressCallback", c_int32, [c_void_p, c_void_p, c_int32, c_void_p])

# Singleton instance for import
sdk = EdsdkWrapper()

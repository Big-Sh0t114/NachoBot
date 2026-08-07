"""Load the active environment's ONNX Runtime before sherpa-onnx on Windows."""

import ctypes
import os
from pathlib import Path
import sys
from typing import Optional


_DLL_DIRECTORY_HANDLE: Optional[object] = None
_ONNXRUNTIME_HANDLE: Optional[object] = None


def preload_onnxruntime() -> None:
    """Prevent an older System32 onnxruntime.dll from shadowing the venv DLL."""
    global _DLL_DIRECTORY_HANDLE, _ONNXRUNTIME_HANDLE

    if sys.platform != "win32" or _ONNXRUNTIME_HANDLE is not None:
        return

    project_root = Path(__file__).resolve().parents[2]
    candidates = [
        Path(sys.prefix) / "Lib" / "site-packages" / "onnxruntime" / "capi",
        (
            project_root
            / ".venv"
            / "Lib"
            / "site-packages"
            / "onnxruntime"
            / "capi"
        ),
    ]

    for capi_dir in candidates:
        runtime_dll = capi_dir / "onnxruntime.dll"
        if not runtime_dll.is_file():
            continue

        try:
            _DLL_DIRECTORY_HANDLE = os.add_dll_directory(str(capi_dir))
            _ONNXRUNTIME_HANDLE = ctypes.WinDLL(str(runtime_dll))
            return
        except OSError:
            if _DLL_DIRECTORY_HANDLE is not None:
                _DLL_DIRECTORY_HANDLE.close()
            _DLL_DIRECTORY_HANDLE = None
            _ONNXRUNTIME_HANDLE = None

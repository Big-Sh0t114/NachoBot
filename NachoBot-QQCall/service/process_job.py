"""Contain CLI-owned children for the entire Windows supervisor lifetime.

The sole non-inheritable job handle deliberately lives until process teardown.
Closing it explicitly would also terminate the supervisor before its final
status is printed. Existing adapters are never assigned to this job.
"""

import ctypes
from ctypes import wintypes
import os


class ProcessJobError(RuntimeError):
    code = "service_process_job_unavailable"


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_lifetime_job = None


def ensure_process_job():
    """Call only from the dedicated CLI, before spawning any service children."""
    global _lifetime_job
    if os.name != "nt" or _lifetime_job is not None:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, result, arguments in (
        ("CreateJobObjectW", wintypes.HANDLE, [wintypes.LPVOID, wintypes.LPCWSTR]),
        ("SetInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]),
        ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        ("GetCurrentProcess", wintypes.HANDLE, []),
        ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
    ):
        method = getattr(kernel, name)
        method.restype, method.argtypes = result, arguments
    # NULL security attributes make the handle non-inheritable. Children join
    # the job by default, but cannot keep its last handle alive after our death.
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ProcessJobError(ProcessJobError.code)
    assigned = False
    try:
        limits = _ExtendedLimits()
        # KILL_ON_JOB_CLOSE; explicit BREAKAWAY_OK preserves the shared
        # UniversalVC child's existing CREATE_BREAKAWAY_FROM_JOB contract.
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ProcessJobError(ProcessJobError.code)
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise ProcessJobError(ProcessJobError.code)
        assigned = True
        _lifetime_job = (kernel, handle)
    finally:
        if not assigned:
            kernel.CloseHandle(handle)

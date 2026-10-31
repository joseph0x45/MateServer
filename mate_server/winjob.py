"""Tie the children's lives to the supervisor's on Windows.

POSIX has process groups and systemd's cgroup to clean up after a supervisor that was killed; on
Windows, TerminateProcess on the supervisor leaves the poller and the web server running — still
holding the port and the cloud session, so the next start collides with them. A Job Object with
KILL_ON_JOB_CLOSE closes that hole: the job's only handle belongs to the supervisor, the OS closes it
however the supervisor dies, and closing it kills every process in the job.

Found by running the service under WinSW: its Ctrl+C could not reach the supervisor ("Failed to
attach to console. Access is denied"), so it terminated it outright and then crashed looking for
the children.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class KillOnCloseJob:
    """Children assigned here die with this process. A no-op off Windows, and never fatal: without
    the job the server still works, it merely loses the guarantee."""

    def __init__(self, log=print):
        self._job = None
        self._log = log
        if sys.platform != "win32":
            return
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD)
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            self._k32 = k32
            job = k32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                               ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._job = job
        except Exception as exc:                               # noqa: BLE001
            log(f"could not create a job object ({exc}); children may outlive a killed supervisor")

    def assign(self, proc) -> None:
        if not self._job:
            return
        if not self._k32.AssignProcessToJobObject(self._job, wintypes.HANDLE(int(proc._handle))):
            err = ctypes.get_last_error()
            self._log(f"could not put pid {proc.pid} in the job object (error {err})")

"""Win32 helpers (ctypes only).

Only *reads* public window information (window owner pid, rectangle, foreground
state) and the system process list. No process handle is ever opened: exe names
come from a Toolhelp snapshot, so nothing here touches the game, the client or an
anti-cheat-protected process. Nothing injects code or reads another process's memory.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x4000

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


@dataclass(frozen=True)
class TargetWindow:
    hwnd: int
    pid: int
    title: str
    rect: Rect


# ---- process names from a Toolhelp snapshot (no OpenProcess) -----------------
_SNAPSHOT_MIN_INTERVAL = 1.0
_exe_by_pid: dict[int, str] = {}
_snapshot_at = -1e9


def snapshot_processes() -> dict[int, str]:
    """pid -> exe file name for every running process."""
    out: dict[int, str] = {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = entry.szExeFile
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def _refresh(force: bool = False) -> None:
    global _exe_by_pid, _snapshot_at
    now = time.monotonic()
    if force or now - _snapshot_at >= _SNAPSHOT_MIN_INTERVAL:
        _exe_by_pid = snapshot_processes()  # replaces the map: dead/reused pids drop out
        _snapshot_at = now


def process_exe_name(pid: int) -> str:
    """Executable file name (e.g. 'foo.exe') of a process, '' if unknown."""
    name = _exe_by_pid.get(pid)
    if name is None:
        _refresh()  # rate-limited: unknown pids don't trigger a snapshot storm
        name = _exe_by_pid.get(pid, "")
    return name


def running_exes(names: set[str], max_age_s: float = 2.0) -> set[str]:
    """Which of `names` (lower-case exe names) are running, from a snapshot no older
    than max_age_s. Sees minimized and background games too."""
    if time.monotonic() - _snapshot_at > max_age_s:
        _refresh(force=True)
    return {n.lower() for n in _exe_by_pid.values()} & names


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return hr == 0 and cloaked.value != 0


def window_rect(hwnd: int) -> Rect | None:
    """Visible window bounds in physical screen pixels (excludes DWM shadow)."""
    r = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
    )
    if hr != 0 and not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return Rect(r.left, r.top, r.right - r.left, r.bottom - r.top)


def foreground_pid() -> int:
    hwnd = user32.GetForegroundWindow()
    return window_pid(hwnd) if hwnd else 0


def foreground_exe() -> str:
    pid = foreground_pid()
    return process_exe_name(pid) if pid else ""


def find_target_windows(
    exe_name: str,
    title_contains: str = "",
    exclude_pid: int | None = None,
    min_size: int = 40,
    exclude_exes: set[str] | None = None,
) -> list[TargetWindow]:
    """Visible, non-minimized top-level windows owned by processes named exe_name,
    largest first. Processes in exclude_exes (games) are never targets."""
    exe_lower = exe_name.lower()
    excluded = exclude_exes or set()
    found: list[TargetWindow] = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = window_pid(hwnd)
        if pid == exclude_pid:
            return True
        rect = window_rect(hwnd)  # cheap checks before the name lookup
        if rect is None or rect.w < min_size or rect.h < min_size:
            return True
        if is_cloaked(hwnd):
            return True
        exe = process_exe_name(pid).lower()
        if exe != exe_lower or exe in excluded:
            return True
        title = window_title(hwnd)
        if title_contains and title_contains not in title:
            return True
        found.append(TargetWindow(int(hwnd), pid, title, rect))
        return True

    user32.EnumWindows(_cb, 0)
    found.sort(key=lambda w: w.rect.w * w.rect.h, reverse=True)
    return found


def make_overlay_window(hwnd: int, exclude_from_capture: bool = True) -> bool:
    """Make a window click-through / non-activating, and hide it from screen capture
    so our own OCR never reads the Korean text we draw. Returns whether the capture
    exclusion was applied (needs Windows 10 2004+)."""
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    if not exclude_from_capture:
        return False
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))


def lower_own_priority() -> bool:
    """Run this process below normal priority so a game always wins the CPU."""
    return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(),
                                          BELOW_NORMAL_PRIORITY_CLASS))

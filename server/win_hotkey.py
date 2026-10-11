"""
Press a keyboard shortcut in LightWave Modeler (Windows only).

LightWave 2020's Modeler does not run commands that arrive on its Command Port,
and modeling commands only work inside a real plug-in call. So the MCP server
triggers the "Claude MCP Modeler" plug-in through a keyboard shortcut the user
assigned to it once: bring Modeler to the front, press the shortcut, and give
focus back to whatever window had it.
"""
from __future__ import annotations

import ctypes
import sys
import time

VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9


def parse_hotkey(spec: str) -> list[int]:
    keys = []
    for part in spec.lower().replace(" ", "").split("+"):
        if part in VK:
            keys.append(VK[part])
        elif len(part) == 1 and part.isalnum():
            keys.append(ord(part.upper()))
        else:
            raise ValueError(f"unknown key {part!r} in hotkey {spec!r}")
    return keys


def _user32():
    if sys.platform != "win32":
        raise OSError("keyboard trigger only works on Windows")
    return ctypes.windll.user32


def find_window(title_prefix: str = "Modeler") -> int:
    """Return the HWND of the first visible top-level window whose title starts with title_prefix."""
    user32 = _user32()
    found = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value.startswith(title_prefix):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(proto(cb), 0)
    return found[0] if found else 0


def _focus(hwnd: int) -> bool:
    user32 = _user32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # A background process may not steal focus; a synthetic Alt tap lifts that lock.
    user32.keybd_event(VK["alt"], 0, 0, 0)
    user32.keybd_event(VK["alt"], 0, KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)
    for _ in range(20):
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.025)
    # Fallback: attach to the foreground thread's input queue.
    kernel32 = ctypes.windll.kernel32
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    me = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(me, fg_tid, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.AttachThreadInput(me, fg_tid, False)
    time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd


def press_in_window(hwnd: int, hotkey: str, restore_focus: bool = True) -> None:
    user32 = _user32()
    keys = parse_hotkey(hotkey)
    previous = user32.GetForegroundWindow()
    if not _focus(hwnd):
        raise OSError("could not bring Modeler to the front")
    time.sleep(0.05)
    for k in keys:
        user32.keybd_event(k, 0, 0, 0)
    time.sleep(0.02)
    for k in reversed(keys):
        user32.keybd_event(k, 0, KEYEVENTF_KEYUP, 0)
    if restore_focus and previous and previous != hwnd:
        # Give Modeler a moment to read the key before handing focus back.
        time.sleep(0.15)
        _focus(previous)

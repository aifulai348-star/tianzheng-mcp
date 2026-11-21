"""Process-scoped ESC recovery; never uses global keyboard injection."""
import ctypes
from ctypes import wintypes as W
import os


class Recovery:
    def __init__(self, pid, hwnd):
        self.pid, self.hwnd = pid, hwnd
        self.u = ctypes.WinDLL('user32', use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.u.IsWindowEnabled.argtypes = [W.HWND]
        self.u.IsWindowVisible.argtypes = [W.HWND]
        self.u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        self.u.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    def windows(self):
        out = []
        def visit(hwnd, _):
            pid = W.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self.pid:
                out.append(int(hwnd))
            return True
        cb = self.callback(visit)
        self.u.EnumWindows(cb, 0)
        return out
    def modal(self):
        # Tianzheng's modeless tool panels also use #32770. A visible dialog
        # alone is not evidence of modality; the owned main window is disabled
        # while a native modal dialog is active.
        return not bool(self.u.IsWindowEnabled(W.HWND(self.hwnd)))
    def escape(self):
        targets = {self.hwnd}
        for hwnd in self.windows():
            name = ctypes.create_unicode_buffer(128)
            self.u.GetClassNameW(W.HWND(hwnd), name, 128)
            if name.value == '#32770' and self.u.IsWindowVisible(W.HWND(hwnd)):
                targets.add(hwnd)
        # Include only the actual focused child, not every toolbar/control.
        class GUIINFO(ctypes.Structure):
            _fields_ = [('cbSize', W.DWORD), ('flags', W.DWORD),
                        ('hwndActive', W.HWND), ('hwndFocus', W.HWND),
                        ('hwndCapture', W.HWND), ('hwndMenuOwner', W.HWND),
                        ('hwndMoveSize', W.HWND), ('hwndCaret', W.HWND), ('rcCaret', W.RECT)]
        info = GUIINFO()
        info.cbSize = ctypes.sizeof(info)
        thread = self.u.GetWindowThreadProcessId(W.HWND(self.hwnd), None)
        if self.u.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.hwndFocus:
            targets.add(int(info.hwndFocus))
        count = 0
        for hwnd in targets:
            # Re-check PID so stale window handles cannot target another app.
            pid = W.DWORD()
            self.u.GetWindowThreadProcessId(W.HWND(hwnd), ctypes.byref(pid))
            if pid.value != self.pid:
                continue
            count += bool(self.u.PostMessageW(W.HWND(hwnd), 0x0100, 0x1B, 0))
            self.u.PostMessageW(W.HWND(hwnd), 0x0101, 0x1B, 0xC0000001)
        return {'posted': count, 'pid': self.pid, 'method': 'targeted_window_escape'}

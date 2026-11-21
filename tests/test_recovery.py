import unittest
from tz_mcp.recovery import Recovery


class Windows:
    def __init__(self, enabled):
        self.enabled = enabled
    def IsWindowEnabled(self, hwnd):
        return self.enabled
    def IsWindowVisible(self, hwnd):
        return True
    def GetClassNameW(self, hwnd, buffer, length):
        buffer.value = '#32770'
        return 6


class RecoveryTests(unittest.TestCase):
    def target(self, enabled):
        r = Recovery.__new__(Recovery)
        r.hwnd = 100
        r.u = Windows(enabled)
        r.windows = lambda: [100, 200]
        return r
    def test_modeless_dialog_does_not_abort_command(self):
        self.assertFalse(self.target(True).modal())
    def test_disabled_main_window_triggers_modal_recovery(self):
        self.assertTrue(self.target(False).modal())

import os
import time
import unittest
from unittest.mock import patch

from tz_mcp import cad


class FakeApp:
    def __init__(self, hwnd):
        self.HWND = hwnd


class ConnectTests(unittest.TestCase):
    def test_get_active_object_is_preferred_and_reports_the_pid(self):
        with patch('win32com.client.GetActiveObject', return_value=FakeApp(100)), \
             patch('tz_mcp.cad.late', lambda app: app), \
             patch('tz_mcp.cad.window_pid', lambda hwnd: 29720):
            app, hwnd, pid = cad.connect('P', 29720)
        self.assertIsNotNone(app)
        self.assertEqual((100, 29720), (hwnd, pid))

    def test_a_foreign_session_fails_closed(self):
        with patch('win32com.client.GetActiveObject', return_value=FakeApp(100)), \
             patch('tz_mcp.cad.late', lambda app: app), \
             patch('tz_mcp.cad.window_pid', lambda hwnd: 17804):
            with self.assertRaises(ValueError):
                cad.connect('P', 29720)

    def test_dispatch_fallback_is_bounded_in_time(self):
        def slow(progid):
            time.sleep(0.4)
            return FakeApp(1)

        with patch('win32com.client.GetActiveObject', side_effect=Exception('no rot')), \
             patch('win32com.client.Dispatch', side_effect=slow):
            with self.assertRaises(RuntimeError):
                cad.connect('P', None, timeout=0.05)

    def test_dispatch_fallback_initializes_com_on_its_own_thread(self):
        calls = []

        class FakePythoncom:
            def CoInitialize(self):
                calls.append('init')

            def CoUninitialize(self):
                calls.append('uninit')

        with patch('win32com.client.GetActiveObject', side_effect=Exception('no rot')), \
             patch('pythoncom.CoInitialize', FakePythoncom().CoInitialize), \
             patch('pythoncom.CoUninitialize', FakePythoncom().CoUninitialize), \
             patch('tz_mcp.cad.late', lambda app: app), \
             patch('win32com.client.Dispatch', return_value=FakeApp(100)):
            app, hwnd, pid = cad.connect('P', None, timeout=2)

        # connect() initializes its own thread, then the dispatch thread does it again
        self.assertEqual(['init', 'init', 'uninit'], calls)
        self.assertEqual(100, hwnd)

    def test_dispatch_fallback_propagates_its_error(self):
        with patch('win32com.client.GetActiveObject', side_effect=Exception('no rot')), \
             patch('win32com.client.Dispatch', side_effect=RuntimeError('class not registered')):
            with self.assertRaises(RuntimeError):
                cad.connect('P', None, timeout=2)


class CadPinTests(unittest.TestCase):
    """TZ_CAD_PID is the only thing standing between a worker and a foreign drawing."""

    def _cad_with(self, env_value, expected_pid=None):
        seen = []

        def fake_connect(progid, expected):
            seen.append(expected)
            return object(), 1, expected or 0

        with patch('tz_mcp.cad.connect', fake_connect):
            previous = os.environ.get('TZ_CAD_PID')
            if env_value is None:
                os.environ.pop('TZ_CAD_PID', None)
            else:
                os.environ['TZ_CAD_PID'] = env_value
            try:
                cad.Cad(expected_pid)
            finally:
                if previous is None:
                    os.environ.pop('TZ_CAD_PID', None)
                else:
                    os.environ['TZ_CAD_PID'] = previous
        return seen

    def test_the_environment_pins_the_instance(self):
        self.assertEqual([29720], self._cad_with('29720'))

    def test_an_explicit_argument_wins_over_the_environment(self):
        self.assertEqual([17804], self._cad_with('29720', 17804))

    def test_no_pin_leaves_the_choice_to_the_rot(self):
        self.assertEqual([None], self._cad_with(None))


if __name__ == '__main__':
    unittest.main()

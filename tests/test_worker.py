import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tz_mcp.worker import WorkerLock, PROJECT

PROD_STATE = PROJECT / '.state'


class FakeStore:
    def recover_interrupted(self):
        return 0

    def has_uncertainty(self):
        return False


def listing():
    return sorted(p.name for p in PROD_STATE.iterdir()) if PROD_STATE.exists() else []


class WorkerTests(unittest.TestCase):
    def setUp(self):
        # A test may never add or remove anything in the production .state:
        # that directory holds the frozen queue, the stop gate and the
        # operator's refusal receipts.
        self.production_before = listing()

    def tearDown(self):
        self.assertEqual(self.production_before, listing(),
                         'test touched the production .state directory')

    def test_second_process_proves_exclusive_lock_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get('TZ_STATE')
            os.environ['TZ_STATE'] = tmp
            try:
                with WorkerLock():
                    env = dict(os.environ)
                    p = subprocess.Popen(
                        [sys.executable, '-X', 'utf8', str(PROJECT / 'bootstrap.py'), 'worker', 'once'],
                        cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    stdout, stderr = p.communicate(timeout=30)
                    self.assertNotEqual(0, p.returncode)
                    receipt = Path(tmp) / f'worker-refusal-{p.pid}.json'
                    self.assertTrue(receipt.exists(), 'A crash alone is not proof of lock refusal')
                    data = json.loads(receipt.read_text(encoding='utf-8'))
                    self.assertEqual('exclusive_queue_lock_held', data['reason'])
                    self.assertEqual(p.pid, data['pid'])
            finally:
                if previous is None:
                    os.environ.pop('TZ_STATE', None)
                else:
                    os.environ['TZ_STATE'] = previous

    def test_several_autocad_windows_means_refusal_not_a_guess(self):
        # Two frames on the desktop: COM answers for the first, which is often
        # the operator's own drawing. The worker must refuse instead of guessing.
        import tz_mcp
        from tz_mcp import cad as cadmod
        from tz_mcp import worker as workermod
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get('TZ_STATE')
            pinned = os.environ.pop('TZ_CAD_PID', None)
            os.environ['TZ_STATE'] = tmp
            original = cadmod.autocad_window_pids
            cadmod.autocad_window_pids = lambda: [17804, 29720]
            try:
                with self.assertRaises(RuntimeError) as caught:
                    workermod.run(FakeStore())
                message = str(caught.exception)
                self.assertIn('Several AutoCAD windows', message)
                self.assertIn('17804', message)
                self.assertIn('TZ_CAD_PID', message)
            finally:
                cadmod.autocad_window_pids = original
                if previous is None:
                    os.environ.pop('TZ_STATE', None)
                else:
                    os.environ['TZ_STATE'] = previous
                if pinned is not None:
                    os.environ['TZ_CAD_PID'] = pinned

    def test_the_only_autocad_window_is_pinned_without_being_asked(self):
        from tz_mcp import cad as cadmod
        from tz_mcp import worker as workermod
        asked = []

        class FakeCad:
            def __init__(self, expected_pid=None):
                asked.append(expected_pid)
                self.pid = 33
                self.hwnd = 7

        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get('TZ_STATE')
            pinned = os.environ.pop('TZ_CAD_PID', None)
            os.environ['TZ_STATE'] = tmp
            original_pids, original_cad = cadmod.autocad_window_pids, cadmod.Cad
            cadmod.autocad_window_pids = lambda: [33]
            cadmod.Cad = FakeCad
            (Path(tmp) / 'stop').touch()
            try:
                workermod.run(FakeStore())
            finally:
                cadmod.autocad_window_pids, cadmod.Cad = original_pids, original_cad
                if previous is None:
                    os.environ.pop('TZ_STATE', None)
                else:
                    os.environ['TZ_STATE'] = previous
                if pinned is not None:
                    os.environ['TZ_CAD_PID'] = pinned
        self.assertEqual([33], asked)

    def test_state_dir_follows_override_and_never_creates_production(self):
        from tz_mcp.worker import state_dir
        self.assertEqual(PROD_STATE, state_dir())
        with tempfile.TemporaryDirectory() as tmp:
            os.environ['TZ_STATE'] = tmp
            try:
                self.assertEqual(Path(tmp), state_dir())
            finally:
                os.environ.pop('TZ_STATE', None)
        self.assertEqual(PROD_STATE, state_dir())

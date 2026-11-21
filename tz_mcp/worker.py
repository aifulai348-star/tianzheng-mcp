import json
import os
from pathlib import Path
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parent.parent


def state_dir():
    """Directory holding queue/stop/lock files.

    Production state lives beside the package (PROJECT/.state). Tests point
    TZ_STATE at a temporary directory so they can never touch a real queue.
    """
    return Path(os.environ.get('TZ_STATE') or (PROJECT / '.state'))


class WorkerLock:
    def __enter__(self):
        import msvcrt
        self.state = state_dir()
        self.state.mkdir(exist_ok=True)
        self.file = open(self.state / 'worker.lock', 'a+b')
        # Reading byte zero before locking fails when another process owns
        # that byte. Query file size without touching the locked range.
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            receipt = self.state / f'worker-refusal-{os.getpid()}.json'
            receipt.write_text(json.dumps({'pid':os.getpid(),'state':'refused_lock',
                                            'reason':'exclusive_queue_lock_held','updated':time.time()}),encoding='utf-8')
            raise RuntimeError('Another worker owns this queue')
        return self
    def __exit__(self, *args):
        import msvcrt
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


def start():
    state = state_dir()
    state.mkdir(exist_ok=True)
    spawned_at = time.time()
    # Every contender must acquire the same OS lock before touching COM.
    with open(state / 'worker.log', 'ab') as log:
        p = subprocess.Popen([sys.executable, '-X', 'utf8', str(PROJECT / 'bootstrap.py'), 'worker', 'run'], cwd=PROJECT,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    return {'spawned_pid': p.pid, 'spawned_at':spawned_at, 'log': str(state / 'worker.log')}


def run(store, once=False):
    from .cad import Cad, autocad_window_pids
    from .engine import Engine
    from .recovery import Recovery
    state = state_dir()
    with WorkerLock():
        store.recover_interrupted()
        if store.has_uncertainty():
            raise RuntimeError('Uncertain prior outcome: inspect and acknowledge before starting worker')
        pinned = os.environ.get('TZ_CAD_PID')
        if not pinned:
            others = autocad_window_pids()
            if len(others) > 1:
                raise RuntimeError('Several AutoCAD windows are running (%s). COM would answer for '
                                   'the first one, which may be a drawing this worker may not touch; '
                                   'set TZ_CAD_PID to the instance it should drive.'
                                   % ', '.join(str(p) for p in others))
            if others:
                pinned = str(others[0])
        cad = Cad(int(pinned) if pinned else None)
        engine = Engine(store, cad, Recovery(cad.pid, cad.hwnd))
        status_file = state / 'worker.json'
        try:
            while True:
                status_file.write_text(json.dumps({'pid': os.getpid(), 'cad_pid': cad.pid,
                                                   'updated': time.time(), 'state': 'ready'}), encoding='utf-8')
                if (state / 'stop').exists():
                    (state / 'stop').unlink()
                    break
                ran = engine.run_one()
                if engine.quarantined or once:
                    break
                if not ran:
                    time.sleep(0.3)
        finally:
            status_file.write_text(json.dumps({'pid': os.getpid(), 'state': 'stopped', 'updated': time.time()}), encoding='utf-8')


def stop():
    """Request a graceful stop at the next job boundary; the worker deletes it."""
    state = state_dir()
    state.mkdir(exist_ok=True)
    (state / 'stop').touch()
    return {'stop_requested': True, 'effective': 'at next step/job boundary', 'state_dir': str(state)}


def status():
    state = state_dir()
    path = state / 'worker.json'
    out = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'state': 'not_started'}
    out['state_dir'] = str(state)
    return out

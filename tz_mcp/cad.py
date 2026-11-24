import ctypes
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .commands import prepare, command_payload

PROGID = 'AutoCAD.Application.24.3'
# Workspace root that document paths must stay inside; the DWG allow-list and
# the cross-job readback root both derive from it. Override for a checkout
# that does not sit under the original layout.
ROOT = Path(os.environ.get('TZ_ROOT', 'D:/CAD绘图'))
# Cross-job readback is deliberately narrower than document.new: only the
# saved test artefacts of this project may be reopened read-only. Keeping the
# root as a module constant lets tests redirect it without touching production.
OUTPUTS = Path(os.environ.get('TZ_OUTPUTS', 'D:/CAD绘图/tianzheng-mcp/outputs'))
# AutoCAD rejects COM calls for a moment while a drawing is created, saved or
# regenerated. View framing happens before any command is sent, so it may wait
# this long for the application to accept the call again.
FRAME_TIMEOUT = 20.0
_OWN_DOCUMENT = object()


def late(obj):
    import win32com.client.dynamic
    return win32com.client.dynamic.DumbDispatch(obj._oleobj_)


def document_identity(doc):
    """Return the live COM identity of a document proxy.

    Add/Open and ActiveDocument may hand back different Python wrappers for the
    same document. QueryInterface(IID_IUnknown) is the identity of the COM
    document itself, so it proves ownership without trusting FullName alone.
    """
    import pythoncom
    return doc._oleobj_.QueryInterface(pythoncom.IID_IUnknown)


def collection_items(collection):
    return [collection.Item(i) for i in range(int(collection.Count))]


class OutcomeUnknown(RuntimeError):
    uncertain = True


class CommandCompletionTimeout(OutcomeUnknown):
    command_completion_timeout = True


class CommandNotSent(RuntimeError):
    """The job stopped before anything was handed to AutoCAD.

    A refused view change or an unreachable member leaves the drawing exactly
    as it was, so this is a plain failure: no human inspection is required and
    replaying the job is safe. It must never be reported as unknown_outcome.
    """


def select_wall(candidates, args, join_tolerance):
    """Select an orthogonal requested segment, excluding split host fragments.

    Tianzheng can extend a joining endpoint to the intersected wall centreline.
    This identifies the segment; it is not a general shape/host verification.
    Multiple matching segments remain uncertain rather than guessing.
    """
    start, end = args['start'], args['end']
    dx, dy = end[0]-start[0], end[1]-start[1]
    if abs(dx) > 1e-6 and abs(dy) > 1e-6:
        if len(candidates) == 1:
            return candidates[0]
        raise OutcomeUnknown('Multiple new oblique walls; segment identification not supported')
    axis = 0 if abs(dx) > 1e-6 else 1
    normal = 1-axis
    positive = end[axis] > start[axis]
    lower_width = 'RightWidth' if (axis == 0 and positive) or (axis == 1 and not positive) else 'LeftWidth'
    low, high = sorted((start[axis], end[axis]))
    tolerances = (join_tolerance, join_tolerance) if isinstance(join_tolerance, (int, float)) else join_tolerance
    matches = []
    for info in candidates:
        if 'bounds' not in info or not {'LeftWidth', 'RightWidth'} <= info.keys():
            continue
        lo, hi = info['bounds']
        centre = lo[normal] + info[lower_width]
        if (abs(centre-start[normal]) <= 0.01
                and low-tolerances[0]-0.01 <= lo[axis] <= low+0.01
                and high-0.01 <= hi[axis] <= high+tolerances[1]+0.01
                and abs(hi[normal]-lo[normal]-info['LeftWidth']-info['RightWidth']) <= 0.01):
            matches.append(info)
    if len(matches) != 1:
        raise OutcomeUnknown(f'Requested wall identification ambiguous: {len(matches)} matches among {len(candidates)} new walls')
    return matches[0]


def select_wall_segments(candidates, args, join_tolerance):
    """Identify the created wall as one segment or as a contiguous chain of split pieces.

    Tianzheng splits a newly drawn wall at every wall it crosses, so one command can leave
    two or more collinear pieces whose union is exactly the requested span. A single-span
    match is preferred; the chain is accepted only when the pieces tile [low, high] without
    a gap, so unrelated fragments still raise.
    """
    try:
        return [select_wall(candidates, args, join_tolerance)]
    except OutcomeUnknown as single_error:
        start, end = args['start'], args['end']
        dx, dy = end[0]-start[0], end[1]-start[1]
        if abs(dx) > 1e-6 and abs(dy) > 1e-6:
            raise
        axis = 0 if abs(dx) > 1e-6 else 1
        normal = 1-axis
        positive = end[axis] > start[axis]
        lower_width = 'RightWidth' if (axis == 0 and positive) or (axis == 1 and not positive) else 'LeftWidth'
        low, high = sorted((start[axis], end[axis]))
        tolerances = (join_tolerance, join_tolerance) if isinstance(join_tolerance, (int, float)) else join_tolerance
        pieces = []
        for info in candidates:
            if 'bounds' not in info or not {'LeftWidth', 'RightWidth'} <= info.keys():
                continue
            lo, hi = info['bounds']
            if abs(lo[normal] + info[lower_width] - start[normal]) > 0.01:
                continue
            if abs(hi[normal]-lo[normal]-info['LeftWidth']-info['RightWidth']) > 0.01:
                continue
            if lo[axis] < low - tolerances[0] - 0.01 or hi[axis] > high + tolerances[1] + 0.01:
                continue
            pieces.append(info)
        pieces.sort(key=lambda info: info['bounds'][0][axis])
        chain, cursor = [], low
        for info in pieces:
            if info['bounds'][0][axis] <= cursor + 2.0:
                chain.append(info)
                cursor = max(cursor, info['bounds'][1][axis])
                if cursor >= high - 0.01:
                    return chain
        raise single_error


# AutoCAD refuses COM calls while it opens, saves or regenerates a drawing.
# RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER mean "not now", not "failed".
BUSY_HRESULTS = (-2147418111, -2147417846)


def retry_busy(action, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except Exception as exc:
            if getattr(exc, 'hresult', None) not in BUSY_HRESULTS or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def retry_document_read(action, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except Exception as exc:
            transient = getattr(exc, 'hresult', None) in BUSY_HRESULTS
            if isinstance(exc, AttributeError):
                transient = any(str(exc).endswith('.' + name) for name in
                                ('ActiveDocument', 'Name', 'FullName', 'ModelSpace', 'Count', 'Item', 'HWND', 'ListArx', 'Version',
                                 'HandleToObject', 'Handle', 'ObjectName', 'ObjectID', 'GetBoundingBox', 'GetVariable',
                                 'SendCommand', 'PostCommand', 'ZoomWindow', 'ZoomExtents',
                                 'Documents', 'Add', 'Open', 'Save', 'SaveAs', 'Close', 'Saved', 'ReadOnly'))
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def com_method(factory, name, timeout=3):
    return retry_document_read(lambda: getattr(factory(), name), timeout=timeout)


def invoke_document(factory, name, *args):
    # Only RPC_E_CALL_REJECTED / RETRYLATER means the server did not accept
    # this invocation. Never retry a timeout or an indeterminate result.
    return retry_busy(lambda: com_method(factory, name)(*args))


def property_lookup(entity, name):
    """Classify one late-bound property name without reading its value.

    win32com's dynamic dispatch swallows a refused GetIDsOfNames
    (RPC_E_CALL_REJECTED) and raises AttributeError('<unknown>.Name'), which
    looks exactly like a property this object kind does not own. Asking
    IDispatch directly separates the two answers.
    """
    try:
        ole = entity._oleobj_
    except Exception:
        return 'unknown'
    try:
        ole.GetIDsOfNames(name)
        return 'present'
    except Exception as exc:
        if getattr(exc, 'hresult', None) in BUSY_HRESULTS:
            return 'rejected'
        return 'absent'


def property_read(entity, name, timeout=2.0):
    """Read one late-bound property, retrying only a refused lookup.

    A name the object kind does not own fails on the first attempt; only a
    refusal is worth waiting for. Swallowing a refusal as "property absent" is
    what made two readbacks of the same file disagree (a wall with HoleArea in
    one read and without it in the next).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return getattr(entity, name)
        except Exception as exc:
            transient = getattr(exc, 'hresult', None) in BUSY_HRESULTS
            if isinstance(exc, AttributeError) and str(exc).endswith('.' + name):
                transient = property_lookup(entity, name) == 'rejected'
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def autocad_window_pids():
    """PIDs of every visible AutoCAD frame on this desktop.

    COM exposes only the first instance through the ROT, so whenever more than
    one frame exists the object we would get back may belong to a drawing we do
    not own. Callers use this to refuse or to pin the intended process.
    """
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        name = ctypes.create_unicode_buffer(64)
        if user32.GetClassNameW(hwnd, name, 64) and name.value.startswith('AfxMDIFrame'):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.add(int(pid.value))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return sorted(found)


def window_pid(hwnd):
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value


def _dispatch_with_timeout(progid, timeout):
    """Dispatch in a bounded thread; a busy first instance must not hang us."""
    import threading
    box = {}

    def work():
        import pythoncom
        try:
            import win32com.client
            # COM is per thread: Dispatch in a bare thread fails with
            # CO_E_NOTINITIALIZED ("尚未调用 CoInitialize") instead of reaching
            # AutoCAD, so initialize this thread before asking for the object.
            pythoncom.CoInitialize()
            box['app'] = win32com.client.Dispatch(progid)
        except Exception as exc:  # surfaced on the caller's thread
            box['error'] = exc
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise RuntimeError('AutoCAD COM did not answer within %.1fs; close other AutoCAD '
                           'instances before retrying' % timeout)
    if 'error' in box:
        raise box['error']
    return box['app']


def connect(progid=PROGID, expected_pid=None, timeout=8.0):
    """Attach to the running AutoCAD and prove which process answered.

    GetActiveObject is tried first because it never starts a new instance.
    Dispatch runs in a bounded thread: with a second AutoCAD running, the ROT
    entry belongs to the first instance, which may be busy with unrelated work.
    A mismatched PID fails closed instead of driving someone else's drawing.
    """
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        app = late(win32com.client.GetActiveObject(progid))
    except Exception:
        app = late(_dispatch_with_timeout(progid, timeout))
    hwnd = int(app.HWND)
    pid = window_pid(hwnd)
    if expected_pid is not None and pid != expected_pid:
        raise ValueError('AutoCAD COM answered from pid %d, expected %d; refusing to drive '
                         'another session' % (pid, expected_pid))
    return app, hwnd, pid


def check_application(app, expected_hwnd, expected_pid, pid_lookup=window_pid):
    hwnd = int(app.HWND)
    if hwnd != expected_hwnd or pid_lookup(hwnd) != expected_pid:
        raise ValueError('Reacquired COM application differs from owned CAD process')
    return app


def require_drawing_window(hwnd, is_iconic=None):
    if is_iconic is None:
        is_iconic = lambda value: ctypes.windll.user32.IsIconic(ctypes.c_void_p(value))
    if is_iconic(hwnd):
        raise ValueError('AutoCAD window is minimized; restore the existing drawing window before native commands; no command sent')


def require_model_space(doc):
    if (int(retry_document_read(lambda: doc.GetVariable('TILEMODE'))) != 1
            and int(retry_document_read(lambda: doc.GetVariable('CVPORT'))) <= 1):
        raise ValueError('Native creation in paper space is unsupported; activate model space before drawing; no command sent')


def guarded_mutation(action):
    try:
        return action()
    except Exception as exc:
        raise OutcomeUnknown(str(exc)) from exc


def safe_path(value):
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT.resolve()) or path.suffix.lower() != '.dwg':
        raise ValueError(f'DWG must be inside {ROOT}')
    return path


def readback_path(value):
    """Validate the narrow subset of saved DWGs a cross-job readback may open."""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('Readback path must be absolute')
    path = safe_path(path)
    outputs = OUTPUTS.resolve()
    if not path.is_relative_to(outputs):
        raise ValueError(f'Readback is restricted to saved test artifacts under {outputs}')
    if not path.is_file():
        raise ValueError('Readback target must be an existing regular DWG file')
    return path


def active_object(progid=PROGID):
    """Attach to the already running AutoCAD; never Dispatch (never start one)."""
    import win32com.client
    return late(win32com.client.GetActiveObject(progid))


class Cad:
    def __init__(self, expected_pid=None):
        # TZ_CAD_PID pins which AutoCAD may be driven: without it, multi-instance
        # COM hands back the first one, which is often an operator's own drawing.
        import os
        if expected_pid is None and os.environ.get('TZ_CAD_PID'):
            expected_pid = int(os.environ['TZ_CAD_PID'])
        self.app, self.hwnd, self.pid = connect(PROGID, expected_pid)
        self.doc = None
        # Original Add/Open proxy. guard() may refresh self.doc to another
        # proxy for the same COM document; ownership stays pinned to this one.
        self.owned_doc = None
        self.document_id = None
        self.path = None
        # Handles whose axis is known from wall.create in this job. A saved
        # wall's DXF 300 blob does not follow its real extent (identical before
        # and after a native split), so only an in-job creation record can
        # prove an axis for wall.split.
        self.created_walls = {}

    def reset_job(self):
        """Forget in-memory ownership at every job boundary.

        The drawing is not closed here: document.close is an explicit step.
        Dropping the reference makes a later job unable to read or write the
        previous job's drawing by accident, even in one resident worker.
        """
        self.doc = None
        self.owned_doc = None
        self.document_id = None
        self.path = None
        self.created_walls = {}

    def diagnose(self):
        return retry_document_read(self._diagnose_once)

    def _diagnose_once(self):
        arx = list(self.app.ListArx())
        count = self._document_count()
        return {'progid': PROGID, 'version': str(self.app.Version), 'pid': self.pid,
                'hwnd': self.hwnd, 'arx': arx,
                'tianzheng_loaded': any('tch' in str(x).lower() for x in arx),
                'document_count': count,
                'active_document': str(self.app.ActiveDocument.Name) if count else None}

    def _document_count(self):
        return int(late(self.app.Documents).Count)

    def guard(self):
        return retry_document_read(self._guard_once)

    def _current_document(self):
        import win32com.client
        # Fresh typed access prevents a weak application proxy from retaining
        # missing-member metadata after a document close/open transition.
        def acquire():
            app = connect(PROGID, self.pid)[0]
            check_application(app, self.hwnd, self.pid)
            doc = late(app.ActiveDocument)
            str(doc.FullName)
            return doc
        return retry_document_read(acquire)

    def _guard_once(self):
        if self.doc is None:
            raise ValueError('No owned active drawing; run document.new first')
        active = self._current_document()
        if Path(active.FullName).resolve() != Path(self.path).resolve():
            raise ValueError('Active drawing differs from owned document')
        self.doc = active
        if int(self.doc.GetVariable('CMDACTIVE')) != 0:
            raise OutcomeUnknown('CAD already executing a command')
        for name, wanted in [('UCSORG', (0,0,0)), ('UCSXDIR', (1,0,0)), ('UCSYDIR', (0,1,0))]:
            if any(abs(a-b) > 1e-8 for a,b in zip(self.doc.GetVariable(name), wanted)):
                raise ValueError('World UCS required')

    def entities(self, document=None):
        import win32com.client.dynamic
        # Generic IAcadEntity wrappers hide Tianzheng's extra IDispatch properties.
        source = self.doc if document is None else document
        return retry_document_read(lambda: {str(e.Handle): late(e)
                                   for e in collection_items(late(source.ModelSpace))})

    def describe(self, entity, document_id=_OWN_DOCUMENT):
        return retry_document_read(lambda: self._describe_once(entity, document_id))

    def _describe_once(self, entity, document_id=_OWN_DOCUMENT):
        data = {'handle': str(entity.Handle), 'object_name': str(entity.ObjectName),
                'object_id': int(entity.ObjectID),
                # A readback description must not hand out (or rely on) a
                # previous job's document_id as a credential.
                'document_id': self.document_id if document_id is _OWN_DOCUMENT else document_id}
        try:
            import win32com.client
            # GetBoundingBox has two output parameters. Use its generated
            # standard CAD signature while keeping custom properties late-bound.
            data['bounds'] = retry_document_read(lambda: [list(x) for x in win32com.client.Dispatch(entity._oleobj_).GetBoundingBox()])
        except Exception as exc:
            data['bounds_error'] = str(exc)
        for key in ('LeftWidth', 'RightWidth', 'Width', 'Height', 'WinSill', 'GetKind', 'GetSubKind',
                    'StairHeight', 'StepNum', 'StepWidth', 'StairWidth','StairWidth2','StepTotalNum',
                    'StepNum1','StepNum2','StepHeight','Deep','SectionShapeText','HoleArea'):
            try:
                value = property_read(entity, key)
                if isinstance(value, (int, float, str)):
                    data[key] = value
            except Exception:
                pass
        return data

    def entity(self, handle, document_id=None):
        return retry_document_read(lambda: self._entity_once(handle, document_id))

    def _entity_once(self, handle, document_id=None):
        if document_id is not None and document_id != self.document_id:
            raise ValueError('Cross-document entity reference')
        import win32com.client.dynamic
        entity = retry_document_read(lambda: late(self.doc.HandleToObject(str(handle))))
        # Reject a stale handle resolving to an object outside ModelSpace.
        if str(entity.Handle) not in self.entities():
            raise ValueError('Entity not in owned ModelSpace')
        return entity

    def run_command(self, payload):
        token = str(uuid.uuid4())
        old_marker = retry_busy(lambda: self.doc.GetVariable('USERS5'))
        # Empty trailing lines are command inputs, not disposable whitespace.
        marked = payload + ('' if payload.endswith(('\n', '\r')) else '\n') + f'(setvar "USERS5" "{token}")\n'
        marked = marked.replace(chr(13) + chr(10), chr(10)).replace(chr(10), chr(13))
        def send():
            require_drawing_window(self.hwnd)
            require_model_space(self.doc)
            return self.doc.SendCommand(marked)
        # AttributeError('<unknown>.SendCommand') means the dispatch lookup was
        # rejected and the command never reached AutoCAD, so a retry is safe.
        retry_document_read(send, timeout=20)
        stable = 0
        for _ in range(300):
            try:
                if self.doc.GetVariable('USERS5') == token and int(self.doc.GetVariable('CMDACTIVE')) == 0:
                    stable += 1
                    if stable >= 2:
                        retry_busy(lambda: self.doc.SetVariable('USERS5', old_marker))
                        return
                else:
                    stable = 0
            except Exception:
                stable = 0
            time.sleep(0.1)
        raise CommandCompletionTimeout('Command completion not confirmed')

    def frame_wall(self, args):
        # T20's own joining/search behaviour is view-dependent even with
        # OSMODE=0 and _non. Use a local view before feeding exact coordinates.
        #
        # app is a DumbDispatch, so every member goes through a live
        # GetIDsOfNames. win32com's dynamic lookup swallows a rejected call
        # (RPC_E_CALL_REJECTED, measured 2026-11-24 within milliseconds of
        # Documents.Add) and reports it as AttributeError <unknown>.ZoomWindow
        # instead of com_error, which is why retry_busy alone never saw it.
        # Resolve the member again on every attempt, and only give up once
        # AutoCAD has refused for the whole window: nothing has been sent by
        # then, so the job failed rather than becoming uncertain.
        import pythoncom
        from win32com.client import VARIANT
        lo = [min(args['start'][i], args['end'][i])-500 for i in (0,1)] + [0]
        hi = [max(args['start'][i], args['end'][i])+500 for i in (0,1)] + [0]
        try:
            zoom = com_method(lambda: self.app, 'ZoomWindow', timeout=FRAME_TIMEOUT)
            retry_busy(lambda: zoom(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, lo),
                                    VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, hi)),
                       timeout=FRAME_TIMEOUT)
        except Exception as exc:
            raise CommandNotSent(f'View framing failed before any command was sent: {exc}') from exc

    def _readback_application(self):
        # GetActiveObject never starts AutoCAD. Re-checking the frame HWND and
        # its process id proves the instance is still the one this worker was
        # bound to before any document is enumerated or opened.
        def acquire():
            return check_application(active_object(), self.hwnd, self.pid,
                                     pid_lookup=window_pid)
        return retry_document_read(acquire)

    def _close_owned(self):
        if (self.doc is None or self.owned_doc is None
                or self.path is None or not self.document_id):
            raise ValueError('No owned drawing to close; run document.new in this job first')
        owned = Path(self.path).resolve()
        active = self._current_document()
        if Path(str(active.FullName)).resolve() != owned:
            raise ValueError('Active drawing differs from owned document; close refused')
        try:
            active_identity = document_identity(active)
            owned_identity = document_identity(self.owned_doc)
        except Exception as exc:
            # Same FullName is not enough: without a COM identity we cannot
            # prove this is the document this job originally opened.
            raise ValueError('Active or owned drawing identity could not be read; '
                             'close refused') from exc
        if active_identity != owned_identity:
            raise ValueError('Active drawing is a different document at the same path; '
                             'close refused')
        if not owned.is_file():
            raise ValueError('Owned drawing is not saved on disk; refusing to close')
        if not bool(retry_document_read(lambda: active.Saved)):
            raise ValueError('Owned drawing has unsaved changes; refusing to close')
        document_id = self.document_id
        try:
            invoke_document(lambda: active, 'Close', False)
        except Exception as exc:
            # Close may or may not have taken effect. Retaining ownership keeps
            # the uncertainty visible; callers must inspect instead of retrying.
            raise OutcomeUnknown(
                f'Document close outcome is unknown; ownership retained: {exc}') from exc
        self.doc = None
        self.owned_doc = None
        self.path = None
        self.document_id = None
        return {'closed': True, 'path': str(owned), 'document_id': document_id}

    @contextmanager
    def _read_only_document(self, path_value, label):
        """Open one saved DWG read-only and prove it is the one we may close.

        document.readback and document.list share this: the path is restricted
        to saved test artefacts, the driving instance is re-proven by HWND/PID,
        and only a document whose FullName and ReadOnly flag were verified is
        closed. Anything unproven stays unknown instead of being guessed.
        """
        path = readback_path(path_value)
        app = self._readback_application()
        documents = retry_document_read(lambda: late(app.Documents))
        existing = retry_document_read(lambda: collection_items(documents))
        for document in existing:
            try:
                existing_path = Path(str(retry_document_read(
                    lambda: document.FullName))).resolve()
            except Exception as exc:
                # If an open document's identity cannot be read we cannot
                # prove the target is not already open; fail closed before
                # opening or touching anything.
                raise ValueError(
                    'Could not prove the target is not already open; '
                    f'{label} refused: {exc}') from exc
            if existing_path == path:
                raise ValueError(f'Target drawing is already open; {label} will not '
                                 'take over an existing document')
        try:
            opened = late(invoke_document(lambda: documents, 'Open', str(path), True))
        except Exception as exc:
            # The call may have produced a document before returning an error;
            # there is nothing we can safely close, so the job stays unknown.
            raise OutcomeUnknown(f'Read-only open outcome is unknown: {exc}') from exc
        verified = False
        try:
            try:
                full_name = Path(str(retry_document_read(lambda: opened.FullName))).resolve()
                read_only = bool(retry_document_read(lambda: opened.ReadOnly))
            except Exception as exc:
                raise OutcomeUnknown(
                    f'Opened document identity could not be verified: {exc}') from exc
            if full_name != path or read_only is not True:
                # The returned document is not proven to be ours with the
                # read-only flag; do not guess which document to close.
                raise OutcomeUnknown(
                    'Opened document FullName/ReadOnly does not match the readback request')
            verified = True
            yield path, opened
        finally:
            if verified:
                try:
                    invoke_document(lambda: opened, 'Close', False)
                except Exception as exc:
                    raise OutcomeUnknown(
                        f'Readback document close outcome is unknown: {exc}') from exc

    def _readback(self, path_value, handles):
        with self._read_only_document(path_value, 'readback') as (path, opened):
            # ModelSpace of the document returned by Open, never ActiveDocument.
            entities = self.entities(opened)
            by_handle = {str(handle).upper(): entity for handle, entity in entities.items()}
            descriptions = []
            for handle in handles:
                entity = by_handle.get(str(handle).upper())
                if entity is None:
                    raise ValueError(
                        f'Requested handle not present in opened ModelSpace: {handle}')
                # No document_id credential: this readback is self-contained.
                descriptions.append(self.describe(entity, document_id=None))
            return {'path': str(path),
                    'handles': [item['handle'] for item in descriptions],
                    'entities': descriptions}

    def _list(self, path_value, limit, after_handle=None):
        with self._read_only_document(path_value, 'document.list') as (path, opened):
            # Every Handle is read once because that is what orders the page;
            # the costly per-object properties are read only for this page.
            entities = self.entities(opened)
            rows = sorted((int(handle, 16), handle) for handle in entities)
            if after_handle is not None:
                cursor = int(after_handle, 16)
                rows = [row for row in rows if row[0] > cursor]
            page = rows[:limit]
            following = len(rows) > limit
            descriptions = [self.describe(entities[handle], document_id=None)
                            for _, handle in page]
            return {'path': str(path),
                    'entities': descriptions,
                    'matched_count': len(entities),
                    'next_after_handle': page[-1][1] if following else None}

    def execute(self, op, args):
        args = prepare(op, args)
        # Parts whose interaction is panel/click driven live in parts.py; the
        # generic branches below only cover command-line constructs.
        if op in ('wall.split','opening.create','opening.move','slab.create','column.create','column.move','stair.rect_create','annotation.text','polyline.create'):
            from .parts import execute_part
            return execute_part(self,op,args)
        if op == 'document.new':
            path = safe_path(args['path'])
            if path.exists():
                raise ValueError('Refusing to overwrite existing DWG')
            if not self.diagnose()['tianzheng_loaded']:
                raise ValueError('Tianzheng ARX not detected')
            path.parent.mkdir(parents=True, exist_ok=True)
            def create():
                docs = late(self.app.Documents)
                count_before = retry_document_read(lambda: int(late(self.app.Documents).Count))
                created = invoke_document(lambda: late(self.app.Documents), 'Add', 'D:/天正/sys24x64/ACAD.DWT')
                # Add may initially return a weak proxy named 'Add'. Reacquire
                # the active document rather than retrying the creation.
                self.doc = late(created)
                count_after = retry_document_read(lambda: int(late(self.app.Documents).Count))
                if count_after != count_before + 1:
                    raise OutcomeUnknown('Document creation count mismatch')
                self.document_id, self.path = str(uuid.uuid4()), path
                invoke_document(lambda: self.doc, 'SaveAs', str(path))
                self.owned_doc = self.doc
            guarded_mutation(create)
            return {'document_id': self.document_id, 'path': str(path)}
        if op == 'document.close':
            return self._close_owned()
        if op == 'document.readback':
            return self._readback(args['path'], args['handles'])
        if op == 'document.list':
            return self._list(args['path'], args['limit'], args.get('after_handle'))
        self.guard()
        if op == 'wall.find':
            matches=[]
            for e in self.entities().values():
                if e.ObjectName!='TDbWall':continue
                info=self.describe(e)
                if 'bounds' in info:
                    lo,hi=info['bounds']
                    if all(lo[i]-.01<=args['point'][i]<=hi[i]+.01 for i in (0,1)):
                        matches.append(info)
            if len(matches)!=1:raise ValueError(f'Wall query must identify exactly one current wall; got {len(matches)}')
            return {'document_id':self.document_id,'handles':[matches[0]['handle']],'entities':matches}
        if op == 'document.save':
            guarded_mutation(lambda: invoke_document(lambda: self.doc, 'Save'))
            return {'path': str(self.path), 'document_id': self.document_id}
        if op == 'document.reopen':
            def reopen():
                invoke_document(lambda: self.doc, 'Save')
                invoke_document(lambda: self.doc, 'Close', False)
                import win32com.client
                # Closing invalidates weak document collection proxies on this
                # build; reacquire the application and use its typed collection.
                self.app = retry_busy(lambda: connect(PROGID, self.pid)[0])
                retry_document_read(lambda: check_application(self.app, self.hwnd, self.pid))
                opened = late(invoke_document(
                    lambda: late(self.app.Documents), 'Open', str(self.path)))
                self.doc = opened
                self.owned_doc = opened
            guarded_mutation(reopen)
            return {'path': str(self.path), 'document_id': self.document_id,
                    'entities': [self.describe(e) for e in self.entities().values()]}
        if op == 'entity.get':
            return self.describe(self.entity(**args))
        if op == 'entity.verify':
            def verify():
                entities = [self.entity(h, args.get('document_id')) for h in args['handles']]
                if any(e.ObjectName != args['object_name'] for e in entities):
                    raise ValueError('Object type mismatch')
                return {'entities': [self.describe(e) for e in entities]}
            return retry_document_read(verify)
        require_drawing_window(self.hwnd)
        require_model_space(self.doc)
        before = self.entities()
        join_tolerance = [0, 0]
        if op == 'wall.create':
            axis = 0 if abs(args['end'][0]-args['start'][0]) > 1e-6 else 1
            endpoints = sorted((args['start'], args['end']), key=lambda p:p[axis])
            for e in before.values():
                if e.ObjectName != 'TDbWall':
                    continue
                info = self.describe(e)
                if 'bounds' not in info:
                    continue
                lo, hi = info['bounds']
                for i, p in enumerate(endpoints):
                    if all(lo[k]-0.01 <= p[k] <= hi[k]+0.01 for k in (0,1)):
                        join_tolerance[i] = max(join_tolerance[i], info['LeftWidth'], info['RightWidth'])
        expected = {'wall.create': 'TDbWall',
                    'stair.create': 'TDbLineStair', 'roof.create': 'TDbSlopeRoof'}[op]
        try:
            if op == 'roof.create':
                import pythoncom
                from win32com.client import VARIANT
                flat = [v for p in args['boundary'] for v in p[:2]]
                boundary = self.doc.ModelSpace.AddLightWeightPolyline(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat))
                boundary.Closed = True
                h = str(boundary.Handle)
                self.run_command(command_payload(op, args, boundary_handle=h))
            else:
                if op == 'wall.create':
                    self.frame_wall(args)
                self.run_command(command_payload(op, args))
            after = self.entities()
            if op == 'roof.create' and h in after:
                raise OutcomeUnknown('Roof boundary was not consumed; selection outcome requires inspection')
            new = [e for h,e in after.items() if h not in before and e.ObjectName == expected]
            effects = {}
            if op == 'wall.create':
                infos = [self.describe(e) for e in new]
                selected = select_wall_segments(infos, args, join_tolerance)
                chosen = [after[item['handle']] for item in selected]
                selected_handles = {item['handle'] for item in selected}
                effects = {'additional_wall_handles': [i['handle'] for i in infos if i['handle'] not in selected_handles],
                           'removed_handles': sorted(set(before)-set(after))}
            elif len(new) != 1:
                raise OutcomeUnknown(f'Expected one new {expected}, observed {len(new)}; inspect drawing')
            else:
                chosen = [new[0]]
            properties = {'left_width':'LeftWidth', 'right_width':'RightWidth'} if op == 'wall.create' else (
                {'height':'StairHeight', 'steps':'StepNum', 'step_width':'StepWidth', 'width':'StairWidth'})
            for entity in chosen:
                for key, prop in properties.items():
                    if key in args:
                        setattr(entity, prop, args[key])
                        if not math.isclose(float(getattr(entity, prop)), args[key], abs_tol=1e-6):
                            raise OutcomeUnknown(f'{prop} readback mismatch')
            infos_out = [self.describe(entity) for entity in chosen]
            if op == 'wall.create':
                for info in infos_out:
                    self.created_walls[str(info['handle']).upper()] = {
                        'start': list(args['start']), 'end': list(args['end']),
                        'document_id': self.document_id}
            return {'success': True, 'document_id': self.document_id, 'handles': [info['handle'] for info in infos_out],
                    'entities': infos_out, 'effects': effects, 'validation': {'type': True,
                    'requested_properties': 'unverified: slope/overhang COM readback unavailable' if op == 'roof.create' else True,
                    'host_association': 'not_applicable',
                    'geometry': 'unverified', 'reopen': 'unverified'}}
        except Exception as exc:
            if isinstance(exc, (OutcomeUnknown, CommandNotSent)):
                raise
            raise OutcomeUnknown(str(exc)) from exc

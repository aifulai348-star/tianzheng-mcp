"""Tianzheng parameter panels whose confirmation is a drawing-area click.

Live evidence (2026-11-19, T20 V9 / AutoCAD 2024, docs/LIVE-PROBE-20261118.md):
- TGColumn opens a modeless #32770 panel whose rows are themselves titled
  dialogs ("横向"/"纵向"/"柱高") holding one Edit each.
- Keyboard-edited values take effect: 700/500/3200 produced a real TDbColumn
  whose DXF section was 700 wide, 500 deep and (149 . 3200).
- Exactly one click in the drawing view creates one column; a second click
  creates a second one. Clicks without mouse motion are dropped by the pick
  handler, so every synthetic click moves first.
- ESC closes the panel and returns the command line to idle.
"""
import ctypes
import math
import re
import time
from contextlib import contextmanager
from ctypes import wintypes as W
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


class PanelTimeout(RuntimeError):
    pass


PANEL_CLASS = '#32770'
FIELDS = ('横向', '纵向', '柱高')


def _u():
    return ctypes.WinDLL('user32', use_last_error=True)


def window_pid(u, hwnd):
    pid = W.DWORD()
    u.GetWindowThreadProcessId(W.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def class_name(u, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(W.HWND(hwnd), buf, 256)
    return buf.value


def title(u, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(W.HWND(hwnd), buf, 256)
    return buf.value


def text_of(u, hwnd):
    """Read a control's text across processes (GetWindowText cannot)."""
    buf = ctypes.create_unicode_buffer(128)
    u.SendMessageW(W.HWND(hwnd), 0x000D, 128, buf)
    return buf.value


def rect(u, hwnd):
    r = W.RECT()
    u.GetWindowRect(W.HWND(hwnd), ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def visible(u, hwnd):
    return bool(u.IsWindowVisible(W.HWND(hwnd)))


def windows(u, pid):
    out = []
    cb = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    def visit(h, _):
        if window_pid(u, h) == pid:
            out.append(int(h))
        return True

    u.EnumWindows(cb(visit), 0)
    return out


def children(u, hwnd):
    out = []
    cb = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    def visit(h, _):
        out.append(int(h))
        return True

    u.EnumChildWindows(W.HWND(hwnd), cb(visit), 0)
    return out


def panels(u, pid):
    """Visible modeless parameter panels owned by the CAD process."""
    return [h for h in windows(u, pid)
            if class_name(u, h) == PANEL_CLASS and visible(u, h)]


def field_edit(u, panel, label):
    """The Edit box of the row titled label; tianzheng titles the row dialog."""
    # The panel keeps one copy of each row per tab; hidden copies must be
    # ignored or the value is written into an invisible edit and lost.
    for child in children(u, panel):
        if class_name(u, child) != PANEL_CLASS or title(u, child) != label:
            continue
        if not visible(u, child):
            continue
        edits = [i for i in children(u, child) if class_name(u, i) == 'Edit']
        shown = [i for i in edits if visible(u, i)]
        if shown:
            return shown[0]
    return None


def set_field(backend, hwnd, value):
    """Focus the edit, replace its content, and prove the new text stuck."""
    u = backend.u
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if backend.focus_control(hwnd):
            break
        time.sleep(0.1)
    else:
        raise PanelTimeout('Panel field could not take keyboard focus')
    backend.select_all()
    for ch in str(value):
        backend.unicode(ch)
    got = text_of(u, hwnd).strip()
    if got != str(value):
        raise PanelTimeout('Panel field readback mismatch: wanted %s, got %r' % (value, got))
    return got


def viewport(u, main_hwnd, pid):
    """Screen rectangle of the MDI drawing area, in physical pixels."""
    best = None
    for h in children(u, main_hwnd):
        if class_name(u, h) != 'MDIClient' or window_pid(u, h) != pid or not visible(u, h):
            continue
        left, top, right, bottom = rect(u, h)
        area = (right - left) * (bottom - top)
        if best is None or area > best[0]:
            best = (area, (left, top, right, bottom))
    if best is None:
        raise PanelTimeout('No visible MDIClient drawing area found')
    return best[1]


def world_to_screen(centre, view_size, view_rect, point):
    left, top, right, bottom = view_rect
    if bottom - top <= 0 or right - left <= 0:
        raise PanelTimeout('Degenerate drawing viewport')
    scale = float(view_size) / float(bottom - top)
    x = (left + right) / 2.0 + (point[0] - centre[0]) / scale
    y = (top + bottom) / 2.0 - (point[1] - centre[1]) / scale
    if not (left <= x <= right and top <= y <= bottom):
        raise PanelTimeout('Column point is outside the visible drawing area')
    return x, y


def screen_to_world(centre, view_size, view_rect, x, y):
    left, top, right, bottom = view_rect
    scale = float(view_size) / float(bottom - top)
    return (centre[0] + (x - (left + right) / 2.0) * scale,
            centre[1] - (y - (top + bottom) / 2.0) * scale)


def _wait(predicate, timeout, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


AXIS_DIR_IDS = (178, 179, 180, 181)
AXIS_DIR_DOWN = 179
AXIS_DIR_LEFT = 180
AXIS_CLEAR_ID = 183
AXIS_ANGLE_ID = 186
AXIS_QUICK_ID = 187
SINGLE_AXIS_STRAIGHT_ID = 1328
SINGLE_AXIS_OVERHANG_ID = 1331
AXIS_DIM_SIDE_IDS = {'double': 137, 'single': 136, 'opposite': 142}
AXIS_DIM_START_LABEL_ID = 141
_AXIS_TOTAL_LABELS = ('总开间:', '总进深:')


def _axis_panel(u, pid):
    required = dict.fromkeys((*AXIS_DIR_IDS, AXIS_CLEAR_ID), 'Button')
    required.update({AXIS_ANGLE_ID: 'Edit', AXIS_QUICK_ID: 'Edit'})
    return _find_axis_panel(u, pid, required)


def _find_axis_panel(u, pid, required, caption=None):
    for hwnd in panels(u, pid):
        if caption is not None and title(u, hwnd) != caption:
            continue
        controls = {}
        for child in children(u, hwnd):
            control_id = u.GetDlgCtrlID(W.HWND(child))
            if control_id in required and class_name(u, child) == required[control_id]:
                if visible(u, child):
                    controls[control_id] = child
        if required.keys() <= controls.keys():
            return hwnd, controls
    return None


def axis_panel_totals(u, hwnd):
    """Read the numeric Static immediately to the right of each summary label."""
    rows = [(text_of(u, h).strip(), rect(u, h)) for h in children(u, hwnd)
            if visible(u, h) and class_name(u, h) == 'Static']
    totals = {}
    for label in _AXIS_TOTAL_LABELS:
        position = next((bounds for text, bounds in rows if text == label), None)
        if position:
            _, top, right, _ = position
            for text, (left, row_top, _, _) in rows:
                if abs(left - right - 6) >= 20 or abs(row_top - top) >= 6:
                    continue
                try:
                    value = float(text)
                except ValueError:
                    continue
                if math.isfinite(value):
                    totals[label] = value
                    break
        if label not in totals:
            raise PanelTimeout(f'TRectAxis summary {label} could not be read')
    return totals


def _axis_viewport(u, frame, pid):
    candidates = []
    for mdi in children(u, frame):
        if class_name(u, mdi) != 'MDIClient' or not visible(u, mdi):
            continue
        for child in children(u, mdi):
            if 'DXGI' not in class_name(u, child) or not visible(u, child):
                continue
            left, top, right, bottom = rect(u, child)
            width, height = right - left, bottom - top
            if min(width, height) > 300 and window_pid(u, child) == pid:
                candidates.append((width * height, child))
    if not candidates:
        raise PanelTimeout('No visible DXGI drawing view found inside MDIClient')
    return max(candidates)[1]


def _axis_foreground(backend, pid, hwnd, focus=None, timeout=3.0, *, allow_child_focus=False):
    u = backend.u
    if window_pid(u, hwnd) != pid:
        raise PanelTimeout('CAD process identity changed during axis command')
    is_panel = class_name(u, hwnd) == PANEL_CLASS
    deadline = time.monotonic() + timeout
    current = backend.k.GetCurrentThreadId()
    target = u.GetWindowThreadProcessId(W.HWND(focus or hwnd), None)
    bring_to_top = getattr(u, 'BringWindowToTop', None)
    attempts = 0
    while True:
        fg = u.GetForegroundWindow()
        cad_foreground = fg and window_pid(u, fg) == pid
        if cad_foreground:
            window_ready = fg == hwnd or (is_panel and attempts >= 2)
            if focus is None and window_ready:
                return
            if focus is not None and (window_ready or allow_child_focus):
                keyboard = backend.focus_handle(focus)
                if window_ready and (keyboard == focus or (
                        allow_child_focus and _axis_focus_is_child(u, hwnd, keyboard))):
                    return
                if (allow_child_focus and keyboard and window_pid(u, keyboard) == pid
                        and _axis_focus_owned_wpf(u, hwnd, keyboard, pid)):
                    return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PanelTimeout('Axis command could not bring the CAD window to the foreground')

        # Click once, and only if the CAD panel actually owns the point.
        if attempts == 2 and is_panel and focus is None:
            window_at = getattr(u, 'WindowFromPoint', None)
            if window_at is not None:
                window_at.restype = W.HWND
                left, top, right, bottom = rect(u, hwnd)
                point = W.POINT((left + right) // 2, top + 8)
                if (right > left and point.y < bottom
                        and window_at(point) == hwnd and window_pid(u, hwnd) == pid):
                    backend.click(point.x, point.y)
                    attempts += 1
                    continue

        other = u.GetWindowThreadProcessId(W.HWND(fg), None) if fg else 0
        attached = []
        try:
            for thread in {target, other} - {0, current}:
                if u.AttachThreadInput(current, thread, True):
                    attached.append(thread)
            if bring_to_top is not None:
                bring_to_top(W.HWND(hwnd))
            u.SetForegroundWindow(W.HWND(hwnd))
            if focus is not None:
                u.SetFocus(W.HWND(focus))
        finally:
            for thread in attached:
                u.AttachThreadInput(current, thread, False)
        attempts += 1
        time.sleep(min(0.15, remaining))


def _axis_focus_is_child(u, frame, focus):
    return bool(frame and focus and u.IsChild(W.HWND(frame), W.HWND(focus)))


def _axis_focus_owned_wpf(u, frame, focus, pid):
    """Verify that the focused WPF window belongs to the CAD frame."""
    if not frame or not focus or not pid:
        return False
    get_owner = getattr(u, 'GetWindow', None)
    if get_owner is None:
        return False
    get_ancestor = getattr(u, 'GetAncestor', None)
    for function in (get_ancestor, get_owner):
        if isinstance(function, ctypes._CFuncPtr):
            function.argtypes = [W.HWND, W.UINT]
            function.restype = W.HWND

    root = int(focus)
    if get_ancestor is not None:
        root = int(get_ancestor(W.HWND(focus), 2) or 0) or root  # GA_ROOT
    if window_pid(u, root) != pid or not class_name(u, root).startswith('HwndWrapper['):
        return False

    frame = int(frame)
    current = root
    for _ in range(8):
        current = int(get_owner(W.HWND(current), 4) or 0)  # GW_OWNER
        if not current:
            return False
        if current == frame:
            return True
    return False


def _axis_type(backend, target, foreground, text, commit, replace=False, *,
               allow_child_focus=False, stage='axis_type'):
    u = backend.u

    def check_focus(idx):
        fg = u.GetForegroundWindow()
        fg_pid = window_pid(u, fg) if fg else 0
        pid = window_pid(u, foreground) if foreground else 0
        cur = backend.focus_handle(foreground if allow_child_focus else target)
        is_child = _axis_focus_is_child(u, foreground, cur)
        is_owned_wpf = allow_child_focus and _axis_focus_owned_wpf(u, foreground, cur, pid)
        focus_ok = cur and cur == target
        if allow_child_focus:
            focus_ok = (cur and window_pid(u, cur) == pid
                        and (focus_ok or cur == foreground or is_child or is_owned_wpf))
        if not fg or not pid or fg_pid != pid or not focus_ok:
            raise PanelTimeout(
                f'Axis command keyboard focus lost (stage={stage}, char_idx={idx}, '
                f'fg={int(fg or 0):#x}, fg_pid={fg_pid}, expected_pid={pid}, '
                f'target={int(target or 0):#x}, actual_focus={int(cur or 0):#x}, '
                f'allow_child_focus={allow_child_focus}, is_child={is_child}, '
                f'is_owned_wpf={is_owned_wpf})')

    check_focus('pre')
    if replace:
        backend.select_all()
    for idx, char in enumerate(text):
        check_focus(idx)
        backend.unicode(char)
    check_focus('commit')
    commit()


def _axis_click(backend, pid, panel, control):
    u = backend.u
    _axis_foreground(backend, pid, panel)
    if not visible(u, control):
        raise PanelTimeout('Axis panel button is no longer visible')
    left, top, right, bottom = rect(u, control)
    backend.click((left + right) // 2, (top + bottom) // 2)
    time.sleep(0.55)


def _axis_edit(backend, pid, panel, control, text, commit):
    u = backend.u
    for _ in range(2):
        _axis_foreground(backend, pid, panel)
        if not backend.focus_control(control):
            raise PanelTimeout('Axis panel edit could not take keyboard focus')
        fg = u.GetForegroundWindow()
        if fg and window_pid(u, fg) == pid:
            break
    else:
        raise PanelTimeout('Axis panel edit could not keep CAD in the foreground after retry')
    _axis_type(backend, control, panel, text, commit, replace=True,
               allow_child_focus=False, stage='axis_edit')


def _focus_axis_view(c, backend):
    u = backend.u
    view = _axis_viewport(u, c.hwnd, c.pid)
    _axis_foreground(backend, c.pid, c.hwnd, focus=view, allow_child_focus=True)
    # AutoCAD may give keyboard focus to a frame child or the floating command line.
    keyboard = backend.focus_handle(c.hwnd)
    if (keyboard != view
            and not _axis_focus_is_child(u, c.hwnd, keyboard)
            and not (keyboard and window_pid(u, keyboard) == c.pid
                     and _axis_focus_owned_wpf(u, c.hwnd, keyboard, c.pid))):
        raise PanelTimeout('No keyboard focus inside the CAD drawing frame')
    return view, keyboard


@contextmanager
def _axis_point_panel(c, backend, dispatch, command, required, caption, timeout):
    from .cad import retry_busy, retry_document_read

    def find_panel():
        return _find_axis_panel(backend.u, c.pid, required, caption)

    old_osmode = retry_document_read(lambda: c.doc.GetVariable('OSMODE'))
    try:
        retry_busy(lambda: c.doc.SetVariable('OSMODE', 0))
        found = None
        try:
            retry_document_read(dispatch)
            found = _wait(find_panel, timeout)
            if found is None:
                raise PanelTimeout(f'{command} panel did not appear')
            time.sleep(0.25)
            yield found
        finally:
            _axis_foreground(backend, c.pid, c.hwnd)
            backend.esc(2)

            def panel_closed():
                if found is not None:
                    return found[0] not in panels(backend.u, c.pid)
                return find_panel() is None

            try:
                if _wait(panel_closed, timeout) is None:
                    raise PanelTimeout(f'{command} panel did not close after Escape')
            finally:
                wait_command_idle(c)
    finally:
        retry_busy(lambda: c.doc.SetVariable('OSMODE', old_osmode))


def _type_axis_points(c, backend, points, delays):
    view, keyboard = _focus_axis_view(c, backend)
    u = backend.u
    coordinates = [f'{x:.4f},{y:.4f}' for x, y, _ in points]
    for i, (coordinate, delay) in enumerate(zip(coordinates, delays)):
        if i > 0:
            fg = u.GetForegroundWindow()
            cur = backend.focus_handle(c.hwnd)
            if (not fg or window_pid(u, fg) != c.pid
                    or not cur or window_pid(u, cur) != c.pid
                    or not (cur == c.hwnd or _axis_focus_is_child(u, c.hwnd, cur)
                            or _axis_focus_owned_wpf(u, c.hwnd, cur, c.pid))):
                view, keyboard = _focus_axis_view(c, backend)
        _axis_type(backend, keyboard, c.hwnd, coordinate, backend.enter,
                   allow_child_focus=True, stage=f'axis_point[{i}]')
        time.sleep(delay)
    return {'view_hwnd': view, 'keyboard_hwnd': keyboard, 'coordinate_texts': coordinates}


def create_single_axis(c, args, backend=None, timeout=15.0):
    """Post TSingleAxis asynchronously and enter the two exact plan coordinates."""
    from .input_bridge import NativeInput

    backend = backend if backend is not None else NativeInput()
    required = {SINGLE_AXIS_STRAIGHT_ID: 'Button', SINGLE_AXIS_OVERHANG_ID: 'Edit'}
    with _axis_point_panel(c, backend, lambda: c.doc.PostCommand('TSingleAxis\r'),
                           'TSingleAxis', required, '单轴绘制', timeout) as (hwnd, controls):
        _axis_click(backend, c.pid, hwnd, controls[SINGLE_AXIS_STRAIGHT_ID])
        overhang = f"{args['overhang']:g}"
        _axis_edit(backend, c.pid, hwnd, controls[SINGLE_AXIS_OVERHANG_ID], overhang,
                   lambda: backend.send(0x09, 0, 0))
        time.sleep(0.6)
        placement = _type_axis_points(c, backend, (args['start'], args['end']), (0.6, 0.8))
    return {'hwnd': hwnd, 'overhang_text': overhang, **placement}


def annotate_axis(c, args, backend=None, timeout=15.0):
    """Set the annotation mode and label, then pick both axes by keyboard."""
    from .input_bridge import NativeInput

    backend = backend if backend is not None else NativeInput()
    required = dict.fromkeys(AXIS_DIM_SIDE_IDS.values(), 'Button')
    required[AXIS_DIM_START_LABEL_ID] = 'Edit'
    with _axis_point_panel(c, backend, lambda: c.doc.SendCommand('TMultAxisDim\r'),
                           'TMultAxisDim', required, '', timeout) as (hwnd, controls):
        side_id = AXIS_DIM_SIDE_IDS[args['side']]
        _axis_click(backend, c.pid, hwnd, controls[side_id])
        label_control = controls[AXIS_DIM_START_LABEL_ID]
        _axis_edit(backend, c.pid, hwnd, label_control, args['start_label'],
                   lambda: backend.send(0x09, 0, 0))
        time.sleep(0.6)
        label = text_of(backend.u, label_control).strip()
        if label != args['start_label']:
            raise PanelTimeout(f"TMultAxisDim label mismatch: wanted {args['start_label']!r}, got {label!r}")
        placement = _type_axis_points(c, backend, (args['start_point'], args['end_point']), (0.7, 0.7))
        _axis_type(backend, placement['keyboard_hwnd'], c.hwnd, '', backend.enter,
                   allow_child_focus=True, stage='annotate_confirm')
        time.sleep(1.0)
    return {'hwnd': hwnd, 'side_control_id': side_id, 'start_label': label, **placement}


def create_rect_axis(c, args, backend=None, timeout=15.0):
    """Drive TRectAxis and return the observed panel values and placement input."""
    from .cad import retry_busy, retry_document_read
    from .input_bridge import NativeInput

    backend = backend if backend is not None else NativeInput()
    u = backend.u
    old_osmode = retry_document_read(lambda: c.doc.GetVariable('OSMODE'))
    try:
        retry_busy(lambda: c.doc.SetVariable('OSMODE', 0))
        try:
            retry_document_read(lambda: c.doc.SendCommand('TRectAxis\r'))
            found = _wait(lambda: _axis_panel(u, c.pid), timeout)
            if found is None:
                raise PanelTimeout('TRectAxis panel did not appear')
            time.sleep(0.25)
            panel, controls = found

            angle = args['axis_angle']
            angle_control = controls[AXIS_ANGLE_ID]
            _axis_edit(backend, c.pid, panel, angle_control, f'{angle:g}',
                       lambda: backend.send(0x09, 0, 0))
            time.sleep(0.6)
            got_angle = text_of(u, angle_control).strip()
            try:
                angle_matches = abs(float(got_angle) - angle) <= 1e-3
            except ValueError:
                angle_matches = False
            if not angle_matches:
                raise PanelTimeout(f'TRectAxis angle mismatch: wanted {angle}, got {got_angle!r}')

            for direction in AXIS_DIR_IDS:
                _axis_click(backend, c.pid, panel, controls[direction])
                _axis_click(backend, c.pid, panel, controls[AXIS_CLEAR_ID])
            cleared = axis_panel_totals(u, panel)
            if any(cleared.values()):
                raise PanelTimeout(f'TRectAxis totals did not reset: {cleared!r}')

            entered = {}
            observed = {}
            for direction, key, label in zip((AXIS_DIR_DOWN, AXIS_DIR_LEFT),
                                             ('bays', 'depths'), _AXIS_TOTAL_LABELS):
                _axis_click(backend, c.pid, panel, controls[direction])
                text = ' '.join(f'{value:g}' for value in args[key])
                _axis_edit(backend, c.pid, panel, controls[AXIS_QUICK_ID], text, backend.enter)
                time.sleep(0.85)
                total = axis_panel_totals(u, panel)[label]
                expected = sum(args[key])
                if not abs(total - expected) <= 1.0:
                    raise PanelTimeout(f'TRectAxis {label} wanted {expected}, got {total}')
                entered[key] = text
                observed[label] = total

            view, keyboard_hwnd = _focus_axis_view(c, backend)
            x, y = args['origin'][:2]
            coordinate = f'{x:.4f},{y:.4f}'
            _axis_type(backend, keyboard_hwnd, c.hwnd, coordinate, backend.enter,
                       allow_child_focus=True, stage='rect_origin')
            time.sleep(1.3)
        finally:
            _axis_foreground(backend, c.pid, c.hwnd)
            backend.esc(2)
            if _wait(lambda: _axis_panel(u, c.pid) is None, timeout) is None:
                raise PanelTimeout('TRectAxis panel did not close after Escape')
            wait_command_idle(c)
    finally:
        retry_busy(lambda: c.doc.SetVariable('OSMODE', old_osmode))
    return {'hwnd': panel, 'angle': float(got_angle), 'cleared_totals': cleared,
            'configured_totals': observed, 'spacing_text': entered,
            'view_hwnd': view, 'keyboard_hwnd': keyboard_hwnd, 'coordinate_text': coordinate}


def create_column(c, args, backend=None, timeout=15.0):
    """Create one rectangular TDbColumn through the TGColumn panel.

    The command is posted, the panel is filled by keyboard, one canvas click
    places the column, and ESC restores the command line. Verification of the
    created entity is left to the caller (parts.execute_part) so a failure here
    can never look like a confirmed creation.
    """
    from .cad import (CommandCompletionTimeout, OutcomeUnknown, retry_busy,
                      retry_document_read)
    from .input_bridge import NativeInput

    backend = backend or NativeInput()
    u = backend.u
    backend.activate_view(c.pid, c.hwnd)
    # Object snap can move the pick by tens of units (3EF landed 41 units off
    # with OSMODE=16383), which would make "the requested point" a guess. It is
    # switched off for the placement and restored whether the click succeeds,
    # fails or the panel never shows up.
    old_osmode = int(float(retry_document_read(lambda: c.doc.GetVariable('OSMODE'))))
    c.run_command('(setvar "OSMODE" 0)\n')
    # Smooth view transitions animate a reframe over about a second while
    # VIEWCTR/VIEWSIZE already report the target. A pick issued during that
    # animation is transformed with the outgoing view, so a framed column lands
    # on the previous view centre (measured 2026-11-21, column c2 landed at
    # (16000,13000) instead of (16000,10400) with VTENABLE=3).
    old_vtenable = int(float(retry_document_read(lambda: c.doc.GetVariable('VTENABLE'))))
    if old_vtenable:
        c.run_command('(setvar "VTENABLE" 0)\n')
    try:
        return _place_column(c, args, backend, timeout)
    finally:
        if old_vtenable:
            try:
                c.run_command('(setvar "VTENABLE" %d)\n' % old_vtenable)
            except Exception:
                pass
        try:
            c.run_command('(setvar "OSMODE" %d)\n' % old_osmode)
        except Exception:
            pass


def _type_command(backend, pid, hwnd, view_rect, name):
    """Type a command into the drawing view, which is what tianzheng listens to."""
    backend.activate_view(pid, hwnd)
    # A click in the view takes keyboard focus back from any panel or palette.
    backend.click((view_rect[0] + view_rect[2]) // 2, (view_rect[1] + view_rect[3]) // 2)
    time.sleep(0.4)
    for ch in name:
        backend.unicode(ch)
    backend.enter()


def _place_column(c, args, backend, timeout):
    from .cad import (CommandCompletionTimeout, OutcomeUnknown, retry_busy,
                      retry_document_read)

    u = backend.u

    def find_column_panel():
        return next((candidate for candidate in panels(u, c.pid)
                     if all(field_edit(u, candidate, label) is not None for label in FIELDS)), None)

    backend.activate_view(c.pid, c.hwnd)
    # COM is blocked while the modeless panel is open, so the view must be
    # measured before the command is posted.
    centre = retry_document_read(lambda: tuple(c.doc.GetVariable('VIEWCTR')))
    view_size = float(retry_document_read(lambda: c.doc.GetVariable('VIEWSIZE')))
    view_rect = viewport(u, c.hwnd, c.pid)
    # The panel blocks COM once it is open, so a point outside the view cannot
    # be reframed afterwards. Callers keep the whole plan in view before the
    # column phase (parts.execute_part), which makes this branch a last resort:
    # a reframe issued here is not always effective for the very next pick.
    frame = getattr(c, 'frame_wall', None)
    if frame is not None:
        try:
            world_to_screen((centre[0], centre[1]), view_size, view_rect, args['point'])
        except PanelTimeout:
            frame({'start': args['point'], 'end': args['point']})
            time.sleep(0.3)
            centre = retry_document_read(lambda: tuple(c.doc.GetVariable('VIEWCTR')))
            view_size = float(retry_document_read(lambda: c.doc.GetVariable('VIEWSIZE')))
            world_to_screen((centre[0], centre[1]), view_size, view_rect, args['point'])
    # The panel is one modeless dialog that tianzheng shows again on the next
    # command; it is not created fresh per invocation. Waiting for a handle
    # that was absent before therefore times out as soon as a previous attempt
    # left the panel open, so leftover panels are closed first instead.
    for _ in range(3):
        if find_column_panel() is None:
            break
        backend.esc(2)
        _wait(lambda: find_column_panel() is None, 2.0)
    # PostCommand does not open this panel: the tianzheng command reacts to
    # keystrokes arriving at the command line, so the name is typed into the
    # foreground drawing view instead (measured 2026-11-19; PostCommand left
    # CMDACTIVE at 0 and no #32770 appeared across twelve seconds).
    _type_command(backend, c.pid, c.hwnd, view_rect, 'TGColumn')
    panel = _wait(find_column_panel, timeout)
    if panel is None:
        raise CommandCompletionTimeout('TGColumn panel did not appear')
    for label, value in (('横向', int(args['width'])), ('纵向', int(args['depth'])),
                         ('柱高', int(args['height']))):
        hwnd = field_edit(u, panel, label)
        if hwnd is None:
            raise OutcomeUnknown('Panel row missing: ' + label)
        set_field(backend, hwnd, value)
    x, y = world_to_screen((centre[0], centre[1]), view_size, view_rect, args['point'])
    backend.activate_view(c.pid, c.hwnd)
    # SetCursorPos rounds to whole pixels; report the world point the click
    # actually landed on so verification compares like with like.
    sx, sy = int(round(x)), int(round(y))
    backend.click(sx, sy)
    time.sleep(0.5)
    for _ in range(3):
        if find_column_panel() is None:
            break
        backend.esc(2)
        _wait(lambda: find_column_panel() is None, 2.0)
    if _wait(lambda: int(retry_document_read(lambda: c.doc.GetVariable('CMDACTIVE'))) == 0, 5.0) is None:
        backend.esc(2)
        _wait(lambda: int(retry_document_read(lambda: c.doc.GetVariable('CMDACTIVE'))) == 0, 5.0)
    world = screen_to_world((centre[0], centre[1]), view_size, view_rect, sx, sy)
    scale = view_size / (view_rect[3] - view_rect[1])
    return {'panel': panel, 'click': (sx, sy), 'scale': round(scale, 6),
            'world': (round(world[0], 3), round(world[1], 3))}

def column_dxf(c, handle, timeout=10.0, folder=None):
    """Read one column's DXF group codes through a typed AutoLISP dump.

    COM cannot report a column's insertion point, and DXF is the only source
    that distinguishes a standard column (71=60/72=0) from a constructed one.
    """
    import re
    from .cad import OutcomeUnknown

    folder = Path(folder) if folder is not None else Path(__file__).resolve().parent.parent / '_tmp'
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / ('dxf-%s.txt' % handle)
    if out.exists():
        out.unlink()
    lisp = ('((lambda (/ f e g v) (setq f (open "%s" "w")) (setq e (handent "%s")) '
            '(foreach g (entget e) '
            '(cond '
            '((member (car g) \'(10 11)) '
            '(princ (strcat "(" (itoa (car g))) f) '
            '(foreach v (cdr g) (princ (strcat " " (rtos v 2 6)) f)) '
            '(if (not (cdddr g)) (princ " 0.0" f)) (princ ")" f)) '
            '((member (car g) \'(50 149)) '
            '(princ (strcat "(" (itoa (car g)) " . " (rtos (cdr g) 2 6) ")") f)) '
            '(T (princ (vl-princ-to-string g) f))) (write-line "" f)) (close f) (princ)))'
            % (out.as_posix(), handle))
    c.run_command(lisp + '\n')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not out.exists():
        time.sleep(0.1)
    if not out.exists():
        raise OutcomeUnknown('Column DXF dump did not appear: ' + str(out))
    text = out.read_text(encoding='gbk', errors='replace')

    def code(number):
        match = re.search(r'\(%s \. ([^)]*)\)' % number, text)
        return match.group(1).strip() if match else None

    def number(name):
        value = code(name)
        return float(value) if value is not None else None

    points = [(float(a), float(b)) for a, b in re.findall(r'\(10 ([\-\d.]+) ([\-\d.]+)', text)]
    bulges = [float(v) for v in re.findall(r'\(50 \. ([\-\d.]+)\)', text)]
    insertion = re.search(r'\(11 ([\-\d.]+) ([\-\d.]+)', text)
    flags = []
    for name in ('70', '71', '72'):
        value = code(name)
        flags.append(int(value) if value is not None else None)
    return {'type': code('0'), 'handle': code('5'), 'layer': code('8'),
            'vertices': points, 'bulges': bulges,
            'insertion': (float(insertion.group(1)), float(insertion.group(2))) if insertion else None,
            'height': number('149'), 'flags': tuple(flags)}


def roof_dxf(c, handle, timeout=10.0, folder=None):
    """Read the roof's elevation (148), XY vertices (10) and edge slopes (51)."""
    import re
    from .cad import OutcomeUnknown

    folder = Path(folder) if folder is not None else Path(__file__).resolve().parent.parent / '_tmp'
    folder.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='roof-dxf-', dir=folder) as directory:
        out = Path(directory).resolve() / 'entget.txt'
        lisp = ('(progn (setq f (open "%s" "w")) (setq e (handent "%s")) '
                '(princ (vl-princ-to-string (entget e)) f) (close f) (princ))'
                % (out.as_posix(), handle))
        c.run_command(lisp + '\n')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not out.exists():
            time.sleep(0.1)
        if not out.exists():
            raise OutcomeUnknown('Roof DXF dump did not appear: ' + str(out))
        raw = out.read_text(encoding='gbk', errors='replace')

    def values(group):
        return re.findall(r'\(\s*%d\s+\.\s+([^)]*)\)' % group, raw)

    def code(group):
        return next((value.strip().strip('"') for value in values(group)), None)

    elevation = code(148)
    vertices = []
    for point in re.findall(r'\(\s*10\s+([^)]*)\)', raw):
        x, y, *_ = point.split()
        vertices.append((float(x), float(y)))
    return {'type': code(0), 'handle': code(5), 'layer': code(8),
            'elevation': float(elevation) if elevation is not None else None,
            'vertices': vertices, 'edge_slopes_rad': [float(value) for value in values(51)]}


def _scalar_dxf(c, handle, fields, timeout, folder, label, *, repeated=()):
    """Dump selected entget groups, preserving whitespace and punctuation in strings."""
    from .cad import OutcomeUnknown

    folder = Path(folder) if folder is not None else Path(__file__).resolve().parent.parent / '_tmp'
    folder.mkdir(parents=True, exist_ok=True)
    groups = ' '.join(str(group) for group in fields)
    with TemporaryDirectory(prefix=f'{label.lower()}-dxf-', dir=folder) as directory:
        out = Path(directory).resolve() / 'entget.txt'
        path = out.as_posix()
        lisp = ('((lambda (/ f g) '
                f'(setq f (if (= 0 (getvar "LISPSYS")) (open "{path}" "w") '
                f'(open "{path}" "w" "utf8-bom"))) '
                f'(foreach g (entget (handent "{handle}")) '
                f'(if (member (car g) (quote ({groups}))) '
                '(write-line (strcat (itoa (car g)) " " (vl-princ-to-string (cdr g))) f))) '
                '(close f) (princ)))')
        c.run_command(lisp + '\n')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not out.exists():
            time.sleep(0.1)
        if not out.exists():
            raise OutcomeUnknown(f'{label} DXF dump did not appear: {out}')
        data = out.read_bytes()

    encoding = 'utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'gbk'
    values = {}
    # Only CR/LF delimit records. Other Unicode separators may occur in text.
    for line in data.decode(encoding).replace('\r\n', '\n').split('\n'):
        group, separator, value = line.partition(' ')
        if separator:
            code = int(group)
            if code in repeated:
                values.setdefault(code, []).append(value)
            else:
                values.setdefault(code, value)
    return {key: convert(values[group]) if group in values else None
            for group, (key, convert) in fields.items()}


def curve_axes(c, handles, timeout=10.0, folder=None):
    """Read WCS endpoints and curve lengths for a batch of entity handles."""
    from .cad import OutcomeUnknown

    requested = {}
    for handle in handles:
        if not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle):
            raise ValueError('Curve axes require hexadecimal handles')
        requested[handle.upper()] = handle
    if not requested:
        return {}

    folder = Path(folder) if folder is not None else Path(__file__).resolve().parent.parent / '_tmp'
    folder.mkdir(parents=True, exist_ok=True)
    listing = ' '.join(f'"{handle}"' for handle in requested)
    with TemporaryDirectory(prefix='curve-axis-', dir=folder) as directory:
        out = Path(directory).resolve() / 'axis.txt'
        path = out.as_posix()
        lisp = (
            '((lambda (/ f h e d sp ep param span tab) '
            '(vl-load-com) (setq tab (chr 9)) '
            f'(setq f (if (= 0 (getvar "LISPSYS")) (open "{path}" "w") '
            f'(open "{path}" "w" "utf8-bom"))) '
            '(if f (progn '
            f'(foreach h (list {listing}) '
            '(if (and (setq e (handent h)) (setq d (entget e))) (progn '
            "(setq sp (vl-catch-all-apply 'vlax-curve-getStartPoint (list e)) "
            "ep (vl-catch-all-apply 'vlax-curve-getEndPoint (list e)) "
            "param (vl-catch-all-apply 'vlax-curve-getEndParam (list e))) "
            '(if (and sp (listp sp) ep (listp ep) (numberp param)) (progn '
            "(setq span (vl-catch-all-apply 'vlax-curve-getDistAtParam (list e param))) "
            '(if (numberp span) '
            '(write-line (strcat h tab (cdr (assoc 0 d)) tab (cdr (assoc 8 d)) '
            "(apply 'strcat (mapcar '(lambda (v) (strcat tab (rtos v 2 8))) "
            '(append sp ep (list span))))) f))))))) '
            '(close f))) (princ)))\n'
        )
        c.run_command(lisp)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not out.exists():
            time.sleep(0.1)
        if not out.exists():
            raise OutcomeUnknown(f'Curve axis dump did not appear: {out}')
        try:
            data = out.read_bytes()
            encoding = 'utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'gbk'
            rows = data.decode(encoding).replace('\r\n', '\n').split('\n')
        except (OSError, UnicodeError) as exc:
            raise OutcomeUnknown(f'Could not read curve axis dump: {exc}') from exc

    axes = {}
    for row in filter(None, rows):
        try:
            handle, kind, layer, *coordinates = row.split('\t')
            handle = requested[handle.upper()]
            values = list(map(float, coordinates))
            if (handle in axes or not kind.strip() or not layer.strip()
                    or len(values) != 7 or not all(map(math.isfinite, values))):
                raise ValueError
        except (KeyError, ValueError) as exc:
            raise OutcomeUnknown(f'Malformed curve axis row: {row!r}') from exc
        axes[handle] = {'type': kind, 'layer': layer, 'start': values[:3],
                        'end': values[3:6], 'length': values[6]}
    missing = sorted(set(requested.values()) - axes.keys())
    if missing:
        raise OutcomeUnknown(f'Curve axes are missing for handles: {missing}')
    return axes


def _dxf_point(value):
    return tuple(float(coordinate) for coordinate in value.strip().strip('()').split())


_ENTITY_DXF_FIELDS = {0: ('type', str), 5: ('handle', str), 8: ('layer', str)}
_TEXT_DXF_FIELDS = {1: ('text', str), 40: ('height', float), 50: ('rotation', float)}
_LINE_DXF_FIELDS = {10: ('start', _dxf_point), 11: ('end', _dxf_point)}
_CIRCLE_DXF_FIELDS = {10: ('center', _dxf_point), 40: ('radius', float)}
# AutoLISP entget returns angles in radians.
_ARC_DXF_FIELDS = {**_CIRCLE_DXF_FIELDS,
                   50: ('start_angle_rad', float), 51: ('end_angle_rad', float)}


def opening_label_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, 302: ('label', str)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'OpeningLabel')


def opening_group_dxf(c, handle, timeout=10.0, folder=None):
    # The scalar reader keeps the first value, which belongs to the outer
    # opening. The embedded child openings repeat these DXF codes.
    fields = {**_ENTITY_DXF_FIELDS, 302: ('label', str), 40: ('width', float),
              41: ('height', float), 71: ('kind_code', int), 140: ('text_height', float)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'OpeningGroup')


def space_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, 1: ('name', str), 2: ('number', str),
              10: ('point', _dxf_point), 41: ('area_m2', float), 42: ('perimeter_mm', float),
              43: ('offset_mm', float), 70: ('flags', int)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Space')


def text_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, **_TEXT_DXF_FIELDS}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Text')


def mtext_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, 3: ('chunks', list), 1: ('tail', str),
              10: ('insertion', _dxf_point), 40: ('height', float), 41: ('width', float),
              50: ('rotation', float), 71: ('attachment', int)}
    raw = _scalar_dxf(c, handle, fields, timeout, folder, 'MText', repeated=(3,))
    chunks, tail = raw.pop('chunks'), raw.pop('tail')
    raw['text'] = None if chunks is None and tail is None else ''.join(chunks or []) + (tail or '')
    return raw


def dim_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS,
              10: ('def_point', _dxf_point), 13: ('ext1', _dxf_point),
              14: ('ext2', _dxf_point), 15: ('chord_point', _dxf_point),
              42: ('measurement', float), 50: ('rotation', float), 70: ('dim_type', int)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Dimension')


def t20_dim_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, 68: ('group_68', int)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'T20 dimension')


def line_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, **_LINE_DXF_FIELDS}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Line')


def circle_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, **_CIRCLE_DXF_FIELDS}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Circle')


def arc_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, **_ARC_DXF_FIELDS}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Arc')


def ellipse_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, 10: ('center', _dxf_point), 11: ('major_axis', _dxf_point),
              40: ('radius_ratio', float), 41: ('start_param', float), 42: ('end_param', float)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Ellipse')


def hatch_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, 2: ('pattern_name', str), 70: ('solid_fill', int),
              71: ('associative', int), 41: ('pattern_scale', float), 52: ('pattern_angle_rad', float)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Hatch')


def block_ref_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, 2: ('name', str), 10: ('insertion', _dxf_point),
              41: ('scale_x', float), 42: ('scale_y', float), 43: ('scale_z', float),
              50: ('rotation', float)}
    result = _scalar_dxf(c, handle, fields, timeout, folder, 'Block reference')
    for key, default in (('scale_x', 1.0), ('scale_y', 1.0), ('scale_z', 1.0), ('rotation', 0.0)):
        if result[key] is None:
            result[key] = default
    return result


def _transform_point(value):
    coordinates = _dxf_point(value)
    if len(coordinates) not in (2, 3) or not all(map(math.isfinite, coordinates)):
        raise ValueError('Expected a finite DXF point')
    return coordinates[:2]


def _transform_vertices(values):
    if len(values) < 2:
        raise ValueError('Expected at least two DXF vertices')
    return [_transform_point(value) for value in values]


_TRANSFORM_DXF_FIELDS = {
    'AcDbLine': ('LINE', _LINE_DXF_FIELDS),
    'AcDbCircle': ('CIRCLE', _CIRCLE_DXF_FIELDS),
    'AcDbArc': ('ARC', _ARC_DXF_FIELDS),
    'AcDbText': ('TEXT', {**_TEXT_DXF_FIELDS, 10: ('insertion', _dxf_point)}),
    'AcDbPolyline': ('LWPOLYLINE', {10: ('vertices', _transform_vertices),
                                   70: ('closed', lambda value: int(value) & 1)}),
}


def polyline_props_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, **_TRANSFORM_DXF_FIELDS['AcDbPolyline'][1],
              38: ('elevation', float), 43: ('constant_width', float),
              42: ('bulges', lambda values: tuple(map(float, values)))}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Polyline properties', repeated=(10, 42))


def transform_dxf(c, handle, object_name, timeout=10.0, folder=None):
    """Read the plan geometry and attributes used to verify an entity transform."""
    from .cad import OutcomeUnknown

    expected_type, geometry = _TRANSFORM_DXF_FIELDS[object_name]
    fields = {0: ('type', str)}
    fields.update({code: (key, _transform_point if convert is _dxf_point else convert)
                   for code, (key, convert) in geometry.items()})
    repeated = (10,) if object_name == 'AcDbPolyline' else ()
    try:
        snapshot = _scalar_dxf(c, handle, fields, timeout, folder, expected_type, repeated=repeated)
    except (OSError, ValueError) as exc:
        raise OutcomeUnknown(f'Malformed {expected_type} DXF for {handle}: {exc}') from exc
    if snapshot['type'] != expected_type:
        raise OutcomeUnknown(f'DXF type mismatch for {handle}: expected {expected_type}, got {snapshot["type"]}')
    for key, value in snapshot.items():
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            raise OutcomeUnknown(f'Missing or invalid {expected_type} DXF {key} for {handle}')
    return snapshot


def entity_layer_dxf(c, handle, timeout=10.0, folder=None):
    return _scalar_dxf(c, handle, _ENTITY_DXF_FIELDS, timeout, folder, 'Entity layer')


def entity_color_dxf(c, handle, timeout=10.0, folder=None):
    fields = {**_ENTITY_DXF_FIELDS, 62: ('color', int)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Entity color')


def slab_dxf(c, handle, timeout=10.0, folder=None):
    fields = {0: ('type', str), 5: ('handle', str), 8: ('layer', str),
              148: ('elevation', float), 149: ('thickness', float)}
    return _scalar_dxf(c, handle, fields, timeout, folder, 'Slab')


def verify_column_dxf(dxf, args, placement, tolerance=0.5):
    """Check the dumped section is the requested rectangle, where requested."""
    from .cad import OutcomeUnknown

    if dxf['type'] != 'TCH_COLUMN':
        raise OutcomeUnknown('Created entity is not a TCH_COLUMN: %s' % dxf['type'])
    if len(dxf['vertices']) != 4:
        raise OutcomeUnknown('Column DXF section is not a four-vertex rectangle')
    xs = [p[0] for p in dxf['vertices']]
    ys = [p[1] for p in dxf['vertices']]
    width, depth = max(xs) - min(xs), max(ys) - min(ys)
    if abs(width - args['width']) > tolerance or abs(depth - args['depth']) > tolerance:
        raise OutcomeUnknown('Column DXF outline mismatch: %.3f x %.3f' % (width, depth))
    if dxf['height'] is None or abs(dxf['height'] - args['height']) > tolerance:
        raise OutcomeUnknown('Column DXF height mismatch: %r' % dxf['height'])
    if dxf['flags'][0] != 1 or dxf['flags'][2] != 0:
        raise OutcomeUnknown('Column DXF flags are not a standard rectangle: %r' % (dxf['flags'],))
    if dxf['insertion'] is None:
        raise OutcomeUnknown('Column DXF insertion point is missing')
    # Object snap moves the pick by a fraction of a pixel; allow two pixels.
    pick_tolerance = max(1.0, 2.0 * float(placement.get('scale') or 0.0))
    if any(abs(dxf['insertion'][i] - placement['world'][i]) > pick_tolerance for i in (0, 1)):
        raise OutcomeUnknown('Column insertion differs from the clicked point: %r vs %r'
                             % (dxf['insertion'], placement['world']))
    return {'type': dxf['type'], 'layer': dxf['layer'], 'outline': [round(width, 3), round(depth, 3)],
            'height': dxf['height'], 'flags': list(dxf['flags']), 'insertion': list(dxf['insertion']),
            'vertices': len(dxf['vertices'])}


DOOR_TYPES = ('普通门', '甲级防火门', '乙级防火门', '丙级防火门', '防火卷帘',
              '人防门', '隔断门', '电梯门')
OPENING_FIELDS = (('门洞宽', 'width'), ('门洞高', 'height'), ('门槛高', 'sill'))
WINDOW_TYPES = ('普通窗',)
WINDOW_FIELDS = (('窗宽', 'width'), ('窗高', 'height'), ('窗台高', 'sill'))
# Doors and windows are inserted by two sibling commands, TGDOOR and TGWINDOW,
# each of which opens its own untitled palette (80,120)-(255,583) holding one
# type ComboBox and three size rows. TOpening is the flat 门窗参数 panel and
# keeps whichever mode it was last in, so a recipe mixing doors and windows
# cannot use it: measured 2026-11-20, a door step after a window step failed
# with 'Opening panel is set to 普通窗, not 普通门' and the panel's type list
# only ever offers the current mode. The palettes draw their row labels as
# owner-drawn text, so the rows are addressed by vertical order: 门洞宽/门洞高/
# 门槛高 and 窗宽/窗高/窗台高 from top to bottom.


def _plain(text):
    """Label text without the spaces tianzheng pads labels with."""
    return ''.join(ch for ch in text if ch != ' ')


def _label_matches(shown, want):
    """Match a panel label where a private-use glyph stands in for one hanzi.

    The 门窗参数 panel draws 门洞宽 as '门<U+E5E5>宽', so the glyph is a
    wildcard for exactly one character rather than something to strip.
    """
    import re
    pattern = ''.join('.' if '\ue000' <= ch <= '\uf8ff' else re.escape(ch)
                      for ch in _plain(shown))
    return re.fullmatch(pattern, _plain(want)) is not None


# Labels tianzheng draws literally; a glyph wildcard must never claim them,
# otherwise 门槛高 would also answer a search for 门洞高.
LITERAL_LABELS = {'门槛高'}


def _row_edit_beside(u, panel, lab):
    ll, lt, lr, lb = rect(u, lab)
    best = None
    for c in children(u, panel):
        if class_name(u, c) != 'Edit' or not visible(u, c):
            continue
        cl, ct, cr, cb = rect(u, c)
        if ct < lb and cb > lt and cl >= lr - 2:
            distance = cl - lr
            if best is None or distance < best[0]:
                best = (distance, c)
    return best[1] if best is not None else None


def row_edit(u, panel, label):
    """The Edit of a labelled row on the flat parameter panel (门窗参数).

    Unlike the column panel these rows are a bare Static label plus an Edit or
    ComboBox on the same line, so the edit is matched by geometry: same row,
    to the right of the label. Labels carrying a private-use glyph are matched
    only after every literal label has been ruled out.
    """
    want = _plain(label)
    labels = [lab for lab in children(u, panel)
              if class_name(u, lab) == 'Static' and visible(u, lab)]
    for lab in labels:
        if _plain(title(u, lab)) == want:
            found = _row_edit_beside(u, panel, lab)
            if found is not None:
                return found
    for lab in labels:
        shown = _plain(title(u, lab))
        if shown in LITERAL_LABELS:
            continue
        if _label_matches(shown, want):
            found = _row_edit_beside(u, panel, lab)
            if found is not None:
                return found
    return None


def value_row_edit(u, panel, index):
    """The Edit of one size row in a TGDOOR/TGWINDOW palette, addressed by order.

    The rows are 门洞宽/门洞高/门槛高 or 窗宽/窗高/窗台高 from top to bottom.
    Each row is an untitled #32770 whose label is drawn by the parent, so there
    is no text to match on and the vertical order carries the meaning. The
    addressable Edit is collapsed to 2px (the row shows a spin button where the
    value would be), while TGDOOR's extra open-direction row above them carries
    ordinary 30px Edits; the width therefore separates value rows from that
    row, which would otherwise shift every index by one.
    """
    rows = []
    for child in children(u, panel):
        if class_name(u, child) != PANEL_CLASS or not visible(u, child):
            continue
        edits = [e for e in children(u, child)
                 if class_name(u, e) == 'Edit' and visible(u, e)
                 and rect(u, e)[2] - rect(u, e)[0] <= 6]
        if edits:
            rows.append((rect(u, child)[1], child, edits[0]))
    rows.sort()
    return rows[index][2] if index < len(rows) else None


def opening_kind(u, panel):
    """The selected type in the 门窗参数 panel, for example 普通门."""
    for c in children(u, panel):
        if class_name(u, c) != 'ComboBox' or not visible(u, c):
            continue
        shown = text_of(u, c).strip()
        if shown in DOOR_TYPES or shown in WINDOW_TYPES:
            return shown
    return None


def create_opening(c, args, backend=None, timeout=15.0):
    """Create one door or window through the TGDOOR / TGWINDOW palette.

    The command opens a modeless palette; its three size rows are typed in, then
    one canvas click on the host wall inserts the opening. The command stays
    ready for another pick, so ESC ends it. COM is blocked while the palette is
    open, therefore every variable is read before the command is typed.
    """
    from .cad import CommandCompletionTimeout, OutcomeUnknown, retry_busy, retry_document_read
    from .input_bridge import NativeInput

    backend = backend or NativeInput()
    backend.activate_view(c.pid, c.hwnd)
    old_osmode = int(float(retry_document_read(lambda: c.doc.GetVariable('OSMODE'))))
    c.run_command('(setvar "OSMODE" 0)\n')
    try:
        return _place_opening(c, args, backend, timeout)
    finally:
        try:
            c.run_command('(setvar "OSMODE" %d)\n' % old_osmode)
        except Exception:
            pass


def _place_opening(c, args, backend, timeout):
    from .cad import CommandCompletionTimeout, OutcomeUnknown, retry_document_read

    u = backend.u

    def find_opening_panel():
        return next((candidate for candidate in panels(u, c.pid)
                     if opening_kind(u, candidate) is not None), None)

    backend.activate_view(c.pid, c.hwnd)
    centre = retry_document_read(lambda: tuple(c.doc.GetVariable('VIEWCTR')))
    view_size = float(retry_document_read(lambda: c.doc.GetVariable('VIEWSIZE')))
    view_rect = viewport(u, c.hwnd, c.pid)
    window = args['kind'] in WINDOW_TYPES
    command = 'TGWINDOW' if window else 'TGDOOR'
    try:
        x, y = world_to_screen((centre[0], centre[1]), view_size, view_rect, args['point'])
    except PanelTimeout:
        # The palette blocks COM, so an unusable view has to be reframed while
        # it is still reachable.
        c.frame_wall({'start': args['point'], 'end': args['point']})
        centre = retry_document_read(lambda: tuple(c.doc.GetVariable('VIEWCTR')))
        view_size = float(retry_document_read(lambda: c.doc.GetVariable('VIEWSIZE')))
        x, y = world_to_screen((centre[0], centre[1]), view_size, view_rect, args['point'])
    # Both palettes are resident dialogs like the column panel: a previous
    # attempt leaves one open, so waiting for a handle that was absent before
    # times out. Close what is left and type the command into the drawing view,
    # because PostCommand does not open these palettes.
    for _ in range(3):
        if find_opening_panel() is None:
            break
        backend.esc(2)
        _wait(lambda: find_opening_panel() is None, 2.0)
    _type_command(backend, c.pid, c.hwnd, view_rect, command)
    panel = _wait(find_opening_panel, timeout)
    if panel is None:
        raise CommandCompletionTimeout('%s palette did not appear' % command)
    kind = opening_kind(u, panel)
    if kind is None:
        backend.esc(2)
        raise OutcomeUnknown('Opening panel type could not be read; refusing an unverified pick')
    if kind != args['kind']:
        backend.esc(2)
        _wait(lambda: find_opening_panel() is None, 2.0)
        raise OutcomeUnknown('Opening panel is set to %s, not %s' % (kind, args['kind']))
    for index, (label, key) in enumerate(WINDOW_FIELDS if window else OPENING_FIELDS):
        if key not in args:
            continue
        hwnd = value_row_edit(u, panel, index)
        if hwnd is None:
            raise OutcomeUnknown('Palette row missing: ' + label)
        set_field(backend, hwnd, int(args[key]))
    backend.activate_view(c.pid, c.hwnd)
    sx, sy = int(round(x)), int(round(y))
    backend.click(sx, sy)
    time.sleep(0.5)
    for _ in range(3):
        if find_opening_panel() is None:
            break
        backend.esc(2)
        _wait(lambda: find_opening_panel() is None, 2.0)
    if _wait(lambda: int(retry_document_read(lambda: c.doc.GetVariable('CMDACTIVE'))) == 0, 5.0) is None:
        backend.esc(2)
        _wait(lambda: int(retry_document_read(lambda: c.doc.GetVariable('CMDACTIVE'))) == 0, 5.0)
    world = screen_to_world((centre[0], centre[1]), view_size, view_rect, sx, sy)
    scale = view_size / (view_rect[3] - view_rect[1])
    return {'panel': panel, 'click': (sx, sy), 'kind': kind, 'scale': round(scale, 6),
            'world': (round(world[0], 3), round(world[1], 3))}


def opening_dxf(c, handle, timeout=10.0, folder=None, *, include_text_height=False):
    """Dump one TCH_OPENING and resolve the host wall handle behind group 330.

    A TCH_OPENING carries two 330 pairs: the first points at the block record
    that owns the entity, the second at the wall it was cut into. Picking the
    first one yields the block record handle (1F in model space), so the dump
    walks every 330 pair and keeps the one that resolves to a TCH_WALL.
    """
    import re
    from .cad import OutcomeUnknown

    folder = Path(folder) if folder is not None else Path(__file__).resolve().parent.parent / '_tmp'
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / ('dxf-open-%s.txt' % handle)
    if out.exists():
        out.unlink()
    lisp = ('(progn (setq f (open "%s" "w")) (setq e (handent "%s")) '
            '(princ (vl-princ-to-string (entget e)) f) (princ "\\nHOST " f) '
            '(setq h nil) '
            '(foreach p (entget e) (if (and (= (car p) 330) (= (type (cdr p)) (quote ENAME)) '
            '(= (cdr (assoc 0 (entget (cdr p)))) "TCH_WALL")) (setq h (cdr p)))) '
            '(if h (princ (cdr (assoc 5 (entget h))) f) (princ "?" f)) (close f) (princ))'
            % (out.as_posix(), handle))
    c.run_command(lisp + '\n')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not out.exists():
        time.sleep(0.1)
    if not out.exists():
        raise OutcomeUnknown('Opening DXF dump did not appear: ' + str(out))
    text = out.read_text(encoding='gbk', errors='replace')

    def code(number):
        match = re.search(r'\(%s \. ([^)]*)\)' % number, text)
        return match.group(1).strip() if match else None

    def number(name):
        value = code(name)
        return float(value) if value is not None else None

    point = re.search(r'\(10 ([\-\d.]+) ([\-\d.]+)', text)
    host = re.search(r'HOST ([0-9A-Fa-f?]+)', text)
    mirror_flag = number('70')
    coordinate = r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?'
    swing = re.search(rf'\(11\s+({coordinate})\s+({coordinate})(?=\s|\))', text)
    result = {'type': code('0'), 'handle': code('5'), 'layer': code('8'),
              'width': number('40'), 'height': number('41'), 'rotation': number('50'),
              'insertion': (float(point.group(1)), float(point.group(2))) if point else None,
              'host': host.group(1).upper() if host and host.group(1) != '?' else None,
              'mirror_flag': int(round(mirror_flag)) if mirror_flag is not None else None,
              'swing_point': tuple(map(float, swing.groups())) if swing else None}
    if include_text_height:
        result['text_height'] = number('140')
    return result


def verify_opening_dxf(dxf, args, placement, host_handle, tolerance=0.5):
    """Check the door landed on the referenced wall with the requested size."""
    from .cad import OutcomeUnknown

    if dxf['type'] != 'TCH_OPENING':
        raise OutcomeUnknown('Created entity is not a TCH_OPENING: %s' % dxf['type'])
    for key, code in (('width', 'width'), ('height', 'height')):
        if key in args and (dxf[code] is None or abs(dxf[code] - float(args[key])) > tolerance):
            raise OutcomeUnknown('Opening %s mismatch: %r vs %r' % (key, dxf[code], args[key]))
    if dxf['insertion'] is None:
        raise OutcomeUnknown('Opening DXF insertion point is missing')
    pick_tolerance = max(1.0, 2.0 * float(placement.get('scale') or 0.0))
    requested = args.get('point')

    def close_to(target, tol):
        return all(abs(dxf['insertion'][i] - target[i]) <= tol for i in (0, 1))

    # Tianzheng snaps an opening onto the wall centreline, so a click that lands a
    # little off the line still inserts at the requested centre. Accept either the
    # exact requested centre or the clicked point within the pixel tolerance.
    if not (close_to(placement['world'], pick_tolerance)
            or (requested is not None and close_to(requested, max(2.0, tolerance)))):
        raise OutcomeUnknown('Opening insertion differs from the clicked point: %r vs %r'
                             % (dxf['insertion'], placement['world']))
    if dxf['host'] is None or int(dxf['host'], 16) != int(str(host_handle), 16):
        raise OutcomeUnknown('Opening is hosted by %r, not by handle %s'
                             % (dxf['host'], host_handle))
    return {'type': dxf['type'], 'layer': dxf['layer'], 'width': dxf['width'],
            'height': dxf['height'], 'rotation': dxf['rotation'],
            'insertion': list(dxf['insertion']), 'host': dxf['host']}


def drawing_view(c):
    """Capture the drawing-area mapping while the command line is still idle."""
    from .cad import retry_document_read
    centre = retry_document_read(
        lambda: tuple(float(v) for v in c.doc.GetVariable('VIEWCTR')[:2]))
    size = retry_document_read(lambda: float(c.doc.GetVariable('VIEWSIZE')))
    return centre, size, viewport(_u(), c.hwnd, c.pid)


def _menu_buttons(u, root):
    """Visible buttons under root as (hwnd, text with whitespace removed)."""
    out = []
    for h in children(u, root):
        if not visible(u, h) or 'Button' not in class_name(u, h):
            continue
        text = re.sub(r'\s+', '', text_of(u, h))
        if text:
            out.append((h, text))
    return out


def _type_wall_split_command(c, command, backend=None):
    """Fallback when the T20 screen menu is collapsed/auto-hidden."""
    from .input_bridge import NativeInput
    backend = backend or NativeInput()
    backend.activate_view(c.pid, c.hwnd)
    backend.esc(1)
    time.sleep(0.2)
    for ch in command:
        backend.unicode(ch)
    backend.enter()
    time.sleep(0.8)
    return ['command:' + command]


def menu_dispatch(c, labels, timeout=3.0, backend=None):
    """Click a T20 screen-menu path by button title, using window messages only.

    The native split command exists in the T20 screen menu (`墙体 -> 墙体切割`),
    and also responds to typing `TKATANA` directly into the foreground drawing
    view when the T20 screen menu is collapsed or auto-hidden.
    """
    u = _u()
    fallback = 'TKATANA' if tuple(labels) == ('墙体', '墙体切割') else None
    bars = [h for h in children(u, c.hwnd)
            if class_name(u, h).startswith('AfxControlBar') and visible(u, h)
            and 'T20' in title(u, h)]
    if len(bars) != 1:
        if fallback is not None:
            return _type_wall_split_command(c, fallback, backend=backend)
        raise PanelTimeout('T20 screen menu bar not found (found %d)' % len(bars))

    def find(root, text):
        for h, label in _menu_buttons(u, root):
            if label == text:
                return h
        return None

    def find_top(root, text):
        buttons = _menu_buttons(u, root)
        for h, label in buttons:
            if label == text:
                return h
        for h, label in buttons:
            if label.startswith(text):
                return h
        for h, label in buttons:
            if label.endswith(text) and len(label) <= len(text) + 2:
                return h
        return None

    target = re.sub(r'\s+', '', labels[-1])
    clicked = []
    item = find(c.hwnd, target)
    if item is None:
        top = re.sub(r'\s+', '', labels[0])
        match = find_top(bars[0], top) or find_top(c.hwnd, top)
        if match is None:
            if fallback is not None:
                return _type_wall_split_command(c, fallback, backend=backend)
            seen = [label for _, label in _menu_buttons(u, c.hwnd)][:20]
            raise PanelTimeout('T20 menu item not found: %s (visible buttons: %r)' % (labels[0], seen))
        u.SendMessageW(W.HWND(match), 0x00F5, 0, 0)  # BM_CLICK
        clicked.append(int(match))
        time.sleep(0.8)
        item = _wait(lambda: find(c.hwnd, target), timeout, interval=0.2)
    if item is None:
        if fallback is not None:
            return _type_wall_split_command(c, fallback, backend=backend)
        seen = [label for _, label in _menu_buttons(u, c.hwnd)][:20]
        raise PanelTimeout('T20 menu item not found: %s (visible buttons: %r)' % (labels[-1], seen))
    u.SendMessageW(W.HWND(item), 0x00F5, 0, 0)
    clicked.append(int(item))
    time.sleep(0.8)
    return clicked


def pick_world_point(c, centre, view_size, view_rect, point, backend=None, timeout=2.0):
    """One real click at a world point, only when AutoCAD is truly foreground."""
    from .input_bridge import NativeInput
    backend = backend or NativeInput()
    x, y = world_to_screen(centre, view_size, view_rect, point)
    screen = (int(round(x)), int(round(y)))
    u = _u()
    backend.activate_view(c.pid, c.hwnd)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if int(u.GetForegroundWindow()) == int(c.hwnd):
            break
        time.sleep(0.05)
    else:
        raise PanelTimeout('AutoCAD is not the foreground window; pick not sent')
    backend.click(screen[0], screen[1])
    return {'screen': list(screen), 'world': list(point)}


def wait_command_idle(c, timeout=10.0):
    """Poll CMDACTIVE only; never send command text while a pick is pending."""
    from .cad import CommandCompletionTimeout
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if int(float(c.doc.GetVariable('CMDACTIVE'))) == 0:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    raise CommandCompletionTimeout('CAD command did not confirm completion')


def entity_wall_hosts(c, handle, folder=None, timeout=8.0):
    """Read the DXF 330 references of one entity as (handle, type) rows.

    A reference the dump cannot resolve is kept as an UNPARSEABLE row instead
    of being dropped: silently filtering it would let a half-read list look
    unique and pass the host check.
    """
    from .cad import OutcomeUnknown
    folder = Path(folder) if folder is not None else Path(__file__).resolve().parent.parent / '_tmp'
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / ('hosts-%s.txt' % handle)
    if out.exists():
        out.unlink()
    lisp = ('(progn (setq f (open "%s" "w")) (setq e (handent "%s")) '
            '(if e (foreach p (entget e) (if (= (car p) 330) '
            '(progn (setq r (cdr p)) (if (and (= (type r) (quote ENAME)) (entget r)) '
            '(princ (strcat (cdr (assoc 5 (entget r))) " " (cdr (assoc 0 (entget r))) "\\n") f) '
            '(princ "? UNRESOLVED\\n" f))))) '
            '(princ "? MISSING\\n" f)) (close f) (princ))') % (out.as_posix(), handle)
    c.run_command(lisp + '\n')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not out.exists():
        time.sleep(0.1)
    if not out.exists():
        raise OutcomeUnknown('Wall-host DXF dump did not appear: ' + str(out))
    refs = []
    for line in out.read_text(encoding='gbk', errors='replace').splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split()
        if len(parts) == 2 and all(ch in '0123456789abcdefABCDEF' for ch in parts[0]):
            refs.append({'handle': parts[0].upper(), 'type': parts[1]})
        else:
            refs.append({'handle': None, 'type': 'UNPARSEABLE', 'raw': text})
    if not refs:
        raise OutcomeUnknown('Entity %s has no readable DXF 330 references' % handle)
    return refs

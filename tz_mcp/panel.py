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
import time
from ctypes import wintypes as W
from pathlib import Path

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
    try:
        return _place_column(c, args, backend, timeout)
    finally:
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
    backend.activate_view(c.pid, c.hwnd)
    # COM is blocked while the modeless panel is open, so the view must be
    # measured before the command is posted.
    centre = retry_document_read(lambda: tuple(c.doc.GetVariable('VIEWCTR')))
    view_size = float(retry_document_read(lambda: c.doc.GetVariable('VIEWSIZE')))
    view_rect = viewport(u, c.hwnd, c.pid)
    # The panel is one modeless dialog that tianzheng shows again on the next
    # command; it is not created fresh per invocation. Waiting for a handle
    # that was absent before therefore times out as soon as a previous attempt
    # left the panel open, so leftover panels are closed first instead.
    for _ in range(3):
        if not panels(u, c.pid):
            break
        backend.esc(2)
        _wait(lambda: not panels(u, c.pid), 2.0)
    # PostCommand does not open this panel: the tianzheng command reacts to
    # keystrokes arriving at the command line, so the name is typed into the
    # foreground drawing view instead (measured 2026-11-19; PostCommand left
    # CMDACTIVE at 0 and no #32770 appeared across twelve seconds).
    _type_command(backend, c.pid, c.hwnd, view_rect, 'TGColumn')
    panel = _wait(lambda: next(iter(panels(u, c.pid)), None), timeout)
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
        if not panels(u, c.pid):
            break
        backend.esc(2)
        _wait(lambda: not panels(u, c.pid), 2.0)
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
    lisp = ('(progn (setq f (open "%s" "w")) (setq e (handent "%s")) '
            '(princ (vl-princ-to-string (entget e)) f) (close f) (princ))'
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
    insertion = re.search(r'\(11 ([\-\d.]+) ([\-\d.]+)', text)
    flags = []
    for name in ('70', '71', '72'):
        value = code(name)
        flags.append(int(value) if value is not None else None)
    return {'type': code('0'), 'handle': code('5'), 'layer': code('8'),
            'vertices': points,
            'insertion': (float(insertion.group(1)), float(insertion.group(2))) if insertion else None,
            'height': number('149'), 'flags': tuple(flags)}


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
        if not panels(u, c.pid):
            break
        backend.esc(2)
        _wait(lambda: not panels(u, c.pid), 2.0)
    _type_command(backend, c.pid, c.hwnd, view_rect, command)
    panel = _wait(lambda: next(iter(panels(u, c.pid)), None), timeout)
    if panel is None:
        raise CommandCompletionTimeout('%s palette did not appear' % command)
    kind = opening_kind(u, panel)
    if kind is None:
        backend.esc(2)
        raise OutcomeUnknown('Opening panel type could not be read; refusing an unverified pick')
    if kind != args['kind']:
        backend.esc(2)
        _wait(lambda: not panels(u, c.pid), 2.0)
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
        if not panels(u, c.pid):
            break
        backend.esc(2)
        _wait(lambda: not panels(u, c.pid), 2.0)
    if _wait(lambda: int(retry_document_read(lambda: c.doc.GetVariable('CMDACTIVE'))) == 0, 5.0) is None:
        backend.esc(2)
        _wait(lambda: int(retry_document_read(lambda: c.doc.GetVariable('CMDACTIVE'))) == 0, 5.0)
    world = screen_to_world((centre[0], centre[1]), view_size, view_rect, sx, sy)
    scale = view_size / (view_rect[3] - view_rect[1])
    return {'panel': panel, 'click': (sx, sy), 'kind': kind, 'scale': round(scale, 6),
            'world': (round(world[0], 3), round(world[1], 3))}


def opening_dxf(c, handle, timeout=10.0, folder=None):
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
    return {'type': code('0'), 'handle': code('5'), 'layer': code('8'),
            'width': number('40'), 'height': number('41'), 'rotation': number('50'),
            'insertion': (float(point.group(1)), float(point.group(2))) if point else None,
            'host': host.group(1).upper() if host and host.group(1) != '?' else None}


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
    if any(abs(dxf['insertion'][i] - placement['world'][i]) > pick_tolerance for i in (0, 1)):
        raise OutcomeUnknown('Opening insertion differs from the clicked point: %r vs %r'
                             % (dxf['insertion'], placement['world']))
    if dxf['host'] is None or int(dxf['host'], 16) != int(str(host_handle), 16):
        raise OutcomeUnknown('Opening is hosted by %r, not by handle %s'
                             % (dxf['host'], host_handle))
    return {'type': dxf['type'], 'layer': dxf['layer'], 'width': dxf['width'],
            'height': dxf['height'], 'rotation': dxf['rotation'],
            'insertion': list(dxf['insertion']), 'host': dxf['host']}

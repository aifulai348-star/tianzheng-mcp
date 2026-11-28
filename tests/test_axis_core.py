"""Offline CAD and Win32 fixtures for the two Stage-4 axis operations."""
import ctypes
import math
import unittest
from ctypes import wintypes as W
from itertools import accumulate
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tz_mcp import panel, parts
from tz_mcp.cad import Cad, OutcomeUnknown
from tz_mcp.cli import VALIDATION
from tz_mcp.commands import SCHEMAS, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


RECT = 'axis.rect_create'
FROM_WALLS = 'axis.from_walls'
RECT_ARGS = {'origin': [10000, 20000], 'bays': [3600, 4200], 'depths': [4500, 3000]}
WALL_ARGS = {'document_id': 'owned', 'wall_handles': ['A1', 'A2', 'A3', 'A4']}


def line(start, end):
    return SimpleNamespace(ObjectName='AcDbLine', Layer='DOTE', StartPoint=(*start, 0),
                           EndPoint=(*end, 0))


def wall():
    return SimpleNamespace(ObjectName='TDbWall', LeftWidth=120, RightWidth=120,
                           Height=3000, HoleArea=1.89)


def axis_grid(args):
    """Build either grid family with reversed endpoint order on alternate lines."""
    x, y = args['origin'][:2]
    bays, depths = args['bays'], args['depths']
    theta = math.radians(90 - args['axis_angle'])
    pairs = [((px, y - 1000), (px, y + sum(depths) + 1000))
             for px in accumulate(bays, initial=x)]
    for station in accumulate(depths, initial=0):
        dy = station / math.cos(theta)
        pairs.append(((x - 1000, y + dy - 1000 * math.tan(theta)),
                      (x + sum(bays) + 1000, y + dy + (sum(bays) + 1000) * math.tan(theta))))
    return {f'B{i}': line(*(pair[::-1] if i % 2 else pair)) for i, pair in enumerate(pairs)}


class FakeCad:
    document_id = 'owned'
    pid = 1
    hwnd = 1000

    def __init__(self, before, after):
        self.before = before
        self.after = after
        self.commands = []
        self.osmode = 16383
        self.active = 0
        self.events = []
        self.dispatched = False
        self.doc = self
        self.backend = FakeAxisInput(self)

    def guard(self):
        pass

    def entities(self):
        return self.after if self.dispatched else self.before

    def describe(self, entity):
        return {'object_name': entity.ObjectName, 'layer': entity.Layer}

    def SendCommand(self, payload):
        self.events.append(('dispatch', payload))
        self.active = 1
        self.backend.foreground = self.backend.hwnd

    def SetVariable(self, name, value):
        assert name == 'OSMODE'
        self.osmode = value
        self.events.append(('osmode', value))

    def GetVariable(self, name):
        return {'CMDACTIVE': self.active, 'OSMODE': self.osmode}[name]

    def run_command(self, payload):
        self.commands.append(payload)
        self.dispatched = True


class FakeAxisInput:
    hwnd = 2000
    view = 1100
    mdi = 1010
    command_line = 1113
    floating_command_line = 0x4a60fa0
    control_ids = (*panel.AXIS_DIR_IDS, panel.AXIS_CLEAR_ID, panel.AXIS_ANGLE_ID,
                   panel.AXIS_QUICK_ID)
    rows = {2100: '总开间:', 2101: '', 2102: '总进深:', 2103: ''}

    def __init__(self, cad):
        self.cad = cad
        self.u = self
        self.k = self
        self.foreground = cad.hwnd
        self.focus = None
        self.keyboard_override = None
        self.float_command_line_on_type = False
        self.window_owners = {self.floating_command_line: cad.hwnd}
        self.direction = None
        self.spacing = dict.fromkeys(panel.AXIS_DIR_IDS, 1234)
        self.text = {panel.AXIS_ANGLE_ID: '60', panel.AXIS_QUICK_ID: ''}
        self.bad_clear = False
        self.bad_bays = False

    def children(self, _, hwnd):
        return {self.hwnd: [*self.control_ids, *self.rows], self.cad.hwnd: [self.mdi],
                self.mdi: [self.view, 1101]}.get(hwnd, [])

    def class_name(self, _, hwnd):
        if hwnd == self.floating_command_line:
            return 'HwndWrapper[DefaultDomain;;test]'
        if hwnd in self.rows:
            return 'Static'
        if hwnd == self.mdi:
            return 'MDIClient'
        if hwnd in (self.view, 1101):
            return 'DXGI'
        return 'Edit' if hwnd in self.text else 'Button'

    def bounds(self, _, hwnd):
        if hwnd in (self.view, 1101):
            return (0, 0, 800, 900 if hwnd == self.view else 500)
        if hwnd in self.rows:
            return {2100: (0, 0, 80, 10), 2101: (86, 0, 130, 10),
                    2102: (0, 25, 80, 35), 2103: (86, 25, 130, 35)}[hwnd]
        return (hwnd * 10 - 1, 0, hwnd * 10 + 1, 2)

    def text_of(self, _, hwnd):
        if hwnd in self.rows:
            if hwnd == 2101:
                value = self.spacing[178] + self.spacing[179]
                self.cad.events.append(('bays_total', value))
                return str(value)
            if hwnd == 2103:
                value = self.spacing[180] + self.spacing[181]
                self.cad.events.append(('depth_total', value))
                return str(value)
            return self.rows[hwnd]
        return self.text[hwnd]

    def GetDlgCtrlID(self, hwnd):
        return hwnd.value

    def GetForegroundWindow(self):
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, pid):
        return 2

    def GetCurrentThreadId(self):
        return 3

    def AttachThreadInput(self, current, target, attach):
        return True

    def SetForegroundWindow(self, hwnd):
        self.foreground = hwnd.value

    def SetFocus(self, hwnd):
        self.focus = self.keyboard_override or hwnd.value
        self.text[self.focus] = ''
        self.cad.events.append(('focus_view', self.focus))

    def IsChild(self, hwnd, child):
        return (hwnd.value == self.cad.hwnd
                and child.value in (self.mdi, self.view, 1101, self.command_line,
                                    self.keyboard_override))

    def GetWindow(self, hwnd, relation):
        assert relation == 4
        return self.window_owners.get(hwnd.value, 0)

    def focus_command_line(self, *, floating=False):
        command_line = self.floating_command_line if floating else self.command_line
        self.text[command_line] = self.text.pop(self.focus)
        self.focus = command_line
        if floating:
            self.foreground = command_line

    def click(self, x, y):
        control = x // 10
        self.cad.events.append(('click', control))
        if control == panel.AXIS_CLEAR_ID:
            if not self.bad_clear:
                self.spacing[self.direction] = 0
        else:
            self.direction = control

    def focus_control(self, hwnd):
        self.focus = hwnd
        return True

    def focus_handle(self, hwnd):
        return self.focus

    def select_all(self):
        self.text[self.focus] = ''

    def unicode(self, ch):
        self.text[self.focus] += ch
        if self.float_command_line_on_type and self.focus == self.view:
            self.focus_command_line(floating=True)

    def send(self, vk, scan, flags):
        assert vk == 0x09
        self.cad.events.append(('angle', self.text[self.focus]))

    def enter(self):
        text = self.text[self.focus]
        if self.focus in (self.view, self.keyboard_override, self.command_line,
                          self.floating_command_line):
            self.cad.events.append(('placement', text))
            self.cad.dispatched = True
        else:
            self.cad.events.append(('spacing', self.direction, text))
            self.spacing[self.direction] = sum(map(float, text.split()))
            if self.direction == panel.AXIS_DIR_DOWN and self.bad_bays:
                self.spacing[self.direction] += 10

    def esc(self, count):
        self.cad.events.append(('esc', count))
        self.cad.active = 0

    def window_patches(self):
        return dict(
            panels=lambda u, pid: [self.hwnd] if self.cad.active else [],
            children=self.children, class_name=self.class_name, rect=self.bounds,
            text_of=self.text_of, visible=lambda u, hwnd: True,
            window_pid=lambda u, hwnd: self.cad.pid,
        )


class AxisTests(unittest.TestCase):
    def test_schema_validation(self):
        self.assertTrue({RECT, FROM_WALLS} <= SCHEMAS.keys())
        for op in (RECT, FROM_WALLS):
            self.assertTrue(VALIDATION[op].startswith('verified_live'))
            self.assertNotIn(op, NO_ESCAPE_STEPS)
        for key in RECT_ARGS:
            with self.subTest(missing=key), self.assertRaises(ValueError):
                prepare(RECT, {k: v for k, v in RECT_ARGS.items() if k != key})
        for key, bad in (
            ('origin', [0, 0, 1]), ('origin', [1, math.inf]), ('origin', [1, 10**1000]),
            ('bays', []), ('depths', []), ('bays', [1] * 31), ('depths', (1,)),
            ('bays', [-1]), ('bays', [0]), ('bays', [math.nan]), ('bays', [math.inf]),
            ('depths', [True]), ('depths', [10**1000]), ('axis_angle', 0),
            ('axis_angle', 180), ('axis_angle', True), ('axis_angle', math.nan),
        ):
            with self.subTest(key=key, value=bad), self.assertRaises(ValueError):
                prepare(RECT, {**RECT_ARGS, key: bad})
        for bad in ([], ('AB',), ['0xAB'], ['!'], ['AB', 'ab'], ['0A', 'A'], [4], ['A'] * 101):
            with self.subTest(handles=bad), self.assertRaises(ValueError):
                prepare(FROM_WALLS, {**WALL_ARGS, 'wall_handles': bad})
        for bad in ('', ' ', 123):
            with self.subTest(document=bad), self.assertRaises(ValueError):
                prepare(FROM_WALLS, {**WALL_ARGS, 'document_id': bad})
        self.assertEqual(prepare(FROM_WALLS, {**WALL_ARGS, 'wall_handles': ['a1', 'b2']})
                         ['wall_handles'], ['A1', 'B2'])

    def run_rect(self, cad, args):
        with (patch.multiple(panel, **cad.backend.window_patches()),
              patch('tz_mcp.input_bridge.NativeInput', return_value=cad.backend),
              patch('tz_mcp.cad.require_drawing_window'),
              patch('tz_mcp.cad.require_model_space'),
              patch('tz_mcp.parts.require_drawing_window'),
              patch('tz_mcp.parts.require_model_space'),
              patch.object(panel.time, 'sleep')):
            return Cad.execute(cad, RECT, args)

    def test_axis_foreground_retries_until_third_attempt(self):
        cad = FakeCad({}, {})
        backend = cad.backend
        external = 3000
        backend.foreground = external
        activate = backend.SetForegroundWindow
        windows = {**backend.window_patches(),
                   'class_name': lambda *_: panel.PANEL_CLASS,
                   'window_pid': lambda _, hwnd: 2 if hwnd == external else cad.pid}
        with (patch.multiple(panel, **windows),
              patch.object(panel.time, 'sleep'),
              patch.object(backend, 'SetForegroundWindow') as set_foreground):
            set_foreground.side_effect = lambda hwnd: (
                activate(hwnd) if set_foreground.call_count == 3 else False)
            panel._axis_foreground(backend, cad.pid, backend.hwnd)
        self.assertEqual(set_foreground.call_count, 3)
        self.assertEqual(backend.foreground, backend.hwnd)

    def test_axis_panel_with_cad_frame_in_foreground_can_type(self):
        cad = FakeCad({}, {})
        backend = cad.backend
        control = panel.AXIS_ANGLE_ID
        with (patch.multiple(panel, **backend.window_patches()),
              patch.object(panel, 'class_name', return_value=panel.PANEL_CLASS),
              patch.object(panel.time, 'sleep'),
              patch.object(backend, 'SetForegroundWindow', return_value=False) as activate):
            panel._axis_foreground(backend, cad.pid, backend.hwnd)
            self.assertEqual(activate.call_count, 2)
            panel._axis_edit(backend, cad.pid, backend.hwnd, control, '90',
                             lambda: backend.send(0x09, 0, 0))
        self.assertEqual(backend.foreground, cad.hwnd)
        self.assertEqual(backend.focus_handle(control), control)
        self.assertEqual(cad.events, [('angle', '90')])

    def test_axis_foreground_can_activate_panel_by_title_bar(self):
        cad = FakeCad({}, {})
        backend = cad.backend
        external = 3000
        backend.foreground = external
        windows = {**backend.window_patches(),
                   'class_name': lambda *_: panel.PANEL_CLASS,
                   'window_pid': lambda _, hwnd: 2 if hwnd == external else cad.pid,
                   'rect': lambda *_: (100, 200, 500, 600)}
        with (patch.multiple(panel, **windows),
              patch.object(panel.time, 'sleep'),
              patch.object(backend, 'SetForegroundWindow', return_value=False) as activate,
              patch.object(backend, 'WindowFromPoint', create=True,
                           return_value=backend.hwnd),
              patch.object(backend, 'click',
                           side_effect=lambda *_: setattr(backend, 'foreground', backend.hwnd)) as click):
            panel._axis_foreground(backend, cad.pid, backend.hwnd)
        self.assertEqual(activate.call_count, 2)
        click.assert_called_once_with(300, 208)

    def test_axis_rejects_foreground_from_another_process(self):
        cad = FakeCad({}, {})
        backend = cad.backend
        external = 3000
        backend.foreground = external
        backend.focus = panel.AXIS_ANGLE_ID
        windows = {**backend.window_patches(),
                   'window_pid': lambda _, hwnd: 2 if hwnd == external else cad.pid}
        with (patch.multiple(panel, **windows),
              patch.object(backend, 'SetForegroundWindow') as activate,
              patch.object(backend, 'select_all') as select_all,
              patch.object(backend, 'unicode') as send_char):
            with self.assertRaises(panel.PanelTimeout):
                panel._axis_foreground(backend, cad.pid, backend.hwnd, timeout=0)
            with self.assertRaisesRegex(panel.PanelTimeout, 'Axis command keyboard focus lost'):
                commit = Mock()
                panel._axis_type(backend, backend.focus, backend.hwnd, '90', commit, replace=True)
        activate.assert_not_called()
        select_all.assert_not_called()
        send_char.assert_not_called()
        commit.assert_not_called()

    def test_axis_view_preserves_keyboard_focus_inside_owned_wpf_root(self):
        cad = FakeCad({}, {})
        backend = cad.backend
        backend.foreground = backend.floating_command_line
        backend.focus = 1114
        backend.window_owners = {backend.foreground: 3000, 3000: cad.hwnd}
        get_ancestor = Mock(return_value=backend.foreground)
        with (patch.multiple(panel, **backend.window_patches()),
              patch.object(backend, 'GetAncestor', get_ancestor, create=True),
              patch.object(backend, 'SetForegroundWindow') as activate,
              patch.object(backend, 'SetFocus') as set_focus):
            self.assertEqual(panel._focus_axis_view(cad, backend), (backend.view, backend.focus))
        activate.assert_not_called()
        set_focus.assert_not_called()
        self.assertTrue(get_ancestor.call_args_list)
        for hwnd, flag in (call.args for call in get_ancestor.call_args_list):
            self.assertEqual((hwnd.value, flag), (backend.focus, 2))

    def test_axis_owned_wpf_owner_search_is_bounded_to_eight_steps(self):
        cad = FakeCad({}, {})
        backend = cad.backend
        popup = backend.floating_command_line
        with patch.multiple(panel, **backend.window_patches()):
            for depth in (8, 9):
                with self.subTest(depth=depth):
                    chain = [popup, *range(3000, 3000 + depth - 1), cad.hwnd]
                    backend.window_owners = dict(zip(chain, chain[1:]))
                    with patch.object(backend, 'GetWindow', wraps=backend.GetWindow) as get_owner:
                        self.assertEqual(panel._axis_focus_owned_wpf(backend, cad.hwnd, popup,
                                                                    cad.pid), depth == 8)
                    self.assertEqual(get_owner.call_count, 8)

    @unittest.skipUnless(ctypes.sizeof(W.HWND) == 8, 'requires 64-bit HWNDs')
    def test_axis_owned_wpf_preserves_64_bit_window_handles(self):
        frame, root, focus = 0x100000001, 0x200000002, 0x300000003
        window_function = ctypes.CFUNCTYPE(W.HWND, W.HWND, W.UINT)
        get_ancestor = window_function(lambda hwnd, flag: root if (hwnd, flag) == (focus, 2) else 0)
        get_window = window_function(lambda hwnd, flag: frame if (hwnd, flag) == (root, 4) else 0)
        for function in (get_ancestor, get_window):
            function.argtypes = None
            function.restype = ctypes.c_int
        u = SimpleNamespace(GetAncestor=get_ancestor, GetWindow=get_window)
        with (patch.object(panel, 'window_pid', return_value=1) as pid,
              patch.object(panel, 'class_name', return_value='HwndWrapper[DefaultDomain;;test]')):
            self.assertTrue(panel._axis_focus_owned_wpf(u, frame, focus, 1))
        pid.assert_called_once_with(u, root)
        for function in (get_ancestor, get_window):
            self.assertEqual(function.argtypes, [W.HWND, W.UINT])
            self.assertIs(function.restype, W.HWND)

    def test_axis_coordinate_focus_loss_reports_the_blocked_key(self):
        for leaked_focus, leaked_foreground, foreign, text, idx in (
            (3000, 1000, None, '90', 1),
            (1100, 3000, 3000, '90', 1),
            (1113, 1000, 1113, '9', 'commit'),
            (None, None, None, '9', 'commit'),
        ):
            with self.subTest(focus=leaked_focus, foreground=leaked_foreground, idx=idx):
                cad = FakeCad({}, {})
                backend = cad.backend
                backend.focus = backend.view

                def drift(_):
                    backend.foreground = leaked_foreground
                    backend.focus = leaked_focus

                def pid(_, hwnd):
                    return 2 if hwnd == foreign else cad.pid

                commit = Mock()
                with (patch.object(panel, 'window_pid', side_effect=pid),
                      patch.object(panel, 'class_name', side_effect=backend.class_name),
                      patch.object(backend, 'focus_handle', wraps=backend.focus_handle) as focus,
                      patch.object(backend, 'unicode', side_effect=drift) as send_char,
                      self.assertRaises(panel.PanelTimeout) as error):
                    panel._axis_type(backend, backend.view, cad.hwnd, text, commit,
                                     allow_child_focus=True, stage='axis_point[0]')
                send_char.assert_called_once_with('9')
                commit.assert_not_called()
                self.assertTrue(all(call.args == (cad.hwnd,) for call in focus.call_args_list))
                self.assertEqual(str(error.exception),
                                 'Axis command keyboard focus lost '
                                 f'(stage=axis_point[0], char_idx={idx}, '
                                 f'fg={leaked_foreground or 0:#x}, '
                                 f'fg_pid={pid(None, leaked_foreground) if leaked_foreground else 0}, '
                                 f'expected_pid={cad.pid}, target={backend.view:#x}, '
                                 f'actual_focus={leaked_focus or 0:#x}, allow_child_focus=True, '
                                 f'is_child={leaked_focus in (backend.view, backend.command_line)}, '
                                 'is_owned_wpf=False)')

    def test_axis_coordinate_rejects_dialog_unowned_wpf_and_cross_process_wpf(self):
        for popup_class, owned, same_pid in (
            ('#32770', True, True),
            ('HwndWrapper[Other;;1]', False, True),
            ('HwndWrapper[Other;;1]', True, False),
        ):
            with self.subTest(popup_class=popup_class, owned=owned, same_pid=same_pid):
                cad = FakeCad({}, {})
                backend = cad.backend
                popup = backend.floating_command_line
                backend.focus = backend.view
                backend.text[backend.view] = ''
                backend.window_owners[popup] = cad.hwnd if owned else 0
                backend.float_command_line_on_type = True

                def class_name(u, hwnd):
                    return popup_class if hwnd == popup else backend.class_name(u, hwnd)

                def pid(_, hwnd):
                    return 2 if hwnd == popup and not same_pid else cad.pid

                windows = {**backend.window_patches(), 'class_name': class_name, 'window_pid': pid}
                commit = Mock()
                with (patch.multiple(panel, **windows),
                      patch.object(backend, 'unicode', wraps=backend.unicode) as send_char,
                      self.assertRaisesRegex(
                          panel.PanelTimeout,
                          r'^Axis command keyboard focus lost \(stage=axis_point\[0\], char_idx=1,'
                          r'.*is_child=False, is_owned_wpf=False\)$')):
                    panel._axis_type(backend, backend.view, cad.hwnd, '90', commit,
                                     allow_child_focus=True, stage='axis_point[0]')
                send_char.assert_called_once_with('9')
                commit.assert_not_called()

    def test_axis_edit_rejects_focus_on_another_cad_child(self):
        cad = FakeCad({}, {})
        backend = cad.backend
        control = panel.AXIS_ANGLE_ID
        commit = Mock()
        with (patch.multiple(panel, **backend.window_patches()),
              patch.object(panel.time, 'sleep'),
              patch.object(backend, 'unicode', side_effect=lambda _: backend.focus_command_line())
              as send_char,
              self.assertRaisesRegex(panel.PanelTimeout,
                                     r'^Axis command keyboard focus lost \(stage=axis_edit, char_idx=1,'
                                     r'.*allow_child_focus=False, is_child=False,'
                                     r' is_owned_wpf=False\)$')):
            panel._axis_edit(backend, cad.pid, backend.hwnd, control, '90', commit)
        send_char.assert_called_once_with('9')
        commit.assert_not_called()

    def test_axis_edit_rejects_focus_on_its_owned_wpf_window(self):
        cad = FakeCad({}, {})
        backend = cad.backend
        backend.window_owners[backend.floating_command_line] = backend.hwnd
        commit = Mock()
        with (patch.multiple(panel, **backend.window_patches()),
              patch.object(panel.time, 'sleep'),
              patch.object(backend, 'unicode',
                           side_effect=lambda _: backend.focus_command_line(floating=True))
              as send_char,
              self.assertRaisesRegex(
                  panel.PanelTimeout,
                  r'^Axis command keyboard focus lost \(stage=axis_edit, char_idx=1,'
                  r'.*allow_child_focus=False, is_child=False, is_owned_wpf=False\)$')):
            panel._axis_edit(backend, cad.pid, backend.hwnd, panel.AXIS_ANGLE_ID, '90', commit)
        send_char.assert_called_once_with('9')
        commit.assert_not_called()

    def test_axis_edit_retries_a_foreground_change_after_focusing(self):
        for recover in (True, False):
            with self.subTest(recover=recover):
                cad = FakeCad({}, {})
                backend = cad.backend
                control = panel.AXIS_ANGLE_ID
                external = 3000
                focus_control = backend.focus_control

                def focus(hwnd):
                    if attempt.call_count == 1 or not recover:
                        backend.foreground = external
                    return focus_control(hwnd)

                windows = {**backend.window_patches(),
                           'window_pid': lambda _, hwnd: 2 if hwnd == external else cad.pid}
                commit = Mock()
                with (patch.multiple(panel, **windows),
                      patch.object(panel.time, 'sleep'),
                      patch.object(backend, 'focus_control', side_effect=focus) as attempt,
                      patch.object(backend, 'select_all', wraps=backend.select_all) as select_all):
                    if recover:
                        panel._axis_edit(backend, cad.pid, backend.hwnd, control, '90', commit)
                        select_all.assert_called_once_with()
                        commit.assert_called_once_with()
                        self.assertEqual(backend.text[control], '90')
                    else:
                        with self.assertRaisesRegex(panel.PanelTimeout, 'foreground after retry'):
                            panel._axis_edit(backend, cad.pid, backend.hwnd, control, '90', commit)
                        select_all.assert_not_called()
                        commit.assert_not_called()
                        self.assertEqual(backend.text[control], '60')
                self.assertEqual(attempt.call_count, 2)

    def test_rectangular_grid_panel_order_and_geometry(self):
        args = prepare(RECT, RECT_ARGS)
        cad = FakeCad({}, axis_grid(args))
        result = self.run_rect(cad, args)
        expected_events = [('osmode', 0), ('dispatch', 'TRectAxis\r'), ('angle', '90')]
        for ctrl in (178, 179, 180, 181):
            expected_events.extend((('click', ctrl), ('click', 183)))
        expected_events.extend([
            ('bays_total', 0), ('depth_total', 0), ('click', 179),
            ('spacing', 179, '3600 4200'), ('bays_total', 7800), ('depth_total', 0),
            ('click', 180), ('spacing', 180, '4500 3000'), ('bays_total', 7800),
            ('depth_total', 7500), ('focus_view', 1100), ('placement', '10000.0000,20000.0000'),
            ('esc', 2), ('osmode', 16383),
        ])
        self.assertEqual(cad.events, expected_events)
        axis = result['evidence']['axis']
        self.assertEqual(axis['vertical_x'], [10000, 13600, 17800])
        self.assertEqual(axis['horizontal_y'], [20000, 24500, 27500])
        self.assertEqual(result['handles'], [f'B{i}' for i in range(6)])
        self.assertEqual(result['entities'], [{'object_name': 'AcDbLine', 'layer': 'DOTE'}] * 6)
        self.assertEqual(result['validation']['geometry'], 'verified_dote_lines')

    def test_skew_grid_spacing_and_direction(self):
        args = prepare(RECT, {**RECT_ARGS, 'bays': [5000], 'depths': [6000], 'axis_angle': 60})
        cad = FakeCad({}, axis_grid(args))
        cad.backend.keyboard_override = 1112
        result = self.run_rect(cad, args)
        axis = result['evidence']['axis']
        self.assertEqual(axis['panel']['view_hwnd'], 1100)
        self.assertEqual(axis['panel']['keyboard_hwnd'], 1112)
        self.assertIn(('angle', '60'), cad.events)
        self.assertEqual(axis['vertical_x'], [10000, 15000])
        self.assertEqual(axis['line_count'], 4)
        self.assertEqual(axis['families']['skew']['angle_deg'], 30)
        self.assertAlmostEqual(axis['families']['skew']['normal_spacings'][0], 6000)

    def test_rectangular_grid_origin_accepts_command_line_focus(self):
        args = prepare(RECT, RECT_ARGS)
        cad = FakeCad({}, axis_grid(args))
        backend = cad.backend
        send_char = backend.unicode

        def enter_char(ch):
            send_char(ch)
            if backend.focus == backend.view:
                backend.focus_command_line()

        with patch.object(backend, 'unicode', side_effect=enter_char):
            result = self.run_rect(cad, args)
        self.assertIn(('placement', '10000.0000,20000.0000'), cad.events)
        self.assertEqual(result['validation']['geometry'], 'verified_dote_lines')

    def test_rectangular_grid_origin_accepts_owned_wpf_command_line_focus(self):
        args = prepare(RECT, RECT_ARGS)
        cad = FakeCad({}, axis_grid(args))
        backend = cad.backend
        backend.float_command_line_on_type = True

        result = self.run_rect(cad, args)

        self.assertIn(('placement', '10000.0000,20000.0000'), cad.events)
        self.assertEqual(cad.events.count(('focus_view', backend.view)), 1)
        self.assertEqual(result['validation']['geometry'], 'verified_dote_lines')

    def test_rectangular_grid_failures_close_panel_and_restore_snap(self):
        args = prepare(RECT, RECT_ARGS)
        for bad in ('clear', 'bays', 'count', 'layer', 'type', 'drift', 'length', 'removed'):
            cad = FakeCad({}, axis_grid(args))
            if bad == 'clear':
                cad.backend.bad_clear = True
            elif bad == 'bays':
                cad.backend.bad_bays = True
            elif bad == 'count':
                del cad.after['B0']
            elif bad == 'layer':
                cad.after['B0'].Layer = '0'
            elif bad == 'type':
                cad.after['B0'].ObjectName = 'AcDbPolyline'
            elif bad == 'drift':
                cad.after['B0'] = line((10000.1, 19000), (10000.1, 28500))
            elif bad == 'length':
                cad.after['B0'] = line((10000, 19000), (10000, 19100))
            else:
                cad.before['A0'] = wall()
            with self.subTest(bad=bad), self.assertRaises(OutcomeUnknown):
                self.run_rect(cad, args)
            self.assertEqual(cad.events[-2:], [('esc', 2), ('osmode', 16383)])

    def wall_fixture(self):
        before = {h: wall() for h in WALL_ARGS['wall_handles']}
        after = {h: wall() for h in before}
        after.update(axis_grid(prepare(RECT, {**RECT_ARGS, 'bays': [6000], 'depths': [4000]})))
        return FakeCad(before, after)

    def test_axis_from_walls_preselects_exact_wall_handles(self):
        cad = self.wall_fixture()
        with (patch('tz_mcp.cad.require_drawing_window'), patch('tz_mcp.cad.require_model_space'),
              patch('tz_mcp.parts.require_drawing_window'), patch('tz_mcp.parts.require_model_space')):
            result = Cad.execute(cad, FROM_WALLS, prepare(FROM_WALLS, WALL_ARGS))
        self.assertEqual(cad.commands, [
            '(progn (setq _ss (ssadd)) (ssadd (handent "A1") _ss) (ssadd (handent "A2") _ss) '
            '(ssadd (handent "A3") _ss) (ssadd (handent "A4") _ss) (sssetfirst nil _ss) (princ))\n'
            'TWall2Axis\n',
        ])
        self.assertEqual(result['handles'], ['B0', 'B1', 'B2', 'B3'])
        evidence = result['evidence']['axis_from_walls']
        self.assertTrue(evidence['walls_unchanged'])
        self.assertEqual(evidence['wall_handles'], WALL_ARGS['wall_handles'])
        self.assertEqual(evidence['lines'][0],
                         {'handle': 'B0', 'start': [10000, 19000], 'end': [10000, 25000],
                          'length': 6000})

    def test_axis_from_walls_rejects_invalid_sources_before_dispatch(self):
        for bad in ('missing', 'type', 'document'):
            cad = self.wall_fixture()
            args = dict(WALL_ARGS)
            if bad == 'missing':
                del cad.before['A1']
            elif bad == 'type':
                cad.before['A1'].ObjectName = 'TDbColumn'
            else:
                args['document_id'] = 'unowned'
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parts.axis_from_walls(cad, args, cad.before)
            self.assertEqual(cad.commands, [])

    def test_axis_from_walls_rejects_invalid_readback(self):
        for bad in ('no lines', 'layer', 'type', 'zero length', 'removed',
                    *parts._AXIS_WALL_TOLERANCES):
            cad = self.wall_fixture()
            if bad == 'no lines':
                cad.after = cad.before.copy()
            elif bad == 'layer':
                cad.after['B0'].Layer = '0'
            elif bad == 'type':
                cad.after['B0'].ObjectName = 'AcDbPolyline'
            elif bad == 'zero length':
                cad.after['B0'].EndPoint = cad.after['B0'].StartPoint
            elif bad == 'removed':
                del cad.after['A1']
            else:
                setattr(cad.after['A1'], bad, getattr(cad.after['A1'], bad) + 1)
            with self.subTest(bad=bad), self.assertRaises(OutcomeUnknown):
                parts.axis_from_walls(cad, WALL_ARGS, cad.before)
            self.assertEqual(len(cad.commands), 1)


if __name__ == '__main__':
    unittest.main()

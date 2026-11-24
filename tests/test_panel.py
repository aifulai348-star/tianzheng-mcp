import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tz_mcp import panel
from tz_mcp.cad import CommandCompletionTimeout


class FakeBackend:
    def __init__(self):
        self.u = object()
        self.calls = []

    def activate_view(self, pid, hwnd):
        self.calls.append(('activate', hwnd))

    def click(self, x, y):
        self.calls.append(('click', x, y))

    def focus_control(self, hwnd):
        self.calls.append(('focus', hwnd))
        return True

    def esc(self, n=1):
        self.calls.append(('esc', n))

    def select_all(self):
        self.calls.append(('select_all',))

    def unicode(self, ch):
        self.calls.append(('char', ch))

    def enter(self):
        self.calls.append(('enter',))


class FakeDoc:
    def __init__(self, viewctr=(1000.0, 500.0, 0.0), viewsize=1000.0, cmdactive=0):
        self.posted = []
        self.vars = {'VIEWCTR': viewctr, 'VIEWSIZE': viewsize, 'CMDACTIVE': cmdactive,
                     'OSMODE': 16383, 'VTENABLE': 0}

    def PostCommand(self, text):
        self.posted.append(text)

    def GetVariable(self, name):
        return self.vars[name]


def fake_session(doc=None):
    session = type('FakeCad', (), {})()
    session.pid = 42
    session.hwnd = 7
    session.doc = doc or FakeDoc()
    session.commands = []
    session.run_command = session.commands.append
    return session


class MappingTests(unittest.TestCase):
    def test_world_to_screen_uses_view_centre_and_height(self):
        rect = (0, 0, 100, 100)
        self.assertEqual((50.0, 50.0), panel.world_to_screen((0, 0), 1000.0, rect, (0, 0)))
        self.assertEqual((70.0, 30.0), panel.world_to_screen((1000, 500), 1000.0, rect, (1200, 700)))
        with self.assertRaises(panel.PanelTimeout):
            panel.world_to_screen((0, 0), 1000.0, rect, (600, 0))

    def test_screen_to_world_inverts_the_mapping(self):
        rect = (0, 0, 100, 100)
        screen = panel.world_to_screen((1000, 500), 1000.0, rect, (1200, 700))
        world = panel.screen_to_world((1000, 500), 1000.0, rect, *screen)
        self.assertAlmostEqual(1200.0, world[0])
        self.assertAlmostEqual(700.0, world[1])

    def test_degenerate_viewport_is_rejected(self):
        with self.assertRaises(panel.PanelTimeout):
            panel.world_to_screen((0, 0), 1000.0, (0, 0, 100, 0), (0, 0))


class FieldTests(unittest.TestCase):
    KIDS = {100: [101, 102], 101: [103, 104], 102: [105, 107], 103: [], 104: [], 105: [], 107: []}
    CLASSES = {100: '#32770', 101: '#32770', 102: '#32770', 103: 'Button', 104: 'Edit',
               105: 'Button', 107: 'Edit'}
    TITLES = {101: '横向', 102: '柱高'}

    def test_field_edit_targets_the_row_titled_like_the_label(self):
        with patch('tz_mcp.panel.children', lambda u, h: self.KIDS.get(h, [])), \
             patch('tz_mcp.panel.class_name', lambda u, h: self.CLASSES.get(h, '')), \
             patch('tz_mcp.panel.title', lambda u, h: self.TITLES.get(h, '')), \
             patch('tz_mcp.panel.visible', lambda u, h: True):
            self.assertEqual(104, panel.field_edit(None, 100, '横向'))
            self.assertEqual(107, panel.field_edit(None, 100, '柱高'))
            self.assertIsNone(panel.field_edit(None, 100, '纵向'))

    def test_set_field_focuses_types_and_checks_readback(self):
        backend = FakeBackend()
        with patch('tz_mcp.panel.text_of', lambda u, h: '700'):
            self.assertEqual('700', panel.set_field(backend, 5, 700))
        self.assertEqual([('focus', 5), ('select_all',), ('char', '7'), ('char', '0'), ('char', '0')],
                         backend.calls)

    def test_set_field_raises_when_the_panel_keeps_the_old_value(self):
        with patch('tz_mcp.panel.text_of', lambda u, h: '600'):
            with self.assertRaises(panel.PanelTimeout):
                panel.set_field(FakeBackend(), 5, 700)

    def test_set_field_refuses_when_focus_is_lost(self):
        backend = FakeBackend()
        backend.focus_control = lambda hwnd: False
        with self.assertRaises(panel.PanelTimeout):
            panel.set_field(backend, 5, 700)


class CreateColumnTests(unittest.TestCase):
    def test_panel_flow_sets_fields_clicks_once_and_returns_placement(self):
        backend = FakeBackend()
        session = fake_session()
        seen = {'n': 0}

        def fake_panels(u, pid):
            seen['n'] += 1
            if seen['n'] == 1 or any(c[0] == 'esc' for c in backend.calls):
                return []
            return [555]

        fields = []
        with patch('tz_mcp.panel.panels', fake_panels), \
             patch('tz_mcp.panel.field_edit', lambda u, p, label: {'横向': 11, '纵向': 12, '柱高': 13}[label]), \
             patch('tz_mcp.panel.set_field', lambda b, h, v: fields.append((h, v))), \
             patch('tz_mcp.panel.viewport', lambda u, h, p: (0, 0, 100, 100)), \
             patch('tz_mcp.panel.time.sleep', lambda s: None):
            out = panel.create_column(session, {'point': [1200, 700], 'width': 700, 'depth': 500,
                                                'height': 3200}, backend=backend)
        # The command is typed into the view, not posted: PostCommand leaves
        # CMDACTIVE at 0 and no panel appears.
        self.assertEqual([], session.doc.posted)
        typed = [c[1] for c in backend.calls if c[0] == 'char']
        self.assertEqual(list('TGColumn'), typed)
        self.assertIn(('enter',), backend.calls)
        self.assertEqual([(11, 700), (12, 500), (13, 3200)], fields)
        self.assertIn(('click', 70, 30), backend.calls)
        self.assertIn(('esc', 2), backend.calls)
        self.assertEqual((1200.0, 700.0), out['world'])

    def test_object_snap_is_switched_off_and_restored_around_the_placement(self):
        backend = FakeBackend()
        session = fake_session()
        seen = {'n': 0}

        def fake_panels(u, pid):
            seen['n'] += 1
            return [] if seen['n'] == 1 or any(c[0] == 'esc' for c in backend.calls) else [555]

        with patch('tz_mcp.panel.panels', fake_panels), \
             patch('tz_mcp.panel.field_edit', lambda u, p, label: 11), \
             patch('tz_mcp.panel.set_field', lambda b, h, v: None), \
             patch('tz_mcp.panel.viewport', lambda u, h, p: (0, 0, 100, 100)), \
             patch('tz_mcp.panel.time.sleep', lambda s: None):
            panel.create_column(session, {'point': [1200, 700], 'width': 700, 'depth': 500,
                                          'height': 3200}, backend=backend)
        self.assertEqual(['(setvar "OSMODE" 0)\n', '(setvar "OSMODE" 16383)\n'], session.commands)

    def test_object_snap_is_restored_even_when_the_panel_never_appears(self):
        backend = FakeBackend()
        session = fake_session()
        with patch('tz_mcp.panel.panels', lambda u, pid: []), \
             patch('tz_mcp.panel.viewport', lambda u, h, p: (0, 0, 100, 100)), \
             patch('tz_mcp.panel.time.sleep', lambda s: None):
            with self.assertRaises(CommandCompletionTimeout):
                panel.create_column(session, {'point': [0, 0], 'width': 400, 'depth': 400,
                                              'height': 3000}, backend=backend, timeout=0.05)
        self.assertEqual(['(setvar "OSMODE" 0)\n', '(setvar "OSMODE" 16383)\n'], session.commands)

    def test_missing_panel_raises_a_timeout_and_never_places(self):
        backend = FakeBackend()
        with patch('tz_mcp.panel.panels', lambda u, pid: []), \
             patch('tz_mcp.panel.viewport', lambda u, h, p: (0, 0, 100, 100)), \
             patch('tz_mcp.panel.time.sleep', lambda s: None):
            with self.assertRaises(CommandCompletionTimeout):
                panel.create_column(fake_session(), {'point': [0, 0], 'width': 400, 'depth': 400,
                                                     'height': 3000}, backend=backend, timeout=0.05)
        # Only the focus click in the middle of the view is allowed; no click
        # may be made at the requested placement point.
        self.assertEqual([('click', 50, 50)],
                         [c for c in backend.calls if c[0] == 'click'])


class DxfTests(unittest.TestCase):
    GOOD = {'type': 'TCH_COLUMN', 'handle': '3E3', 'layer': 'COLUMN',
            'vertices': [(0.0, 0.0), (700.0, 0.0), (700.0, 500.0), (0.0, 500.0)],
            'insertion': (1200.4, 700.3), 'height': 3200.0, 'flags': (1, 60, 0)}
    ARGS = {'point': [1200, 700], 'width': 700, 'depth': 500, 'height': 3200}
    PLACEMENT = {'world': (1200.0, 700.0)}

    def test_rectangular_standard_column_passes(self):
        out = panel.verify_column_dxf(self.GOOD, self.ARGS, self.PLACEMENT)
        self.assertEqual([700.0, 500.0], out['outline'])
        self.assertEqual([1, 60, 0], out['flags'])

    def test_other_shapes_and_parameters_are_rejected(self):
        corner = dict(self.GOOD, vertices=[(0, 0)] * 12, flags=(0, 60, 0))
        wide = dict(self.GOOD, vertices=[(0.0, 0.0), (600.0, 0.0), (600.0, 500.0), (0.0, 500.0)])
        tall = dict(self.GOOD, height=3000.0)
        far = dict(self.GOOD, insertion=(1300.0, 700.0))
        for bad in (corner, wide, tall, far):
            with self.assertRaises(Exception):
                panel.verify_column_dxf(bad, self.ARGS, self.PLACEMENT)
        with self.assertRaises(Exception):
            panel.verify_column_dxf(dict(self.GOOD, type='TCH_WALL'), self.ARGS, self.PLACEMENT)

    def test_dump_is_requested_by_handle_and_parsed(self):
        folder = tempfile.mkdtemp()
        dump = ('(0 . TCH_COLUMN) (5 . 3E3) (8 . COLUMN) (11 1200.4 700.3 0.0) '
                '(149 . 3200.0) (70 . 1) (71 . 60) (72 . 0) '
                '(10 850.0 450.0 0.0) (10 1550.0 450.0 0.0) (10 1550.0 950.0 0.0) (10 850.0 950.0 0.0)')

        class FakeCad:
            def __init__(self):
                self.sent = []

            def run_command(self, payload):
                self.sent.append(payload)
                Path(folder, 'dxf-3E3.txt').write_text(dump, encoding='gbk')

        c = FakeCad()
        out = panel.column_dxf(c, '3E3', folder=folder)
        self.assertIn('handent "3E3"', c.sent[0])
        self.assertEqual('TCH_COLUMN', out['type'])
        self.assertEqual(4, len(out['vertices']))
        self.assertEqual(3200.0, out['height'])
        self.assertEqual((1, 60, 0), out['flags'])
        self.assertEqual((1200.4, 700.3), out['insertion'])

    def test_missing_dump_raises(self):
        class Slow:
            def run_command(self, payload):
                pass

        with self.assertRaises(Exception):
            panel.column_dxf(Slow(), '3E3', timeout=0.05, folder=tempfile.mkdtemp())


class RowEditTests(unittest.TestCase):
    PANEL = 900
    KIDS = {900: [1, 2, 3, 4, 5, 6, 7], 1: [], 2: [], 3: [], 4: [], 5: [], 6: [], 7: []}
    RECTS = {1: (100, 50, 200, 68), 2: (205, 52, 300, 72), 3: (100, 90, 200, 108),
             4: (205, 92, 300, 112), 5: (400, 50, 500, 68), 6: (100, 130, 200, 148),
             7: (205, 132, 300, 152)}
    CLASSES = {1: 'Static', 2: 'Edit', 3: 'Static', 4: 'Edit', 5: 'ComboBox', 6: 'Static',
               7: 'Edit'}
    TITLES = {1: '门\ue5e5宽', 3: '门\ue5e5高', 5: '普通门', 6: '门槛高'}

    def test_row_edit_matches_label_across_the_private_use_glyph(self):
        with patch('tz_mcp.panel.children', lambda u, h: self.KIDS.get(h, [])), \
             patch('tz_mcp.panel.class_name', lambda u, h: self.CLASSES.get(h, '')), \
             patch('tz_mcp.panel.title', lambda u, h: self.TITLES.get(h, '')), \
             patch('tz_mcp.panel.rect', lambda u, h: self.RECTS.get(h, (0, 0, 0, 0))), \
             patch('tz_mcp.panel.visible', lambda u, h: True):
            self.assertEqual(2, panel.row_edit(None, self.PANEL, '门洞宽'))
            self.assertEqual(4, panel.row_edit(None, self.PANEL, '门洞高'))
            self.assertEqual(7, panel.row_edit(None, self.PANEL, '门槛高'))
            self.assertIsNone(panel.row_edit(None, self.PANEL, '门洞深'))

    def test_opening_kind_reads_the_selected_door_type(self):
        with patch('tz_mcp.panel.children', lambda u, h: self.KIDS.get(h, [])), \
             patch('tz_mcp.panel.class_name', lambda u, h: self.CLASSES.get(h, '')), \
             patch('tz_mcp.panel.visible', lambda u, h: True), \
             patch('tz_mcp.panel.text_of', lambda u, h: '普通门' if h == 5 else ''):
            self.assertEqual('普通门', panel.opening_kind(None, self.PANEL))
        with patch('tz_mcp.panel.children', lambda u, h: self.KIDS.get(h, [])), \
             patch('tz_mcp.panel.class_name', lambda u, h: self.CLASSES.get(h, '')), \
             patch('tz_mcp.panel.visible', lambda u, h: True), \
             patch('tz_mcp.panel.text_of', lambda u, h: ''):
            self.assertIsNone(panel.opening_kind(None, self.PANEL))


class CreateOpeningTests(unittest.TestCase):
    ARGS = {'point': [1200, 700], 'kind': '普通门', 'subkind': '普通门',
            'width': 900, 'height': 2100, 'sill': 0}
    WINDOW_ARGS = {'point': [1200, 700], 'kind': '普通窗', 'subkind': '普通窗',
                   'width': 1200, 'height': 1600, 'sill': 600}

    def _run(self, kind='普通门', args=None):
        backend = FakeBackend()
        session = fake_session()
        fields, typed, calls = [], [], {'n': 0}

        def fake_panels(u, pid):
            # Nothing is left over from a previous attempt, then the palette.
            calls['n'] += 1
            return [] if calls['n'] == 1 else [555]

        with patch('tz_mcp.panel.panels', fake_panels), \
             patch('tz_mcp.panel._type_command',
                   lambda b, pid, hwnd, rect, name: typed.append(name)), \
             patch('tz_mcp.panel.opening_kind', lambda u, p: kind), \
             patch('tz_mcp.panel.value_row_edit', lambda u, p, i: 20 + i), \
             patch('tz_mcp.panel.set_field', lambda b, h, v: fields.append((h, v))), \
             patch('tz_mcp.panel.viewport', lambda u, h, p: (0, 0, 100, 100)), \
             patch('tz_mcp.panel.time.sleep', lambda s: None):
            out = panel.create_opening(session, args or self.ARGS, backend=backend)
        return backend, session, fields, typed, out

    def test_door_flow_types_tgdoor_and_fills_the_size_rows(self):
        backend, session, fields, typed, out = self._run()
        # Doors go through TGDOOR, not TOpening: the flat 门窗参数 panel keeps
        # the mode it was last left in, so a recipe that mixes doors and
        # windows would be refused after the first window.
        self.assertEqual(['TGDOOR'], typed)
        self.assertEqual([], session.doc.posted)
        self.assertEqual([(20, 900), (21, 2100), (22, 0)], fields)
        self.assertIn(('click', 70, 30), backend.calls)
        self.assertEqual('普通门', out['kind'])
        self.assertEqual((1200.0, 700.0), out['world'])
        self.assertEqual(['(setvar "OSMODE" 0)\n', '(setvar "OSMODE" 16383)\n'], session.commands)

    def test_a_palette_set_to_another_type_is_refused_before_any_click(self):
        backend = FakeBackend()
        session = fake_session()
        calls = {'n': 0}

        def fake_panels(u, pid):
            calls['n'] += 1
            return [] if calls['n'] == 1 else [555]

        with patch('tz_mcp.panel.panels', fake_panels), \
             patch('tz_mcp.panel._type_command', lambda b, pid, hwnd, rect, name: None), \
             patch('tz_mcp.panel.opening_kind', lambda u, p: '甲级防火门'), \
             patch('tz_mcp.panel.viewport', lambda u, h, p: (0, 0, 100, 100)), \
             patch('tz_mcp.panel.time.sleep', lambda s: None):
            with self.assertRaises(Exception):
                panel.create_opening(session, self.ARGS, backend=backend)
        self.assertFalse([c for c in backend.calls if c[0] == 'click'])
        self.assertIn(('esc', 2), backend.calls)

    def test_window_flow_types_tgwindow_and_fills_the_size_rows(self):
        backend, session, fields, typed, out = self._run(kind='普通窗', args=self.WINDOW_ARGS)
        # TGWINDOW is a different inserter with a different palette; posting
        # TOpening for a window would silently create a door-shaped hole.
        self.assertEqual(['TGWINDOW'], typed)
        self.assertEqual([], session.doc.posted)
        self.assertEqual([(20, 1200), (21, 1600), (22, 600)], fields)
        self.assertIn(('click', 70, 30), backend.calls)
        self.assertEqual('普通窗', out['kind'])
        self.assertEqual(['(setvar "OSMODE" 0)\n', '(setvar "OSMODE" 16383)\n'], session.commands)



class OpeningDxfTests(unittest.TestCase):
    GOOD = {'type': 'TCH_OPENING', 'handle': '45A', 'layer': 'WINDOW',
            'width': 1200.0, 'height': 2100.0, 'rotation': 0.0,
            'insertion': (1200.4, 700.3), 'host': '3A7'}
    ARGS = {'point': [1200, 700], 'width': 1200, 'height': 2100}
    PLACEMENT = {'world': (1200.0, 700.0)}

    def test_door_on_the_referenced_wall_passes(self):
        out = panel.verify_opening_dxf(self.GOOD, self.ARGS, self.PLACEMENT, '3a7')
        self.assertEqual(1200.0, out['width'])
        self.assertEqual('3A7', out['host'])

    def test_wrong_size_host_or_position_is_rejected(self):
        for bad in (dict(self.GOOD, width=900.0), dict(self.GOOD, host='3EA'),
                    dict(self.GOOD, insertion=(1300.0, 700.0)),
                    dict(self.GOOD, type='TCH_COLUMN')):
            with self.assertRaises(Exception):
                panel.verify_opening_dxf(bad, self.ARGS, self.PLACEMENT, '3A7')

    def test_dump_is_parsed_with_the_resolved_host_handle(self):
        folder = tempfile.mkdtemp()
        dump = ('(0 . TCH_OPENING) (5 . 45A) (8 . WINDOW) (10 1200.4 700.3 0.0) '
                '(40 . 1200.0) (41 . 2100.0) (50 . 0.0) (330 . <图元名 1f08756cca0>)\nHOST 3A7')

        class FakeCad:
            def __init__(self):
                self.sent = []

            def run_command(self, payload):
                self.sent.append(payload)
                Path(folder, 'dxf-open-45A.txt').write_text(dump, encoding='gbk')

        c = FakeCad()
        out = panel.opening_dxf(c, '45A', folder=folder)
        self.assertIn('handent "45A"', c.sent[0])
        self.assertEqual('TCH_OPENING', out['type'])
        self.assertEqual(1200.0, out['width'])
        self.assertEqual('3A7', out['host'])

class ValueRowTests(unittest.TestCase):
    """The TGDOOR/TGWINDOW palettes label their rows by drawing, not by text."""

    # 41 is TGDOOR's open-direction row: it sits above the size rows and holds
    # ordinary 30px Edits, which must not consume an index.
    KIDS = {900: [11, 21, 31, 41], 11: [12], 21: [22], 31: [32],
            41: [42, 43]}
    RECTS = {11: (118, 395, 244, 416), 21: (118, 373, 244, 394), 31: (118, 417, 244, 438),
             41: (118, 351, 244, 372),
             12: (186, 399, 188, 412), 22: (186, 377, 188, 390), 32: (186, 421, 188, 434),
             42: (138, 355, 171, 369), 43: (191, 355, 223, 369)}
    CLASSES = {11: '#32770', 21: '#32770', 31: '#32770', 41: '#32770',
               12: 'Edit', 22: 'Edit', 32: 'Edit', 42: 'Edit', 43: 'Edit'}

    def test_rows_are_addressed_top_to_bottom(self):
        with patch('tz_mcp.panel.children', lambda u, h: self.KIDS.get(h, [])), \
             patch('tz_mcp.panel.class_name', lambda u, h: self.CLASSES.get(h, '')), \
             patch('tz_mcp.panel.rect', lambda u, h: self.RECTS.get(h, (0, 0, 0, 0))), \
             patch('tz_mcp.panel.visible', lambda u, h: True):
            self.assertEqual(22, panel.value_row_edit(None, 900, 0))
            self.assertEqual(12, panel.value_row_edit(None, 900, 1))
            self.assertEqual(32, panel.value_row_edit(None, 900, 2))
            self.assertIsNone(panel.value_row_edit(None, 900, 3))

if __name__ == '__main__':
    unittest.main()

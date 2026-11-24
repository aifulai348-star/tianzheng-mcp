import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tz_mcp import parts
from tz_mcp.cad import CommandCompletionTimeout, OutcomeUnknown
from tz_mcp.commands import prepare
from tz_mcp.engine import NO_ESCAPE_STEPS

DWG = str(Path(__file__).resolve().parents[1] / 'outputs' / 'v11-p6' / 'two-rooms-p6.dwg')


class FakeEntity:
    def __init__(self, handle, name, bounds=None, **props):
        self.Handle = handle
        self.ObjectName = name
        self.bounds = bounds
        self.props = props


class FakeDocument:
    def __init__(self):
        self.save_calls = 0
        self.close_calls = 0

    def Save(self):
        self.save_calls += 1

    def Close(self, *args):
        self.close_calls += 1


class FakeCad:
    """Only the read surface wall.split needs; any command text is a failure."""

    document_id = 'doc'

    def __init__(self, before, after, path=DWG):
        self.path = Path(path)
        self.created_walls = {}
        self.frames = [before, after]
        self.entities_calls = 0
        self.doc = FakeDocument()

    def entities(self):
        self.entities_calls += 1
        return self.frames[0] if self.entities_calls == 1 else self.frames[1]

    def entity(self, handle, document_id=None):
        if document_id is not None and document_id != self.document_id:
            raise ValueError('Entity document_id mismatch')
        handle = handle.upper()
        return self.frames[0][handle]

    def describe(self, entity):
        out = {'handle': entity.Handle.upper(), 'object_name': entity.ObjectName,
               'document_id': None, 'bounds': [list(p) for p in entity.bounds]}
        out.update(entity.props)
        return out

    def run_command(self, text):
        raise AssertionError('wall.split must not send command text: %r' % text)


def wall(handle, low, high, hole=0.0, x=24200.0, half=120.0):
    return FakeEntity(handle, 'TDbWall', [[x - half, low, 0.0], [x + half, high, 0.0]],
                      LeftWidth=half, RightWidth=half, Width=2 * half,
                      Height=3000.0, HoleArea=hole)


def door(handle, low, high, x=24200.0, half=120.0):
    return FakeEntity(handle, 'TDbOpening', [[x - half, low, 0.0], [x + half, high, 0.0]],
                      GetKind='普通门', GetSubKind='普通门', Width=900.0, Height=2100.0)


def katana(handle, low, high, x=24200.0, half=120.0):
    return FakeEntity(handle, 'GlwKatana', [[x - half, low, 0.0], [x + half, high, 0.0]])


def case(with_opening=True):
    before = {
        '35B': wall('35B', 20000.0, 23600.0, hole=1.89 if with_opening else 0.0),
        '362': wall('362', 20000.0, 23600.0, hole=1.80, x=28400.0),
    }
    if with_opening:
        before['3D7'] = door('3D7', 21352.021, 22252.021)
    after = {
        '35B': wall('35B', 20000.0, 20800.0, hole=0.0),
        '500': wall('500', 20800.0, 23600.0, hole=1.89 if with_opening else 0.0),
        '362': before['362'],
        '501': katana('501', 20750.0, 20850.0),
    }
    if with_opening:
        after['3D7'] = before['3D7']
    c = FakeCad(before, after)
    c.created_walls['35B'] = {'start': [24200.0, 20000.0], 'end': [24200.0, 23600.0],
                              'document_id': 'doc'}
    args = {'path': DWG, 'document_id': 'doc', 'handle': '35B', 'point': [24080.0, 20800.0]}
    return c, args, before


VIEW = ((24200.0, 21800.0), 2000.0, (800, 300, 2500, 1400))


def _idle(cad):
    return True


def _pick(cad, centre, size, rect, point, backend=None):
    return {'screen': [1, 1], 'world': list(point)}


def _dispatch(cad, labels):
    return [11, 12]


class SplitContractTests(unittest.TestCase):
    def test_prepare_requires_absolute_dwg_and_plan_point(self):
        args = {'path': DWG, 'document_id': 'doc', 'handle': '35B', 'point': [24080, 20800]}
        self.assertEqual('35B', prepare('wall.split', args)['handle'])
        for bad in (dict(args, path='relative.dwg'),
                    dict(args, path='D:/x/a.txt'),
                    dict(args, document_id=''),
                    dict(args, handle='0x35B'),
                    dict(args, point=[24080, 20800, 3000])):
            with self.assertRaises(ValueError):
                prepare('wall.split', bad)

    def test_split_is_not_a_no_escape_step(self):
        self.assertNotIn('wall.split', NO_ESCAPE_STEPS)

    def test_split_rejects_a_wall_that_this_job_did_not_create(self):
        c, args, before = case()
        c.created_walls.clear()
        with self.assertRaises(ValueError):
            parts.split_wall(c, args, before)
        self.assertEqual(0, c.entities_calls)


class SplitPrecheckTests(unittest.TestCase):
    def test_station_must_clear_the_wall_end(self):
        c, args, before = case()
        with self.assertRaises(ValueError):
            parts.split_wall(c, dict(args, point=[24080.0, 20005.0]), before)
        self.assertEqual(0, c.entities_calls)

    def test_station_must_clear_a_measured_opening(self):
        c, args, before = case()
        with self.assertRaises(ValueError):
            parts.split_wall(c, dict(args, point=[24080.0, 21350.0]), before)
        # The opening scan reads the model once, but no menu/pick is dispatched.
        self.assertEqual(1, c.entities_calls)

    def test_point_must_lie_inside_the_band(self):
        c, args, before = case()
        with self.assertRaises(ValueError):
            parts.split_wall(c, dict(args, point=[26000.0, 20800.0]), before)

    def test_multiple_openings_are_refused_before_any_dispatch(self):
        c, args, before = case()
        before['3D8'] = door('3D8', 22500.0, 23200.0)
        with self.assertRaises(ValueError):
            parts.split_wall(c, args, before)
        self.assertEqual(1, c.entities_calls)

    def test_axis_must_match_the_creation_record(self):
        c, args, before = case()
        c.created_walls['35B'] = {'start': [24000.0, 20000.0], 'end': [24000.0, 23600.0],
                                  'document_id': 'doc'}
        with self.assertRaises(OutcomeUnknown):
            parts.split_wall(c, args, before)

    def test_creation_record_must_belong_to_the_same_document(self):
        c, args, before = case()
        c.created_walls['35B']['document_id'] = 'other'
        with self.assertRaises(ValueError) as caught:
            parts.split_wall(c, args, before)
        self.assertIn('another document', str(caught.exception))
        self.assertEqual(0, c.entities_calls)

    def test_a_broken_hole_area_is_refused_before_any_dispatch(self):
        c, args, before = case()
        before['35B'] = wall('35B', 20000.0, 23600.0, hole='not-a-number')
        with self.assertRaises(OutcomeUnknown) as caught:
            parts.split_wall(c, args, before)
        self.assertIn('hole area could not be read', str(caught.exception))
        self.assertEqual(1, c.entities_calls)

    def test_a_different_owned_path_is_refused(self):
        c, args, before = case()
        c.path = Path(DWG).with_name('other.dwg')
        with self.assertRaises(ValueError):
            parts.split_wall(c, args, before)


class SplitFlowTests(unittest.TestCase):
    @patch('tz_mcp.parts.wait_command_idle', new=lambda c: True)
    @patch('tz_mcp.parts.pick_world_point',
           new=lambda c, centre, size, rect, point, backend=None:
           {'screen': [1, 1], 'world': list(point)})
    @patch('tz_mcp.parts.menu_dispatch', new=lambda c, labels: [11, 12])
    @patch('tz_mcp.parts.drawing_view', new=lambda c: VIEW)
    @patch('tz_mcp.parts.entity_wall_hosts',
           new=lambda c, handle: [{'handle': '1F', 'type': 'BLOCK_RECORD'},
                                  {'handle': '500', 'type': 'TCH_WALL'}])
    def test_split_with_a_hosted_opening_saves_nothing(self):
        c, args, before = case()
        result = parts.split_wall(c, args, before)
        self.assertTrue(result['success'])
        self.assertEqual({'35B', '500'}, set(result['handles']))
        split = result['evidence']['split']
        self.assertEqual(20800.0, split['station'])
        self.assertEqual(20800.0, split['seam'])
        self.assertEqual('500', split['host_handle'])
        self.assertEqual(1.89, split['segments']['500']['hole_area'])
        self.assertEqual(0.0, split['segments']['35B']['hole_area'])
        self.assertEqual([24080.0, 20800.0], result['evidence']['pick']['world'])
        self.assertEqual('verified_dxf_330_and_hole_area', result['validation']['host'])
        self.assertEqual(0, c.doc.save_calls)
        self.assertEqual(0, c.doc.close_calls)
        self.assertNotIn('35B', c.created_walls)

    @patch('tz_mcp.parts.wait_command_idle', new=lambda c: True)
    @patch('tz_mcp.parts.pick_world_point',
           new=lambda c, centre, size, rect, point, backend=None:
           {'screen': [1, 1], 'world': list(point)})
    @patch('tz_mcp.parts.menu_dispatch', new=lambda c, labels: [11, 12])
    @patch('tz_mcp.parts.drawing_view', new=lambda c: VIEW)
    def test_a_wall_without_openings_splits_with_zero_areas(self):
        c, args, before = case(with_opening=False)
        result = parts.split_wall(c, args, before)
        self.assertIsNone(result['evidence']['split']['host_handle'])
        self.assertEqual('not_applicable', result['validation']['host'])
        self.assertEqual(0.0, result['evidence']['split']['segments']['500']['hole_area'])

    def run_with(self, c, args, before, hosts=None, idle=None, pick=None, dispatch=None):
        patches = [patch('tz_mcp.parts.drawing_view', new=lambda cad: VIEW),
                   patch('tz_mcp.parts.menu_dispatch', new=dispatch or _dispatch),
                   patch('tz_mcp.parts.pick_world_point', new=pick or _pick),
                   patch('tz_mcp.parts.wait_command_idle', new=idle or _idle)]
        if hosts is not None:
            patches.append(patch('tz_mcp.parts.entity_wall_hosts', new=hosts))
        with patches[0], patches[1], patches[2], patches[3]:
            if len(patches) == 5:
                with patches[4]:
                    return parts.split_wall(c, args, before)
            return parts.split_wall(c, args, before)

    def test_hole_area_text_from_the_live_build_is_accepted(self):
        # P5/P5B readbacks returned HoleArea as text: "1.89" and "0.00".
        c, args, before = case()
        before['35B'] = wall('35B', 20000.0, 23600.0, hole='1.89')
        before['362'] = wall('362', 20000.0, 23600.0, hole='1.80', x=28400.0)
        c.frames[1]['35B'] = wall('35B', 20000.0, 20800.0, hole='0.00')
        c.frames[1]['500'] = wall('500', 20800.0, 23600.0, hole='1.89')
        c.frames[1]['362'] = before['362']
        result = self.run_with(
            c, args, before, hosts=lambda cand, h: [{'handle': '500', 'type': 'TCH_WALL'}])
        split = result['evidence']['split']
        self.assertEqual(1.89, split['segments']['500']['hole_area'])
        self.assertEqual(0.0, split['segments']['35B']['hole_area'])
        self.assertEqual('verified_dxf_330_and_hole_area', result['validation']['host'])

    def test_an_unresolved_host_reference_keeps_the_outcome_unknown(self):
        c, args, before = case()
        with self.assertRaises(CommandCompletionTimeout) as caught:
            self.run_with(c, args, before, hosts=lambda cand, h: [
                {'handle': '500', 'type': 'TCH_WALL'},
                {'handle': None, 'type': 'UNPARSEABLE', 'raw': '? UNRESOLVED'}])
        self.assertIn('could not be resolved', str(caught.exception))

    def test_an_unrelated_change_becomes_an_unconfirmed_outcome(self):
        c, args, before = case()
        c.frames[1]['362'] = wall('362', 20000.0, 23000.0, hole=1.80, x=28400.0)
        with self.assertRaises(CommandCompletionTimeout) as caught:
            self.run_with(c, args, before,
                          hosts=lambda c, h: [{'handle': '500', 'type': 'TCH_WALL'}])
        self.assertIn('Unrelated entity 362 changed', str(caught.exception))

    def test_a_missing_katana_keeps_the_outcome_unknown(self):
        c, args, before = case()
        del c.frames[1]['501']
        with self.assertRaises(CommandCompletionTimeout) as caught:
            self.run_with(c, args, before,
                          hosts=lambda c, h: [{'handle': '500', 'type': 'TCH_WALL'}])
        self.assertIn('Expected one new TDbWall and one GlwKatana', str(caught.exception))

    def test_an_ambiguous_dxf_host_is_refused(self):
        c, args, before = case()
        with self.assertRaises(CommandCompletionTimeout) as caught:
            self.run_with(c, args, before,
                          hosts=lambda c, h: [{'handle': '35B', 'type': 'TCH_WALL'},
                                              {'handle': '500', 'type': 'TCH_WALL'}])
        self.assertIn('not a unique TCH_WALL', str(caught.exception))

    def test_hole_area_must_stay_on_one_segment(self):
        c, args, before = case()
        c.frames[1]['35B'] = wall('35B', 20000.0, 20800.0, hole=1.89)
        with self.assertRaises(CommandCompletionTimeout) as caught:
            self.run_with(c, args, before,
                          hosts=lambda c, h: [{'handle': '500', 'type': 'TCH_WALL'}])
        self.assertIn('not confined', str(caught.exception))

    def test_a_pending_command_keeps_its_own_timeout(self):
        c, args, before = case()

        def pending(cad):
            raise CommandCompletionTimeout('Wall split command did not confirm completion')

        with self.assertRaises(CommandCompletionTimeout) as caught:
            self.run_with(c, args, before, idle=pending,
                          hosts=lambda c, h: [{'handle': '500', 'type': 'TCH_WALL'}])
        self.assertEqual('Wall split command did not confirm completion', str(caught.exception))


class HostDumpTests(unittest.TestCase):
    def test_an_unresolved_row_survives_the_parse(self):
        from tz_mcp import panel

        class Runner:
            def __init__(self, folder):
                self.folder = Path(folder)

            def run_command(self, text):
                (self.folder / 'hosts-3D7.txt').write_text(
                    '1F BLOCK_RECORD\n500 TCH_WALL\n? UNRESOLVED\n', encoding='gbk')

        with tempfile.TemporaryDirectory() as tmp:
            refs = panel.entity_wall_hosts(Runner(tmp), '3D7', folder=tmp)
        self.assertEqual(['1F', '500', None], [r['handle'] for r in refs])
        self.assertEqual('UNPARSEABLE', refs[2]['type'])
        self.assertEqual('? UNRESOLVED', refs[2]['raw'])

    def test_menu_dispatch_falls_back_to_main_frame_for_top_menu(self):
        from tz_mcp import panel

        class FakeU:
            def __init__(self):
                self.sent = []

            def SendMessageW(self, hwnd, msg, wparam, lparam):
                self.sent.append((int(hwnd.value), msg))
                return 0

        fake_u = FakeU()
        cad = type('C', (), {'hwnd': 100})()
        calls = {'n': 0}

        def fake_buttons(u, root):
            if root == 200:
                return []
            calls['n'] += 1
            if calls['n'] <= 2:
                return [(301, '墙体')]
            return [(301, '墙体'), (302, '墙体切割')]

        with patch('tz_mcp.panel._u', return_value=fake_u), \
             patch('tz_mcp.panel.children', return_value=[200]), \
             patch('tz_mcp.panel.class_name', return_value='AfxControlBar140u'), \
             patch('tz_mcp.panel.visible', return_value=True), \
             patch('tz_mcp.panel.title', return_value='T20天正建筑软件 V9.0'), \
             patch('tz_mcp.panel._menu_buttons', side_effect=fake_buttons), \
             patch('tz_mcp.panel.time.sleep', return_value=None):
            clicked = panel.menu_dispatch(cad, ('墙体', '墙体切割'))
        self.assertEqual([301, 302], clicked)
        self.assertEqual([(301, 0x00F5), (302, 0x00F5)], fake_u.sent)

    def test_menu_dispatch_falls_back_to_tkatana_command_when_menu_hidden(self):
        from tz_mcp import panel

        class FakeBackend:
            def __init__(self):
                self.calls = []

            def activate_view(self, pid, hwnd):
                self.calls.append(('activate', pid, hwnd))

            def esc(self, n=1):
                self.calls.append(('esc', n))

            def unicode(self, ch):
                self.calls.append(('char', ch))

            def enter(self):
                self.calls.append(('enter',))

        backend = FakeBackend()
        cad = type('C', (), {'pid': 1404, 'hwnd': 100})()
        with patch('tz_mcp.panel._u', return_value=object()), \
             patch('tz_mcp.panel.children', return_value=[200]), \
             patch('tz_mcp.panel.class_name', return_value='AfxControlBar140u'), \
             patch('tz_mcp.panel.visible', return_value=True), \
             patch('tz_mcp.panel.title', return_value='T20天正建筑软件 V9.0'), \
             patch('tz_mcp.panel._menu_buttons', return_value=[]), \
             patch('tz_mcp.panel.time.sleep', return_value=None):
            clicked = panel.menu_dispatch(cad, ('墙体', '墙体切割'), backend=backend)
        self.assertEqual(['command:TKATANA'], clicked)
        self.assertEqual(('activate', 1404, 100), backend.calls[0])
        self.assertEqual(('esc', 1), backend.calls[1])
        self.assertEqual('TKATANA', ''.join(c[1] for c in backend.calls if c[0] == 'char'))
        self.assertEqual(('enter',), backend.calls[-1])


if __name__ == '__main__':
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tz_mcp import cli, parts
from tz_mcp.cad import Cad, OutcomeUnknown
from tz_mcp.commands import SCHEMAS, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


class FakeOpening:
    Handle = '3CF'
    ObjectName = 'TDbOpening'
    bounds = [[2550.0, -120.0, 0.0], [3450.0, 120.0, 0.0]]
    Width = 900.0
    Height = 2100.0
    WinSill = 0.0

    def __init__(self, on_update):
        self.writes = []
        self.updated = 0
        self.on_update = on_update

    def __setattr__(self, name, value):
        if name in ('Width', 'Height', 'WinSill'):
            self.writes.append((name, value))
        super().__setattr__(name, value)

    def Update(self):
        self.updated += 1
        self.on_update()


class FakeCad:
    document_id = 'doc'
    hwnd = 42
    doc = object()

    def __init__(self):
        self.events = []
        self.commands = []
        self.host = SimpleNamespace(Handle='35A', ObjectName='TDbWall', HoleArea=1.89,
                                    LeftWidth=120.0, RightWidth=120.0,
                                    bounds=[[0, -120, 0], [6000, 120, 3000]])
        self.other_wall = SimpleNamespace(Handle='35B', ObjectName='TDbWall', HoleArea=0.0)
        self.opening = FakeOpening(self.update_hole_area)
        self.store = {e.Handle: e for e in (self.host, self.other_wall, self.opening)}
        self.refs = {
            '3CF': [{'handle': '1F', 'type': 'BLOCK_RECORD'},
                    {'handle': '35A', 'type': 'TCH_WALL'}],
        }
        self.insertion = (3000.0, 0.0)
        self.before_dxf = {}
        self.after_dxf = {}

    def guard(self):
        self.events.append('guard')

    def entities(self):
        self.events.append('entities')
        return self.store.copy()

    def entity(self, handle, document_id):
        if document_id != self.document_id or handle not in self.store:
            raise AssertionError('unexpected entity lookup')
        return self.store[handle]

    def describe(self, entity):
        data = {'handle': entity.Handle, 'object_name': entity.ObjectName,
                'document_id': self.document_id}
        for key in ('bounds', 'Width', 'Height', 'WinSill', 'LeftWidth', 'RightWidth', 'HoleArea'):
            if hasattr(entity, key):
                data[key] = getattr(entity, key)
        return data

    def run_command(self, command):
        self.commands.append(command)

    def update_hole_area(self):
        self.host.HoleArea = self.opening.Width * self.opening.Height / 1_000_000.0

    def read_dxf(self, cad, handle):
        opening = self.store[handle]
        changes = self.after_dxf if opening.updated else self.before_dxf
        return {'type': 'TCH_OPENING', 'handle': handle, 'layer': 'DOOR',
                'width': opening.Width, 'height': opening.Height, 'rotation': 0.0,
                'insertion': self.insertion, 'host': '35A', **changes}

    def add_nearby_opening(self, host_handle):
        self.store['3D0'] = SimpleNamespace(Handle='3D0', ObjectName='TDbOpening',
                                            bounds=[[3490, -120, 0], [4390, 120, 0]])
        self.refs['3D0'] = [{'handle': host_handle, 'type': 'TCH_WALL'}]
        if host_handle == self.host.Handle:
            self.host.HoleArea = 3.78


class OpeningEditTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        self.opening = self.cad.opening
        self.args = {'document_id': 'doc', 'handle': '3cf'}
        self.dxf = self.enterContext(patch.object(parts, 'opening_dxf', side_effect=self.cad.read_dxf))
        self.hosts = self.enterContext(patch.object(parts, 'entity_wall_hosts',
                                                   side_effect=lambda c, handle: self.cad.refs[handle]))
        self.drawing_guard = self.enterContext(patch.object(
            parts, 'require_drawing_window', side_effect=lambda hwnd: self.cad.events.append('window')))
        self.model_guard = self.enterContext(patch.object(
            parts, 'require_model_space', side_effect=lambda doc: self.cad.events.append('model')))

    def edit(self, **overrides):
        return Cad.execute(self.cad, 'opening.edit', {**self.args, **overrides})

    def assert_no_writes(self):
        self.assertEqual([], self.opening.writes)
        self.assertEqual(0, self.opening.updated)
        self.assertEqual([], self.cad.commands)

    def test_capabilities_and_no_escape_registration(self):
        schema = ({'document_id', 'handle'}, {'width', 'height', 'sill', 'text_height'})
        self.assertEqual(schema, SCHEMAS['opening.edit'])
        self.assertIn('opening.edit', NO_ESCAPE_STEPS)
        self.assertTrue(cli.VALIDATION['opening.edit'].startswith('verified_live'))
        self.assertIn('opening.edit', cli.capabilities())

    def test_prepare_normalizes_handle_and_dimensions(self):
        args = prepare('opening.edit', {**self.args, 'width': 1000, 'height': 2200, 'sill': 0})
        self.assertEqual('3CF', args['handle'])
        self.assertEqual('doc', args['document_id'])
        for key, expected in [('width', 1000.0), ('height', 2200.0), ('sill', 0.0)]:
            self.assertIs(type(args[key]), float)
            self.assertEqual(expected, args[key])

    def test_prepare_rejects_missing_or_invalid_references_and_dimensions(self):
        for args in (
            self.args,
            {'handle': '3CF', 'width': 1000},
            {'document_id': 'doc', 'width': 1000},
            {**self.args, 'document_id': ' ', 'sill': 0},
            {**self.args, 'handle': '0x3CF', 'sill': 0},
            {**self.args, 'width': 1000, 'delta': [1, 0]},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                prepare('opening.edit', args)
        for key in ('width', 'height', 'sill'):
            for value in (-1, True, '900', None, float('nan'), float('inf'), -float('inf'), 10**400):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare('opening.edit', {**self.args, key: value})
        for key in ('width', 'height'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepare('opening.edit', {**self.args, key: 0})

    def test_width_only_runs_through_adapter_guards(self):
        result = self.edit(width=1000)
        self.assertEqual(['guard', 'window', 'model', 'entities'], self.cad.events[:4])
        self.drawing_guard.assert_called_once_with(self.cad.hwnd)
        self.model_guard.assert_called_once_with(self.cad.doc)
        self.assertEqual([('Width', 1000.0)], self.opening.writes)
        self.assertEqual(1, self.opening.updated)
        self.assertEqual([], self.cad.commands)
        self.assertAlmostEqual(0.21, result['evidence']['edit']['expected_hole_area_delta'])

    def test_height_only_verifies_area_decrease(self):
        edit = self.edit(height=2000)['evidence']['edit']
        self.assertEqual([('Height', 2000.0)], self.opening.writes)
        self.assertAlmostEqual(-0.09, edit['expected_hole_area_delta'])
        self.assertAlmostEqual(1.80, edit['host_hole_area_after'])

    def test_sill_only_preserves_area_and_size(self):
        edit = self.edit(sill=600)['evidence']['edit']
        self.assertEqual([('WinSill', 600.0)], self.opening.writes)
        self.assertEqual(0.0, edit['expected_hole_area_delta'])
        self.assertEqual(1.89, edit['host_hole_area_after'])
        self.assertEqual(900.0, edit['dxf_after']['width'])
        self.assertEqual(2100.0, edit['dxf_after']['height'])

    def test_all_dimensions_return_com_dxf_and_host_evidence(self):
        result = self.edit(width=1000, height=2200, sill=600)
        self.assertTrue(result['success'])
        self.assertEqual('doc', result['document_id'])
        self.assertEqual(['3CF'], result['handles'])
        self.assertEqual([self.cad.describe(self.opening)], result['entities'])
        self.assertEqual([('Width', 1000.0), ('Height', 2200.0), ('WinSill', 600.0)], self.opening.writes)
        self.assertEqual(1, self.opening.updated)
        self.assertEqual({
            'handle': '3CF', 'host_handle': '35A',
            'properties_before': {'Width': 900.0, 'Height': 2100.0, 'WinSill': 0.0},
            'properties_requested': {'Width': 1000.0, 'Height': 2200.0, 'WinSill': 600.0},
            'properties_after': {'Width': 1000.0, 'Height': 2200.0, 'WinSill': 600.0},
            'dxf_before': {'type': 'TCH_OPENING', 'handle': '3CF', 'layer': 'DOOR',
                           'width': 900.0, 'height': 2100.0, 'rotation': 0.0,
                           'insertion': (3000.0, 0.0), 'host': '35A'},
            'dxf_after': self.cad.read_dxf(self.cad, '3CF'),
            'host_hole_area_before': 1.89, 'host_hole_area_after': 2.2,
            'expected_hole_area_delta': 0.31,
        }, result['evidence']['edit'])
        self.assertEqual({'type': True, 'properties': True, 'geometry': 'verified_com_and_dxf_40_41',
                          'host': 'verified_dxf_330_and_hole_area_delta', 'reopen': 'pending'},
                         result['validation'])

    def test_non_opening_is_rejected_before_write(self):
        with self.assertRaisesRegex(ValueError, 'native opening'):
            self.edit(handle='35A', width=1000)
        self.hosts.assert_not_called()
        self.assert_no_writes()

    def test_foreign_document_and_unknown_handle_are_rejected_before_write(self):
        for args, error in (({'document_id': 'foreign'}, 'document_id'), ({'handle': 'ABC'}, 'Unknown handle')):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, error):
                self.edit(width=1000, **args)
        self.hosts.assert_not_called()
        self.assert_no_writes()

    def test_ambiguous_host_is_rejected_before_write(self):
        self.cad.refs['3CF'].append({'handle': '35B', 'type': 'TCH_WALL'})
        with self.assertRaisesRegex(ValueError, 'unique TCH_WALL'):
            self.edit(width=1000)
        self.assert_no_writes()

    def test_host_missing_from_model_space_is_rejected_before_write(self):
        del self.cad.store['35A']
        with self.assertRaisesRegex(ValueError, 'native wall in ModelSpace'):
            self.edit(height=2200)
        self.assert_no_writes()

    def test_width_over_wall_end_is_rejected_before_write(self):
        with self.assertRaisesRegex(ValueError, 'wall end'):
            self.edit(width=6000)
        self.assert_no_writes()

    def test_width_violating_same_wall_opening_clearance_is_rejected_before_write(self):
        self.cad.add_nearby_opening('35A')
        with self.assertRaisesRegex(ValueError, 'overlaps another opening'):
            self.edit(width=970)
        self.assert_no_writes()

    def test_width_ignores_overlapping_opening_on_another_host(self):
        self.cad.add_nearby_opening('35B')
        self.assertTrue(self.edit(width=1000)['success'])

    def test_unreadable_com_property_and_invalid_dxf_fail_before_write(self):
        with patch.object(FakeOpening, 'WinSill', None):
            with self.assertRaisesRegex(OutcomeUnknown, 'WinSill'):
                self.edit(width=1000)
        for dxf in ({'type': 'OTHER'}, {'insertion': None}):
            with self.subTest(dxf=dxf), self.assertRaisesRegex(OutcomeUnknown, 'DXF geometry'):
                self.cad.before_dxf = dxf
                self.edit(width=1000)
        self.assert_no_writes()

    def test_handle_set_change_after_write_is_unknown(self):
        self.opening.on_update = lambda: self.cad.store.pop('35B')
        with self.assertRaisesRegex(OutcomeUnknown, 'handles changed'):
            self.edit(width=1000)
        self.assertEqual(1, self.opening.updated)

    def test_requested_and_unmodified_com_readback_mismatch_is_unknown(self):
        for name, value in (('Width', 900.0), ('WinSill', 1.0), ('Height', float('nan'))):
            with self.subTest(name=name):
                with patch.object(self.opening, 'on_update', side_effect=lambda: setattr(self.opening, name, value)):
                    with self.assertRaisesRegex(OutcomeUnknown, name + ' readback mismatch'):
                        self.edit(width=1000)
                object.__setattr__(self.opening, name, getattr(FakeOpening, name))

    def test_dxf_dimension_mismatch_is_unknown(self):
        for dxf in ({'width': 1001.0}, {'height': 2100.6}, {'height': None}):
            with self.subTest(dxf=dxf):
                self.cad.after_dxf = dxf
                with self.assertRaisesRegex(OutcomeUnknown, 'DXF (width|height) mismatch'):
                    self.edit(width=1000)
                self.opening.updated = 0

    def test_dxf_insertion_shift_over_half_mm_is_unknown(self):
        self.cad.after_dxf = {'insertion': (3000.4, 0.4)}
        with self.assertRaisesRegex(OutcomeUnknown, 'insertion shifted'):
            self.edit(width=1000)

    def test_changed_or_ambiguous_host_after_write_is_unknown(self):
        initial_refs = self.cad.refs['3CF']
        for refs, error in (
            ([{'handle': '35B', 'type': 'TCH_WALL'}], 'host changed'),
            (initial_refs + [{'handle': '35B', 'type': 'TCH_WALL'}], 'unique TCH_WALL'),
        ):
            with self.subTest(refs=refs):
                self.opening.on_update = lambda: self.cad.refs.update({'3CF': refs})
                with self.assertRaisesRegex(OutcomeUnknown, error):
                    self.edit(width=1000)
                self.cad.refs['3CF'] = initial_refs

    def test_host_hole_area_mismatch_after_write_is_unknown(self):
        self.opening.on_update = lambda: None
        with patch.object(parts, 'time') as clock:
            clock.monotonic.side_effect = [0.0, 5.0]
            with self.assertRaisesRegex(OutcomeUnknown, 'hole-area update mismatch'):
                self.edit(width=1000)

    def test_other_wall_hole_area_change_after_write_is_unknown(self):
        def update_other_wall():
            self.cad.update_hole_area()
            self.cad.other_wall.HoleArea = 0.03

        self.opening.on_update = update_other_wall
        with patch.object(parts, 'time') as clock:
            clock.monotonic.side_effect = [0.0, 5.0]
            with self.assertRaisesRegex(OutcomeUnknown, 'others_ok=False'):
                self.edit(width=1000)

    def test_update_error_after_property_write_is_unknown(self):
        with patch.object(self.opening, 'Update', side_effect=RuntimeError('Update failed')):
            with self.assertRaisesRegex(OutcomeUnknown, 'Update failed'):
                self.edit(width=1000)
        self.assertEqual([('Width', 1000.0)], self.opening.writes)


if __name__ == '__main__':
    unittest.main()

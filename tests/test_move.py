import unittest
from unittest.mock import patch

from tz_mcp import parts
from tz_mcp.cad import Cad, OutcomeUnknown
from tz_mcp.commands import prepare


class FakeEntity:
    def __init__(self, handle, name, bounds=None, **props):
        self.Handle = handle
        self.ObjectName = name
        self.bounds = bounds
        for key, value in props.items():
            setattr(self, key, value)
        self.props = props


class FakeCad:
    document_id = 'doc'

    def __init__(self, before, after=None):
        self.before = dict(before)
        self.after = dict(after if after is not None else before)
        self.commands = []
        self.calls = 0

    def guard(self):
        return None

    def entity(self, handle, document_id=None):
        if document_id is not None and document_id != self.document_id:
            raise ValueError('Entity document_id mismatch')
        key = handle.upper()
        if key not in self.before:
            raise ValueError('Unknown handle: ' + key)
        return self.before[key]

    def entities(self):
        self.calls += 1
        return self.after if self.commands else self.before

    def describe(self, entity):
        out = {
            'handle': entity.Handle.upper(),
            'object_name': entity.ObjectName,
            'document_id': self.document_id,
            'bounds': [list(p) for p in entity.bounds],
        }
        out.update(entity.props)
        return out

    def run_command(self, text):
        self.commands.append(text)


def col_entity(handle='420', shape='矩形'):
    return FakeEntity(
        handle, 'TDbColumn', [[1750.0, 1750.0, 0.0], [2250.0, 2250.0, 3000.0]],
        SectionShapeText=shape, Width=500.0, Deep=500.0, Height=3000.0)


def wall_entity(handle='35A', low=0.0, high=5000.0, line=0.0, hole=1.89, vertical=False):
    if vertical:
        bounds = [[line - 120.0, low, 0.0], [line + 120.0, high, 3000.0]]
    else:
        bounds = [[low, line - 120.0, 0.0], [high, line + 120.0, 3000.0]]
    return FakeEntity(handle, 'TDbWall', bounds,
                      LeftWidth=120.0, RightWidth=120.0, Width=240.0, Height=3000.0, HoleArea=hole)


def door_entity(handle='3CF', station=1500.0, line=0.0, vertical=False):
    if vertical:
        bounds = [[line - 120.0, station - 450.0, 0.0], [line + 120.0, station + 450.0, 0.0]]
    else:
        bounds = [[station - 450.0, line - 120.0, 0.0], [station + 450.0, line + 120.0, 0.0]]
    return FakeEntity(handle, 'TDbOpening', bounds,
                      GetKind='普通门', GetSubKind='普通门', Width=900.0, Height=2100.0)


class MoveContractTests(unittest.TestCase):
    def test_prepare_validates_move_arguments(self):
        for op in ('column.move', 'opening.move'):
            ok = prepare(op, {'document_id': 'doc', 'handle': '3cf', 'delta': [500, 0]})
            self.assertEqual([500.0, 0.0, 0.0], ok['delta'])
            for bad in (
                {'document_id': '', 'handle': '3CF', 'delta': [500, 0]},
                {'document_id': 'doc', 'handle': '0x3CF', 'delta': [500, 0]},
                {'document_id': 'doc', 'handle': '3CF', 'delta': [500, 0, 100]},
                {'document_id': 'doc', 'handle': '3CF', 'delta': [0, 0]},
            ):
                with self.assertRaises(ValueError):
                    prepare(op, bad)


class ColumnMoveTests(unittest.TestCase):
    def test_rejects_non_rectangular_column_before_command(self):
        cad = FakeCad({'420': col_entity(shape='圆形')})
        with self.assertRaisesRegex(ValueError, 'rectangular native column'):
            parts.move_column(cad, {'document_id': 'doc', 'handle': '420', 'delta': [500.0, 0.0, 0.0]}, cad.before)
        self.assertEqual([], cad.commands)

    def test_column_move_success_checks_insertion_and_vertices(self):
        before = {'420': col_entity('420')}
        after = {'420': FakeEntity('420', 'TDbColumn', [[2250.0, 1750.0, 0.0], [2750.0, 2250.0, 3000.0]],
                                   SectionShapeText='矩形', Width=500.0, Deep=500.0, Height=3000.0)}
        cad = FakeCad(before, after)
        dxf_before = {
            'type': 'TCH_COLUMN', 'handle': '420', 'layer': 'COLUMN',
            'vertices': [(1750.0, 1750.0), (2250.0, 1750.0), (2250.0, 2250.0), (1750.0, 2250.0)],
            'insertion': (2000.0, 2000.0), 'height': 3000.0, 'flags': (1, 60, 0),
        }
        dxf_after = {
            'type': 'TCH_COLUMN', 'handle': '420', 'layer': 'COLUMN',
            'vertices': [(2250.0, 1750.0), (2750.0, 1750.0), (2750.0, 2250.0), (2250.0, 2250.0)],
            'insertion': (2500.0, 2000.0), 'height': 3000.0, 'flags': (1, 60, 0),
        }
        seq = [dxf_before, dxf_after]
        with patch('tz_mcp.parts.column_dxf', side_effect=lambda c, h: seq.pop(0)):
            res = parts.move_column(cad, {'document_id': 'doc', 'handle': '420', 'delta': [500.0, 0.0, 0.0]}, before)
        self.assertTrue(res['success'])
        self.assertEqual(['420'], res['handles'])
        self.assertIn('_.MOVE', cad.commands[0])
        self.assertEqual([2500.0, 2000.0], res['evidence']['move']['insertion_after'])

    def test_column_move_raises_outcome_unknown_on_short_shift(self):
        before = {'420': col_entity('420')}
        cad = FakeCad(before, before)
        dxf_before = {
            'type': 'TCH_COLUMN', 'handle': '420', 'layer': 'COLUMN',
            'vertices': [(1750.0, 1750.0), (2250.0, 1750.0), (2250.0, 2250.0), (1750.0, 2250.0)],
            'insertion': (2000.0, 2000.0), 'height': 3000.0, 'flags': (1, 60, 0),
        }
        dxf_bad = dict(dxf_before, insertion=(2100.0, 2000.0))
        seq = [dxf_before, dxf_bad]
        with patch('tz_mcp.parts.column_dxf', side_effect=lambda c, h: seq.pop(0)):
            with self.assertRaises(OutcomeUnknown):
                parts.move_column(cad, {'document_id': 'doc', 'handle': '420', 'delta': [500.0, 0.0, 0.0]}, before)


class OpeningMoveTests(unittest.TestCase):
    def _dxf(self, x=1500.0, y=0.0):
        return {'type': 'TCH_OPENING', 'handle': '3CF', 'layer': 'WINDOW',
                'width': 900.0, 'height': 2100.0, 'rotation': 0.0,
                'insertion': (x, y), 'host': '35A'}

    def test_rejects_off_axis_and_wall_end_and_overlap(self):
        before = {
            '35A': wall_entity('35A', 0.0, 5000.0, 0.0, hole=3.78),
            '3CF': door_entity('3CF', 1500.0, 0.0),
            '3D0': door_entity('3D0', 3600.0, 0.0),
        }
        cad = FakeCad(before)
        hosts = lambda c, h: [{'handle': '35A', 'type': 'TCH_WALL'}]
        with patch('tz_mcp.parts.opening_dxf', return_value=self._dxf(1500.0, 0.0)), \
             patch('tz_mcp.parts.entity_wall_hosts', side_effect=hosts):
            with self.assertRaisesRegex(ValueError, 'host wall axis'):
                parts.move_opening(cad, {'document_id': 'doc', 'handle': '3CF', 'delta': [0.0, 500.0, 0.0]}, before)
            with self.assertRaisesRegex(ValueError, 'wall end'):
                parts.move_opening(cad, {'document_id': 'doc', 'handle': '3CF', 'delta': [-1200.0, 0.0, 0.0]}, before)
            with self.assertRaisesRegex(ValueError, 'overlaps another opening'):
                parts.move_opening(cad, {'document_id': 'doc', 'handle': '3CF', 'delta': [2000.0, 0.0, 0.0]}, before)
        self.assertEqual([], cad.commands)

    def test_opening_move_success_and_host_change_guard(self):
        before = {'35A': wall_entity('35A', 0.0, 5000.0, 0.0, hole=1.89),
                  '3CF': door_entity('3CF', 1500.0, 0.0)}
        after = {'35A': wall_entity('35A', 0.0, 5000.0, 0.0, hole=1.89),
                 '3CF': door_entity('3CF', 2300.0, 0.0)}
        cad = FakeCad(before, after)
        dxfs = [self._dxf(1500.0, 0.0), self._dxf(2300.0, 0.0)]
        with patch('tz_mcp.parts.opening_dxf', side_effect=lambda c, h: dxfs.pop(0)), \
             patch('tz_mcp.parts.entity_wall_hosts', return_value=[{'handle': '1F', 'type': 'BLOCK_RECORD'},
                                                                   {'handle': '35A', 'type': 'TCH_WALL'}]):
            res = parts.move_opening(cad, {'document_id': 'doc', 'handle': '3CF', 'delta': [800.0, 0.0, 0.0]}, before)
        self.assertTrue(res['success'])
        self.assertEqual('35A', res['evidence']['move']['host_handle'])
        self.assertEqual([2300.0, 0.0], res['evidence']['move']['insertion_after'])

        cad2 = FakeCad(before, after)
        dxfs2 = [self._dxf(1500.0, 0.0), self._dxf(2300.0, 0.0)]
        hosts_seq = [[{'handle': '35A', 'type': 'TCH_WALL'}], [{'handle': '35B', 'type': 'TCH_WALL'}]]
        with patch('tz_mcp.parts.opening_dxf', side_effect=lambda c, h: dxfs2.pop(0)), \
             patch('tz_mcp.parts.entity_wall_hosts', side_effect=lambda c, h: hosts_seq.pop(0)):
            with self.assertRaises(OutcomeUnknown):
                parts.move_opening(cad2, {'document_id': 'doc', 'handle': '3CF', 'delta': [800.0, 0.0, 0.0]}, before)


if __name__ == '__main__':
    unittest.main()

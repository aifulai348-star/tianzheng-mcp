import math
import unittest
from unittest.mock import patch

from tz_mcp import cli, parts
from tz_mcp.cad import OutcomeUnknown
from tz_mcp.commands import prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


class FakeEntity:
    def __init__(self, handle, name, bounds=None, on_delete=None, **props):
        self.Handle = handle
        self.ObjectName = name
        self.bounds = bounds or [[0.0, 0.0, 0.0], [100.0, 100.0, 0.0]]
        self._on_delete = on_delete
        self.updated = 0
        self.deleted = 0
        for key, value in props.items():
            setattr(self, key, value)
        self._prop_keys = list(props.keys())

    def Update(self):
        self.updated += 1

    def Delete(self):
        self.deleted += 1
        if self._on_delete is not None:
            self._on_delete(self)


class FakeCad:
    document_id = 'doc'

    def __init__(self, entities):
        self.store = dict(entities)
        self.commands = []
        self.created_walls = {}

    def guard(self):
        return None

    def entity(self, handle, document_id=None):
        if document_id is not None and document_id != self.document_id:
            raise ValueError('Entity document_id mismatch')
        key = handle.upper()
        if key not in self.store:
            raise ValueError('Unknown handle: ' + key)
        return self.store[key]

    def entities(self):
        return dict(self.store)

    def describe(self, entity):
        out = {
            'handle': entity.Handle.upper(),
            'object_name': entity.ObjectName,
            'document_id': self.document_id,
            'bounds': [list(p) for p in entity.bounds],
        }
        for k in entity._prop_keys:
            out[k] = getattr(entity, k)
        return out

    def run_command(self, text):
        self.commands.append(text)


def make_col(handle='3D3', shape='矩形'):
    return FakeEntity(handle, 'TDbColumn', [[1561.43, 1866.31, 0.0], [2461.43, 2166.31, 2600.0]],
                      SectionShapeText=shape, Width=900.0, Deep=300.0, Height=2600.0)


def make_wall(handle='35A', lw=120.0, rw=120.0, h=3000.0, hole=1.89):
    return FakeEntity(handle, 'TDbWall', [[0.0, -lw, 0.0], [6000.0, rw, h]],
                      LeftWidth=lw, RightWidth=rw, Height=h, HoleArea=hole)


def make_door(handle='3D2', w=900.0, h=2100.0, on_delete=None):
    return FakeEntity(handle, 'TDbOpening', [[2550.0, -120.0, 0.0], [3450.0, 120.0, 0.0]],
                      on_delete=on_delete, GetKind='普通门', GetSubKind='普通门', Width=w, Height=h)


def rotated_pts(origin, pts, angle_deg):
    rad = math.radians(angle_deg)
    c, s = math.cos(rad), math.sin(rad)
    ox, oy = origin
    return [(ox + (x - ox) * c - (y - oy) * s, oy + (x - ox) * s + (y - oy) * c) for x, y in pts]


class Stage4ContractTests(unittest.TestCase):
    def test_no_escape_steps_and_capabilities_registration(self):
        self.assertIn('wall.edit', NO_ESCAPE_STEPS)
        self.assertIn('entity.delete', NO_ESCAPE_STEPS)
        self.assertNotIn('column.rotate', NO_ESCAPE_STEPS)
        caps = cli.capabilities()
        for op in ('column.rotate', 'wall.edit', 'entity.delete'):
            self.assertIn(op, caps)

    def test_prepare_validates_stage4_ops(self):
        ok_rot = prepare('column.rotate', {'document_id': 'doc', 'handle': '3d3', 'angle_deg': 30})
        self.assertEqual(30.0, ok_rot['angle_deg'])
        for bad in (
            {'document_id': '', 'handle': '3D3', 'angle_deg': 30},
            {'document_id': 'doc', 'handle': '0x3D3', 'angle_deg': 30},
            {'document_id': 'doc', 'handle': '3D3', 'angle_deg': 0},
            {'document_id': 'doc', 'handle': '3D3', 'angle_deg': 360},
            {'document_id': 'doc', 'handle': '3D3', 'angle_deg': -720},
            {'document_id': 'doc', 'handle': '3D3', 'angle_deg': True},
            {'document_id': 'doc', 'handle': '3D3', 'angle_deg': float('nan')},
        ):
            with self.assertRaises(ValueError):
                prepare('column.rotate', bad)

        ok_edit = prepare('wall.edit', {'document_id': 'doc', 'handle': '35a', 'height': 3600})
        self.assertEqual(3600.0, ok_edit['height'])
        for bad in (
            {'document_id': 'doc', 'handle': '35A'},
            {'document_id': 'doc', 'handle': '35A', 'left_width': 0},
            {'document_id': 'doc', 'handle': '35A', 'right_width': -10},
            {'document_id': 'doc', 'handle': '35A', 'height': True},
            {'document_id': '', 'handle': '35A', 'height': 3000},
        ):
            with self.assertRaises(ValueError):
                prepare('wall.edit', bad)

        ok_del = prepare('entity.delete', {'document_id': 'doc', 'handle': '3d4'})
        self.assertEqual('3d4', ok_del['handle'])
        for bad in (
            {'document_id': '', 'handle': '3D4'},
            {'document_id': 'doc', 'handle': '0x3D4'},
        ):
            with self.assertRaises(ValueError):
                prepare('entity.delete', bad)


class ColumnRotateTests(unittest.TestCase):
    ORIGIN = (2011.43, 2016.31)
    VERTS = [(1561.43, 1866.31), (2461.43, 1866.31), (2461.43, 2166.31), (1561.43, 2166.31)]

    def _dxf(self, angle_deg=0.0, height=2600.0, flags=(1, 60, 0), bulges=(0.0, 0.0, 0.0, 0.0), insertion=None):
        ins = self.ORIGIN if insertion is None else insertion
        pts = self.VERTS if angle_deg == 0.0 else rotated_pts(self.ORIGIN, self.VERTS, angle_deg)
        return {
            'type': 'TCH_COLUMN', 'handle': '3D3', 'layer': 'COLUMN',
            'vertices': pts, 'bulges': list(bulges),
            'insertion': ins, 'height': height, 'flags': flags,
        }

    def test_rejects_non_rectangular_column_before_command(self):
        cad = FakeCad({'3D3': make_col(shape='圆形')})
        with self.assertRaisesRegex(ValueError, 'rectangular native column'):
            parts.rotate_column(cad, {'document_id': 'doc', 'handle': '3D3', 'angle_deg': 30.0}, cad.entities())
        self.assertEqual([], cad.commands)

    def test_rejects_missing_dxf_fields_before_command(self):
        cad = FakeCad({'3D3': make_col()})
        with patch('tz_mcp.parts.column_dxf', return_value=self._dxf(height=None, flags=(None, None, None))):
            with self.assertRaisesRegex(OutcomeUnknown, 'before rotate'):
                parts.rotate_column(cad, {'document_id': 'doc', 'handle': '3D3', 'angle_deg': 30.0}, cad.entities())
        self.assertEqual([], cad.commands)

    def test_rotate_column_success_verifies_vertices_and_bulges(self):
        cad = FakeCad({'3D3': make_col()})
        seq = [self._dxf(0.0), self._dxf(30.0)]
        with patch('tz_mcp.parts.column_dxf', side_effect=lambda c, h: seq.pop(0)):
            res = parts.rotate_column(cad, {'document_id': 'doc', 'handle': '3D3', 'angle_deg': 30.0}, cad.entities())
        self.assertTrue(res['success'])
        self.assertEqual(['3D3'], res['handles'])
        self.assertIn('_.ROTATE', cad.commands[0])
        self.assertEqual(30.0, res['evidence']['rotate']['angle_deg'])
        self.assertEqual([0.0, 0.0, 0.0, 0.0], res['evidence']['rotate']['bulges'])

    def test_rotate_column_raises_on_wrong_angle_or_nonzero_bulge(self):
        for name, bad_after in (
            ('wrong_angle', self._dxf(15.0)),
            ('insertion_shifted', self._dxf(30.0, insertion=(2020.0, 2016.31))),
            ('nonzero_bulge', self._dxf(30.0, bulges=(0.0, 0.25, 0.0, 0.0))),
            ('missing_after_flag', self._dxf(30.0, flags=(1, 60, None))),
        ):
            with self.subTest(case=name):
                cad = FakeCad({'3D3': make_col()})
                seq = [self._dxf(0.0), bad_after]
                with patch('tz_mcp.parts.column_dxf', side_effect=lambda c, h: seq.pop(0)):
                    with self.assertRaises(OutcomeUnknown):
                        parts.rotate_column(cad, {'document_id': 'doc', 'handle': '3D3', 'angle_deg': 30.0}, cad.entities())


class WallEditTests(unittest.TestCase):
    def test_rejects_non_wall_before_write(self):
        cad = FakeCad({'3D3': make_col()})
        with self.assertRaisesRegex(ValueError, 'native wall'):
            parts.edit_wall(cad, {'document_id': 'doc', 'handle': '3D3', 'height': 3600.0}, cad.entities())

    def test_wall_edit_success_and_preserves_unmodified_props_and_hole_area(self):
        wall = make_wall('35A', lw=120.0, rw=120.0, h=3000.0, hole=1.89)
        cad = FakeCad({'35A': wall})
        res = parts.edit_wall(cad, {'document_id': 'doc', 'handle': '35A', 'left_width': 150.0, 'height': 3600.0},
                              cad.entities())
        self.assertTrue(res['success'])
        self.assertEqual(1, wall.updated)
        self.assertEqual(150.0, wall.LeftWidth)
        self.assertEqual(120.0, wall.RightWidth)
        self.assertEqual(3600.0, wall.Height)
        self.assertEqual(1.89, res['evidence']['edit']['hole_area_after'])

    def test_wall_edit_raises_outcome_unknown_if_hole_area_changes(self):
        wall = make_wall('35A', lw=120.0, rw=120.0, h=3000.0, hole=1.89)
        orig_update = wall.Update

        def corrupt_update():
            orig_update()
            wall.HoleArea = 0.0

        wall.Update = corrupt_update
        cad = FakeCad({'35A': wall})
        with self.assertRaisesRegex(OutcomeUnknown, 'hole area'):
            parts.edit_wall(cad, {'document_id': 'doc', 'handle': '35A', 'height': 3600.0}, cad.entities())


class EntityDeleteTests(unittest.TestCase):
    def test_rejects_unsupported_type_and_wall_with_openings_before_delete(self):
        cad = FakeCad({
            '999': FakeEntity('999', 'TDbUnknownFoo'),
            '35A': make_wall('35A', hole=1.89),
        })
        with self.assertRaisesRegex(ValueError, 'does not support'):
            parts.delete_entity(cad, {'document_id': 'doc', 'handle': '999'}, cad.entities())
        with self.assertRaisesRegex(ValueError, 'Cannot delete wall with openings'):
            parts.delete_entity(cad, {'document_id': 'doc', 'handle': '35A'}, cad.entities())
        self.assertEqual(0, cad.store['35A'].deleted)

    def test_delete_standalone_text_succeeds(self):
        cad = FakeCad({})
        txt = FakeEntity('3D4', 'AcDbText', on_delete=lambda e: cad.store.pop('3D4'), TextString='S4')
        cad.store['3D4'] = txt
        res = parts.delete_entity(cad, {'document_id': 'doc', 'handle': '3D4'}, cad.entities())
        self.assertTrue(res['success'])
        self.assertEqual(['3D4'], res['evidence']['delete']['removed_handles'])
        self.assertEqual('not_applicable', res['validation']['host'])

    def test_delete_hosted_door_verifies_wall_hole_area_decrement(self):
        wall = make_wall('35A', hole=1.89)
        cad = FakeCad({'35A': wall})

        def on_del(_e):
            cad.store.pop('3D2')
            wall.HoleArea = 0.0

        cad.store['3D2'] = make_door('3D2', w=900.0, h=2100.0, on_delete=on_del)
        with patch('tz_mcp.parts.entity_wall_hosts', return_value=[{'handle': '35A', 'type': 'TCH_WALL'}]):
            res = parts.delete_entity(cad, {'document_id': 'doc', 'handle': '3D2'}, cad.entities())
        self.assertTrue(res['success'])
        self.assertEqual('35A', res['evidence']['delete']['host_handle'])
        self.assertEqual(1.89, res['evidence']['delete']['host_hole_area_before'])
        self.assertEqual(0.0, res['evidence']['delete']['host_hole_area_after'])
        self.assertEqual('verified_host_hole_area_decrement', res['validation']['host'])

    def test_delete_raises_outcome_unknown_on_extra_removed_handle(self):
        wall = make_wall('35A', hole=0.0)
        cad = FakeCad({'35A': wall})
        col = make_col('3D3')
        col._on_delete = lambda _e: (cad.store.pop('3D3'), cad.store.pop('35A'))
        cad.store['3D3'] = col
        with self.assertRaisesRegex(OutcomeUnknown, 'Unexpected handle diff'):
            parts.delete_entity(cad, {'document_id': 'doc', 'handle': '3D3'}, cad.entities())


if __name__ == '__main__':
    unittest.main()

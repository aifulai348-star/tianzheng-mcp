import math
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, CommandCompletionTimeout, OutcomeUnknown
from tz_mcp.commands import SCHEMAS, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


REF = {'document_id': 'doc', 'handle': '3cf'}
EDIT_FIELDS = {
    'annotation.text_edit': {'text', 'height', 'rotation_deg'},
    'roof.edit': {'elevation', 'slope_angle'},
    'column.edit': {'width', 'depth', 'height'},
    'slab.edit': {'thickness', 'elevation'},
}
SAMPLES = {
    'annotation.text_edit': {'text': '新文字'},
    'roof.edit': {'slope_angle': 40},
    'column.edit': {'depth': 550},
    'slab.edit': {'thickness': 180},
    'opening.mirror': {'direction': 'left_right'},
}


class PrepareTests(unittest.TestCase):
    def test_capabilities_schemas_and_recovery(self):
        capabilities = cli.capabilities()
        self.assertEqual(71, len(capabilities))
        for op in SAMPLES:
            with self.subTest(op=op):
                required = set(REF)
                if op == 'opening.mirror':
                    required.add('direction')
                optional = EDIT_FIELDS.get(op, set())
                self.assertEqual((required, optional), SCHEMAS[op])
                self.assertEqual(sorted(required), capabilities[op]['required'])
                self.assertEqual(sorted(optional), capabilities[op]['optional'])
                self.assertTrue(cli.VALIDATION[op].startswith('verified_live'))
                self.assertEqual(op in EDIT_FIELDS, op in NO_ESCAPE_STEPS)

    def test_prepare_accepts_each_supported_field_and_normalizes_numbers(self):
        values = {'text': '字' * 160, 'height': 250, 'rotation_deg': -450, 'width': 400,
                  'depth': 600, 'slope_angle': 89.9, 'elevation': -200, 'thickness': 180}
        for op, fields in EDIT_FIELDS.items():
            for field in fields:
                with self.subTest(op=op, field=field):
                    args = prepare(op, {**REF, 'document_id': ' doc ', field: values[field]})
                    self.assertEqual({'document_id': 'doc', 'handle': '3CF', field: values[field]}, args)
                    if field != 'text':
                        self.assertIs(type(args[field]), float)
        for direction in ('left_right', 'inside_outside'):
            self.assertEqual({'document_id': 'doc', 'handle': '3CF', 'direction': direction},
                             prepare('opening.mirror', {**REF, 'direction': direction}))

    def test_prepare_rejects_invalid_references_empty_edits_and_unknown_fields(self):
        for op, changes in SAMPLES.items():
            valid = {**REF, **changes}
            invalid = [
                REF, {k: v for k, v in valid.items() if k != 'document_id'},
                {k: v for k, v in valid.items() if k != 'handle'},
                {**valid, 'document_id': ' '}, {**valid, 'document_id': None},
                {**valid, 'handle': 31}, {**valid, 'handle': ''}, {**valid, 'handle': '3G'},
                {**valid, 'handle': '0x3CF'}, {**valid, 'handle': '3CF\n'},
                {**valid, 'unexpected': False},
            ]
            for args in invalid:
                with self.subTest(op=op, args=args), self.assertRaises(ValueError):
                    prepare(op, args)

    def test_prepare_rejects_invalid_values(self):
        invalid_number = (True, '100', None, math.nan, math.inf, -math.inf, 10**400)
        for op, fields in EDIT_FIELDS.items():
            for field in fields:
                invalid = invalid_number
                if field in ('height', 'width', 'depth', 'thickness'):
                    invalid += (0, -1)
                if field == 'slope_angle':
                    invalid += (-1, 0, 90, 91)
                if field == 'text':
                    invalid = ('', 'x' * 161, 'a\rb', 'a\nb', 'a\x00b', 1, None)
                for value in invalid:
                    with self.subTest(op=op, field=field, value=value), self.assertRaises(ValueError):
                        prepare(op, {**REF, field: value})
        for direction in ('LEFT_RIGHT', 'inside', 'left_right ', None, True, ['left_right']):
            with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, 'direction'):
                prepare('opening.mirror', {**REF, 'direction': direction})


class FakeEntity:
    Handle = '3CF'

    def __init__(self, object_name, **properties):
        self.ObjectName = object_name
        self.__dict__.update(properties)
        self.writes = []
        self.updates = 0
        self.on_update = lambda: None

    def __setattr__(self, name, value):
        if 'writes' in self.__dict__ and name in (
            'TextString', 'Height', 'Rotation', 'Elevation', 'SlopeAngle', 'Width', 'Deep', 'Thickness', 'WinSill',
        ):
            self.writes.append((name, value))
        super().__setattr__(name, value)

    def Update(self):
        self.updates += 1
        self.on_update()


class FakeCad:
    document_id = 'doc'
    hwnd = 42

    def __init__(self):
        self.doc = object()
        self.events = []
        self.commands = []
        self.on_command = lambda: None
        self.host = SimpleNamespace(Handle='35A', ObjectName='TDbWall', HoleArea=1.89)
        self.other_wall = SimpleNamespace(Handle='35B', ObjectName='TDbWall', HoleArea=0.0)
        self.store = {e.Handle: e for e in (self.host, self.other_wall)}
        self.refs = [{'handle': '1F', 'type': 'BLOCK_RECORD'}, {'handle': '35A', 'type': 'TCH_WALL'}]
        self.bounds_override = None

    def guard(self):
        self.events.append('guard')
        self.prior_updates = {h: getattr(e, 'updates', 0) for h, e in self.store.items()}
        self.prior_commands = len(self.commands)

    def entities(self):
        self.events.append('entities')
        return self.store.copy()

    def entity(self, handle, document_id):
        self.events.append('entity')
        assert document_id == self.document_id
        return self.store[handle]

    def describe(self, entity):
        info = {'handle': entity.Handle, 'object_name': entity.ObjectName, 'document_id': self.document_id}
        if entity.ObjectName == 'TDbSlab':
            low, high = sorted((entity.Elevation, entity.Elevation + entity.Thickness))
            info['bounds'] = self.bounds_override or ((0, 0, low), (1000, 1000, high))
        return info

    def run_command(self, command):
        self.commands.append(command)
        self.on_command()


class EditTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        self.enterContext(patch.object(parts, 'require_drawing_window',
                                       side_effect=lambda hwnd: self.cad.events.append('window')))
        self.enterContext(patch.object(parts, 'require_model_space',
                                       side_effect=lambda doc: self.cad.events.append('model')))
        self.hosts = self.enterContext(patch.object(parts, 'entity_wall_hosts',
                                                    side_effect=lambda c, handle: self.cad.refs))

    def execute(self, op, **args):
        return Cad.execute(self.cad, op, {**REF, **args})

    def set_target(self, name, **props):
        entity = FakeEntity(name, **props)
        self.cad.store[entity.Handle] = entity
        return entity

    def install_dxf(self, parser, read):
        self.dxf_before = {}
        self.dxf_after = {}

        def snapshot(cad, handle):
            entity = cad.store[handle]
            mutated = entity.updates != cad.prior_updates[handle] or len(cad.commands) != cad.prior_commands
            changes = self.dxf_after if mutated else self.dxf_before
            return {**read(entity), **changes}

        return self.enterContext(patch.object(parts, parser, side_effect=snapshot))

    def setup_text(self):
        entity = self.set_target('AcDbText', TextString='Original', Height=250.0, Rotation=0.0)
        self.install_dxf('text_dxf', lambda e: {'type': 'TEXT', 'height': e.Height,
                                               'text': e.TextString, 'rotation': e.Rotation % math.tau})
        return entity

    def setup_roof(self):
        entity = self.set_target('TDbSlopeRoof', Elevation=-60.0, SlopeAngle=30.0)
        self.install_dxf('roof_dxf', lambda e: {
            'type': 'TCH_SLOPEROOF', 'elevation': e.Elevation,
            'vertices': [(0, 0), (10, 0), (10, 10), (0, 10)],
            'edge_slopes_rad': [math.radians(e.SlopeAngle), 0.4, 0.6, 0.7],
        })
        return entity

    def setup_column(self):
        entity = self.set_target('TDbColumn', SectionShapeText='矩形', Width=600.0, Deep=400.0, Height=3000.0)

        def read(e):
            # Put the depth edge first and rotate 30 degrees about the insertion.
            points = [(0.0, 0.0), (0.0, e.Deep), (e.Width, e.Deep), (e.Width, 0.0)]
            sin, cos = 0.5, math.sqrt(3) / 2
            vertices = [(500 + x * cos - y * sin, 700 + x * sin + y * cos) for x, y in points]
            return {'type': 'TCH_COLUMN', 'insertion': (500, 700), 'height': e.Height,
                    'vertices': vertices, 'flags': (0, 60, 0), 'bulges': [0.0] * 4}

        self.install_dxf('column_dxf', read)
        return entity

    def setup_slab(self, thickness=-120.0):
        entity = self.set_target('TDbSlab', Thickness=thickness, Elevation=10.0)
        self.install_dxf('slab_dxf', lambda e: {'type': 'TCH_SLAB', 'thickness': e.Thickness,
                                               'elevation': e.Elevation})
        return entity

    def setup_opening(self):
        entity = self.set_target('TDbOpening', Width=900.0, Height=2100.0, WinSill=0.0)

        def read(e):
            inside_outside = self.cad.commands and 'TMirWinIO' in self.cad.commands[-1]
            flag = (3 if inside_outside else 1) if self.cad.commands else 0
            return {'type': 'TCH_OPENING', 'mirror_flag': flag, 'insertion': (3000.0, 0.0),
                    'swing_point': (3000.0, -950.0 if inside_outside else 950.0)}

        self.install_dxf('opening_dxf', read)
        return entity

    def assert_no_writes(self, entity):
        self.assertEqual([], entity.writes)
        self.assertEqual(0, entity.updates)
        self.assertEqual([], self.cad.commands)

    def test_guards_refuse_wrong_type_foreign_document_and_unknown_handle_for_each_operation(self):
        target = self.set_target('AcDbCircle')
        for op, args in SAMPLES.items():
            for changes, error in (({}, 'native'), ({'document_id': 'other'}, 'document_id'),
                                   ({'handle': 'ABC'}, 'Unknown handle')):
                with self.subTest(op=op, changes=changes), self.assertRaisesRegex(ValueError, error):
                    self.execute(op, **args, **changes)
                self.assert_no_writes(target)
        self.hosts.assert_not_called()
        self.assertEqual(['guard', 'window', 'model', 'entities', 'entity'], self.cad.events[:5])

    def test_text_edit_updates_requested_properties_wraps_rotation_and_records_evidence(self):
        entity = self.setup_text()
        entity.on_update = lambda: object.__setattr__(entity, 'Rotation', entity.Rotation % math.tau)
        result = self.execute('annotation.text_edit', text='修订 (A)', rotation_deg=-90, height=300)
        self.assertEqual([('TextString', '修订 (A)'), ('Height', 300.0), ('Rotation', -math.pi / 2)], entity.writes)
        self.assertEqual(1, entity.updates)
        self.assertTrue(result['success'])
        self.assertEqual(['3CF'], result['handles'])
        self.assertEqual('doc', result['document_id'])
        self.assertEqual([self.cad.describe(entity)], result['entities'])
        self.assertEqual({'type': True, 'properties': True, 'dxf': 'verified_1_40_50', 'reopen': 'pending'},
                         result['validation'])
        edit = result['evidence']['edit']
        self.assertEqual(dict(entity.writes), edit['properties_requested'])
        self.assertEqual('Original', edit['properties_before']['TextString'])
        self.assertEqual(250.0, edit['dxf_before']['height'])
        self.assertEqual('修订 (A)', edit['dxf_after']['text'])
        self.assertEqual(-90.0, edit['rotation_deg'])
        self.assertEqual(-math.pi / 2, edit['rotation_rad'])
        self.assertAlmostEqual(1.5 * math.pi, edit['properties_after']['Rotation'])

    def test_text_height_only_preserves_text_and_rotation(self):
        entity = self.setup_text()
        result = self.execute('annotation.text_edit', height=300)
        self.assertEqual([('Height', 300.0)], entity.writes)
        self.assertEqual('Original', result['evidence']['edit']['properties_after']['TextString'])
        self.assertEqual(0.0, result['evidence']['edit']['properties_after']['Rotation'])

    def test_text_edit_refuses_invalid_baseline_without_mutating(self):
        entity = self.setup_text()
        self.dxf_before['type'] = 'MTEXT'
        with self.assertRaisesRegex(OutcomeUnknown, 'DXF type'):
            self.execute('annotation.text_edit', text='Edited')
        object.__setattr__(entity, 'Height', math.nan)
        with self.assertRaisesRegex(OutcomeUnknown, 'COM Height'):
            self.execute('annotation.text_edit', text='Edited')
        self.assert_no_writes(entity)

    def test_text_edit_detects_com_mismatch_and_write_exception(self):
        entity = self.setup_text()
        entity.on_update = lambda: object.__setattr__(entity, 'Height', 299.98)
        with self.assertRaisesRegex(OutcomeUnknown, 'COM Height'):
            self.execute('annotation.text_edit', height=300)
        error = RuntimeError('Update failed after the setter ran')
        entity.on_update = Mock(side_effect=error)
        with self.assertRaises(OutcomeUnknown) as result:
            self.execute('annotation.text_edit', height=300)
        self.assertIs(error, result.exception.__cause__)

    def test_text_edit_detects_dxf_mismatches(self):
        self.setup_text()
        for key, value, label in (('text', 'stale', 'DXF type or text'), ('height', 298, 'DXF 40'),
                                  ('rotation', 0.1, 'DXF 50'), ('type', 'MTEXT', 'DXF type')):
            with self.subTest(key=key), self.assertRaisesRegex(OutcomeUnknown, label):
                self.dxf_after = {key: value}
                self.execute('annotation.text_edit', height=300)

    def test_roof_primary_slope_and_elevation_edit(self):
        entity = self.setup_roof()
        evidence = self.execute('roof.edit', slope_angle=45, elevation=0)['evidence']['edit']
        self.assertEqual([('Elevation', 0.0), ('SlopeAngle', 45.0)], entity.writes)
        self.assertEqual(1, entity.updates)
        self.assertEqual(-60.0, evidence['properties_before']['Elevation'])
        self.assertEqual(0.0, evidence['dxf_after']['elevation'])
        self.assertEqual([math.pi / 4, 0.4, 0.6, 0.7], evidence['dxf_after']['edge_slopes_rad'])
        self.assertEqual('primary_edge_dxf_51_0', evidence['slope_angle_scope'])

    def test_roof_elevation_only_preserves_slopes(self):
        entity = self.setup_roof()
        evidence = self.execute('roof.edit', elevation=-250)['evidence']['edit']
        self.assertEqual([('Elevation', -250.0)], entity.writes)
        self.assertEqual(evidence['dxf_before']['edge_slopes_rad'], evidence['dxf_after']['edge_slopes_rad'])

    def test_roof_edit_refuses_missing_baseline_primary_slope(self):
        entity = self.setup_roof()
        self.dxf_before['edge_slopes_rad'] = []
        with self.assertRaisesRegex(OutcomeUnknown, 'edge slopes'):
            self.execute('roof.edit', elevation=-100)
        self.assert_no_writes(entity)

    def test_roof_edit_detects_dxf_and_com_mismatches(self):
        entity = self.setup_roof()
        for changes, error in (
            ({'edge_slopes_rad': [math.radians(30), 0.4, 0.6, 0.7]}, r'DXF 51\[0\]'),
            ({'edge_slopes_rad': [math.pi / 4, 0.41, 0.6, 0.7]}, r'DXF 51\[1\]'),
            ({'edge_slopes_rad': [math.pi / 4]}, 'edge slope count'),
            ({'vertices': []}, 'vertex count'),
            ({'elevation': -60}, 'DXF 148'),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(OutcomeUnknown, error):
                self.dxf_after = changes
                self.execute('roof.edit', elevation=-100, slope_angle=45)
        entity.on_update = lambda: object.__setattr__(entity, 'SlopeAngle', 44.94)
        with self.assertRaisesRegex(OutcomeUnknown, 'COM SlopeAngle'):
            self.execute('roof.edit', slope_angle=45)

    def test_column_edit_maps_depth_and_verifies_rotated_rectangle(self):
        entity = self.setup_column()
        result = self.execute('column.edit', width=700, depth=450, height=3600)
        self.assertEqual([('Width', 700.0), ('Deep', 450.0), ('Height', 3600.0)], entity.writes)
        self.assertEqual(1, entity.updates)
        evidence = result['evidence']['edit']
        self.assertEqual(3600.0, evidence['dxf_after']['height'])
        self.assertEqual(evidence['dxf_before']['insertion'], evidence['dxf_after']['insertion'])
        self.assertEqual(evidence['dxf_before']['flags'], evidence['dxf_after']['flags'])
        self.assertEqual(evidence['dxf_before']['bulges'], evidence['dxf_after']['bulges'])

    def test_column_edit_refuses_non_rectangular_section_or_incomplete_baseline(self):
        entity = self.setup_column()
        entity.SectionShapeText = '圆形'
        with self.assertRaisesRegex(ValueError, 'rectangular native column'):
            self.execute('column.edit', height=3500)
        entity.SectionShapeText = '矩形'
        for field, value in (('flags', (0, 60, None)), ('insertion', None), ('height', None),
                             ('vertices', [(0, 0)]), ('bulges', [0.0])):
            with self.subTest(field=field), self.assertRaisesRegex(OutcomeUnknown, 'DXF'):
                self.dxf_before = {field: value}
                self.execute('column.edit', height=3500)
        self.assert_no_writes(entity)

    def test_column_edit_detects_com_and_dxf_mismatch(self):
        entity = self.setup_column()
        for key, value, error in (
            ('insertion', (503, 702), 'DXF insertion'),
            ('flags', (0, 61, 0), 'DXF flags'),
            ('bulges', [0.1, 0, 0, 0], 'DXF bulge'),
            ('height', 2998, 'DXF 149'),
            ('vertices', [(0, 0), (0, 450), (590, 450), (600, 0)], 'opposite side length'),
            ('vertices', [(0, 0), (0, 480), (600, 480), (600, 0)], 'DXF side length'),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(OutcomeUnknown, error):
                self.dxf_after = {key: value}
                self.execute('column.edit', depth=450)
        entity.on_update = lambda: object.__setattr__(entity, 'Deep', 449.98)
        with self.assertRaisesRegex(OutcomeUnknown, 'COM Deep'):
            self.execute('column.edit', depth=450)

    def test_slab_thickness_preserves_direction_including_zero_default(self):
        self.setup_slab()
        for before, expected in ((-120.0, -180.0), (120.0, 180.0), (0.0, -180.0)):
            with self.subTest(before=before):
                entity = self.set_target('TDbSlab', Thickness=before, Elevation=10.0)
                result = self.execute('slab.edit', thickness=180, elevation=-100)
                self.assertEqual([('Thickness', expected), ('Elevation', -100.0)], entity.writes)
                self.assertEqual(1, entity.updates)
                evidence = result['evidence']['edit']
                self.assertEqual(expected, evidence['dxf_after']['thickness'])
                lo, hi = evidence['bounds_after']
                self.assertEqual(180.0, hi[2] - lo[2])
                self.assertEqual(-100.0, hi[2] if expected < 0 else lo[2])

    def test_slab_elevation_only_preserves_signed_thickness(self):
        entity = self.setup_slab()
        evidence = self.execute('slab.edit', elevation=0)['evidence']['edit']
        self.assertEqual([('Elevation', 0.0)], entity.writes)
        self.assertEqual(-120.0, evidence['properties_after']['Thickness'])
        self.assertEqual(0.0, evidence['bounds_after'][1][2])

    def test_slab_edit_detects_bad_dxf_and_wrong_bounds_span_or_face(self):
        entity = self.setup_slab()
        for changes, box, error in (
            ({'thickness': 180}, None, 'DXF 149'),
            ({'elevation': -1}, None, 'DXF 148'),
            ({}, ((0, 0, -170), (1000, 1000, 0)), 'Z-span'),
            ({}, ((0, 0, -160), (1000, 1000, 20)), 'reference Z face'),
        ):
            with self.subTest(changes=changes, box=box), self.assertRaisesRegex(OutcomeUnknown, error):
                self.dxf_after = changes
                entity.on_update = lambda: setattr(self.cad, 'bounds_override', box)
                self.execute('slab.edit', thickness=180, elevation=0)
        entity.on_update = lambda: object.__setattr__(entity, 'Thickness', 180.0)
        with self.assertRaisesRegex(OutcomeUnknown, 'COM Thickness'):
            self.execute('slab.edit', thickness=180)

    def test_edit_detects_handle_and_object_type_changes(self):
        entity = self.setup_text()
        entity.on_update = lambda: self.cad.store.pop('35B')
        with self.assertRaisesRegex(OutcomeUnknown, 'handles changed'):
            self.execute('annotation.text_edit', text='Edited')
        entity.on_update = lambda: setattr(entity, 'ObjectName', 'AcDbMText')
        with self.assertRaisesRegex(OutcomeUnknown, 'COM type changed'):
            self.execute('annotation.text_edit', text='Edited')

    def test_mirror_commands_preserve_properties_and_hole_area(self):
        entity = self.setup_opening()
        for direction, name, flag in (('left_right', 'TMirWinLR', 1), ('inside_outside', 'TMirWinIO', 3)):
            with self.subTest(direction=direction):
                self.cad.commands.clear()
                result = self.execute('opening.mirror', direction=direction)
                self.assertEqual([f'(progn (command "{name}" (handent "3CF") "") (princ))\n'], self.cad.commands)
                evidence = result['evidence']['mirror']
                self.assertEqual('35A', evidence['host_handle'])
                self.assertEqual(evidence['properties_before'], evidence['properties_after'])
                self.assertEqual(evidence['hole_areas_before'], evidence['hole_areas_after'])
                self.assertEqual(flag, evidence['dxf_after']['mirror_flag'])
                if direction == 'inside_outside':
                    self.assertEqual((3000.0, -950.0), evidence['dxf_after']['swing_point'])
        self.assertEqual([], entity.writes)
        self.assertEqual(0, entity.updates)

    def test_mirror_refuses_missing_or_ambiguous_host(self):
        entity = self.setup_opening()
        for refs in ([], [{'handle': '1F', 'type': 'BLOCK_RECORD'}],
                     [{'handle': '35A', 'type': 'TCH_WALL'}, {'handle': '35B', 'type': 'TCH_WALL'}]):
            self.cad.refs = refs
            with self.subTest(refs=refs), self.assertRaisesRegex(ValueError, 'unique TCH_WALL'):
                self.execute('opening.mirror', direction='left_right')
            self.assert_no_writes(entity)
        self.cad.refs = [{'handle': '999', 'type': 'TCH_WALL'}]
        with self.assertRaisesRegex(ValueError, 'native wall in ModelSpace'):
            self.execute('opening.mirror', direction='left_right')
        self.assert_no_writes(entity)

    def test_mirror_refuses_missing_dxf_fields_or_nonfinite_baseline(self):
        entity = self.setup_opening()
        for change, error in (({'mirror_flag': None}, 'mirror flag'), ({'insertion': (math.nan, 0)}, 'insertion'),
                              ({'swing_point': None}, 'swing point'), ({'swing_point': (0, math.inf)}, 'swing point')):
            self.dxf_before = change
            with self.subTest(change=change), self.assertRaisesRegex(OutcomeUnknown, error):
                self.execute('opening.mirror', direction='inside_outside')
            self.assert_no_writes(entity)
        self.dxf_before = {}
        self.cad.other_wall.HoleArea = math.nan
        with self.assertRaisesRegex(OutcomeUnknown, 'HoleArea'):
            self.execute('opening.mirror', direction='left_right')
        self.assert_no_writes(entity)

    def test_left_right_mirror_accepts_a_missing_swing_point(self):
        self.setup_opening()
        self.dxf_before['swing_point'] = self.dxf_after['swing_point'] = None
        self.assertTrue(self.execute('opening.mirror', direction='left_right')['success'])

    def test_mirror_detects_com_dxf_and_host_changes(self):
        entity = self.setup_opening()
        for fields, error in (({'mirror_flag': 0}, 'flag did not change'),
                              ({'insertion': (3010, 0)}, 'insertion shift'),
                              ({'swing_point': (3000, 949.1)}, 'swing point did not change'),
                              ({'swing_point': (math.inf, 0)}, 'swing point')):
            self.cad.commands.clear()
            self.dxf_after = fields
            with self.subTest(fields=fields), self.assertRaisesRegex(OutcomeUnknown, error):
                self.execute('opening.mirror', direction='inside_outside')
        self.dxf_after = {}
        self.cad.commands.clear()
        self.cad.on_command = lambda: object.__setattr__(entity, 'WinSill', 1.0)
        with self.assertRaisesRegex(OutcomeUnknown, 'COM WinSill'):
            self.execute('opening.mirror', direction='left_right')
        self.cad.commands.clear()
        self.cad.on_command = lambda: setattr(self.cad, 'refs', [{'handle': '35B', 'type': 'TCH_WALL'}])
        with self.assertRaisesRegex(OutcomeUnknown, 'host changed'):
            self.execute('opening.mirror', direction='left_right')

    def test_mirror_detects_hole_area_change_on_host_or_unrelated_wall(self):
        self.setup_opening()
        for wall in (self.cad.host, self.cad.other_wall):
            self.cad.commands.clear()
            original = wall.HoleArea
            self.cad.on_command = lambda: setattr(wall, 'HoleArea', original + 0.03)
            with self.subTest(wall=wall.Handle), patch.object(parts, 'time') as clock:
                clock.monotonic.side_effect = [0, 6]
                with self.assertRaisesRegex(OutcomeUnknown, 'hole-area update mismatch'):
                    self.execute('opening.mirror', direction='left_right')
            wall.HoleArea = original

    def test_mirror_preserves_command_completion_timeout_for_escape_recovery(self):
        self.setup_opening()
        timeout = CommandCompletionTimeout('Command completion not confirmed')
        self.cad.on_command = Mock(side_effect=timeout)
        with self.assertRaises(CommandCompletionTimeout) as failure:
            self.execute('opening.mirror', direction='left_right')
        self.assertIs(timeout, failure.exception)

    def test_annotation_create_retries_both_document_dispatch_reads(self):
        cad = self.cad

        class TransientText:
            Handle = '3CF'
            ObjectName = 'AcDbText'
            attempts = 0

            @property
            def TextString(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise AttributeError('<unknown>.TextString')
                return 'Hello'

        class TransientDocument:
            attempts = 0
            adds = []

            @property
            def ModelSpace(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise AttributeError('<unknown>.ModelSpace')
                return self

            def AddText(self, text, point, height):
                self.adds.append((text, point, height))
                cad.store['3CF'] = TransientText()

        cad.doc = TransientDocument()
        with patch.object(parts, 'vector', side_effect=tuple), patch('tz_mcp.cad.time.sleep'):
            result = Cad.execute(cad, 'annotation.text', {'text': 'Hello', 'point': [5, 10], 'height': 250})
        self.assertTrue(result['success'])
        self.assertEqual(2, cad.doc.attempts)
        self.assertEqual(2, cad.store['3CF'].attempts)
        self.assertEqual([('Hello', (5, 10, 0), 250)], cad.doc.adds)


class DxfParserTests(unittest.TestCase):
    def read_dxf(self, parser, data):
        def run(lisp):
            self.assertIn('(entget', lisp)
            self.assertIn('(handent "3CF")', lisp)
            match = re.search(r'\(open "([^"]+)" "w"', lisp)
            self.assertIsNotNone(match)
            Path(match[1]).write_bytes(data)

        with TemporaryDirectory() as folder:
            return parser(SimpleNamespace(run_command=run), '3CF', folder=folder)

    def test_opening_keeps_legacy_fields_and_reads_mirror_and_swing(self):
        base = '((0 . TCH_OPENING) (5 . 3CF) (8 . DOOR) (10 3000.0 -4.25 0.0) (40 . 900) (41 . 2100) (50 . 0.1)'
        legacy = {'type': 'TCH_OPENING', 'handle': '3CF', 'layer': 'DOOR', 'insertion': (3000.0, -4.25),
                  'width': 900.0, 'height': 2100.0, 'rotation': 0.1, 'host': '35A'}
        for extra, flag, swing in (('', None, None), (' (70 . 3.0) (11 3e3 -9.5E+2 0)', 3, (3000.0, -950.0)),
                                   (' (70 . 0) (11 0 0 0)', 0, (0.0, 0.0))):
            with self.subTest(extra=extra):
                expected = {**legacy, 'mirror_flag': flag, 'swing_point': swing}
                self.assertEqual(expected, self.read_dxf(panel.opening_dxf, f'{base}{extra})\nHOST 35a'.encode('gbk')))
        self.assertIsNone(self.read_dxf(panel.opening_dxf, f'{base})\nHOST ?'.encode('gbk'))['host'])

    def test_text_dxf_preserves_literal_text_and_reads_radians(self):
        for value, encoding in (('  标签 ") (50 . 9)  ', 'gbk'), ('  ✨\u2028注释  ', 'utf-8-sig')):
            with self.subTest(encoding=encoding):
                data = f'0 TEXT\r\n5 3CF\r\n8 文字层\r\n1 {value}\r\n40 250.5\r\n50 1.57079632679\r\n'
                self.assertEqual({'type': 'TEXT', 'handle': '3CF', 'layer': '文字层', 'text': value,
                                  'height': 250.5, 'rotation': 1.57079632679},
                                 self.read_dxf(panel.text_dxf, data.encode(encoding)))

    def test_slab_dxf_returns_signed_thickness_and_distinguishes_missing_from_zero(self):
        data = b'0 TCH_SLAB\n5 3CF\n8 SLAB\n148 0.0\n149 -1.2e2\n'
        self.assertEqual({'type': 'TCH_SLAB', 'handle': '3CF', 'layer': 'SLAB', 'elevation': 0.0, 'thickness': -120.0},
                         self.read_dxf(panel.slab_dxf, data))
        self.assertIsNone(self.read_dxf(panel.slab_dxf, b'0 TCH_SLAB\n148 0\n')['thickness'])


if __name__ == '__main__':
    unittest.main()

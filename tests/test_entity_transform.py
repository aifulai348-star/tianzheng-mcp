import math
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tz_mcp import panel, parts
from tz_mcp.cad import Cad, CommandCompletionTimeout, OutcomeUnknown
from tz_mcp.cli import capabilities
from tz_mcp.commands import prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


REF = {'document_id': ' drawing ', 'handle': '0a3'}
TRANSFORMS = {
    'entity.move': {'delta': [-3.5, 4.25]},
    'entity.rotate': {'base_point': [10, 20, 0], 'angle_deg': 90},
}
COMMANDS = {
    'entity.move': ('(progn (command "_.MOVE" (handent "A3") "" "_non" (list 0.0 0.0 0.0) '
                    '"_non" (list -3.500000 4.250000 0.0)) (princ))\n'),
    'entity.rotate': ('(progn (command "_.ROTATE" (handent "A3") "" "_non" '
                      '(list 10.000000 20.000000 0.0) 90.000000) (princ))\n'),
}
ARC_START = 5.5 + math.pi / 2 - math.tau
TEXT_ROTATION = 5.75 + math.pi / 2 - math.tau
TEXT = '  标签 ") (50 . 9)\u2028  '
CASES = {
    'AcDbLine': (
        '0 LINE\n10 (12 21 0)\n11 (15 20 0)\n',
        '0 LINE\n10 (8.5 25.25 0)\n11 (11.5 24.25 0)\n',
        '0 LINE\n10 (9 22 0)\n11 (10 25 0)\n',
        {'type': 'LINE', 'start': (12, 21), 'end': (15, 20)},
    ),
    'AcDbCircle': (
        '0 CIRCLE\n10 (12 21 0)\n40 10\n',
        '0 CIRCLE\n10 (8.5 25.25 0)\n40 10\n',
        '0 CIRCLE\n10 (9 22 0)\n40 10\n',
        {'type': 'CIRCLE', 'center': (12, 21), 'radius': 10},
    ),
    'AcDbArc': (
        '0 ARC\n10 (12 21 0)\n40 10\n50 5.5\n51 0.25\n',
        f'0 ARC\n10 (8.5 25.25 0)\n40 10\n50 {5.5 - math.tau}\n51 0.25\n',
        f'0 ARC\n10 (9 22 0)\n40 10\n50 {ARC_START}\n51 {0.25 + math.pi / 2}\n',
        {'type': 'ARC', 'center': (12, 21), 'radius': 10,
         'start_angle_rad': 5.5, 'end_angle_rad': 0.25},
    ),
    'AcDbText': (
        f'0 TEXT\n10 (12 21 0)\n1 {TEXT}\n40 2.5\n50 5.75\n',
        f'0 TEXT\n10 (8.5 25.25 0)\n1 {TEXT}\n40 2.5\n50 5.75\n',
        f'0 TEXT\n10 (9 22 0)\n1 {TEXT}\n40 2.5\n50 {TEXT_ROTATION}\n',
        {'type': 'TEXT', 'insertion': (12, 21), 'text': TEXT, 'height': 2.5, 'rotation': 5.75},
    ),
    'AcDbPolyline': (
        '0 LWPOLYLINE\n70 129\n10 (12 21)\n42 0.5\n10 (15 20)\n10 (11 23)\n',
        '0 LWPOLYLINE\n70 1\n10 (8.5 25.25)\n10 (11.5 24.25)\n10 (7.5 27.25)\n',
        '0 LWPOLYLINE\n70 129\n10 (9 22)\n10 (10 25)\n10 (7 21)\n',
        {'type': 'LWPOLYLINE', 'vertices': [(12, 21), (15, 20), (11, 23)], 'closed': 1},
    ),
}


class FakeCad:
    document_id = 'drawing'
    hwnd = 42
    doc = object()

    def __init__(self, object_name, before, after):
        self.store = {'A3': SimpleNamespace(ObjectName=object_name),
                      'A4': SimpleNamespace(ObjectName='TDbWall')}
        self.before = before
        self.after = after
        self.commands = []
        self.dumps = 0
        self.on_write = lambda: None

    def guard(self):
        pass

    def entities(self):
        return self.store.copy()

    def entity(self, handle, document_id):
        assert document_id == self.document_id
        return self.store[handle]

    def describe(self, entity):
        return {'handle': 'A3', 'object_name': entity.ObjectName, 'document_id': self.document_id}

    def run_command(self, command):
        dump = re.search(r'\(open "([^"]+)" "w"', command)
        if dump:
            assert '(entget (handent "A3"))' in command
            self.dumps += 1
            data = self.after if self.commands else self.before
            Path(dump[1]).write_bytes(data.replace('\n', '\r\n').encode('utf-8-sig'))
        else:
            self.commands.append(command)
            self.on_write()


class TransformTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'require_model_space'))
        self.enterContext(patch.object(parts, 'transform_dxf', side_effect=lambda c, handle, name:
                                      panel.transform_dxf(c, handle, name, folder=folder)))

    def execute(self, cad, op, **overrides):
        return Cad.execute(cad, op, {**REF, **TRANSFORMS[op], **overrides})

    def test_registration_normalizes_inputs_and_allows_escape_recovery(self):
        listed = capabilities()
        for op, parameters in TRANSFORMS.items():
            with self.subTest(op=op):
                self.assertEqual(set(REF) | parameters.keys(), set(listed[op]['required']))
                self.assertEqual([], listed[op]['optional'])
                self.assertTrue(listed[op]['validation'].startswith('verified_live'))
                self.assertNotIn(op, NO_ESCAPE_STEPS)
        self.assertEqual({'document_id': 'drawing', 'handle': '0A3', 'delta': [-3.5, 4.25, 0.0]},
                         prepare('entity.move', {**REF, **TRANSFORMS['entity.move']}))
        self.assertEqual({'document_id': 'drawing', 'handle': '0A3', 'base_point': [10.0, 20.0, 0.0],
                          'angle_deg': -450.0},
                         prepare('entity.rotate', {**REF, 'base_point': (10, 20), 'angle_deg': -450}))

    def test_both_transforms_verify_every_supported_type(self):
        for op, after_index in (('entity.move', 1), ('entity.rotate', 2)):
            for name, case in CASES.items():
                with self.subTest(op=op, name=name):
                    cad = FakeCad(name, case[0], case[after_index])
                    result = self.execute(cad, op)
                    self.assertTrue(result['success'])
                    self.assertEqual(['A3'], result['handles'])
                    self.assertEqual('drawing', result['document_id'])
                    self.assertEqual(name, result['entities'][0]['object_name'])
                    self.assertEqual('verified_dxf', result['validation']['geometry'])
                    evidence = result['evidence'][op.split('.')[1]]
                    self.assertEqual(case[3], evidence['dxf_before'])
                    self.assertEqual(case[3]['type'], evidence['dxf_after']['type'])
                    self.assertEqual([COMMANDS[op]], cad.commands)
                    self.assertEqual(2, cad.dumps)

    def test_negative_rotation_and_open_polyline(self):
        cad = FakeCad('AcDbPolyline', '0 LWPOLYLINE\n70 128\n10 (12 21)\n10 (15 20)\n',
                      '0 LWPOLYLINE\n70 0\n10 (11 18)\n10 (10 15)\n')
        result = self.execute(cad, 'entity.rotate', angle_deg=-450)
        self.assertEqual(0, result['evidence']['rotate']['dxf_after']['closed'])
        self.assertIn(' -450.000000)', cad.commands[0])

    def test_refuses_unsupported_types_without_dump_or_write(self):
        for op in TRANSFORMS:
            for name in ('TDbWall', 'TDbColumn', 'TDbOpening', 'TDbSlab', 'TDbSlopeRoof', 'AcDbMText'):
                with self.subTest(op=op, name=name):
                    cad = FakeCad(name, '', '')
                    with self.assertRaisesRegex(ValueError, f'{op} does not support ObjectName {name}'):
                        self.execute(cad, op)
                    self.assertEqual([], cad.commands)
                    self.assertEqual(0, cad.dumps)

    def test_refuses_invalid_schema_values_and_foreign_targets(self):
        invalid_common = (('document_id', '  '), ('document_id', None), ('document_id', 'foreign'),
                          ('handle', ''), ('handle', '0xa3'), ('handle', 'A3\n'),
                          ('handle', 'xyz'), ('handle', 'B1'), ('handle', 163))
        invalid_specific = {
            'entity.move': tuple(('delta', value) for value in
                                 ([0, 0], [1e-6, -1e-6], [1, 2, 1], [1], [True, 2],
                                  [math.inf, 0], [math.nan, 0], [10**400, 0])),
            'entity.rotate': (tuple(('base_point', value) for value in
                                    ([1, 2, 0.1], [1], [False, 0], [0, math.inf], [10**400, 0]))
                              + tuple(('angle_deg', value) for value in
                                      (True, '90', math.inf, math.nan, 10**400, 0, -360, 720, 1e-6,
                                       360 - 0.5e-6))),
        }
        for op in TRANSFORMS:
            for key, value in invalid_common + invalid_specific[op]:
                with self.subTest(op=op, key=key, value=value):
                    cad = FakeCad('AcDbLine', '', '')
                    with self.assertRaises(ValueError):
                        self.execute(cad, op, **{key: value})
                    self.assertEqual([], cad.commands)
                    self.assertEqual(0, cad.dumps)

    def test_readback_rejects_changed_handles_and_object_name(self):
        for op, after_index in (('entity.move', 1), ('entity.rotate', 2)):
            changes = (
                ('handles changed', lambda store: store.pop('A3')),
                ('handles changed', lambda store: store.update(A5=SimpleNamespace(ObjectName='AcDbLine'))),
                ('COM type changed', lambda store: setattr(store['A3'], 'ObjectName', 'AcDbArc')),
            )
            for message, change in changes:
                with self.subTest(op=op, change=change):
                    case = CASES['AcDbLine']
                    cad = FakeCad('AcDbLine', case[0], case[after_index])
                    cad.on_write = lambda: change(cad.store)
                    with self.assertRaisesRegex(OutcomeUnknown, message):
                        self.execute(cad, op)
                    self.assertEqual([COMMANDS[op]], cad.commands)
                    self.assertEqual(1, cad.dumps)

    def test_readback_rejects_changed_dxf_geometry_and_attributes(self):
        corruptions = (
            ('AcDbLine', '10 (8.5 25.25 0)', '10 (8.6 25.25 0)', 'DXF start'),
            ('AcDbLine', '11 (11.5 24.25 0)', '11 (11.5 24.15 0)', 'DXF end'),
            ('AcDbCircle', '40 10', '40 10.1', 'DXF radius'),
            ('AcDbArc', '50 ', '52 ', 'DXF start_angle_rad'),
            ('AcDbArc', '51 0.25', '51 0.26', 'DXF end_angle_rad'),
            ('AcDbText', '1 ' + TEXT, '1 different', 'DXF text'),
            ('AcDbText', '40 2.5', '40 2.4', 'DXF height'),
            ('AcDbText', '50 5.75', '50 5.74', 'DXF rotation'),
            ('AcDbPolyline', '70 1', '70 128', 'DXF closed'),
            ('AcDbPolyline', '10 (7.5 27.25)\n', '', 'DXF vertex count'),
            ('AcDbPolyline', '10 (7.5 27.25)', '10 (7.6 27.25)', 'DXF vertex 2'),
        )
        for name, old, new, message in corruptions:
            with self.subTest(name=name, message=message):
                before, moved, _, _ = CASES[name]
                self.assertIn(old, moved)
                cad = FakeCad(name, before, moved.replace(old, new))
                with self.assertRaisesRegex(OutcomeUnknown, message):
                    self.execute(cad, 'entity.move')
                self.assertEqual([COMMANDS['entity.move']], cad.commands)
                self.assertEqual(2, cad.dumps)

        before, _, rotated, _ = CASES['AcDbArc']
        cad = FakeCad('AcDbArc', before, rotated.replace(str(ARC_START), '5.5'))
        with self.assertRaisesRegex(OutcomeUnknown, 'DXF start_angle_rad'):
            self.execute(cad, 'entity.rotate')

    def test_invalid_baseline_fails_before_write(self):
        case = CASES['AcDbCircle']
        cad = FakeCad('AcDbCircle', case[0].replace('40 10', '40 nan'), case[1])
        with self.assertRaisesRegex(OutcomeUnknown, 'DXF radius'):
            self.execute(cad, 'entity.move')
        self.assertEqual([], cad.commands)
        self.assertEqual(1, cad.dumps)

    def test_command_completion_timeout_passes_through(self):
        for op in TRANSFORMS:
            with self.subTest(op=op):
                case = CASES['AcDbLine']
                cad = FakeCad('AcDbLine', case[0], '')
                timeout = CommandCompletionTimeout('Completion not confirmed')
                cad.on_write = self.enterContext(patch.object(cad, 'on_write', side_effect=timeout))
                with self.assertRaises(CommandCompletionTimeout) as caught:
                    self.execute(cad, op)
                self.assertIs(timeout, caught.exception)
                self.assertEqual([COMMANDS[op]], cad.commands)
                self.assertEqual(1, cad.dumps)


class TransformDxfTests(unittest.TestCase):
    def test_parser_reads_all_five_types(self):
        with TemporaryDirectory() as folder:
            for name, (before, _, _, snapshot) in CASES.items():
                with self.subTest(name=name):
                    cad = FakeCad(name, before, '')
                    self.assertEqual(snapshot, panel.transform_dxf(cad, 'A3', name, folder=folder))
                    self.assertEqual([], cad.commands)
                    self.assertEqual(1, cad.dumps)

    def test_parser_rejects_wrong_type_missing_groups_and_invalid_coordinates(self):
        cases = (
            ('AcDbLine', '0 CIRCLE\n', 'DXF type mismatch'),
            ('AcDbLine', '0 LINE\n11 (2 3)\n', 'DXF start'),
            ('AcDbLine', '0 LINE\n10 (inf 1)\n11 (2 3)\n', 'Expected a finite DXF point'),
            ('AcDbText', '0 TEXT\n10 (5 6)\n40 2\n50 0\n', 'DXF text'),
            ('AcDbPolyline', '0 LWPOLYLINE\n70 1\n10 (1 2)\n', 'at least two DXF vertices'),
            ('AcDbPolyline', '0 LWPOLYLINE\n10 (1 2)\n10 (2 3)\n', 'DXF closed'),
        )
        with TemporaryDirectory() as folder:
            for name, raw, message in cases:
                with self.subTest(name=name, message=message):
                    with self.assertRaisesRegex(OutcomeUnknown, message):
                        panel.transform_dxf(FakeCad(name, raw, ''), 'A3', name, folder=folder)

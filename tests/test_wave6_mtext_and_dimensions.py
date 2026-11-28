import math
import sys
import unittest
from functools import partial
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_primitives_and_layers import CallRejected, Document, FakeCad as BaseCad, Properties
from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, OutcomeUnknown, retry_document_read
from tz_mcp.commands import prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


MTEXT = {'point': [10, 20], 'width': 300, 'text': r' 中文 \P "A (B)" '}
ALIGNED = {'ext1': [1, 2], 'ext2': [4, 6], 'text_position': [0, 9]}
LINEAR = {'ext1': [1, 2], 'ext2': [4, 6], 'dim_line_point': [0, 9]}
RADIAL = {'center': [-1, 2], 'chord_point': [5, 10]}
ENTITY_DXF = '5 B\n8 0\n'
MTEXT_DXF = ('0 MTEXT\n' + ENTITY_DXF + f'1 {MTEXT["text"]}\n'
             '10 (10 20 0)\n40 25\n41 300\n50 0\n71 1\n')
EXTENSIONS_DXF = '13 (1 2 0)\n14 (4 6 0)\n'
ALIGNED_DXF = '0 DIMENSION\n' + ENTITY_DXF + EXTENSIONS_DXF + '42 5\n70 33\n'
LINEAR_DXF = ('0 DIMENSION\n' + ENTITY_DXF + EXTENSIONS_DXF
              + f'42 4\n50 {math.pi / 2}\n70 32\n')
RADIAL_DXF = '0 DIMENSION\n' + ENTITY_DXF + '10 (-1 2 0)\n15 (5 10 0)\n42 10\n70 36\n'


class PreparationTests(unittest.TestCase):
    def test_schemas_validation_status_and_recovery_policy(self):
        capabilities = cli.capabilities()
        for op, required, optional in (
            ('annotation.mtext', MTEXT.keys(), ['height', 'rotation_deg', 'attachment']),
            ('dim.aligned', ALIGNED.keys(), ['text_height']),
            ('dim.linear', LINEAR.keys(), ['rotation_deg', 'text_height']),
            ('dim.radial', RADIAL.keys(), ['leader_length', 'text_height']),
        ):
            with self.subTest(op=op):
                self.assertEqual(sorted(required), capabilities[op]['required'])
                self.assertEqual(sorted(optional), capabilities[op]['optional'])
                self.assertTrue(capabilities[op]['validation'].startswith('verified_live'))
                self.assertIn(op, NO_ESCAPE_STEPS)

    def test_normalizes_points_numbers_and_defaults(self):
        mtext = prepare('annotation.mtext', {**MTEXT, 'height': 25, 'rotation_deg': -30, 'attachment': 9})
        self.assertEqual(MTEXT['text'], mtext['text'])
        self.assertEqual([10.0, 20.0, 0.0], mtext['point'])
        for key in ('width', 'height', 'rotation_deg'):
            self.assertIs(type(mtext[key]), float)
        self.assertIs(type(mtext['attachment']), int)
        self.assertEqual('x' * 2000, prepare('annotation.mtext', {**MTEXT, 'text': 'x' * 2000})['text'])

        aligned = prepare('dim.aligned', {**ALIGNED, 'text_height': 20})
        self.assertEqual([0.0, 9.0, 0.0], aligned['text_position'])
        self.assertIs(type(aligned['text_height']), float)
        linear = prepare('dim.linear', LINEAR)
        self.assertEqual([0.0, 9.0, 0.0], linear['dim_line_point'])
        self.assertIs(type(linear['rotation_deg']), float)
        self.assertEqual(0.0, linear['rotation_deg'])
        radial = prepare('dim.radial', RADIAL)
        self.assertEqual([-1.0, 2.0, 0.0], radial['center'])
        self.assertEqual([5.0, 10.0, 0.0], radial['chord_point'])
        self.assertIs(type(radial['leader_length']), float)
        self.assertEqual(0.0, radial['leader_length'])

    def test_rejects_invalid_mtext_values(self):
        for key, values in (
            ('text', (None, 42, '', '  ', 'x' * 2001, 'a\nb', 'a\rb', 'a\r\nb',
                      'a\x00b', 'a\tb', 'a\x1fb', 'a\x7fb')),
            ('point', ([10, 20, 1], [True, 20], [10**400, 20])),
            ('width', (0, -1, '5', True, math.inf, 10**400)),
            ('height', (0, -1, None, math.nan)),
            ('rotation_deg', (True, '30', -math.inf)),
            ('attachment', (True, 0, 10, 1.0, '1')),
        ):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare('annotation.mtext', {**MTEXT, key: value})

    def test_rejects_invalid_dimensions(self):
        for op, args, change, error in (
            ('dim.aligned', ALIGNED, {'ext2': [1, 2]}, 'requires distinct ext1 and ext2'),
            ('dim.aligned', ALIGNED, {'ext2': [1, 2.0000001]}, 'requires distinct ext1 and ext2'),
            ('dim.aligned', ALIGNED, {'text_position': [0, 9, 1]}, 'Z=0'),
            ('dim.linear', LINEAR, {'ext2': [1, 8]}, 'projected measurement'),
            ('dim.linear', LINEAR, {'ext2': [4, 2], 'rotation_deg': 90}, 'projected measurement'),
            ('dim.linear', LINEAR, {'dim_line_point': [0, 9, -1]}, 'Z=0'),
            ('dim.linear', LINEAR, {'rotation_deg': math.inf}, 'rotation_deg'),
            ('dim.radial', RADIAL, {'chord_point': RADIAL['center']}, 'requires distinct center and chord_point'),
            ('dim.radial', RADIAL, {'center': [-1, 2, 1]}, 'Z=0'),
            ('dim.radial', RADIAL, {'leader_length': -1}, 'leader_length'),
            ('dim.radial', RADIAL, {'leader_length': True}, 'leader_length'),
            ('dim.radial', RADIAL, {'leader_length': math.nan}, 'leader_length'),
        ):
            with self.subTest(op=op, change=change), self.assertRaisesRegex(ValueError, error):
                prepare(op, {**args, **change})
        for op, args in (('dim.aligned', ALIGNED), ('dim.linear', LINEAR), ('dim.radial', RADIAL)):
            for value in (0, -1, True, '20', math.inf):
                with self.subTest(op=op, text_height=value), self.assertRaisesRegex(ValueError, 'text_height'):
                    prepare(op, {**args, 'text_height': value})


class Wave6Document(Document):
    def AddMText(self, point, width, text):
        return self.add('AddMText', 'AcDbMText', (point, width, text),
                        InsertionPoint=point, Width=width, TextString=text, Height=25.0,
                        Rotation=0.0, AttachmentPoint=1)

    def AddDimAligned(self, ext1, ext2, position):
        return self.add('AddDimAligned', 'AcDbAlignedDimension', (ext1, ext2, position),
                        Measurement=5.0, TextHeight=12.0)

    def AddDimRotated(self, ext1, ext2, point, rotation):
        measurement = 3.0 if rotation == 0.0 else 4.0
        return self.add('AddDimRotated', 'AcDbRotatedDimension', (ext1, ext2, point, rotation),
                        Measurement=measurement, TextHeight=12.0)

    def AddDimRadial(self, center, chord, leader_length):
        return self.add('AddDimRadial', 'AcDbRadialDimension', (center, chord, leader_length),
                        Measurement=10.0, TextHeight=12.0)


class FakeCad(BaseCad):
    def __init__(self):
        super().__init__()
        self.doc = Wave6Document(self)


class DxfTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        self.folder = self.enterContext(TemporaryDirectory())

    def test_group_3_chunks_and_group_1_tail_preserve_long_mtext_exactly(self):
        first = ' 范例 ' + 'a' * 245 + ' '
        second = ' ' + 'b' * 248 + ' '
        tail = r' \P 结束 "C (D)" '
        self.assertEqual((250, 250), (len(first), len(second)))
        expected = first + second + tail
        self.assertGreater(len(expected), 250)
        self.cad.dump = ('B', '0 MTEXT\n' + ENTITY_DXF + f'3 {first}\n3 {second}\n1 {tail}\n'
                         '10 (10 20 0)\n40 25\n41 300\n50 0.5\n71 9\n')
        result = panel.mtext_dxf(self.cad, 'B', folder=self.folder)
        self.assertEqual({'type': 'MTEXT', 'handle': 'B', 'layer': '0', 'text': expected,
                          'insertion': (10.0, 20.0, 0.0), 'height': 25.0, 'width': 300.0,
                          'rotation': 0.5, 'attachment': 9}, result)

    def test_missing_text_is_distinct_from_empty_text(self):
        for records, expected in (('', None), ('1 \n', ''), ('3 chunk\n', 'chunk')):
            with self.subTest(records=records):
                self.cad.dump = ('B', '0 MTEXT\n' + records)
                self.assertEqual(expected, panel.mtext_dxf(self.cad, 'B', folder=self.folder)['text'])

    def test_dimension_dxf_returns_subtype_and_optional_fields(self):
        self.cad.dump = ('B', RADIAL_DXF)
        self.assertEqual({'type': 'DIMENSION', 'handle': 'B', 'layer': '0',
                          'def_point': (-1.0, 2.0, 0.0), 'ext1': None, 'ext2': None,
                          'chord_point': (5.0, 10.0, 0.0), 'measurement': 10.0,
                          'rotation': None, 'dim_type': 36},
                         panel.dim_dxf(self.cad, 'B', folder=self.folder))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'late', side_effect=lambda value: value))
        self.enterContext(patch.object(parts, 'vector', side_effect=tuple))
        self.enterContext(patch('tz_mcp.cad.time.sleep'))
        self.enterContext(patch.dict(sys.modules, pythoncom=SimpleNamespace(DISPATCH_PROPERTYPUT=4)))
        for parser in ('mtext_dxf', 'dim_dxf'):
            self.enterContext(patch.object(parts, parser, partial(getattr(panel, parser), folder=folder)))

    def execute(self, op, args, dxf):
        self.cad.dump = ('B', dxf)
        return Cad.execute(self.cad, op, args)

    def assert_result(self, result, object_name, groups):
        self.assertTrue(result['success'])
        self.assertEqual('draw-1', result['document_id'])
        self.assertEqual(['B'], result['handles'])
        self.assertEqual([{'handle': 'B', 'object_name': object_name}], result['entities'])
        self.assertEqual({'A', 'B'}, self.cad.store.keys())
        self.assertEqual({'type': True, 'properties': True, 'dxf': f'verified_{groups}', 'reopen': 'pending'},
                         result['validation'])
        self.assertEqual(1, len(self.cad.commands))

    def test_mtext_default_height_and_exact_text(self):
        self.cad.calls.failures = {'AddMText': CallRejected(), 'Width': CallRejected()}
        result = self.execute('annotation.mtext', MTEXT, MTEXT_DXF)
        self.assert_result(result, 'AcDbMText', '1_3_10_40_41_50_71')
        evidence = result['evidence']['mtext']
        self.assertEqual(MTEXT['text'], evidence['text'])
        self.assertEqual(evidence['text'], evidence['dxf']['text'])
        self.assertEqual(25.0, evidence['height'])
        self.assertEqual([('AddMText', ((10.0, 20.0, 0.0), 300.0, MTEXT['text']))], self.cad.doc.adds)
        self.assertEqual([], self.cad.store['B'].writes)
        self.assertEqual(0, self.cad.store['B'].updates)
        self.assertEqual({}, self.cad.calls.failures)

    def test_mtext_option_writes_preserve_requested_insertion_when_attachment_moves_it(self):
        original_invoke = Properties.Invoke

        def move_attachment(entity, name, lcid, flags, result_wanted, value):
            original_invoke(entity, name, lcid, flags, result_wanted, value)
            if name == 'AttachmentPoint':
                entity.values['InsertionPoint'] = (160.0, 5.0, 0.0)

        rotation = math.radians(-30)
        args = {**MTEXT, 'height': 40, 'rotation_deg': -30, 'attachment': 5}
        dxf = MTEXT_DXF.replace('40 25', '40 40').replace('50 0', f'50 {rotation % math.tau}')
        dxf = dxf.replace('71 1', '71 5')
        with patch.object(Properties, 'Invoke', move_attachment):
            result = self.execute('annotation.mtext', args, dxf)
        entity = self.cad.store['B']
        self.assertEqual([('Height', 40.0), ('Rotation', rotation), ('AttachmentPoint', 5),
                          ('InsertionPoint', (10.0, 20.0, 0.0))], entity.writes)
        self.assertEqual(1, entity.updates)
        evidence = result['evidence']['mtext']
        self.assertEqual([10.0, 20.0, 0.0], evidence['insertion'])
        self.assertEqual((40.0, rotation, 5), (evidence['height'], evidence['rotation'], evidence['attachment']))

    def test_mtext_rejects_inconsistent_com_or_dxf_readback(self):
        for props, dxf, error in (
            ({'ObjectName': 'AcDbText'}, MTEXT_DXF, 'COM type'),
            ({'TextString': 'other'}, MTEXT_DXF, 'COM TextString'),
            ({'InsertionPoint': (10, 20, 1)}, MTEXT_DXF, 'COM InsertionPoint'),
            ({'Height': math.nan}, MTEXT_DXF, 'COM Height'),
            ({'Width': 290}, MTEXT_DXF, 'COM Width'),
            ({}, MTEXT_DXF.replace('0 MTEXT', '0 TEXT'), 'DXF type'),
            ({}, MTEXT_DXF.replace(MTEXT['text'], 'other'), 'DXF MTEXT text'),
            ({}, MTEXT_DXF.replace('10 20 0', '10 20 1'), 'DXF 10'),
            ({}, MTEXT_DXF.replace('41 300', '41 290'), 'DXF 41'),
        ):
            with self.subTest(error=error):
                self.cad = FakeCad()
                self.cad.new_properties = props
                with self.assertRaisesRegex(OutcomeUnknown, error):
                    self.execute('annotation.mtext', MTEXT, dxf)

    def test_mtext_rejects_inconsistent_optional_dxf_properties(self):
        args = {**MTEXT, 'height': 25, 'rotation_deg': 0, 'attachment': 1}
        for line, replacement, error in (('40 25', '40 31', 'DXF 40'),
                                         ('50 0', '50 0.1', 'DXF 50'),
                                         ('71 1', '71 2', 'DXF 71')):
            with self.subTest(error=error), self.assertRaisesRegex(OutcomeUnknown, error):
                self.cad = FakeCad()
                self.execute('annotation.mtext', args, MTEXT_DXF.replace(line, replacement))

    def test_dimensions_verify_expected_measurements_and_optional_text_height(self):
        for op, args, dxf, kind, groups, expected, call in (
            ('dim.aligned', ALIGNED, ALIGNED_DXF, 'Aligned', '13_14_42', 5.0,
             ((1.0, 2.0, 0.0), (4.0, 6.0, 0.0), (0.0, 9.0, 0.0))),
            ('dim.linear', {**LINEAR, 'rotation_deg': -270}, LINEAR_DXF, 'Rotated', '13_14_42_50', 4.0,
             ((1.0, 2.0, 0.0), (4.0, 6.0, 0.0), (0.0, 9.0, 0.0), math.radians(-270))),
            ('dim.radial', {**RADIAL, 'leader_length': 60}, RADIAL_DXF, 'Radial', '10_15_42', 10.0,
             ((-1.0, 2.0, 0.0), (5.0, 10.0, 0.0), 60.0)),
        ):
            with self.subTest(op=op):
                self.cad = FakeCad()
                method = 'AddDim' + kind
                self.cad.calls.failures = {method: CallRejected(), 'Measurement': CallRejected(),
                                           'put:TextHeight': CallRejected(), 'TextHeight': CallRejected(),
                                           'Update': CallRejected()}
                result = self.execute(op, {**args, 'text_height': 20}, dxf)
                self.assert_result(result, f'AcDb{kind}Dimension', groups)
                self.assertEqual([(method, call)], self.cad.doc.adds)
                self.assertEqual([('TextHeight', 20.0)], self.cad.store['B'].writes)
                self.assertEqual(1, self.cad.store['B'].updates)
                evidence = result['evidence'][op.replace('.', '_')]
                self.assertEqual(expected, evidence['measurement'])
                self.assertEqual(20.0, evidence['text_height'])
                self.assertEqual(expected, evidence['dxf']['measurement'])
                self.assertEqual({}, self.cad.calls.failures)

    def test_linear_accepts_omitted_dxf_rotation_at_zero(self):
        dxf = '0 DIMENSION\n' + ENTITY_DXF + EXTENSIONS_DXF + '42 3\n70 32\n'
        result = self.execute('dim.linear', LINEAR, dxf)
        evidence = result['evidence']['dim_linear']
        self.assertEqual(3.0, evidence['measurement'])
        self.assertEqual(0.0, evidence['rotation_rad'])
        self.assertIsNone(evidence['text_height'])
        self.assertEqual([], self.cad.store['B'].writes)
        self.assertEqual(0, self.cad.store['B'].updates)

    def test_dimensions_reject_inconsistent_com_or_dxf_readback(self):
        for op, args, props, dxf, error in (
            ('dim.aligned', ALIGNED, {'Measurement': math.nan}, ALIGNED_DXF, 'COM Measurement'),
            ('dim.aligned', ALIGNED, {}, ALIGNED_DXF.replace('13 ', '11 '), 'DXF 13'),
            ('dim.aligned', ALIGNED, {}, ALIGNED_DXF.replace('0 DIMENSION', '0 LINE'), 'DXF type'),
            ('dim.linear', {**LINEAR, 'rotation_deg': 90}, {}, LINEAR_DXF.replace('50 ', '51 '), 'DXF 50'),
            ('dim.linear', {**LINEAR, 'rotation_deg': 90}, {}, LINEAR_DXF.replace('14 (4 6 0)', '14 (4 7 0)'),
             'DXF 14'),
            ('dim.radial', RADIAL, {'ObjectName': 'AcDbAlignedDimension'}, RADIAL_DXF, 'COM type'),
            ('dim.radial', RADIAL, {}, RADIAL_DXF.replace('10 (-1 2 0)', '10 (5 10 0)'), 'DXF 10'),
            ('dim.radial', RADIAL, {}, RADIAL_DXF.replace('15 (5 10 0)', '15 (5 10 1)'), 'DXF 15'),
            ('dim.radial', RADIAL, {}, RADIAL_DXF.replace('42 10', '42 11'), 'DXF 42'),
        ):
            with self.subTest(op=op, error=error):
                self.cad = FakeCad()
                self.cad.new_properties = props
                with self.assertRaisesRegex(OutcomeUnknown, error):
                    self.execute(op, args, dxf)

    def test_dimension_text_height_must_be_read_back(self):
        with patch.object(Properties, 'Invoke'), self.assertRaisesRegex(OutcomeUnknown, 'COM TextHeight'):
            self.execute('dim.radial', {**RADIAL, 'text_height': 20}, RADIAL_DXF)

    def test_creation_rejects_unrelated_handle_changes(self):
        for op, args, dxf in (('annotation.mtext', MTEXT, MTEXT_DXF), ('dim.aligned', ALIGNED, ALIGNED_DXF)):
            for handles in ({'B'}, {'A', 'B', 'C'}):
                with self.subTest(op=op, handles=handles):
                    before = {'A': self.cad.entity_a}
                    with patch.object(self.cad, 'entities', side_effect=[before, dict.fromkeys(handles)]):
                        with self.assertRaisesRegex(OutcomeUnknown, 'handle changes'):
                            self.execute(op, args, dxf)

    def test_transient_com_lookup_names_retry(self):
        for name in ('AddMText', 'AddDimRotated', 'AddDimAligned', 'AddDimRadial',
                     'Measurement', 'TextHeight', 'AttachmentPoint', 'Width'):
            with self.subTest(name=name):
                read = Mock(side_effect=[AttributeError(f'<unknown>.{name}'), name])
                self.assertEqual(name, retry_document_read(read))
                self.assertEqual(2, read.call_count)

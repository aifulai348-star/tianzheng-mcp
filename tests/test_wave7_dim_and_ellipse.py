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


ANGULAR = {'vertex': [2, 3], 'first_endpoint': [12, 3], 'second_endpoint': [2, 13],
           'text_position': [8, 9]}
DIAMETER = {'chord_point': [-1, 2], 'far_chord_point': [5, 10]}
ELLIPSE = {'center': [7, -4], 'major_axis': [6, 8], 'radius_ratio': 0.25}
ENTITY_DXF = '5 B\n8 0\n'
ANGULAR_DXF = ('0 DIMENSION\n' + ENTITY_DXF + '13 (12 3 0)\n14 (2 13 0)\n15 (2 3 0)\n'
               f'42 {math.pi / 2}\n70 37\n')
ANGULAR_2LINE = {'vertex': [0, 0], 'first_endpoint': [3000, 0], 'second_endpoint': [0, 3000],
                 'text_position': [1500, 1500]}
DIAMETER_DXF = '0 DIMENSION\n' + ENTITY_DXF + '10 (5 10 0)\n15 (-1 2 0)\n42 10\n70 35\n'
ELLIPSE_DXF = ('0 ELLIPSE\n' + ENTITY_DXF + '10 (7 -4 0)\n11 (6 8 0)\n40 0.25\n'
               f'41 0\n42 {math.tau}\n')


def angular_2line_dxf(ray1, ray2):
    points = ''.join(f'{group} ({x} {y} 0)\n' for group, (x, y) in zip((13, 14, 15, 10), (*ray1, *ray2)))
    return '0 DIMENSION\n' + ENTITY_DXF + points + '42 1.5708\n70 34\n'


class PreparationTests(unittest.TestCase):
    def test_capabilities_and_schema_enforcement(self):
        capabilities = cli.capabilities()
        for op, args, optional in (
            ('dim.angular', ANGULAR, ['text_height']),
            ('dim.diameter', DIAMETER, ['leader_length', 'text_height']),
            ('ellipse.create', ELLIPSE, []),
        ):
            with self.subTest(op=op):
                self.assertEqual(sorted(args), capabilities[op]['required'])
                self.assertEqual(optional, capabilities[op]['optional'])
                self.assertTrue(capabilities[op]['validation'].startswith('verified_live'))
                self.assertIn(op, NO_ESCAPE_STEPS)
                with self.assertRaises(ValueError):
                    prepare(op, {**args, 'unexpected': 0})
                for key in args:
                    with self.assertRaises(ValueError):
                        prepare(op, {field: val for field, val in args.items() if field != key})

    def test_normalizes_plan_geometry_and_optionals(self):
        self.assertEqual({'vertex': [2., 3., 0.], 'first_endpoint': [12., 3., 0.],
                          'second_endpoint': [2., 13., 0.], 'text_position': [8., 9., 0.],
                          'text_height': 20.},
                         prepare('dim.angular', {**ANGULAR, 'vertex': (2, 3, 0), 'text_height': 20}))
        diameter = prepare('dim.diameter', DIAMETER)
        self.assertEqual({'chord_point': [-1., 2., 0.], 'far_chord_point': [5., 10., 0.],
                          'leader_length': 0.}, diameter)
        self.assertIs(type(diameter['leader_length']), float)
        ellipse = prepare('ellipse.create', {**ELLIPSE, 'major_axis': (6, 8, 0), 'radius_ratio': 1})
        self.assertEqual({'center': [7., -4., 0.], 'major_axis': [6., 8., 0.], 'radius_ratio': 1.}, ellipse)
        self.assertIs(type(ellipse['radius_ratio']), float)

    def test_rejects_nonplanar_and_nonfinite_points(self):
        for op, args in (('dim.angular', ANGULAR), ('dim.diameter', DIAMETER), ('ellipse.create', ELLIPSE)):
            for key, point in args.items():
                if not isinstance(point, list):
                    continue
                for value in ([*point, 0.01], [True, 0], [math.inf, 0], [0, math.nan], [10**400, 0], [0]):
                    with self.subTest(op=op, key=key, value=value), self.assertRaises(ValueError):
                        prepare(op, {**args, key: value})

    def test_rejects_degenerate_geometry(self):
        axis_angle = {**ANGULAR, 'vertex': [0, 0], 'first_endpoint': [100, 0]}
        for field, endpoint in (('first_endpoint', [1e-6, 0]), ('second_endpoint', [0, 1e-6]),
                                ('second_endpoint', [20, 0]), ('second_endpoint', [-20, 0]),
                                ('second_endpoint', [100, 0.00005]), ('second_endpoint', [-100, 0.00005])):
            with self.subTest(field=field, endpoint=endpoint), self.assertRaises(ValueError):
                prepare('dim.angular', {**axis_angle, field: endpoint})
        prepare('dim.angular', {**axis_angle, 'second_endpoint': [100, 0.0002]})
        prepare('dim.angular', {**axis_angle, 'first_endpoint': [1e200, 0], 'second_endpoint': [0, 1e200]})
        for point in ([0, 0], [1e-6, 0]):
            with self.subTest(point=point):
                with self.assertRaisesRegex(ValueError, 'distinct'):
                    prepare('dim.diameter', {'chord_point': [0, 0], 'far_chord_point': point})
                with self.assertRaisesRegex(ValueError, 'major_axis'):
                    prepare('ellipse.create', {**ELLIPSE, 'major_axis': point})

    def test_rejects_invalid_scalars(self):
        for op, args, key, values in (
            ('dim.angular', ANGULAR, 'text_height', (0, -1)),
            ('dim.diameter', DIAMETER, 'text_height', (0, -1)),
            ('dim.diameter', DIAMETER, 'leader_length', (-1,)),
            ('ellipse.create', ELLIPSE, 'radius_ratio', (-1, 0, 1e-6, 1.01)),
        ):
            for value in (*values, True, '1', None, math.inf, math.nan, 10**400):
                with self.subTest(op=op, key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    prepare(op, {**args, key: value})
        self.assertEqual(1.01e-6, prepare('ellipse.create', {**ELLIPSE, 'radius_ratio': 1.01e-6})['radius_ratio'])


class Wave7Document(Document):
    angular_object_name = 'AcDb3PointAngularDimension'
    angular_measurement = math.pi / 2

    def AddDimAngular(self, vertex, first, second, text):
        return self.add('AddDimAngular', self.angular_object_name, (vertex, first, second, text),
                        Measurement=self.angular_measurement, TextHeight=12.0)

    def AddDimDiametric(self, chord, far_chord, leader):
        return self.add('AddDimDiametric', 'AcDbDiametricDimension', (chord, far_chord, leader),
                        Measurement=10.0, TextHeight=12.0)

    def AddEllipse(self, center, axis, ratio):
        return self.add('AddEllipse', 'AcDbEllipse', (center, axis, ratio), Center=center,
                        MajorRadius=10.0, MinorRadius=10.0 * ratio, RadiusRatio=ratio)


class FakeCad(BaseCad):
    def __init__(self):
        super().__init__()
        self.doc = Wave7Document(self)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'late', side_effect=lambda obj: obj))
        self.enterContext(patch.object(parts, 'vector', side_effect=tuple))
        self.enterContext(patch('tz_mcp.cad.time.sleep'))
        self.enterContext(patch.dict(sys.modules, pythoncom=SimpleNamespace(DISPATCH_PROPERTYPUT=4)))
        for parser in ('dim_dxf', 'ellipse_dxf'):
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

    def test_angular_measures_the_sector_containing_text(self):
        for first, second, text, expected in (
            ([12, 3], [2, 13], [8, 9], math.pi / 2),
            ([12, 3], [2, 13], [-4, -3], 3 * math.pi / 2),
            ([2, 13], [12, 3], [8, 9], math.pi / 2),
            ([2, 13], [12, 3], [-4, -3], 3 * math.pi / 2),
            ([-1, 7], [-1, -1], [-5, 3], 2 * math.atan2(4, 3)),
        ):
            with self.subTest(first=first, second=second, text=text):
                self.cad = FakeCad()
                self.cad.doc.angular_measurement = expected
                args = {**ANGULAR, 'first_endpoint': first, 'second_endpoint': second,
                        'text_position': text, 'text_height': 20}
                dxf = (f'0 DIMENSION\n{ENTITY_DXF}13 ({first[0]} {first[1]} 0)\n'
                       f'14 ({second[0]} {second[1]} 0)\n15 (2 3 0)\n42 {expected}\n')
                result = self.execute('dim.angular', args, dxf)
                self.assert_result(result, 'AcDb3PointAngularDimension', '10_13_14_15_42')
                call = ((2., 3., 0.), (*first, 0.), (*second, 0.), (*text, 0.))
                self.assertEqual([('AddDimAngular', call)], self.cad.doc.adds)
                self.assertEqual([('TextHeight', 20.)], self.cad.store['B'].writes)
                self.assertEqual(1, self.cad.store['B'].updates)
                evidence = result['evidence']['dim_angular']
                self.assertAlmostEqual(expected, evidence['measurement'])
                self.assertEqual(expected, evidence['dxf']['measurement'])
                self.assertEqual(20., evidence['text_height'])

    def test_angular_accepts_two_line_dxf_ray_assignments_and_endpoint_orders(self):
        vertex, first, second = (ANGULAR_2LINE[key] for key in ('vertex', 'first_endpoint', 'second_endpoint'))
        for ray1, ray2 in (
            ((vertex, first), (vertex, second)),
            ((vertex, second), (vertex, first)),
            ((first, vertex), (second, vertex)),
            (([0.3, 0.3], [3000.4, 0]), ([0, 3000.4], vertex)),
        ):
            with self.subTest(ray1=ray1, ray2=ray2):
                self.cad = FakeCad()
                self.cad.doc.angular_object_name = 'AcDb2LineAngularDimension'
                self.cad.doc.angular_measurement = 1.5707963267948952
                result = self.execute('dim.angular', ANGULAR_2LINE, angular_2line_dxf(ray1, ray2))
                self.assert_result(result, 'AcDb2LineAngularDimension', '10_13_14_15_42')
                evidence = result['evidence']['dim_angular']
                dxf = evidence['dxf']
                self.assertEqual(34, dxf['dim_type'])
                self.assertEqual(1.5708, dxf['measurement'])
                self.assertEqual(self.cad.doc.angular_measurement, evidence['measurement'])
                self.assertEqual([(*point, 0.) for point in (*ray1, *ray2)],
                                 [dxf[key] for key in ('ext1', 'ext2', 'chord_point', 'def_point')])

    def test_angular_rejects_two_line_dxf_ray_mismatches(self):
        vertex, first, second = (ANGULAR_2LINE[key] for key in ('vertex', 'first_endpoint', 'second_endpoint'))
        dxf = angular_2line_dxf((vertex, first), (vertex, second))
        for line, replacement in (
            ('13 (0 0 0)', '13 (0.6 0 0)'),
            ('14 (3000 0 0)', '14 (3000 0 0.6)'),
            ('15 (0 0 0)', '15 (0.4 0.4 0)'),
            ('10 (0 3000 0)\n', ''),
            ('10 (0 3000 0)', '10 (3000 0 0)'),
        ):
            with self.subTest(line=line, replacement=replacement):
                self.cad = FakeCad()
                self.cad.doc.angular_object_name = 'AcDb2LineAngularDimension'
                with self.assertRaisesRegex(OutcomeUnknown, 'DXF 13/14 and 15/10 rays mismatch'):
                    self.execute('dim.angular', ANGULAR_2LINE, dxf.replace(line, replacement))

    def test_diameter_accepts_both_dxf_chord_orders(self):
        swapped_dxf = DIAMETER_DXF.replace('10 (5 10 0)\n15 (-1 2 0)', '10 (-1 2 0)\n15 (5 10 0)')
        for dxf, options, leader, height in ((DIAMETER_DXF, {}, 0., None),
                                             (swapped_dxf, {'leader_length': 40, 'text_height': 20}, 40., 20.)):
            with self.subTest(options=options):
                self.cad = FakeCad()
                result = self.execute('dim.diameter', {**DIAMETER, **options}, dxf)
                self.assert_result(result, 'AcDbDiametricDimension', '10_15_42')
                self.assertEqual([('AddDimDiametric', ((-1., 2., 0.), (5., 10., 0.), leader))], self.cad.doc.adds)
                self.assertEqual([] if height is None else [('TextHeight', height)], self.cad.store['B'].writes)
                self.assertEqual(int(height is not None), self.cad.store['B'].updates)
                evidence = result['evidence']['dim_diameter']
                self.assertEqual((10., leader, height), tuple(evidence[key] for key in (
                    'measurement', 'leader_length', 'text_height')))

    def test_ellipse_accepts_reversed_dxf_axis_and_unit_ratio(self):
        for axis, ratio in (((6., 8., 0.), 0.25), ((-6., -8., 0.), 1.0)):
            with self.subTest(axis=axis, ratio=ratio):
                self.cad = FakeCad()
                args = {**ELLIPSE, 'radius_ratio': ratio}
                dxf = ELLIPSE_DXF.replace('11 (6 8 0)\n40 0.25', f'11 ({axis[0]} {axis[1]} 0)\n40 {ratio}')
                result = self.execute('ellipse.create', args, dxf)
                self.assert_result(result, 'AcDbEllipse', '10_11_40')
                self.assertEqual([('AddEllipse', ((7., -4., 0.), (6., 8., 0.), ratio))], self.cad.doc.adds)
                self.assertEqual([], self.cad.store['B'].writes)
                self.assertEqual(0, self.cad.store['B'].updates)
                evidence = result['evidence']['ellipse']
                self.assertEqual((10., 10. * ratio, ratio), tuple(evidence[key] for key in (
                    'major_radius', 'minor_radius', 'radius_ratio')))
                self.assertEqual({'type': 'ELLIPSE', 'handle': 'B', 'layer': '0', 'center': (7., -4., 0.),
                                  'major_axis': axis, 'radius_ratio': ratio, 'start_param': 0.,
                                  'end_param': math.tau}, evidence['dxf'])
                self.assertIn('(0 5 8 10 11 40 41 42)', self.cad.commands[0])

    def test_rejects_invalid_arguments_before_cad_access(self):
        for op, args in (('dim.angular', {**ANGULAR, 'vertex': ANGULAR['first_endpoint']}),
                         ('dim.diameter', {**DIAMETER, 'leader_length': -1}),
                         ('ellipse.create', {**ELLIPSE, 'radius_ratio': 0})):
            with self.subTest(op=op), patch.object(self.cad, 'guard') as guard:
                with self.assertRaises(ValueError):
                    self.execute(op, args, '')
                guard.assert_not_called()
        self.assertEqual([], self.cad.doc.adds)
        self.assertEqual([], self.cad.commands)

    def test_rejects_cad_handle_delta_or_object_type_mismatch(self):
        for op, args, dxf in (('dim.angular', ANGULAR, ANGULAR_DXF), ('dim.diameter', DIAMETER, DIAMETER_DXF),
                              ('ellipse.create', ELLIPSE, ELLIPSE_DXF)):
            for handles in ({'B'}, {'A', 'B', 'C'}, {'A'}):
                with self.subTest(op=op, handles=handles):
                    self.cad = FakeCad()
                    before = dict(self.cad.store)
                    with patch.object(self.cad, 'entities', side_effect=[before, dict.fromkeys(handles)]):
                        with self.assertRaisesRegex(OutcomeUnknown, 'handle changes'):
                            self.execute(op, args, dxf)
            with self.subTest(op=op):
                self.cad = FakeCad()
                self.cad.new_properties = {'ObjectName': 'AcDbLine'}
                with self.assertRaisesRegex(OutcomeUnknown, 'COM type'):
                    self.execute(op, args, dxf)
                self.assertEqual([], self.cad.commands)

    def test_rejects_inconsistent_com_readback(self):
        for op, args, dxf, properties, error in (
            ('dim.angular', ANGULAR, ANGULAR_DXF, {'Measurement': math.pi / 2 + 0.002}, 'COM Measurement'),
            ('dim.diameter', DIAMETER, DIAMETER_DXF, {'Measurement': 10.02}, 'COM Measurement'),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, {'Center': (7, -4, 0.02)}, 'COM Center'),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, {'MajorRadius': 10.02}, 'COM MajorRadius'),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, {'MinorRadius': math.nan}, 'COM MinorRadius'),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, {'RadiusRatio': 0.2502}, 'COM RadiusRatio'),
        ):
            with self.subTest(op=op, error=error), self.assertRaisesRegex(OutcomeUnknown, error):
                self.cad = FakeCad()
                self.cad.new_properties = properties
                self.execute(op, args, dxf)

    def test_optional_text_height_is_verified_for_both_dimensions(self):
        for op, args, dxf in (('dim.angular', ANGULAR, ANGULAR_DXF), ('dim.diameter', DIAMETER, DIAMETER_DXF)):
            with self.subTest(op=op), patch.object(Properties, 'Invoke'):
                self.cad = FakeCad()
                with self.assertRaisesRegex(OutcomeUnknown, 'COM TextHeight'):
                    self.execute(op, {**args, 'text_height': 50}, dxf)

    def test_rejects_inconsistent_or_missing_dxf_values(self):
        for op, args, dxf, line, replacement, error in (
            ('dim.angular', ANGULAR, ANGULAR_DXF, '0 DIMENSION', '0 LINE', 'DXF type'),
            ('dim.angular', ANGULAR, ANGULAR_DXF, '13 (12 3 0)', '13 (12 3 0.6)', 'DXF 13'),
            ('dim.angular', ANGULAR, ANGULAR_DXF, '14 (2 13 0)\n', '', 'DXF 14'),
            ('dim.angular', ANGULAR, ANGULAR_DXF, '15 (2 3 0)', '15 (3 3 0)', 'DXF 15'),
            ('dim.angular', ANGULAR, ANGULAR_DXF, f'42 {math.pi / 2}', '42 90', 'DXF 42'),
            ('dim.diameter', DIAMETER, DIAMETER_DXF, '0 DIMENSION', '0 LINE', 'DXF type'),
            ('dim.diameter', DIAMETER, DIAMETER_DXF, '10 (5 10 0)', '10 (-1 2 0)', 'DXF 10/15'),
            ('dim.diameter', DIAMETER, DIAMETER_DXF, '15 (-1 2 0)\n', '', 'DXF 10/15'),
            ('dim.diameter', DIAMETER, DIAMETER_DXF, '42 10', '42 10.6', 'DXF 42'),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, '0 ELLIPSE', '0 CIRCLE', 'DXF type'),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, '10 (7 -4 0)', '10 (7 -4 0.6)', 'DXF 10'),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, '11 (6 8 0)', '11 (8 6 0)', 'DXF 11'),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, '40 0.25', '40 0.252', 'DXF 40'),
        ):
            with self.subTest(op=op, error=error), self.assertRaisesRegex(OutcomeUnknown, error):
                self.cad = FakeCad()
                self.execute(op, args, dxf.replace(line, replacement))

    def test_angular_dxf_must_agree_with_com_as_well_as_expected_measurement(self):
        self.cad.doc.angular_measurement += 0.0009
        dxf = ANGULAR_DXF.replace(f'42 {math.pi / 2}', f'42 {math.pi / 2 - 0.0009}')
        with self.assertRaisesRegex(OutcomeUnknown, 'DXF 42 vs COM Measurement'):
            self.execute('dim.angular', ANGULAR, dxf)

    def test_retries_rejected_calls_without_creating_extra_entities(self):
        for op, args, dxf, method, members in (
            ('dim.angular', {**ANGULAR, 'text_height': 20}, ANGULAR_DXF, 'AddDimAngular',
             ('Measurement', 'put:TextHeight', 'TextHeight', 'Update')),
            ('dim.diameter', DIAMETER, DIAMETER_DXF, 'AddDimDiametric', ('Measurement',)),
            ('ellipse.create', ELLIPSE, ELLIPSE_DXF, 'AddEllipse', ('MajorRadius', 'MinorRadius', 'RadiusRatio')),
        ):
            with self.subTest(op=op):
                self.cad = FakeCad()
                self.cad.calls.failures = {name: CallRejected() for name in (method, *members)}
                self.execute(op, args, dxf)
                self.assertEqual({}, self.cad.calls.failures)
                self.assertEqual(2, self.cad.calls.attempts[method])
                self.assertEqual(1, len(self.cad.doc.adds))

    def test_late_bound_metadata_retries_all_new_names(self):
        for name in ('AddDimAngular', 'AddDimDiametric', 'AddEllipse', 'MajorAxis', 'RadiusRatio',
                     'MajorRadius', 'MinorRadius'):
            with self.subTest(name=name):
                read = Mock(side_effect=[AttributeError('<unknown>.' + name), name])
                self.assertEqual(name, retry_document_read(read))
                self.assertEqual(2, read.call_count)

import math
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, OutcomeUnknown
from tz_mcp.commands import HATCH_PATTERNS, SCHEMAS, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


OP = 'hatch.create'
REF = {'document_id': ' doc ', 'boundary_handle': 'ab'}


class PrepareHatchTests(unittest.TestCase):
    def test_schema_and_capability(self):
        required = {'document_id', 'boundary_handle'}
        optional = {'pattern_name', 'pattern_scale', 'pattern_angle_deg'}
        self.assertEqual((required, optional), SCHEMAS[OP])
        capability = cli.capabilities()[OP]
        self.assertEqual(sorted(required), capability['required'])
        self.assertEqual(sorted(optional), capability['optional'])
        self.assertTrue(capability['validation'].startswith('verified_live'))
        self.assertIn(OP, NO_ESCAPE_STEPS)

    def test_normalizes_document_handle_and_supported_patterns(self):
        self.assertEqual({'document_id': 'doc', 'boundary_handle': 'AB', 'pattern_name': 'SOLID'},
                         prepare(OP, REF))
        self.assertEqual({'SOLID', 'ANSI31', 'ANSI37', 'NET', 'AR-CONC'}, HATCH_PATTERNS)
        for pattern in HATCH_PATTERNS:
            with self.subTest(pattern=pattern):
                args = prepare(OP, {**REF, 'pattern_name': f' {pattern.lower()} '})
                self.assertEqual(pattern, args['pattern_name'])

    def test_normalizes_optional_inputs_independently(self):
        for field, value in [('pattern_scale', 2), ('pattern_angle_deg', -450)]:
            with self.subTest(field=field):
                args = prepare(OP, {**REF, 'pattern_name': 'ANSI31', field: value})
                self.assertEqual({'document_id': 'doc', 'boundary_handle': 'AB',
                                  'pattern_name': 'ANSI31', field: float(value)}, args)
                self.assertIs(type(args[field]), float)

    def test_rejects_invalid_shape_document_handle_and_pattern(self):
        invalid = [{}, {'document_id': 'doc'}, {'boundary_handle': 'AB'}, {**REF, 'unknown': 1}]
        invalid.extend({**REF, 'document_id': value} for value in ('', ' ', 1, None))
        invalid.extend({**REF, 'boundary_handle': value}
                       for value in ('', '0xab', 'ab\n', ' ab', 'G1', 123, None))
        invalid.extend({**REF, 'pattern_name': value} for value in ('', 'brick', 1, None, []))
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                prepare(OP, args)

    def test_rejects_solid_options_and_invalid_numbers(self):
        invalid = (True, False, '1', None, math.nan, math.inf, -math.inf, 10**400)
        for key in ('pattern_scale', 'pattern_angle_deg'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f'SOLID hatch does not accept {key}'):
                prepare(OP, {**REF, key: 1})
            for value in invalid + ((0, -1) if key == 'pattern_scale' else ()):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    prepare(OP, {**REF, 'pattern_name': 'ANSI31', key: value})


class FakeHatch:
    Handle = 'ce'
    ObjectName = 'AcDbHatch'
    AssociativeHatch = False
    NumberOfLoops = 1
    Area = 1000.0

    def __init__(self, cad, pattern):
        self.cad = cad
        self.PatternName = pattern.lower()
        self.PatternScale = None
        self.PatternAngle = None

    def AppendOuterLoop(self, outer_loop):
        self.cad.calls.append(('AppendOuterLoop', outer_loop))

    def Evaluate(self):
        self.cad.calls.append(('Evaluate',))

    def Update(self):
        self.cad.calls.append(('Update',))


class FakeCad:
    document_id = 'doc'
    hwnd = 42

    def __init__(self):
        self.boundary = SimpleNamespace(Handle='AB', ObjectName='AcDbPolyline', Closed=True, Area=1000.0)
        self.items = {'AB': self.boundary, 'CD': SimpleNamespace(Handle='CD', ObjectName='AcDbLine')}
        self.doc = SimpleNamespace(ModelSpace=SimpleNamespace(AddHatch=self.add_hatch))
        self.calls = []
        self.com_overrides = {}
        self.dxf_overrides = {}
        self.after_add = lambda: None

    def guard(self):
        pass

    def entities(self):
        return self.items.copy()

    def entity(self, handle, document_id):
        return self.items[handle]

    def describe(self, entity):
        return {'handle': entity.Handle.upper(), 'object_name': entity.ObjectName}

    def add_hatch(self, kind, name, associative):
        self.calls.append(('AddHatch', kind, name, associative))
        hatch = FakeHatch(self, name)
        vars(hatch).update(self.com_overrides)
        self.items[hatch.Handle.upper()] = hatch
        self.after_add()
        return hatch

    def dxf(self, handle):
        hatch = self.items[handle]
        self.calls.append(('hatch_dxf', handle))
        return {'type': 'HATCH', 'handle': handle, 'layer': '0', 'pattern_name': hatch.PatternName,
                'solid_fill': int(hatch.PatternName == 'solid'), 'associative': 0,
                'pattern_scale': hatch.PatternScale, 'pattern_angle_rad': hatch.PatternAngle,
                **self.dxf_overrides}


class CreateHatchTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'require_model_space'))
        self.enterContext(patch.object(parts, 'late', side_effect=lambda entity: entity))
        self.loop = object()
        self.dispatch_array = self.enterContext(patch.object(parts, '_dispatch_array', return_value=self.loop))
        self.enterContext(patch.object(parts, 'hatch_dxf', side_effect=lambda c, handle: c.dxf(handle)))
        self.write = self.enterContext(patch.object(parts, 'property_write', side_effect=self.write_property))

    def write_property(self, entity, name, value):
        self.cad.calls.append((name, value))
        setattr(entity, name, value)

    def execute(self, **kwargs):
        return Cad.execute(self.cad, OP, {**REF, **kwargs})

    def test_solid_creates_one_non_associative_hatch_and_returns_evidence(self):
        before = self.cad.entities()
        result = self.execute(boundary_handle='00ab')
        self.assertEqual([
            ('AddHatch', 0, 'SOLID', False), ('AppendOuterLoop', self.loop),
            ('Evaluate',), ('Update',), ('hatch_dxf', 'CE'),
        ], self.cad.calls)
        self.dispatch_array.assert_called_once_with([self.cad.boundary])
        self.write.assert_not_called()
        self.assertEqual({**before, 'CE': self.cad.items['CE']}, self.cad.entities())
        self.assertEqual({
            'success': True, 'document_id': 'doc', 'handles': ['CE'],
            'entities': [{'handle': 'CE', 'object_name': 'AcDbHatch'}],
            'evidence': {'hatch': {
                'boundary_handle': 'AB', 'boundary_area': 1000.0, 'pattern_name': 'SOLID',
                'associative': False, 'number_of_loops': 1, 'area': 1000.0,
                'pattern_scale': None, 'pattern_angle_deg': None,
                'dxf': {'type': 'HATCH', 'handle': 'CE', 'layer': '0', 'pattern_name': 'solid',
                        'solid_fill': 1, 'associative': 0, 'pattern_scale': None, 'pattern_angle_rad': None},
            }},
            'validation': {'type': True, 'properties': True, 'dxf': 'verified_2_70_71_41_52',
                           'area': 'verified_within_0_5pct', 'reopen': 'pending'},
        }, result)

    def test_patterned_hatch_writes_scale_and_angle_and_verifies_wrapped_readback(self):
        self.cad.dxf_overrides.update(pattern_angle_rad=math.radians(270), pattern_scale=2.005)
        result = self.execute(pattern_name=' ansi31 ', pattern_scale=2, pattern_angle_deg=-450)
        self.assertEqual([
            ('AddHatch', 0, 'ANSI31', False), ('PatternScale', 2.0), ('PatternAngle', math.radians(-450)),
            ('AppendOuterLoop', self.loop), ('Evaluate',), ('Update',), ('hatch_dxf', 'CE'),
        ], self.cad.calls)
        evidence = result['evidence']['hatch']
        self.assertEqual(('ANSI31', 2.0, -450.0), (evidence['pattern_name'], evidence['pattern_scale'],
                                                 evidence['pattern_angle_deg']))
        self.assertEqual(0, evidence['dxf']['solid_fill'])

    def test_pattern_options_are_independent_and_angle_zero_is_written(self):
        for options, properties in [({}, []), ({'pattern_scale': 3}, ['PatternScale']),
                                    ({'pattern_angle_deg': 0}, ['PatternAngle'])]:
            with self.subTest(options=options):
                self.cad = FakeCad()
                self.execute(pattern_name='NET', **options)
                self.assertEqual(properties, [call[0] for call in self.cad.calls if call[0].startswith('Pattern')])

    def test_rejects_invalid_boundary_before_write(self):
        for overrides, message in [
            ({'ObjectName': 'AcDbCircle'}, 'requires a closed AcDbPolyline boundary'),
            ({'Closed': False}, 'boundary polyline must be closed'),
            *[({'Area': value}, 'boundary polyline must have positive area')
              for value in (0, -1, 1e-3, math.nan, math.inf, -math.inf)],
        ]:
            with self.subTest(overrides=overrides):
                self.cad = FakeCad()
                vars(self.cad.boundary).update(overrides)
                with self.assertRaisesRegex(ValueError, message):
                    self.execute()
                self.assertEqual([], self.cad.calls)
        self.dispatch_array.assert_not_called()

    def test_rejects_unknown_boundary_and_wrong_document_before_write(self):
        with self.assertRaisesRegex(ValueError, 'Unknown handle'):
            self.execute(boundary_handle='FF')
        with self.assertRaisesRegex(ValueError, 'document_id mismatch'):
            self.execute(document_id='other')
        self.assertEqual([], self.cad.calls)

    def test_rejects_unexpected_model_space_handle_changes(self):
        for action in (lambda: self.cad.items.pop('AB'), lambda: self.cad.items.pop('CE'),
                       lambda: self.cad.items.update({'CF': object()})):
            with self.subTest(action=action):
                self.cad = FakeCad()
                self.cad.after_add = action
                with self.assertRaisesRegex(OutcomeUnknown, 'ModelSpace handle changes'):
                    self.execute()

    def test_rejects_unverified_com_readback(self):
        for property_name, value, message in [
            ('ObjectName', 'AcDbLine', 'COM type'), ('PatternName', 'ANSI31', 'COM PatternName'),
            ('AssociativeHatch', True, 'COM AssociativeHatch'), ('NumberOfLoops', 2, 'COM NumberOfLoops'),
            ('Area', 994.9, 'COM Area'), ('Area', 1005.1, 'COM Area'), ('Area', math.nan, 'COM Area'),
            ('Area', math.inf, 'COM Area'),
        ]:
            with self.subTest(property_name=property_name, value=value):
                self.cad = FakeCad()
                self.cad.com_overrides[property_name] = value
                with self.assertRaisesRegex(OutcomeUnknown, message):
                    self.execute()

    def test_area_tolerance_uses_half_percent_with_a_one_unit_floor(self):
        for boundary_area, hatch_area in [(1000.0, 1005.0), (1000.0, 995.0), (10.0, 11.0)]:
            with self.subTest(boundary_area=boundary_area, hatch_area=hatch_area):
                self.cad = FakeCad()
                self.cad.boundary.Area = boundary_area
                self.cad.com_overrides['Area'] = hatch_area
                self.assertEqual(hatch_area, self.execute()['evidence']['hatch']['area'])

    def test_rejects_unverified_dxf_readback(self):
        cases = [
            ('type', 'LINE', 'DXF type'), ('pattern_name', None, 'DXF 2'),
            ('pattern_name', 'ANSI37', 'DXF 2'), ('solid_fill', 1, 'DXF 70'),
            ('associative', 1, 'DXF 71'), ('pattern_scale', 2.02, 'DXF 41'),
            ('pattern_scale', None, 'DXF 41'), ('pattern_angle_rad', 0.0, 'DXF 52'),
            ('pattern_angle_rad', None, 'DXF 52'),
        ]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                self.cad = FakeCad()
                self.cad.dxf_overrides[field] = value
                with self.assertRaisesRegex(OutcomeUnknown, message):
                    self.execute(pattern_name='ANSI31', pattern_scale=2, pattern_angle_deg=30)

    def test_indeterminate_append_error_is_wrapped_without_repeating_add(self):
        with patch.object(FakeHatch, 'AppendOuterLoop', side_effect=RuntimeError('append failed')):
            with self.assertRaisesRegex(OutcomeUnknown, 'append failed'):
                self.execute()
        self.assertEqual([('AddHatch', 0, 'SOLID', False)], self.cad.calls)


class HatchReadbackTests(unittest.TestCase):
    def test_dispatch_array_uses_vt_dispatch_and_preserves_entities(self):
        pythoncom = ModuleType('pythoncom')
        pythoncom.VT_ARRAY, pythoncom.VT_DISPATCH = 0x2000, 9
        win32com = ModuleType('win32com')
        client = win32com.client = ModuleType('win32com.client')
        client.VARIANT = Mock()
        boundary = SimpleNamespace(_oleobj_=object())
        with patch.dict(sys.modules, {'pythoncom': pythoncom, 'win32com': win32com, 'win32com.client': client}):
            self.assertIs(client.VARIANT.return_value, parts._dispatch_array((boundary,)))
        client.VARIANT.assert_called_once_with(0x2009, [boundary])

    def test_dxf_dump_reads_patterned_and_solid_fields(self):
        for pattern, solid, optional in [
            ('ANSI31', 0, '41 2.5\n52 0.523599\n'),
            ('SOLID', 1, ''),
        ]:
            with self.subTest(pattern=pattern), TemporaryDirectory() as folder:
                def run_command(command):
                    self.assertIn('(entget (handent "CE"))', command)
                    self.assertIn('(0 5 8 2 70 71 41 52)', command)
                    path = re.search(r'\(open "([^"]+)"', command).group(1)
                    Path(path).write_text(f'0 HATCH\n5 CE\n8 Hatch Layer\n2 {pattern}\n'
                                          f'70 {solid}\n71 0\n{optional}', encoding='utf-8-sig')

                self.assertEqual({
                    'type': 'HATCH', 'handle': 'CE', 'layer': 'Hatch Layer', 'pattern_name': pattern,
                    'solid_fill': solid, 'associative': 0, 'pattern_scale': None if solid else 2.5,
                    'pattern_angle_rad': None if solid else 0.523599,
                }, panel.hatch_dxf(SimpleNamespace(run_command=run_command), 'CE', folder=folder))


if __name__ == '__main__':
    unittest.main()

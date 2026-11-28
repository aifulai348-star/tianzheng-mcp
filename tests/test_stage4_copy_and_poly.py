import re
import unittest
from copy import deepcopy
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tz_mcp import panel, parts
from tz_mcp.cad import Cad, OutcomeUnknown
from tz_mcp.cli import VALIDATION
from tz_mcp.commands import SCHEMAS, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


COPY_ARGS = {'document_id': 'copy-offline', 'handle': 'a1', 'delta': [50, -15]}
POINTS = [[0, 0], [30, 0], [30, 40]]
CLOSED_ERROR = "Property '<unknown>.Closed' can not be set."


def entity(handle, object_name, **properties):
    return SimpleNamespace(Handle=handle, ObjectName=object_name, **properties)


def column_dxf(x, y):
    return {'type': 'TCH_COLUMN', 'insertion': (x, y), 'height': 2600.0,
            'vertices': [(x - 450, y - 150), (x + 450, y - 150),
                         (x + 450, y + 150), (x - 450, y + 150)],
            'flags': (1, 60, 0)}


class FakeCAD:
    execute = Cad.execute
    document_id = COPY_ARGS['document_id']
    hwnd = 1

    def __init__(self, source, added):
        self.objects = {'A1': source, '01': entity('01', 'TDbWall')}
        self.added = {obj.Handle: obj for obj in added}
        self.commands = []
        self.after_command = lambda: None
        self.guard = Mock()
        self.doc = SimpleNamespace()

    def entities(self):
        return dict(self.objects)

    def entity(self, handle, document_id):
        return self.objects[handle]

    def describe(self, obj):
        return {'handle': obj.Handle, 'object_name': obj.ObjectName}

    def run_command(self, payload):
        self.commands.append(payload)
        self.objects.update(self.added)
        self.after_command()


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.window = self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.model_space = self.enterContext(patch.object(parts, 'require_model_space'))

    def test_prepare_and_capability(self):
        self.assertEqual(prepare('entity.copy', COPY_ARGS)['delta'], [50.0, -15.0, 0.0])
        for field in COPY_ARGS:
            with self.subTest(missing=field), self.assertRaises(ValueError):
                prepare('entity.copy', {k: v for k, v in COPY_ARGS.items() if k != field})
        for invalid in (
            {'document_id': ' '}, {'document_id': 12}, {'handle': '0xA1'},
            {'handle': 'AG'}, {'handle': 'A1\n'}, {'delta': [0, 0]},
            {'delta': [-1e-6, 1e-6]}, {'delta': [50, -15, 1]},
            {'delta': [float('nan'), 0]}, {'delta': [True, 0]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                prepare('entity.copy', dict(COPY_ARGS, **invalid))
        self.assertIn('entity.copy', SCHEMAS)
        self.assertTrue(VALIDATION['entity.copy'].startswith('verified_live'))
        self.assertNotIn('entity.copy', NO_ESCAPE_STEPS)

    def assert_copy(self, cad, geometry):
        result = cad.execute('entity.copy', COPY_ARGS)
        self.assertEqual(set(cad.objects), {'01', 'A1', 'B2'})
        self.assertEqual(cad.commands, [
            '(progn (command "_.COPY" (handent "A1") "" "_non" "100.000000,200.000000" '
            '"_non" "150.000000,185.000000") (princ))\n'
        ])
        self.assertEqual(result['handles'], ['B2'])
        self.assertEqual(result['document_id'], cad.document_id)
        self.assertEqual(result['entities'], [cad.describe(cad.objects['B2'])])
        self.assertEqual(result['validation'], {'type': True, 'properties': True,
                                                'geometry': geometry, 'reopen': 'pending'})
        evidence = result['evidence']['copy']
        self.assertEqual(evidence['source_handle'], 'A1')
        self.assertEqual(evidence['target_origin'], [150.0, 185.0])
        self.assertEqual(evidence['before'], evidence['source_after'])
        cad.guard.assert_called_once()
        self.window.assert_called_once_with(cad.hwnd)
        self.model_space.assert_called_once_with(cad.doc)
        return evidence['copy_after']

    def test_copy_column(self):
        cad = FakeCAD(entity('A1', 'TDbColumn', SectionShapeText='\u77e9\u5f62'),
                      [entity('B2', 'TDbColumn')])
        original = column_dxf(100, 200)
        copied = column_dxf(150, 185)
        with patch.object(parts, 'column_dxf', side_effect=[original, original, copied]) as probe:
            evidence = self.assert_copy(cad, 'verified_dxf_10_and_11')
        self.assertEqual([call.args[1] for call in probe.call_args_list], ['A1', 'A1', 'B2'])
        self.assertEqual(evidence, {k: v for k, v in copied.items() if k != 'type'})

    def test_copy_column_preserves_six_decimal_places_in_dxf(self):
        source = entity('A1', 'TDbColumn', SectionShapeText='\u77e9\u5f62')
        cad = FakeCAD(source, [entity('B2', 'TDbColumn')])
        dumps = {
            'A1': (9704.520000, 19808.500000),
            'B2': (12204.520000, 21008.500000),
        }
        run_copy = cad.run_command
        probes = []

        def run_command(payload):
            if payload.startswith('(progn (command "_.COPY"'):
                run_copy(payload)
                return
            match = re.search(r'\(open "([^"]+)" "w"\).*?\(handent "([^"]+)"\)', payload)
            self.assertIsNotNone(match)
            filename, handle = match.groups()
            self.assertRegex(payload, r"\(member \(car g\) '\(10 11\)\).*?\(rtos v 2 6\)")
            self.assertRegex(payload, r"\(member \(car g\) '\(50 149\)\).*?\(rtos \(cdr g\) 2 6\)")
            self.assertIn('(princ (vl-princ-to-string g) f)', payload)
            self.assertNotIn('(terpri ', payload)
            self.assertIn('(write-line "" f)', payload)
            probes.append(handle)
            x, y = dumps[handle]
            groups = [
                '(0 . TCH_COLUMN)', f'(5 . {handle})', '(70 . 1)', '(71 . 60)', '(72 . 0)',
                f'(11 {x:.6f} {y:.6f} 0.000000)', '(149 . 2600.123456)', '(50 . -0.123456)',
            ]
            # The fractional offsets expose rounding of the vertices too.
            for dx, dy in ((-450.123456, -150), (450.123456, -150),
                           (450.123456, 150), (-450.123456, 150)):
                groups.append(f'(10 {x + dx:.6f} {y + dy:.6f} 0.000000)')
            Path(filename).write_text('\n'.join(groups), encoding='gbk')

        with TemporaryDirectory() as folder, patch.object(cad, 'run_command', run_command):
            read_dxf = partial(panel.column_dxf, folder=folder)
            copied = read_dxf(cad, 'B2')
            with patch.object(parts, 'column_dxf', read_dxf):
                result = cad.execute('entity.copy', {**COPY_ARGS, 'delta': [2500, 1200]})
        self.assertEqual(probes, ['B2', 'A1', 'A1', 'B2'])
        for actual, expected in zip(copied['insertion'], dumps['B2']):
            self.assertAlmostEqual(actual, expected, delta=1e-6)
        self.assertAlmostEqual(copied['vertices'][0][0], 11754.396544, delta=1e-6)
        self.assertAlmostEqual(copied['vertices'][0][1], 20858.500000, delta=1e-6)
        self.assertAlmostEqual(copied['bulges'][0], -0.123456, delta=1e-6)
        self.assertAlmostEqual(copied['height'], 2600.123456, delta=1e-6)
        self.assertEqual(result['handles'], ['B2'])
        self.assertEqual(result['evidence']['copy']['target_origin'], list(dumps['B2']))
        self.assertEqual(result['validation']['geometry'], 'verified_dxf_10_and_11')

    def test_copy_text(self):
        cad = self.text_case()
        evidence = self.assert_copy(cad, 'verified_com_geometry')
        self.assertEqual(evidence, {'text': 'Label', 'height': 20.0, 'insertion': (150, 185, 5)})

    def test_copy_polyline(self):
        cad = FakeCAD(entity('A1', 'AcDbPolyline', Coordinates=(100, 200, 130, 240),
                             Closed=True, Length=100),
                      [entity('B2', 'AcDbPolyline', Coordinates=(150, 185, 180, 225),
                              Closed=True, Length=100)])
        evidence = self.assert_copy(cad, 'verified_com_geometry')
        self.assertEqual(evidence, {'vertices': [(150, 185), (180, 225)], 'closed': True, 'length': 100})

    @staticmethod
    def text_case():
        return FakeCAD(entity('A1', 'AcDbText', TextString='Label', Height=20, InsertionPoint=(100, 200, 5)),
                       [entity('B2', 'AcDbText', TextString='Label', Height=20, InsertionPoint=(150, 185, 5))])

    def test_prewrite_rejections(self):
        for name in ('TDbWall', 'TDbOpening', 'TDbSlab', 'TDbSlopeRoof', 'TDbRectStair', 'TDbColumn'):
            cad = FakeCAD(entity('A1', name, SectionShapeText='round'), [])
            with self.subTest(name=name), self.assertRaises(ValueError):
                cad.execute('entity.copy', COPY_ARGS)
            self.assertEqual(cad.commands, [])
        cad = self.text_case()
        for invalid in ({'handle': 'CA'}, {'document_id': 'foreign'}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cad.execute('entity.copy', dict(COPY_ARGS, **invalid))
        self.assertEqual(cad.commands, [])

    def test_constructed_rectangular_column_rejected(self):
        cad = FakeCAD(entity('A1', 'TDbColumn', SectionShapeText='\u77e9\u5f62'), [])
        dxf = dict(column_dxf(100, 200), flags=(1, 60, 1))
        with patch.object(parts, 'column_dxf', return_value=dxf), self.assertRaises(ValueError):
            cad.execute('entity.copy', COPY_ARGS)
        self.assertEqual(cad.commands, [])

    def test_invalid_com_geometry_rejected_before_copy(self):
        for name, props in (
            ('AcDbText', {'TextString': None, 'Height': 20, 'InsertionPoint': (100, 200)}),
            ('AcDbPolyline', {'Coordinates': (100, 200, 300, 400, 500), 'Closed': True, 'Length': 10}),
        ):
            cad = FakeCAD(entity('A1', name, **props), [])
            with self.subTest(name=name), self.assertRaises(OutcomeUnknown):
                cad.execute('entity.copy', COPY_ARGS)
            self.assertEqual(cad.commands, [])

    def test_postwrite_rejections(self):
        cases = (
            lambda c: c.objects.pop('B2'),
            lambda c: c.objects.update(B3=entity('B3', 'AcDbText')),
            lambda c: c.objects.pop('01'),
            lambda c: setattr(c.objects['B2'], 'ObjectName', 'AcDbPolyline'),
            lambda c: setattr(c.objects['A1'], 'TextString', 'changed source'),
            lambda c: setattr(c.objects['A1'], 'InsertionPoint', (100, 200, 6)),
            lambda c: setattr(c.objects['B2'], 'Height', 20.001),
            lambda c: setattr(c.objects['B2'], 'InsertionPoint', (150.011, 185, 5)),
        )
        for index, change in enumerate(cases):
            cad = self.text_case()
            cad.after_command = lambda: change(cad)
            with self.subTest(case=index), self.assertRaises(OutcomeUnknown):
                cad.execute('entity.copy', COPY_ARGS)
            self.assertEqual(len(cad.commands), 1)

    def test_missing_required_column_codes_before_and_after_copy(self):
        for stage in range(3):
            for group in (149, 72):
                cad = FakeCAD(entity('A1', 'TDbColumn', SectionShapeText='\u77e9\u5f62'),
                              [entity('B2', 'TDbColumn')])
                states = [column_dxf(100, 200), column_dxf(100, 200), column_dxf(150, 185)]
                invalid = deepcopy(states[stage])
                if group == 149:
                    del invalid['height']
                else:
                    invalid['flags'] = (1, 60, None)
                states[stage] = invalid
                with self.subTest(stage=stage, group=group), patch.object(
                        parts, 'column_dxf', side_effect=states), self.assertRaises(OutcomeUnknown):
                    cad.execute('entity.copy', COPY_ARGS)
                self.assertEqual(len(cad.commands), int(stage != 0))


class Busy(Exception):
    hresult = -2147418111


class FakePolyline:
    ObjectName = 'AcDbPolyline'

    def __init__(self, close_failures, ignore_write=False):
        self.close_failures = list(close_failures)
        self.ignore_write = ignore_write
        self.write_attempts = 0
        self.closed = False
        self.handle_attempts = 0

    @property
    def Closed(self):
        return self.closed

    @Closed.setter
    def Closed(self, value):
        self.write_attempts += 1
        if self.close_failures:
            raise self.close_failures.pop(0)
        if not self.ignore_write:
            self.closed = value

    @property
    def Handle(self):
        self.handle_attempts += 1
        if self.handle_attempts == 1:
            raise Busy()
        return 'A3'


class FakePolylineDocument:
    def __init__(self, add_polyline):
        self.accesses = 0
        self.model_space = SimpleNamespace(AddLightWeightPolyline=add_polyline)

    @property
    def ModelSpace(self):
        self.accesses += 1
        if self.accesses == 1:
            raise AttributeError('<unknown>.ModelSpace')
        return self.model_space


class PolylineRetryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(parts, 'vector', side_effect=tuple))
        self.late = self.enterContext(patch.object(parts, 'late', side_effect=lambda obj: obj))
        self.enterContext(patch('tz_mcp.cad.time.sleep'))

    def test_make_polyline_rebinds_and_retries_reads_without_duplicate_creation(self):
        polyline = FakePolyline([AttributeError(CLOSED_ERROR), AttributeError(CLOSED_ERROR)])
        add = Mock(return_value=polyline)
        doc = FakePolylineDocument(add)
        self.assertEqual(parts.make_polyline(SimpleNamespace(doc=doc), POINTS), 'A3')
        self.assertEqual(doc.accesses, 2)
        self.assertEqual(polyline.write_attempts, 3)
        self.assertEqual(polyline.handle_attempts, 2)
        self.assertTrue(polyline.Closed)
        add.assert_called_once_with((0, 0, 30, 0, 30, 40))
        self.assertEqual(self.late.call_count, 3)

    def test_persistent_closed_failure_surfaces(self):
        polyline = FakePolyline([AttributeError(CLOSED_ERROR) for _ in range(4)])
        add = Mock(return_value=polyline)
        doc = SimpleNamespace(ModelSpace=SimpleNamespace(AddLightWeightPolyline=add))
        with patch('tz_mcp.cad.time.monotonic', side_effect=[0, 0, 0, 1, 2, 3]):
            with self.assertRaisesRegex(AttributeError, 'Closed'):
                parts.make_polyline(SimpleNamespace(doc=doc), POINTS)
        self.assertEqual(polyline.write_attempts, 4)
        add.assert_called_once()

    def test_readback_mismatch_and_indeterminate_creation_surface(self):
        add = Mock(return_value=FakePolyline([], ignore_write=True))
        cad = SimpleNamespace(doc=SimpleNamespace(ModelSpace=SimpleNamespace(AddLightWeightPolyline=add)))
        with self.assertRaisesRegex(OutcomeUnknown, 'Closed readback mismatch'):
            parts.make_polyline(cad, POINTS)
        add.assert_called_once()
        add.reset_mock(side_effect=True)
        add.side_effect = Busy
        with self.assertRaises(Busy):
            parts.make_polyline(cad, POINTS)
        add.assert_called_once()

    def test_polyline_create_uses_same_retry_for_open_and_closed(self):
        with patch.object(parts, 'require_drawing_window'), patch.object(parts, 'require_model_space'):
            for closed in (False, True):
                with self.subTest(closed=closed):
                    cad = FakeCAD(entity('A1', 'TDbWall'), [])
                    polyline = FakePolyline([Busy()])
                    polyline.closed = not closed

                    def create(_):
                        cad.objects['A3'] = polyline
                        return polyline

                    add = Mock(side_effect=create)
                    cad.doc = FakePolylineDocument(add)
                    result = cad.execute('polyline.create', {'points': POINTS, 'closed': closed})
                    self.assertEqual(result['handles'], ['A3'])
                    self.assertIs(result['evidence']['closed'], closed)
                    self.assertEqual(polyline.write_attempts, 2)
                    add.assert_called_once()

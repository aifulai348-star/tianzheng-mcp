import math
import re
import sys
import unittest
from collections import Counter
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, OutcomeUnknown, retry_document_read
from tz_mcp.commands import prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


REF = {'document_id': 'draw-1', 'handle': 'a'}
LINE = {'start': [1, 2], 'end': [4, 6]}
CIRCLE = {'center': [-2, 3], 'radius': 5}
ARC = {**CIRCLE, 'start_angle_deg': 350, 'end_angle_deg': 10}
BASE_DXF = '5 B\n8 0\n'
LINE_DXF = '0 LINE\n' + BASE_DXF + '10 (1.0 2.0 0.0)\n11 (4.0 6.0 0.0)\n'
CIRCLE_DXF = '0 CIRCLE\n' + BASE_DXF + '10 (-2.0 3.0 0.0)\n40 5.0\n'
ARC_DXF = ('0 ARC\n' + BASE_DXF + '10 (-2.0 3.0 0.0)\n40 5.0\n'
           f'50 {math.radians(350)}\n51 {math.radians(10)}\n')


class PreparationTests(unittest.TestCase):
    def test_capabilities_are_pending_live_and_skip_escape_recovery(self):
        capabilities = cli.capabilities()
        for op, required, optional in (
            ('line.create', ['end', 'start'], []),
            ('circle.create', ['center', 'radius'], []),
            ('arc.create', ['center', 'end_angle_deg', 'radius', 'start_angle_deg'], []),
            ('layer.create', ['name'], ['color']),
            ('entity.set_layer', ['document_id', 'handle', 'layer'], []),
        ):
            with self.subTest(op=op):
                self.assertEqual(required, capabilities[op]['required'])
                self.assertEqual(optional, capabilities[op]['optional'])
                self.assertTrue(capabilities[op]['validation'].startswith('verified_live'))
                self.assertIn(op, NO_ESCAPE_STEPS)

    def test_normalization(self):
        self.assertEqual({'start': [1., 2., 0.], 'end': [4., 6., 0.]}, prepare('line.create', LINE))
        circle = prepare('circle.create', CIRCLE)
        self.assertEqual([-2., 3., 0.], circle['center'])
        self.assertIs(type(circle['radius']), float)
        arc = prepare('arc.create', {**ARC, 'start_angle_deg': -10, 'end_angle_deg': 370})
        self.assertIs(type(arc['start_angle_deg']), float)
        self.assertEqual((-10., 370.), (arc['start_angle_deg'], arc['end_angle_deg']))
        self.assertEqual({'name': 'Some Layer', 'color': 255},
                         prepare('layer.create', {'name': ' Some Layer ', 'color': 255}))
        self.assertEqual({'document_id': 'draw-1', 'handle': 'A', 'layer': 'Some Layer'},
                         prepare('entity.set_layer', {'document_id': ' draw-1 ', 'handle': 'a',
                                                      'layer': ' Some Layer '}))

    def test_invalid_geometry(self):
        for op, args, key, bad_values in (
            ('line.create', LINE, 'end', ([1, 2], [1.0000001, 2], [4, 6, 1], [True, 6],
                                         [float('inf'), 6], [10**400, 0])),
            ('circle.create', CIRCLE, 'center', ([0, 0, 1], [0], [0, float('nan')])),
            ('circle.create', CIRCLE, 'radius', (0, -1, True, '5', float('inf'), 10**400)),
            ('arc.create', ARC, 'center', ([0, 0, -1], ['0', 2])),
            ('arc.create', ARC, 'radius', (False, 0, float('nan'))),
            ('arc.create', ARC, 'start_angle_deg', (True, float('inf'), 10**400, '10')),
            ('arc.create', {**ARC, 'start_angle_deg': 0}, 'end_angle_deg',
             (0, 360, -720, 0.0000005, 359.9999995, float('nan'))),
        ):
            for value in bad_values:
                with self.subTest(op=op, key=key, value=value), self.assertRaises(ValueError):
                    prepare(op, {**args, key: value})

    def test_invalid_layer_inputs(self):
        forbidden = '<>/"\\:;?*|='
        for name in ['', '  ', 3, 'x' * 256, 'test\x00', '\tname',
                     *(f'a{ch}b' for ch in forbidden)]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    prepare('layer.create', {'name': name})
                with self.assertRaises(ValueError):
                    prepare('entity.set_layer', {**REF, 'layer': name})
        for color in (0, 256, True, 1.0, '7', None):
            with self.subTest(color=color), self.assertRaises(ValueError):
                prepare('layer.create', {'name': 'Test', 'color': color})
        for change in ({'document_id': ' '}, {'document_id': None}, {'handle': '0xa'},
                       {'handle': 'G'}, {'handle': ' A '}, {'extra': 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare('entity.set_layer', {**REF, 'layer': 'Test', **change})


class CallRejected(RuntimeError):
    hresult = -2147418111  # RPC_E_CALL_REJECTED


class ComCalls:
    def __init__(self):
        self.failures = {}
        self.attempts = Counter()

    def call(self, name):
        self.attempts[name] += 1
        if name in self.failures:
            raise self.failures.pop(name)


class Properties:
    def __init__(self, calls, **values):
        self.calls = calls
        self.values = values
        self.writes = []
        self.updates = 0
        self._oleobj_ = self

    def __getattr__(self, name):
        if name in self.values:
            self.calls.call(name)
            return self.values[name]
        raise AttributeError(name)

    def GetIDsOfNames(self, name):
        self.calls.call(f'lookup:{name}')
        return name

    def Invoke(self, name, lcid, flags, result_wanted, value):
        self.calls.call(f'put:{name}')
        self.writes.append((name, value))
        self.values[name] = value

    def Update(self):
        self.calls.call('Update')
        self.updates += 1


class Layers:
    def __init__(self, calls):
        self.calls = calls
        self.layers = [Properties(calls, Name='0', Color=7)]
        self.adds = []

    @property
    def Count(self):
        self.calls.call('Count')
        return len(self.layers)

    def Item(self, index):
        self.calls.call('Item')
        return self.layers[index]

    def Add(self, name):
        self.calls.call('Add')
        self.adds.append(name)
        layer = Properties(self.calls, Name=name, Color=7)
        self.layers.append(layer)
        return layer


class Document:
    def __init__(self, cad):
        self.cad = cad
        self.layers = Layers(cad.calls)
        self.adds = []

    @property
    def ModelSpace(self):
        self.cad.calls.call('ModelSpace')
        return self

    @property
    def Layers(self):
        self.cad.calls.call('Layers')
        return self.layers

    def GetVariable(self, name):
        if name == 'TILEMODE':
            return 1
        raise AssertionError(name)

    def add(self, method, kind, args, **properties):
        self.cad.calls.call(method)
        self.adds.append((method, args))
        values = {'Handle': 'b', 'ObjectName': kind, **properties, **self.cad.new_properties}
        entity = Properties(self.cad.calls, **values)
        self.cad.store['B'] = entity
        return entity

    def AddLine(self, start, end):
        return self.add('AddLine', 'AcDbLine', (start, end), StartPoint=start, EndPoint=end)

    def AddCircle(self, center, radius):
        return self.add('AddCircle', 'AcDbCircle', (center, radius), Center=center, Radius=radius)

    def AddArc(self, center, radius, start, end):
        return self.add('AddArc', 'AcDbArc', (center, radius, start, end), Center=center, Radius=radius,
                        StartAngle=start % math.tau, EndAngle=end % math.tau)


def write_entget(lisp, handle, data):
    if '(entget (handent "' + handle + '"))' not in lisp:
        raise AssertionError(lisp)
    match = re.search(r'\(open "([^"]+)" "w"', lisp)
    if match is None:
        raise AssertionError(lisp)
    Path(match[1]).write_bytes(data)


class FakeCad:
    hwnd = 123
    document_id = 'draw-1'

    def __init__(self):
        self.calls = ComCalls()
        self.entity_a = Properties(self.calls, Handle='A', ObjectName='AcDbLine', Layer='0')
        self.store = {'A': self.entity_a}
        self.doc = Document(self)
        self.new_properties = {}
        self.dump = None
        self.commands = []

    def guard(self):
        pass

    def entities(self):
        return dict(self.store)

    def entity(self, handle, document_id):
        if document_id != self.document_id:
            raise AssertionError(document_id)
        return self.store[handle]

    def describe(self, entity):
        return {'handle': entity.Handle.upper(), 'object_name': entity.ObjectName}

    def run_command(self, lisp):
        self.commands.append(lisp)
        handle, data = self.dump
        write_entget(lisp, handle, data.encode('utf-8-sig'))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'late', side_effect=lambda value: value))
        self.enterContext(patch.object(parts, 'vector', side_effect=tuple))
        self.enterContext(patch('tz_mcp.cad.time.sleep'))
        self.enterContext(patch.dict(sys.modules, pythoncom=SimpleNamespace(DISPATCH_PROPERTYPUT=4)))
        for parser in ('line_dxf', 'circle_dxf', 'arc_dxf', 'entity_layer_dxf'):
            self.enterContext(patch.object(parts, parser, partial(getattr(panel, parser), folder=folder)))

    def execute(self, op, args, dxf=None, handle='B'):
        self.cad.dump = (handle, dxf) if dxf is not None else None
        return Cad.execute(self.cad, op, args)

    def test_line_creation_and_busy_retries(self):
        self.cad.calls.failures = {'ModelSpace': AttributeError('<unknown>.ModelSpace'),
                                   'AddLine': CallRejected(), 'Handle': CallRejected(),
                                   'StartPoint': CallRejected(), 'EndPoint': CallRejected()}
        result = self.execute('line.create', LINE, LINE_DXF)
        self.assertTrue(result['success'])
        self.assertEqual(['B'], result['handles'])
        self.assertEqual({'A', 'B'}, set(self.cad.store))
        self.assertEqual([('AddLine', ((1., 2., 0.), (4., 6., 0.)))], self.cad.doc.adds)
        self.assertEqual({'type': True, 'properties': True, 'dxf': 'verified_10_11', 'reopen': 'pending'},
                         result['validation'])
        self.assertEqual(5., result['evidence']['line']['length'])
        self.assertEqual((4., 6., 0.), result['evidence']['line']['dxf']['end'])
        self.assertEqual(1, len(self.cad.commands))
        self.assertEqual({}, self.cad.calls.failures)

    def test_circle_creation_and_busy_retries(self):
        self.cad.calls.failures = {'AddCircle': CallRejected(), 'Center': CallRejected(), 'Radius': CallRejected()}
        result = self.execute('circle.create', CIRCLE, CIRCLE_DXF)
        self.assertEqual([('AddCircle', ((-2., 3., 0.), 5.))], self.cad.doc.adds)
        self.assertEqual([-2., 3., 0.], result['evidence']['circle']['center'])
        self.assertEqual(5., result['evidence']['circle']['radius'])
        self.assertEqual('verified_10_40', result['validation']['dxf'])
        self.assertEqual({}, self.cad.calls.failures)

    def test_arc_wraparound_radians_and_busy_retries(self):
        self.cad.calls.failures = {'AddArc': CallRejected(), 'StartAngle': CallRejected(), 'EndAngle': CallRejected()}
        result = self.execute('arc.create', {**ARC, 'start_angle_deg': -10, 'end_angle_deg': 370}, ARC_DXF)
        start, end = math.radians(-10), math.radians(370)
        self.assertEqual([('AddArc', ((-2., 3., 0.), 5., start, end))], self.cad.doc.adds)
        evidence = result['evidence']['arc']
        self.assertEqual((-10., 370.), (evidence['start_angle_deg'], evidence['end_angle_deg']))
        self.assertEqual((start, end), (evidence['start_angle_rad'], evidence['end_angle_rad']))
        self.assertAlmostEqual(math.radians(350), evidence['dxf']['start_angle_rad'])
        self.assertEqual('verified_10_40_50_51', result['validation']['dxf'])
        self.assertEqual({}, self.cad.calls.failures)

    def test_primitive_rejects_unverified_com_or_dxf(self):
        for op, args, overrides, dxf, message in (
            ('line.create', LINE, {'ObjectName': 'AcDbArc'}, LINE_DXF, 'COM type mismatch'),
            ('line.create', LINE, {'EndPoint': (5., 6., 0.)}, LINE_DXF, 'COM EndPoint'),
            ('line.create', LINE, {}, LINE_DXF.replace('4.0 6.0', '5.0 6.0'), 'DXF 11'),
            ('circle.create', CIRCLE, {}, CIRCLE_DXF.replace('CIRCLE', 'ARC'), 'DXF type'),
            ('circle.create', CIRCLE, {}, CIRCLE_DXF.replace('40 5.0', '40 6.0'), 'DXF 40'),
            ('arc.create', ARC, {'EndAngle': 0.9}, ARC_DXF, 'COM EndAngle'),
            ('arc.create', ARC, {}, ARC_DXF.replace('51 ', '52 '), 'DXF 51'),
        ):
            with self.subTest(op=op, message=message):
                self.cad = FakeCad()
                self.cad.new_properties = overrides
                with self.assertRaisesRegex(OutcomeUnknown, message):
                    self.execute(op, args, dxf)

    def test_creation_rejects_added_or_removed_unrelated_handles(self):
        for unexpected in ({'B'}, {'A', 'B', 'C'}):
            with self.subTest(unexpected=unexpected):
                with patch.object(self.cad, 'entities', side_effect=[{'A': self.cad.entity_a},
                                                                    dict.fromkeys(unexpected)]):
                    with self.assertRaisesRegex(OutcomeUnknown, 'handle changes'):
                        self.execute('line.create', LINE, LINE_DXF)

    def test_layer_creation_and_idempotent_color_update_retry(self):
        self.cad.calls.failures = {'Layers': AttributeError('<unknown>.Layers'),
                                   'Count': CallRejected(), 'Item': CallRejected(),
                                   'Add': CallRejected(), 'Color': CallRejected(),
                                   'lookup:Color': CallRejected(), 'put:Color': CallRejected()}
        result = self.execute('layer.create', {'name': ' Walls ', 'color': 22})
        self.assertEqual({'name': 'Walls', 'created': True, 'color': 22}, result['evidence']['layer'])
        self.assertEqual([], result['handles'])
        self.assertEqual([], result['entities'])
        self.assertEqual([], self.cad.commands)
        self.assertEqual({}, self.cad.calls.failures)

        result = self.execute('layer.create', {'name': 'walls'})
        self.assertEqual({'name': 'Walls', 'created': False, 'color': 22}, result['evidence']['layer'])
        result = self.execute('layer.create', {'name': 'WALLS', 'color': 42})
        self.assertEqual({'name': 'Walls', 'created': False, 'color': 42}, result['evidence']['layer'])
        self.assertEqual(['Walls'], self.cad.doc.layers.adds)
        self.assertEqual([('Color', 22), ('Color', 42)], self.cad.doc.layers.layers[-1].writes)
        self.assertEqual({'A'}, set(self.cad.store))

    def test_layer_without_requested_color_reports_com_default(self):
        result = self.execute('layer.create', {'name': 'Walls'})
        self.assertEqual({'name': 'Walls', 'created': True, 'color': 7}, result['evidence']['layer'])
        self.assertEqual([], self.cad.doc.layers.layers[-1].writes)

    def test_layer_verification_requires_same_handles_and_color(self):
        layer = self.cad.doc.layers.Add('Walls')
        with patch.object(layer, 'Invoke'):
            with self.assertRaisesRegex(OutcomeUnknown, 'color readback mismatch'):
                self.execute('layer.create', {'name': 'Walls', 'color': 22})
        with patch.object(self.cad, 'entities', side_effect=[{'A': self.cad.entity_a}, {}]):
            with self.assertRaisesRegex(OutcomeUnknown, 'handles changed'):
                self.execute('layer.create', {'name': 'Walls'})

    def test_set_layer_uses_canonical_name_and_retries_property_put_and_update(self):
        self.cad.doc.layers.Add('Walls')
        self.cad.calls.failures = {'Layer': CallRejected(), 'ObjectName': CallRejected(),
                                   'put:Layer': CallRejected(), 'Update': CallRejected()}
        dxf = '0 LINE\n5 A\n8 WALLS\n'
        result = self.execute('entity.set_layer', {**REF, 'layer': ' walls '}, dxf, handle='A')
        self.assertEqual(['A'], result['handles'])
        self.assertEqual([('Layer', 'Walls')], self.cad.entity_a.writes)
        self.assertEqual(1, self.cad.entity_a.updates)
        self.assertEqual({'A'}, set(self.cad.store))
        self.assertEqual({'handle': 'A', 'object_name': 'AcDbLine', 'layer_before': '0',
                          'layer_requested': 'Walls', 'layer_after': 'Walls',
                          'dxf_after': {'type': 'LINE', 'handle': 'A', 'layer': 'WALLS'}},
                         result['evidence']['set_layer'])
        self.assertEqual('verified_8', result['validation']['dxf'])
        self.assertEqual({}, self.cad.calls.failures)

    def test_set_layer_refuses_missing_layer_or_invalid_reference_before_write(self):
        for args, message in ((REF, 'Layer does not exist: Missing'),
                              ({**REF, 'document_id': 'foreign'}, 'document_id mismatch'),
                              ({**REF, 'handle': 'F'}, 'Unknown handle: F')):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, message):
                self.execute('entity.set_layer', {**args, 'layer': 'Missing'})
        self.assertEqual([], self.cad.entity_a.writes)
        self.assertEqual(0, self.cad.entity_a.updates)
        self.assertEqual([], self.cad.doc.layers.adds)
        self.assertEqual([], self.cad.commands)

    def test_set_layer_reports_com_and_dxf_mismatch(self):
        self.cad.doc.layers.Add('Walls')
        args = {**REF, 'layer': 'Walls'}
        with patch.object(self.cad.entity_a, 'Invoke'):
            with self.assertRaisesRegex(OutcomeUnknown, 'COM Layer readback mismatch'):
                self.execute('entity.set_layer', args)
        with self.assertRaisesRegex(OutcomeUnknown, 'DXF 8 layer readback mismatch'):
            self.execute('entity.set_layer', args, '0 LINE\n5 A\n8 Wrong\n', handle='A')

    def test_new_dispatch_properties_retry_transient_lookup_errors(self):
        for name in ('Layers', 'StartPoint', 'EndPoint', 'Center', 'Radius', 'StartAngle',
                     'EndAngle', 'Layer', 'Color', 'AddLine', 'AddCircle', 'AddArc'):
            with self.subTest(name=name):
                def read():
                    self.cad.calls.call(name)
                    return name

                self.cad.calls.failures[name] = AttributeError(f'<unknown>.{name}')
                self.assertEqual(name, retry_document_read(read))
                self.assertEqual(2, self.cad.calls.attempts[name])


class DxfParserTests(unittest.TestCase):
    def test_selected_groups_and_parsed_values(self):
        for parser, groups, data, expected in (
            (panel.line_dxf, '0 5 8 10 11', LINE_DXF,
             {'type': 'LINE', 'start': (1., 2., 0.), 'end': (4., 6., 0.)}),
            (panel.circle_dxf, '0 5 8 10 40', CIRCLE_DXF,
             {'type': 'CIRCLE', 'center': (-2., 3., 0.), 'radius': 5.}),
            (panel.arc_dxf, '0 5 8 10 40 50 51', ARC_DXF,
             {'type': 'ARC', 'center': (-2., 3., 0.), 'radius': 5.,
              'start_angle_rad': math.radians(350), 'end_angle_rad': math.radians(10)}),
            (panel.entity_layer_dxf, '0 5 8', '0 TEXT\r\n5 B\r\n8 带 空格图层\r\n',
             {'type': 'TEXT', 'layer': '带 空格图层'}),
        ):
            for encoding in ('gbk', 'utf-8-sig'):
                with self.subTest(parser=parser.__name__, encoding=encoding), TemporaryDirectory() as folder:
                    def run(lisp):
                        self.assertIn(f'(quote ({groups}))', lisp)
                        write_entget(lisp, 'B', data.encode(encoding))

                    self.assertEqual({'handle': 'B', 'layer': '0', **expected},
                                     parser(SimpleNamespace(run_command=run), 'B', folder=folder))
                    self.assertEqual([], list(Path(folder).iterdir()))

    def test_missing_layer_code_is_none(self):
        with TemporaryDirectory() as folder:
            cad = SimpleNamespace(run_command=lambda lisp: write_entget(lisp, 'B', b'0 LINE\n5 B\n'))
            self.assertEqual({'type': 'LINE', 'handle': 'B', 'layer': None},
                             panel.entity_layer_dxf(cad, 'B', folder=folder))


if __name__ == '__main__':
    unittest.main()

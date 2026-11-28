import sys
import unittest
from collections import deque
from functools import partial
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_primitives_and_layers import CallRejected, Document, FakeCad as BaseCad, Properties
from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, OutcomeUnknown, retry_document_read
from tz_mcp.commands import prepare
from tz_mcp.engine import NO_ESCAPE_STEPS
from tz_mcp.store import validate


REF = {'document_id': 'draw-1', 'handle': 'A'}
RECTANGLE = {'corner1': [9, -2, 0], 'corner2': [-3, 4]}
VERTICES = [(-3., -2.), (9., -2.), (9., 4.), (-3., 4.)]
SCHEMAS = {
    'rectangle.create': (RECTANGLE, ['constant_width', 'elevation']),
    'polyline.edit': (REF, ['closed', 'constant_width', 'elevation']),
    'entity.set_color': ({**REF, 'color': 256}, []),
    'layer.set_active': ({'name': 'Walls'}, []),
    'entity.query': ({}, ['bbox', 'layer', 'limit', 'object_name']),
}


def polyline_dump(handle='A', *, flags=0, vertices=VERTICES, elevation=None, width=None, bulges=()):
    records = ['0 LWPOLYLINE', f'5 {handle}', '8 0', f'70 {flags}']
    records.extend(f'10 ({x} {y})' for x, y in vertices)
    records.extend(f'42 {bulge}' for bulge in bulges)
    if elevation is not None:
        records.append(f'38 {elevation}')
    if width is not None:
        records.append(f'43 {width}')
    return handle, '\n'.join(records) + '\n'


class PreparationTests(unittest.TestCase):
    def test_capabilities_and_recipe_validation(self):
        capabilities = cli.capabilities()
        for op, (required, optional) in SCHEMAS.items():
            with self.subTest(op=op):
                self.assertEqual(sorted(required), capabilities[op]['required'])
                self.assertEqual(optional, capabilities[op]['optional'])
                self.assertTrue(capabilities[op]['validation'].startswith('verified_live'))
                self.assertIn(op, NO_ESCAPE_STEPS)
                args = {**required, 'closed': False} if op == 'polyline.edit' else required
                validate({'steps': [{'id': 'step', 'op': op, 'args': args}]})

    def test_normalizes_inputs_and_preserves_omitted_properties(self):
        self.assertEqual({'corner1': [9., -2., 0.], 'corner2': [-3., 4., 0.]},
                         prepare('rectangle.create', RECTANGLE))
        self.assertEqual({**REF, 'closed': False, 'constant_width': 0., 'elevation': -5.},
                         prepare('polyline.edit', {'document_id': ' draw-1 ', 'handle': 'a',
                                                   'closed': False, 'constant_width': 0, 'elevation': -5}))
        for value in (0, 1, 255, 256):
            self.assertEqual({**REF, 'color': value},
                             prepare('entity.set_color', {'document_id': ' draw-1 ', 'handle': 'a',
                                                          'color': value}))
        self.assertEqual({'name': 'Walls'}, prepare('layer.set_active', {'name': ' Walls '}))
        self.assertEqual({'limit': 100}, prepare('entity.query', {}))
        self.assertEqual({'object_name': 'AcDbLine', 'layer': 'Walls',
                          'bbox': [[1., 2., 0.], [1., 5., 0.]], 'limit': 1},
                         prepare('entity.query', {'object_name': ' AcDbLine ', 'layer': ' Walls ',
                                                  'bbox': [(1, 2), [1, 5, 0]], 'limit': 1}))

    def test_invalid_values_are_rejected(self):
        for op, args, key, values in (
            ('rectangle.create', RECTANGLE, 'corner2', ([9, 4], [9 - 1e-7, 4], [-3, -2], [-3, 4, 1],
                                                        [True, 0], [10**400, 0])),
            ('rectangle.create', RECTANGLE, 'elevation', (True, '1', float('inf'))),
            ('rectangle.create', RECTANGLE, 'constant_width', (-1, False, float('nan'))),
            ('polyline.edit', REF, 'closed', (0, 1, None, 'true')),
            ('polyline.edit', REF, 'constant_width', (-1, True, 10**400)),
            ('polyline.edit', REF, 'elevation', (False, None, float('nan'))),
            ('polyline.edit', {**REF, 'closed': True}, 'handle', ('0xa', 'G', ' A ')),
            ('entity.set_color', REF, 'color', (-1, 257, True, 1.0, '1')),
            ('entity.set_color', {**REF, 'color': 2}, 'document_id', (None, ' ')),
            ('layer.set_active', {}, 'name', ('', 'a*b', 'a\n', 'x' * 256)),
            ('entity.query', {}, 'object_name', ('', '\tname', 'x' * 129, 'a\x85b', 22)),
            ('entity.query', {}, 'layer', (' ', 'a|b')),
            ('entity.query', {}, 'bbox', ([1, 2], [[1, 2]], ((0, 0), (1, 1)),
                                          [[2, 0], [1, 1]], [[0, 2], [1, 1]], [[0, 0, 1], [1, 1]])),
            ('entity.query', {}, 'limit', (0, 101, True, 1.0, '1')),
        ):
            for value in values:
                with self.subTest(op=op, key=key, value=value), self.assertRaises(ValueError):
                    prepare(op, {**args, key: value})
        with self.assertRaisesRegex(ValueError, 'requires at least one property to change'):
            prepare('polyline.edit', REF)


class GeometryDocument(Document):
    def __init__(self, cad):
        super().__init__(cad)
        self.layers.layers = [Properties(cad.calls, Name=name, Freeze=frozen)
                              for name, frozen in (('0', False), ('Walls', False), ('Frozen', True))]
        self.active_layer = self.layers.layers[0]
        self.clayer = self.active_layer.Name
        self.variable_writes = []

    @property
    def ActiveLayer(self):
        return self.active_layer

    def GetVariable(self, name):
        if name == 'CLAYER':
            return self.clayer
        return super().GetVariable(name)

    def SetVariable(self, name, value):
        self.cad.calls.call('SetVariable')
        self.variable_writes.append((name, value))
        self.active_layer = next(layer for layer in self.layers.layers if layer.Name == value)
        self.clayer = value

    def AddLightWeightPolyline(self, flat):
        x0, y0, x1, _, _, y1, _, _ = flat
        return self.add('AddLightWeightPolyline', 'AcDbPolyline', (flat,), Closed=False,
                        Elevation=0., ConstantWidth=0., Area=(x1 - x0) * (y1 - y0))


class GeometryCad(BaseCad):
    def __init__(self):
        super().__init__()
        self.doc = GeometryDocument(self)
        self.dumps = deque()

    def run_command(self, lisp):
        self.dump = self.dumps.popleft()
        return super().run_command(lisp)

    def describe(self, entity):
        return {**super().describe(entity), **entity.values.get('description', {})}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cad = GeometryCad()
        self.folder = self.enterContext(TemporaryDirectory())
        self.window = self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'late', side_effect=lambda value: value))
        self.enterContext(patch.object(parts, 'vector', side_effect=tuple))
        self.enterContext(patch('tz_mcp.cad.time.sleep'))
        self.enterContext(patch.dict(sys.modules, pythoncom=SimpleNamespace(DISPATCH_PROPERTYPUT=4)))
        for parser in ('polyline_props_dxf', 'entity_color_dxf'):
            self.enterContext(patch.object(parts, parser, partial(getattr(panel, parser), folder=self.folder)))

    def execute(self, op, args, *dumps):
        self.cad.dumps = deque(dumps)
        return Cad.execute(self.cad, op, args)

    def set_polyline(self):
        entity = self.cad.entity_a
        entity.values.update(ObjectName='AcDbPolyline', Closed=False, Elevation=0., ConstantWidth=0.)
        return entity

    def test_dxf_parsers_preserve_vertex_and_bulge_order_and_optional_values(self):
        self.cad.dumps.append(polyline_dump(flags=129, elevation=-15, width=0, bulges=(0, -0.25)))
        dxf = panel.polyline_props_dxf(self.cad, 'A', folder=self.folder)
        self.assertEqual({'type': 'LWPOLYLINE', 'handle': 'A', 'layer': '0', 'closed': 1,
                          'vertices': VERTICES, 'elevation': -15., 'constant_width': 0.,
                          'bulges': (0., -0.25)}, dxf)
        self.cad.dumps.extend((('A', '0 LINE\n5 A\n62 0\n'), ('A', '0 LINE\n5 A\n')))
        self.assertEqual(0, panel.entity_color_dxf(self.cad, 'A', folder=self.folder)['color'])
        self.assertIsNone(panel.entity_color_dxf(self.cad, 'A', folder=self.folder)['color'])

    def test_rectangle_creates_canonical_geometry_and_retries_refused_com_calls(self):
        self.cad.calls.failures.update({'ModelSpace': AttributeError('<unknown>.ModelSpace'),
                                       'AddLightWeightPolyline': CallRejected(), 'put:Closed': CallRejected(),
                                       'Update': CallRejected()})
        result = self.execute('rectangle.create', {**RECTANGLE, 'elevation': -15, 'constant_width': 2},
                              polyline_dump('B', flags=1, elevation=-15, width=2))
        entity = self.cad.store['B']
        self.assertEqual([('AddLightWeightPolyline', ((-3., -2., 9., -2., 9., 4., -3., 4.),))],
                         self.cad.doc.adds)
        self.assertEqual([('Closed', True), ('Elevation', -15.), ('ConstantWidth', 2.)], entity.writes)
        self.assertEqual(1, entity.updates)
        self.assertEqual(['B'], result['handles'])
        self.assertEqual({'A', 'B'}, self.cad.store.keys())
        self.assertEqual('verified_10_70_38_43', result['validation']['dxf'])
        evidence = result['evidence']['rectangle']
        self.assertEqual((VERTICES, 12., 6., 72., -15., 2.),
                         tuple(evidence[key] for key in ('corners', 'width', 'height', 'area',
                                                         'elevation', 'constant_width')))
        self.assertEqual({}, self.cad.calls.failures)

    def test_rectangle_omits_optional_com_writes(self):
        self.execute('rectangle.create', RECTANGLE, polyline_dump('B', flags=1))
        self.assertEqual([('Closed', True)], self.cad.store['B'].writes)

    def test_rectangle_rejects_unverified_com_or_dxf_readback(self):
        good = polyline_dump('B', flags=1, width=2, elevation=-15)[1]
        for props, data, message in (
            ({'ObjectName': 'AcDbLine'}, good, 'COM type'),
            ({'Area': 75}, good, 'COM rectangle area'),
            ({}, good.replace('LWPOLYLINE', 'POLYLINE'), 'DXF type'),
            ({}, good.replace('70 1', '70 0'), 'closed four-vertex'),
            ({}, good.replace('10 (9.0 4.0)\n', ''), 'closed four-vertex'),
            ({}, good.replace('10 (9.0 4.0)', '10 (10.0 4.0)'), 'DXF 10'),
            ({}, good.replace('38 -15', '38 -14'), 'DXF 38'),
            ({}, good.replace('43 2\n', ''), 'DXF 43'),
        ):
            with self.subTest(props=props, message=message), self.assertRaisesRegex(OutcomeUnknown, message):
                self.cad.store.pop('B', None)
                self.cad.new_properties = props
                self.execute('rectangle.create', {**RECTANGLE, 'elevation': -15, 'constant_width': 2},
                             ('B', data))
        self.cad.store.pop('B')
        self.cad.new_properties = {}
        with patch.object(self.cad, 'entities', side_effect=[{'A': self.cad.entity_a}, {'B': None}]):
            with self.assertRaisesRegex(OutcomeUnknown, 'handle changes'):
                self.execute('rectangle.create', RECTANGLE)

    def test_polyline_edits_requested_properties_and_preserves_other_properties(self):
        entity = self.set_polyline()
        result = self.execute('polyline.edit', {**REF, 'closed': True, 'elevation': -2, 'constant_width': 3},
                              polyline_dump(), polyline_dump(flags=129, elevation=-2, width=3))
        self.assertEqual('verified_70_38_43', result['validation']['dxf'])
        self.assertEqual({'Closed': True, 'Elevation': -2., 'ConstantWidth': 3.},
                         result['evidence']['edit']['properties_after'])
        self.assertEqual(1, entity.updates)
        entity.writes.clear()
        self.execute('polyline.edit', {**REF, 'closed': False},
                     polyline_dump(flags=1, elevation=-2, width=3), polyline_dump(elevation=-2, width=3))
        self.assertEqual([('Closed', False)], entity.writes)

    def test_polyline_and_color_reference_guards_run_before_writes(self):
        for op, change in (('polyline.edit', {'closed': True}), ('entity.set_color', {'color': 7})):
            for invalid in ({'document_id': 'foreign'}, {'handle': 'BAD'}):
                with self.subTest(op=op, invalid=invalid), self.assertRaises(ValueError):
                    self.execute(op, {**REF, **change, **invalid})
        with self.assertRaisesRegex(ValueError, 'native AcDbPolyline'):
            self.execute('polyline.edit', {**REF, 'closed': True})
        self.assertEqual([], self.cad.entity_a.writes)
        self.assertEqual([], self.cad.commands)

    def test_polyline_refuses_wrong_dxf_type_bulges_and_unreadable_width_before_writing(self):
        entity = self.set_polyline()
        for dump, message in ((('A', '0 POLYLINE\n'), 'requires DXF'),
                              (polyline_dump(bulges=(0, -0.2)), 'bulged polylines')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.execute('polyline.edit', {**REF, 'closed': True}, dump)
        self.cad.calls.failures['ConstantWidth'] = RuntimeError('mixed widths')
        with self.assertRaisesRegex(ValueError, 'ConstantWidth'):
            self.execute('polyline.edit', {**REF, 'closed': True}, polyline_dump())
        self.assertEqual([], entity.writes)
        self.assertEqual(0, entity.updates)

    def test_polyline_detects_dxf_and_com_changes(self):
        entity = self.set_polyline()
        for dxf, message in (
            (polyline_dump(elevation=-2, width=3), 'DXF 70'),
            (polyline_dump(flags=1, elevation=-2, width=3, vertices=VERTICES[:3]), 'vertex count'),
            (polyline_dump(flags=1, elevation=-1, width=3), 'DXF 38'),
            (polyline_dump(flags=1, elevation=-2, width=4), 'DXF 43'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(OutcomeUnknown, message):
                self.execute('polyline.edit', {**REF, 'closed': True, 'constant_width': 3, 'elevation': -2},
                             polyline_dump(), dxf)
        with patch.object(entity, 'Invoke'), self.assertRaisesRegex(OutcomeUnknown, 'COM Elevation'):
            self.execute('polyline.edit', {**REF, 'elevation': 10}, polyline_dump())

    def test_color_verifies_byblock_aci_and_implicit_bylayer(self):
        entity = self.cad.entity_a
        entity.values['Color'] = 7
        for color, group in ((0, '62 0\n'), (255, '62 255\n'), (256, '')):
            with self.subTest(color=color):
                result = self.execute('entity.set_color', {**REF, 'color': color}, ('A', '0 LINE\n' + group))
                self.assertEqual('verified_62', result['validation']['dxf'])
                self.assertEqual(color, result['evidence']['set_color']['color_after'])
        self.assertEqual([('Color', 0), ('Color', 255), ('Color', 256)], entity.writes)
        self.assertEqual(3, entity.updates)

    def test_color_rejects_com_or_dxf_disagreement(self):
        entity = self.cad.entity_a
        entity.values['Color'] = 7
        with patch.object(entity, 'Invoke'), self.assertRaisesRegex(OutcomeUnknown, 'COM Color'):
            self.execute('entity.set_color', {**REF, 'color': 6})
        for group in ('', '62 7\n'):
            with self.subTest(group=group), self.assertRaisesRegex(OutcomeUnknown, 'DXF 62'):
                self.execute('entity.set_color', {**REF, 'color': 6}, ('A', '0 LINE\n' + group))
        with self.assertRaisesRegex(OutcomeUnknown, 'DXF 62'):
            self.execute('entity.set_color', {**REF, 'color': 256}, ('A', ''))

    def test_active_layer_writes_canonical_name_and_checks_both_readbacks(self):
        self.cad.calls.failures['SetVariable'] = CallRejected()
        result = self.execute('layer.set_active', {'name': ' walls '})
        self.assertEqual({'before': '0', 'requested': 'Walls', 'after': 'Walls', 'clayer': 'Walls'},
                         result['evidence']['active_layer'])
        self.assertEqual([('CLAYER', 'Walls')], self.cad.doc.variable_writes)
        self.assertEqual([], result['handles'])
        self.assertEqual({'A'}, self.cad.store.keys())
        self.assertEqual([], self.cad.commands)
        for bad_readback in ('ActiveLayer', 'CLAYER'):
            with self.subTest(bad_readback=bad_readback):
                old, walls, _ = self.cad.doc.layers.layers
                self.cad.doc.active_layer = old if bad_readback == 'ActiveLayer' else walls
                self.cad.doc.clayer = '0' if bad_readback == 'CLAYER' else 'Walls'
                with patch.object(self.cad.doc, 'SetVariable'), self.assertRaisesRegex(OutcomeUnknown, 'readback mismatch'):
                    self.execute('layer.set_active', {'name': 'Walls'})

    def test_active_layer_rejects_missing_and_frozen_before_writes_and_detects_handle_changes(self):
        for name, message in (('missing', 'does not exist'), ('frozen', 'frozen layer')):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                self.execute('layer.set_active', {'name': name})
        self.assertEqual([], self.cad.doc.variable_writes)
        with patch.object(self.cad, 'entities', side_effect=[self.cad.store, {}]):
            with self.assertRaisesRegex(OutcomeUnknown, 'Entity handles changed'):
                self.execute('layer.set_active', {'name': 'walls'})

    def test_query_intersects_xy_bounds_sorts_handles_and_reports_truncation(self):
        inside = {'bounds': [[1, 2, 300], [3, 4, 400]]}
        for handle, kind, layer, description in (
            ('C', 'AcDbLine', 'WALLS', {'bounds': [[3, 4, -450], [4, 5, -400]]}),
            ('F', 'AcDbLine', 'Other', inside), ('D', 'AcDbCircle', 'Walls', inside),
            ('E', 'AcDbLine', 'Walls', {}),
            ('A', 'AcDbLine', 'Walls', {'bounds': [[0, 2, 0], [2.99, 4, 0]]}),
            ('B', 'AcDbLine', 'walls', {**inside, 'color': 0}),
        ):
            self.cad.store[handle] = Properties(self.cad.calls, Handle=handle, ObjectName=kind,
                                                Layer=layer, Color=256, description=description)
        query = {'object_name': ' AcDbLine ', 'layer': ' walls ', 'bbox': [[3, 4], [3, 4]], 'limit': 1}
        result = self.execute('entity.query', query)
        self.assertEqual(['B'], result['handles'])
        self.assertEqual([{'handle': 'B', 'object_name': 'AcDbLine', 'layer': 'walls', 'color': 0,
                           **inside}], result['entities'])
        self.assertEqual({'filters': {'object_name': 'AcDbLine', 'layer': 'walls',
                                      'bbox': [[3., 4., 0.], [3., 4., 0.]]},
                          'total_matched': 2, 'returned': 1, 'truncated': True}, result['evidence']['query'])
        self.assertEqual('not_applicable', result['validation']['reopen'])
        all_entities = self.execute('entity.query', {})
        self.assertEqual(['A', 'B', 'C', 'D', 'E', 'F'], all_entities['handles'])
        self.assertEqual(('Walls', 256), (all_entities['entities'][0]['layer'], all_entities['entities'][0]['color']))
        self.assertEqual([], self.execute('entity.query', {'object_name': 'acdbline'})['handles'])
        self.assertEqual([], self.cad.commands)
        self.assertFalse(any(entity.writes for entity in self.cad.store.values()))
        self.assertEqual([], self.cad.doc.variable_writes)

    def test_query_default_limit_counts_every_match(self):
        self.cad.store = {}
        for number in reversed(range(256, 357)):
            handle = f'{number:X}'
            self.cad.store[handle] = Properties(self.cad.calls, Handle=handle, ObjectName='AcDbLine',
                                                Layer='0', Color=7)
        result = self.execute('entity.query', {})
        self.assertEqual([f'{number:X}' for number in range(256, 356)], result['handles'])
        self.assertEqual({'filters': {}, 'total_matched': 101, 'returned': 100, 'truncated': True},
                         result['evidence']['query'])

    def test_query_drawing_guards_precede_enumeration(self):
        with patch.object(self.cad, 'guard') as guard, patch.object(self.cad, 'entities') as entities:
            with patch.object(self.cad.doc, 'GetVariable', return_value=0):
                with self.assertRaisesRegex(ValueError, 'paper space'):
                    self.execute('entity.query', {})
            guard.assert_called_once_with()
            self.window.assert_called_once_with(self.cad.hwnd)
            entities.assert_not_called()

    def test_transient_dispatch_names_are_retried(self):
        for name in ('ConstantWidth', 'Elevation', 'Coordinates', 'AddLightWeightPolyline'):
            with self.subTest(name=name):
                read = Mock(side_effect=[AttributeError(f'<unknown>.{name}'), name])
                self.assertEqual(name, retry_document_read(read))
                self.assertEqual(2, read.call_count)

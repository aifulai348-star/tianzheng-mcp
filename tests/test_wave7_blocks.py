import math
import re
import unittest
from collections import Counter
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tz_mcp import cli, panel
from tz_mcp.cad import Cad, OutcomeUnknown, retry_document_read
from tz_mcp.commands import prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


LINE = {'kind': 'line', 'start': [1, 2], 'end': [4, 6]}
CIRCLE = {'kind': 'circle', 'center': [2, 3], 'radius': 5}
ARC = {**CIRCLE, 'kind': 'arc', 'start_angle_deg': 350, 'end_angle_deg': 10}
POLYLINE = {'kind': 'polyline', 'points': [[0, 0], [10, 0], [10, 8]], 'closed': True}
TEXT = {'kind': 'text', 'text': 'Door 标记', 'point': [3, 4], 'height': 2}
INSERT = {'name': 'door unit', 'point': [9, -1]}


class PreparationTests(unittest.TestCase):
    def test_capability_contracts(self):
        capabilities = cli.capabilities()
        for op, required, optional in (
            ('block.define', ['items', 'name'], ['base_point']),
            ('block.insert', ['name', 'point'], ['rotation_deg', 'scale_x', 'scale_y', 'scale_z']),
            ('block.list', [], ['include_system', 'prefix']),
        ):
            with self.subTest(op=op):
                self.assertEqual(required, capabilities[op]['required'])
                self.assertEqual(optional, capabilities[op]['optional'])
                self.assertTrue(capabilities[op]['validation'].startswith('verified_live'))
                self.assertIn(op, NO_ESCAPE_STEPS)

    def test_normalization_and_inclusive_limits(self):
        points = [[i, 0] for i in range(100)]
        polyline = {'kind': 'polyline', 'points': points}
        data = prepare('block.define', {'name': '  ' + 'A' * 255 + '  ', 'items': [polyline] * 50})
        self.assertEqual('A' * 255, data['name'])
        self.assertEqual([0.0, 0.0, 0.0], data['base_point'])
        self.assertEqual(50, len(data['items']))
        self.assertEqual([99.0, 0.0, 0.0], data['items'][-1]['points'][-1])
        self.assertIs(data['items'][0]['closed'], False)
        self.assertNotIn('closed', polyline)
        self.assertEqual([99, 0], points[-1])
        self.assertEqual({'name': 'door unit', 'point': [9.0, -1.0, 0.0], 'scale_x': 1.0,
                          'scale_y': 1.0, 'scale_z': 1.0, 'rotation_deg': 0.0},
                         prepare('block.insert', {**INSERT, 'name': ' door unit '}))
        self.assertEqual({'include_system': False}, prepare('block.list', {}))
        self.assertEqual({'prefix': '*MODEL', 'include_system': True},
                         prepare('block.list', {'prefix': ' *MODEL  ', 'include_system': True}))

    def test_invalid_block_names_and_arguments(self):
        for op, args in (('block.define', {'name': 'A', 'items': [LINE]}), ('block.insert', INSERT)):
            for name in ('', 5, 'x' * 256, '*Model_Space', '  *U1 ', 'a\n', 'a\x7f',
                         *(f'a{ch}b' for ch in '<>/"\\:;?*|=')):
                with self.subTest(op=op, name=name), self.assertRaises(ValueError):
                    prepare(op, {**args, 'name': name})
            with self.assertRaises(ValueError):
                prepare(op, {**args, 'extra': 1})
        for key in ('scale_x', 'scale_y', 'scale_z'):
            for value in (0, -1, True, '1', float('inf'), float('nan'), 10**400):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare('block.insert', {**INSERT, key: value})
        for key, value in (('point', [9, -1, 0.1]), ('point', [10**400, 0]),
                           ('rotation_deg', True), ('rotation_deg', float('nan'))):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                prepare('block.insert', {**INSERT, key: value})
        for key, values in (
            ('prefix', (' ', None, 1, 'a' * 256, 'a\t', 'a\x7f')),
            ('include_system', (1, 'false', None)),
        ):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare('block.list', {key: value})

    def test_invalid_definition_items_and_base_point(self):
        for items in ([], [LINE] * 51, tuple([LINE]), [None], [{}], [{'kind': []}],
                      [{'kind': 'hatch'}], [{'kind': 'line', 'start': [0, 0]}],
                      [{**LINE, 'extra': 1}], [{**LINE, 'end': LINE['start']}],
                      [{**LINE, 'end': [4, 6, 1]}], [{**CIRCLE, 'center': [1, 2, 1]}],
                      [{**CIRCLE, 'radius': 0}], [{**CIRCLE, 'radius': float('nan')}],
                      [{**ARC, 'end_angle_deg': -10}], [{**ARC, 'start_angle_deg': True}],
                      [{**ARC, 'radius': -1}], [{**POLYLINE, 'closed': 1}],
                      [{**POLYLINE, 'points': [[0, 0], [1, 1]]}],
                      [{**POLYLINE, 'points': [[0, 0], [1, 1], [0, 0]]}],
                      [{**POLYLINE, 'points': [[0, 0], [0, 0], [1, 1]]}],
                      [{**POLYLINE, 'points': [[0, 0, 1], [1, 0], [1, 1]]}],
                      [{**POLYLINE, 'points': [[i, 0] for i in range(101)]}],
                      [{**TEXT, 'text': 'bad\ntext'}], [{**TEXT, 'height': 0}],
                      [{**TEXT, 'point': [3, 4, 1]}]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                prepare('block.define', {'name': 'A', 'items': items})
        for base_point in (None, [0, 0, 1], [True, 0], [10**400, 0]):
            with self.subTest(base_point=base_point), self.assertRaises(ValueError):
                prepare('block.define', {'name': 'A', 'items': [LINE], 'base_point': base_point})


class BlockDefinition:
    def __init__(self, name, origin=(0.0, 0.0, 0.0), *, layout=False, xref=False):
        self.Name, self.Origin = name, origin
        self.IsLayout, self.IsXRef = layout, xref
        self.calls = []
        self.polyline = SimpleNamespace(Closed=False)

    @property
    def Count(self):
        return len(self.calls)

    def _add(self, method, args):
        self.calls.append((method, args))

    def AddLine(self, *args):
        self._add('line', args)

    def AddCircle(self, *args):
        self._add('circle', args)

    def AddArc(self, *args):
        self._add('arc', args)

    def AddLightWeightPolyline(self, *args):
        self._add('polyline', args)
        return self.polyline

    def AddText(self, *args):
        self._add('text', args)


class Blocks:
    def __init__(self):
        self.items = []
        self.Add = Mock(side_effect=self.add)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index]

    def add(self, origin, name):
        block = BlockDefinition(name.swapcase(), origin)
        self.items.append(block)
        return block


class FakeCad:
    execute = Cad.execute
    hwnd = 42
    document_id = 'draw-1'

    def __init__(self):
        self.ref = {}
        self.model = {'A': SimpleNamespace(Handle='A', ObjectName='AcDbLine')}
        self.guard = Mock()
        self.entities = Mock(side_effect=lambda: dict(self.model))
        self.insert = Mock(side_effect=self.insert_block)
        self.doc = SimpleNamespace(Blocks=Blocks(), ModelSpace=SimpleNamespace(InsertBlock=self.insert))
        self.dxf_dump = '0 INSERT\n5 B\n8 0\n2 Door Unit\n10 (9.0 -1.0 0.0)\n'
        self.run_command = Mock(side_effect=self.write_dxf)

    def insert_block(self, point, name, sx, sy, sz, rotation):
        entity = SimpleNamespace(Handle='b', ObjectName='AcDbBlockReference', Name=name.lower(),
                                 InsertionPoint=point, XScaleFactor=sx, YScaleFactor=sy,
                                 ZScaleFactor=sz, Rotation=rotation % math.tau)
        vars(entity).update(self.ref)
        self.model['B'] = entity
        return entity

    def describe(self, entity):
        return {'handle': entity.Handle.upper(), 'object_name': entity.ObjectName}

    def write_dxf(self, payload):
        path = re.search(r'\(open "([^"]+)"', payload)[1]
        Path(path).write_text(self.dxf_dump, encoding='utf-8-sig')


class BlockExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        self.blocks = self.cad.doc.Blocks
        folder = self.enterContext(TemporaryDirectory())
        self.window = self.enterContext(patch('tz_mcp.parts.require_drawing_window'))
        self.model_space = self.enterContext(patch('tz_mcp.parts.require_model_space'))
        self.enterContext(patch('tz_mcp.parts.late', side_effect=lambda obj: obj))
        self.enterContext(patch('tz_mcp.parts.vector', side_effect=tuple))
        self.enterContext(patch('tz_mcp.parts.block_ref_dxf',
                                partial(panel.block_ref_dxf, folder=folder)))

    def test_define_builds_all_primitives_inside_the_block(self):
        result = self.cad.execute('block.define', {'name': ' Door Unit ', 'base_point': [7, -4],
                                                   'items': [LINE, CIRCLE, ARC, POLYLINE, TEXT]})
        self.blocks.Add.assert_called_once_with((7.0, -4.0, 0.0), 'Door Unit')
        block, = self.blocks.items
        self.assertEqual([
            ('line', ((1., 2., 0.), (4., 6., 0.))),
            ('circle', ((2., 3., 0.), 5.)),
            ('arc', ((2., 3., 0.), 5., math.radians(350), math.radians(10))),
            ('polyline', ((0., 0., 10., 0., 10., 8.),)),
            ('text', ('Door 标记', (3., 4., 0.), 2.)),
        ], block.calls)
        self.assertIs(block.polyline.Closed, True)
        self.assertEqual({'A'}, set(self.cad.model))
        self.assertEqual({'success': True, 'document_id': 'draw-1', 'handles': [], 'entities': [],
                          'evidence': {'block': {'name': 'dOOR uNIT', 'base_point': [7., -4., 0.],
                                                 'item_count': 5,
                                                 'kinds': ['line', 'circle', 'arc', 'polyline', 'text']}},
                          'validation': {'type': True, 'properties': True, 'reopen': 'pending'}}, result)
        self.cad.run_command.assert_not_called()

    def test_invalid_descriptor_is_rejected_before_cad_access(self):
        with self.assertRaises(ValueError):
            self.cad.execute('block.define', {'name': 'Door Unit', 'items': [LINE, {'kind': 'line'}]})
        self.cad.guard.assert_not_called()
        self.cad.entities.assert_not_called()
        self.blocks.Add.assert_not_called()

    def test_duplicate_definition_and_missing_insert_leave_drawing_untouched(self):
        self.blocks.items.append(BlockDefinition('Door Unit'))
        with self.assertRaisesRegex(ValueError, 'Block already exists: door unit'):
            self.cad.execute('block.define', {'name': 'door unit', 'items': [LINE]})
        with self.assertRaisesRegex(ValueError, 'Block does not exist: Window'):
            self.cad.execute('block.insert', {**INSERT, 'name': 'Window'})
        self.blocks.Add.assert_not_called()
        self.cad.insert.assert_not_called()
        self.cad.run_command.assert_not_called()

    def test_definition_readback_mismatch_is_uncertain(self):
        for field, message in (('handles', 'ModelSpace'), ('missing', 'missing after creation'),
                               ('count', 'Count'), ('origin', 'Origin')):
            def add(origin, name):
                block = self.blocks.add(origin, name)
                if field == 'handles':
                    self.cad.model.pop('A')
                elif field == 'missing':
                    self.blocks.items.remove(block)
                elif field == 'count':
                    block.calls.append(('unexpected entity', ()))
                elif field == 'origin':
                    block.Origin = (*origin[:2], 1.0)
                return block

            with self.subTest(field=field), self.assertRaisesRegex(OutcomeUnknown, message):
                self.blocks.Add.side_effect = add
                self.cad.execute('block.define', {'name': field, 'items': [LINE]})

    def test_insert_verifies_nonuniform_scales_and_wrapped_rotation(self):
        self.blocks.items.append(BlockDefinition('Door Unit'))
        self.cad.ref.update(Name='*U1', EffectiveName='Door Unit')
        self.cad.dxf_dump += f'41 2.0\n42 0.5\n43 3.0\n50 {math.radians(330)}\n'
        result = self.cad.execute('block.insert', {**INSERT, 'scale_x': 2, 'scale_y': 0.5,
                                                   'scale_z': 3, 'rotation_deg': -30})
        self.cad.insert.assert_called_once_with((9., -1., 0.), 'Door Unit', 2., 0.5, 3.,
                                               math.radians(-30))
        self.assertEqual(['B'], result['handles'])
        self.assertEqual([{'handle': 'B', 'object_name': 'AcDbBlockReference'}], result['entities'])
        evidence = result['evidence']['block_insert']
        self.assertEqual('Door Unit', evidence['name'])
        self.assertEqual([9., -1., 0.], evidence['insertion'])
        self.assertEqual((2., 0.5, 3.), tuple(evidence[key] for key in ('scale_x', 'scale_y', 'scale_z')))
        self.assertAlmostEqual(math.radians(330), evidence['rotation'])
        self.assertEqual('verified_2_10_41_42_43_50', result['validation']['dxf'])
        self.assertEqual('pending', result['validation']['reopen'])
        payload = self.cad.run_command.call_args.args[0]
        self.assertIn('(entget (handent "B"))', payload)
        self.assertIn('(0 5 8 2 10 41 42 43 50)', payload)

    def test_insert_defaults_for_omitted_dxf_groups(self):
        self.blocks.items.append(BlockDefinition('Door Unit'))
        result = self.cad.execute('block.insert', INSERT)
        dxf = result['evidence']['block_insert']['dxf']
        self.assertEqual({'type': 'INSERT', 'handle': 'B', 'layer': '0', 'name': 'Door Unit',
                          'insertion': (9., -1., 0.), 'scale_x': 1., 'scale_y': 1., 'scale_z': 1.,
                          'rotation': 0.}, dxf)

    def test_insert_com_mismatch_stops_before_dxf_read(self):
        for properties, message in (
            ({'Handle': 'C'}, 'handle changes'), ({'ObjectName': 'AcDbLine'}, 'COM type'),
            ({'Name': 'Other', 'EffectiveName': 'Other'}, 'COM block reference name'),
            ({'InsertionPoint': (9., -1., 0.02)}, 'COM InsertionPoint'),
            ({'XScaleFactor': 1.001}, 'COM XScaleFactor'), ({'YScaleFactor': 1.001}, 'COM YScaleFactor'),
            ({'ZScaleFactor': 1.001}, 'COM ZScaleFactor'), ({'Rotation': 0.001}, 'COM Rotation'),
        ):
            with self.subTest(properties=properties), self.assertRaisesRegex(OutcomeUnknown, message):
                self.cad.model.pop('B', None)
                self.blocks.items[:] = [BlockDefinition('Door Unit')]
                self.cad.ref = properties
                self.cad.execute('block.insert', INSERT)
        self.cad.run_command.assert_not_called()

    def test_insert_rejects_removed_modelspace_handle(self):
        self.blocks.items.append(BlockDefinition('Door Unit'))

        def insert(*args):
            self.cad.model.pop('A')
            return self.cad.insert_block(*args)

        self.cad.insert.side_effect = insert
        with self.assertRaisesRegex(OutcomeUnknown, 'handle changes'):
            self.cad.execute('block.insert', INSERT)

    def test_insert_dxf_mismatch_is_uncertain(self):
        good = self.cad.dxf_dump
        self.blocks.items.append(BlockDefinition('Door Unit'))
        for dxf, message in (
            (good.replace('INSERT', 'TEXT'), 'DXF type'),
            (good.replace('2 Door Unit\n', '2 Other\n'), 'DXF 2'),
            (good.replace('10 (9.0 -1.0 0.0)', '10 (9.0 -1.0 0.6)'), 'DXF 10'),
            (good + '41 0.0\n', 'DXF 41'), (good + '42 1.002\n', 'DXF 42'),
            (good + '43 nan\n', 'DXF 43'), (good + '50 0.002\n', 'DXF 50'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(OutcomeUnknown, message):
                self.cad.model.pop('B', None)
                self.cad.dxf_dump = dxf
                self.cad.execute('block.insert', INSERT)

    def test_list_is_read_only_and_filters_system_names_and_layouts(self):
        xref = BlockDefinition('Door Xref', xref=True)
        del xref.Origin
        self.blocks.items.extend([BlockDefinition('other'), BlockDefinition('*U1'), xref,
                                  BlockDefinition('Door Body', (1., 2., 0.)),
                                  BlockDefinition('Door Layout', layout=True)])
        self.blocks.items[3].AddLine((0., 0., 0.), (1., 1., 0.))
        result = self.cad.execute('block.list', {'prefix': ' doOR '})
        self.assertEqual({'success': True, 'document_id': 'draw-1', 'handles': [], 'entities': [],
                          'evidence': {'blocks': [
                              {'name': 'Door Body', 'is_layout': False, 'is_xref': False,
                               'item_count': 1, 'base_point': [1., 2., 0.]},
                              {'name': 'Door Xref', 'is_layout': False, 'is_xref': True,
                               'item_count': 0, 'base_point': [0., 0., 0.]},
                          ], 'count': 2, 'prefix': 'doOR', 'include_system': False},
                          'validation': {'type': True, 'properties': True, 'reopen': 'not_applicable'}},
                         result)
        self.cad.guard.assert_called_once_with()
        self.window.assert_called_once_with(self.cad.hwnd)
        self.model_space.assert_called_once_with(self.cad.doc)
        evidence = self.cad.execute('block.list', {'include_system': True})['evidence']
        self.assertEqual(['*U1', 'Door Body', 'Door Layout', 'Door Xref', 'other'],
                         [item['name'] for item in evidence['blocks']])
        self.assertEqual(5, evidence['count'])
        self.assertIs(evidence['blocks'][2]['is_layout'], True)
        self.assertIsNone(evidence['prefix'])
        self.cad.entities.assert_not_called()
        self.blocks.Add.assert_not_called()
        self.cad.insert.assert_not_called()
        self.cad.run_command.assert_not_called()


class RetryTests(unittest.TestCase):
    def test_late_bound_block_members_retry_metadata_lookup(self):
        attempts = Counter()

        def get(name):
            attempts[name] += 1
            if attempts[name] == 1:
                raise AttributeError('<unknown>.' + name)
            return name

        with patch('tz_mcp.cad.time.sleep'):
            for name in ('Blocks', 'InsertBlock', 'EffectiveName', 'XScaleFactor', 'YScaleFactor',
                         'ZScaleFactor', 'IsLayout', 'IsXRef', 'Origin', 'AddText'):
                with self.subTest(name=name):
                    self.assertEqual(name, retry_document_read(partial(get, name)))
                    self.assertEqual(2, attempts[name])


if __name__ == '__main__':
    unittest.main()

import re
import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, CommandCompletionTimeout, OutcomeUnknown
from tz_mcp.commands import SCHEMAS, command_payload, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


ANGULAR = 'dim.t20_angular'
PICKS = {'first_pick': (1, -2), 'second_pick': [3.25, 4, 0], 'arc_point': [8, 9]}
PLAN_PICKS = {'first_pick': [1., -2., 0.], 'second_pick': [3.25, 4., 0.],
              'arc_point': [8., 9., 0.]}
DIM_COMMAND = 'TDimAng\n1.0,-2.0\n3.25,4.0\n8.0,9.0\n\n'
DISABLE_SNAPS = '(setvar "OSMODE" 0)\n'
DIM_DXF = '0 TCH_DIMENSION2\n5 C\n8 PuB_DiM\n68 1\n'


def entity(kind, layer='0', **fields):
    return SimpleNamespace(ObjectName=kind, Layer=layer, **fields)


class PrepareTests(unittest.TestCase):
    def test_capability_contracts(self):
        capabilities = cli.capabilities()
        self.assertEqual(71, len(capabilities))
        for op, args in ((ANGULAR, PICKS), ('layer.delete', {'name': 'Temp Layer'}),
                         ('block.delete', {'name': 'Temp Block'})):
            with self.subTest(op=op):
                self.assertEqual((set(args), set()), SCHEMAS[op])
                self.assertEqual(sorted(args), capabilities[op]['required'])
                self.assertEqual([], capabilities[op]['optional'])
                self.assertTrue(capabilities[op]['validation'].startswith('verified_live'))
                self.assertEqual(op != ANGULAR, op in NO_ESCAPE_STEPS)
                with self.assertRaises(ValueError):
                    prepare(op, {})
                with self.assertRaises(ValueError):
                    prepare(op, {**args, 'extra': True})

    def test_plan_points_and_exact_native_payload(self):
        prepared = prepare(ANGULAR, PICKS)
        self.assertEqual(PLAN_PICKS, prepared)
        self.assertEqual(DIM_COMMAND, command_payload(ANGULAR, prepared))

    def test_rejects_invalid_plan_coordinates_in_every_field(self):
        for field in PICKS:
            for value in ([1, 2, 1e-12], [1, float('nan')], [float('inf'), 2],
                          [True, 2], ['1', 2], [10**400, 2], [1], [1, 2, 0, 0]):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    prepare(ANGULAR, {**PICKS, field: value})

    def test_pick_separation_must_exceed_tolerance(self):
        for second in ([0, 0], [1e-6, 0], [5e-7, 5e-7]):
            with self.subTest(second=second), self.assertRaisesRegex(
                    ValueError, 'requires distinct first_pick and second_pick points'):
                prepare(ANGULAR, {**PICKS, 'first_pick': [0, 0], 'second_pick': second})
        self.assertEqual([1.01e-6, 0., 0.], prepare(
            ANGULAR, {**PICKS, 'first_pick': [0, 0], 'second_pick': [1.01e-6, 0]})['second_pick'])

    def test_delete_names_follow_symbol_validation(self):
        for op in ('layer.delete', 'block.delete'):
            with self.subTest(op=op):
                self.assertEqual({'name': 'Temp Name'}, prepare(op, {'name': ' Temp Name '}))
                for invalid in ('', 'a|b', 'a\n', 'x' * 256, 123):
                    with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                        prepare(op, {'name': invalid})
        with self.assertRaisesRegex(ValueError, r'Block name must not start with \*'):
            prepare('block.delete', {'name': ' *U1'})


class Collection:
    def __init__(self):
        self.items = []

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index]

    def add(self, name, **properties):
        item = SimpleNamespace(Name=name, **properties)
        item.Delete = Mock(side_effect=lambda: self.items.remove(item))
        self.items.append(item)
        return item


class FakeCad:
    execute = Cad.execute
    hwnd = 42
    document_id = 'owned-drawing'

    def __init__(self):
        self.guard = Mock()
        self.model = {'A': entity('AcDbLine'), 'B': entity('TDbWall', 'WALL')}
        self.entities = Mock(side_effect=lambda: dict(self.model))
        self.layers = Collection()
        self.blocks = Collection()
        self.osmode = 16383
        self.doc = SimpleNamespace(Layers=self.layers, Blocks=self.blocks, ActiveLayer=self.layers.add('0'),
                                   GetVariable=Mock(side_effect=lambda _: self.osmode),
                                   SetVariable=Mock(side_effect=self.set_variable))
        self.run_command = Mock(side_effect=self.run)
        self.on_dimension = Mock(side_effect=self.add_dimension)
        self.dimension_osmodes = []
        self.dimension = entity('TDbDimension2', 'pub_dim', Handle='C')
        self.dump = DIM_DXF

    def set_variable(self, name, value):
        if name != 'OSMODE':
            raise AssertionError(name)
        self.osmode = value

    def add_dimension(self):
        self.model['C'] = self.dimension

    def run(self, payload):
        if payload == DISABLE_SNAPS:
            self.osmode = 0
        elif payload == DIM_COMMAND:
            self.dimension_osmodes.append(self.osmode)
            self.on_dimension()
        else:
            path = re.search(r'\(open "([^"]+)"', payload)[1]
            Path(path).write_text(self.dump, encoding='utf-8-sig')

    def describe(self, obj):
        return {'handle': obj.Handle, 'object_name': obj.ObjectName}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'require_model_space'))
        self.enterContext(patch.object(parts, 'late', side_effect=lambda obj: obj))

    def test_invalid_request_never_accesses_cad(self):
        for op, args in ((ANGULAR, {}),
                         ('layer.delete', {'name': 'bad|layer'}),
                         ('block.delete', {'name': '*Model_Space'})):
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.cad.execute(op, args)
        self.cad.guard.assert_not_called()
        self.cad.entities.assert_not_called()
        self.cad.run_command.assert_not_called()

    def test_native_dimension_verifies_com_and_reads_all_dxf_groups(self):
        folder = self.enterContext(TemporaryDirectory())
        with patch.object(parts, 't20_dim_dxf', partial(panel.t20_dim_dxf, folder=folder)):
            result = self.cad.execute(ANGULAR, PICKS)
        self.assertEqual({
            'success': True, 'document_id': 'owned-drawing', 'handles': ['C'],
            'entities': [{'handle': 'C', 'object_name': 'TDbDimension2'}],
            'evidence': {'t20_angular_dim': {'command': 'TDimAng', **PLAN_PICKS, 'layer': 'pub_dim',
                                            'dxf': {'type': 'TCH_DIMENSION2', 'handle': 'C',
                                                    'layer': 'PuB_DiM', 'group_68': 1}}},
            'validation': {'type': True, 'properties': True, 'dxf': 'verified_0_8',
                           'reopen': 'pending'},
        }, result)
        first, native, dxf = self.cad.run_command.call_args_list
        self.assertEqual(call(DISABLE_SNAPS), first)
        self.assertEqual(call(DIM_COMMAND), native)
        self.assertIn('(entget (handent "C"))', dxf.args[0])
        self.assertIn('(0 5 8 68)', dxf.args[0])
        self.assertEqual([0], self.cad.dimension_osmodes)
        self.cad.doc.SetVariable.assert_called_once_with('OSMODE', 16383)
        self.assertEqual({'A', 'B', 'C'}, set(self.cad.model))

    def test_dimension_rejects_insufficient_eligible_entities_before_any_write(self):
        self.cad.model['B'] = entity('AcDbCircle')
        self.cad.model['C'] = entity('AcDbBlockReference')
        with self.assertRaisesRegex(ValueError, 'at least two line or wall entities'):
            self.cad.execute(ANGULAR, PICKS)
        self.cad.doc.GetVariable.assert_not_called()
        self.cad.doc.SetVariable.assert_not_called()
        self.cad.run_command.assert_not_called()

    def test_dimension_reports_unexpected_handle_changes_as_uncertain(self):
        original = dict(self.cad.model)
        for model in (original, {**original, 'C': self.cad.dimension, 'D': self.cad.dimension},
                      {'B': original['B'], 'C': self.cad.dimension}):
            with self.subTest(handles=list(model)), self.assertRaisesRegex(OutcomeUnknown, 'TDimAng'):
                self.cad.model = dict(original)
                self.cad.on_dimension.side_effect = partial(setattr, self.cad, 'model', dict(model))
                self.cad.execute(ANGULAR, PICKS)
            self.assertEqual(16383, self.cad.osmode)

    def test_native_dimension_reports_wrong_com_class_or_layer_before_dxf(self):
        for kind, layer in (('AcDbLine', 'PUB_DIM'), ('TDbDimension2', '0')):
            with self.subTest(kind=kind, layer=layer), patch.object(parts, 't20_dim_dxf') as dxf:
                self.cad.model.pop('C', None)
                self.cad.dimension = entity(kind, layer)
                with self.assertRaisesRegex(OutcomeUnknown, 'unexpected COM entity'):
                    self.cad.execute(ANGULAR, PICKS)
                dxf.assert_not_called()

    def test_native_dimension_reports_wrong_or_missing_dxf_type_or_layer(self):
        folder = self.enterContext(TemporaryDirectory())
        with patch.object(parts, 't20_dim_dxf', partial(panel.t20_dim_dxf, folder=folder)):
            for dump in (DIM_DXF.replace('TCH_DIMENSION2', 'LINE'), DIM_DXF.replace('PuB_DiM', '0'),
                         DIM_DXF.replace('8 PuB_DiM\n', '')):
                with self.subTest(dump=dump), self.assertRaisesRegex(OutcomeUnknown, 'unexpected DXF'):
                    self.cad.model.pop('C', None)
                    self.cad.dump = dump
                    self.cad.execute(ANGULAR, PICKS)

    def test_native_timeout_restores_snaps_and_remains_uncertain(self):
        self.cad.on_dimension.side_effect = CommandCompletionTimeout('TDimAng did not finish')
        with self.assertRaisesRegex(CommandCompletionTimeout, 'did not finish'):
            self.cad.execute(ANGULAR, PICKS)
        self.cad.doc.SetVariable.assert_called_once_with('OSMODE', 16383)

    def test_deletes_use_com_and_return_canonical_names(self):
        cases = (('layer.delete', self.cad.layers, {}),
                 ('block.delete', self.cad.blocks, {'IsLayout': False, 'IsXRef': False}))
        self.cad.model['D'] = entity('AcDbBlockReference', Name='Unrelated')
        for op, collection, props in cases:
            with self.subTest(op=op):
                target = collection.add('Temp Name', **props)
                result = self.cad.execute(op, {'name': ' temp name '})
                target.Delete.assert_called_once_with()
                self.assertNotIn(target, collection.items)
                self.assertEqual({'success': True, 'document_id': 'owned-drawing',
                                  'handles': [], 'entities': [],
                                  'evidence': {op.replace('.', '_'): {'name': 'Temp Name',
                                                                      'deleted': True}},
                                  'validation': {'type': True, 'deleted': True,
                                                 'reopen': 'pending'}}, result)
                self.assertEqual({'A', 'B', 'D'}, set(self.cad.model))
        self.cad.run_command.assert_not_called()
        self.cad.doc.SetVariable.assert_not_called()

    def test_layer_guards_precede_delete(self):
        defpoints = self.cad.layers.add('Defpoints')
        active = self.cad.layers.add('Current')
        occupied = self.cad.layers.add('Wall')
        self.cad.doc.ActiveLayer = SimpleNamespace(Name='CURRENT')
        for name, message in (
            ('0', 'Cannot delete protected layer: 0'),
            ('deFPOINTS', 'Cannot delete protected layer: deFPOINTS'),
            ('Missing', 'Layer does not exist: Missing'),
            ('current', 'Cannot delete active layer: Current'),
            ('wall', 'Cannot delete layer containing ModelSpace entities: Wall'),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                self.cad.execute('layer.delete', {'name': name})
        for item in (self.cad.layers.Item(0), defpoints, active, occupied):
            item.Delete.assert_not_called()
        self.cad.run_command.assert_not_called()

    def test_block_guards_precede_delete(self):
        layout = self.cad.blocks.add('Layout', IsLayout=True, IsXRef=False)
        xref = self.cad.blocks.add('Xref', IsLayout=False, IsXRef=True)
        static = self.cad.blocks.add('Static', IsLayout=False, IsXRef=False)
        dynamic = self.cad.blocks.add('Dynamic', IsLayout=False, IsXRef=False)
        self.cad.model['D'] = entity('AcDbBlockReference', Name='STATIC')
        self.cad.model['E'] = entity('AcDbBlockReference', Name='*U1', EffectiveName='DYNAMIC')
        for name, message in (
            ('Missing', 'Block does not exist: Missing'),
            ('layout', 'Cannot delete layout or xref block: Layout'),
            ('xref', 'Cannot delete layout or xref block: Xref'),
            ('static', 'Cannot delete block referenced in ModelSpace: Static'),
            ('dynamic', 'Cannot delete block referenced in ModelSpace: Dynamic'),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                self.cad.execute('block.delete', {'name': name})
        for item in (layout, xref, static, dynamic):
            item.Delete.assert_not_called()
        self.cad.run_command.assert_not_called()

    def test_block_guard_does_not_ignore_an_unreadable_effective_name(self):
        class UnreadableReference:
            ObjectName = 'AcDbBlockReference'
            Name = '*U1'
            _oleobj_ = SimpleNamespace(GetIDsOfNames=Mock(return_value=1))

            @property
            def EffectiveName(self):
                raise AttributeError('<unknown>.EffectiveName')

        target = self.cad.blocks.add('Target', IsLayout=False, IsXRef=False)
        self.cad.model['D'] = UnreadableReference()
        with self.assertRaisesRegex(AttributeError, 'EffectiveName'):
            self.cad.execute('block.delete', {'name': 'Target'})
        target.Delete.assert_not_called()

    def test_delete_readback_mismatches_and_com_failure_are_uncertain(self):
        cases = (('layer.delete', self.cad.layers, {}),
                 ('block.delete', self.cad.blocks, {'IsLayout': False, 'IsXRef': False}))
        for op, collection, props in cases:
            with self.subTest(op=op):
                item = collection.add('Target', **props)
                item.Delete.side_effect = None
                with self.assertRaisesRegex(OutcomeUnknown, 'still exists after deletion'):
                    self.cad.execute(op, {'name': 'Target'})
                item.Delete.side_effect = RuntimeError('COM rejected delete')
                with self.assertRaisesRegex(OutcomeUnknown, 'COM rejected delete'):
                    self.cad.execute(op, {'name': 'Target'})

                def delete_and_remove_entity():
                    collection.items.remove(item)
                    self.cad.model.pop('A')

                item.Delete.side_effect = delete_and_remove_entity
                with self.assertRaisesRegex(OutcomeUnknown, 'ModelSpace handles changed'):
                    self.cad.execute(op, {'name': 'Target'})
                self.assertEqual(3, item.Delete.call_count)
                self.cad.model['A'] = entity('AcDbLine')
        self.cad.run_command.assert_not_called()

    def test_delete_retries_a_transient_com_member_lookup(self):
        owner = self.cad.blocks

        class BusyBlock:
            Name = 'Temp'
            IsLayout = False
            IsXRef = False
            lookup = Mock(side_effect=[AttributeError('<unknown>.Delete'), None])
            delete = Mock(side_effect=lambda: owner.items.remove(block))

            @property
            def Delete(self):
                self.lookup()
                return self.delete

        block = BusyBlock()
        owner.items.append(block)
        with patch('tz_mcp.cad.time.sleep') as sleep:
            self.cad.execute('block.delete', {'name': block.Name})
        self.assertEqual(2, block.lookup.call_count)
        block.delete.assert_called_once_with()
        sleep.assert_called_once()


if __name__ == '__main__':
    unittest.main()

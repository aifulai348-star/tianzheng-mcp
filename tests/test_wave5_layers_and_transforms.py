import math
import re
import sys
import unittest
from functools import partial
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from test_entity_transform import CASES, TEXT
from test_primitives_and_layers import CallRejected, Document, FakeCad as BaseCad, Properties, write_entget
from tz_mcp import panel, parts
from tz_mcp.cad import Cad, OutcomeUnknown
from tz_mcp.cli import capabilities
from tz_mcp.commands import prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


REF = {'document_id': ' draw-1 ', 'handle': '0a3'}
SCALE = {**REF, 'base_point': [10, 20], 'scale_factor': 2}
MIRROR = {**REF, 'axis_start': [10, 20], 'axis_end': [14, 24, 0]}
SCALE_COMMAND = ('(progn (command "_.SCALE" (handent "A3") "" "_non" '
                 '(list 10.000000 20.000000 0.0) 2.000000) (princ))\n')
MIRROR_COMMAND = ('(progn (command "_.MIRROR" (handent "A3") "" "_non" (list 10.000000 20.000000 0.0) '
                  '"_non" (list 14.000000 24.000000 0.0) "{answer}") (princ))\n')
SCALED_DXF = {
    'AcDbLine': '0 LINE\n10 (14 22 0)\n11 (20 20 0)\n',
    'AcDbCircle': '0 CIRCLE\n10 (14 22 0)\n40 20\n',
    'AcDbArc': f'0 ARC\n10 (14 22 0)\n40 20\n50 {5.5 - math.tau}\n51 0.25\n',
    'AcDbText': f'0 TEXT\n10 (14 22 0)\n1 {TEXT}\n40 5\n50 {5.75 - math.tau}\n',
    'AcDbPolyline': '0 LWPOLYLINE\n70 129\n10 (14 22)\n10 (20 20)\n10 (12 26)\n',
}
MIRRORED_DXF = {
    'AcDbLine': '0 LINE\n10 (11 22 0)\n11 (10 25 0)\n',
    'AcDbCircle': '0 CIRCLE\n10 (11 22 0)\n40 10\n',
    'AcDbArc': f'0 ARC\n10 (11 22 0)\n40 10\n50 {math.pi / 2 - 0.25}\n51 {math.pi / 2 - 5.5}\n',
    'AcDbPolyline': '0 LWPOLYLINE\n70 1\n10 (11 22)\n10 (10 25)\n10 (13 21)\n',
}


class PreparationTests(unittest.TestCase):
    def test_registry_contracts(self):
        registry = capabilities()
        for op, required, optional, no_escape in (
            ('layer.list', [], ['prefix'], True),
            ('layer.edit', ['name'], ['color', 'freeze', 'layer_on', 'lock'], True),
            ('entity.scale', ['base_point', 'document_id', 'handle', 'scale_factor'], [], False),
            ('entity.mirror', ['axis_end', 'axis_start', 'document_id', 'handle'], ['erase_source'], False),
        ):
            with self.subTest(op=op):
                self.assertEqual(required, registry[op]['required'])
                self.assertEqual(optional, registry[op]['optional'])
                self.assertTrue(registry[op]['validation'].startswith('verified_live'))
                self.assertEqual(no_escape, op in NO_ESCAPE_STEPS)

    def test_normalization(self):
        self.assertEqual({'prefix': 'wa'}, prepare('layer.list', {'prefix': ' wa '}))
        self.assertEqual({'name': 'Walls', 'color': 255, 'lock': False},
                         prepare('layer.edit', {'name': ' Walls ', 'color': 255, 'lock': False}))
        self.assertEqual({'document_id': 'draw-1', 'handle': '0A3', 'base_point': [10., 20., 0.],
                          'scale_factor': 2.}, prepare('entity.scale', SCALE))
        self.assertIs(type(prepare('entity.scale', SCALE)['scale_factor']), float)
        self.assertEqual({'document_id': 'draw-1', 'handle': '0A3', 'axis_start': [10., 20., 0.],
                          'axis_end': [14., 24., 0.], 'erase_source': False},
                         prepare('entity.mirror', MIRROR))

    def test_rejects_invalid_layer_values(self):
        for prefix in (None, 1, '', '  ', 'x' * 256, 'a\x00b', '\tname', 'a\x7f'):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                prepare('layer.list', {'prefix': prefix})
        with self.assertRaisesRegex(ValueError, 'requires at least one property to change'):
            prepare('layer.edit', {'name': 'Walls'})
        for key, values in (
            ('name', (None, ' ', 'x' * 256, 'a\x7f', 'a\x00', 'a/b', 'a:b')),
            ('color', (True, False, 0, 256, 1.0, '22')),
            *((key, (0, 1, 'true', None)) for key in ('layer_on', 'freeze', 'lock')),
        ):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare('layer.edit', {'name': 'Walls', 'color': 10, key: value})

    def test_rejects_invalid_transform_values(self):
        for op, args, cases in (
            ('entity.scale', SCALE, {
                'scale_factor': (True, '2', 0, -1, 1, 1 + 0.5e-6, 1 - 0.5e-6, math.nan, math.inf, 10**400),
                'base_point': ([10, 20, 1], [1], [False, 2], [math.inf, 0], [10**400, 0]),
            }),
            ('entity.mirror', MIRROR, {
                'axis_start': ([10, 20, 1], [True, 0], [math.nan, 1]),
                'axis_end': ([10, 20], [10 + 0.5e-6, 20], [14, 24, 1], [10**400, 0]),
                'erase_source': (None, 0, 1, 'false'),
            }),
        ):
            for key, values in {**cases, 'document_id': (None, ' '), 'handle': (1, '0xa3', 'G')}.items():
                for value in values:
                    with self.subTest(op=op, key=key, value=value), self.assertRaises(ValueError):
                        prepare(op, {**args, key: value})


class LayerDocument(Document):
    @property
    def ActiveLayer(self):
        self.cad.calls.call('ActiveLayer')
        return self.active_layer


class LayerTests(unittest.TestCase):
    def setUp(self):
        self.cad = BaseCad()
        self.cad.doc = LayerDocument(self.cad)
        collection = self.cad.doc.layers
        collection.layers = [Properties(self.cad.calls, Name=name, Color=color, LayerOn=on,
                                        Freeze=freeze, Lock=lock, Linetype=linetype)
                             for name, color, on, freeze, lock, linetype in (
                                 ('Walls-z', 22, False, True, False, 'Dashed'),
                                 ('Defpoints', 7, True, False, False, 'Continuous'),
                                 ('Walls-A', 7, True, False, True, 'Continuous'),
                                 ('0', 7, True, False, False, 'Continuous'),
                             )]
        self.target = collection.layers[0]
        self.cad.doc.active_layer = SimpleNamespace(Name='walls-a')
        self.window = self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'late', side_effect=lambda value: value))
        self.enterContext(patch('tz_mcp.cad.time.sleep'))
        self.enterContext(patch.dict(sys.modules, pythoncom=SimpleNamespace(DISPATCH_PROPERTYPUT=4)))
        self.enterContext(patch.object(self.cad, 'guard'))

    def execute(self, op, **args):
        return Cad.execute(self.cad, op, args)

    def test_list_filters_sorts_and_uses_retry_bus_without_entity_reads(self):
        self.cad.calls.failures = {'Layers': CallRejected(), 'Count': CallRejected(), 'Item': CallRejected(),
                                   'ActiveLayer': AttributeError('<unknown>.ActiveLayer'),
                                   'Freeze': CallRejected(), 'LayerOn': CallRejected(),
                                   'Lock': CallRejected(), 'Linetype': CallRejected()}
        with patch.object(self.cad, 'entities', side_effect=AssertionError('layer.list is read-only')):
            result = self.execute('layer.list', prefix=' WALls- ')
        self.assertEqual({
            'success': True, 'document_id': 'draw-1', 'handles': [], 'entities': [],
            'evidence': {'layers': {'count': 2, 'active_layer': 'walls-a', 'prefix': 'WALls-', 'items': [
                {'name': 'Walls-A', 'color': 7, 'layer_on': True, 'freeze': False, 'lock': True,
                 'linetype': 'Continuous', 'active': True},
                {'name': 'Walls-z', 'color': 22, 'layer_on': False, 'freeze': True, 'lock': False,
                 'linetype': 'Dashed', 'active': False},
            ]}}, 'validation': {'type': True, 'properties': True, 'reopen': 'not_applicable'},
        }, result)
        self.cad.guard.assert_called_once_with()
        self.window.assert_called_once_with(self.cad.hwnd)
        self.assertEqual({}, self.cad.calls.failures)
        self.assertEqual([], self.cad.commands)
        self.assertFalse(any(layer.writes for layer in self.cad.doc.layers.layers))

    def test_list_all_and_empty_matches_with_unavailable_active_layer(self):
        self.cad.calls.failures['ActiveLayer'] = RuntimeError('unavailable')
        rows = self.execute('layer.list')['evidence']['layers']
        self.assertIsNone(rows['active_layer'])
        self.assertIsNone(rows['prefix'])
        self.assertEqual(['0', 'Defpoints', 'Walls-A', 'Walls-z'], [row['name'] for row in rows['items']])
        self.assertFalse(any(row['active'] for row in rows['items']))
        self.assertEqual({'count': 0, 'active_layer': 'walls-a', 'prefix': 'none', 'items': []},
                         self.execute('layer.list', prefix='none')['evidence']['layers'])

    def test_drawing_guards_stop_both_layer_operations(self):
        for op, args in (('layer.list', {}), ('layer.edit', {'name': 'Walls-z', 'color': 3})):
            with self.subTest(op=op):
                with patch.object(self.cad.doc, 'GetVariable', return_value=0):
                    with self.assertRaisesRegex(ValueError, 'paper space'):
                        self.execute(op, **args)
                self.cad.guard.side_effect = ValueError('unowned drawing')
                with self.assertRaisesRegex(ValueError, 'unowned drawing'):
                    self.execute(op, **args)
                self.cad.guard.side_effect = None
        self.assertEqual(0, self.cad.calls.attempts['Layers'])

    def test_edit_retries_write_and_verifies_all_properties_and_handles(self):
        self.cad.calls.failures = {'Color': CallRejected(), 'lookup:Color': CallRejected(),
                                   'put:Color': CallRejected(), 'put:Lock': CallRejected()}
        result = self.execute('layer.edit', name=' walls-Z ', color=42, layer_on=True, freeze=False, lock=True)
        before = {'Color': 22, 'LayerOn': False, 'Freeze': True, 'Lock': False}
        after = {'Color': 42, 'LayerOn': True, 'Freeze': False, 'Lock': True}
        self.assertEqual({
            'success': True, 'document_id': 'draw-1', 'handles': [], 'entities': [],
            'evidence': {'layer_edit': {'name': 'Walls-z', 'properties_before': before,
                                        'properties_requested': after, 'properties_after': after}},
            'validation': {'type': True, 'properties': True, 'reopen': 'pending'},
        }, result)
        self.assertEqual(list(after.items()), self.target.writes)
        self.assertEqual({}, self.cad.calls.failures)
        self.assertEqual({'A'}, set(self.cad.store))
        self.assertEqual([], self.cad.commands)

    def test_edit_refuses_to_hide_protected_or_active_layer_before_any_write(self):
        for name, reason in (('0', 'protected'), ('DEFPOINTS', 'protected'), ('WALLS-A', 'active')):
            for flag in ({'freeze': True}, {'layer_on': False}):
                with self.subTest(name=name, flag=flag), self.assertRaisesRegex(ValueError, f'{reason} layer'):
                    self.execute('layer.edit', name=name, color=42, **flag)
        with self.assertRaisesRegex(ValueError, 'Layer does not exist: Missing'):
            self.execute('layer.edit', name='Missing', lock=True)
        self.assertFalse(any(layer.writes for layer in self.cad.doc.layers.layers))
        self.assertEqual([], self.cad.doc.layers.adds)

    def test_edit_allows_color_and_lock_on_protected_or_active_layers(self):
        for name in ('0', 'defpoints', 'walls-a'):
            with self.subTest(name=name):
                evidence = self.execute('layer.edit', name=name, lock=True, color=3)['evidence']['layer_edit']
                self.assertEqual({'Lock': True, 'Color': 3}, evidence['properties_requested'])
                self.assertEqual({'Color': 3, 'LayerOn': True, 'Freeze': False, 'Lock': True},
                                 evidence['properties_after'])

    def test_edit_detects_requested_property_mismatch_and_unrequested_drift(self):
        with patch.object(self.target, 'Invoke'):
            with self.assertRaisesRegex(OutcomeUnknown, 'Color readback mismatch'):
                self.execute('layer.edit', name='Walls-z', color=42)
        original_put = self.target.Invoke

        def drift(*args):
            original_put(*args)
            self.target.values['Lock'] = True

        with patch.object(self.target, 'Invoke', side_effect=drift):
            with self.assertRaisesRegex(OutcomeUnknown, 'Lock readback mismatch'):
                self.execute('layer.edit', name='Walls-z', color=42)

    def test_edit_detects_handle_changes_and_post_write_read_failures(self):
        with patch.object(self.cad, 'entities', side_effect=[self.cad.store, {}]):
            with self.assertRaisesRegex(OutcomeUnknown, 'Entity handles changed during layer.edit'):
                self.execute('layer.edit', name='Walls-z', lock=True)
        with patch.object(self.cad, 'entities', side_effect=[self.cad.store, RuntimeError('lost drawing')]):
            with self.assertRaisesRegex(OutcomeUnknown, 'lost drawing'):
                self.execute('layer.edit', name='Walls-z', color=4)
        self.assertEqual([('Lock', True), ('Color', 4)], self.target.writes)


class TransformCad(BaseCad):
    def __init__(self, object_name, result_dxf, result_handle='A3', keep_source=False):
        super().__init__()
        self.source = SimpleNamespace(Handle='A3', ObjectName=object_name)
        self.store['A3'] = self.source
        self.dxf = {'A3': CASES.get(object_name, ('',))[0]}
        self.result_dxf = result_dxf
        self.result_handle = result_handle
        self.keep_source = keep_source
        self.dumps = []
        self.after_write = lambda: None

    def run_command(self, command):
        match = re.search(r'\(entget \(handent "([0-9A-F]+)"\)\)', command)
        if match:
            handle = match[1]
            self.dumps.append(handle)
            write_entget(command, handle, self.dxf[handle].encode('utf-8-sig'))
            return
        self.commands.append(command)
        if self.result_handle != 'A3':
            self.store[self.result_handle] = SimpleNamespace(Handle=self.result_handle,
                                                             ObjectName=self.source.ObjectName)
            if not self.keep_source:
                del self.store['A3']
        self.dxf[self.result_handle] = self.result_dxf
        self.after_write()


class TransformTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'transform_dxf', partial(panel.transform_dxf, folder=folder)))

    def test_scale_all_supported_types(self):
        for name, scaled in SCALED_DXF.items():
            with self.subTest(name=name):
                cad = TransformCad(name, scaled)
                result = Cad.execute(cad, 'entity.scale', SCALE)
                self.assertEqual(['A3'], result['handles'])
                self.assertEqual([{'handle': 'A3', 'object_name': name}], result['entities'])
                self.assertEqual({'type': True, 'properties': True, 'geometry': 'verified_dxf',
                                  'reopen': 'pending'}, result['validation'])
                evidence = result['evidence']['scale']
                self.assertEqual(('A3', name, [10., 20., 0.], 2.),
                                 (evidence['handle'], evidence['object_name'],
                                  evidence['base_point'], evidence['scale_factor']))
                self.assertEqual(CASES[name][3], evidence['dxf_before'])
                self.assertEqual(evidence['dxf_before']['type'], evidence['dxf_after']['type'])
                self.assertEqual([SCALE_COMMAND], cad.commands)
                self.assertEqual(['A3', 'A3'], cad.dumps)

    def test_mirror_all_supported_types_in_each_handle_mode(self):
        for name, reflected in MIRRORED_DXF.items():
            for erase, handle in ((False, 'A5'), (True, 'A3'), (True, 'A5')):
                with self.subTest(name=name, erase=erase, handle=handle):
                    cad = TransformCad(name, reflected, handle, keep_source=not erase)
                    args = {**MIRROR, 'erase_source': True} if erase else MIRROR
                    result = Cad.execute(cad, 'entity.mirror', args)
                    self.assertEqual([handle], result['handles'])
                    self.assertEqual([{'handle': handle, 'object_name': name}], result['entities'])
                    self.assertEqual('verified_dxf', result['validation']['geometry'])
                    evidence = result['evidence']['mirror']
                    self.assertEqual(('A3', handle, erase, name, [10., 20., 0.], [14., 24., 0.]),
                                     (evidence['source_handle'], evidence['result_handle'], evidence['erase_source'],
                                      evidence['object_name'], evidence['axis_start'], evidence['axis_end']))
                    self.assertEqual(CASES[name][3], evidence['dxf_before'])
                    self.assertEqual([MIRROR_COMMAND.format(answer='Y' if erase else 'N')], cad.commands)
                    self.assertEqual(['A3', handle] if erase else ['A3', 'A3', handle], cad.dumps)

    def test_rejects_unsupported_types_and_references_before_dump_or_write(self):
        for op, args, name, overrides, message in (
            ('entity.scale', SCALE, 'TDbWall', {}, 'does not support ObjectName TDbWall'),
            ('entity.scale', SCALE, 'AcDbMText', {}, 'does not support ObjectName AcDbMText'),
            ('entity.mirror', MIRROR, 'TDbOpening', {}, 'does not support ObjectName TDbOpening'),
            ('entity.mirror', MIRROR, 'AcDbText', {}, 'does not support ObjectName AcDbText'),
            ('entity.scale', SCALE, 'AcDbLine', {'document_id': 'other'}, 'document_id mismatch'),
            ('entity.mirror', MIRROR, 'AcDbLine', {'handle': 'F1'}, 'Unknown handle'),
        ):
            with self.subTest(op=op, name=name, overrides=overrides):
                cad = TransformCad(name, '')
                with self.assertRaisesRegex(ValueError, message):
                    Cad.execute(cad, op, {**args, **overrides})
                self.assertEqual([], cad.commands)
                self.assertEqual([], cad.dumps)

    def test_invalid_dxf_baseline_refuses_to_write(self):
        for op, args in (('entity.scale', SCALE), ('entity.mirror', MIRROR)):
            with self.subTest(op=op):
                cad = TransformCad('AcDbLine', '')
                cad.dxf['A3'] = '0 CIRCLE\n10 (12 21 0)\n11 (15 20 0)\n'
                with self.assertRaisesRegex(OutcomeUnknown, 'DXF type mismatch'):
                    Cad.execute(cad, op, args)
                self.assertEqual([], cad.commands)

    def test_scale_rejects_unexpected_handles_and_com_type(self):
        for message, change in (
            ('handles changed', lambda cad: cad.store.pop('A')),
            ('COM type changed', lambda cad: setattr(cad.source, 'ObjectName', 'AcDbCircle')),
        ):
            with self.subTest(message=message):
                cad = TransformCad('AcDbLine', SCALED_DXF['AcDbLine'])
                cad.after_write = partial(change, cad)
                with self.assertRaisesRegex(OutcomeUnknown, message):
                    Cad.execute(cad, 'entity.scale', SCALE)
                self.assertEqual([SCALE_COMMAND], cad.commands)
                self.assertEqual(['A3'], cad.dumps)

    def test_mirror_rejects_unexpected_handle_changes(self):
        for erase, handle, keep_source, remove_bystander in (
            (False, 'A3', True, False),
            (False, 'A5', False, False),
            (True, 'A5', True, False),
            (True, 'A5', True, True),
        ):
            with self.subTest(erase=erase, handle=handle, keep_source=keep_source):
                cad = TransformCad('AcDbLine', MIRRORED_DXF['AcDbLine'], handle, keep_source)
                if remove_bystander:
                    cad.after_write = lambda: cad.store.pop('A')
                with self.assertRaisesRegex(OutcomeUnknown, 'Unexpected entity handle changes'):
                    Cad.execute(cad, 'entity.mirror', {**MIRROR, 'erase_source': erase})
                self.assertEqual(['A3'], cad.dumps)

    def test_mirror_requires_zero_source_drift_and_matching_result_type(self):
        for message, change in (
            ('Source entity changed', lambda cad: setattr(cad.source, 'ObjectName', 'AcDbCircle')),
            ('Source entity changed', lambda cad: cad.dxf.update(
                A3=cad.dxf['A3'].replace('(12 21 0)', '(12.001 21 0)'))),
            ('COM type changed', lambda cad: setattr(cad.store['A5'], 'ObjectName', 'AcDbCircle')),
        ):
            with self.subTest(message=message):
                cad = TransformCad('AcDbLine', MIRRORED_DXF['AcDbLine'], 'A5', keep_source=True)
                cad.after_write = partial(change, cad)
                with self.assertRaisesRegex(OutcomeUnknown, message):
                    Cad.execute(cad, 'entity.mirror', MIRROR)

    def test_scale_rejects_dxf_drift(self):
        for name, old, new, message in (
            ('AcDbLine', '0 LINE', '0 ARC', 'DXF type mismatch'),
            ('AcDbLine', '(20 20 0)', '(20.1 20 0)', 'DXF end'),
            ('AcDbCircle', '40 20', '40 20.1', 'DXF radius'),
            ('AcDbArc', '51 0.25', '51 0.26', 'DXF end_angle_rad'),
            ('AcDbText', f'1 {TEXT}', '1 changed', 'DXF text'),
            ('AcDbText', '40 5', '40 5.1', 'DXF height'),
            ('AcDbText', f'50 {5.75 - math.tau}', '50 5.7', 'DXF rotation'),
            ('AcDbPolyline', '70 129', '70 128', 'DXF closed'),
            ('AcDbPolyline', '10 (12 26)\n', '', 'DXF vertex count'),
        ):
            with self.subTest(name=name, message=message):
                cad = TransformCad(name, SCALED_DXF[name].replace(old, new))
                with self.assertRaisesRegex(OutcomeUnknown, message):
                    Cad.execute(cad, 'entity.scale', SCALE)

    def test_mirror_rejects_dxf_drift_including_unswapped_arc_angles(self):
        for name, old, new, message in (
            ('AcDbLine', '(11 22 0)', '(11.1 22 0)', 'DXF start'),
            ('AcDbCircle', '(11 22 0)', '(11 22.1 0)', 'DXF center'),
            ('AcDbCircle', '40 10', '40 10.1', 'DXF radius'),
            ('AcDbArc', f'50 {math.pi / 2 - 0.25}', f'50 {math.pi / 2 - 5.5}', 'DXF start_angle_rad'),
            ('AcDbArc', f'51 {math.pi / 2 - 5.5}', '51 0.25', 'DXF end_angle_rad'),
            ('AcDbPolyline', '10 (13 21)', '10 (13 21.1)', 'DXF vertex 2'),
            ('AcDbPolyline', '70 1', '70 128', 'DXF closed'),
        ):
            with self.subTest(name=name, message=message):
                cad = TransformCad(name, MIRRORED_DXF[name].replace(old, new), 'A5', keep_source=True)
                with self.assertRaisesRegex(OutcomeUnknown, message):
                    Cad.execute(cad, 'entity.mirror', MIRROR)


if __name__ == '__main__':
    unittest.main()

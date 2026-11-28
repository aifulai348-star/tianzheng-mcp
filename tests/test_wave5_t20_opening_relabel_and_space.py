import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, CommandCompletionTimeout, OutcomeUnknown, retry_document_read
from tz_mcp.commands import SCHEMAS, command_payload, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


RELABEL = {'document_id': ' plan ', 'handle': '3cf', 'label': ' FM甲1021 '}
SPACE = {'point': (3000, 2500)}
SPACE_COMMAND = 'TSpArea\n3000.0000,2500.0000\n\n'
DISABLE_SNAPS = '(setvar "OSMODE" 0)\n'
SPACE_DXF = {'type': 'TCH_SPACE', 'handle': '36A', 'layer': 'SPACE', 'name': '房间',
             'number': '1001', 'point': (3000.0, 2500.0, 0.0), 'area_m2': 27.4176,
             'perimeter_mm': 21040.0, 'offset_mm': 120.0, 'flags': 5}
SPACE_ROWS = ('0 TCH_SPACE\n5 36A\n8 SPACE\n1 房间\n2 1001\n'
              '10 (3000.0 2500.0 0.0)\n41 27.4176\n42 21040.0\n43 120.0\n70 5\n')


class PrepareTests(unittest.TestCase):
    def test_registration_and_escape_policy(self):
        for op, args, no_escape in (('opening.relabel', RELABEL, True),
                                     ('space.create', SPACE, False)):
            with self.subTest(op=op):
                self.assertEqual((set(args), set()), SCHEMAS[op])
                capability = cli.capabilities()[op]
                self.assertEqual(sorted(args), capability['required'])
                self.assertEqual([], capability['optional'])
                self.assertTrue(capability['validation'].startswith('verified_live'))
                self.assertEqual(no_escape, op in NO_ESCAPE_STEPS)
                for key in args:
                    with self.assertRaises(ValueError):
                        prepare(op, {k: v for k, v in args.items() if k != key})
                with self.assertRaises(ValueError):
                    prepare(op, {**args, 'unsupported': True})

    def test_relabel_reference_and_label_normalization(self):
        for label in ('M0921', 'C1518', 'FM甲1021', '甲' * 64):
            with self.subTest(label=label):
                self.assertEqual({'document_id': 'plan', 'handle': '3CF', 'label': label},
                                 prepare('opening.relabel', {**RELABEL, 'label': f'  {label}  '}))

    def test_relabel_rejects_invalid_labels_and_references(self):
        invalid = {'label': (None, 1, '', '   ', '甲' * 65, '\tM0921', 'M0\r921',
                             'M0921\n', 'M\0', 'C\x1f', 'C\x7f'),
                   'document_id': (None, 1, ' '),
                   'handle': (None, 1, '', '0x3cf', '3CG', '3CF\n')}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare('opening.relabel', {**RELABEL, key: value})

    def test_space_plan_point_and_exact_raw_payload(self):
        args = prepare('space.create', {'point': [-12.125, 3.1, 0]})
        self.assertEqual({'point': [-12.125, 3.1, 0.0]}, args)
        self.assertEqual('TSpArea\n-12.1250,3.1000\n\n', command_payload('space.create', args))
        self.assertEqual(SPACE_COMMAND, command_payload('space.create', prepare('space.create', SPACE)))

    def test_space_rejects_invalid_points(self):
        for coords in (None, [1], [1, 2, 0, 0], [True, 2], [1, 2, False], [1, '2'],
                       [float('nan'), 0], [0, float('inf')], [10**400, 0], [0, 0, 1e-12]):
            with self.subTest(coords=coords), self.assertRaises(ValueError):
                prepare('space.create', {'point': coords})

    def test_label_transient_lookup_retries(self):
        reader = Mock(side_effect=[AttributeError('<unknown>.Label'), 'M0921'])
        with patch('tz_mcp.cad.time.sleep'):
            self.assertEqual('M0921', retry_document_read(reader))
        self.assertEqual(2, reader.call_count)


class DxfTests(unittest.TestCase):
    def read(self, adapter, handle, rows, groups):
        def dump(payload):
            self.assertIn(f'(entget (handent "{handle}"))', payload)
            self.assertIn(f'(quote ({groups}))', payload)
            self.assertIn('"utf8-bom"', payload)
            path = re.search(r'\(open "([^"]+)" "w"', payload)[1]
            Path(path).write_bytes(rows.replace('\n', '\r\n').encode('utf-8-sig'))

        cad = SimpleNamespace(run_command=Mock(side_effect=dump))
        with TemporaryDirectory() as folder:
            result = adapter(cad, handle, folder=folder)
        cad.run_command.assert_called_once()
        return result

    def test_opening_label_retains_unicode_and_spaces(self):
        result = self.read(panel.opening_label_dxf, '3CF',
                           '0 TCH_OPENING\n5 3CF\n8 DOOR\n302  FM甲1021 \n', '0 5 8 302')
        self.assertEqual({'type': 'TCH_OPENING', 'handle': '3CF', 'layer': 'DOOR',
                          'label': ' FM甲1021 '}, result)

    def test_space_dxf_reads_all_fields_and_types(self):
        self.assertEqual(SPACE_DXF, self.read(panel.space_dxf, '36A', SPACE_ROWS,
                                              '0 5 8 1 2 10 41 42 43 70'))


class FakeCad:
    document_id = 'plan'
    hwnd = 10

    def __init__(self):
        self.opening = SimpleNamespace(ObjectName='TDbOpening', Handle='3CF', Label='M0921',
                                       Width=900.0, Height=2100.0, WinSill=0.0, Update=Mock())
        self.store = {handle: SimpleNamespace(ObjectName='TDbWall', HoleArea=area)
                      for handle, area in (('35A', 1.89), ('35B', 0.0), ('35C', 0.0))}
        self.store['3CF'] = self.opening
        self.osmode = 16383
        self.doc = SimpleNamespace(GetVariable=Mock(return_value=self.osmode),
                                   SetVariable=Mock(side_effect=self.set_variable))
        self.guard = Mock()
        self.run_command = Mock(side_effect=self.run)
        self.on_space_command = Mock(side_effect=self.add_space)

    def entities(self):
        return self.store.copy()

    def entity(self, handle, document_id):
        return self.store[handle]

    def describe(self, entity):
        return {'handle': entity.Handle, 'object_name': entity.ObjectName}

    def set_variable(self, name, value):
        if name != 'OSMODE':
            raise AssertionError(name)
        self.osmode = value

    def run(self, payload):
        if payload == DISABLE_SNAPS:
            self.osmode = 0
        elif payload == SPACE_COMMAND and self.osmode == 0:
            self.on_space_command()
        else:
            raise AssertionError((payload, self.osmode))

    def add_space(self):
        self.store['36A'] = SimpleNamespace(ObjectName='TDbSpace', Handle='36A', Name='房间')


def label_dxf(label):
    return {'type': 'TCH_OPENING', 'handle': '3CF', 'layer': 'DOOR', 'label': label}


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        self.window = self.start_patch('require_drawing_window')
        self.model_space = self.start_patch('require_model_space')
        self.start_patch('late', side_effect=lambda entity: entity)
        self.hosts = self.start_patch('entity_wall_hosts',
                                     return_value=[{'type': 'TCH_WALL', 'handle': '35A'}])
        self.opening_dxf = self.start_patch('opening_label_dxf',
                                          side_effect=lambda c, _: label_dxf(c.opening.Label))
        self.space_dxf = self.start_patch('space_dxf', return_value=SPACE_DXF.copy())

    def start_patch(self, name, **kwargs):
        patcher = patch.object(parts, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def relabel(self, **changes):
        return Cad.execute(self.cad, 'opening.relabel', {**RELABEL, **changes})

    def create_space(self):
        return Cad.execute(self.cad, 'space.create', SPACE)

    def test_relabel_uses_guarded_dispatch_and_verifies_com_dxf_and_host(self):
        result = self.relabel(handle='03cf')
        self.cad.guard.assert_called_once_with()
        self.window.assert_called_once_with(self.cad.hwnd)
        self.model_space.assert_called_once_with(self.cad.doc)
        self.cad.opening.Update.assert_called_once_with()
        self.assertEqual('FM甲1021', self.cad.opening.Label)
        self.assertEqual([call(self.cad, '3CF')] * 2, self.opening_dxf.call_args_list)
        self.assertEqual([call(self.cad, '3CF')] * 2, self.hosts.call_args_list)
        self.cad.run_command.assert_not_called()
        self.assertEqual({
            'success': True, 'document_id': 'plan', 'handles': ['3CF'],
            'entities': [{'handle': '3CF', 'object_name': 'TDbOpening'}],
            'evidence': {'relabel': {'handle': '3CF', 'host_handle': '35A',
                                     'label_before': 'M0921', 'label_requested': 'FM甲1021',
                                     'label_after': 'FM甲1021', 'dxf_before': label_dxf('M0921'),
                                     'dxf_after': label_dxf('FM甲1021'),
                                     'properties_after': {'Width': 900.0, 'Height': 2100.0, 'WinSill': 0.0}}},
            'validation': {'type': True, 'properties': True, 'dxf': 'verified_302',
                           'host': 'verified_dxf_330_and_hole_areas', 'reopen': 'pending'},
        }, result)

    def test_relabel_reference_and_com_type_guards_leave_opening_unchanged(self):
        for changes, error in (({'document_id': 'other'}, 'document_id mismatch'),
                               ({'handle': 'FFFF'}, 'Unknown handle'),
                               ({'handle': '35A'}, 'requires a native opening')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, error):
                self.relabel(**changes)
        self.hosts.assert_not_called()
        self.assert_unwritten_opening()

    def assert_unwritten_opening(self):
        self.assertEqual('M0921', self.cad.opening.Label)
        self.cad.opening.Update.assert_not_called()
        self.cad.run_command.assert_not_called()

    def test_relabel_rejects_invalid_pre_write_host(self):
        for refs in ([], [{'type': 'TCH_WALL', 'handle': '35A'}] * 2,
                     [{'type': 'TCH_WALL', 'handle': 'FFF'}]):
            self.hosts.return_value = refs
            with self.subTest(refs=refs), self.assertRaisesRegex(ValueError, 'host'):
                self.relabel()
        self.assert_unwritten_opening()

    def test_relabel_rejects_bad_dxf_and_nonfinite_pre_write_readback(self):
        with patch.object(parts, 'opening_label_dxf', return_value={'type': 'LINE'}):
            with self.assertRaisesRegex(OutcomeUnknown, 'DXF type'):
                self.relabel()
        self.cad.store['35B'].HoleArea = float('nan')
        with self.assertRaisesRegex(OutcomeUnknown, 'HoleArea is not finite'):
            self.relabel()
        self.cad.opening.Width = float('inf')
        with self.assertRaisesRegex(OutcomeUnknown, 'COM Width is not finite'):
            self.relabel()
        self.assert_unwritten_opening()

    def test_relabel_detects_com_and_handle_changes(self):
        for key, value in (('Label', 'M0921'), ('Width', 901), ('Height', 2099), ('WinSill', 1),
                           ('ObjectName', 'AcDbLine')):
            self.cad = FakeCad()
            opening = self.cad.opening
            opening.Update.side_effect = lambda: setattr(opening, key, value)
            with self.subTest(key=key), self.assertRaisesRegex(OutcomeUnknown, f'{key}|COM type'):
                self.relabel()
        self.cad = FakeCad()
        self.cad.opening.Update.side_effect = self.cad.add_space
        with self.assertRaisesRegex(OutcomeUnknown, 'Entity handles changed'):
            self.relabel()

    def test_relabel_detects_dxf_group_302_and_type_drift(self):
        for wrong in ({'label': 'M0921'}, {'label': None}, {'type': 'LINE'}):
            self.cad = FakeCad()
            self.opening_dxf.side_effect = [label_dxf('M0921'), {**label_dxf('FM甲1021'), **wrong}]
            with self.subTest(wrong=wrong), self.assertRaisesRegex(OutcomeUnknown, 'DXF.*302'):
                self.relabel()

    def test_relabel_detects_host_switch_and_hole_area_drift_on_any_wall(self):
        self.hosts.side_effect = [[{'type': 'TCH_WALL', 'handle': handle}]
                                  for handle in ('35A', '35B')]
        with self.assertRaisesRegex(OutcomeUnknown, 'host changed'):
            self.relabel()
        self.hosts.side_effect = None
        for handle in ('35A', '35B'):
            self.cad = FakeCad()
            self.cad.opening.Update.side_effect = lambda: setattr(self.cad.store[handle], 'HoleArea', 10.0)
            clock = Mock(monotonic=Mock(side_effect=[0, 6]))
            with self.subTest(handle=handle), patch.object(parts, 'time', clock):
                with self.assertRaisesRegex(OutcomeUnknown, 'hole-area update mismatch'):
                    self.relabel()

    def assert_snap_restore(self):
        self.assertEqual([call(DISABLE_SNAPS), call(SPACE_COMMAND)], self.cad.run_command.call_args_list)
        self.cad.doc.GetVariable.assert_called_once_with('OSMODE')
        self.cad.doc.SetVariable.assert_called_once_with('OSMODE', 16383)
        self.assertEqual(16383, self.cad.osmode)

    def test_space_creates_one_native_space_and_reports_dxf(self):
        result = self.create_space()
        self.cad.guard.assert_called_once_with()
        self.window.assert_called_once_with(self.cad.hwnd)
        self.model_space.assert_called_once_with(self.cad.doc)
        self.assert_snap_restore()
        self.space_dxf.assert_called_once_with(self.cad, '36A')
        self.assertEqual({
            'success': True, 'document_id': 'plan', 'handles': ['36A'],
            'entities': [{'handle': '36A', 'object_name': 'TDbSpace'}],
            'evidence': {'space': {'command': 'TSpArea', 'point': [3000.0, 2500.0, 0.0],
                                   'com_name': '房间', 'name': '房间', 'number': '1001',
                                   'area_m2': 27.4176, 'perimeter_mm': 21040.0, 'dxf': SPACE_DXF}},
            'validation': {'type': True, 'properties': True,
                           'dxf': 'verified_1_2_10_41_42', 'reopen': 'pending'},
        }, result)

    def test_space_pre_write_wall_guard_ignores_nonwalls(self):
        self.cad.store['35C'].ObjectName = 'AcDbLine'
        with self.assertRaisesRegex(ValueError, 'at least three native walls'):
            self.create_space()
        self.cad.doc.GetVariable.assert_not_called()
        self.cad.doc.SetVariable.assert_not_called()
        self.cad.run_command.assert_not_called()
        self.space_dxf.assert_not_called()

    def test_space_rejects_unexpected_entity_changes(self):
        def mutate(mode):
            if mode != 'none':
                self.cad.add_space()
            if mode == 'multiple':
                self.cad.store['36B'] = self.cad.store['36A']
            if mode == 'removed':
                del self.cad.store['35A']
            if mode == 'wrong_type':
                self.cad.store['36A'].ObjectName = 'AcDbLine'

        for mode in ('none', 'multiple', 'removed', 'wrong_type'):
            self.cad = FakeCad()
            self.cad.on_space_command.side_effect = lambda: mutate(mode)
            with self.subTest(mode=mode), self.assertRaises(OutcomeUnknown):
                self.create_space()
            self.assert_snap_restore()
        self.space_dxf.assert_not_called()

    def test_space_rejects_missing_or_invalid_required_dxf(self):
        for key, invalid in (('type', ('LINE', None)), ('point', (None,)),
                             ('area_m2', (None, float('nan'), float('inf'), 0.01, -1)),
                             ('perimeter_mm', (None, float('nan'), float('inf'), 10.0, -1))):
            for value in invalid:
                self.cad = FakeCad()
                self.space_dxf.return_value = {**SPACE_DXF, key: value}
                with self.subTest(key=key, value=value), self.assertRaisesRegex(OutcomeUnknown, 'space DXF'):
                    self.create_space()
                self.assert_snap_restore()

    def test_space_restores_osmode_on_command_timeout(self):
        self.cad.on_space_command.side_effect = CommandCompletionTimeout('no completion')
        with self.assertRaisesRegex(CommandCompletionTimeout, 'no completion'):
            self.create_space()
        self.assert_snap_restore()
        self.space_dxf.assert_not_called()

    def test_space_reports_unknown_when_snap_restore_fails(self):
        self.cad.doc.SetVariable.side_effect = RuntimeError('restore failure')
        with self.assertRaisesRegex(OutcomeUnknown, 'Could not restore OSMODE'):
            self.create_space()
        self.space_dxf.assert_not_called()

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, CommandCompletionTimeout, OutcomeUnknown
from tz_mcp.commands import SCHEMAS, command_payload, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


DIM_ARGS = {'start': [13000, 19000], 'end': [13000, 25000]}
FIX_ARGS = {'corner1': [15000, 19000], 'corner2': [17000, 21000]}
DIM_COMMAND = 'TDimWall\n13000.0000,19000.0000\n13000.0000,25000.0000\n\n'
FIX_COMMAND = ('(progn (command "TFixWallEx" (list 15000.0000 19000.0000 0.0) '
               '(list 17000.0000 21000.0000 0.0) "") (princ))\n')
DISABLE_SNAPS = '(setvar "OSMODE" 0)\n'


class PrepareTests(unittest.TestCase):
    def test_schemas_capabilities_and_escape(self):
        for op, args in (('dim.wall', DIM_ARGS), ('wall.fix_corner', FIX_ARGS)):
            with self.subTest(op=op):
                self.assertEqual((set(args), set()), SCHEMAS[op])
                self.assertEqual(sorted(args), cli.capabilities()[op]['required'])
                self.assertEqual([], cli.capabilities()[op]['optional'])
                self.assertTrue(cli.VALIDATION[op].startswith('verified_live'))
                self.assertNotIn(op, NO_ESCAPE_STEPS)
                with self.assertRaises(ValueError):
                    prepare(op, {})
                with self.assertRaises(ValueError):
                    prepare(op, {**args, 'extra': True})

    def test_points_normalize_mixed_dimensions_and_corner_order(self):
        self.assertEqual({'start': [-2.0, 3.0, 0.0], 'end': [4.0, 3.0, 0.0]},
                         prepare('dim.wall', {'start': (-2, 3, 0), 'end': [4, 3]}))
        self.assertEqual({'corner1': [4.0, 5.0, 0.0], 'corner2': [-2.0, 3.0, 0.0]},
                         prepare('wall.fix_corner', {'corner1': [4, 5], 'corner2': [-2, 3, 0]}))

    def test_invalid_or_nonplan_points(self):
        invalid = ([1, 2, 1e-12], [float('inf'), 2], [1, float('nan')],
                   [True, 2], ['1', 2], [2], [2, 2, 0, 0], [10**400, 0])
        for op, first, second in (('dim.wall', 'start', 'end'),
                                  ('wall.fix_corner', 'corner1', 'corner2')):
            for bad in invalid:
                with self.subTest(op=op, point=bad), self.assertRaises(ValueError):
                    prepare(op, {first: bad, second: [3, 4]})

    def test_degenerate_segment_and_box(self):
        for end in ([0, 0], [1e-6, 0], [5e-7, 5e-7]):
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, 'distinct'):
                prepare('dim.wall', {'start': [0, 0], 'end': end})
        for corner2 in ([0, 0], [0, 5], [5, 0], [1e-6, 5], [5, 1e-6]):
            with self.subTest(corner2=corner2), self.assertRaisesRegex(ValueError, 'box'):
                prepare('wall.fix_corner', {'corner1': [0, 0], 'corner2': corner2})

    def test_exact_native_payloads(self):
        self.assertEqual(DIM_COMMAND, command_payload('dim.wall', prepare('dim.wall', DIM_ARGS)))
        self.assertEqual(FIX_COMMAND,
                         command_payload('wall.fix_corner', prepare('wall.fix_corner', FIX_ARGS)))


def entity(kind, layer='0', hole_area=0.0):
    return SimpleNamespace(ObjectName=kind, Layer=layer, HoleArea=hole_area)


def axis(kind, layer, start, end, length):
    return {'type': kind, 'layer': layer, 'start': [*start, 0.0],
            'end': [*end, 0.0], 'length': length}


def wall_axis(start, end, length):
    return axis('TCH_WALL', 'WALL', start, end, length)


class FakeCad:
    def __init__(self):
        self.hwnd = 10
        self.document_id = 'owned-drawing'
        self.store = {'35A': entity('TDbWall', 'WALL', 1.89), '35B': entity('TDbWall', 'WALL'),
                      '3CF': entity('TDbOpening')}
        self.axes = {'35A': wall_axis((10000, 20000), (15800, 20000), 5800.0),
                     '35B': wall_axis((15600, 19500), (15600, 23300), 3800.0)}
        self.osmode = 16383
        self.doc = SimpleNamespace(GetVariable=Mock(side_effect=lambda _: str(self.osmode)),
                                   SetVariable=Mock(side_effect=self.set_osmode))
        self.guard = Mock()
        self.run_command = Mock(side_effect=self.run)
        self.on_command = Mock()
        self.command_osmodes = []

    def set_osmode(self, name, value):
        if name != 'OSMODE':
            raise AssertionError(name)
        self.osmode = value

    def run(self, payload):
        if payload == DISABLE_SNAPS:
            self.osmode = 0
        else:
            self.command_osmodes.append(self.osmode)
            self.on_command()

    def entities(self):
        return dict(self.store)

    def describe(self, entity):
        return {'object_name': entity.ObjectName, 'layer': entity.Layer}


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.cad = FakeCad()
        self.axes = self.start_patch('curve_axes', side_effect=self.read_axes)
        self.window_guard = self.start_patch('require_drawing_window')
        self.model_guard = self.start_patch('require_model_space')

    def start_patch(self, name, **kwargs):
        patcher = patch.object(parts, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    @staticmethod
    def read_axes(cad, handles):
        return {h: cad.axes[h].copy() for h in handles}

    def dimension(self):
        return Cad.execute(self.cad, 'dim.wall', DIM_ARGS)

    def fix_corner(self):
        return Cad.execute(self.cad, 'wall.fix_corner', FIX_ARGS)

    def add_dimensions(self):
        for h, y in (('36B', 24000), ('36A', 20000)):
            self.cad.store[h] = entity('TDbDimension2', 'pub_dim')
            self.cad.axes[h] = axis('TCH_DIMENSION2', 'PUB_DIM',
                                    (13000, y + 50), (13000, y - 50), 100.0)

    def repair_crossing(self):
        self.cad.store['371'] = entity('TDbWall', 'WALL')
        self.cad.axes.update({
            '35A': wall_axis((10000, 20000), (15600, 20000), 5600.0),
            '35B': wall_axis((15600, 19500), (15600, 20000), 500.0),
            '371': wall_axis((15600, 20000), (15600, 23300), 3300.0),
        })

    def split_crossing(self):
        self.repair_crossing()
        self.cad.store['372'] = entity('TDbWall', 'WALL')
        self.cad.axes['372'] = wall_axis((15600, 20000), (15800, 20000), 200.0)

    def assert_snap_scope(self, command):
        self.cad.doc.GetVariable.assert_called_once_with('OSMODE')
        self.assertEqual([call(DISABLE_SNAPS), call(command)], self.cad.run_command.call_args_list)
        self.assertEqual([0], self.cad.command_osmodes)
        self.cad.doc.SetVariable.assert_called_once_with('OSMODE', 16383)
        self.assertEqual(16383, self.cad.osmode)

    def test_dimension_guards_and_batched_evidence(self):
        self.cad.on_command.side_effect = self.add_dimensions
        result = self.dimension()
        self.cad.guard.assert_called_once_with()
        self.window_guard.assert_called_once_with(self.cad.hwnd)
        self.model_guard.assert_called_once_with(self.cad.doc)
        self.assert_snap_scope(DIM_COMMAND)
        self.assertTrue(result['success'])
        self.assertEqual('owned-drawing', result['document_id'])
        self.assertEqual(['36A', '36B'], result['handles'])
        self.assertEqual([{'object_name': 'TDbDimension2', 'layer': 'pub_dim'}] * 2,
                         result['entities'])
        self.axes.assert_called_once_with(self.cad, ['36A', '36B'])
        self.assertEqual({'command': 'TDimWall', **DIM_ARGS, 'dimension_count': 2, 'layer': 'PUB_DIM',
                          'dimensions': [{'handle': h, **self.cad.axes[h]} for h in ('36A', '36B')]},
                         result['evidence']['dim_wall'])
        self.assertEqual({'type': True, 'properties': 'layer_and_curve_span_verified',
                          'geometry': 'verified_vlax_curve_span', 'reopen': 'pending'},
                         result['validation'])

    def test_pre_write_wall_minimums(self):
        del self.cad.store['35B']
        with self.assertRaisesRegex(ValueError, 'at least two native walls'):
            self.fix_corner()
        del self.cad.store['35A']
        with self.assertRaisesRegex(ValueError, 'at least one native wall'):
            self.dimension()
        self.cad.run_command.assert_not_called()
        self.cad.doc.GetVariable.assert_not_called()
        self.axes.assert_not_called()

    def test_dimension_requires_added_entity(self):
        with self.assertRaisesRegex(OutcomeUnknown, 'created no new dimension entities'):
            self.dimension()
        self.assert_snap_scope(DIM_COMMAND)
        self.axes.assert_not_called()

    def test_dimension_rejects_existing_entity_removal(self):
        def modify():
            self.add_dimensions()
            del self.cad.store['3CF']

        self.cad.on_command.side_effect = modify
        with self.assertRaisesRegex(OutcomeUnknown, 'removed existing entities.*3CF'):
            self.dimension()
        self.axes.assert_not_called()

    def test_dimension_wrong_com_type_or_layer(self):
        for attr, value in (('ObjectName', 'AcDbLine'), ('Layer', 'AXIS')):
            with self.subTest(attr=attr):
                self.cad = FakeCad()

                def modify():
                    self.add_dimensions()
                    setattr(self.cad.store['36B'], attr, value)

                self.cad.on_command.side_effect = modify
                with self.assertRaisesRegex(OutcomeUnknown, 'unexpected entity 36B'):
                    self.dimension()
        self.axes.assert_not_called()

    def test_dimension_wrong_dxf_type_layer_or_span(self):
        for key, value in (('type', 'LINE'), ('layer', 'AXIS'), ('length', 0.01)):
            with self.subTest(key=key):
                self.cad = FakeCad()

                def modify():
                    self.add_dimensions()
                    self.cad.axes['36B'][key] = value

                self.cad.on_command.side_effect = modify
                with self.assertRaisesRegex(OutcomeUnknown, 'invalid dimension curve span for 36B'):
                    self.dimension()

    def test_fix_corner_conserves_lengths_and_returns_split_walls(self):
        self.cad.on_command.side_effect = self.split_crossing
        result = self.fix_corner()
        self.assert_snap_scope(FIX_COMMAND)
        self.assertEqual([call(self.cad, ['35A', '35B']),
                          call(self.cad, ['35A', '35B', '371', '372'])], self.axes.call_args_list)
        self.assertTrue(result['success'])
        self.assertEqual('owned-drawing', result['document_id'])
        self.assertEqual(['371', '372'], result['handles'])
        self.assertEqual([{'object_name': 'TDbWall', 'layer': 'WALL'}] * 2, result['entities'])
        evidence = result['evidence']['wall_fix_corner']
        self.assertTrue(evidence['repaired'])
        self.assertEqual('TFixWallEx', evidence['command'])
        for key, value in FIX_ARGS.items():
            self.assertEqual(value, evidence[key])
        self.assertEqual(['371', '372'], evidence['added_wall_handles'])
        self.assertEqual([], evidence['removed_wall_handles'])
        self.assertEqual(['35A', '35B', '371', '372'], evidence['changed_wall_handles'])
        self.assertEqual((2, 4), (evidence['wall_count_before'], evidence['wall_count_after']))
        self.assertEqual((9600.0, 9600.0),
                         (evidence['total_wall_length_before'], evidence['total_wall_length_after']))
        self.assertEqual(0.0, evidence['trimmed_wall_length'])
        self.assertEqual(5800.0, evidence['axes_before']['35A']['length'])
        self.assertEqual(self.cad.axes, evidence['axes_after'])
        self.assertEqual({'type': True,
                          'properties': 'wall_length_non_increasing_and_hole_area_conserved',
                          'geometry': 'verified_vlax_curve_axes', 'reopen': 'pending'},
                         result['validation'])

    def test_fix_corner_trims_crossing_overshoot(self):
        self.cad.on_command.side_effect = self.repair_crossing
        result = self.fix_corner()
        self.assert_snap_scope(FIX_COMMAND)
        self.assertTrue(result['success'])
        self.assertEqual(['371'], result['handles'])
        evidence = result['evidence']['wall_fix_corner']
        self.assertTrue(evidence['repaired'])
        self.assertEqual((2, 3), (evidence['wall_count_before'], evidence['wall_count_after']))
        self.assertEqual((9600.0, 9400.0),
                         (evidence['total_wall_length_before'], evidence['total_wall_length_after']))
        self.assertEqual(200.0, evidence['trimmed_wall_length'])

    def test_fix_corner_refuses_unverified_baseline(self):
        self.cad.axes['35B']['type'] = 'LINE'
        with self.assertRaisesRegex(OutcomeUnknown, 'Invalid native wall curve axis for 35B'):
            self.fix_corner()
        self.cad.run_command.assert_not_called()

    def test_fix_corner_disjoint_gap_is_successful_noop(self):
        self.cad.axes['35B'] = wall_axis((15600, 20200), (15600, 24000), 3800.0)
        result = self.fix_corner()
        self.assert_snap_scope(FIX_COMMAND)
        self.assertTrue(result['success'])
        self.assertEqual([], result['handles'])
        self.assertEqual([], result['entities'])
        evidence = result['evidence']['wall_fix_corner']
        self.assertFalse(evidence['repaired'])
        for key in ('added_wall_handles', 'removed_wall_handles', 'changed_wall_handles'):
            self.assertEqual([], evidence[key])
        self.assertEqual(evidence['axes_before'], evidence['axes_after'])

    def test_fix_corner_reports_removed_wall_and_in_place_change(self):
        def merge():
            del self.cad.store['35B']
            self.cad.axes['35A'] = wall_axis((10000, 20000), (19600, 20000), 9600.0)

        self.cad.on_command.side_effect = merge
        result = self.fix_corner()
        self.assertEqual(['35A', '35B'], result['handles'])
        self.assertEqual([{'object_name': 'TDbWall', 'layer': 'WALL'}], result['entities'])
        evidence = result['evidence']['wall_fix_corner']
        self.assertTrue(evidence['repaired'])
        self.assertEqual(['35B'], evidence['removed_wall_handles'])
        self.assertEqual(['35A', '35B'], evidence['changed_wall_handles'])

    def test_fix_corner_rejects_non_wall_handle_changes(self):
        for handle, replacement in (('3CF', None), ('400', entity('AcDbLine')),
                                    ('3CF', entity('TDbWall'))):
            with self.subTest(handle=handle, replacement=replacement):
                self.cad = FakeCad()

                def modify():
                    if replacement is None:
                        del self.cad.store[handle]
                    else:
                        self.cad.store[handle] = replacement

                self.cad.on_command.side_effect = modify
                with self.assertRaisesRegex(OutcomeUnknown, 'Non-wall entity handles changed'):
                    self.fix_corner()
                self.assertEqual(16383, self.cad.osmode)

    def test_fix_corner_rejects_wall_length_increase(self):
        def modify():
            self.split_crossing()
            self.cad.axes['372'] = wall_axis((15600, 20000), (15810, 20000), 210.0)

        self.cad.on_command.side_effect = modify
        with self.assertRaisesRegex(OutcomeUnknown, 'Total wall length changed unexpectedly'):
            self.fix_corner()
        self.assert_snap_scope(FIX_COMMAND)

    def test_fix_corner_rejects_zero_total_wall_length(self):
        def remove_walls():
            del self.cad.store['35A']
            del self.cad.store['35B']

        self.cad.on_command.side_effect = remove_walls
        with self.assertRaisesRegex(OutcomeUnknown, 'Total wall length changed unexpectedly'):
            self.fix_corner()
        self.assert_snap_scope(FIX_COMMAND)

    def test_fix_corner_hole_area_conservation(self):
        def modify():
            self.repair_crossing()
            self.cad.store['35A'].HoleArea = 0.0

        self.cad.on_command.side_effect = modify
        with self.assertRaisesRegex(OutcomeUnknown, 'Total wall HoleArea changed unexpectedly'):
            self.fix_corner()
        self.assert_snap_scope(FIX_COMMAND)

    def test_native_failure_restores_snaps_for_both_ops(self):
        for run, payload in ((self.dimension, DIM_COMMAND), (self.fix_corner, FIX_COMMAND)):
            with self.subTest(payload=payload):
                self.cad = FakeCad()
                self.cad.on_command.side_effect = CommandCompletionTimeout('timed out')
                with self.assertRaisesRegex(CommandCompletionTimeout, 'timed out'):
                    run()
                self.assert_snap_scope(payload)

    def test_failed_osmode_disable_still_attempts_restore(self):
        def fail_after_setting(payload):
            self.cad.run(payload)
            raise CommandCompletionTimeout('disable timed out')

        self.cad.run_command.side_effect = fail_after_setting
        with self.assertRaisesRegex(CommandCompletionTimeout, 'disable timed out'):
            self.dimension()
        self.cad.run_command.assert_called_once_with(DISABLE_SNAPS)
        self.cad.doc.SetVariable.assert_called_once_with('OSMODE', 16383)
        self.assertEqual(16383, self.cad.osmode)

    def test_failed_restore_preserves_timeout_recovery_signal(self):
        self.cad.on_command.side_effect = CommandCompletionTimeout('timed out')
        self.cad.doc.SetVariable.side_effect = RuntimeError('COM refused')
        with self.assertRaisesRegex(CommandCompletionTimeout, 'timed out.*Could not restore OSMODE'):
            self.dimension()
        self.cad.doc.SetVariable.assert_called_once_with('OSMODE', 16383)


class CurveAxesTests(unittest.TestCase):
    ROW = '35A\tTCH_WALL\t墙体 层\t-1.23456789\t.5\t0\t3\t4e+1\t-2\t100.125'

    def read(self, content, handles=('35A',)):
        paths = []

        def dump(payload):
            path = Path(re.search(r'\(open "([^"]+/axis\.txt)"', payload).group(1))
            paths.append(path)
            if content is not None:
                path.write_bytes(content)

        cad = SimpleNamespace(run_command=Mock(side_effect=dump))
        with tempfile.TemporaryDirectory() as folder:
            try:
                result = panel.curve_axes(cad, handles, timeout=0, folder=folder)
            finally:
                self.assertEqual(1, len(paths))
                self.assertTrue(paths[0].parent.name.startswith('curve-axis-'))
                self.assertEqual(Path(folder).resolve(), paths[0].parent.parent)
                self.assertFalse(paths[0].parent.exists())
        return result, cad.run_command.call_args.args[0]

    def test_batch_payload_and_utf8_parser(self):
        dim = '36B\tTCH_DIMENSION2\tPUB_DIM\t1\t2\t0\t1\t4\t0\t2'
        result, payload = self.read((self.ROW + '\r\n' + dim + '\r\n').encode('utf-8-sig'),
                                    ('35A', '36b'))
        self.assertTrue(payload.startswith('((lambda '))
        self.assertTrue(payload.endswith('\n'))
        self.assertEqual(1, payload.count('\n'))
        for form in ('(vl-load-com)', '(foreach h (list "35A" "36B")', '(handent h)',
                     'vlax-curve-getStartPoint', 'vlax-curve-getEndPoint',
                     'vlax-curve-getDistAtParam', '(assoc 0 d)', '(assoc 8 d)',
                     '(rtos v 2 8)', '(close f)'):
            self.assertIn(form, payload)
        depth = 0
        for char in re.sub(r'"[^"]*"', '', payload):
            depth += (char == '(') - (char == ')')
            self.assertGreaterEqual(depth, 0)
        self.assertEqual(0, depth)
        self.assertEqual({'type': 'TCH_WALL', 'layer': '墙体 层', 'start': [-1.23456789, 0.5, 0.0],
                          'end': [3.0, 40.0, -2.0], 'length': 100.125}, result['35A'])
        self.assertEqual({'type': 'TCH_DIMENSION2', 'layer': 'PUB_DIM', 'start': [1.0, 2.0, 0.0],
                          'end': [1.0, 4.0, 0.0], 'length': 2.0}, result['36b'])

    def test_gbk_layer_roundtrip(self):
        result, _ = self.read(self.ROW.encode('gbk'))
        self.assertEqual('墙体 层', result['35A']['layer'])

    def test_missing_file_and_handle(self):
        with self.assertRaisesRegex(OutcomeUnknown, 'dump did not appear'):
            self.read(None)
        with self.assertRaisesRegex(OutcomeUnknown, 'missing for handles.*36B'):
            self.read(self.ROW.encode('gbk'), ('35A', '36B'))

    def test_malformed_rows(self):
        rows = [
            self.ROW.rsplit('\t', 1)[0],
            self.ROW + '\t0',
            self.ROW.replace('-1.23456789', 'nan'),
            self.ROW.replace('100.125', 'inf'),
            self.ROW.replace('100.125', 'bad'),
            self.ROW.replace('35A', '36B'),
            self.ROW.replace('TCH_WALL', ''),
            self.ROW.replace('墙体 层', ' '),
            self.ROW + '\n' + self.ROW,
        ]
        for row in rows:
            with self.subTest(row=row), self.assertRaisesRegex(OutcomeUnknown, 'Malformed'):
                self.read(row.encode('gbk'))

    def test_empty_and_invalid_handles_skip_cad(self):
        cad = SimpleNamespace(run_command=Mock())
        self.assertEqual({}, panel.curve_axes(cad, []))
        with self.assertRaisesRegex(ValueError, 'hexadecimal'):
            panel.curve_axes(cad, ['" bad'])
        cad.run_command.assert_not_called()

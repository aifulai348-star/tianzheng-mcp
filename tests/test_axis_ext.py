"""Offline protocol and writeback tests for SPEC-20261127-007."""
import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_axis_core import FakeAxisInput, FakeCad, line
from tz_mcp import panel
from tz_mcp.cad import Cad, OutcomeUnknown
from tz_mcp.cli import VALIDATION
from tz_mcp.commands import SCHEMAS, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


SINGLE = 'axis.single_create'
ANNOTATE = 'axis.annotate'
SINGLE_ARGS = {'start': [10000.0123, 22000.0042], 'end': [13000.0123, 26000.0042]}
ANNOTATE_ARGS = {'start_point': [10000, 22000], 'end_point': [17800, 22000]}


class AxisExtCad(FakeCad):
    def __init__(self, op, before, after):
        super().__init__(before, after)
        self.backend = AxisExtInput(self, op)

    def PostCommand(self, payload):
        self._dispatch('post', payload)

    def SendCommand(self, payload):
        self._dispatch('send', payload)

    def _dispatch(self, method, payload):
        self.events.append((method, payload))
        self.active = 1
        self.backend.foreground = self.backend.hwnd


class AxisExtInput(FakeAxisInput):
    rows = {}

    def __init__(self, cad, op):
        super().__init__(cad)
        if op == SINGLE:
            self.control_ids = (1328, 1331)
            self.caption = '单轴绘制'
            self.text = {1331: '750'}
            self.pick_count = 2
        else:
            self.control_ids = (136, 137, 142, 141)
            self.caption = ''
            self.text = {141: 'Z'}
            self.pick_count = 3
        self.picks = []
        self.reject_edit = False

    def window_patches(self):
        return {**super().window_patches(), 'title': lambda u, hwnd: self.caption}

    def send(self, vk, scan, flags):
        assert (vk, scan, flags) == (0x09, 0, 0)
        self.cad.events.append(('tab', self.focus, self.text[self.focus]))
        if self.reject_edit:
            self.text[self.focus] = 'Z'

    def enter(self):
        assert self.focus in (self.view, self.keyboard_override, self.command_line,
                              self.floating_command_line)
        text = self.text[self.focus]
        self.cad.events.append(('enter', text))
        self.text[self.focus] = ''
        self.picks.append(text)
        self.cad.dispatched = len(self.picks) == self.pick_count


def single_line(overhang, reverse=False):
    x, y = SINGLE_ARGS['start']
    end_x, end_y = SINGLE_ARGS['end']
    # The fixture is a 3:4:5 diagonal, so its direction is known independently.
    points = [(x - 0.6 * overhang, y - 0.8 * overhang),
              (end_x + 0.6 * overhang, end_y + 0.8 * overhang)]
    return line(*(points[::-1] if reverse else points))


def annotation_entities(dim_count):
    return {'FF': SimpleNamespace(ObjectName='TDbAxisLabelSet', Layer='AXIS'),
            **{f'D{i}': SimpleNamespace(ObjectName='TDbDimension2', Layer='axis')
               for i in range(dim_count)}}


class AxisExtTests(unittest.TestCase):
    def run_axis(self, cad, op, args):
        with (patch.multiple(panel, **cad.backend.window_patches()),
              patch('tz_mcp.input_bridge.NativeInput', return_value=cad.backend),
              patch('tz_mcp.parts.require_drawing_window'),
              patch('tz_mcp.parts.require_model_space'),
              patch.object(panel.time, 'sleep')):
            return Cad.execute(cad, op, args)

    def assert_cleanup(self, cad):
        self.assertEqual(cad.events[-2:], [('esc', 2), ('osmode', 16383)])
        self.assertEqual(cad.backend.foreground, cad.hwnd)
        self.assertEqual(cad.active, 0)
        self.assertEqual(cad.osmode, 16383)

    def test_prepare_and_validation_state(self):
        self.assertTrue({SINGLE, ANNOTATE} <= SCHEMAS.keys())
        for op in (SINGLE, ANNOTATE):
            self.assertTrue(VALIDATION[op].startswith('verified_live'))
            self.assertNotIn(op, NO_ESCAPE_STEPS)
        self.assertEqual(prepare(SINGLE, SINGLE_ARGS)['overhang'], 1000.0)
        self.assertEqual(prepare(SINGLE, {**SINGLE_ARGS, 'overhang': 0})['overhang'], 0.0)
        self.assertEqual(prepare(ANNOTATE, ANNOTATE_ARGS)['start_label'], '1')
        self.assertEqual(prepare(ANNOTATE, ANNOTATE_ARGS)['side'], 'double')
        for op, args, invalid in (
            (SINGLE, SINGLE_ARGS, (
                ('end', SINGLE_ARGS['start']), ('start', [0, 0, 1]), ('start', [0, math.inf]),
                ('start', [0, 10**1000]), ('overhang', -1), ('overhang', math.nan),
                ('overhang', math.inf), ('overhang', True), ('overhang', 100001))),
            (ANNOTATE, ANNOTATE_ARGS, (
                ('end_point', ANNOTATE_ARGS['start_point']), ('end_point', [0, 0, 1]),
                ('side', 'both'), ('side', []), ('start_label', ''), ('start_label', 'A B'),
                ('start_label', 'A\n'), ('start_label', 'Ａ'), ('start_label', '123456789'))),
        ):
            for key, value in invalid:
                with self.subTest(op=op, key=key, value=value), self.assertRaises(ValueError):
                    prepare(op, {**args, key: value})

    def test_single_axis_default_and_configured_overhang(self):
        for options, overhang, reverse in (({}, 1000, False), ({'overhang': 500}, 500, True)):
            with self.subTest(overhang=overhang):
                cad = AxisExtCad(SINGLE, {}, {'B0': single_line(overhang, reverse)})
                result = self.run_axis(cad, SINGLE, {**SINGLE_ARGS, **options})
                self.assertEqual(cad.events, [
                    ('osmode', 0), ('post', 'TSingleAxis\r'), ('click', 1328),
                    ('tab', 1331, str(overhang)), ('focus_view', 1100),
                    ('enter', '10000.0123,22000.0042'), ('enter', '13000.0123,26000.0042'),
                    ('esc', 2), ('osmode', 16383),
                ])
                self.assert_cleanup(cad)
                self.assertEqual(result['handles'], ['B0'])
                evidence = result['evidence']['single_axis']
                self.assertEqual(evidence['overhang'], overhang)
                self.assertAlmostEqual(evidence['line']['length'], 5000 + 2 * overhang)
                self.assertEqual(result['validation']['geometry'], 'verified_dote_lines')

    def test_single_axis_keeps_command_line_focus_after_a_coordinate_character(self):
        cad = AxisExtCad(SINGLE, {}, {'B0': single_line(1000)})
        backend = cad.backend
        send_char = backend.unicode

        def enter_char(ch):
            send_char(ch)
            if backend.focus == backend.view:
                backend.focus_command_line()

        with patch.object(backend, 'unicode', side_effect=enter_char):
            result = self.run_axis(cad, SINGLE, SINGLE_ARGS)
        self.assertEqual(backend.picks, ['10000.0123,22000.0042', '13000.0123,26000.0042'])
        self.assertEqual(cad.events.count(('focus_view', backend.view)), 1)
        self.assertEqual(result['handles'], ['B0'])
        self.assert_cleanup(cad)

    def test_single_axis_restores_frame_focus_before_the_second_point(self):
        cad = AxisExtCad(SINGLE, {}, {'B0': single_line(1000)})
        backend = cad.backend
        enter = backend.enter

        def enter_point():
            enter()
            if len(backend.picks) == 1:
                backend.focus = backend.hwnd

        with patch.object(backend, 'enter', side_effect=enter_point):
            result = self.run_axis(cad, SINGLE, SINGLE_ARGS)
        self.assertEqual(cad.events[4:8], [
            ('focus_view', backend.view), ('enter', '10000.0123,22000.0042'),
            ('focus_view', backend.view), ('enter', '13000.0123,26000.0042'),
        ])
        self.assertEqual(result['handles'], ['B0'])
        self.assert_cleanup(cad)

    def test_single_axis_keeps_owned_wpf_command_line_focus_between_points(self):
        cad = AxisExtCad(SINGLE, {}, {'B0': single_line(1000)})
        backend = cad.backend
        backend.float_command_line_on_type = True

        result = self.run_axis(cad, SINGLE, SINGLE_ARGS)

        self.assertEqual(backend.picks, ['10000.0123,22000.0042', '13000.0123,26000.0042'])
        self.assertEqual(cad.events.count(('focus_view', backend.view)), 1)
        self.assertEqual(result['handles'], ['B0'])
        self.assert_cleanup(cad)

    def test_single_axis_panel_timeout(self):
        cad = AxisExtCad(SINGLE, {}, {})
        cad.backend.caption = 'Unrelated panel'
        with patch.object(panel, '_wait', side_effect=lambda predicate, timeout: predicate()):
            with self.assertRaisesRegex(OutcomeUnknown, 'panel did not appear'):
                self.run_axis(cad, SINGLE, SINGLE_ARGS)
        self.assertEqual(cad.events[:2], [('osmode', 0), ('post', 'TSingleAxis\r')])
        self.assert_cleanup(cad)

    def test_single_axis_requires_post_command(self):
        cad = AxisExtCad(SINGLE, {}, {})
        cad.doc = SimpleNamespace(GetVariable=cad.GetVariable, SetVariable=cad.SetVariable,
                                  SendCommand=cad.SendCommand)
        with self.assertRaisesRegex(OutcomeUnknown, 'PostCommand'):
            self.run_axis(cad, SINGLE, SINGLE_ARGS)
        self.assertEqual(cad.events, [('osmode', 0), ('esc', 2), ('osmode', 16383)])
        self.assert_cleanup(cad)

    def test_single_axis_invalid_writeback(self):
        expected = single_line(1000)
        wrong_layer = single_line(1000)
        wrong_layer.Layer = '0'
        wrong_length = line(expected.StartPoint[:2], SINGLE_ARGS['end'])
        shifted = line(*(tuple(v + 0.2 for v in pt[:2])
                         for pt in (expected.StartPoint, expected.EndPoint)))
        for after, error in (
            ({'B0': wrong_layer}, 'AcDbLine on DOTE'),
            ({'B0': expected, 'B1': expected}, 'Expected 1 new axis lines'),
            ({'B0': wrong_length}, 'length mismatch'),
            ({'B0': shifted}, 'endpoint mismatch'),
        ):
            with self.subTest(error=error):
                cad = AxisExtCad(SINGLE, {}, after)
                with self.assertRaisesRegex(OutcomeUnknown, error):
                    self.run_axis(cad, SINGLE, SINGLE_ARGS)
                self.assert_cleanup(cad)

    def test_annotation_modes(self):
        for options, side, button, label, count in (
            ({}, 'double', 137, '1', 4),
            ({'side': 'single', 'start_label': '5'}, 'single', 136, '5', 2),
            ({'side': 'opposite', 'start_label': 'A'}, 'opposite', 142, 'A', 2),
        ):
            with self.subTest(side=side):
                existing = {'A0': single_line(1000)}
                cad = AxisExtCad(ANNOTATE, existing, {**existing, **annotation_entities(count)})
                result = self.run_axis(cad, ANNOTATE, {**ANNOTATE_ARGS, **options})
                self.assertEqual(cad.events, [
                    ('osmode', 0), ('send', 'TMultAxisDim\r'), ('click', button),
                    ('tab', 141, label), ('focus_view', 1100),
                    ('enter', '10000.0000,22000.0000'), ('enter', '17800.0000,22000.0000'),
                    ('enter', ''), ('esc', 2), ('osmode', 16383),
                ])
                self.assert_cleanup(cad)
                dims = [f'D{i}' for i in range(count)]
                self.assertEqual(result['handles'], ['FF', *dims])
                evidence = result['evidence']['axis_annotate']
                self.assertEqual((evidence['side'], evidence['start_label']), (side, label))
                self.assertEqual(evidence['label_set_handle'], 'FF')
                self.assertEqual(evidence['dimension_handles'], dims)
                self.assertEqual(result['validation'], {
                    'type': True, 'properties': 'layer_and_counts_verified',
                    'geometry': 'label_text_unreadable_without_explode', 'reopen': 'pending',
                })

    def test_annotation_keeps_command_line_focus_through_final_confirmation(self):
        cad = AxisExtCad(ANNOTATE, {}, annotation_entities(4))
        backend = cad.backend
        enter = backend.enter

        def enter_point():
            enter()
            if len(backend.picks) == 1:
                backend.focus_command_line()

        with patch.object(backend, 'enter', side_effect=enter_point):
            result = self.run_axis(cad, ANNOTATE, ANNOTATE_ARGS)
        self.assertEqual(backend.picks, ['10000.0000,22000.0000', '17800.0000,22000.0000', ''])
        self.assertEqual(cad.events.count(('focus_view', backend.view)), 1)
        self.assertEqual(result['handles'], ['FF', 'D0', 'D1', 'D2', 'D3'])
        self.assert_cleanup(cad)

    def test_annotation_rejected_label_readback(self):
        cad = AxisExtCad(ANNOTATE, {}, {})
        cad.backend.reject_edit = True
        with self.assertRaisesRegex(OutcomeUnknown, 'label mismatch'):
            self.run_axis(cad, ANNOTATE, ANNOTATE_ARGS)
        self.assertEqual(cad.backend.picks, [])
        self.assert_cleanup(cad)

    def test_annotation_keeps_owned_wpf_command_line_focus_through_final_confirmation(self):
        cad = AxisExtCad(ANNOTATE, {}, annotation_entities(4))
        backend = cad.backend
        backend.float_command_line_on_type = True

        result = self.run_axis(cad, ANNOTATE, ANNOTATE_ARGS)

        self.assertEqual(backend.picks, ['10000.0000,22000.0000', '17800.0000,22000.0000', ''])
        self.assertEqual(cad.events.count(('focus_view', backend.view)), 1)
        self.assertEqual(result['handles'], ['FF', 'D0', 'D1', 'D2', 'D3'])
        self.assert_cleanup(cad)

    def test_annotation_invalid_writeback(self):
        for failure, error in (
            ('extra_label', 'found 2 label sets'), ('dimension_count', 'and 2 dimensions'),
            ('wrong_layer', 'unexpected entity'), ('unknown_type', 'unexpected entity'),
            ('removed', 'removed existing entities'),
        ):
            with self.subTest(failure=failure):
                before = {'A0': single_line(1000)}
                after = {**before, **annotation_entities(2 if failure == 'dimension_count' else 4)}
                if failure == 'extra_label':
                    after['FE'] = after['FF']
                elif failure == 'wrong_layer':
                    after['D0'].Layer = 'DOTE'
                elif failure == 'unknown_type':
                    after['D0'].ObjectName = 'AcDbText'
                elif failure == 'removed':
                    del after['A0']
                cad = AxisExtCad(ANNOTATE, before, after)
                with self.assertRaisesRegex(OutcomeUnknown, error):
                    self.run_axis(cad, ANNOTATE, ANNOTATE_ARGS)
                self.assert_cleanup(cad)


if __name__ == '__main__':
    unittest.main()

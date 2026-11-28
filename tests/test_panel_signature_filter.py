import unittest
from unittest.mock import patch

from test_panel import FakeBackend, fake_session
from tz_mcp import panel


HELP_DIALOG = 400
PLACEMENT_PANEL = 555


class PanelSignatureTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.session = fake_session()
        self.panels = self.enterContext(patch.object(panel, 'panels', side_effect=[
            [HELP_DIALOG],
            [HELP_DIALOG, PLACEMENT_PANEL],
            [HELP_DIALOG, PLACEMENT_PANEL],
            [HELP_DIALOG, PLACEMENT_PANEL],
            [HELP_DIALOG],
            [HELP_DIALOG],
        ]))
        self.set_field = self.enterContext(patch.object(panel, 'set_field'))
        self.enterContext(patch.object(panel, 'viewport', return_value=(0, 0, 100, 100)))
        self.enterContext(patch.object(panel.time, 'sleep'))

    def assert_placement(self, result, command, fields):
        self.assertEqual(PLACEMENT_PANEL, result['panel'])
        self.assertEqual((1200.0, 700.0), result['world'])
        self.assertEqual(list(command),
                         [call[1] for call in self.backend.calls if call[0] == 'char'])
        self.assertEqual([('click', 50, 50), ('click', 70, 30), ('esc', 2)],
                         [call for call in self.backend.calls if call[0] in ('click', 'esc')])
        self.assertEqual(fields, [call.args[1:] for call in self.set_field.call_args_list])
        self.assertEqual(6, self.panels.call_count)

    def test_column_waits_for_all_rows_with_help_dialog_present(self):
        edits = {'横向': 11, '纵向': 12, '柱高': 13}

        def field_edit(u, hwnd, label):
            if hwnd == HELP_DIALOG:
                return edits['横向'] if label == '横向' else None
            if self.panels.call_count == 2 and label == '柱高':
                return None
            return edits[label]

        with patch.object(panel, 'field_edit', side_effect=field_edit):
            result = panel.create_column(
                self.session, {'point': [1200, 700], 'width': 700, 'depth': 500, 'height': 3200},
                backend=self.backend,
            )

        self.assert_placement(result, 'TGColumn', [(11, 700), (12, 500), (13, 3200)])

    def assert_opening_placement(self, kind, command, width, height, sill):
        def value_row_edit(u, hwnd, index):
            if hwnd == HELP_DIALOG:
                return None
            return 20 + index

        def opening_kind(u, hwnd):
            if hwnd != PLACEMENT_PANEL or self.panels.call_count == 2:
                return None
            return kind

        with (
            patch.object(panel, 'opening_kind', side_effect=opening_kind),
            patch.object(panel, 'value_row_edit', side_effect=value_row_edit),
        ):
            result = panel.create_opening(
                self.session, {'point': [1200, 700], 'kind': kind, 'width': width,
                               'height': height, 'sill': sill},
                backend=self.backend,
            )

        self.assertEqual(kind, result['kind'])
        self.assert_placement(result, command, [(20, width), (21, height), (22, sill)])

    def test_door_waits_for_value_row_with_help_dialog_present(self):
        self.assert_opening_placement('普通门', 'TGDOOR', 900, 2100, 0)

    def test_window_waits_for_value_row_with_help_dialog_present(self):
        self.assert_opening_placement('普通窗', 'TGWINDOW', 1200, 1600, 600)

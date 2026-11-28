import sys
import unittest
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock, call, patch

from tz_mcp import parts
from tz_mcp.cad import Cad, invoke_document, property_write


CALL_REJECTED = -2147418111
RETRY_LATER = -2147417846
UNKNOWN_NAME = -2147352570
TYPE_MISMATCH = -2147352571
E_FAIL = -2147467259
PROPERTY_PUT = 4
DISPID = 123


class ComError(Exception):
    def __init__(self, hresult):
        super().__init__(hresult)
        self.hresult = hresult


class OfflineComTests(unittest.TestCase):
    def setUp(self):
        pythoncom = SimpleNamespace(DISPATCH_PROPERTYPUT=PROPERTY_PUT)
        self.enterContext(patch.dict(sys.modules, {'pythoncom': pythoncom}))
        self.sleep = self.enterContext(patch('tz_mcp.cad.time.sleep'))


class PropertyWriteTests(OfflineComTests):
    def test_retries_both_busy_codes_during_lookup_and_property_put(self):
        ole = Mock()
        ole.GetIDsOfNames.side_effect = [ComError(CALL_REJECTED), ComError(RETRY_LATER), DISPID]
        ole.Invoke.side_effect = [ComError(RETRY_LATER), ComError(CALL_REJECTED), None]

        property_write(SimpleNamespace(_oleobj_=ole), 'TextString', 'Updated')

        self.assertEqual(
            [call.GetIDsOfNames('TextString')] * 3
            + [call.Invoke(DISPID, 0, PROPERTY_PUT, 0, 'Updated')] * 3,
            ole.mock_calls,
        )
        self.assertEqual(4, self.sleep.call_count)

    def test_accepts_scalar_tuple_and_list_dispids(self):
        for result in (DISPID, (DISPID,), [DISPID]):
            with self.subTest(result=result):
                ole = Mock()
                ole.GetIDsOfNames.return_value = result
                entity = SimpleNamespace(_oleobj_=ole, Width=900.0)

                property_write(entity, 'Width', 1000.0)

                self.assertEqual(
                    [call.GetIDsOfNames('Width'), call.Invoke(DISPID, 0, PROPERTY_PUT, 0, 1000.0)],
                    ole.mock_calls,
                )
                self.assertEqual(900.0, entity.Width)

    def test_propagates_non_busy_errors_without_retrying(self):
        for method, hresult in (
            ('GetIDsOfNames', UNKNOWN_NAME),
            ('GetIDsOfNames', E_FAIL),
            ('Invoke', TYPE_MISMATCH),
            ('Invoke', E_FAIL),
        ):
            with self.subTest(method=method, hresult=hresult):
                ole = Mock()
                ole.GetIDsOfNames.return_value = DISPID
                error = ComError(hresult)
                getattr(ole, method).side_effect = error

                with self.assertRaises(ComError) as caught:
                    property_write(SimpleNamespace(_oleobj_=ole), 'Width', 1000.0)

                self.assertIs(error, caught.exception)
                ole.GetIDsOfNames.assert_called_once_with('Width')
                if method == 'GetIDsOfNames':
                    ole.Invoke.assert_not_called()
                else:
                    ole.Invoke.assert_called_once_with(DISPID, 0, PROPERTY_PUT, 0, 1000.0)
                self.sleep.assert_not_called()

    def test_honors_expired_retry_budget_at_either_com_step(self):
        for method in ('GetIDsOfNames', 'Invoke'):
            with self.subTest(method=method):
                ole = Mock()
                ole.GetIDsOfNames.return_value = DISPID
                error = ComError(CALL_REJECTED)
                step = getattr(ole, method)
                step.side_effect = error

                with self.assertRaises(ComError) as caught:
                    property_write(SimpleNamespace(_oleobj_=ole), 'Height', 3600.0, timeout=0)

                self.assertIs(error, caught.exception)
                step.assert_called_once()
                self.sleep.assert_not_called()

    def test_uses_setattr_without_importing_pythoncom_for_plain_entities(self):
        class FakeEntity:
            def __setattr__(self, name, value):
                super().__setattr__(name, value.upper())

        entity = FakeEntity()
        with patch.dict(sys.modules, {'pythoncom': None}):
            property_write(entity, 'TextString', 'updated')

        self.assertEqual('UPDATED', entity.TextString)
        self.sleep.assert_not_called()

    def test_update_retries_member_lookup_and_rejected_invocation(self):
        update = Mock(side_effect=[ComError(CALL_REJECTED), None])
        lookup = Mock(side_effect=[AttributeError('<unknown>.Update'), update, update])

        class FakeEntity:
            @property
            def Update(self):
                return lookup()

        entity = FakeEntity()
        invoke_document(lambda: entity, 'Update')

        self.assertEqual([call()] * 3, lookup.call_args_list)
        self.assertEqual([call()] * 2, update.call_args_list)
        self.assertEqual(2, self.sleep.call_count)


class BusyEditEntity:
    def __init__(self, handle, object_name, edit_property, *, bounds=None, **properties):
        self.Handle = handle
        self.ObjectName = object_name
        self.bounds = bounds
        self.properties = properties
        self.edit_property = edit_property
        self.on_update = Mock()
        self.read = Mock(
            side_effect=[ComError(CALL_REJECTED), DEFAULT, ComError(CALL_REJECTED), DEFAULT],
            wraps=lambda: self.properties[edit_property],
        )
        self.Update = Mock(side_effect=[ComError(CALL_REJECTED), DEFAULT], wraps=self._update)
        self._oleobj_ = Mock()
        self._oleobj_.GetIDsOfNames.side_effect = [ComError(CALL_REJECTED), DISPID]
        self._oleobj_.Invoke = Mock(
            side_effect=[ComError(CALL_REJECTED), DEFAULT], wraps=self._put,
        )

    def __getattr__(self, name):
        if name == self.edit_property:
            return self.read()
        if name in self.properties:
            return self.properties[name]
        raise AttributeError(name)

    def _put(self, dispid, lcid, flags, want_result, value):
        self.properties[self.edit_property] = value

    def _update(self):
        self.on_update()


class FakeCad:
    document_id = 'doc'
    hwnd = 42
    doc = object()

    def __init__(self, *entities):
        self.store = {entity.Handle: entity for entity in entities}

    def guard(self):
        pass

    def entity(self, handle, document_id):
        return self.store[handle]

    def entities(self):
        return self.store.copy()

    def describe(self, entity):
        return {'handle': entity.Handle, 'object_name': entity.ObjectName,
                'bounds': entity.bounds, **entity.properties}


class EditRetryTests(OfflineComTests):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'require_model_space'))

    def run_edit(self, cad, op, entity, **changes):
        result = Cad.execute(cad, op, {
            'document_id': cad.document_id, 'handle': entity.Handle, **changes,
        })
        self.assertTrue(result['success'])
        self.assertEqual([entity.Handle], result['handles'])
        return result['evidence']['edit']

    def assert_edit_retried(self, edit, entity, before, after):
        name = entity.edit_property
        self.assertEqual(before, edit['properties_before'][name])
        self.assertEqual(after, edit['properties_after'][name])
        self.assertEqual(4, entity.read.call_count)
        self.assertEqual([call(name)] * 2, entity._oleobj_.GetIDsOfNames.call_args_list)
        self.assertEqual([call(DISPID, 0, PROPERTY_PUT, 0, after)] * 2,
                         entity._oleobj_.Invoke.call_args_list)
        self.assertEqual([call()] * 2, entity.Update.call_args_list)
        entity.on_update.assert_called_once_with()

    def test_text_edit_retries_text_reads_write_and_update(self):
        text = BusyEditEntity('3D4', 'AcDbText', 'TextString', TextString='Original',
                              Height=250.0, Rotation=0.0)

        def read_dxf(_cad, _handle):
            return {'type': 'TEXT', 'text': text.properties['TextString'],
                    'height': 250.0, 'rotation': 0.0}

        with patch.object(parts, 'text_dxf', side_effect=read_dxf):
            edit = self.run_edit(FakeCad(text), 'annotation.text_edit', text, text='Updated')

        self.assert_edit_retried(edit, text, 'Original', 'Updated')

    def test_opening_edit_retries_width_reads_write_and_update(self):
        opening = BusyEditEntity('3CF', 'TDbOpening', 'Width', Width=900.0, Height=2100.0,
                                 WinSill=0.0, bounds=((2550, -120, 0), (3450, 120, 2100)))
        wall = SimpleNamespace(Handle='35A', ObjectName='TDbWall', HoleArea=1.89,
                               properties={'LeftWidth': 120.0, 'RightWidth': 120.0},
                               bounds=((0, -120, 0), (6000, 120, 3000)))

        def update_hole_area():
            wall.HoleArea = opening.properties['Width'] * opening.Height / 1_000_000

        def read_dxf(_cad, _handle):
            return {'type': 'TCH_OPENING', 'width': opening.properties['Width'],
                    'height': opening.Height, 'insertion': (3000.0, 0.0)}

        opening.on_update.side_effect = update_hole_area
        with patch.object(parts, 'opening_dxf', side_effect=read_dxf), patch.object(
            parts, 'entity_wall_hosts', return_value=[{'handle': '35A', 'type': 'TCH_WALL'}],
        ):
            edit = self.run_edit(FakeCad(opening, wall), 'opening.edit', opening, width=1000.0)

        self.assert_edit_retried(edit, opening, 900.0, 1000.0)
        self.assertAlmostEqual(0.21, edit['expected_hole_area_delta'])
        self.assertAlmostEqual(2.10, edit['host_hole_area_after'])

    def test_wall_edit_retries_height_reads_write_and_update(self):
        wall = BusyEditEntity('35A', 'TDbWall', 'Height', LeftWidth=120.0, RightWidth=120.0,
                              Height=3000.0, HoleArea=1.89)

        edit = self.run_edit(FakeCad(wall), 'wall.edit', wall, height=3600.0)

        self.assert_edit_retried(edit, wall, 3000.0, 3600.0)
        self.assertEqual(1.89, edit['hole_area_after'])

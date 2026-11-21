import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tz_mcp import cad as cadmod
from tz_mcp import cli
from tz_mcp.commands import MAX_LIST_LIMIT, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS, Engine
from tz_mcp.store import Store

PATH = 'C:\\CAD绘图\\job-a.dwg'


def write_dwg(path, payload=b'AC1032'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class Collection:
    def __init__(self, items=()):
        self.items = list(items)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index]


class Entity:
    """Stand-in for a ModelSpace entity; records how often it is described."""

    def __init__(self, handle, object_name='TDbWall', kinds=None, fail_handle=False):
        self.handle_value = handle
        self.ObjectName = object_name
        self.ObjectID = int(handle, 16)
        self.Width = 200
        self.bounds_calls = 0
        self.fail_handle = fail_handle
        for key, value in (kinds or {}).items():
            setattr(self, key, value)
        self._oleobj_ = SimpleNamespace(GetBoundingBox=self._bounds)

    @property
    def Handle(self):
        if self.fail_handle:
            raise RuntimeError('handle read failed')
        return self.handle_value

    def _bounds(self):
        self.bounds_calls += 1
        return ((0.0, 0.0, 0.0), (1000.0, 200.0, 0.0))


class FakeDoc:
    def __init__(self, path, read_only=True, entities=(), fail_close=False):
        self.FullName = str(path)
        self.ReadOnly = read_only
        self.Saved = True
        self.ModelSpace = Collection(entities)
        self.close_calls = []
        self.fail_close = fail_close

    def Close(self, save_changes):
        self.close_calls.append(save_changes)
        if self.fail_close:
            raise RuntimeError('close refused')

    def Save(self):
        raise AssertionError('discovery must not save')

    def SaveAs(self, path):
        raise AssertionError('discovery must not save')

    def SendCommand(self, payload):
        raise AssertionError('discovery must not drive commands')


class FakeDocuments(Collection):
    def __init__(self, items=(), opened=None, fail_open=False):
        Collection.__init__(self, items)
        self.open_calls = []
        self.opened = opened
        self.fail_open = fail_open

    def Open(self, path, read_only=False):
        self.open_calls.append((str(path), read_only))
        if self.fail_open:
            raise RuntimeError('open refused')
        return self.opened


class FakeApp:
    def __init__(self, documents, hwnd=100):
        self.Documents = documents
        self.HWND = hwnd

    @property
    def ActiveDocument(self):
        raise AssertionError('discovery must never use ActiveDocument')


def fake_win32_modules():
    dynamic = types.ModuleType('win32com.client.dynamic')
    client = types.ModuleType('win32com.client')
    client.dynamic = dynamic
    client.Dispatch = lambda obj: obj
    win32com = types.ModuleType('win32com')
    win32com.client = client
    return {'win32com': win32com, 'win32com.client': client,
            'win32com.client.dynamic': dynamic}


class DiscoveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'CAD绘图'
        self.outputs = self.root / 'tianzheng-mcp' / 'outputs'
        self.target = write_dwg(self.outputs / 'job-a.dwg')
        for patcher in (mock.patch.object(cadmod, 'ROOT', self.root),
                        mock.patch.object(cadmod, 'OUTPUTS', self.outputs),
                        mock.patch.object(cadmod, 'late', lambda obj: obj),
                        mock.patch.dict(sys.modules, fake_win32_modules())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def adapter(self, app):
        cad = cadmod.Cad.__new__(cadmod.Cad)
        cad.app, cad.hwnd, cad.pid = app, 100, 7
        cad.doc = cad.owned_doc = cad.document_id = cad.path = None
        return cad

    def run_op(self, op, args, app):
        cad = self.adapter(app)
        with mock.patch.object(cadmod, 'active_object', lambda *a, **k: app), \
             mock.patch.object(cadmod, 'window_pid', lambda hwnd: 7), \
             mock.patch.object(cadmod, 'connect',
                               side_effect=AssertionError('must not reconnect')):
            return cad.execute(op, args)

    def app_with(self, entities, **doc):
        doc = FakeDoc(self.target, entities=entities, **doc)
        documents = FakeDocuments(opened=doc)
        return FakeApp(documents), documents, doc


class ListArgumentTests(unittest.TestCase):
    def test_limit_boundaries_and_default(self):
        self.assertEqual(100, MAX_LIST_LIMIT)
        self.assertEqual(100, prepare('document.list', {'path': PATH})['limit'])
        for limit in (1, 2, MAX_LIST_LIMIT):
            self.assertEqual(limit, prepare('document.list',
                                            {'path': PATH, 'limit': limit})['limit'])
        for limit in (True, False, 0, MAX_LIST_LIMIT + 1, 1.5, '10', None, [1]):
            with self.assertRaises(ValueError):
                prepare('document.list', {'path': PATH, 'limit': limit})

    def test_after_handle_must_be_bare_hexadecimal(self):
        for handle in ('0x10', '', 'ZZ', ' 10', '10 ', 16, None, '1G'):
            with self.assertRaises(ValueError):
                prepare('document.list', {'path': PATH, 'after_handle': handle})
        for handle in ('2', 'a', 'A', '10', '35A'):
            args = prepare('document.list', {'path': PATH, 'after_handle': handle})
            self.assertEqual(handle, args['after_handle'])

    def test_path_and_schema_rejections(self):
        with self.assertRaises(ValueError):
            prepare('document.list', {})
        for path in ('', '   ', 'relative.dwg', 'C:\\CAD绘图\\job-a.dxf', 5, None):
            with self.assertRaises(ValueError):
                prepare('document.list', {'path': path})
        with self.assertRaises(ValueError):
            prepare('document.list', {'path': PATH, 'handles': ['1']})
        with self.assertRaises(ValueError):
            prepare('document.list', {'path': PATH, 'lisp': '(quit)'})


class ListReadbackTests(DiscoveryBase):
    def test_empty_drawing_reports_no_entities(self):
        app, documents, doc = self.app_with([])
        result = self.run_op('document.list', {'path': str(self.target)}, app)
        self.assertEqual(str(self.target), result['path'])
        self.assertEqual([], result['entities'])
        self.assertEqual(0, result['matched_count'])
        self.assertIsNone(result['next_after_handle'])
        self.assertEqual([(str(self.target), True)], documents.open_calls)
        self.assertEqual([False], doc.close_calls)

    def test_handles_are_ordered_numerically_not_lexically(self):
        app, documents, doc = self.app_with([Entity('10'), Entity('2'), Entity('A')])
        result = self.run_op('document.list', {'path': str(self.target)}, app)
        self.assertEqual(['2', 'A', '10'], [item['handle'] for item in result['entities']])
        self.assertEqual(3, result['matched_count'])
        self.assertIsNone(result['next_after_handle'])

    def test_pagination_is_exclusive_and_lossless(self):
        handles = ['1', '2', '3', 'A', 'B', '10', '11']
        app, documents, doc = self.app_with([Entity(handle) for handle in handles])
        seen, pages = [], 0
        cursor = None
        while True:
            args = {'path': str(self.target), 'limit': 3}
            if cursor is not None:
                args['after_handle'] = cursor
            result = self.run_op('document.list', args, app)
            pages += 1
            page = [item['handle'] for item in result['entities']]
            self.assertLessEqual(len(page), 3)
            self.assertEqual(len(handles), result['matched_count'])
            self.assertFalse(set(page) & set(seen))
            seen.extend(page)
            cursor = result['next_after_handle']
            if cursor is None:
                break
        self.assertEqual(handles, seen)
        self.assertEqual(3, pages)
        self.assertEqual(pages, len(doc.close_calls))

    def test_exact_page_boundary_and_cursor_past_end(self):
        app, documents, doc = self.app_with([Entity('1'), Entity('2')])
        first = self.run_op('document.list', {'path': str(self.target), 'limit': 2}, app)
        self.assertEqual(['1', '2'], [item['handle'] for item in first['entities']])
        self.assertIsNone(first['next_after_handle'])
        tail = self.run_op('document.list',
                           {'path': str(self.target), 'after_handle': 'FFFF'}, app)
        self.assertEqual([], tail['entities'])
        self.assertEqual(2, tail['matched_count'])
        self.assertIsNone(tail['next_after_handle'])

    def test_only_the_page_entities_are_described(self):
        entities = [Entity(handle) for handle in ('1', '2', '3', '4', '5')]
        app, documents, doc = self.app_with(entities)
        result = self.run_op('document.list',
                             {'path': str(self.target), 'limit': 2}, app)
        self.assertEqual(['1', '2'], [item['handle'] for item in result['entities']])
        self.assertEqual([1, 1, 0, 0, 0], [entity.bounds_calls for entity in entities])

    def test_discovered_handles_can_be_read_back(self):
        entities = [Entity('35A', 'TDbWall'),
                    Entity('3C8', 'TDbOpening',
                           kinds={'GetKind': '普通门', 'GetSubKind': '普通门'}),
                    Entity('409', 'TDbOpening')]
        app, documents, doc = self.app_with(entities)
        found = self.run_op('document.list', {'path': str(self.target)}, app)
        handles = [item['handle'] for item in found['entities']]
        self.assertEqual(['35A', '3C8', '409'], handles)
        reply = self.run_op('document.readback',
                            {'path': str(self.target), 'handles': handles}, app)
        self.assertEqual(handles, reply['handles'])
        self.assertEqual(['TDbWall', 'TDbOpening', 'TDbOpening'],
                         [item['object_name'] for item in reply['entities']])
        self.assertEqual('普通门', reply['entities'][1]['GetKind'])
        self.assertEqual('普通门', reply['entities'][1]['GetSubKind'])
        self.assertNotIn('GetKind', reply['entities'][2])
        self.assertTrue(all(item['document_id'] is None for item in reply['entities']))
        self.assertNotIn('host', reply['entities'][1])

    def test_read_failure_still_closes_the_document(self):
        app, documents, doc = self.app_with([Entity('1'), Entity('2', fail_handle=True)])
        with self.assertRaises(RuntimeError):
            self.run_op('document.list', {'path': str(self.target)}, app)
        self.assertEqual([False], doc.close_calls)

    def test_unsafe_close_is_reported_as_unknown(self):
        app, documents, doc = self.app_with([Entity('1')], fail_close=True)
        with self.assertRaises(cadmod.OutcomeUnknown) as caught:
            self.run_op('document.list', {'path': str(self.target)}, app)
        self.assertIn('close outcome is unknown', str(caught.exception))

    def test_open_failure_is_unknown(self):
        documents = FakeDocuments(fail_open=True)
        app = FakeApp(documents)
        with self.assertRaises(cadmod.OutcomeUnknown) as caught:
            self.run_op('document.list', {'path': str(self.target)}, app)
        self.assertIn('Read-only open outcome is unknown', str(caught.exception))

    def test_read_only_flag_mismatch_is_unknown(self):
        app, documents, doc = self.app_with([Entity('1')], read_only=False)
        with self.assertRaises(cadmod.OutcomeUnknown):
            self.run_op('document.list', {'path': str(self.target)}, app)

    def test_already_open_target_is_refused(self):
        opened = FakeDoc(self.target, entities=[Entity('1')])
        documents = FakeDocuments(items=[opened], opened=opened)
        app = FakeApp(documents)
        with self.assertRaises(ValueError) as caught:
            self.run_op('document.list', {'path': str(self.target)}, app)
        self.assertIn('already open', str(caught.exception))
        self.assertEqual([], documents.open_calls)
        self.assertEqual([], opened.close_calls)

    def test_target_outside_outputs_is_refused(self):
        outside = write_dwg(self.root / 'user.dwg')
        app, documents, doc = self.app_with([Entity('1')])
        with self.assertRaises(ValueError) as caught:
            self.run_op('document.list', {'path': str(outside)}, app)
        self.assertIn('restricted', str(caught.exception))
        self.assertEqual([], documents.open_calls)

    def test_missing_target_is_refused(self):
        app, documents, doc = self.app_with([Entity('1')])
        with self.assertRaises(ValueError):
            self.run_op('document.list',
                        {'path': str(self.outputs / 'absent.dwg')}, app)


class ListIntegrationTests(unittest.TestCase):
    def test_document_list_joins_the_no_escape_steps(self):
        self.assertIn('document.list', NO_ESCAPE_STEPS)

    def test_capabilities_expose_document_list_as_verified_live(self):
        caps = cli.capabilities()
        self.assertIn('document.list', caps)
        self.assertEqual(['path'], caps['document.list']['required'])
        self.assertEqual(['after_handle', 'limit'], caps['document.list']['optional'])
        self.assertTrue(caps['document.list']['validation'].startswith('verified_live'))


class ListWatchdogTests(unittest.TestCase):
    def test_timeout_quarantines_without_sending_escape(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / 'queue.sqlite3')
        job = store.submit({'steps': [{'id': 'scan', 'op': 'document.list',
                                       'args': {'path': PATH}, 'timeout_s': 0.03}]})

        class Slow:
            def execute(self, op, args):
                time.sleep(0.15)
                return {}

        class Recovery:
            escapes = 0

            def modal(self):
                return False

            def escape(self):
                Recovery.escapes += 1
                return {'posted': 1}

        recovery = Recovery()
        engine = Engine(store, Slow(), recovery, poll=0.005)
        engine.run_one()
        result = store.get(job)
        self.assertEqual('unknown_outcome', result['state'])
        self.assertTrue(engine.quarantined)
        self.assertEqual(0, recovery.escapes)
        self.assertIn('no foreground command', result.get('error') or '')
        self.assertFalse(engine.run_one())


if __name__ == '__main__':
    unittest.main()

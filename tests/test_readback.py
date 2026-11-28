"""v1.1 cross-job read-only DWG readback and explicit document.close.

All COM objects here are local fakes.  Nothing in this file starts AutoCAD or
touches the production queue/outputs; paths are redirected to a temp dir that
looks like C:/CADWorkspace/tianzheng-mcp/outputs.
"""
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tz_mcp import cad as cadmod
from tz_mcp.cli import capabilities
from tz_mcp.commands import prepare
from tz_mcp.engine import Engine
from tz_mcp.store import Store


def write_dwg(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'AC1032')


class Collection:
    def __init__(self, items=()):
        self.items = list(items)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index]


class Entity:
    def __init__(self, handle='A1', object_name='TDbWall'):
        self.Handle = handle
        self.ObjectName = object_name
        self.ObjectID = 42
        self.Width = 200
        self._oleobj_ = types.SimpleNamespace(
            GetBoundingBox=lambda: ((0.0, 0.0, 0.0), (4000.0, 0.0, 3000.0)))


class FakeIdentity:
    """Stable COM IUnknown identity returned by a document proxy's QI."""

    def __init__(self):
        self.queries = []
        self.fail = False

    def QueryInterface(self, iid):
        self.queries.append(iid)
        if self.fail:
            raise RuntimeError('COM identity unavailable')
        return self


class FakeOle:
    def __init__(self, identity):
        self.identity = identity

    def QueryInterface(self, iid):
        return self.identity.QueryInterface(iid)


class FakeDoc:
    def __init__(self, path, read_only=True, entities=(), fail_close=False,
                 identity=None):
        self.FullName = str(path)
        self.ReadOnly = read_only
        self.Saved = True
        self.ModelSpace = Collection(entities)
        self.close_calls = []
        self.fail_close = fail_close
        self.identity = identity or FakeIdentity()
        self._oleobj_ = FakeOle(self.identity)

    def Close(self, save_changes):
        self.close_calls.append(save_changes)
        if self.fail_close:
            raise RuntimeError('close call did not return')

    def Save(self):
        raise AssertionError('readback/close must not Save')

    def SaveAs(self, *args):
        raise AssertionError('readback/close must not SaveAs')

    def SendCommand(self, *args):
        raise AssertionError('readback/close must not SendCommand')


class FakeDocuments(Collection):
    def __init__(self, items=(), opened=None):
        super().__init__(items)
        self.open_calls = []
        self.opened = opened
        self.fail_open = False

    def Open(self, path, read_only=False):
        self.open_calls.append((path, read_only))
        if self.fail_open:
            raise RuntimeError('open call did not return')
        document = self.opened or FakeDoc(path, True)
        self.items.append(document)
        return document


class NewDocuments(FakeDocuments):
    def __init__(self, created):
        super().__init__()
        self.created = created
        self.add_calls = []

    def Add(self, template):
        self.add_calls.append(template)
        self.items.append(self.created)
        return self.created


class MutableDoc(FakeDoc):
    """A document that can be created/reopened by document.new/reopen."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0
        self.save_as_calls = []

    def Save(self):
        self.save_calls += 1

    def SaveAs(self, path):
        self.save_as_calls.append(path)
        write_dwg(Path(path))

    def GetVariable(self, name):
        return {'CMDACTIVE': 0, 'UCSORG': (0, 0, 0),
                'UCSXDIR': (1, 0, 0), 'UCSYDIR': (0, 1, 0)}[name]


class NewApp:
    HWND = 100
    Version = '24.3s'

    def __init__(self, documents):
        self.Documents = documents

    def ListArx(self):
        return ['tch_kernal.arx']


class FakeApp:
    HWND = 100

    def __init__(self, documents):
        self.Documents = documents

    @property
    def ActiveDocument(self):
        raise AssertionError('readback must not use ActiveDocument as data source')


def fake_win32_modules():
    win32com = types.ModuleType('win32com')
    client = types.ModuleType('win32com.client')
    dynamic = types.ModuleType('win32com.client.dynamic')
    client.dynamic = dynamic
    client.Dispatch = lambda obj: obj
    win32com.client = client
    return {'win32com': win32com, 'win32com.client': client,
            'win32com.client.dynamic': dynamic}


class FakeRecovery:
    def __init__(self):
        self.escapes = 0
        self.is_modal = False

    def modal(self):
        return self.is_modal

    def escape(self):
        self.escapes += 1
        return {'posted': 1}


class TempOutputsTest(unittest.TestCase):
    """Routes both the general DWG root and the v1.1 outputs root to temp."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'CAD绘图'
        self.outputs = self.root / 'tianzheng-mcp' / 'outputs'
        self.outputs.mkdir(parents=True)
        self.target = self.outputs / 'job-a.dwg'
        write_dwg(self.target)
        for patcher in (
                patch.object(cadmod, 'ROOT', self.root),
                patch.object(cadmod, 'OUTPUTS', self.outputs),
                patch.object(cadmod, 'late', lambda obj: obj),
                patch.dict(sys.modules, fake_win32_modules())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cad_with(self, app, pid=7, hwnd=100):
        cad = cadmod.Cad.__new__(cadmod.Cad)
        cad.app, cad.hwnd, cad.pid = app, hwnd, pid
        cad.doc, cad.path, cad.document_id = None, None, None
        cad.owned_doc = None
        return cad


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'queue.sqlite3')

    def submit(self, op, args):
        return self.store.submit({'steps': [{'id': 'step', 'op': op, 'args': args}]})

    def test_close_is_registered_with_no_arguments(self):
        self.assertEqual({}, prepare('document.close', {}))
        job = self.submit('document.close', {})
        self.assertEqual('document.close', self.store.get(job)['recipe']['steps'][0]['op'])
        with self.assertRaises(ValueError):
            prepare('document.close', {'path': 'D:/x.dwg'})
        with self.assertRaises(ValueError):
            self.submit('document.close', {'path': 'D:/x.dwg'})

    def test_readback_requires_absolute_shape_and_hex_handles(self):
        good = {'path': 'C:/CADWorkspace/tianzheng-mcp/outputs/a.dwg', 'handles': ['A1', 'b2']}
        self.assertEqual(good, prepare('document.readback', good))
        for bad in ({'handles': ['A1']},
                    {'path': good['path']},
                    {'path': good['path'], 'handles': []},
                    {'path': good['path'], 'handles': 'A1'},
                    {'path': good['path'], 'handles': ['G1']},
                    {'path': good['path'], 'handles': [1]},
                    {'path': '', 'handles': ['A1']},
                    {'path': 'relative/a.dwg', 'handles': ['A1']},
                    {'path': 'C:/CADWorkspace/tianzheng-mcp/outputs/a.txt', 'handles': ['A1']},
                    {'path': good['path'], 'handles': ['A1'], 'extra': True}):
            with self.assertRaises(ValueError):
                prepare('document.readback', bad)
            with self.assertRaises(ValueError):
                self.submit('document.readback', bad)

    def test_readback_has_a_reasonable_handle_upper_limit(self):
        path = 'C:/CADWorkspace/tianzheng-mcp/outputs/a.dwg'
        self.store.submit({'steps': [{'id': 'ok', 'op': 'document.readback',
                                      'args': {'path': path,
                                               'handles': [format(i, 'X') for i in range(1, 101)]}}]})
        with self.assertRaises(ValueError):
            self.submit('document.readback',
                        {'path': path, 'handles': [format(i, 'X') for i in range(1, 102)]})

    def test_capability_states_are_verified_live_validation(self):
        caps = capabilities()
        for op in ('document.close', 'document.readback'):
            self.assertIn(op, caps)
            self.assertIn('verified_live', caps[op]['validation'])
            self.assertNotIn('pending_live', caps[op]['validation'])

    def test_old_recipe_with_same_job_reopen_and_get_still_validates(self):
        recipe = {'steps': [
            {'id': 'doc', 'op': 'document.new', 'args': {'path': 'C:/CADWorkspace/x.dwg'}, 'bind': 'doc'},
            {'id': 'save', 'op': 'document.save', 'args': {}},
            {'id': 'reopen', 'op': 'document.reopen', 'args': {}},
            {'id': 'get', 'op': 'entity.get', 'args': {'handle': 'A1',
                                                       'document_id': {'$ref': 'doc.document_id'}}},
        ]}
        self.store.submit(recipe)


class PathPolicyTests(TempOutputsTest):
    def test_accepts_only_absolute_existing_dwg_inside_outputs(self):
        self.assertEqual(self.target.resolve(), cadmod.readback_path(str(self.target)))

    def test_rejects_relative_missing_directory_and_non_dwg_paths(self):
        missing = self.outputs / 'missing.dwg'
        directory = self.outputs / 'folder.dwg'
        directory.mkdir()
        txt = self.outputs / 'notes.txt'
        txt.write_text('x', encoding='utf-8')
        for bad in (str(Path('relative') / 'a.dwg'), str(missing), str(directory), str(txt)):
            with self.assertRaises(ValueError):
                cadmod.readback_path(bad)

    def test_rejects_paths_outside_the_outputs_directory(self):
        outside_outputs = self.root / 'other.dwg'
        write_dwg(outside_outputs)
        outside_root = Path(self.tmp.name) / 'elsewhere' / 'other.dwg'
        write_dwg(outside_root)
        with self.assertRaises(ValueError):
            cadmod.readback_path(str(outside_outputs))
        with self.assertRaises(ValueError):
            cadmod.readback_path(str(outside_root))

    def test_rejects_traversal_that_resolves_outside_outputs(self):
        outside_outputs = self.root / 'escaped.dwg'
        write_dwg(outside_outputs)
        traversal = str(self.outputs / '..' / 'escaped.dwg')
        with self.assertRaises(ValueError):
            cadmod.readback_path(traversal)


class ReadbackTests(TempOutputsTest):
    def execute(self, cad, handles=('A1',)):
        return cad.execute('document.readback',
                           {'path': str(self.target), 'handles': list(handles)})

    def run_readback(self, opened, existing=()):
        documents = FakeDocuments(list(existing), opened=opened)
        app = FakeApp(documents)
        cad = self.cad_with(app)
        with patch.object(cadmod, 'active_object', lambda: app), \
                patch.object(cadmod, 'window_pid', lambda hwnd: 7), \
                patch.object(cadmod, 'connect',
                             side_effect=AssertionError('readback must not reconnect/start CAD')):
            return cad, documents, self.execute(cad)

    def test_readback_opens_read_only_reads_modelspace_and_closes_own_document(self):
        opened = FakeDoc(self.target, True, [Entity('A1')])
        sentinel = object()
        documents = FakeDocuments([FakeDoc(self.outputs / 'other.dwg')], opened=opened)
        app = FakeApp(documents)
        cad = self.cad_with(app)
        cad.doc, cad.path, cad.document_id = sentinel, Path('D:/owned.dwg'), 'owned'
        with patch.object(cadmod, 'active_object', lambda: app), \
                patch.object(cadmod, 'window_pid', lambda hwnd: 7), \
                patch.object(cadmod, 'connect',
                             side_effect=AssertionError('readback must not reconnect/start CAD')):
            result = self.execute(cad)
        self.assertEqual(str(self.target.resolve()), result['path'])
        self.assertEqual(['A1'], result['handles'])
        self.assertEqual('A1', result['entities'][0]['handle'])
        self.assertIsNone(result['entities'][0]['document_id'])
        self.assertEqual([(str(self.target.resolve()), True)], documents.open_calls)
        self.assertEqual([False], opened.close_calls)
        self.assertIs(sentinel, cad.doc)
        self.assertEqual('owned', cad.document_id)

    def test_target_already_open_is_rejected_without_open_or_close(self):
        existing = FakeDoc(self.target)
        documents = FakeDocuments([existing], opened=FakeDoc(self.target, True, [Entity()]))
        app = FakeApp(documents)
        cad = self.cad_with(app)
        with patch.object(cadmod, 'active_object', lambda: app), \
                patch.object(cadmod, 'window_pid', lambda hwnd: 7):
            with self.assertRaisesRegex(ValueError, 'already open'):
                self.execute(cad)
        self.assertEqual([], documents.open_calls)
        self.assertEqual([], existing.close_calls)

    def test_unreadable_open_document_identity_fails_closed(self):
        class BrokenDoc:
            @property
            def FullName(self):
                raise RuntimeError('document metadata unavailable')

        documents = FakeDocuments([BrokenDoc()], opened=FakeDoc(self.target, True, [Entity()]))
        app = FakeApp(documents)
        cad = self.cad_with(app)
        with patch.object(cadmod, 'active_object', lambda: app), \
                patch.object(cadmod, 'window_pid', lambda hwnd: 7):
            with self.assertRaisesRegex(ValueError, 'already open'):
                self.execute(cad)
        self.assertEqual([], documents.open_calls)

    def test_wrong_pid_or_hwnd_refuses_before_opening(self):
        documents = FakeDocuments(opened=FakeDoc(self.target, True, [Entity()]))
        app = FakeApp(documents)
        cad = self.cad_with(app)
        with patch.object(cadmod, 'active_object', lambda: app), \
                patch.object(cadmod, 'window_pid', lambda hwnd: 8):
            with self.assertRaisesRegex(ValueError, 'differs'):
                self.execute(cad)
        app.HWND = 101
        with patch.object(cadmod, 'active_object', lambda: app), \
                patch.object(cadmod, 'window_pid', lambda hwnd: 7):
            with self.assertRaisesRegex(ValueError, 'differs'):
                self.execute(cad)
        self.assertEqual([], documents.open_calls)

    def test_failed_handle_read_still_closes_the_document(self):
        opened = FakeDoc(self.target, True, [Entity('B2')])
        with self.assertRaisesRegex(ValueError, 'A1'):
            self.run_readback(opened)
        self.assertEqual([False], opened.close_calls)

    def test_wrong_read_only_or_identity_is_unknown_and_not_closed(self):
        wrong_flag = FakeDoc(self.target, False, [Entity('A1')])
        with self.assertRaises(cadmod.OutcomeUnknown):
            self.run_readback(wrong_flag)
        self.assertEqual([], wrong_flag.close_calls)
        wrong_name = FakeDoc(self.outputs / 'not-the-target.dwg', True, [Entity('A1')])
        with self.assertRaises(cadmod.OutcomeUnknown):
            self.run_readback(wrong_name)
        self.assertEqual([], wrong_name.close_calls)

    def test_uncertain_close_is_unknown_and_not_retried(self):
        opened = FakeDoc(self.target, True, [Entity('A1')], fail_close=True)
        with self.assertRaises(cadmod.OutcomeUnknown):
            self.run_readback(opened)
        self.assertEqual([False], opened.close_calls)

    def test_open_failure_is_unknown_and_not_retried(self):
        opened = FakeDoc(self.target, True, [Entity('A1')])
        documents = FakeDocuments(opened=opened)
        documents.fail_open = True
        app = FakeApp(documents)
        cad = self.cad_with(app)
        with patch.object(cadmod, 'active_object', lambda: app), \
                patch.object(cadmod, 'window_pid', lambda hwnd: 7):
            with self.assertRaises(cadmod.OutcomeUnknown):
                self.execute(cad)
        self.assertEqual(1, len(documents.open_calls))
        self.assertEqual([], opened.close_calls)


class DocumentCloseTests(TempOutputsTest):
    def cad_with_owned(self, active, path=None, document_id='doc-a', owned=None):
        cad = self.cad_with(FakeApp(FakeDocuments()))
        path = path or self.target
        cad.doc, cad.path, cad.document_id = active, path, document_id
        # The original Add/Open proxy is kept separately: guard() may replace
        # cad.doc with a refreshed proxy for the same COM document.
        cad.owned_doc = owned if owned is not None else FakeDoc(path, identity=active.identity)
        cad._current_document = lambda: active
        return cad

    def test_close_only_owned_active_saved_document_and_clears_ownership(self):
        active = FakeDoc(self.target)
        cad = self.cad_with_owned(active)
        result = cad.execute('document.close', {})
        self.assertTrue(result['closed'])
        self.assertEqual([False], active.close_calls)
        self.assertIsNone(cad.doc)
        self.assertIsNone(cad.path)
        self.assertIsNone(cad.document_id)
        self.assertIsNone(cad.owned_doc)

    def test_close_accepts_same_document_through_a_refreshed_proxy(self):
        import pythoncom
        original = FakeDoc(self.target)
        refreshed = FakeDoc(self.target, identity=original.identity)
        cad = self.cad_with_owned(refreshed, owned=original)
        result = cad.execute('document.close', {})
        self.assertTrue(result['closed'])
        self.assertEqual([False], refreshed.close_calls)
        self.assertEqual({pythoncom.IID_IUnknown}, set(original.identity.queries))
        self.assertIsNone(cad.owned_doc)

    def test_close_refuses_same_path_different_document_identity(self):
        original = FakeDoc(self.target)
        reopened = FakeDoc(self.target)  # same FullName, new COM identity
        cad = self.cad_with_owned(reopened, owned=original)
        with self.assertRaisesRegex(ValueError, 'different document'):
            cad.execute('document.close', {})
        self.assertEqual([], reopened.close_calls)
        self.assertIs(reopened, cad.doc)
        self.assertIs(original, cad.owned_doc)

    def test_close_refuses_unreadable_identity_before_close(self):
        for active_fails in (False, True):
            with self.subTest(active_fails=active_fails):
                original = FakeDoc(self.target)
                active = FakeDoc(self.target)
                (active if active_fails else original).identity.fail = True
                cad = self.cad_with_owned(active, owned=original)
                with self.assertRaisesRegex(ValueError, 'identity'):
                    cad.execute('document.close', {})
                self.assertEqual([], active.close_calls)
                self.assertIs(active, cad.doc)
                self.assertIs(original, cad.owned_doc)

    def test_close_refuses_unsaved_changes_and_foreign_active_drawing(self):
        unsaved = FakeDoc(self.target)
        unsaved.Saved = False
        cad = self.cad_with_owned(unsaved)
        with self.assertRaisesRegex(ValueError, 'unsaved'):
            cad.execute('document.close', {})
        self.assertEqual([], unsaved.close_calls)
        self.assertIs(unsaved, cad.doc)

        foreign = FakeDoc(self.outputs / 'foreign.dwg')
        cad = self.cad_with_owned(foreign)
        with self.assertRaisesRegex(ValueError, 'differs'):
            cad.execute('document.close', {})
        self.assertEqual([], foreign.close_calls)
        self.assertIs(foreign, cad.doc)

    def test_close_without_job_ownership_is_refused(self):
        cad = self.cad_with(FakeApp(FakeDocuments()))
        with self.assertRaisesRegex(ValueError, 'No owned'):
            cad.execute('document.close', {})

    def test_reset_job_drops_ownership_without_closing(self):
        active = FakeDoc(self.target)
        cad = self.cad_with_owned(active)
        cad.reset_job()
        self.assertIsNone(cad.doc)
        self.assertIsNone(cad.path)
        self.assertIsNone(cad.document_id)
        self.assertIsNone(cad.owned_doc)
        self.assertEqual([], active.close_calls)
        with self.assertRaisesRegex(ValueError, 'No owned'):
            cad.execute('document.close', {})

    def test_close_uncertainty_preserves_ownership(self):
        active = FakeDoc(self.target, fail_close=True)
        cad = self.cad_with_owned(active)
        with self.assertRaises(cadmod.OutcomeUnknown):
            cad.execute('document.close', {})
        self.assertEqual([False], active.close_calls)
        self.assertIs(active, cad.doc)
        self.assertEqual(self.target, cad.path)
        self.assertEqual('doc-a', cad.document_id)
        self.assertIsNotNone(cad.owned_doc)


class DocumentOwnershipTests(TempOutputsTest):
    def test_new_remembers_the_added_document_not_a_guard_refresh(self):
        target = self.outputs / 'new.dwg'
        created = MutableDoc(target)
        app = NewApp(NewDocuments(created))
        cad = self.cad_with(app)
        result = cad.execute('document.new', {'path': str(target)})
        self.assertEqual(str(target), result['path'])
        self.assertIs(created, cad.doc)
        self.assertIs(created, cad.owned_doc)
        self.assertEqual([str(target)], created.save_as_calls)

        refreshed = MutableDoc(target, identity=created.identity)
        cad.doc = refreshed
        cad._current_document = lambda: refreshed
        self.assertTrue(cad.execute('document.close', {})['closed'])
        self.assertEqual([False], refreshed.close_calls)
        self.assertIsNone(cad.owned_doc)

    def test_reopen_remembers_the_newly_opened_document(self):
        old = MutableDoc(self.target)
        opened = MutableDoc(self.target)
        documents = FakeDocuments(opened=opened)
        app = FakeApp(documents)
        cad = self.cad_with(app)
        cad.doc, cad.path, cad.document_id = old, self.target, 'doc-a'
        cad.owned_doc = old
        cad._current_document = lambda: old
        with patch.object(cadmod, 'connect', lambda progid, pid: (app, 100, 7)), \
                patch.object(cadmod, 'check_application', lambda app, hwnd, pid: app), \
                patch.object(cadmod, 'window_pid', lambda hwnd: 7):
            result = cad.execute('document.reopen', {})
        self.assertEqual([], result['entities'])
        self.assertIs(opened, cad.doc)
        self.assertIs(opened, cad.owned_doc)
        self.assertIsNot(old, cad.owned_doc)
        self.assertEqual([False], old.close_calls)
        self.assertEqual(1, old.save_calls)


class ResidentWorkerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'queue.sqlite3')

    class Adapter:
        def __init__(self):
            self.owned = None
            self.resets = 0
            self.seen = []
            self.inputs = []
            self.close_calls = 0

        def reset_job(self):
            self.resets += 1
            self.owned = None

        def execute(self, op, args):
            self.seen.append((op, self.owned))
            self.inputs.append((op, args))
            if op == 'document.new':
                self.owned = args['path']
                return {'document_id': 'doc-a', 'path': args['path']}
            if op == 'document.close':
                self.close_calls += 1
                self.owned = None
                return {'closed': True}
            if op == 'document.reopen':
                return {'path': args.get('path'), 'document_id': 'doc-a', 'entities': []}
            if op == 'document.readback':
                return {'path': args['path'], 'handles': [], 'entities': []}
            return {'handles': ['A1'], 'document_id': 'doc-a'}

    def test_same_resident_engine_resets_ownership_between_jobs(self):
        self.store.submit({'steps': [
            {'id': 'new', 'op': 'document.new',
             'args': {'path': 'C:/CADWorkspace/tianzheng-mcp/outputs/a.dwg'}, 'bind': 'doc'}]})
        self.store.submit({'steps': [
            {'id': 'read', 'op': 'document.readback',
             'args': {'path': 'C:/CADWorkspace/tianzheng-mcp/outputs/a.dwg', 'handles': ['A1']}}]})
        adapter = self.Adapter()
        engine = Engine(self.store, adapter, FakeRecovery())
        self.assertTrue(engine.run_one())
        self.assertTrue(engine.run_one())
        self.assertEqual(2, adapter.resets)
        self.assertEqual([('document.new', None), ('document.readback', None)], adapter.seen)
        self.assertEqual(0, adapter.close_calls)
        self.assertIsNone(self.store.claim())

    def test_job_a_saves_and_explicitly_closes_then_job_b_reads_saved_path(self):
        path = 'C:/CADWorkspace/tianzheng-mcp/outputs/a.dwg'
        first = self.store.submit({'steps': [
            {'id': 'new', 'op': 'document.new', 'args': {'path': path}, 'bind': 'doc'},
            {'id': 'save', 'op': 'document.save', 'args': {}},
            {'id': 'close', 'op': 'document.close', 'args': {}}]})
        second = self.store.submit({'steps': [
            {'id': 'read', 'op': 'document.readback',
             'args': {'path': path, 'handles': ['A1']}}]})
        adapter = self.Adapter()
        engine = Engine(self.store, adapter, FakeRecovery())
        self.assertTrue(engine.run_one())
        self.assertTrue(engine.run_one())
        self.assertEqual('succeeded', self.store.get(first)['state'])
        self.assertEqual('succeeded', self.store.get(second)['state'])
        self.assertEqual(1, adapter.close_calls)
        self.assertEqual([
            ('document.new', None),
            ('document.save', path),
            ('document.close', path),
            ('document.readback', None),
        ], adapter.seen)
        self.assertEqual(('document.readback', {'path': path, 'handles': ['A1']}),
                         adapter.inputs[-1])

    def test_same_job_reopen_and_get_still_works(self):
        job = self.store.submit({'steps': [
            {'id': 'new', 'op': 'document.new',
             'args': {'path': 'C:/CADWorkspace/tianzheng-mcp/outputs/a.dwg'}, 'bind': 'doc'},
            {'id': 'save', 'op': 'document.save', 'args': {}},
            {'id': 'reopen', 'op': 'document.reopen', 'args': {}},
            {'id': 'get', 'op': 'entity.get',
             'args': {'handle': 'A1', 'document_id': {'$ref': 'doc.document_id'}}}]})
        adapter = self.Adapter()
        Engine(self.store, adapter, FakeRecovery()).run_one()
        result = self.store.get(job)
        self.assertEqual('succeeded', result['state'])
        self.assertEqual(['document.new', 'document.save', 'document.reopen', 'entity.get'],
                         [op for op, _ in adapter.seen])
        self.assertEqual(('entity.get', {'handle': 'A1', 'document_id': 'doc-a'}),
                         adapter.inputs[-1])


class NoEscapeWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'queue.sqlite3')

    def run_timeout(self, op, args):
        class Slow:
            def execute(self, op, args):
                time.sleep(0.15)
                return {'closed': True} if op == 'document.close' else {
                    'path': args['path'], 'handles': [], 'entities': []}

        job = self.store.submit({'steps': [
            {'id': 'step', 'op': op, 'timeout_s': 0.03, 'args': args}]})
        recovery = FakeRecovery()
        engine = Engine(self.store, Slow(), recovery, poll=0.005)
        self.assertTrue(engine.run_one())
        result = self.store.get(job)
        self.assertEqual('unknown_outcome', result['state'])
        self.assertTrue(engine.quarantined)
        self.assertEqual(0, recovery.escapes)
        self.assertIn('no foreground command', result['error'])
        self.assertFalse(engine.run_one())
        return result

    def test_readback_timeout_is_unknown_without_escape(self):
        self.run_timeout('document.readback',
                         {'path': 'C:/CADWorkspace/tianzheng-mcp/outputs/a.dwg',
                          'handles': ['A1']})

    def test_close_timeout_is_unknown_without_escape(self):
        self.run_timeout('document.close', {})


if __name__ == '__main__':
    unittest.main()
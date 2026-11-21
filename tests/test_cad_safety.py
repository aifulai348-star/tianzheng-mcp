import unittest
from unittest.mock import patch
from pathlib import Path
from tz_mcp.cad import Cad, OutcomeUnknown, guarded_mutation, retry_busy, retry_document_read, com_method, invoke_document, check_application, require_drawing_window, select_wall, require_model_space


class CadSafetyTests(unittest.TestCase):
    def test_rejects_paper_space_before_native_mutation(self):
        class Doc:
            def __init__(self,tilemode,cvport):self.values={'TILEMODE':tilemode,'CVPORT':cvport}
            def GetVariable(self,name):return self.values[name]
        with self.assertRaisesRegex(ValueError,'paper space'):
            require_model_space(Doc(0,1))
        require_model_space(Doc(1,1))
        require_model_space(Doc(0,2))
    def test_frames_wall_in_local_view_before_native_interaction(self):
        class App:
            def ZoomWindow(self, lo, hi):
                self.lo,self.hi=lo.value,hi.value
        cad=Cad.__new__(Cad);cad.app=App()
        cad.frame_wall({'start':[6590,4800,0],'end':[9760,4800,0]})
        self.assertEqual([6090,4300,0],list(cad.app.lo))
        self.assertEqual([10260,5300,0],list(cad.app.hi))
    def test_selects_requested_wall_without_modifying_intersection_fragments(self):
        candidates = [dict(handle='split',bounds=[[120,7760,0],[6500,8000,3000]],LeftWidth=120,RightWidth=120),
                      dict(handle='new',bounds=[[6450,240,0],[6550,7880,3000]],LeftWidth=50,RightWidth=50)]
        args={'start':[6500,240,0],'end':[6500,7760,0]}
        self.assertEqual('new',select_wall(candidates,args,120)['handle'])
        with self.assertRaises(OutcomeUnknown):
            select_wall(candidates+[dict(candidates[1],handle='duplicate')],args,120)
        with self.assertRaises(OutcomeUnknown):
            select_wall(candidates[:1],args,120)
    def test_wall_identification_rejects_shortened_fragment_and_allows_short_wall(self):
        short=dict(handle='shortened',bounds=[[100,-120,0],[900,120,3000]],LeftWidth=120,RightWidth=120)
        with self.assertRaises(OutcomeUnknown):
            select_wall([short],{'start':[0,0,0],'end':[1000,0,0]},120)
        wall=dict(handle='short',bounds=[[-120,0,0],[120,100,3000]],LeftWidth=120,RightWidth=120)
        self.assertEqual('short',select_wall([wall],{'start':[0,0,0],'end':[0,100,0]},0)['handle'])
    def test_minimized_drawing_window_is_rejected_before_native_mutation(self):
        with self.assertRaisesRegex(ValueError, 'minimized'):
            require_drawing_window(100, is_iconic=lambda hwnd: True)
        require_drawing_window(100, is_iconic=lambda hwnd: False)
    @patch('tz_mcp.cad.require_drawing_window')
    def test_native_command_termination_inputs_survive_marker_append(self, ready):
        class Doc:
            def GetVariable(self, name):
                if name == 'TILEMODE':return 1
                return getattr(self, 'marker', '') if name == 'USERS5' else 0
            def SendCommand(self, payload):
                self.payload = payload
                self.marker = payload.split('"')[-2]
            def SetVariable(self, name, value):
                self.marker = value
        cad = Cad.__new__(Cad); cad.doc = Doc(); cad.hwnd = 100
        cad.run_command('TGWall\n0,0,0\n4000,0,0\n\n\n')
        self.assertIn('4000,0,0\r\r\r(setvar', cad.doc.payload)
        self.assertNotIn('\n', cad.doc.payload)
        self.assertEqual('', cad.doc.marker)
    def test_handle_read_retries_entire_read_only_resolution(self):
        cad=Cad.__new__(Cad)
        attempts=[]
        def resolve(handle,document_id):
            attempts.append(handle)
            if len(attempts)==1:
                raise AttributeError('<unknown>.Handle')
            return 'resolved'
        cad._entity_once=resolve
        self.assertEqual('resolved',cad.entity('35A','doc'))
    def test_guard_waits_for_getvariable_metadata_after_reopen(self):
        class Doc:
            FullName = 'D:/CAD绘图/tianzheng-mcp/outputs/test.dwg'
            reads = 0
            @property
            def GetVariable(self):
                self.reads += 1
                if self.reads == 1:
                    raise AttributeError('<unknown>.GetVariable')
                return lambda name: {'CMDACTIVE':0,'UCSORG':(0,0,0),'UCSXDIR':(1,0,0),'UCSYDIR':(0,1,0)}[name]
        cad=Cad.__new__(Cad)
        cad.doc=Doc();cad.path=Path(Doc.FullName)
        cad._current_document=lambda:cad.doc
        cad.guard()
        self.assertGreater(cad.doc.reads,1)
    def test_start_page_is_connected_even_without_active_document(self):
        class App:
            Version = '24.3s'
            def ListArx(self):
                return ['tch_kernal.arx']
            @property
            def ActiveDocument(self):
                raise AssertionError('No document exists on start page')
        cad = Cad.__new__(Cad)
        cad.app, cad.pid, cad.hwnd = App(), 7, 100
        cad._document_count = lambda: 0
        result = cad.diagnose()
        self.assertIsNone(result['active_document'])
        self.assertTrue(result['tianzheng_loaded'])
    def test_explicit_rpc_rejection_does_not_duplicate_accepted_open(self):
        class Busy(Exception):
            hresult = -2147418111
        class Docs:
            attempts = 0
            accepted = 0
            def Open(self, path):
                self.attempts += 1
                if self.attempts == 1:
                    raise Busy()
                self.accepted += 1
                return path
        docs = Docs()
        self.assertEqual('test.dwg', invoke_document(lambda: docs, 'Open', 'test.dwg'))
        self.assertEqual(1, docs.accepted)
    def test_reacquired_application_cannot_switch_process(self):
        class App:
            HWND = 100
        with self.assertRaises(ValueError):
            check_application(App(), 100, 7, pid_lookup=lambda hwnd: 8)
        self.assertIsInstance(check_application(App(),100,7,pid_lookup=lambda hwnd:7),App)
    def test_method_lookup_retry_does_not_repeat_document_mutation(self):
        class Collection:
            queries = 0
            mutations = 0
            @property
            def Open(self):
                self.queries += 1
                if self.queries == 1:
                    raise AttributeError('<unknown>.Open')
                def mutate(path):
                    self.mutations += 1
                    return path
                return mutate
        docs = Collection()
        method = com_method(lambda: docs, 'Open')
        self.assertEqual(0, docs.mutations)
        self.assertEqual('test.dwg', method('test.dwg'))
        self.assertEqual(1, docs.mutations)
    def test_transient_document_metadata_reacquires_without_mutating(self):
        attempts = []
        def acquire():
            attempts.append(1)
            if len(attempts) == 1:
                raise AttributeError('<unknown>.ActiveDocument')
            return 'ready'
        self.assertEqual('ready', retry_document_read(acquire))
    def test_unknown_attribute_is_not_hidden_by_read_retry(self):
        with self.assertRaises(AttributeError):
            retry_document_read(lambda: (_ for _ in ()).throw(AttributeError('<unknown>.Typo')))
    def test_guard_rebinds_document_after_reopen_and_checks_full_path(self):
        class Doc:
            FullName = 'D:/CAD绘图/tianzheng-mcp/outputs/test.dwg'
            def GetVariable(self, name):
                return {'CMDACTIVE':0,'UCSORG':(0,0,0),'UCSXDIR':(1,0,0),'UCSYDIR':(0,1,0)}[name]
        cad = Cad.__new__(Cad)
        cad.doc = object()
        cad.path = Path(Doc.FullName)
        new_doc = Doc()
        cad._current_document = lambda: new_doc
        cad.guard()
        self.assertIs(new_doc, cad.doc)
    def test_guard_rejects_other_active_drawing_before_rebinding(self):
        class Doc:
            FullName = 'D:/CAD绘图/user-source.dwg'
        cad = Cad.__new__(Cad)
        old = cad.doc = object()
        cad.path = Path('D:/CAD绘图/tianzheng-mcp/outputs/test.dwg')
        cad._current_document = lambda: Doc()
        with self.assertRaises(ValueError):
            cad.guard()
        self.assertIs(old, cad.doc)
    def test_known_busy_rejection_can_clear_without_reissuing_creation(self):
        class Busy(Exception):
            hresult = -2147418111
        attempts = []
        def read():
            attempts.append(1)
            if len(attempts) < 2:
                raise Busy()
            return 'ready'
        self.assertEqual('ready', retry_busy(read))
    @patch('tz_mcp.cad.require_drawing_window')
    def test_complete_recipe_uses_cr_without_focus_dependent_post(self, ready):
        class Doc:
            def SendCommand(self, payload):
                if not payload.endswith(chr(13)):
                    raise ValueError('Command requires a carriage-return terminator')
                self.payload = payload
                self.marker = payload.split('"')[-2]
            def PostCommand(self, payload):
                raise RuntimeError('Post waits for document focus')
            def GetVariable(self, name):
                if name == 'TILEMODE':return 1
                return getattr(self, 'marker', '') if name == 'USERS5' else 0
            def SetVariable(self, name, value):
                self.marker = value
        cad = Cad.__new__(Cad)
        cad.hwnd = 100
        cad.doc = Doc()
        cad.run_command('(princ)\n')
        self.assertTrue(cad.doc.payload.startswith('(princ)'))
        self.assertEqual('', cad.doc.marker)
    def test_partial_document_mutation_is_uncertain(self):
        events = []
        def partial():
            events.append('document_added')
            raise RuntimeError('SaveAs failed')
        with self.assertRaises(OutcomeUnknown):
            guarded_mutation(partial)
        self.assertEqual(['document_added'], events)
    def test_unverified_opening_kind_and_host_rejected_before_cad_access(self):
        cad = Cad.__new__(Cad)
        with self.assertRaisesRegex(ValueError, 'measured'):
            cad.execute('opening.create', {'host': {'handle': 'A1', 'document_id': 'doc'},
                        'point': [1000,0], 'kind': 0, 'subkind': 0})


if __name__ == '__main__':
    unittest.main()

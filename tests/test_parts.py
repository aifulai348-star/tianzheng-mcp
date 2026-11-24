import unittest
from tz_mcp.commands import prepare,command_payload
from tz_mcp.parts import verify_host_delta,is_rectangular_column
from types import SimpleNamespace
from unittest.mock import patch

class PartsTests(unittest.TestCase):
    def test_annotation_text_stays_literal_and_rejects_multiline(self):
        args={'text':'(princ) 普通文字','point':[0,0],'height':250}
        self.assertEqual(args['text'],prepare('annotation.text',args)['text'])
        with self.assertRaises(ValueError):prepare('annotation.text',dict(args,text='a\nb'))
    def test_polyline_contract_rejects_non_boolean_closure(self):
        with self.assertRaises(ValueError):prepare('polyline.create',{'points':[[0,0],[1000,0]],'closed':'true'})
    def test_find_wall_resolves_current_fragment_by_insertion_point(self):
        from tz_mcp.cad import Cad
        c=Cad.__new__(Cad);c.document_id='doc';c.guard=lambda:None
        c.entities=lambda:{'old':SimpleNamespace(ObjectName='TDbWall',Handle='old'),'split':SimpleNamespace(ObjectName='TDbWall',Handle='split')}
        c.describe=lambda e:{'handle':e.Handle,'object_name':'TDbWall','bounds':[[0,0,0],[3000,240,3000]] if e.Handle=='old' else [[3000,0,0],[6000,240,3000]]}
        self.assertEqual(['split'],c.execute('wall.find',{'point':[4500,120]})['handles'])
        with self.assertRaises(ValueError):c.execute('wall.find',{'point':[3000,120]})
    def test_column_prototype_requires_rectangular_section(self):
        self.assertTrue(is_rectangular_column(SimpleNamespace(ObjectName='TDbColumn',SectionShapeText='矩形')))
        self.assertFalse(is_rectangular_column(SimpleNamespace(ObjectName='TDbColumn',SectionShapeText='圆形')))
    def test_host_delta_rejects_wrong_wall_and_overlapping_opening(self):
        verify_host_delta('A',{'A':0,'B':0},{'A':2.25,'B':0},1500,1500)
        for after in ({'A':0,'B':2.25},{'A':2.25,'B':1},{'A':1,'B':0}):
            with self.assertRaises(Exception):verify_host_delta('A',{'A':0,'B':0},after,1500,1500)
    def test_opening_contract_uses_observed_kind_strings(self):
        args={'host':{'handle':'35A','document_id':'doc'},'point':[1000,0], 'kind':'普通窗','subkind':'普通窗','width':1500,'height':1500,'sill':900}
        self.assertEqual('普通窗',prepare('opening.create',args)['kind'])
        for bad in (0,'arbitrary'):
            with self.assertRaises(ValueError):prepare('opening.create',dict(args,kind=bad))
    def test_native_column_requires_2d_point_payload(self):
        args=prepare('column.create',{'point':[200,200],'width':400,'depth':400,'height':3000})
        self.assertEqual('TGColumn\n200.0,200.0\n\n',command_payload('column.create',args))
    def test_floor_hole_must_stay_strictly_inside_slab(self):
        args={'boundary':[[0,0],[10000,0],[10000,8000],[0,8000]],'holes':[[[900,4800],[4700,4800],[4700,7500],[900,7500]]],'thickness':120}
        self.assertEqual(120,prepare('slab.create',args)['thickness'])
        with self.assertRaises(ValueError):prepare('slab.create',dict(args,holes=[[[0,0],[1000,0],[1000,1000],[0,1000]]]))
        with self.assertRaises(ValueError):prepare('slab.create',dict(args,thickness=0))
        with self.assertRaises(ValueError):prepare('slab.create',dict(args,boundary=[[0,0],[1000,0],[0,0],[0,1000]],holes=[]))
        with self.assertRaises(ValueError):prepare('slab.create',dict(args,boundary=[[p[0],p[1],3000] for p in args['boundary']]))
    def test_rectangular_stair_limits_whole_flight_count(self):
        args={'point':[1000,4900],'height':3000,'steps':18,'step_width':260,'width':1200,'rotation_deg':90}
        self.assertEqual(18,prepare('stair.rect_create',args)['steps'])
        with self.assertRaises(ValueError):prepare('stair.rect_create',dict(args,steps=18.5))

    def test_unframed_part_fails_without_becoming_uncertain(self):
        from tz_mcp.cad import CommandNotSent, OutcomeUnknown
        from tz_mcp.parts import execute_part
        class Doc:
            def GetVariable(self,name):return {'TILEMODE':1,'CVPORT':1}[name]
        class Cad:
            hwnd=1;doc=Doc();document_id='doc'
            def __init__(self,error):self.error=error
            def guard(self):pass
            def entities(self):return {}
            def frame_wall(self,args):raise self.error
            def run_command(self,payload):raise AssertionError('command must not be sent')
        args={'point':[1000,4900],'height':3000,'steps':18,'step_width':260,'width':1200,'rotation_deg':90}
        with patch('tz_mcp.parts.require_drawing_window'), patch('tz_mcp.parts.require_model_space'):
            with self.assertRaises(CommandNotSent):
                execute_part(Cad(CommandNotSent('framing refused')),'stair.rect_create',args)
            with self.assertRaises(OutcomeUnknown):
                execute_part(Cad(RuntimeError('unexpected')),'stair.rect_create',args)

class _FakeStair:
    """Stand-in for the wizard-built TDbRectStair used by the width regression.

    Only the properties stair.rect_create touches are modelled: the wizard
    builds the stair at 1230 mm, refuses writes to StairWidth/StairWidth2, or
    silently clamps them, depending on what the test needs to reproduce.
    """
    ObjectName='TDbRectStair';Handle='34D'
    def __init__(self,clamp=None,refuse=()):
        object.__setattr__(self,'_values',{'StairHeight':3000.0,'StepTotalNum':18.0,'StepWidth':270.0,
                                           'StairWidth':1230.0,'StairWidth2':1230.0})
        object.__setattr__(self,'refuse',set(refuse));object.__setattr__(self,'clamp',clamp)
        object.__setattr__(self,'calls',{'rotate':0,'move':0,'update':0})
        object.__setattr__(self,'bounds',[[0.0,0.0,0.0],[2560.0,3340.0,3000.0]])
    def __getattr__(self,name):
        values=object.__getattribute__(self,'_values')
        if name in values:return values[name]
        raise AttributeError(name)
    def __setattr__(self,name,value):
        values=object.__getattribute__(self,'_values')
        if name in values:
            if name in object.__getattribute__(self,'refuse'):
                raise RuntimeError(f"Property '<unknown>.{name}' can not be set.")
            clamp=object.__getattribute__(self,'clamp')
            if clamp is not None and name in ('StairWidth','StairWidth2') and float(value)>clamp:
                value=clamp
            values[name]=float(value)
            return
        object.__setattr__(self,name,value)
    def Rotate(self,base,angle):self.calls['rotate']+=1
    def Move(self,lo,point):
        self.calls['move']+=1
        lo=list(getattr(lo,'value',lo));point=list(getattr(point,'value',point))
        dx=point[0]-lo[0];dy=point[1]-lo[1]
        object.__setattr__(self,'bounds',[[b[0]+dx,b[1]+dy,b[2]] for b in self.bounds])
    def Update(self):self.calls['update']+=1

class _FakeCad:
    hwnd=1;document_id='doc'
    def __init__(self,stair):
        self.stair=stair;self.commands=[];self.seen=False;self.doc=object()
    def guard(self):pass
    def frame_wall(self,args):pass
    def run_command(self,payload):self.commands.append(payload);self.seen=True
    def entities(self):return {self.stair.Handle:self.stair} if self.seen else {}
    def describe(self,e):return {'handle':e.Handle,'object_name':'TDbRectStair','bounds':e.bounds}

def run_stair_case(stair,args):
    from tz_mcp.parts import execute_part
    cad=_FakeCad(stair)
    with patch('tz_mcp.parts.require_drawing_window'),patch('tz_mcp.parts.require_model_space'):
        return cad,execute_part(cad,'stair.rect_create',args)

class StairWidthRegressionTests(unittest.TestCase):
    args={'point':[13400,8300],'height':3000,'steps':16,'step_width':280,'width':1500,'rotation_deg':90}
    def test_wizard_refused_flight_width_is_uncertain_and_names_the_side_effect(self):
        from tz_mcp.cad import OutcomeUnknown
        stair=_FakeStair(refuse=('StairWidth','StairWidth2'))
        with self.assertRaises(OutcomeUnknown) as ctx:
            run_stair_case(stair,self.args)
        message=str(ctx.exception)
        for token in ('StairWidth','1500','1230','34D','remains in the drawing','side effect present','can not be set'):
            self.assertIn(token,message)
        self.assertTrue(ctx.exception.uncertain)
    def test_refused_width_still_reports_the_build_as_a_side_effect(self):
        from tz_mcp.cad import OutcomeUnknown
        stair=_FakeStair(refuse=('StairWidth','StairWidth2'))
        with self.assertRaises(OutcomeUnknown):
            run_stair_case(stair,self.args)
        cad=_FakeCad(stair)
        with self.assertRaises(OutcomeUnknown):
            with patch('tz_mcp.parts.require_drawing_window'),patch('tz_mcp.parts.require_model_space'):
                from tz_mcp.parts import execute_part
                execute_part(cad,'stair.rect_create',self.args)
        self.assertEqual(1,len(cad.commands))
        self.assertEqual('TRStair\n13400,8300\n\n',cad.commands[0])
        self.assertEqual(0,stair.calls['rotate'])
        self.assertEqual(0,stair.calls['move'])
    def test_silently_clamped_width_is_not_a_property_pass(self):
        from tz_mcp.cad import OutcomeUnknown
        stair=_FakeStair(clamp=1230)
        with self.assertRaises(OutcomeUnknown) as ctx:
            run_stair_case(stair,self.args)
        message=str(ctx.exception)
        self.assertIn('measured 1230',message)
        self.assertNotIn('write refused',message)
        self.assertTrue(ctx.exception.uncertain)
    def test_width_that_matches_the_wizard_value_still_passes(self):
        stair=_FakeStair(refuse=('StairWidth','StairWidth2'))
        result=run_stair_case(stair,dict(self.args,width=1230,steps=18))[1]
        self.assertTrue(result['validation']['properties'])
        self.assertEqual(['StairWidth','StairWidth2'],result['evidence']['properties_wizard_owned'])
        self.assertEqual(1230.0,result['evidence']['properties_measured']['StairWidth'])
        self.assertEqual(1,stair.calls['rotate']);self.assertEqual(1,stair.calls['move'])
    def test_property_pass_records_requested_default_and_measured_widths(self):
        stair=_FakeStair()
        cad,result=run_stair_case(stair,{'point':[0,3500],'height':3000,'steps':18,'step_width':260,
                                         'width':1200,'rotation_deg':0})
        evidence=result['evidence']
        self.assertTrue(result['validation']['properties'])
        self.assertEqual(1200.0,evidence['properties_requested']['StairWidth'])
        self.assertEqual(1200.0,evidence['properties_measured']['StairWidth2'])
        self.assertEqual(1230.0,evidence['properties_wizard_default']['StairWidth'])
        self.assertEqual([],evidence['properties_wizard_owned'])
        self.assertEqual(1200.0,evidence['stair_width'])

if __name__=='__main__':unittest.main()

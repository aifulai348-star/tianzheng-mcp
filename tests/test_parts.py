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

if __name__=='__main__':unittest.main()

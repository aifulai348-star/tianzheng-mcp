"""Locally probed Tianzheng components and their evidence checks."""
import math
from .commands import command_payload
from .cad import OutcomeUnknown, late, invoke_document, require_drawing_window, require_model_space
from .panel import (create_column, column_dxf, verify_column_dxf,
                     create_opening, opening_dxf, verify_opening_dxf)


def is_rectangular_column(e):
    return e.ObjectName=='TDbColumn' and str(e.SectionShapeText)=='矩形'


def verify_host_delta(handle, before, after, width, height):
    expected = width*height/1_000_000
    if set(before)!=set(after):
        raise OutcomeUnknown('Wall handles changed during opening; inspect exact host')
    if not math.isclose(after[handle]-before[handle],expected,abs_tol=.002):
        raise OutcomeUnknown('Referenced wall hole-area increase does not match opening')
    if any(abs(after[h]-before[h])>.002 for h in before if h!=handle):
        raise OutcomeUnknown('Another wall hole area changed; host association ambiguous')


def vector(values):
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,values)


def hole_areas(c):
    return {h:float(e.HoleArea) for h,e in c.entities().items() if e.ObjectName=='TDbWall'}


def bounds(c,e):
    result=c.describe(e)
    if 'bounds' not in result:raise OutcomeUnknown('Component geometry could not be read')
    return result['bounds']


def set_properties(e,props):
    for name,value in props.items():
        setattr(e,name,value)
        if not math.isclose(float(getattr(e,name)),value,abs_tol=1e-6):
            raise OutcomeUnknown(name+' readback mismatch')
    e.Update()


def make_polyline(c,points):
    e=late(c.doc.ModelSpace.AddLightWeightPolyline(vector([v for p in points for v in p[:2]])))
    e.Closed=True
    return str(e.Handle)


def execute_part(c,op,args):
    c.guard();require_drawing_window(c.hwnd);require_model_space(c.doc)
    before=c.entities();host=None;holes_before=None;placement=None
    if op=='opening.create':
        host=c.entity(args['host']['handle'],args['host']['document_id'])
        if host.ObjectName!='TDbWall':raise ValueError('Opening host must be a native wall')
        lo,hi=bounds(c,host)
        if not all(lo[i]-.01<=args['point'][i]<=hi[i]+.01 for i in (0,1)):
            raise ValueError('Opening insertion point is outside referenced wall')
        holes_before=hole_areas(c)
    try:
        consumed=[]
        if op=='annotation.text':
            c.doc.ModelSpace.AddText(args['text'],vector(args['point']),args['height'])
        elif op=='polyline.create':
            obj=late(c.doc.ModelSpace.AddLightWeightPolyline(vector([v for p in args['points'] for v in p[:2]])))
            obj.Closed=args.get('closed',False)
        elif op=='slab.create':
            outer=make_polyline(c,args['boundary']);hole_handles=[make_polyline(c,h) for h in args['holes']]
            payload=f'TSlab\n(handent "{outer}")\n\n'
            payload+=''.join(f'(handent "{h}")\n' for h in hole_handles)
            payload+=f'\n{-args["thickness"]}\n'
            consumed=[outer]+hole_handles
            c.run_command(payload)
        elif op=='column.create':
            # The panel is the only verified constructor: the seed CopyObjects
            # route blocks on a second copy inside one drawing (job 144ad30f).
            placement=create_column(c,args)
        elif op=='opening.create':
            # The opening's size lives in the TGDOOR/TGWINDOW palette, not in
            # the command line: typing coordinates at the pick prompt is
            # rejected.
            placement=create_opening(c,args)
        else:
            c.frame_wall({'start':args['point'],'end':args['point']})
            c.run_command(command_payload(op,args))
        after=c.entities()
        expected={'slab.create':'TDbSlab','column.create':'TDbColumn','stair.rect_create':'TDbRectStair','opening.create':'TDbOpening',
                  'annotation.text':'AcDbText','polyline.create':'AcDbPolyline'}[op]
        new=[e for h,e in after.items() if h not in before and e.ObjectName==expected]
        if len(new)!=1:raise OutcomeUnknown(f'Expected one new {expected}; got {len(new)}')
        e=new[0];evidence={}
        if op=='annotation.text':
            if str(e.TextString)!=args['text']:raise OutcomeUnknown('Annotation text readback mismatch')
            evidence={'construction':'ordinary_cad_text'}
        elif op=='polyline.create':
            evidence={'construction':'ordinary_cad_polyline','closed':bool(e.Closed)}
        elif op=='opening.create':
            if (str(e.GetKind),str(e.GetSubKind))!=(args['kind'],args['subkind']):
                raise OutcomeUnknown('Opening type does not match the requested kind')
            dxf=opening_dxf(c,str(e.Handle))
            check=verify_opening_dxf(dxf,args,placement,args['host']['handle'])
            # Placement is already proven by verify_opening_dxf, which compares
            # the DXF insertion point with the world point the click landed on.
            # The COM bounding box of a door also covers the swing arc, so its
            # centre is not the insertion point and must not be compared here.
            if check['height']+float(args.get('sill') or 0.0)>float(host.Height)+.01:
                raise OutcomeUnknown('Opening exceeds wall height')
            after_holes=hole_areas(c)
            verify_host_delta(args['host']['handle'],holes_before,after_holes,float(e.Width),float(e.Height))
            evidence={'host_reference':args['host'],'dxf':check,
                      'host_hole_area_before':holes_before[args['host']['handle']],
                      'host_hole_area_after':after_holes[args['host']['handle']],
                      'host_association':'verified_by_wall_delta_and_position'}
        elif op=='stair.rect_create':
            set_properties(e,{'StairHeight':args['height'],'StepTotalNum':args['steps'],'StepWidth':args['step_width'],
                              'StairWidth':args['width'],'StairWidth2':args['width']})
            e.Rotate(vector([0,0,0]),math.radians(args.get('rotation_deg',0)))
            lo,hi=bounds(c,e);e.Move(vector(lo),vector(args['point']));e.Update()
            lo,hi=bounds(c,e)
            if any(abs(lo[i]-args['point'][i])>.01 for i in (0,1)):
                raise OutcomeUnknown('Stair bounding-box placement mismatch')
            evidence={'placement':'bounding_box_min','rotation_deg':args.get('rotation_deg',0)}
        elif op=='column.create':
            if not is_rectangular_column(e):raise OutcomeUnknown('Column section is not rectangular')
            dxf=column_dxf(c,str(e.Handle))
            evidence={'creation':'tgcolumn_panel_canvas_click','section_shape':str(e.SectionShapeText),
                      'placement':placement,'dxf':verify_column_dxf(dxf,args,placement)}
        else:
            if any(h in after for h in consumed):raise OutcomeUnknown('Slab boundary/hole selection not consumed')
            lo,hi=bounds(c,e)
            if abs(hi[2]-lo[2]-args['thickness'])>.01:raise OutcomeUnknown('Slab thickness geometry mismatch')
            evidence={'thickness':args['thickness'],'boundary':args['boundary'],'holes':args['holes'],
                      'hole_selection':'native_closed_boundaries_consumed','hole_geometry':'pending_independent_check'}
        info=c.describe(e)
        return {'success':True,'document_id':c.document_id,'handles':[info['handle']],'entities':[info],
                'evidence':evidence,'validation':{'type':True,'properties':True,'reopen':'pending'}}
    except Exception as exc:
        if isinstance(exc,OutcomeUnknown):raise
        raise OutcomeUnknown(str(exc)) from exc

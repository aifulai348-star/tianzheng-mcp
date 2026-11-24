"""Locally probed Tianzheng components and their evidence checks."""
import math
import time
from pathlib import Path
from .commands import command_payload
from .cad import (CommandCompletionTimeout, CommandNotSent, OutcomeUnknown, late,
                  invoke_document, require_drawing_window, require_model_space)
from .panel import (create_column, column_dxf, verify_column_dxf,
                    create_opening, opening_dxf, verify_opening_dxf,
                    drawing_view, entity_wall_hosts, menu_dispatch,
                    pick_world_point, wait_command_idle)


def is_rectangular_column(e):
    return e.ObjectName=='TDbColumn' and str(e.SectionShapeText)=='矩形'


def hole_ok(delta, expected):
    """Tianzheng rounds each wall hole to 0.01 m2, so compare at that precision."""
    return abs(delta-expected) <= 0.02


def verify_host_delta(handle, before, after, width, height):
    expected = width*height/1_000_000
    if set(before)!=set(after):
        raise OutcomeUnknown('Wall handles changed during opening; inspect exact host')
    delta = after[handle]-before[handle]
    if not hole_ok(delta,expected):
        raise OutcomeUnknown(f'Referenced wall hole-area increase does not match opening '
                            f'(expected {expected:.4f}, delta {delta:.4f}, before {before[handle]:.4f}, after {after[handle]:.4f})')
    if any(abs(after[h]-before[h])>.02 for h in before if h!=handle):
        raise OutcomeUnknown('Another wall hole area changed; host association ambiguous')


def vector(values):
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,values)


def hole_areas(c):
    return {h:float(e.HoleArea) for h,e in c.entities().items() if e.ObjectName=='TDbWall'}


def wait_hole_delta(c, handle, before, width, height, timeout=5.0):
    """Poll HoleArea until the host wall records the new opening.

    Tianzheng updates wall hole statistics after the palette command returns, so a
    single read straight after the click can still show the previous total.
    """
    import time
    expected = width*height/1_000_000
    deadline = time.monotonic()+timeout
    seen = None
    while True:
        after = hole_areas(c)
        if set(after) != set(before):
            seen = f'wall handles changed {len(before)} -> {len(after)}'
        else:
            seen = f'expected {expected:.4f}, delta {after[handle]-before[handle]:.4f}'
            if hole_ok(after[handle]-before[handle], expected):
                return after
        if time.monotonic() >= deadline:
            raise OutcomeUnknown(f'Referenced wall hole-area increase does not match opening ({seen})')
        time.sleep(.25)


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
    if op=='wall.split':return split_wall(c,args,before)
    if op=='column.move':return move_column(c,args,before)
    if op=='opening.move':return move_opening(c,args,before)
    if op=='opening.create':
        host=c.entity(args['host']['handle'],args['host']['document_id'])
        if host.ObjectName!='TDbWall':raise ValueError('Opening host must be a native wall')
        lo,hi=bounds(c,host)
        if not all(lo[i]-.01<=args['point'][i]<=hi[i]+.01 for i in (0,1)):
            raise ValueError('Opening insertion point is outside referenced wall')
        # HoleArea is recomputed lazily by Tianzheng: read twice so the baseline
        # reflects the previous opening instead of a stale pre-regen value.
        holes_before=hole_areas(c)
        import time as _time
        _time.sleep(.2)
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
            # Fit the whole plan first, with view transitions off: the pick is
            # then made against a view that never changes during the phase, so
            # no column depends on a reframe issued while the panel is already
            # monopolising COM (measured 2026-11-21: c7 was off screen and c2
            # then landed on the previous view's centre).
            c.run_command('(setvar "VTENABLE" 0)\n')
            px, py = float(args['point'][0]), float(args['point'][1])
            c.frame_wall({'start': [min(-2000.0, px - 2000.0), min(-2000.0, py - 2000.0)],
                          'end': [max(31300.0, px + 2000.0), max(15200.0, py + 2000.0)]})
            time.sleep(0.5)
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
            after_holes=wait_hole_delta(c,args['host']['handle'],holes_before,float(e.Width),float(e.Height))
            verify_host_delta(args['host']['handle'],holes_before,after_holes,float(e.Width),float(e.Height))
            evidence={'host_reference':args['host'],'dxf':check,
                      'host_hole_area_before':holes_before[args['host']['handle']],
                      'host_hole_area_after':after_holes[args['host']['handle']],
                      'host_association':'verified_by_wall_delta_and_position'}
        elif op=='stair.rect_create':
            # The wizard builds the stair from its own palette defaults and keeps
            # StairWidth/StairWidth2 under its control: a flight wider than the
            # stairwell it just created is refused or silently clamped (measured
            # 2026-11-24: every request above 1230 mm stayed at 1230, and the
            # refused write raised "not set"). A rejected width still leaves a
            # built stair, so the build is a side effect: report the measurement
            # as an uncertain outcome instead of calling the properties a pass.
            wanted={'StairHeight':args['height'],'StepTotalNum':args['steps'],'StepWidth':args['step_width'],
                    'StairWidth':args['width'],'StairWidth2':args['width']}
            defaults={};applied={};refused={}
            for name in wanted:
                try:defaults[name]=float(getattr(e,name))
                except Exception:defaults[name]=None
            for name,value in wanted.items():
                try:
                    setattr(e,name,value)
                    applied[name]=float(getattr(e,name))
                except Exception as exc:
                    refused[name]=str(exc)
            measured={}
            for name in wanted:
                try:measured[name]=float(getattr(e,name))
                except Exception:measured[name]=None
            mismatch=[name for name in wanted
                      if measured[name] is None or abs(measured[name]-float(wanted[name]))>.01]
            if mismatch:
                raise OutcomeUnknown('Stair property mismatch after build: '+'; '.join(
                    f'{name} requested {float(wanted[name]):g}, '
                    f'measured {measured[name] if measured[name] is None else format(measured[name],".6g")}, '
                    f'wizard default {defaults[name] if defaults[name] is None else format(defaults[name],".6g")}'
                    +(f', write refused: {refused[name]}' if name in refused else '')
                    for name in mismatch)
                    +f'; {expected} handle {e.Handle} remains in the drawing - side effect present, do not replay blind')
            e.Rotate(vector([0,0,0]),math.radians(args.get('rotation_deg',0)))
            lo,hi=bounds(c,e);e.Move(vector(lo),vector(args['point']));e.Update()
            lo,hi=bounds(c,e)
            if any(abs(lo[i]-args['point'][i])>.01 for i in (0,1)):
                raise OutcomeUnknown(f'Stair bounding-box placement mismatch; {expected} handle {e.Handle} '
                                     'remains in the drawing - side effect present, do not replay blind')
            for name,value in wanted.items():
                got=float(getattr(e,name))
                if abs(got-float(value))>.01:
                    raise OutcomeUnknown(f'Stair property changed after placement: {name} {got:g} != {float(value):g}; '
                                         f'{expected} handle {e.Handle} remains in the drawing - '
                                         'side effect present, do not replay blind')
            evidence={'placement':'bounding_box_min','rotation_deg':args.get('rotation_deg',0),
                      'properties_requested':{k:float(v) for k,v in wanted.items()},
                      'properties_wizard_default':defaults,'properties_measured':measured,
                      'properties_applied':applied,'properties_wizard_owned':sorted(refused),
                      'stair_width':measured['StairWidth'],'bounds':[[lo[0],lo[1]],[hi[0],hi[1]]]}
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
        if isinstance(exc,(OutcomeUnknown,CommandNotSent)):raise
        raise OutcomeUnknown(str(exc)) from exc


SPLIT_CLEARANCE_MM = 10.0
SPLIT_TOLERANCE_MM = 2.0
SPLIT_KATANA_TOLERANCE_MM = 50.0


def _split_number(info, key):
    value = info.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _split_area(info, key):
    """HoleArea arrives from COM as text ("1.89"), occasionally as a number.

    Only a finite, non-negative measurement is accepted: a missing property, a
    sentinel such as "nan" or a negative value must never be read as 0 m2.
    """
    value = info.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _split_along(lo, hi, vertical):
    return (float(lo[1]), float(hi[1])) if vertical else (float(lo[0]), float(hi[0]))


def _split_across(lo, hi, vertical):
    return (float(lo[0]), float(hi[0])) if vertical else (float(lo[1]), float(hi[1]))


def _split_frame(info):
    """Axis frame from the measured bounding box, cross-checked with widths."""
    if 'bounds' not in info:
        raise OutcomeUnknown('Wall geometry could not be read')
    lo, hi = info['bounds']
    x0, x1 = sorted((float(lo[0]), float(hi[0])))
    y0, y1 = sorted((float(lo[1]), float(hi[1])))
    vertical = (y1 - y0) >= (x1 - x0)
    frame = {'vertical': vertical,
             'line': (x0 + x1) / 2.0 if vertical else (y0 + y1) / 2.0,
             'thick': (x1 - x0) if vertical else (y1 - y0),
             'span': (y0, y1) if vertical else (x0, x1)}
    left, right = _split_number(info, 'LeftWidth'), _split_number(info, 'RightWidth')
    if left is not None and right is not None:
        declared = left + right
    else:
        declared = _split_number(info, 'Width')
    if declared is None:
        raise OutcomeUnknown('Wall thickness properties could not be read')
    if abs(declared - frame['thick']) > 1.0:
        raise OutcomeUnknown('Wall bounding box and declared widths disagree')
    return frame


def _split_creation_record(frame, created):
    start, end = created['start'], created['end']
    dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
    vertical = dy >= dx
    if vertical != frame['vertical']:
        raise OutcomeUnknown('Wall axis direction differs from its creation record')
    line = start[0] if vertical else start[1]
    if abs(line - frame['line']) > 1.0:
        raise OutcomeUnknown('Wall axis line differs from its creation record')


def _split_openings(c, frame):
    """Openings whose bounding box crosses this wall, with their along-wall span."""
    band = (frame['line'] - frame['thick'] / 2.0, frame['line'] + frame['thick'] / 2.0)
    found = []
    for entity in c.entities().values():
        if entity.ObjectName != 'TDbOpening':
            continue
        try:
            info = c.describe(entity)
        except Exception as exc:
            raise OutcomeUnknown('Opening geometry could not be read') from exc
        if 'bounds' not in info:
            raise OutcomeUnknown('Opening bounding box is missing; split clearance unknown')
        along = _split_along(info['bounds'][0], info['bounds'][1], frame['vertical'])
        across = _split_across(info['bounds'][0], info['bounds'][1], frame['vertical'])
        if min(along[1], frame['span'][1]) - max(along[0], frame['span'][0]) <= 0:
            continue
        if min(across[1], band[1]) - max(across[0], band[0]) <= 0:
            continue
        found.append({'handle': str(info['handle']).upper(), 'interval': along})
    return found


def _split_same_value(left, right, tolerance=1e-6):
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), abs_tol=tolerance, rel_tol=0.0)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _split_same_value(a, b, tolerance) for a, b in zip(left, right))
    return left == right


def _split_same_info(before, after, ignore=('document_id',)):
    keys = (set(before) | set(after)) - set(ignore)
    return all(_split_same_value(before.get(k), after.get(k)) for k in keys)


def _unique_wall_host(c, handle):
    """Resolve DXF 330 references for an opening and require a unique TCH_WALL host."""
    refs = entity_wall_hosts(c, handle)
    broken = [r for r in refs if not r.get('handle')]
    if broken:
        raise OutcomeUnknown(
            'DXF 330 host references could not be resolved: %r' % refs)
    wall_refs = [r for r in refs if str(r['type']).upper() == 'TCH_WALL']
    if len(wall_refs) != 1:
        raise OutcomeUnknown('Opening host is not a unique TCH_WALL in DXF 330: %r' % refs)
    return str(wall_refs[0]['handle']).upper(), refs


def _verify_split(c, handle, before, before_desc, after, after_desc, frame, station,
                  openings, placement, menu):
    removed = sorted(set(before) - set(after))
    if removed:
        raise OutcomeUnknown('Split removed existing handles: ' + ', '.join(removed))
    added = [h for h in after if h not in before]
    walls = [h for h in added if after[h].ObjectName == 'TDbWall']
    katanas = [h for h in added if after[h].ObjectName == 'GlwKatana']
    if len(added) != 2 or len(walls) != 1 or len(katanas) != 1:
        names = ['%s:%s' % (h, after[h].ObjectName) for h in added]
        raise OutcomeUnknown('Expected one new TDbWall and one GlwKatana; observed %r' % names)
    if handle not in after:
        raise OutcomeUnknown('Original wall handle disappeared during the split')
    segments = walls + [handle]
    frames = {h: _split_frame(after_desc[h]) for h in segments}
    for h in segments:
        if frames[h]['vertical'] != frame['vertical']:
            raise OutcomeUnknown('Split segment direction changed')
        if abs(frames[h]['line'] - frame['line']) > SPLIT_TOLERANCE_MM:
            raise OutcomeUnknown('Split segment left the original axis')
        if abs(frames[h]['thick'] - frame['thick']) > SPLIT_TOLERANCE_MM:
            raise OutcomeUnknown('Split segment thickness changed')
    spans = sorted(frames[h]['span'] for h in segments)
    if not (abs(spans[0][0] - frame['span'][0]) <= SPLIT_TOLERANCE_MM
            and abs(spans[1][1] - frame['span'][1]) <= SPLIT_TOLERANCE_MM):
        raise OutcomeUnknown('Split segments do not cover the original wall extent')
    if abs(spans[1][0] - spans[0][1]) > SPLIT_TOLERANCE_MM:
        raise OutcomeUnknown('Split segments do not meet at one seam')
    seam = (spans[0][1] + spans[1][0]) / 2.0
    if abs(seam - station) > SPLIT_TOLERANCE_MM:
        raise OutcomeUnknown('Split seam is %.3f mm from the requested station' % (seam - station))

    areas = {}
    for h in segments:
        value = _split_area(after_desc[h], 'HoleArea')
        if value is None:
            raise OutcomeUnknown('Split wall hole area could not be read')
        areas[h] = value
    before_area = _split_area(before_desc[handle], 'HoleArea')
    if before_area is None:
        raise OutcomeUnknown('Original wall hole area could not be read')
    for key in ('LeftWidth', 'RightWidth', 'Width', 'Height'):
        if key in before_desc[handle] and not _split_same_value(
                before_desc[handle][key], after_desc[handle][key]):
            raise OutcomeUnknown('Original wall %s changed during the split' % key)
    for h in before:
        if h == handle:
            continue
        if not _split_same_info(before_desc[h], after_desc[h]):
            raise OutcomeUnknown('Unrelated entity %s changed during the split' % h)

    host_handle = None
    if openings:
        host_handle, _ = _unique_wall_host(c, openings[0]['handle'])
        if host_handle not in segments:
            raise OutcomeUnknown('Opening host %s is not one of the split segments' % host_handle)
        if areas[host_handle] <= 0.002:
            raise OutcomeUnknown('Opening host segment reports no hole area')
        if any(abs(areas[h]) > 0.002 for h in segments if h != host_handle):
            raise OutcomeUnknown('Hole area is not confined to the opening host segment')
        if abs(areas[host_handle] - before_area) > 0.002:
            raise OutcomeUnknown('Hole area changed across the split')
        host_span = frames[host_handle]['span']
        if not (host_span[0] - SPLIT_TOLERANCE_MM <= openings[0]['interval'][0]
                and openings[0]['interval'][1] <= host_span[1] + SPLIT_TOLERANCE_MM):
            raise OutcomeUnknown('Opening lies outside the segment that hosts it')
    elif before_area > 0.002 or any(abs(areas[h]) > 0.002 for h in segments):
        raise OutcomeUnknown('Hole area appeared without a measurable opening')

    katana = after_desc[katanas[0]]
    if 'bounds' not in katana:
        raise OutcomeUnknown('GlwKatana geometry could not be read')
    k_along = _split_along(katana['bounds'][0], katana['bounds'][1], frame['vertical'])
    if abs((k_along[0] + k_along[1]) / 2.0 - seam) > SPLIT_KATANA_TOLERANCE_MM:
        raise OutcomeUnknown('GlwKatana is not at the split seam')

    return {
        'success': True,
        'document_id': c.document_id,
        'handles': segments,
        'entities': [after_desc[h] for h in segments],
        'evidence': {
            'split': {
                'station': station,
                'seam': seam,
                'original_span': list(frame['span']),
                'segments': {h: {'span': list(frames[h]['span']), 'hole_area': areas[h]}
                             for h in segments},
                'added_handles': walls + katanas,
                'host_handle': host_handle,
                'openings': [o['handle'] for o in openings],
            },
            'pick': placement,
            'menu': menu,
        },
        'validation': {'type': True, 'properties': True,
                       'geometry': 'verified_in_memory',
                       'host': 'verified_dxf_330_and_hole_area' if openings else 'not_applicable',
                       'reopen': 'pending'},
    }


def split_wall(c, args, before, backend=None):
    """Split one job-created wall at a station through the native TKATANA menu.

    Only walls created by wall.create in this job are accepted: their axis is
    known from the creation request, while a DXF 300 blob on a saved wall proved
    not to follow its real extent (identical before and after a split).
    """
    handle = args['handle'].upper()
    created = getattr(c, 'created_walls', {}).get(handle)
    if not created:
        raise ValueError('wall.split requires a wall created in this job')
    if created.get('document_id') != c.document_id:
        raise ValueError('Split wall creation record belongs to another document')
    owned = getattr(c, 'path', None)
    if owned is not None and Path(str(owned)).resolve() != Path(args['path']).resolve():
        raise ValueError('Split path is not the document owned by this job')
    wall = c.entity(handle, args['document_id'])
    if wall.ObjectName != 'TDbWall':
        raise ValueError('wall.split requires a native wall')
    info = c.describe(wall)
    frame = _split_frame(info)
    _split_creation_record(frame, created)
    station = args['point'][1] if frame['vertical'] else args['point'][0]
    across = args['point'][0] if frame['vertical'] else args['point'][1]
    low, high = frame['span']
    if station - low < SPLIT_CLEARANCE_MM or high - station < SPLIT_CLEARANCE_MM:
        raise ValueError('Split station is too close to the wall end')
    if abs(across - frame['line']) > frame['thick'] / 2.0 + 1.0:
        raise ValueError('Split point must lie inside the wall band')
    openings = _split_openings(c, frame)
    if len(openings) > 1:
        raise ValueError('Multiple openings on this wall are not supported')
    before_area = _split_area(info, 'HoleArea')
    if before_area is None:
        raise OutcomeUnknown('Original wall hole area could not be read')
    if openings:
        open_low, open_high = openings[0]['interval']
        if open_low - SPLIT_CLEARANCE_MM <= station <= open_high + SPLIT_CLEARANCE_MM:
            raise ValueError('Split station is too close to an opening')
    elif before_area > 0.002:
        raise ValueError('Wall reports a hole area without a measurable opening')

    try:
        before_desc = {h: c.describe(e) for h, e in before.items()}
    except Exception as exc:
        raise OutcomeUnknown('Existing entities could not be read before the split') from exc

    centre, view_size, view_rect = drawing_view(c)
    edge = frame['line'] + (frame['thick'] / 2.0 if across >= frame['line']
                            else -frame['thick'] / 2.0)
    pick_point = (edge, station) if frame['vertical'] else (station, edge)
    try:
        menu = menu_dispatch(c, ('墙体', '墙体切割'))
        time.sleep(0.4)
        placement = pick_world_point(c, centre, view_size, view_rect, pick_point,
                                     backend=backend)
        wait_command_idle(c)
        after = c.entities()
        after_desc = {h: c.describe(e) for h, e in after.items()}
        result = _verify_split(c, handle, before, before_desc, after, after_desc,
                               frame, station, openings, placement, menu)
    except CommandCompletionTimeout:
        raise
    except Exception as exc:
        # Any failure after the menu dispatch leaves the drawing in an unknown
        # state: the native command may have cut the wall, half-cut it or still
        # be waiting for a pick. The engine turns this into quarantine plus its
        # existing ESC recovery; nothing here closes or saves the drawing.
        raise CommandCompletionTimeout('Wall split outcome is unconfirmed: %s' % exc) from exc
    c.created_walls.pop(handle, None)
    return result


def _validate_move_target(c, args):
    if args['document_id'] != c.document_id:
        raise ValueError('Entity document_id mismatch')
    handle = args['handle'].upper()
    return handle, c.entity(handle, args['document_id'])


def _run_move_command(c, handle, origin, delta):
    bx, by = float(origin[0]), float(origin[1])
    tx, ty = bx + float(delta[0]), by + float(delta[1])
    lisp = ('(progn (command "_.MOVE" (handent "%s") "" "_non" (list %.6f %.6f 0.0) '
            '"_non" (list %.6f %.6f 0.0)) (princ))\n' % (handle, bx, by, tx, ty))
    c.run_command(lisp)
    return (bx, by), (tx, ty)


def move_column(c, args, before):
    """Translate a standalone rectangular native column via atomic LISP _.MOVE."""
    handle, col = _validate_move_target(c, args)
    if not is_rectangular_column(col):
        raise ValueError('column.move requires a rectangular native column')
    before_dxf = column_dxf(c, handle)
    if (before_dxf['type'] != 'TCH_COLUMN' or before_dxf['insertion'] is None
            or len(before_dxf['vertices']) != 4):
        raise OutcomeUnknown('Column DXF geometry could not be read before move')
    dx, dy = float(args['delta'][0]), float(args['delta'][1])
    origin, target = _run_move_command(c, handle, before_dxf['insertion'], (dx, dy))
    try:
        after = c.entities()
        if set(after) != set(before):
            raise OutcomeUnknown('Entity handles changed during column.move')
        after_dxf = column_dxf(c, handle)
        if (after_dxf['type'] != 'TCH_COLUMN' or after_dxf['insertion'] is None
                or len(after_dxf['vertices']) != 4):
            raise OutcomeUnknown('Column DXF geometry could not be read after move')
        if (abs((after_dxf['insertion'][0] - origin[0]) - dx) > 0.01
                or abs((after_dxf['insertion'][1] - origin[1]) - dy) > 0.01):
            raise OutcomeUnknown('Column insertion shift mismatch after move: %r -> %r'
                                 % (origin, after_dxf['insertion']))
        for b_pt, a_pt in zip(before_dxf['vertices'], after_dxf['vertices']):
            if (abs((a_pt[0] - b_pt[0]) - dx) > 0.01
                    or abs((a_pt[1] - b_pt[1]) - dy) > 0.01):
                raise OutcomeUnknown('Column vertex shift mismatch after move')
        if after_dxf['height'] != before_dxf['height'] or after_dxf['flags'] != before_dxf['flags']:
            raise OutcomeUnknown('Column properties changed unexpectedly during move')
        info = c.describe(after[handle])
        return {
            'success': True,
            'document_id': c.document_id,
            'handles': [handle],
            'entities': [info],
            'evidence': {
                'move': {
                    'handle': handle,
                    'delta': [dx, dy],
                    'insertion_before': list(origin),
                    'insertion_target': list(target),
                    'insertion_after': list(after_dxf['insertion']),
                    'vertices_before': [list(p) for p in before_dxf['vertices']],
                    'vertices_after': [list(p) for p in after_dxf['vertices']],
                    'height': after_dxf['height'],
                    'flags': list(after_dxf['flags']),
                },
            },
            'validation': {'type': True, 'properties': True,
                           'geometry': 'verified_dxf_10_and_11',
                           'reopen': 'pending'},
        }
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(str(exc)) from exc


def move_opening(c, args, before):
    """Translate an ordinary opening along its verified TDbWall host axis."""
    handle, opening = _validate_move_target(c, args)
    if opening.ObjectName != 'TDbOpening':
        raise ValueError('opening.move requires a native opening')
    before_dxf = opening_dxf(c, handle)
    if before_dxf['type'] != 'TCH_OPENING' or before_dxf['insertion'] is None:
        raise OutcomeUnknown('Opening DXF geometry could not be read before move')
    host_handle, _ = _unique_wall_host(c, handle)
    host_wall = c.entity(host_handle, args['document_id'])
    frame = _split_frame(c.describe(host_wall))

    dx, dy = float(args['delta'][0]), float(args['delta'][1])
    if frame['vertical']:
        if abs(dx) > 1e-6 or abs(dy) <= 1e-6:
            raise ValueError('Opening move delta must align with the host wall axis')
    elif abs(dy) > 1e-6 or abs(dx) <= 1e-6:
        raise ValueError('Opening move delta must align with the host wall axis')

    bx, by = before_dxf['insertion']
    tx, ty = bx + dx, by + dy
    target_station = ty if frame['vertical'] else tx
    width = float(before_dxf['width'] if before_dxf['width'] is not None else getattr(opening, 'Width'))
    half_w = width / 2.0
    low, high = frame['span']
    if target_station - half_w < low + SPLIT_CLEARANCE_MM or target_station + half_w > high - SPLIT_CLEARANCE_MM:
        raise ValueError('Opening target position is too close to the wall end')

    target_low = target_station - half_w - SPLIT_CLEARANCE_MM
    target_high = target_station + half_w + SPLIT_CLEARANCE_MM
    for other in _split_openings(c, frame):
        if other['handle'] == handle:
            continue
        if min(target_high, other['interval'][1]) - max(target_low, other['interval'][0]) > 0:
            raise ValueError('Opening target position overlaps another opening')

    holes_before = hole_areas(c)
    origin, target = _run_move_command(c, handle, (bx, by), (dx, dy))
    try:
        after = c.entities()
        if set(after) != set(before):
            raise OutcomeUnknown('Entity handles changed during opening.move')
        after_dxf = opening_dxf(c, handle)
        if after_dxf['type'] != 'TCH_OPENING' or after_dxf['insertion'] is None:
            raise OutcomeUnknown('Opening DXF geometry could not be read after move')
        if (abs((after_dxf['insertion'][0] - origin[0]) - dx) > 0.5
                or abs((after_dxf['insertion'][1] - origin[1]) - dy) > 0.5):
            raise OutcomeUnknown('Opening insertion shift mismatch after move: %r -> %r'
                                 % (origin, after_dxf['insertion']))
        for prop in ('width', 'height', 'rotation'):
            if (before_dxf[prop] is None or after_dxf[prop] is None
                    or abs(float(after_dxf[prop]) - float(before_dxf[prop])) > 0.01):
                raise OutcomeUnknown('Opening %s changed during move: %r -> %r'
                                     % (prop, before_dxf[prop], after_dxf[prop]))
        after_host, _ = _unique_wall_host(c, handle)
        if after_host != host_handle:
            raise OutcomeUnknown('Opening host changed during move: %s -> %s'
                                 % (host_handle, after_host))
        holes_after = hole_areas(c)
        if set(holes_after) != set(holes_before) or any(
                abs(holes_after[h] - holes_before[h]) > 0.02 for h in holes_before):
            raise OutcomeUnknown('Wall hole area changed during opening.move')
        info = c.describe(after[handle])
        return {
            'success': True,
            'document_id': c.document_id,
            'handles': [handle],
            'entities': [info],
            'evidence': {
                'move': {
                    'handle': handle,
                    'host_handle': host_handle,
                    'delta': [dx, dy],
                    'insertion_before': list(origin),
                    'insertion_target': list(target),
                    'insertion_after': list(after_dxf['insertion']),
                    'width': after_dxf['width'],
                    'height': after_dxf['height'],
                    'rotation': after_dxf['rotation'],
                    'host_hole_area_before': holes_before[host_handle],
                    'host_hole_area_after': holes_after[host_handle],
                },
            },
            'validation': {'type': True, 'properties': True,
                           'geometry': 'verified_dxf_10',
                           'host': 'verified_dxf_330_and_hole_area',
                           'reopen': 'pending'},
        }
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(str(exc)) from exc

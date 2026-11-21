import json
import math
import re
from pathlib import Path

MAX_READBACK_HANDLES = 100
# One page of ModelSpace discovery: bounds a single COM read of a saved drawing.
MAX_LIST_LIMIT = 100

SCHEMAS = {
    'document.new': ({'path'}, set()),
    'document.save': (set(), set()),
    'document.reopen': (set(), set()),
    'document.close': (set(), set()),
    'document.readback': ({'path', 'handles'}, set()),
    'document.list': ({'path'}, {'limit', 'after_handle'}),
    'entity.get': ({'handle'}, {'document_id'}),
    'entity.verify': ({'handles', 'object_name'}, {'document_id'}),
    'wall.create': ({'start', 'end'}, {'left_width', 'right_width'}),
    'wall.find': ({'point'}, set()),
    'opening.create': ({'host', 'point', 'kind', 'subkind'}, {'width', 'height', 'sill'}),
    'stair.create': ({'point'}, {'height', 'steps', 'step_width', 'width'}),
    'stair.rect_create': ({'point','height','steps','step_width','width'}, {'rotation_deg'}),
    'column.create': ({'point','width','depth','height'}, set()),
    'slab.create': ({'boundary','thickness'}, {'holes'}),
    'annotation.text': ({'text','point','height'}, set()),
    'polyline.create': ({'points'}, {'closed'}),
    'roof.create': ({'boundary', 'angle', 'overhang'}, set()),
}


def point(value):
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise ValueError('Point requires 2 or 3 finite numbers')
    out = list(value) + ([0] if len(value) == 2 else [])
    for n in out:
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n):
            raise ValueError('Coordinates must be finite numbers')
    return [float(n) for n in out]


def prepare(op, args):
    if op not in SCHEMAS:
        raise ValueError('Unsupported operation')
    required, optional = SCHEMAS[op]
    if not required <= args.keys() or args.keys() - required - optional:
        raise ValueError(f'{op}: requires {sorted(required)}, optional {sorted(optional)}')
    out = dict(args)
    for key in ('point', 'start', 'end'):
        if key in out:
            out[key] = point(out[key])
    if op == 'wall.create' and out['start'] == out['end']:
        raise ValueError('Wall must have nonzero length')
    if op == 'document.readback':
        if not isinstance(out['path'], str) or not out['path'].strip():
            raise ValueError('Readback path must be a non-empty string')
        candidate = Path(out['path'])
        if not candidate.is_absolute() or candidate.suffix.lower() != '.dwg':
            raise ValueError('Readback path must be an absolute DWG path')
        handles = out['handles']
        if not isinstance(handles, list) or not 1 <= len(handles) <= MAX_READBACK_HANDLES:
            raise ValueError(f'Readback requires 1..{MAX_READBACK_HANDLES} handles')
        for handle in handles:
            if not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle):
                raise ValueError('Readback handles must be hexadecimal strings')
    if op == 'document.list':
        if not isinstance(out['path'], str) or not out['path'].strip():
            raise ValueError('Discovery path must be a non-empty string')
        candidate = Path(out['path'])
        if not candidate.is_absolute() or candidate.suffix.lower() != '.dwg':
            raise ValueError('Discovery path must be an absolute DWG path')
        # A bool or a fractional number would silently change how far one COM
        # read reaches, so the type is checked before the range.
        limit = out.setdefault('limit', MAX_LIST_LIMIT)
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f'Discovery limit must be an integer 1..{MAX_LIST_LIMIT}')
        if 'after_handle' in out:
            handle = out['after_handle']
            if not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle):
                raise ValueError('after_handle must be a hexadecimal handle without 0x prefix')
    if op == 'annotation.text':
        if not isinstance(out['text'],str) or not 0<len(out['text'])<=160 or any(c in out['text'] for c in '\r\n\x00'):
            raise ValueError('Annotation must be a short single-line string')
    if op == 'polyline.create':
        if not isinstance(out['points'],list) or not 2<=len(out['points'])<=100:raise ValueError('Polyline needs 2..100 points')
        out['points']=[point(p) for p in out['points']]
        if any(p[2]!=0 for p in out['points']) or len({tuple(p) for p in out['points']})<2:raise ValueError('Plan polyline needs distinct Z=0 points')
        if type(out.get('closed',False)) is not bool:raise ValueError('closed must be boolean')
    if op in ('roof.create','slab.create'):
        if not isinstance(out['boundary'], list) or len(out['boundary']) < 3:
            raise ValueError('Roof requires at least 3 boundary points')
        out['boundary'] = [point(p) for p in out['boundary']]
        if op == 'roof.create' and not 0 < out['angle'] < 90:
            raise ValueError('Roof angle must be between 0 and 90')
    for key in ('width', 'height', 'left_width', 'right_width', 'step_width', 'steps', 'sill', 'overhang', 'depth','thickness'):
        if key in out:
            n = out[key]
            if isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) or n < 0:
                raise ValueError(f'{key} must be nonnegative finite number')
    if op == 'opening.create':
        host = out['host']
        if not isinstance(host, dict) or not {'handle', 'document_id'} <= host.keys():
            raise ValueError('host must be an entity reference including handle and document_id')
        for key in ('kind', 'subkind'):
            if out[key] not in ('普通门','普通窗'):
                raise ValueError('Only measured ordinary door/window kind strings are supported')
        # kind and subkind both come from the created object (GetKind /
        # GetSubKind) and are equal for the two measured plain types; a mixed
        # pair would only be caught after the panel had already been driven.
        if out['kind'] != out['subkind']:
            raise ValueError('kind and subkind must name the same measured type')
    if op in ('column.create','slab.create','stair.rect_create','annotation.text'):
        if any(out[k] <= 0 for k in ('width','depth','height','thickness','step_width') if k in out):
            raise ValueError('Part dimensions must be positive')
    if op == 'stair.rect_create':
        if type(out['steps']) is not int or out['steps'] < 4 or out['steps']%2:
            raise ValueError('Rectangular stair requires an even integer total step count')
        rotation=out.get('rotation_deg',0)
        if isinstance(rotation,bool) or not isinstance(rotation,(int,float)) or not math.isfinite(rotation):
            raise ValueError('rotation_deg must be finite')
    if op == 'slab.create':
        def rectangle(points):
            if any(abs(p[2])>1e-9 for p in points):raise ValueError('Current plan slab adapter supports Z=0 only')
            if len({tuple(p[:2]) for p in points})!=4:raise ValueError('Rectangle requires four distinct corners')
            if len(points)!=4 or len({p[0] for p in points})!=2 or len({p[1] for p in points})!=2:
                raise ValueError('Current slab adapter requires axis-aligned rectangular boundaries')
            if any(points[i][0]!=points[(i+1)%4][0] and points[i][1]!=points[(i+1)%4][1] for i in range(4)):
                raise ValueError('Rectangle vertices must follow perimeter order')
        rectangle(out['boundary'])
        holes=out.get('holes',[])
        if not isinstance(holes,list) or len(holes)>1:raise ValueError('At most one verified stair opening is supported')
        out['holes']=[[point(p) for p in h] for h in holes]
        lo=[min(p[i] for p in out['boundary']) for i in (0,1)];hi=[max(p[i] for p in out['boundary']) for i in (0,1)]
        for hole in out['holes']:
            rectangle(hole)
            if any(not all(lo[i]<p[i]<hi[i] for i in (0,1)) for p in hole):
                raise ValueError('Slab hole must stay strictly inside boundary')
    return out


def command_payload(op, args, *, boundary_handle=None):
    def coord(p):
        return ','.join(str(x) for x in p)
    if op == 'wall.create':
        # First Enter ends this wall chain; the second exits the outer loop
        # asking for another starting point (observed on this T20 V9 build).
        tokens = ['TGWall', coord(args['start']), coord(args['end']), '', '']
        # Invoke Tianzheng directly. A nested (command ...) leaves completion
        # AutoLISP unavailable on the fourth joining segment on T20 V9/2024.
        return '\n'.join(tokens) + '\n'
    elif op == 'opening.create':
        return '\n'.join(['TOpening', coord(args['point'][:2]), '', ''])
    elif op == 'column.create':
        return '\n'.join(['TGColumn',coord(args['point'][:2]),'',''])
    elif op == 'stair.rect_create':
        return '\n'.join(['TRStair',coord(args['point'][:2]),'',''])
    elif op == 'stair.create':
        tokens = ['TLStair', coord(args['point']), '']
    elif op == 'roof.create':
        if not isinstance(boundary_handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', boundary_handle):
            raise ValueError('Exact roof boundary handle required')
        # Selection evaluates handent at the native prompt, without wrapping
        # the Tianzheng command in another AutoLISP command execution context.
        return f'TSlopeRoof\n(handent "{boundary_handle}")\n{args["angle"]}\n{args["overhang"]}\n'
    else:
        raise ValueError('No generic payload for this operation')
    encoded = ' '.join(json.dumps(t, ensure_ascii=False) for t in tokens)
    return f'(progn (command {encoded}) (princ))\n'

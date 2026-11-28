import json
import math
import re
from pathlib import Path

MAX_READBACK_HANDLES = 100
# One page of ModelSpace discovery: bounds a single COM read of a saved drawing.
MAX_LIST_LIMIT = 100
HATCH_PATTERNS = frozenset({'SOLID', 'ANSI31', 'ANSI37', 'NET', 'AR-CONC'})

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
    'wall.split': ({'path', 'document_id', 'handle', 'point'}, set()),
    'wall.edit': ({'document_id', 'handle'}, {'left_width', 'right_width', 'height'}),
    'opening.create': ({'host', 'point', 'kind', 'subkind'}, {'width', 'height', 'sill'}),
    'opening.move': ({'document_id', 'handle', 'delta'}, set()),
    'opening.edit': ({'document_id', 'handle'}, {'width', 'height', 'sill', 'text_height'}),
    'opening.group_create': ({'document_id', 'handles', 'label'}, set()),
    'opening.relabel': ({'document_id', 'handle', 'label'}, set()),
    'opening.mirror': ({'document_id', 'handle', 'direction'}, set()),
    'stair.create': ({'point'}, {'height', 'steps', 'step_width', 'width'}),
    'stair.rect_create': ({'point','height','steps','step_width','width'}, {'rotation_deg'}),
    'column.create': ({'point','width','depth','height'}, set()),
    'column.edit': ({'document_id', 'handle'}, {'width', 'depth', 'height'}),
    'column.move': ({'document_id', 'handle', 'delta'}, set()),
    'column.rotate': ({'document_id', 'handle', 'angle_deg'}, set()),
    'entity.copy': ({'document_id', 'handle', 'delta'}, set()),
    'entity.move': ({'document_id', 'handle', 'delta'}, set()),
    'entity.rotate': ({'document_id', 'handle', 'base_point', 'angle_deg'}, set()),
    'entity.scale': ({'document_id', 'handle', 'base_point', 'scale_factor'}, set()),
    'entity.mirror': ({'document_id', 'handle', 'axis_start', 'axis_end'}, {'erase_source'}),
    'entity.set_layer': ({'document_id', 'handle', 'layer'}, set()),
    'entity.set_color': ({'document_id', 'handle', 'color'}, set()),
    'entity.query': (set(), {'object_name', 'layer', 'bbox', 'limit'}),
    'entity.delete': ({'document_id', 'handle'}, set()),
    'slab.create': ({'boundary','thickness'}, {'holes'}),
    'slab.edit': ({'document_id', 'handle'}, {'thickness', 'elevation'}),
    'annotation.text': ({'text','point','height'}, set()),
    'annotation.mtext': ({'text', 'point', 'width'}, {'height', 'rotation_deg', 'attachment'}),
    'annotation.text_edit': ({'document_id', 'handle'}, {'text', 'height', 'rotation_deg'}),
    'line.create': ({'start', 'end'}, set()),
    'circle.create': ({'center', 'radius'}, set()),
    'arc.create': ({'center', 'radius', 'start_angle_deg', 'end_angle_deg'}, set()),
    'ellipse.create': ({'center', 'major_axis', 'radius_ratio'}, set()),
    'hatch.create': ({'document_id', 'boundary_handle'}, {'pattern_name', 'pattern_scale', 'pattern_angle_deg'}),
    'layer.create': ({'name'}, {'color'}),
    'layer.delete': ({'name'}, set()),
    'layer.list': (set(), {'prefix'}),
    'layer.edit': ({'name'}, {'color', 'layer_on', 'freeze', 'lock'}),
    'layer.set_active': ({'name'}, set()),
    'block.define': ({'name', 'items'}, {'base_point'}),
    'block.delete': ({'name'}, set()),
    'block.insert': ({'name', 'point'}, {'scale_x', 'scale_y', 'scale_z', 'rotation_deg'}),
    'block.list': (set(), {'prefix', 'include_system'}),
    'polyline.create': ({'points'}, {'closed'}),
    'polyline.edit': ({'document_id', 'handle'}, {'closed', 'constant_width', 'elevation'}),
    'rectangle.create': ({'corner1', 'corner2'}, {'elevation', 'constant_width'}),
    'roof.create': ({'boundary', 'angle', 'overhang'}, set()),
    'roof.edit': ({'document_id', 'handle'}, {'elevation', 'slope_angle'}),
    'axis.rect_create': ({'origin', 'bays', 'depths'}, {'axis_angle'}),
    'axis.from_walls': ({'document_id', 'wall_handles'}, set()),
    'axis.single_create': ({'start', 'end'}, {'overhang'}),
    'axis.annotate': ({'start_point', 'end_point'}, {'side', 'start_label'}),
    'space.create': ({'point'}, set()),
    'space.edit': ({'document_id', 'handle', 'name'}, set()),
    'dim.wall': ({'start', 'end'}, set()),
    'dim.t20_angular': ({'first_pick', 'second_pick', 'arc_point'}, set()),
    'dim.aligned': ({'ext1', 'ext2', 'text_position'}, {'text_height'}),
    'dim.linear': ({'ext1', 'ext2', 'dim_line_point'}, {'rotation_deg', 'text_height'}),
    'dim.radial': ({'center', 'chord_point'}, {'leader_length', 'text_height'}),
    'dim.angular': ({'vertex', 'first_endpoint', 'second_endpoint', 'text_position'}, {'text_height'}),
    'dim.diameter': ({'chord_point', 'far_chord_point'}, {'leader_length', 'text_height'}),
    'wall.fix_corner': ({'corner1', 'corner2'}, set()),
}


def point(value):
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise ValueError('Point requires 2 or 3 finite numbers')
    out = list(value) + ([0] if len(value) == 2 else [])
    for n in out:
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n):
            raise ValueError('Coordinates must be finite numbers')
    return [float(n) for n in out]


def _prepare_rect_axis(out):
    try:
        out['origin'] = point(out['origin'])
        for key in ('bays', 'depths'):
            values = out[key]
            if not isinstance(values, list) or not 1 <= len(values) <= 30:
                raise ValueError(f'{key} must be a list of 1..30 spacings')
            if any(type(v) not in (int, float) or v <= 0 or not math.isfinite(v)
                   for v in values):
                raise ValueError(f'{key} spacings must be positive finite numbers')
            out[key] = [float(v) for v in values]
    except OverflowError as exc:
        raise ValueError('Axis coordinates and spacings must be finite numbers') from exc
    if out['origin'][2] != 0:
        raise ValueError('Axis origin must lie in the plan (Z=0)')
    angle = out.get('axis_angle', 90.0)
    if type(angle) not in (int, float) or not 0 < angle < 180:
        raise ValueError('axis_angle must be a finite number between 0 and 180')
    out['axis_angle'] = float(angle)
    return out


def _prepare_wall_axis(out):
    if not isinstance(out['document_id'], str) or not out['document_id'].strip():
        raise ValueError('Axis source document_id must be a non-empty string')
    handles = out['wall_handles']
    if not isinstance(handles, list) or not 1 <= len(handles) <= 100:
        raise ValueError('wall_handles must be a list of 1..100 handles')
    if any(not isinstance(h, str) or not re.fullmatch(r'[0-9A-Fa-f]+', h) for h in handles):
        raise ValueError('wall_handles must be hexadecimal strings without 0x prefix')
    out['wall_handles'] = [h.upper() for h in handles]
    if len({h.lstrip('0') for h in out['wall_handles']}) != len(handles):
        raise ValueError('wall_handles must be unique')
    return out


def _prepare_group_opening(out):
    document_id = out['document_id']
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError('opening.group_create document_id must be a non-empty string')
    handles = out['handles']
    if not isinstance(handles, list) or not 2 <= len(handles) <= 10:
        raise ValueError('opening.group_create handles must be a list of 2..10 handles')
    if any(not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle)
           for handle in handles):
        raise ValueError('opening.group_create handles must be hexadecimal strings without 0x prefix')
    handles = [handle.upper() for handle in handles]
    if len({handle.lstrip('0') for handle in handles}) != len(handles):
        raise ValueError('opening.group_create handles must be unique')
    label = out['label']
    if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 64
            or any(ord(ch) < 32 or ord(ch) == 127 or ch == '"' for ch in label)
            or any(ch.isspace() for ch in label.strip())):
        raise ValueError('opening.group_create label must contain 1..64 characters without whitespace, '
                         'quotes, or control characters after trimming')
    return {**out, 'document_id': document_id.strip(), 'handles': handles, 'label': label.strip()}


def _plan_points(out, first_key, second_key, label):
    try:
        first, second = point(out[first_key]), point(out[second_key])
    except OverflowError as exc:
        raise ValueError(f'{label} coordinates must be finite numbers') from exc
    if first[2] != 0 or second[2] != 0:
        raise ValueError(f'{label} points must lie in the plan (Z=0)')
    return first, second


def _prepare_axis_points(out, start_key, end_key, label='Axis'):
    start, end = _plan_points(out, start_key, end_key, label)
    if math.hypot(end[0] - start[0], end[1] - start[1]) <= 1e-6:
        raise ValueError(f'{label} points must be distinct')
    out[start_key], out[end_key] = start, end


def _validate_annotation_text(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or any(c in value for c in '\r\n\x00'):
        raise ValueError('Annotation must be a short single-line string')


def _finite_number(value, error):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(error)
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(error) from exc
    if not math.isfinite(number):
        raise ValueError(error)
    return number


def _prepare_plan_point(op, out, key):
    try:
        coords = point(out[key])
    except OverflowError as exc:
        raise ValueError(f'{op} {key} coordinates must be finite numbers') from exc
    if coords[2] != 0:
        raise ValueError(f'{op} {key} must lie in the plan (Z=0)')
    out[key] = coords


def _positive_number(op, key, value):
    error = f'{op} {key} must be a positive finite number'
    number = _finite_number(value, error)
    if number <= 0:
        raise ValueError(error)
    return number


def _prepare_mtext(out):
    op = 'annotation.mtext'
    value = out['text']
    if (not isinstance(value, str) or not 1 <= len(value) <= 2000 or not value.strip()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ValueError('annotation.mtext text requires 1..2000 characters without control characters')
    _prepare_plan_point(op, out, 'point')
    out['width'] = _positive_number(op, 'width', out['width'])
    if 'height' in out:
        out['height'] = _positive_number(op, 'height', out['height'])
    if 'rotation_deg' in out:
        out['rotation_deg'] = _finite_number(out['rotation_deg'], 'annotation.mtext rotation_deg must be finite')
    if 'attachment' in out:
        attachment = out['attachment']
        if type(attachment) is not int or not 1 <= attachment <= 9:
            raise ValueError('annotation.mtext attachment must be an integer in 1..9')
    return out


def _prepare_angular_dimension(out):
    op = 'dim.angular'
    for key in ('vertex', 'first_endpoint', 'second_endpoint', 'text_position'):
        _prepare_plan_point(op, out, key)
    x, y = out['vertex'][:2]
    x1, y1 = out['first_endpoint'][:2]
    x2, y2 = out['second_endpoint'][:2]
    dx1, dy1 = x1 - x, y1 - y
    dx2, dy2 = x2 - x, y2 - y
    len1, len2 = math.hypot(dx1, dy1), math.hypot(dx2, dy2)
    if not all(math.isfinite(length) and length > 1e-6 for length in (len1, len2)):
        raise ValueError(f'{op} rays must have finite length greater than 1e-6')
    # Normalize before multiplying to avoid overflow for long rays.
    cross = (dx1 / len1) * (dy2 / len2) - (dy1 / len1) * (dx2 / len2)
    if abs(cross) <= 1e-6:
        raise ValueError(f'{op} rays must be non-collinear')
    if 'text_height' in out:
        out['text_height'] = _positive_number(op, 'text_height', out['text_height'])
    return out


def _prepare_dimension(op, out):
    if op in ('dim.radial', 'dim.diameter'):
        first, second = ('center', 'chord_point') if op == 'dim.radial' else ('chord_point', 'far_chord_point')
        _prepare_plan_point(op, out, first)
        _prepare_plan_point(op, out, second)
        span = math.dist(out[first][:2], out[second][:2])
        if span <= 1e-6:
            raise ValueError(f'{op} requires distinct {first} and {second}')
        error = f'{op} leader_length must be a non-negative finite number'
        leader_length = _finite_number(out.get('leader_length', 0.0), error)
        if leader_length < 0:
            raise ValueError(error)
        out['leader_length'] = leader_length
    else:
        _prepare_plan_point(op, out, 'ext1')
        _prepare_plan_point(op, out, 'ext2')
        ext1, ext2 = out['ext1'], out['ext2']
        if op == 'dim.aligned':
            _prepare_plan_point(op, out, 'text_position')
            span = math.dist(ext1[:2], ext2[:2])
            if span <= 1e-6:
                raise ValueError('dim.aligned requires distinct ext1 and ext2 points')
        else:
            _prepare_plan_point(op, out, 'dim_line_point')
            out['rotation_deg'] = _finite_number(out.get('rotation_deg', 0.0), 'dim.linear rotation_deg must be finite')
            rad = math.radians(out['rotation_deg'])
            span = abs((ext2[0] - ext1[0]) * math.cos(rad) + (ext2[1] - ext1[1]) * math.sin(rad))
            if span <= 1e-6:
                raise ValueError('dim.linear projected measurement along rotation_deg must be nonzero')
    if not math.isfinite(span):
        raise ValueError(f'{op} measurement must be finite')
    if 'text_height' in out:
        out['text_height'] = _positive_number(op, 'text_height', out['text_height'])
    return out


def _prepare_ellipse(out):
    op = 'ellipse.create'
    _prepare_plan_point(op, out, 'center')
    _prepare_plan_point(op, out, 'major_axis')
    major_radius = math.hypot(*out['major_axis'][:2])
    if not math.isfinite(major_radius) or major_radius <= 1e-6:
        raise ValueError(f'{op} major_axis must have finite length greater than 1e-6')
    error = f'{op} radius_ratio must be a finite number in (1e-6, 1]'
    ratio = _finite_number(out['radius_ratio'], error)
    if not 1e-6 < ratio <= 1.0:
        raise ValueError(error)
    out['radius_ratio'] = ratio
    return out


def _validate_layer_name(value, label='Layer name'):
    name = value.strip() if isinstance(value, str) else ''
    if not 1 <= len(name) <= 255:
        raise ValueError(f'{label} must contain 1..255 characters after trimming')
    if any(ch in '<>/"\\:;?*|=' or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f'{label} contains a forbidden character')
    return name


def _validate_block_name(value):
    if isinstance(value, str) and value.strip().startswith('*'):
        raise ValueError('Block name must not start with *')
    return _validate_layer_name(value, 'Block name')


def _prepare_prefix(op, out):
    if 'prefix' in out:
        prefix = out['prefix']
        if (not isinstance(prefix, str) or not 1 <= len(prefix.strip()) <= 255
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in prefix)):
            raise ValueError(f'{op} prefix must contain 1..255 characters without control characters')
        out['prefix'] = prefix.strip()
    return out


def _prepare_arc_angles(op, out, tolerance):
    for key in ('start_angle_deg', 'end_angle_deg'):
        out[key] = _finite_number(out[key], f'{op} {key} must be a finite number')
    start, end = out['start_angle_deg'], out['end_angle_deg']
    delta = end - start
    if not math.isfinite(delta):
        delta = end % 360.0 - start % 360.0
    if not tolerance < delta % 360.0 < 360.0 - tolerance:
        raise ValueError(f'{op} requires a nonzero sweep smaller than 360 degrees')


def _prepare_round_primitive(op, out):
    _prepare_plan_point(op, out, 'center')
    out['radius'] = _positive_number(op, 'radius', out['radius'])
    if op == 'arc.create':
        _prepare_arc_angles(op, out, tolerance=1e-6)
    return out


_BLOCK_ITEM_SCHEMAS = {
    'line': ({'start', 'end'}, set()),
    'circle': ({'center', 'radius'}, set()),
    'arc': ({'center', 'radius', 'start_angle_deg', 'end_angle_deg'}, set()),
    'polyline': ({'points'}, {'closed'}),
    'text': ({'text', 'point', 'height'}, set()),
}


def _prepare_block_polyline(item, label):
    closed = item.setdefault('closed', False)
    if type(closed) is not bool:
        raise ValueError(f'{label} closed must be boolean')
    points = item['points']
    minimum = 3 if closed else 2
    if not isinstance(points, list) or not minimum <= len(points) <= 100:
        raise ValueError(f'{label} requires {minimum}..100 points')
    try:
        points = [point(value) for value in points]
    except OverflowError as exc:
        raise ValueError(f'{label} coordinates must be finite numbers') from exc
    if any(p[2] != 0 for p in points):
        raise ValueError(f'{label} points must lie in the plan (Z=0)')
    if any(a == b for a, b in zip(points, points[1:])) or (closed and points[-1] == points[0]):
        raise ValueError(f'{label} adjacent points must be distinct')
    item['points'] = points


def _prepare_block_item(value, index):
    label = f'block.define items[{index}]'
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be a primitive descriptor')
    kind = value.get('kind')
    if not isinstance(kind, str) or kind not in _BLOCK_ITEM_SCHEMAS:
        raise ValueError(f'{label} has an unsupported kind: {kind!r}')
    required, optional = _BLOCK_ITEM_SCHEMAS[kind]
    if not required <= value.keys() or value.keys() - required - optional - {'kind'}:
        raise ValueError(f'{label} {kind} requires {sorted(required)}, optional {sorted(optional)}')
    item = dict(value)
    if kind == 'line':
        start, end = _plan_points(item, 'start', 'end', label)
        if start == end:
            raise ValueError(f'{label} line endpoints must be distinct')
        item['start'], item['end'] = start, end
    elif kind in ('circle', 'arc'):
        _prepare_plan_point(label, item, 'center')
        item['radius'] = _positive_number(label, 'radius', item['radius'])
        if kind == 'arc':
            _prepare_arc_angles(label, item, tolerance=0.0)
    elif kind == 'polyline':
        _prepare_block_polyline(item, label)
    elif kind == 'text':
        _validate_annotation_text(item['text'])
        _prepare_plan_point(label, item, 'point')
        item['height'] = _positive_number(label, 'height', item['height'])
    return item


def _prepare_block(op, out):
    if op == 'block.list':
        if type(out.setdefault('include_system', False)) is not bool:
            raise ValueError('block.list include_system must be boolean')
        return _prepare_prefix(op, out)
    out['name'] = _validate_block_name(out['name'])
    if op == 'block.define':
        out.setdefault('base_point', [0.0, 0.0, 0.0])
        _prepare_plan_point(op, out, 'base_point')
        items = out['items']
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise ValueError('block.define items must be a list of 1..50 primitives')
        out['items'] = [_prepare_block_item(item, index) for index, item in enumerate(items)]
    else:
        _prepare_plan_point(op, out, 'point')
        for key in ('scale_x', 'scale_y', 'scale_z'):
            out[key] = _positive_number(op, key, out.get(key, 1.0))
        out['rotation_deg'] = _finite_number(
            out.get('rotation_deg', 0.0), 'block.insert rotation_deg must be a finite number')
    return out


def _prepare_polyline_properties(op, out):
    if 'closed' in out and type(out['closed']) is not bool:
        raise ValueError(f'{op} closed must be boolean')
    if 'constant_width' in out:
        error = f'{op} constant_width must be a non-negative finite number'
        out['constant_width'] = _finite_number(out['constant_width'], error)
        if out['constant_width'] < 0:
            raise ValueError(error)
    if 'elevation' in out:
        out['elevation'] = _finite_number(out['elevation'], f'{op} elevation must be a finite number')
    return out


def _prepare_entity_query(out):
    if 'object_name' in out:
        value = out['object_name']
        name = value.strip() if isinstance(value, str) else ''
        if not 1 <= len(name) <= 128 or re.search(r'[\x00-\x1f\x7f-\x9f]', value):
            raise ValueError('entity.query object_name must contain 1..128 characters without control characters')
        out['object_name'] = name
    if 'layer' in out:
        out['layer'] = _validate_layer_name(out['layer'])
    if 'bbox' in out:
        bbox = out['bbox']
        if not isinstance(bbox, list) or len(bbox) != 2:
            raise ValueError('entity.query bbox must be a list of two plan points')
        corners = {'min_pt': bbox[0], 'max_pt': bbox[1]}
        lo, hi = _plan_points(corners, 'min_pt', 'max_pt', 'entity.query bbox')
        if lo[0] > hi[0] or lo[1] > hi[1]:
            raise ValueError('entity.query bbox min_pt must be at or below max_pt on both axes')
        out['bbox'] = [lo, hi]
    limit = out.setdefault('limit', MAX_LIST_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f'entity.query limit must be an integer 1..{MAX_LIST_LIMIT}')
    return out


def _prepare_hatch(out):
    document_id = out['document_id']
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError('hatch.create document_id must be a non-empty string')
    handle = out['boundary_handle']
    if not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle):
        raise ValueError('hatch.create boundary_handle must be a hexadecimal handle without 0x prefix')
    pattern = out.get('pattern_name', 'SOLID')
    pattern = pattern.strip().upper() if isinstance(pattern, str) else None
    if pattern not in HATCH_PATTERNS:
        raise ValueError(f'hatch.create pattern_name must be one of {", ".join(sorted(HATCH_PATTERNS))}')
    out.update(document_id=document_id.strip(), boundary_handle=handle.upper(), pattern_name=pattern)

    for key in ('pattern_scale', 'pattern_angle_deg'):
        if key not in out:
            continue
        if pattern == 'SOLID':
            raise ValueError(f'SOLID hatch does not accept {key}')
        out[key] = _finite_number(out[key], f'hatch.create {key} must be a finite number')
        if key == 'pattern_scale' and out[key] <= 0:
            raise ValueError('hatch.create pattern_scale must be positive')
    return out


def _prepare_com_edit(op, out, optional):
    keys = optional & out.keys()
    if not keys:
        raise ValueError(f'{op} requires at least one of {", ".join(sorted(optional))}')
    if 'text' in keys:
        _validate_annotation_text(out['text'])
    for key in sorted(keys - {'text'}):
        value = out[key]
        error = f'{op} {key} must be a finite number'
        number = _finite_number(value, error)
        if key == 'slope_angle' and not 0 < number < 90:
            raise ValueError('roof.edit slope_angle must be between 0 and 90 degrees')
        if key in ('height', 'width', 'depth', 'thickness') and number <= 0:
            raise ValueError(f'{op} {key} must be positive')
        out[key] = number
    return out


def _prepare_entity_transform(op, out):
    document_id = out['document_id']
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError(f'{op} document_id must be a non-empty string')
    handle = out['handle']
    if not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle):
        raise ValueError(f'{op} handle must be a hexadecimal handle without 0x prefix')
    out.update(document_id=document_id.strip(), handle=handle.upper())

    if op == 'entity.mirror':
        start, end = _plan_points(out, 'axis_start', 'axis_end', op)
        if math.dist(start[:2], end[:2]) <= 1e-6:
            raise ValueError('entity.mirror requires distinct axis_start and axis_end points')
        if type(out.setdefault('erase_source', False)) is not bool:
            raise ValueError('erase_source must be boolean')
        out.update(axis_start=start, axis_end=end)
        return out

    key = 'delta' if op == 'entity.move' else 'base_point'
    try:
        coords = point(out[key])
    except OverflowError as exc:
        raise ValueError(f'{op} {key} coordinates must be finite numbers') from exc
    if coords[2] != 0:
        raise ValueError(f'{op} {key} must lie in the plan (Z=0)')
    if op == 'entity.move':
        if max(abs(coords[0]), abs(coords[1])) <= 1e-6:
            raise ValueError(f'{op} delta must be nonzero')
    elif op == 'entity.rotate':
        angle = _finite_number(out['angle_deg'], f'{op} angle_deg must be a finite number')
        if not 1e-6 < angle % 360.0 < 360.0 - 1e-6:
            raise ValueError(f'{op} angle_deg must represent a nonzero rotation')
        out['angle_deg'] = angle
    else:
        error = 'entity.scale scale_factor must be positive, finite, and differ from 1 by more than 1e-6'
        factor = _finite_number(out['scale_factor'], error)
        if factor <= 0 or abs(factor - 1.0) <= 1e-6:
            raise ValueError(error)
        out['scale_factor'] = factor
    out[key] = coords
    return out


def prepare(op, args):
    if op not in SCHEMAS:
        raise ValueError('Unsupported operation')
    required, optional = SCHEMAS[op]
    if not required <= args.keys() or args.keys() - required - optional:
        raise ValueError(f'{op}: requires {sorted(required)}, optional {sorted(optional)}')
    out = dict(args)
    if op == 'annotation.mtext':
        return _prepare_mtext(out)
    if op == 'dim.angular':
        return _prepare_angular_dimension(out)
    if op == 'dim.t20_angular':
        for key in ('first_pick', 'second_pick', 'arc_point'):
            _prepare_plan_point(op, out, key)
        if math.dist(out['first_pick'][:2], out['second_pick'][:2]) <= 1e-6:
            raise ValueError('dim.t20_angular requires distinct first_pick and second_pick points')
        return out
    if op in ('dim.aligned', 'dim.linear', 'dim.radial', 'dim.diameter'):
        return _prepare_dimension(op, out)
    if op in ('entity.move', 'entity.rotate', 'entity.scale', 'entity.mirror'):
        return _prepare_entity_transform(op, out)
    if op in ('line.create', 'dim.wall'):
        _prepare_axis_points(out, 'start', 'end', op)
        return out
    if op in ('circle.create', 'arc.create'):
        return _prepare_round_primitive(op, out)
    if op == 'ellipse.create':
        return _prepare_ellipse(out)
    if op == 'rectangle.create':
        first, second = _plan_points(out, 'corner1', 'corner2', op)
        if any(abs(a - b) <= 1e-6 for a, b in zip(first[:2], second[:2])):
            raise ValueError('rectangle.create requires corner1 and corner2 with positive width and height')
        out['corner1'], out['corner2'] = first, second
        return _prepare_polyline_properties(op, out)
    if op == 'entity.query':
        return _prepare_entity_query(out)
    if op == 'hatch.create':
        return _prepare_hatch(out)
    if op == 'layer.list':
        return _prepare_prefix(op, out)
    if op in ('block.define', 'block.insert', 'block.list'):
        return _prepare_block(op, out)
    if op == 'block.delete':
        out['name'] = _validate_block_name(out['name'])
        return out
    if op == 'layer.delete':
        out['name'] = _validate_layer_name(out['name'])
        return out
    if op in ('layer.create', 'layer.edit', 'layer.set_active'):
        out['name'] = _validate_layer_name(out['name'])
        if op == 'layer.edit' and not optional & out.keys():
            raise ValueError('layer.edit requires at least one property to change')
        if 'color' in out and (type(out['color']) is not int or not 1 <= out['color'] <= 255):
            raise ValueError(f'{op} color must be an ACI integer from 1 to 255')
        for key in ('layer_on', 'freeze', 'lock'):
            if key in out and type(out[key]) is not bool:
                raise ValueError(f'{op} {key} must be boolean')
        return out
    if op == 'wall.fix_corner':
        first, second = _plan_points(out, 'corner1', 'corner2', op)
        if any(abs(b - a) <= 1e-6 for a, b in zip(first[:2], second[:2])):
            raise ValueError('wall.fix_corner requires a non-degenerate box')
        out['corner1'], out['corner2'] = first, second
        return out
    if op == 'axis.single_create':
        _prepare_axis_points(out, 'start', 'end')
        overhang = out.get('overhang', 1000.0)
        if type(overhang) not in (int, float) or not 0 <= overhang <= 100000:
            raise ValueError('overhang must be a finite number between 0 and 100000')
        out['overhang'] = float(overhang)
        return out
    if op == 'axis.annotate':
        _prepare_axis_points(out, 'start_point', 'end_point')
        side = out.setdefault('side', 'double')
        if side not in ('double', 'single', 'opposite'):
            raise ValueError('side must be double, single, or opposite')
        label = out.setdefault('start_label', '1')
        if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9]{1,8}', label):
            raise ValueError('start_label must contain 1..8 ASCII letters or digits')
        return out
    if op == 'space.create':
        try:
            out['point'] = point(out['point'])
        except OverflowError as exc:
            raise ValueError('space.create point coordinates must be finite numbers') from exc
        if out['point'][2] != 0:
            raise ValueError('space.create point must lie in the plan (Z=0)')
        return out
    for key in ('point', 'start', 'end', 'delta'):
        if key in out:
            out[key] = point(out[key])
    if op == 'wall.create' and out['start'] == out['end']:
        raise ValueError('Wall must have nonzero length')
    if op == 'axis.rect_create':
        return _prepare_rect_axis(out)
    if op == 'axis.from_walls':
        return _prepare_wall_axis(out)
    if op == 'opening.group_create':
        return _prepare_group_opening(out)
    if op in ('opening.move', 'column.move', 'entity.copy'):
        action = 'Copy' if op == 'entity.copy' else 'Move'
        if not isinstance(out['document_id'], str) or not out['document_id'].strip():
            raise ValueError(f'{action} document_id must be a non-empty string')
        handle = out['handle']
        if not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle):
            raise ValueError(f'{action} handle must be a hexadecimal handle without 0x prefix')
        if out['delta'][2] != 0:
            raise ValueError(f'{action} delta must lie in the plan (Z=0)')
        if abs(out['delta'][0]) <= 1e-6 and abs(out['delta'][1]) <= 1e-6:
            raise ValueError(f'{action} delta must be nonzero')
    com_edit = op in ('annotation.text_edit', 'roof.edit', 'column.edit', 'slab.edit')
    normalized_ref = com_edit or op in ('opening.mirror', 'opening.relabel', 'entity.set_layer',
                                        'entity.set_color', 'polyline.edit', 'space.edit')
    if normalized_ref or op in ('column.rotate', 'wall.edit', 'opening.edit', 'entity.delete'):
        if not isinstance(out['document_id'], str) or not out['document_id'].strip():
            raise ValueError(f'{op} document_id must be a non-empty string')
        handle = out['handle']
        if op == 'space.edit' and isinstance(handle, str):
            handle = handle.strip()
        if not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle):
            raise ValueError(f'{op} handle must be a hexadecimal handle without 0x prefix')
    if normalized_ref:
        out['handle'] = handle.upper()
        out['document_id'] = out['document_id'].strip()
        if com_edit:
            return _prepare_com_edit(op, out, optional)
        if op == 'entity.set_layer':
            out['layer'] = _validate_layer_name(out['layer'])
            return out
        if op == 'entity.set_color':
            color = out['color']
            if type(color) is not int or not 0 <= color <= 256:
                raise ValueError('entity.set_color color must be an integer 0..256 (0=ByBlock, 1..255=ACI, 256=ByLayer)')
            return out
        if op == 'polyline.edit':
            if not optional & out.keys():
                raise ValueError('polyline.edit requires at least one property to change')
            return _prepare_polyline_properties(op, out)
        if op in ('opening.relabel', 'space.edit'):
            key = 'label' if op == 'opening.relabel' else 'name'
            value = out[key]
            if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 64
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                raise ValueError(f'{op} {key} must contain 1..64 characters without control characters')
            out[key] = value.strip()
            return out
        if out['direction'] not in ('left_right', 'inside_outside'):
            raise ValueError("opening.mirror direction must be 'left_right' or 'inside_outside'")
        return out
    if op == 'opening.edit':
        keys = ('width', 'height', 'sill', 'text_height')
        if not any(key in out for key in keys):
            raise ValueError('opening.edit requires at least one of width, height, sill, text_height')
        for key in keys:
            if key not in out:
                continue
            value = out[key]
            requirement = 'nonnegative' if key == 'sill' else 'positive'
            error = f'opening.edit {key} must be a {requirement} finite number'
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(error)
            try:
                number = float(value)
            except OverflowError as exc:
                raise ValueError(error) from exc
            if not math.isfinite(number) or number < 0 or (number == 0 and key != 'sill'):
                raise ValueError(error)
            out[key] = number
        out['handle'] = handle.upper()
        return out
    if op == 'column.rotate':
        angle = out['angle_deg']
        if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not math.isfinite(angle):
            raise ValueError('angle_deg must be a finite number')
        rem = float(angle) % 360.0
        if rem <= 1e-6 or abs(360.0 - rem) <= 1e-6:
            raise ValueError('angle_deg must represent a nonzero rotation')
        out['angle_deg'] = float(angle)
    if op == 'wall.edit':
        if not any(k in out for k in ('left_width', 'right_width', 'height')):
            raise ValueError('wall.edit requires at least one of left_width, right_width, height')
        for k in ('left_width', 'right_width', 'height'):
            if k in out:
                v = out[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                    raise ValueError(f'wall.edit {k} must be a positive finite number')
                out[k] = float(v)
    if op == 'wall.split':
        if not isinstance(out['path'], str) or not out['path'].strip():
            raise ValueError('Split path must be a non-empty string')
        candidate = Path(out['path'])
        if not candidate.is_absolute() or candidate.suffix.lower() != '.dwg':
            raise ValueError('Split path must be an absolute DWG path')
        if not isinstance(out['document_id'], str) or not out['document_id'].strip():
            raise ValueError('Split document_id must be a non-empty string')
        handle = out['handle']
        if not isinstance(handle, str) or not re.fullmatch(r'[0-9A-Fa-f]+', handle):
            raise ValueError('Split handle must be a hexadecimal handle without 0x prefix')
        if out['point'][2] != 0:
            raise ValueError('Split point must lie in the plan (Z=0)')
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
        _validate_annotation_text(out['text'])
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
    elif op == 'space.create':
        x, y = args['point'][:2]
        return f'TSpArea\n{x:.4f},{y:.4f}\n\n'
    elif op == 'dim.wall':
        # The native command hangs when invoked through AutoLISP (command).
        sx, sy = args['start'][:2]
        ex, ey = args['end'][:2]
        return f'TDimWall\n{sx:.4f},{sy:.4f}\n{ex:.4f},{ey:.4f}\n\n'
    elif op == 'dim.t20_angular':
        return '\n'.join(['TDimAng', coord(args['first_pick'][:2]), coord(args['second_pick'][:2]),
                          coord(args['arc_point'][:2]), '', ''])
    elif op == 'wall.fix_corner':
        x1, y1 = args['corner1'][:2]
        x2, y2 = args['corner2'][:2]
        return (f'(progn (command "TFixWallEx" (list {x1:.4f} {y1:.4f} 0.0) '
                f'(list {x2:.4f} {y2:.4f} 0.0) "") (princ))\n')
    elif op == 'opening.create':
        return '\n'.join(['TOpening', coord(args['point'][:2]), '', ''])
    elif op == 'opening.group_create':
        lines = ['TGroupOpening', *(f'(handent "{handle}")' for handle in args['handles']), '', args['label']]
        return '\n'.join(lines) + '\n'
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

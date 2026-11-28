"""Locally probed Tianzheng components and their evidence checks."""
import math
import time
from contextlib import contextmanager
from itertools import accumulate
from pathlib import Path
from .commands import command_payload
from .cad import (CommandCompletionTimeout, CommandNotSent, OutcomeUnknown, collection_items, late,
                  invoke_document, property_lookup, property_read, property_write,
                  require_drawing_window, require_model_space, retry_document_read)
from .panel import (create_column, column_dxf, verify_column_dxf, create_rect_axis,
                    create_single_axis, annotate_axis,
                    create_opening, opening_dxf, verify_opening_dxf, roof_dxf, text_dxf, slab_dxf,
                    drawing_view, entity_wall_hosts, menu_dispatch,
                    pick_world_point, wait_command_idle, curve_axes,
                    line_dxf, circle_dxf, arc_dxf, ellipse_dxf, hatch_dxf, entity_layer_dxf, transform_dxf,
                    opening_label_dxf, space_dxf, mtext_dxf, dim_dxf, polyline_props_dxf,
                    entity_color_dxf, opening_group_dxf, block_ref_dxf, t20_dim_dxf)


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


def _dispatch_array(entities):
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, list(entities))


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


def _add_polyline(c, points, closed):
    coordinates = vector([v for p in points for v in p[:2]])
    model_space = retry_document_read(lambda: c.doc.ModelSpace)
    # A failed creation call may already have added an entity.
    entity = model_space.AddLightWeightPolyline(coordinates)

    def set_closed():
        obj = late(entity)
        obj.Closed = closed
        return obj

    obj = retry_document_read(set_closed)
    if retry_document_read(lambda: bool(obj.Closed)) != closed:
        raise OutcomeUnknown('Polyline Closed readback mismatch')
    return retry_document_read(lambda: str(obj.Handle))


def make_polyline(c, points):
    return _add_polyline(c, points, True)


def _roof_elevation(args):
    return -float(args['overhang']) * math.tan(math.radians(args['angle']))


def _roof_mismatch(handle, field, expected, actual):
    return (f'Roof {field}: expected {expected!r}, observed {actual!r}; '
            f'{handle} remains in the drawing - side effect present, do not replay blind')


def _verify_roof_number(handle, field, expected, actual, tolerance):
    if actual is None or not abs(actual - expected) <= tolerance:
        raise OutcomeUnknown(_roof_mismatch(handle, field, expected, actual))


def _verify_roof_readback(args, handle, com_angle, com_elev, dxf):
    """Validate the COM and DXF snapshots without a CAD connection."""
    angle = float(args['angle'])
    slope = math.radians(angle)
    elevation = _roof_elevation(args)
    _verify_roof_number(handle, 'COM SlopeAngle (deg)', angle, com_angle, 0.05)
    _verify_roof_number(handle, 'COM Elevation (mm)', elevation, com_elev, 0.5)
    if dxf['type'] != 'TCH_SLOPEROOF':
        raise OutcomeUnknown(_roof_mismatch(handle, 'DXF type', 'TCH_SLOPEROOF', dxf['type']))
    _verify_roof_number(handle, 'DXF 148 elevation (mm)', elevation, dxf['elevation'], 0.5)
    count = len(args['boundary'])
    vertices = dxf['vertices']
    slopes = dxf['edge_slopes_rad']
    if len(vertices) != count:
        raise OutcomeUnknown(_roof_mismatch(handle, 'DXF 10 vertex count', count, len(vertices)))
    if not all(math.isfinite(x) and math.isfinite(y) for x, y in vertices):
        raise OutcomeUnknown(_roof_mismatch(handle, 'DXF 10 vertices', 'finite XY coordinates', vertices))
    if len(slopes) != count:
        raise OutcomeUnknown(_roof_mismatch(handle, 'DXF 51 edge count', count, len(slopes)))
    for index, measured in enumerate(slopes, start=1):
        if not abs(measured - slope) <= 0.002:
            raise OutcomeUnknown(_roof_mismatch(handle, f'DXF 51 edge {index} slope (rad)', slope, measured))
    return {'angle_requested_deg': angle, 'overhang_requested_mm': float(args['overhang']),
            'com_slope_angle_deg': com_angle, 'com_elevation_mm': com_elev,
            'expected_elevation_mm': elevation, 'dxf': dxf, 'boundary_consumed': True}


def create_roof(c, args, before):
    boundary_handle = make_polyline(c, args['boundary'])
    try:
        c.run_command(command_payload('roof.create', args, boundary_handle=boundary_handle))
    except CommandNotSent as exc:
        raise OutcomeUnknown(_roof_mismatch(boundary_handle, 'command dispatch',
                                            'TSlopeRoof sent', str(exc))) from exc
    after = c.entities()
    if boundary_handle in after:
        raise OutcomeUnknown(_roof_mismatch(boundary_handle, 'boundary consumption',
                                            'temporary boundary removed', 'temporary boundary remains'))
    new = {h: e for h, e in after.items() if h not in before and e.ObjectName == 'TDbSlopeRoof'}
    if len(new) != 1:
        raise OutcomeUnknown(f'Expected one new TDbSlopeRoof, observed {len(new)} '
                             f'(handles {list(new)}); inspect drawing before replay')
    handle, entity = next(iter(new.items()))
    measurements = {}
    for name, unit, expected, tolerance in (
        ('SlopeAngle', 'deg', float(args['angle']), 0.05),
        ('Elevation', 'mm', _roof_elevation(args), 0.5),
    ):
        field = f'COM {name} ({unit})'
        try:
            measured = float(getattr(entity, name))
        except Exception as exc:
            raise OutcomeUnknown(_roof_mismatch(handle, field, expected, f'unreadable ({exc})')) from exc
        _verify_roof_number(handle, field, expected, measured, tolerance)
        measurements[name] = measured
    try:
        dxf = roof_dxf(c, handle)
    except Exception as exc:
        error = CommandCompletionTimeout if isinstance(exc, CommandCompletionTimeout) else OutcomeUnknown
        raise error(_roof_mismatch(handle, 'DXF readback', 'readable entget groups 0, 10, 51 and 148',
                                   str(exc))) from exc
    roof = _verify_roof_readback(args, handle, measurements['SlopeAngle'], measurements['Elevation'], dxf)
    return {'success': True, 'document_id': c.document_id, 'handles': [handle],
            'entities': [c.describe(entity)], 'effects': {}, 'evidence': {'roof': roof},
            'validation': {'type': True, 'properties': True, 'dxf': 'verified_10_51_and_148',
                           'host_association': 'not_applicable', 'reopen': 'pending'}}


def _new_axis_lines(before, after, count=None):
    removed = sorted(before.keys() - after.keys())
    handles = sorted(after.keys() - before.keys())
    if removed:
        raise OutcomeUnknown(f'Axis command removed existing entities: {removed}')
    if not handles or (count is not None and len(handles) != count):
        expected = count if count is not None else 'at least one'
        raise OutcomeUnknown(f'Expected {expected} new axis lines, found {len(handles)}')
    lines = []
    for handle in handles:
        entity = after[handle]

        def read_line():
            if str(entity.ObjectName) != 'AcDbLine' or str(entity.Layer).upper() != 'DOTE':
                raise OutcomeUnknown(f'New entity {handle} must be an AcDbLine on DOTE')
            return ([float(v) for v in entity.StartPoint[:2]],
                    [float(v) for v in entity.EndPoint[:2]])

        start, end = retry_document_read(read_line)
        length = math.dist(start, end)
        if (len(start) != 2 or len(end) != 2 or not math.isfinite(length)
                or length <= (0.01 if count is None else 0)):
            raise OutcomeUnknown(f'Axis line {handle} has invalid endpoints or length')
        lines.append({'handle': handle, 'start': start, 'end': end, 'length': length})
    return lines


def _verify_axis_stations(observed, expected, tolerance, label):
    if len(observed) != len(expected) or any(
            not abs(value - target) <= tolerance for value, target in zip(observed, expected)):
        raise OutcomeUnknown(f'Axis {label}: expected {expected}, observed {observed}')


def _verify_rect_axis(args, lines):
    x, y = args['origin'][:2]
    bays, depths = args['bays'], args['depths']
    skew = 90.0 - args['axis_angle']
    orthogonal = abs(skew) <= 1e-6
    vertical_x, horizontal_y, projections = [], [], []
    theta = math.radians(skew)
    width, depth = sum(bays), sum(depths)
    for line in lines:
        x0, y0 = line['start']
        x1, y1 = line['end']
        dx, dy = x1 - x0, y1 - y0
        if orthogonal:
            if abs(dx) <= 0.05 and abs(dy) >= abs(dx) and line['length'] >= depth - 0.5:
                vertical_x.append(x0)
            elif abs(dy) <= 0.05 and abs(dx) >= abs(dy) and line['length'] >= width - 0.5:
                horizontal_y.append(y0)
            else:
                raise OutcomeUnknown(f'Axis line {line["handle"]} has incorrect orientation or length')
        else:
            angle = math.degrees(math.atan2(dy, dx)) % 180.0
            vertical_error = abs(angle - 90.0)
            skew_error = abs((angle - skew + 90.0) % 180.0 - 90.0)
            if vertical_error <= min(0.1, skew_error):
                vertical_x.append(x0)
            elif skew_error <= 0.1:
                # Use the normal with positive Y so stations follow the requested depth order.
                projections.append(-(x0 - x) * math.sin(theta) + (y0 - y) * math.cos(theta))
            else:
                raise OutcomeUnknown(f'Axis line {line["handle"]} has unexpected angle {angle:g}°')

    vertical_x.sort()
    _verify_axis_stations(vertical_x, list(accumulate(bays, initial=x)), 0.05, 'vertical X')
    horizontal_y.sort()
    families = {}
    if orthogonal:
        _verify_axis_stations(horizontal_y, list(accumulate(depths, initial=y)), 0.05, 'horizontal Y')
    else:
        projections.sort()
        spacings = [right - left for left, right in zip(projections, projections[1:])]
        _verify_axis_stations(spacings, depths, 0.1, 'skew normal spacings')
        families = {
            'vertical': {'angle_deg': 90.0, 'x': vertical_x},
            'skew': {'angle_deg': skew % 180.0, 'normal_stations': projections,
                     'normal_spacings': spacings},
        }
    return {'vertical_x': vertical_x, 'horizontal_y': horizontal_y, 'families': families}


def _axis_result(c, after, lines, evidence):
    handles = [line['handle'] for line in lines]
    return {
        'success': True, 'document_id': c.document_id, 'handles': handles,
        'entities': [c.describe(after[h]) for h in handles], 'evidence': evidence,
        'validation': {'type': True, 'properties': True,
                       'geometry': 'verified_dote_lines', 'reopen': 'pending'},
    }


def create_rect_axis_part(c, args, before, backend=None):
    try:
        panel_evidence = create_rect_axis(c, args, backend=backend)
        after = c.entities()
        lines = _new_axis_lines(before, after, len(args['bays']) + len(args['depths']) + 2)
        geometry = _verify_rect_axis(args, lines)
        evidence = {'command': 'TRectAxis', 'origin': args['origin'][:2],
                    'bays': list(args['bays']), 'depths': list(args['depths']),
                    'axis_angle': args['axis_angle'], 'line_count': len(lines), 'layer': 'DOTE',
                    'panel': panel_evidence, **geometry}
        return _axis_result(c, after, lines, {'axis': evidence})
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(f'TRectAxis: {exc}') from exc


def create_single_axis_part(c, args, before, backend=None):
    try:
        panel_evidence = create_single_axis(c, args, backend=backend)
        after = c.entities()
        lines = _new_axis_lines(before, after, count=1)
        line, = lines
        start, end, overhang = args['start'], args['end'], args['overhang']
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        ux, uy = dx / length, dy / length
        exp_a = (start[0] - overhang * ux, start[1] - overhang * uy)
        exp_b = (end[0] + overhang * ux, end[1] + overhang * uy)
        if not abs(line['length'] - (length + 2 * overhang)) <= 0.1:
            raise OutcomeUnknown('TSingleAxis extended line length mismatch')
        a, b = line['start'], line['end']
        if not any(math.dist(a, first) <= 0.05 and math.dist(b, second) <= 0.05
                   for first, second in ((exp_a, exp_b), (exp_b, exp_a))):
            raise OutcomeUnknown('TSingleAxis extended line endpoint mismatch')
        evidence = {'command': 'TSingleAxis', 'start': start, 'end': end,
                    'overhang': overhang, 'line': line, 'panel': panel_evidence}
        return _axis_result(c, after, lines, {'single_axis': evidence})
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(f'TSingleAxis: {exc}') from exc


def annotate_axis_part(c, args, before, backend=None):
    try:
        panel_evidence = annotate_axis(c, args, backend=backend)
        after = c.entities()
        removed = sorted(before.keys() - after.keys())
        if removed:
            raise OutcomeUnknown(f'TMultAxisDim removed existing entities: {removed}')

        added_by_type = {'TDbAxisLabelSet': [], 'TDbDimension2': []}
        for handle in sorted(after.keys() - before.keys()):
            entity = after[handle]
            kind, layer = retry_document_read(lambda: (str(entity.ObjectName), str(entity.Layer)))
            if kind not in added_by_type or layer.upper() != 'AXIS':
                raise OutcomeUnknown(f'TMultAxisDim unexpected entity {handle}: {kind} on {layer}')
            added_by_type[kind].append(handle)
        labels = added_by_type['TDbAxisLabelSet']
        dimensions = added_by_type['TDbDimension2']
        expected = 4 if args['side'] == 'double' else 2
        if len(labels) != 1 or len(dimensions) != expected:
            raise OutcomeUnknown(
                f'TMultAxisDim expected one label set and {expected} dimensions, '
                f'found {len(labels)} label sets and {len(dimensions)} dimensions')

        handles = labels + dimensions
        evidence = {'command': 'TMultAxisDim', **args, 'label_set_handle': labels[0],
                    'dimension_handles': dimensions, 'layer': 'AXIS', 'panel': panel_evidence}
        return {'success': True, 'document_id': c.document_id, 'handles': handles,
                'entities': [c.describe(after[h]) for h in handles],
                'evidence': {'axis_annotate': evidence},
                'validation': {'type': True, 'properties': 'layer_and_counts_verified',
                               'geometry': 'label_text_unreadable_without_explode',
                               'reopen': 'pending'}}
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(f'TMultAxisDim: {exc}') from exc


_AXIS_WALL_TOLERANCES = {'LeftWidth': 0.01, 'RightWidth': 0.01, 'Height': 0.01, 'HoleArea': 0.02}


def _axis_wall_snapshot(entity):
    return retry_document_read(
        lambda: {name: float(getattr(entity, name)) for name in _AXIS_WALL_TOLERANCES})


def axis_from_walls(c, args, before):
    if args['document_id'] != c.document_id:
        raise ValueError('Cross-document wall reference')
    handles = args['wall_handles']
    for handle in handles:
        if handle not in before or retry_document_read(lambda: str(before[handle].ObjectName)) != 'TDbWall':
            raise ValueError(f'Axis source {handle} must be a native wall in the owned ModelSpace')
    snapshots = {handle: _axis_wall_snapshot(before[handle]) for handle in handles}
    if any(not math.isfinite(value) for wall in snapshots.values() for value in wall.values()):
        raise ValueError('Axis source wall properties must be finite')

    selection = ' '.join(f'(ssadd (handent "{handle}") _ss)' for handle in handles)
    payload = f'(progn (setq _ss (ssadd)) {selection} (sssetfirst nil _ss) (princ))\nTWall2Axis\n'
    try:
        # Keep preselection and its consumer together in the command stream.
        c.run_command(payload)
        after = c.entities()
        lines = _new_axis_lines(before, after)
        for handle in handles:
            entity = after[handle]
            if retry_document_read(lambda: str(entity.ObjectName)) != 'TDbWall':
                raise OutcomeUnknown(f'Axis source {handle} changed entity type')
            observed = _axis_wall_snapshot(entity)
            for name, tolerance in _AXIS_WALL_TOLERANCES.items():
                old, new = snapshots[handle][name], observed[name]
                if not abs(new - old) <= tolerance:
                    raise OutcomeUnknown(f'Axis source wall {handle} {name} changed: {old} -> {new}')
        evidence = {'wall_handles': list(handles), 'line_count': len(lines), 'lines': lines,
                    'walls_unchanged': True}
        return _axis_result(c, after, lines, {'axis_from_walls': evidence})
    except (OutcomeUnknown, CommandNotSent):
        raise
    except Exception as exc:
        raise OutcomeUnknown(f'TWall2Axis: {exc}') from exc


@contextmanager
def _without_snaps(c):
    old_osmode = int(float(retry_document_read(lambda: c.doc.GetVariable('OSMODE'))))
    command_error = None
    try:
        c.run_command('(setvar "OSMODE" 0)\n')
        yield
    except Exception as exc:
        command_error = exc
        raise
    finally:
        try:
            # Use COM so the restore cannot become input to a pending prompt.
            invoke_document(lambda: c.doc, 'SetVariable', 'OSMODE', old_osmode)
        except Exception as exc:
            message = f'Could not restore OSMODE to {old_osmode}: {exc}'
            if command_error is not None:
                message = f'{command_error}. {message}'
            error = (CommandCompletionTimeout if isinstance(command_error, CommandCompletionTimeout)
                     else OutcomeUnknown)
            raise error(message) from exc


def _native_wall_handles(entities):
    return {handle for handle, entity in entities.items()
            if retry_document_read(lambda: str(entity.ObjectName)) == 'TDbWall'}


def create_space(c, args, before):
    if len(_native_wall_handles(before)) < 3:
        raise ValueError('space.create requires at least three native walls in ModelSpace to enclose a room')
    with _verify_mutation():
        with _without_snaps(c):
            c.run_command(command_payload('space.create', args))
        after = c.entities()
        removed = sorted(before.keys() - after.keys())
        added = sorted(after.keys() - before.keys())
        if removed or len(added) != 1:
            raise OutcomeUnknown(f'TSpArea expected one new entity and no removals: added {added}, removed {removed}')
        handle = added[0]
        entity = late(after[handle])
        if str(property_read(entity, 'ObjectName')) != 'TDbSpace':
            raise OutcomeUnknown(f'TSpArea created a non-space entity: {handle}')
        com_name = str(property_read(entity, 'Name'))
        dxf = space_dxf(c, handle)
        area, perimeter = dxf['area_m2'], dxf['perimeter_mm']
        if (dxf['type'] != 'TCH_SPACE' or dxf['point'] is None
                or area is None or not math.isfinite(area) or area <= 0.01
                or perimeter is None or not math.isfinite(perimeter) or perimeter <= 10.0):
            raise OutcomeUnknown(f'TSpArea invalid space DXF for {handle}: {dxf}')

        return {
            'success': True, 'document_id': c.document_id,
            'handles': [handle], 'entities': [c.describe(entity)],
            'evidence': {'space': {
                'command': 'TSpArea', 'point': args['point'],
                'com_name': com_name, 'name': dxf['name'], 'number': dxf['number'],
                'area_m2': area, 'perimeter_mm': perimeter, 'dxf': dxf,
            }},
            'validation': {'type': True, 'properties': True,
                           'dxf': 'verified_1_2_10_41_42', 'reopen': 'pending'},
        }


def dimension_walls(c, args, before):
    if not _native_wall_handles(before):
        raise ValueError('dim.wall requires at least one native wall in ModelSpace')
    with _verify_mutation():
        payload = command_payload('dim.wall', args)
        with _without_snaps(c):
            c.run_command(payload)
        after = c.entities()
        removed = sorted(before.keys() - after.keys())
        if removed:
            raise OutcomeUnknown(f'TDimWall removed existing entities: {removed}')
        added = sorted(after.keys() - before.keys())
        if not added:
            raise OutcomeUnknown('TDimWall created no new dimension entities')
        for handle in added:
            entity = after[handle]
            kind, layer = retry_document_read(lambda: (str(entity.ObjectName), str(entity.Layer)))
            if kind != 'TDbDimension2' or layer.upper() != 'PUB_DIM':
                raise OutcomeUnknown(f'TDimWall unexpected entity {handle}: {kind} on {layer}')

        axes = curve_axes(c, added)
        for handle in added:
            axis = axes[handle]
            if (axis['type'] != 'TCH_DIMENSION2' or axis['layer'].upper() != 'PUB_DIM'
                    or axis['length'] <= 0.01):
                raise OutcomeUnknown(f'TDimWall invalid dimension curve span for {handle}: {axis}')

        evidence = {'command': 'TDimWall', 'start': list(args['start'][:2]),
                    'end': list(args['end'][:2]), 'dimension_count': len(added),
                    'layer': 'PUB_DIM', 'dimensions': [{'handle': h, **axes[h]} for h in added]}
        return {'success': True, 'document_id': c.document_id, 'handles': added,
                'entities': [c.describe(after[h]) for h in added],
                'evidence': {'dim_wall': evidence},
                'validation': {'type': True, 'properties': 'layer_and_curve_span_verified',
                               'geometry': 'verified_vlax_curve_span', 'reopen': 'pending'}}


def create_t20_angular_dim(c, args, before):
    source_count = sum(str(property_read(late(entity), 'ObjectName')) in ('AcDbLine', 'TDbWall')
                       for entity in before.values())
    if source_count < 2:
        raise ValueError('dim.t20_angular requires at least two line or wall entities in ModelSpace')
    with _verify_mutation():
        with _without_snaps(c):
            c.run_command(command_payload('dim.t20_angular', args))
        after = c.entities()
        removed = sorted(before.keys() - after.keys())
        added = sorted(after.keys() - before.keys())
        if removed or len(added) != 1:
            raise OutcomeUnknown(f'TDimAng expected one new dimension and no removals: '
                                 f'added={added}, removed={removed}')

        handle = added[0]
        entity = late(after[handle])
        kind = str(property_read(entity, 'ObjectName'))
        layer = str(property_read(entity, 'Layer'))
        if kind != 'TDbDimension2' or layer.upper() != 'PUB_DIM':
            raise OutcomeUnknown(f'TDimAng unexpected COM entity {handle}: {kind} on {layer}')
        dxf = t20_dim_dxf(c, handle)
        if dxf['type'] != 'TCH_DIMENSION2' or (dxf['layer'] or '').upper() != 'PUB_DIM':
            raise OutcomeUnknown(f'TDimAng unexpected DXF type or layer for {handle}: {dxf}')

        evidence = {'command': 'TDimAng', 'first_pick': args['first_pick'],
                    'second_pick': args['second_pick'], 'arc_point': args['arc_point'],
                    'layer': layer, 'dxf': dxf}
        return {'success': True, 'document_id': c.document_id, 'handles': [handle],
                'entities': [c.describe(entity)], 'evidence': {'t20_angular_dim': evidence},
                'validation': {'type': True, 'properties': True, 'dxf': 'verified_0_8',
                               'reopen': 'pending'}}


def _wall_axis_snapshot(c, handles):
    axes = curve_axes(c, sorted(handles))
    for handle, axis in axes.items():
        if axis['type'] != 'TCH_WALL' or axis['length'] <= 0:
            raise OutcomeUnknown(f'Invalid native wall curve axis for {handle}: {axis}')
    total = sum(axis['length'] for axis in axes.values())
    if not math.isfinite(total):
        raise OutcomeUnknown('Total wall length is not finite')
    return axes, total


def _wall_hole_total(c, handles):
    areas = hole_areas(c)
    total = sum(areas.values())
    if areas.keys() != handles or not math.isfinite(total):
        raise OutcomeUnknown('Could not verify HoleArea for all native walls')
    return total


def _wall_axis_changed(before, after):
    return (math.dist(before['start'], after['start']) > 0.05
            or math.dist(before['end'], after['end']) > 0.05
            or abs(before['length'] - after['length']) > 0.05)


def fix_wall_corner(c, args, before):
    walls_before = _native_wall_handles(before)
    if len(walls_before) < 2:
        raise ValueError('wall.fix_corner requires at least two native walls in ModelSpace')
    non_walls_before = before.keys() - walls_before
    holes_before = _wall_hole_total(c, walls_before)
    axes_before, length_before = _wall_axis_snapshot(c, walls_before)

    with _verify_mutation():
        payload = command_payload('wall.fix_corner', args)
        with _without_snaps(c):
            c.run_command(payload)
        after = c.entities()
        walls_after = _native_wall_handles(after)
        if after.keys() - walls_after != non_walls_before:
            raise OutcomeUnknown('Non-wall entity handles changed during wall.fix_corner')
        added = sorted(walls_after - walls_before)
        removed = sorted(walls_before - walls_after)
        axes_after, length_after = _wall_axis_snapshot(c, walls_after)
        if length_after <= 0 or length_after > length_before + max(2.0, 0.5 * len(walls_after)):
            raise OutcomeUnknown('Total wall length changed unexpectedly during wall.fix_corner')
        holes_after = _wall_hole_total(c, walls_after)
        if abs(holes_after - holes_before) > 0.02 * max(1, len(walls_after)):
            raise OutcomeUnknown('Total wall HoleArea changed unexpectedly during wall.fix_corner')

        changed = sorted((walls_before ^ walls_after) | {
            h for h in walls_before & walls_after if _wall_axis_changed(axes_before[h], axes_after[h])
        })
        handles = added or changed
        evidence = {'command': 'TFixWallEx', 'corner1': list(args['corner1'][:2]),
                    'corner2': list(args['corner2'][:2]), 'repaired': bool(changed),
                    'added_wall_handles': added, 'removed_wall_handles': removed,
                    'changed_wall_handles': changed, 'wall_count_before': len(walls_before),
                    'wall_count_after': len(walls_after),
                    'total_wall_length_before': round(length_before, 4),
                    'total_wall_length_after': round(length_after, 4),
                    'trimmed_wall_length': round(max(0.0, length_before - length_after), 4),
                    'axes_before': axes_before, 'axes_after': axes_after}
        return {'success': True, 'document_id': c.document_id, 'handles': handles,
                'entities': [c.describe(after[h]) for h in handles if h in after],
                'evidence': {'wall_fix_corner': evidence},
                'validation': {'type': True,
                               'properties': 'wall_length_non_increasing_and_hole_area_conserved',
                               'geometry': 'verified_vlax_curve_axes', 'reopen': 'pending'}}


def execute_part(c,op,args):
    if op == 'layer.list':
        return list_layers(c, args)
    if op == 'block.list':
        return list_blocks(c, args)
    if op == 'entity.query':
        return query_entities(c, args)
    c.guard();require_drawing_window(c.hwnd);require_model_space(c.doc)
    before=c.entities();host=None;holes_before=None;placement=None
    if op == 'axis.rect_create':
        return create_rect_axis_part(c, args, before)
    if op == 'axis.from_walls':
        return axis_from_walls(c, args, before)
    if op == 'axis.single_create':
        return create_single_axis_part(c, args, before)
    if op == 'axis.annotate':
        return annotate_axis_part(c, args, before)
    if op == 'space.create':
        return create_space(c, args, before)
    if op == 'space.edit':
        return edit_space(c, args, before)
    if op == 'dim.wall':
        return dimension_walls(c, args, before)
    if op == 'dim.t20_angular':
        return create_t20_angular_dim(c, args, before)
    if op == 'wall.fix_corner':
        return fix_wall_corner(c, args, before)
    if op=='wall.split':return split_wall(c,args,before)
    if op=='wall.edit':return edit_wall(c,args,before)
    if op=='column.move':return move_column(c,args,before)
    if op=='column.rotate':return rotate_column(c,args,before)
    if op=='entity.copy':return copy_entity(c,args,before)
    if op in ('entity.move', 'entity.rotate'):
        return transform_entity(c, op, args, before)
    if op == 'entity.scale':
        return scale_entity(c, args, before)
    if op == 'entity.mirror':
        return mirror_entity(c, args, before)
    if op=='opening.move':return move_opening(c,args,before)
    if op=='opening.edit':return edit_opening(c,args,before)
    if op == 'opening.group_create':
        return create_group_opening(c, args, before)
    if op == 'opening.relabel':
        return relabel_opening(c, args, before)
    if op == 'opening.mirror':
        return mirror_opening(c, args, before)
    if op == 'annotation.text_edit':
        return edit_text(c, args, before)
    if op == 'annotation.mtext':
        return create_mtext(c, args, before)
    if op == 'dim.aligned':
        return create_dim_aligned(c, args, before)
    if op == 'dim.linear':
        return create_dim_linear(c, args, before)
    if op == 'dim.radial':
        return create_dim_radial(c, args, before)
    if op == 'dim.angular':
        return create_dim_angular(c, args, before)
    if op == 'dim.diameter':
        return create_dim_diameter(c, args, before)
    if op == 'roof.edit':
        return edit_roof(c, args, before)
    if op == 'column.edit':
        return edit_column(c, args, before)
    if op == 'slab.edit':
        return edit_slab(c, args, before)
    if op == 'line.create':
        return create_line(c, args, before)
    if op == 'circle.create':
        return create_circle(c, args, before)
    if op == 'arc.create':
        return create_arc(c, args, before)
    if op == 'ellipse.create':
        return create_ellipse(c, args, before)
    if op == 'rectangle.create':
        return create_rectangle(c, args, before)
    if op == 'polyline.edit':
        return edit_polyline(c, args, before)
    if op == 'hatch.create':
        return create_hatch(c, args, before)
    if op == 'layer.create':
        return create_layer(c, args, before)
    if op == 'layer.edit':
        return edit_layer(c, args, before)
    if op == 'layer.delete':
        return delete_layer(c, args, before)
    if op == 'layer.set_active':
        return set_active_layer(c, args, before)
    if op == 'block.define':
        return define_block(c, args, before)
    if op == 'block.insert':
        return insert_block(c, args, before)
    if op == 'block.delete':
        return delete_block(c, args, before)
    if op == 'entity.set_layer':
        return set_entity_layer(c, args, before)
    if op == 'entity.set_color':
        return set_entity_color(c, args, before)
    if op=='entity.delete':return delete_entity(c,args,before)
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
        if op=='roof.create':return create_roof(c,args,before)
        consumed=[]
        if op=='annotation.text':
            model_space = retry_document_read(lambda: c.doc.ModelSpace)
            model_space.AddText(args['text'], vector(args['point']), args['height'])
        elif op=='polyline.create':
            polyline_handle = _add_polyline(c, args['points'], args.get('closed', False))
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
            if retry_document_read(lambda: str(e.TextString)) != args['text']:
                raise OutcomeUnknown('Annotation text readback mismatch')
            evidence={'construction':'ordinary_cad_text'}
        elif op=='polyline.create':
            if retry_document_read(lambda: str(e.Handle)) != polyline_handle:
                raise OutcomeUnknown('Unexpected new polyline handle')
            evidence={'construction':'ordinary_cad_polyline','closed':args.get('closed', False)}
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


def _unique_wall_host(c, handle, *, invalid_host=OutcomeUnknown, allow_duplicate_same_host=False):
    """Resolve DXF 330 references for an opening and require a unique TCH_WALL host."""
    refs = entity_wall_hosts(c, handle)
    broken = [r for r in refs if not r.get('handle')]
    if broken:
        raise OutcomeUnknown(
            'DXF 330 host references could not be resolved: %r' % refs)
    wall_refs = [r for r in refs if str(r['type']).upper() == 'TCH_WALL']
    if allow_duplicate_same_host:
        unique_walls = {str(r['handle']).upper() for r in wall_refs}
        if len(unique_walls) != 1:
            raise invalid_host('Opening host is not a unique TCH_WALL in DXF 330: %r' % refs)
        return next(iter(unique_walls)), refs
    if len(wall_refs) != 1:
        raise invalid_host('Opening host is not a unique TCH_WALL in DXF 330: %r' % refs)
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


def _validate_move_target(c, args, before=None):
    if args['document_id'] != c.document_id:
        raise ValueError('Entity document_id mismatch')
    handle = args['handle'].upper()
    if before is not None and handle not in before:
        handle = handle.lstrip('0') or '0'
        if handle not in before:
            raise ValueError(f'Unknown handle: {args["handle"]}')
    return handle, c.entity(handle, args['document_id'])


def _run_move_command(c, handle, origin, delta):
    bx, by = float(origin[0]), float(origin[1])
    tx, ty = bx + float(delta[0]), by + float(delta[1])
    lisp = ('(progn (command "_.MOVE" (handent "%s") "" "_non" (list %.6f %.6f 0.0) '
            '"_non" (list %.6f %.6f 0.0)) (princ))\n' % (handle, bx, by, tx, ty))
    c.run_command(lisp)
    return (bx, by), (tx, ty)


def _require_column_dxf(dxf, stage):
    """Validate required column DXF geometry and attributes (codes 10/11/149/70/71/72).

    Strict `is None` checks ensure valid zero values (such as DXF 72 == 0) pass
    while missing fields never falsely compare equal (`None == None`).
    """
    if (dxf.get('type') != 'TCH_COLUMN' or dxf.get('insertion') is None
            or len(dxf.get('vertices') or ()) != 4):
        raise OutcomeUnknown('Column DXF geometry could not be read %s' % stage)
    missing = []
    if dxf.get('height') is None:
        missing.append('height(149)')
    flags = dxf.get('flags')
    if not isinstance(flags, (tuple, list)) or len(flags) != 3:
        missing.append('flags(70/71/72)')
    else:
        for code, val in zip((70, 71, 72), flags):
            if val is None:
                missing.append('flag(%d)' % code)
    if missing:
        raise OutcomeUnknown('Column required DXF fields missing %s: %s'
                             % (stage, ', '.join(missing)))


COPYABLE_OBJECT_NAMES = ('TDbColumn', 'AcDbText', 'AcDbPolyline')
COPY_COORDINATE_TOLERANCE = 0.01


def _copy_snapshot(c, handle, entity, object_name, stage):
    try:
        if object_name == 'TDbColumn':
            dxf = column_dxf(c, handle)
            _require_column_dxf(dxf, stage)
            insertion = tuple(dxf['insertion'])
            vertices = [tuple(vertex) for vertex in dxf['vertices']]
            flags = tuple(dxf['flags'])
            height = dxf['height']
            if (any(len(pt) != 2 or not all(math.isfinite(v) for v in pt)
                    for pt in [insertion, *vertices])
                    or not math.isfinite(height) or height <= 0
                    or any(type(flag) is not int for flag in flags)):
                raise ValueError('Invalid column geometry or flags')
            return {'insertion': insertion, 'vertices': vertices, 'height': height, 'flags': flags}

        def read_com():
            if object_name == 'AcDbText':
                text = entity.TextString
                height = float(entity.Height)
                insertion = tuple(entity.InsertionPoint)
                if (not isinstance(text, str) or not math.isfinite(height) or height <= 0
                        or len(insertion) not in (2, 3)
                        or not all(math.isfinite(v) for v in insertion)):
                    raise ValueError('Invalid text content, height or insertion point')
                return {'insertion': insertion, 'text': text, 'height': height}

            coordinates = tuple(entity.Coordinates)
            closed = entity.Closed
            length = float(entity.Length)
            if (len(coordinates) < 4 or len(coordinates) % 2
                    or not all(math.isfinite(v) for v in coordinates)
                    or not isinstance(closed, bool) or not math.isfinite(length) or length < 0):
                raise ValueError('Invalid polyline coordinates, Closed or Length')
            return {'vertices': list(zip(coordinates[::2], coordinates[1::2])),
                    'closed': closed, 'length': length}

        return retry_document_read(read_com)
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(f'{object_name} {handle} snapshot {stage}: {exc}') from exc


def _verify_copy_snapshot(original, observed, delta, object_name, stage):
    def verify_point(before, after):
        offsets = (*delta, 0.0)
        if len(before) != len(after) or any(
                abs(actual - (value + offset)) > COPY_COORDINATE_TOLERANCE
                for value, actual, offset in zip(before, after, offsets)):
            raise OutcomeUnknown(f'entity.copy {stage}: coordinate mismatch ({before!r} -> {after!r})')

    if 'insertion' in original:
        verify_point(original['insertion'], observed['insertion'])
    if 'vertices' in original:
        if len(original['vertices']) != len(observed['vertices']):
            raise OutcomeUnknown(f'entity.copy {stage}: vertex count mismatch')
        for before, after in zip(original['vertices'], observed['vertices']):
            verify_point(before, after)

    tolerances = {
        'TDbColumn': {'height': 0, 'flags': None},
        'AcDbText': {'height': 1e-4, 'text': None},
        'AcDbPolyline': {'length': 0.01, 'closed': None},
    }
    for field, tolerance in tolerances[object_name].items():
        expected, actual = original[field], observed[field]
        matches = actual == expected if tolerance is None else abs(actual - expected) <= tolerance
        if not matches:
            raise OutcomeUnknown(f'entity.copy {stage}: {field} mismatch ({expected!r} -> {actual!r})')


def copy_entity(c, args, before):
    """Copy one standalone entity in plan and verify both handles."""
    if args['handle'].upper() not in before:
        raise ValueError('Copy source handle is not in owned ModelSpace')
    handle, target = _validate_move_target(c, args)
    try:
        object_name = retry_document_read(lambda: str(target.ObjectName))
        rectangular = (object_name != 'TDbColumn'
                       or retry_document_read(lambda: is_rectangular_column(target)))
    except Exception as exc:
        raise OutcomeUnknown(f'Copy source {handle} type could not be read: {exc}') from exc
    if object_name not in COPYABLE_OBJECT_NAMES:
        raise ValueError(f'entity.copy does not support {object_name}')
    if not rectangular:
        raise ValueError('entity.copy requires a standalone rectangular native column')

    original = _copy_snapshot(c, handle, target, object_name, 'before copy')
    if object_name == 'TDbColumn' and (original['flags'][0] != 1 or original['flags'][2] != 0):
        raise ValueError('entity.copy requires a standard rectangular native column')
    origin = original['vertices'][0] if object_name == 'AcDbPolyline' else original['insertion'][:2]
    dx, dy = args['delta'][:2]
    target_pt = (origin[0] + dx, origin[1] + dy)
    if not all(math.isfinite(v) for v in target_pt):
        raise OutcomeUnknown('Copy target coordinates are not finite')
    c.run_command(
        '(progn (command "_.COPY" (handent "%s") "" "_non" "%.6f,%.6f" '
        '"_non" "%.6f,%.6f") (princ))\n' % (handle, *origin, *target_pt)
    )

    try:
        after = c.entities()
        removed = set(before) - set(after)
        added = set(after) - set(before)
        if removed or len(added) != 1:
            raise OutcomeUnknown(f'entity.copy expected one addition and no removals; '
                                 f'added {sorted(added)}, removed {sorted(removed)}')
        new_handle = added.pop()
        if any(str(after[h].ObjectName) != object_name for h in (handle, new_handle)):
            raise OutcomeUnknown(f'entity.copy object type mismatch for {handle} or {new_handle}')
        source_after = _copy_snapshot(c, handle, after[handle], object_name, 'source after copy')
        copy_after = _copy_snapshot(c, new_handle, after[new_handle], object_name, 'new entity after copy')
        _verify_copy_snapshot(original, source_after, (0, 0), object_name, f'source {handle}')
        _verify_copy_snapshot(original, copy_after, (dx, dy), object_name, f'new entity {new_handle}')
        return {
            'success': True, 'document_id': c.document_id,
            'handles': [new_handle], 'entities': [c.describe(after[new_handle])],
            'evidence': {'copy': {'source_handle': handle, 'new_handle': new_handle,
                                  'object_name': object_name, 'delta': [dx, dy],
                                  'source_origin': list(origin), 'target_origin': list(target_pt),
                                  'before': original, 'source_after': source_after, 'copy_after': copy_after}},
            'validation': {'type': True, 'properties': True, 'reopen': 'pending',
                           'geometry': ('verified_dxf_10_and_11' if object_name == 'TDbColumn'
                                        else 'verified_com_geometry')},
        }
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(f'entity.copy readback failed for {handle}: {exc}') from exc


TRANSFORMABLE_OBJECT_NAMES = frozenset({
    'AcDbText', 'AcDbPolyline', 'AcDbLine', 'AcDbCircle', 'AcDbArc',
})
MIRRORABLE_OBJECT_NAMES = frozenset({'AcDbLine', 'AcDbCircle', 'AcDbArc', 'AcDbPolyline'})


def _verify_transform(before, after, map_point, angle_rad=0.0, *,
                      scale_factor=1.0, mirror_angle=None):
    for key, value in before.items():
        actual = after[key]
        if key in ('start', 'end', 'center', 'insertion'):
            _verify_primitive_point(f'DXF {key}', actual, map_point(value), 0.05)
        elif key == 'vertices':
            if len(actual) != len(value):
                raise OutcomeUnknown('DXF vertex count changed during transform')
            for index, (original, updated) in enumerate(zip(value, actual)):
                _verify_primitive_point(f'DXF vertex {index}', updated, map_point(original), 0.05)
        elif key in ('start_angle_rad', 'end_angle_rad', 'rotation'):
            expected = value + angle_rad
            if mirror_angle is not None and key in ('start_angle_rad', 'end_angle_rad'):
                opposite = 'end_angle_rad' if key == 'start_angle_rad' else 'start_angle_rad'
                # Reflection reverses the sweep, so the original end becomes the start.
                expected = (2 * mirror_angle - before[opposite]) % math.tau
            _verify_rotation(f'DXF {key}', actual, expected, 1e-3)
        elif key in ('radius', 'height'):
            _verify_edit_number(f'DXF {key}', actual, value * scale_factor, 0.05)
        elif actual != value:
            raise OutcomeUnknown(f'DXF {key} changed during transform')


def transform_entity(c, op, args, before):
    handle, entity = _validate_move_target(c, args, before)
    object_name = str(property_read(entity, 'ObjectName'))
    if object_name not in TRANSFORMABLE_OBJECT_NAMES:
        raise ValueError(f'{op} does not support ObjectName {object_name}')
    before_dxf = transform_dxf(c, handle, object_name)

    if op == 'entity.move':
        dx, dy, _ = args['delta']
        angle_rad = 0.0

        def map_point(pt):
            return pt[0] + dx, pt[1] + dy

        evidence = {'delta': args['delta']}
        c.run_command(f'(progn (command "_.MOVE" (handent "{handle}") "" "_non" (list 0.0 0.0 0.0) '
                      f'"_non" (list {dx:.6f} {dy:.6f} 0.0)) (princ))\n')
    else:
        cx, cy, _ = args['base_point']
        angle_rad = math.radians(args['angle_deg'])
        cosine, sine = math.cos(angle_rad), math.sin(angle_rad)

        def map_point(pt):
            x, y = pt[0] - cx, pt[1] - cy
            return cx + x * cosine - y * sine, cy + x * sine + y * cosine

        evidence = {'base_point': args['base_point'], 'angle_deg': args['angle_deg']}
        _run_rotate_command(c, handle, args['base_point'], args['angle_deg'])

    with _verify_mutation():
        updated = _after_edit(c, op, handle, before, object_name)
        after_dxf = transform_dxf(c, handle, object_name)
        _verify_transform(before_dxf, after_dxf, map_point, angle_rad)
        return _part_edit_result(c, handle, updated, op.split('.')[1], {
            'object_name': object_name, **evidence,
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
        }, geometry='verified_dxf')


def _scale_pt(pt, base, factor):
    return (base[0] + (pt[0] - base[0]) * factor,
            base[1] + (pt[1] - base[1]) * factor)


def scale_entity(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    object_name = str(property_read(entity, 'ObjectName'))
    if object_name not in TRANSFORMABLE_OBJECT_NAMES:
        raise ValueError(f'entity.scale does not support ObjectName {object_name}')
    before_dxf = transform_dxf(c, handle, object_name)
    base = args['base_point']
    bx, by, _ = base
    factor = args['scale_factor']
    c.run_command(f'(progn (command "_.SCALE" (handent "{handle}") "" "_non" '
                  f'(list {bx:.6f} {by:.6f} 0.0) {factor:.6f}) (princ))\n')

    with _verify_mutation():
        updated = _after_edit(c, 'entity.scale', handle, before, object_name)
        after_dxf = transform_dxf(c, handle, object_name)
        _verify_transform(before_dxf, after_dxf, lambda pt: _scale_pt(pt, base, factor),
                          scale_factor=factor)
        return _part_edit_result(c, handle, updated, 'scale', {
            'object_name': object_name, 'base_point': base, 'scale_factor': factor,
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
        }, geometry='verified_dxf')


def _mirror_pt(pt, a, u):
    vx, vy = pt[0] - a[0], pt[1] - a[1]
    projection = vx * u[0] + vy * u[1]
    fx, fy = a[0] + projection * u[0], a[1] + projection * u[1]
    return 2 * fx - pt[0], 2 * fy - pt[1]


def mirror_entity(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    object_name = str(property_read(entity, 'ObjectName'))
    if object_name not in MIRRORABLE_OBJECT_NAMES:
        raise ValueError(f'entity.mirror does not support ObjectName {object_name}')
    before_dxf = transform_dxf(c, handle, object_name)
    axis_start, axis_end = args['axis_start'], args['axis_end']
    x1, y1, _ = axis_start
    x2, y2, _ = axis_end
    erase_source = args['erase_source']
    erase_answer = 'Y' if erase_source else 'N'
    c.run_command(f'(progn (command "_.MIRROR" (handent "{handle}") "" "_non" '
                  f'(list {x1:.6f} {y1:.6f} 0.0) "_non" (list {x2:.6f} {y2:.6f} 0.0) '
                  f'"{erase_answer}") (princ))\n')

    with _verify_mutation():
        after = c.entities()
        added, removed = set(after) - set(before), set(before) - set(after)
        if erase_source and not added and not removed:
            target_handle = handle
        elif len(added) == 1 and removed == ({handle} if erase_source else set()):
            target_handle = next(iter(added))
        else:
            raise OutcomeUnknown('Unexpected entity handle changes during entity.mirror')
        if not erase_source:
            if (str(property_read(after[handle], 'ObjectName')) != object_name
                    or transform_dxf(c, handle, object_name) != before_dxf):
                raise OutcomeUnknown('Source entity changed during entity.mirror')
        target = after[target_handle]
        if str(property_read(target, 'ObjectName')) != object_name:
            raise OutcomeUnknown('COM type changed during entity.mirror')
        after_dxf = transform_dxf(c, target_handle, object_name)
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        unit = (dx / length, dy / length)
        _verify_transform(before_dxf, after_dxf, lambda pt: _mirror_pt(pt, axis_start, unit),
                          mirror_angle=math.atan2(dy, dx))
        return {'success': True, 'document_id': c.document_id, 'handles': [target_handle],
                'entities': [c.describe(target)], 'evidence': {'mirror': {
                    'source_handle': handle, 'result_handle': target_handle, 'erase_source': erase_source,
                    'object_name': object_name, 'axis_start': axis_start, 'axis_end': axis_end,
                    'dxf_before': before_dxf, 'dxf_after': after_dxf,
                }}, 'validation': {'type': True, 'properties': True,
                                   'geometry': 'verified_dxf', 'reopen': 'pending'}}


def move_column(c, args, before):
    """Translate a standalone rectangular native column via atomic LISP _.MOVE."""
    handle, col = _validate_move_target(c, args)
    if not is_rectangular_column(col):
        raise ValueError('column.move requires a rectangular native column')
    before_dxf = column_dxf(c, handle)
    _require_column_dxf(before_dxf, 'before move')
    dx, dy = float(args['delta'][0]), float(args['delta'][1])
    origin, target = _run_move_command(c, handle, before_dxf['insertion'], (dx, dy))
    try:
        after = c.entities()
        if set(after) != set(before):
            raise OutcomeUnknown('Entity handles changed during column.move')
        after_dxf = column_dxf(c, handle)
        _require_column_dxf(after_dxf, 'after move')
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


def _require_opening_clearance(c, handle, frame, station, width, *, host_handle=None):
    half_width = width / 2.0
    low, high = frame['span']
    if station - half_width < low + SPLIT_CLEARANCE_MM or station + half_width > high - SPLIT_CLEARANCE_MM:
        raise ValueError('Opening target position is too close to the wall end')

    target_low = station - half_width - SPLIT_CLEARANCE_MM
    target_high = station + half_width + SPLIT_CLEARANCE_MM
    for other in _split_openings(c, frame):
        if other['handle'] == handle:
            continue
        if min(target_high, other['interval'][1]) <= max(target_low, other['interval'][0]):
            continue
        if host_handle is not None:
            refs = entity_wall_hosts(c, other['handle'])
            if any(str(ref['type']).upper() == 'TCH_WALL'
                   and str(ref['handle']).upper() == host_handle for ref in refs):
                raise ValueError('Opening target position overlaps another opening')
            if any(not ref.get('handle') for ref in refs):
                raise OutcomeUnknown(f'Could not resolve overlapping opening {other["handle"]} host references')
            continue
        raise ValueError('Opening target position overlaps another opening')


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
    _require_opening_clearance(c, handle, frame, target_station, width)

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


DELETABLE_OBJECT_NAMES = {
    'TDbOpening', 'TDbColumn', 'TDbWall', 'TDbSlab', 'TDbSlopeRoof',
    'TDbRectStair', 'TDbLineStair', 'AcDbText', 'AcDbPolyline',
}


def _wait_hole_area_match(c, handle, before, expected_delta, timeout=5.0):
    """Poll wall HoleArea until target wall shifts by expected_delta (positive or negative)."""
    deadline = time.monotonic() + timeout
    seen = None
    while True:
        after = hole_areas(c)
        if set(after) != set(before):
            seen = f'wall handles changed {len(before)} -> {len(after)}'
        else:
            delta = after[handle] - before[handle]
            others_ok = all(abs(after[h] - before[h]) <= 0.02 for h in before if h != handle)
            seen = f'expected delta {expected_delta:.4f}, got {delta:.4f}, others_ok={others_ok}'
            if abs(delta - expected_delta) <= 0.02 and others_ok:
                return after
        if time.monotonic() >= deadline:
            raise OutcomeUnknown(f'Host wall hole-area update mismatch ({seen})')
        time.sleep(0.25)


def _run_rotate_command(c, handle, origin, angle_deg):
    bx, by = float(origin[0]), float(origin[1])
    lisp = ('(progn (command "_.ROTATE" (handent "%s") "" "_non" (list %.6f %.6f 0.0) %.6f) (princ))\n'
            % (handle, bx, by, float(angle_deg)))
    c.run_command(lisp)
    return (bx, by)


def rotate_column(c, args, before):
    """Rotate a standalone rectangular native column around its DXF 11 insertion via LISP _.ROTATE."""
    handle, col = _validate_move_target(c, args)
    if not is_rectangular_column(col):
        raise ValueError('column.rotate requires a rectangular native column')
    before_dxf = column_dxf(c, handle)
    _require_column_dxf(before_dxf, 'before rotate')
    before_bulges = before_dxf.get('bulges')
    if before_bulges is not None and (len(before_bulges) != 4
                                     or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in before_bulges)):
        raise OutcomeUnknown('Column DXF bulges (code 50) invalid before rotate')
    angle_deg = float(args['angle_deg'])
    origin = _run_rotate_command(c, handle, before_dxf['insertion'], angle_deg)
    try:
        after = c.entities()
        if set(after) != set(before):
            raise OutcomeUnknown('Entity handles changed during column.rotate')
        after_dxf = column_dxf(c, handle)
        _require_column_dxf(after_dxf, 'after rotate')
        if (abs(after_dxf['insertion'][0] - origin[0]) > 0.01
                or abs(after_dxf['insertion'][1] - origin[1]) > 0.01):
            raise OutcomeUnknown('Column insertion shifted during rotate: %r -> %r'
                                 % (origin, after_dxf['insertion']))
        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        for b_pt, a_pt in zip(before_dxf['vertices'], after_dxf['vertices']):
            rx = b_pt[0] - origin[0]
            ry = b_pt[1] - origin[1]
            exp_x = origin[0] + rx * cos_a - ry * sin_a
            exp_y = origin[1] + rx * sin_a + ry * cos_a
            if abs(a_pt[0] - exp_x) > 0.05 or abs(a_pt[1] - exp_y) > 0.05:
                raise OutcomeUnknown('Column vertex rotation mismatch after rotate')
        if after_dxf['height'] != before_dxf['height'] or after_dxf['flags'] != before_dxf['flags']:
            raise OutcomeUnknown('Column properties changed unexpectedly during rotate')
        after_bulges = after_dxf.get('bulges')
        if after_bulges is not None:
            if (len(after_bulges) != 4
                    or any(abs(float(v)) > 1e-6 for v in after_bulges)):
                raise OutcomeUnknown('Column DXF bulges (code 50) changed during rotate: %r' % (after_bulges,))
        info = c.describe(after[handle])
        return {
            'success': True,
            'document_id': c.document_id,
            'handles': [handle],
            'entities': [info],
            'evidence': {
                'rotate': {
                    'handle': handle,
                    'angle_deg': angle_deg,
                    'insertion_before': list(origin),
                    'insertion_after': list(after_dxf['insertion']),
                    'vertices_before': [list(p) for p in before_dxf['vertices']],
                    'vertices_after': [list(p) for p in after_dxf['vertices']],
                    'height': after_dxf['height'],
                    'flags': list(after_dxf['flags']),
                    'bulges': list(after_bulges) if after_bulges is not None else None,
                },
            },
            'validation': {'type': True, 'properties': True,
                           'geometry': 'verified_dxf_10_11_and_50',
                           'reopen': 'pending'},
        }
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(str(exc)) from exc


def edit_wall(c, args, before):
    """Edit an existing native TDbWall's thickness/height via pure COM properties."""
    handle, wall = _validate_move_target(c, args)
    if wall.ObjectName != 'TDbWall':
        raise ValueError('wall.edit requires a native wall')
    prop_map = (('left_width', 'LeftWidth'), ('right_width', 'RightWidth'), ('height', 'Height'))
    before_props = {}
    for _, com_name in prop_map:
        try:
            before_props[com_name] = float(property_read(wall, com_name))
        except Exception as exc:
            raise OutcomeUnknown(f'Wall {com_name} could not be read before edit') from exc
    holes_before = hole_areas(c)
    wanted = {com_name: float(args[arg_key]) for arg_key, com_name in prop_map if arg_key in args}
    try:
        for com_name, value in wanted.items():
            property_write(wall, com_name, value)
        invoke_document(lambda: wall, 'Update')
        after = c.entities()
        if set(after) != set(before):
            raise OutcomeUnknown('Entity handles changed during wall.edit')
        updated = after[handle]
        after_props = {}
        for _, com_name in prop_map:
            after_props[com_name] = float(property_read(updated, com_name))
            expected = wanted.get(com_name, before_props[com_name])
            if abs(after_props[com_name] - expected) > 0.01:
                raise OutcomeUnknown(f'Wall {com_name} readback mismatch: expected {expected:g}, got {after_props[com_name]:g}')
        holes_after = hole_areas(c)
        if set(holes_after) != set(holes_before) or any(
                abs(holes_after[h] - holes_before[h]) > 0.02 for h in holes_before):
            raise OutcomeUnknown('Wall hole area changed unexpectedly during wall.edit')
        info = c.describe(updated)
        return {
            'success': True,
            'document_id': c.document_id,
            'handles': [handle],
            'entities': [info],
            'evidence': {
                'edit': {
                    'handle': handle,
                    'properties_before': before_props,
                    'properties_requested': wanted,
                    'properties_after': after_props,
                    'hole_area_before': holes_before[handle],
                    'hole_area_after': holes_after[handle],
                },
            },
            'validation': {'type': True, 'properties': True,
                           'host': 'preserved_hole_area',
                           'reopen': 'pending'},
        }
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(str(exc)) from exc


def relabel_opening(c, args, before):
    """Change the native opening label and verify its dimensions and host."""
    handle, opening = _validate_move_target(c, args, before)
    if str(property_read(opening, 'ObjectName')) != 'TDbOpening':
        raise ValueError('opening.relabel requires a native opening')
    host_handle, _ = _unique_wall_host(c, handle, invalid_host=ValueError)
    before_label = str(property_read(opening, 'Label'))
    names = ('Width', 'Height', 'WinSill')
    before_props = _edit_properties(opening, names)
    before_dxf = opening_label_dxf(c, handle)
    if before_dxf['type'] != 'TCH_OPENING':
        raise OutcomeUnknown('Opening DXF type could not be verified before relabel')
    holes_before = hole_areas(c)
    if host_handle not in holes_before:
        raise ValueError('Opening host is not a native wall in ModelSpace')
    if any(not math.isfinite(area) for area in holes_before.values()):
        raise OutcomeUnknown('Wall HoleArea is not finite before relabel')

    label = args['label']
    with _verify_mutation():
        property_write(opening, 'Label', label)
        invoke_document(lambda: opening, 'Update')
        updated = _after_edit(c, 'opening.relabel', handle, before, 'TDbOpening')
        after_label = str(property_read(updated, 'Label'))
        if after_label != label:
            raise OutcomeUnknown(f'Opening COM Label mismatch: expected {label!r}, got {after_label!r}')
        after_props = _edit_properties(updated, names)
        for name in names:
            _verify_edit_number(f'Opening COM {name}', after_props[name], before_props[name], 0.01)
        after_dxf = opening_label_dxf(c, handle)
        if after_dxf['type'] != 'TCH_OPENING' or after_dxf['label'] != label:
            raise OutcomeUnknown(f'Opening DXF type or 302 label mismatch after relabel: {after_dxf}')
        if _unique_wall_host(c, handle)[0] != host_handle:
            raise OutcomeUnknown('Opening host changed during relabel')
        _wait_hole_area_match(c, host_handle, holes_before, 0.0)
        evidence = {'host_handle': host_handle, 'label_before': before_label,
                    'label_requested': label, 'label_after': after_label, 'dxf_before': before_dxf,
                    'dxf_after': after_dxf, 'properties_after': after_props}
        return _part_edit_result(c, handle, updated, 'relabel', evidence,
                                 dxf='verified_302', host='verified_dxf_330_and_hole_areas')


def create_group_opening(c, args, before):
    if args['document_id'] != c.document_id:
        raise ValueError('Entity document_id mismatch')
    handles = args['handles']
    host_handles = set()
    for handle in handles:
        if handle not in before:
            raise ValueError(f'Opening handle not found in ModelSpace: {handle}')
        if str(property_read(late(before[handle]), 'ObjectName')) != 'TDbOpening':
            raise ValueError(f'opening.group_create requires native TDbOpening handles, got {handle}')
        host_handles.add(_unique_wall_host(c, handle, invalid_host=ValueError)[0])
    if len(host_handles) != 1:
        raise ValueError('opening.group_create requires all openings to belong to the same host wall')
    host_handle, = host_handles
    holes_before = hole_areas(c)
    if host_handle not in holes_before:
        raise ValueError('Opening host is not a native wall in ModelSpace')

    label = args['label']
    with _verify_mutation():
        with _without_snaps(c):
            c.run_command(command_payload('opening.group_create', args))
        after = c.entities()
        removed = sorted(before.keys() - after.keys())
        added = sorted(after.keys() - before.keys())
        if set(removed) != set(handles) or len(added) != 1:
            raise OutcomeUnknown(f'TGroupOpening replacement mismatch: removed {removed}, added {added}')
        group_handle = added[0]
        entity = late(after[group_handle])
        if str(property_read(entity, 'ObjectName')) != 'TDbGroupOpening':
            raise OutcomeUnknown('TGroupOpening did not create a native TDbGroupOpening')
        if str(property_read(entity, 'Label')) != label:
            raise OutcomeUnknown('Group opening COM Label readback mismatch')
        width = float(property_read(entity, 'Width'))
        height = float(property_read(entity, 'Height'))
        kind = str(property_read(entity, 'GetKind') or '')
        if not (math.isfinite(width) and width > 0 and math.isfinite(height) and height > 0 and kind.strip()):
            raise OutcomeUnknown('Group opening COM dimensions or kind are invalid')
        dxf = opening_group_dxf(c, group_handle)
        if dxf['type'] != 'TCH_OPENING' or dxf['label'] != label:
            raise OutcomeUnknown('Group opening DXF type or label readback mismatch')
        _verify_edit_number('Group opening DXF 40', dxf['width'], width, 0.5)
        _verify_edit_number('Group opening DXF 41', dxf['height'], height, 0.5)
        if dxf.get('kind_code') not in (None, 8):
            raise OutcomeUnknown('Group opening DXF 71 readback mismatch')
        if _unique_wall_host(c, group_handle, allow_duplicate_same_host=True)[0] != host_handle:
            raise OutcomeUnknown('Group opening host changed during creation')
        return {
            'success': True, 'document_id': c.document_id,
            'handles': [group_handle], 'entities': [c.describe(entity)],
            'evidence': {'group_opening': {
                'command': 'TGroupOpening', 'source_handles': handles, 'removed_handles': removed,
                'host_handle': host_handle, 'label': label, 'kind': kind,
                'width': width, 'height': height, 'dxf': dxf,
            }},
            'validation': {'type': True, 'properties': True, 'dxf': 'verified_302_40_41_71',
                           'host': 'verified_dxf_330', 'reopen': 'pending'},
        }


def edit_opening(c, args, before):
    """Edit native opening dimensions via COM and verify the DXF and host effects."""
    handle, opening = _validate_move_target(c, args, before)
    if str(opening.ObjectName) != 'TDbOpening':
        raise ValueError('opening.edit requires a native opening')
    host_handle, _ = _unique_wall_host(c, handle, invalid_host=ValueError)
    prop_map = (('width', 'Width'), ('height', 'Height'), ('sill', 'WinSill'))
    dxf_options = {}
    if 'text_height' in args:
        prop_map += (('text_height', 'TextHeight'),)
        dxf_options['include_text_height'] = True
    before_props = {}
    for _, com_name in prop_map:
        try:
            value = float(property_read(opening, com_name))
        except Exception as exc:
            raise OutcomeUnknown(f'Opening {com_name} could not be read before edit') from exc
        if not math.isfinite(value):
            raise OutcomeUnknown(f'Opening {com_name} is not finite before edit')
        if com_name == 'TextHeight' and value <= 0:
            raise OutcomeUnknown('Opening TextHeight is not positive before edit')
        before_props[com_name] = value
    before_dxf = opening_dxf(c, handle, **dxf_options)
    insertion = before_dxf['insertion']
    if before_dxf['type'] != 'TCH_OPENING' or insertion is None or not all(map(math.isfinite, insertion)):
        raise OutcomeUnknown('Opening DXF geometry could not be read before edit')
    try:
        holes_before = hole_areas(c)
    except Exception as exc:
        raise OutcomeUnknown('Wall HoleArea could not be read before edit') from exc
    if host_handle not in holes_before:
        raise ValueError('Opening host is not a native wall in ModelSpace')
    if any(not math.isfinite(area) for area in holes_before.values()):
        raise OutcomeUnknown('Wall HoleArea is not finite before edit')
    if 'width' in args:
        host_wall = c.entity(host_handle, args['document_id'])
        frame = _split_frame(c.describe(host_wall))
        station = insertion[1] if frame['vertical'] else insertion[0]
        _require_opening_clearance(c, handle, frame, station, args['width'], host_handle=host_handle)

    wanted = {com_name: float(args[arg_key]) for arg_key, com_name in prop_map if arg_key in args}
    expected = {**before_props, **wanted}
    try:
        for com_name, value in wanted.items():
            property_write(opening, com_name, value)
        invoke_document(lambda: opening, 'Update')
        after = c.entities()
        if set(after) != set(before):
            raise OutcomeUnknown('Entity handles changed during opening.edit')
        updated = after[handle]
        if str(updated.ObjectName) != 'TDbOpening':
            raise OutcomeUnknown('Opening COM type changed during edit')
        after_props = {}
        for com_name, target in expected.items():
            value = float(property_read(updated, com_name))
            if not abs(value - target) <= 0.01:
                raise OutcomeUnknown(f'Opening {com_name} readback mismatch: expected {target:g}, got {value:g}')
            after_props[com_name] = value
        after_dxf = opening_dxf(c, handle, **dxf_options)
        if after_dxf['type'] != 'TCH_OPENING' or after_dxf['insertion'] is None:
            raise OutcomeUnknown('Opening DXF geometry could not be read after edit')
        for key, com_name in prop_map[:2]:
            value = after_dxf[key]
            if value is None or not abs(value - expected[com_name]) <= 0.5:
                raise OutcomeUnknown(f'Opening DXF {key} mismatch after edit: {value!r} vs {expected[com_name]:g}')
        if 'text_height' in args:
            _verify_edit_number('Opening DXF 140', after_dxf.get('text_height'), args['text_height'], 0.05)
        if not math.dist(insertion, after_dxf['insertion']) <= 0.5:
            raise OutcomeUnknown('Opening DXF insertion shifted during edit')
        if _unique_wall_host(c, handle)[0] != host_handle:
            raise OutcomeUnknown('Opening host changed during edit')
        expected_delta = (after_props['Width'] * after_props['Height']
                          - before_props['Width'] * before_props['Height']) / 1_000_000.0
        holes_after = _wait_hole_area_match(c, host_handle, holes_before, expected_delta)
        return {
            'success': True,
            'document_id': c.document_id,
            'handles': [handle],
            'entities': [c.describe(updated)],
            'evidence': {
                'edit': {
                    'handle': handle,
                    'host_handle': host_handle,
                    'properties_before': before_props,
                    'properties_requested': wanted,
                    'properties_after': after_props,
                    'dxf_before': before_dxf,
                    'dxf_after': after_dxf,
                    'host_hole_area_before': holes_before[host_handle],
                    'host_hole_area_after': holes_after[host_handle],
                    'expected_hole_area_delta': expected_delta,
                },
            },
            'validation': {'type': True, 'properties': True,
                           'geometry': 'verified_com_and_dxf_40_41',
                           'host': 'verified_dxf_330_and_hole_area_delta',
                           'reopen': 'pending'},
        }
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(str(exc)) from exc


@contextmanager
def _verify_mutation():
    try:
        yield
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(str(exc)) from exc


def _edit_properties(entity, names):
    props = {}
    for name in names:
        value = property_read(entity, name)
        if name == 'TextString':
            props[name] = str(value)
        else:
            number = float(value)
            if not math.isfinite(number):
                raise OutcomeUnknown(f'COM {name} is not finite')
            props[name] = number
    return props


def _after_edit(c, op, handle, before, object_name):
    after = c.entities()
    if set(after) != set(before):
        raise OutcomeUnknown(f'Entity handles changed during {op}')
    updated = after[handle]
    if str(property_read(updated, 'ObjectName')) != object_name:
        raise OutcomeUnknown(f'COM type changed during {op}')
    return updated


def _apply_com_edit(c, op, handle, entity, before, expected_type, wanted, names):
    for name, value in wanted.items():
        property_write(entity, name, value)
    invoke_document(lambda: entity, 'Update')
    updated = _after_edit(c, op, handle, before, expected_type)
    return updated, _edit_properties(updated, names)


def _verify_edit_number(label, value, expected, tolerance):
    if value is None or not abs(value - expected) <= tolerance:
        raise OutcomeUnknown(f'{label} readback mismatch: expected {expected!r}, got {value!r}')


def _verify_rotation(label, value, expected, tolerance):
    if value is None or not abs((value - expected + math.pi) % math.tau - math.pi) <= tolerance:
        raise OutcomeUnknown(f'{label} rotation mismatch: expected {expected!r}, got {value!r}')


def _part_edit_result(c, handle, updated, operation, evidence, **validation):
    return {'success': True, 'document_id': c.document_id, 'handles': [handle],
            'entities': [c.describe(updated)], 'evidence': {operation: {'handle': handle, **evidence}},
            'validation': {'type': True, 'properties': True, **validation, 'reopen': 'pending'}}


def edit_space(c, args, before):
    handle, space = _validate_move_target(c, args, before)
    if str(property_read(space, 'ObjectName')) != 'TDbSpace':
        raise ValueError('space.edit requires a native TDbSpace')
    before_name = str(property_read(space, 'Name'))
    with _verify_mutation():
        before_dxf = space_dxf(c, handle)
        if before_dxf['type'] != 'TCH_SPACE' or any(
                value is None or not math.isfinite(value)
                for value in (before_dxf['area_m2'], before_dxf['perimeter_mm'])):
            raise OutcomeUnknown('Space DXF type, area, or perimeter could not be verified before edit')

    name = args['name']
    with _verify_mutation():
        property_write(space, 'Name', name)
        invoke_document(lambda: space, 'Update')
        updated = _after_edit(c, 'space.edit', handle, before, 'TDbSpace')
        after_name = str(property_read(updated, 'Name'))
        if after_name != name:
            raise OutcomeUnknown('Space COM Name readback mismatch')
        after_dxf = space_dxf(c, handle)
        if after_dxf['type'] != 'TCH_SPACE' or after_dxf['name'] != name:
            raise OutcomeUnknown('Space DXF type or name readback mismatch')
        if after_dxf['number'] != before_dxf['number']:
            raise OutcomeUnknown('Space DXF number changed during edit')
        _verify_edit_number('Space DXF area', after_dxf['area_m2'], before_dxf['area_m2'], 0.01)
        _verify_edit_number('Space DXF perimeter', after_dxf['perimeter_mm'], before_dxf['perimeter_mm'], 0.5)
        return _part_edit_result(c, handle, updated, 'space_edit', {
            'name_before': before_name, 'name_requested': name, 'name_after': after_name,
            'area_m2': after_dxf['area_m2'], 'perimeter_mm': after_dxf['perimeter_mm'],
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
        }, dxf='verified_1_2_41_42')


def _add_primitive(c, before, kind, *args):
    model_space = retry_document_read(lambda: c.doc.ModelSpace)
    created = invoke_document(lambda: model_space, 'Add' + kind, *args)
    handle = retry_document_read(lambda: str(late(created).Handle)).upper()
    return _verify_primitive_creation(c, before, handle, kind)


def _verify_primitive_creation(c, before, handle, kind, *, object_names=()):
    after = c.entities()
    if set(after) - set(before) != {handle} or set(before) - set(after):
        raise OutcomeUnknown(f'Unexpected ModelSpace handle changes during {kind} creation')
    entity = after[handle]
    if str(property_read(entity, 'ObjectName')) not in (object_names or ('AcDb' + kind,)):
        raise OutcomeUnknown(f'COM type mismatch for {kind} {handle}')
    return handle, entity


def _verify_primitive_point(label, actual, expected, tolerance):
    if actual is None or len(actual) < 2 or not math.dist(actual[:2], expected[:2]) <= tolerance:
        raise OutcomeUnknown(f'{label} readback mismatch: expected {expected!r}, got {actual!r}')


def _primitive_result(c, handle, entity, kind, evidence, dxf, groups, *, dxf_type=None):
    if dxf['type'] != (dxf_type or kind.upper()):
        raise OutcomeUnknown(f'DXF type mismatch for {kind} {handle}')
    return {'success': True, 'document_id': c.document_id, 'handles': [handle],
            'entities': [c.describe(entity)], 'evidence': {kind: {**evidence, 'dxf': dxf}},
            'validation': {'type': True, 'properties': True, 'dxf': f'verified_{groups}',
                           'reopen': 'pending'}}


def create_line(c, args, before):
    with _verify_mutation():
        handle, entity = _add_primitive(c, before, 'Line', vector(args['start']), vector(args['end']))
        start = list(property_read(entity, 'StartPoint'))
        end = list(property_read(entity, 'EndPoint'))
        _verify_primitive_point('COM StartPoint', start, args['start'], 0.01)
        _verify_primitive_point('COM EndPoint', end, args['end'], 0.01)
        dxf = line_dxf(c, handle)
        _verify_primitive_point('DXF 10', dxf['start'], args['start'], 0.5)
        _verify_primitive_point('DXF 11', dxf['end'], args['end'], 0.5)
        evidence = {'start': start, 'end': end, 'length': math.dist(start[:2], end[:2])}
        return _primitive_result(c, handle, entity, 'line', evidence, dxf, '10_11')


def _verify_com_center_radius(entity, args):
    center = list(property_read(entity, 'Center'))
    radius = float(property_read(entity, 'Radius'))
    _verify_primitive_point('COM Center', center, args['center'], 0.01)
    _verify_edit_number('COM Radius', radius, args['radius'], 0.01)
    return {'center': center, 'radius': radius}


def _verify_dxf_center_radius(dxf, args):
    _verify_primitive_point('DXF 10', dxf['center'], args['center'], 0.5)
    _verify_edit_number('DXF 40', dxf['radius'], args['radius'], 0.5)


def create_circle(c, args, before):
    with _verify_mutation():
        handle, entity = _add_primitive(c, before, 'Circle', vector(args['center']), args['radius'])
        evidence = _verify_com_center_radius(entity, args)
        dxf = circle_dxf(c, handle)
        _verify_dxf_center_radius(dxf, args)
        return _primitive_result(c, handle, entity, 'circle', evidence, dxf, '10_40')


def create_arc(c, args, before):
    start_rad = math.radians(args['start_angle_deg'])
    end_rad = math.radians(args['end_angle_deg'])
    with _verify_mutation():
        handle, entity = _add_primitive(c, before, 'Arc', vector(args['center']), args['radius'],
                                        start_rad, end_rad)
        evidence = _verify_com_center_radius(entity, args)
        _verify_rotation('COM StartAngle', float(property_read(entity, 'StartAngle')), start_rad, 1e-4)
        _verify_rotation('COM EndAngle', float(property_read(entity, 'EndAngle')), end_rad, 1e-4)
        dxf = arc_dxf(c, handle)
        _verify_dxf_center_radius(dxf, args)
        _verify_rotation('DXF 50', dxf['start_angle_rad'], start_rad, 1e-3)
        _verify_rotation('DXF 51', dxf['end_angle_rad'], end_rad, 1e-3)
        evidence.update(start_angle_deg=args['start_angle_deg'], end_angle_deg=args['end_angle_deg'],
                        start_angle_rad=start_rad, end_angle_rad=end_rad)
        return _primitive_result(c, handle, entity, 'arc', evidence, dxf, '10_40_50_51')


_POLYLINE_NUMBERS = (('elevation', 'Elevation', 38), ('constant_width', 'ConstantWidth', 43))


def create_rectangle(c, args, before):
    first, second = args['corner1'], args['corner2']
    x0, x1 = sorted((first[0], second[0]))
    y0, y1 = sorted((first[1], second[1]))
    vertices = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    flat = [coord for vertex in vertices for coord in vertex]
    width, height = x1 - x0, y1 - y0
    expected_area = width * height

    with _verify_mutation():
        model_space = retry_document_read(lambda: c.doc.ModelSpace)
        created = late(invoke_document(lambda: model_space, 'AddLightWeightPolyline', vector(flat)))
        property_write(created, 'Closed', True)
        for key, name, _ in _POLYLINE_NUMBERS:
            if key in args:
                property_write(created, name, args[key])
        invoke_document(lambda: created, 'Update')
        handle = str(property_read(created, 'Handle')).upper()
        handle, entity = _verify_primitive_creation(c, before, handle, 'Polyline')
        if not bool(property_read(entity, 'Closed')):
            raise OutcomeUnknown('COM Closed readback mismatch for rectangle')
        area = float(property_read(entity, 'Area'))
        if not (math.isfinite(area) and math.isfinite(expected_area)):
            raise OutcomeUnknown('Rectangle area must be finite')
        _verify_edit_number('COM rectangle area', area, expected_area, max(1.0, 0.005 * expected_area))
        dxf = polyline_props_dxf(c, handle)
        if dxf['closed'] != 1 or len(dxf['vertices'] or ()) != 4:
            raise OutcomeUnknown('Rectangle DXF must describe a closed four-vertex polyline')
        for actual, expected in zip(dxf['vertices'], vertices):
            _verify_primitive_point('DXF 10 rectangle vertex', actual, expected, 0.5)
        for key, _, group in _POLYLINE_NUMBERS:
            if key in args:
                _verify_edit_number(f'DXF {group}', dxf[key], args[key], 0.5)
        evidence = {'corners': vertices, 'width': width, 'height': height, 'area': area,
                    'elevation': dxf['elevation'], 'constant_width': dxf['constant_width']}
        return _primitive_result(c, handle, entity, 'rectangle', evidence, dxf, '10_70_38_43',
                                 dxf_type='LWPOLYLINE')


def edit_polyline(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    if str(property_read(entity, 'ObjectName')) != 'AcDbPolyline':
        raise ValueError('polyline.edit requires a native AcDbPolyline')
    before_dxf = polyline_props_dxf(c, handle)
    if before_dxf['type'] != 'LWPOLYLINE' or not before_dxf['vertices']:
        raise ValueError('polyline.edit requires DXF for an LWPOLYLINE with vertices')
    if any(abs(bulge) > 1e-6 for bulge in (before_dxf.get('bulges') or ())):
        raise ValueError('polyline.edit does not support bulged polylines')

    closed = bool(property_read(entity, 'Closed'))
    try:
        constant_width = float(property_read(entity, 'ConstantWidth'))
    except Exception as exc:
        raise ValueError('polyline.edit requires a readable ConstantWidth (mixed widths are unsupported)') from exc
    before_props = {'Closed': closed, 'ConstantWidth': constant_width,
                    'Elevation': float(property_read(entity, 'Elevation'))}
    wanted = {'Closed': args['closed']} if 'closed' in args else {}
    wanted.update({name: args[key] for key, name, _ in _POLYLINE_NUMBERS if key in args})
    expected = {**before_props, **wanted}

    with _verify_mutation():
        for name, value in wanted.items():
            property_write(entity, name, value)
        invoke_document(lambda: entity, 'Update')
        updated = _after_edit(c, 'polyline.edit', handle, before, 'AcDbPolyline')
        after_props = {'Closed': bool(property_read(updated, 'Closed')),
                       **_edit_properties(updated, ('ConstantWidth', 'Elevation'))}
        if after_props['Closed'] != expected['Closed']:
            raise OutcomeUnknown('COM Closed readback mismatch')
        for _, name, _ in _POLYLINE_NUMBERS:
            _verify_edit_number(f'COM {name}', after_props[name], expected[name], 0.01)
        after_dxf = polyline_props_dxf(c, handle)
        if after_dxf['type'] != 'LWPOLYLINE':
            raise OutcomeUnknown('DXF polyline type changed during edit')
        if after_dxf['closed'] != int(expected['Closed']):
            raise OutcomeUnknown('DXF 70 closed flag readback mismatch')
        if len(after_dxf['vertices'] or ()) != len(before_dxf['vertices']):
            raise OutcomeUnknown('DXF polyline vertex count changed during edit')
        for key, _, group in _POLYLINE_NUMBERS:
            if key in args:
                _verify_edit_number(f'DXF {group}', after_dxf[key], args[key], 0.01)
        return _part_edit_result(c, handle, updated, 'edit', {
            'properties_before': before_props, 'properties_requested': wanted, 'properties_after': after_props,
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
        }, dxf='verified_70_38_43')


def _plan_readback_matches(actual, expected, tolerance):
    if actual is not None and len(actual) == 2:
        actual = (*actual, 0.0)
    return actual is not None and len(actual) == 3 and math.dist(actual, expected) <= tolerance


def _verify_plan_readback(label, actual, expected, tolerance):
    if not _plan_readback_matches(actual, expected, tolerance):
        raise OutcomeUnknown(f'{label} readback mismatch: expected {expected!r}, got {actual!r}')


def create_mtext(c, args, before):
    point = args['point']
    rotation = attachment = None
    with _verify_mutation():
        insertion = vector(point)
        handle, entity = _add_primitive(c, before, 'MText', insertion, args['width'], args['text'])
        if 'height' in args:
            property_write(entity, 'Height', args['height'])
        if 'rotation_deg' in args:
            rotation = math.radians(args['rotation_deg'])
            property_write(entity, 'Rotation', rotation)
        if 'attachment' in args:
            attachment = args['attachment']
            property_write(entity, 'AttachmentPoint', attachment)
            # AutoCAD keeps the original text bounds when changing justification,
            # so the new attachment point can move away from the requested point.
            if list(property_read(entity, 'InsertionPoint')) != point:
                property_write(entity, 'InsertionPoint', insertion)
        if 'height' in args or rotation is not None or attachment is not None:
            invoke_document(lambda: entity, 'Update')

        text = property_read(entity, 'TextString')
        if text != args['text']:
            raise OutcomeUnknown('COM TextString readback mismatch')
        position = list(property_read(entity, 'InsertionPoint'))
        width = float(property_read(entity, 'Width'))
        height = float(property_read(entity, 'Height'))
        _verify_plan_readback('COM InsertionPoint', position, point, 0.01)
        _verify_edit_number('COM Width', width, args['width'], 0.01)
        if 'height' in args:
            _verify_edit_number('COM Height', height, args['height'], 0.01)
        elif not math.isfinite(height) or height <= 0:
            raise OutcomeUnknown('COM Height must be positive and finite')
        if rotation is not None:
            actual_rotation = float(property_read(entity, 'Rotation'))
            _verify_rotation('COM Rotation', actual_rotation, rotation, 1e-4)
        else:
            actual_rotation = None
        if attachment is not None:
            actual_attachment = property_read(entity, 'AttachmentPoint')
            if type(actual_attachment) is not int or actual_attachment != attachment:
                raise OutcomeUnknown('COM AttachmentPoint readback mismatch')

        dxf = mtext_dxf(c, handle)
        if dxf['text'] != text:
            raise OutcomeUnknown('DXF MTEXT text readback mismatch')
        _verify_plan_readback('DXF 10', dxf['insertion'], point, 0.5)
        _verify_edit_number('DXF 41', dxf['width'], args['width'], 0.5)
        if 'height' in args:
            _verify_edit_number('DXF 40', dxf['height'], args['height'], 0.5)
        if rotation is not None:
            _verify_rotation('DXF 50', dxf['rotation'], rotation, 1e-3)
        if attachment is not None and dxf['attachment'] != attachment:
            raise OutcomeUnknown('DXF 71 attachment readback mismatch')

        evidence = {'text': text, 'insertion': position, 'width': width, 'height': height,
                    'rotation': actual_rotation, 'attachment': attachment}
        return _primitive_result(c, handle, entity, 'mtext', evidence, dxf, '1_3_10_40_41_50_71')


def _add_dimension(c, before, args, kind, *method_args, object_names=()):
    model_space = retry_document_read(lambda: c.doc.ModelSpace)
    created = late(invoke_document(lambda: model_space, 'AddDim' + kind, *method_args))
    if 'text_height' in args:
        property_write(created, 'TextHeight', args['text_height'])
        invoke_document(lambda: created, 'Update')
    handle = retry_document_read(lambda: str(created.Handle)).upper()
    return _verify_primitive_creation(c, before, handle, kind + 'Dimension', object_names=object_names)


def _dimension_properties(entity, args, expected, tolerance=0.01):
    measurement = float(property_read(entity, 'Measurement'))
    _verify_edit_number('COM Measurement', measurement, expected, tolerance)
    text_height = None
    if 'text_height' in args:
        text_height = float(property_read(entity, 'TextHeight'))
        _verify_edit_number('COM TextHeight', text_height, args['text_height'], 0.01)
    return {'measurement': measurement, 'text_height': text_height}


def _verify_dimension_extensions(dxf, ext1, ext2, measurement):
    _verify_plan_readback('DXF 13', dxf['ext1'], ext1, 0.5)
    _verify_plan_readback('DXF 14', dxf['ext2'], ext2, 0.5)
    _verify_edit_number('DXF 42', dxf['measurement'], measurement, 0.5)


def create_dim_aligned(c, args, before):
    ext1, ext2, text_position = args['ext1'], args['ext2'], args['text_position']
    expected = math.dist(ext1[:2], ext2[:2])
    with _verify_mutation():
        handle, entity = _add_dimension(c, before, args, 'Aligned', vector(ext1), vector(ext2),
                                        vector(text_position))
        evidence = _dimension_properties(entity, args, expected)
        dxf = dim_dxf(c, handle)
        _verify_dimension_extensions(dxf, ext1, ext2, expected)
        evidence.update(ext1=ext1, ext2=ext2, text_position=text_position)
        return _primitive_result(c, handle, entity, 'dim_aligned', evidence, dxf, '13_14_42',
                                 dxf_type='DIMENSION')


def create_dim_linear(c, args, before):
    ext1, ext2, dim_line_point = args['ext1'], args['ext2'], args['dim_line_point']
    rotation = math.radians(args['rotation_deg'])
    expected = abs((ext2[0] - ext1[0]) * math.cos(rotation) + (ext2[1] - ext1[1]) * math.sin(rotation))
    with _verify_mutation():
        handle, entity = _add_dimension(c, before, args, 'Rotated', vector(ext1), vector(ext2),
                                        vector(dim_line_point), rotation)
        evidence = _dimension_properties(entity, args, expected)
        dxf = dim_dxf(c, handle)
        _verify_dimension_extensions(dxf, ext1, ext2, expected)
        dxf_rotation = 0.0 if dxf['rotation'] is None else dxf['rotation']
        _verify_rotation('DXF 50', dxf_rotation, rotation, 1e-3)
        evidence.update(ext1=ext1, ext2=ext2, dim_line_point=dim_line_point,
                        rotation_deg=args['rotation_deg'], rotation_rad=rotation)
        return _primitive_result(c, handle, entity, 'dim_linear', evidence, dxf, '13_14_42_50',
                                 dxf_type='DIMENSION')


def create_dim_radial(c, args, before):
    center, chord_point, leader_length = args['center'], args['chord_point'], args['leader_length']
    expected = math.dist(center[:2], chord_point[:2])
    with _verify_mutation():
        handle, entity = _add_dimension(c, before, args, 'Radial', vector(center), vector(chord_point),
                                        leader_length)
        evidence = _dimension_properties(entity, args, expected)
        dxf = dim_dxf(c, handle)
        _verify_plan_readback('DXF 10', dxf['def_point'], center, 0.5)
        _verify_plan_readback('DXF 15', dxf['chord_point'], chord_point, 0.5)
        _verify_edit_number('DXF 42', dxf['measurement'], expected, 0.5)
        evidence.update(center=center, chord_point=chord_point, leader_length=leader_length)
        return _primitive_result(c, handle, entity, 'dim_radial', evidence, dxf, '10_15_42',
                                 dxf_type='DIMENSION')


def _dimension_ray_matches(ray, vertex, endpoint):
    start, end = ray
    return ((_plan_readback_matches(start, vertex, 0.5) and _plan_readback_matches(end, endpoint, 0.5))
            or (_plan_readback_matches(end, vertex, 0.5) and _plan_readback_matches(start, endpoint, 0.5)))


def create_dim_angular(c, args, before):
    vertex = args['vertex']
    first, second, text_position = args['first_endpoint'], args['second_endpoint'], args['text_position']
    x, y = vertex[:2]
    ang1, ang2, ang_text = (math.atan2(py - y, px - x) for px, py, _ in (first, second, text_position))
    ccw_span = (ang2 - ang1) % math.tau
    ccw_text = (ang_text - ang1) % math.tau
    expected = ccw_span if ccw_text <= ccw_span else math.tau - ccw_span
    with _verify_mutation():
        handle, entity = _add_dimension(c, before, args, 'Angular', vector(vertex), vector(first),
                                        vector(second), vector(text_position),
                                        object_names=('AcDb2LineAngularDimension', 'AcDb3PointAngularDimension'))
        evidence = _dimension_properties(entity, args, expected, tolerance=1e-3)
        dxf = dim_dxf(c, handle)
        if str(property_read(entity, 'ObjectName')) == 'AcDb2LineAngularDimension':
            ray1 = (dxf['ext1'], dxf['ext2'])
            ray2 = (dxf['chord_point'], dxf['def_point'])
            direct = _dimension_ray_matches(ray1, vertex, first) and _dimension_ray_matches(ray2, vertex, second)
            swapped = _dimension_ray_matches(ray1, vertex, second) and _dimension_ray_matches(ray2, vertex, first)
            if not (direct or swapped):
                raise OutcomeUnknown(f'DXF 13/14 and 15/10 rays mismatch: expected vertex {vertex!r} with '
                                     f'endpoints {first!r} and {second!r}, got {ray1!r} and {ray2!r}')
        else:
            _verify_plan_readback('DXF 13', dxf['ext1'], first, 0.5)
            _verify_plan_readback('DXF 14', dxf['ext2'], second, 0.5)
            _verify_plan_readback('DXF 15', dxf['chord_point'], vertex, 0.5)
        _verify_edit_number('DXF 42', dxf['measurement'], expected, 1e-3)
        _verify_edit_number('DXF 42 vs COM Measurement', dxf['measurement'], evidence['measurement'], 1e-3)
        evidence.update(vertex=vertex, first_endpoint=first, second_endpoint=second, text_position=text_position)
        return _primitive_result(c, handle, entity, 'dim_angular', evidence, dxf, '10_13_14_15_42',
                                 dxf_type='DIMENSION')


def create_dim_diameter(c, args, before):
    chord, far_chord = args['chord_point'], args['far_chord_point']
    leader_length = args['leader_length']
    expected = math.dist(chord[:2], far_chord[:2])
    with _verify_mutation():
        handle, entity = _add_dimension(c, before, args, 'Diametric', vector(chord), vector(far_chord),
                                        leader_length)
        evidence = _dimension_properties(entity, args, expected)
        dxf = dim_dxf(c, handle)
        def_point, chord_point = dxf['def_point'], dxf['chord_point']
        direct_order = (_plan_readback_matches(def_point, far_chord, 0.5)
                        and _plan_readback_matches(chord_point, chord, 0.5))
        reversed_order = (_plan_readback_matches(def_point, chord, 0.5)
                          and _plan_readback_matches(chord_point, far_chord, 0.5))
        if not (direct_order or reversed_order):
            raise OutcomeUnknown(f'DXF 10/15 chord points mismatch: expected {chord!r} and {far_chord!r}, '
                                 f'got {def_point!r} and {chord_point!r}')
        _verify_edit_number('DXF 42', dxf['measurement'], expected, 0.5)
        evidence.update(chord_point=chord, far_chord_point=far_chord, leader_length=leader_length)
        return _primitive_result(c, handle, entity, 'dim_diameter', evidence, dxf, '10_15_42',
                                 dxf_type='DIMENSION')


def create_ellipse(c, args, before):
    center, major_axis, ratio = args['center'], args['major_axis'], args['radius_ratio']
    expected_major = math.hypot(*major_axis[:2])
    expected_minor = expected_major * ratio
    with _verify_mutation():
        handle, entity = _add_primitive(c, before, 'Ellipse', vector(center), vector(major_axis), ratio)
        observed_center = list(property_read(entity, 'Center'))
        major_radius = float(property_read(entity, 'MajorRadius'))
        minor_radius = float(property_read(entity, 'MinorRadius'))
        observed_ratio = float(property_read(entity, 'RadiusRatio'))
        _verify_plan_readback('COM Center', observed_center, center, 0.01)
        _verify_edit_number('COM MajorRadius', major_radius, expected_major, 0.01)
        _verify_edit_number('COM MinorRadius', minor_radius, expected_minor, 0.01)
        _verify_edit_number('COM RadiusRatio', observed_ratio, ratio, 1e-4)
        dxf = ellipse_dxf(c, handle)
        _verify_plan_readback('DXF 10', dxf['center'], center, 0.5)
        if not _plan_readback_matches(dxf['major_axis'], major_axis, 0.5):
            _verify_plan_readback('DXF 11 (either major-axis direction)', dxf['major_axis'],
                                  [-component for component in major_axis], 0.5)
        _verify_edit_number('DXF 40', dxf['radius_ratio'], ratio, 1e-3)
        evidence = {'center': observed_center, 'major_axis': major_axis, 'major_radius': major_radius,
                    'minor_radius': minor_radius, 'radius_ratio': observed_ratio}
        return _primitive_result(c, handle, entity, 'ellipse', evidence, dxf, '10_11_40')


def create_hatch(c, args, before):
    boundary_handle, boundary = _validate_move_target(
        c, {'document_id': args['document_id'], 'handle': args['boundary_handle']}, before)
    if str(property_read(boundary, 'ObjectName')) != 'AcDbPolyline':
        raise ValueError('hatch.create requires a closed AcDbPolyline boundary')
    if not bool(property_read(boundary, 'Closed')):
        raise ValueError('hatch.create boundary polyline must be closed')
    boundary_area = float(property_read(boundary, 'Area'))
    if not math.isfinite(boundary_area) or boundary_area <= 1e-3:
        raise ValueError('hatch.create boundary polyline must have positive area')

    pattern_name = args['pattern_name']
    solid = pattern_name == 'SOLID'
    scale = args.get('pattern_scale')
    angle_deg = args.get('pattern_angle_deg')
    angle_rad = math.radians(angle_deg) if angle_deg is not None else None
    with _verify_mutation():
        model_space = retry_document_read(lambda: c.doc.ModelSpace)
        created = late(invoke_document(lambda: model_space, 'AddHatch', 0, pattern_name, False))
        if not solid:
            if scale is not None:
                property_write(created, 'PatternScale', scale)
            if angle_rad is not None:
                property_write(created, 'PatternAngle', angle_rad)
        invoke_document(lambda: created, 'AppendOuterLoop', _dispatch_array([boundary]))
        invoke_document(lambda: created, 'Evaluate')
        invoke_document(lambda: created, 'Update')

        handle = retry_document_read(lambda: str(created.Handle)).upper()
        after = c.entities()
        if set(after) - set(before) != {handle} or set(before) - set(after):
            raise OutcomeUnknown('Unexpected ModelSpace handle changes during hatch.create')
        entity = late(after[handle])
        if str(property_read(entity, 'ObjectName')) != 'AcDbHatch':
            raise OutcomeUnknown(f'COM type mismatch for hatch {handle}')
        actual_pattern = str(property_read(entity, 'PatternName')).upper()
        if actual_pattern != pattern_name:
            raise OutcomeUnknown(f'COM PatternName mismatch for hatch {handle}')
        if bool(property_read(entity, 'AssociativeHatch')):
            raise OutcomeUnknown(f'COM AssociativeHatch mismatch for hatch {handle}')
        if int(property_read(entity, 'NumberOfLoops')) != 1:
            raise OutcomeUnknown(f'COM NumberOfLoops mismatch for hatch {handle}')
        hatch_area = float(property_read(entity, 'Area'))
        _verify_edit_number('COM Area', hatch_area, boundary_area, max(1.0, 0.005 * boundary_area))

        dxf = hatch_dxf(c, handle)
        if dxf['type'] != 'HATCH':
            raise OutcomeUnknown(f'DXF type mismatch for hatch {handle}')
        if (dxf['pattern_name'] or '').upper() != pattern_name:
            raise OutcomeUnknown(f'DXF 2 pattern name mismatch for hatch {handle}')
        if dxf['solid_fill'] != int(solid):
            raise OutcomeUnknown(f'DXF 70 solid fill mismatch for hatch {handle}')
        if dxf['associative'] != 0:
            raise OutcomeUnknown(f'DXF 71 associativity mismatch for hatch {handle}')
        if not solid:
            if scale is not None:
                _verify_edit_number('DXF 41', dxf['pattern_scale'], scale, max(1e-3, 0.005 * scale))
            if angle_rad is not None:
                _verify_rotation('DXF 52', dxf['pattern_angle_rad'], angle_rad, 1e-3)
        return {
            'success': True, 'document_id': c.document_id,
            'handles': [handle], 'entities': [c.describe(entity)],
            'evidence': {'hatch': {
                'boundary_handle': boundary_handle, 'boundary_area': boundary_area,
                'pattern_name': actual_pattern, 'associative': False, 'number_of_loops': 1,
                'area': hatch_area, 'pattern_scale': scale, 'pattern_angle_deg': angle_deg, 'dxf': dxf,
            }},
            'validation': {'type': True, 'properties': True, 'dxf': 'verified_2_70_71_41_52',
                           'area': 'verified_within_0_5pct', 'reopen': 'pending'},
        }


def _find_block(blocks, name):
    target = name.upper()
    for block in retry_document_read(lambda: collection_items(blocks)):
        block = late(block)
        actual_name = str(property_read(block, 'Name'))
        if actual_name.upper() == target:
            return block, actual_name
    return None, None


def _add_block_item(block, item):
    kind = item['kind']
    if kind == 'line':
        invoke_document(lambda: block, 'AddLine', vector(item['start']), vector(item['end']))
    elif kind == 'circle':
        invoke_document(lambda: block, 'AddCircle', vector(item['center']), item['radius'])
    elif kind == 'arc':
        invoke_document(lambda: block, 'AddArc', vector(item['center']), item['radius'],
                        math.radians(item['start_angle_deg']), math.radians(item['end_angle_deg']))
    elif kind == 'polyline':
        xy = [value for point in item['points'] for value in point[:2]]
        polyline = late(invoke_document(lambda: block, 'AddLightWeightPolyline', vector(xy)))
        if item['closed']:
            property_write(polyline, 'Closed', True)
    elif kind == 'text':
        invoke_document(lambda: block, 'AddText', item['text'], vector(item['point']), item['height'])


def define_block(c, args, before):
    name = args['name']
    blocks = retry_document_read(lambda: late(c.doc.Blocks))
    existing, _ = _find_block(blocks, name)
    if existing is not None:
        raise ValueError(f'Block already exists: {name}')
    with _verify_mutation():
        block = late(invoke_document(lambda: blocks, 'Add', vector(args['base_point']), name))
        for item in args['items']:
            _add_block_item(block, item)
        if set(c.entities()) != set(before):
            raise OutcomeUnknown('ModelSpace handles changed during block.define')
        found, actual_name = _find_block(blocks, name)
        if found is None:
            raise OutcomeUnknown(f'COM block definition missing after creation: {name}')
        count = int(property_read(found, 'Count'))
        if count != len(args['items']):
            raise OutcomeUnknown(f'COM block Count readback mismatch for {name}: {count}')
        origin = list(property_read(found, 'Origin'))
        _verify_plan_readback('COM block Origin', origin, args['base_point'], 0.01)
        return {'success': True, 'document_id': c.document_id, 'handles': [], 'entities': [],
                'evidence': {'block': {'name': actual_name, 'base_point': origin, 'item_count': count,
                                       'kinds': [item['kind'] for item in args['items']]}},
                'validation': {'type': True, 'properties': True, 'reopen': 'pending'}}


_BLOCK_SCALES = (('scale_x', 'XScaleFactor', 41), ('scale_y', 'YScaleFactor', 42),
                 ('scale_z', 'ZScaleFactor', 43))


def insert_block(c, args, before):
    blocks = retry_document_read(lambda: late(c.doc.Blocks))
    block, name = _find_block(blocks, args['name'])
    if block is None:
        raise ValueError(f'Block does not exist: {args["name"]}')
    rotation = math.radians(args['rotation_deg'])
    with _verify_mutation():
        model_space = retry_document_read(lambda: c.doc.ModelSpace)
        created = late(invoke_document(lambda: model_space, 'InsertBlock', vector(args['point']),
                                       name, args['scale_x'], args['scale_y'], args['scale_z'], rotation))
        handle = str(property_read(created, 'Handle')).upper()
        handle, entity = _verify_primitive_creation(c, before, handle, 'BlockReference')
        try:
            actual_name = str(property_read(entity, 'Name'))
        except AttributeError:
            actual_name = ''
        if actual_name.upper() != name.upper():
            actual_name = str(property_read(entity, 'EffectiveName'))
        if actual_name.upper() != name.upper():
            raise OutcomeUnknown(f'COM block reference name readback mismatch for {name}: {actual_name}')
        insertion = list(property_read(entity, 'InsertionPoint'))
        _verify_plan_readback('COM InsertionPoint', insertion, args['point'], 0.01)
        actual_rotation = float(property_read(entity, 'Rotation'))
        _verify_rotation('COM Rotation', actual_rotation, rotation, 1e-4)
        scales = {}
        for key, member, _ in _BLOCK_SCALES:
            scales[key] = float(property_read(entity, member))
            _verify_edit_number(f'COM {member}', scales[key], args[key], 1e-4)

        dxf = block_ref_dxf(c, handle)
        if dxf['type'] != 'INSERT':
            raise OutcomeUnknown(f'DXF type mismatch for block reference {handle}')
        if (dxf['name'] or '').upper() != name.upper():
            raise OutcomeUnknown(f'DXF 2 block name readback mismatch for {name}: {dxf["name"]}')
        _verify_plan_readback('DXF 10', dxf['insertion'], args['point'], 0.5)
        for key, _, group in _BLOCK_SCALES:
            _verify_edit_number(f'DXF {group}', dxf[key], args[key], 1e-3)
        _verify_rotation('DXF 50', dxf['rotation'], rotation, 1e-3)
        evidence = {'name': actual_name, 'insertion': insertion, **scales, 'rotation': actual_rotation}
        return _primitive_result(c, handle, entity, 'block_insert', evidence, dxf,
                                 '2_10_41_42_43_50', dxf_type='INSERT')


def delete_block(c, args, before):
    blocks = retry_document_read(lambda: late(c.doc.Blocks))
    block, actual_name = _find_block(blocks, args['name'])
    if block is None:
        raise ValueError(f'Block does not exist: {args["name"]}')
    if bool(property_read(block, 'IsLayout')) or bool(property_read(block, 'IsXRef')):
        raise ValueError(f'Cannot delete layout or xref block: {actual_name}')

    target = actual_name.upper()
    for entity in before.values():
        reference = late(entity)
        if str(property_read(reference, 'ObjectName')) != 'AcDbBlockReference':
            continue
        if str(property_read(reference, 'Name')).upper() == target:
            raise ValueError(f'Cannot delete block referenced in ModelSpace: {actual_name}')
        try:
            effective_name = str(property_read(reference, 'EffectiveName'))
        except AttributeError:
            if property_lookup(reference, 'EffectiveName') in ('present', 'rejected'):
                raise
            continue
        if effective_name.upper() == target:
            raise ValueError(f'Cannot delete block referenced in ModelSpace: {actual_name}')

    with _verify_mutation():
        invoke_document(lambda: block, 'Delete')
        if _find_block(blocks, actual_name)[0] is not None:
            raise OutcomeUnknown(f'Block still exists after deletion: {actual_name}')
        if set(c.entities()) != set(before):
            raise OutcomeUnknown('ModelSpace handles changed during block.delete')
        return {'success': True, 'document_id': c.document_id, 'handles': [], 'entities': [],
                'evidence': {'block_delete': {'name': actual_name, 'deleted': True}},
                'validation': {'type': True, 'deleted': True, 'reopen': 'pending'}}


def list_blocks(c, args):
    c.guard()
    require_drawing_window(c.hwnd)
    require_model_space(c.doc)
    blocks = retry_document_read(lambda: late(c.doc.Blocks))
    prefix = args.get('prefix')
    prefix_key = prefix.upper() if prefix is not None else ''
    include_system = args['include_system']
    items = []
    for block in retry_document_read(lambda: collection_items(blocks)):
        block = late(block)
        name = str(property_read(block, 'Name'))
        is_layout = bool(property_read(block, 'IsLayout'))
        if not include_system and (is_layout or name.startswith('*')):
            continue
        if not name.upper().startswith(prefix_key):
            continue
        try:
            origin = property_read(block, 'Origin')
        except AttributeError:
            origin = None
        items.append({'name': name, 'is_layout': is_layout, 'is_xref': bool(property_read(block, 'IsXRef')),
                      'item_count': int(property_read(block, 'Count')),
                      'base_point': list(origin) if origin is not None else [0.0, 0.0, 0.0]})
    items.sort(key=lambda item: item['name'].upper())
    return {'success': True, 'document_id': c.document_id, 'handles': [], 'entities': [],
            'evidence': {'blocks': items, 'count': len(items), 'prefix': prefix,
                         'include_system': include_system},
            'validation': {'type': True, 'properties': True, 'reopen': 'not_applicable'}}


def _find_layer(layers, name):
    target = name.upper()
    for layer in retry_document_read(lambda: collection_items(layers)):
        actual_name = str(property_read(layer, 'Name'))
        if actual_name.upper() == target:
            return layer, actual_name
    return None, None


def create_layer(c, args, before):
    name = args['name']
    layers = retry_document_read(lambda: late(c.doc.Layers))
    layer, _ = _find_layer(layers, name)
    created = layer is None
    with _verify_mutation():
        if created:
            layer = late(invoke_document(lambda: layers, 'Add', name))
        if 'color' in args:
            property_write(layer, 'Color', args['color'])
        actual_name = str(property_read(layer, 'Name'))
        actual_color = int(property_read(layer, 'Color'))
        if actual_name.upper() != name.upper() or ('color' in args and actual_color != args['color']):
            raise OutcomeUnknown(f'COM name or color readback mismatch for layer {name}')
        if set(c.entities()) != set(before):
            raise OutcomeUnknown('Entity handles changed during layer.create')
        return {'success': True, 'document_id': c.document_id, 'handles': [], 'entities': [],
                'evidence': {'layer': {'name': actual_name, 'created': created, 'color': actual_color}},
                'validation': {'type': True, 'properties': True, 'reopen': 'pending'}}


def _active_layer_name(c):
    return str(property_read(late(retry_document_read(lambda: c.doc.ActiveLayer)), 'Name'))


def delete_layer(c, args, before):
    name = args['name']
    if name.upper() in ('0', 'DEFPOINTS'):
        raise ValueError(f'Cannot delete protected layer: {name}')
    layers = retry_document_read(lambda: late(c.doc.Layers))
    layer, actual_name = _find_layer(layers, name)
    if layer is None:
        raise ValueError(f'Layer does not exist: {name}')
    target = actual_name.upper()
    if target == _active_layer_name(c).upper():
        raise ValueError(f'Cannot delete active layer: {actual_name}')
    if any(str(property_read(late(entity), 'Layer')).upper() == target for entity in before.values()):
        raise ValueError(f'Cannot delete layer containing ModelSpace entities: {actual_name}')

    with _verify_mutation():
        invoke_document(lambda: late(layer), 'Delete')
        if _find_layer(layers, actual_name)[0] is not None:
            raise OutcomeUnknown(f'Layer still exists after deletion: {actual_name}')
        if set(c.entities()) != set(before):
            raise OutcomeUnknown('ModelSpace handles changed during layer.delete')
        return {'success': True, 'document_id': c.document_id, 'handles': [], 'entities': [],
                'evidence': {'layer_delete': {'name': actual_name, 'deleted': True}},
                'validation': {'type': True, 'deleted': True, 'reopen': 'pending'}}


def set_active_layer(c, args, before):
    layers = retry_document_read(lambda: late(c.doc.Layers))
    layer, actual_name = _find_layer(layers, args['name'])
    if layer is None:
        raise ValueError(f'Layer does not exist: {args["name"]}')
    if bool(property_read(late(layer), 'Freeze')):
        raise ValueError(f'Cannot set frozen layer as active: {actual_name}')
    before_active = _active_layer_name(c)
    with _verify_mutation():
        invoke_document(lambda: c.doc, 'SetVariable', 'CLAYER', actual_name)
        after_active = _active_layer_name(c)
        after_clayer = str(retry_document_read(lambda: c.doc.GetVariable('CLAYER')))
        if after_active.upper() != actual_name.upper() or after_clayer.upper() != actual_name.upper():
            raise OutcomeUnknown(f'ActiveLayer or CLAYER readback mismatch for {actual_name}')
        if set(c.entities()) != set(before):
            raise OutcomeUnknown('Entity handles changed during layer.set_active')
        return {'success': True, 'document_id': c.document_id, 'handles': [], 'entities': [],
                'evidence': {'active_layer': {'before': before_active, 'requested': actual_name,
                                             'after': after_active, 'clayer': after_clayer}},
                'validation': {'type': True, 'properties': True, 'reopen': 'pending'}}


def list_layers(c, args):
    c.guard()
    require_drawing_window(c.hwnd)
    require_model_space(c.doc)
    layers = retry_document_read(lambda: late(c.doc.Layers))
    try:
        active_name = _active_layer_name(c)
    except Exception:
        active_name = None
    active_key = active_name.upper() if active_name is not None else None
    prefix = args.get('prefix')
    prefix_key = prefix.upper() if prefix is not None else ''
    rows = []
    for item in retry_document_read(lambda: collection_items(layers)):
        item = late(item)
        name = str(property_read(item, 'Name'))
        if name.upper().startswith(prefix_key):
            rows.append({
                'name': name, 'color': int(property_read(item, 'Color')),
                'layer_on': bool(property_read(item, 'LayerOn')),
                'freeze': bool(property_read(item, 'Freeze')),
                'lock': bool(property_read(item, 'Lock')),
                'linetype': str(property_read(item, 'Linetype')), 'active': name.upper() == active_key,
            })
    rows.sort(key=lambda row: (row['name'].upper(), row['name']))
    return {'success': True, 'document_id': c.document_id, 'handles': [], 'entities': [],
            'evidence': {'layers': {'count': len(rows), 'active_layer': active_name,
                                    'prefix': prefix, 'items': rows}},
            'validation': {'type': True, 'properties': True, 'reopen': 'not_applicable'}}


_LAYER_EDIT_PROPERTIES = (('color', 'Color'), ('layer_on', 'LayerOn'),
                          ('freeze', 'Freeze'), ('lock', 'Lock'))


def edit_layer(c, args, before):
    layers = retry_document_read(lambda: late(c.doc.Layers))
    layer, actual_name = _find_layer(layers, args['name'])
    if layer is None:
        raise ValueError(f'Layer does not exist: {args["name"]}')
    hiding = args.get('freeze') is True or args.get('layer_on') is False
    if hiding and actual_name.upper() in ('0', 'DEFPOINTS'):
        raise ValueError(f'Refusing to freeze or turn off protected layer: {actual_name}')
    active_name = _active_layer_name(c)
    if hiding and actual_name.upper() == active_name.upper():
        raise ValueError(f'Refusing to freeze or turn off active layer: {actual_name}')

    layer = late(layer)
    before_props = {name: property_read(layer, name) for _, name in _LAYER_EDIT_PROPERTIES}
    wanted = {name: args[key] for key, name in _LAYER_EDIT_PROPERTIES if key in args}
    expected = {**before_props, **wanted}
    with _verify_mutation():
        for name, value in wanted.items():
            property_write(layer, name, value)
        after_props = {name: property_read(layer, name) for name in before_props}
        for name, value in expected.items():
            if after_props[name] != value:
                raise OutcomeUnknown(f'COM {name} readback mismatch for layer {actual_name}')
        if set(c.entities()) != set(before):
            raise OutcomeUnknown('Entity handles changed during layer.edit')
        return {'success': True, 'document_id': c.document_id, 'handles': [], 'entities': [],
                'evidence': {'layer_edit': {'name': actual_name, 'properties_before': before_props,
                                           'properties_requested': wanted, 'properties_after': after_props}},
                'validation': {'type': True, 'properties': True, 'reopen': 'pending'}}


def set_entity_layer(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    layers = retry_document_read(lambda: late(c.doc.Layers))
    _, canonical_layer = _find_layer(layers, args['layer'])
    if canonical_layer is None:
        raise ValueError(f'Layer does not exist: {args["layer"]}')
    before_layer = str(property_read(entity, 'Layer'))
    before_type = str(property_read(entity, 'ObjectName'))
    with _verify_mutation():
        property_write(entity, 'Layer', canonical_layer)
        invoke_document(lambda: entity, 'Update')
        updated = _after_edit(c, 'entity.set_layer', handle, before, before_type)
        after_layer = str(property_read(updated, 'Layer'))
        if after_layer.upper() != canonical_layer.upper():
            raise OutcomeUnknown('COM Layer readback mismatch')
        dxf = entity_layer_dxf(c, handle)
        if (dxf['layer'] or '').upper() != canonical_layer.upper():
            raise OutcomeUnknown('DXF 8 layer readback mismatch')
        return _part_edit_result(c, handle, updated, 'set_layer', {
            'object_name': before_type, 'layer_before': before_layer, 'layer_requested': canonical_layer,
            'layer_after': after_layer, 'dxf_after': dxf,
        }, dxf='verified_8')


def set_entity_color(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    before_type = str(property_read(entity, 'ObjectName'))
    before_color = int(property_read(entity, 'Color'))
    color = args['color']
    with _verify_mutation():
        property_write(entity, 'Color', color)
        invoke_document(lambda: entity, 'Update')
        updated = _after_edit(c, 'entity.set_color', handle, before, before_type)
        after_color = int(property_read(updated, 'Color'))
        if after_color != color:
            raise OutcomeUnknown('COM Color readback mismatch')
        dxf = entity_color_dxf(c, handle)
        dxf_color = 256 if dxf['color'] is None else dxf['color']
        if dxf['type'] is None or dxf_color != color:
            raise OutcomeUnknown('DXF 62 color readback mismatch')
        return _part_edit_result(c, handle, updated, 'set_color', {
            'object_name': before_type, 'color_before': before_color, 'color_requested': color,
            'color_after': after_color, 'dxf_after': dxf,
        }, dxf='verified_62')


def query_entities(c, args):
    c.guard()
    require_drawing_window(c.hwnd)
    require_model_space(c.doc)
    object_name = args.get('object_name')
    layer = args['layer'].upper() if 'layer' in args else None
    bbox = args.get('bbox')
    limit = args['limit']
    items = []
    total_matched = 0
    for _, entity in sorted(c.entities().items()):
        ent = late(entity)
        if object_name is not None and str(property_read(ent, 'ObjectName')) != object_name:
            continue
        if layer is not None and str(property_read(ent, 'Layer')).upper() != layer:
            continue
        info = None
        if bbox is not None:
            info = c.describe(ent)
            if 'bounds' not in info:
                continue
            lo, hi = info['bounds']
            box_lo, box_hi = bbox
            if hi[0] < box_lo[0] or lo[0] > box_hi[0] or hi[1] < box_lo[1] or lo[1] > box_hi[1]:
                continue
        total_matched += 1
        if len(items) >= limit:
            continue
        info = dict(c.describe(ent) if info is None else info)
        if 'layer' not in info:
            info['layer'] = str(property_read(ent, 'Layer'))
        if 'color' not in info:
            info['color'] = int(property_read(ent, 'Color'))
        items.append(info)
    filters = {key: value for key, value in args.items() if key != 'limit'}
    return {'success': True, 'document_id': c.document_id,
            'handles': [item['handle'] for item in items], 'entities': items,
            'evidence': {'query': {'filters': filters, 'total_matched': total_matched,
                                   'returned': len(items), 'truncated': total_matched > limit}},
            'validation': {'type': True, 'properties': True, 'reopen': 'not_applicable'}}


def edit_text(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    if str(entity.ObjectName) != 'AcDbText':
        raise ValueError('annotation.text_edit requires a native AcDbText')
    before_props = _edit_properties(entity, ('TextString', 'Height', 'Rotation'))
    before_dxf = text_dxf(c, handle)
    if before_dxf['type'] != 'TEXT':
        raise OutcomeUnknown('Text DXF type could not be verified before edit')
    wanted = {name: args[key] for key, name in (('text', 'TextString'), ('height', 'Height'))
              if key in args}
    if 'rotation_deg' in args:
        wanted['Rotation'] = math.radians(args['rotation_deg'])
    expected = {**before_props, **wanted}
    rotation_rad = expected['Rotation']

    with _verify_mutation():
        updated, after_props = _apply_com_edit(c, 'annotation.text_edit', handle, entity, before,
                                              'AcDbText', wanted, before_props)
        if after_props['TextString'] != expected['TextString']:
            raise OutcomeUnknown('COM TextString readback mismatch')
        _verify_edit_number('COM Height', after_props['Height'], expected['Height'], 0.01)
        _verify_rotation('COM', after_props['Rotation'], rotation_rad, 1e-4)
        after_dxf = text_dxf(c, handle)
        if after_dxf['type'] != 'TEXT' or after_dxf['text'] != expected['TextString']:
            raise OutcomeUnknown('Text DXF type or text readback mismatch')
        _verify_edit_number('DXF 40', after_dxf['height'], expected['Height'], 0.5)
        _verify_rotation('DXF 50', after_dxf['rotation'], rotation_rad, 1e-3)
        return _part_edit_result(c, handle, updated, 'edit', {
            'properties_before': before_props, 'properties_requested': wanted, 'properties_after': after_props,
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
            'rotation_deg': args.get('rotation_deg', math.degrees(rotation_rad)), 'rotation_rad': rotation_rad,
        }, dxf='verified_1_40_50')


def edit_roof(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    if str(entity.ObjectName) != 'TDbSlopeRoof':
        raise ValueError('roof.edit requires a native slope roof')
    before_props = _edit_properties(entity, ('Elevation', 'SlopeAngle'))
    before_dxf = roof_dxf(c, handle)
    before_slopes = before_dxf['edge_slopes_rad']
    if before_dxf['type'] != 'TCH_SLOPEROOF' or not before_slopes or not all(map(math.isfinite, before_slopes)):
        raise OutcomeUnknown('Roof DXF type or edge slopes could not be verified before edit')
    wanted = {name: args[key] for key, name in (('elevation', 'Elevation'), ('slope_angle', 'SlopeAngle'))
              if key in args}
    expected = {**before_props, **wanted}

    with _verify_mutation():
        updated, after_props = _apply_com_edit(c, 'roof.edit', handle, entity, before,
                                              'TDbSlopeRoof', wanted, before_props)
        _verify_edit_number('COM Elevation', after_props['Elevation'], expected['Elevation'], 0.5)
        _verify_edit_number('COM SlopeAngle', after_props['SlopeAngle'], expected['SlopeAngle'], 0.05)
        after_dxf = roof_dxf(c, handle)
        after_slopes = after_dxf['edge_slopes_rad']
        if (after_dxf['type'] != 'TCH_SLOPEROOF'
                or len(after_dxf['vertices']) != len(before_dxf['vertices'])
                or len(after_slopes) != len(before_slopes)):
            raise OutcomeUnknown('Roof DXF type, vertex count or edge slope count changed during edit')
        _verify_edit_number('DXF 148', after_dxf['elevation'], expected['Elevation'], 0.5)
        _verify_edit_number('DXF 51[0]', after_slopes[0], math.radians(expected['SlopeAngle']), 0.002)
        for i, slope in enumerate(before_slopes[1:], start=1):
            _verify_edit_number(f'DXF 51[{i}]', after_slopes[i], slope, 0.002)
        return _part_edit_result(c, handle, updated, 'edit', {
            'properties_before': before_props, 'properties_requested': wanted, 'properties_after': after_props,
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
            'slope_angle_scope': 'primary_edge_dxf_51_0',
        }, dxf='verified_10_51_148')


def _require_edit_column_dxf(dxf, stage):
    _require_column_dxf(dxf, stage)
    bulges = dxf.get('bulges') or ()
    if len(bulges) != 4 or not all(map(math.isfinite, bulges)):
        raise OutcomeUnknown(f'Column DXF bulges (code 50) invalid {stage}')


def edit_column(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    if not is_rectangular_column(entity):
        raise ValueError('column.edit requires a rectangular native column')
    properties = (('width', 'Width'), ('depth', 'Deep'), ('height', 'Height'))
    before_props = _edit_properties(entity, (name for _, name in properties))
    before_dxf = column_dxf(c, handle)
    _require_edit_column_dxf(before_dxf, 'before edit')
    wanted = {name: args[key] for key, name in properties if key in args}
    expected = {**before_props, **wanted}

    with _verify_mutation():
        updated, after_props = _apply_com_edit(c, 'column.edit', handle, entity, before,
                                              'TDbColumn', wanted, before_props)
        if not is_rectangular_column(updated):
            raise OutcomeUnknown('Column section shape changed during edit')
        for name, target in expected.items():
            _verify_edit_number(f'COM {name}', after_props[name], target, 0.01)
        after_dxf = column_dxf(c, handle)
        _require_edit_column_dxf(after_dxf, 'after edit')
        _verify_edit_number('DXF 149', after_dxf['height'], expected['Height'], 0.5)
        _verify_edit_number('Column DXF insertion shift',
                            math.dist(after_dxf['insertion'], before_dxf['insertion']), 0, 0.5)
        if after_dxf['flags'] != before_dxf['flags']:
            raise OutcomeUnknown('Column DXF flags changed during edit')
        for actual, original in zip(after_dxf['bulges'], before_dxf['bulges']):
            _verify_edit_number('Column DXF bulge', actual, original, 1e-6)
        vertices = after_dxf['vertices']
        d0, d1, d2, d3 = (math.dist(a, b) for a, b in zip(vertices, vertices[1:] + vertices[:1]))
        _verify_edit_number('Column DXF opposite side length', d0, d2, 0.5)
        _verify_edit_number('Column DXF opposite side length', d1, d3, 0.5)
        for actual, target in zip(sorted((d0, d1)), sorted((expected['Width'], expected['Deep']))):
            _verify_edit_number('Column DXF side length', actual, target, 0.5)
        return _part_edit_result(c, handle, updated, 'edit', {
            'properties_before': before_props, 'properties_requested': wanted, 'properties_after': after_props,
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
        }, dxf='verified_10_11_50_70_71_72_149')


def edit_slab(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    if str(entity.ObjectName) != 'TDbSlab':
        raise ValueError('slab.edit requires a native slab')
    before_props = _edit_properties(entity, ('Thickness', 'Elevation'))
    before_dxf = slab_dxf(c, handle)
    if before_dxf['type'] != 'TCH_SLAB':
        raise OutcomeUnknown('Slab DXF type could not be verified before edit')
    before_bounds = bounds(c, entity)
    wanted = {}
    if 'thickness' in args:
        sign = -1.0 if before_props['Thickness'] <= 0 else 1.0
        wanted['Thickness'] = sign * args['thickness']
    if 'elevation' in args:
        wanted['Elevation'] = args['elevation']
    expected = {**before_props, **wanted}
    thickness = expected['Thickness']
    elevation = expected['Elevation']

    with _verify_mutation():
        updated, after_props = _apply_com_edit(c, 'slab.edit', handle, entity, before,
                                              'TDbSlab', wanted, before_props)
        _verify_edit_number('COM Thickness', after_props['Thickness'], thickness, 0.01)
        _verify_edit_number('COM Elevation', after_props['Elevation'], elevation, 0.01)
        after_dxf = slab_dxf(c, handle)
        if after_dxf['type'] != 'TCH_SLAB':
            raise OutcomeUnknown('Slab DXF type changed during edit')
        _verify_edit_number('DXF 149', after_dxf['thickness'], thickness, 0.5)
        _verify_edit_number('DXF 148', after_dxf['elevation'], elevation, 0.5)
        lo, hi = bounds(c, updated)
        _verify_edit_number('Slab bounds Z-span', hi[2] - lo[2], abs(thickness), 0.5)
        _verify_edit_number('Slab bounds reference Z face', hi[2] if thickness < 0 else lo[2], elevation, 0.5)
        return _part_edit_result(c, handle, updated, 'edit', {
            'properties_before': before_props, 'properties_requested': wanted, 'properties_after': after_props,
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
            'bounds_before': before_bounds, 'bounds_after': (lo, hi),
        }, dxf='verified_148_149', bounds='verified_z_span_and_reference_face')


def _opening_mirror_dxf(dxf, inside_outside):
    insertion = dxf['insertion']
    if (dxf['type'] != 'TCH_OPENING' or insertion is None or not all(map(math.isfinite, insertion))
            or dxf['mirror_flag'] is None):
        raise OutcomeUnknown('Opening DXF insertion or mirror flag could not be verified')
    swing = dxf['swing_point']
    if inside_outside and (swing is None or not all(map(math.isfinite, swing))):
        raise OutcomeUnknown('Opening DXF swing point could not be verified')


def mirror_opening(c, args, before):
    handle, entity = _validate_move_target(c, args, before)
    if str(entity.ObjectName) != 'TDbOpening':
        raise ValueError('opening.mirror requires a native opening')
    host_handle, _ = _unique_wall_host(c, handle, invalid_host=ValueError)
    inside_outside = args['direction'] == 'inside_outside'
    before_props = _edit_properties(entity, ('Width', 'Height', 'WinSill'))
    before_dxf = opening_dxf(c, handle)
    _opening_mirror_dxf(before_dxf, inside_outside)
    holes_before = hole_areas(c)
    if host_handle not in holes_before:
        raise ValueError('Opening host is not a native wall in ModelSpace')
    if not all(map(math.isfinite, holes_before.values())):
        raise OutcomeUnknown('Wall HoleArea is not finite before mirror')

    command = 'TMirWinIO' if inside_outside else 'TMirWinLR'
    with _verify_mutation():
        c.run_command(f'(progn (command "{command}" (handent "{handle}") "") (princ))\n')
        updated = _after_edit(c, 'opening.mirror', handle, before, 'TDbOpening')
        after_props = _edit_properties(updated, before_props)
        for name, expected in before_props.items():
            _verify_edit_number(f'COM {name}', after_props[name], expected, 0.01)
        after_dxf = opening_dxf(c, handle)
        _opening_mirror_dxf(after_dxf, inside_outside)
        _verify_edit_number('Opening DXF insertion shift',
                            math.dist(before_dxf['insertion'], after_dxf['insertion']), 0, 0.5)
        if after_dxf['mirror_flag'] == before_dxf['mirror_flag']:
            raise OutcomeUnknown('Opening DXF mirror flag did not change')
        if inside_outside and not math.dist(before_dxf['swing_point'], after_dxf['swing_point']) > 1.0:
            raise OutcomeUnknown('Opening DXF swing point did not change')
        if _unique_wall_host(c, handle)[0] != host_handle:
            raise OutcomeUnknown('Opening host changed during mirror')
        holes_after = _wait_hole_area_match(c, host_handle, holes_before, 0.0)
        return _part_edit_result(c, handle, updated, 'mirror', {
            'direction': args['direction'], 'command': command, 'host_handle': host_handle,
            'properties_before': before_props, 'properties_after': after_props,
            'dxf_before': before_dxf, 'dxf_after': after_dxf,
            'hole_areas_before': holes_before, 'hole_areas_after': holes_after,
        }, dxf='verified_70_11' if inside_outside else 'verified_70',
            host='verified_dxf_330_and_hole_areas')


def delete_entity(c, args, before):
    """Delete a verified owned entity via pure COM Delete(), checking host updates."""
    handle, target = _validate_move_target(c, args)
    obj_name = str(target.ObjectName)
    if obj_name not in DELETABLE_OBJECT_NAMES:
        raise ValueError(f'entity.delete does not support ObjectName {obj_name}')
    host_handle = None
    holes_before = None
    expected_drop = None
    if obj_name == 'TDbWall':
        wall_hole = _split_area(c.describe(target), 'HoleArea')
        if wall_hole is None:
            raise OutcomeUnknown('Wall HoleArea could not be read before delete')
        if wall_hole > 0.002:
            raise ValueError('Cannot delete wall with openings; delete hosted openings first')
    elif obj_name == 'TDbOpening':
        host_handle, _ = _unique_wall_host(c, handle)
        holes_before = hole_areas(c)
        try:
            expected_drop = float(target.Width) * float(target.Height) / 1_000_000.0
        except Exception as exc:
            raise OutcomeUnknown('Opening dimensions could not be read before delete') from exc
    try:
        target.Delete()
        after = c.entities()
        removed = set(before) - set(after)
        added = set(after) - set(before)
        if removed != {handle} or added:
            raise OutcomeUnknown(f'Unexpected handle diff during entity.delete: removed={sorted(removed)}, added={sorted(added)}')
        evidence = {'delete': {'handle': handle, 'object_name': obj_name, 'removed_handles': [handle]}}
        validation = {'type': True, 'deleted': True, 'reopen': 'pending'}
        if obj_name == 'TDbOpening':
            holes_after = _wait_hole_area_match(c, host_handle, holes_before, -expected_drop)
            evidence['delete']['host_handle'] = host_handle
            evidence['delete']['host_hole_area_before'] = holes_before[host_handle]
            evidence['delete']['host_hole_area_after'] = holes_after[host_handle]
            validation['host'] = 'verified_host_hole_area_decrement'
        else:
            validation['host'] = 'not_applicable'
        if hasattr(c, 'created_walls'):
            c.created_walls.pop(handle, None)
        return {
            'success': True,
            'document_id': c.document_id,
            'handles': [],
            'entities': [],
            'evidence': evidence,
            'validation': validation,
        }
    except OutcomeUnknown:
        raise
    except Exception as exc:
        raise OutcomeUnknown(str(exc)) from exc

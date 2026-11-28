import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
from .store import Store, validate
from .worker import state_dir, start, run, status, stop


def store():
    return Store(state_dir() / 'queue.sqlite3')


# Validation status for AutoCAD 2024 with Tianzheng T20 V9.
VALIDATION = {
    'rectangle.create': 'verified_live',
    'polyline.edit': 'verified_live',
    'entity.set_color': 'verified_live',
    'layer.set_active': 'verified_live',
    'entity.query': 'verified_live',
    'line.create': 'verified_live',
    'circle.create': 'verified_live',
    'arc.create': 'verified_live',
    'hatch.create': 'verified_live',
    'layer.create': 'verified_live',
    'layer.delete': 'verified_live',
    'layer.list': 'verified_live',
    'layer.edit': 'verified_live',
    'entity.set_layer': 'verified_live',
    'axis.rect_create': 'verified_live',
    'axis.from_walls': 'verified_live',
    'axis.single_create': 'verified_live',
    'axis.annotate': 'verified_live',
    'document.new': 'verified_live',
    'document.save': 'verified_live',
    'document.reopen': 'verified_live',
    'document.close': 'verified_live',
    'document.readback': 'verified_live',
    'document.list': 'verified_live',
    'wall.create': 'verified_live',
    'opening.create': 'verified_live',
    'opening.move': 'verified_live',
    'opening.edit': 'verified_live',
    'opening.group_create': 'verified_live',
    'opening.mirror': 'verified_live',
    'annotation.text_edit': 'verified_live',
    'roof.edit': 'verified_live',
    'column.edit': 'verified_live',
    'slab.edit': 'verified_live',
    'slab.create': 'verified_live',
    'annotation.text': 'verified_live',
    'annotation.mtext': 'verified_live',
    'dim.aligned': 'verified_live',
    'dim.linear': 'verified_live',
    'dim.radial': 'verified_live',
    'dim.angular': 'verified_live',
    'dim.t20_angular': 'verified_live',
    'dim.diameter': 'verified_live',
    'ellipse.create': 'verified_live',
    'block.delete': 'verified_live',
    'block.define': 'verified_live',
    'block.insert': 'verified_live',
    'block.list': 'verified_live',
    'polyline.create': 'verified_live',
    'wall.split': 'verified_live',
    'wall.edit': 'verified_live',
    'column.create': 'verified_live',
    'column.move': 'verified_live',
    'column.rotate': 'verified_live',
    'entity.copy': 'verified_live',
    'entity.move': 'verified_live',
    'entity.rotate': 'verified_live',
    'entity.scale': 'verified_live',
    'entity.mirror': 'verified_live',
    'entity.delete': 'verified_live',
    'entity.get': 'verified_live',
    'entity.verify': 'verified_live',
    'wall.find': 'verified_live',
    'opening.relabel': 'verified_live',
    'space.create': 'verified_live',
    'space.edit': 'verified_live',
    'dim.wall': 'verified_live',
    'wall.fix_corner': 'verified_live',
    'stair.create': 'unverified',
    'stair.rect_create': 'verified_live',
    'roof.create': 'verified_live',
}


def capabilities():
    from .commands import SCHEMAS
    fallback = 'historical_evidence; current adapter needs live verification'
    return {op: {'required': sorted(req), 'optional': sorted(opt),
                 'validation': VALIDATION.get(op, fallback)}
            for op, (req, opt) in SCHEMAS.items()}


ACK_DIR = 'acks'


def _ack_path(job_id):
    return state_dir() / ACK_DIR / (job_id + '.json')


def acknowledgement(job_id):
    """Return the operator's recorded observation for an acknowledged job."""
    path = _ack_path(str(job_id))
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def acknowledge(job_id, side_effects):
    """Close an unknown_outcome job after a human inspected the drawing.

    "Unknown" is not "nothing happened": the timeout or crash that produced the
    job may have left walls, a column or a half-driven palette behind. So the
    caller has to say what it actually found, and that sentence is stored twice
    -- in the job row and as a receipt under the state directory -- before the
    job is closed. A settled job is never rewritten.

    The OS lock proves no abandoned COM worker is still running. Only an
    uncertain job may be acknowledged, and it is never replayed.
    """
    note = side_effects.strip() if isinstance(side_effects, str) else ''
    if not note:
        raise ValueError('side_effects must describe what the drawing actually shows')
    if len(note) > 4000:
        raise ValueError('side_effects must be at most 4000 characters')
    from .worker import WorkerLock
    with WorkerLock():
        s = store()
        row = s.get(job_id)
        if row['state'] != 'unknown_outcome':
            raise ValueError('Only unknown_outcome can be acknowledged')
        receipt = {'job_id': str(job_id), 'state_before': row['state'],
                   'error_before': row['error'], 'side_effects': note,
                   'recorded_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        path = _ack_path(str(job_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding='utf-8')
        s.finish(job_id, 'failed', row['context'], row['results'],
                 'Operator inspected outcome; original job will not be replayed. '
                 'Observed side effects: ' + note)
    return {'acknowledged': True, 'replayed': False, 'side_effects': note,
            'receipt': str(path)}


def doctor():
    out = {'cad_executable': Path(os.environ.get('TZ_CAD_EXE', 'C:/Program Files/Autodesk/AutoCAD 2024/acad.exe')).exists(),
           'tianzheng_root': (Path(os.environ.get('TZ_TIANZHENG_ROOT', 'C:/Tianzheng')) / 'SYS').exists(), 'progid': 'AutoCAD.Application.24.3'}
    try:
        from .cad import Cad
        pinned = os.environ.get('TZ_CAD_PID')
        out['session'] = Cad(int(pinned) if pinned else None).diagnose()
    except Exception as exc:
        out['connection_error'] = str(exc)
    return out


def main():
    parser = argparse.ArgumentParser(description='Local Tianzheng queue CLI/MCP')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('doctor', 'capabilities', 'serve'):
        sub.add_parser(name)
    for name in ('preview', 'submit'):
        p = sub.add_parser(name)
        p.add_argument('recipe')
    for name in ('status', 'result', 'cancel', 'acknowledge'):
        p = sub.add_parser(name)
        p.add_argument('job_id')
        if name == 'acknowledge':
            p.add_argument('--inspected', action='store_true', required=True)
            p.add_argument('--side-effects', required=True,
                           help='what the drawing actually shows after the uncertain job')
    p = sub.add_parser('worker')
    p.add_argument('action', choices=['start', 'run', 'once', 'status', 'stop'])
    args = parser.parse_args()
    try:
        if args.cmd == 'serve':
            from .server import serve
            serve()
            return
        if args.cmd == 'doctor':
            out = doctor()
        elif args.cmd == 'capabilities':
            out = capabilities()
        elif args.cmd in ('preview', 'submit'):
            recipe = json.loads(Path(args.recipe).read_text(encoding='utf-8-sig'))
            validate(recipe)
            out = {'valid': True, 'recipe': recipe} if args.cmd == 'preview' else {'job_id': store().submit(recipe)}
        elif args.cmd in ('status', 'result'):
            out = store().get(args.job_id)
        elif args.cmd == 'cancel':
            store().cancel(args.job_id)
            out = {'cancel_requested': True}
        elif args.cmd == 'acknowledge':
            out = acknowledge(args.job_id, args.side_effects)
        elif args.action in ('run', 'once'):
            run(store(), once=args.action == 'once')
            out = status()
        elif args.action == 'start':
            out = start()
        elif args.action == 'status':
            out = status()
        else:
            out = stop()
        print(json.dumps(out, ensure_ascii=True, indent=2))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(1)

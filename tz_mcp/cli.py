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


# Measured on T20 V9/ACAD 2024; see docs/LIVE-PROBE-20261118.md and
# docs/STATUS-20261119.md before changing any label here.
VALIDATION = {
    'document.new': 'verified_live: Documents.Add + SaveAs, count checked',
    'document.close': 'verified_live (2026-11-20, AutoCAD 2024 + T20, same worker new/save/close): '
                      'explicit Close(False) of the job-owned, active, saved drawing only; ownership '
                      'is the original Add/Open proxy, verified by COM IUnknown identity so a '
                      'same-path reopened drawing is refused; unsaved/foreign/identity-unreadable '
                      'refusals and COM timeout/unknown outcomes are offline-tested only; see '
                      'outputs/v11-readback-live-71a413c5ef/evidence.json',
    'document.readback': 'verified_live (2026-11-20, AutoCAD 2024 + T20, saved DWG under '
                         'D:/CAD绘图/tianzheng-mcp/outputs): Documents.Open(path, True), '
                         'ModelSpace handle readback of polyline 34C and TGWALL 35A, then '
                         'Close(False) of only that verified document; hashes unchanged, both '
                         'readback documents closed, pre-opened target refused and left open; '
                         'COM timeout/unknown-outcome boundaries are offline-tested only; see '
                         'outputs/v11-readback-live-71a413c5ef/evidence.json',
    'document.list': 'verified_live (2026-11-20, AutoCAD 2024 + T20, saved DWG under '
                     'D:/CAD绘图/tianzheng-mcp/outputs, path-only start): paged ModelSpace Handle '
                     'discovery sharing the read-only Open(path, True)/verify/Close(False) path of '
                     'document.readback; three limit=3 pages returned 7 entities in Handle order -- '
                     '4 TDbWall, 1 rectangular TDbColumn, 1 ordinary door and 1 ordinary window -- '
                     'with door/window GetKind/GetSubKind read as stored; all 7 Handles were then '
                     'read back with matching properties, the file sha256 was unchanged and no '
                     'document was left open; negative/refusal and COM timeout/unknown-outcome '
                     'paths are offline-tested only; host fields and cross-page drawing-change '
                     'detection are not verified; see '
                     'outputs/v11-discovery-live-cc80957f9306/evidence.json',
    'wall.create': 'verified_live: TGWall panel + canvas clicks, segment identified by geometry',
    'opening.create': 'verified_live (2026-11-19 door handle 3C8 and 2026-11-20 door+window in one '
                      'job: door 3CF 900x2100 host 35A hole 0.00 -> 1.89, window 409 1200x1600 '
                      'sill 600 hole 1.89 -> 3.81, both GetKind/GetSubKind as requested and both '
                      'present after save+reopen): TGDOOR or TGWINDOW palette, three size rows '
                      'typed by order, then one canvas click; kind/width/height read back from COM '
                      'and DXF. See docs/OPENING-WINDOW-20261120.md',
    'opening.move': 'verified_live (2026-11-24, real stdio MCP queue_submit + worker, AutoCAD 2024 '
                    '+ T20): a door moved along its host wall in plan; DXF insertion, width, height '
                    'and rotation plus the host HoleArea read back consistently; cross-axis, '
                    'off-wall-end and overlap refusals and short-delta protection are '
                    'offline-tested only; read back twice after save+close with an unchanged file '
                    'hash',
    'slab.create': 'verified_live: TSlab consumes boundary/hole polylines, thickness read back',
    'annotation.text': 'verified_live: ordinary AcDbText',
    'polyline.create': 'verified_live: ordinary AcDbPolyline',
    'wall.split': 'verified_live (2026-11-24, real stdio MCP queue_submit + worker, AutoCAD 2024 + '
                  'T20): TKATANA via the T20 screen menu, both host cases through one recipe shape '
                  '-- the door stays on the original wall or migrates to the new segment, seam and '
                  'object counts verified, paged document.list and two document.readback passes '
                  'after save+close with unchanged hash and no document left open; '
                  'timeout/focus-loss paths remain offline-tested only',
    'column.create': 'verified_live (2026-11-19 end-to-end through MCP + worker, handle 35B): '
                     'TGColumn panel edited field by field then one canvas click; '
                     'width/depth/height/insertion read back from DXF; OSMODE forced to 0 for the '
                     'click. Seed CopyObjects route (job 144ad30f) removed. Live runs drove the UI '
                     'directly because COM only reaches the first AutoCAD instance',
    'column.move': 'verified_live (2026-11-24, real stdio MCP queue_submit + worker, AutoCAD 2024 '
                   '+ T20): a standalone rectangular column moved in plan; DXF insertion and all '
                   'four vertices shifted by the same delta, height and flags unchanged; '
                   'non-rectangular/foreign handle and short-delta refusals are offline-tested '
                   'only; read back twice after save+close with an unchanged file hash',
    'stair.create': 'unverified: TLStair payload is historical evidence only',
    'stair.rect_create': 'verified_live (2026-11-24, real stdio MCP queue_submit + worker, AutoCAD '
                         '2024 + T20): TDbRectStair created from a plan point with height, step '
                         'count and width read back after save+close+reopen; the wizard builds a '
                         'two-flight stair, so GetBoundingBox spans both flights and is not the '
                         'requested single-flight width. Flight width is only honoured up to the '
                         '1230 mm wizard default: 600/900/1200/1230 passed, 1300/1500/3000 stayed '
                         'at 1230. A requested/measured mismatch now fails the job as '
                         'unknown_outcome naming the built handle instead of passing the '
                         'properties',
    'roof.create': 'unverified: TSlopeRoof boundary consumption not live-verified',
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
    out = {'cad_executable': Path('D:/CAD/AutoCAD 2024/acad.exe').exists(),
           'tianzheng_root': Path('D:/天正/SYS').exists(), 'progid': 'AutoCAD.Application.24.3'}
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

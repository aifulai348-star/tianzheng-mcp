import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .context import references
from .commands import prepare, SCHEMAS

OPS = set(SCHEMAS)


def validate(recipe):
    if not isinstance(recipe, dict) or not isinstance(recipe.get('steps'), list) or not recipe['steps']:
        raise ValueError('Non-empty steps required')
    if len(recipe['steps']) > 200:
        raise ValueError('Maximum 200 steps')
    if set(recipe) - {'steps', 'operation_id', 'on_error'}:
        raise ValueError('Unknown recipe fields; document creation must be an explicit step')
    if recipe.get('on_error', 'stop') != 'stop':
        raise ValueError('Only on_error=stop is supported')
    names, ids = set(), set()
    for step in recipe['steps']:
        if not isinstance(step, dict) or step.get('op') not in OPS:
            raise ValueError('Unsupported operation')
        if set(step) - {'id', 'op', 'args', 'bind', 'timeout_s'}:
            raise ValueError('Unknown step fields')
        if not isinstance(step.get('args', {}), dict):
            raise ValueError('args must be an object')
        sid = step.get('id')
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', sid) or sid in ids:
            raise ValueError('Unique identifier required for every step')
        ids.add(sid)
        timeout = step.get('timeout_s', 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError('timeout_s must be finite, >0 and <=300')
        args = step.get('args', {})
        required, optional = SCHEMAS[step['op']]
        if not required <= args.keys() or args.keys() - required - optional:
            raise ValueError(f'Invalid arguments for {step["op"]}')
        refs = list(references(args))
        if not refs:
            prepare(step['op'], args)
        for ref in refs:
            if ref.split('.')[0] not in names:
                raise ValueError(f'Forward or missing reference: {ref}')
        bind = step.get('bind')
        if bind:
            if not isinstance(bind, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', bind) or bind in names:
                raise ValueError('Unique context binding required')
            names.add(bind)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, token TEXT UNIQUE, digest TEXT, recipe TEXT, state TEXT, context TEXT, results TEXT, error TEXT, cancel INTEGER DEFAULT 0, updated REAL)')
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()
    def submit(self, recipe, operation_id=None):
        validate(recipe)
        payload = json.dumps(recipe, sort_keys=True, ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        token = operation_id or recipe.get('operation_id') or str(uuid.uuid4())
        if not isinstance(token, str) or not token:
            raise ValueError('operation_id must be non-empty text')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id,digest FROM jobs WHERE token=?', (token,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('operation_id reused with different input')
                return old['id']
            job = str(uuid.uuid4())
            db.execute('INSERT INTO jobs(id,token,digest,recipe,state,context,results,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (job, token, digest, payload, 'queued', '{}', '[]', time.time()))
            return job
    def get(self, job):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None:
            raise ValueError('Unknown job')
        out = dict(row)
        for key in ('recipe', 'context', 'results'):
            out[key] = json.loads(out[key])
        return out
    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), row['id']))
        return self.get(row['id'])
    def finish(self, job, state, context, results, error=None):
        with self.connect() as db:
            db.execute('UPDATE jobs SET state=?,context=?,results=?,error=?,updated=? WHERE id=?',
                       (state, json.dumps(context), json.dumps(results), error, time.time(), job))
    def cancel(self, job):
        self.get(job)
        with self.connect() as db:
            db.execute("UPDATE jobs SET cancel=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END WHERE id=?", (job,))
    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='unknown_outcome',error='Worker interrupted; do not replay automatically' WHERE state='running'")
    def has_uncertainty(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM jobs WHERE state='unknown_outcome' LIMIT 1").fetchone() is not None

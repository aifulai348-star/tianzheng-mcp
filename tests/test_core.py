import tempfile
import time
import unittest
from pathlib import Path

from tz_mcp.store import Store
from tz_mcp.context import resolve
from tz_mcp.engine import Engine


def recipe(op='wall.create', args=None):
    default = {'start': [0,0], 'end': [4000,0]} if op == 'wall.create' else {'handle': 'A1'}
    return {'steps': [{'id': 'a', 'op': op, 'args': default if args is None else args, 'bind': 'wall'}]}


class Recovery:
    def __init__(self, modal=False):
        self.is_modal = modal
        self.escapes = 0
    def modal(self):
        return self.is_modal
    def escape(self):
        self.escapes += 1
        return {'posted': 1}


class Adapter:
    def __init__(self, delay=0):
        self.delay = delay
        self.inputs = []
    def execute(self, op, args):
        self.inputs.append(args)
        time.sleep(self.delay)
        return {'handles': ['A1'], 'object_name': 'TDbWall', 'document_id': 'drawing1'}


class CoreTests(unittest.TestCase):
    def test_internal_completion_timeout_persists_unknown_then_escapes(self):
        class Timeout(Exception):
            uncertain = True
            command_completion_timeout = True
        class Broken(Adapter):
            def execute(self, op, args):
                self.inputs.append(args)
                raise Timeout('completion not confirmed')
        job=self.s.submit(recipe());self.s.submit(recipe(),'next')
        recovery,adapter=Recovery(),Broken()
        original=recovery.escape
        def inspect():
            self.assertEqual('unknown_outcome',self.s.get(job)['state'])
            return original()
        recovery.escape=inspect
        engine=Engine(self.s,adapter,recovery)
        engine.run_one()
        self.assertGreater(recovery.escapes,0)
        self.assertFalse(engine.run_one())
        self.assertEqual(1,len(adapter.inputs))
        self.assertNotIn('wall',self.s.get(job)['context'])
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'queue.sqlite3'
        self.s = Store(self.path)
    def tearDown(self):
        self.tmp.cleanup()
    def test_fifo_and_idempotency(self):
        a = self.s.submit(recipe(), 'same')
        self.assertEqual(a, self.s.submit(recipe(), 'same'))
        b = self.s.submit(recipe(), 'other')
        self.assertEqual(a, self.s.claim()['id'])
        self.assertEqual(b, self.s.claim()['id'])
        with self.assertRaises(ValueError):
            self.s.submit(recipe('entity.get'), 'same')
    def test_reference_preserves_values_and_rejects_missing(self):
        self.assertEqual('A1', resolve({'$ref': 'wall.handles.0'}, {'wall': {'handles': ['A1']}}))
        with self.assertRaises(ValueError):
            resolve({'$ref': 'wall.handles.3'}, {'wall': {'handles': ['A1']}})
    def test_context_is_durable_and_passed_to_next_step(self):
        r = recipe()
        r['steps'].append({'id': 'b', 'op': 'entity.get', 'args': {'handle': {'$ref': 'wall.handles.0'}}})
        job = self.s.submit(r)
        adapter = Adapter()
        Engine(self.s, adapter, Recovery()).run_one()
        self.assertEqual({'handle': 'A1'}, adapter.inputs[1])
        result = Store(self.path).get(job)
        self.assertEqual('succeeded', result['state'])
        self.assertEqual(['A1'], result['context']['wall']['handles'])
    def test_timeout_posts_escape_and_stops_chain(self):
        r = recipe()
        r['steps'][0]['timeout_s'] = 0.03
        r['steps'].append({'id': 'b', 'op': 'entity.get', 'args': {'handle': 'A1'}})
        job = self.s.submit(r)
        recovery, adapter = Recovery(), Adapter(0.12)
        Engine(self.s, adapter, recovery, poll=0.005).run_one()
        result = self.s.get(job)
        self.assertEqual('unknown_outcome', result['state'])
        self.assertEqual(1, len(adapter.inputs))
        self.assertGreater(recovery.escapes, 0)
        self.assertNotIn('wall', result['context'])
    def test_modal_aborts_and_posts_escape(self):
        job = self.s.submit(recipe())
        recovery = Recovery(True)
        Engine(self.s, Adapter(0.08), recovery, poll=0.005).run_one()
        self.assertEqual('unknown_outcome', self.s.get(job)['state'])
        self.assertGreater(recovery.escapes, 0)
    def test_normal_step_does_not_escape(self):
        self.s.submit(recipe())
        recovery = Recovery()
        Engine(self.s, Adapter(), recovery).run_one()
        self.assertEqual(0, recovery.escapes)
    def test_restart_does_not_replay_running_job(self):
        job = self.s.submit(recipe())
        self.s.claim()
        self.s.recover_interrupted()
        self.assertEqual('unknown_outcome', self.s.get(job)['state'])
        self.assertIsNone(self.s.claim())
    def test_forward_reference_rejected_on_submit(self):
        with self.assertRaises(ValueError):
            self.s.submit(recipe(args={'handle': {'$ref': 'future.handles.0'}}))
    def test_invalid_arguments_rejected_before_queue(self):
        with self.assertRaises(ValueError):
            self.s.submit(recipe(args={'start': [0,0], 'end': [0,0]}))
    def test_cancel_queued(self):
        job = self.s.submit(recipe())
        self.s.cancel(job)
        self.assertIsNone(self.s.claim())
    def test_uncertain_adapter_failure_stops_later_jobs(self):
        class Uncertain(Exception):
            uncertain = True
        class Broken(Adapter):
            def execute(self, op, args):
                raise Uncertain('CAD may already have changed')
        a = self.s.submit(recipe())
        b = self.s.submit(recipe())
        engine = Engine(self.s, Broken(), Recovery())
        engine.run_one()
        self.assertEqual('unknown_outcome', self.s.get(a)['state'])
        self.assertFalse(engine.run_one())
        self.assertEqual('queued', self.s.get(b)['state'])


if __name__ == '__main__':
    unittest.main()

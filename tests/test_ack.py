import json
import os
import tempfile
import unittest
from pathlib import Path

from tz_mcp import cli
from tz_mcp.store import Store

RECIPE = {'operation_id': 'ack-test', 'steps': [
    {'id': 'doc', 'op': 'document.new', 'args': {'path': 'D:/work/ack.dwg'}}]}


class AcknowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get('TZ_STATE')
        os.environ['TZ_STATE'] = self.tmp.name
        self.store = Store(Path(self.tmp.name) / 'queue.sqlite3')
        self.job = self.store.submit(RECIPE)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop('TZ_STATE', None)
        else:
            os.environ['TZ_STATE'] = self.previous
        self.tmp.cleanup()

    def uncertain(self):
        self.store.finish(self.job, 'unknown_outcome', {'doc': {}}, [],
                          "{'reason': 'timeout'}")

    def test_side_effects_are_required(self):
        # An uncertain job may well have drawn something; a bare "ack" would
        # let the next operator assume the drawing is untouched.
        self.uncertain()
        for value in ('', '   ', None):
            with self.assertRaises(ValueError) as caught:
                cli.acknowledge(self.job, value)
            self.assertIn('side_effects', str(caught.exception))
        self.assertEqual('unknown_outcome', self.store.get(self.job)['state'])

    def test_acknowledge_records_what_the_drawing_shows(self):
        self.uncertain()
        note = 'One 650x450 column 3EB left at (19800,7203); no wall drawn.'
        out = cli.acknowledge(self.job, '  ' + note + '  ')
        self.assertTrue(out['acknowledged'])
        self.assertFalse(out['replayed'])
        self.assertEqual(note, out['side_effects'])

        row = self.store.get(self.job)
        self.assertEqual('failed', row['state'])
        self.assertIn('will not be replayed', row['error'])
        self.assertIn('Observed side effects: ' + note, row['error'])

        receipt = Path(out['receipt'])
        self.assertEqual(Path(self.tmp.name) / 'acks' / (self.job + '.json'), receipt)
        saved = json.loads(receipt.read_text(encoding='utf-8'))
        self.assertEqual(note, saved['side_effects'])
        self.assertEqual('unknown_outcome', saved['state_before'])
        self.assertEqual("{'reason': 'timeout'}", saved['error_before'])
        self.assertEqual(saved, cli.acknowledgement(self.job))

    def test_settled_jobs_cannot_be_acknowledged(self):
        self.store.finish(self.job, 'succeeded', {}, [], None)
        with self.assertRaises(ValueError) as caught:
            cli.acknowledge(self.job, 'nothing happened')
        self.assertIn('Only unknown_outcome', str(caught.exception))
        self.assertEqual('succeeded', self.store.get(self.job)['state'])

    def test_acknowledgement_is_absent_until_recorded(self):
        self.uncertain()
        self.assertIsNone(cli.acknowledgement(self.job))


if __name__ == '__main__':
    unittest.main()

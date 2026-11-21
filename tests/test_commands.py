import unittest
from tz_mcp.commands import prepare, command_payload


class CommandTests(unittest.TestCase):
    def test_wall_rejects_zero_length_and_nonfinite_points(self):
        for end in ([0, 0], [float('nan'), 2], ['(quit)', 3]):
            with self.assertRaises(ValueError):
                prepare('wall.create', {'start': [0, 0], 'end': end})
    def test_opening_rejects_unmeasured_kind_and_mixed_pair(self):
        host = {'handle': '35A', 'document_id': 'doc'}
        for kind, subkind in (('推拉窗', '推拉窗'), ('普通门', '普通窗'), ('普通窗', '普通门')):
            with self.assertRaises(ValueError):
                prepare('opening.create', {'host': host, 'point': [0, 0], 'kind': kind,
                                           'subkind': subkind, 'width': 900, 'height': 2100})
        args = prepare('opening.create', {'host': host, 'point': [0, 0], 'kind': '普通窗',
                                          'subkind': '普通窗', 'width': 900, 'height': 2100})
        self.assertEqual('普通窗', args['kind'])
    def test_opening_requires_host_entity_reference(self):
        with self.assertRaises(ValueError):
            prepare('opening.create', {'point': [1000, 0], 'kind': 0})
    def test_numeric_only_command_payload(self):
        payload = command_payload('wall.create', prepare('wall.create', {'start': [0, 0], 'end': [4000, 0]}))
        self.assertIn('TGWall', payload)
        self.assertIn('4000.0,0.0,0.0', payload)
        self.assertTrue(payload.endswith('\n'))
    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            prepare('wall.create', {'start': [0, 0], 'end': [4000, 0], 'lisp': '(quit)'})
    def test_wall_uses_native_command_and_two_empty_inputs(self):
        payload = command_payload('wall.create', prepare('wall.create', {'start': [0, 0], 'end': [4000, 0]}))
        self.assertEqual(['TGWall', '0.0,0.0,0.0', '4000.0,0.0,0.0', '', '', ''], payload.split('\n'))
    def test_roof_binds_exact_boundary_without_nested_command(self):
        args=prepare('roof.create',{'boundary':[[0,0],[4000,0],[4000,3000],[0,3000]],'angle':30,'overhang':600})
        self.assertEqual('TSlopeRoof\n(handent "35A")\n30\n600\n',command_payload('roof.create',args,boundary_handle='35A'))
        for h in (None,'(entdel (entlast))'):
            with self.assertRaises(ValueError):
                command_payload('roof.create',args,boundary_handle=h)


if __name__ == '__main__':
    unittest.main()

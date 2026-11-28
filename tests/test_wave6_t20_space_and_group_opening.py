import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tz_mcp import cli, panel, parts
from tz_mcp.cad import Cad, OutcomeUnknown, retry_document_read
from tz_mcp.commands import command_payload, prepare
from tz_mcp.engine import NO_ESCAPE_STEPS


REF = {'document_id': 'doc', 'handle': '3CF'}
GROUP = {'document_id': 'doc', 'handles': ['409', '3CF'], 'label': '组合01'}
GROUP_COMMAND = 'TGroupOpening\n(handent "409")\n(handent "3CF")\n\n组合01\n'
GROUP_DXF = {'type': 'TCH_OPENING', 'handle': '40A', 'layer': 'WINDOW',
             'label': '组合01', 'width': 2100.0, 'height': 2200.0,
             'kind_code': 8, 'text_height': 3.5}


class SchemaTests(unittest.TestCase):
    def test_capability_schema_and_recovery(self):
        capabilities = cli.capabilities()
        for op, required in (
            ('space.edit', ['document_id', 'handle', 'name']),
            ('opening.group_create', ['document_id', 'handles', 'label']),
        ):
            with self.subTest(op=op):
                self.assertEqual(required, capabilities[op]['required'])
                self.assertEqual([], capabilities[op]['optional'])
                self.assertTrue(capabilities[op]['validation'].startswith('verified_live'))
        self.assertIn('space.edit', NO_ESCAPE_STEPS)
        self.assertNotIn('opening.group_create', NO_ESCAPE_STEPS)
        self.assertEqual(['height', 'sill', 'text_height', 'width'],
                         capabilities['opening.edit']['optional'])

    def test_space_name_and_reference_validation(self):
        self.assertEqual({**REF, 'name': '客 厅'}, prepare(
            'space.edit', {'document_id': ' doc ', 'handle': ' 3cf ', 'name': ' 客 厅 '}))
        self.assertEqual('字' * 64, prepare('space.edit', {**REF, 'name': '字' * 64})['name'])
        for value in ('', ' ', '字' * 65, '厅\n', '\t厅', '客\x00厅', '客\x7f厅', 3):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare('space.edit', {**REF, 'name': value})
        for reference in ({'document_id': ' '}, {'handle': '0x3CF'}, {'handle': 3}):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                prepare('space.edit', {**REF, 'name': '客厅', **reference})

    def test_group_normalization_and_exact_payload(self):
        args = prepare('opening.group_create', {'document_id': ' doc ', 'handles': ['409', '3cf'],
                                                'label': ' 组合01 '})
        self.assertEqual(GROUP, args)
        self.assertEqual(GROUP_COMMAND, command_payload('opening.group_create', args))
        handles = [f'{i:X}' for i in range(1, 11)]
        label = '组' * 64
        self.assertEqual({**GROUP, 'handles': handles, 'label': label}, prepare(
            'opening.group_create', {**GROUP, 'handles': handles, 'label': label}))

    def test_group_rejects_invalid_selections_or_labels(self):
        for handles in ([], ['3CF'], ['3CF'] * 11, ('409', '3CF'), ['409', '0x3CF'],
                        ['409', 3], ['3cf', '3CF'], ['03cf', '3CF'], ['409 ', '3CF']):
            with self.subTest(handles=handles), self.assertRaises(ValueError):
                prepare('opening.group_create', {**GROUP, 'handles': handles})
        for label in ('', '组' * 65, '组 合', '组\u3000合', '组\n合', '\t组', '组\x00',
                      '组\x7f', '"组"', False):
            with self.subTest(label=label), self.assertRaises(ValueError):
                prepare('opening.group_create', {**GROUP, 'label': label})
        with self.assertRaises(ValueError):
            prepare('opening.group_create', {**GROUP, 'document_id': ''})

    def test_text_height_must_be_positive_finite_number(self):
        args = prepare('opening.edit', {**REF, 'text_height': 5})
        self.assertEqual({**REF, 'text_height': 5.0}, args)
        self.assertIs(type(args['text_height']), float)
        for value in (True, 0, -1, float('nan'), float('inf'), '5', None, 10**400):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare('opening.edit', {**REF, 'text_height': value})


class FakeCad:
    document_id = 'doc'
    hwnd = 42

    def __init__(self, *entities):
        self.store = {entity.Handle: entity for entity in entities}
        self.osmode = 33
        self.doc = SimpleNamespace(GetVariable=self.get_variable, SetVariable=self.set_variable)
        self.commands = []
        self.on_group = lambda: None

    def guard(self):
        pass

    def entities(self):
        return self.store.copy()

    def entity(self, handle, document_id):
        return self.store[handle]

    def describe(self, entity):
        return {'handle': entity.Handle, 'object_name': entity.ObjectName}

    def get_variable(self, name):
        assert name == 'OSMODE'
        return self.osmode

    def set_variable(self, name, value):
        assert name == 'OSMODE'
        self.osmode = value

    def run_command(self, command):
        self.commands.append(command)
        if command == '(setvar "OSMODE" 0)\n':
            self.osmode = 0
        elif command == GROUP_COMMAND:
            assert self.osmode == 0
            self.on_group()
        else:
            raise AssertionError(command)


def entity(handle, object_name, **properties):
    return SimpleNamespace(Handle=handle, ObjectName=object_name, Update=Mock(), **properties)


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(parts, 'require_drawing_window'))
        self.enterContext(patch.object(parts, 'require_model_space'))
        self.enterContext(patch.object(parts, 'late', side_effect=lambda obj: obj))

    def setup_space(self, object_name='TDbSpace'):
        space = entity('3CF', object_name, Name='房间')
        cad = FakeCad(space)
        dxf = {'type': 'TCH_SPACE', 'name': '房间', 'number': '101',
               'area_m2': 28.91, 'perimeter_mm': 21600.0}
        return cad, space, dxf

    def test_space_edit_returns_evidence_and_accepts_geometry_tolerance(self):
        cad, space, before = self.setup_space()
        after = {**before, 'name': '客厅', 'area_m2': 28.915, 'perimeter_mm': 21600.5}
        with patch.object(parts, 'space_dxf', side_effect=[before, after]):
            result = Cad.execute(cad, 'space.edit', {**REF, 'name': ' 客厅 '})
        space.Update.assert_called_once_with()
        self.assertEqual('客厅', space.Name)
        self.assertEqual(['3CF'], result['handles'])
        self.assertEqual('verified_1_2_41_42', result['validation']['dxf'])
        self.assertEqual({
            'handle': '3CF', 'name_before': '房间', 'name_requested': '客厅',
            'name_after': '客厅', 'area_m2': 28.915, 'perimeter_mm': 21600.5,
            'dxf_before': before, 'dxf_after': after,
        }, result['evidence']['space_edit'])
        self.assertEqual([], cad.commands)

    def test_space_edit_rejects_invalid_target_before_writing(self):
        for changes, object_name in (({'document_id': 'other'}, 'TDbSpace'),
                                     ({'handle': 'A1'}, 'TDbSpace'), ({}, 'AcDbText')):
            with self.subTest(changes=changes, object_name=object_name):
                cad, space, _ = self.setup_space(object_name)
                with patch.object(parts, 'space_dxf') as read, self.assertRaises(ValueError):
                    Cad.execute(cad, 'space.edit', {**REF, 'name': '客厅', **changes})
                read.assert_not_called()
                space.Update.assert_not_called()
                self.assertEqual('房间', space.Name)

    def test_space_edit_rejects_invalid_before_geometry(self):
        for field, value in (('type', 'TEXT'), ('area_m2', float('nan')), ('perimeter_mm', None)):
            with self.subTest(field=field):
                cad, space, before = self.setup_space()
                with patch.object(parts, 'space_dxf', return_value={**before, field: value}):
                    with self.assertRaises(OutcomeUnknown):
                        Cad.execute(cad, 'space.edit', {**REF, 'name': '客厅'})
                space.Update.assert_not_called()
                self.assertEqual('房间', space.Name)

    def test_space_edit_rejects_readback_mismatches(self):
        for field, value in (('type', 'TEXT'), ('name', '旧称'), ('number', '102'),
                             ('area_m2', 28.93), ('perimeter_mm', 21600.51)):
            with self.subTest(field=field):
                cad, space, before = self.setup_space()
                after = {**before, 'name': '客厅', field: value}
                with patch.object(parts, 'space_dxf', side_effect=[before, after]):
                    with self.assertRaises(OutcomeUnknown):
                        Cad.execute(cad, 'space.edit', {**REF, 'name': '客厅'})
                space.Update.assert_called_once_with()

        for change in ('name', 'handles', 'type'):
            with self.subTest(change=change):
                cad, space, before = self.setup_space()

                def change_target():
                    if change == 'name':
                        space.Name = '旧称'
                    elif change == 'handles':
                        cad.store['B0'] = entity('B0', 'AcDbText')
                    else:
                        space.ObjectName = 'AcDbText'

                space.Update.side_effect = change_target
                with patch.object(parts, 'space_dxf', return_value=before):
                    with self.assertRaises(OutcomeUnknown):
                        Cad.execute(cad, 'space.edit', {**REF, 'name': '客厅'})

    def setup_group(self):
        cad = FakeCad(entity('35A', 'TDbWall', HoleArea=3.81),
                      entity('35B', 'TDbWall', HoleArea=0.0),
                      entity('3CF', 'TDbOpening'), entity('409', 'TDbOpening'))
        group = entity('40A', 'TDbGroupOpening', Label=GROUP['label'], GetKind='组合门窗',
                       Width=2100.0, Height=2200.0)

        def replace_sources():
            for handle in GROUP['handles']:
                del cad.store[handle]
            cad.store[group.Handle] = group

        cad.on_group = replace_sources
        return cad, group

    def run_group(self, cad, *, args=GROUP, hosts=None, dxf=None):
        if hosts is None:
            hosts = {h: ['35A'] for h in ('3CF', '409', '40A')}

        def references(_cad, handle):
            return [{'handle': '1F', 'type': 'BLOCK_RECORD'},
                    *({'handle': wall, 'type': 'TCH_WALL'} for wall in hosts[handle])]

        with patch.object(parts, 'entity_wall_hosts', side_effect=references), \
             patch.object(parts, 'opening_group_dxf', return_value=GROUP_DXF if dxf is None else dxf):
            return Cad.execute(cad, 'opening.group_create', args)

    def test_group_replaces_sources_and_returns_host_and_outer_dxf(self):
        cad, _ = self.setup_group()
        result = self.run_group(cad)
        self.assertEqual(['(setvar "OSMODE" 0)\n', GROUP_COMMAND], cad.commands)
        self.assertEqual(33, cad.osmode)
        self.assertEqual({'35A', '35B', '40A'}, cad.store.keys())
        self.assertEqual(['40A'], result['handles'])
        self.assertEqual([{'handle': '40A', 'object_name': 'TDbGroupOpening'}], result['entities'])
        self.assertEqual({
            'command': 'TGroupOpening', 'source_handles': ['409', '3CF'],
            'removed_handles': ['3CF', '409'], 'host_handle': '35A', 'label': '组合01',
            'kind': '组合门窗', 'width': 2100.0, 'height': 2200.0, 'dxf': GROUP_DXF,
        }, result['evidence']['group_opening'])
        self.assertEqual({'type': True, 'properties': True, 'dxf': 'verified_302_40_41_71',
                          'host': 'verified_dxf_330', 'reopen': 'pending'}, result['validation'])

    def test_group_allows_absent_kind_code(self):
        cad, _ = self.setup_group()
        dxf = {key: value for key, value in GROUP_DXF.items() if key != 'kind_code'}
        self.assertTrue(self.run_group(cad, dxf=dxf)['success'])

    def test_group_accepts_repeated_references_to_the_same_host(self):
        for group_hosts in (['35A', '35A', '35A'], ['35a', '35A', '35a']):
            with self.subTest(group_hosts=group_hosts):
                cad, _ = self.setup_group()
                result = self.run_group(cad, hosts={'3CF': ['35A'], '409': ['35A'], '40A': group_hosts})
                self.assertTrue(result['success'])
                self.assertEqual('35A', result['evidence']['group_opening']['host_handle'])

    def test_group_write_guards(self):
        scenarios = [
            ({'document_id': 'other'}, None, None, 'document_id'),
            ({'handles': ['409', '404']}, None, None, 'not found'),
            ({}, 'AcDbText', None, 'native TDbOpening'),
            ({}, None, ['35B'], 'same host wall'),
            ({}, None, ['35A', '35B'], 'not a unique'),
            ({}, None, ['35A', '35A'], 'not a unique'),
            ({}, None, [], 'not a unique'),
        ]
        for overrides, object_name, second_host, error in scenarios:
            with self.subTest(error=error, second_host=second_host):
                cad, _ = self.setup_group()
                if object_name is not None:
                    cad.store['409'].ObjectName = object_name
                hosts = None if second_host is None else {'3CF': ['35A'], '409': second_host}
                with self.assertRaisesRegex(ValueError, error):
                    self.run_group(cad, args={**GROUP, **overrides}, hosts=hosts)
                self.assertEqual([], cad.commands)
                self.assertEqual(33, cad.osmode)

    def test_group_rejects_bad_replacement_and_restores_snaps(self):
        for status in ('missing removal', 'unexpected removal', 'extra addition', 'command error'):
            with self.subTest(status=status):
                cad, _ = self.setup_group()
                original_replace = cad.on_group
                source = cad.store['409']

                def replace():
                    original_replace()
                    if status == 'missing removal':
                        cad.store['409'] = source
                    elif status == 'unexpected removal':
                        del cad.store['35B']
                    elif status == 'extra addition':
                        cad.store['40B'] = entity('40B', 'AcDbText')
                    else:
                        raise RuntimeError('command interrupted')

                cad.on_group = replace
                with self.assertRaises(OutcomeUnknown):
                    self.run_group(cad)
                self.assertEqual(33, cad.osmode)

    def test_group_rejects_bad_com_dxf_and_post_write_host(self):
        for field, value in (('ObjectName', 'TDbOpening'), ('Label', '旧'), ('GetKind', ''),
                             ('Width', 0), ('Height', float('inf'))):
            with self.subTest(COM=field):
                cad, group = self.setup_group()
                setattr(group, field, value)
                with self.assertRaises(OutcomeUnknown):
                    self.run_group(cad)
        for field, value in (('type', 'TEXT'), ('label', ''), ('width', 2100.51),
                             ('height', None), ('kind_code', 1)):
            with self.subTest(DXF=field):
                cad, _ = self.setup_group()
                with self.assertRaises(OutcomeUnknown):
                    self.run_group(cad, dxf={**GROUP_DXF, field: value})
        for wrong_host, error in ((['35B'], 'host changed'), ([], 'not a unique'),
                                  (['35A', '35B'], 'not a unique'),
                                  (['35A', ''], 'could not be resolved')):
            with self.subTest(host=wrong_host):
                cad, _ = self.setup_group()
                with self.assertRaisesRegex(OutcomeUnknown, error):
                    self.run_group(cad, hosts={'3CF': ['35A'], '409': ['35A'], '40A': wrong_host})

    def setup_opening(self, **properties):
        opening = entity('3CF', 'TDbOpening', Width=900.0, Height=2100.0, WinSill=0.0, **properties)
        cad = FakeCad(entity('35A', 'TDbWall', HoleArea=1.89), opening)
        self.enterContext(patch.object(parts, 'entity_wall_hosts',
                                     return_value=[{'handle': '35A', 'type': 'TCH_WALL'}]))
        dxf = {'type': 'TCH_OPENING', 'width': 900.0, 'height': 2100.0, 'insertion': (3000.0, 0.0)}
        return cad, opening, dxf

    def test_opening_text_height_verifies_com_dxf_and_zero_hole_delta(self):
        cad, opening, dxf = self.setup_opening(TextHeight=3.5)

        def read(_cad, _handle, *, include_text_height):
            self.assertTrue(include_text_height)
            return {**dxf, 'text_height': opening.TextHeight}

        with patch.object(parts, 'opening_dxf', side_effect=read) as readback:
            result = Cad.execute(cad, 'opening.edit', {**REF, 'text_height': 5})
        opening.Update.assert_called_once_with()
        self.assertEqual(2, readback.call_count)
        evidence = result['evidence']['edit']
        self.assertEqual({'TextHeight': 5.0}, evidence['properties_requested'])
        self.assertEqual(3.5, evidence['properties_before']['TextHeight'])
        self.assertEqual(5.0, evidence['properties_after']['TextHeight'])
        self.assertEqual(3.5, evidence['dxf_before']['text_height'])
        self.assertEqual(5.0, evidence['dxf_after']['text_height'])
        self.assertEqual(0.0, evidence['expected_hole_area_delta'])
        self.assertEqual(evidence['host_hole_area_before'], evidence['host_hole_area_after'])

    def test_opening_legacy_edit_never_reads_text_height(self):
        cad, opening, dxf = self.setup_opening()

        def legacy_reader(_cad, _handle):
            return dxf

        with patch.object(parts, 'opening_dxf', side_effect=legacy_reader):
            result = Cad.execute(cad, 'opening.edit', {**REF, 'sill': 30})
        opening.Update.assert_called_once_with()
        self.assertFalse(hasattr(opening, 'TextHeight'))
        self.assertEqual({'WinSill': 30.0}, result['evidence']['edit']['properties_requested'])

    def test_opening_rejects_invalid_initial_com_text_height(self):
        for value in (0.0, -1.0, float('nan'), float('inf'), None):
            with self.subTest(value=value):
                cad, opening, _ = self.setup_opening(TextHeight=value)
                with patch.object(parts, 'opening_dxf') as read, self.assertRaises(OutcomeUnknown):
                    Cad.execute(cad, 'opening.edit', {**REF, 'text_height': 5})
                read.assert_not_called()
                opening.Update.assert_not_called()

    def test_opening_rejects_com_or_dxf_text_height_mismatch(self):
        for com, dxf_value in ((5.02, 5.0), (5.0, 4.94), (5.0, None)):
            with self.subTest(com=com, dxf=dxf_value):
                cad, opening, dxf = self.setup_opening(TextHeight=3.5)
                opening.Update.side_effect = lambda: setattr(opening, 'TextHeight', com)
                with patch.object(parts, 'opening_dxf', side_effect=[
                    {**dxf, 'text_height': 3.5}, {**dxf, 'text_height': dxf_value},
                ]):
                    with self.assertRaisesRegex(OutcomeUnknown, 'TextHeight|DXF 140'):
                        Cad.execute(cad, 'opening.edit', {**REF, 'text_height': 5})


class DxfTests(unittest.TestCase):
    def read_dxf(self, parser, data, **options):
        def run(lisp):
            self.assertIn('(handent "3CF")', lisp)
            match = re.search(r'\(open "([^"]+)" "w"', lisp)
            self.assertIsNotNone(match)
            Path(match[1]).write_bytes(data)

        with TemporaryDirectory() as folder:
            return parser(SimpleNamespace(run_command=run), '3CF', folder=folder, **options)

    def test_group_dxf_uses_first_of_every_repeated_opening_code(self):
        dump = ('0 TCH_OPENING\n5 40A\n8 WINDOW\n'
                '302 组合01\n40 2100\n41 2200\n71 8\n140 3.5\n'
                '302 \n40 900\n41 2100\n71 0\n140 4.0\n'
                '302 子窗\n40 1200\n41 1200\n71 1\n140 5.0\n')
        for encoding in ('gbk', 'utf-8-sig'):
            with self.subTest(encoding=encoding):
                self.assertEqual(GROUP_DXF, self.read_dxf(panel.opening_group_dxf, dump.encode(encoding)))

    def test_opening_dxf_text_height_is_opt_in(self):
        dump = b'((0 . TCH_OPENING) (40 . 900) (41 . 2100) (140 . 3.5))\nHOST 35A'
        self.assertEqual(3.5, self.read_dxf(panel.opening_dxf, dump, include_text_height=True)['text_height'])
        malformed = dump.replace(b'3.5', b'invalid')
        self.assertNotIn('text_height', self.read_dxf(panel.opening_dxf, malformed))
        missing = dump.replace(b' (140 . 3.5)', b'')
        self.assertIsNone(self.read_dxf(panel.opening_dxf, missing, include_text_height=True)['text_height'])

    def test_native_property_lookups_are_retryable(self):
        for name in ('Name', 'GetKind', 'TextHeight'):
            with self.subTest(name=name), patch('tz_mcp.cad.time.sleep'):
                read = Mock(side_effect=[AttributeError(f'<unknown>.{name}'), 'value'])
                self.assertEqual('value', retry_document_read(read))
                self.assertEqual(2, read.call_count)


if __name__ == '__main__':
    unittest.main()

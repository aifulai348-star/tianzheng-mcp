"""Offline coverage and fixtures for TSlopeRoof-style adapters."""
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tz_mcp.cad import Cad, CommandCompletionTimeout, CommandNotSent, OutcomeUnknown
from tz_mcp.panel import roof_dxf


BOUNDARY = [(10000.0, 20000.0), (16000.0, 20000.0), (16000.0, 24000.0), (10000.0, 24000.0)]
# Raw entget pairs from WP2, roof_30_600_entget.txt.
WP2_ENTGET = """(-1 . <图元名: 1389a2a8a50>)
(0 . "TCH_SLOPEROOF")
(330 . <图元名: 1386f6c31f0>)
(5 . "34D")
(100 . "AcDbEntity")
(67 . 0)
(410 . "Model")
(8 . "ROOF")
(100 . "TDbEntity")
(46 . 0.0)
(47 . 100.0)
(68 . 0)
(100 . "TDbPolyPathEnt")
(100 . "TDbSlopeRoof")
(148 . -346.41)
(10 10000.0 20000.0 0.0)
(51 . 0.523599)
(10 16000.0 20000.0 0.0)
(51 . 0.523599)
(10 16000.0 24000.0 0.0)
(51 . 0.523599)
(10 10000.0 24000.0 0.0)
(51 . 0.523599)
"""


class FakeRoofEntity:
    ObjectName = 'TDbSlopeRoof'
    Handle = '34D'

    def __init__(self, angle=29.999999999999996, elevation=-346.41016151377545):
        self.SlopeAngle = angle
        self.Elevation = elevation


class FakeCAD:
    execute = Cad.execute
    document_id = 'roof-offline'
    hwnd = 1
    boundary_handle = '34C'

    def __init__(self, roof=None, consume=True, entget=WP2_ENTGET):
        self.roof = FakeRoofEntity() if roof is None else roof
        self.consume = consume
        self.entget = entget
        self.commands = []
        self.objects = {'30A': SimpleNamespace(ObjectName='TDbSlopeRoof')}
        self.doc = SimpleNamespace(ModelSpace=SimpleNamespace(AddLightWeightPolyline=self.add_polyline))

    def guard(self):
        pass

    def entities(self):
        return dict(self.objects)

    def add_polyline(self, coordinates):
        self.boundary = SimpleNamespace(Handle=self.boundary_handle, ObjectName='AcDbPolyline',
                                        Closed=False, coordinates=coordinates)
        self.objects[self.boundary_handle] = self.boundary
        return self.boundary

    def run_command(self, payload):
        self.commands.append(payload)
        if payload.startswith('TSlopeRoof\n'):
            if self.consume:
                del self.objects[self.boundary_handle]
            self.objects[self.roof.Handle] = self.roof
        else:
            path, handle = re.search(r'\(open "([^"]+)" "w"\).*?\(handent "([^"]+)"\)', payload).groups()
            if handle != self.roof.Handle:
                raise AssertionError(f'DXF requested for {handle}, expected {self.roof.Handle}')
            Path(path).write_text(self.entget, encoding='gbk')

    def describe(self, entity):
        return {'handle': entity.Handle, 'object_name': entity.ObjectName}


class RoofTests(unittest.TestCase):
    def setUp(self):
        self.drawing_guard = self.enterContext(patch('tz_mcp.parts.require_drawing_window'))
        self.model_guard = self.enterContext(patch('tz_mcp.parts.require_model_space'))
        self.enterContext(patch('tz_mcp.parts.vector', side_effect=list))
        self.enterContext(patch('tz_mcp.parts.late', side_effect=lambda entity: entity))
        self.folder = self.enterContext(TemporaryDirectory())
        self.probe = self.enterContext(patch('tz_mcp.parts.roof_dxf',
                                             side_effect=lambda c, handle: roof_dxf(c, handle, folder=self.folder)))

    def create(self, c, angle=30, overhang=600):
        return c.execute('roof.create', {'boundary': BOUNDARY, 'angle': angle, 'overhang': overhang})

    def assert_unknown(self, cad, field, handle='34D'):
        with self.assertRaises(OutcomeUnknown) as failure:
            self.create(cad)
        message = str(failure.exception)
        for part in (field, 'expected', 'observed',
                     f'{handle} remains in the drawing - side effect present, do not replay blind'):
            self.assertIn(part, message)

    def test_roof_dxf_reads_wp2_entget(self):
        self.assertEqual(roof_dxf(FakeCAD(), '34D', folder=self.folder),
                         {'type': 'TCH_SLOPEROOF', 'handle': '34D', 'layer': 'ROOF',
                          'elevation': -346.41, 'vertices': BOUNDARY,
                          'edge_slopes_rad': [0.523599] * 4})

    def test_create_known_roofs_and_zero_overhang(self):
        cases = [
            (30, 600, -346.41016151377545, '-346.41', '0.523599'),
            (45, 600, -599.9999999999999, '-600.0', '0.785398'),
            (30, 300, -173.20508075688772, '-173.205', '0.523599'),
            (30, 0, 0.0, '0.0', '0.523599'),
        ]
        for angle, overhang, elevation, dxf_elev, slope in cases:
            with self.subTest(angle=angle, overhang=overhang):
                angle_measured = 29.999999999999996 if angle == 30 else 45.0
                raw = WP2_ENTGET.replace('-346.41', dxf_elev).replace('0.523599', slope)
                cad = FakeCAD(FakeRoofEntity(angle_measured, elevation), entget=raw)
                result = self.create(cad, angle, overhang)
                self.assertTrue(result['success'])
                self.drawing_guard.assert_called_with(cad.hwnd)
                self.model_guard.assert_called_with(cad.doc)
                self.assertEqual(cad.commands[0], f'TSlopeRoof\n(handent "34C")\n{angle}\n{overhang}\n')
                self.assertTrue(cad.boundary.Closed)
                self.assertEqual(cad.boundary.coordinates, [value for point in BOUNDARY for value in point])
                self.assertNotIn('34C', cad.objects)
                self.assertEqual(result['handles'], ['34D'])
                self.assertEqual(result['validation'],
                                 {'type': True, 'properties': True, 'dxf': 'verified_10_51_and_148',
                                  'host_association': 'not_applicable', 'reopen': 'pending'})
                roof = result['evidence']['roof']
                self.assertTrue(roof['boundary_consumed'])
                self.assertEqual(roof['angle_requested_deg'], angle)
                self.assertEqual(roof['overhang_requested_mm'], overhang)
                self.assertEqual(roof['com_slope_angle_deg'], angle_measured)
                self.assertEqual(roof['com_elevation_mm'], elevation)
                self.assertAlmostEqual(roof['expected_elevation_mm'], elevation)
                self.assertEqual(roof['dxf']['vertices'], BOUNDARY)
                self.assertEqual(roof['dxf']['elevation'], float(dxf_elev))
                self.assertEqual(roof['dxf']['edge_slopes_rad'], [float(slope)] * 4)

    def test_retained_boundary_stops_readback(self):
        self.assert_unknown(FakeCAD(consume=False), 'boundary consumption', '34C')
        self.probe.assert_not_called()

    def test_roof_claim_must_be_unique(self):
        for object_name, count in (('AcDbProxyEntity', 0), ('TDbSlopeRoof', 2)):
            with self.subTest(count=count):
                cad = FakeCAD(SimpleNamespace(Handle='34D', ObjectName=object_name))
                run_command = cad.run_command

                def create_extra(payload):
                    run_command(payload)
                    if count == 2:
                        cad.objects['34E'] = FakeRoofEntity()

                cad.run_command = create_extra
                with self.assertRaisesRegex(OutcomeUnknown, f'Expected one new TDbSlopeRoof, observed {count}'):
                    self.create(cad)
        self.probe.assert_not_called()

    def test_missing_com_properties_stop_readback(self):
        for field in ('SlopeAngle', 'Elevation'):
            with self.subTest(field=field):
                roof = FakeRoofEntity()
                delattr(roof, field)
                self.assert_unknown(FakeCAD(roof), field)
        self.probe.assert_not_called()

    def test_invalid_com_values(self):
        for field, value in (('SlopeAngle', 29.9), ('Elevation', -347.0),
                             ('SlopeAngle', float('nan')), ('Elevation', float('inf'))):
            with self.subTest(field=field, value=value):
                roof = FakeRoofEntity()
                setattr(roof, field, value)
                self.assert_unknown(FakeCAD(roof), field)
        self.probe.assert_not_called()

    def test_invalid_dxf_values(self):
        cases = [
            ('DXF type', WP2_ENTGET.replace('TCH_SLOPEROOF', 'TCH_SLAB')),
            ('DXF 148', WP2_ENTGET.replace('(148 . -346.41)', '')),
            ('DXF 148', WP2_ENTGET.replace('-346.41', '-347.0')),
            ('DXF 148', WP2_ENTGET.replace('-346.41', 'nan')),
            ('DXF 10 vertex count', WP2_ENTGET + '\n(10 10000.0 20000.0 0.0)'),
            ('DXF 10 vertex count', WP2_ENTGET.replace('(10 10000.0 20000.0 0.0)', '')),
            ('DXF 51 edge count', WP2_ENTGET.replace('(51 . 0.523599)', '', 1)),
            ('DXF 51 edge 2', WP2_ENTGET.replace('(51 . 0.523599)\n(10 16000.0 24000.0',
                                                 '(51 . 0.54)\n(10 16000.0 24000.0')),
        ]
        for field, raw in cases:
            with self.subTest(field=field):
                self.assert_unknown(FakeCAD(entget=raw), field)

    def test_probe_dispatch_failure_carries_built_handle(self):
        self.probe.side_effect = CommandNotSent('DXF command not sent')
        self.assert_unknown(FakeCAD(), 'DXF readback')

    def test_probe_timeout_preserves_timeout_exception(self):
        self.probe.side_effect = CommandCompletionTimeout('DXF command completion not confirmed')
        with self.assertRaisesRegex(CommandCompletionTimeout, '34D remains in the drawing'):
            self.create(FakeCAD())


if __name__ == '__main__':
    unittest.main()

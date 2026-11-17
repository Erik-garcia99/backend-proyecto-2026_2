import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.telemetry import (InfluxSettings, create_router, flux_query,
                           influxql_query, read_roi_occupancy)

BRANCH = UUID('d07018c7-99c1-490f-95b9-dd54589cf60b')
ROI = UUID('a5760093-b323-48d6-a1bb-d1574ab07857')
NODE = UUID('5c485efa-cc20-4f9a-91eb-ceeba0f99289')
ORG = UUID('11111111-1111-1111-1111-111111111111')
SETTINGS = InfluxSettings('2', 'https://influx.test', 'secret', 'temp', 'value',
                          'branch_id', bucket='iot', org='test')
CONTEXT = dict(camera_node_id=NODE, config_version=28, calibration_version=16,
               calibration_status='calibrated')


class RoiTelemetryTests(unittest.TestCase):
    def test_context_filters_before_latest(self):
        settings = replace(SETTINGS, node_ids=(str(NODE),), roi_id=str(ROI),
                           roi_config_version=28, roi_calibration_version=16)
        for query, end in ((flux_query(settings, BRANCH, 'roi_occ', 'value'), '|> last()'),
                           (influxql_query(settings, BRANCH, 'roi_occ', 'value'), 'ORDER BY')):
            for name in ('roi_id', 'config_version', 'calibration_version', 'esp_id'):
                self.assertLess(query.index(name), query.index(end))
            self.assertIn(str(ROI), query)
            self.assertIn(str(NODE), query)
            self.assertIn('roi_occ', query)

    def test_reading_no_data_and_stale(self):
        for point, expected in ((None, 'no_data'),
                                ((datetime.now(timezone.utc) - timedelta(seconds=5), 63.0), 'ok'),
                                ((datetime.now(timezone.utc) - timedelta(minutes=2), 0.0), 'stale')):
            with patch('app.telemetry._fetch_point', return_value=point) as fetch:
                result = read_roi_occupancy(SETTINGS, BRANCH, ROI, CONTEXT)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.occupancy_pct, int(point[1]) if point else None)
                self.assertEqual(result.clock_quality, 'gateway_received')
                self.assertEqual(fetch.call_args.args[0].node_ids, (str(NODE),))
        with patch('app.telemetry._fetch_point') as fetch:
            result = read_roi_occupancy(SETTINGS, BRANCH, ROI,
                                        {**CONTEXT, 'calibration_version': None})
            self.assertEqual(result.status, 'no_data')
            fetch.assert_not_called()

    def test_real_flux_transport_filters_roi_and_parses_value(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
        seen = []
        def handle(request):
            seen.append(request.content.decode())
            return httpx.Response(200, text=f',_time,_value\n,{timestamp},100\n')
        client = httpx.Client(transport=httpx.MockTransport(handle))
        with patch('app.telemetry.httpx.Client', return_value=client):
            result = read_roi_occupancy(SETTINGS, BRANCH, ROI, CONTEXT)
        self.assertEqual(result.occupancy_pct, 100)
        self.assertIn(str(ROI), seen[0])

    def test_authorization_and_endpoint(self):
        row = {**CONTEXT}
        branch = dict(id=BRANCH, organization_id=ORG)
        user = dict(role='owner', is_superuser=False, organization_id=ORG, branch_id=BRANCH)
        queries = []
        @contextmanager
        def connection():
            db = Mock()
            def execute(query, params):
                queries.append((query, params))
                return Mock(fetchone=lambda: row if 'FROM roi r' in query else branch)
            db.execute.side_effect = execute
            yield db
        app = FastAPI()
        app.include_router(create_router(lambda: user, connection))
        path = f'/branches/{BRANCH}/rois/{ROI}/occupancy/latest'
        with TestClient(app) as client, patch('app.telemetry.InfluxSettings.from_env', return_value=SETTINGS), \
                patch('app.telemetry._fetch_point', return_value=None) as fetch:
            response = client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'no_data')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertIn("c.id=r.active_calibration_id", queries[-1][0])
            row = None
            self.assertEqual(client.get(path).status_code, 404)
            user['organization_id'] = ROI
            fetch.reset_mock()
            self.assertEqual(client.get(path).status_code, 403)
            fetch.assert_not_called()

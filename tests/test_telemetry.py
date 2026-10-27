import os
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.telemetry import (InfluxSettings, can_read_branch, create_router, flux_query,
                           influxql_query, parse_flux, parse_influxql, read_temperature)

BRANCH = UUID('11111111-1111-4111-8111-111111111111')
OTHER_BRANCH = UUID('22222222-2222-4222-8222-222222222222')
ORG = UUID('33333333-3333-4333-8333-333333333333')
SETTINGS = InfluxSettings('2', 'https://example.influxdata.com', 'test-secret',
                          'environment', 'temperature_c', 'branch_id', org='org', bucket='iot')


def csv_point(timestamp, value=25.5):
    return f'#datatype,string,long,dateTime:RFC3339,double\n,result,table,_time,_value\n,,0,{timestamp},{value}\n'


class InfluxTests(unittest.TestCase):
    def read_with_response(self, response, settings=SETTINGS):
        real_client = httpx.Client
        requests = []

        def handler(request):
            requests.append(request)
            if isinstance(response, Exception):
                raise response
            return response

        client = real_client(transport=httpx.MockTransport(handler))
        with patch('app.telemetry.httpx.Client', return_value=client):
            result = read_temperature(settings, BRANCH)
        return result, requests

    def test_recent_temperature_and_flux_request(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        data, requests = self.read_with_response(httpx.Response(200, text=csv_point(timestamp)))
        self.assertEqual(data.temperature_c, 25.5)
        self.assertEqual(data.status, 'ok')
        self.assertEqual(requests[0].url.path, '/api/v2/query')
        self.assertEqual(requests[0].headers['Authorization'], 'Token test-secret')
        self.assertIn(str(BRANCH), requests[0].content.decode())
        self.assertNotIn('test-secret', repr(SETTINGS))

    def test_stale_reading(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        data, _ = self.read_with_response(httpx.Response(200, text=csv_point(timestamp)))
        self.assertEqual(data.status, 'stale')
        self.assertGreaterEqual(data.age_seconds, 300)

    def test_empty_flux_is_not_zero(self):
        data, _ = self.read_with_response(httpx.Response(200, text=''))
        self.assertEqual(data.status, 'no_data')
        self.assertIsNone(data.temperature_c)
        self.assertIsNone(data.measured_at)

    def test_zero_is_valid_temperature(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        data, _ = self.read_with_response(httpx.Response(200, text=csv_point(timestamp, 0)))
        self.assertEqual(data.temperature_c, 0)
        self.assertEqual(data.status, 'ok')

    def test_provider_failures_do_not_leak_secrets(self):
        for provider_status, status in [(401, 503), (403, 503), (429, 503), (400, 502), (500, 502), (302, 502)]:
            with self.subTest(provider_status=provider_status), self.assertRaises(HTTPException) as raised:
                self.read_with_response(httpx.Response(provider_status, text='test-secret'))
            self.assertEqual(raised.exception.status_code, status)
            self.assertNotIn('test-secret', raised.exception.detail)

    def test_timeout(self):
        with self.assertRaises(HTTPException) as raised:
            self.read_with_response(httpx.ReadTimeout('test-secret'))
        self.assertEqual(raised.exception.status_code, 504)

    def test_malformed_nonfinite_or_future_data(self):
        for text in ['<html>unexpected</html>', csv_point('invalid'),
                     csv_point('2026-01-01T00:00:00Z', 'NaN'),
                     csv_point((datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
                     ',error,reference\n,query failed,123\n']:
            with self.subTest(text=text), self.assertRaises(HTTPException) as raised:
                self.read_with_response(httpx.Response(200, text=text))
            self.assertEqual(raised.exception.status_code, 502)

    def test_v3_query_and_latest_across_series(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        payload = {'results': [{'series': [
            {'columns': ['time', 'temperature_c'], 'values': [['2025-01-01T00:00:00Z', 20]]},
            {'columns': ['time', 'temperature_c'], 'values': [[timestamp, 28]]},
        ]}]}
        settings = replace(SETTINGS, version='3', database='iot', retention_policy='autogen')
        data, requests = self.read_with_response(httpx.Response(200, json=payload), settings)
        self.assertEqual(data.temperature_c, 28)
        self.assertEqual(requests[0].url.path, '/query')
        self.assertEqual(requests[0].url.params['db'], 'iot')
        self.assertEqual(requests[0].url.params['rp'], 'autogen')
        self.assertIn(str(BRANCH), requests[0].url.params['q'])

    def test_empty_influxql_and_error_response(self):
        self.assertIsNone(parse_influxql({'results': [{}]}))
        with self.assertRaises(ValueError):
            parse_influxql({'results': [{'error': 'query failed'}]})

    def test_queries_quote_configuration_and_filter_branch(self):
        settings = replace(SETTINGS, measurement='a"b', branch_tag='sucursal', bucket='${bad}')
        self.assertIn(r'\${bad}', flux_query(settings, BRANCH))
        self.assertIn(r'a\"b', flux_query(settings, BRANCH))
        self.assertIn('r["sucursal"]', flux_query(settings, BRANCH))
        self.assertIn('"sucursal" =', influxql_query(settings, BRANCH))
        self.assertIn(str(BRANCH), influxql_query(settings, BRANCH))

    def test_csv_without_annotations(self):
        self.assertEqual(parse_flux(',_time,_value\n,2026-01-01T00:00:00Z,12\n'),
                         ('2026-01-01T00:00:00Z', '12'))

    def test_flux_partial_response_error_is_not_reported_as_success(self):
        with self.assertRaises(ValueError):
            parse_flux(csv_point('2026-01-01T00:00:00Z') + '\n,error,reference\n,failed,123\n')

    def test_environment_validation(self):
        env = {'INFLUXDB_VERSION': '2', 'INFLUXDB_URL': SETTINGS.url, 'INFLUXDB_TOKEN': 'test-secret',
               'INFLUXDB_MEASUREMENT': 'environment', 'INFLUXDB_TEMPERATURE_FIELD': 'temperature_c',
               'INFLUXDB_BRANCH_TAG': 'branch_id', 'INFLUXDB_ORG': 'org', 'INFLUXDB_BUCKET': 'iot'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(InfluxSettings.from_env().version, '2')
        with patch.dict(os.environ, {**env, 'INFLUXDB_VERSION': '3', 'INFLUXDB_DATABASE': 'iot',
                                     'INFLUXDB_ORG': '', 'INFLUXDB_BUCKET': ''}, clear=True):
            self.assertEqual(InfluxSettings.from_env().database, 'iot')
        for change in [{'INFLUXDB_TOKEN': ''}, {'INFLUXDB_URL': 'http://example.com'},
                       {'INFLUXDB_URL': 'https://example.com/api/v2/write'},
                       {'INFLUXDB_URL': 'https://[invalid'},
                       {'INFLUXDB_VERSION': 'unknown'}, {'INFLUXDB_TIMEOUT_SECONDS': '99'},
                       {'INFLUXDB_STALE_SECONDS': 'bad'}]:
            with patch.dict(os.environ, {**env, **change}, clear=True), self.assertRaises(HTTPException) as raised:
                InfluxSettings.from_env()
            self.assertEqual(raised.exception.status_code, 503)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.user = {'is_superuser': False, 'organization_id': ORG, 'branch_id': BRANCH, 'role': 'employee'}
        self.branch = {'id': BRANCH, 'organization_id': ORG}
        self.connection = Mock()
        self.connection.execute.return_value.fetchone.return_value = self.branch

        @contextmanager
        def get_connection():
            yield self.connection

        def current_user():
            if self.user is None:
                raise HTTPException(401, 'Se requiere sesión')
            return self.user

        app = FastAPI()
        app.include_router(create_router(current_user, get_connection))
        self.client = TestClient(app)
        self.path = f'/branches/{BRANCH}/telemetry/temperature/latest'

    def test_unauthenticated_and_foreign_branch_never_query_influx(self):
        with patch('app.telemetry.read_temperature') as read:
            self.user = None
            self.assertEqual(self.client.get(self.path).status_code, 401)
            self.user = {'is_superuser': False, 'organization_id': ORG, 'branch_id': OTHER_BRANCH, 'role': 'employee'}
            self.assertEqual(self.client.get(self.path).status_code, 403)
            read.assert_not_called()

    def test_missing_branch(self):
        self.connection.execute.return_value.fetchone.return_value = None
        self.assertEqual(self.client.get(self.path).status_code, 404)

    def test_invalid_uuid(self):
        self.assertEqual(self.client.get('/branches/not-a-uuid/telemetry/temperature/latest').status_code, 422)

    def test_authorized_response_and_no_store(self):
        payload = {'branch_id': BRANCH, 'temperature_c': None, 'measured_at': None,
                   'age_seconds': None, 'stale_after_seconds': 90, 'status': 'no_data'}
        with patch('app.telemetry.InfluxSettings.from_env', return_value=SETTINGS), \
                patch('app.telemetry.read_temperature', return_value=payload):
            response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.json()['status'], 'no_data')

    def test_roles_and_organization_isolation(self):
        self.assertTrue(can_read_branch(self.user, self.branch))
        self.assertTrue(can_read_branch({**self.user, 'branch_id': None, 'role': 'owner'}, self.branch))
        self.assertFalse(can_read_branch({**self.user, 'organization_id': OTHER_BRANCH, 'role': 'owner'}, self.branch))
        self.assertTrue(can_read_branch({**self.user, 'is_superuser': True}, self.branch))

    def test_branch_list_scopes_sql_and_returns_only_public_fields(self):
        self.connection.execute.return_value.fetchall.return_value = [{**self.branch, 'name': 'Piloto'}]
        response = self.client.get('/telemetry/branches')
        self.assertEqual(response.json(), [{'id': str(BRANCH), 'name': 'Piloto'}])
        self.assertEqual(self.connection.execute.call_args.args[1], (False, ORG, False, BRANCH))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')


if __name__ == '__main__':
    unittest.main()

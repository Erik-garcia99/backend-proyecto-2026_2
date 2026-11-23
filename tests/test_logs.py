import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.logs import create_router, record_change

B = UUID('11111111-1111-4111-8111-111111111111')
O = UUID('22222222-2222-4222-8222-222222222222')
N = UUID('33333333-3333-4333-8333-333333333333')
U = UUID('44444444-4444-4444-8444-444444444444')


class FakeConnection:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [{
            'id': 1, 'branch_id': B, 'node_id': N, 'user_id': U,
            'category': 'configuration', 'severity': 'info',
            'event_type': 'shelf.created', 'message': 'Cambio guardado en el sistema',
            'metadata': {'before': None, 'after': {'name': 'Repisa 1'}},
            'created_at': datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
        }]
        self.calls = []

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        if 'SELECT b.id' in sql:
            return _Result(row={'id': B, 'organization_id': O})
        return _Result(rows=self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class RecordChangeTests(unittest.TestCase):
    def test_change_keeps_node_and_before_after(self):
        connection = FakeConnection()
        record_change(connection, B, {'id': U}, 'device.renamed',
                      node_id=N, before={'name': 'Antes'}, after={'name': 'Después'})
        sql, args = connection.calls[-1]
        self.assertIn('INSERT INTO system_log', sql)
        self.assertIn("'configuration'", sql)
        self.assertEqual(args[2], N)
        metadata = json.dumps(args[-1].obj, ensure_ascii=False)
        self.assertIn('Antes', metadata)
        self.assertIn('Después', metadata)
        self.assertIn('application_status', metadata)


class LogsTests(unittest.TestCase):
    def setUp(self):
        self.user = {'id': U, 'role': 'manager', 'organization_id': O,
                     'branch_id': B, 'is_superuser': False}
        self.conn = FakeConnection()
        self.client = TestClient(self._app())

    def _app(self):
        @contextmanager
        def connect():
            yield self.conn

        app = FastAPI()
        app.include_router(create_router(lambda: self.user, connect))
        return app

    def test_list_logs_is_filtered_paginated_and_not_cached(self):
        response = self.client.get(
            f'/branches/{B}/logs',
            params={'since': '2026-11-01T00:00:00Z', 'category': 'configuration',
                    'node_id': str(N), 'limit': 10})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload['items']), 1)
        self.assertFalse(payload['has_more'])
        self.assertEqual(payload['items'][0]['event_type'], 'shelf.created')
        self.assertEqual(payload['items'][0]['node_id'], str(N))
        self.assertTrue(payload['items'][0]['created_at'].startswith('2026-11-22'))
        self.assertEqual(response.headers['cache-control'], 'no-store')
        sql = self.conn.calls[-1][0]
        self.assertIn('created_at >= %s', sql)
        self.assertIn('category=%s', sql)
        self.assertIn('node_id=%s', sql)
        self.assertIn('LIMIT %s OFFSET %s', sql)

    def test_invalid_range_is_rejected(self):
        response = self.client.get(f'/branches/{B}/logs', params={
            'since': '2026-11-22T00:00:00Z', 'until': '2026-11-21T00:00:00Z'})
        self.assertEqual(response.status_code, 422)
        response = self.client.get(f'/branches/{B}/logs', params={
            'since': '2026-11-21'})
        self.assertEqual(response.status_code, 422)

    def test_node_history_uses_the_same_log_source(self):
        response = self.client.get(f'/branches/{B}/logs/nodes/{N}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items'][0]['event_type'], 'shelf.created')
        self.assertIn('node_id=%s', self.conn.calls[-1][0])

    def test_export_is_csv_attachment(self):
        response = self.client.get(f'/branches/{B}/logs/export')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="bitacora.csv"',
                      response.headers['content-disposition'])
        self.assertTrue(response.text.startswith('\ufefffecha,categoria,severidad,evento'))
        self.assertIn('shelf.created', response.text)
        self.assertIn('Repisa 1', response.text)

    def test_employee_cannot_read_the_log(self):
        self.user = {**self.user, 'role': 'employee'}
        response = self.client.get(f'/branches/{B}/logs')
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()

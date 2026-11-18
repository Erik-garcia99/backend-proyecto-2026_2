import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.alerts import create_router, episode, heartbeat_condition, scan_heartbeats

B = UUID('11111111-1111-4111-8111-111111111111')
O = UUID('22222222-2222-4222-8222-222222222222')
N = UUID('33333333-3333-4333-8333-333333333333')
A = UUID('44444444-4444-4444-8444-444444444444')


class Connection:
    def __init__(self):
        self.calls = []
        self.items = []
        self.row = None

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        result = Mock()
        result.fetchall.return_value = self.items
        result.fetchone.return_value = (
            {'id': B, 'organization_id': O} if 'SELECT b.id, b.organization_id' in sql else self.row)
        return result

    def commit(self):
        pass


class AlertsApiTest(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()
        self.user = {'id': N, 'role': 'manager', 'branch_id': B,
                     'organization_id': O, 'is_superuser': False}

        @contextmanager
        def get_connection():
            yield self.connection

        app = FastAPI()
        app.include_router(create_router(lambda: self.user, get_connection))
        self.client = TestClient(app)
        self.path = f'/branches/{B}/alerts'

    def test_empty_is_not_all_sources_normal(self):
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertEqual(data['items'], [])
        self.assertEqual(data['sources']['stock_empty'], 'roi_state_monitor')
        self.assertEqual(data['sources']['stock_low'], 'roi_state_monitor')
        self.assertEqual(data['sources']['environment'], 'not_connected')
        self.assertIn('monitor', data)

    def test_customer_denied_even_with_legacy_branch(self):
        for role in ('customer', 'cliente', 'cashier', 'unknown'):
            self.user['role'] = role
            self.assertEqual(self.client.get(self.path).status_code, 403)
        self.assertFalse(any('FROM alert' in sql for sql, _ in self.connection.calls))

    def test_cross_branch_and_organization_denied(self):
        self.user['branch_id'] = N
        self.assertEqual(self.client.get(self.path).status_code, 403)
        self.user.update(role='owner', organization_id=N)
        self.assertEqual(self.client.get(self.path).status_code, 403)

    def test_employee_and_staff_can_read_only_own_branch(self):
        for role in ('employee', 'staff'):
            self.user.update(role=role, branch_id=B)
            self.assertEqual(self.client.get(self.path).status_code, 200)
            self.user['branch_id'] = N
            self.assertEqual(self.client.get(self.path).status_code, 403)

    def test_filter_validation(self):
        for query in ('limit=101', 'offset=-1', 'alert_type=orders',
                      'node_id=bad', 'severity=high', 'since=2026-11-14T10:00:00',
                      'since=2026-11-15T00:00:00Z&until=2026-11-14T00:00:00Z'):
            self.assertEqual(self.client.get(f'{self.path}?{query}').status_code, 422, query)

    def test_filters_are_parameterized_and_scoped(self):
        response = self.client.get(
            f'{self.path}?status=all&alert_type=node_offline&product_id={N}&node_id={N}')
        self.assertEqual(response.status_code, 200)
        sql, args = self.connection.calls[-1]
        self.assertIn('branch_id=%s', sql)
        self.assertIn('node_id=%s', sql)
        self.assertIn(str(N), args)
        self.assertNotIn(str(N), sql)

    def test_pagination(self):
        self.connection.items = [{'id': str(i)} for i in range(3)]
        data = self.client.get(f'{self.path}?limit=2').json()
        self.assertEqual(len(data['items']), 2)
        self.assertTrue(data['has_more'])

    def test_locations_group_lights_without_exposing_unconfirmed_nodes(self):
        common = {'id': A, 'name': 'Estante A', 'location_label': 'Entrada',
                  'node_id': N, 'node_name': 'Guía', 'confirmed_node_id': N,
                  'led_index': 0, 'light_name': 'Luz A'}
        self.connection.items = [common, {**common, 'led_index': 1, 'light_name': 'Luz B'},
                                 {**common, 'id': N, 'name': 'Estante B', 'confirmed_node_id': None}]
        response = self.client.get(f'/branches/{B}/alert-locations')
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]['lights']), 2)
        self.assertEqual(rows[1]['lights'], [])
        sql, args = self.connection.calls[-1]
        self.assertIn('g.branch_id=s.branch_id', sql)
        self.assertEqual(args, (B,))

    def test_locations_deny_customer(self):
        self.user['role'] = 'customer'
        self.assertEqual(self.client.get(f'/branches/{B}/alert-locations').status_code, 403)

    def test_missing_detail_and_ack(self):
        self.assertEqual(self.client.get(f'{self.path}/{A}').status_code, 404)
        self.assertEqual(self.client.post(f'{self.path}/{A}/acknowledge').status_code, 404)
        self.assertIn('branch_id=%s AND id=%s', self.connection.calls[-1][0])

    def test_acknowledge_is_idempotent(self):
        self.connection.row = {'id': A, 'status': 'acknowledged'}
        for _ in range(2):
            self.assertEqual(self.client.post(f'{self.path}/{A}/acknowledge').status_code, 200)
        self.assertFalse(any('INSERT INTO system_log' in sql for sql, _ in self.connection.calls))

    def test_acknowledge_resolved_conflicts(self):
        self.connection.row = {'id': A, 'status': 'resolved'}
        self.assertEqual(self.client.post(f'{self.path}/{A}/acknowledge').status_code, 409)

    def test_acknowledge_open_logs_but_does_not_resolve(self):
        self.connection.row = {'id': A, 'status': 'open'}
        self.assertEqual(self.client.post(f'{self.path}/{A}/acknowledge').status_code, 200)
        statements = '\n'.join(sql for sql, _ in self.connection.calls)
        self.assertIn("status='acknowledged'", statements)
        self.assertIn('INSERT INTO system_log', statements)
        self.assertNotIn("status='resolved'", statements)


class HeartbeatTest(unittest.TestCase):
    def test_unknown_stale_recent_future(self):
        now = datetime.now(timezone.utc)
        self.assertIsNone(heartbeat_condition(None, now))
        self.assertIsNone(heartbeat_condition(now + timedelta(seconds=1), now))
        self.assertFalse(heartbeat_condition(now - timedelta(seconds=44), now))
        self.assertTrue(heartbeat_condition(now - timedelta(seconds=45), now))

    def run_episode(self, condition, existing):
        connection = Mock()
        connection.execute.side_effect = [
            Mock(), Mock(fetchone=lambda: existing),
            Mock(fetchone=lambda: {'id': A}), Mock()]
        episode(connection, branch_id=B, node_id=N, gateway_id=A,
                condition=condition, metadata={'source': 'signed_node_heartbeat'})
        return connection

    def test_open_persists_and_logs(self):
        connection = self.run_episode(True, None)
        sql = '\n'.join(call.args[0] for call in connection.execute.call_args_list)
        self.assertIn('pg_advisory_xact_lock', sql)
        self.assertIn('INSERT INTO alert', sql)
        self.assertIn('INSERT INTO system_log', sql)

    def test_persistent_condition_does_not_repeat_even_when_acknowledged(self):
        connection = self.run_episode(True, {'id': A})
        self.assertEqual(connection.execute.call_count, 2)
        self.assertIn("'acknowledged'", connection.execute.call_args.args[0])

    def test_recovery_persists_and_logs(self):
        connection = self.run_episode(False, {'id': A})
        sql = '\n'.join(call.args[0] for call in connection.execute.call_args_list)
        self.assertIn("status='resolved'", sql)
        self.assertIn('INSERT INTO system_log', sql)

    def test_no_unknown_episode(self):
        connection = Mock()
        episode(connection, branch_id=B, node_id=N, gateway_id=A,
                condition=None, metadata={})
        connection.execute.assert_not_called()

    def test_healthy_without_existing_episode_does_not_write(self):
        connection = self.run_episode(False, None)
        self.assertEqual(connection.execute.call_count, 2)

    def test_scanner_ignores_candidates_and_inactive_entities(self):
        connection = Connection()

        @contextmanager
        def get_connection():
            yield connection

        scan_heartbeats(get_connection)
        sql = connection.calls[0][0]
        for term in ("n.node_code IS NOT NULL", "n.registration_status='registered'",
                     'g.is_active', 'b.is_active', 'o.is_active', 'FOR UPDATE OF n'):
            self.assertIn(term, sql)


if __name__ == '__main__':
    unittest.main()

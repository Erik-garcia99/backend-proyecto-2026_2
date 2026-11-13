import unittest
from contextlib import contextmanager
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from app import main
from app.telemetry import OccupancyResponse
from datetime import datetime, timezone

ORG = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
BRANCH = UUID('dddddddd-dddd-4ddd-8ddd-dddddddddddd')
USER = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')


class FakeCursor:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        if isinstance(self.result, list):
            return self.result[0] if self.result else None
        return self.result

    def fetchall(self):
        if isinstance(self.result, list):
            return self.result
        return [self.result] if self.result is not None else []


class FakeConnection:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))
        result = self.results.pop(0) if self.results else None
        return FakeCursor(result)

    def commit(self):
        pass

    def rollback(self):
        pass


class CustomerHomeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.token = None

    def use_connection(self, connection):
        @contextmanager
        def get_connection():
            yield connection
        return patch.object(main.db, 'get_connection', get_connection)

    def auth_header(self):
        # bypass current_user by dependency override
        def current_user():
            return {'id': USER, 'role': 'customer', 'organization_id': ORG,
                    'branch_id': None, 'is_superuser': False}
        main.app.dependency_overrides[main.current_user] = current_user
        return {'Authorization': 'Bearer fake'}

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_rejects_staff_role(self):
        def current_user():
            return {'id': USER, 'role': 'employee', 'organization_id': ORG,
                    'branch_id': BRANCH, 'is_superuser': False}
        main.app.dependency_overrides[main.current_user] = current_user
        connection = FakeConnection(results=[[{'id': BRANCH, 'name': 'Sucursal', 'organization_id': ORG}]])
        with self.use_connection(connection):
            response = self.client.get(f'/customer/home?branch_id={BRANCH}', headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.status_code, 403, response.text)

    def test_returns_404_for_inactive_branch(self):
        self.auth_header()
        connection = FakeConnection(results=[None])
        with self.use_connection(connection):
            response = self.client.get(f'/customer/home?branch_id={BRANCH}', headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.status_code, 404, response.text)

    def test_returns_403_for_other_organization(self):
        self.auth_header()
        other_org = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
        connection = FakeConnection(results=[[{'id': BRANCH, 'name': 'Sucursal', 'organization_id': other_org}]])
        with self.use_connection(connection):
            response = self.client.get(f'/customer/home?branch_id={BRANCH}', headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.status_code, 403, response.text)

    def test_returns_occupancy_for_customer_branch(self):
        self.auth_header()
        connection = FakeConnection(results=[
            [{'id': BRANCH, 'name': 'Sucursal Centro', 'organization_id': ORG}],
            [],
        ])
        occ = OccupancyResponse(branch_id=BRANCH, occupancy=7, measured_at=datetime(2026, 11, 12, tzinfo=timezone.utc),
                                age_seconds=5, stale_after_seconds=20, status='ok')
        with self.use_connection(connection):
            with patch.object(main, 'InfluxSettings') as mock_settings:
                mock_settings.from_env.return_value = object()
                with patch.object(main, 'replace', side_effect=lambda s, **kw: s):
                    with patch.object(main, 'read_occupancy', return_value=occ):
                        response = self.client.get(f'/customer/home?branch_id={BRANCH}', headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['branch_name'], 'Sucursal Centro')
        self.assertEqual(body['occupancy'], 7)
        self.assertEqual(body['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from app import main
from app.security import hash_password, verify_password

USER = {
    'id': UUID('11111111-1111-4111-8111-111111111111'),
    'organization_id': UUID('22222222-2222-4222-8222-222222222222'),
    'branch_id': None,
    'email': 'ana@selfiq.local',
    'first_name': 'Ana',
    'last_name': 'López',
    'phone_number': '5551234567',
    'role': 'employee',
    'account_status': 'active',
    'is_superuser': False,
    'password_hash': hash_password('Actual123'),
    'deactivated_at': None,
}

PENDING_ID = UUID('33333333-3333-4333-8333-333333333333')


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        if isinstance(self.row, list):
            return self.row[0] if self.row else None
        return self.row

    def fetchall(self):
        if isinstance(self.row, list):
            return self.row
        return [self.row] if self.row is not None else []


class FakeConnection:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.executed = []
        self.commits = 0

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if self.error:
            raise self.error
        return FakeCursor(self.row)

    def commit(self):
        self.commits += 1


class ProfileEndpointTests(unittest.TestCase):
    def setUp(self):
        self.user = dict(USER)
        main.app.dependency_overrides[main.current_user] = lambda: self.user
        self.client = TestClient(main.app)

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def use_connection(self, connection):
        @contextmanager
        def get_connection():
            yield connection
        return patch.object(main.db, 'get_connection', get_connection)

    def test_patch_me_updates_only_sent_fields(self):
        updated = {**self.user, 'first_name': 'Ana María', 'phone_number': None}
        connection = FakeConnection(updated)
        with self.use_connection(connection):
            response = self.client.patch('/auth/me', json={'first_name': ' Ana María ', 'phone_number': ''})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['first_name'], 'Ana María')
        self.assertIsNone(response.json()['phone_number'])
        query, params = connection.executed[0]
        self.assertIn('first_name=%s', query)
        self.assertIn('phone_number=%s', query)
        self.assertNotIn('email=', query)
        self.assertEqual(params, ('Ana María', None, self.user['id']))
        self.assertEqual(connection.commits, 1)

    def test_patch_me_rejects_duplicated_email(self):
        connection = FakeConnection(error=UniqueViolation('duplicate key'))
        with self.use_connection(connection):
            response = self.client.patch('/auth/me', json={'email': 'otra@selfiq.local'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], 'El correo ya está registrado')

    def test_patch_me_rejects_empty_name(self):
        with self.use_connection(FakeConnection()):
            response = self.client.patch('/auth/me', json={'last_name': None})
        self.assertEqual(response.status_code, 422)

    def test_change_password_requires_current_password(self):
        connection = FakeConnection()
        with self.use_connection(connection):
            response = self.client.post('/auth/change-password',
                                        json={'current_password': 'incorrecta', 'new_password': 'NuevaClave123'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(connection.executed, [])

    def test_change_password_updates_hash(self):
        connection = FakeConnection()
        with self.use_connection(connection):
            response = self.client.post('/auth/change-password',
                                        json={'current_password': 'Actual123', 'new_password': 'NuevaClave123'})
        self.assertEqual(response.status_code, 200, response.text)
        query, params = connection.executed[0]
        self.assertIn('password_hash=%s', query)
        self.assertTrue(verify_password('NuevaClave123', params[0]))
        self.assertEqual(params[1], self.user['id'])
        self.assertEqual(connection.commits, 1)

    def test_change_password_rejects_same_password(self):
        connection = FakeConnection()
        with self.use_connection(connection):
            response = self.client.post('/auth/change-password',
                                        json={'current_password': 'Actual123', 'new_password': 'Actual123'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(connection.executed, [])

    def test_get_preferences_returns_defaults_without_row(self):
        with self.use_connection(FakeConnection(None)):
            response = self.client.get('/auth/me/preferences')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'low_stock': True, 'capacity': True, 'temperature': True,
                                           'offline': True, 'sound': False})

    def test_get_preferences_returns_stored_values(self):
        row = {'low_stock': False, 'capacity': True, 'temperature': False, 'offline': True, 'sound': True}
        with self.use_connection(FakeConnection(row)):
            response = self.client.get('/auth/me/preferences')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), row)

    def test_put_preferences_upserts_and_returns_values(self):
        row = {'low_stock': True, 'capacity': False, 'temperature': True, 'offline': False, 'sound': True}
        connection = FakeConnection(row)
        with self.use_connection(connection):
            response = self.client.put('/auth/me/preferences', json=row)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), row)
        query, params = connection.executed[0]
        self.assertIn('ON CONFLICT(user_id) DO UPDATE', query)
        self.assertEqual(params, (self.user['id'], True, False, True, False, True))
        self.assertEqual(connection.commits, 1)

    def test_put_preferences_requires_all_fields(self):
        connection = FakeConnection()
        with self.use_connection(connection):
            response = self.client.put('/auth/me/preferences', json={'low_stock': False})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(connection.executed, [])

    def test_pending_users_requires_approval_permission(self):
        self.user['role'] = 'employee'
        with self.use_connection(FakeConnection([])):
            response = self.client.get('/auth/pending')
        self.assertEqual(response.status_code, 403)

    def test_pending_users_lists_pending(self):
        self.user['role'] = 'owner'
        row = {'id': PENDING_ID, 'email': 'pendiente@selfiq.local', 'first_name': 'Pendiente',
               'last_name': 'Uno', 'phone_number': '123', 'role': 'employee', 'account_status': 'pending',
               'organization_id': self.user['organization_id'], 'branch_id': self.user['branch_id'],
               'created_at': datetime(2026, 1, 1, tzinfo=timezone.utc)}
        with self.use_connection(FakeConnection([row])):
            response = self.client.get('/auth/pending')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]['email'], 'pendiente@selfiq.local')

    def test_reject_user_marks_rejected(self):
        self.user['role'] = 'owner'
        row = {'id': PENDING_ID, 'account_status': 'pending', 'deactivated_at': None,
               'branch_id': self.user['branch_id'], 'organization_id': self.user['organization_id']}
        connection = FakeConnection(row)
        with self.use_connection(connection):
            response = self.client.post(f'/auth/reject/{PENDING_ID}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['message'], 'Cuenta rechazada')
        self.assertEqual(connection.commits, 1)


if __name__ == '__main__':
    unittest.main()

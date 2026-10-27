import unittest
from contextlib import contextmanager
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


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


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


if __name__ == '__main__':
    unittest.main()

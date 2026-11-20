import unittest
from contextlib import contextmanager
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from app import main
from app.security import verify_password
from app.telemetry import can_read_branch

ORG = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
ORG_B = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
USER = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
BRANCH = UUID('dddddddd-dddd-4ddd-8ddd-dddddddddddd')


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
    def __init__(self, results=None, error=None, error_at=None):
        self.results = list(results or [])
        self.error = error
        self.error_at = error_at
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if self.error and (self.error_at is None or len(self.executed) == self.error_at):
            raise self.error
        result = self.results.pop(0) if self.results else None
        return FakeCursor(result)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class CustomerRegisterTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.previous_slug = main.CUSTOMER_ORGANIZATION_SLUG
        main.CUSTOMER_ORGANIZATION_SLUG = ''

    def tearDown(self):
        main.CUSTOMER_ORGANIZATION_SLUG = self.previous_slug
        main.app.dependency_overrides.clear()

    def use_connection(self, connection):
        @contextmanager
        def get_connection():
            yield connection
        return patch.object(main.db, 'get_connection', get_connection)

    def payload(self, **overrides):
        data = {
            'first_name': 'Nuevo',
            'last_name': 'Cliente',
            'email': 'Nuevo@SelfIQ.test',
            'password': 'ClaveSegura123',
        }
        data.update(overrides)
        return data

    def test_creates_active_customer_without_branch(self):
        connection = FakeConnection(results=[[{'id': ORG}], {'id': USER}])
        with self.use_connection(connection):
            response = self.client.post('/auth/customer-register', json=self.payload())
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body['user_id'], str(USER))
        self.assertEqual(body['account_status'], 'ACTIVE')
        self.assertIn('ya puedes iniciar sesión', body['message'])
        insert_query, params = connection.executed[1]
        self.assertIn("'customer','active'", insert_query)
        self.assertIn('NULL', insert_query)
        self.assertEqual(params[0], ORG)
        self.assertEqual(params[1], 'nuevo@selfiq.test')
        self.assertTrue(verify_password('ClaveSegura123', params[5]))
        self.assertEqual(connection.commits, 1)

    def test_rejects_role_field_instead_of_creating_owner(self):
        connection = FakeConnection(results=[[{'id': ORG}], {'id': USER}])
        with self.use_connection(connection):
            response = self.client.post('/auth/customer-register',
                                        json=self.payload(role='owner'))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(connection.executed, [])

    def test_rejects_short_password(self):
        connection = FakeConnection(results=[[{'id': ORG}]])
        with self.use_connection(connection):
            response = self.client.post('/auth/customer-register', json=self.payload(password='corta'))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(connection.executed, [])

    def test_rejects_duplicate_email(self):
        connection = FakeConnection(results=[[{'id': ORG}]], error=UniqueViolation('duplicate key'),
                                    error_at=2)
        with self.use_connection(connection):
            response = self.client.post('/auth/customer-register', json=self.payload())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail'], 'El correo ya está registrado')
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)

    def test_requires_organization_when_more_than_one_is_active(self):
        connection = FakeConnection(results=[[{'id': ORG}, {'id': ORG_B}]])
        with self.use_connection(connection):
            response = self.client.post('/auth/customer-register', json=self.payload())
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('organization_slug', response.json()['detail'])
        self.assertEqual(len(connection.executed), 1)

    def test_uses_requested_organization_slug(self):
        connection = FakeConnection(results=[{'id': ORG}, {'id': USER}])
        with self.use_connection(connection):
            response = self.client.post('/auth/customer-register',
                                        json=self.payload(organization_slug='tienda-centro'))
        self.assertEqual(response.status_code, 201, response.text)
        query, params = connection.executed[0]
        self.assertIn('slug=%s', query)
        self.assertEqual(params, ('tienda-centro',))

    def test_uses_configured_organization_slug(self):
        main.CUSTOMER_ORGANIZATION_SLUG = 'tienda-configurada'
        connection = FakeConnection(results=[{'id': ORG}, {'id': USER}])
        with self.use_connection(connection):
            response = self.client.post('/auth/customer-register', json=self.payload())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(connection.executed[0][1], ('tienda-configurada',))

    def test_fails_when_no_active_organization_exists(self):
        connection = FakeConnection(results=[[]])
        with self.use_connection(connection):
            response = self.client.post('/auth/customer-register', json=self.payload())
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(len(connection.executed), 1)

    def test_customer_without_branch_cannot_read_staff_telemetry(self):
        user = {'role': 'customer', 'organization_id': ORG, 'branch_id': None, 'is_superuser': False}
        branch = {'id': BRANCH, 'organization_id': ORG}
        self.assertFalse(can_read_branch(user, branch))


if __name__ == '__main__':
    unittest.main()

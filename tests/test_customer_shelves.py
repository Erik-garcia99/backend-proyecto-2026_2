import unittest
from contextlib import contextmanager
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from app import main

ORG = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
BRANCH = UUID('dddddddd-dddd-4ddd-8ddd-dddddddddddd')
USER = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
SHELF = UUID('22222222-2222-4222-8222-222222222222')


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


class CustomerShelvesTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.token = None

    def use_connection(self, connection):
        @contextmanager
        def get_connection():
            yield connection
        return patch.object(main.db, 'get_connection', get_connection)

    def auth_header(self):
        def current_user():
            return {'id': USER, 'role': 'customer', 'organization_id': ORG,
                    'branch_id': None, 'is_superuser': False}
        main.app.dependency_overrides[main.current_user] = current_user
        return {'Authorization': 'Bearer fake'}

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_rejects_staff_role(self):
        def current_user():
            return {'id': USER, 'role': 'guest', 'organization_id': ORG,
                    'branch_id': BRANCH, 'is_superuser': False}
        main.app.dependency_overrides[main.current_user] = current_user
        connection = FakeConnection(results=[[{'id': BRANCH, 'name': 'Sucursal', 'organization_id': ORG}]])
        with self.use_connection(connection):
            response = self.client.get(f'/customer/branches/{BRANCH}/shelves', headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.status_code, 403, response.text)

    def test_returns_404_for_inactive_branch(self):
        self.auth_header()
        connection = FakeConnection(results=[None])
        with self.use_connection(connection):
            response = self.client.get(f'/customer/branches/{BRANCH}/shelves', headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.status_code, 404, response.text)

    def test_returns_403_for_other_organization(self):
        self.auth_header()
        other_org = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
        connection = FakeConnection(results=[[{'id': BRANCH, 'name': 'Sucursal', 'organization_id': other_org}]])
        with self.use_connection(connection):
            response = self.client.get(f'/customer/branches/{BRANCH}/shelves', headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.status_code, 403, response.text)

    def test_returns_shelves_with_counts(self):
        self.auth_header()
        connection = FakeConnection(results=[
            [{'id': BRANCH, 'name': 'Sucursal Centro', 'organization_id': ORG}],  # require_branch_actor
            [{'total': 1}],  # count shelves
            [{'id': SHELF, 'name': 'Estante A1', 'location_label': 'Pasillo 2'}],  # shelves
            {'c': 3},  # product count
            {'c': 1},  # guide count
        ])
        with self.use_connection(connection):
            response = self.client.get(f'/customer/branches/{BRANCH}/shelves?page=1&page_size=5', headers={'Authorization': 'Bearer fake'})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['page_size'], 5)
        self.assertEqual(len(data['items']), 1)
        item = data['items'][0]
        self.assertEqual(item['name'], 'Estante A1')
        self.assertEqual(item['location'], 'Pasillo 2')
        self.assertEqual(item['product_count'], 3)
        self.assertEqual(item['guide_count'], 1)


if __name__ == '__main__':
    unittest.main()
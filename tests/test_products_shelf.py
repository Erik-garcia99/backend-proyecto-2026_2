import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import ANY, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from app import devices

BRANCH = UUID('11111111-1111-4111-8111-111111111111')
OTHER_BRANCH = UUID('22222222-2222-4222-8222-222222222222')
ORG = UUID('33333333-3333-4333-8333-333333333333')
DEVICE = UUID('44444444-4444-4444-8444-444444444444')
OTHER_DEVICE = UUID('55555555-5555-4555-8555-555555555555')
SHELF = UUID('66666666-6666-4666-8666-666666666666')


def result(row=None, rows=None):
    return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: rows or [])


class FakeConnection:
    def __init__(self, handler):
        self.handler = handler
        self.commits = 0
        self.rollbacks = 0
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self.handler(sql, params)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def product_row(**overrides):
    row = {
        'id': UUID('77777777-7777-4777-8777-777777777777'),
        'sku': 'SKU-1',
        'name': 'Leche entera',
        'is_active': True,
    }
    row.update(overrides)
    return row


def shelf_row(**overrides):
    row = {
        'id': SHELF,
        'branch_id': BRANCH,
        'camera_node_id': None,
        'camera_node_type': 'shelf_cam',
        'name': 'Repisa 1',
        'location_label': 'Piso 1',
        'is_active': True,
        'roi_version': 2,
        'roi_applied_version': 1,
        'roi_applied_at': None,
    }
    row.update(overrides)
    return row


class CatalogBase(unittest.TestCase):
    def setUp(self):
        self.user = {
            'id': UUID('88888888-8888-4888-8888-888888888888'),
            'role': 'manager',
            'is_superuser': False,
            'organization_id': ORG,
            'branch_id': BRANCH,
        }
        self.require_branch = patch.object(
            devices, 'require_branch', return_value={'id': BRANCH, 'organization_id': ORG}
        ).start()
        self.addCleanup(patch.stopall)

    def client(self, connection):
        @contextmanager
        def connect():
            yield connection

        app = FastAPI()
        app.include_router(devices.create_router(lambda: self.user, connect, SimpleNamespace()))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client


class ProductTests(CatalogBase):
    def test_lists_active_products_of_the_organization_with_search(self):
        connection = FakeConnection(lambda sql, params: result(rows=[product_row()]))
        response = self.client(connection).get(
            f'/branches/{BRANCH}/products?search= leche &limit=5')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{
            'id': '77777777-7777-4777-8777-777777777777',
            'sku': 'SKU-1',
            'name': 'Leche entera',
            'active': True,
        }])
        _, params = connection.calls[-1]
        self.assertEqual(params, (ORG, '%leche%', '%leche%', '%leche%', 5))

    def test_blank_search_does_not_filter(self):
        connection = FakeConnection(lambda sql, params: result(rows=[]))
        response = self.client(connection).get(f'/branches/{BRANCH}/products?search=   ')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(connection.calls[-1][1], (ORG, None, None, None, 20))

    def test_creates_a_trimmed_product(self):
        created = product_row(sku='SKU-9', name='Producto nuevo')

        def handler(sql, params):
            if 'SELECT 1 FROM product' in sql:
                return result(row=None)
            if sql.startswith('INSERT INTO product'):
                return result(row=created)
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).post(
            f'/branches/{BRANCH}/products',
            json={'sku': '  SKU-9  ', 'name': '  Producto nuevo  '})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['sku'], 'SKU-9')
        self.assertEqual(connection.commits, 1)
        insert = next(item for item in connection.calls if item[0].startswith('INSERT INTO product'))
        self.assertEqual(insert[1], (ORG, 'SKU-9', 'Producto nuevo'))

    def test_rejects_duplicate_sku_without_inserting(self):
        connection = FakeConnection(lambda sql, params: result(row={'?column?': 1}))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/products', json={'sku': 'SKU-1', 'name': 'Otra'})

        self.assertEqual(response.status_code, 409)
        self.assertIn('SKU', response.json()['detail'])
        self.assertEqual(connection.commits, 0)

    def test_duplicate_race_is_reported_as_conflict(self):
        def handler(sql, params):
            if 'SELECT 1 FROM product' in sql:
                return result(row=None)
            raise UniqueViolation('duplicate')

        connection = FakeConnection(handler)
        response = self.client(connection).post(
            f'/branches/{BRANCH}/products', json={'sku': 'SKU-1', 'name': 'Otra'})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)

    def test_rejects_blank_or_too_long_fields(self):
        connection = FakeConnection(lambda sql, params: result(rows=[]))
        client = self.client(connection)

        for body in ({'sku': '   ', 'name': 'Producto'},
                     {'sku': 'SKU', 'name': '   '},
                     {'sku': 'x' * 81, 'name': 'Producto'}):
            self.assertEqual(client.post(f'/branches/{BRANCH}/products', json=body).status_code, 422)
        self.assertFalse(connection.calls)

    def test_limit_is_bounded(self):
        connection = FakeConnection(lambda sql, params: result(rows=[]))
        client = self.client(connection)

        self.assertEqual(client.get(f'/branches/{BRANCH}/products?limit=0').status_code, 422)
        self.assertEqual(client.get(f'/branches/{BRANCH}/products?limit=101').status_code, 422)


class ShelfTests(CatalogBase):
    def setUp(self):
        super().setUp()
        self.load_node = patch.object(
            devices, 'load_node', return_value={'id': DEVICE, 'node_type': 'shelf_cam'}
        ).start()
        self.addCleanup(self.load_node.stop)

    def test_returns_null_when_camera_has_no_shelf(self):
        connection = FakeConnection(lambda sql, params: result(row=None))
        response = self.client(connection).get(
            f'/branches/{BRANCH}/devices/{DEVICE}/shelf')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'shelf': None})
        self.load_node.assert_called_with(
            ANY, BRANCH, DEVICE, node_type='shelf_cam')

    def test_returns_versions_of_the_linked_shelf(self):
        connection = FakeConnection(lambda sql, params: result(row=shelf_row()))
        response = self.client(connection).get(
            f'/branches/{BRANCH}/devices/{DEVICE}/shelf')

        self.assertEqual(response.status_code, 200)
        shelf = response.json()['shelf']
        self.assertEqual(shelf['id'], str(SHELF))
        self.assertEqual(shelf['roiVersion'], 2)
        self.assertEqual(shelf['roiAppliedVersion'], 1)
        self.assertEqual(shelf['cameraNodeId'], None)

    def test_links_an_existing_shelf_from_the_same_branch(self):
        shelf = shelf_row()

        def handler(sql, params):
            if 'FOR UPDATE' in sql:
                return result(row=shelf)
            if 'id<>%s' in sql:
                return result(row=None)
            if sql.startswith('UPDATE shelf'):
                return result(row=shelf_row(camera_node_id=DEVICE))
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).put(
            f'/branches/{BRANCH}/devices/{DEVICE}/shelf',
            json={'shelf_id': str(SHELF)})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['cameraNodeId'], str(DEVICE))
        self.assertEqual(connection.commits, 1)

    def test_rejects_shelf_already_used_by_another_camera(self):
        def handler(sql, params):
            if 'FOR UPDATE' in sql:
                return result(row=shelf_row(camera_node_id=OTHER_DEVICE))
            if 'id<>%s' in sql:
                return result(row=None)
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).put(
            f'/branches/{BRANCH}/devices/{DEVICE}/shelf',
            json={'shelf_id': str(SHELF)})

        self.assertEqual(response.status_code, 409)
        self.assertIn('otra cámara', response.json()['detail'])
        self.assertEqual(connection.commits, 0)

    def test_rejects_replacing_a_shelf_already_linked_to_the_camera(self):
        def handler(sql, params):
            if 'FOR UPDATE' in sql:
                return result(row=shelf_row())
            if 'id<>%s' in sql:
                return result(row={'id': SHELF, 'name': 'Repisa actual'})
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).put(
            f'/branches/{BRANCH}/devices/{DEVICE}/shelf',
            json={'shelf_id': str(SHELF)})

        self.assertEqual(response.status_code, 409)
        self.assertIn('ya tiene una repisa', response.json()['detail'])
        self.assertEqual(connection.commits, 0)

    def test_creates_and_links_a_shelf(self):
        created = shelf_row(camera_node_id=DEVICE, roi_version=0, roi_applied_version=0)

        def handler(sql, params):
            if sql.startswith('SELECT * FROM shelf WHERE camera_node_id'):
                return result(row=None)
            if sql.startswith('INSERT INTO shelf'):
                return result(row=created)
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).post(
            f'/branches/{BRANCH}/devices/{DEVICE}/shelf',
            json={'name': '  Repisa nueva  ', 'location_label': '  Piso 2  '})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['cameraNodeId'], str(DEVICE))
        self.assertEqual(connection.commits, 1)
        insert = next(item for item in connection.calls if item[0].startswith('INSERT INTO shelf'))
        self.assertEqual(insert[1], (BRANCH, DEVICE, 'Repisa nueva', 'Piso 2'))

    def test_create_is_idempotent_when_the_camera_already_has_a_shelf(self):
        connection = FakeConnection(lambda sql, params: result(
            row=shelf_row(camera_node_id=DEVICE)))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/devices/{DEVICE}/shelf',
            json={'name': 'Otra repisa'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['id'], str(SHELF))
        self.assertEqual(connection.commits, 0)


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.user = {
            'id': UUID('99999999-9999-4999-8999-999999999999'),
            'role': 'manager',
            'is_superuser': False,
            'organization_id': ORG,
            'branch_id': BRANCH,
        }
        self.addCleanup(patch.stopall)

    def client(self, connection):
        @contextmanager
        def connect():
            yield connection

        app = FastAPI()
        app.include_router(devices.create_router(lambda: self.user, connect, SimpleNamespace()))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_manager_of_another_branch_is_rejected(self):
        self.user['branch_id'] = OTHER_BRANCH

        def handler(sql, params):
            if 'SELECT b.id, b.organization_id FROM branch b' in sql:
                return result(row={'id': BRANCH, 'organization_id': ORG})
            raise AssertionError(sql)

        response = self.client(FakeConnection(handler)).get(
            f'/branches/{BRANCH}/products')
        self.assertEqual(response.status_code, 403)

    def test_employee_is_rejected(self):
        self.user['role'] = 'employee'

        def handler(sql, params):
            if 'SELECT b.id, b.organization_id FROM branch b' in sql:
                return result(row={'id': BRANCH, 'organization_id': ORG})
            raise AssertionError(sql)

        response = self.client(FakeConnection(handler)).post(
            f'/branches/{BRANCH}/products', json={'sku': 'SKU', 'name': 'Producto'})
        self.assertEqual(response.status_code, 403)

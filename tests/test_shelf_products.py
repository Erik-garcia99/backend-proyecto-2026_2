import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import devices, shelf_products

BRANCH = UUID('11111111-1111-4111-8111-111111111111')
SHELF = UUID('22222222-2222-4222-8222-222222222222')
PRODUCT = UUID('33333333-3333-4333-8333-333333333333')
USER = UUID('44444444-4444-4444-8444-444444444444')


def result(row=None, rows=None):
    return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: rows or [])


class FakeConnection:
    def __init__(self, handler):
        self.handler = handler
        self.commits = 0

    def execute(self, sql, params=()):
        return self.handler(sql, params)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class ShelfProductsTests(unittest.TestCase):
    def setUp(self):
        self.user = {'id': USER, 'role': 'owner', 'is_superuser': False}
        patcher = patch.object(shelf_products, 'require_branch_actor',
                               return_value={'id': BRANCH, 'organization_id': UUID(int=9)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, connection):
        @contextmanager
        def connect():
            yield connection

        app = FastAPI()
        app.include_router(shelf_products.create_router(lambda: self.user, connect))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_create_shelf_without_camera(self):
        row = {'id': SHELF, 'name': 'Repisa A', 'location_label': 'Pasillo 1',
               'camera_node_id': None, 'is_active': True}
        connection = FakeConnection(lambda sql, params: result(row=row))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/shelves', json={'name': 'Repisa A', 'location_label': 'Pasillo 1'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['id'], str(SHELF))
        self.assertIsNone(response.json()['cameraNodeId'])

    def test_products_report_physical_configuration(self):
        rows = [{'id': PRODUCT, 'sku': 'SKU-1', 'name': 'Leche', 'physical_configured': True}]
        def handler(sql, params):
            if 'FROM shelf ' in sql:
                return result(row={'id': SHELF})
            return result(rows=rows)

        connection = FakeConnection(handler)
        response = self.client(connection).get(f'/branches/{BRANCH}/shelves/{SHELF}/products')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()[0]['physicalConfigured'])

    def test_delete_shelf_rejects_linked_zones(self):
        linked = {'total': 1}
        connection = FakeConnection(lambda sql, params: result(
            row={'id': SHELF} if 'FROM shelf' in sql else linked))
        response = self.client(connection).delete(f'/branches/{BRANCH}/shelves/{SHELF}')
        self.assertEqual(response.status_code, 409, response.text)

    def test_delete_shelf_deactivates_without_associations(self):
        connection = FakeConnection(lambda sql, params: result(
            row={'id': SHELF} if 'FROM shelf' in sql else {'total': 0}))
        response = self.client(connection).delete(f'/branches/{BRANCH}/shelves/{SHELF}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(connection.commits, 1)


class CameraGateTests(unittest.TestCase):
    def test_unconfigured_product_blocks_camera_link(self):
        rows = [{'name': 'Leche', 'sku': 'SKU-1', 'ready': False}]
        connection = FakeConnection(lambda sql, params: result(rows=rows))
        with self.assertRaises(HTTPException) as raised:
            devices.require_configured_shelf_products(connection, BRANCH, SHELF)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn('Leche', raised.exception.detail)

    def test_configured_product_allows_camera_link(self):
        rows = [{'name': 'Leche', 'sku': 'SKU-1', 'ready': True}]
        connection = FakeConnection(lambda sql, params: result(rows=rows))
        devices.require_configured_shelf_products(connection, BRANCH, SHELF)


if __name__ == '__main__':
    unittest.main()

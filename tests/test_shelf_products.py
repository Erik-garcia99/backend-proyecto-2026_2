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
LIGHT = UUID('55555555-5555-4555-8555-555555555555')
NODE = UUID('66666666-6666-4666-8666-666666666666')


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

    def test_read_shelf_includes_lights_and_their_products(self):
        def handler(sql, params):
            if 'FROM shelf s' in sql:
                return result(row={'id': SHELF, 'name': 'Repisa A', 'location_label': 'Pasillo 1',
                                   'camera_node_id': None, 'is_active': True, 'camera_name': None})
            if 'FROM shelf_product sp' in sql:
                return result(rows=[])
            if 'FROM led_guide_point' in sql:
                return result(rows=[{'id': LIGHT, 'name': 'Luz 1', 'led_index': 0, 'node_id': NODE}])
            if 'FROM guide_product_light' in sql:
                return result(rows=[{'guide_point_id': LIGHT, 'id': PRODUCT,
                                     'name': 'Leche', 'sku': 'SKU-1'}])
            raise AssertionError(sql)

        response = self.client(FakeConnection(handler)).get(
            f'/branches/{BRANCH}/shelves/{SHELF}')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body['cameraName'])
        self.assertEqual(body['lights'][0]['channel'], 0)
        self.assertEqual(body['lights'][0]['products'],
                         [{'id': str(PRODUCT), 'name': 'Leche', 'sku': 'SKU-1'}])

    def test_products_report_inactive_instead_of_hiding_them(self):
        rows = [{'id': PRODUCT, 'sku': 'SKU-1', 'name': 'Leche',
                 'is_active': False, 'physical_configured': True}]
        def handler(sql, params):
            if 'FROM shelf ' in sql:
                return result(row={'id': SHELF})
            return result(rows=rows)
        connection = FakeConnection(handler)
        response = self.client(connection).get(f'/branches/{BRANCH}/shelves/{SHELF}/products')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()[0]['active'])

    def test_remove_product_blocks_when_a_light_points_to_it(self):
        def handler(sql, params):
            if 'FROM guide_product_light' in sql:
                return result(row={'1': 1})
            raise AssertionError(sql)
        response = self.client(FakeConnection(handler)).delete(
            f'/branches/{BRANCH}/shelves/{SHELF}/products/{PRODUCT}')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('LED asociado', response.json()['detail'])

    def test_remove_product_accepts_explicit_light_confirmation(self):
        def handler(sql, params):
            if 'FROM guide_product_light' in sql:
                return result(row={'1': 1})
            return result(row=None)
        connection = FakeConnection(handler)
        response = self.client(connection).delete(
            f'/branches/{BRANCH}/shelves/{SHELF}/products/{PRODUCT}?with_light=true')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(connection.commits, 1)

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

    def test_shelf_without_products_blocks_camera_link(self):
        connection = FakeConnection(lambda sql, params: result(rows=[]))
        with self.assertRaises(HTTPException) as raised:
            devices.require_configured_shelf_products(connection, BRANCH, SHELF)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn('no tiene productos', raised.exception.detail)

    def test_configured_product_allows_camera_link(self):
        rows = [{'name': 'Leche', 'sku': 'SKU-1', 'ready': True}]
        connection = FakeConnection(lambda sql, params: result(rows=rows))
        devices.require_configured_shelf_products(connection, BRANCH, SHELF)


if __name__ == '__main__':
    unittest.main()

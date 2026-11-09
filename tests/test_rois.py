import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import rois

BRANCH = UUID('11111111-1111-4111-8111-111111111111')
OTHER_BRANCH = UUID('22222222-2222-4222-8222-222222222222')
ORG = UUID('33333333-3333-4333-8333-333333333333')
DEVICE = UUID('44444444-4444-4444-8444-444444444444')
SHELF = UUID('66666666-6666-4666-8666-666666666666')
PRODUCT = UUID('77777777-7777-4777-8777-777777777777')
OTHER_PRODUCT = UUID('88888888-8888-4888-8888-888888888888')
ROI_1 = UUID('99999999-9999-4999-8999-999999999999')
ROI_2 = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
APPLIED_AT = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def result(row=None, rows=None):
    return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: rows or [])


class FakeConnection:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self.handler(sql, params)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def shelf_row(**overrides):
    row = {
        'id': SHELF,
        'branch_id': BRANCH,
        'camera_node_id': DEVICE,
        'camera_node_type': 'shelf_cam',
        'name': 'Repisa 1',
        'is_active': True,
        'roi_version': 3,
        'roi_applied_version': 2,
        'roi_applied_at': None,
        'rpi_identifier': 'rpi-1',
    }
    row.update(overrides)
    return row


def roi_row(**overrides):
    row = {
        'id': ROI_1,
        'shelf_id': SHELF,
        'product_id': PRODUCT,
        'name': 'Fila 1',
        'bbox_x': Decimal('0.10'),
        'bbox_y': Decimal('0.20'),
        'bbox_w': Decimal('0.30'),
        'bbox_h': Decimal('0.25'),
        'visual_capacity_units': 12,
        'is_active': True,
        'sku': 'SKU-1',
        'product_name': 'Leche entera',
    }
    row.update(overrides)
    return row


def body(*items, base_version=3):
    return {'base_version': base_version, 'rois': list(items)}


def item(**overrides):
    value = {
        'id': str(ROI_1),
        'product_id': str(PRODUCT),
        'name': 'Fila 1',
        'x': 0.10,
        'y': 0.20,
        'w': 0.30,
        'h': 0.25,
        'visual_capacity_units': 12,
    }
    value.update(overrides)
    return value


class RoiBase(unittest.TestCase):
    def setUp(self):
        self.user = {
            'id': UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'),
            'role': 'manager',
            'is_superuser': False,
            'organization_id': ORG,
            'branch_id': BRANCH,
        }
        self.require_branch = patch.object(
            rois, 'require_branch', return_value={'id': BRANCH, 'organization_id': ORG}
        ).start()
        self.addCleanup(patch.stopall)
        self.hub = SimpleNamespace(
            send=AsyncMock(return_value={'ok': True, 'data': {'ok': True}}))

    def client(self, connection):
        @contextmanager
        def connect():
            yield connection

        app = FastAPI()
        app.include_router(rois.create_router(lambda: self.user, connect, self.hub))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client


class ValidationTests(unittest.TestCase):
    def make(self, **overrides):
        values = {'product_id': PRODUCT, 'name': 'Zona', 'x': 0.10, 'y': 0.10,
                  'w': 0.20, 'h': 0.20}
        values.update(overrides)
        return rois.RoiItem(**values)

    def test_accepts_eight_rois_and_rejects_nine(self):
        eight = [self.make(x=(index % 4) * 0.22, y=(index // 4) * 0.22,
                           w=0.10, h=0.10) for index in range(8)]
        rois.validate_rois(eight)
        with self.assertRaisesRegex(ValueError, 'Máximo 8'):
            rois.validate_rois(eight + [self.make(x=0.90, y=0.90, w=0.05, h=0.05)])

    def test_rejects_bbox_outside_the_image(self):
        for values in ({'x': -0.01}, {'y': -0.01},
                       {'x': 0.90, 'w': 0.20}, {'y': 0.90, 'h': 0.20}):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, 'se sale de la imagen'):
                    rois.validate_rois([self.make(**values)])

    def test_rejects_rois_smaller_than_the_minimum_side(self):
        for values in ({'w': 0.029}, {'h': 0.029}):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, 'se sale de la imagen'):
                    rois.validate_rois([self.make(**values)])

    def test_rejects_non_finite_geometry(self):
        with self.assertRaisesRegex(ValueError, 'se sale de la imagen'):
            rois.validate_rois([self.make(x=float('nan'))])

    def test_rejects_overlap_above_two_percent_of_the_smallest(self):
        with self.assertRaisesRegex(ValueError, 'se solapan'):
            rois.validate_rois([self.make(), self.make(name='Zona 2')])

    def test_allows_shared_borders(self):
        rois.validate_rois([
            self.make(x=0.10, y=0.10, w=0.20, h=0.20),
            self.make(x=0.30, y=0.10, w=0.20, h=0.20),
        ])

    def test_allows_small_overlap(self):
        rois.validate_rois([
            self.make(x=0.0, y=0.0, w=0.50, h=0.50),
            self.make(x=0.495, y=0.0, w=0.50, h=0.50),
        ])

    def test_rejects_duplicate_ids(self):
        with self.assertRaisesRegex(ValueError, 'repetidas'):
            rois.validate_rois([
                self.make(id=ROI_1),
                self.make(id=ROI_1, x=0.50, y=0.50, w=0.10, h=0.10),
            ])

    def test_validates_name_and_capacity_in_the_model(self):
        with self.assertRaises(Exception):
            rois.RoiItem(product_id=PRODUCT, name='   ', x=0.1, y=0.1, w=0.2, h=0.2)
        with self.assertRaises(Exception):
            rois.RoiItem(product_id=PRODUCT, name='Zona', x=0.1, y=0.1, w=0.2, h=0.2,
                         visual_capacity_units=0)


class ListTests(RoiBase):
    def test_lists_active_rois_with_versions(self):
        def handler(sql, params):
            if 'FROM shelf s' in sql:
                return result(row=shelf_row())
            if 'FROM roi r JOIN product p' in sql:
                return result(rows=[roi_row()])
            raise AssertionError(sql)

        response = self.client(FakeConnection(handler)).get(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'version': 3,
            'applied_version': 2,
            'applied_at': None,
            'rois': [{
                'id': str(ROI_1),
                'product_id': str(PRODUCT),
                'product_sku': 'SKU-1',
                'product_name': 'Leche entera',
                'name': 'Fila 1',
                'x': 0.1,
                'y': 0.2,
                'w': 0.3,
                'h': 0.25,
                'visual_capacity_units': 12,
                'calibration_status': 'uncalibrated',
                'calibration_version': 0,
                'last_calibrated_at': None,
                'calibration_invalidated_reason': None,
            }],
        })

    def test_unknown_shelf_is_not_found(self):
        connection = FakeConnection(lambda sql, params: result(row=None))
        response = self.client(connection).get(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(connection.calls), 1)
        self.assertEqual(connection.calls[0][1], (SHELF, BRANCH))

    def test_shelf_without_camera_is_conflict(self):
        connection = FakeConnection(
            lambda sql, params: result(row=shelf_row(camera_node_id=None)))
        response = self.client(connection).get(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois')

        self.assertEqual(response.status_code, 409)
        self.assertIn('cámara', response.json()['detail'])


class AccessTests(unittest.TestCase):
    def test_manager_of_another_branch_is_rejected(self):
        user = {
            'id': UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc'),
            'role': 'manager',
            'is_superuser': False,
            'organization_id': ORG,
            'branch_id': OTHER_BRANCH,
        }

        def handler(sql, params):
            if 'SELECT b.id, b.organization_id FROM branch b' in sql:
                return result(row={'id': BRANCH, 'organization_id': ORG})
            raise AssertionError(sql)

        @contextmanager
        def connect():
            yield FakeConnection(handler)

        app = FastAPI()
        app.include_router(rois.create_router(lambda: user, connect, SimpleNamespace()))
        with TestClient(app) as client:
            response = client.get(f'/branches/{BRANCH}/shelves/{SHELF}/rois')
        self.assertEqual(response.status_code, 403)


class ReplaceTests(RoiBase):
    def test_rejects_invalid_geometry_before_touching_the_database(self):
        connection = FakeConnection(lambda sql, params: result())
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois',
            json=body(item(x=0.90, w=0.20)))

        self.assertEqual(response.status_code, 422)
        self.assertFalse(connection.calls)

    def test_rejects_more_than_eight_rois(self):
        connection = FakeConnection(lambda sql, params: result())
        many = [item(id=None, x=(index % 4) * 0.22, y=(index // 4) * 0.22,
                     w=0.10, h=0.10) for index in range(9)]
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois', json=body(*many))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(connection.calls, [])

    def test_rejects_overlap(self):
        connection = FakeConnection(lambda sql, params: result())
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois',
            json=body(item(id=None), item(id=None, name='Fila 2')))

        self.assertEqual(response.status_code, 422)
        self.assertIn('solapan', response.json()['detail'])

    def test_rejects_product_from_another_organization(self):
        def handler(sql, params):
            if 'FROM shelf s' in sql:
                return result(row=shelf_row())
            if sql.startswith('SELECT id FROM product'):
                return result(rows=[])
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois', json=body(item()))

        self.assertEqual(response.status_code, 422)
        self.assertIn('producto', response.json()['detail'])
        self.assertEqual(connection.commits, 0)

    def test_stale_base_version_is_conflict(self):
        connection = FakeConnection(lambda sql, params: result(row=shelf_row(roi_version=3)))
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois',
            json=body(item(), base_version=2))

        self.assertEqual(response.status_code, 409)
        self.assertIn('recarga', response.json()['detail'])
        self.assertEqual(connection.commits, 0)

    def test_put_is_blocked_while_a_calibration_is_open(self):
        connection = FakeConnection(
            lambda sql, params: result(row=shelf_row(open_calibration_id=ROI_1)))
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois',
            json=body(item()))

        self.assertEqual(response.status_code, 409)
        self.assertIn('calibración abierta', response.json()['detail'])
        self.assertEqual(connection.commits, 0)

    def test_rejects_roi_id_from_another_shelf(self):
        def handler(sql, params):
            if 'FROM shelf s' in sql:
                return result(row=shelf_row())
            if sql.startswith('SELECT id FROM product'):
                return result(rows=[{'id': PRODUCT}])
            if sql.startswith('SELECT id FROM roi'):
                return result(rows=[])
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois', json=body(item()))

        self.assertEqual(response.status_code, 422)
        self.assertIn('no pertenece', response.json()['detail'])
        self.assertEqual(connection.commits, 0)

    def test_increments_version_once_and_preserves_applied_version(self):
        updated_rows = [roi_row(), roi_row(id=ROI_2, name='Fila 2', product_id=OTHER_PRODUCT,
                                           sku='SKU-2', product_name='Pan')]

        def handler(sql, params):
            if 'FROM shelf s' in sql:
                return result(row=shelf_row(roi_version=3, roi_applied_version=2))
            if sql.startswith('SELECT id FROM product'):
                return result(rows=[{'id': PRODUCT}, {'id': OTHER_PRODUCT}])
            if sql.startswith('SELECT id FROM roi'):
                return result(rows=[{'id': ROI_1}])
            if sql.startswith('UPDATE roi SET is_active=false'):
                return result()
            if sql.startswith('UPDATE roi SET product_id'):
                return result()
            if sql.startswith('INSERT INTO roi'):
                return result()
            if sql.startswith('UPDATE shelf SET roi_version'):
                return result(row={'roi_version': 4, 'roi_applied_version': 2})
            if 'FROM roi r JOIN product p' in sql:
                return result(rows=updated_rows)
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois',
            json=body(item(id=str(ROI_1)),
                      item(id=None, product_id=str(OTHER_PRODUCT), name='Fila 2',
                           x=0.50, y=0.20, w=0.30, h=0.25)))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['version'], 4)
        self.assertEqual(response.json()['applied_version'], 2)
        self.assertEqual(len(response.json()['rois']), 2)
        self.assertEqual(connection.commits, 1)

        version_updates = [call for call in connection.calls
                           if call[0].startswith('UPDATE shelf SET roi_version')]
        self.assertEqual(len(version_updates), 1)

        deactivation = next(call for call in connection.calls
                            if call[0].startswith('UPDATE roi SET is_active=false'))
        self.assertEqual(deactivation[1], (SHELF, [ROI_1]))

        updates = [call for call in connection.calls
                   if call[0].startswith('UPDATE roi SET product_id')]
        inserts = [call for call in connection.calls
                   if call[0].startswith('INSERT INTO roi')]
        self.assertEqual(len(updates), 1)
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][0], SHELF)

    def test_deactivates_rois_that_are_no_longer_sent(self):
        def handler(sql, params):
            if 'FROM shelf s' in sql:
                return result(row=shelf_row())
            if sql.startswith('SELECT id FROM product'):
                return result(rows=[{'id': PRODUCT}])
            if sql.startswith('SELECT id FROM roi'):
                return result(rows=[{'id': ROI_1}, {'id': ROI_2}])
            if sql.startswith('UPDATE roi SET is_active=false'):
                return result()
            if sql.startswith('UPDATE roi SET product_id'):
                return result()
            if sql.startswith('UPDATE shelf SET roi_version'):
                return result(row={'roi_version': 4, 'roi_applied_version': 3})
            if 'FROM roi r JOIN product p' in sql:
                return result(rows=[roi_row()])
            raise AssertionError(sql)

        connection = FakeConnection(handler)
        response = self.client(connection).put(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois', json=body(item()))

        self.assertEqual(response.status_code, 200)
        deactivation = next(call for call in connection.calls
                            if call[0].startswith('UPDATE roi SET is_active=false'))
        self.assertEqual(deactivation[1], (SHELF, [ROI_1]))


class ApplyTests(RoiBase):
    def handler(self, update_row='default'):
        def handle(sql, params):
            if 'FROM shelf s' in sql:
                return result(row=shelf_row())
            if 'FROM roi r JOIN product p' in sql:
                return result(rows=[roi_row()])
            if sql.startswith('UPDATE shelf SET roi_applied_version'):
                if update_row != 'default':
                    return result(row=update_row)
                return result(row={
                    'roi_applied_version': 3, 'roi_applied_at': APPLIED_AT})
            raise AssertionError(sql)
        return handle

    def test_ack_ok_confirms_and_updates_applied_version(self):
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois/apply', json={'version': 3})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['confirmed'])
        self.assertEqual(payload['version'], 3)
        self.assertEqual(payload['applied_version'], 3)
        self.assertEqual(payload['count'], 1)
        self.assertTrue(payload['req_id'])
        self.assertEqual(connection.commits, 1)

        self.hub.send.assert_awaited_once()
        gateway, command = self.hub.send.await_args.args[:2]
        sent = self.hub.send.await_args.kwargs['payload']
        self.assertEqual(gateway, 'rpi-1')
        self.assertEqual(command, 'roi.set')
        self.assertEqual(sent['node_id'], str(DEVICE))
        self.assertEqual(sent['branch_id'], str(BRANCH))
        self.assertEqual(sent['module'], 'shelf_cam')
        self.assertEqual(sent['version'], 3)
        self.assertEqual(sent['req_id'], payload['req_id'])
        self.assertEqual(sent['rois'], [{
            'id': str(ROI_1), 'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.25,
        }])
        update = next(call for call in connection.calls
                      if call[0].startswith('UPDATE shelf SET roi_applied_version'))
        self.assertEqual(update[1], (3, SHELF, 3))

    def test_node_rejection_is_bad_gateway(self):
        self.hub.send.return_value = {
            'ok': True, 'data': {'ok': False, 'error': 'invalid_bbox'}}
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois/apply', json={'version': 3})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['detail'], 'invalid_bbox')
        self.assertEqual(connection.commits, 0)

    def test_node_without_ack_is_gateway_timeout(self):
        self.hub.send.return_value = {
            'ok': True, 'data': {'ok': False, 'error': 'timeout esperando ACK'}}
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois/apply', json={'version': 3})

        self.assertEqual(response.status_code, 504)

    def test_gateway_down_is_service_unavailable(self):
        self.hub.send.side_effect = HTTPException(503, 'Raspberry desconectada')
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois/apply', json={'version': 3})

        self.assertEqual(response.status_code, 503)
        self.assertIn('No se pudo aplicar', response.json()['detail'])

    def test_stale_version_is_conflict_without_sending(self):
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois/apply', json={'version': 2})

        self.assertEqual(response.status_code, 409)
        self.hub.send.assert_not_awaited()

    def test_retry_does_not_duplicate_rois(self):
        connection = FakeConnection(self.handler())
        client = self.client(connection)

        first = client.post(f'/branches/{BRANCH}/shelves/{SHELF}/rois/apply',
                            json={'version': 3})
        second = client.post(f'/branches/{BRANCH}/shelves/{SHELF}/rois/apply',
                             json={'version': 3})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        writes = [sql for sql, _ in connection.calls
                  if sql.startswith(('INSERT INTO roi', 'UPDATE roi', 'DELETE'))]
        self.assertEqual(writes, [])

    def test_concurrent_put_during_apply_is_conflict(self):
        connection = FakeConnection(self.handler(update_row=None))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/shelves/{SHELF}/rois/apply', json={'version': 3})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)

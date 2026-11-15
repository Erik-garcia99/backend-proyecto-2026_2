import unittest
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.physical_config import ProductSpec, RoiConfig, create_router

B = UUID('11111111-1111-4111-8111-111111111111')
P = UUID('22222222-2222-4222-8222-222222222222')
R = UUID('33333333-3333-4333-8333-333333333333')
O = UUID('44444444-4444-4444-8444-444444444444')
SPEC = dict(base_version=0, width_mm=60, height_mm=120, depth_mm=60, usual_orientation='upright')
CONFIG = dict(base_version=0, expected_product_id=str(P), visual_capacity_units=6, threshold_empty_pct=5,
              threshold_low_pct=25, threshold_medium_pct=60, confirm_samples=3,
              virtual_slots_enabled=False, slots_rows=None, slots_cols=None)


class ValidationTests(unittest.TestCase):
    def test_valid_configs(self):
        ProductSpec(**SPEC)
        RoiConfig(**CONFIG)
        RoiConfig(**{**CONFIG, 'virtual_slots_enabled': True, 'slots_rows': 2, 'slots_cols': 3})

    def test_invalid_dimensions(self):
        for value in (0, -1, float('nan'), float('inf'), True, '30', 100001):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ProductSpec(**{**SPEC, 'width_mm': value})

    def test_invalid_orientation_and_extra_fields(self):
        for change in ({'usual_orientation': 'upside_down'}, {'geometry': [1, 2]}):
            with self.assertRaises(ValidationError):
                ProductSpec(**{**SPEC, **change})

    def test_threshold_order_and_bounds(self):
        for patch in ({'threshold_low_pct': 5}, {'threshold_medium_pct': 25},
                      {'threshold_empty_pct': -1}, {'threshold_medium_pct': 100},
                      {'threshold_low_pct': float('nan')}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                RoiConfig(**{**CONFIG, **patch})

    def test_slot_validation_and_no_hidden_grid_when_disabled(self):
        for patch in ({'slots_rows': 2}, {'virtual_slots_enabled': True},
                      {'virtual_slots_enabled': True, 'slots_rows': 16, 'slots_cols': 16},
                      {'confirm_samples': 1.2}, {'visual_capacity_units': 0},
                      {'virtual_slots_enabled': 'true'}, {'bbox_x': 0.2}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                RoiConfig(**{**CONFIG, **patch})


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.spec = None
        self.product = {'id': P, 'sku': 'SKU', 'name': 'Producto'}
        self.roi = {'id': R, 'product_id': P, 'name': 'Zona', 'shelf_name': 'Estante',
                    'physical_config_version': 0, **{k: v for k, v in CONFIG.items()
                                                    if k not in {'base_version', 'expected_product_id'}}}
        self.committed = False

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        row, rows = None, []
        if 'SELECT b.id' in sql:
            row = {'id': B, 'organization_id': O}
        elif 'SELECT id,sku,name FROM product' in sql:
            row = self.product
        elif 'SELECT * FROM product_physical_spec' in sql:
            row = self.spec
        elif 'INSERT INTO product_physical_spec' in sql:
            row = {k: v for k, v in SPEC.items() if k != 'base_version'}
            row['config_version'] = 1
        elif 'SELECT r.*' in sql:
            row = self.roi
            rows = [row] if row else []
        elif 'UPDATE roi SET' in sql:
            row = {**self.roi, 'physical_config_version': self.roi['physical_config_version'] + 1}
        result = Mock()
        result.fetchone.return_value = row
        result.fetchall.return_value = rows
        return result

    def commit(self):
        self.committed = True


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()
        self.user = {'id': P, 'role': 'manager', 'organization_id': O,
                     'branch_id': B, 'is_superuser': False}

        @contextmanager
        def connect():
            yield self.conn

        app = FastAPI()
        app.include_router(create_router(lambda: self.user, connect))
        self.client = TestClient(app)
        self.spec_path = f'/branches/{B}/products/{P}/physical-spec'
        self.roi_path = f'/branches/{B}/rois/{R}/physical-config'
        self.list_path = f'/branches/{B}/products/{P}/roi-configs'

    def test_unconfigured_dimensions_do_not_become_default_measurements(self):
        response = self.client.get(self.spec_path)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['spec']['width_mm'])
        self.assertEqual(response.json()['spec']['version'], 0)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_spec_save_is_versioned_and_audited(self):
        response = self.client.put(self.spec_path, json=SPEC)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['spec']['version'], 1)
        self.assertEqual(response.json()['application_status'], 'not_integrated')
        self.assertTrue(self.conn.committed)
        self.assertTrue(any('INSERT INTO system_log' in sql for sql, _ in self.conn.calls))

    def test_spec_conflict_does_not_write(self):
        self.conn.spec = {'config_version': 1}
        self.assertEqual(self.client.put(self.spec_path, json=SPEC).status_code, 409)
        self.assertFalse(self.conn.committed)
        self.assertFalse(any('INSERT INTO' in sql for sql, _ in self.conn.calls))

    def test_roi_save_never_touches_geometry_calibration_or_shelf_version(self):
        response = self.client.put(self.roi_path, json=CONFIG)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['config']['version'], 1)
        updates = '\n'.join(sql for sql, _ in self.conn.calls if 'UPDATE ' in sql)
        for forbidden in ('bbox_', 'calibration', 'roi_version', 'roi_applied_version', 'UPDATE shelf'):
            self.assertNotIn(forbidden, updates)
        self.assertTrue(self.conn.committed)

    def test_roi_conflict_and_missing(self):
        self.conn.roi['physical_config_version'] = 1
        self.assertEqual(self.client.put(self.roi_path, json=CONFIG).status_code, 409)
        self.conn.roi = None
        self.assertEqual(self.client.put(self.roi_path, json=CONFIG).status_code, 404)
        self.assertFalse(self.conn.committed)

    def test_remapped_roi_cannot_receive_previous_products_configuration(self):
        self.conn.roi['product_id'] = R
        self.assertEqual(self.client.put(self.roi_path, json=CONFIG).status_code, 409)
        self.assertFalse(self.conn.committed)

    def test_list_scope_and_application_status(self):
        response = self.client.get(self.list_path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['application_status'], 'not_integrated')
        sql, args = self.conn.calls[-1]
        self.assertIn('s.branch_id=%s', sql)
        self.assertEqual(args, (P, B))

    def test_no_association_returns_empty_list(self):
        self.conn.roi = None
        self.assertEqual(self.client.get(self.list_path).json(), [])

    def test_inactive_or_foreign_product_not_found(self):
        self.conn.product = None
        self.assertEqual(self.client.get(self.spec_path).status_code, 404)
        self.assertEqual(self.client.get(self.list_path).status_code, 404)

    def test_permissions_for_all_routes(self):
        for role in ('customer', 'employee', 'staff', 'unknown'):
            self.user['role'] = role
            for method, path, body in [('get', self.spec_path, None), ('get', self.list_path, None),
                                       ('put', self.spec_path, SPEC), ('put', self.roi_path, CONFIG)]:
                response = getattr(self.client, method)(path, **({'json': body} if body else {}))
                self.assertEqual(response.status_code, 403, (role, path))

    def test_cross_branch_and_organization_rejected(self):
        self.user['branch_id'] = R
        self.assertEqual(self.client.get(self.spec_path).status_code, 403)
        self.user.update(role='owner', organization_id=R)
        self.assertEqual(self.client.put(self.roi_path, json=CONFIG).status_code, 403)

    def test_invalid_payload_does_not_query_database(self):
        self.assertEqual(self.client.put(self.roi_path, json={**CONFIG, 'threshold_low_pct': 1}).status_code, 422)
        self.assertEqual(self.conn.calls, [])


if __name__ == '__main__':
    unittest.main()

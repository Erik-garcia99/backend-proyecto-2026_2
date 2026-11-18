import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.roi_classification import (Thresholds, candidate_status, classify_step,
                                    create_router)
from app.telemetry import RoiOccupancyResponse

B = UUID('11111111-1111-4111-8111-111111111111')
R = UUID('33333333-3333-4333-8333-333333333333')
P = UUID('22222222-2222-4222-8222-222222222222')
S = UUID('55555555-5555-4555-8555-555555555555')
N = UUID('66666666-6666-4666-8666-666666666666')
O = UUID('44444444-4444-4444-8444-444444444444')
NOW = datetime.now(timezone.utc)
T = Thresholds(5, 25, 60, 3, 1, 1)
CONFIG = dict(threshold_empty_pct=5, threshold_low_pct=25, threshold_medium_pct=60,
              confirm_samples=1, physical_config_version=1)


def occupancy(pct=None, *, status='ok', cam_moved=False, age=0):
    return RoiOccupancyResponse(
        branch_id=B, roi_id=R, occupancy_pct=pct, config_version=1, calibration_version=1,
        received_at=NOW - timedelta(seconds=age) if pct is not None else None,
        age_seconds=age, stale_after_seconds=60, status=status, cam_moved=cam_moved)


class PureClassificationTests(unittest.TestCase):
    def test_inclusive_non_overlapping_bands(self):
        for pct, expected in ((0, 'empty'), (5, 'empty'), (6, 'low'), (25, 'low'),
                              (26, 'medium'), (60, 'medium'), (61, 'adequate')):
            with self.subTest(pct=pct):
                self.assertEqual(candidate_status(pct, T), expected)

    def test_three_consecutive_samples_confirm(self):
        state = {'status': 'adequate'}
        first = classify_step(3, T, state)
        self.assertEqual(first['status'], 'adequate')
        self.assertEqual(first['pending_status'], 'empty')
        self.assertEqual(first['pending_samples'], 1)
        second = classify_step(3, T, first)
        self.assertEqual(second['pending_samples'], 2)
        self.assertEqual(second['status'], 'adequate')
        third = classify_step(3, T, second)
        self.assertEqual(third['status'], 'empty')
        self.assertIsNone(third['pending_status'])
        self.assertEqual(third['confidence'], 1.0)

    def test_different_candidate_restarts_counter(self):
        state = {'status': 'adequate', 'pending_status': 'empty', 'pending_samples': 2}
        result = classify_step(30, T, state)
        self.assertEqual(result['pending_status'], 'medium')
        self.assertEqual(result['pending_samples'], 1)

    def test_hysteresis_does_not_flap_at_boundary(self):
        # Estado adequate con 60 % no debe caer a medium solo por tocar el límite.
        self.assertEqual(candidate_status(60, T, current='adequate'), 'adequate')
        self.assertEqual(candidate_status(57, T, current='adequate'), 'medium')
        # Para salir de agotado hace falta rebasar el limite hacia arriba.
        self.assertEqual(candidate_status(6, T, current='empty'), 'empty')
        self.assertEqual(candidate_status(8, T, current='empty'), 'low')

    def test_isolated_jump_is_discarded_but_repeated_one_is_accepted(self):
        state = {'status': 'low', 'occupancy_pct': 50}
        first = classify_step(95, T, state, last_pct=50)
        self.assertTrue(first['discarded'])
        self.assertEqual(first['status'], 'low')
        second = classify_step(96, T, first, last_pct=50, last_discarded_pct=95)
        self.assertFalse(second['discarded'])
        self.assertEqual(second['candidate_status'], 'adequate')


class FakeConnection:
    def __init__(self, *, status='adequate', occupancy_pct=None, calibrated=True,
                 stored=None):
        self.roi = {'roi_id': R, 'product_id': P, 'roi_name': 'Zona', 'sku': 'SKU-1',
                    'shelf_id': S, 'shelf_name': 'Estante', 'camera_node_id': N,
                    'config_version': 1, 'calibration_version': 1 if calibrated else None,
                    'calibration_status': 'calibrated' if calibrated else 'uncalibrated',
                    'last_calibrated_at': NOW, **CONFIG}
        self.stored = stored or {'status': status, 'occupancy_pct': occupancy_pct,
                                 'pending_status': None, 'pending_samples': 0,
                                 'confidence': 1.0, 'last_changed_at': NOW, 'updated_at': NOW}
        self.calls, self.readings, self.committed = [], [], False

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        result = Mock()
        if 'SELECT b.id, b.organization_id FROM branch b' in sql:
            result.fetchone.return_value = {'id': B, 'organization_id': O}
        elif 'FROM roi r' in sql and 'LEFT JOIN roi_state' in sql:
            result.fetchall.return_value = [{**self.roi, **self.stored,
                                             'last_reading_at': self.stored.get('last_reading_at')}]
        elif 'FROM roi r' in sql:
            result.fetchone.return_value = self.roi
        elif 'SELECT * FROM roi_state WHERE roi_id' in sql:
            result.fetchone.return_value = self.stored
        elif 'FROM roi_reading WHERE roi_id' in sql:
            result.fetchall.return_value = []
        elif 'INSERT INTO roi_reading' in sql:
            self.readings.append(args)
        elif 'INSERT INTO roi_state' in sql:
            self.stored = {**self.stored, 'status': args[1],
                           'occupancy_pct': args[0] if args[0] is not None else self.stored['occupancy_pct']}
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

        self.serve = [occupancy(3)]
        def reader(_context):
            return self.serve[0]

        app = FastAPI()
        app.include_router(create_router(lambda: self.user, connect, reader))
        self.client = TestClient(app)
        self.state_path = f'/branches/{B}/rois/{R}/state'
        self.list_path = f'/branches/{B}/shelf-states'

    def test_ok_reading_persists_and_returns_state(self):
        response = self.client.get(self.state_path)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'empty')
        self.assertEqual(body['status_label'], 'Agotado')
        self.assertEqual(body['quality'], 'ok')
        self.assertEqual(body['occupancy_pct'], 3)
        self.assertEqual(body['confirm_samples'], 1)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertTrue(self.conn.committed)
        self.assertTrue(any('INSERT INTO roi_reading' in sql for sql, _ in self.conn.calls))
        self.assertTrue(any('INSERT INTO roi_state' in sql for sql, _ in self.conn.calls))

    def test_stale_keeps_last_status_but_reports_quality(self):
        self.conn.stored['status'] = 'low'
        self.serve[0] = occupancy(90, status='stale', age=120)
        body = self.client.get(self.state_path).json()
        self.assertEqual(body['status'], 'low')
        self.assertEqual(body['quality'], 'stale')
        self.assertEqual(body['cam_moved'], False)

    def test_no_data_does_not_insert_null_into_roi_reading(self):
        self.serve[0] = occupancy(None, status='no_data')
        body = self.client.get(self.state_path).json()
        self.assertEqual(body['quality'], 'no_data')
        self.assertEqual(body['occupancy_pct'], None)
        self.assertFalse(any('INSERT INTO roi_reading' in sql for sql, _ in self.conn.calls))

    def test_cam_moved_reports_camera_moved_without_changing_status(self):
        self.serve[0] = occupancy(90, cam_moved=True)
        body = self.client.get(self.state_path).json()
        self.assertEqual(body['quality'], 'camera_moved')
        self.assertTrue(body['cam_moved'])
        self.assertEqual(body['status'], 'adequate')

    def test_uncalibrated_is_unknown_not_empty(self):
        self.conn.roi['calibration_status'] = 'uncalibrated'
        self.conn.roi['calibration_version'] = None
        self.serve[0] = occupancy(0)
        body = self.client.get(self.state_path).json()
        self.assertEqual(body['status'], 'unknown')
        self.assertEqual(body['quality'], 'not_calibrated')

    def test_shelf_states_reads_persisted_state_without_influx(self):
        self.conn.stored['last_reading_at'] = NOW  # lectura fresca: no reclasifica
        self.serve[0] = None  # el listado no llama al lector
        response = self.client.get(self.list_path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]['status'], 'adequate')
        self.assertEqual(response.json()[0]['shelf_name'], 'Estante')

    def test_shelf_states_classifies_when_no_persisted_reading(self):
        self.conn.stored['last_reading_at'] = None
        self.serve[0] = occupancy(2)
        response = self.client.get(self.list_path)
        self.assertEqual(response.status_code, 200)
        body = response.json()[0]
        self.assertEqual(body['status'], 'empty')
        self.assertEqual(body['quality'], 'ok')
        self.assertEqual(body['occupancy_pct'], 2)
        self.assertTrue(any('INSERT INTO roi_state' in sql for sql, _ in self.conn.calls))

    def test_forbidden_user_is_403(self):
        self.user = {**self.user, 'role': 'employee', 'organization_id': UUID(int=9)}
        self.assertEqual(self.client.get(self.state_path).status_code, 403)


if __name__ == '__main__':
    unittest.main()

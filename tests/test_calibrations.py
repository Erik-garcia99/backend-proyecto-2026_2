import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import calibrations

BRANCH = UUID('11111111-1111-4111-8111-111111111111')
ORG = UUID('33333333-3333-4333-8333-333333333333')
USER = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
DEVICE = UUID('44444444-4444-4444-8444-444444444444')
SHELF = UUID('66666666-6666-4666-8666-666666666666')
PRODUCT = UUID('77777777-7777-4777-8777-777777777777')
ROI = UUID('99999999-9999-4999-8999-999999999999')
CAL = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
CAPTURE = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
IMAGE = UUID('dddddddd-dddd-4ddd-8ddd-dddddddddddd')

NOW = datetime(2026, 11, 8, 18, 2, 11, 123456, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(minutes=30)

EXPECTED_HASH = 'f8383492e91e018739fc428377a9d19b8471acef494a40d104908ad721722636'


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


def roi_row(**overrides):
    row = {
        'id': ROI,
        'shelf_id': SHELF,
        'branch_id': BRANCH,
        'product_id': PRODUCT,
        'name': 'Fila 1',
        'bbox_x': Decimal('0.10'),
        'bbox_y': Decimal('0.20'),
        'bbox_w': Decimal('0.30'),
        'bbox_h': Decimal('0.25'),
        'visual_capacity_units': 12,
        'calibration_status': 'uncalibrated',
        'calibration_version': 0,
        'active_calibration_id': None,
        'last_calibrated_at': None,
        'calibration_invalidated_reason': None,
        'sku': 'AGUA-600',
        'product_name': 'Agua 600 ml',
        'roi_version': 7,
        'roi_applied_version': 7,
        'camera_node_id': DEVICE,
        'camera_frame_width': 1600,
        'camera_frame_height': 1200,
        'camera_orientation': 0,
        'camera_visual_revision': 1,
        'rpi_identifier': 'rpi-1',
    }
    row.update(overrides)
    return row


def session_row(**overrides):
    row = {
        'id': CAL,
        'roi_id': ROI,
        'shelf_id': SHELF,
        'camera_node_id': DEVICE,
        'branch_id': BRANCH,
        'product_id': PRODUCT,
        'product_sku': 'AGUA-600',
        'product_name': 'Agua 600 ml',
        'context': {},
        'context_hash': EXPECTED_HASH,
        'config_version': 7,
        'calibration_version': 1,
        'descriptor_format': 1,
        'state': 'draft',
        'session_version': 1,
        'created_by': USER,
        'reason': None,
        'created_at': NOW,
        'updated_at': NOW,
        'expires_at': EXPIRES,
        'confirmed_at': None,
        'closed_at': None,
        'error_code': None,
    }
    row.update(overrides)
    return row


class PureTests(unittest.TestCase):
    def test_millionths_avoid_binary_noise(self):
        self.assertEqual(calibrations.millionths(Decimal('0.100000')), 100000)
        self.assertEqual(calibrations.millionths(0.3), 300000)
        self.assertEqual(calibrations.millionths(Decimal('0.000001')), 1)
        self.assertEqual(calibrations.millionths(1), 1000000)

    def test_context_is_canonical_and_has_a_fixed_hash(self):
        context = calibrations.build_context(roi_row())
        self.assertEqual(context, {
            'bbox': [100000, 200000, 300000, 250000],
            'config_version': 7,
            'format_version': 1,
            'height': 1200,
            'node_id': str(DEVICE),
            'orientation': 0,
            'product_id': str(PRODUCT),
            'roi_id': str(ROI),
            'sku': 'AGUA-600',
            'visual_revision': 1,
            'width': 1600,
        })
        self.assertEqual(calibrations.compute_context_hash(context), EXPECTED_HASH)

    def test_hash_does_not_depend_on_dictionary_order(self):
        first = {'b': 2, 'a': 1, 'nested': {'y': 2, 'x': 1}}
        second = {'nested': {'x': 1, 'y': 2}, 'a': 1, 'b': 2}
        self.assertEqual(
            calibrations.compute_context_hash(first),
            calibrations.compute_context_hash(second),
        )

    def test_sku_and_whitespace_normalisation(self):
        context = calibrations.build_context(roi_row(sku='  agua-600  '))
        self.assertEqual(context['sku'], 'AGUA-600')

    def test_rfc3339_always_has_milliseconds_and_z(self):
        self.assertEqual(
            calibrations.rfc3339(datetime(2026, 11, 8, 18, 2, 11, 123456, tzinfo=timezone.utc)),
            '2026-11-08T18:02:11.123Z',
        )
        self.assertEqual(
            calibrations.rfc3339(datetime(2026, 11, 8, 18, 2, 11, 0)),
            '2026-11-08T18:02:11.000Z',
        )
        self.assertIsNone(calibrations.rfc3339(None))

    def test_cursor_roundtrip_and_invalid_values(self):
        cursor = calibrations.encode_cursor(NOW, CAL)
        self.assertEqual(calibrations.decode_cursor(cursor), (NOW, CAL))
        self.assertIsNone(calibrations.decode_cursor(None))
        with self.assertRaises(calibrations.CalibrationError) as caught:
            calibrations.decode_cursor('%%%')
        self.assertEqual(caught.exception.code, 'invalid_request')

    def test_missing_idempotency_key_is_invalid_request(self):
        for value in (None, '', '   '):
            with self.subTest(value=value):
                with self.assertRaises(calibrations.CalibrationError) as caught:
                    calibrations.require_idempotency_key(value)
                self.assertEqual(caught.exception.status_code, 422)
                self.assertEqual(caught.exception.code, 'invalid_request')


class CalibrationBase(unittest.TestCase):
    def setUp(self):
        self.user = {
            'id': USER,
            'role': 'manager',
            'is_superuser': False,
            'organization_id': ORG,
            'branch_id': BRANCH,
        }
        self.require_branch = patch.object(
            calibrations, 'require_branch',
            return_value={'id': BRANCH, 'organization_id': ORG},
        ).start()
        self.addCleanup(patch.stopall)

    def client(self, connection):
        @contextmanager
        def connect():
            yield connection

        app = FastAPI()
        app.include_router(calibrations.create_router(lambda: self.user, connect, None))

        @app.exception_handler(calibrations.CalibrationError)
        async def handler(_, exc):
            return JSONResponse(status_code=exc.status_code, content={
                'code': exc.code, 'detail': exc.detail, 'request_id': 'req-1',
            })

        client = TestClient(app)
        self.addCleanup(client.close)
        return client


class CreateSessionTests(CalibrationBase):
    def handler(self, *, previous=None, open_row=None, roi=None):
        def handle(sql, params):
            if 'FROM calibration_operation WHERE idempotency_key' in sql:
                return result(row=previous)
            if 'FROM roi r' in sql:
                return result(row=roi or roi_row())
            if sql.startswith('UPDATE roi_calibration_session'):
                return result()
            if 'FROM roi_calibration_session' in sql and 'camera_node_id' in sql:
                return result(row=open_row)
            if sql.startswith('INSERT INTO calibration_operation'):
                return result()
            if 'calibration_version=calibration_version+1' in sql:
                return result(row={'calibration_version': 1})
            if sql.startswith('INSERT INTO roi_calibration_session'):
                return result(row=session_row())
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)
        return handle

    def test_creates_draft_and_reserves_version(self):
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations',
            json={'expected_config_version': 7, 'reason': 'Primera calibración'},
            headers={'Idempotency-Key': 'key-1'},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload['state'], 'draft')
        self.assertEqual(payload['session_version'], 1)
        self.assertEqual(payload['calibration_version'], 1)
        self.assertEqual(payload['config_version'], 7)
        self.assertEqual(payload['context_hash'], EXPECTED_HASH)
        self.assertEqual(payload['product'], {
            'id': str(PRODUCT), 'sku': 'AGUA-600', 'name': 'Agua 600 ml',
        })
        self.assertEqual(payload['references'], [])
        self.assertIsNone(payload['active'])
        self.assertEqual(connection.commits, 1)
        self.assertEqual(response.headers['cache-control'], 'no-store')

        inserted = next(call for call in connection.calls
                        if call[0].startswith('INSERT INTO roi_calibration_session'))
        self.assertEqual(inserted[1][8], EXPECTED_HASH)

    def test_missing_idempotency_key_never_touches_the_database(self):
        connection = FakeConnection(lambda sql, params: result())
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations', json={})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'invalid_request')
        self.assertEqual(connection.calls, [])

    def test_expected_version_mismatch_is_config_mismatch(self):
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations',
            json={'expected_config_version': 6},
            headers={'Idempotency-Key': 'key-2'},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'config_mismatch')
        self.assertEqual(connection.commits, 0)

    def test_desired_version_without_apply_is_session_conflict(self):
        connection = FakeConnection(self.handler(roi=roi_row(roi_version=8, roi_applied_version=7)))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations',
            json={},
            headers={'Idempotency-Key': 'key-3'},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'session_conflict')

    def test_second_session_on_same_camera_is_busy(self):
        connection = FakeConnection(self.handler(open_row={'id': CAL}))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations',
            json={},
            headers={'Idempotency-Key': 'key-4'},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'busy')

    def test_same_key_with_different_body_is_idempotency_conflict(self):
        previous = {
            'session_id': CAL, 'action': 'create',
            'request_hash': calibrations.hash_json({'expected_config_version': 7}),
        }
        connection = FakeConnection(self.handler(previous=previous))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations',
            json={'reason': 'otra cosa'},
            headers={'Idempotency-Key': 'key-5'},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'idempotency_conflict')

    def test_same_key_same_body_replays_the_same_session(self):
        previous = {
            'session_id': CAL, 'action': 'create',
            'request_hash': calibrations.hash_json({}),
        }

        def handle(sql, params):
            if 'FROM calibration_operation WHERE idempotency_key' in sql:
                return result(row=previous)
            if 'FROM roi r' in sql:
                return result(row=roi_row())
            if 'FROM roi_calibration_session WHERE id=%s AND roi_id=%s' in sql:
                return result(row=session_row())
            if 'FROM roi_calibration' in sql:
                return result(rows=[])
            if 'FROM calibration_operation' in sql:
                return result(rows=[])
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations',
            json={},
            headers={'Idempotency-Key': 'key-6'},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['id'], str(CAL))
        self.assertEqual(connection.commits, 0)


class ReadSessionTests(CalibrationBase):
    def handler(self, *, rows=None, references=None, operations=None):
        def handle(sql, params):
            if 'FROM roi r' in sql:
                return result(row=roi_row())
            if sql.startswith('UPDATE roi_calibration_session'):
                return result()
            if 'FROM roi_calibration_session WHERE id=%s AND roi_id=%s' in sql:
                return result(row=session_row())
            if sql.startswith('SELECT * FROM roi_calibration_session WHERE roi_id'):
                return result(rows=rows or [])
            if 'FROM roi_calibration' in sql:
                return result(rows=references or [])
            if 'FROM calibration_operation' in sql:
                return result(rows=operations or [])
            raise AssertionError(sql)
        return handle

    def test_detail_includes_references_and_operations(self):
        reference = {
            'capture_id': CAPTURE,
            'reference_type': 'empty',
            'status': 'ready',
            'captured_at': NOW,
            'received_at': NOW,
            'image_id': IMAGE,
            'descriptor_crc': 'deadbeef',
            'descriptor_format': 1,
            'quality': {'mean': 121.4},
            'clock_quality': 'unknown',
        }
        operation = {
            'id': 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
            'action': 'capture',
            'status': 'applied',
            'attempts': 1,
            'code': None,
            'updated_at': NOW,
        }
        connection = FakeConnection(self.handler(
            references=[reference], operations=[operation]))
        response = self.client(connection).get(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['state'], 'draft')
        self.assertEqual(payload['references'][0]['capture_id'], str(CAPTURE))
        self.assertEqual(payload['references'][0]['image_id'], str(IMAGE))
        self.assertEqual(payload['references'][0]['quality'], {'mean': 121.4})
        self.assertEqual(payload['operations'][0]['operation_id'], 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee')
        self.assertEqual(payload['operations'][0]['status'], 'applied')

    def test_list_returns_summaries_and_cursor(self):
        rows = [session_row(id=CAL), session_row(id=UUID('ffffffff-ffff-4fff-8fff-ffffffffffff'))]
        connection = FakeConnection(self.handler(rows=rows))
        response = self.client(connection).get(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations?limit=1')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload['items']), 1)
        self.assertTrue(payload['next_cursor'])
        self.assertEqual(payload['items'][0]['id'], str(CAL))

    def test_invalid_cursor_is_invalid_request(self):
        connection = FakeConnection(self.handler())
        response = self.client(connection).get(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations?cursor=%25%25%25')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'invalid_request')


class SnapshotTests(CalibrationBase):
    def test_serves_only_snapshots_of_the_branch(self):
        def handle(sql, params):
            if 'FROM snapshot' in sql:
                return result(row={
                    'data': b'\xff\xd8\xff\xe0jpeg',
                    'content_type': 'image/jpeg',
                    'captured_at': NOW,
                })
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        response = self.client(connection).get(
            f'/branches/{BRANCH}/snapshots/{IMAGE}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'\xff\xd8\xff\xe0jpeg')
        self.assertEqual(response.headers['content-type'], 'image/jpeg')
        self.assertEqual(response.headers['x-captured-at'], '2026-11-08T18:02:11.123Z')
        self.assertEqual(response.headers['cache-control'], 'private, max-age=300')
        self.assertTrue(response.headers['etag'])

        query, params = next(call for call in connection.calls if 'FROM snapshot' in call[0])
        self.assertEqual(params, (IMAGE, BRANCH))


if __name__ == '__main__':
    unittest.main()

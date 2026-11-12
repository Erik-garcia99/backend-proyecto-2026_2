import asyncio
import base64
import hashlib
import json
import time
import unittest
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException
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


def connection_provider(connection):
    @contextmanager
    def connect():
        yield connection
    return connect


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


def operation_row(**overrides):
    row = {
        'id': UUID('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'),
        'session_id': CAL,
        'roi_id': ROI,
        'action': 'capture',
        'idempotency_key': 'key-1',
        'request_hash': 'hash',
        'status': 'pending',
        'attempts': 0,
        'payload': {},
        'result': None,
        'code': None,
        'detail': None,
        'created_by': USER,
        'created_at': NOW,
        'updated_at': NOW,
    }
    row.update(overrides)
    return row


def reference_row(kind='empty', status='pending', **overrides):
    row = {
        'id': UUID('12121212-1212-4212-8212-121212121212'),
        'roi_id': ROI,
        'reference_type': kind,
        'reference_metrics': {},
        'captured_by': USER,
        'captured_at': NOW,
        'is_current': False,
        'session_id': CAL,
        'capture_id': CAPTURE,
        'status': status,
        'descriptor_format': 1,
        'descriptor_crc': None,
        'pair_hash': None,
        'context_hash': EXPECTED_HASH,
        'image_id': None,
        'image_sha256': None,
        'image_bytes': None,
        'width': None,
        'height': None,
        'quality': None,
        'clock_quality': None,
        'uptime_ms': None,
        'received_at': NOW,
    }
    row.update(overrides)
    return row


def capture_ack(**overrides):
    ack = {
        'schema_version': 1,
        'type': 'cal_result',
        'id': str(operation_row()['id']),
        'req_id': 'req-1',
        'action': 'capture',
        'status': 'stored',
        'calibration_id': str(CAL),
        'calibration_version': 1,
        'roi_id': str(ROI),
        'config_version': 7,
        'context_hash': EXPECTED_HASH,
        'capture_id': str(CAPTURE),
        'kind': 'empty',
        'format_version': 1,
        'descriptor_crc': 'deadbeef',
        'image_sha256': hashlib.sha256(b'image').hexdigest(),
        'image_bytes': 5,
        'width': 640,
        'height': 480,
        'quality': {'mean': 121.4},
        'clock_quality': 'unknown',
        'uptime_ms': 1234,
        'persisted': True,
    }
    ack.update(overrides)
    return ack


class FakeHub:
    def __init__(self, result=None, error=None, echo_req=False):
        self.result = result
        self.error = error
        self.echo_req = echo_req
        self.calls = []

    async def send(self, identifier, command, branch_token=None, payload=None, timeout=None):
        self.calls.append((identifier, command, payload, timeout))
        if self.error:
            raise self.error
        if self.echo_req and isinstance(self.result, dict) and isinstance(payload, dict):
            data = payload.get('data') or {}
            if isinstance(self.result.get('data'), dict):
                self.result['data']['req_id'] = data.get('req_id')
                self.result['data']['id'] = data.get('id')
        return self.result


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


class CommandAndAckTests(unittest.TestCase):
    def test_capture_command_matches_annex_a(self):
        operation = operation_row(payload={'capture_id': str(CAPTURE), 'kind': 'empty'})
        command = calibrations.build_command(
            operation, session_row(), 'req-1',
            now=datetime(2026, 11, 8, 18, 4, tzinfo=timezone.utc))
        self.assertEqual(command['type'], 'calibrate')
        self.assertEqual(command['id'], str(operation['id']))
        self.assertEqual(command['req_id'], 'req-1')
        self.assertEqual(command['capture_id'], str(CAPTURE))
        self.assertEqual(command['kind'], 'empty')
        self.assertEqual(command['calibration_version'], 1)
        self.assertEqual(command['config_version'], 7)
        self.assertEqual(command['context_hash'], EXPECTED_HASH)
        self.assertEqual(command['issued_at'], '2026-11-08T18:04:00.000Z')
        self.assertEqual(command['expires_at'], '2026-11-08T18:04:10.000Z')

    def test_commit_command_carries_pair_hash_and_capture_ids(self):
        payload = {
            'references': {
                'empty': {'capture_id': str(CAPTURE)},
                'full': {'capture_id': str(IMAGE)},
            },
            'pair_hash': 'a' * 64,
        }
        command = calibrations.build_command(
            operation_row(action='commit', payload=payload), session_row(), 'req-2')
        self.assertEqual(command['type'], 'calibration.commit')
        self.assertEqual(command['empty_capture_id'], str(CAPTURE))
        self.assertEqual(command['full_capture_id'], str(IMAGE))
        self.assertEqual(command['pair_hash'], 'a' * 64)

    def test_cancel_and_status_commands(self):
        cancel = calibrations.build_command(
            operation_row(action='cancel', payload={}), session_row(), 'req-3')
        self.assertEqual(cancel['type'], 'calibration.cancel')
        self.assertNotIn('capture_id', cancel)
        status = calibrations.build_command(
            operation_row(action='status', payload={}), session_row(), 'req-4')
        self.assertEqual(status['type'], 'calibration.status')
        self.assertEqual(status['calibration_id'], str(CAL))

    def test_valid_capture_ack_passes(self):
        ack = capture_ack(req_id='req-1')
        self.assertEqual(
            calibrations.validate_ack(
                operation_row(payload={'capture_id': str(CAPTURE), 'kind': 'empty'}),
                session_row(), ack, req_id='req-1'),
            ack,
        )

    def test_incomplete_capture_ack_is_invalid(self):
        ack = capture_ack()
        ack.pop('persisted')
        with self.assertRaises(calibrations.AckError) as caught:
            calibrations.validate_ack(
                operation_row(payload={'capture_id': str(CAPTURE), 'kind': 'empty'}),
                session_row(), ack, req_id='req-1')
        self.assertEqual(caught.exception.code, 'invalid_ack')

    def test_ack_from_another_calibration_is_rejected(self):
        ack = capture_ack(calibration_id=str(ROI))
        with self.assertRaises(calibrations.AckError):
            calibrations.validate_ack(
                operation_row(payload={'capture_id': str(CAPTURE), 'kind': 'empty'}),
                session_row(), ack, req_id='req-1')

    def test_ack_from_another_capture_or_action_is_rejected(self):
        base = operation_row(payload={'capture_id': str(CAPTURE), 'kind': 'empty'})
        for ack in (capture_ack(capture_id=str(IMAGE)),
                    capture_ack(action='commit')):
            with self.subTest(ack=ack['action']):
                with self.assertRaises(calibrations.AckError):
                    calibrations.validate_ack(base, session_row(), ack, req_id='req-1')

    def test_valid_commit_ack(self):
        payload = {
            'references': {
                'empty': {'capture_id': str(CAPTURE)},
                'full': {'capture_id': str(IMAGE)},
            },
            'pair_hash': 'b' * 64,
        }
        ack = {
            'schema_version': 1, 'id': str(operation_row()['id']),
            'action': 'commit', 'status': 'applied',
            'calibration_id': str(CAL), 'calibration_version': 1,
            'roi_id': str(ROI), 'config_version': 7, 'context_hash': EXPECTED_HASH,
            'empty_capture_id': str(CAPTURE), 'full_capture_id': str(IMAGE),
            'pair_hash': 'b' * 64, 'persisted': True,
            'active_calibration_version': 1,
        }
        self.assertEqual(
            calibrations.validate_ack(
                operation_row(action='commit', payload=payload),
                session_row(), ack),
            ack,
        )

    def test_commit_ack_with_other_version_does_not_activate(self):
        payload = {
            'references': {
                'empty': {'capture_id': str(CAPTURE)},
                'full': {'capture_id': str(IMAGE)},
            },
            'pair_hash': 'b' * 64,
        }
        ack = {
            'schema_version': 1, 'action': 'commit', 'status': 'applied',
            'calibration_id': str(CAL), 'calibration_version': 1,
            'roi_id': str(ROI),
            'empty_capture_id': str(CAPTURE), 'full_capture_id': str(IMAGE),
            'pair_hash': 'b' * 64, 'persisted': True,
            'active_calibration_version': 9,
        }
        with self.assertRaises(calibrations.AckError):
            calibrations.validate_ack(
                operation_row(action='commit', payload=payload), session_row(), ack)

    def test_status_ack_requires_report_shape(self):
        ack = {
            'schema_version': 1, 'action': 'status', 'status': 'reported',
            'calibration_id': str(CAL), 'roi_id': str(ROI),
            'active': None,
            'draft': {
                'calibration_id': str(CAL), 'calibration_version': 1,
                'context_hash': EXPECTED_HASH,
                'empty_crc': 'deadbeef', 'full_crc': 'feedface',
            },
        }
        self.assertEqual(calibrations.validate_status_ack(session_row(), ack), ack)
        broken = {**ack, 'active': {'calibration_version': 1}}
        with self.assertRaises(calibrations.AckError):
            calibrations.validate_status_ack(session_row(), broken)

    def test_signature_message_uses_the_gateway_convention(self):
        message = calibrations.reference_signature_message(
            'rpi-1', '123', 'nonce', str(DEVICE), 'a' * 64)
        self.assertIn(b'selfiq-calibration-reference-v1\\n', message)
        self.assertIn(str(DEVICE).encode(), message)


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

    def client(self, connection, hub=None):
        @contextmanager
        def connect():
            yield connection

        app = FastAPI()
        app.include_router(calibrations.create_router(lambda: self.user, connect, hub))

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
            if 'FROM calibration_operation' in sql and 'updated_at < NOW()' in sql:
                return result(rows=[])
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

    def test_stale_capture_is_marked_unknown_not_failed(self):
        stale = operation_row(action='capture', status='sent',
                              payload={'kind': 'empty'})

        def handle(sql, params):
            if 'FROM roi r' in sql:
                return result(row=roi_row())
            if sql.startswith('UPDATE roi_calibration_session') and "state='expired'" in sql:
                return result()
            if 'FROM calibration_operation' in sql and 'updated_at < NOW()' in sql:
                return result(rows=[stale])
            if 'FROM roi_calibration_session WHERE id=%s AND roi_id=%s' in sql:
                return result(row=session_row())
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            if 'FROM roi_calibration' in sql and 'reference_type=%s' in sql:
                return result(row=reference_row())
            if sql.startswith('UPDATE roi_calibration'):
                return result()
            if 'FROM roi_calibration' in sql:
                return result(rows=[])
            if 'FROM calibration_operation' in sql:
                return result(rows=[])
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        response = self.client(connection).get(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}')
        self.assertEqual(response.status_code, 200)
        statuses = [call[1][0] for call in connection.calls
                    if call[0].startswith('UPDATE calibration_operation')]
        self.assertEqual(statuses, ['unknown'])
        reference_update = next(call for call in connection.calls
                                if call[0].startswith('UPDATE roi_calibration')
                                and 'SET status=' in call[0])
        self.assertIn("status='unknown'", reference_update[0])
        self.assertGreaterEqual(connection.commits, 1)

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


class MutationRouteTests(CalibrationBase):
    def handler(self, *, session, refs=None, previous=None, busy=None):
        def handle(sql, params):
            if 'FROM roi r' in sql:
                return result(row=roi_row())
            if sql.startswith('UPDATE roi_calibration_session') and "state='expired'" in sql:
                return result()
            if 'FROM roi_calibration_session WHERE id=%s AND roi_id=%s' in sql:
                return result(row=session)
            if 'FROM calibration_operation WHERE idempotency_key' in sql:
                return result(row=previous)
            if "action IN ('capture','commit')" in sql:
                return result(row=busy)
            if sql.startswith('SELECT id FROM roi_calibration'):
                return result(row=None)
            if sql.startswith('INSERT INTO roi_calibration'):
                return result(row=reference_row())
            if sql.startswith('INSERT INTO calibration_operation'):
                return result(row=operation_row(action=params[2], payload=params[5]))
            if sql.startswith('UPDATE roi_calibration_session'):
                state = params[0] if 'state=%s' in sql else session['state']
                return result(row={**session, 'state': state,
                                   'session_version': session['session_version'] + 1,
                                   'expires_at': EXPIRES})
            if sql.startswith('UPDATE calibration_operation SET result='):
                return result()
            if sql.startswith('SELECT * FROM roi_calibration'):
                return result(rows=refs or [])
            raise AssertionError(sql)
        return handle

    def test_capture_returns_202_and_dispatches(self):
        connection = FakeConnection(self.handler(session=session_row()))
        dispatch = AsyncMock()
        with patch.object(calibrations, 'dispatch_operation', dispatch):
            response = self.client(connection, FakeHub()).post(
                f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/capture',
                json={'kind': 'empty', 'capture_id': str(CAPTURE), 'session_version': 1},
                headers={'Idempotency-Key': 'capture-1'})
        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload['state'], 'draft')
        self.assertEqual(payload['capture']['status'], 'pending')
        self.assertEqual(payload['session_version'], 2)
        dispatch.assert_awaited_once()
        self.assertTrue(any(call[0].startswith('INSERT INTO roi_calibration')
                            for call in connection.calls))

    def test_capture_full_before_empty_is_invalid_state(self):
        connection = FakeConnection(self.handler(session=session_row()))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/capture',
            json={'kind': 'full', 'capture_id': str(CAPTURE), 'session_version': 1},
            headers={'Idempotency-Key': 'capture-2'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'invalid_state')
        self.assertFalse(any(call[0].startswith('INSERT INTO calibration_operation')
                             for call in connection.calls))

    def test_capture_with_stale_version_is_session_conflict(self):
        connection = FakeConnection(self.handler(session=session_row(session_version=3)))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/capture',
            json={'kind': 'empty', 'capture_id': str(CAPTURE), 'session_version': 1},
            headers={'Idempotency-Key': 'capture-3'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'session_conflict')

    def test_capture_replay_does_not_create_another_operation(self):
        stored = {'operation_id': 'op-1', 'session_version': 2,
                  'state': 'draft', 'capture': {'capture_id': str(CAPTURE),
                                                'kind': 'empty', 'status': 'pending'}}
        previous = operation_row(action='capture', request_hash=calibrations.hash_json({
            'kind': 'empty', 'capture_id': str(CAPTURE), 'session_version': 1,
        }), result=stored)
        connection = FakeConnection(self.handler(session=session_row(), previous=previous))
        dispatch = AsyncMock()
        with patch.object(calibrations, 'dispatch_operation', dispatch):
            response = self.client(connection, FakeHub()).post(
                f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/capture',
                json={'kind': 'empty', 'capture_id': str(CAPTURE), 'session_version': 1},
                headers={'Idempotency-Key': 'capture-1'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), stored)
        dispatch.assert_not_awaited()

    def test_commit_with_pending_reference_is_incomplete_pair(self):
        refs = [reference_row('empty', 'ready'),
                reference_row('full', 'pending', capture_id=IMAGE)]
        connection = FakeConnection(self.handler(
            session=session_row(state='empty_ready', session_version=2), refs=refs))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/commit',
            json={'session_version': 2, 'references': {
                'empty': {'capture_id': str(CAPTURE)},
                'full': {'capture_id': str(IMAGE)}}},
            headers={'Idempotency-Key': 'commit-1'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'incomplete_pair')

    def test_commit_returns_202_committing(self):
        refs = [
            reference_row('empty', 'ready',
                          reference_metrics={'descriptor': base64.b64encode(b'empty').decode()}),
            reference_row('full', 'ready', capture_id=IMAGE,
                          reference_metrics={'descriptor': base64.b64encode(b'full').decode()}),
        ]
        connection = FakeConnection(self.handler(
            session=session_row(state='pair_ready', session_version=3), refs=refs))
        dispatch = AsyncMock()
        with patch.object(calibrations, 'dispatch_operation', dispatch):
            response = self.client(connection, FakeHub()).post(
                f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/commit',
                json={'session_version': 3, 'references': {
                    'empty': {'capture_id': str(CAPTURE)},
                    'full': {'capture_id': str(IMAGE)}}},
                headers={'Idempotency-Key': 'commit-2'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['state'], 'committing')
        dispatch.assert_awaited_once()
        inserted = next(call for call in connection.calls
                        if call[0].startswith('INSERT INTO calibration_operation'))
        self.assertEqual(inserted[1][2], 'commit')
        self.assertEqual(len(inserted[1][5].obj['pair_hash']), 64)

    def test_commit_with_identical_descriptors_is_reference_too_similar(self):
        same = base64.b64encode(b'same-descriptor').decode()
        refs = [
            reference_row('empty', 'ready', reference_metrics={'descriptor': same}),
            reference_row('full', 'ready', capture_id=IMAGE,
                          reference_metrics={'descriptor': same}),
        ]
        connection = FakeConnection(self.handler(
            session=session_row(state='pair_ready', session_version=3), refs=refs))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/commit',
            json={'session_version': 3, 'references': {
                'empty': {'capture_id': str(CAPTURE)},
                'full': {'capture_id': str(IMAGE)}}},
            headers={'Idempotency-Key': 'commit-3'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'reference_too_similar')

    def test_cancel_returns_202_cancel_pending(self):
        connection = FakeConnection(self.handler(session=session_row(session_version=2)))
        dispatch = AsyncMock()
        with patch.object(calibrations, 'dispatch_operation', dispatch):
            response = self.client(connection, FakeHub()).post(
                f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/cancel',
                json={'session_version': 2},
                headers={'Idempotency-Key': 'cancel-1'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['state'], 'cancel_pending')
        dispatch.assert_awaited_once()

    def test_cancel_committing_is_invalid_state(self):
        connection = FakeConnection(self.handler(
            session=session_row(state='committing', session_version=4)))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/cancel',
            json={'session_version': 4},
            headers={'Idempotency-Key': 'cancel-2'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'invalid_state')

    def test_cancel_terminal_returns_200_without_new_operation(self):
        connection = FakeConnection(self.handler(
            session=session_row(state='active', session_version=9)))
        response = self.client(connection).post(
            f'/branches/{BRANCH}/rois/{ROI}/calibrations/{CAL}/cancel',
            json={'session_version': 9},
            headers={'Idempotency-Key': 'cancel-3'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['state'], 'active')
        self.assertFalse(any(call[0].startswith('INSERT INTO calibration_operation')
                             for call in connection.calls))


class DispatchTests(unittest.TestCase):
    def session(self, **overrides):
        return session_row(**overrides)

    def test_capture_ack_marks_reference_image_pending_and_advances(self):
        operation = operation_row(
            status='pending', payload={'capture_id': str(CAPTURE), 'kind': 'empty'})
        hub = FakeHub(result={'type': 'result', 'id': str(operation['id']),
                              'ok': True, 'data': capture_ack(req_id='req-1')}, echo_req=True)

        def handle(sql, params):
            if 'FROM calibration_operation WHERE id=%s' in sql and 'FOR UPDATE' not in sql:
                return result(row=operation)
            if 'FROM roi_calibration_session WHERE id=%s' in sql:
                return result(row=self.session())
            if 'FROM esp32_node' in sql:
                return result(row={'rpi_identifier': 'rpi-1'})
            if sql.startswith('UPDATE calibration_operation') and "status='sent'" in sql:
                return result()
            if 'FOR UPDATE' in sql and 'FROM calibration_operation' in sql:
                return result(row={**operation, 'status': 'sent'})
            if 'FOR UPDATE' in sql and 'FROM roi_calibration_session' in sql:
                return result(row=self.session())
            if 'FROM roi_calibration' in sql:
                return result(row=reference_row())
            if sql.startswith('UPDATE roi_calibration') and 'SET status=' in sql:
                return result()
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row=self.session(state='empty_ready', session_version=2))
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        asyncio.run(calibrations.dispatch_operation(connection_provider(connection), hub, operation['id']))
        self.assertEqual(hub.calls[0][1], 'calibrate')
        self.assertEqual(hub.calls[0][2]['data']['capture_id'], str(CAPTURE))
        self.assertEqual(connection.commits, 2)
        applied = next(call for call in connection.calls
                       if call[0].startswith('UPDATE calibration_operation') and call[1][0] == 'applied')
        self.assertEqual(applied[1][0], 'applied')
        session_update = next(call for call in connection.calls
                              if call[0].startswith('UPDATE roi_calibration_session'))
        self.assertIn('empty_ready', session_update[1])

    def test_timeout_leaves_operation_unknown(self):
        operation = operation_row(status='sent', payload={
            'capture_id': str(CAPTURE), 'kind': 'empty'})
        hub = FakeHub(error=HTTPException(504, 'Sin confirmación'))

        def handle(sql, params):
            if 'FROM calibration_operation WHERE id=%s' in sql and 'FOR UPDATE' not in sql:
                return result(row=operation)
            if 'FROM roi_calibration_session WHERE id=%s' in sql:
                return result(row=self.session())
            if 'FROM esp32_node' in sql:
                return result(row={'rpi_identifier': 'rpi-1'})
            if sql.startswith('UPDATE calibration_operation') and "status='sent'" in sql:
                return result()
            if 'FOR UPDATE' in sql and 'FROM calibration_operation' in sql:
                return result(row={**operation, 'status': 'sent'})
            if 'FOR UPDATE' in sql and 'FROM roi_calibration_session' in sql:
                return result(row=self.session())
            if 'FROM roi_calibration' in sql:
                return result(row=reference_row())
            if sql.startswith('UPDATE roi_calibration') and 'SET status=' in sql:
                return result()
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row=self.session())
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        asyncio.run(calibrations.dispatch_operation(connection_provider(connection), hub, operation['id']))
        statuses = [call[1][0] for call in connection.calls
                    if call[0].startswith('UPDATE calibration_operation')]
        self.assertIn('unknown', statuses)
        self.assertNotIn('failed', statuses)

    def test_invalid_ack_never_advances_the_session(self):
        operation = operation_row(status='sent', payload={
            'capture_id': str(CAPTURE), 'kind': 'empty'})
        broken = capture_ack(req_id='req-1')
        broken.pop('persisted')
        hub = FakeHub(result={'type': 'result', 'id': str(operation['id']),
                              'ok': True, 'data': broken}, echo_req=True)

        def handle(sql, params):
            if 'FROM calibration_operation WHERE id=%s' in sql and 'FOR UPDATE' not in sql:
                return result(row=operation)
            if 'FROM roi_calibration_session WHERE id=%s' in sql:
                return result(row=self.session())
            if 'FROM esp32_node' in sql:
                return result(row={'rpi_identifier': 'rpi-1'})
            if sql.startswith('UPDATE calibration_operation') and "status='sent'" in sql:
                return result()
            if 'FOR UPDATE' in sql and 'FROM calibration_operation' in sql:
                return result(row={**operation, 'status': 'sent'})
            if 'FOR UPDATE' in sql and 'FROM roi_calibration_session' in sql:
                return result(row=self.session())
            if 'FROM roi_calibration' in sql:
                return result(row=reference_row())
            if sql.startswith('UPDATE roi_calibration') and 'SET status=' in sql:
                return result()
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row=self.session())
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        asyncio.run(calibrations.dispatch_operation(connection_provider(connection), hub, operation['id']))
        session_updates = [call for call in connection.calls
                           if call[0].startswith('UPDATE roi_calibration_session')]
        self.assertTrue(all('empty_ready' not in call[0] and
                            (not call[1] or call[1][0] != 'empty_ready')
                            for call in session_updates))

    def test_capture_rejection_is_final_and_does_not_advance(self):
        operation = operation_row(status='sent', payload={
            'capture_id': str(CAPTURE), 'kind': 'empty'})
        hub = FakeHub(result={'type': 'result', 'id': str(operation['id']),
                              'ok': False, 'error': {'code': 'invalid_frame'}})

        def handle(sql, params):
            if 'FROM calibration_operation WHERE id=%s' in sql and 'FOR UPDATE' not in sql:
                return result(row=operation)
            if 'FROM roi_calibration_session WHERE id=%s' in sql:
                return result(row=self.session())
            if 'FROM esp32_node' in sql:
                return result(row={'rpi_identifier': 'rpi-1'})
            if sql.startswith('UPDATE calibration_operation') and "status='sent'" in sql:
                return result()
            if 'FOR UPDATE' in sql and 'FROM calibration_operation' in sql:
                return result(row={**operation, 'status': 'sent'})
            if 'FOR UPDATE' in sql and 'FROM roi_calibration_session' in sql:
                return result(row=self.session())
            if 'FROM roi_calibration' in sql:
                return result(row=reference_row())
            if sql.startswith('UPDATE roi_calibration') and 'SET status=' in sql:
                return result()
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row=self.session())
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        asyncio.run(calibrations.dispatch_operation(connection_provider(connection), hub,
                                                    operation['id']))
        statuses = [call[1][0] for call in connection.calls
                    if call[0].startswith('UPDATE calibration_operation') and
                    'status=%s' in call[0]]
        self.assertIn('rejected', statuses)
        reference_update = next(call for call in connection.calls
                                if call[0].startswith('UPDATE roi_calibration'))
        self.assertIn("status='failed'", reference_update[0])

    def test_cancel_ack_marks_session_cancelled(self):
        operation = operation_row(action='cancel', status='sent', payload={})
        session = self.session(state='cancel_pending', session_version=5)
        ack = {
            'schema_version': 1, 'id': str(operation['id']),
            'action': 'cancel', 'status': 'cancelled',
            'calibration_id': str(CAL), 'calibration_version': 1,
            'roi_id': str(ROI),
        }
        hub = FakeHub(result={'type': 'result', 'id': str(operation['id']),
                              'ok': True, 'data': ack}, echo_req=True)

        def handle(sql, params):
            if 'FROM calibration_operation WHERE id=%s' in sql and 'FOR UPDATE' not in sql:
                return result(row=operation)
            if 'FROM roi_calibration_session WHERE id=%s' in sql:
                return result(row=session)
            if 'FROM esp32_node' in sql:
                return result(row={'rpi_identifier': 'rpi-1'})
            if sql.startswith('UPDATE calibration_operation') and "status='sent'" in sql:
                return result()
            if 'FOR UPDATE' in sql and 'FROM calibration_operation' in sql:
                return result(row={**operation, 'status': 'sent'})
            if 'FOR UPDATE' in sql and 'FROM roi_calibration_session' in sql:
                return result(row=session)
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row={**session, 'state': 'cancelled',
                                   'session_version': 6})
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        asyncio.run(calibrations.dispatch_operation(connection_provider(connection), hub,
                                                    operation['id']))
        session_update = next(call for call in connection.calls
                              if call[0].startswith('UPDATE roi_calibration_session'))
        self.assertIn('cancelled', session_update[1])

    def test_commit_ack_activates_and_updates_roi(self):
        operation = operation_row(action='commit', status='sent', payload={
            'references': {'empty': {'capture_id': str(CAPTURE)},
                           'full': {'capture_id': str(IMAGE)}},
            'pair_hash': 'c' * 64,
        })
        session = self.session(state='committing', session_version=4)
        ack = {
            'schema_version': 1, 'id': str(operation['id']),
            'action': 'commit', 'status': 'applied',
            'calibration_id': str(CAL), 'calibration_version': 1,
            'roi_id': str(ROI), 'config_version': 7, 'context_hash': EXPECTED_HASH,
            'empty_capture_id': str(CAPTURE), 'full_capture_id': str(IMAGE),
            'pair_hash': 'c' * 64, 'persisted': True,
            'active_calibration_version': 1,
        }
        hub = FakeHub(result={'type': 'result', 'id': str(operation['id']),
                              'ok': True, 'data': ack}, echo_req=True)

        def handle(sql, params):
            if 'FROM calibration_operation WHERE id=%s' in sql and 'FOR UPDATE' not in sql:
                return result(row=operation)
            if 'FROM roi_calibration_session WHERE id=%s' in sql:
                return result(row=session)
            if 'FROM esp32_node' in sql:
                return result(row={'rpi_identifier': 'rpi-1'})
            if sql.startswith('UPDATE calibration_operation') and "status='sent'" in sql:
                return result()
            if 'FOR UPDATE' in sql and 'FROM calibration_operation' in sql:
                return result(row={**operation, 'status': 'sent'})
            if 'FOR UPDATE' in sql and 'FROM roi_calibration_session' in sql:
                return result(row=session)
            if 'UPDATE roi_calibration SET is_current=false' in sql:
                return result()
            if 'UPDATE roi_calibration SET is_current=true' in sql:
                return result()
            if sql.startswith('UPDATE roi') and 'calibration_status=' in sql:
                return result()
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row={**session, 'state': 'active',
                                   'session_version': 5, 'confirmed_at': NOW})
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        asyncio.run(calibrations.dispatch_operation(connection_provider(connection), hub, operation['id']))
        activation = next(call for call in connection.calls
                          if 'calibration_status=' in call[0])
        self.assertIn('active_calibration_id', activation[0])
        self.assertEqual(activation[1][0], CAL)
        self.assertEqual(activation[1][1], EXPECTED_HASH)
        self.assertTrue(any("state='active'" in call[0] or
                            (call[1] and call[1][0] == 'active')
                            for call in connection.calls))


class ReconciliationTests(unittest.TestCase):
    def test_status_report_applies_the_pair_when_node_already_active(self):
        session = session_row(state='committing', session_version=4)
        ack = {
            'schema_version': 1, 'action': 'status', 'status': 'reported',
            'calibration_id': str(CAL), 'roi_id': str(ROI),
            'active': {'calibration_version': 1, 'context_hash': EXPECTED_HASH,
                       'empty_crc': 'deadbeef', 'full_crc': 'feedface'},
            'draft': None,
        }

        def handle(sql, params):
            if 'FROM roi_calibration_session WHERE id=%s FOR UPDATE' in sql:
                return result(row=session)
            if 'FROM calibration_operation' in sql and 'action IN' in sql:
                return result(row=operation_row(action='commit', status='sent'))
            if 'UPDATE roi_calibration SET is_current=false' in sql:
                return result()
            if 'UPDATE roi_calibration SET is_current=true' in sql:
                return result()
            if sql.startswith('UPDATE roi') and 'calibration_status=' in sql:
                return result()
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row={**session, 'state': 'active',
                                   'session_version': 5, 'confirmed_at': NOW})
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        calibrations.apply_status_report(connection_provider(connection), str(CAL), ack)
        self.assertTrue(any('calibration_status=' in call[0] for call in connection.calls))
        self.assertEqual(connection.commits, 1)

    def test_status_report_without_draft_cancels_a_pending_cancel(self):
        session = session_row(state='cancel_pending', session_version=5)
        ack = {
            'schema_version': 1, 'action': 'status', 'status': 'reported',
            'calibration_id': str(CAL), 'roi_id': str(ROI),
            'active': None, 'draft': None,
        }

        def handle(sql, params):
            if 'FROM roi_calibration_session WHERE id=%s FOR UPDATE' in sql:
                return result(row=session)
            if 'FROM calibration_operation' in sql and 'action IN' in sql:
                return result(row=operation_row(action='cancel', status='sent'))
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row={**session, 'state': 'cancelled'})
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        calibrations.apply_status_report(connection_provider(connection), str(CAL), ack)
        update = next(call for call in connection.calls
                      if call[0].startswith('UPDATE roi_calibration_session'))
        self.assertIn('cancelled', update[1])

    def test_status_report_without_node_draft_fails_the_session(self):
        session = session_row(state='committing', session_version=4)
        ack = {
            'schema_version': 1, 'action': 'status', 'status': 'reported',
            'calibration_id': str(CAL), 'roi_id': str(ROI),
            'active': None, 'draft': None,
        }

        def handle(sql, params):
            if 'FROM roi_calibration_session WHERE id=%s FOR UPDATE' in sql:
                return result(row=session)
            if 'FROM calibration_operation' in sql and 'action IN' in sql:
                return result(row=operation_row(action='commit', status='sent'))
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row={**session, 'state': 'failed'})
            if sql.startswith('UPDATE calibration_operation'):
                return result()
            raise AssertionError(sql)

        connection = FakeConnection(handle)
        calibrations.apply_status_report(connection_provider(connection), str(CAL), ack)
        update = next(call for call in connection.calls
                      if call[0].startswith('UPDATE roi_calibration_session'))
        self.assertIn('failed', update[1])


class ReferenceUploadTests(CalibrationBase):
    def setUp(self):
        super().setUp()
        self.private_key = Ed25519PrivateKey.generate()
        public = self.private_key.public_key().public_bytes_raw()
        self.public_b64 = base64.b64encode(public).decode()
        self.image = b'\xff\xd8\xff\xe0jpeg-reference-body'
        self.image_sha = hashlib.sha256(self.image).hexdigest()
        self.descriptor = base64.b64encode(b'empty-descriptor').decode()
        self.crc = f'{zlib.crc32(b"empty-descriptor") & 0xffffffff:08x}'
        self.meta = {
            'calibration_id': str(CAL),
            'calibration_version': 1,
            'roi_id': str(ROI),
            'capture_id': str(CAPTURE),
            'kind': 'empty',
            'context_hash': EXPECTED_HASH,
            'descriptor_crc': self.crc,
            'format_version': 1,
            'captured_at': '2026-11-08T18:04:02.000Z',
            'clock_quality': 'unknown',
            'uptime_ms': 1234,
            'image_sha256': self.image_sha,
            'image_bytes': len(self.image),
            'width': 640,
            'height': 480,
            'descriptor': self.descriptor,
        }

    def headers(self, *, meta=None, signature=None):
        stamp = str(int(time.time()))
        nonce = 'nonce-1'
        message = calibrations.reference_signature_message(
            'rpi-1', stamp, nonce, str(DEVICE), self.image_sha)
        signature = signature or base64.b64encode(
            self.private_key.sign(message)).decode()
        meta_b64 = base64.urlsafe_b64encode(
            json.dumps(meta or self.meta).encode()).decode().rstrip('=')
        return {
            'x-timestamp': stamp,
            'x-nonce': nonce,
            'x-signature': signature,
            'x-calibration-meta': meta_b64,
        }

    def handler(self, *, existing=None, session=None, reference=None):
        session = session or session_row()

        def handle(sql, params):
            if 'FROM gateway g JOIN branch b' in sql:
                return result(row={
                    'id': SHELF, 'branch_id': BRANCH,
                    'device_public_key': self.public_b64,
                    'is_active': True, 'token_hash': 'token-hash',
                })
            if 'FROM organization_token t JOIN branch b' in sql:
                return result(row={'id': UUID('13131313-1313-4313-8313-131313131313')})
            if 'FROM esp32_node n' in sql:
                return result(row={'id': DEVICE})
            if 'FROM roi_calibration_session' in sql:
                return result(row=session)
            if 'FROM roi_calibration' in sql and 'capture_id=%s' in sql:
                return result(row=reference or reference_row(
                    descriptor_crc=self.crc, descriptor_format=1))
            if 'FROM snapshot' in sql and 'calibration_session_id' in sql:
                return result(row=existing)
            if sql.startswith('INSERT INTO snapshot'):
                return result(row={'id': IMAGE})
            if sql.startswith('UPDATE roi_calibration') and 'SET status=' in sql:
                return result()
            if sql.startswith('FROM roi_calibration'):
                return result(rows=[reference_row('empty', 'ready'),
                                    reference_row('full', 'ready', capture_id=IMAGE)])
            if sql.startswith('SELECT * FROM roi_calibration\n'):
                return result(rows=[reference_row('empty', 'ready')])
            if sql.startswith('UPDATE roi_calibration_session'):
                return result(row={**session, 'state': 'pair_ready'})
            raise AssertionError(sql)
        return handle

    def test_signed_upload_attaches_image_and_marks_reference_ready(self):
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/gateways/rpi-1/devices/{DEVICE}/calibration-references',
            content=self.image, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['image_id'], str(IMAGE))
        self.assertEqual(response.json()['status'], 'ready')
        insert = next(call for call in connection.calls
                      if call[0].startswith('INSERT INTO snapshot'))
        self.assertIn("'calibration'", insert[0])
        self.assertEqual(insert[1][4], CAL)
        self.assertEqual(insert[1][5], CAPTURE)
        update = next(call for call in connection.calls
                      if call[0].startswith('UPDATE roi_calibration') and 'SET status=' in call[0])
        self.assertIn("status='ready'", update[0])
        self.assertEqual(update[1][0], IMAGE)

    def test_bad_signature_is_rejected(self):
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/gateways/rpi-1/devices/{DEVICE}/calibration-references',
            content=self.image, headers=self.headers(signature=base64.b64encode(b'x' * 64).decode()))
        self.assertEqual(response.status_code, 401)
        self.assertFalse(any(call[0].startswith('INSERT INTO snapshot')
                             for call in connection.calls))

    def test_repeated_capture_is_idempotent(self):
        connection = FakeConnection(self.handler(existing={
            'id': IMAGE, 'image_sha256': self.image_sha}))
        response = self.client(connection).post(
            f'/gateways/rpi-1/devices/{DEVICE}/calibration-references',
            content=self.image, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['replay'])
        self.assertFalse(any(call[0].startswith('INSERT INTO snapshot')
                             for call in connection.calls))

    def test_descriptor_crc_mismatch_is_rejected(self):
        meta = {**self.meta, 'descriptor': base64.b64encode(b'other').decode()}
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/gateways/rpi-1/devices/{DEVICE}/calibration-references',
            content=self.image, headers=self.headers(meta=meta))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['code'], 'invalid_request')

    def test_image_hash_mismatch_is_rejected(self):
        meta = {**self.meta, 'image_sha256': 'a' * 64}
        connection = FakeConnection(self.handler())
        response = self.client(connection).post(
            f'/gateways/rpi-1/devices/{DEVICE}/calibration-references',
            content=self.image, headers=self.headers(meta=meta))
        self.assertEqual(response.status_code, 422)


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

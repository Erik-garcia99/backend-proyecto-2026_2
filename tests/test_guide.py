import base64
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from app import main

BRANCH = UUID('22222222-2222-4222-8222-222222222222')
ORG = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
PRODUCT = UUID('33333333-3333-4333-8333-333333333333')
POINT = UUID('44444444-4444-4444-8444-444444444444')
NODE = UUID('55555555-5555-4555-8555-555555555555')
GATEWAY = UUID('66666666-6666-4666-8666-666666666666')
CUSTOMER = UUID('77777777-7777-4777-8777-777777777777')
OWNER = UUID('88888888-8888-4888-8888-888888888888')
RPI = 'rpi-guia-01'
NOW = datetime.now(timezone.utc)


def user(role='customer', uid=CUSTOMER):
    return {'id': uid, 'role': role, 'is_superuser': False,
            'organization_id': ORG, 'branch_id': BRANCH if role != 'customer' else None}


class FakeCursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, destination=True, channels=8, existing=None):
        self.destination = destination
        self.channels = channels
        self.calls = []
        self.idempotency = {}
        self.requests = {}
        self.existing = existing or {}
        self.gateway_row = None
        self.node_row = None
        self.occupants = []
        self.association = None
        self.active_light = False
        self.token_valid = True

    def execute(self, sql, params=None):
        params = params or ()
        self.calls.append((sql, params))
        if 'FROM organization_token t' in sql:
            # Reproducir el fallo real text = uuid sin simular active_token.
            if not isinstance(params[0], str):
                raise AssertionError('token_hash debe compararse con texto, no con el UUID del token')
            if not self.token_valid or params[0] != self.gateway_row['token_hash']:
                return FakeCursor()
            return FakeCursor({'id': self.gateway_row['registered_with_token'],
                               'branch_id': BRANCH, 'organization_id': ORG})
        if 'FROM branch b' in sql:
            return FakeCursor({'id': BRANCH, 'organization_id': ORG})
        if 'FROM idempotency_key' in sql:
            previous = self.idempotency.get(params[1])
            return FakeCursor(previous)
        if 'INSERT INTO idempotency_key' in sql:
            self.idempotency[params[1]] = {
                'body_hash': params[3], 'response': None}
            return FakeCursor()
        if 'UPDATE idempotency_key' in sql:
            self.idempotency[params[2]]['response'] = params[0]
            return FakeCursor()
        if 'FROM guide_product_light' in sql and 'JOIN' in sql and 'ORDER BY' in sql:
            return FakeCursor(rows=[{
                'guide_point_id': POINT, 'light_name': 'Luz 3', 'led_index': 2, 'node_id': NODE,
                'shelf_id': None, 'shelf_name': 'Estante 3',
                'product_id': PRODUCT, 'product_name': 'Agua', 'sku': 'SKU-1'}])
        if 'FROM guide_product_light' in sql and 'lpl.product_id' in sql:
            if not self.destination:
                return FakeCursor(rows=[])
            return FakeCursor(row={
                'guide_point_id': POINT, 'led_index': 2, 'node_id': NODE,
                'rpi_identifier': RPI, 'shelf_name': 'Estante 3',
                'location_label': 'Pasillo central', 'guide_channels': self.channels})
        if 'FROM guide_product_light' in sql and 'WHERE branch_id=%s AND product_id=%s' in sql:
            return FakeCursor(self.association)
        if 'SELECT product_id FROM guide_product_light' in sql:
            return FakeCursor(rows=[{'product_id': value} for value in self.occupants])
        if 'SELECT 1 FROM led_guide_request' in sql and 'guide_point_id=%s' in sql:
            return FakeCursor({'exists': 1} if self.active_light else None)
        if 'FROM led_guide_point gp' in sql:
            return FakeCursor({'id': POINT, 'shelf_name': 'Estante 3'})
        if 'FROM product' in sql and 'is_active' in sql:
            return FakeCursor({'id': PRODUCT, 'name': 'Agua', 'sku': 'SKU-1'})
        if 'INSERT INTO guide_product_light' in sql:
            return FakeCursor()
        if 'DELETE FROM guide_product_light' in sql:
            cursor = FakeCursor()
            cursor.rowcount = 1
            return cursor
        if 'INSERT INTO led_guide_request' in sql:
            keys = ['id', 'branch_id', 'customer_id', 'requester_user_id', 'product_id',
                    'guide_point_id', 'node_id', 'led_index', 'gateway_rpi', 'session_id',
                    'command_id', 'ttl_ms', 'expires_at', 'reservation_until', 'issued_at',
                    'requested_at', 'updated_at', 'shelf_name', 'location_reference']
            row = dict(zip(keys, params))
            row.update({'status': 'pending', 'delivery': 'unknown', 'light': 'unknown',
                        'closing': False, 'intent': None, 'reason': None})
            self.requests[row['id']] = row
            return FakeCursor(row)
        if 'FROM led_guide_request WHERE id=%s' in sql:
            return FakeCursor(self.requests.get(params[0]))
        if 'SET closing=TRUE, intent=%s' in sql:
            row = self.requests[params[-1]]
            row.update({'closing': True, 'intent': params[0]})
            return FakeCursor()
        if 'closing=%s' in sql:
            row = self.requests[params[-1]]
            row.update({'status': params[0], 'light': params[1], 'reason': params[2],
                        'closing': params[3]})
            return FakeCursor()
        if 'delivery=%s' in sql:
            row = self.requests[params[-1]]
            row.update({'status': params[0], 'light': params[1], 'delivery': params[2],
                        'reason': params[3]})
            return FakeCursor()
        if 'FROM gateway g' in sql:
            return FakeCursor(self.gateway_row)
        if 'FROM esp32_node WHERE id=%s AND gateway_id=%s' in sql:
            return FakeCursor(self.node_row)
        if 'UPDATE esp32_node SET guide_driver' in sql:
            return FakeCursor()
        return FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


class GuideApiTests(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.hub = main.gateway_hub

    def use(self, current_user, connection=None, hub_send=None):
        context = {}
        target = connection or self.connection
        patches = [
            patch.object(main.db, 'get_connection',
                         lambda: contextmanager(lambda: (yield target))()),
            patch.object(self.hub, 'send', new_callable=AsyncMock,
                         side_effect=hub_send or self._no_send),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        main.app.dependency_overrides[main.current_user] = lambda: current_user
        self.addCleanup(main.app.dependency_overrides.clear)
        context['client'] = TestClient(main.app)
        return context['client']

    async def _no_send(self, *_args, **_kwargs):
        raise RuntimeError('El gateway no respondió')

    def _create(self, client, key='idempotencia-123456', product=PRODUCT):
        return client.post(f'/branches/{BRANCH}/guide-requests',
                           headers={'Idempotency-Key': key},
                           json={'product_id': str(product)})

    def test_create_pending_when_gateway_does_not_answer(self):
        client = self.use(user())
        response = self._create(client)
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body['status'], 'pending')
        self.assertEqual(body['light'], 'unknown')
        self.assertEqual(body['reason'], 'gateway_timeout')
        self.assertEqual(body['location']['shelfName'], 'Estante 3')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('channel', body)

    def test_create_active_when_node_applies(self):
        async def applied(*_args, **_kwargs):
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'on',
                                         'channel': 2, 'session_id': 'x', 'node_boot': 'abc'}}
        client = self.use(user(), hub_send=applied)
        body = self._create(client).json()
        self.assertEqual(body['status'], 'active')
        self.assertEqual(body['light'], 'on')
        self.assertEqual(body['ttlSeconds'], 60)

    async def _busy(self, *_args, **_kwargs):
        return {'ok': False, 'error': 'guide_node_unavailable'}

    def test_idempotent_replay_without_second_dispatch(self):
        async def applied(*_args, **_kwargs):
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'on'}}
        client = self.use(user(), hub_send=applied)
        first = self._create(client).json()
        first_call_count = self.hub.send.call_count
        second = self._create(client).json()
        self.assertEqual(first, second)
        self.assertEqual(self.hub.send.call_count, first_call_count)
        self.assertEqual(len(self.connection.requests), 1)

    def test_idempotency_conflict(self):
        client = self.use(user())
        self._create(client)
        conflict = self._create(client, product=UUID('99999999-9999-4999-8999-999999999999'))
        self.assertEqual(conflict.status_code, 409)

    def test_owner_isolation(self):
        client = self.use(user())
        request_id = self._create(client).json()['id']
        other = user(uid=UUID('aaaaaaaa-0000-4000-8000-000000000001'))
        client_other = self.use(other)
        self.assertEqual(client_other.get(
            f'/branches/{BRANCH}/guide-requests/{request_id}').status_code, 404)

    def test_confirm_closes_with_real_off_result(self):
        async def applied(*_args, **_kwargs):
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'on'}}
        client = self.use(user(), hub_send=applied)
        request_id = self._create(client).json()['id']

        async def closed(*_args, **_kwargs):
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'off'}}
        with patch.object(self.hub, 'send', new_callable=AsyncMock, side_effect=closed):
            response = client.post(f'/branches/{BRANCH}/guide-requests/{request_id}/confirm')
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body['status'], 'confirmed')
        self.assertEqual(body['light'], 'off')
        self.assertFalse(body['closing'])

    def test_cancel_is_retryable(self):
        async def unavailable(*_args, **_kwargs):
            return {'ok': True, 'data': {'delivery': 'unknown', 'light': 'unknown'}}
        client = self.use(user(), hub_send=unavailable)
        request_id = self._create(client).json()['id']
        first = client.delete(f'/branches/{BRANCH}/guide-requests/{request_id}').json()
        self.assertTrue(first['closing'])
        with patch.object(self.hub, 'send', new_callable=AsyncMock,
                          side_effect=AssertionError('no debe reenviar')):
            second = client.delete(f'/branches/{BRANCH}/guide-requests/{request_id}').json()
        self.assertEqual(first, second)

    def test_busy_channel_returns_409_not_500(self):
        """psycopg3 usa `sqlstate`; con `pgcode` el 409 se convertía en 500."""
        class Busy(FakeConnection):
            def execute(self, sql, params=None):
                if 'INSERT INTO led_guide_request' in sql:
                    error = Exception('duplicate key value violates unique constraint')
                    error.sqlstate = '23505'
                    raise error
                return super().execute(sql, params)

        client = self.use(user(), connection=Busy())
        response = self._create(client)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail'], 'guide_channel_busy')

    def test_stale_reservation_is_released_before_reserving(self):
        """Un `active` viejo o un cierre abandonado no pueden bloquear el canal."""
        client = self.use(user())
        response = self._create(client)
        self.assertEqual(response.status_code, 202, response.text)
        releases = [sql for sql, _ in self.connection.calls
                    if "status = CASE WHEN closing THEN 'failed'" in sql]
        self.assertEqual(len(releases), 1)
        self.assertIn("reason = CASE WHEN closing THEN 'off_unverified' ELSE 'ttl'", releases[0])

    def test_read_guide_expires_active_session_past_its_ttl(self):
        async def applied(*_args, **_kwargs):
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'on'}}

        client = self.use(user(), hub_send=applied)
        request_id = UUID(self._create(client).json()['id'])
        self.connection.requests[request_id].update({
            'status': 'active', 'light': 'on',
            'expires_at': NOW - timedelta(seconds=61)})
        body = client.get(f'/branches/{BRANCH}/guide-requests/{request_id}').json()
        self.assertEqual(body['status'], 'expired')
        self.assertEqual(body['reason'], 'ttl')
        self.assertEqual(body['light'], 'unknown')

    def test_gateway_wait_stays_below_mobile_timeout(self):
        """La app aborta a los 15 s: start y stop no deben retener la respuesta."""
        seen = []

        async def applied(*_args, **kwargs):
            seen.append(kwargs.get('timeout'))
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'on'}}

        client = self.use(user(), hub_send=applied)
        request_id = self._create(client).json()['id']

        async def closed(*_args, **kwargs):
            seen.append(kwargs.get('timeout'))
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'off'}}

        with patch.object(self.hub, 'send', new_callable=AsyncMock, side_effect=closed):
            response = client.delete(f'/branches/{BRANCH}/guide-requests/{request_id}')
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(seen), 2)
        for timeout in seen:
            self.assertIsNotNone(timeout)
            self.assertLessEqual(timeout, 15)

    def test_missing_destination_and_missing_capabilities(self):
        client = self.use(user(), connection=FakeConnection(destination=False))
        self.assertEqual(self._create(client).status_code, 404)
        client = self.use(user(), connection=FakeConnection(channels=None))
        self.assertEqual(self._create(client).status_code, 503)

    def test_resolution_never_touches_roi(self):
        client = self.use(user())
        self._create(client)
        executed = ' '.join(sql for sql, _ in self.connection.calls)
        self.assertNotIn('roi', executed)
        self.assertIn('guide_product_light', executed)

    def test_association_crud_is_staff_only_and_independent_of_roi(self):
        owner = user('owner', OWNER)
        client = self.use(owner)
        listed = client.get(f'/branches/{BRANCH}/guide-associations')
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()[0]['productId'], str(PRODUCT))
        self.assertEqual(listed.json()[0]['nodeId'], str(NODE))
        assigned = client.post(f'/branches/{BRANCH}/guide-associations',
                               json={'product_id': str(PRODUCT), 'guide_point_id': str(POINT)})
        self.assertEqual(assigned.status_code, 201, assigned.text)
        removed = client.delete(f'/branches/{BRANCH}/guide-associations/{PRODUCT}')
        self.assertEqual(removed.status_code, 204, removed.text)

        employee = self.use(user('employee', OWNER))
        self.assertEqual(employee.get(f'/branches/{BRANCH}/guide-associations').status_code, 200)
        for response in (
            employee.post(f'/branches/{BRANCH}/guide-associations',
                          json={'product_id': str(PRODUCT), 'guide_point_id': str(POINT)}),
            employee.delete(f'/branches/{BRANCH}/guide-associations/{PRODUCT}'),
        ):
            self.assertEqual(response.status_code, 403)

    def test_employee_can_turn_on_and_off_without_roi(self):
        async def applied(*_args, **_kwargs):
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'on'}}
        client = self.use(user('employee', OWNER), hub_send=applied)
        started = self._create(client)
        self.assertEqual(started.status_code, 202, started.text)
        self.assertEqual(started.json()['light'], 'on')
        async def off(*_args, **_kwargs):
            return {'ok': True, 'data': {'delivery': 'applied', 'light': 'off'}}
        with patch.object(self.hub, 'send', new_callable=AsyncMock, side_effect=off):
            stopped = client.delete(f'/branches/{BRANCH}/guide-requests/{started.json()["id"]}')
        self.assertEqual(stopped.status_code, 202, stopped.text)
        self.assertEqual(stopped.json()['light'], 'off')
        self.assertNotIn('roi', ' '.join(sql for sql, _ in self.connection.calls).lower())

    def test_customer_and_foreign_employee_cannot_list_associations(self):
        client = self.use(user())
        self.assertEqual(client.get(f'/branches/{BRANCH}/guide-associations').status_code, 403)
        other = user('employee', OWNER)
        other['branch_id'] = ORG
        client = self.use(other)
        self.assertEqual(client.get(f'/branches/{BRANCH}/guide-associations').status_code, 403)

    def test_invalid_body_and_key(self):
        client = self.use(user())
        self.assertEqual(client.post(f'/branches/{BRANCH}/guide-requests',
                                     json={'product_id': str(PRODUCT)}).status_code, 422)
        self.assertEqual(client.post(f'/branches/{BRANCH}/guide-requests',
                                     headers={'Idempotency-Key': 'abc'},
                                     json={'product_id': str(PRODUCT)}).status_code, 422)
        self.assertEqual(client.post(f'/branches/{BRANCH}/guide-requests',
                                     headers={'Idempotency-Key': 'idempotencia-123456'},
                                     json={'product_id': str(PRODUCT),
                                           'channel': 2}).status_code, 422)

    def _assign(self, **extra):
        client = self.use(user('owner', OWNER))
        return client.post(f'/branches/{BRANCH}/guide-associations',
                           json={'product_id': str(PRODUCT),
                                 'guide_point_id': str(POINT), **extra})

    def test_occupied_led_requires_explicit_replacement(self):
        self.connection.occupants = [OWNER]
        self.assertEqual(self._assign().status_code, 409)
        self.assertFalse(any('INSERT INTO guide_product_light' in sql
                             for sql, _ in self.connection.calls))

    def test_replacement_deletes_only_expected_product_before_insert(self):
        self.connection.occupants = [OWNER]
        response = self._assign(replace_product_id=str(OWNER))
        self.assertEqual(response.status_code, 201, response.text)
        writes = [(sql, params) for sql, params in self.connection.calls
                  if 'DELETE FROM guide_product_light' in sql or
                  'INSERT INTO guide_product_light' in sql]
        self.assertEqual(len(writes), 2)
        self.assertIn('DELETE', writes[0][0])
        self.assertEqual(writes[0][1], (BRANCH, POINT, OWNER))
        self.assertIn('INSERT', writes[1][0])

    def test_stale_replacement_does_not_delete_anything(self):
        self.connection.occupants = [CUSTOMER]
        self.assertEqual(self._assign(replace_product_id=str(OWNER)).status_code, 409)
        self.assertFalse(any('DELETE FROM guide_product_light' in sql
                             for sql, _ in self.connection.calls))

    def test_active_led_cannot_be_replaced(self):
        self.connection.occupants = [OWNER]
        self.connection.active_light = True
        self.assertEqual(self._assign(replace_product_id=str(OWNER)).status_code, 409)
        self.assertFalse(any('DELETE FROM guide_product_light' in sql
                             for sql, _ in self.connection.calls))

    def test_product_on_another_led_is_not_silently_moved(self):
        self.connection.association = {'guide_point_id': NODE}
        self.assertEqual(self._assign().status_code, 409)

    def test_same_association_is_idempotent(self):
        self.connection.association = {'guide_point_id': POINT}
        self.assertEqual(self._assign().status_code, 201)
        self.assertFalse(any('INSERT INTO guide_product_light' in sql
                             for sql, _ in self.connection.calls))


class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.connection = FakeConnection()
        self.connection.gateway_row = {
            'id': GATEWAY, 'device_public_key': base64.b64encode(public).decode(),
            'registered_with_token': uuid4(), 'branch_id': BRANCH,
            'token_hash': 'a' * 64}
        self.connection.node_row = {'id': NODE}

    def use(self):
        patches = [
            patch.object(main.db, 'get_connection',
                         lambda: contextmanager(lambda: (yield self.connection))()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return TestClient(main.app)

    def _post(self, client, boot='1f3ac9d2', channels=8, tamper=False):
        timestamp = int(__import__('time').time())
        nonce = 'nonce-seguro-1234567890'
        map_values = [0, 1, 3, 4, 5, 6, 7, 10][:channels]
        map_csv = ','.join(str(value) for value in map_values)
        message = (f'selfiq-guide-capabilities-v1\n{RPI}\n{timestamp}\n{nonce}\n'
                   f'{NODE}\n{boot}\ndirect\n{channels}\n{map_csv}').encode()
        if tamper:
            message += b'x'
        signature = base64.b64encode(self.private.sign(message)).decode()
        body = {'timestamp': timestamp, 'nonce': nonce, 'signature': signature,
                'boot': boot, 'driver': 'direct', 'channels': channels,
                'channels_map': map_values, 'fw': '1.0.0'}
        return client.post(f'/gateways/{RPI}/nodes/{NODE}/guide-capabilities', json=body)

    def test_signed_capabilities_are_stored(self):
        response = self._post(self.use())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['ok'])

    def test_capabilities_validate_hash_not_registered_token_uuid(self):
        response = self._post(self.use())
        self.assertEqual(response.status_code, 200, response.text)
        gateway_sql = next(sql for sql, _ in self.connection.calls if 'FROM gateway g' in sql)
        self.assertIn('t.id=g.registered_with_token', gateway_sql)
        sql, params = next((sql, params) for sql, params in self.connection.calls
                           if 'FROM organization_token t' in sql)
        self.assertEqual(params, ('a' * 64, BRANCH, BRANCH))
        self.assertIn('t.token_hash=%s', sql)
        self.assertIn('t.revoked_at IS NULL', sql)
        self.assertIn('t.expires_at>NOW()', sql)
        self.assertIn("'register_gateway'=ANY(t.scopes)", sql)

    def test_capabilities_reject_missing_registered_token(self):
        self.connection.gateway_row['token_hash'] = None
        response = self._post(self.use())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(any('UPDATE esp32_node SET guide_driver' in sql
                             for sql, _ in self.connection.calls))

    def test_capabilities_reject_inactive_token_without_updating_channels(self):
        self.connection.token_valid = False
        response = self._post(self.use())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(any('UPDATE esp32_node SET guide_driver' in sql
                             for sql, _ in self.connection.calls))

    def test_tampered_signature_is_rejected(self):
        response = self._post(self.use(), tamper=True)
        self.assertEqual(response.status_code, 401)

    def test_invalid_map_is_rejected_before_signature(self):
        client = self.use()
        body = {'timestamp': int(__import__('time').time()),
                'nonce': 'nonce-seguro-1234567890', 'signature': 'A' * 88,
                'boot': '1f3ac9d2', 'driver': 'direct', 'channels': 8,
                'channels_map': [0, 0, 0, 0, 0, 0, 0, 0]}
        response = client.post(f'/gateways/{RPI}/nodes/{NODE}/guide-capabilities', json=body)
        self.assertEqual(response.status_code, 422)


if __name__ == '__main__':
    unittest.main()

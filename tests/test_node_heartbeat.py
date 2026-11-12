import base64
import json
import time
import unittest
from datetime import datetime, timezone
from contextlib import contextmanager
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import node_registration

GATEWAY = 'rpi-test'
NODE = '11111111-1111-4111-8111-111111111111'


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.gateway = {'id': 'internal', 'branch_id': 'branch', 'device_public_key':
                        base64.b64encode(public).decode(), 'registered_with_token': 'token',
                        'token_hash': 'hash'}
        self.updates = []
        self.node = {'id': NODE, 'node_code': 'NODO-TEST', 'registration_status': 'registered',
                     'registered_at': datetime.now(timezone.utc)}

        def execute(sql, params):
            if sql.lstrip().startswith('UPDATE esp32_node'):
                self.updates.append(params)
                return SimpleNamespace(fetchone=lambda: {'id': params[0]})
            if 'FROM gateway g' in sql:
                return SimpleNamespace(fetchone=lambda: dict(self.gateway))
            if 'FROM esp32_node' in sql:
                return SimpleNamespace(fetchone=lambda: self.node)
            return SimpleNamespace(fetchone=lambda: {'id': 'token'})

        self.connection = SimpleNamespace(execute=execute, commit=lambda: None)

        @contextmanager
        def connect():
            yield self.connection

        app = FastAPI()
        app.include_router(node_registration.make_router(connect, SimpleNamespace(), lambda: {}))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def payload(self, node_id=NODE, timestamp=None, action='heartbeat'):
        timestamp = int(time.time()) if timestamp is None else timestamp
        message = (f'selfiq-node-{action}-v1\n{GATEWAY}\n{timestamp}\nnonce-1234567890\n{node_id}').encode()
        return {'timestamp': timestamp, 'nonce': 'nonce-1234567890',
                'signature': base64.b64encode(self.key.sign(message)).decode()}

    def test_signed_heartbeat_marks_node_online(self):
        response = self.client.post(f'/gateways/{GATEWAY}/nodes/{NODE}/heartbeat', json=self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.updates, [(node_registration.UUID(NODE), 'internal')])

    def test_authorization_requires_confirmed_registration_without_claiming_presence(self):
        url = f'/gateways/{GATEWAY}/nodes/{NODE}/authorize'
        result = self.client.post(url, json=self.payload(action='authorize'))
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['authorized'])
        self.assertEqual(result.json()['registered_at'], self.node['registered_at'].timestamp())
        self.assertFalse(self.updates)
        for change in ({'node_code': None}, {'registration_status': 'pending'}, {'registered_at': None}):
            old = self.node.copy()
            self.node.update(change)
            self.assertFalse(self.client.post(url, json=self.payload(action='authorize')).json()['authorized'])
            self.node = old
        self.node = None
        self.assertFalse(self.client.post(url, json=self.payload(action='authorize')).json()['authorized'])

    def test_authorization_rejects_heartbeat_signature_and_revoked_gateway(self):
        url = f'/gateways/{GATEWAY}/nodes/{NODE}/authorize'
        self.assertEqual(self.client.post(url, json=self.payload()).status_code, 401)
        self.gateway['token_hash'] = None
        self.assertEqual(self.client.post(url, json=self.payload(action='authorize')).status_code, 403)

    def test_signature_cannot_be_reused_for_another_node(self):
        response = self.client.post(
            f'/gateways/{GATEWAY}/nodes/22222222-2222-4222-8222-222222222222/heartbeat',
            json=self.payload())
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.updates)

    def test_expired_or_tampered_heartbeat_is_rejected(self):
        self.assertEqual(self.client.post(
            f'/gateways/{GATEWAY}/nodes/{NODE}/heartbeat',
            json=self.payload(timestamp=int(time.time()) - 600)).status_code, 401)
        body = self.payload()
        body['nonce'] = 'otro-nonce-123456'
        self.assertEqual(self.client.post(
            f'/gateways/{GATEWAY}/nodes/{NODE}/heartbeat', json=body).status_code, 401)
        self.assertFalse(self.updates)

    def test_unknown_node_does_not_claim_presence(self):
        def execute(sql, params):
            if sql.lstrip().startswith('UPDATE esp32_node'):
                return SimpleNamespace(fetchone=lambda: None)
            if 'FROM gateway g' in sql:
                return SimpleNamespace(fetchone=lambda: dict(self.gateway))
            return SimpleNamespace(fetchone=lambda: {'id': 'token'})
        self.connection.execute = execute
        response = self.client.post(f'/gateways/{GATEWAY}/nodes/{NODE}/heartbeat', json=self.payload())
        self.assertEqual(response.status_code, 404)

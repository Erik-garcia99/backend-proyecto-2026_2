import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.gateways import create_router


class GatewayNamesTests(unittest.TestCase):
    def setUp(self):
        self.branch = UUID('11111111-1111-4111-8111-111111111111')
        self.organization = UUID('22222222-2222-4222-8222-222222222222')
        self.user = dict(role='manager', is_superuser=False,
                         organization_id=self.organization, branch_id=self.branch)
        self.gateway = dict(id='internal', branch_id=self.branch,
                            organization_id=self.organization, rpi_identifier='rpi-long-id',
                            hostname='technical-host', display_name='Gateway-1',
                            registered_with_token='token', device_public_key='key')
        self.writes = []

        def execute(sql, params):
            if sql.startswith('UPDATE gateway'):
                self.writes.append(params)
                self.gateway['display_name'] = params[0]
            return SimpleNamespace(fetchone=lambda: dict(self.gateway))

        connection = SimpleNamespace(execute=execute, commit=Mock())

        @contextmanager
        def connect():
            yield connection

        app = FastAPI()
        app.include_router(create_router(SimpleNamespace(sessions={}), lambda: self.user, connect))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_rename_survives_read_without_changing_technical_identity(self):
        response = self.client.patch('/gateways/rpi-long-id', json={'name': '  Entrada principal  '})
        self.assertEqual(response.status_code, 200)
        result = self.client.get('/gateways/rpi-long-id').json()
        self.assertEqual(result['name'], 'Entrada principal')
        self.assertEqual(result['gateway_id'], 'rpi-long-id')
        self.assertEqual(self.gateway['hostname'], 'technical-host')

    def test_blank_or_long_name_cannot_be_saved(self):
        for name in ('   ', 'x' * 121):
            self.assertEqual(self.client.patch('/gateways/rpi-long-id', json={'name': name}).status_code, 422)
        self.assertFalse(self.writes)

    def test_other_branch_cannot_rename_gateway(self):
        self.user['branch_id'] = UUID('33333333-3333-4333-8333-333333333333')
        self.assertEqual(self.client.patch('/gateways/rpi-long-id', json={'name': 'Entrada'}).status_code, 403)
        self.assertFalse(self.writes)

    def test_employee_cannot_rename_gateway(self):
        self.user['role'] = 'employee'
        self.assertEqual(self.client.patch('/gateways/rpi-long-id', json={'name': 'Entrada'}).status_code, 403)
        self.assertFalse(self.writes)

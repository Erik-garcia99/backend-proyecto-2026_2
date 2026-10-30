import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import devices

BRANCH = UUID('11111111-1111-4111-8111-111111111111')
OTHER = UUID('22222222-2222-4222-8222-222222222222')


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        devices._discoveries.clear()
        self.user = {'role': 'manager', 'is_superuser': False,
                     'organization_id': OTHER, 'branch_id': BRANCH}
        self.hub = SimpleNamespace(send=AsyncMock(return_value={'ok': False}), sessions={})
        connection = SimpleNamespace(execute=lambda *args: SimpleNamespace(
            fetchone=lambda: {'id': BRANCH, 'organization_id': OTHER}))

        @contextmanager
        def connect():
            yield connection

        app = FastAPI()
        app.include_router(devices.create_router(lambda: self.user, connect, self.hub))
        self.client = TestClient(app)
        self.session = {'id': 'search', 'branch_id': BRANCH, 'gateway_id': 'rpi',
                        'expires_at': time.time() + 120}
        devices._discoveries['search'] = self.session

    def tearDown(self):
        self.client.close()
        devices._discoveries.clear()

    def test_rejected_start_does_not_create_session(self):
        devices._discoveries.clear()
        response = self.client.post(f'/branches/{BRANCH}/gateways/rpi/discovery')
        self.assertEqual(response.status_code, 502)
        self.assertFalse(devices._discoveries)

    def test_failed_stop_preserves_session_for_retry(self):
        response = self.client.post(f'/branches/{BRANCH}/discoveries/search/stop')
        self.assertEqual(response.status_code, 502)
        self.assertIn('search', devices._discoveries)
        self.hub.send.return_value = {'ok': True}
        self.assertEqual(self.client.post(
            f'/branches/{BRANCH}/discoveries/search/stop').status_code, 200)
        self.assertNotIn('search', devices._discoveries)

    def test_other_branch_cannot_stop_session(self):
        response = self.client.post(f'/branches/{OTHER}/discoveries/search/stop')
        self.assertEqual(response.status_code, 404)
        self.assertIn('search', devices._discoveries)
        self.hub.send.assert_not_called()

    def test_unauthorized_user_cannot_stop_session(self):
        self.user['role'] = 'employee'
        response = self.client.post(f'/branches/{BRANCH}/discoveries/search/stop')
        self.assertEqual(response.status_code, 403)
        self.assertIn('search', devices._discoveries)
        self.hub.send.assert_not_called()

    def test_new_node_requires_active_search_in_same_gateway_and_branch(self):
        self.assertTrue(devices.pairing_allowed('rpi', BRANCH))
        self.assertFalse(devices.pairing_allowed('other-rpi', BRANCH))
        self.assertFalse(devices.pairing_allowed('rpi', OTHER))
        self.session['expires_at'] = time.time() - 1
        self.assertFalse(devices.pairing_allowed('rpi', BRANCH))
        self.session['expires_at'] = time.time() + 120
        self.session['stopped'] = True
        self.assertFalse(devices.pairing_allowed('rpi', BRANCH))


class ExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_closes_ap_without_polling(self):
        hub = SimpleNamespace(send=AsyncMock(return_value={'ok': True}))
        await devices.expire_discovery({'gateway_id': 'rpi', 'expires_at': 0}, hub)
        hub.send.assert_awaited_once_with('rpi', 'tcp.disable')

    async def test_stopped_session_does_not_close_new_search(self):
        hub = SimpleNamespace(send=AsyncMock())
        await devices.expire_discovery({'stopped': True, 'expires_at': 0}, hub)
        hub.send.assert_not_called()


class DeleteTests(unittest.TestCase):
    def test_deletes_only_after_exact_node_confirmation(self):
        node_id = OTHER
        for result, expected in [({'ok': True, 'data': {'published': True}}, 502),
                                 ({'ok': False, 'error': 'Sin respuesta del nodo'}, 502),
                                 ({'ok': True, 'data': {'identity_reset': True, 'node_id': 'otro'}}, 502),
                                 ({'ok': True, 'data': {'identity_reset': True, 'node_id': str(node_id)}}, 200)]:
            with self.subTest(result=result):
                connection = Mock()
                @contextmanager
                def connect():
                    yield connection
                hub = SimpleNamespace(send=AsyncMock(return_value=result))
                app = FastAPI()
                app.include_router(devices.create_router(lambda: {}, connect, hub))
                with patch.object(devices, 'require_branch'), patch.object(devices, 'load_node',
                        return_value={'rpi_identifier': 'rpi', 'node_type': 'guide'}), TestClient(app) as client:
                    response = client.delete(f'/branches/{BRANCH}/devices/{node_id}')
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    connection.execute.assert_called_once_with('DELETE FROM esp32_node WHERE id=%s', (node_id,))
                    connection.commit.assert_called_once()
                else:
                    connection.execute.assert_not_called()

"""ME-0: rotación del refresh y revocación efectiva del access token (SEC-14).

Los tokens nuevos llevan `sid` (id de auth_session). Al rotar o cerrar sesión,
esa sesión queda revocada y `current_user` rechaza el access token aunque su
`exp` todavía no haya llegado. Se usan conexiones falsas, como en test_auth_session.
"""
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from app import main
from app.security import create_access_token, hash_password

ORG = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
USER_ID = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
SESSION_ID = UUID('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee')


def make_user():
    return {
        'id': USER_ID,
        'email': 'test@selfiq.test',
        'first_name': 'Test',
        'last_name': 'User',
        'phone_number': None,
        'role': 'employee',
        'account_status': 'active',
        'organization_id': ORG,
        'branch_id': None,
        'deactivated_at': None,
        'password_hash': hash_password('ClaveSegura123'),
        'is_superuser': False,
    }


def make_session(**overrides):
    session = {
        'id': SESSION_ID,
        'user_id': USER_ID,
        'expires_at': datetime.now(timezone.utc) + timedelta(days=30),
        'revoked_at': None,
    }
    session.update(overrides)
    return session


class FakeCursor:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        if isinstance(self.result, list):
            return self.result[0] if self.result else None
        return self.result


class FakeConnection:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.executed = []
        self.commits = 0

    def execute(self, query, params=None):
        self.executed.append((query, params))
        result = self.results.pop(0) if self.results else None
        return FakeCursor(result)

    def commit(self):
        self.commits += 1


def use_connection(connection):
    @contextmanager
    def get_connection():
        yield connection
    return patch.object(main.db, 'get_connection', get_connection)


def auth_header(session_id=None):
    token = create_access_token(str(USER_ID), session_id)
    return {'Authorization': f'Bearer {token}'}


class SessionBindingTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.app.dependency_overrides.clear()

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_login_access_token_lleva_el_id_de_sesion(self):
        conn = FakeConnection(results=[make_user()])
        with use_connection(conn):
            r = self.client.post('/auth/login', json={'email': 'test@selfiq.test', 'password': 'ClaveSegura123'})
        self.assertEqual(r.status_code, 200, r.text)
        insert = next((p for q, p in conn.executed if 'INSERT INTO auth_session' in q), None)
        self.assertIsNotNone(insert)
        import jwt
        payload = jwt.decode(r.json()['access_token'], options={'verify_signature': False, 'verify_exp': False})
        self.assertEqual(payload['sid'], str(insert[0]))

    def test_refresh_rotates_refresh_token_and_session(self):
        conn = FakeConnection(results=[make_session(), make_user()])
        with use_connection(conn):
            r = self.client.post('/auth/refresh', json={'refresh_token': 'refresh-viejo'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn('refresh_token', body)
        self.assertNotEqual(body['refresh_token'], 'refresh-viejo')
        revoked = [p for q, p in conn.executed if 'SET revoked_at=NOW()' in q]
        inserts = [p for q, p in conn.executed if 'INSERT INTO auth_session' in q]
        self.assertEqual(len(revoked), 1)
        self.assertEqual(revoked[0][0], SESSION_ID)
        self.assertEqual(len(inserts), 1)
        import jwt
        payload = jwt.decode(body['access_token'], options={'verify_signature': False, 'verify_exp': False})
        self.assertEqual(payload['sid'], str(inserts[0][0]))

    def test_refresh_token_viejo_es_rechazado_tras_rotacion(self):
        conn = FakeConnection(results=[make_session(revoked_at=datetime.now(timezone.utc))])
        with use_connection(conn):
            r = self.client.post('/auth/refresh', json={'refresh_token': 'refresh-viejo'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('cerrada', r.json()['detail'])

    def test_current_user_acepta_sesion_activa_y_rechaza_la_revocada(self):
        conn = FakeConnection(results=[{'id': SESSION_ID}, make_user()])
        with use_connection(conn):
            ok = self.client.get('/auth/me', headers=auth_header(SESSION_ID))
        self.assertEqual(ok.status_code, 200, ok.text)

        revoked = FakeConnection(results=[None])
        with use_connection(revoked):
            denied = self.client.get('/auth/me', headers=auth_header(SESSION_ID))
        self.assertEqual(denied.status_code, 401)
        self.assertIn('sesión fue cerrada', denied.json()['detail'].lower())

    def test_current_user_sigue_aceptando_tokens_heredados_sin_sid(self):
        # Los tokens emitidos antes de este cambio no tienen `sid`; no se les
        # exige la consulta de sesión y siguen válidos hasta su exp.
        conn = FakeConnection(results=[make_user()])
        with use_connection(conn):
            r = self.client.get('/auth/me', headers=auth_header())
        self.assertEqual(r.status_code, 200, r.text)
        session_queries = [q for q, _ in conn.executed if 'FROM auth_session' in q]
        self.assertEqual(session_queries, [])

    def test_logout_revoca_y_el_access_token_deja_de_servir(self):
        user = make_user()
        # 1) current_user valida el sid, 2) current_user lee el usuario,
        # 3) logout comprueba el RETURNING de la revocación.
        conn = FakeConnection(results=[{'id': SESSION_ID}, user, {'id': SESSION_ID}])
        with use_connection(conn):
            out = self.client.post('/auth/logout', json={'refresh_token': 'refresh-actual'},
                                   headers=auth_header(SESSION_ID))
        self.assertEqual(out.status_code, 200, out.text)
        revoked = [p for q, p in conn.executed if 'UPDATE auth_session SET revoked_at' in q]
        self.assertEqual(len(revoked), 1)

        denied_conn = FakeConnection(results=[None])
        with use_connection(denied_conn):
            denied = self.client.get('/auth/me', headers=auth_header(SESSION_ID))
        self.assertEqual(denied.status_code, 401)


if __name__ == '__main__':
    unittest.main()

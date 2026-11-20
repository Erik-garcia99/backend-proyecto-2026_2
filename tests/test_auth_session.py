"""Tests de sesión: login con refresh, refresh, logout, expiración y rehash (CL1)."""
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from app import main
from app.security import hash_password, hash_refresh_token, verify_password

ORG = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
USER_ID = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
SESSION_ID = UUID('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee')

# Hash bcrypt válido de 'ClaveSegura123'
import bcrypt
BCRYPT_HASH = bcrypt.hashpw(b'ClaveSegura123', bcrypt.gensalt()).decode()


def make_user(overrides=None):
    user = {
        'id': USER_ID,
        'email': 'test@selfiq.test',
        'first_name': 'Test',
        'last_name': 'User',
        'phone_number': None,
        'role': 'customer',
        'account_status': 'active',
        'organization_id': ORG,
        'branch_id': None,
        'deactivated_at': None,
        'password_hash': hash_password('ClaveSegura123'),
        'is_superuser': False,
    }
    if overrides:
        user.update(overrides)
    return user


class FakeCursor:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        if isinstance(self.result, list):
            return self.result[0] if self.result else None
        return self.result

    def fetchall(self):
        if isinstance(self.result, list):
            return self.result
        return [self.result] if self.result is not None else []


class FakeConnection:
    def __init__(self, results=None, error=None, error_at=None):
        self.results = list(results or [])
        self.error = error
        self.error_at = error_at
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if self.error and (self.error_at is None or len(self.executed) == self.error_at):
            raise self.error
        result = self.results.pop(0) if self.results else None
        return FakeCursor(result)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def use_connection(connection):
    @contextmanager
    def get_connection():
        yield connection
    return patch.object(main.db, 'get_connection', get_connection)


class LoginSessionTests(unittest.TestCase):
    """Login devuelve access_token + refresh_token y crea auth_session."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.app.dependency_overrides.clear()

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_login_returns_refresh_token(self):
        user = make_user()
        # Query 1: SELECT user, Query 2: INSERT auth_session
        conn = FakeConnection(results=[user])
        with use_connection(conn):
            r = self.client.post('/auth/login', json={'email': 'test@selfiq.test', 'password': 'ClaveSegura123'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn('access_token', body)
        self.assertIn('refresh_token', body)
        self.assertIn('expires_in', body)
        self.assertEqual(body['expires_in'], main.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
        self.assertEqual(body['user']['id'], str(USER_ID))
        # Verifica que se insertó en auth_session
        insert_queries = [q for q, _ in conn.executed if 'INSERT INTO auth_session' in q]
        self.assertEqual(len(insert_queries), 1)
        self.assertGreaterEqual(conn.commits, 1)

    def test_login_wrong_password(self):
        user = make_user()
        conn = FakeConnection(results=[user])
        with use_connection(conn):
            r = self.client.post('/auth/login', json={'email': 'test@selfiq.test', 'password': 'MalaClave999'})
        self.assertEqual(r.status_code, 401)

    def test_login_inactive_account(self):
        user = make_user({'account_status': 'inactive', 'deactivated_at': datetime.now(timezone.utc)})
        conn = FakeConnection(results=[user])
        with use_connection(conn):
            r = self.client.post('/auth/login', json={'email': 'test@selfiq.test', 'password': 'ClaveSegura123'})
        self.assertEqual(r.status_code, 403)


class LoginRehashTests(unittest.TestCase):
    """D-C15: login con bcrypt migra a Argon2id."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.app.dependency_overrides.clear()

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_login_rehashes_bcrypt_to_argon2id(self):
        user = make_user({'password_hash': BCRYPT_HASH})
        # Query 1: SELECT user
        # Query 2: UPDATE password_hash (rehash)
        # Query 3: INSERT auth_session
        conn = FakeConnection(results=[user])
        with use_connection(conn):
            r = self.client.post('/auth/login', json={'email': 'test@selfiq.test', 'password': 'ClaveSegura123'})
        self.assertEqual(r.status_code, 200, r.text)
        # Verifica que se hizo UPDATE del hash
        rehash_queries = [q for q, _ in conn.executed if 'UPDATE "user" SET password_hash' in q]
        self.assertEqual(len(rehash_queries), 1, 'Debería hacer rehash de bcrypt a Argon2id')
        # El nuevo hash debería ser argon2id
        _, params = next((q, p) for q, p in conn.executed if 'UPDATE "user" SET password_hash' in q)
        self.assertTrue(params[0].startswith('$argon2'), f'Hash nuevo debería ser Argon2id: {params[0][:20]}')

    def test_login_does_not_rehash_argon2id(self):
        user = make_user()  # make_user ya usa hash_password que genera Argon2id
        conn = FakeConnection(results=[user])
        with use_connection(conn):
            r = self.client.post('/auth/login', json={'email': 'test@selfiq.test', 'password': 'ClaveSegura123'})
        self.assertEqual(r.status_code, 200, r.text)
        rehash_queries = [q for q, _ in conn.executed if 'UPDATE "user" SET password_hash' in q]
        self.assertEqual(len(rehash_queries), 0, 'No debería hacer rehash si ya es Argon2id')


class RefreshTests(unittest.TestCase):
    """POST /auth/refresh emite un nuevo access_token."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.app.dependency_overrides.clear()

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_refresh_returns_new_access_token(self):
        session = {
            'id': SESSION_ID,
            'user_id': USER_ID,
            'expires_at': datetime.now(timezone.utc) + timedelta(days=30),
            'revoked_at': None,
        }
        user = make_user()
        conn = FakeConnection(results=[session, user])
        with use_connection(conn):
            r = self.client.post('/auth/refresh', json={'refresh_token': 'valid-token-abc'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn('access_token', body)
        self.assertIn('expires_in', body)
        # Verifica que se actualizó last_used_at
        update_queries = [q for q, _ in conn.executed if 'UPDATE auth_session SET last_used_at' in q]
        self.assertEqual(len(update_queries), 1)

    def test_refresh_invalid_token(self):
        conn = FakeConnection(results=[None])
        with use_connection(conn):
            r = self.client.post('/auth/refresh', json={'refresh_token': 'bad-token'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('inválido', r.json()['detail'])

    def test_refresh_revoked_session(self):
        session = {
            'id': SESSION_ID,
            'user_id': USER_ID,
            'expires_at': datetime.now(timezone.utc) + timedelta(days=30),
            'revoked_at': datetime.now(timezone.utc),
        }
        conn = FakeConnection(results=[session])
        with use_connection(conn):
            r = self.client.post('/auth/refresh', json={'refresh_token': 'revoked-token'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('cerrada', r.json()['detail'])

    def test_refresh_expired_session(self):
        session = {
            'id': SESSION_ID,
            'user_id': USER_ID,
            'expires_at': datetime.now(timezone.utc) - timedelta(days=1),
            'revoked_at': None,
        }
        conn = FakeConnection(results=[session])
        with use_connection(conn):
            r = self.client.post('/auth/refresh', json={'refresh_token': 'expired-token'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('expiró', r.json()['detail'])

    def test_refresh_inactive_user(self):
        session = {
            'id': SESSION_ID,
            'user_id': USER_ID,
            'expires_at': datetime.now(timezone.utc) + timedelta(days=30),
            'revoked_at': None,
        }
        conn = FakeConnection(results=[session, None])  # user not found / inactive
        with use_connection(conn):
            r = self.client.post('/auth/refresh', json={'refresh_token': 'valid-token'})
        self.assertEqual(r.status_code, 403)


class LogoutTests(unittest.TestCase):
    """POST /auth/logout revoca sesiones."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.app.dependency_overrides.clear()

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def _auth_header(self):
        from app.security import create_access_token
        token = create_access_token(str(USER_ID))
        return {'Authorization': f'Bearer {token}'}

    def test_logout_specific_session(self):
        user = make_user()
        revoked = {'id': SESSION_ID}
        # Query 1: SELECT user (current_user), Query 2: UPDATE auth_session
        conn = FakeConnection(results=[user, revoked])
        with use_connection(conn):
            r = self.client.post('/auth/logout', json={'refresh_token': 'my-refresh-token'},
                                headers=self._auth_header())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('Sesión cerrada', r.json()['message'])
        update_queries = [q for q, _ in conn.executed if 'UPDATE auth_session SET revoked_at' in q]
        self.assertEqual(len(update_queries), 1)
        # Verifica que usó el hash del refresh token y el user_id
        _, params = next((q, p) for q, p in conn.executed if 'UPDATE auth_session SET revoked_at' in q)
        self.assertEqual(params[0], USER_ID)

    def test_logout_all_sessions(self):
        user = make_user()
        conn = FakeConnection(results=[user])
        with use_connection(conn):
            r = self.client.post('/auth/logout', headers=self._auth_header())
        self.assertEqual(r.status_code, 200, r.text)
        update_queries = [q for q, _ in conn.executed if 'UPDATE auth_session SET revoked_at' in q]
        self.assertEqual(len(update_queries), 1)
        # Sin refresh_token específico, revoca todas del usuario
        query, params = next((q, p) for q, p in conn.executed if 'UPDATE auth_session SET revoked_at' in q)
        self.assertNotIn('refresh_token_hash', query)
        self.assertEqual(params, (USER_ID,))

    def test_logout_not_found(self):
        user = make_user()
        conn = FakeConnection(results=[user, None])
        with use_connection(conn):
            r = self.client.post('/auth/logout', json={'refresh_token': 'wrong-token'},
                                headers=self._auth_header())
        self.assertEqual(r.status_code, 404)

    def test_logout_requires_auth(self):
        conn = FakeConnection()
        with use_connection(conn):
            r = self.client.post('/auth/logout')
        self.assertEqual(r.status_code, 401)


class AccessTokenExpirationTests(unittest.TestCase):
    """El access_token tiene exp y se rechaza al expirar."""

    def test_access_token_has_exp(self):
        from app.security import create_access_token, decode_access_token
        import jwt as pyjwt
        token = create_access_token(str(USER_ID))
        # Decodificar sin verificar exp para inspeccionar el payload
        payload = pyjwt.decode(token, options={'verify_exp': False, 'verify_signature': False})
        self.assertIn('exp', payload)
        self.assertIn('iat', payload)
        self.assertIn('sub', payload)

    def test_expired_token_is_rejected(self):
        import jwt as pyjwt
        from app.config import JWT_SECRET, JWT_ALGORITHM
        # Crear token ya expirado
        payload = {
            'sub': str(USER_ID),
            'iat': datetime.now(timezone.utc) - timedelta(hours=2),
            'exp': datetime.now(timezone.utc) - timedelta(hours=1),
        }
        expired_token = pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        client = TestClient(main.app)
        conn = FakeConnection()
        with use_connection(conn):
            r = client.get('/auth/me', headers={'Authorization': f'Bearer {expired_token}'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('expirado', r.json()['detail'].lower())


class SecurityHashTests(unittest.TestCase):
    """Argon2id funciona y es retrocompatible con bcrypt (D-C15)."""

    def test_argon2id_hash_and_verify(self):
        h = hash_password('TestPassword123')
        self.assertTrue(h.startswith('$argon2'))
        self.assertTrue(verify_password('TestPassword123', h))
        self.assertFalse(verify_password('WrongPassword', h))

    def test_bcrypt_backward_compatibility(self):
        self.assertTrue(verify_password('ClaveSegura123', BCRYPT_HASH))
        self.assertFalse(verify_password('IncorrectPassword', BCRYPT_HASH))

    def test_needs_rehash_for_bcrypt(self):
        from app.security import needs_rehash
        self.assertTrue(needs_rehash(BCRYPT_HASH))

    def test_no_rehash_for_fresh_argon2id(self):
        from app.security import needs_rehash
        h = hash_password('TestPassword123')
        self.assertFalse(needs_rehash(h))


if __name__ == '__main__':
    unittest.main()

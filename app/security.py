from datetime import datetime, timedelta, timezone
import secrets
from .security_tokens import token_hash

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .config import JWT_ALGORITHM, JWT_SECRET, ACCESS_TOKEN_EXPIRE_MINUTES

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash con Argon2id (D-C15)."""
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verifica contra Argon2id o bcrypt. Devuelve True/False."""
    if password_hash.startswith('$argon2'):
        try:
            return _ph.verify(password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False
    # Fallback: bcrypt (hashes anteriores a D-C15).
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True si el hash es bcrypt o Argon2id con parámetros viejos."""
    if not password_hash.startswith('$argon2'):
        return True
    return _ph.check_needs_rehash(password_hash)


def hash_registration_token(token: str) -> str:
    return token_hash(token)


def generate_registration_token() -> str:
    return secrets.token_urlsafe(32)


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return token_hash(token)


def create_access_token(user_id: str, session_id=None) -> str:
    """Access token. Con session_id incluye `sid` para poder revocar la sesión.

    El argumento es opcional para no romper tokens emitidos/creados antes de
    este cambio (por ejemplo, en pruebas y herramientas de diagnóstico).
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    if session_id is not None:
        payload["sid"] = str(session_id)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


# El desbloqueo del POS es un permiso de corta duración, distinto del access
# token: la contraseña solo se pide al desbloquear, no en cada venta.
POS_UNLOCK_MINUTES = 20


def create_pos_unlock_token(user_id: str, branch_id: str) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=POS_UNLOCK_MINUTES)
    payload = {
        "sub": str(user_id),
        "branch_id": str(branch_id),
        "scope": "pos_unlock",
        "iat": now,
        "exp": expires_at,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), expires_at


def decode_pos_unlock_token(token: str, user_id: str, branch_id: str) -> dict:
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    if payload.get('scope') != 'pos_unlock':
        raise jwt.InvalidTokenError('Token no es de desbloqueo POS')
    if str(payload.get('sub')) != str(user_id) or str(payload.get('branch_id')) != str(branch_id):
        raise jwt.InvalidTokenError('Token de otra sesión o sucursal')
    return payload

from datetime import datetime, timedelta, timezone
import secrets
from .security_tokens import token_hash

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
import jwt

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


def create_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

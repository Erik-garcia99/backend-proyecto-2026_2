from datetime import datetime, timezone
import secrets
from .security_tokens import token_hash

import bcrypt
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .config import JWT_ALGORITHM, JWT_SECRET

_argon2 = PasswordHasher()
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verifica contraseñas hasheadas con bcrypt o argon2id.

    Los usuarios sembrados a mano tienen hashes argon2id, mientras que los
    creados por la API usan bcrypt. Se aceptan ambos formatos para no dejar
    cuentas fuera por el algoritmo con el que se generó su hash.
    """
    if not password_hash:
        return False
    if password_hash.startswith(_BCRYPT_PREFIXES):
        try:
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except (ValueError, TypeError):
            return False
    if password_hash.startswith("$argon2"):
        try:
            return _argon2.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False
    return False


def hash_registration_token(token: str) -> str:
    return token_hash(token)


def generate_registration_token() -> str:
    return secrets.token_urlsafe(32)


def create_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
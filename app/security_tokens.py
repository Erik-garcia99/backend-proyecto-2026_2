"""Hashes para tokens de alta entropía; sin dependencia de configuración/JWT."""
import hashlib


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
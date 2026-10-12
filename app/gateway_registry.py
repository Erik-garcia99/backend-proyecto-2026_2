"""PostgreSQL enrollment and device identity; no per-device environment mapping."""
import base64
import re
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException
from psycopg.errors import UniqueViolation
from .security_tokens import token_hash


def verify_proof(identifier, nonce, public_key, signature):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier):
        raise ValueError('Identificador incorrecto')
    try:
        key = base64.b64decode(public_key, validate=True)
        proof = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(key).verify(
            proof, ('selfiq-gateway-v1:' + identifier + ':' + nonce).encode())
    except (ValueError, TypeError, InvalidSignature):
        raise ValueError('Identidad de dispositivo inválida') from None


def active_token(connection, digest, branch_id=None):
    return connection.execute('''SELECT t.id,t.branch_id,t.organization_id
        FROM organization_token t JOIN branch b ON b.id=t.branch_id AND b.organization_id=t.organization_id
        JOIN organization o ON o.id=b.organization_id
        WHERE t.token_hash=%s AND t.is_active AND t.revoked_at IS NULL
        AND b.is_active AND o.is_active AND 'register_gateway'=ANY(t.scopes)
        AND (t.expires_at IS NULL OR t.expires_at>NOW())
        AND (%s::uuid IS NULL OR t.branch_id=%s::uuid)''',
        (digest, branch_id, branch_id)).fetchone()


def enroll(get_connection, identifier, public_key, raw_token):
    digest = token_hash(raw_token)
    try:
        with get_connection() as connection:
            existing = connection.execute('SELECT * FROM gateway WHERE rpi_identifier=%s FOR UPDATE',
                                          (identifier,)).fetchone()
            if existing and existing['device_public_key'] not in (None, public_key):
                raise HTTPException(403, 'La identidad no coincide con la Raspberry registrada')
            if existing and not existing['is_active']:
                raise HTTPException(403, 'Gateway desactivado; contacte a soporte')
            # A previously enrolled private key may enter recovery even with a revoked token.
            # Never grant this path by knowing only a shared, old branch token.
            token = active_token(connection, digest, existing['branch_id'] if existing else None)
            if not token:
                if not existing or existing['device_public_key'] is None:
                    raise HTTPException(403, 'Token de sucursal inválido; configure un token vigente')
                branch = connection.execute('''SELECT b.id FROM branch b JOIN organization o ON o.id=b.organization_id
                    WHERE b.id=%s AND b.is_active AND o.is_active''', (existing['branch_id'],)).fetchone()
                if not branch:
                    raise HTTPException(403, 'Sucursal o empresa desactivada; contacte a soporte')
                return {**existing, 'token_valid': False, 'token_hash': digest}
            if not existing or existing['device_public_key'] is None:
                # Lock the token row to serialize enrollment / rotation and usage limits.
                limits = connection.execute('SELECT max_uses,use_count FROM organization_token WHERE id=%s FOR UPDATE',
                                            (token['id'],)).fetchone()
                if not existing and connection.execute('SELECT id FROM gateway WHERE branch_id=%s', (token['branch_id'],)).fetchone():
                    raise HTTPException(409, 'La sucursal ya tiene una Raspberry; solicite reemplazo a soporte')
                # Recheck after acquiring the row lock, including revocation.
                if not active_token(connection, digest, token['branch_id']):
                    raise HTTPException(403, 'Token de sucursal revocado durante el registro')
                if limits['max_uses'] is not None and limits['use_count'] >= limits['max_uses']:
                    raise HTTPException(403, 'Token de sucursal sin usos disponibles')
                connection.execute('UPDATE organization_token SET use_count=use_count+1,last_used_at=NOW() WHERE id=%s',
                                   (token['id'],))
            if existing:
                row = connection.execute('''UPDATE gateway SET device_public_key=%s,registered_with_token=%s,
                    registered_at=COALESCE(registered_at,NOW()),updated_at=NOW() WHERE id=%s RETURNING *''',
                    (public_key,token['id'],existing['id'])).fetchone()
            else:
                row = connection.execute('''INSERT INTO gateway(branch_id,rpi_identifier,device_public_key,
                    registered_with_token,registered_at) VALUES(%s,%s,%s,%s,NOW()) RETURNING *''',
                    (token['branch_id'],identifier,public_key,token['id'])).fetchone()
            connection.commit()
            return {**row, 'token_valid': True, 'token_hash': digest}
    except UniqueViolation:
        raise HTTPException(409, 'La sucursal ya tiene una Raspberry; solicite reemplazo a soporte') from None

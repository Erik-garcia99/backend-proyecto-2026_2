"""Registro idempotente de ESP32 por gateway ya enrolado (Ed25519 + WSS)."""
import base64
import re
import time
from datetime import datetime, timezone
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .gateway_registry import active_token
from .security_tokens import token_hash

MAC_RE = re.compile(r'^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$')
NODE_TYPES = {'guide', 'access_counter', 'shelf_cam', 'environment'}


class RegisterNode(BaseModel):
    mac_address: str = Field(min_length=17, max_length=17)
    node_type: str = Field(min_length=1, max_length=32)
    timestamp: int
    nonce: str = Field(min_length=16, max_length=64)
    signature: str = Field(min_length=80, max_length=128)


def signed_message(gateway_id, body):
    return ('selfiq-node-register-v1\n' + gateway_id + '\n' + str(body.timestamp)
            + '\n' + body.nonce + '\n' + body.mac_address + '\n' + body.node_type).encode()


def make_router(get_connection, hub, current_user):
    router = APIRouter(tags=['ESP32 nodes'])

    @router.post('/gateways/{gateway_id}/nodes/register')
    def register_node(gateway_id: str, body: RegisterNode):
        # RPi signs each REST request with the private key already used for WSS.
        if not MAC_RE.fullmatch(body.mac_address) or body.node_type not in NODE_TYPES:
            raise HTTPException(422, 'MAC o tipo de nodo incorrecto')
        if abs(time.time() - body.timestamp) > 120:
            raise HTTPException(401, 'Firma expirada; compruebe NTP en la Raspberry')
        try:
            signature = base64.b64decode(body.signature, validate=True)
        except (ValueError, TypeError):
            raise HTTPException(401, 'Firma incorrecta') from None

        with get_connection() as conn:
            gateway = conn.execute('''SELECT g.id,g.branch_id,g.device_public_key,g.is_active,
                       g.registered_with_token,t.token_hash
                FROM gateway g LEFT JOIN organization_token t ON t.id=g.registered_with_token
                JOIN branch b ON b.id=g.branch_id JOIN organization o ON o.id=b.organization_id
                WHERE g.rpi_identifier=%s AND g.is_active AND b.is_active AND o.is_active''',
                (gateway_id,)).fetchone()
            session = hub.sessions.get(gateway_id)
            if (not gateway or not gateway['device_public_key'] or not session
                    or not session['authorized'] or str(session['branch_id']) != str(gateway['branch_id'])):
                raise HTTPException(403, 'Gateway desconectado o sin autorización')
            if not gateway['token_hash'] or not active_token(conn, gateway['token_hash'], gateway['branch_id']):
                raise HTTPException(403, 'Token de sucursal revocado')
            try:
                public_key = base64.b64decode(gateway['device_public_key'], validate=True)
                Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signed_message(gateway_id, body))
            except (ValueError, InvalidSignature):
                raise HTTPException(401, 'Firma no corresponde al gateway registrado') from None

            # La unicidad de mac_address serializa altas concurrentes incluso con varios workers.
            with conn.cursor() as cur:
                cur.execute('''INSERT INTO esp32_node
                     (gateway_id,node_type,mac_address,registration_status,registered_at)
                     VALUES (%s,%s,%s,'registered',NOW())
                     ON CONFLICT (mac_address) DO NOTHING RETURNING id,gateway_id,node_type''',
                    (gateway['id'], body.node_type, body.mac_address))
                inserted = cur.fetchone()
                row = inserted or cur.execute('''SELECT id,gateway_id,node_type,registration_status
                     FROM esp32_node WHERE mac_address=%s FOR UPDATE''',
                     (body.mac_address,)).fetchone()
                if not row or row['gateway_id'] != gateway['id'] or row['node_type'] != body.node_type:
                    raise HTTPException(409, 'MAC ya registrada para otro gateway o tipo de módulo')
                if not inserted:
                    cur.execute('''UPDATE esp32_node SET registration_status='registered',
                         registered_at=COALESCE(registered_at,NOW()),updated_at=NOW()
                         WHERE id=%s''', (row['id'],))
            conn.commit()
        return {'mac_address': body.mac_address, 'esp_id': str(row['id']),
                'branch_id': str(gateway['branch_id']), 'node_type': body.node_type,
                'registration_status': 'registered', 'already_registered': not bool(inserted)}

    @router.get('/branches/{branch_id}/nodes')
    def list_nodes(branch_id: UUID, user=Depends(current_user)):
        from .registration_tokens import require_branch_access
        with get_connection() as conn:
            branch = conn.execute('SELECT organization_id FROM branch WHERE id=%s AND is_active',
                                  (branch_id,)).fetchone()
            if not branch:
                raise HTTPException(404, 'Sucursal no encontrada')
            require_branch_access(user, branch_id, branch['organization_id'])
            rows = conn.execute('''SELECT n.id,n.mac_address,n.node_type,n.node_code,n.name,
                       n.registration_status,n.registered_at,n.gateway_id,n.is_online
                FROM esp32_node n JOIN gateway g ON g.id=n.gateway_id
                WHERE g.branch_id=%s ORDER BY n.created_at''', (branch_id,)).fetchall()
        return [{**dict(r), 'id': str(r['id']), 'gateway_id': str(r['gateway_id'])} for r in rows]

    return router

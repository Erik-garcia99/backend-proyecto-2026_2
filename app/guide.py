"""Guía LED: resolución de destino, reserva por canal y contrato C/E.

Rutas bajo ``/branches/{branch_id}/guide-requests`` (ARQUITECTURA §6.6).
El backend resuelve producto → ROI → estante → punto LED → nodo → gateway;
el cliente nunca envía canal, GPIO ni tópico.
"""
import base64
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .gateway_registry import active_token
from .permissions import is_admin, require_branch_actor


def _connection():
    return db.get_connection()

TTL_MS = 60000
ACCEPT_WINDOW_MS = 3000
MAX_WAIT_SECONDS = 65
CLOSE_WAIT_SECONDS = 10
IDEMPOTENCY_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
BOOT_RE = re.compile(r'^[0-9a-f]{8}$')
MODULE = 'shelf_monitor'


class GuideCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    product_id: UUID


class GuideAssociationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    product_id: UUID
    guide_point_id: UUID
    replace_product_id: UUID | None = None


class GuideCapabilities(BaseModel):
    model_config = ConfigDict(extra='forbid')
    timestamp: int
    nonce: str = Field(min_length=16, max_length=64)
    signature: str = Field(min_length=80, max_length=128)
    boot: str = Field(min_length=8, max_length=8, pattern=r'^[0-9a-f]{8}$')
    driver: str = Field(pattern=r'^(direct|hc595)$')
    channels: int = Field(ge=1, le=8)
    channels_map: list[int]
    fw: str | None = Field(default=None, max_length=32)


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z') if isinstance(value, datetime) else None


def _now():
    return datetime.now(timezone.utc)


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'


def _state(row):
    """Cuerpo reducido y coherente con el validador del contrato v1."""
    status = row['status']
    expires = row.get('expires_at')
    remaining = 0
    if status == 'active' and isinstance(expires, datetime):
        remaining = max(0, int((expires - _now()).total_seconds()))
    return {
        'id': str(row['id']),
        'status': status,
        'ttlSeconds': int(row.get('ttl_ms') or TTL_MS) // 1000,
        'expiresAt': _iso(expires),
        'remainingSeconds': remaining,
        'location': {'shelfName': row.get('shelf_name'),
                     'reference': row.get('location_reference')},
        'light': row.get('light') or 'unknown',
        'closing': bool(row.get('closing')),
        'intent': row.get('intent'),
        'reason': row.get('reason'),
    }


def _body_hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _load_request(connection, branch_id, request_id):
    row = connection.execute(
        '''SELECT * FROM led_guide_request WHERE id=%s AND branch_id=%s''',
        (request_id, branch_id)).fetchone()
    if not row:
        raise HTTPException(404, 'Solicitud de guía no encontrada')
    return row


def _owns(row, user):
    if row.get('customer_id') and str(row['customer_id']) == str(user['id']):
        return True
    if row.get('requester_user_id') and str(row['requester_user_id']) == str(user['id']):
        return True
    return is_admin(user)


def _command_payload(row):
    return {'node_id': str(row['node_id']), 'branch_id': str(row['branch_id']),
            'node_type': 'guide', 'module': MODULE,
            'channel': int(row['led_index']), 'session_id': str(row['session_id']),
            'ttl_ms': int(row.get('ttl_ms') or TTL_MS),
            'accept_window_ms': ACCEPT_WINDOW_MS}


def _resolve(connection, branch_id, product_id, organization_id):
    rows = connection.execute(
        '''SELECT gp.id AS guide_point_id, gp.led_index, gp.node_id,
                  g.rpi_identifier, s.name AS shelf_name, s.location_label,
                  n.guide_channels
             FROM guide_product_light lpl
             JOIN product p ON p.id=lpl.product_id AND p.is_active
             JOIN led_guide_point gp ON gp.id=lpl.guide_point_id
             LEFT JOIN shelf s ON s.id=gp.shelf_id
             JOIN esp32_node n ON n.id=gp.node_id AND n.node_type='guide'
                  AND n.registration_status='registered'
             JOIN gateway g ON g.id=n.gateway_id AND g.is_active AND g.branch_id=%s
            WHERE lpl.branch_id=%s AND lpl.product_id=%s AND p.organization_id=%s
            FOR UPDATE OF gp, lpl''',
        (branch_id, branch_id, product_id, organization_id)).fetchall()
    if not rows:
        raise HTTPException(404, 'guide_association_missing')
    if len(rows) != 1:
        raise HTTPException(409, 'guide_destination_ambiguous')
    row = rows[0]
    if row.get('guide_channels') is None:
        raise HTTPException(503, 'guide_capabilities_missing')
    if int(row['led_index']) >= int(row['guide_channels']):
        raise HTTPException(422, 'invalid_channel')
    return row


def create_router(current_user, get_connection, hub):
    router = APIRouter(tags=['Guide'])

    @router.post('/branches/{branch_id}/guide-requests', status_code=202)
    async def create_guide(branch_id: UUID, body: GuideCreate, response: Response,
                           idempotency_key: str | None = Header(default=None,
                                                                alias='Idempotency-Key'),
                           user=Depends(current_user)):
        if not idempotency_key or not IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise HTTPException(422, 'Idempotency-Key inválida')
        request = body.model_dump(mode='json')
        digest = _body_hash(request)
        with _connection() as connection:
            branch = require_branch_actor(connection, branch_id, user,
                                          customer=True, staff=True, admin=True)
            previous = connection.execute(
                '''SELECT body_hash, response FROM idempotency_key
                    WHERE branch_id=%s AND key=%s''',
                (branch_id, idempotency_key)).fetchone()
            if previous:
                if previous['body_hash'] != digest:
                    raise HTTPException(409, 'idempotency_conflict')
                if not previous.get('response'):
                    raise HTTPException(409, 'guide_request_in_progress')
                _no_store(response)
                return json.loads(previous['response'])

            dest = _resolve(connection, branch_id, body.product_id,
                            branch['organization_id'])
            now = _now()
            request_id = uuid4()
            session_id = uuid4()
            command_id = 'gd-' + uuid4().hex[:16]
            connection.execute(
                '''INSERT INTO idempotency_key
                       (branch_id, key, user_id, body_hash, status, created_at)
                   VALUES (%s,%s,%s,%s,'pending',NOW())''',
                (branch_id, idempotency_key, user['id'], digest))
            try:
                row = connection.execute(
                    '''INSERT INTO led_guide_request
                           (id, branch_id, customer_id, requester_user_id, product_id,
                            guide_point_id, status, delivery, light, node_id, led_index,
                            gateway_rpi, session_id, command_id, ttl_ms, expires_at,
                            reservation_until, issued_at, requested_at, updated_at,
                            shelf_name, location_reference)
                       VALUES (%s,%s,%s,%s,%s,%s,'pending','unknown','unknown',%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                    (request_id, branch_id,
                     user['id'] if str(user.get('role', '')).lower() == 'customer' else None,
                     None if str(user.get('role', '')).lower() == 'customer' else user['id'],
                     body.product_id, dest['guide_point_id'], dest['node_id'],
                     int(dest['led_index']), dest['rpi_identifier'], session_id,
                     command_id, TTL_MS, now + timedelta(seconds=60),
                     now + timedelta(seconds=MAX_WAIT_SECONDS), now, now, now,
                     dest['shelf_name'], dest['location_label'])).fetchone()
            except Exception as exc:
                connection.rollback()
                if getattr(exc, 'pgcode', None) == '23505':
                    raise HTTPException(409, 'guide_channel_busy') from None
                raise
            connection.commit()

        outcome = None
        try:
            outcome = await hub.send(dest['rpi_identifier'], 'guide.start',
                                     payload=_command_payload(row))
        except Exception:
            outcome = None
        status, light, reason = 'pending', 'unknown', None
        data = outcome.get('data') if isinstance(outcome, dict) else None
        delivery = data.get('delivery') if isinstance(data, dict) else None
        if delivery == 'applied' and data.get('light') == 'on':
            status, light = 'active', 'on'
        elif delivery in ('rejected', 'error'):
            status = 'failed'
            reason = 'node_rejected' if delivery == 'rejected' else 'node_error'
        elif delivery is None:
            reason = 'gateway_timeout'
        with _connection() as connection:
            connection.execute(
                '''UPDATE led_guide_request SET status=%s, light=%s, delivery=%s,
                        reason=%s, activated_at=CASE WHEN %s='active' THEN NOW() ELSE NULL END,
                        updated_at=NOW() WHERE id=%s''',
                (status, light, delivery or 'unknown', reason, status, request_id))
            body_state = _state({**dict(row), 'status': status, 'light': light,
                                 'reason': reason, 'updated_at': _now()})
            connection.execute(
                '''UPDATE idempotency_key SET status='applied', response=%s::jsonb
                    WHERE branch_id=%s AND key=%s''',
                (json.dumps(body_state), branch_id, idempotency_key))
            connection.commit()
        _no_store(response)
        return body_state

    @router.get('/branches/{branch_id}/guide-requests/{request_id}')
    def read_guide(branch_id: UUID, request_id: UUID, response: Response,
                   user=Depends(current_user)):
        with _connection() as connection:
            require_branch_actor(connection, branch_id, user,
                                 customer=True, staff=True, admin=True)
            row = _load_request(connection, branch_id, request_id)
            if not _owns(row, user):
                raise HTTPException(404, 'Solicitud de guía no encontrada')
            status = row['status']
            reason = row.get('reason')
            updated = row.get('updated_at')
            if (row.get('closing') and status in ('pending', 'active')
                    and isinstance(updated, datetime)
                    and updated + timedelta(seconds=CLOSE_WAIT_SECONDS) < _now()):
                status, reason = 'failed', 'off_unverified'
                connection.execute(
                    '''UPDATE led_guide_request SET status='failed', reason='off_unverified',
                            light='unknown', closing=FALSE, updated_at=NOW() WHERE id=%s''',
                    (request_id,))
                connection.commit()
                row = {**dict(row), 'status': status, 'reason': reason,
                       'light': 'unknown', 'closing': False}
            if status == 'pending' and row.get('expires_at') and row['expires_at'] < _now():
                status, reason = 'expired', 'ttl'
                connection.execute(
                    '''UPDATE led_guide_request SET status='expired', reason='ttl',
                            light='unknown', updated_at=NOW() WHERE id=%s''', (request_id,))
                connection.commit()
                row = {**dict(row), 'status': status, 'reason': reason, 'light': 'unknown'}
        _no_store(response)
        return _state(row)

    async def _close(branch_id, request_id, response, user, intent):
        with _connection() as connection:
            require_branch_actor(connection, branch_id, user,
                                 customer=True, staff=True, admin=True)
            row = _load_request(connection, branch_id, request_id)
            if not _owns(row, user):
                raise HTTPException(404, 'Solicitud de guía no encontrada')
            if row.get('closing'):
                _no_store(response)
                return _state(row)
            if row['status'] not in ('pending', 'active'):
                _no_store(response)
                return _state(row)
            connection.execute(
                '''UPDATE led_guide_request SET closing=TRUE, intent=%s,
                        updated_at=NOW() WHERE id=%s''', (intent, request_id))
            connection.commit()
            row = {**dict(row), 'closing': True, 'intent': intent}

        outcome = None
        try:
            outcome = await hub.send(row['gateway_rpi'], 'guide.stop',
                                     payload=_command_payload(row))
        except Exception:
            outcome = None
        data = outcome.get('data') if isinstance(outcome, dict) else None
        delivery = data.get('light') if isinstance(data, dict) else None
        closed = data is not None and data.get('delivery') == 'applied' and delivery == 'off'
        status, light, reason = row['status'], 'unknown', None
        if closed:
            status = 'confirmed' if intent == 'confirm' else 'cancelled'
            light = 'off'
        elif data is not None and data.get('delivery') in ('rejected', 'error'):
            status, reason = 'failed', 'off_unverified'
        with _connection() as connection:
            connection.execute(
                '''UPDATE led_guide_request SET status=%s, light=%s, reason=%s,
                        closing=%s, ended_at=CASE WHEN %s THEN NOW() ELSE NULL END,
                        updated_at=NOW() WHERE id=%s''',
                (status, light, reason, not closed, closed, request_id))
            final = {**row, 'status': status, 'light': light, 'reason': reason,
                     'closing': not closed}
        _no_store(response)
        return _state(final)

    @router.post('/branches/{branch_id}/guide-requests/{request_id}/confirm', status_code=202)
    async def confirm_guide(branch_id: UUID, request_id: UUID, response: Response,
                            user=Depends(current_user)):
        return await _close(branch_id, request_id, response, user, 'confirm')

    @router.delete('/branches/{branch_id}/guide-requests/{request_id}', status_code=202)
    async def cancel_guide(branch_id: UUID, request_id: UUID, response: Response,
                           user=Depends(current_user)):
        return await _close(branch_id, request_id, response, user, 'cancel')

    # --- Asociaciones propias de la guía (independientes de la cámara/ROI) ---
    @router.get('/branches/{branch_id}/guide-associations')
    def list_associations(branch_id: UUID, response: Response, user=Depends(current_user)):
        with _connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True, staff=True)
            rows = connection.execute(
                '''SELECT gp.id AS guide_point_id, gp.name AS light_name, gp.led_index, gp.node_id,
                          s.id AS shelf_id, s.name AS shelf_name,
                          p.id AS product_id, p.name AS product_name, p.sku
                     FROM guide_product_light lpl
                     JOIN led_guide_point gp ON gp.id=lpl.guide_point_id
                     JOIN esp32_node n ON n.id=gp.node_id AND n.node_type='guide'
                          AND n.registration_status='registered'
                     JOIN gateway g ON g.id=n.gateway_id AND g.is_active AND g.branch_id=lpl.branch_id
                     LEFT JOIN shelf s ON s.id=gp.shelf_id
                     JOIN product p ON p.id=lpl.product_id AND p.is_active
                    WHERE lpl.branch_id=%s AND p.organization_id=%s
                    ORDER BY s.name, gp.led_index, p.name''',
                (branch_id, branch['organization_id'])).fetchall()
        _no_store(response)
        return [{'guidePointId': str(row['guide_point_id']), 'lightName': row['light_name'],
                 'nodeId': str(row['node_id']),
                 'channel': int(row['led_index']), 'shelfId': str(row['shelf_id']) if row['shelf_id'] else None,
                 'shelfName': row['shelf_name'], 'productId': str(row['product_id']),
                 'productName': row['product_name'], 'sku': row['sku']} for row in rows]

    @router.post('/branches/{branch_id}/guide-associations', status_code=201)
    def assign_product(branch_id: UUID, body: GuideAssociationCreate, response: Response,
                       user=Depends(current_user)):
        with _connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            point = connection.execute(
                '''SELECT gp.id, s.name AS shelf_name
                     FROM led_guide_point gp
                     JOIN esp32_node n ON n.id=gp.node_id AND n.node_type='guide'
                     JOIN gateway g ON g.id=n.gateway_id AND g.is_active AND g.branch_id=%s
                     LEFT JOIN shelf s ON s.id=gp.shelf_id
                    WHERE gp.id=%s FOR UPDATE OF gp''', (branch_id, body.guide_point_id)).fetchone()
            if not point:
                raise HTTPException(404, 'Luz de guía no encontrada en esta sucursal')
            product = connection.execute(
                '''SELECT id, name, sku FROM product
                    WHERE id=%s AND is_active AND organization_id=%s''',
                (body.product_id, branch['organization_id'])).fetchone()
            if not product:
                raise HTTPException(404, 'Producto no encontrado en esta sucursal')
            current = connection.execute(
                '''SELECT guide_point_id FROM guide_product_light
                    WHERE branch_id=%s AND product_id=%s''',
                (branch_id, body.product_id)).fetchone()
            body_out = {'guidePointId': str(body.guide_point_id), 'productId': str(body.product_id),
                        'productName': product['name'], 'lightName': point['shelf_name']}
            if current:
                if current['guide_point_id'] == body.guide_point_id:
                    _no_store(response)
                    return body_out
                raise HTTPException(409, 'El producto ya está asociado a otra luz de la guía')
            occupants = connection.execute(
                '''SELECT product_id FROM guide_product_light
                    WHERE branch_id=%s AND guide_point_id=%s''',
                (branch_id, body.guide_point_id)).fetchall()
            expected = [body.replace_product_id] if body.replace_product_id else []
            if [row['product_id'] for row in occupants] != expected:
                raise HTTPException(409, 'La asociación de esta luz cambió o está ocupada; actualiza la lista')
            active = connection.execute(
                '''SELECT 1 FROM led_guide_request
                    WHERE branch_id=%s AND guide_point_id=%s
                      AND (status IN ('pending','active') OR closing) LIMIT 1''',
                (branch_id, body.guide_point_id)).fetchone()
            if active:
                raise HTTPException(409, 'La luz tiene una guía activa; ciérrala antes de cambiar el producto')
            try:
                if body.replace_product_id:
                    connection.execute(
                        '''DELETE FROM guide_product_light
                            WHERE branch_id=%s AND guide_point_id=%s AND product_id=%s''',
                        (branch_id, body.guide_point_id, body.replace_product_id))
                connection.execute(
                    '''INSERT INTO guide_product_light
                           (branch_id, product_id, guide_point_id, created_by)
                       VALUES (%s,%s,%s,%s)''',
                    (branch_id, body.product_id, body.guide_point_id, user['id']))
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'El producto ya está asociado a otra luz de la guía') from None
        _no_store(response)
        return body_out

    @router.delete('/branches/{branch_id}/guide-associations/{product_id}', status_code=204)
    def unassign_product(branch_id: UUID, product_id: UUID, user=Depends(current_user)):
        with _connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            connection.execute(
                '''SELECT gp.id FROM led_guide_point gp
                    JOIN guide_product_light lpl ON lpl.guide_point_id=gp.id
                    WHERE lpl.branch_id=%s AND lpl.product_id=%s FOR UPDATE OF gp''',
                (branch_id, product_id)).fetchone()
            active = connection.execute(
                '''SELECT 1 FROM led_guide_request
                    WHERE branch_id=%s AND product_id=%s
                      AND (status IN ('pending','active') OR closing) LIMIT 1''',
                (branch_id, product_id)).fetchone()
            if active:
                raise HTTPException(409, 'El producto tiene una guía activa; ciérrala antes de desasociarlo')
            cursor = connection.execute(
                '''DELETE FROM guide_product_light
                    WHERE branch_id=%s AND product_id=%s''', (branch_id, product_id))
            if not getattr(cursor, 'rowcount', 1):
                raise HTTPException(404, 'El producto no estaba asociado a ninguna luz')
            connection.commit()
        return Response(status_code=204)

    return router


def create_gateway_router(get_connection):
    """Capacidades declaradas por el nodo, firmadas por el gateway (GL-3)."""
    router = APIRouter(tags=['Guide capabilities'])

    @router.post('/gateways/{gateway_id}/nodes/{node_id}/guide-capabilities')
    def guide_capabilities(gateway_id: str, node_id: UUID, body: GuideCapabilities):
        if abs(time.time() - body.timestamp) > 120:
            raise HTTPException(401, 'Firma expirada')
        try:
            signature = base64.b64decode(body.signature, validate=True)
        except (ValueError, TypeError):
            raise HTTPException(401, 'Firma incorrecta') from None
        map_csv = ','.join(str(value) for value in body.channels_map)
        message = (f'selfiq-guide-capabilities-v1\n{gateway_id}\n{body.timestamp}\n'
                   f'{body.nonce}\n{node_id}\n{body.boot}\n{body.driver}\n'
                   f'{body.channels}\n{map_csv}').encode()
        if (len(body.channels_map) != body.channels
                or len(set(body.channels_map)) != body.channels
                or any(not 0 <= value <= 255 for value in body.channels_map)):
            raise HTTPException(422, 'channels_map inválido')
        with _connection() as connection:
            gateway = connection.execute(
                '''SELECT g.id, g.device_public_key, g.registered_with_token, g.branch_id,
                          t.token_hash
                     FROM gateway g
                     JOIN branch b ON b.id=g.branch_id
                     JOIN organization o ON o.id=b.organization_id
                     LEFT JOIN organization_token t ON t.id=g.registered_with_token
                    WHERE g.rpi_identifier=%s AND g.is_active AND b.is_active AND o.is_active''',
                (gateway_id,)).fetchone()
            if not gateway or not gateway['device_public_key']:
                raise HTTPException(403, 'Gateway no registrado o desactivado')
            # registered_with_token es la FK UUID; active_token recibe el hash textual.
            if (not gateway['token_hash']
                    or not active_token(connection, gateway['token_hash'],
                                        gateway['branch_id'])):
                raise HTTPException(403, 'Token de sucursal revocado')
            try:
                public_key = base64.b64decode(gateway['device_public_key'], validate=True)
                Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
            except (ValueError, InvalidSignature):
                raise HTTPException(401, 'Firma no corresponde al gateway registrado') from None
            node = connection.execute(
                '''SELECT id FROM esp32_node WHERE id=%s AND gateway_id=%s''',
                (node_id, gateway['id'])).fetchone()
            if not node:
                raise HTTPException(404, 'Nodo no registrado en este gateway')
            connection.execute(
                '''UPDATE esp32_node SET guide_driver=%s, guide_channels=%s,
                        guide_channels_map=%s::jsonb, guide_boot=%s,
                        guide_capabilities_at=NOW(), updated_at=NOW() WHERE id=%s''',
                (body.driver, body.channels, json.dumps(body.channels_map), body.boot, node_id))
            connection.commit()
        return {'ok': True, 'node_id': str(node_id), 'channels': body.channels}

    return router

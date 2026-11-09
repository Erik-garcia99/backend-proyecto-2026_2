"""Administración de dispositivos: gateways, nodos ESP32, estantes y guía de luces.

Contrato consumido por el frontend (src/services/deviceService.js):

* Los identificadores de gateway son el ``rpi_identifier`` de la Raspberry.
* Los identificadores de dispositivo son ``esp32_node.id``.
* La sucursal y los permisos se validan en cada operación; nunca se confía en
  un ID enviado por el cliente.
* Las confirmaciones son idempotentes: repetir el alta o la edición devuelve
  el mismo registro.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
import base64
import hashlib
import time
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from .registration_tokens import require_branch_access

LIGHT_LIMIT = 10
DISCOVERY_TIMEOUT_SECONDS = 120
DISCOVERY_SETTLE_SECONDS = 3

NODE_TYPE_TO_API = {
    'guide': 'guide',
    'access_counter': 'occupancy',
    'shelf_cam': 'camera',
    'environment': 'environment',
}
NODE_MODULE = {'guide': 'shelf_monitor'}

# Sesiones de descubrimiento en memoria. El canal WebSocket de los gateways ya
# vive en memoria (GatewayHub), por lo que el backend se ejecuta con un worker.
_discoveries: dict[str, dict] = {}
_expiry_tasks: set[asyncio.Task] = set()
_starting_gateways: set[str] = set()


def pairing_allowed(gateway_id, branch_id):
    return any(session['gateway_id'] == gateway_id and session['branch_id'] == branch_id
               and not session.get('stopped') and time.time() < session['expires_at']
               for session in _discoveries.values())


async def close_discoveries():
    tasks = tuple(_expiry_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def expire_discovery(session, hub):
    await asyncio.sleep(max(0, session['expires_at'] - time.time()))
    if session.get('stopped'):
        return
    try:
        result = await hub.send(session['gateway_id'], 'tcp.disable')
        if not result or result.get('ok') is not True:
            logging.getLogger(__name__).warning('La RPI no confirmó el cierre de descubrimiento')
    except HTTPException:
        logging.getLogger(__name__).warning('RPI inaccesible al caducar descubrimiento; aplica su temporizador local')


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=160)


class DeviceRegister(DeviceUpdate):
    discovery_id: str | None = Field(default=None, max_length=64)
    gateway_id: str | None = Field(default=None, max_length=64)


class LightPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    shelf_id: UUID
    channel: int = Field(ge=0, lt=LIGHT_LIMIT)


class LightTest(BaseModel):
    channel: int = Field(ge=0, lt=LIGHT_LIMIT)
    duration_seconds: int = Field(default=5, ge=1, le=30)


class ProductCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)


class ShelfLink(BaseModel):
    shelf_id: UUID


class ShelfCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    location_label: str | None = Field(default=None, max_length=160)


class ShelfUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    location_label: str | None = Field(default=None, max_length=160)


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def require_branch(connection, branch_id: UUID, user):
    branch = connection.execute(
        '''SELECT b.id, b.organization_id FROM branch b
           JOIN organization o ON o.id=b.organization_id
           WHERE b.id=%s AND b.is_active AND o.is_active''',
        (branch_id,)).fetchone()
    if not branch:
        raise HTTPException(404, 'Sucursal no encontrada o inactiva')
    require_branch_access(user, branch_id, branch['organization_id'])
    return branch


def load_node(connection, branch_id: UUID, device_id: UUID, *, node_type=None, lock=False, candidate=False):
    query = '''SELECT n.*, g.branch_id AS gateway_branch_id, g.rpi_identifier,
                      g.display_name AS gateway_name
               FROM esp32_node n
               JOIN gateway g ON g.id=n.gateway_id
               JOIN branch b ON b.id=g.branch_id
               JOIN organization o ON o.id=b.organization_id
               WHERE n.id=%s AND g.branch_id=%s AND g.is_active
                 AND b.is_active AND o.is_active'''
    params = [device_id, branch_id]
    if not candidate:
        query += " AND n.node_code IS NOT NULL AND n.registration_status='registered'"
    if node_type:
        query += ' AND n.node_type=%s'
        params.append(node_type)
    if lock:
        query += ' FOR UPDATE OF n'
    node = connection.execute(query, tuple(params)).fetchone()
    if not node:
        raise HTTPException(404, 'Dispositivo no encontrado en esta sucursal')
    return node


def device_online(node):
    # La presencia real llega por el heartbeats MQTT reenviado por el gateway.
    last_seen = node.get('last_heartbeat_at')
    if not last_seen:
        return None
    if (datetime.now(timezone.utc) - last_seen).total_seconds() > 120:
        return False
    return bool(node.get('is_online'))


def serialize_device(node):
    node_type = node['node_type']
    api_type = NODE_TYPE_TO_API.get(node_type, node_type)
    display_code = node.get('node_code') or (node.get('mac_address') or '')[-5:]
    default_name = {
        'guide': 'Guía de luces',
        'access_counter': 'Control de aforo',
        'shelf_cam': 'Cámara',
        'environment': 'Sensor ambiental',
    }.get(node_type, 'Dispositivo')
    return {
        'id': str(node['id']),
        'type': api_type,
        # El alta asigna "Guía de luces B9:88". Este respaldo cubre registros
        # antiguos sin nombre; el usuario puede editar `name` en cualquier caso.
        'name': node.get('name') or f'{default_name} {display_code}',
        'location': node.get('location'),
        'online': device_online(node),
        'lastSeenAt': iso(node.get('last_heartbeat_at') or node.get('registered_at')),
        'nodeType': node_type,
        'nodeCode': node.get('node_code'),
        'displayCode': display_code,
        'macAddress': node.get('mac_address'),
        'gatewayId': node.get('rpi_identifier') or str(node['gateway_id']),
        'gatewayName': node.get('gateway_name'),
        'firmwareVersion': node.get('firmware_version'),
        'ipAddress': str(node['ip_address']) if node.get('ip_address') else None,
        'wifiRssi': (float(node['wifi_rssi'])
                     if node.get('wifi_rssi') is not None else None),
        'batteryLevelPct': (float(node['battery_level_pct'])
                            if node.get('battery_level_pct') is not None else None),
        'powerSource': node.get('power_source'),
        'registeredAt': iso(node.get('registered_at')),
    }


def serialize_light(row):
    return {
        'id': str(row['id']),
        'name': row.get('name') or f"Luz {row['led_index'] + 1}",
        'shelfId': str(row['shelf_id']),
        'shelfName': row.get('shelf_name'),
        'channel': row['led_index'],
    }


def serialize_product(row):
    return {
        'id': str(row['id']),
        'sku': row['sku'],
        'name': row['name'],
        'active': row['is_active'],
    }


def serialize_shelf(row):
    camera_node_id = row.get('camera_node_id')
    return {
        'id': str(row['id']),
        'name': row['name'],
        'location': row.get('location_label'),
        'branchId': str(row['branch_id']),
        'cameraNodeId': str(camera_node_id) if camera_node_id else None,
        'roiVersion': row.get('roi_version', 0),
        'roiAppliedVersion': row.get('roi_applied_version', 0),
        'roiAppliedAt': iso(row.get('roi_applied_at')),
    }


def _prune_discoveries():
    now = time.time()
    for key, session in tuple(_discoveries.items()):
        if now > session['expires_at'] + 300:
            _discoveries.pop(key, None)


def _new_discovery_id():
    return uuid4().hex


def _discovery_candidates(connection, gateway_id, branch_id, started_at):
    # Candidato = nodo que todavía no fue confirmado por el usuario. El nombre
    # por defecto no lo excluye; node_code se asigna justo al confirmar el alta.
    rows = connection.execute(
        '''SELECT n.*, g.rpi_identifier FROM esp32_node n
           JOIN gateway g ON g.id=n.gateway_id
           WHERE g.rpi_identifier=%s AND g.branch_id=%s
             AND n.node_code IS NULL
              AND n.last_heartbeat_at >= %s
            ORDER BY n.created_at''',
        (gateway_id, branch_id, datetime.fromtimestamp(started_at, timezone.utc))).fetchall()
    return [serialize_device(row) for row in rows]


def _discovery_state(session, devices, now):
    fingerprint = tuple(device['id'] for device in devices)
    if fingerprint != session.get('fingerprint'):
        session['fingerprint'] = fingerprint
        session['settled_at'] = now
    settled = bool(devices) and now - session.get('settled_at', now) >= DISCOVERY_SETTLE_SECONDS
    if settled:
        return 'completed'
    if now >= session['expires_at']:
        return 'failed' if session.get('gateway_lost') and not devices else 'completed'
    return 'searching'


def _discard_stale_candidates(get_connection, gateway_id, branch_id):
    """Retira candidatos que nunca se confirmaron y llevan días sin aparecer.

    No toca nodos confirmados (`node_code`) ni candidatos vistos recientemente.
    Se ejecuta al iniciar una búsqueda para que los registros huérfanos de
    búsquedas anteriores no se acumulen en «Mis nodos».
    """
    with get_connection() as connection:
        connection.execute(
            '''DELETE FROM esp32_node n USING gateway g
               WHERE n.gateway_id=g.id AND g.rpi_identifier=%s AND g.branch_id=%s
                 AND n.node_code IS NULL
                 AND (n.last_heartbeat_at IS NULL
                      OR n.last_heartbeat_at < NOW() - INTERVAL '7 days')''',
            (gateway_id, branch_id))


def create_router(current_user, get_connection, hub):
    router = APIRouter(tags=['Devices'])

    def gateway_for(connection, branch_id: UUID, user):
        require_branch(connection, branch_id, user)
        gateway = connection.execute(
            '''SELECT g.* FROM gateway g
               JOIN branch b ON b.id=g.branch_id
               JOIN organization o ON o.id=b.organization_id
               WHERE g.branch_id=%s AND g.is_active AND b.is_active AND o.is_active''',
            (branch_id,)).fetchone()
        if not gateway:
            raise HTTPException(404, 'Esta sucursal todavía no tiene un equipo principal registrado')
        return gateway

    @router.get('/branches/{branch_id}/devices')
    def list_devices(branch_id: UUID, response: Response, gateway_id: str | None = None,
                     user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            rows = connection.execute(
                '''SELECT n.*, g.rpi_identifier, g.display_name AS gateway_name
                   FROM esp32_node n
                   JOIN gateway g ON g.id=n.gateway_id
                   JOIN branch b ON b.id=g.branch_id
                   JOIN organization o ON o.id=b.organization_id
                    WHERE g.branch_id=%s AND g.is_active AND b.is_active AND o.is_active
                      AND n.node_code IS NOT NULL AND n.registration_status='registered'
                      AND (%s::text IS NULL OR g.rpi_identifier=%s)
                    ORDER BY n.created_at''',
                (branch_id, gateway_id, gateway_id)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return [serialize_device(row) for row in rows]

    @router.get('/branches/{branch_id}/devices/{device_id}')
    def get_device(branch_id: UUID, device_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id)
            occupancy = connection.execute(
                '''SELECT current_count, recorded_at FROM occupancy_snapshot
                   WHERE node_id=%s ORDER BY recorded_at DESC LIMIT 1''',
                (device_id,)).fetchone()
            environment = connection.execute(
                '''SELECT er.temperature_c, er.humidity_pct, er.recorded_at
                   FROM environment_reading er
                   JOIN esp32_node n ON n.id=er.node_id
                   JOIN gateway g ON g.id=n.gateway_id
                   WHERE g.branch_id=%s ORDER BY er.recorded_at DESC LIMIT 1''',
                (branch_id,)).fetchone()
        response.headers['Cache-Control'] = 'no-store'
        readings = {
            'occupancy': occupancy['current_count'] if occupancy else None,
            'temperature': float(environment['temperature_c']) if environment else None,
            'humidity': float(environment['humidity_pct']) if environment else None,
            'measuredAt': iso(
                max((row['recorded_at'] for row in (occupancy, environment) if row), default=None)),
        }
        return {**serialize_device(node), 'readings': readings}

    @router.post('/branches/{branch_id}/devices/{device_id}/register')
    def register_device(branch_id: UUID, device_id: UUID, body: DeviceRegister,
                        response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id, lock=True, candidate=True)
            if body.gateway_id and node['rpi_identifier'] != body.gateway_id:
                raise HTTPException(409, 'El dispositivo no pertenece al equipo seleccionado')
            if body.discovery_id:
                session = _discoveries.get(body.discovery_id)
                if not session or session['branch_id'] != branch_id:
                    raise HTTPException(409, 'La búsqueda expiró; vuelve a buscar el dispositivo')
            code = node.get('node_code') or 'NODO-' + uuid4().hex[:6].upper()
            updated = connection.execute(
                '''UPDATE esp32_node SET name=%s, location=%s, node_code=%s,
                     registered_at=CASE WHEN node_code IS NULL THEN NOW() ELSE registered_at END,
                     registration_status='registered', updated_at=NOW()
                   WHERE id=%s RETURNING *''',
                (body.name, body.location, code, device_id)).fetchone()
            connection.commit()
            updated = {**updated, 'rpi_identifier': node['rpi_identifier']}
        response.headers['Cache-Control'] = 'no-store'
        return serialize_device(updated)

    @router.patch('/branches/{branch_id}/devices/{device_id}')
    def update_device(branch_id: UUID, device_id: UUID, body: DeviceUpdate,
                      response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id, lock=True)
            updated = connection.execute(
                '''UPDATE esp32_node SET name=%s, location=%s, updated_at=NOW()
                   WHERE id=%s RETURNING *''',
                (body.name, body.location, device_id)).fetchone()
            connection.commit()
            updated = {**updated, 'rpi_identifier': node['rpi_identifier']}
        response.headers['Cache-Control'] = 'no-store'
        return serialize_device(updated)

    @router.delete('/branches/{branch_id}/devices/{device_id}')
    async def delete_device(branch_id: UUID, device_id: UUID, response: Response,
                            force: bool = False, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id, candidate=True)
        reset_error = ''
        if not force:
            try:
                result = await hub.send(node['rpi_identifier'], 'node.reset', payload={
                    'node_id': str(device_id), 'branch_id': str(branch_id)})
                data = (result or {}).get('data') or {}
                if (not result or result.get('ok') is not True or not isinstance(data, dict)
                        or data.get('identity_reset') is not True
                        or data.get('node_id') != str(device_id)):
                    reset_error = (result or {}).get('error') or 'El nodo no confirmó el borrado'
            except HTTPException as exc:
                reset_error = exc.detail
            if reset_error:
                raise HTTPException(
                    409, f'No se pudo borrar la identidad del nodo: {reset_error}. '
                         'Actualiza el servicio del gateway si no reconoce la orden. '
                         'También puedes retirar solo el registro si el equipo no está disponible.')
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            connection.execute('DELETE FROM esp32_node WHERE id=%s', (device_id,))
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return {'id': str(device_id), 'deleted': True, 'type': NODE_TYPE_TO_API.get(node['node_type'])}

    @router.get('/branches/{branch_id}/devices/{device_id}/guide')
    def get_guide(branch_id: UUID, device_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id, node_type='guide')
            lights = connection.execute(
                '''SELECT p.*, s.name AS shelf_name FROM led_guide_point p
                   JOIN shelf s ON s.id=p.shelf_id
                   WHERE p.node_id=%s ORDER BY p.led_index''',
                (device_id,)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return {**serialize_device(node), 'lights': [serialize_light(row) for row in lights]}

    @router.get('/branches/{branch_id}/devices/{device_id}/light-channels')
    def light_channels(branch_id: UUID, device_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            load_node(connection, branch_id, device_id, node_type='guide')
            used = {row['led_index'] for row in connection.execute(
                'SELECT led_index FROM led_guide_point WHERE node_id=%s', (device_id,)).fetchall()}
        response.headers['Cache-Control'] = 'no-store'
        return [{'id': channel, 'label': f'Luz {channel + 1}', 'available': channel not in used}
                for channel in range(LIGHT_LIMIT)]

    @router.get('/branches/{branch_id}/shelves')
    def list_shelves(branch_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            rows = connection.execute(
                '''SELECT id, name, location_label FROM shelf
                   WHERE branch_id=%s AND is_active ORDER BY name''',
                (branch_id,)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return [{'id': str(row['id']), 'name': row['name'], 'location': row['location_label']}
                for row in rows]

    def shelf_in_branch(connection, branch_id, shelf_id):
        shelf = connection.execute(
            'SELECT id, name FROM shelf WHERE id=%s AND branch_id=%s AND is_active',
            (shelf_id, branch_id)).fetchone()
        if not shelf:
            raise HTTPException(404, 'Estante no encontrado en esta sucursal')
        return shelf

    # --- Fase 3: catálogo mínimo de productos y vínculo cámara-repisa ---

    @router.get('/branches/{branch_id}/products')
    def list_products(branch_id: UUID, response: Response,
                      search: str | None = Query(default=None, max_length=100),
                      limit: int = Query(default=20, ge=1, le=100),
                      user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch(connection, branch_id, user)
            term = (search or '').strip()
            pattern = f'%{term}%' if term else None
            rows = connection.execute(
                '''SELECT id, sku, name, is_active FROM product
                   WHERE organization_id=%s AND is_active
                     AND (%s::text IS NULL OR sku ILIKE %s OR name ILIKE %s)
                   ORDER BY name, sku LIMIT %s''',
                (branch['organization_id'], pattern, pattern, pattern, limit)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return [serialize_product(row) for row in rows]

    @router.post('/branches/{branch_id}/products', status_code=201)
    def create_product(branch_id: UUID, body: ProductCreate, response: Response,
                       user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch(connection, branch_id, user)
            existing = connection.execute(
                '''SELECT 1 FROM product
                   WHERE organization_id=%s AND lower(sku)=lower(%s)''',
                (branch['organization_id'], body.sku)).fetchone()
            if existing:
                raise HTTPException(409, 'Ya existe un producto con ese SKU')
            try:
                product = connection.execute(
                    '''INSERT INTO product(organization_id, sku, name)
                       VALUES(%s, %s, %s)
                       RETURNING id, sku, name, is_active''',
                    (branch['organization_id'], body.sku, body.name)).fetchone()
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'Ya existe un producto con ese SKU') from None
        response.headers['Cache-Control'] = 'no-store'
        return serialize_product(product)

    @router.get('/branches/{branch_id}/devices/{device_id}/shelf')
    def get_device_shelf(branch_id: UUID, device_id: UUID, response: Response,
                         user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            load_node(connection, branch_id, device_id, node_type='shelf_cam')
            shelf = connection.execute(
                '''SELECT * FROM shelf
                   WHERE camera_node_id=%s AND branch_id=%s AND is_active''',
                (device_id, branch_id)).fetchone()
        response.headers['Cache-Control'] = 'no-store'
        return {'shelf': serialize_shelf(shelf) if shelf else None}

    @router.put('/branches/{branch_id}/devices/{device_id}/shelf')
    def link_device_shelf(branch_id: UUID, device_id: UUID, body: ShelfLink,
                          response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            load_node(connection, branch_id, device_id, node_type='shelf_cam', lock=True)
            shelf = connection.execute(
                '''SELECT * FROM shelf
                   WHERE id=%s AND branch_id=%s AND is_active FOR UPDATE''',
                (body.shelf_id, branch_id)).fetchone()
            if not shelf:
                raise HTTPException(404, 'Estante no encontrado en esta sucursal')
            current = connection.execute(
                '''SELECT id, name FROM shelf
                   WHERE camera_node_id=%s AND branch_id=%s AND id<>%s''',
                (device_id, branch_id, body.shelf_id)).fetchone()
            if current:
                raise HTTPException(409, 'La cámara ya tiene una repisa vinculada')
            if shelf.get('camera_node_id') and shelf['camera_node_id'] != device_id:
                raise HTTPException(409, 'Ese estante ya está vinculado a otra cámara')
            updated = connection.execute(
                '''UPDATE shelf
                   SET camera_node_id=%s, camera_node_type='shelf_cam', updated_at=NOW()
                   WHERE id=%s RETURNING *''',
                (device_id, body.shelf_id)).fetchone()
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return serialize_shelf(updated)

    @router.post('/branches/{branch_id}/devices/{device_id}/shelf', status_code=201)
    def create_device_shelf(branch_id: UUID, device_id: UUID, body: ShelfCreate,
                            response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            load_node(connection, branch_id, device_id, node_type='shelf_cam', lock=True)
            current = connection.execute(
                'SELECT * FROM shelf WHERE camera_node_id=%s AND branch_id=%s',
                (device_id, branch_id)).fetchone()
            if current:
                response.headers['Cache-Control'] = 'no-store'
                response.status_code = 200
                return serialize_shelf(current)
            try:
                shelf = connection.execute(
                    '''INSERT INTO shelf(branch_id, camera_node_id, camera_node_type,
                                        name, location_label)
                       VALUES(%s, %s, 'shelf_cam', %s, %s)
                       RETURNING *''',
                    (branch_id, device_id, body.name, body.location_label)).fetchone()
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'La cámara ya tiene una repisa vinculada') from None
        response.headers['Cache-Control'] = 'no-store'
        return serialize_shelf(shelf)

    @router.patch('/branches/{branch_id}/shelves/{shelf_id}')
    def update_shelf(branch_id: UUID, shelf_id: UUID, body: ShelfUpdate,
                     response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            shelf = connection.execute(
                '''SELECT * FROM shelf WHERE id=%s AND branch_id=%s AND is_active FOR UPDATE''',
                (shelf_id, branch_id)).fetchone()
            if not shelf:
                raise HTTPException(404, 'Estante no encontrado en esta sucursal')
            updated = connection.execute(
                '''UPDATE shelf
                      SET name=%s, location_label=%s, updated_at=NOW()
                    WHERE id=%s RETURNING *''',
                (body.name, body.location_label, shelf_id)).fetchone()
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return serialize_shelf(updated)

    @router.post('/branches/{branch_id}/devices/{device_id}/lights', status_code=201)
    def add_light(branch_id: UUID, device_id: UUID, body: LightPayload,
                  response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            load_node(connection, branch_id, device_id, node_type='guide', lock=True)
            shelf = shelf_in_branch(connection, branch_id, body.shelf_id)
            existing = connection.execute(
                'SELECT * FROM led_guide_point WHERE node_id=%s AND led_index=%s',
                (device_id, body.channel)).fetchone()
            if existing:
                if existing['shelf_id'] == body.shelf_id and existing.get('name') == body.name:
                    return serialize_light({**existing, 'shelf_name': shelf['name']})
                raise HTTPException(409, 'Esa luz ya está asignada')
            total = connection.execute(
                'SELECT count(*) AS total FROM led_guide_point WHERE node_id=%s',
                (device_id,)).fetchone()['total']
            if total >= LIGHT_LIMIT:
                raise HTTPException(409, f'La guía ya tiene el límite de {LIGHT_LIMIT} luces')
            if connection.execute(
                    'SELECT 1 FROM led_guide_point WHERE node_id=%s AND led_index=%s',
                    (device_id, body.channel)).fetchone():
                raise HTTPException(409, 'Esa luz ya está asignada')
            try:
                light = connection.execute(
                    '''INSERT INTO led_guide_point(node_id, node_type, shelf_id, led_index, name)
                       VALUES(%s, 'guide', %s, %s, %s) RETURNING *''',
                    (device_id, body.shelf_id, body.channel, body.name)).fetchone()
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'Ese estante o esa luz ya están asignados') from None
        response.headers['Cache-Control'] = 'no-store'
        return serialize_light({**light, 'shelf_name': shelf['name']})

    @router.patch('/branches/{branch_id}/devices/{device_id}/lights/{light_id}')
    def update_light(branch_id: UUID, device_id: UUID, light_id: UUID, body: LightPayload,
                     response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            load_node(connection, branch_id, device_id, node_type='guide', lock=True)
            light = connection.execute(
                'SELECT * FROM led_guide_point WHERE id=%s AND node_id=%s FOR UPDATE',
                (light_id, device_id)).fetchone()
            if not light:
                raise HTTPException(404, 'Luz no encontrada en esta guía')
            if light['led_index'] != body.channel:
                raise HTTPException(422, 'La identidad física de la luz no puede cambiar')
            shelf = shelf_in_branch(connection, branch_id, body.shelf_id)
            try:
                updated = connection.execute(
                    '''UPDATE led_guide_point SET name=%s, shelf_id=%s
                       WHERE id=%s RETURNING *''',
                    (body.name, body.shelf_id, light_id)).fetchone()
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'Ese estante ya está asignado a otra luz') from None
        response.headers['Cache-Control'] = 'no-store'
        return serialize_light({**updated, 'shelf_name': shelf['name']})

    @router.post('/branches/{branch_id}/devices/{device_id}/lights/test')
    async def test_light(branch_id: UUID, device_id: UUID, body: LightTest,
                         response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id, node_type='guide')
            gateway = connection.execute(
                'SELECT rpi_identifier FROM gateway WHERE id=%s AND is_active',
                (node['gateway_id'],)).fetchone()
        if not gateway:
            raise HTTPException(404, 'El equipo principal de la sucursal no está disponible')
        payload = {
            'node_id': str(device_id),
            'branch_id': str(branch_id),
            'node_type': 'guide',
            'module': NODE_MODULE['guide'],
            'channel': body.channel,
            'duration_ms': body.duration_seconds * 1000,
        }
        try:
            result = await hub.send(gateway['rpi_identifier'], 'light.test', payload=payload)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, f'No se pudo enviar la prueba: {exc.detail}') from None
        if not result or result.get('ok') is not True:
            raise HTTPException(502, (result or {}).get('error') or 'La Raspberry no confirmó el envío')
        response.headers['Cache-Control'] = 'no-store'
        return {'confirmed': True, 'channel': body.channel}

    @router.post('/branches/{branch_id}/gateways/{gateway_id}/discovery', status_code=201)
    async def start_discovery(branch_id: UUID, gateway_id: str, response: Response,
                              user=Depends(current_user)):
        _prune_discoveries()
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            gateway = connection.execute(
                '''SELECT id FROM gateway WHERE rpi_identifier=%s AND branch_id=%s AND is_active''',
                (gateway_id, branch_id)).fetchone()
        if not gateway:
            raise HTTPException(404, 'Equipo principal no registrado en esta sucursal')
        if gateway_id in _starting_gateways or any(
                s['gateway_id'] == gateway_id and not s.get('stopped')
                and time.time() < s['expires_at'] + 60 for s in _discoveries.values()):
            raise HTTPException(409, 'Ya hay una búsqueda activa en este equipo; espera a que termine')
        _starting_gateways.add(gateway_id)
        try:
            result = await hub.send(gateway_id, 'tcp.enable')
            if not result or result.get('ok') is not True:
                raise HTTPException(502, (result or {}).get('error') or 'La Raspberry no confirmó el inicio')
        except HTTPException as exc:
            raise HTTPException(exc.status_code,
                                f'No se pudo iniciar el emparejamiento: {exc.detail}') from None
        finally:
            _starting_gateways.discard(gateway_id)
        try:
            _discard_stale_candidates(get_connection, gateway_id, branch_id)
        except Exception:
            # La limpieza es mantenimiento: no debe impedir una búsqueda válida.
            logging.getLogger(__name__).warning(
                'No se pudieron retirar candidatos huérfanos', exc_info=True)
        now = time.time()
        discovery_id = _new_discovery_id()
        _discoveries[discovery_id] = {
            'id': discovery_id,
            'branch_id': branch_id,
            'gateway_id': gateway_id,
            'started_at': now,
            'expires_at': now + DISCOVERY_TIMEOUT_SECONDS,
            'fingerprint': None,
            'settled_at': now,
            'gateway_lost': False,
        }
        task = asyncio.create_task(expire_discovery(_discoveries[discovery_id], hub))
        _expiry_tasks.add(task)
        task.add_done_callback(_expiry_tasks.discard)
        response.headers['Cache-Control'] = 'no-store'
        return {'id': discovery_id}

    @router.get('/branches/{branch_id}/discoveries/{discovery_id}')
    def get_discovery(branch_id: UUID, discovery_id: str, response: Response,
                      user=Depends(current_user)):
        session = _discoveries.get(discovery_id)
        if not session or session['branch_id'] != branch_id:
            raise HTTPException(404, 'Búsqueda no encontrada o expirada')
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            devices = _discovery_candidates(connection, session['gateway_id'], branch_id, session['started_at'])
        session['gateway_lost'] = session['gateway_id'] not in hub.sessions
        status = _discovery_state(session, devices, time.time())
        response.headers['Cache-Control'] = 'no-store'
        return {'id': discovery_id, 'status': status, 'devices': devices}

    @router.post('/branches/{branch_id}/discoveries/{discovery_id}/stop')
    async def stop_discovery(branch_id: UUID, discovery_id: str, response: Response,
                             user=Depends(current_user)):
        session = _discoveries.get(discovery_id)
        if not session or session['branch_id'] != branch_id:
            raise HTTPException(404, 'Búsqueda no encontrada o expirada')
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
        result = await hub.send(session['gateway_id'], 'tcp.disable')
        if not result or result.get('ok') is not True:
            raise HTTPException(502, (result or {}).get('error') or 'La Raspberry no confirmó la detención')
        session['stopped'] = True
        _discoveries.pop(discovery_id, None)
        response.headers['Cache-Control'] = 'no-store'
        return {'id': discovery_id, 'status': 'stopped'}

    @router.post('/gateways/{gateway_id}/devices/{device_id}/snapshot')
    async def upload_snapshot(gateway_id: str, device_id: UUID, request: Request,
                              response: Response):
        """Recibe una imagen JPEG firmada por el gateway (Ed25519)."""
        timestamp = request.headers.get('x-timestamp', '')
        nonce = request.headers.get('x-nonce', '')
        signature_b64 = request.headers.get('x-signature', '')
        if not timestamp or not nonce or not signature_b64:
            raise HTTPException(400, 'Faltan headers de firma')
        try:
            ts = int(timestamp)
        except ValueError:
            raise HTTPException(400, 'Timestamp inválido')
        if abs(time.time() - ts) > 120:
            raise HTTPException(401, 'Firma expirada; compruebe NTP en la Raspberry')
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (ValueError, TypeError):
            raise HTTPException(401, 'Firma incorrecta') from None

        raw = await request.body()
        if not raw or len(raw) > 200000:
            raise HTTPException(413, 'Imagen demasiado grande o vacía')

        image_hash = hashlib.sha256(raw).hexdigest()

        with get_connection() as connection:
            gateway = connection.execute(
                '''SELECT g.id, g.branch_id, g.device_public_key, g.is_active, t.token_hash
                   FROM gateway g JOIN branch b ON b.id=g.branch_id
                   JOIN organization o ON o.id=b.organization_id
                   LEFT JOIN organization_token t ON t.id=g.registered_with_token
                   WHERE g.rpi_identifier=%s AND g.is_active AND b.is_active AND o.is_active''',
                (gateway_id,)).fetchone()
            if not gateway or not gateway['device_public_key']:
                raise HTTPException(403, 'Gateway no registrado o desactivado')
            from .gateway_registry import active_token
            if not gateway['token_hash'] or not active_token(connection, gateway['token_hash'], gateway['branch_id']):
                raise HTTPException(403, 'Token de sucursal revocado')
            public_key = base64.b64decode(gateway['device_public_key'], validate=True)
            message = f'selfiq-snapshot-v1\\n{gateway_id}\\n{timestamp}\\n{nonce}\\n{device_id}\\n{image_hash}'.encode()
            try:
                Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
            except (ValueError, InvalidSignature):
                raise HTTPException(401, 'Firma no corresponde al gateway') from None

            # Verificar que el dispositivo pertenece al gateway
            node = connection.execute(
                '''SELECT n.id FROM esp32_node n WHERE n.id=%s AND n.gateway_id=%s
                     AND n.node_code IS NOT NULL AND n.registration_status='registered' ''',
                (device_id, gateway['id'])).fetchone()
            if not node:
                raise HTTPException(404, 'Dispositivo no encontrado en este gateway')

            # Guardar snapshot (se conserva historial; GET devuelve el mas reciente)
            connection.execute(
                '''INSERT INTO snapshot (branch_id, device_id, data, content_type, captured_at)
                   VALUES (%s, %s, %s, 'image/jpeg', NOW())''',
                (gateway['branch_id'], device_id, raw))
            connection.commit()

        response.headers['Cache-Control'] = 'no-store'
        return {'ok': True, 'bytes': len(raw), 'device_id': str(device_id)}

    @router.get('/branches/{branch_id}/devices/{device_id}/snapshot')
    def get_snapshot(branch_id: UUID, device_id: UUID, response: Response,
                     user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id)
            row = connection.execute(
                '''SELECT data, captured_at FROM snapshot WHERE device_id=%s
                   ORDER BY captured_at DESC LIMIT 1''',
                (device_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Sin imagen para este dispositivo')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Captured-At'] = str(row['captured_at'])
        return Response(content=row['data'], media_type='image/jpeg')

    @router.post('/branches/{branch_id}/devices/{device_id}/snapshot/request')
    async def request_snapshot(branch_id: UUID, device_id: UUID, response: Response,
                              user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id)
            gateway = connection.execute(
                'SELECT rpi_identifier FROM gateway WHERE id=%s AND is_active',
                (node['gateway_id'],)).fetchone()
        if not gateway:
            raise HTTPException(404, 'El equipo principal de la sucursal no está disponible')
        import uuid as _uuid
        req_id = str(_uuid.uuid4())
        try:
            result = await hub.send(gateway['rpi_identifier'], 'snapshot.request', payload={
                'node_id': str(device_id),
                'branch_id': str(branch_id),
                'module': 'shelf_cam',
                'req_id': req_id,
            })
        except HTTPException as exc:
            raise HTTPException(exc.status_code, f'No se pudo solicitar la foto: {exc.detail}') from None
        if not result or result.get('ok') is not True:
            raise HTTPException(502, (result or {}).get('error') or 'La Raspberry no confirmó el envío')
        response.headers['Cache-Control'] = 'no-store'
        return {'confirmed': True, 'req_id': req_id}

    return router

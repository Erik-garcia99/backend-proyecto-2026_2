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

from fastapi import APIRouter, Depends, HTTPException, Response
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


def load_node(connection, branch_id: UUID, device_id: UUID, *, node_type=None, lock=False):
    query = '''SELECT n.*, g.branch_id AS gateway_branch_id, g.rpi_identifier
               FROM esp32_node n
               JOIN gateway g ON g.id=n.gateway_id
               JOIN branch b ON b.id=g.branch_id
               JOIN organization o ON o.id=b.organization_id
               WHERE n.id=%s AND g.branch_id=%s AND g.is_active
                 AND b.is_active AND o.is_active'''
    params = [device_id, branch_id]
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
    # Un alta no demuestra conexión permanente; el firmware básico no envía
    # todavía latidos periódicos al backend.
    last_seen = node.get('last_heartbeat_at')
    if not last_seen or (datetime.now(timezone.utc) - last_seen).total_seconds() > 120:
        return None
    return bool(node.get('is_online'))


def serialize_device(node):
    node_type = node['node_type']
    api_type = NODE_TYPE_TO_API.get(node_type, node_type)
    name = node.get('name') or {
        'guide': 'Guía de luces',
        'occupancy': 'Control de aforo',
        'camera': 'Cámara',
    }.get(api_type, 'Dispositivo')
    display_code = node.get('node_code') or (node.get('mac_address') or '')[-5:]
    return {
        'id': str(node['id']),
        'type': api_type,
        'name': name,
        'location': node.get('location'),
        'online': device_online(node),
        'lastSeenAt': iso(node.get('last_heartbeat_at') or node.get('registered_at')),
        'nodeType': node_type,
        'nodeCode': node.get('node_code'),
        'displayCode': display_code,
        'macAddress': node.get('mac_address'),
        'gatewayId': node.get('rpi_identifier') or str(node['gateway_id']),
    }


def serialize_light(row):
    return {
        'id': str(row['id']),
        'name': row.get('name') or f"Luz {row['led_index'] + 1}",
        'shelfId': str(row['shelf_id']),
        'shelfName': row.get('shelf_name'),
        'channel': row['led_index'],
    }


def _prune_discoveries():
    now = time.time()
    for key, session in tuple(_discoveries.items()):
        if now > session['expires_at'] + 300:
            _discoveries.pop(key, None)


def _new_discovery_id():
    return uuid4().hex


def _discovery_candidates(connection, gateway_id, branch_id):
    rows = connection.execute(
        '''SELECT n.* FROM esp32_node n
           JOIN gateway g ON g.id=n.gateway_id
           WHERE g.rpi_identifier=%s AND g.branch_id=%s
             AND n.node_code IS NULL
             AND NULLIF(btrim(COALESCE(n.name, '')), '') IS NULL
           ORDER BY n.created_at''',
        (gateway_id, branch_id)).fetchall()
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
                '''SELECT n.*, g.rpi_identifier FROM esp32_node n
                   JOIN gateway g ON g.id=n.gateway_id
                   JOIN branch b ON b.id=g.branch_id
                   JOIN organization o ON o.id=b.organization_id
                    WHERE g.branch_id=%s AND g.is_active AND b.is_active AND o.is_active
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
            node = load_node(connection, branch_id, device_id, lock=True)
            if body.gateway_id and node['rpi_identifier'] != body.gateway_id:
                raise HTTPException(409, 'El dispositivo no pertenece al equipo seleccionado')
            if body.discovery_id:
                session = _discoveries.get(body.discovery_id)
                if not session or session['branch_id'] != branch_id:
                    raise HTTPException(409, 'La búsqueda expiró; vuelve a buscar el dispositivo')
            code = node.get('node_code') or 'NODO-' + uuid4().hex[:6].upper()
            updated = connection.execute(
                '''UPDATE esp32_node SET name=%s, location=%s, node_code=%s,
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
    async def delete_device(branch_id: UUID, device_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, device_id)
        result = await hub.send(node['rpi_identifier'], 'node.reset', payload={
            'node_id': str(device_id), 'branch_id': str(branch_id)})
        data = (result or {}).get('data') or {}
        if (not result or result.get('ok') is not True or not isinstance(data, dict)
                or data.get('identity_reset') is not True or data.get('node_id') != str(device_id)):
            raise HTTPException(502, (result or {}).get('error') or
                                'El nodo no confirmó el borrado de identidad. Puedes reintentar.')
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
            devices = _discovery_candidates(connection, session['gateway_id'], branch_id)
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

    return router

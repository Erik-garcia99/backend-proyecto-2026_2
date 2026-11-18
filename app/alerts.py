"""Bandeja Must. Nunca interpreta ausencia de lecturas como agotado.

Solo el monitor de heartbeat produce episodios en esta entrega. Los demás
productores necesitan contratos de ingestión propios, no escrituras desde la UI.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal, get_args
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)
AlertType = Literal['stock_low', 'stock_empty', 'discrepancy', 'environment',
                    'battery_low', 'node_offline', 'gateway_offline']
TYPES = list(get_args(AlertType))
ACTIVE = ('open', 'acknowledged')
HEARTBEAT_TIMEOUT = 45
MONITOR_INTERVAL = 10
monitor_health = {'last_success_at': None, 'error': False}


def staff_branch(connection, branch_id, user):
    role = user.get('role', '').lower()
    if role not in {'sudo', 'owner', 'gerencia', 'manager', 'rh', 'employee', 'staff'}:
        raise HTTPException(403, 'Alertas es exclusivo para personal autorizado')
    branch = connection.execute(
        """SELECT b.id, b.organization_id FROM branch b
           JOIN organization o ON o.id=b.organization_id
           WHERE b.id=%s AND b.is_active AND o.is_active""", (branch_id,)).fetchone()
    if not branch:
        raise HTTPException(404, 'Sucursal no encontrada o inactiva')
    if user.get('is_superuser'):
        return branch
    if user.get('organization_id') == branch['organization_id'] and (
            role in {'owner', 'gerencia'} or user.get('branch_id') == branch_id):
        return branch
    raise HTTPException(403, 'No tienes permisos sobre esta sucursal')


def heartbeat_condition(last_heartbeat, now):
    """None = fuente desconocida; no genera ni recupera un episodio."""
    if last_heartbeat is None or last_heartbeat > now:
        return None
    return (now - last_heartbeat).total_seconds() >= HEARTBEAT_TIMEOUT


def episode(connection, *, branch_id, node_id, gateway_id, condition, metadata):
    """Serializado por nodo, durable y transaccional junto con su bitácora."""
    if condition is None:
        return
    key = f'heartbeat:{node_id}'
    connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (key,))
    existing = connection.execute(
        """SELECT id FROM alert WHERE branch_id=%s AND dedupe_key=%s
           AND status IN ('open','acknowledged') FOR UPDATE""",
        (branch_id, key)).fetchone()
    if condition and not existing:
        message = 'No se recibió heartbeat del nodo durante al menos 45 segundos'
        row = connection.execute(
            """INSERT INTO alert(branch_id, alert_type, severity, node_id, gateway_id,
                 message, metadata, dedupe_key)
               VALUES(%s,'node_offline','warning',%s,%s,%s,%s,%s) RETURNING id""",
            (branch_id, node_id, gateway_id, message, Jsonb(metadata), key)).fetchone()
        event = 'alert.opened'
    elif not condition and existing:
        # No se cierra por reconocimiento manual: solo por nueva presencia.
        row = existing
        connection.execute(
            """UPDATE alert SET status='resolved', resolved_at=NOW()
               WHERE branch_id=%s AND dedupe_key=%s AND status IN ('open','acknowledged')""",
            (branch_id, key))
        message = 'Se volvió a recibir heartbeat del nodo'
        event = 'alert.recovered'
    else:
        return
    connection.execute(
        """INSERT INTO system_log(branch_id,node_id,gateway_id,category,severity,
             event_type,message,metadata) VALUES(%s,%s,%s,'alerts','info',%s,%s,%s)""",
        (branch_id, node_id, gateway_id, event, message,
         Jsonb({'alert_id': str(row['id']), **metadata})))


def scan_heartbeats(get_connection):
    with get_connection() as connection:
        # Bloquear presencia evita resolver usando una lectura previa a un heartbeat.
        rows = connection.execute(
            """SELECT n.id,n.gateway_id,n.name,n.last_heartbeat_at,g.branch_id
               FROM esp32_node n JOIN gateway g ON g.id=n.gateway_id
               JOIN branch b ON b.id=g.branch_id
               JOIN organization o ON o.id=b.organization_id
               WHERE n.node_code IS NOT NULL AND n.registration_status='registered'
                 AND g.is_active AND b.is_active AND o.is_active
               ORDER BY n.id FOR UPDATE OF n""").fetchall()
        now = datetime.now(timezone.utc)
        for row in rows:
            stamp = row['last_heartbeat_at']
            episode(connection, branch_id=row['branch_id'], node_id=row['id'],
                    gateway_id=row['gateway_id'],
                    condition=heartbeat_condition(stamp, now),
                    metadata={'source': 'signed_node_heartbeat', 'node_name': row['name'],
                              'last_heartbeat_at': stamp.isoformat() if stamp else None})
        connection.commit()


async def monitor(get_connection, stop):
    monitor_health.update(last_success_at=None, error=False)
    while not stop.is_set():
        try:
            await asyncio.to_thread(scan_heartbeats, get_connection)
            monitor_health.update(last_success_at=datetime.now(timezone.utc), error=False)
        except Exception:
            monitor_health['error'] = True
            log.exception('No se pudo evaluar la presencia de nodos para Alertas')
        try:
            await asyncio.wait_for(stop.wait(), MONITOR_INTERVAL)
        except asyncio.TimeoutError:
            pass


def create_router(current_user, get_connection):
    router = APIRouter(tags=['Alerts'])

    @router.get('/branches/{branch_id}/alert-locations')
    def locations(branch_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            staff_branch(connection, branch_id, user)
            rows = connection.execute(
                """SELECT s.id,s.name,s.location_label,l.node_id,l.led_index,
                     l.name AS light_name,n.name AS node_name,n.id AS confirmed_node_id
                   FROM shelf s LEFT JOIN led_guide_point l ON l.shelf_id=s.id
                   LEFT JOIN esp32_node n ON n.id=l.node_id AND n.node_type='guide'
                     AND n.node_code IS NOT NULL AND n.registration_status='registered'
                     AND EXISTS (SELECT 1 FROM gateway g WHERE g.id=n.gateway_id
                       AND g.branch_id=s.branch_id AND g.is_active)
                   WHERE s.branch_id=%s AND s.is_active
                   ORDER BY s.name,s.id,l.node_id,l.led_index""", (branch_id,)).fetchall()
        shelves = {}
        for row in rows:
            shelf = shelves.setdefault(str(row['id']), {
                'id': row['id'], 'name': row['name'],
                'location': row['location_label'], 'lights': []})
            if row['confirmed_node_id']:
                shelf['lights'].append({'node_id': row['node_id'], 'node_name': row['node_name'] or 'Nodo guía',
                                        'channel': row['led_index'], 'name': row['light_name']})
        response.headers['Cache-Control'] = 'no-store'
        return list(shelves.values())

    @router.get('/branches/{branch_id}/alerts')
    def list_alerts(branch_id: UUID, response: Response,
                    status: Literal['active', 'all', 'resolved'] = 'active',
                    alert_type: AlertType | None = None,
                    severity: Literal['info', 'warning', 'critical'] | None = None,
                    node_id: UUID | None = None, product_id: UUID | None = None,
                    since: datetime | None = None, until: datetime | None = None,
                    offset: int = Query(0, ge=0, le=100000),
                    limit: int = Query(50, ge=1, le=100), user=Depends(current_user)):
        if any(value and value.tzinfo is None for value in (since, until)):
            raise HTTPException(422, 'Las fechas deben incluir zona horaria')
        if since and until and since > until:
            raise HTTPException(422, 'El inicio debe ser anterior al final')
        clauses, args = ['branch_id=%s', 'alert_type=ANY(%s)'], [branch_id, TYPES]
        if status == 'active':
            clauses.append("status IN ('open','acknowledged')")
        elif status == 'resolved':
            clauses.append("status='resolved'")
        for column, value in [('alert_type', alert_type), ('severity', severity), ('node_id', node_id)]:
            if value is not None:
                clauses.append(f'{column}=%s')
                args.append(value)
        if product_id:
            clauses.append("metadata->>'product_id'=%s")
            args.append(str(product_id))
        for op, value in [('>=', since), ('<=', until)]:
            if value:
                clauses.append(f'triggered_at {op} %s')
                args.append(value)
        with get_connection() as connection:
            staff_branch(connection, branch_id, user)
            rows = connection.execute(
                f"""SELECT * FROM alert WHERE {' AND '.join(clauses)}
                    ORDER BY triggered_at DESC,id DESC LIMIT %s OFFSET %s""",
                tuple(args + [limit + 1, offset])).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return {'items': rows[:limit], 'has_more': len(rows) > limit,
                'monitor': dict(monitor_health),
                'sources': {
                    'node_offline': 'signed_heartbeat_monitor',
                    'stock_low': 'roi_state_monitor',
                    'stock_empty': 'roi_state_monitor',
                    **{name: 'not_connected' for name in TYPES
                       if name not in ('node_offline', 'stock_low', 'stock_empty')}}}

    @router.get('/branches/{branch_id}/alerts/{alert_id}')
    def detail(branch_id: UUID, alert_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            staff_branch(connection, branch_id, user)
            row = connection.execute(
                'SELECT * FROM alert WHERE branch_id=%s AND id=%s AND alert_type=ANY(%s)',
                (branch_id, alert_id, TYPES)).fetchone()
        if not row:
            raise HTTPException(404, 'Alerta no encontrada')
        response.headers['Cache-Control'] = 'no-store'
        return row

    @router.post('/branches/{branch_id}/alerts/{alert_id}/acknowledge')
    def acknowledge(branch_id: UUID, alert_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            staff_branch(connection, branch_id, user)
            row = connection.execute(
                'SELECT * FROM alert WHERE branch_id=%s AND id=%s AND alert_type=ANY(%s) FOR UPDATE',
                (branch_id, alert_id, TYPES)).fetchone()
            if not row:
                raise HTTPException(404, 'Alerta no encontrada')
            if row['status'] == 'resolved':
                raise HTTPException(409, 'La condición ya se recuperó; recarga el historial')
            if row['status'] not in ACTIVE:
                raise HTTPException(409, 'La alerta no tiene un estado de atención válido')
            if row['status'] == 'open':
                row = connection.execute(
                    """UPDATE alert SET status='acknowledged',acknowledged_by=%s,
                         acknowledged_at=NOW() WHERE branch_id=%s AND id=%s RETURNING *""",
                    (user['id'], branch_id, alert_id)).fetchone()
                connection.execute(
                    """INSERT INTO system_log(branch_id,user_id,category,event_type,message,metadata)
                       VALUES(%s,%s,'alerts','alert.acknowledged','Alerta reconocida por personal',%s)""",
                    (branch_id, user['id'], Jsonb({'alert_id': str(alert_id)})))
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return row

    return router

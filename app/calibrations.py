"""Calibración de zonas con referencias vacía/abastecida — Fase 4.

Esta primera parte (E3.1) cubre el esquema y las operaciones de lectura y
creación de sesión. Las mutaciones físicas (captura, confirmación, cancelación),
el despacho por WSS y la subida de imágenes se implementan en E3.2.

Reglas que este módulo garantiza desde ya:

* Una sola sesión abierta por cámara.
* La sesión congela SKU, geometría, nodo y ``config_version``; la huella
  canónica se calcula sobre ese contexto y nunca sobre datos mutables.
* La versión de calibración se reserva al crear y no se reutiliza.
* Los ``POST`` son idempotentes por ``Idempotency-Key``.
* Los errores llevan ``code`` y ``detail``; el cliente móvil ya los consume.

Contrato: ``docs/PLAN_FASE_4_CALIBRACION.md`` (Anexo A).
"""
import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from .devices import require_branch

FORMAT_VERSION = 1
SESSION_TTL = timedelta(minutes=30)
CAPTURE_TTL = timedelta(seconds=10)
LIST_LIMIT = 20
LIST_LIMIT_MAX = 50

OPEN_STATES = ('draft', 'empty_ready', 'pair_ready', 'committing', 'cancel_pending')
TERMINAL_STATES = ('active', 'cancelled', 'expired', 'failed')

STATE_LABELS = {
    'draft': 'Preparando',
    'empty_ready': 'Falta la zona abastecida',
    'pair_ready': 'Lista para revisar',
    'committing': 'Confirmando con la cámara',
    'active': 'Calibración guardada',
    'cancel_pending': 'Cancelando',
    'cancelled': 'Cancelada',
    'expired': 'Caducada',
    'failed': 'No se pudo completar',
}


class CalibrationError(HTTPException):
    """Error con ``code`` estable además de ``detail``.

    El manejador registrado en ``app.main`` añade ``request_id``. Al ser una
    subclase de ``HTTPException`` sigue funcionando en pruebas que no registran
    el manejador.
    """

    def __init__(self, status_code, code, detail):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


class CreateSessionBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    expected_config_version: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# Utilidades puras (probadas sin base de datos)
# ---------------------------------------------------------------------------


def rfc3339(value):
    """Fecha UTC RFC 3339 con milisegundos y sufijo Z."""
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime('%Y-%m-%dT%H:%M:%S.') + f'{value.microsecond // 1000:03d}Z'


def millionths(value):
    """Convierte una fracción 0..1 a millonésimas enteras sin ruido binario."""
    if isinstance(value, bool):
        raise TypeError('La geometría debe ser numérica')
    return int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def canonical_context(context):
    """JSON compacto con claves ordenadas, sin espacios."""
    return json.dumps(context, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def compute_context_hash(context):
    return hashlib.sha256(canonical_context(context)).hexdigest()


def hash_json(value):
    return hashlib.sha256(canonical_context(value)).hexdigest()


def build_context(roi, format_version=FORMAT_VERSION):
    """Contexto canónico de la pareja que se va a calibrar.

    ``roi`` debe traer la geometría de la zona, el SKU y los datos del nodo
    resueltos por ``load_roi_context``. No se acepta nada de esto desde el
    cliente.
    """
    return {
        'bbox': [
            millionths(roi['bbox_x']),
            millionths(roi['bbox_y']),
            millionths(roi['bbox_w']),
            millionths(roi['bbox_h']),
        ],
        'config_version': int(roi['roi_applied_version']),
        'format_version': int(format_version),
        'height': int(roi.get('camera_frame_height') or 480),
        'node_id': str(roi['camera_node_id']),
        'orientation': int(roi.get('camera_orientation') or 0),
        'product_id': str(roi['product_id']),
        'roi_id': str(roi['id']),
        'sku': (roi.get('sku') or '').strip().upper(),
        'visual_revision': int(roi.get('camera_visual_revision') or 1),
        'width': int(roi.get('camera_frame_width') or 640),
    }


def new_request_id():
    return str(uuid4())


def require_idempotency_key(value):
    if not isinstance(value, str) or not value.strip():
        raise CalibrationError(
            422, 'invalid_request',
            'Falta la clave de idempotencia de la operación.')
    value = value.strip()
    if len(value) > 200:
        raise CalibrationError(422, 'invalid_request', 'La clave de idempotencia es demasiado larga.')
    return value


def parse_uuid(value, field):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise CalibrationError(422, 'invalid_request', f'El campo {field} no es un identificador válido.') from None


def encode_cursor(created_at, identifier):
    stamp = created_at.astimezone(timezone.utc).isoformat()
    raw = f'{stamp}|{identifier}'.encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def decode_cursor(value):
    if not value:
        return None
    try:
        padded = value + '=' * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        stamp, identifier = raw.split('|', 1)
        created_at = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return created_at, UUID(identifier)
    except (ValueError, TypeError, binascii.Error):
        raise CalibrationError(422, 'invalid_request', 'El cursor del historial no es válido.') from None


# ---------------------------------------------------------------------------
# Consultas compartidas con rois.py
# ---------------------------------------------------------------------------


def expire_stale_sessions(connection, *, camera_node_id=None, roi_id=None):
    """Marca como ``expired`` las sesiones abiertas cuyo plazo venció.

    Se llama antes de crear o leer para que el índice parcial de una sesión
    abierta por cámara no quede bloqueado por una sesión abandonada.
    """
    query = '''UPDATE roi_calibration_session
                  SET state='expired', closed_at=NOW(), updated_at=NOW(),
                      session_version=session_version+1
                WHERE state = ANY(%s) AND expires_at < NOW()'''
    params = [list(OPEN_STATES)]
    if camera_node_id is not None:
        query += ' AND camera_node_id=%s'
        params.append(camera_node_id)
    if roi_id is not None:
        query += ' AND roi_id=%s'
        params.append(roi_id)
    connection.execute(query, tuple(params))


def load_roi_context(connection, branch_id, roi_id, *, lock=False):
    query = '''SELECT r.*, p.sku, p.name AS product_name,
                      s.roi_version, s.roi_applied_version, s.camera_node_id,
                      n.camera_frame_width, n.camera_frame_height,
                      n.camera_orientation, n.camera_visual_revision,
                      g.rpi_identifier
                 FROM roi r
                 JOIN product p ON p.id=r.product_id
                 JOIN shelf s ON s.id=r.shelf_id
                 LEFT JOIN esp32_node n ON n.id=s.camera_node_id
                 LEFT JOIN gateway g ON g.id=n.gateway_id AND g.is_active
                 JOIN branch b ON b.id=s.branch_id AND b.is_active
                 JOIN organization o ON o.id=b.organization_id AND o.is_active
                WHERE r.id=%s AND s.branch_id=%s AND r.is_active'''
    if lock:
        query += ' FOR UPDATE OF r'
    row = connection.execute(query, (roi_id, branch_id)).fetchone()
    if not row:
        raise CalibrationError(404, 'roi_not_found', 'No encontramos esta zona en la sucursal.')
    return row


def load_session(connection, roi_id, calibration_id, *, lock=False):
    query = 'SELECT * FROM roi_calibration_session WHERE id=%s AND roi_id=%s'
    if lock:
        query += ' FOR UPDATE'
    row = connection.execute(query, (calibration_id, roi_id)).fetchone()
    if not row:
        raise CalibrationError(404, 'calibration_not_found', 'No encontramos esta calibración.')
    return row


def load_references(connection, session_id):
    return connection.execute(
        '''SELECT * FROM roi_calibration
            WHERE session_id=%s
            ORDER BY reference_type, received_at NULLS LAST, captured_at NULLS LAST''',
        (session_id,)).fetchall()


def load_operations(connection, session_id, limit=20):
    return connection.execute(
        '''SELECT * FROM calibration_operation
            WHERE session_id=%s
            ORDER BY created_at DESC, id DESC
            LIMIT %s''',
        (session_id, limit)).fetchall()


def active_pair(connection, roi):
    if not roi.get('active_calibration_id'):
        return None
    row = connection.execute(
        'SELECT * FROM roi_calibration_session WHERE id=%s AND state=%s',
        (roi['active_calibration_id'], 'active')).fetchone()
    if not row:
        return None
    return {
        'calibration_version': row['calibration_version'],
        'confirmed_at': rfc3339(row.get('confirmed_at')),
        'context_valid': roi.get('calibration_status') == 'calibrated',
    }


def branch_context(connection, branch_id, user):
    try:
        return require_branch(connection, branch_id, user)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise CalibrationError(403, 'branch_forbidden', exc.detail) from None
        raise CalibrationError(404, 'roi_not_found', 'Sucursal no encontrada o inactiva') from None


# ---------------------------------------------------------------------------
# Serialización (sin descriptores ni imágenes)
# ---------------------------------------------------------------------------


def serialize_product(row):
    return {
        'id': str(row['product_id']),
        'sku': row['product_sku'],
        'name': row['product_name'],
    }


def serialize_reference(row):
    return {
        'capture_id': str(row['capture_id']) if row.get('capture_id') else None,
        'kind': row['reference_type'],
        'status': row.get('status') or 'unknown',
        'captured_at': rfc3339(row.get('captured_at')),
        'received_at': rfc3339(row.get('received_at')),
        'image_id': str(row['image_id']) if row.get('image_id') else None,
        'descriptor_crc': row.get('descriptor_crc'),
        'descriptor_format': row.get('descriptor_format'),
        'quality': row.get('quality') or {},
        'clock_quality': row.get('clock_quality'),
    }


def serialize_operation(row):
    return {
        'operation_id': str(row['id']),
        'action': row['action'],
        'status': row['status'],
        'attempts': row.get('attempts') or 0,
        'code': row.get('code'),
        'updated_at': rfc3339(row.get('updated_at')),
    }


def serialize_session(row, references=(), operations=(), active=None):
    return {
        'id': str(row['id']),
        'roi_id': str(row['roi_id']),
        'state': row['state'],
        'session_version': row['session_version'],
        'calibration_version': row['calibration_version'],
        'config_version': row['config_version'],
        'context_hash': row['context_hash'],
        'product': serialize_product(row),
        'active': active,
        'references': [serialize_reference(reference) for reference in references],
        'operations': [serialize_operation(operation) for operation in operations],
        'created_at': rfc3339(row.get('created_at')),
        'expires_at': rfc3339(row.get('expires_at')),
        'confirmed_at': rfc3339(row.get('confirmed_at')),
    }


def serialize_summary(row, context_valid):
    return {
        'id': str(row['id']),
        'state': row['state'],
        'calibration_version': row['calibration_version'],
        'session_version': row['session_version'],
        'config_version': row['config_version'],
        'context_valid': bool(context_valid),
        'product': serialize_product(row),
        'created_at': rfc3339(row.get('created_at')),
        'confirmed_at': rfc3339(row.get('confirmed_at')),
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_router(current_user, get_connection, hub=None):
    router = APIRouter(tags=['Calibration'])

    def detail_payload(connection, roi, session):
        return serialize_session(
            session,
            load_references(connection, session['id']),
            load_operations(connection, session['id']),
            active_pair(connection, roi),
        )

    @router.post('/branches/{branch_id}/rois/{roi_id}/calibrations', status_code=201)
    def create_session(
        branch_id: UUID,
        roi_id: UUID,
        body: CreateSessionBody,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
        user=Depends(current_user),
    ):
        key = require_idempotency_key(idempotency_key)
        request = body.model_dump(exclude_none=True)
        request_hash = hash_json(request)

        with get_connection() as connection:
            branch_context(connection, branch_id, user)

            previous = connection.execute(
                'SELECT * FROM calibration_operation WHERE idempotency_key=%s',
                (key,)).fetchone()
            if previous:
                if previous['request_hash'] != request_hash or previous['action'] != 'create':
                    raise CalibrationError(
                        409, 'idempotency_conflict',
                        'Se repitió la solicitud con datos distintos. Abre la zona de nuevo.')
                if not previous.get('session_id'):
                    raise CalibrationError(
                        409, 'operation_conflict',
                        'La operación anterior no terminó. Vuelve a intentarlo.')
                roi = load_roi_context(connection, branch_id, roi_id)
                session = load_session(connection, roi_id, previous['session_id'])
                response.headers['Cache-Control'] = 'no-store'
                return detail_payload(connection, roi, session)

            roi = load_roi_context(connection, branch_id, roi_id, lock=True)
            if not roi.get('camera_node_id'):
                raise CalibrationError(
                    503, 'camera_unavailable',
                    'El estante no está vinculado a una cámara. Vincúlalo antes de calibrar.')
            expire_stale_sessions(connection, camera_node_id=roi['camera_node_id'])

            expected = request.get('expected_config_version')
            if expected is not None and int(expected) != int(roi['roi_applied_version']):
                raise CalibrationError(
                    409, 'config_mismatch',
                    'La configuración de zonas cambió. Vuelve a cargar la zona antes de calibrar.')
            if int(roi['roi_version']) != int(roi['roi_applied_version']):
                raise CalibrationError(
                    409, 'session_conflict',
                    'La zona tiene cambios sin aplicar. Aplica las zonas antes de calibrar.')

            open_session = connection.execute(
                '''SELECT id FROM roi_calibration_session
                    WHERE camera_node_id=%s AND state = ANY(%s)
                    LIMIT 1''',
                (roi['camera_node_id'], list(OPEN_STATES))).fetchone()
            if open_session:
                raise CalibrationError(
                    409, 'busy',
                    'La cámara está ocupada con otra calibración. Espera a que termine e inténtalo de nuevo.')

            operation_id = uuid4()
            context = build_context(roi)
            context_hash = compute_context_hash(context)
            expires_at = datetime.now(timezone.utc) + SESSION_TTL

            try:
                connection.execute(
                    '''INSERT INTO calibration_operation
                           (id, session_id, roi_id, action, idempotency_key, request_hash,
                            status, payload, created_by)
                       VALUES (%s, NULL, %s, 'create', %s, %s, 'pending', %s, %s)''',
                    (operation_id, roi_id, key, request_hash, Jsonb(request), user['id']))

                version_row = connection.execute(
                    '''UPDATE roi
                          SET calibration_version=calibration_version+1, updated_at=NOW()
                        WHERE id=%s
                        RETURNING calibration_version''',
                    (roi_id,)).fetchone()
                calibration_version = version_row['calibration_version']

                session = connection.execute(
                    '''INSERT INTO roi_calibration_session
                           (roi_id, shelf_id, camera_node_id, branch_id,
                            product_id, product_sku, product_name,
                            context, context_hash, config_version,
                            calibration_version, descriptor_format, state,
                            session_version, created_by, reason, expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, 'draft', 1, %s, %s, %s)
                       RETURNING *''',
                    (roi_id, roi['shelf_id'], roi['camera_node_id'], branch_id,
                     roi['product_id'], (roi.get('sku') or '').strip().upper(),
                     roi.get('product_name') or '', Jsonb(context), context_hash,
                     roi['roi_applied_version'], calibration_version, FORMAT_VERSION,
                     user['id'], request.get('reason'), expires_at)).fetchone()
                connection.execute(
                    'UPDATE calibration_operation SET session_id=%s WHERE id=%s',
                    (session['id'], operation_id))
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                replay = connection.execute(
                    'SELECT * FROM calibration_operation WHERE idempotency_key=%s',
                    (key,)).fetchone()
                if replay and replay['request_hash'] == request_hash and replay.get('session_id'):
                    roi = load_roi_context(connection, branch_id, roi_id)
                    session = load_session(connection, roi_id, replay['session_id'])
                    response.headers['Cache-Control'] = 'no-store'
                    return detail_payload(connection, roi, session)
                raise CalibrationError(
                    409, 'busy',
                    'La cámara está ocupada con otra calibración. Espera e inténtalo de nuevo.') from None

            response.headers['Cache-Control'] = 'no-store'
            return serialize_session(
                session, [], [], active_pair(connection, roi))

    @router.get('/branches/{branch_id}/rois/{roi_id}/calibrations')
    def list_sessions(
        branch_id: UUID,
        roi_id: UUID,
        response: Response,
        limit: int = Query(default=LIST_LIMIT, ge=1, le=LIST_LIMIT_MAX),
        cursor: str | None = None,
        user=Depends(current_user),
    ):
        with get_connection() as connection:
            branch_context(connection, branch_id, user)
            roi = load_roi_context(connection, branch_id, roi_id)
            expire_stale_sessions(connection, roi_id=roi_id)

            query = 'SELECT * FROM roi_calibration_session WHERE roi_id=%s'
            params = [roi_id]
            position = decode_cursor(cursor)
            if position:
                query += ' AND (created_at, id) < (%s, %s)'
                params.extend(position)
            query += ' ORDER BY created_at DESC, id DESC LIMIT %s'
            params.append(limit + 1)

            rows = connection.execute(query, tuple(params)).fetchall()
            page = rows[:limit]
            next_cursor = None
            if len(rows) > limit and page:
                next_cursor = encode_cursor(page[-1]['created_at'], page[-1]['id'])
            context_valid = roi.get('calibration_status') == 'calibrated'
            items = [
                serialize_summary(row, context_valid and row['id'] == roi.get('active_calibration_id'))
                for row in page
            ]

        response.headers['Cache-Control'] = 'no-store'
        return {'items': items, 'next_cursor': next_cursor}

    @router.get('/branches/{branch_id}/rois/{roi_id}/calibrations/{calibration_id}')
    def get_session(
        branch_id: UUID,
        roi_id: UUID,
        calibration_id: UUID,
        response: Response,
        user=Depends(current_user),
    ):
        with get_connection() as connection:
            branch_context(connection, branch_id, user)
            roi = load_roi_context(connection, branch_id, roi_id)
            expire_stale_sessions(connection, roi_id=roi_id)
            session = load_session(connection, roi_id, calibration_id)
            result = detail_payload(connection, roi, session)

        response.headers['Cache-Control'] = 'no-store'
        return result

    @router.get('/branches/{branch_id}/snapshots/{snapshot_id}')
    def get_snapshot_asset(
        branch_id: UUID,
        snapshot_id: UUID,
        user=Depends(current_user),
    ):
        with get_connection() as connection:
            branch_context(connection, branch_id, user)
            row = connection.execute(
                '''SELECT data, content_type, captured_at
                     FROM snapshot
                    WHERE id=%s AND branch_id=%s''',
                (snapshot_id, branch_id)).fetchone()
        if not row:
            raise CalibrationError(404, 'snapshot_not_found', 'No encontramos esta imagen en la sucursal.')

        content = row['data'] or b''
        result = Response(
            content=content,
            media_type=row.get('content_type') or 'image/jpeg',
        )
        result.headers['Cache-Control'] = 'private, max-age=300'
        result.headers['X-Captured-At'] = rfc3339(row.get('captured_at')) or ''
        result.headers['ETag'] = '"' + hashlib.sha256(content).hexdigest()[:32] + '"'
        return result

    return router

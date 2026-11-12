"""Calibración de zonas con referencias vacía/abastecida — Fase 4.

E3.1 aportó el esquema y las operaciones de lectura/creación de sesión. E3.2
añade las mutaciones físicas (captura, confirmación y cancelación), el despacho
por WSS, la validación estricta de ACK, la activación/reconciliación y la
subida firmada de imágenes de referencia.

Reglas que este módulo garantiza:

* Una sola sesión abierta por cámara.
* La sesión congela SKU, geometría, nodo y ``config_version``; la huella
  canónica se calcula sobre ese contexto y nunca sobre datos mutables.
* La versión de calibración se reserva al crear y no se reutiliza.
* Los ``POST`` son idempotentes por ``Idempotency-Key``.
* Los errores llevan ``code`` y ``detail``; el cliente móvil ya los consume.
* Un timeout deja la operación en ``unknown``; nunca se simula un fallo ni un
  éxito. La activación solo ocurre con ACK válido y SQL confirmado.
* Las transacciones SQL se cierran antes de esperar a la red.

Contrato: ``docs/PLAN_FASE_4_CALIBRACION.md`` (Anexo A).
"""
import base64
import binascii
import hashlib
import json
import logging
import re
import time
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID, uuid4

from fastapi import (APIRouter, BackgroundTasks, Depends, Header, HTTPException,
                     Query, Request, Response)
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from .devices import require_branch

try:  # La firma Ed25519 solo se importa donde hace falta.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ModuleNotFoundError:  # pragma: no cover - el despliegue siempre la trae
    InvalidSignature = ValueError
    Ed25519PublicKey = None

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
SESSION_TTL = timedelta(minutes=30)
CAPTURE_TTL = timedelta(seconds=10)
LIST_LIMIT = 20
LIST_LIMIT_MAX = 50
DISPATCH_TIMEOUT = 15
STALE_MUTATION_SECONDS = 15
REFERENCE_MAX_BYTES = 2_000_000

OPEN_STATES = ('draft', 'empty_ready', 'pair_ready', 'committing', 'cancel_pending')
TERMINAL_STATES = ('active', 'cancelled', 'expired', 'failed')
PHYSICAL_ACTIONS = ('capture', 'commit')

# Estados de referencia que cuentan como “almacenada en el nodo”.
STORED_REFERENCE_STATES = ('stored', 'image_pending', 'ready')
CORRECTABLE_CODES = ('reference_too_similar', 'incomplete_pair', 'capture_too_dark',
                     'capture_blurry', 'invalid_frame')

HEX8 = re.compile(r'^[0-9a-fA-F]{8}$')
HEX64 = re.compile(r'^[0-9a-fA-F]{64}$')

# Evita lanzar varias reconciliaciones para la misma sesión en un mismo worker.
_reconcile_scheduled = {}

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


class CaptureBody(BaseModel):
    kind: str = Field(pattern='^(empty|full)$')
    capture_id: UUID
    session_version: int = Field(ge=1)


class CaptureReferenceBody(BaseModel):
    capture_id: UUID


class CommitReferencesBody(BaseModel):
    empty: CaptureReferenceBody
    full: CaptureReferenceBody


class CommitBody(BaseModel):
    session_version: int = Field(ge=1)
    references: CommitReferencesBody


class CancelBody(BaseModel):
    session_version: int = Field(ge=1)


class AckError(Exception):
    """ACK inválido, incompleto o de otra operación."""

    def __init__(self, code, detail=''):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


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


def reference_signature_message(gateway_id, timestamp, nonce, device_id, image_sha256):
    """Cadena firmada por el gateway; mismo estilo que el snapshot existente."""
    return (f'selfiq-calibration-reference-v1\\n{gateway_id}\\n{timestamp}\\n'
            f'{nonce}\\n{device_id}\\n{image_sha256}').encode()


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
# Contrato WSS/MQTT: comandos y validación estricta de ACK
# ---------------------------------------------------------------------------


def parse_timestamp(value):
    """Convierte una fecha RFC 3339 del nodo; ``None`` si no es válida."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_stale(value, seconds=STALE_MUTATION_SECONDS):
    if not isinstance(value, datetime):
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds() >= seconds


def descriptor_bytes(reference):
    """Bytes del descriptor guardado en ``reference_metrics.descriptor``.

    El descriptor no viaja por WSS (presupuesto de 1 KiB); se recibe en la
    subida firmada de la imagen. Si falta, se usa el CRC como respaldo para no
    romper referencias antiguas.
    """
    metrics = reference.get('reference_metrics') or {}
    encoded = metrics.get('descriptor') if isinstance(metrics, dict) else None
    if isinstance(encoded, str) and encoded:
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error):
            pass
    crc = reference.get('descriptor_crc') or ''
    return str(crc).encode()


def compute_pair_hash(empty_reference, full_reference):
    return hashlib.sha256(
        descriptor_bytes(empty_reference) + descriptor_bytes(full_reference)).hexdigest()


def build_command(operation, session, req_id, now=None):
    """Construye el objeto ``data`` del comando WSS de calibración."""
    now = now or datetime.now(timezone.utc)
    action = operation['action']
    payload = operation.get('payload') or {}
    base = {
        'schema_version': 1,
        'id': str(operation['id']),
        'req_id': str(req_id),
        'branch_id': str(session['branch_id']),
        'node_id': str(session['camera_node_id']),
        'calibration_id': str(session['id']),
        'calibration_version': int(session['calibration_version']),
        'roi_id': str(session['roi_id']),
    }
    if action == 'capture':
        base.update({
            'type': 'calibrate',
            'config_version': int(session['config_version']),
            'context_hash': session['context_hash'],
            'capture_id': str(payload['capture_id']),
            'kind': payload['kind'],
            'format_version': int(session.get('descriptor_format') or FORMAT_VERSION),
            'issued_at': rfc3339(now),
            'expires_at': rfc3339(now + CAPTURE_TTL),
        })
        return base
    if action == 'commit':
        base.update({
            'type': 'calibration.commit',
            'config_version': int(session['config_version']),
            'context_hash': session['context_hash'],
            'empty_capture_id': str(payload['references']['empty']['capture_id']),
            'full_capture_id': str(payload['references']['full']['capture_id']),
            'pair_hash': payload['pair_hash'],
            'format_version': int(session.get('descriptor_format') or FORMAT_VERSION),
            'issued_at': rfc3339(now),
            'expires_at': rfc3339(now + CAPTURE_TTL),
        })
        return base
    if action == 'cancel':
        base.update({
            'type': 'calibration.cancel',
            'context_hash': session['context_hash'],
            'issued_at': rfc3339(now),
        })
        return base
    if action == 'status':
        base.update({'type': 'calibration.status', 'issued_at': rfc3339(now)})
        return base
    raise ValueError(f'Acción de calibración desconocida: {action}')


def ack_error_code(result):
    error = (result or {}).get('error')
    if isinstance(error, dict):
        code = error.get('code')
        if isinstance(code, str) and code.strip():
            return code.strip()
        detail = error.get('detail')
        if isinstance(detail, str) and detail.strip():
            return 'node_rejected'
    if isinstance(error, str) and error.strip():
        text = error.strip().lower()
        for code in ('operation_conflict', 'invalid_ack', 'node_timeout',
                     'gateway_offline'):
            if text == code or text.startswith(code + ':') or text.startswith(code + ' '):
                return code
        for code in CORRECTABLE_CODES:
            if code in text:
                return code
        if 'timeout' in text or 'sin ack' in text or 'sin confirmación' in text:
            return 'node_timeout'
    return 'node_rejected'


def extract_ack(result):
    """Devuelve el ACK del nodo desde la respuesta WSS del gateway."""
    if not isinstance(result, dict):
        raise AckError('invalid_ack', 'La Raspberry no devolvió un ACK')
    if result.get('ok') is False:
        raise AckError(ack_error_code(result), 'El nodo rechazó la operación')
    data = result.get('data')
    if isinstance(data, dict):
        return data
    if 'action' in result:
        return result
    raise AckError('invalid_ack', 'El ACK no trae datos de la operación')


def _require(condition, detail):
    if not condition:
        raise AckError('invalid_ack', detail)


def _as_int(value, field, minimum=0):
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum,
             f'El ACK no trae {field} válido')
    return value


def _hex(value, pattern, field):
    _require(isinstance(value, str) and pattern.fullmatch(value), f'El ACK no trae {field} válido')
    return value.lower()


def _identity(ack, operation, session, req_id):
    _require(ack.get('schema_version') == 1, 'Versión de esquema desconocida')
    if 'id' in ack:
        _require(str(ack['id']) == str(operation['id']), 'El ACK pertenece a otra operación')
    if 'req_id' in ack and req_id:
        _require(str(ack['req_id']) == str(req_id), 'El ACK pertenece a otro intento')
    _require(str(ack.get('calibration_id')) == str(session['id']),
             'El ACK pertenece a otra calibración')
    _require(str(ack.get('roi_id')) == str(session['roi_id']),
             'El ACK pertenece a otra zona')
    _require(_as_int(ack.get('calibration_version'), 'calibration_version', 1)
             == int(session['calibration_version']),
             'La versión de calibración no coincide')
    if 'config_version' in ack:
        _require(_as_int(ack.get('config_version'), 'config_version')
                 == int(session['config_version']), 'La configuración no coincide')
    if 'context_hash' in ack:
        _hex(ack.get('context_hash'), HEX64, 'context_hash')
        _require(ack['context_hash'].lower() == session['context_hash'].lower(),
                 'La huella del contexto no coincide')


def validate_ack(operation, session, ack, req_id=None):
    """Valida un ACK de captura/commit/cancel. Lanza ``AckError``.

    Un ACK incompleto nunca se completa con lo solicitado: si falta un campo
    obligatorio, es inválido.
    """
    _identity(ack, operation, session, req_id)
    action = operation['action']
    _require(ack.get('action') == action, 'El ACK corresponde a otra acción')
    payload = operation.get('payload') or {}
    if action in ('capture', 'commit'):
        _require('config_version' in ack and 'context_hash' in ack,
                 'El ACK no identifica la configuración de la zona')

    if action == 'capture':
        _require(str(ack.get('capture_id')) == str(payload.get('capture_id')),
                 'El ACK corresponde a otra captura')
        _require(ack.get('kind') == payload.get('kind'), 'El ACK corresponde a otra referencia')
        _require(_as_int(ack.get('format_version'), 'format_version', 1)
                 == int(session.get('descriptor_format') or FORMAT_VERSION),
                 'El formato del descriptor no coincide')
        status = ack.get('status')
        _require(status in ('stored', 'failed', 'unknown'), 'Estado de captura desconocido')
        if status == 'stored':
            _require(ack.get('persisted') is True, 'El nodo no confirmó la persistencia')
            _hex(ack.get('descriptor_crc'), HEX8, 'descriptor_crc')
            _hex(ack.get('image_sha256'), HEX64, 'image_sha256')
            _as_int(ack.get('image_bytes'), 'image_bytes', 1)
            _as_int(ack.get('width'), 'width', 1)
            _as_int(ack.get('height'), 'height', 1)
            _require(isinstance(ack.get('quality'), dict), 'El ACK no trae calidad')
            _require(isinstance(ack.get('clock_quality'), str) and ack['clock_quality'],
                     'El ACK no trae clock_quality')
            _as_int(ack.get('uptime_ms'), 'uptime_ms', 0)
        return ack

    if action == 'commit':
        _require(str(ack.get('empty_capture_id')) == str(
            payload['references']['empty']['capture_id']), 'Cambió la captura vacía')
        _require(str(ack.get('full_capture_id')) == str(
            payload['references']['full']['capture_id']), 'Cambió la captura abastecida')
        status = ack.get('status')
        _require(status in ('applied', 'rejected', 'failed'), 'Estado de confirmación desconocido')
        if status == 'applied':
            _hex(ack.get('pair_hash'), HEX64, 'pair_hash')
            _require(ack['pair_hash'].lower() == str(payload['pair_hash']).lower(),
                     'La huella de la pareja no coincide')
            _require(ack.get('persisted') is True, 'El nodo no confirmó la persistencia')
            _require(_as_int(ack.get('active_calibration_version'),
                             'active_calibration_version', 1)
                     == int(session['calibration_version']),
                     'El nodo activó otra versión')
        return ack

    if action == 'cancel':
        _require(ack.get('status') in ('cancelled', 'failed'), 'Estado de cancelación desconocido')
        return ack

    raise AckError('invalid_ack', 'Acción de ACK inesperada')


def _validate_status_entry(value, field, *, draft=False):
    if value is None:
        return
    _require(isinstance(value, dict), f'El ACK no trae {field} válido')
    _require(_as_int(value.get('calibration_version'), f'{field}.calibration_version', 1) >= 1,
             f'El ACK no trae {field}.calibration_version válido')
    if draft:
        _require(isinstance(value.get('calibration_id'), str) and value['calibration_id'],
                 f'El ACK no trae {field}.calibration_id')
    else:
        _hex(value.get('context_hash'), HEX64, f'{field}.context_hash')
    _hex(value.get('empty_crc'), HEX8, f'{field}.empty_crc')
    _hex(value.get('full_crc'), HEX8, f'{field}.full_crc')


def validate_status_ack(session, ack):
    """Valida el ACK de ``calibration.status`` (reconciliación)."""
    _require(ack.get('schema_version') == 1, 'Versión de esquema desconocida')
    _require(str(ack.get('calibration_id')) == str(session['id']),
             'El estado pertenece a otra calibración')
    _require(str(ack.get('roi_id')) == str(session['roi_id']),
             'El estado pertenece a otra zona')
    _require(ack.get('action') == 'status', 'El ACK corresponde a otra acción')
    _require(ack.get('status') == 'reported', 'Estado de consulta desconocido')
    _validate_status_entry(ack.get('active'), 'active')
    _validate_status_entry(ack.get('draft'), 'draft', draft=True)
    return ack


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


def expire_stale_captures(connection, session, seconds=STALE_MUTATION_SECONDS):
    """Recupera capturas que quedaron ``pending``/``sent`` tras un reinicio.

    No inventa éxito ni fallo definitivo: la referencia y la operación pasan a
    ``unknown`` para que el asistente pueda repetir la captura.
    """
    if session['state'] not in ('draft', 'empty_ready', 'pair_ready'):
        return False
    rows = connection.execute(
        '''SELECT * FROM calibration_operation
            WHERE session_id=%s AND action='capture'
              AND status IN ('pending','sent') AND updated_at < NOW() - %s''',
        (session['id'], timedelta(seconds=seconds))).fetchall()
    for operation in rows:
        mark_operation(connection, operation['id'], 'unknown', code='node_timeout')
        reference = load_reference(
            connection, session['id'],
            (operation.get('payload') or {}).get('kind'), lock=True)
        if reference and reference.get('status') == 'pending':
            connection.execute(
                "UPDATE roi_calibration SET status='unknown' WHERE id=%s",
                (reference['id'],))
    return bool(rows)


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


def load_operation_by_key(connection, key):
    return connection.execute(
        'SELECT * FROM calibration_operation WHERE idempotency_key=%s', (key,)).fetchone()


def load_operation(connection, operation_id, *, lock=False):
    query = 'SELECT * FROM calibration_operation WHERE id=%s'
    if lock:
        query += ' FOR UPDATE'
    return connection.execute(query, (operation_id,)).fetchone()


def insert_operation(connection, *, session, roi_id, action, key, request_hash,
                     payload, user_id):
    return connection.execute(
        '''INSERT INTO calibration_operation
               (session_id, roi_id, action, idempotency_key, request_hash,
                status, payload, created_by)
           VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)
           RETURNING *''',
        (session['id'], roi_id, action, key, request_hash, Jsonb(payload), user_id)).fetchone()


def mark_operation(connection, operation_id, status, *, code=None, detail=None, result=None):
    connection.execute(
        '''UPDATE calibration_operation
              SET status=%s, code=%s, detail=%s,
                  result=COALESCE(%s, result), updated_at=NOW()
            WHERE id=%s''',
        (status, code, detail, Jsonb(result) if result is not None else None, operation_id))


def bump_session(connection, session_id, state=None, *, touch_expiry=True,
                 confirmed=False, closed=False, error_code=None):
    sets = ['session_version=session_version+1', 'updated_at=NOW()']
    params = []
    if state is not None:
        sets.append('state=%s')
        params.append(state)
    if touch_expiry:
        sets.append('expires_at=NOW() + %s')
        params.append(SESSION_TTL)
    if confirmed:
        sets.append('confirmed_at=NOW()')
    if closed:
        sets.append('closed_at=NOW()')
    if error_code is not None:
        sets.append('error_code=%s')
        params.append(error_code)
    params.append(session_id)
    return connection.execute(
        f'UPDATE roi_calibration_session SET {", ".join(sets)} WHERE id=%s RETURNING *',
        tuple(params)).fetchone()


def load_reference(connection, session_id, kind, *, lock=False):
    query = '''SELECT * FROM roi_calibration
                WHERE session_id=%s AND reference_type=%s
                ORDER BY received_at DESC NULLS LAST, id DESC LIMIT 1'''
    if lock:
        query += ' FOR UPDATE'
    return connection.execute(query, (session_id, kind)).fetchone()


def prepare_reference(connection, session, kind, capture_id, user_id, context_hash):
    """Crea o sustituye el borrador de una referencia conservando la otra."""
    existing = connection.execute(
        '''SELECT id FROM roi_calibration
            WHERE session_id=%s AND reference_type=%s
            ORDER BY received_at DESC NULLS LAST, id DESC LIMIT 1''',
        (session['id'], kind)).fetchone()
    if existing:
        return connection.execute(
            '''UPDATE roi_calibration
                  SET capture_id=%s, status='pending', descriptor_crc=NULL,
                      descriptor_format=%s, pair_hash=NULL, context_hash=%s,
                      image_id=NULL, image_sha256=NULL, image_bytes=NULL,
                      width=NULL, height=NULL, quality=NULL, clock_quality=NULL,
                      uptime_ms=NULL, captured_at=NULL, received_at=NULL,
                      reference_metrics='{}'::jsonb
                WHERE id=%s RETURNING *''',
            (capture_id, session.get('descriptor_format') or FORMAT_VERSION,
             context_hash, existing['id'])).fetchone()
    return connection.execute(
        '''INSERT INTO roi_calibration
               (roi_id, reference_type, reference_metrics, captured_by,
                captured_at, is_current, session_id, capture_id, status,
                descriptor_format, context_hash)
           VALUES (%s, %s, '{}'::jsonb, %s, NULL, false, %s, %s, 'pending',
                   %s, %s)
           RETURNING *''',
        (session['roi_id'], kind, user_id, session['id'], capture_id,
         session.get('descriptor_format') or FORMAT_VERSION,
         context_hash)).fetchone()


def state_from_references(connection, session, current=None):
    """Estado coherente con las referencias (solo mientras está abierta)."""
    current = current or session['state']
    if current not in ('draft', 'empty_ready', 'pair_ready'):
        return current
    rows = load_references(connection, session['id'])
    by_kind = {}
    for row in rows:
        by_kind[row['reference_type']] = row
    empty = by_kind.get('empty')
    full = by_kind.get('full')
    if empty and full and empty.get('status') == 'ready' and full.get('status') == 'ready':
        return 'pair_ready'
    if empty and empty.get('status') in STORED_REFERENCE_STATES:
        return 'empty_ready'
    return 'draft'


def next_state_after_capture(current, kind, status):
    if status not in STORED_REFERENCE_STATES:
        return current
    if current == 'draft' and kind == 'empty':
        return 'empty_ready'
    if current == 'empty_ready' and kind == 'full':
        return 'pair_ready'
    return current


def activate_session(connection, session):
    """Activa la pareja: una sola referencia actual por tipo y ROI."""
    connection.execute(
        '''UPDATE roi_calibration SET is_current=false, superseded_at=NOW()
            WHERE roi_id=%s AND is_current''', (session['roi_id'],))
    connection.execute(
        '''UPDATE roi_calibration SET is_current=true
            WHERE session_id=%s AND reference_type IN ('empty','full')
              AND status='ready' ''', (session['id'],))
    connection.execute(
        '''UPDATE roi
              SET calibration_status='calibrated',
                  active_calibration_id=%s,
                  calibration_context_hash=%s,
                  calibration_invalidated_reason=NULL,
                  last_calibrated_at=NOW(),
                  updated_at=NOW()
            WHERE id=%s''',
        (session['id'], session['context_hash'], session['roi_id']))
    return bump_session(connection, session['id'], 'active',
                        touch_expiry=False, confirmed=True, closed=True)


def apply_capture_ack(connection, operation, session, ack):
    reference = load_reference(connection, session['id'],
                               (operation.get('payload') or {}).get('kind'), lock=True)
    if not reference:
        raise AckError('invalid_ack', 'No existe el borrador de esa referencia')
    raw_status = ack.get('status')
    if raw_status == 'stored':
        # Si la imagen ya llegó antes que el ACK, no se degrada a image_pending.
        if reference.get('status') == 'ready':
            status = 'ready'
        else:
            status = 'image_pending' if ack.get('image_sha256') else 'stored'
    elif raw_status in ('failed', 'unknown'):
        status = raw_status
    else:
        raise AckError('invalid_ack', 'Estado de captura desconocido')

    metrics = dict(reference.get('reference_metrics') or {})
    if ack.get('descriptor'):
        try:
            decoded = base64.b64decode(ack['descriptor'], validate=True)
            if HEX8.fullmatch(ack.get('descriptor_crc') or '') and \
                    f'{zlib.crc32(decoded) & 0xffffffff:08x}' != ack['descriptor_crc'].lower():
                raise AckError('invalid_ack', 'El descriptor no corresponde al CRC')
            metrics['descriptor'] = ack['descriptor']
        except (ValueError, TypeError, binascii.Error):
            raise AckError('invalid_ack', 'El descriptor no es base64 válido') from None

    connection.execute(
        '''UPDATE roi_calibration
              SET status=%s, captured_at=NOW(), received_at=NOW(),
                  descriptor_crc=%s, descriptor_format=%s, quality=%s,
                  clock_quality=%s, uptime_ms=%s, image_sha256=%s,
                  image_bytes=%s, width=%s, height=%s,
                  reference_metrics=%s
            WHERE id=%s''',
        (status, ack.get('descriptor_crc'), ack.get('format_version'),
         Jsonb(ack.get('quality') or {}), ack.get('clock_quality'),
         ack.get('uptime_ms'), ack.get('image_sha256'), ack.get('image_bytes'),
         ack.get('width'), ack.get('height'), Jsonb(metrics), reference['id']))

    state = next_state_after_capture(session['state'], reference['reference_type'], status)
    session = bump_session(connection, session['id'], state)
    if raw_status == 'stored':
        mark_operation(connection, operation['id'], 'applied',
                       result={'capture_id': str(reference['capture_id']),
                               'kind': reference['reference_type'],
                               'status': status})
    else:
        mark_operation(connection, operation['id'],
                       'rejected' if raw_status == 'failed' else 'unknown',
                       code=ack.get('code') or ('capture_failed' if raw_status == 'failed' else None))
    return session


def apply_commit_ack(connection, operation, session, ack):
    if session['state'] != 'committing':
        raise AckError('invalid_ack', 'La sesión no está confirmando')
    if ack.get('status') == 'applied':
        session = activate_session(connection, session)
        mark_operation(connection, operation['id'], 'applied',
                       result={'calibration_version': int(session['calibration_version']),
                               'status': 'active'})
    else:
        code = ack.get('code') or ('reference_too_similar'
                                   if ack.get('status') == 'rejected' else 'node_rejected')
        state = 'pair_ready' if code in CORRECTABLE_CODES else 'failed'
        session = bump_session(connection, session['id'], state, touch_expiry=state != 'failed',
                               closed=state == 'failed', error_code=code)
        mark_operation(connection, operation['id'], 'rejected', code=code)
    return session


def apply_cancel_ack(connection, operation, session, ack):
    if ack.get('status') == 'cancelled':
        session = bump_session(connection, session['id'], 'cancelled',
                               touch_expiry=False, closed=True)
        mark_operation(connection, operation['id'], 'applied',
                       result={'status': 'cancelled'})
    else:
        code = ack.get('code') or 'cancel_failed'
        mark_operation(connection, operation['id'], 'rejected', code=code)
    return session


def apply_dispatch_failure(get_connection, operation_id, code, *, unknown):
    """Registra un fallo de transporte sin inventar confirmaciones."""
    with get_connection() as connection:
        operation = load_operation(connection, operation_id, lock=True)
        if not operation or operation['status'] in ('applied', 'rejected', 'failed'):
            return
        session = load_session(connection, operation['roi_id'], operation['session_id'], lock=True)
        status = 'unknown' if unknown else 'failed'
        mark_operation(connection, operation_id, status, code=code)
        if operation['action'] == 'capture':
            kind = (operation.get('payload') or {}).get('kind')
            reference = load_reference(connection, session['id'], kind, lock=True)
            if reference:
                connection.execute(
                    'UPDATE roi_calibration SET status=%s WHERE id=%s',
                    ('unknown' if unknown else 'failed', reference['id']))
            bump_session(connection, session['id'], session['state'])
        elif operation['action'] == 'commit' and not unknown:
            state = state_from_references(connection, session, 'pair_ready')
            bump_session(connection, session['id'], state, error_code=code)
        elif operation['action'] == 'cancel' and not unknown:
            state = state_from_references(connection, session)
            bump_session(connection, session['id'], state, error_code=code)
        connection.commit()


def apply_status_report(get_connection, session_id, ack):
    """Aplica ``calibration.status`` y resuelve ``committing``/``cancel_pending``."""
    with get_connection() as connection:
        session = connection.execute(
            'SELECT * FROM roi_calibration_session WHERE id=%s FOR UPDATE',
            (session_id,)).fetchone()
        if not session or session['state'] not in ('committing', 'cancel_pending'):
            return
        operation = connection.execute(
            '''SELECT * FROM calibration_operation
                WHERE session_id=%s AND action IN ('commit','cancel')
                ORDER BY created_at DESC, id DESC LIMIT 1''',
            (session_id,)).fetchone()
        active = ack.get('active')
        draft = ack.get('draft')
        applied = bool(
            active
            and int(active['calibration_version']) == int(session['calibration_version'])
            and str(active['context_hash']).lower() == session['context_hash'].lower()
        )
        if applied:
            session = activate_session(connection, session)
            if operation:
                mark_operation(connection, operation['id'], 'applied',
                               result={'calibration_version': int(session['calibration_version']),
                                       'status': 'active', 'reconciled': True})
        elif session['state'] == 'committing':
            draft_matches = bool(
                draft
                and str(draft.get('calibration_id')) == str(session['id'])
                and int(draft.get('calibration_version')) == int(session['calibration_version'])
            )
            if not draft_matches:
                session = bump_session(connection, session['id'], 'failed',
                                       touch_expiry=False, closed=True,
                                       error_code='not_applied')
                if operation:
                    mark_operation(connection, operation['id'], 'failed', code='not_applied')
            else:
                state = state_from_references(connection, session, 'pair_ready')
                session = bump_session(connection, session['id'], state, error_code='not_applied')
                if operation:
                    mark_operation(connection, operation['id'], 'failed', code='not_applied')
        else:  # cancel_pending
            if not draft or str(draft.get('calibration_id')) != str(session['id']):
                session = bump_session(connection, session['id'], 'cancelled',
                                       touch_expiry=False, closed=True)
                if operation:
                    mark_operation(connection, operation['id'], 'applied',
                                   result={'status': 'cancelled', 'reconciled': True})
            elif operation:
                mark_operation(connection, operation['id'], 'failed',
                               code='cancel_not_confirmed')
        connection.commit()


async def dispatch_operation(get_connection, hub, operation_id, timeout=DISPATCH_TIMEOUT):
    """Envía una operación durable por WSS y aplica el ACK.

    La transacción SQL se cierra antes de esperar a la Raspberry. Un timeout
    deja la operación en ``unknown``.
    """
    if hub is None:
        return
    target_name = None
    command = None
    payload = None
    session = None
    operation = None
    with get_connection() as connection:
        operation = load_operation(connection, operation_id)
        if not operation or operation['status'] not in ('pending', 'sent'):
            return
        session = connection.execute(
            'SELECT * FROM roi_calibration_session WHERE id=%s',
            (operation['session_id'],)).fetchone()
        if not session:
            return
        target = connection.execute(
            '''SELECT g.rpi_identifier
                 FROM esp32_node n JOIN gateway g ON g.id=n.gateway_id
                  AND g.is_active
                WHERE n.id=%s''', (session['camera_node_id'],)).fetchone()
        if target and target.get('rpi_identifier'):
            target_name = target['rpi_identifier']
            req_id = str(uuid4())
            command = build_command(operation, session, req_id)
            payload = dict(operation.get('payload') or {})
            payload['req_id'] = req_id
            connection.execute(
                '''UPDATE calibration_operation
                      SET status='sent', attempts=attempts+1, payload=%s, updated_at=NOW()
                    WHERE id=%s''', (Jsonb(payload), operation_id))
            connection.commit()
        else:
            connection.rollback()

    if not target_name:
        apply_dispatch_failure(get_connection, operation_id, 'gateway_offline', unknown=False)
        return

    try:
        result = await hub.send(target_name, command['type'],
                                payload={'data': command}, timeout=timeout)
    except HTTPException as exc:
        detail = str(exc.detail or '')
        not_sent = 'no se encola' in detail or 'no pudo enviarse' in detail
        if exc.status_code == 504:
            apply_dispatch_failure(get_connection, operation_id, 'node_timeout', unknown=True)
        elif exc.status_code == 503 and not_sent:
            apply_dispatch_failure(get_connection, operation_id, 'gateway_offline', unknown=False)
        elif exc.status_code == 429:
            apply_dispatch_failure(get_connection, operation_id, 'busy', unknown=False)
        else:
            apply_dispatch_failure(get_connection, operation_id, 'node_timeout', unknown=True)
        return
    except (RuntimeError, OSError):
        apply_dispatch_failure(get_connection, operation_id, 'node_timeout', unknown=True)
        return

    try:
        ack = extract_ack(result)
        operation = {**operation, 'payload': payload}
        validate_ack(operation, session, ack, req_id=req_id)
    except AckError as exc:
        if exc.code in ('invalid_ack', 'node_timeout', 'operation_conflict'):
            # Sin ACK válido no hay evidencia de rechazo ni de éxito.
            apply_dispatch_failure(get_connection, operation_id, exc.code, unknown=True)
        elif exc.code == 'gateway_offline':
            apply_dispatch_failure(get_connection, operation_id, exc.code, unknown=False)
        else:
            # Rechazo explícito del nodo: sí es definitivo y no se activa nada.
            with get_connection() as connection:
                stored = load_operation(connection, operation_id, lock=True)
                if not stored or stored['status'] in ('applied', 'rejected', 'failed'):
                    return
                session = load_session(connection, stored['roi_id'], stored['session_id'], lock=True)
                if stored['action'] == 'commit':
                    state = 'pair_ready' if exc.code in CORRECTABLE_CODES else 'failed'
                    session = bump_session(connection, session['id'], state,
                                           touch_expiry=state != 'failed',
                                           closed=state == 'failed', error_code=exc.code)
                elif stored['action'] == 'capture':
                    kind = (stored.get('payload') or {}).get('kind')
                    reference = load_reference(connection, session['id'], kind, lock=True)
                    if reference:
                        connection.execute(
                            "UPDATE roi_calibration SET status='failed' WHERE id=%s",
                            (reference['id'],))
                    session = bump_session(connection, session['id'], session['state'])
                mark_operation(connection, operation_id, 'rejected', code=exc.code)
                connection.commit()
        return

    with get_connection() as connection:
        stored = load_operation(connection, operation_id, lock=True)
        if not stored or stored['status'] in ('applied', 'rejected', 'failed'):
            return
        session = load_session(connection, stored['roi_id'], stored['session_id'], lock=True)
        try:
            if stored['action'] == 'capture':
                apply_capture_ack(connection, stored, session, ack)
            elif stored['action'] == 'commit':
                apply_commit_ack(connection, stored, session, ack)
            elif stored['action'] == 'cancel':
                apply_cancel_ack(connection, stored, session, ack)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


async def reconcile_session(get_connection, hub, session_id, timeout=DISPATCH_TIMEOUT):
    """Consulta ``calibration.status`` una vez para resolver un estado incierto."""
    if hub is None:
        return
    with get_connection() as connection:
        session = connection.execute(
            'SELECT * FROM roi_calibration_session WHERE id=%s', (session_id,)).fetchone()
        if not session or session['state'] not in ('committing', 'cancel_pending'):
            return
        target = connection.execute(
            '''SELECT g.rpi_identifier
                 FROM esp32_node n JOIN gateway g ON g.id=n.gateway_id
                  AND g.is_active
                WHERE n.id=%s''', (session['camera_node_id'],)).fetchone()
    if not target or not target.get('rpi_identifier'):
        return
    req_id = str(uuid4())
    command = {
        'schema_version': 1,
        'type': 'calibration.status',
        'id': req_id,
        'req_id': req_id,
        'branch_id': str(session['branch_id']),
        'node_id': str(session['camera_node_id']),
        'calibration_id': str(session['id']),
        'calibration_version': int(session['calibration_version']),
        'roi_id': str(session['roi_id']),
        'context_hash': session['context_hash'],
        'issued_at': rfc3339(datetime.now(timezone.utc)),
    }
    try:
        result = await hub.send(target['rpi_identifier'], 'calibration.status',
                                payload={'data': command}, timeout=timeout)
        ack = extract_ack(result)
        validate_status_ack(session, ack)
    except (HTTPException, AckError):
        log.warning('No se pudo reconciliar la calibración %s', session_id, exc_info=True)
        return
    apply_status_report(get_connection, session_id, ack)


def schedule_reconcile(background, get_connection, hub, session):
    """Programa una reconciliación como mucho una vez por sesión/worker."""
    if background is None or hub is None or not session:
        return
    now = time.monotonic()
    for identifier, stamp in list(_reconcile_scheduled.items()):
        if now - stamp > 60:
            _reconcile_scheduled.pop(identifier, None)
    if str(session['id']) in _reconcile_scheduled:
        return
    if session['state'] not in ('committing', 'cancel_pending'):
        return
    if not is_stale(session.get('updated_at')):
        return
    _reconcile_scheduled[str(session['id'])] = now
    background.add_task(reconcile_session, get_connection, hub, session['id'])


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
                    '''UPDATE calibration_operation
                          SET session_id=%s, status='applied', updated_at=NOW()
                        WHERE id=%s''',
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

    def previous_operation(connection, key, action, request_hash):
        previous = load_operation_by_key(connection, key)
        if not previous:
            return None
        if previous['action'] != action or previous['request_hash'] != request_hash:
            raise CalibrationError(
                409, 'idempotency_conflict',
                'Se repitió la solicitud con datos distintos. Abre la zona de nuevo.')
        return previous

    def accepted_payload(operation, fallback):
        result = operation.get('result') if operation else None
        return result if isinstance(result, dict) and result else fallback

    def require_open_session(connection, branch_id, roi_id, calibration_id, user):
        branch_context(connection, branch_id, user)
        roi = load_roi_context(connection, branch_id, roi_id, lock=True)
        expire_stale_sessions(connection, roi_id=roi_id)
        session = load_session(connection, roi_id, calibration_id, lock=True)
        return roi, session

    def ensure_context(roi, session):
        if compute_context_hash(build_context(roi)) != session['context_hash']:
            raise CalibrationError(
                409, 'context_mismatch',
                'El producto o la zona cambiaron. Vuelve a cargar la zona y calibra de nuevo.')

    def ensure_no_physical_operation(connection, session_id):
        busy = connection.execute(
            '''SELECT id FROM calibration_operation
                WHERE session_id=%s AND action IN ('capture','commit')
                  AND status IN ('pending','sent') LIMIT 1''',
            (session_id,)).fetchone()
        if busy:
            raise CalibrationError(
                409, 'busy',
                'La cámara está ocupada con otra operación. Espera a que termine.')

    @router.post(
        '/branches/{branch_id}/rois/{roi_id}/calibrations/{calibration_id}/capture',
        status_code=202)
    async def capture_reference(
        branch_id: UUID,
        roi_id: UUID,
        calibration_id: UUID,
        body: CaptureBody,
        response: Response,
        background: BackgroundTasks,
        idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
        user=Depends(current_user),
    ):
        key = require_idempotency_key(idempotency_key)
        request = body.model_dump(mode='json')
        request_hash = hash_json(request)

        with get_connection() as connection:
            roi, session = require_open_session(
                connection, branch_id, roi_id, calibration_id, user)
            previous = previous_operation(connection, key, 'capture', request_hash)
            if previous:
                response.headers['Cache-Control'] = 'no-store'
                return accepted_payload(previous, {
                    'operation_id': str(previous['id']),
                    'session_version': session['session_version'],
                    'state': session['state'],
                })

            if session['state'] in TERMINAL_STATES or session['state'] in (
                    'committing', 'cancel_pending'):
                raise CalibrationError(
                    409, 'invalid_state',
                    'La calibración ya cambió de paso. Vuelve a abrir la zona para continuar.')
            if int(body.session_version) != int(session['session_version']):
                raise CalibrationError(
                    409, 'session_conflict', 'Otra persona cambió esta calibración. Vuelve a cargar la zona.')
            if body.kind == 'full' and session['state'] == 'draft':
                raise CalibrationError(
                    409, 'invalid_state', 'Captura primero la zona vacía.')
            if body.kind == 'empty' and session['state'] not in (
                    'draft', 'empty_ready', 'pair_ready'):
                raise CalibrationError(409, 'invalid_state', 'No se puede capturar la zona vacía ahora.')
            if body.kind == 'full' and session['state'] not in ('empty_ready', 'pair_ready'):
                raise CalibrationError(409, 'invalid_state', 'No se puede capturar la zona abastecida ahora.')
            ensure_context(roi, session)
            if not roi.get('rpi_identifier'):
                raise CalibrationError(
                    503, 'camera_unavailable',
                    'No pudimos comunicarnos con la cámara. Verifica que esté encendida.')
            ensure_no_physical_operation(connection, session['id'])

            prepare_reference(connection, session, body.kind, body.capture_id,
                              user['id'], session['context_hash'])
            operation = insert_operation(
                connection, session=session, roi_id=roi_id, action='capture',
                key=key, request_hash=request_hash, payload=request, user_id=user['id'])
            session = bump_session(connection, session['id'], session['state'])
            result = {
                'operation_id': str(operation['id']),
                'session_version': session['session_version'],
                'state': session['state'],
                'capture': {
                    'capture_id': str(body.capture_id),
                    'kind': body.kind,
                    'status': 'pending',
                },
                'expires_at': rfc3339(session.get('expires_at')),
            }
            connection.execute(
                'UPDATE calibration_operation SET result=%s WHERE id=%s',
                (Jsonb(result), operation['id']))
            connection.commit()

        response.headers['Cache-Control'] = 'no-store'
        background.add_task(dispatch_operation, get_connection, hub, operation['id'])
        return result

    @router.post(
        '/branches/{branch_id}/rois/{roi_id}/calibrations/{calibration_id}/commit',
        status_code=202)
    async def commit_calibration(
        branch_id: UUID,
        roi_id: UUID,
        calibration_id: UUID,
        body: CommitBody,
        response: Response,
        background: BackgroundTasks,
        idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
        user=Depends(current_user),
    ):
        key = require_idempotency_key(idempotency_key)
        request = body.model_dump(mode='json')
        request_hash = hash_json(request)

        with get_connection() as connection:
            roi, session = require_open_session(
                connection, branch_id, roi_id, calibration_id, user)
            previous = previous_operation(connection, key, 'commit', request_hash)
            if previous:
                response.headers['Cache-Control'] = 'no-store'
                return accepted_payload(previous, {
                    'operation_id': str(previous['id']),
                    'session_version': session['session_version'],
                    'state': session['state'],
                })

            if session['state'] in TERMINAL_STATES or session['state'] in (
                    'committing', 'cancel_pending'):
                raise CalibrationError(
                    409, 'invalid_state',
                    'La calibración ya cambió de paso. Vuelve a abrir la zona para continuar.')
            if int(body.session_version) != int(session['session_version']):
                raise CalibrationError(
                    409, 'session_conflict', 'Otra persona cambió esta calibración. Vuelve a cargar la zona.')
            if session['state'] in ('draft', 'empty_ready'):
                raise CalibrationError(
                    409, 'incomplete_pair',
                    'Faltan las dos referencias o una todavía no está lista.')
            ensure_context(roi, session)
            ensure_no_physical_operation(connection, session['id'])

            references = {row['reference_type']: row
                          for row in load_references(connection, session['id'])}
            empty = references.get('empty')
            full = references.get('full')
            requested_empty = str(body.references.empty.capture_id)
            requested_full = str(body.references.full.capture_id)
            if (not empty or not full
                    or empty.get('status') != 'ready' or full.get('status') != 'ready'
                    or str(empty.get('capture_id')) != requested_empty
                    or str(full.get('capture_id')) != requested_full
                    or requested_empty == requested_full):
                raise CalibrationError(
                    409, 'incomplete_pair',
                    'Faltan las dos referencias o una todavía no está lista.')

            empty_bytes = descriptor_bytes(empty)
            full_bytes = descriptor_bytes(full)
            if empty_bytes and empty_bytes == full_bytes:
                raise CalibrationError(
                    409, 'reference_too_similar',
                    'Las dos capturas se parecen demasiado. Vacía o abastece la zona de forma más clara y repite.')
            pair_hash = hashlib.sha256(empty_bytes + full_bytes).hexdigest()

            payload = dict(request)
            payload['pair_hash'] = pair_hash
            operation = insert_operation(
                connection, session=session, roi_id=roi_id, action='commit',
                key=key, request_hash=request_hash, payload=payload, user_id=user['id'])
            session = bump_session(connection, session['id'], 'committing')
            result = {
                'operation_id': str(operation['id']),
                'session_version': session['session_version'],
                'state': session['state'],
            }
            connection.execute(
                'UPDATE calibration_operation SET result=%s WHERE id=%s',
                (Jsonb(result), operation['id']))
            connection.commit()

        response.headers['Cache-Control'] = 'no-store'
        background.add_task(dispatch_operation, get_connection, hub, operation['id'])
        return result

    @router.post(
        '/branches/{branch_id}/rois/{roi_id}/calibrations/{calibration_id}/cancel',
        status_code=202)
    async def cancel_calibration(
        branch_id: UUID,
        roi_id: UUID,
        calibration_id: UUID,
        body: CancelBody,
        response: Response,
        background: BackgroundTasks,
        idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
        user=Depends(current_user),
    ):
        key = require_idempotency_key(idempotency_key)
        request = body.model_dump(mode='json')
        request_hash = hash_json(request)

        with get_connection() as connection:
            roi, session = require_open_session(
                connection, branch_id, roi_id, calibration_id, user)
            previous = previous_operation(connection, key, 'cancel', request_hash)
            if previous:
                response.headers['Cache-Control'] = 'no-store'
                return accepted_payload(previous, {
                    'operation_id': str(previous['id']),
                    'session_version': session['session_version'],
                    'state': session['state'],
                })

            if session['state'] in TERMINAL_STATES:
                response.headers['Cache-Control'] = 'no-store'
                response.status_code = 200
                return {
                    'operation_id': None,
                    'session_version': session['session_version'],
                    'state': session['state'],
                }
            if session['state'] == 'committing':
                raise CalibrationError(
                    409, 'invalid_state',
                    'La cámara ya está confirmando esta calibración. Consulta el estado antes de cancelar.')
            if int(body.session_version) != int(session['session_version']):
                raise CalibrationError(
                    409, 'session_conflict', 'Otra persona cambió esta calibración. Vuelve a cargar la zona.')
            ensure_no_physical_operation(connection, session['id'])

            operation = insert_operation(
                connection, session=session, roi_id=roi_id, action='cancel',
                key=key, request_hash=request_hash, payload=request, user_id=user['id'])
            session = bump_session(connection, session['id'], 'cancel_pending')
            result = {
                'operation_id': str(operation['id']),
                'session_version': session['session_version'],
                'state': session['state'],
            }
            connection.execute(
                'UPDATE calibration_operation SET result=%s WHERE id=%s',
                (Jsonb(result), operation['id']))
            connection.commit()

        response.headers['Cache-Control'] = 'no-store'
        background.add_task(dispatch_operation, get_connection, hub, operation['id'])
        return result

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
        background: BackgroundTasks,
        response: Response,
        user=Depends(current_user),
    ):
        with get_connection() as connection:
            branch_context(connection, branch_id, user)
            roi = load_roi_context(connection, branch_id, roi_id)
            expire_stale_sessions(connection, roi_id=roi_id)
            session = load_session(connection, roi_id, calibration_id)
            if expire_stale_captures(connection, session):
                connection.commit()
                session = load_session(connection, roi_id, calibration_id)
            result = detail_payload(connection, roi, session)

        schedule_reconcile(background, get_connection, hub, session)
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

    @router.post('/gateways/{gateway_id}/devices/{device_id}/calibration-references')
    async def upload_calibration_reference(
        gateway_id: str,
        device_id: UUID,
        request: Request,
        response: Response,
    ):
        """Subida firmada de una imagen/descriptor de referencia (E3.2)."""
        timestamp = request.headers.get('x-timestamp', '')
        nonce = request.headers.get('x-nonce', '')
        signature_b64 = request.headers.get('x-signature', '')
        meta_b64 = request.headers.get('x-calibration-meta', '')
        if not timestamp or not nonce or not signature_b64 or not meta_b64:
            raise CalibrationError(400, 'invalid_request', 'Faltan headers de firma o metadatos')
        try:
            ts = int(timestamp)
        except ValueError:
            raise CalibrationError(400, 'invalid_request', 'Timestamp inválido') from None
        if abs(time.time() - ts) > 120:
            raise CalibrationError(401, 'invalid_request',
                                   'Firma expirada; compruebe NTP en la Raspberry')
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (ValueError, TypeError):
            raise CalibrationError(401, 'invalid_request', 'Firma incorrecta') from None

        raw = await request.body()
        if not raw or len(raw) > REFERENCE_MAX_BYTES:
            raise CalibrationError(413, 'invalid_request', 'Imagen demasiado grande o vacía')
        if not raw.startswith(b'\xff\xd8\xff'):
            raise CalibrationError(422, 'unsupported_format', 'La referencia debe ser JPEG')

        try:
            padded = meta_b64 + '=' * (-len(meta_b64) % 4)
            meta = json.loads(base64.urlsafe_b64decode(padded).decode())
        except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
            raise CalibrationError(422, 'invalid_request', 'Los metadatos no son válidos') from None
        if not isinstance(meta, dict):
            raise CalibrationError(422, 'invalid_request', 'Los metadatos no son un objeto')

        def need(name, kind=str):
            value = meta.get(name)
            if not isinstance(value, kind) or isinstance(value, bool):
                raise CalibrationError(422, 'invalid_request', f'Falta {name} en los metadatos')
            return value

        calibration_id = parse_uuid(need('calibration_id'), 'calibration_id')
        roi_id = parse_uuid(need('roi_id'), 'roi_id')
        capture_id = parse_uuid(need('capture_id'), 'capture_id')
        kind = need('kind')
        if kind not in ('empty', 'full'):
            raise CalibrationError(422, 'invalid_request', 'Tipo de referencia desconocido')
        context_hash = need('context_hash')
        if not HEX64.fullmatch(context_hash):
            raise CalibrationError(422, 'invalid_request', 'Huella de contexto inválida')
        descriptor_crc = need('descriptor_crc')
        if not HEX8.fullmatch(descriptor_crc):
            raise CalibrationError(422, 'invalid_request', 'CRC del descriptor inválido')
        calibration_version = need('calibration_version', int)
        format_version = need('format_version', int)
        image_sha256 = need('image_sha256').lower()
        if not HEX64.fullmatch(image_sha256):
            raise CalibrationError(422, 'invalid_request', 'Hash de imagen inválido')
        if need('image_bytes', int) != len(raw):
            raise CalibrationError(422, 'invalid_request', 'El tamaño de imagen no coincide')
        width = need('width', int)
        height = need('height', int)
        if width < 1 or height < 1:
            raise CalibrationError(422, 'invalid_request', 'Dimensiones inválidas')
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != image_sha256:
            raise CalibrationError(422, 'invalid_request', 'El hash de la imagen no coincide')
        captured_at = parse_timestamp(meta.get('captured_at')) or datetime.now(timezone.utc)
        descriptor = meta.get('descriptor')

        message = reference_signature_message(
            gateway_id, timestamp, nonce, device_id, actual_hash)

        with get_connection() as connection:
            gateway = connection.execute(
                '''SELECT g.id, g.branch_id, g.device_public_key, g.is_active, t.token_hash
                     FROM gateway g JOIN branch b ON b.id=g.branch_id
                     JOIN organization o ON o.id=b.organization_id
                     LEFT JOIN organization_token t ON t.id=g.registered_with_token
                    WHERE g.rpi_identifier=%s AND g.is_active AND b.is_active AND o.is_active''',
                (gateway_id,)).fetchone()
            if not gateway or not gateway['device_public_key']:
                raise CalibrationError(403, 'permission_denied',
                                       'Gateway no registrado o desactivado')
            from .gateway_registry import active_token
            if not gateway['token_hash'] or not active_token(
                    connection, gateway['token_hash'], gateway['branch_id']):
                raise CalibrationError(403, 'permission_denied', 'Token de sucursal revocado')
            if Ed25519PublicKey is None:
                raise CalibrationError(503, 'gateway_offline', 'Firma no disponible')
            public_key = base64.b64decode(gateway['device_public_key'], validate=True)
            try:
                Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
            except (ValueError, InvalidSignature):
                raise CalibrationError(401, 'invalid_request',
                                       'Firma no corresponde al gateway') from None

            node = connection.execute(
                '''SELECT n.id FROM esp32_node n
                    WHERE n.id=%s AND n.gateway_id=%s AND n.node_code IS NOT NULL
                      AND n.registration_status='registered' ''',
                (device_id, gateway['id'])).fetchone()
            if not node:
                raise CalibrationError(404, 'roi_not_found',
                                       'Dispositivo no encontrado en este gateway')

            session = connection.execute(
                '''SELECT * FROM roi_calibration_session
                    WHERE id=%s AND roi_id=%s''',
                (calibration_id, roi_id)).fetchone()
            if not session:
                raise CalibrationError(404, 'calibration_not_found',
                                       'No encontramos esta calibración.')
            if (session['branch_id'] != gateway['branch_id']
                    or session['camera_node_id'] != device_id):
                raise CalibrationError(404, 'calibration_not_found',
                                       'La calibración no pertenece a este dispositivo.')
            if int(session['calibration_version']) != calibration_version:
                raise CalibrationError(409, 'context_mismatch',
                                       'La versión de calibración no coincide.')
            if session['context_hash'].lower() != context_hash.lower():
                raise CalibrationError(409, 'context_mismatch',
                                       'La huella del contexto no coincide.')
            if int(session.get('descriptor_format') or FORMAT_VERSION) != format_version:
                raise CalibrationError(422, 'unsupported_format',
                                       'Formato de descriptor no compatible.')
            if session['state'] not in ('draft', 'empty_ready', 'pair_ready', 'committing'):
                raise CalibrationError(409, 'invalid_state',
                                       'La calibración ya no admite imágenes.')

            reference = connection.execute(
                '''SELECT * FROM roi_calibration
                    WHERE session_id=%s AND capture_id=%s FOR UPDATE''',
                (session['id'], capture_id)).fetchone()
            if not reference:
                raise CalibrationError(404, 'calibration_not_found',
                                       'No encontramos esa captura en la calibración.')
            if reference['reference_type'] != kind:
                raise CalibrationError(409, 'operation_conflict',
                                       'La captura pertenece a otra referencia.')
            stored_crc = (reference.get('descriptor_crc') or '').lower()
            if stored_crc and stored_crc != descriptor_crc.lower():
                raise CalibrationError(409, 'context_mismatch',
                                       'El CRC del descriptor no coincide.')

            metrics = dict(reference.get('reference_metrics') or {})
            if descriptor is not None:
                if not isinstance(descriptor, str):
                    raise CalibrationError(422, 'invalid_request', 'Descriptor inválido')
                try:
                    decoded = base64.b64decode(descriptor, validate=True)
                except (ValueError, TypeError, binascii.Error):
                    raise CalibrationError(422, 'invalid_request',
                                           'El descriptor no es base64 válido') from None
                if f'{zlib.crc32(decoded) & 0xffffffff:08x}' != descriptor_crc.lower():
                    raise CalibrationError(422, 'invalid_request',
                                           'El descriptor no corresponde al CRC')
                metrics['descriptor'] = descriptor

            existing = connection.execute(
                '''SELECT id, image_sha256 FROM snapshot
                    WHERE calibration_session_id=%s AND calibration_capture_id=%s''',
                (session['id'], capture_id)).fetchone()
            if existing:
                if (existing.get('image_sha256') or image_sha256) != image_sha256:
                    raise CalibrationError(409, 'operation_conflict',
                                           'Esa captura ya tiene otra imagen.')
                image_id = existing['id']
                connection.execute(
                    '''UPDATE roi_calibration SET status='ready', image_id=%s,
                              image_sha256=%s, image_bytes=%s, width=%s, height=%s,
                              descriptor_crc=COALESCE(descriptor_crc, %s),
                              descriptor_format=COALESCE(descriptor_format, %s),
                              received_at=COALESCE(received_at, NOW()),
                              reference_metrics=%s
                        WHERE id=%s''',
                    (image_id, image_sha256, len(raw), width, height,
                     descriptor_crc.lower(), format_version,
                     Jsonb(metrics), reference['id']))
                connection.commit()
                response.headers['Cache-Control'] = 'no-store'
                return {'ok': True, 'replay': True, 'image_id': str(image_id),
                        'capture_id': str(capture_id), 'status': 'ready'}

            image_id = connection.execute(
                '''INSERT INTO snapshot
                       (branch_id, device_id, data, content_type, captured_at,
                        purpose, calibration_session_id, calibration_capture_id,
                        image_sha256)
                   VALUES (%s, %s, %s, 'image/jpeg', %s, 'calibration', %s, %s, %s)
                   RETURNING id''',
                (gateway['branch_id'], device_id, raw, captured_at,
                 session['id'], capture_id, image_sha256)).fetchone()['id']
            connection.execute(
                '''UPDATE roi_calibration
                      SET status='ready', image_id=%s, image_sha256=%s,
                          image_bytes=%s, width=%s, height=%s, captured_at=%s,
                          descriptor_crc=COALESCE(descriptor_crc, %s),
                          descriptor_format=COALESCE(descriptor_format, %s),
                          received_at=NOW(), reference_metrics=%s
                    WHERE id=%s''',
                (image_id, image_sha256, len(raw), width, height, captured_at,
                 descriptor_crc.lower(), format_version,
                 Jsonb(metrics), reference['id']))
            state = state_from_references(connection, session)
            bump_session(connection, session['id'], state)
            connection.commit()

        response.headers['Cache-Control'] = 'no-store'
        return {'ok': True, 'image_id': str(image_id),
                'capture_id': str(capture_id), 'status': 'ready'}

    return router

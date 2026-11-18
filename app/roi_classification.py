"""Fase 7 (B4) — F-11, F-41, R-13: clasificación del estado visual en backend.

El ESP32 mide (``roi_occ``); el backend decide. Los umbrales viven en ``roi``
(configuración ME-2A) y la confirmación por muestras en ``roi_state``.

Una lectura sin frescura, con ``cam_moved`` o sin calibración vigente **no**
cambia el estado: se expone la calidad y se conserva el último estado válido.
La ocupación visual nunca modifica inventario lógico (F-21, C-17).
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from .devices import iso, require_branch
from .telemetry import InfluxSettings, RoiOccupancyResponse, read_roi_occupancy

STATUS_RANK = {'empty': 0, 'low': 1, 'medium': 2, 'adequate': 3}
LABELS = {'unknown': 'Desconocido', 'empty': 'Agotado', 'low': 'Bajo',
          'medium': 'Medio', 'adequate': 'Adecuado'}
STALE_SECONDS = 60
HYSTERESIS_PCT = 2
JUMP_LIMIT_PCT = 40
JUMP_REPEAT_PCT = 5

StateCode = Literal['unknown', 'empty', 'low', 'medium', 'adequate']
Quality = Literal['ok', 'stale', 'no_data', 'camera_moved', 'not_calibrated']

RoiReader = Callable[[dict], RoiOccupancyResponse]


@dataclass(frozen=True)
class Thresholds:
    """Umbrales inclusivos: 0 <= empty < low < medium <= 100."""

    empty: float
    low: float
    medium: float
    confirm_samples: int
    physical_version: int = 0
    configuration_version: int = 0

    def __post_init__(self):
        if not (0 <= self.empty < self.low < self.medium <= 100):
            raise ValueError('Los umbrales deben cumplir 0 <= agotado < bajo < medio <= 100')
        if self.confirm_samples < 1:
            raise ValueError('confirm_samples debe ser al menos 1')


def _band(pct: float, empty: float, low: float, medium: float) -> StateCode:
    if pct <= empty:
        return 'empty'
    if pct <= low:
        return 'low'
    if pct <= medium:
        return 'medium'
    return 'adequate'


def candidate_status(pct: float, thresholds: Thresholds, *, current: str | None = None,
                     hysteresis: float = HYSTERESIS_PCT) -> StateCode:
    """Banda candidata. Para cambiar de banda hay que rebasar el limite con holgura.

    Entrar en una banda peor exige ``umbral - histeresis``; entrar en una mejor
    exige ``umbral + histeresis``. Si la holgura no se cruza, se conserva el
    estado actual: eso evita el parpadeo entre estados vecinos (R-13).
    """
    base = _band(pct, thresholds.empty, thresholds.low, thresholds.medium)
    if current not in STATUS_RANK or base == current:
        return base
    shift = -hysteresis if STATUS_RANK[base] < STATUS_RANK[current] else hysteresis
    adjusted = _band(
        pct,
        min(100, max(0, thresholds.empty + shift)),
        min(100, max(0, thresholds.low + shift)),
        min(100, max(0, thresholds.medium + shift)),
    )
    return current if adjusted == current else base


def classify_step(pct: float, thresholds: Thresholds, state: dict, *,
                  last_pct: float | None = None,
                  last_discarded_pct: float | None = None,
                  now: datetime | None = None) -> dict:
    """Devuelve el estado resultante de una muestra. Sin efectos en base de datos."""
    now = now or datetime.now(timezone.utc)
    candidate = candidate_status(pct, thresholds, current=state.get('status'))

    discarded = False
    if last_pct is not None and abs(pct - last_pct) > JUMP_LIMIT_PCT:
        # Un salto aislado no cuenta como muestra; si se repite cerca del
        # anterior descartado, se acepta (puede ser un cambio real de repisa).
        if last_discarded_pct is None or abs(pct - last_discarded_pct) > JUMP_REPEAT_PCT:
            discarded = True
    if discarded:
        return {'status': state.get('status') or 'unknown', 'candidate_status': candidate,
                'pending_status': state.get('pending_status'),
                'pending_samples': int(state.get('pending_samples') or 0),
                'confidence': _confidence(state, thresholds),
                'last_changed_at': state.get('last_changed_at'), 'discarded': True}

    status = state.get('status') or 'unknown'
    pending = state.get('pending_status')
    samples = int(state.get('pending_samples') or 0)
    changed = state.get('last_changed_at')
    if candidate == status:
        pending, samples = None, 0
    else:
        if candidate == pending:
            samples += 1
        else:
            pending, samples = candidate, 1
        # La primera muestra también confirma cuando confirm_samples=1.
        if samples >= thresholds.confirm_samples:
            status, pending, samples, changed = candidate, None, 0, now

    result = {'status': status, 'candidate_status': candidate,
              'pending_status': pending, 'pending_samples': samples,
              'confidence': 1.0 if pending is None else min(1.0, samples / thresholds.confirm_samples),
              'last_changed_at': changed, 'discarded': False}
    return result


def _confidence(state: dict, thresholds: Thresholds) -> float:
    if not state.get('pending_status'):
        return 1.0
    return min(1.0, int(state.get('pending_samples') or 0) / thresholds.confirm_samples)


def thresholds_from(row: dict) -> Thresholds:
    return Thresholds(
        empty=float(row['threshold_empty_pct']),
        low=float(row['threshold_low_pct']),
        medium=float(row['threshold_medium_pct']),
        confirm_samples=int(row['confirm_samples']),
        physical_version=int(row.get('physical_config_version') or 0),
        configuration_version=int(row.get('config_version') or 0),
    )


ROI_CONTEXT_SQL = '''
    SELECT r.id AS roi_id, r.product_id, r.name AS roi_name, p.sku,
           r.threshold_empty_pct, r.threshold_low_pct, r.threshold_medium_pct,
           r.confirm_samples, r.physical_config_version, r.calibration_status,
           r.last_calibrated_at, s.id AS shelf_id, s.name AS shelf_name,
           s.camera_node_id, s.roi_applied_version AS config_version,
           c.calibration_version
    FROM roi r
    JOIN shelf s ON s.id=r.shelf_id
    JOIN product p ON p.id=r.product_id
    LEFT JOIN roi_calibration_session c ON c.id=r.active_calibration_id
         AND c.state='active' AND c.config_version=s.roi_applied_version
    WHERE r.id=%s AND s.branch_id=%s AND r.is_active AND s.is_active AND p.is_active
'''


def load_roi(connection, branch_id: UUID, roi_id: UUID, user):
    """Autoriza la sucursal y carga umbrales + contexto de medición de una ROI."""
    require_branch(connection, branch_id, user)
    row = connection.execute(ROI_CONTEXT_SQL, (roi_id, branch_id)).fetchone()
    if not row:
        raise LookupError('Zona no encontrada en esta sucursal')
    return row


def _reader_context(row: dict) -> dict:
    return {'camera_node_id': row['camera_node_id'], 'config_version': row['config_version'],
            'calibration_version': row['calibration_version'],
            'calibration_status': row['calibration_status']}


def default_reader(row: dict) -> RoiOccupancyResponse:
    return read_roi_occupancy(InfluxSettings.from_env(), UUID(str(row['branch_id'])),
                              UUID(str(row['roi_id'])), _reader_context(row))


def classify(connection, row: dict, occupancy: RoiOccupancyResponse, thresholds: Thresholds) -> dict:
    """Persiste lectura y estado. El llamador hace commit."""
    roi_id = row['roi_id']
    state = connection.execute('SELECT * FROM roi_state WHERE roi_id=%s', (roi_id,)).fetchone() or {}
    recent = connection.execute(
        'SELECT occupancy_pct,is_discarded FROM roi_reading WHERE roi_id=%s ORDER BY id DESC LIMIT 2',
        (roi_id,)).fetchall()
    last_pct = next((r['occupancy_pct'] for r in recent if not r['is_discarded']), None)
    last_discarded = recent[0]['occupancy_pct'] if recent and recent[0]['is_discarded'] else None

    calibrated = row['calibration_status'] == 'calibrated' and row['calibration_version'] is not None
    usable = (calibrated and row['config_version'] is not None
              and occupancy.status == 'ok' and occupancy.occupancy_pct is not None
              and not occupancy.cam_moved)

    if not usable:
        # No se transforma ausencia, antigüedad ni cámara movida en un estado.
        connection.execute(
            'INSERT INTO roi_reading(roi_id,occupancy_pct,candidate_status,is_discarded) '
            'VALUES(%s,%s,NULL,TRUE)',
            (roi_id, occupancy.occupancy_pct))
        status = 'unknown' if not calibrated else state.get('status') or 'unknown'
        connection.execute(
            'INSERT INTO roi_state(roi_id,occupancy_pct,status,pending_status,pending_samples,'
            'last_reading_at,updated_at) VALUES(%s,NULL,%s,NULL,0,NULL,NOW()) '
            'ON CONFLICT(roi_id) DO UPDATE SET status=EXCLUDED.status,updated_at=NOW()',
            (roi_id, status))
        return {**state, 'status': status, 'occupancy_pct': None, 'pending_status': None,
                'pending_samples': 0, 'confidence': None}

    pct = int(occupancy.occupancy_pct)
    result = classify_step(pct, thresholds, state, last_pct=last_pct,
                           last_discarded_pct=last_discarded)
    connection.execute(
        'INSERT INTO roi_reading(roi_id,occupancy_pct,candidate_status,is_discarded) '
        'VALUES(%s,%s,%s,%s)',
        (roi_id, pct, result['candidate_status'], result['discarded']))
    changed = ('NOW()' if result['last_changed_at'] != state.get('last_changed_at')
               and result['status'] != state.get('status') else 'roi_state.last_changed_at')
    connection.execute(
        f'''INSERT INTO roi_state(roi_id,occupancy_pct,status,confidence,pending_status,
                pending_samples,last_reading_at,last_changed_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
            ON CONFLICT(roi_id) DO UPDATE SET occupancy_pct=EXCLUDED.occupancy_pct,
                status=EXCLUDED.status,confidence=EXCLUDED.confidence,
                pending_status=EXCLUDED.pending_status,pending_samples=EXCLUDED.pending_samples,
                last_reading_at=EXCLUDED.last_reading_at,last_changed_at={changed},updated_at=NOW()''',
        (roi_id, pct, result['status'], result['confidence'], result['pending_status'],
         result['pending_samples'], occupancy.received_at))
    return {**state, **result, 'occupancy_pct': pct}


def _quality(row: dict, occupancy: RoiOccupancyResponse | None) -> Quality:
    if row['calibration_status'] != 'calibrated':
        return 'not_calibrated'
    if occupancy is None:
        return 'no_data'
    if occupancy.cam_moved:
        return 'camera_moved'
    return occupancy.status


class RoiStateResponse(BaseModel):
    branch_id: UUID
    roi_id: UUID
    shelf_id: UUID | None
    shelf_name: str | None
    roi_name: str | None
    product_id: UUID | None
    sku: str | None
    status: StateCode
    status_label: str
    quality: Quality
    occupancy_pct: int | None
    confidence: float | None
    pending_status: StateCode | None
    pending_samples: int
    confirm_samples: int
    config_version: int | None
    calibration_version: int | None
    received_at: datetime | None
    age_seconds: int | None
    stale_after_seconds: int
    last_changed_at: datetime | None
    updated_at: datetime | None
    cam_moved: bool = False


def _response(branch_id: UUID, row: dict, state: dict, occupancy: RoiOccupancyResponse | None,
              thresholds: Thresholds) -> RoiStateResponse:
    status = state.get('status') if state.get('status') in STATUS_RANK else 'unknown'
    age = occupancy.age_seconds if occupancy else None
    return RoiStateResponse(
        branch_id=branch_id, roi_id=row['roi_id'], shelf_id=row.get('shelf_id'),
        shelf_name=row.get('shelf_name'), roi_name=row.get('roi_name'),
        product_id=row.get('product_id'), sku=row.get('sku'),
        status=status, status_label=LABELS[status], quality=_quality(row, occupancy),
        occupancy_pct=state.get('occupancy_pct'), confidence=state.get('confidence'),
        pending_status=state.get('pending_status'),
        pending_samples=int(state.get('pending_samples') or 0),
        confirm_samples=thresholds.confirm_samples,
        config_version=row.get('config_version'), calibration_version=row.get('calibration_version'),
        received_at=occupancy.received_at if occupancy else None, age_seconds=age,
        stale_after_seconds=STALE_SECONDS, last_changed_at=state.get('last_changed_at'),
        updated_at=state.get('updated_at'), cam_moved=bool(occupancy and occupancy.cam_moved))


STATE_ROWS_SQL = '''
    SELECT r.id AS roi_id, r.product_id, r.name AS roi_name, p.sku,
           r.threshold_empty_pct, r.threshold_low_pct, r.threshold_medium_pct,
           r.confirm_samples, r.physical_config_version, r.calibration_status,
           s.id AS shelf_id, s.name AS shelf_name, s.roi_applied_version AS config_version,
           c.calibration_version, st.status, st.occupancy_pct, st.confidence,
           st.pending_status, st.pending_samples, st.last_reading_at,
           st.last_changed_at, st.updated_at
    FROM roi r
    JOIN shelf s ON s.id=r.shelf_id
    JOIN product p ON p.id=r.product_id
    LEFT JOIN roi_calibration_session c ON c.id=r.active_calibration_id
         AND c.state='active' AND c.config_version=s.roi_applied_version
    LEFT JOIN roi_state st ON st.roi_id=r.id
    WHERE s.branch_id=%s AND r.is_active AND s.is_active AND p.is_active
    ORDER BY s.name,r.name,r.id
'''


def scan_branch_states(get_connection, branch_id: UUID, reader: RoiReader | None = None):
    """Clasifica todas las ROI activas de una sucursal. Lo usará el monitor del paso 2."""
    with get_connection() as connection:
        require_branch(connection, branch_id, {'is_superuser': True, 'role': 'sudo',
                                               'organization_id': None, 'branch_id': branch_id})
        rows = connection.execute(STATE_ROWS_SQL, (branch_id,)).fetchall()
        for row in rows:
            context = {**_reader_context(row), 'branch_id': str(branch_id), 'roi_id': str(row['roi_id'])}
            occupancy = (reader or default_reader)(context)
            classify(connection, row, occupancy, thresholds_from(row))
        connection.commit()
    return len(rows)


def create_router(current_user, get_connection, reader: RoiReader | None = None):
    router = APIRouter(tags=['ROI state'])
    read = reader or default_reader

    @router.get('/branches/{branch_id}/rois/{roi_id}/state', response_model=RoiStateResponse)
    def roi_state(branch_id: UUID, roi_id: UUID, response: Response,
                  user=Depends(current_user)):
        with get_connection() as connection:
            row = load_roi(connection, branch_id, roi_id, user)
            context = {**_reader_context(row), 'branch_id': str(branch_id), 'roi_id': str(roi_id)}
            occupancy = read(context)
            thresholds = thresholds_from(row)
            state = classify(connection, row, occupancy, thresholds)
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return _response(branch_id, row, state, occupancy, thresholds)

    @router.get('/branches/{branch_id}/shelf-states', response_model=list[RoiStateResponse])
    def shelf_states(branch_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            rows = connection.execute(STATE_ROWS_SQL, (branch_id,)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        result = []
        for row in rows:
            last = row.get('last_reading_at')
            age = max(0, int((datetime.now(timezone.utc) - last).total_seconds())) if last else None
            quality: Quality = ('no_data' if last is None else
                                'stale' if age >= STALE_SECONDS else 'ok')
            if row['calibration_status'] != 'calibrated':
                quality = 'not_calibrated'
            fake = RoiOccupancyResponse(
                branch_id=branch_id, roi_id=row['roi_id'], occupancy_pct=None,
                config_version=row.get('config_version'),
                calibration_version=row.get('calibration_version'),
                received_at=last, age_seconds=age, stale_after_seconds=STALE_SECONDS,
                status=quality if quality in ('ok', 'stale', 'no_data') else 'no_data')
            result.append(_response(branch_id, row, row, fake, thresholds_from(row)))
        return result

    return router

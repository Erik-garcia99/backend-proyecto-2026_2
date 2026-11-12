"""Zonas (ROI) configurables por repisa — Fase 3.

El conjunto de zonas vive en la repisa y se reemplaza completo en cada
``PUT``. La versión deseada (``shelf.roi_version``) sube al guardar; la
versión confirmada por el nodo (``shelf.roi_applied_version``) solo cambia
cuando el ESP32 responde ``roi_ack``. Nunca se borran filas de ``roi``:
las zonas retiradas se marcan ``is_active = false`` para conservar su
histórico de lecturas y calibraciones.
"""
from math import isfinite
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .calibrations import CalibrationError
from .devices import iso, require_branch

ROI_LIMIT = 8
ROI_MIN_SIDE = 0.03
ROI_MAX_OVERLAP = 0.02
# Holgura para no rechazar sumas como 0.1 + 0.2 por redondeo binario.
GEOMETRY_EPSILON = 1e-9


class RoiItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: UUID | None = None
    product_id: UUID
    name: str = Field(min_length=1, max_length=80)
    x: float
    y: float
    w: float
    h: float
    visual_capacity_units: int | None = Field(default=None, ge=1, le=9999)


class RoiSet(BaseModel):
    base_version: int = Field(ge=0)
    rois: list[RoiItem]


class RoiApply(BaseModel):
    version: int = Field(ge=0)


def _overlap_area(first, second):
    left = max(first.x, second.x)
    top = max(first.y, second.y)
    right = min(first.x + first.w, second.x + second.w)
    bottom = min(first.y + first.h, second.y + second.h)
    if right <= left or bottom <= top:
        return 0.0
    return (right - left) * (bottom - top)


def validate_rois(rois):
    """Valida un conjunto completo de zonas sin tocar la base de datos.

    Lanza ``ValueError`` con un mensaje apto para el usuario. Es pura para
    poder probarla y reutilizarla desde el gateway si hiciera falta.
    """
    if len(rois) > ROI_LIMIT:
        raise ValueError(f'Máximo {ROI_LIMIT} zonas por cámara')

    seen = set()
    for roi in rois:
        if roi.id is not None:
            if roi.id in seen:
                raise ValueError('Hay zonas repetidas en el conjunto')
            seen.add(roi.id)
        if (not all(isfinite(value) for value in (roi.x, roi.y, roi.w, roi.h))
                or roi.x < 0 or roi.y < 0
                or roi.w < ROI_MIN_SIDE or roi.h < ROI_MIN_SIDE
                or roi.x + roi.w > 1 + GEOMETRY_EPSILON
                or roi.y + roi.h > 1 + GEOMETRY_EPSILON):
            raise ValueError('La zona se sale de la imagen')

    for index, first in enumerate(rois):
        first_area = first.w * first.h
        for second in rois[index + 1:]:
            smaller = min(first_area, second.w * second.h)
            if smaller <= 0:
                continue
            if _overlap_area(first, second) / smaller > ROI_MAX_OVERLAP + GEOMETRY_EPSILON:
                raise ValueError('Las zonas se solapan')


def serialize_roi(row):
    return {
        'id': str(row['id']),
        'product_id': str(row['product_id']),
        'product_sku': row.get('sku'),
        'product_name': row.get('product_name'),
        'name': row['name'],
        'x': float(row['bbox_x']),
        'y': float(row['bbox_y']),
        'w': float(row['bbox_w']),
        'h': float(row['bbox_h']),
        'visual_capacity_units': row.get('visual_capacity_units'),
        'calibration_status': row.get('calibration_status') or 'uncalibrated',
        'calibration_version': row.get('calibration_version') or 0,
        'last_calibrated_at': iso(row.get('last_calibrated_at')),
        'calibration_invalidated_reason': row.get('calibration_invalidated_reason'),
    }


def load_shelf(connection, branch_id, shelf_id, *, lock=False):
    query = '''SELECT s.*, n.gateway_id AS camera_gateway_id, g.rpi_identifier,
                      (SELECT c.id FROM roi_calibration_session c
                        WHERE c.camera_node_id=s.camera_node_id
                          AND c.state IN ('draft','empty_ready','pair_ready',
                                          'committing','cancel_pending')
                          AND c.expires_at > NOW()
                        LIMIT 1) AS open_calibration_id
               FROM shelf s
               LEFT JOIN esp32_node n ON n.id=s.camera_node_id
               LEFT JOIN gateway g ON g.id=n.gateway_id AND g.is_active
               WHERE s.id=%s AND s.branch_id=%s AND s.is_active'''
    if lock:
        query += ' FOR UPDATE OF s'
    shelf = connection.execute(query, (shelf_id, branch_id)).fetchone()
    if not shelf:
        raise HTTPException(404, 'Estante no encontrado en esta sucursal')
    if not shelf.get('camera_node_id'):
        raise HTTPException(409, 'El estante no está vinculado a una cámara')
    return shelf


def _active_rois(connection, shelf_id):
    return connection.execute(
        '''SELECT r.*, p.sku, p.name AS product_name
           FROM roi r JOIN product p ON p.id=r.product_id
           WHERE r.shelf_id=%s AND r.is_active
           ORDER BY r.name, r.created_at, r.id''',
        (shelf_id,)).fetchall()


def _ensure_products(connection, organization_id, rois):
    product_ids = {roi.product_id for roi in rois}
    if not product_ids:
        return
    rows = connection.execute(
        '''SELECT id FROM product
           WHERE organization_id=%s AND is_active AND id = ANY(%s::uuid[])''',
        (organization_id, list(product_ids))).fetchall()
    found = {row['id'] for row in rows}
    if product_ids - found:
        raise HTTPException(422, 'Hay un producto inactivo o de otra organización')


def _confirmation_error(result):
    """Devuelve el error de la confirmación o ``None`` si fue exitosa.

    La Raspberry responde ``{ok, data}``. El ACK del ESP32 llega dentro de
    ``data``; según la versión del gateway se identifica con ``ok`` o con
    ``rois``. Se aceptan ambas formas para no acoplar el backend a un
    despliegue concreto.
    """
    if not isinstance(result, dict) or result.get('ok') is not True:
        return (result or {}).get('error') or 'La Raspberry no confirmó el envío'
    data = result.get('data')
    if not isinstance(data, dict):
        return 'La Raspberry no devolvió la confirmación del nodo'
    if data.get('ok') is False or data.get('rois') is False:
        return data.get('error') or 'El nodo rechazó las zonas'
    if data.get('ok') is not True and data.get('rois') is not True:
        return data.get('error') or 'El nodo no confirmó la aplicación'
    return None


def _apply_error_status(error):
    text = (error or '').lower()
    if 'timeout' in text or 'sin ack' in text or 'sin confirmación' in text:
        return 504
    return 502


def create_router(current_user, get_connection, hub):
    router = APIRouter(tags=['ROIs'])

    @router.get('/branches/{branch_id}/shelves/{shelf_id}/rois')
    def list_rois(branch_id: UUID, shelf_id: UUID, response: Response,
                  user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            shelf = load_shelf(connection, branch_id, shelf_id)
            rows = _active_rois(connection, shelf_id)
        response.headers['Cache-Control'] = 'no-store'
        return {
            'version': shelf['roi_version'],
            'applied_version': shelf['roi_applied_version'],
            'applied_at': iso(shelf.get('roi_applied_at')),
            'rois': [serialize_roi(row) for row in rows],
        }

    @router.put('/branches/{branch_id}/shelves/{shelf_id}/rois')
    def replace_rois(branch_id: UUID, shelf_id: UUID, body: RoiSet, response: Response,
                     user=Depends(current_user)):
        try:
            validate_rois(body.rois)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

        with get_connection() as connection:
            branch = require_branch(connection, branch_id, user)
            shelf = load_shelf(connection, branch_id, shelf_id, lock=True)
            if body.base_version != shelf['roi_version']:
                raise HTTPException(409, 'Otra persona cambió las zonas; recarga')
            if shelf.get('open_calibration_id'):
                raise CalibrationError(
                    409, 'session_conflict',
                    'Hay una calibración abierta en esta cámara; termínala o cancélala antes de cambiar las zonas')
            _ensure_products(connection, branch['organization_id'], body.rois)

            existing = {row['id'] for row in connection.execute(
                'SELECT id FROM roi WHERE shelf_id=%s', (shelf_id,)).fetchall()}
            submitted = {roi.id for roi in body.rois if roi.id is not None}
            if submitted - existing:
                raise HTTPException(422, 'Una zona no pertenece a esta repisa')

            # Las zonas retiradas se conservan para no perder su histórico.
            connection.execute(
                '''UPDATE roi SET is_active=false, updated_at=NOW()
                   WHERE shelf_id=%s AND is_active AND id <> ALL(%s::uuid[])''',
                (shelf_id, list(submitted)))

            for roi in body.rois:
                values = (roi.product_id, roi.name, roi.x, roi.y, roi.w, roi.h,
                          roi.visual_capacity_units)
                if roi.id is not None:
                    connection.execute(
                        '''UPDATE roi SET product_id=%s, name=%s, bbox_x=%s, bbox_y=%s,
                             bbox_w=%s, bbox_h=%s, visual_capacity_units=%s,
                             is_active=true, updated_at=NOW()
                           WHERE id=%s AND shelf_id=%s''',
                        values + (roi.id, shelf_id))
                else:
                    connection.execute(
                        '''INSERT INTO roi(shelf_id, product_id, name, bbox_x, bbox_y,
                             bbox_w, bbox_h, visual_capacity_units)
                           VALUES(%s, %s, %s, %s, %s, %s, %s, %s)''',
                        (shelf_id,) + values)

            updated = connection.execute(
                '''UPDATE shelf SET roi_version=roi_version+1, updated_at=NOW()
                   WHERE id=%s RETURNING roi_version, roi_applied_version''',
                (shelf_id,)).fetchone()
            rows = _active_rois(connection, shelf_id)
            connection.commit()

        response.headers['Cache-Control'] = 'no-store'
        return {
            'version': updated['roi_version'],
            'applied_version': updated['roi_applied_version'],
            'rois': [serialize_roi(row) for row in rows],
        }

    @router.post('/branches/{branch_id}/shelves/{shelf_id}/rois/apply')
    async def apply_rois(branch_id: UUID, shelf_id: UUID, body: RoiApply,
                         response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            shelf = load_shelf(connection, branch_id, shelf_id, lock=True)
            if body.version != shelf['roi_version']:
                raise HTTPException(409, 'La versión solicitada no es la actual; recarga')
            if shelf.get('open_calibration_id'):
                raise CalibrationError(
                    409, 'session_conflict',
                    'Hay una calibración abierta en esta cámara; termínala o cancélala antes de aplicar zonas')
            rows = _active_rois(connection, shelf_id)

        gateway = shelf.get('rpi_identifier')
        if not gateway:
            raise HTTPException(503, 'El equipo principal de la sucursal no está disponible')

        req_id = uuid4().hex
        payload = {
            'node_id': str(shelf['camera_node_id']),
            'branch_id': str(branch_id),
            'module': 'shelf_cam',
            'req_id': req_id,
            'version': body.version,
            'rois': [
                {'id': str(row['id']), 'x': float(row['bbox_x']), 'y': float(row['bbox_y']),
                 'w': float(row['bbox_w']), 'h': float(row['bbox_h'])}
                for row in rows
            ],
        }
        try:
            result = await hub.send(gateway, 'roi.set', payload=payload)
        except HTTPException as exc:
            raise HTTPException(
                exc.status_code,
                f'No se pudo aplicar en la cámara: {exc.detail}') from None

        error = _confirmation_error(result)
        if error:
            raise HTTPException(_apply_error_status(error), error)

        with get_connection() as connection:
            updated = connection.execute(
                '''UPDATE shelf SET roi_applied_version=%s, roi_applied_at=NOW(),
                     updated_at=NOW()
                   WHERE id=%s AND roi_version=%s
                   RETURNING roi_applied_version, roi_applied_at''',
                (body.version, shelf_id, body.version)).fetchone()
            if not updated:
                connection.rollback()
                raise HTTPException(
                    409, 'Las zonas cambiaron mientras se aplicaban; vuelve a intentar')
            connection.commit()

        response.headers['Cache-Control'] = 'no-store'
        return {
            'confirmed': True,
            'version': body.version,
            'applied_version': updated['roi_applied_version'],
            'count': len(rows),
            'req_id': req_id,
        }

    return router

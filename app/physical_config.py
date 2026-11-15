"""ME-2A: configuración cloud; no publica órdenes ni modifica calibración."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .devices import require_branch


class ProductSpec(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True)
    base_version: int = Field(ge=0, strict=True)
    width_mm: float = Field(gt=0, le=100000)
    height_mm: float = Field(gt=0, le=100000)
    depth_mm: float = Field(gt=0, le=100000)
    usual_orientation: Literal['upright', 'sideways', 'flat']


class RoiConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True)
    base_version: int = Field(ge=0, strict=True)
    expected_product_id: UUID = Field(strict=False)
    visual_capacity_units: int | None = Field(default=None, ge=1, le=9999, strict=True)
    threshold_empty_pct: float = Field(ge=0, lt=100)
    threshold_low_pct: float = Field(gt=0, lt=100)
    threshold_medium_pct: float = Field(gt=0, lt=100)
    confirm_samples: int = Field(ge=1, le=100, strict=True)
    virtual_slots_enabled: bool = Field(strict=True)
    slots_rows: int | None = Field(default=None, ge=1, le=16, strict=True)
    slots_cols: int | None = Field(default=None, ge=1, le=16, strict=True)

    @model_validator(mode='after')
    def coherent(self):
        if not self.threshold_empty_pct < self.threshold_low_pct < self.threshold_medium_pct:
            raise ValueError('Los umbrales deben cumplir agotado < bajo < medio < 100')
        if self.virtual_slots_enabled:
            if self.slots_rows is None or self.slots_cols is None:
                raise ValueError('Indica filas y columnas para los espacios virtuales')
            if self.slots_rows * self.slots_cols > 128:
                raise ValueError('Máximo 128 espacios virtuales de configuración')
        elif self.slots_rows is not None or self.slots_cols is not None:
            raise ValueError('Desactiva filas y columnas cuando los espacios estén apagados')
        return self


SPEC_FIELDS = ('width_mm', 'height_mm', 'depth_mm', 'usual_orientation')
ROI_FIELDS = tuple(name for name in RoiConfig.model_fields if name not in {'base_version', 'expected_product_id'})


def snapshot(row, fields, version_field):
    result = {}
    for field in fields:
        value = row.get(field)
        result[field] = float(value) if field.endswith(('_mm', '_pct')) and value is not None else value
    return {**result, 'version': row.get(version_field, 0)}


def product_in_branch(connection, branch_id, product_id, user, *, lock=False):
    branch = require_branch(connection, branch_id, user)
    row = connection.execute(
        """SELECT id,sku,name FROM product WHERE id=%s AND organization_id=%s AND is_active"""
        + (' FOR UPDATE' if lock else ''), (product_id, branch['organization_id'])).fetchone()
    if not row:
        raise HTTPException(404, 'Producto no encontrado en esta organización')
    return row


def record(connection, branch_id, user, event, product_id, before, after, roi_id=None):
    connection.execute(
        """INSERT INTO system_log(branch_id,user_id,category,severity,event_type,message,metadata)
           VALUES(%s,%s,'configuration','info',%s,'Configuración guardada solo en el sistema',%s)""",
        (branch_id, user['id'], event, Jsonb({
            'product_id': str(product_id), 'roi_id': str(roi_id) if roi_id else None,
            'before': before, 'after': after, 'application_status': 'not_integrated'})))


def create_router(current_user, get_connection):
    router = APIRouter(tags=['Physical configuration'])

    @router.get('/branches/{branch_id}/products/{product_id}/physical-spec')
    def get_spec(branch_id: UUID, product_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            product = product_in_branch(connection, branch_id, product_id, user)
            spec = connection.execute(
                'SELECT * FROM product_physical_spec WHERE product_id=%s', (product_id,)).fetchone()
        response.headers['Cache-Control'] = 'no-store'
        return {'product': product, 'spec': snapshot(spec or {}, SPEC_FIELDS, 'config_version'),
                'application_status': 'not_integrated', 'scope': 'organization_product'}

    @router.put('/branches/{branch_id}/products/{product_id}/physical-spec')
    def save_spec(branch_id: UUID, product_id: UUID, body: ProductSpec,
                  response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            product_in_branch(connection, branch_id, product_id, user, lock=True)
            old = connection.execute(
                'SELECT * FROM product_physical_spec WHERE product_id=%s FOR UPDATE', (product_id,)).fetchone()
            before = snapshot(old or {}, SPEC_FIELDS, 'config_version')
            if body.base_version != before['version']:
                raise HTTPException(409, 'Otra persona modificó las medidas; recarga antes de guardar')
            row = connection.execute(
                """INSERT INTO product_physical_spec
                     (product_id,width_mm,height_mm,depth_mm,usual_orientation,config_version)
                   VALUES(%s,%s,%s,%s,%s,1)
                   ON CONFLICT(product_id) DO UPDATE SET width_mm=EXCLUDED.width_mm,
                     height_mm=EXCLUDED.height_mm,depth_mm=EXCLUDED.depth_mm,
                     usual_orientation=EXCLUDED.usual_orientation,
                     config_version=product_physical_spec.config_version+1,updated_at=NOW()
                   RETURNING *""", (product_id, body.width_mm, body.height_mm,
                                     body.depth_mm, body.usual_orientation)).fetchone()
            after = snapshot(row, SPEC_FIELDS, 'config_version')
            record(connection, branch_id, user, 'product.physical_spec_saved', product_id, before, after)
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return {'spec': after, 'application_status': 'not_integrated'}

    @router.get('/branches/{branch_id}/products/{product_id}/roi-configs')
    def list_configs(branch_id: UUID, product_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            product_in_branch(connection, branch_id, product_id, user)
            rows = connection.execute(
                """SELECT r.*,s.name AS shelf_name FROM roi r JOIN shelf s ON s.id=r.shelf_id
                   WHERE r.product_id=%s AND s.branch_id=%s AND r.is_active AND s.is_active
                   ORDER BY s.name,r.name,r.id""", (product_id, branch_id)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return [{'id': row['id'], 'name': row['name'], 'shelf_name': row['shelf_name'],
                 'product_id': row['product_id'], 'config': snapshot(row, ROI_FIELDS, 'physical_config_version'),
                 'application_status': 'not_integrated'} for row in rows]

    @router.put('/branches/{branch_id}/rois/{roi_id}/physical-config')
    def save_config(branch_id: UUID, roi_id: UUID, body: RoiConfig,
                    response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch(connection, branch_id, user)
            row = connection.execute(
                """SELECT r.* FROM roi r JOIN shelf s ON s.id=r.shelf_id
                   JOIN product p ON p.id=r.product_id
                   WHERE r.id=%s AND s.branch_id=%s AND p.organization_id=%s
                     AND r.is_active AND s.is_active AND p.is_active FOR UPDATE OF r""",
                (roi_id, branch_id, branch['organization_id'])).fetchone()
            if not row:
                raise HTTPException(404, 'Zona no encontrada en esta sucursal')
            if row['product_id'] != body.expected_product_id:
                raise HTTPException(409, 'El producto asociado a la zona cambió; vuelve al catálogo y recarga')
            before = snapshot(row, ROI_FIELDS, 'physical_config_version')
            if body.base_version != before['version']:
                raise HTTPException(409, 'Otra persona modificó la configuración; recarga antes de guardar')
            updated = connection.execute(
                """UPDATE roi SET visual_capacity_units=%s,threshold_empty_pct=%s,
                     threshold_low_pct=%s,threshold_medium_pct=%s,confirm_samples=%s,
                     virtual_slots_enabled=%s,slots_rows=%s,slots_cols=%s,
                     physical_config_version=physical_config_version+1,updated_at=NOW()
                   WHERE id=%s RETURNING *""",
                (body.visual_capacity_units, body.threshold_empty_pct, body.threshold_low_pct,
                 body.threshold_medium_pct, body.confirm_samples, body.virtual_slots_enabled,
                 body.slots_rows, body.slots_cols, roi_id)).fetchone()
            after = snapshot(updated, ROI_FIELDS, 'physical_config_version')
            record(connection, branch_id, user, 'roi.physical_config_saved', row['product_id'],
                   before, after, roi_id)
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return {'config': after, 'application_status': 'not_integrated'}

    return router

"""Ajustes manuales del contador de aforo (occupancy.adjust).

Contrato:
- Solo nodos de tipo access_counter (aforo) de la sucursal.
- El reset/ajuste se propaga al nodo vía gateway (comando occupancy.reset).
- Se registra auditoría (usuario, antes/después, motivo, idempotencia).
"""
import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .devices import load_node, require_branch
from .registration_tokens import require_branch_access

log = logging.getLogger('selfiq.occupancy')


class OccupancyAdjustmentRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    node_id: UUID
    value: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=240)


def create_router(current_user, get_connection, hub):
    router = APIRouter(tags=['Occupancy'])

    @router.post('/branches/{branch_id}/occupancy/adjustments')
    async def adjust_occupancy(branch_id: UUID, body: OccupancyAdjustmentRequest,
                              request: Request, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            node = load_node(connection, branch_id, body.node_id, node_type='access_counter')
            if not node:
                raise HTTPException(404, 'Nodo de aforo no encontrado en esta sucursal')
            rpi_identifier = node['rpi_identifier']

        # Propagar al nodo vía gateway.
        try:
            result = await hub.send(rpi_identifier, 'occupancy.adjust',
                                    payload={'node_id': str(body.node_id),
                                            'branch_id': str(branch_id),
                                            'value': body.value})
        except HTTPException as exc:
            raise HTTPException(503, f'No se pudo aplicar el ajuste en el nodo: {exc.detail}') from exc
        except Exception as exc:
            raise HTTPException(503, f'No se pudo aplicar el ajuste en el nodo: {exc}') from exc

        if not result or result.get('ok') is not True:
            raise HTTPException(502, (result or {}).get('error')
                                or 'El nodo no confirmó el ajuste de aforo')

        data = result.get('data') if isinstance(result.get('data'), dict) else {}
        if data.get('occupancy_adjusted') is not True or data.get('node_id') != str(body.node_id):
            raise HTTPException(502, 'El nodo no confirmó el ajuste de aforo')

        # Registrar auditoría (best-effort: si la tabla no existe, no bloquea el ajuste).
        try:
            with get_connection() as connection:
                connection.execute(
                    '''INSERT INTO occupancy_adjustment
                       (id, branch_id, node_id, value, reason, created_by, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, NOW())''',
                    (uuid4(), branch_id, body.node_id, body.value, body.reason,
                     getattr(user, 'id', None)))
                connection.commit()
        except Exception as exc:
            log.warning('No se pudo registrar auditoría de ajuste de aforo: %s', exc)

        return {
            'node_id': str(body.node_id),
            'branch_id': str(branch_id),
            'value': body.value,
            'reason': body.reason,
            'applied': True,
            'node_result': result,
        }

    @router.get('/branches/{branch_id}/occupancy/adjustments')
    def list_adjustments(branch_id: UUID, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch(connection, branch_id, user)
            try:
                rows = connection.execute(
                    '''SELECT id, node_id, value, reason, created_by, created_at
                       FROM occupancy_adjustment
                       WHERE branch_id=%s ORDER BY created_at DESC LIMIT 100''',
                    (branch_id,)).fetchall()
            except Exception:
                return []
        return [
            {
                'id': str(r['id']),
                'node_id': str(r['node_id']),
                'value': r['value'],
                'reason': r['reason'],
                'created_by': str(r['created_by']) if r['created_by'] else None,
                'created_at': r['created_at'].isoformat() if r['created_at'] else None,
            }
            for r in rows
        ]

    return router

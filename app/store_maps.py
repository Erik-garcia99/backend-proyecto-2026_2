"""Borrador privado del croquis. No publica mapas ni modifica estantes/hardware."""
import math
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .logs import record_change
from .permissions import can_edit_map, require_branch_actor

Coordinate = Annotated[float, Field(strict=True, ge=0, le=10000, allow_inf_nan=False)]
Size = Annotated[float, Field(strict=True, ge=1, le=10000, allow_inf_nan=False)]


class MapElement(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    id: UUID
    kind: Literal['rectangle', 'ellipse', 'hexagon', 'wall', 'door', 'double_door', 'window']
    label: str = Field(default='', max_length=80)
    x: Coordinate
    y: Coordinate
    width: Size
    height: Size
    rotation: float = Field(default=0, strict=True, ge=0, lt=360, allow_inf_nan=False)


class MapDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schema_version: Literal[1] = 1
    width: Size
    height: Size
    elements: list[MapElement] = Field(max_length=200)

    @model_validator(mode='after')
    def validate_elements(self):
        ids = [item.id for item in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError('Cada figura debe tener un identificador distinto')
        for item in self.elements:
            # Bounding box conservadora de la figura girada sobre su centro.
            angle = math.radians(item.rotation)
            half_w = (abs(math.cos(angle)) * item.width + abs(math.sin(angle)) * item.height) / 2
            half_h = (abs(math.sin(angle)) * item.width + abs(math.cos(angle)) * item.height) / 2
            cx, cy = item.x + item.width / 2, item.y + item.height / 2
            if (cx - half_w < -1e-7 or cy - half_h < -1e-7
                    or cx + half_w > self.width + 1e-7
                    or cy + half_h > self.height + 1e-7):
                raise ValueError('Las figuras deben quedar dentro del área, incluso al girarlas')
        return self


class DraftSave(BaseModel):
    model_config = ConfigDict(extra='forbid')

    base_version: int = Field(strict=True, ge=0, le=2147483646)
    document: MapDocument


class DraftResponse(BaseModel):
    branch_id: UUID
    version: int
    document: MapDocument | None
    updated_by: UUID | None = None
    updated_at: datetime | None = None


def require_editor(user):
    if not can_edit_map(user):
        raise HTTPException(403, 'Solo el propietario, gerente o gerencia pueden editar el mapa')


def _response(branch_id, row):
    return DraftResponse(
        branch_id=branch_id, version=row['draft_version'] if row else 0,
        document=row['draft_document'] if row else None,
        updated_by=row.get('draft_updated_by') if row else None,
        updated_at=row.get('draft_updated_at') if row else None)


def create_router(current_user, get_connection):
    router = APIRouter(tags=['Store map'])

    @router.get('/branches/{branch_id}/map/draft', response_model=DraftResponse)
    def read_draft(branch_id: UUID, response: Response, user=Depends(current_user)):
        require_editor(user)
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            row = connection.execute(
                '''SELECT draft_document,draft_version,draft_updated_by,draft_updated_at
                   FROM store_map WHERE branch_id=%s''', (branch_id,)).fetchone()
        response.headers['Cache-Control'] = 'no-store'
        return _response(branch_id, row)

    @router.put('/branches/{branch_id}/map/draft', response_model=DraftResponse)
    def save_draft(branch_id: UUID, body: DraftSave, response: Response,
                   user=Depends(current_user)):
        require_editor(user)
        document = body.document.model_dump(mode='json')
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            # Serializa también dos primeras escrituras concurrentes. La fila
            # nueva solo contiene borrador; no inventa dimensiones publicadas.
            connection.execute(
                'INSERT INTO store_map(branch_id) VALUES(%s) ON CONFLICT(branch_id) DO NOTHING',
                (branch_id,))
            before = connection.execute(
                '''SELECT draft_document,draft_version,draft_updated_by,draft_updated_at
                   FROM store_map WHERE branch_id=%s FOR UPDATE''', (branch_id,)).fetchone()
            if before['draft_version'] != body.base_version:
                raise HTTPException(409, 'El borrador cambió en otra sesión. Recárgalo antes de guardar')
            # Un reintento con el contenido ya guardado y su versión vigente
            # no crea una revisión ni otra entrada en la bitácora.
            if before['draft_document'] == document:
                row = before
            else:
                row = connection.execute(
                    '''UPDATE store_map SET draft_document=%s,draft_version=draft_version+1,
                           draft_updated_by=%s,draft_updated_at=NOW()
                       WHERE branch_id=%s
                       RETURNING draft_document,draft_version,draft_updated_by,draft_updated_at''',
                    (Jsonb(document), user['id'], branch_id)).fetchone()
                record_change(connection, branch_id, user, 'map.draft_saved',
                              before={'version': before['draft_version'],
                                      'document': before['draft_document']},
                              after={'version': row['draft_version'], 'document': document},
                              message='Borrador del mapa guardado; todavía no está publicado')
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return _response(branch_id, row)

    return router

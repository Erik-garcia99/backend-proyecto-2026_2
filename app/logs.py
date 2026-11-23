"""S-04 / S-11: bitácora filtrable y exportable, e historial de cambios.

``system_log`` ya existe y ya recibe eventos de alertas, accesos y configuración
física. Aquí solo se lee y se ofrece un helper para registrar cambios con
valores anteriores/nuevos. No crea otra tabla ni otro cliente HTTP.
"""
import csv
import io
import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg.types.json import Jsonb

from .permissions import require_branch_actor

CATEGORIES = ('configuration', 'alert', 'security', 'access', 'error')
SEVERITIES = ('info', 'warning', 'critical')


def record_change(connection, branch_id, user, event_type, *, node_id=None,
                  before=None, after=None, message='Cambio guardado en el sistema'):
    """Registro S-11: quién cambió qué, con estado anterior y nuevo."""
    connection.execute(
        """INSERT INTO system_log(branch_id,user_id,node_id,category,severity,
                                   event_type,message,metadata)
           VALUES(%s,%s,%s,'configuration','info',%s,%s,%s)""",
        (branch_id, user['id'], node_id, event_type, message,
         Jsonb({'before': before, 'after': after,
                'application_status': 'not_integrated'})))


def _serialize(row):
    item = dict(row)
    for key in ('id', 'branch_id', 'gateway_id', 'node_id', 'user_id'):
        if item.get(key) is not None:
            item[key] = str(item[key])
    for key in ('created_at',):
        if item.get(key) is not None:
            item[key] = item[key].isoformat() if isinstance(item[key], datetime) else str(item[key])
    return item


def _where(branch_id, since, until, category, severity, node_id, event_type):
    if since and until and since > until:
        raise HTTPException(422, 'El inicio debe ser anterior al final')
    if any(value and value.tzinfo is None for value in (since, until)):
        raise HTTPException(422, 'Las fechas deben incluir zona horaria')
    clauses, args = ['branch_id=%s'], [branch_id]
    for column, value in (('category', category), ('severity', severity),
                          ('node_id', node_id), ('event_type', event_type)):
        if value is not None:
            clauses.append(f'{column}=%s')
            args.append(value)
    for operator, value in (('>=', since), ('<=', until)):
        if value:
            clauses.append(f'created_at {operator} %s')
            args.append(value)
    return ' AND '.join(clauses), tuple(args)


def create_router(current_user, get_connection):
    router = APIRouter(tags=['Logs'])

    @router.get('/branches/{branch_id}/logs')
    def list_logs(branch_id: UUID, response: Response,
                  since: datetime | None = None, until: datetime | None = None,
                  category: str | None = None, severity: str | None = None,
                  node_id: UUID | None = None, event_type: str | None = None,
                  offset: int = Query(0, ge=0, le=100000),
                  limit: int = Query(50, ge=1, le=200), user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            where, args = _where(branch_id, since, until, category, severity,
                                 node_id, event_type)
            rows = connection.execute(
                f"""SELECT * FROM system_log WHERE {where}
                    ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s""",
                args + (limit + 1, offset)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return {'items': [_serialize(row) for row in rows[:limit]],
                'has_more': len(rows) > limit}

    @router.get('/branches/{branch_id}/logs/export')
    def export_logs(branch_id: UUID,
                    since: datetime | None = None, until: datetime | None = None,
                    category: str | None = None, severity: str | None = None,
                    node_id: UUID | None = None, event_type: str | None = None,
                    user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            where, args = _where(branch_id, since, until, category, severity,
                                 node_id, event_type)
            rows = connection.execute(
                f"""SELECT * FROM system_log WHERE {where}
                    ORDER BY created_at DESC,id DESC LIMIT 5000""", args).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['fecha', 'categoria', 'severidad', 'evento',
                         'mensaje', 'nodo', 'usuario', 'detalle'])
        for row in rows:
            writer.writerow([
                _serialize(row).get('created_at') or '', row['category'],
                row['severity'], row['event_type'], row.get('message') or '',
                str(row['node_id']) if row.get('node_id') else '',
                str(row['user_id']) if row.get('user_id') else '',
                json.dumps(row.get('metadata'), ensure_ascii=False, default=str)
                if row.get('metadata') is not None else '',
            ])
        return Response(
            content='\ufeff' + output.getvalue(),
            media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': 'attachment; filename="bitacora.csv"'})

    @router.get('/branches/{branch_id}/logs/nodes/{node_id}')
    def node_history(branch_id: UUID, node_id: UUID, response: Response,
                     since: datetime | None = None, until: datetime | None = None,
                     offset: int = Query(0, ge=0, le=100000),
                     limit: int = Query(50, ge=1, le=200),
                     user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            where, args = _where(branch_id, since, until, None, None,
                                 node_id, None)
            rows = connection.execute(
                f"""SELECT * FROM system_log WHERE {where}
                    ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s""",
                args + (limit + 1, offset)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return {'items': [_serialize(row) for row in rows[:limit]],
                'has_more': len(rows) > limit}

    return router

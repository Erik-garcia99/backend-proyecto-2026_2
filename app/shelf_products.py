"""M8 / ME-7 — repisas como entidad propia y asociación configurada de productos.

La asociación ``shelf_product`` es configuración previa (qué producto se espera
en cada repisa). No es inventario ni disponibilidad visual: la ocupación sigue
saliendo de ROI/InfluxDB. Por eso el cliente nunca recibe estas filas crudas.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from .devices import ShelfCreate
from .permissions import require_branch_actor

log = logging.getLogger(__name__)


class ProductLink(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    product_id: UUID


PRODUCT_COLUMNS = '''
    SELECT p.id, p.sku, p.name,
           CASE WHEN spec.config_version > 0 AND spec.width_mm IS NOT NULL
                     AND spec.height_mm IS NOT NULL AND spec.depth_mm IS NOT NULL
                     AND spec.usual_orientation IS NOT NULL
                THEN TRUE ELSE FALSE END AS physical_configured
      FROM shelf_product sp
      JOIN product p ON p.id=sp.product_id AND p.is_active
      LEFT JOIN product_physical_spec spec ON spec.product_id=p.id
'''


def _shelf(row):
    return {'id': str(row['id']), 'name': row['name'],
            'location': row.get('location_label'),
            'cameraNodeId': str(row['camera_node_id']) if row.get('camera_node_id') else None,
            'active': bool(row.get('is_active', True))}


def _product(row):
    return {'id': str(row['id']), 'sku': row['sku'], 'name': row['name'],
            'physicalConfigured': bool(row['physical_configured'])}


def create_router(current_user, get_connection):
    router = APIRouter(tags=['Shelves'])

    @router.post('/branches/{branch_id}/shelves', status_code=201)
    def create_shelf(branch_id: UUID, body: ShelfCreate, response: Response,
                     user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            row = connection.execute(
                '''INSERT INTO shelf(branch_id, name, location_label)
                   VALUES(%s,%s,%s)
                   RETURNING id, name, location_label, camera_node_id, is_active''',
                (branch_id, body.name, body.location_label)).fetchone()
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return _shelf(row)

    @router.get('/branches/{branch_id}/shelves/{shelf_id}')
    def read_shelf(branch_id: UUID, shelf_id: UUID, response: Response,
                   user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True, staff=True)
            row = connection.execute(
                '''SELECT id, name, location_label, camera_node_id, is_active
                     FROM shelf WHERE id=%s AND branch_id=%s AND is_active''',
                (shelf_id, branch_id)).fetchone()
            if not row:
                raise HTTPException(404, 'Repisa no encontrada en esta sucursal')
            products = connection.execute(
                PRODUCT_COLUMNS + ' WHERE sp.shelf_id=%s AND sp.branch_id=%s ORDER BY p.name, p.sku',
                (shelf_id, branch_id)).fetchall()
            lights = connection.execute(
                '''SELECT id, name, led_index, node_id FROM led_guide_point
                    WHERE shelf_id=%s ORDER BY led_index''', (shelf_id,)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return {**_shelf(row),
                'products': [_product(product) for product in products],
                'lights': [{'id': str(light['id']), 'name': light['name'],
                            'channel': int(light['led_index']),
                            'nodeId': str(light['node_id'])} for light in lights]}

    @router.delete('/branches/{branch_id}/shelves/{shelf_id}')
    def delete_shelf(branch_id: UUID, shelf_id: UUID, response: Response,
                     user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            row = connection.execute(
                'SELECT id FROM shelf WHERE id=%s AND branch_id=%s AND is_active',
                (shelf_id, branch_id)).fetchone()
            if not row:
                raise HTTPException(404, 'Repisa no encontrada en esta sucursal')
            linked = connection.execute(
                '''SELECT (SELECT COUNT(*) FROM roi WHERE shelf_id=%s AND is_active)
                          + (SELECT COUNT(*) FROM led_guide_point WHERE shelf_id=%s) AS total''',
                (shelf_id, shelf_id)).fetchone()
            if linked and int(linked['total'] or 0) > 0:
                raise HTTPException(409, 'La repisa todavía tiene zonas o luces asociadas; retíralas antes de borrarla')
            connection.execute('DELETE FROM shelf_product WHERE shelf_id=%s', (shelf_id,))
            connection.execute(
                'UPDATE shelf SET is_active=FALSE, updated_at=NOW() WHERE id=%s', (shelf_id,))
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return {'id': str(shelf_id), 'deleted': True}

    @router.get('/branches/{branch_id}/shelves/{shelf_id}/products')
    def list_products(branch_id: UUID, shelf_id: UUID, response: Response,
                      user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True, staff=True)
            shelf = connection.execute(
                'SELECT id FROM shelf WHERE id=%s AND branch_id=%s AND is_active',
                (shelf_id, branch_id)).fetchone()
            if not shelf:
                raise HTTPException(404, 'Repisa no encontrada en esta sucursal')
            rows = connection.execute(
                PRODUCT_COLUMNS + ' WHERE sp.shelf_id=%s AND sp.branch_id=%s ORDER BY p.name, p.sku',
                (shelf_id, branch_id)).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return [_product(row) for row in rows]

    @router.post('/branches/{branch_id}/shelves/{shelf_id}/products', status_code=201)
    def add_product(branch_id: UUID, shelf_id: UUID, body: ProductLink,
                    response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            shelf = connection.execute(
                'SELECT id FROM shelf WHERE id=%s AND branch_id=%s AND is_active',
                (shelf_id, branch_id)).fetchone()
            if not shelf:
                raise HTTPException(404, 'Repisa no encontrada en esta sucursal')
            product = connection.execute(
                'SELECT id, name, sku FROM product WHERE id=%s AND organization_id=%s AND is_active',
                (body.product_id, branch['organization_id'])).fetchone()
            if not product:
                raise HTTPException(404, 'Producto no encontrado en esta sucursal')
            try:
                connection.execute(
                    '''INSERT INTO shelf_product(shelf_id, product_id, branch_id, created_by)
                       VALUES(%s,%s,%s,%s)''',
                    (shelf_id, body.product_id, branch_id, user['id']))
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                response.status_code = 200
        response.headers['Cache-Control'] = 'no-store'
        return {'id': str(product['id']), 'name': product['name'], 'sku': product['sku']}

    @router.delete('/branches/{branch_id}/shelves/{shelf_id}/products/{product_id}')
    def remove_product(branch_id: UUID, shelf_id: UUID, product_id: UUID,
                       response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            connection.execute(
                'DELETE FROM shelf_product WHERE shelf_id=%s AND product_id=%s AND branch_id=%s',
                (shelf_id, product_id, branch_id))
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return {'id': str(product_id), 'deleted': True}

    return router

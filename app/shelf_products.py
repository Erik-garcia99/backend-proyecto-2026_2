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
from .logs import record_change
from .permissions import require_branch_actor

log = logging.getLogger(__name__)


class ProductLink(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    product_id: UUID


PRODUCT_COLUMNS = '''
    SELECT p.id, p.sku, p.name, p.is_active,
           CASE WHEN spec.config_version > 0 AND spec.width_mm IS NOT NULL
                     AND spec.height_mm IS NOT NULL AND spec.depth_mm IS NOT NULL
                     AND spec.usual_orientation IS NOT NULL
                THEN TRUE ELSE FALSE END AS physical_configured
      FROM shelf_product sp
      JOIN product p ON p.id=sp.product_id
      LEFT JOIN product_physical_spec spec ON spec.product_id=p.id
'''


def _shelf(row):
    return {'id': str(row['id']), 'name': row['name'],
            'location': row.get('location_label'),
            'cameraNodeId': str(row['camera_node_id']) if row.get('camera_node_id') else None,
            'cameraName': row.get('camera_name'),
            'active': bool(row.get('is_active', True))}


def _product(row):
    # Un producto dado de baja sigue visible en staff como "Inactivo": así se
    # puede retirar de la repisa en vez de desaparecer en silencio.
    return {'id': str(row['id']), 'sku': row['sku'], 'name': row['name'],
            'active': bool(row.get('is_active', True)),
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
            record_change(connection, branch_id, user, 'shelf.created',
                          before=None, after={'id': str(row['id']), 'name': row['name'],
                                              'location': row['location_label']})
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return _shelf(row)

    @router.get('/branches/{branch_id}/shelves/{shelf_id}')
    def read_shelf(branch_id: UUID, shelf_id: UUID, response: Response,
                   user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True, staff=True)
            row = connection.execute(
                '''SELECT s.id, s.name, s.location_label, s.camera_node_id, s.is_active,
                          cam.name AS camera_name
                     FROM shelf s
                     LEFT JOIN esp32_node cam ON cam.id=s.camera_node_id
                    WHERE s.id=%s AND s.branch_id=%s AND s.is_active''',
                (shelf_id, branch_id)).fetchone()
            if not row:
                raise HTTPException(404, 'Repisa no encontrada en esta sucursal')
            products = connection.execute(
                PRODUCT_COLUMNS + ' WHERE sp.shelf_id=%s AND sp.branch_id=%s ORDER BY p.name, p.sku',
                (shelf_id, branch_id)).fetchall()
            lights = connection.execute(
                '''SELECT id, name, led_index, node_id FROM led_guide_point
                    WHERE shelf_id=%s ORDER BY led_index''', (shelf_id,)).fetchall()
            light_products = connection.execute(
                '''SELECT gpl.guide_point_id, p.id, p.name, p.sku
                     FROM guide_product_light gpl
                     JOIN led_guide_point gp ON gp.id=gpl.guide_point_id
                     JOIN product p ON p.id=gpl.product_id AND p.is_active
                    WHERE gpl.branch_id=%s AND gp.shelf_id=%s
                    ORDER BY p.name, p.sku''',
                (branch_id, shelf_id)).fetchall()
        products_by_light = {}
        for item in light_products:
            products_by_light.setdefault(str(item['guide_point_id']), []).append(
                {'id': str(item['id']), 'name': item['name'], 'sku': item['sku']})
        response.headers['Cache-Control'] = 'no-store'
        return {**_shelf(row),
                'products': [_product(product) for product in products],
                'lights': [{'id': str(light['id']), 'name': light['name'],
                            'channel': int(light['led_index']),
                            'nodeId': str(light['node_id']),
                            'products': products_by_light.get(str(light['id']), [])}
                           for light in lights]}

    @router.delete('/branches/{branch_id}/shelves/{shelf_id}')
    def delete_shelf(branch_id: UUID, shelf_id: UUID, response: Response,
                     user=Depends(current_user)):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            row = connection.execute(
                'SELECT id,name,location_label FROM shelf WHERE id=%s AND branch_id=%s AND is_active',
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
            record_change(connection, branch_id, user, 'shelf.deleted',
                          before={'id': str(row['id']), 'name': row.get('name'),
                                  'location': row.get('location_label')}, after=None)
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
                record_change(connection, branch_id, user, 'shelf.product_added',
                              before=None, after={'shelf_id': str(shelf_id),
                                                  'product_id': str(body.product_id),
                                                  'name': product['name']})
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                response.status_code = 200
        response.headers['Cache-Control'] = 'no-store'
        return {'id': str(product['id']), 'name': product['name'], 'sku': product['sku']}

    @router.delete('/branches/{branch_id}/shelves/{shelf_id}/products/{product_id}')
    def remove_product(branch_id: UUID, shelf_id: UUID, product_id: UUID,
                       response: Response, user=Depends(current_user),
                       with_light: bool = False):
        with get_connection() as connection:
            require_branch_actor(connection, branch_id, user, admin=True)
            linked = connection.execute(
                '''SELECT 1 FROM guide_product_light lpl
                    JOIN led_guide_point gp ON gp.id=lpl.guide_point_id
                   WHERE lpl.branch_id=%s AND lpl.product_id=%s AND gp.shelf_id=%s
                   LIMIT 1''',
                (branch_id, product_id, shelf_id)).fetchone()
            if linked and not with_light:
                raise HTTPException(
                    409, 'Este producto tiene un LED asociado en la repisa; retira la asociación antes de quitarlo.')
            connection.execute(
                'DELETE FROM shelf_product WHERE shelf_id=%s AND product_id=%s AND branch_id=%s',
                (shelf_id, product_id, branch_id))
            record_change(connection, branch_id, user, 'shelf.product_removed',
                          before={'shelf_id': str(shelf_id), 'product_id': str(product_id)},
                          after=None, message='Producto retirado de la repisa')
            connection.commit()
        response.headers['Cache-Control'] = 'no-store'
        return {'id': str(product_id), 'deleted': True}

    return router

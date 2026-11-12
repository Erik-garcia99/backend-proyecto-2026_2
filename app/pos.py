"""POS: catálogo vendible, inventario y venta transaccional.

Prefijo real: /api/v1/branches/{branch_id}/...
Fase 0-3 del plan POS.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from .schemas_pos import (
    AdjustmentRequest,
    GoodsReceiptCreate,
    ReturnRequest,
    SaleCreate,
    VoidRequest,
)

MONEY = Decimal('0.01')
MOVEMENT_TYPES = ['sale', 'sale_void', 'return', 'receipt', 'adjustment_in', 'adjustment_out']


class PosError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def error_response(exc: PosError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={
        'code': exc.code,
        'message': exc.message,
        'details': exc.details,
        'request_id': str(uuid4()),
    })


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _iso(value):
    return value.isoformat() if value is not None else None


def _body_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def create_router(current_user, get_connection):
    router = APIRouter(prefix='/api/v1', tags=['POS'])

    def branch_org(cursor, user, branch_id):
        row = cursor.execute('SELECT organization_id FROM branch WHERE id=%s AND is_active', (branch_id,)).fetchone()
        if not row:
            raise PosError(404, 'BRANCH_NOT_FOUND', 'Sucursal no encontrada.')
        if not user['is_superuser'] and str(row['organization_id']) != str(user['organization_id']):
            raise PosError(404, 'BRANCH_NOT_FOUND', 'Sucursal no encontrada.')
        return row['organization_id']

    def product_row(cursor, product_id, org_id):
        row = cursor.execute(
            '''SELECT id, sku, name, barcode, sale_price, tax_rate
               FROM product WHERE id=%s AND organization_id=%s AND is_active''',
            (product_id, org_id)).fetchone()
        if not row:
            raise PosError(422, 'PRODUCT_NOT_FOUND', 'Producto no encontrado.', {'product_id': str(product_id)})
        return row

    @router.get('/branches/{branch_id}/products')
    def search_products(branch_id: UUID, search: str = Query(default=''), limit: int = Query(default=50, ge=1, le=100),
                        user=Depends(current_user)):
        with get_connection() as conn:
            with conn.cursor() as cursor:
                org_id = branch_org(cursor, user, branch_id)
                rows = cursor.execute(
                    '''SELECT id, sku, name, barcode, sale_price, tax_rate
                       FROM product
                       WHERE organization_id=%s AND is_active
                         AND (%s='' OR name ILIKE %s OR sku ILIKE %s OR barcode ILIKE %s)
                       ORDER BY name LIMIT %s''',
                    (org_id, search, f'%{search}%', f'%{search}%', f'%{search}%', limit)).fetchall()
        return [dict(r) for r in rows]

    @router.get('/branches/{branch_id}/products/lookup')
    def lookup_product(branch_id: UUID, barcode: str = Query(..., min_length=1), user=Depends(current_user)):
        with get_connection() as conn:
            with conn.cursor() as cursor:
                org_id = branch_org(cursor, user, branch_id)
                row = cursor.execute(
                    '''SELECT id, sku, name, barcode, sale_price, tax_rate
                       FROM product
                       WHERE organization_id=%s AND is_active AND (barcode=%s OR sku=%s)
                       LIMIT 1''', (org_id, barcode, barcode)).fetchone()
        if not row:
            raise PosError(404, 'PRODUCT_NOT_FOUND', 'Producto no encontrado.', {'barcode': barcode})
        return dict(row)

    @router.get('/branches/{branch_id}/inventory')
    def list_inventory(branch_id: UUID, search: str = Query(default=''), limit: int = Query(default=100, ge=1, le=500),
                       user=Depends(current_user)):
        with get_connection() as conn:
            with conn.cursor() as cursor:
                branch_org(cursor, user, branch_id)
                rows = cursor.execute(
                    '''SELECT bi.product_id, p.sku, p.name, p.barcode,
                              bi.quantity_on_hand, bi.updated_at
                       FROM branch_inventory bi JOIN product p ON p.id=bi.product_id
                       WHERE bi.branch_id=%s
                         AND (%s='' OR p.name ILIKE %s OR p.sku ILIKE %s OR p.barcode ILIKE %s)
                       ORDER BY p.name LIMIT %s''',
                    (branch_id, search, f'%{search}%', f'%{search}%', f'%{search}%', limit)).fetchall()
        return [dict(r) for r in rows]

    @router.get('/branches/{branch_id}/inventory/{product_id}')
    def get_inventory(branch_id: UUID, product_id: UUID, user=Depends(current_user)):
        with get_connection() as conn:
            with conn.cursor() as cursor:
                branch_org(cursor, user, branch_id)
                row = cursor.execute(
                    '''SELECT bi.product_id, p.sku, p.name, p.barcode,
                              bi.quantity_on_hand, bi.updated_at
                       FROM branch_inventory bi JOIN product p ON p.id=bi.product_id
                       WHERE bi.branch_id=%s AND bi.product_id=%s''',
                    (branch_id, product_id)).fetchone()
        if not row:
            raise PosError(404, 'INVENTORY_NOT_FOUND', 'Inventario no encontrado.')
        return dict(row)

    @router.post('/branches/{branch_id}/sales', status_code=201)
    def create_sale(branch_id: UUID, request: SaleCreate,
                    idempotency_key: str = Header(..., alias='Idempotency-Key'),
                    user=Depends(current_user)):
        body = request.model_dump(mode='json')
        digest = _body_hash(body)
        with get_connection() as conn:
            with conn.cursor() as cursor:
                try:
                    org_id = branch_org(cursor, user, branch_id)
                    inserted = cursor.execute(
                        '''INSERT INTO idempotency_key (branch_id, key, user_id, body_hash, status)
                           VALUES (%s,%s,%s,%s,'pending')
                           ON CONFLICT (branch_id, key) DO NOTHING RETURNING id''',
                        (branch_id, idempotency_key, user['id'], digest)).fetchone()
                    if not inserted:
                        existing = cursor.execute(
                            '''SELECT body_hash, response, status FROM idempotency_key
                               WHERE branch_id=%s AND key=%s''',
                            (branch_id, idempotency_key)).fetchone()
                        if existing and existing['body_hash'] != digest:
                            raise PosError(409, 'IDEMPOTENCY_CONFLICT',
                                           'La clave de idempotencia ya se usó con otro contenido.')
                        if existing and existing['status'] == 'completed' and existing['response']:
                            return JSONResponse(status_code=201, content=existing['response'])
                        raise PosError(409, 'SALE_IN_PROGRESS', 'La venta con esta clave está en proceso.')

                    # Bloquear inventario en orden de producto para evitar deadlocks.
                    product_ids = sorted({str(item.product_id) for item in request.items})
                    products = {}
                    for pid in product_ids:
                        row = product_row(cursor, UUID(pid), org_id)
                        products[str(row['id'])] = row

                    quantities = {}
                    for item in request.items:
                        key = str(item.product_id)
                        quantities[key] = quantities.get(key, 0) + item.quantity

                    stock = cursor.execute(
                        '''SELECT product_id, quantity_on_hand FROM branch_inventory
                           WHERE branch_id=%s AND product_id = ANY(%s)
                           ORDER BY product_id FOR UPDATE''',
                        (branch_id, [UUID(pid) for pid in sorted(quantities)])).fetchall()
                    stock_by_product = {str(r['product_id']): r['quantity_on_hand'] for r in stock}

                    for pid, qty in quantities.items():
                        available = stock_by_product.get(pid, 0)
                        if available < qty:
                            raise PosError(409, 'INSUFFICIENT_STOCK', 'No hay existencias suficientes.',
                                           {'product_id': pid, 'available': available, 'requested': qty})

                    subtotal = Decimal('0')
                    tax_total = Decimal('0')
                    lines = []
                    for item in request.items:
                        product = products[str(item.product_id)]
                        unit_price = _money(product['sale_price'])
                        line_subtotal = _money(unit_price * item.quantity)
                        line_tax = _money(line_subtotal * Decimal(str(product['tax_rate'])))
                        subtotal += line_subtotal
                        tax_total += line_tax
                        lines.append((item, product, unit_price, line_subtotal))

                    total = _money(subtotal + tax_total)
                    payment = request.payment
                    if payment.method == 'cash' and payment.amount_received is not None:
                        if _money(payment.amount_received) < total:
                            raise PosError(422, 'INSUFFICIENT_PAYMENT',
                                           'El monto recibido es menor al total.')

                    sale_id = str(uuid4())
                    sale_number = f"V-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid4().hex[:6].upper()}"
                    sale = cursor.execute(
                        '''INSERT INTO sale (id, branch_id, sale_number, cashier_id, customer_id,
                                            subtotal, tax, discount, total, payment_method, status, notes)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s,%s,'completed',%s)
                           RETURNING id, sale_number, status, subtotal, tax, discount, total,
                                     payment_method, customer_id, created_at''',
                        (sale_id, branch_id, sale_number, user['id'], request.customer_id,
                         subtotal, tax_total, total, payment.method, request.notes)).fetchone()

                    out_items = []
                    for item, product, unit_price, line_subtotal in lines:
                        item_row = cursor.execute(
                            '''INSERT INTO sale_item (sale_id, product_id, quantity, unit_price, subtotal)
                               VALUES (%s,%s,%s,%s,%s)
                               RETURNING id, product_id, quantity, unit_price, subtotal''',
                            (sale_id, item.product_id, item.quantity, unit_price, line_subtotal)).fetchone()
                        cursor.execute(
                            '''UPDATE branch_inventory
                               SET quantity_on_hand=quantity_on_hand-%s, updated_at=NOW()
                               WHERE branch_id=%s AND product_id=%s''',
                            (item.quantity, branch_id, item.product_id))
                        after = stock_by_product.get(str(item.product_id), 0) - item.quantity
                        cursor.execute(
                            '''INSERT INTO inventory_movement
                                 (branch_id, product_id, movement_type, quantity_delta,
                                  quantity_after, reason, sale_id, created_by)
                               VALUES (%s,%s,'sale',%s,%s,'Venta POS',%s,%s)''',
                            (branch_id, item.product_id, -item.quantity, after, sale_id, user['id']))
                        stock_by_product[str(item.product_id)] = after
                        out_items.append({**dict(item_row), 'sku': product['sku'], 'name': product['name']})

                    response = {
                        'id': str(sale['id']),
                        'sale_number': sale['sale_number'],
                        'status': sale['status'],
                        'subtotal': float(sale['subtotal']),
                        'tax': float(sale['tax']),
                        'discount': float(sale['discount']),
                        'total': float(sale['total']),
                        'payment_method': sale['payment_method'],
                        'customer_id': str(sale['customer_id']) if sale['customer_id'] else None,
                        'created_at': _iso(sale['created_at']),
                        'items': [{
                            **{k: (str(v) if k in {'id', 'product_id'} else v) for k, v in item.items()},
                            'unit_price': float(item['unit_price']),
                            'subtotal': float(item['subtotal']),
                        } for item in out_items],
                    }
                    cursor.execute(
                        '''UPDATE idempotency_key SET response=%s, status='completed'
                           WHERE branch_id=%s AND key=%s''',
                        (json.dumps(response), branch_id, idempotency_key))
                    return JSONResponse(status_code=201, content=response)
                except PosError:
                    conn.rollback()
                    raise

    @router.get('/branches/{branch_id}/sales')
    def list_sales(branch_id: UUID, limit: int = Query(default=50, ge=1, le=100),
                   before: str | None = Query(default=None), user=Depends(current_user)):
        with get_connection() as conn:
            with conn.cursor() as cursor:
                branch_org(cursor, user, branch_id)
                params = [branch_id]
                clause = ''
                if before:
                    clause = 'AND occurred_at < %s'
                    params.append(before)
                params.append(limit + 1)
                rows = cursor.execute(
                    f'''SELECT id, sale_number, status, subtotal, tax, discount, total,
                               payment_method, customer_id, occurred_at, created_at
                        FROM sale WHERE branch_id=%s {clause}
                        ORDER BY occurred_at DESC, id DESC LIMIT %s''', params).fetchall()
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = _iso(rows[-1]['occurred_at'])
        return {'items': [{
            **{k: (str(v) if k in {'id', 'customer_id'} else v) for k, v in dict(r).items()},
            'subtotal': float(r['subtotal']), 'tax': float(r['tax']),
            'discount': float(r['discount']), 'total': float(r['total']),
            'occurred_at': _iso(r['occurred_at']), 'created_at': _iso(r['created_at']),
        } for r in rows], 'next_cursor': next_cursor}

    @router.get('/branches/{branch_id}/sales/{sale_id}')
    def get_sale(branch_id: UUID, sale_id: UUID, user=Depends(current_user)):
        with get_connection() as conn:
            with conn.cursor() as cursor:
                branch_org(cursor, user, branch_id)
                sale = cursor.execute(
                    '''SELECT id, sale_number, status, subtotal, tax, discount, total,
                              payment_method, customer_id, occurred_at, created_at
                       FROM sale WHERE id=%s AND branch_id=%s''', (sale_id, branch_id)).fetchone()
                if not sale:
                    raise PosError(404, 'SALE_NOT_FOUND', 'Venta no encontrada.')
                items = cursor.execute(
                    '''SELECT si.id, si.product_id, p.sku, p.name, si.quantity, si.unit_price, si.subtotal
                       FROM sale_item si JOIN product p ON p.id=si.product_id
                       WHERE si.sale_id=%s ORDER BY si.id''', (sale_id,)).fetchall()
        return {
            'id': str(sale['id']), 'sale_number': sale['sale_number'], 'status': sale['status'],
            'subtotal': float(sale['subtotal']), 'tax': float(sale['tax']),
            'discount': float(sale['discount']), 'total': float(sale['total']),
            'payment_method': sale['payment_method'],
            'customer_id': str(sale['customer_id']) if sale['customer_id'] else None,
            'occurred_at': _iso(sale['occurred_at']), 'created_at': _iso(sale['created_at']),
            'items': [{
                **{k: (str(v) if k in {'id', 'product_id'} else v) for k, v in dict(i).items()},
                'unit_price': float(i['unit_price']), 'subtotal': float(i['subtotal']),
            } for i in items],
        }

    def require_manager(user):
        role = (user.get('role') or '').lower()
        if user.get('is_superuser') or role in {'owner', 'manager', 'gerencia', 'rh'}:
            return
        raise PosError(403, 'FORBIDDEN', 'No tienes permiso para esta acción.')

    def lock_inventory(cursor, branch_id, product_id):
        row = cursor.execute(
            '''SELECT quantity_on_hand FROM branch_inventory
               WHERE branch_id=%s AND product_id=%s FOR UPDATE''',
            (branch_id, product_id)).fetchone()
        return row['quantity_on_hand'] if row else 0

    def apply_inventory(cursor, branch_id, product_id, delta, movement_type, reason=None,
                        sale_id=None, goods_receipt_id=None, user_id=None):
        before = lock_inventory(cursor, branch_id, product_id)
        after = before + delta
        if after < 0:
            raise PosError(409, 'INSUFFICIENT_STOCK', 'No hay existencias suficientes.',
                           {'product_id': str(product_id), 'available': before, 'requested': -delta})
        cursor.execute(
            '''INSERT INTO branch_inventory (branch_id, product_id, quantity_on_hand)
               VALUES (%s,%s,%s)
               ON CONFLICT (branch_id, product_id)
               DO UPDATE SET quantity_on_hand=EXCLUDED.quantity_on_hand, updated_at=NOW()''',
            (branch_id, product_id, after))
        cursor.execute(
            '''INSERT INTO inventory_movement
                 (branch_id, product_id, movement_type, quantity_delta, quantity_after,
                  reason, sale_id, goods_receipt_id, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (branch_id, product_id, movement_type, delta, after, reason,
             sale_id, goods_receipt_id, user_id))
        return after

    @router.post('/branches/{branch_id}/sales/{sale_id}/void')
    def void_sale(branch_id: UUID, sale_id: UUID, request: VoidRequest, user=Depends(current_user)):
        require_manager(user)
        with get_connection() as conn:
            with conn.cursor() as cursor:
                try:
                    branch_org(cursor, user, branch_id)
                    sale = cursor.execute(
                        '''SELECT id, status FROM sale WHERE id=%s AND branch_id=%s FOR UPDATE''',
                        (sale_id, branch_id)).fetchone()
                    if not sale:
                        raise PosError(404, 'SALE_NOT_FOUND', 'Venta no encontrada.')
                    if sale['status'] == 'voided':
                        raise PosError(409, 'SALE_ALREADY_VOIDED', 'La venta ya fue anulada.')
                    items = cursor.execute(
                        'SELECT product_id, quantity FROM sale_item WHERE sale_id=%s', (sale_id,)).fetchall()
                    returned = {str(r['product_id']): r['quantity'] for r in cursor.execute(
                        '''SELECT product_id, SUM(quantity_delta) AS quantity FROM inventory_movement
                           WHERE sale_id=%s AND movement_type='return' GROUP BY product_id''', (sale_id,)).fetchall()}
                    for item in items:
                        qty = item['quantity'] - returned.get(str(item['product_id']), 0)
                        if qty <= 0:
                            continue
                        apply_inventory(cursor, branch_id, item['product_id'], qty, 'sale_void',
                                        request.reason, sale_id=sale_id, user_id=user['id'])
                    cursor.execute(
                        '''UPDATE sale SET status='voided', updated_at=NOW(),
                           notes=COALESCE(notes || E'\\n', '') || %s WHERE id=%s''',
                        (f"Anulada: {request.reason}", sale_id))
                    return {'id': str(sale_id), 'status': 'voided'}
                except PosError:
                    conn.rollback()
                    raise

    @router.post('/branches/{branch_id}/sales/{sale_id}/returns')
    def return_sale(branch_id: UUID, sale_id: UUID, request: ReturnRequest, user=Depends(current_user)):
        require_manager(user)
        with get_connection() as conn:
            with conn.cursor() as cursor:
                try:
                    branch_org(cursor, user, branch_id)
                    sale = cursor.execute(
                        'SELECT id, status FROM sale WHERE id=%s AND branch_id=%s FOR UPDATE',
                        (sale_id, branch_id)).fetchone()
                    if not sale:
                        raise PosError(404, 'SALE_NOT_FOUND', 'Venta no encontrada.')
                    if sale['status'] == 'voided':
                        raise PosError(409, 'SALE_ALREADY_VOIDED', 'No se puede devolver una venta anulada.')
                    sold = {str(r['product_id']): r['quantity'] for r in cursor.execute(
                        'SELECT product_id, SUM(quantity) AS quantity FROM sale_item WHERE sale_id=%s GROUP BY product_id',
                        (sale_id,)).fetchall()}
                    already = {str(r['product_id']): r['quantity'] for r in cursor.execute(
                        '''SELECT product_id, SUM(quantity_delta) AS quantity FROM inventory_movement
                           WHERE sale_id=%s AND movement_type='return' GROUP BY product_id''', (sale_id,)).fetchall()}
                    out = []
                    for item in request.items:
                        pid = str(item.product_id)
                        if pid not in sold:
                            raise PosError(422, 'PRODUCT_NOT_IN_SALE', 'El producto no pertenece a la venta.',
                                           {'product_id': pid})
                        if already.get(pid, 0) + item.quantity > sold[pid]:
                            raise PosError(422, 'RETURN_EXCEEDS_SOLD',
                                           'La devolución excede lo vendido.',
                                           {'product_id': pid, 'sold': sold[pid], 'returned': already.get(pid, 0)})
                        after = apply_inventory(cursor, branch_id, item.product_id, item.quantity, 'return',
                                                request.reason, sale_id=sale_id, user_id=user['id'])
                        already[pid] = already.get(pid, 0) + item.quantity
                        out.append({'product_id': pid, 'quantity': item.quantity, 'quantity_after': after})
                    return {'sale_id': str(sale_id), 'items': out}
                except PosError:
                    conn.rollback()
                    raise

    @router.post('/branches/{branch_id}/inventory/adjustments')
    def adjust_inventory(branch_id: UUID, request: AdjustmentRequest,
                         idempotency_key: str = Header(..., alias='Idempotency-Key'),
                         user=Depends(current_user)):
        require_manager(user)
        digest = _body_hash(request.model_dump(mode='json'))
        with get_connection() as conn:
            with conn.cursor() as cursor:
                try:
                    branch_org(cursor, user, branch_id)
                    inserted = cursor.execute(
                        '''INSERT INTO idempotency_key (branch_id, key, user_id, body_hash, status)
                           VALUES (%s,%s,%s,%s,'pending')
                           ON CONFLICT (branch_id, key) DO NOTHING RETURNING id''',
                        (branch_id, idempotency_key, user['id'], digest)).fetchone()
                    if not inserted:
                        existing = cursor.execute(
                            'SELECT body_hash, response, status FROM idempotency_key WHERE branch_id=%s AND key=%s',
                            (branch_id, idempotency_key)).fetchone()
                        if existing and existing['body_hash'] != digest:
                            raise PosError(409, 'IDEMPOTENCY_CONFLICT', 'La clave ya se usó con otro contenido.')
                        if existing and existing['status'] == 'completed' and existing['response']:
                            return JSONResponse(status_code=200, content=existing['response'])
                        raise PosError(409, 'ADJUSTMENT_IN_PROGRESS', 'El ajuste está en proceso.')
                    product_row(cursor, request.product_id, branch_org(cursor, user, branch_id))
                    movement_type = 'adjustment_in' if request.quantity_delta > 0 else 'adjustment_out'
                    after = apply_inventory(cursor, branch_id, request.product_id, request.quantity_delta,
                                            movement_type, request.reason, user_id=user['id'])
                    response = {'product_id': str(request.product_id), 'quantity_after': after,
                                'movement_type': movement_type}
                    cursor.execute(
                        '''UPDATE idempotency_key SET response=%s, status='completed'
                           WHERE branch_id=%s AND key=%s''',
                        (json.dumps(response), branch_id, idempotency_key))
                    return response
                except PosError:
                    conn.rollback()
                    raise

    def receipt_payload(cursor, receipt_id):
        receipt = cursor.execute(
            '''SELECT id, branch_id, supplier_id, received_by, status, notes, received_at
               FROM goods_receipt WHERE id=%s''', (receipt_id,)).fetchone()
        items = cursor.execute(
            '''SELECT gri.id, gri.product_id, p.sku, p.name, gri.declared_units, gri.received_units
               FROM goods_receipt_item gri JOIN product p ON p.id=gri.product_id
               WHERE gri.goods_receipt_id=%s ORDER BY gri.id''', (receipt_id,)).fetchall()
        return {
            'id': str(receipt['id']), 'status': receipt['status'], 'notes': receipt['notes'],
            'supplier_id': str(receipt['supplier_id']) if receipt['supplier_id'] else None,
            'received_at': _iso(receipt['received_at']),
            'items': [{**{k: (str(v) if k in {'id', 'product_id'} else v) for k, v in dict(i).items()}}
                      for i in items],
        }

    @router.post('/branches/{branch_id}/goods-receipts', status_code=201)
    def create_receipt(branch_id: UUID, request: GoodsReceiptCreate, user=Depends(current_user)):
        require_manager(user)
        with get_connection() as conn:
            with conn.cursor() as cursor:
                try:
                    org_id = branch_org(cursor, user, branch_id)
                    for item in request.items:
                        product_row(cursor, item.product_id, org_id)
                    receipt = cursor.execute(
                        '''INSERT INTO goods_receipt (branch_id, supplier_id, received_by, status, notes)
                           VALUES (%s,%s,%s,'draft',%s) RETURNING id''',
                        (branch_id, request.supplier_id, user['id'], request.notes)).fetchone()
                    for item in request.items:
                        cursor.execute(
                            '''INSERT INTO goods_receipt_item
                                 (goods_receipt_id, product_id, declared_units, received_units)
                               VALUES (%s,%s,%s,%s)''',
                            (receipt['id'], item.product_id, item.declared_units, item.received_units))
                    return receipt_payload(cursor, receipt['id'])
                except PosError:
                    conn.rollback()
                    raise

    @router.get('/branches/{branch_id}/goods-receipts')
    def list_receipts(branch_id: UUID, limit: int = Query(default=50, ge=1, le=100), user=Depends(current_user)):
        with get_connection() as conn:
            with conn.cursor() as cursor:
                branch_org(cursor, user, branch_id)
                rows = cursor.execute(
                    '''SELECT id FROM goods_receipt WHERE branch_id=%s
                       ORDER BY received_at DESC LIMIT %s''', (branch_id, limit)).fetchall()
                return [receipt_payload(cursor, row['id']) for row in rows]

    @router.post('/branches/{branch_id}/goods-receipts/{receipt_id}/confirm')
    def confirm_receipt(branch_id: UUID, receipt_id: UUID, user=Depends(current_user)):
        require_manager(user)
        with get_connection() as conn:
            with conn.cursor() as cursor:
                try:
                    branch_org(cursor, user, branch_id)
                    receipt = cursor.execute(
                        '''SELECT id, status FROM goods_receipt
                           WHERE id=%s AND branch_id=%s FOR UPDATE''',
                        (receipt_id, branch_id)).fetchone()
                    if not receipt:
                        raise PosError(404, 'RECEIPT_NOT_FOUND', 'Recepción no encontrada.')
                    if receipt['status'] == 'confirmed':
                        return receipt_payload(cursor, receipt_id)
                    items = cursor.execute(
                        '''SELECT product_id, received_units FROM goods_receipt_item
                           WHERE goods_receipt_id=%s''', (receipt_id,)).fetchall()
                    for item in items:
                        if item['received_units'] > 0:
                            apply_inventory(cursor, branch_id, item['product_id'], item['received_units'],
                                            'receipt', 'Recepción confirmada',
                                            goods_receipt_id=receipt_id, user_id=user['id'])
                    cursor.execute(
                        "UPDATE goods_receipt SET status='confirmed', received_at=NOW() WHERE id=%s",
                        (receipt_id,))
                    return receipt_payload(cursor, receipt_id)
                except PosError:
                    conn.rollback()
                    raise

    @router.get('/branches/{branch_id}/inventory/movements')
    def list_movements(branch_id: UUID, product_id: UUID | None = Query(default=None),
                       movement_type: str | None = Query(default=None),
                       date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
                       limit: int = Query(default=100, ge=1, le=500), user=Depends(current_user)):
        with get_connection() as conn:
            with conn.cursor() as cursor:
                branch_org(cursor, user, branch_id)
                clauses = ['m.branch_id=%s']
                params = [branch_id]
                if product_id:
                    clauses.append('m.product_id=%s'); params.append(product_id)
                if movement_type:
                    clauses.append('m.movement_type=%s'); params.append(movement_type)
                if date_from:
                    clauses.append('m.occurred_at >= %s'); params.append(date_from)
                if date_to:
                    clauses.append('m.occurred_at <= %s'); params.append(date_to)
                params.append(limit)
                rows = cursor.execute(
                    f'''SELECT m.id, m.product_id, p.sku, p.name, m.movement_type,
                               m.quantity_delta, m.quantity_after, m.reason, m.sale_id,
                               m.goods_receipt_id, m.occurred_at
                        FROM inventory_movement m JOIN product p ON p.id=m.product_id
                        WHERE {' AND '.join(clauses)}
                        ORDER BY m.occurred_at DESC LIMIT %s''', params).fetchall()
        return [{
            **{k: (str(v) if k in {'id', 'product_id', 'sale_id', 'goods_receipt_id'} and v else v)
               for k, v in dict(r).items()},
            'occurred_at': _iso(r['occurred_at']),
        } for r in rows]

    return router

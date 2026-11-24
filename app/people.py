"""F-02 (ME-9) — administración de personal: empleados, proveedores y clientes.

Reglas del requisito:

* Crear, consultar, editar y **desactivar** (baja lógica) registros.
* Solo roles administradores; la autorización real vive aquí, nunca en la UI.
* Email único (ya existe el índice ``uq_user_email_lower``) y campos obligatorios.
* Proveedores pueden asociarse a productos del catálogo de su organización.
* Los clientes conservan su historial de compras del POS (solo lectura).

No crea tablas ni roles: ``user``, ``supplier``, ``supplier_product`` y
``user_management`` ya están en el esquema actual.
"""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from .logs import record_change
from .permissions import can_approve_users, can_create_users, require_branch_actor
from .schemas import EMAIL_PATTERN
from .security import hash_password

# `employee` es el rol del personal desde el inicio (lo crea /auth/register).
# `staff` era un alias histórico: ya no se ofrece al dar de alta, pero se
# conserva en la lectura para no ocultar registros antiguos.
STAFF_ROLES_ALL = frozenset({'manager', 'rh', 'gerencia', 'employee', 'staff'})
ASSIGNABLE_ROLES = frozenset({'manager', 'rh', 'gerencia', 'employee'})

# `sudo` y `owner` quedan fuera: sudo es el administrador único de la
# plataforma y owner es quien la inicia; no se crean desde esta pantalla.


# --------------------------------------------------------------------------- #
# Contratos
# --------------------------------------------------------------------------- #
class PersonCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    email: str = Field(min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone_number: str | None = Field(default=None, max_length=30)
    role: str = Field(default='employee', max_length=30)


class CustomerCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    email: str = Field(min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone_number: str | None = Field(default=None, max_length=30)


class PersonUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    email: str | None = Field(default=None, min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    phone_number: str | None = Field(default=None, max_length=30)
    role: str | None = Field(default=None, max_length=30)


class SupplierCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    name: str = Field(min_length=2, max_length=150)
    contact_email: str | None = Field(default=None, max_length=320)
    whatsapp_number: str | None = Field(default=None, max_length=30)


class SupplierUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    name: str | None = Field(default=None, min_length=2, max_length=150)
    contact_email: str | None = Field(default=None, max_length=320)
    whatsapp_number: str | None = Field(default=None, max_length=30)


class SupplierProduct(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    product_id: UUID
    supplier_sku: str | None = Field(default=None, max_length=100)
    unit_cost: float | None = Field(default=None, ge=0)
    is_preferred: bool = False


class SupplierProducts(BaseModel):
    model_config = ConfigDict(extra='forbid')

    products: list[SupplierProduct] = Field(default_factory=list, max_length=500)


# --------------------------------------------------------------------------- #
# Serialización y consultas compartidas
# --------------------------------------------------------------------------- #
PERSON_COLUMNS = '''id, organization_id, branch_id, email, first_name, last_name,
                    phone_number, role, account_status, created_at, deactivated_at'''


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _person(row):
    return {
        'id': str(row['id']),
        'email': row['email'],
        'firstName': row['first_name'],
        'lastName': row['last_name'],
        'phoneNumber': row['phone_number'],
        'role': row['role'],
        'accountStatus': row['account_status'],
        'organizationId': str(row['organization_id']),
        'branchId': str(row['branch_id']) if row['branch_id'] else None,
        'createdAt': _iso(row['created_at']),
        'active': row['deactivated_at'] is None,
    }


def _supplier(row):
    return {
        'id': str(row['id']),
        'organizationId': str(row['organization_id']),
        'name': row['name'],
        'contactEmail': row['contact_email'],
        'whatsappNumber': row['whatsapp_number'],
        'active': bool(row['is_active']),
    }


def _get_person(connection, user_id, branch, *, role, include_inactive=False):
    clauses = ['id=%s', 'branch_id=%s' if role in STAFF_ROLES_ALL else 'branch_id IS NULL',
               'organization_id=%s', 'lower(role)=ANY(%s)']
    args = [user_id, branch['organization_id']]
    if role in STAFF_ROLES_ALL:
        args.insert(1, branch['id'])
    args.append([role] if role not in STAFF_ROLES_ALL else sorted(STAFF_ROLES_ALL))
    if not include_inactive:
        clauses.append('deactivated_at IS NULL')
    row = connection.execute(
        f'SELECT {PERSON_COLUMNS} FROM "user" WHERE ' + ' AND '.join(clauses),
        tuple(args)).fetchone()
    if not row:
        raise HTTPException(404, 'Registro no encontrado en esta sucursal')
    return row


def _get_supplier(connection, supplier_id, organization_id):
    row = connection.execute(
        '''SELECT id, organization_id, name, contact_email, whatsapp_number, is_active
             FROM supplier WHERE id=%s AND organization_id=%s AND is_active''',
        (supplier_id, organization_id)).fetchone()
    if not row:
        raise HTTPException(404, 'Proveedor no encontrado en esta empresa')
    return row


def _deactivate(connection, user_id):
    """Baja lógica: conserva el registro y revoca las sesiones abiertas."""
    connection.execute('''UPDATE "user" SET deactivated_at=NOW(), account_status='inactive',
                          updated_at=NOW() WHERE id=%s''', (user_id,))
    connection.execute('''UPDATE auth_session SET revoked_at=NOW()
                          WHERE user_id=%s AND revoked_at IS NULL''', (user_id,))


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'


def create_router(current_user, get_connection):
    router = APIRouter(tags=['People'])

    # ---- Empleados -------------------------------------------------------- #
    @router.get('/branches/{branch_id}/employees')
    def list_employees(branch_id: UUID, response: Response,
                       include_inactive: bool = Query(False),
                       user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            clauses = ['branch_id=%s', 'organization_id=%s', 'lower(role)=ANY(%s)']
            args = [branch['id'], branch['organization_id'], sorted(STAFF_ROLES_ALL)]
            if not include_inactive:
                clauses.append('deactivated_at IS NULL')
            rows = connection.execute(
                f'SELECT {PERSON_COLUMNS} FROM "user" WHERE ' + ' AND '.join(clauses) +
                ' ORDER BY first_name, last_name, email', tuple(args)).fetchall()
        _no_store(response)
        return {'items': [_person(row) for row in rows]}

    @router.post('/branches/{branch_id}/employees', status_code=201)
    def create_employee(branch_id: UUID, body: PersonCreate, response: Response,
                        user=Depends(current_user)):
        role = body.role.lower()
        if role not in ASSIGNABLE_ROLES:
            raise HTTPException(422, 'El rol debe ser manager, rh, gerencia o employee; owner, sudo y staff no se crean aquí')
        if not can_create_users(user):
            raise HTTPException(403, 'Solo owner, RRHH, gerente o gerencia pueden dar de alta personal')
        # Doble verificación: gerente/gerencia crean, pero owner/RRHH aprueban.
        status = 'active' if can_approve_users(user) else 'pending'
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            args = (branch['organization_id'], branch['id'], body.email.lower(),
                    body.first_name, body.last_name, body.phone_number or None,
                    hash_password(body.password), role, status)
            try:
                row = connection.execute(
                    f'''INSERT INTO "user" (organization_id, branch_id, email, first_name,
                           last_name, phone_number, password_hash, role, account_status)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING {PERSON_COLUMNS}''', args).fetchone()
                record_change(connection, branch_id, user, 'employee.created',
                              after={'id': str(row['id']), 'email': row['email'],
                                     'role': role, 'accountStatus': status})
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'El correo ya está registrado')
        _no_store(response)
        return _person(row)

    @router.get('/branches/{branch_id}/employees/{user_id}')
    def read_employee(branch_id: UUID, user_id: UUID, response: Response,
                      include_inactive: bool = Query(False), user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            row = _get_person(connection, user_id, branch, role='employee',
                              include_inactive=include_inactive)
        _no_store(response)
        return _person(row)

    @router.patch('/branches/{branch_id}/employees/{user_id}')
    def update_employee(branch_id: UUID, user_id: UUID, body: PersonUpdate,
                        response: Response, user=Depends(current_user)):
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if 'role' in changes:
            changes['role'] = changes['role'].lower()
            if changes['role'] not in ASSIGNABLE_ROLES:
                raise HTTPException(422, 'El rol debe ser manager, rh, gerencia o employee')
        if not changes:
            raise HTTPException(422, 'No se enviaron cambios')
        if 'email' in changes:
            changes['email'] = changes['email'].lower()
        assignments = ', '.join(f'{field}=%s' for field in changes)
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            before = _get_person(connection, user_id, branch, role='employee')
            try:
                updated = connection.execute(
                    f'UPDATE "user" SET {assignments}, updated_at=NOW() WHERE id=%s '
                    f'RETURNING {PERSON_COLUMNS}',
                    (*changes.values(), user_id)).fetchone()
                record_change(connection, branch_id, user, 'employee.updated',
                              before={'email': before['email'], 'role': before['role']},
                              after={**changes, 'id': str(user_id)})
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'El correo ya está registrado')
        _no_store(response)
        return _person(updated)

    @router.delete('/branches/{branch_id}/employees/{user_id}')
    def deactivate_employee(branch_id: UUID, user_id: UUID, response: Response,
                            user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            before = _get_person(connection, user_id, branch, role='employee')
            _deactivate(connection, user_id)
            record_change(connection, branch_id, user, 'employee.deactivated',
                          before={'email': before['email'], 'active': True},
                          after={'email': before['email'], 'active': False})
            connection.commit()
        _no_store(response)
        return {'message': 'Empleado dado de baja; su historial se conserva'}

    # ---- Clientes --------------------------------------------------------- #
    @router.get('/branches/{branch_id}/customers')
    def list_customers(branch_id: UUID, response: Response,
                       include_inactive: bool = Query(False), user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            clauses = ['organization_id=%s', "lower(role)='customer'"]
            args = [branch['organization_id']]
            if not include_inactive:
                clauses.append('deactivated_at IS NULL')
            rows = connection.execute(
                f'SELECT {PERSON_COLUMNS} FROM "user" WHERE ' + ' AND '.join(clauses) +
                ' ORDER BY first_name, last_name, email', tuple(args)).fetchall()
        _no_store(response)
        return {'items': [_person(row) for row in rows]}

    @router.post('/branches/{branch_id}/customers', status_code=201)
    def create_customer(branch_id: UUID, body: CustomerCreate, response: Response,
                        user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            try:
                row = connection.execute(
                    f'''INSERT INTO "user" (organization_id, branch_id, email, first_name,
                           last_name, phone_number, password_hash, role, account_status)
                        VALUES(%s,NULL,%s,%s,%s,%s,%s,'customer','active')
                        RETURNING {PERSON_COLUMNS}''',
                    (branch['organization_id'], body.email.lower(), body.first_name,
                     body.last_name, body.phone_number or None,
                     hash_password(body.password))).fetchone()
                record_change(connection, branch_id, user, 'customer.created',
                              after={'id': str(row['id']), 'email': row['email']})
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'El correo ya está registrado')
        _no_store(response)
        return _person(row)

    @router.get('/branches/{branch_id}/customers/{user_id}')
    def read_customer(branch_id: UUID, user_id: UUID, response: Response,
                      include_inactive: bool = Query(False), user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            row = _get_person(connection, user_id, branch, role='customer',
                              include_inactive=include_inactive)
        _no_store(response)
        return _person(row)

    @router.patch('/branches/{branch_id}/customers/{user_id}')
    def update_customer(branch_id: UUID, user_id: UUID, body: PersonUpdate,
                        response: Response, user=Depends(current_user)):
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        changes.pop('role', None)  # el rol de cliente nunca se cambia desde aquí
        if not changes:
            raise HTTPException(422, 'No se enviaron cambios')
        if 'email' in changes:
            changes['email'] = changes['email'].lower()
        assignments = ', '.join(f'{field}=%s' for field in changes)
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            _get_person(connection, user_id, branch, role='customer')
            try:
                updated = connection.execute(
                    f'UPDATE "user" SET {assignments}, updated_at=NOW() WHERE id=%s '
                    f'RETURNING {PERSON_COLUMNS}',
                    (*changes.values(), user_id)).fetchone()
                record_change(connection, branch_id, user, 'customer.updated',
                              after={**changes, 'id': str(user_id)})
                connection.commit()
            except UniqueViolation:
                connection.rollback()
                raise HTTPException(409, 'El correo ya está registrado')
        _no_store(response)
        return _person(updated)

    @router.delete('/branches/{branch_id}/customers/{user_id}')
    def deactivate_customer(branch_id: UUID, user_id: UUID, response: Response,
                            user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            before = _get_person(connection, user_id, branch, role='customer')
            _deactivate(connection, user_id)
            record_change(connection, branch_id, user, 'customer.deactivated',
                          before={'email': before['email'], 'active': True},
                          after={'email': before['email'], 'active': False})
            connection.commit()
        _no_store(response)
        return {'message': 'Cliente dado de baja; su historial de compras se conserva'}

    @router.get('/branches/{branch_id}/customers/{user_id}/purchases')
    def customer_purchases(branch_id: UUID, user_id: UUID, response: Response,
                           user=Depends(current_user)):
        """Historial POS del cliente. No se inventan compras ni se mezclan con ocupación."""
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            customer = _get_person(connection, user_id, branch, role='customer',
                                   include_inactive=True)
            sales = connection.execute(
                '''SELECT id, sale_number, occurred_at, total, status
                     FROM sale WHERE customer_id=%s AND branch_id=%s
                     ORDER BY occurred_at DESC, created_at DESC LIMIT 200''',
                (customer['id'], branch['id'])).fetchall()
            items = connection.execute(
                '''SELECT si.sale_id, si.quantity, si.unit_price, si.subtotal,
                          p.id AS product_id, p.name AS product_name, p.sku
                     FROM sale_item si JOIN sale s ON s.id=si.sale_id
                     JOIN product p ON p.id=si.product_id
                    WHERE s.customer_id=%s AND s.branch_id=%s''',
                (customer['id'], branch['id'])).fetchall()
        by_sale = {}
        for item in items:
            by_sale.setdefault(str(item['sale_id']), []).append({
                'productId': str(item['product_id']), 'name': item['product_name'],
                'sku': item['sku'], 'quantity': int(item['quantity']),
                'unitPrice': float(item['unit_price']), 'subtotal': float(item['subtotal']),
            })
        _no_store(response)
        return {'items': [{
            'id': str(sale['id']), 'saleNumber': sale['sale_number'],
            'occurredAt': _iso(sale['occurred_at']), 'total': float(sale['total']),
            'status': sale['status'],
            'items': by_sale.get(str(sale['id']), []),
        } for sale in sales]}

    # ---- Proveedores ------------------------------------------------------ #
    @router.get('/branches/{branch_id}/suppliers')
    def list_suppliers(branch_id: UUID, response: Response,
                       include_inactive: bool = Query(False), user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            sql = '''SELECT id, organization_id, name, contact_email, whatsapp_number, is_active
                       FROM supplier WHERE organization_id=%s'''
            args = [branch['organization_id']]
            if not include_inactive:
                sql += ' AND is_active'
            rows = connection.execute(sql + ' ORDER BY name', tuple(args)).fetchall()
        _no_store(response)
        return {'items': [_supplier(row) for row in rows]}

    @router.post('/branches/{branch_id}/suppliers', status_code=201)
    def create_supplier(branch_id: UUID, body: SupplierCreate, response: Response,
                        user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            row = connection.execute(
                '''INSERT INTO supplier(organization_id, name, contact_email, whatsapp_number)
                   VALUES(%s,%s,%s,%s)
                   RETURNING id, organization_id, name, contact_email, whatsapp_number, is_active''',
                (branch['organization_id'], body.name, body.contact_email,
                 body.whatsapp_number)).fetchone()
            record_change(connection, branch_id, user, 'supplier.created',
                          after={'id': str(row['id']), 'name': row['name']})
            connection.commit()
        _no_store(response)
        return _supplier(row)

    @router.get('/branches/{branch_id}/suppliers/{supplier_id}')
    def read_supplier(branch_id: UUID, supplier_id: UUID, response: Response,
                      user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            row = _get_supplier(connection, supplier_id, branch['organization_id'])
        _no_store(response)
        return _supplier(row)

    @router.patch('/branches/{branch_id}/suppliers/{supplier_id}')
    def update_supplier(branch_id: UUID, supplier_id: UUID, body: SupplierUpdate,
                        response: Response, user=Depends(current_user)):
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise HTTPException(422, 'No se enviaron cambios')
        assignments = ', '.join(f'{field}=%s' for field in changes)
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            before = _get_supplier(connection, supplier_id, branch['organization_id'])
            updated = connection.execute(
                '''UPDATE supplier SET ''' + assignments + ''', updated_at=NOW() WHERE id=%s
                   RETURNING id, organization_id, name, contact_email, whatsapp_number, is_active''',
                (*changes.values(), supplier_id)).fetchone()
            record_change(connection, branch_id, user, 'supplier.updated',
                          before={'name': before['name']}, after={'name': updated['name']})
            connection.commit()
        _no_store(response)
        return _supplier(updated)

    @router.delete('/branches/{branch_id}/suppliers/{supplier_id}')
    def deactivate_supplier(branch_id: UUID, supplier_id: UUID, response: Response,
                            user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            before = _get_supplier(connection, supplier_id, branch['organization_id'])
            connection.execute('UPDATE supplier SET is_active=FALSE, updated_at=NOW() WHERE id=%s',
                               (supplier_id,))
            record_change(connection, branch_id, user, 'supplier.deactivated',
                          before={'name': before['name'], 'active': True},
                          after={'name': before['name'], 'active': False})
            connection.commit()
        _no_store(response)
        return {'message': 'Proveedor dado de baja; su historial se conserva'}

    @router.get('/branches/{branch_id}/suppliers/{supplier_id}/products')
    def supplier_products(branch_id: UUID, supplier_id: UUID, response: Response,
                          user=Depends(current_user)):
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            _get_supplier(connection, supplier_id, branch['organization_id'])
            rows = connection.execute(
                '''SELECT sp.product_id, sp.supplier_sku, sp.unit_cost, sp.is_preferred,
                          p.name AS product_name, p.sku, p.is_active
                     FROM supplier_product sp JOIN product p ON p.id=sp.product_id
                    WHERE sp.supplier_id=%s ORDER BY p.name, p.sku''',
                (supplier_id,)).fetchall()
        _no_store(response)
        return {'items': [{
            'productId': str(row['product_id']), 'name': row['product_name'],
            'sku': row['sku'], 'active': bool(row['is_active']),
            'supplierSku': row['supplier_sku'],
            'unitCost': float(row['unit_cost']) if row['unit_cost'] is not None else None,
            'isPreferred': bool(row['is_preferred']),
        } for row in rows]}

    @router.put('/branches/{branch_id}/suppliers/{supplier_id}/products')
    def replace_supplier_products(branch_id: UUID, supplier_id: UUID, body: SupplierProducts,
                                  response: Response, user=Depends(current_user)):
        """Reemplaza la asociación proveedor→producto en una sola transacción."""
        with get_connection() as connection:
            branch = require_branch_actor(connection, branch_id, user, admin=True)
            _get_supplier(connection, supplier_id, branch['organization_id'])
            # Validar todo antes de borrar: un producto ajeno no deja la lista a medias.
            seen = set()
            for link in body.products:
                if link.product_id in seen:
                    raise HTTPException(422, 'El producto está repetido en la lista')
                seen.add(link.product_id)
                product = connection.execute(
                    'SELECT id FROM product WHERE id=%s AND organization_id=%s AND is_active',
                    (link.product_id, branch['organization_id'])).fetchone()
                if not product:
                    raise HTTPException(404, 'Hay un producto que no existe en esta empresa')
            connection.execute('DELETE FROM supplier_product WHERE supplier_id=%s', (supplier_id,))
            for link in body.products:
                connection.execute(
                    '''INSERT INTO supplier_product(supplier_id, product_id, supplier_sku,
                           unit_cost, is_preferred) VALUES(%s,%s,%s,%s,%s)
                       ON CONFLICT(supplier_id, product_id) DO UPDATE SET
                           supplier_sku=EXCLUDED.supplier_sku, unit_cost=EXCLUDED.unit_cost,
                           is_preferred=EXCLUDED.is_preferred''',
                    (supplier_id, link.product_id, link.supplier_sku, link.unit_cost,
                     link.is_preferred))
            record_change(connection, branch_id, user, 'supplier.products.updated',
                          before=None,
                          after={'supplierId': str(supplier_id),
                                 'productIds': [str(link.product_id) for link in body.products]})
            connection.commit()
        _no_store(response)
        return {'message': 'Productos del proveedor actualizados', 'count': len(body.products)}

    return router

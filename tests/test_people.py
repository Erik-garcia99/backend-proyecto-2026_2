"""F-02 (ME-9): administración de personal. Pruebas con BD simulada."""
import unittest
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from app import main
from app.people import create_router
from app.security import verify_password

BRANCH = UUID('aaaaaaa1-1111-4111-8111-111111111111')
ORG = UUID('bbbbbbb2-2222-4222-8222-222222222222')
EMP = UUID('ccccccc3-3333-4333-8333-333333333333')
CUSTOMER = UUID('ddddddd4-4444-4444-8444-444444444444')
SUPPLIER = UUID('eeeeeee5-5555-4555-8555-555555555555')
PRODUCT = UUID('ffffffff-6666-4666-8666-666666666666')
OTHER_PRODUCT = UUID('11111111-7777-4777-8777-777777777777')
SALE = UUID('22222222-8888-4888-8888-888888888888')

ADMIN = {
    'id': UUID('99999999-9999-4999-8999-999999999999'),
    'organization_id': ORG, 'branch_id': BRANCH,
    'role': 'owner', 'account_status': 'active', 'is_superuser': False,
}


ASSIGNABLE = {'manager', 'rh', 'gerencia', 'employee', 'staff'}


def person_row(user_id=EMP, *, role='employee', email='ana@selfiq.local', active=True):
    return {
        'id': user_id, 'organization_id': ORG,
        'branch_id': BRANCH if role in ASSIGNABLE else None,
        'email': email, 'first_name': 'Ana', 'last_name': 'López',
        'phone_number': '5551234567', 'role': role, 'account_status': 'active',
        'created_at': datetime(2026, 11, 23, tzinfo=timezone.utc),
        'deactivated_at': None if active else datetime(2026, 11, 24, tzinfo=timezone.utc),
    }


def supplier_row(active=True):
    return {'id': SUPPLIER, 'organization_id': ORG, 'name': 'Distribuidora Central',
            'contact_email': 'ventas@proveedor.local', 'whatsapp_number': '5559876543',
            'is_active': active}


class FakeCursor:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        if isinstance(self.value, list):
            return self.value[0] if self.value else None
        return self.value

    def fetchall(self):
        if isinstance(self.value, list):
            return self.value
        return [self.value] if self.value is not None else []


class FakeConnection:
    """Enruta por fragmentos de SQL; registra cada consulta para aserciones."""

    def __init__(self, *, user_rows=None, supplier_rows=None, error=None,
                 unknown_product=False, sales=None, sale_items=None, supplier_products=None):
        self.user_rows = user_rows if user_rows is not None else [person_row()]
        self.supplier_rows = supplier_rows if supplier_rows is not None else [supplier_row()]
        self.error = error
        self.unknown_product = unknown_product
        self.sales = sales if sales is not None else []
        self.sale_items = sale_items if sale_items is not None else []
        self.supplier_products = supplier_products if supplier_products is not None else []
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        key = ' '.join(sql.split())
        if self.error and ('INSERT INTO "user"' in key or 'UPDATE "user"' in key):
            raise self.error
        if 'SELECT b.id, b.organization_id' in key:
            return FakeCursor({'id': BRANCH, 'organization_id': ORG, 'name': 'Centro'})
        if 'INSERT INTO "user"' in key:
            is_customer = 'VALUES(%s,NULL,' in key
            role = 'customer' if is_customer else params[7]
            email = params[1] if is_customer else params[2]
            return FakeCursor(person_row(role=role, email=email))
        if 'UPDATE "user"' in key:
            return FakeCursor(person_row())
        if 'FROM "user"' in key:
            return FakeCursor(self.user_rows)
        if 'INSERT INTO supplier' in key or 'UPDATE supplier' in key:
            return FakeCursor({**supplier_row(), 'name': params[1]
                               if 'INSERT INTO supplier' in key else params[0]})
        if 'FROM supplier' in key and 'supplier_product' in key:
            return FakeCursor(self.supplier_products)
        if 'FROM supplier' in key:
            return FakeCursor(self.supplier_rows)
        if 'FROM product WHERE id=%s' in key:
            return FakeCursor(None if self.unknown_product else {'id': params[0]})
        if 'FROM sale_item' in key:
            return FakeCursor(self.sale_items)
        if 'FROM sale ' in key:
            return FakeCursor(self.sales)
        return FakeCursor(None)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PeopleEndpointTests(unittest.TestCase):
    def setUp(self):
        self.user = dict(ADMIN)
        self.connection = FakeConnection()
        app = FastAPI()
        app.include_router(create_router(lambda: self.user, lambda: self.connection))
        self.client = TestClient(app)

    def test_router_is_registered_in_main_app(self):
        paths = main.app.openapi()['paths']
        for suffix in ('employees', 'customers', 'suppliers'):
            self.assertIn(f'/branches/{{branch_id}}/{suffix}', paths)
        self.assertIn('/branches/{branch_id}/customers/{user_id}/purchases', paths)
        self.assertIn('/branches/{branch_id}/suppliers/{supplier_id}/products', paths)

    def test_create_employee_hashes_password_and_logs_change(self):
        response = self.client.post(f'/branches/{BRANCH}/employees', json={
            'email': 'Nueva@SelfIQ.local', 'password': 'ClaveSegura123',
            'first_name': 'Nueva', 'last_name': 'Empleada', 'role': 'employee',
        })
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body['email'], 'nueva@selfiq.local')
        self.assertEqual(body['role'], 'employee')
        self.assertTrue(body['active'])
        inserts = [params for sql, params in self.connection.executed if 'INSERT INTO "user"' in sql]
        self.assertEqual(len(inserts), 1)
        self.assertTrue(verify_password('ClaveSegura123', inserts[0][6]))
        self.assertTrue(any('employee.created' in (params or ())
                            for _, params in self.connection.executed))
        self.assertEqual(self.connection.commits, 1)

    def test_create_employee_accepts_any_assignable_role(self):
        for role in ('manager', 'rh', 'gerencia', 'employee'):
            with self.subTest(role=role):
                self.connection = FakeConnection()
                response = self.client.post(f'/branches/{BRANCH}/employees', json={
                    'email': f'{role}@selfiq.local', 'password': 'ClaveSegura123',
                    'first_name': 'Nueva', 'last_name': 'Empleada', 'role': role,
                })
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json()['role'], role,
                                 'El valor del rol viaja sin traducir al backend')
        # `staff` era alias de `employee`: no se crea, pero sigue listándose.
        for reserved in ('owner', 'sudo', 'customer', 'staff'):
            with self.subTest(reserved=reserved):
                self.connection = FakeConnection()
                response = self.client.post(f'/branches/{BRANCH}/employees', json={
                    'email': f'{reserved}@selfiq.local', 'password': 'ClaveSegura123',
                    'first_name': 'X', 'last_name': 'Y', 'role': reserved,
                })
                self.assertEqual(response.status_code, 422)

        self.connection = FakeConnection()
        response = self.client.get(f'/branches/{BRANCH}/employees')
        self.assertEqual(response.status_code, 200, response.text)
        list_params = next(params for sql, params in self.connection.executed if 'FROM "user"' in sql)
        self.assertIn('staff', list_params[2], 'Los registros staff antiguos siguen visibles')

    def test_create_employee_rejects_duplicated_email(self):
        failing = FakeConnection(error=UniqueViolation('duplicado'))
        self.connection = failing
        response = self.client.post(f'/branches/{BRANCH}/employees', json={
            'email': ' repetido@selfiq.local ', 'password': 'ClaveSegura123',
            'first_name': 'X', 'last_name': 'Y',
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], 'El correo ya está registrado')
        self.assertEqual(failing.rollbacks, 1)

    def test_employee_patch_and_soft_delete(self):
        response = self.client.patch(f'/branches/{BRANCH}/employees/{EMP}', json={
            'first_name': 'Ana María', 'phone_number': '5550000000',
        })
        self.assertEqual(response.status_code, 200, response.text)
        update = [ ' '.join(sql.split()) for sql, _ in self.connection.executed
                   if 'UPDATE "user" SET' in sql ]
        self.assertEqual(len(update), 1)
        self.assertIn('first_name=%s', update[0])
        self.assertIn('phone_number=%s', update[0])
        self.assertNotIn('role=', update[0])

        response = self.client.delete(f'/branches/{BRANCH}/employees/{EMP}')
        self.assertEqual(response.status_code, 200, response.text)
        sqls = [ ' '.join(sql.split()) for sql, _ in self.connection.executed ]
        self.assertTrue(any("account_status='inactive'" in sql for sql in sqls))
        self.assertTrue(any('UPDATE auth_session SET revoked_at=NOW()' in sql for sql in sqls))
        self.assertTrue(any('employee.deactivated' in (params or ())
                            for _, params in self.connection.executed))

    def test_roles_other_than_admin_cannot_administer_people(self):
        self.user.update(role='employee', is_superuser=False)
        response = self.client.get(f'/branches/{BRANCH}/employees')
        self.assertEqual(response.status_code, 403)
        response = self.client.get(f'/branches/{BRANCH}/suppliers')
        self.assertEqual(response.status_code, 403)

    def test_customer_has_no_branch_and_cannot_change_role(self):
        response = self.client.post(f'/branches/{BRANCH}/customers', json={
            'email': 'cliente@selfiq.local', 'password': 'ClaveSegura123',
            'first_name': 'Cliente', 'last_name': 'Uno', 'role': 'owner',
        })
        self.assertEqual(response.status_code, 422)
        response = self.client.post(f'/branches/{BRANCH}/customers', json={
            'email': 'cliente@selfiq.local', 'password': 'ClaveSegura123',
            'first_name': 'Cliente', 'last_name': 'Uno',
        })
        self.assertEqual(response.status_code, 201, response.text)
        sql, params = next((sql, params) for sql, params in self.connection.executed
                           if 'INSERT INTO "user"' in sql)
        self.assertIn('VALUES(%s,NULL,', sql)   # los clientes no pertenecen a una sucursal
        self.assertEqual(params[0], ORG)
        self.assertIn("'customer'", sql)

    def test_customer_purchases_come_from_pos_sales(self):
        self.connection.user_rows = [person_row(user_id=CUSTOMER, role='customer')]
        self.connection.sales = [{
            'id': SALE, 'sale_number': 'V-001',
            'occurred_at': datetime(2026, 11, 20, tzinfo=timezone.utc),
            'total': 125.5, 'status': 'completed',
        }]
        self.connection.sale_items = [{
            'sale_id': SALE, 'quantity': 2, 'unit_price': 50, 'subtotal': 100,
            'product_id': PRODUCT, 'product_name': 'Café', 'sku': 'CAF-1',
        }]
        response = self.client.get(f'/branches/{BRANCH}/customers/{CUSTOMER}/purchases')
        self.assertEqual(response.status_code, 200, response.text)
        purchase = response.json()['items'][0]
        self.assertEqual(purchase['saleNumber'], 'V-001')
        self.assertEqual(purchase['items'][0]['name'], 'Café')

    def test_supplier_crud_and_product_association_is_transactional(self):
        response = self.client.post(f'/branches/{BRANCH}/suppliers', json={
            'name': 'Proveedor Nuevo', 'contact_email': 'contacto@selfiq.local',
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['name'], 'Proveedor Nuevo')

        response = self.client.put(f'/branches/{BRANCH}/suppliers/{SUPPLIER}/products',
                                   json={'products': [
                                       {'product_id': str(PRODUCT), 'is_preferred': True},
                                   ]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['count'], 1)
        sqls = [' '.join(sql.split()) for sql, _ in self.connection.executed]
        delete_at = next(i for i, sql in enumerate(sqls) if sql.startswith('DELETE FROM supplier_product'))
        self.assertTrue(any('INSERT INTO supplier_product' in sql for sql in sqls[delete_at:]))

        failing = FakeConnection(unknown_product=True)
        self.connection = failing
        response = self.client.put(f'/branches/{BRANCH}/suppliers/{SUPPLIER}/products',
                                   json={'products': [{'product_id': str(OTHER_PRODUCT)}]})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(any('DELETE FROM supplier_product' in sql
                             for sql, _ in failing.executed), 'No borra si la validación falla')

    def test_supplier_soft_delete_preserves_history(self):
        response = self.client.delete(f'/branches/{BRANCH}/suppliers/{SUPPLIER}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(any('UPDATE supplier SET is_active=FALSE' in ' '.join(sql.split())
                            for sql, _ in self.connection.executed))
        self.assertTrue(any('supplier.deactivated' in (params or ())
                            for _, params in self.connection.executed))


if __name__ == '__main__':
    unittest.main()

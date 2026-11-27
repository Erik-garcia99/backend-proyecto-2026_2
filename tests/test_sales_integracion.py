"""PS-01 a PS-05: integración real de ventas contra PostgreSQL.

Requiere DATABASE_URL apuntando a una BD DE PRUEBA (nunca Supabase de producción)
y POS_TEST_DB=1. El nombre de la base debe contener "test". Sin eso el módulo
se omite, para que la suite normal no intente conectarse a producción.
Sin cámara, InfluxDB, MQTT ni WSS: solo FastAPI + PostgreSQL real.
"""
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app import pos
from app.pos import PosError, create_router, error_response
from app.security import create_pos_unlock_token, hash_password

DATABASE_URL = os.environ.get('DATABASE_URL', '')
DBNAME = conninfo_to_dict(DATABASE_URL).get('dbname', '') if DATABASE_URL else ''
if os.environ.get('POS_TEST_DB') != '1' or 'test' not in DBNAME:
    pytest.skip(
        'PS-01..PS-05 requieren POS_TEST_DB=1 y una BD con "test" en el nombre',
        allow_module_level=True,
    )

ORG = UUID('10000000-0000-4000-8000-000000000001')
BRANCH = UUID('10000000-0000-4000-8000-000000000002')
USER_ID = UUID('10000000-0000-4000-8000-000000000003')
EMPLOYEE_ID = UUID('10000000-0000-4000-8000-000000000004')
RRHH_ID = UUID('10000000-0000-4000-8000-000000000005')
OTHER_BRANCH = UUID('10000000-0000-4000-8000-000000000099')
OTHER_ORG = UUID('20000000-0000-4000-8000-000000000001')
P1 = UUID('10000000-0000-4000-8000-000000000011')
P2 = UUID('10000000-0000-4000-8000-000000000012')

PASSWORD = 'Prueba123!'
PASSWORD_HASH = hash_password(PASSWORD)

USER = {
    'id': USER_ID,
    'organization_id': ORG,
    'branch_id': BRANCH,
    'role': 'manager',
    'is_superuser': False,
    'password_hash': PASSWORD_HASH,
}

EMPLOYEE = {**USER, 'id': EMPLOYEE_ID, 'role': 'employee'}
OWNER = {**USER, 'role': 'owner', 'branch_id': None}


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def make_client(user=USER, raise_server_exceptions=True):
    app = FastAPI()
    app.add_exception_handler(PosError, lambda request, exc: error_response(exc))
    app.include_router(create_router(lambda: user, connect))
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def sale_payload(quantity=2, product_id=P1, received=1000):
    return {
        'customer_id': None,
        'items': [{'product_id': str(product_id), 'quantity': quantity}],
        'payment': {'method': 'cash', 'amount_received': received},
    }


def unlock_header(user_id=USER_ID):
    token, _ = create_pos_unlock_token(user_id, BRANCH)
    return {'X-POS-Unlock': token}


def sell(client, key, payload, user_id=USER_ID, unlock=True):
    headers = {'Idempotency-Key': key}
    if unlock:
        headers.update(unlock_header(user_id))
    return client.post(
        f'/api/v1/branches/{BRANCH}/sales',
        json=payload,
        headers=headers,
    )


def stock(product_id=P1):
    with connect() as conn:
        row = conn.execute(
            'SELECT quantity_on_hand FROM branch_inventory WHERE branch_id=%s AND product_id=%s',
            (BRANCH, product_id)).fetchone()
    return row['quantity_on_hand']


def counts():
    with connect() as conn:
        return {
            name: conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']
            for name, table in (
                ('sales', 'sale'), ('items', 'sale_item'),
                ('movements', 'inventory_movement'), ('keys', 'idempotency_key'),
            )
        }


def count(table):
    with connect() as conn:
        return conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']


def movements_of_type(movement_type):
    with connect() as conn:
        return conn.execute(
            '''SELECT product_id, quantity_delta, quantity_after, reason
               FROM inventory_movement WHERE movement_type=%s ORDER BY id''',
            (movement_type,)).fetchall()


@pytest.fixture(autouse=True)
def seed_db():
    """Base limpia por test: organización, sucursal, cajero, 2 productos y stock."""
    with connect() as conn:
        conn.execute('''
            TRUNCATE TABLE sale_item, inventory_movement, sale, goods_receipt_item,
                goods_receipt, idempotency_key, pos_seller, branch_inventory, product,
                "user", branch, organization
            RESTART IDENTITY CASCADE
        ''')
        conn.execute('INSERT INTO organization (id, name, slug) VALUES (%s,%s,%s)',
                     (ORG, 'Organización prueba', 'pos-test'))
        conn.execute('''INSERT INTO branch (id, organization_id, name, branch_code)
                        VALUES (%s,%s,%s,%s)''',
                     (BRANCH, ORG, 'Sucursal prueba', 'POS-TEST'))
        for uid, email, role in (
            (USER_ID, 'cajero@test.local', 'manager'),
            (EMPLOYEE_ID, 'empleado@test.local', 'employee'),
            (RRHH_ID, 'rrhh@test.local', 'rh'),
        ):
            conn.execute('''INSERT INTO "user"
                            (id, organization_id, branch_id, email, role, account_status, password_hash)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                         (uid, ORG, BRANCH, email, role, 'active', PASSWORD_HASH))
        # El cajero de prueba sí tiene permiso; lo otorgó RRHH.
        conn.execute('''INSERT INTO pos_seller (branch_id, user_id, granted_by)
                        VALUES (%s,%s,%s)''', (BRANCH, USER_ID, RRHH_ID))
        for pid, sku, name, price in (
            (P1, 'P-OK', 'Café molido 500 g', Decimal('10.00')),
            (P2, 'P-BAJO', 'Galletas de avena', Decimal('20.00')),
        ):
            conn.execute('''INSERT INTO product
                            (id, organization_id, sku, name, sale_price, tax_rate, is_active)
                            VALUES (%s,%s,%s,%s,%s,%s,TRUE)''',
                         (pid, ORG, sku, name, price, Decimal('0.16')))
            conn.execute('''INSERT INTO branch_inventory (branch_id, product_id, quantity_on_hand)
                            VALUES (%s,%s,%s)''', (BRANCH, pid, 5 if pid == P1 else 3))
    yield


@pytest.fixture
def client():
    with make_client() as c:
        yield c


# PS-01 -----------------------------------------------------------------------
def test_ps01_venta_normal_descuenta_stock_y_crea_movimientos(client):
    response = sell(client, str(uuid4()), sale_payload(quantity=2))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body['status'] == 'completed'
    assert Decimal(str(body['subtotal'])) == Decimal('20.00')
    assert Decimal(str(body['tax'])) == Decimal('3.20')
    assert Decimal(str(body['total'])) == Decimal('23.20')
    assert len(body['items']) == 1

    assert stock() == 3
    c = counts()
    assert c['sales'] == 1 and c['items'] == 1 and c['movements'] == 1 and c['keys'] == 1
    with connect() as conn:
        m = conn.execute('''SELECT movement_type, quantity_delta, quantity_after, sale_id
                            FROM inventory_movement''').fetchone()
    assert m['movement_type'] == 'sale'
    assert m['quantity_delta'] == -2
    assert m['quantity_after'] == 3
    assert m['sale_id'] == UUID(body['id'])


# PS-02 -----------------------------------------------------------------------
def test_ps02_reenvio_misma_clave_y_cuerpo_no_duplica(client):
    key = str(uuid4())
    first = sell(client, key, sale_payload(quantity=2))
    second = sell(client, key, sale_payload(quantity=2))

    assert first.status_code == second.status_code == 201
    assert second.json()['id'] == first.json()['id']
    assert stock() == 3
    c = counts()
    assert c['sales'] == 1 and c['items'] == 1 and c['movements'] == 1


# PS-03 -----------------------------------------------------------------------
def test_ps03_misma_clave_otro_cuerpo_da_409(client):
    key = str(uuid4())
    first = sell(client, key, sale_payload(quantity=1))
    conflict = sell(client, key, sale_payload(quantity=2))

    assert first.status_code == 201
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()['code'] == 'IDEMPOTENCY_CONFLICT'
    assert stock() == 4
    assert counts()['sales'] == 1


# PS-04 -----------------------------------------------------------------------
def test_ps04_dos_ventas_concurrentes_del_ultimo_stock():
    with connect() as conn:
        conn.execute('UPDATE branch_inventory SET quantity_on_hand=1 WHERE branch_id=%s AND product_id=%s',
                     (BRANCH, P1))

    barrier = Barrier(2)

    def intento(index):
        with make_client() as client:
            barrier.wait(timeout=10)  # ambas transacciones arrancan juntas
            return sell(client, str(uuid4()), sale_payload(quantity=1, received=100)).json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(intento, range(2)))

    codes = sorted(str(r.get('code', 'OK')) for r in results)
    assert codes == ['INSUFFICIENT_STOCK', 'OK'], results
    assert stock() == 0
    c = counts()
    assert c['sales'] == 1 and c['items'] == 1 and c['movements'] == 1


# PS-05 -----------------------------------------------------------------------
def test_ps05_fallo_a_mitad_de_transaccion_hace_rollback(monkeypatch):
    real_dumps = pos.json.dumps

    def dumps_con_fallo(obj, *args, **kwargs):
        # El último paso de create_sale serializa la respuesta YA con la venta,
        # partidas y movimientos insertados. Fallar aquí fuerza rollback real.
        if isinstance(obj, dict) and 'sale_number' in obj:
            raise RuntimeError('fallo forzado a mitad de transacción')
        return real_dumps(obj, *args, **kwargs)

    monkeypatch.setattr(pos.json, 'dumps', dumps_con_fallo)
    with make_client(raise_server_exceptions=False) as client:
        response = sell(client, str(uuid4()), sale_payload(quantity=2))

    assert response.status_code >= 500, response.text
    assert stock() == 5
    assert counts() == {'sales': 0, 'items': 0, 'movements': 0, 'keys': 0}


def nueva_venta(client, quantity=2, product_id=P1):
    response = sell(client, str(uuid4()), sale_payload(quantity=quantity, product_id=product_id))
    assert response.status_code == 201, response.text
    return response.json()['id']


# PS-06 -----------------------------------------------------------------------
def test_ps06_anulacion_restituye_stock_y_segunda_anulacion_da_409(client):
    sale_id = nueva_venta(client, quantity=2)
    assert stock() == 3

    void = client.post(f'/api/v1/branches/{BRANCH}/sales/{sale_id}/void',
                       json={'reason': 'Cliente se arrepintió'},
                       headers={'Idempotency-Key': str(uuid4())})
    assert void.status_code == 200, void.text
    assert void.json()['status'] == 'voided'
    assert stock() == 5

    ms = movements_of_type('sale_void')
    assert len(ms) == 1
    assert ms[0]['product_id'] == P1
    assert ms[0]['quantity_delta'] == 2
    assert ms[0]['quantity_after'] == 5

    second = client.post(f'/api/v1/branches/{BRANCH}/sales/{sale_id}/void',
                         json={'reason': 'Otra vez'},
                         headers={'Idempotency-Key': str(uuid4())})
    assert second.status_code == 409, second.text
    assert second.json()['code'] == 'SALE_ALREADY_VOIDED'
    assert stock() == 5
    assert len(movements_of_type('sale_void')) == 1


# PS-07 -----------------------------------------------------------------------
def test_ps07_devolucion_parcial_valida_y_excedida_da_422(client):
    sale_id = nueva_venta(client, quantity=2)
    url = f'/api/v1/branches/{BRANCH}/sales/{sale_id}/returns'

    partial = client.post(url, json={
        'items': [{'product_id': str(P1), 'quantity': 1}],
        'reason': 'Producto defectuoso',
    }, headers={'Idempotency-Key': str(uuid4())})
    assert partial.status_code == 200, partial.text
    assert stock() == 4
    assert len(movements_of_type('return')) == 1

    # Ya se devolvió 1 de 2: pedir 3 más excede lo vendido.
    excedida = client.post(url, json={
        'items': [{'product_id': str(P1), 'quantity': 3}],
        'reason': 'Demasiados',
    }, headers={'Idempotency-Key': str(uuid4())})
    assert excedida.status_code == 422, excedida.text
    assert excedida.json()['code'] == 'RETURN_EXCEEDS_SOLD'
    assert stock() == 4
    assert len(movements_of_type('return')) == 1

    # 1 devuelto + 2 solicitados tampoco cabe en lo vendido.
    excedida2 = client.post(url, json={
        'items': [{'product_id': str(P1), 'quantity': 2}],
    }, headers={'Idempotency-Key': str(uuid4())})
    assert excedida2.status_code == 422, excedida2.text
    assert excedida2.json()['code'] == 'RETURN_EXCEEDS_SOLD'
    assert stock() == 4


# PS-08 -----------------------------------------------------------------------
def test_ps08_confirmar_recepcion_dos_veces_aplica_una_sola_entrada(client):
    draft = client.post(f'/api/v1/branches/{BRANCH}/goods-receipts', json={
        'supplier_id': None,
        'notes': 'Pedido de prueba',
        'items': [{'product_id': str(P2), 'declared_units': 5, 'received_units': 4}],
    })
    assert draft.status_code == 201, draft.text
    receipt = draft.json()
    assert receipt['status'] == 'draft'
    assert stock(P2) == 3

    url = f'/api/v1/branches/{BRANCH}/goods-receipts/{receipt["id"]}/confirm'
    first = client.post(url, headers={'Idempotency-Key': str(uuid4())})
    assert first.status_code == 200, first.text
    assert first.json()['status'] == 'confirmed'
    assert stock(P2) == 7

    ms = movements_of_type('receipt')
    assert len(ms) == 1
    assert ms[0]['product_id'] == P2
    assert ms[0]['quantity_delta'] == 4
    assert ms[0]['quantity_after'] == 7

    second = client.post(url, headers={'Idempotency-Key': str(uuid4())})
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert stock(P2) == 7
    assert len(movements_of_type('receipt')) == 1
    assert count('goods_receipt') == 1


# Bloqueo de caja -------------------------------------------------------------
def test_bloqueo_sin_contrasena_no_vende_y_contrasena_correcta_desbloquea(client):
    bloqueado = sell(client, str(uuid4()), sale_payload(), unlock=False)
    assert bloqueado.status_code == 423, bloqueado.text
    assert bloqueado.json()['code'] == 'POS_LOCKED'
    assert counts()['sales'] == 0

    incorrecta = client.post(f'/api/v1/branches/{BRANCH}/pos/unlock',
                             json={'password': 'no-es-esta'})
    assert incorrecta.status_code == 401, incorrecta.text
    assert incorrecta.json()['code'] == 'INVALID_PASSWORD'

    correcta = client.post(f'/api/v1/branches/{BRANCH}/pos/unlock',
                           json={'password': PASSWORD})
    assert correcta.status_code == 200, correcta.text
    token = correcta.json()['token']
    assert correcta.json()['unlocked_until']

    venta = client.post(
        f'/api/v1/branches/{BRANCH}/sales', json=sale_payload(),
        headers={'Idempotency-Key': str(uuid4()), 'X-POS-Unlock': token})
    assert venta.status_code == 201, venta.text
    assert counts()['sales'] == 1


# Permiso por persona (la asignación se hace desde la app móvil) --------------
def test_empleado_sin_permiso_no_vende_owner_lo_otorga_y_lo_revoca():
    with make_client(user=EMPLOYEE) as cajero:
        denegada = sell(cajero, str(uuid4()), sale_payload(), user_id=EMPLOYEE_ID)
        assert denegada.status_code == 403, denegada.text
        assert denegada.json()['code'] == 'POS_SELLER_NOT_ALLOWED'
        assert counts()['sales'] == 0

    with make_client(user=OWNER) as admin:
        listado = admin.get(f'/api/v1/branches/{BRANCH}/pos/sellers')
        assert listado.status_code == 200, listado.text
        fila = next(r for r in listado.json() if r['user_id'] == str(EMPLOYEE_ID))
        assert fila['can_sell'] is False

        grant = admin.post(f'/api/v1/branches/{BRANCH}/pos/sellers',
                           json={'user_id': str(EMPLOYEE_ID)})
        assert grant.status_code == 201, grant.text

    with make_client(user=EMPLOYEE) as cajero:
        permitida = sell(cajero, str(uuid4()), sale_payload(), user_id=EMPLOYEE_ID)
        assert permitida.status_code == 201, permitida.text
        assert counts()['sales'] == 1

    with make_client(user=OWNER) as admin:
        revoke = admin.delete(f'/api/v1/branches/{BRANCH}/pos/sellers/{EMPLOYEE_ID}')
        assert revoke.status_code == 200, revoke.text

    with make_client(user=EMPLOYEE) as cajero:
        denegada_de_nuevo = sell(cajero, str(uuid4()), sale_payload(), user_id=EMPLOYEE_ID)
        assert denegada_de_nuevo.status_code == 403
        assert counts()['sales'] == 1


def test_rrhh_otorga_pero_no_vende_si_no_se_le_otorga():
    rrhh = {**USER, 'id': RRHH_ID, 'role': 'rh'}
    with make_client(user=rrhh) as admin:
        denegada = sell(admin, str(uuid4()), sale_payload())
        assert denegada.status_code == 403, denegada.text
        assert denegada.json()['code'] == 'POS_SELLER_NOT_ALLOWED'

        grant = admin.post(f'/api/v1/branches/{BRANCH}/pos/sellers',
                           json={'user_id': str(RRHH_ID)})
        assert grant.status_code == 201, grant.text

    with make_client(user=rrhh) as admin:
        permitida = sell(admin, str(uuid4()), sale_payload(), user_id=RRHH_ID)
        assert permitida.status_code == 201, permitida.text


# PS-09: permisos finos y bitácora -------------------------------------------
def test_ps09_cajero_no_puede_ajustar_inventario_y_queda_en_bitacora(caplog):
    with caplog.at_level(logging.WARNING, logger='selfiq.pos'):
        with make_client(user=EMPLOYEE) as cajero:
            response = cajero.post(
                f'/api/v1/branches/{BRANCH}/inventory/adjustments',
                json={'product_id': str(P1), 'quantity_delta': 5, 'reason': 'sin permiso'},
                headers={'Idempotency-Key': str(uuid4())})

    assert response.status_code == 403, response.text
    assert response.json()['code'] == 'FORBIDDEN'
    assert stock() == 5
    denied = [r for r in caplog.records if r.message == 'pos.permission.denied']
    assert denied and getattr(denied[0], 'action', None) == 'pos.inventory.adjust'


def test_ps09_cliente_no_entra_al_pos_y_queda_en_bitacora(caplog):
    cliente = {**USER, 'role': 'customer', 'branch_id': BRANCH}
    with caplog.at_level(logging.WARNING, logger='selfiq.pos'):
        with make_client(user=cliente) as c:
            lecturas = [c.get(f'/api/v1/branches/{BRANCH}{path}') for path in (
                '/inventory', '/sales', '/products', '/inventory/movements')]
            venta = sell(c, str(uuid4()), sale_payload())

    assert all(r.status_code in (403, 404) for r in [*lecturas, venta])
    assert all(r.json()['code'] == 'FORBIDDEN' for r in [*lecturas, venta])
    denied = [r for r in caplog.records if r.message == 'pos.access.denied']
    assert denied and {getattr(r, 'reason', None) for r in denied} == {'customer_role'}


def test_ps09_otra_sucursal_y_otra_organizacion_se_rechazan_y_se_registran(caplog):
    otra_sucursal = {**USER, 'branch_id': OTHER_BRANCH}
    with caplog.at_level(logging.WARNING, logger='selfiq.pos'):
        with make_client(user=otra_sucursal) as c:
            local = c.get(f'/api/v1/branches/{BRANCH}/inventory')
    assert local.status_code == 403, local.text
    assert any(r.message == 'pos.access.denied' and getattr(r, 'reason', None) == 'branch_mismatch'
               for r in caplog.records)

    caplog.clear()
    externo = {**OWNER, 'organization_id': OTHER_ORG}
    with caplog.at_level(logging.WARNING, logger='selfiq.pos'):
        with make_client(user=externo) as c:
            remoto = c.get(f'/api/v1/branches/{BRANCH}/inventory')
    assert remoto.status_code == 404, remoto.text
    assert remoto.json()['code'] == 'BRANCH_NOT_FOUND'
    assert any(r.message == 'pos.access.denied' and getattr(r, 'reason', None) == 'other_organization'
               for r in caplog.records)


# PS-15 -----------------------------------------------------------------------
def test_ps15_logs_no_contienen_tokens_ni_datos_personales(caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger='selfiq.pos')
    client = make_client()
    key = str(uuid4())
    response = sell(client, key, sale_payload())
    assert response.status_code == 201, response.text

    texto = caplog.text
    # No deben aparecer tokens completos ni la clave de idempotencia.
    assert 'Bearer ' not in texto
    assert key not in texto
    # No debe aparecer la contraseña de prueba ni patrones de email.
    assert PASSWORD not in texto
    assert '@' not in texto

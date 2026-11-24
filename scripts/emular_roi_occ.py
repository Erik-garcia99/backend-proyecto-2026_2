"""Emulador del envío de roi_occ de la ESP32-CAM y validación de PS-10 / PS-11.

Dos modos:

  --mock (por defecto): no necesita credenciales. Intercepta las consultas a
    InfluxDB con httpx.MockTransport y alimenta un punto sintético de baja
    ocupación al código REAL de app.telemetry y app.pos. Sirve para avanzar
    sin la placa ni la instancia de Influx.

  --influx-write: escribe una línea line-protocol en la InfluxDB real usando
    INFLUXDB_URL / INFLUXDB_TOKEN / INFLUXDB_ORG / INFLUXDB_BUCKET. Requiere
    pasar --branch-id, --node-id, --roi-id, --config-version y
    --calibration-version con los valores de tu sucursal/ROI de prueba.

Uso:
  PYTHONPATH=. uv run --with 'argon2-cffi>=23,<25' python scripts/emular_roi_occ.py --mock
  PYTHONPATH=. INFLUXDB_URL=https://... INFLUXDB_TOKEN=... INFLUXDB_ORG=... INFLUXDB_BUCKET=... \
    uv run python scripts/emular_roi_occ.py --influx-write \
      --branch-id ... --node-id ... --roi-id ... --config-version 43 --calibration-version 1

NOTA: el gateway RPi actual (influx_sync.event_to_line) solo escribe los tags
branch_id, esp_id, module y event_id. El backend filtra además por roi_id,
config_version y calibration_version. Esta diferencia explica que la relación
visual no aparezca aunque la cámara publique: revisar antes de la prueba física.
"""

import argparse
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx

# Capturado antes de que los tests parcheen app.telemetry.httpx.Client.
REAL_HTTPX_CLIENT = httpx.Client

BRANCH = UUID('d07018c7-99c1-490f-95b9-dd54589cf60b')
ROI = UUID('a5760093-b323-48d6-a1bb-d1574ab07857')
NODE = UUID('5c485efa-cc20-4f9a-91eb-ceeba0f99289')
ORG = UUID('11111111-1111-1111-1111-111111111111')
PRODUCT_A = UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
PRODUCT_B = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
STOCK_A = 37
STOCK_B = 12
LOW_OCC = 5  # roi_occ "bajo" simulado
NOW = datetime.now(timezone.utc)
CONTEXT = dict(camera_node_id=NODE, config_version=43, calibration_version=1,
               calibration_status='calibrated')


def csv_point(timestamp, value):
    return (',result,table,_time,_value\n'
            f',,0,{timestamp.isoformat()},{value}\n')


def make_influx_mock(queries, value=LOW_OCC):
    """Client httpx que simula POST /api/v2/query del InfluxDB (Flux)."""
    def handler(request):
        body = request.content.decode()
        queries.append(body)
        if request.url.path != '/api/v2/query':
            return httpx.Response(200, text='')
        # La serie cam_moved es opcional: devolver vacío la desactiva.
        if 'cam_moved' in body:
            return httpx.Response(200, text='')
        return httpx.Response(200, text=csv_point(NOW - timedelta(seconds=2), value))

    return REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeCursor:
    """Cursor de BD falso: devuelve filas y registra TODO el SQL ejecutado."""

    def __init__(self, data, sql_log):
        self.data = data
        self.sql_log = sql_log
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        flat = ' '.join(sql.split()).lower()
        self.sql_log.append(flat)
        if 'from branch where' in flat:
            rows = [{'organization_id': ORG}]
        elif 'from product where' in flat:
            pid = str(params[0])
            rows = [self.data['products'][pid]] if pid in self.data['products'] else []
        elif 'from branch_inventory' in flat:
            rows = [{'quantity_on_hand': self.data['stock'].get(str(params[1]), 0),
                     'updated_at': NOW}]
        elif 'from roi r' in flat:
            rows = self.data['roi_by_product'].get(str(params[0]), [])
        else:
            rows = []
        self._rows = rows
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, data, sql_log):
        self.data = data
        self.sql_log = sql_log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.data, self.sql_log)


def availability_endpoint(data, sql_log):
    """Obtiene el handler real de /availability con un get_connection falso."""
    from app.pos import create_router

    router = create_router(lambda user: user, lambda: FakeConn(data, sql_log))
    for route in router.routes:
        if getattr(route, 'path', '').endswith('/availability'):
            return route.endpoint
    raise RuntimeError('No se encontró el endpoint /availability')


def assert_no_writes(sql_log, label):
    writes = [s for s in sql_log if s.startswith(('update ', 'insert ', 'delete '))]
    if writes:
        raise AssertionError(f'{label}: la lectura visual escribió en la BD: {writes}')


def check(label, condition):
    print(f"  [{'OK' if condition else 'FALLO'}] {label}")
    if not condition:
        raise AssertionError(label)


def validate_mock():
    from unittest.mock import patch

    from app.telemetry import InfluxSettings, read_roi_occupancy

    settings = InfluxSettings('2', 'https://influx.test', 'token', 'temp', 'value',
                              'branch_id', org='org', bucket='iot')
    queries = []
    print('\n--- Emulación roi_occ (mock, sin credenciales) ---')
    # Cada _fetch_point abre su propio `with httpx.Client(...)`: hay que
    # devolver una instancia nueva por llamada.
    with patch('app.telemetry.httpx.Client',
               side_effect=lambda *a, **kw: make_influx_mock(queries)):
        res = read_roi_occupancy(settings, BRANCH, ROI, CONTEXT)
    print(f'  lectura -> occupancy_pct={res.occupancy_pct}, status={res.status}, '
          f'received_at={res.received_at}')
    check('el punto bajo simulado se lee como 5 %', res.occupancy_pct == LOW_OCC)
    check('el estado es ok (lectura reciente)', res.status == 'ok')

    q = '\n'.join(queries)
    check('la consulta filtra por measurement roi_occ', 'roi_occ' in q)
    for name, value in (('roi_id', ROI), ('esp_id', NODE), ('branch_id', BRANCH),
                        ('config_version', CONTEXT['config_version']),
                        ('calibration_version', CONTEXT['calibration_version'])):
        check(f'la consulta filtra por {name}={value}', str(value) in q)

    roi_row = {
        'roi_id': ROI, 'roi_name': 'Zona A', 'shelf_id': UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc'),
        'shelf_name': 'Repisa 1', 'location': 'Pasillo 2', 'camera_node_id': NODE,
        'config_version': CONTEXT['config_version'], 'calibration_status': 'calibrated',
        'calibration_version': CONTEXT['calibration_version'],
    }
    data = {
        'products': {str(PRODUCT_A): {'id': PRODUCT_A, 'sku': 'SKU-A', 'name': 'Producto A'},
                     str(PRODUCT_B): {'id': PRODUCT_B, 'sku': 'SKU-B', 'name': 'Producto B'}},
        'stock': {str(PRODUCT_A): STOCK_A, str(PRODUCT_B): STOCK_B},
        'roi_by_product': {str(PRODUCT_A): [roi_row]},  # ROI asignado a A
    }
    user = dict(id=UUID('11111111-1111-4111-8111-111111111111'), is_superuser=True,
                role='sudo', organization_id=ORG, branch_id=BRANCH)

    print('\n--- PS-10: roi_occ bajo no modifica el inventario lógico ---')
    log_a = []
    with patch('app.telemetry.httpx.Client',
               side_effect=lambda *a, **kw: make_influx_mock([])), \
         patch.object(InfluxSettings, 'from_env', return_value=settings):
        out_a = availability_endpoint(data, log_a)(BRANCH, PRODUCT_A, user)
    logical_a = out_a['logical']['quantity_on_hand']
    visual_a = out_a['visual']
    print(f'  logical={logical_a}, visual={[(v["occupancy_pct"], v["status"]) for v in visual_a]}')
    check('stock lógico intacto tras roi_occ bajo', logical_a == STOCK_A)
    check('la zona visual muestra 5 %', len(visual_a) == 1 and visual_a[0]['occupancy_pct'] == LOW_OCC)
    check('bloques logical y visual son independientes',
          set(out_a) >= {'logical', 'visual', 'checked_at'})
    assert_no_writes(log_a, 'PS-10')
    sql_a = ' '.join(log_a)
    check('la consulta de disponibilidad usa la columna real s.location_label',
          's.location_label' in sql_a)

    print('\n--- PS-11: remapear el ROI a otro SKU no cambia existencias ---')
    data['roi_by_product'] = {str(PRODUCT_B): [roi_row]}  # remapeo A -> B
    log_old = []
    with patch('app.telemetry.httpx.Client',
               side_effect=lambda *a, **kw: make_influx_mock([])), \
         patch.object(InfluxSettings, 'from_env', return_value=settings):
        out_old = availability_endpoint(data, log_old)(BRANCH, PRODUCT_A, user)
    print(f'  producto A (antes del ROI): visual={out_old["visual"]}, logical={out_old["logical"]["quantity_on_hand"]}')
    check('el producto anterior ya no muestra la zona visual', out_old['visual'] == [])
    check('el producto anterior conserva su stock', out_old['logical']['quantity_on_hand'] == STOCK_A)

    log_new = []
    with patch('app.telemetry.httpx.Client',
               side_effect=lambda *a, **kw: make_influx_mock([])), \
         patch.object(InfluxSettings, 'from_env', return_value=settings):
        out_new = availability_endpoint(data, log_new)(BRANCH, PRODUCT_B, user)
    print(f'  producto B (ahora con el ROI): visual={len(out_new["visual"])} zona(s), '
          f'logical={out_new["logical"]["quantity_on_hand"]}')
    check('el producto nuevo muestra la zona visual', len(out_new['visual']) == 1)
    check('el producto nuevo conserva su propio stock', out_new['logical']['quantity_on_hand'] == STOCK_B)
    assert_no_writes(log_old + log_new, 'PS-11')

    print('\nRESULTADO: PS-10 y PS-11 PASS (modo mock). '
          'Falta repetir con Influx real y placa física.\n')


def influx_write(args):
    env = {k: os.environ.get('INFLUXDB_' + k, '').strip()
           for k in ('URL', 'TOKEN', 'ORG', 'BUCKET')}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise SystemExit('Faltan variables de entorno: ' + ', '.join('INFLUXDB_' + k for k in missing))

    ts_ns = int((datetime.now(timezone.utc) - timedelta(seconds=2)).timestamp() * 1_000_000_000)
    line = (
        'roi_occ,'
        f'branch_id={args.branch_id},esp_id={args.node_id},module=shelf_cam,'
        f'roi_id={args.roi_id},config_version={args.config_version},'
        f'calibration_version={args.calibration_version},'
        f'event_id=emulador-{ts_ns},source=prod,status=ok '
        f'value={args.value} {ts_ns}'
    )
    url = env['URL'].rstrip('/') + '/api/v2/write'
    print('Escribiendo punto en InfluxDB real:\n  ' + line)
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        resp = client.post(url,
                           params={'org': env['ORG'], 'bucket': env['BUCKET'], 'precision': 'ns'},
                           headers={'Authorization': 'Token ' + env['TOKEN'],
                                    'Content-Type': 'text/plain; charset=utf-8'},
                           content=line.encode())
    if resp.status_code >= 300:
        raise SystemExit(f'InfluxDB respondió {resp.status_code}: {resp.text}')
    print(f'OK {resp.status_code}. Ahora consulta /api/v1/branches/{{branch}}/products/{{id}}/availability '
          'y comprueba que el stock lógico no cambió.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--mock', action='store_true', help='validar con Influx simulado (por defecto)')
    mode.add_argument('--influx-write', action='store_true', help='escribir punto real en InfluxDB')
    p.add_argument('--branch-id', type=UUID, default=BRANCH)
    p.add_argument('--node-id', type=UUID, default=NODE)
    p.add_argument('--roi-id', type=UUID, default=ROI)
    p.add_argument('--config-version', type=int, default=CONTEXT['config_version'])
    p.add_argument('--calibration-version', type=int, default=CONTEXT['calibration_version'])
    p.add_argument('--value', type=int, default=LOW_OCC, help='ocupación % a emular')
    args = p.parse_args()
    if args.influx_write:
        influx_write(args)
    else:
        validate_mock()


if __name__ == '__main__':
    main()

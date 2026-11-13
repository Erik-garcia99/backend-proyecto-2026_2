# Aforo: LD2450 → ESP32 → MQTT → RPI → InfluxDB → API

Esta integración publica la **presencia instantánea** en la zona de la puerta
(hasta 3 objetivos simultáneos que el LD2450 puede detectar a la vez) y la
expone al backend. **No es el aforo de la tienda**: el aforo de negocio
(personas dentro) se calcula por cruces de entrada/salida, más ajustes manuales.
El frontend queda para la siguiente fase.

## Flujo

```
HLK-LD2450 --UART--> ESP32 --MQTT--> Raspberry --SQLite--> InfluxDB
                                                              |
                                             FastAPI <--------+
                                                |
                                       GET /branches/{id}/telemetry/occupancy/latest
```

La Raspberry usa el mismo `influx_sync.event_to_line()` para cualquier evento
numérico `data.value`, con `event_type` como measurement. Antes de escribir en
InfluxDB autoriza el nodo contra el backend: solo las altas confirmadas (con
`node_code`) envían mediciones, y la API también filtra por esos `esp_id`.

## 1. ESP32

Cada `LD2450_TELEMETRY_INTERVAL_MS` (2000 ms por defecto, en `ld2450.h`) publica
una muestra aunque el valor no cambie. Así el backend distingue "sigue en 0" de
"el nodo se cayó" usando `age_seconds`.

- Topic: `selfiq/<branch_id>/<esp_id>/capacity_atmosphere/telemetry`
- Payload:

```json
{
  "event_id": "esp-a1b2c3d4e5f6-0000000100000002-00000003",
  "event_type": "occupancy",
  "uptime_ms": 123456,
  "data": { "value": 2 }
}
```

`data.value` es `ld2450_occupancy()`: conteo estable `0..3` de objetivos
presentes en la ROI en ese instante, con debounce de 5 frames (~0.5 s).
**Es presencia instantánea en la zona del radar, no el aforo de la tienda.**
El LD2450 solo detecta hasta 3 objetivos a la vez, por lo que este valor no
puede representar la ocupación total del comercio. El aforo de negocio
(personas dentro) se obtiene de los eventos `entry`/`exit` (cruces de línea),
acumulados en NVS y ajustables manualmente; ver `PLAN_AFORO_LD2450.md` §1 y §3.4.

Para cambiar la frecuencia, edita `LD2450_TELEMETRY_INTERVAL_MS`. Ten en cuenta
F-05: el conteo debe reflejarse en <= 2 s, así que no conviene subirlo de 2000 ms.

## 2. RPI → InfluxDB

La RPI ya convierte el evento en line protocol:

```text
occupancy,branch_id=<uuid>,esp_id=<uuid>,module=capacity_atmosphere,event_id=<id> value=2.0 <ns>
```

- Measurement: `occupancy`
- Campo: `value`
- Tags: `branch_id`, `esp_id`, `module`, `event_id`

No hay que configurar nada nuevo en la RPI ni en el `.env` de InfluxDB.

## 3. Backend

### Variables opcionales

Se suman a las de temperatura (`INFLUXDB_VERSION`, `INFLUXDB_URL`,
`INFLUXDB_TOKEN`, `INFLUXDB_BRANCH_TAG`, etc.). Si no existen, se usan estos
valores, que coinciden con lo que escribe la RPI:

| Variable | Valor por defecto |
| --- | --- |
| `INFLUXDB_OCCUPANCY_MEASUREMENT` | `occupancy` |
| `INFLUXDB_OCCUPANCY_FIELD` | `value` |
| `INFLUXDB_OCCUPANCY_STALE_SECONDS` | `20` |

`20` s equivale a 10 muestras perdidas a 2 s. Si la última lectura es más vieja,
el estado pasa a `stale`.

### Endpoint

`GET /branches/{branch_id}/telemetry/occupancy/latest`  
Requiere `Authorization: Bearer <sesión de la app>` (mismos permisos que
temperatura).

```json
{
  "branch_id": "11111111-1111-4111-8111-111111111111",
  "occupancy": 2,
  "measured_at": "2026-10-28T20:15:04.250000Z",
  "age_seconds": 3,
  "stale_after_seconds": 20,
  "status": "ok"
}
```

- `status`: `ok`, `stale` o `no_data`.
- Sin muestras: `occupancy` y `measured_at` son `null`, `status = "no_data"`;
  no se inventa un cero.
- Valores no enteros, negativos o > 100 se rechazan con `502`.
- `Cache-Control: no-store`.

Mismos códigos de error que temperatura: `503` (configuración/token/cuota),
`502` (esquema o respuesta inválida), `504` (timeout), `403` (sucursal ajena).

## 4. Verificación

```bash
# Backend (sin credenciales reales)
python -m unittest tests.test_telemetry -v

# Contrato RPI -> line protocol
cd ../firmware_RPI_SelfIQ-26-2/SelfIQ_RPI
python3 -c "from selfiq.influx_sync import event_to_line; ..."
```

En el ESP32, el monitor serie debe mostrar `aforo estable=N`. En la RPI, el
`outbox` debe bajar de pendientes y en InfluxDB Data Explorer debe aparecer el
measurement `occupancy`.

## Alcance

- Se implementa la lectura de la **presencia instantánea** (0..3 objetivos en la
  ROI) para el backend. No representa el aforo de la tienda.
- El **aforo de negocio** (personas dentro) se gestiona por eventos `entry`/`exit`
  (cruces de línea), acumulados en NVS y ajustables manualmente vía
  `POST /branches/{branch}/occupancy/adjustments` (gateway `occupancy.adjust`).
- Entradas/salidas acumuladas, histórico por franja y tarjeta de frontend quedan
  pendientes (A6 del plan de aforo).
- El LD2450 no identifica personas; con varias personas cruzando a la vez el
  conteo instantáneo puede oscilar. El tracker con línea e histéresis corrige eso.

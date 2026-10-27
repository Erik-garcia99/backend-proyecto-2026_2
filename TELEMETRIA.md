# Temperatura: ESP32 → Raspberry → InfluxDB Cloud → API → app

La Raspberry sigue escribiendo como hasta ahora. La API **solo lee** InfluxDB y
la tarjeta Temperatura de Inicio consulta la API cada 15 s mientras está activa.
No se modifican firmware, MQTT, registros de nodos ni esquema de PostgreSQL.

## 1. Identificar tu InfluxDB y los datos existentes

Antes de llenar variables, abre la consola de InfluxDB:

- **Cloud TSM / Storage Engine Version 2** y consultas Flux: `INFLUXDB_VERSION=2`.
- **Cloud Serverless / Storage Engine Version 3**: `INFLUXDB_VERSION=3`.
  Se usa su API HTTP compatible `/query` con InfluxQL, no `/api/v2/query`.
  Esta opción no está diseñada para Cloud Dedicated ni otros productos v3.
- Que la Raspberry escriba en `/api/v2/write` **no demuestra** que sea Cloud 2.

En Data Explorer, o en el código de escritura de la Raspberry, identifica:

1. Bucket; organización para v2 o base de datos mapeada al bucket para v3.
2. Measurement (nombre de la serie/tabla).
3. Campo numérico de temperatura, en **grados Celsius**.
4. Tag que identifica la sucursal. Su valor debe coincidir exactamente con el
   UUID de `branch.id` en PostgreSQL.

Ejemplo **ilustrativo**, no se presupone que tu Raspberry use estos nombres:

```text
environment,branch_id=11111111-1111-4111-8111-111111111111,esp_id=esp32-01 temperature_c=25.5
```

Con ese ejemplo: measurement=`environment`, campo=`temperature_c`, tag=`branch_id`.
Si tu campo se llama `temperature` o `temp`, configura ese nombre sin renombrar datos.
Si el tag se llama `sucursal_id`, usa `INFLUXDB_BRANCH_TAG=sucursal_id`.
**Si tus datos solo tienen MAC/esp_id/gateway_id y no un UUID de sucursal, esta
consulta no encontrará lecturas.** Hay que acordar el mapeo con los nodos de
PostgreSQL o agregar el tag de sucursal al envío; no se consultará todo el bucket
sin filtro, para evitar mezclar empresas. No uses un tag de nodo como si fuera sucursal.

Para v3, revisa el mapeo DBRP del API `/query`: `INFLUXDB_DATABASE` es la base
mapeada, y `INFLUXDB_RETENTION_POLICY` su política si hace falta especificarla.
No siempre basta con copiar el nombre del bucket.

## 2. Credenciales locales (no se suben)

Archivo privado: **`backend-proyecto-2026_2/.env`**, junto a `requirements.txt`.
Ya existe en este proyecto: **no lo sobrescribas**; conserva `DATABASE_URL` y
`JWT_SECRET`. Agrega las variables de [`.env.example`](.env.example), sustituyendo
los ejemplos. La plantilla no contiene secretos y sí puede subirse a GitHub.

El backend carga `.env` al arrancar. Las variables ya definidas en el proceso
tienen prioridad. `.gitignore` excluye `.env` y `.env.*` salvo `.env.example`;
`.dockerignore` impide copiarlos dentro de la imagen.

Crea un token **nuevo, exclusivo del backend y con permiso de lectura únicamente**
en ese bucket. No reutilices el token de escritura de la Raspberry, ni uses un
token administrativo. Aquí no se necesitan tu contraseña ni usuario de InfluxDB.
No pegues tokens en código, capturas, chats o archivos versionados. Si alguna vez
se publicó un secreto, ignorar el archivo no borra su historial: revoca el secreto.

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Con Docker Compose, las variables de InfluxDB del `.env` también se pasan al contenedor.

## 3. Railway: nombres exactos

En **proyecto → servicio del backend → Variables**, añade:

| Variable | Valor |
| --- | --- |
| `INFLUXDB_VERSION` | `2` o `3`, según la consola |
| `INFLUXDB_URL` | URL base HTTPS de tu región, sin ruta `/api/...` ni parámetros |
| `INFLUXDB_TOKEN` | Token exclusivo de lectura |
| `INFLUXDB_MEASUREMENT` | Measurement/tabla exacta |
| `INFLUXDB_TEMPERATURE_FIELD` | Nombre exacto del campo numérico en °C |
| `INFLUXDB_BRANCH_TAG` | Tag cuyo valor es el UUID de sucursal |
| `INFLUXDB_ORG` | Solo v2: nombre o ID de organización |
| `INFLUXDB_BUCKET` | Solo v2: nombre del bucket |
| `INFLUXDB_DATABASE` | Solo v3: base de datos mapeada al bucket |
| `INFLUXDB_RETENTION_POLICY` | Solo v3: opcional, política del mapeo DBRP |
| `INFLUXDB_LOOKBACK_HOURS` | Opcional: `24` (entre 1 y 168 horas) |
| `INFLUXDB_STALE_SECONDS` | Opcional: `90` (entre 30 y 86400 segundos) |
| `INFLUXDB_TIMEOUT_SECONDS` | Opcional: `10` (entre 1 y 12 segundos) |

Conserva las variables actuales `DATABASE_URL` y `JWT_SECRET`.
Aplica los cambios y redespliega el backend para instalar dependencias y tomar
las variables. Railway las inyecta: **no subas el archivo `.env`**.

En el frontend, lo único necesario es su variable pública existente:

```dotenv
EXPO_PUBLIC_API_URL=https://tu-backend.up.railway.app
```

**Nunca uses `EXPO_PUBLIC_INFLUXDB_TOKEN`**: todo `EXPO_PUBLIC_*` es visible en la app.
Las variables de InfluxDB deben estar solamente en el servicio **backend**, no en
el frontend ni en el ESP32. Usa credenciales diferentes en desarrollo y producción.

## 4. Endpoints y verificación

Ambos exigen `Authorization: Bearer <sesión de la app>`:

- `GET /telemetry/branches`: sucursales activas que puede consultar el usuario.
- `GET /branches/{branch_id}/telemetry/temperature/latest`: lectura más reciente
  de temperatura en la ventana configurada. No promedia sensores; si hay varios
  en la sucursal devuelve el más reciente.

Empleado/usuario asignado: solo su sucursal; propietario: las de su organización;
SUDO: todas. Se comprueban permisos antes de consultar InfluxDB.
Puedes probarlos con **Authorize** en `/docs` del backend usando el JWT de la app,
no el token de InfluxDB.

La respuesta incluye `temperature_c`, `measured_at` en UTC, `age_seconds`,
`stale_after_seconds` y `status` (`ok`, `stale`, `no_data`). Sin lecturas en la
ventana devuelve `200`, valores `null` y `no_data`: no inventa un cero.
La app convierte la hora a la zona del dispositivo y marca lecturas antiguas.
Un error de actualización conserva la última lectura visible con advertencia;
al cambiar sucursal no muestra la lectura de la anterior.

- `503`: faltan variables, token/permisos incorrectos o cuota de InfluxDB agotada.
- `502`: revisar versión, measurement, campo, tag y, en v3, mapeo DBRP.
- `504`: tiempo de espera agotado.
- `403`: el usuario no pertenece a la sucursal autorizada.

## Alcance y pruebas

Se implementa la visualización de **temperatura del sensor instalado** (modelo
por confirmar: DHT11/DHT22 u otro), no humedad,
históricos, alertas push, LWT ni monitoreo de conexión del sensor.
Una lectura vieja no demuestra por sí sola que un nodo esté desconectado.
F-07 del PDF menciona **DHT22 y humedad**: esta integración parcial no implica
cumplir todo ese requerimiento. Se muestra un aviso local sobre 40 °C.
Se siguen los criterios de secretos fuera de Git y acceso autorizado (SEC-08,
SEC-12, SEC-14, SEC-15, SEC-16), sin auditar el resto del sistema.

```bash
python -m unittest discover -s tests -v
```

Las pruebas simulan InfluxDB y la base de datos; no usan credenciales reales.
Para cerrar la verificación de punta a punta, configura tus variables, confirma
el tag de sucursal y compara una lectura de Data Explorer con la app.

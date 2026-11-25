# SelfIQ — Backend API

Servicio central en la nube para el sistema **SelfIQ**, desarrollado en **Python** con **FastAPI**, **PostgreSQL (Supabase)** e **InfluxDB Cloud**.

Este repositorio contiene **exclusivamente el código del servicio backend**: la API RESTful consumida por los frontends (móvil y web), el hub WebSocket que mantiene la conexión bidireccional con los gateways locales de cada sucursal, y la capa de persistencia y lógica transaccional del sistema.

---

## 📌 Contexto en el Sistema (Resumen)

SelfIQ utiliza un modelo híbrido donde los dispositivos de campo (microcontroladores ESP32) no se conectan a internet, sino que dialogan localmente por MQTT con una Raspberry Pi (gateway de sucursal). 

El rol de este backend dentro del ecosistema es:

```text
[ Frontends: Web / Móvil ]           [ Gateway Local: Raspberry Pi ]
          │                                          │
          │ REST (HTTPS)                             │ WebSocket seguro (WSS)
          ▼                                          ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        BACKEND API (FastAPI)                           │
│  - Autenticación, JWT y RBAC         - Hub WebSocket de gateways       │
│  - Transacciones de venta e inventario - Despacho de guía lumínica     │
│  - Topología de repisas y productos    - Auditoría y bitácora          │
└──────────────────┬─────────────────────────────────┬───────────────────┘
                   │                                 │
                   ▼                                 ▼
       [ PostgreSQL (Supabase) ]             [ InfluxDB Cloud ]
       Datos transaccionales, ACID           Series temporales de aforo
       y relaciones del negocio              y clima (solo lectura)
```

---

## ⚙️ Capacidades y Lógica Implementada

El backend concentra las reglas de negocio del sistema, agrupadas en las siguientes áreas:

### 1. Autenticación y Control de Accesos (RBAC)
- Autenticación basada en **JWT** (algoritmo HMAC-SHA256) con tokens de acceso y refresco.
- Hashing de contraseñas con **Argon2id** (y compatibilidad con Bcrypt).
- Jerarquía de roles acotada a nivel de sucursal (`sudo`, `owner`, `manager`, `rh`, `employee`, `customer`).
- Flujo de doble aprobación para altas de personal gerencial y revocación inmediata de sesiones activas (`auth_session`).

### 2. Punto de Venta (POS) y Gestión de Inventario
- **Transaccionalidad estricta (ACID):** Descuento atómico de existencias con bloqueo de fila (`SELECT ... FOR UPDATE`) en `branch_inventory` para prevenir sobreventas en entornos concurrentes.
- **Garantía de idempotencia:** Operaciones de venta asociadas a una clave única (`UNIQUE (branch_id, idempotency_key)`); solicitudes duplicadas por reintentos de red retornan la venta original sin duplicar cobros ni movimientos de inventario.
- **Seguridad en caja:** Bloqueo temporal que exige el encabezado `X-POS-Unlock` con un token de sesión de caja (validez de 20 min).
- Soporte para devoluciones parciales/totales y anulación de tickets con registro en la bitácora de auditoría.

### 3. Hub WebSocket de Gateways y Despacho de Comandos
- Mantiene conexiones persistentes (`/api/v1/gateways/ws`) con los gateways de sucursal.
- **Guía lumínica LED:** Traduce una solicitud de búsqueda de producto al mapeo físico del estante y despacha la orden lumínica hacia el gateway correspondiente para su ejecución en tiempo real en los LEDs WS2812B.
- Registro, confirmación y sincronización de estado de nodos ESP32 autorizados.

### 4. Capa de Lectura de Telemetría (InfluxDB)
- La API **únicamente lee** de InfluxDB (la escritura la realiza el gateway).
- Soporte para **InfluxDB v2** (consultas Flux) y **v3 / Serverless** (consultas InfluxQL vía endpoint `/query` con DBRP).
- Vinculación obligatoria: solo consulta métricas de nodos confirmados y registrados en la sucursal del usuario que consulta. Para más detalles, consultar [TELEMETRIA.md](TELEMETRIA.md) y [TELEMETRIA_AFORO.md](TELEMETRIA_AFORO.md).

### 5. Estantes, Calibración y Visión (ROI)
- Administración de repisas físicas: niveles, slots y dimensiones.
- Asignación de productos y regiones de interés (ROI) para supervisión de presencia de producto.
- Registro de sesiones de calibración para cámaras de repisa (ESP32-CAM).

### 6. Auditoría y Bitácora del Sistema
- Registro inmutable de eventos sensibles en `audit_line` (cambios de precios, altas de usuarios, movimientos manuales de inventario, etc.).
- Filtros por sucursal, actor, entidad y exportación en formato CSV.

---

## 📁 Organización del Código (`app/`)

```text
app/
├── main.py                 # Instancia de FastAPI, middlewares (CORS) y montaje de routers
├── config.py               # Carga de variables de entorno (.env) y constantes
├── db.py                   # Pool de conexiones a PostgreSQL vía psycopg 3
├── security.py             # Generación y validación de tokens JWT, hashing de contraseñas
├── permissions.py          # Dependencias de autorización y validación de roles
├── schemas.py              # Esquemas declarativos de validación Pydantic generales
├── schemas_pos.py          # Esquemas Pydantic del módulo POS y ventas
│
├── auth/ (en main/security)# Endpoints de login, registro, refresco y logout
├── gateways.py             # Router y endpoint WebSocket para comunicación con gateways
├── gateway_registry.py     # Gestor en memoria de conexiones activas de gateways
├── devices.py              # CRUD y administración de nodos ESP32
├── node_registration.py    # Flujo de vinculación de hardware por token
├── telemetry.py            # Adaptador de lectura y cálculo de métricas sobre InfluxDB
├── guide.py                # Lógica y endpoints para la guía lumínica de repisa
├── pos.py                  # Endpoints de ventas, cobros, devoluciones y caja POS
├── shelf_products.py       # Asignación de productos a estantes
├── physical_config.py      # Configuración de especificaciones físicas de estantes
├── rois.py                 # Definición y consulta de ROIs de estantes
├── calibrations.py         # Sesiones de calibración de cámaras de visión
├── roi_classification.py   # Clasificación de ocupación visual
├── alerts.py               # Gestión de alertas del sistema
├── visual_alerts.py        # Detección de episodios anómalos visuales
├── people.py               # Administración de usuarios, personal y vendedores POS
├── logs.py                 # Consulta y exportación de bitácoras de auditoría
└── domain/                 # Entidades y reglas puras de dominio (ej. reglas de inventario)
```

---

## 🛠 Requisitos y Configuración Local

### Prerrequisitos
- **Python 3.11+** (probado en 3.12 y 3.14).
- Una base de datos **PostgreSQL** accesible (instancia en Supabase o contenedor local).
- Opcional: Instancia de **InfluxDB Cloud v2/v3** si se desea probar la lectura de telemetría en vivo.

### Variables de Entorno (`.env`)

Crea un archivo `.env` en la raíz de este directorio (`backend-proyecto-2026_2/.env`):

```bash
# Conexión a PostgreSQL (Supabase o local)
DATABASE_URL="postgresql://usuario:password@host:5432/nombre_db?sslmode=require"

# Secreto para firma de JWT (mínimo 32 caracteres aleatorios)
JWT_SECRET="tu-clave-secreta-para-firmar-jwt"

# Opcionales de sesión
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=30
CUSTOMER_ORGANIZATION_SLUG=""

# Opcionales de InfluxDB (para telemetría de aforo y temperatura)
INFLUXDB_VERSION=2
INFLUXDB_URL="https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUXDB_TOKEN="token_de_lectura_influx"
INFLUXDB_ORG="tu_organizacion"
INFLUXDB_BUCKET="tu_bucket"
```

---

## 🚀 Instalación y Ejecución

```bash
# 1. Crear y activar el entorno virtual
python3 -m venv .venv
source .venv/bin/activate

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Iniciar el servidor de desarrollo
uvicorn app.main:app --reload --port 8000
```

El servicio estará disponible en `http://localhost:8000`.

---

## 🗄 Base de Datos y Scripts de Migración

El proyecto no utiliza un ORM pesado ni herramientas automáticas de migración (como Alembic); el acceso a datos se realiza mediante **SQL explícito con `psycopg 3`** para garantizar rendimiento y control total sobre bloqueos e índices.

Las migraciones son scripts SQL idempotentes ubicados en `scripts/`:

```bash
# Aplicar esquema base:
psql "$DATABASE_URL" -f ../docs/selfiq_esquema_actual.sql

# O aplicar scripts de evolución específicos según la funcionalidad:
psql "$DATABASE_URL" -f scripts/migrate_devices.sql
psql "$DATABASE_URL" -f scripts/migrate_shelf_products.sql
psql "$DATABASE_URL" -f scripts/migrate_me2a_physical_config.sql
psql "$DATABASE_URL" -f scripts/migrate_alerts.sql
psql "$DATABASE_URL" -f scripts/migrate_pos.sql
psql "$DATABASE_URL" -f scripts/migrate_pos_seller.sql

# Verificar integridad del esquema POS:
psql "$DATABASE_URL" -f scripts/verify_pos_schema.sql
```

---

## 🧪 Pruebas Automatizadas

La suite de pruebas utiliza `unittest` (con soporte para assertions de dominio con `pytest`) y cuenta con más de **340 casos de prueba**.

La mayoría de las pruebas utilizan mocks para la base de datos e InfluxDB, por lo que no requieren una base de datos real para ejecutarse:

```bash
# Ejecutar toda la suite de pruebas desde el directorio backend:
DATABASE_URL='postgresql://user:pass@host/db' JWT_SECRET='test-secret' \
  .venv/bin/python -m unittest discover -s tests -v

# Ejecutar un archivo específico:
DATABASE_URL='postgresql://user:pass@host/db' JWT_SECRET='test-secret' \
  .venv/bin/python -m unittest discover -s tests -p 'test_inventory_rules.py' -v
```

> 💡 **Pruebas de Integración POS (`test_sales_integracion.py`):**
> Estas pruebas validan concurrencia real y bloqueos con `FOR UPDATE`. Requieren definir la variable de entorno `POS_TEST_DB=1` y apuntar `DATABASE_URL` a una base de datos cuyo nombre contenga la palabra `"test"`, evitando cualquier alteración accidental en bases de datos de desarrollo o producción.

---

## ☁️ Despliegue en Producción (Railway)

El backend está configurado para desplegarse en **Railway**.

> ⚠️ **Requisito Crítico:** El servicio debe ejecutarse siempre con **un solo worker** (`--workers 1`):
> ```bash
> uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
> ```
> **Motivo:** El hub de WebSockets con los gateways de sucursal (`gateway_registry.py`) y el estado de búsqueda de hardware residen en la memoria del proceso. Ejecutar múltiples workers fragmentaría las conexiones WebSocket, provocando que los comandos de control (como la activación de la guía LED) no lleguen al gateway conectado.

---

## 📖 Documentación Interactiva

FastAPI genera automáticamente la documentación interactiva a partir de las rutas y modelos Pydantic:

- **Swagger UI:** `http://localhost:8000/docs` (o `/docs` en la URL de Railway).
- **ReDoc:** `http://localhost:8000/redoc`.

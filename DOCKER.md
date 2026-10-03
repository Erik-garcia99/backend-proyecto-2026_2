# SelfIQ con Docker

## Inicio

```bash
cp .env.example .env
docker compose up --build
```

Docker inicia PostgreSQL y el backend FastAPI. En la primera ejecución se aplican
automáticamente `selfiq_sprint1_postgresql.sql` y `scripts/seed_auth.sql`.

- API: `http://localhost:8000`
- Documentación: `http://localhost:8000/docs`
- PostgreSQL: `localhost:5432`

## Datos iniciales

- SUDO: `sudo@selfiq.local` / `Sudo123!`
- Administrador: `admin@selfiq.local` / `Admin123!`
- Token de registro: `token_prueba_123`
- Token para crear sucursales: `token_sucursal_123`

Estas credenciales son únicamente para desarrollo.

Si la base de datos ya existía antes de esta funcionalidad, aplica una vez:

```bash
docker exec -i selfiq-postgres psql -U selfiq -d selfiq < scripts/migrate_branch_creation.sql
```

> Nota: las cuentas usan el dominio `.local` intencionalmente. El backend
> acepta ese dominio únicamente como correo de desarrollo; no uses estas
> credenciales en producción.

## Reiniciar la base de datos

```bash
docker compose down -v
docker compose up --build
```

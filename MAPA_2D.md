# Mapa móvil — primera entrega: borrador privado

27/11/2026. Implementación local, no desplegada por el agente.

## Alcance de esta entrega

- Backend: permiso específico, lectura/guardado del borrador por sucursal,
  validación geométrica, versión optimista y bitácora transaccional.
- Móvil: Panel → Mapa del local, ancho/largo relativos, guardar y recargar.
  Advierte al salir con cambios y al reemplazarlos por una recarga.
- Sin editor gráfico, asociaciones, publicación ni visor cliente todavía.
- No se modifica web, POS, cámara/ROI, calibración, gateway ni firmware.

## Esquema y migración

Referencia del despliegue proporcionada por el operador:
`../database/schema_current.sql`, tablas `store_map`, `shelf`, `branch`,
`user`, `system_log`. Es un esquema de contexto, **no ejecutarlo como SQL**.

Reutilizamos `store_map`; no creamos un segundo almacén de mapas.
`scripts/migrate_map_draft.sql` agrega:

- `draft_document jsonb`, inicialmente NULL.
- `draft_version integer`, inicialmente 0.
- `draft_updated_by uuid`, `draft_updated_at timestamptz`.

Las dimensiones antiguas `width_cm` y `height_cm` pasan a admitir NULL para
permitir una fila nueva de solo borrador sin inventar un plano publicado.
Sus valores existentes, `version`, `updated_by`, `updated_at` y todas las
coordenadas `shelf.map_*` se conservan. No se convierten automáticamente a
unidades relativas, ni se presentan como mapa publicado en esta entrega.
Los futuros lectores del publicado no deben confundir una fila de solo
borrador con una publicación.

**Orden:** respaldar/probar en BD de prueba → ejecutar migración → desplegar
backend → actualizar móvil. El agente no ha ejecutado la migración.

Desde este repositorio, con las credenciales ya configuradas por el operador:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrate_map_draft.sql
```

Es idempotente y transaccional. Probar dos ejecuciones, conservación de
valores antiguos y dos primeras escrituras concurrentes en PostgreSQL real:
las pruebas unitarias usan una conexión falsa y no certifican el SQL.
Aquí no hay binarios PostgreSQL disponibles para esa comprobación local.

Para volver al backend anterior no borrar columnas ni borradores. Las
columnas nuevas pueden conservarse. Restaurar NOT NULL de las dimensiones
antiguas exigiría tratar antes las filas nuevas con dimensiones NULL; no
rellenarlas con medidas ficticias ni eliminarlas automáticamente.

## Permisos

`can_edit_map` / `canEditMap` permiten owner, manager, gerencia y sudo o
superusuario. RRHH no edita aunque conserve administración en otros módulos.
Tanto GET como PUT del borrador exigen ese permiso.

Se conserva el alcance existente: manager en su sucursal, owner/gerencia
en sucursales de su organización, sudo con su alcance de superusuario.
Sucursales/organizaciones inactivas no están disponibles.
Cliente, employee y staff no acceden al borrador. La consulta pública/autorizada
del mapa publicado se incorporará por separado, nunca abriendo este endpoint.

## Contrato

Sin prefijo POS `/api/v1`.

### GET `/branches/{branch_id}/map/draft`

Sin borrador devuelve 200 con `version: 0`, `document: null` y metadatos NULL.
No crea filas al consultar. Una fila legacy sin borrador se trata igual.

### PUT `/branches/{branch_id}/map/draft`

```json
{
  "base_version": 0,
  "document": {
    "schema_version": 1,
    "width": 100,
    "height": 80,
    "elements": []
  }
}
```

Respuesta en ambas rutas:

```json
{
  "branch_id": "UUID de sucursal",
  "version": 1,
  "document": {"schema_version": 1, "width": 100, "height": 80, "elements": []},
  "updated_by": "UUID del editor",
  "updated_at": "fecha ISO 8601 con zona horaria"
}
```

- Cabecera `Cache-Control: no-store`.
- Área y dimensiones de figuras: números finitos de 1 a 10000, unidades
  relativas (no centímetros, metros, m² ni píxeles de la pantalla).
- Máximo 200 elementos; orden del array = orden de dibujo.
- Elemento: `id` UUID estable/único, `kind`, `label` opcional (máx. 80),
  `x`, `y`, `width`, `height`, `rotation` opcional (0 por defecto).
- `kind`: `rectangle`, `ellipse`, `hexagon`, `wall`, `door`, `double_door`, `window`.
  Un cuadrado/círculo usa el mismo ancho y alto que su rectángulo/elipse.
- `x/y`: esquina superior izquierda antes del giro; giro sobre el centro,
  grados en `[0,360)`. Validación conservadora: toda la caja girada debe
  caber en el área. `wall`/puertas/ventanas usan una caja de espesor positivo;
  su representación gráfica se añadirá después.
- Campos desconocidos se rechazan. `shelf_id` y asociaciones aún no forman
  parte de este contrato; se incorporarán con su validación relacional.
- 403: permiso/alcance rechazado. 404: sucursal inexistente/inactiva.
  422: documento inválido. 409: revisión cambió, recargar antes de sobrescribir.
- No necesita un estante para **guardar borrador**. El mínimo de un estante
  se exigirá al publicar, funcionalidad todavía no implementada.

## Concurrencia y errores

Inserción `ON CONFLICT DO NOTHING` y lectura `FOR UPDATE` serializan también
las primeras escrituras. Comparar `base_version` bajo bloqueo; cada cambio
incrementa la revisión y registra `map.draft_saved` con antes/después.
Si falla la bitácora, se revierte el guardado.

Guardar contenido idéntico **con la revisión vigente** no genera otra
revisión/log. Repetir una petición con revisión antigua devuelve 409,
incluso si la primera se guardó pero se perdió la respuesta: recargar permite
comprobarlo. No se promete una clave de idempotencia de operación en esta entrega.
El móvil evita doble envío; ante 409 conserva los campos locales y exige
recarga confirmada. Renovar sesión no descarta cambios locales.

## Prueba manual pendiente

1. Aplicar/probar migración antes del backend; desplegar con un worker por
   las restricciones existentes de guía/hub.
2. Como editor, seleccionar sucursal y abrir Mapa del local.
3. Guardar área, salir/reabrir y comprobar tamaños y versión.
4. Abrir dos sesiones: guardar desde una; la otra debe recibir conflicto
   y conservar sus campos hasta confirmar recarga.
5. RRHH/employee/staff no ven el acceso; su llamada directa devuelve 403.
6. Probar otra organización/sucursal, red interrumpida y salida con cambios.
7. Ver bitácora y comprobar que no se modificaron mapas legacy,
   estantes ni asociaciones de hardware.

No declarar esta entrega aceptada en teléfono/BD real solo por sus mocks.
Después de revisarla: visor/editor móvil del creador, asociaciones y publicación
en entregas separadas.

## Verificación local del 27/11

- Backend del borrador: **12/12** pruebas.
- Suite general backend: 357 casos, 356 sin errores y 1 error de importación
  ajeno al mapa: `test_sales_integracion.py` lanza `pytest.skip` bajo
  `unittest` al no habilitar BD POS de prueba. No se habilitó POS ni se
  cambiaron sus pruebas para ocultar el resultado.
- Móvil: **166/166** pruebas; incluye 5 pruebas de interacción del borrador
  (doble toque, conflicto/recarga, token/salida y respuestas tras desmontar).
- Lint móvil: **0 errores / 177 advertencias** en archivos existentes;
  ninguna advertencia en los archivos nuevos.
- No se comprobó SQL real ni operación en celular físico.

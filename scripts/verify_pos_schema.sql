-- Verificación de solo lectura del POS (Fases 0, 1 y 2).
-- Pegar en el SQL Editor de Supabase. NO modifica datos.
-- Para probar la idempotencia de la migración, ejecuta antes DOS VECES
-- scripts/migrate_pos.sql y luego este archivo.

-- 1. Tablas y columnas que el POS necesita -----------------------------------
SELECT table_name, column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_schema = 'public'
   AND table_name = ANY (ARRAY[
       'product', 'branch_inventory', 'sale', 'sale_item',
       'inventory_movement', 'goods_receipt', 'goods_receipt_item',
       'idempotency_key', 'roi', 'roi_state', 'roi_reading', 'shelf_product'
   ])
 ORDER BY table_name, ordinal_position;

-- 2. Índices creados por migrate_pos.sql -------------------------------------
SELECT indexname
  FROM pg_indexes
 WHERE schemaname = 'public'
   AND indexname = ANY (ARRAY[
       'idx_product_org_sku', 'uq_product_org_barcode', 'idx_product_name',
       'idx_branch_inventory_branch', 'idx_inventory_movement_product_date',
       'idx_sale_branch_date', 'idx_sale_item_sale', 'idx_idempotency_key_created'
   ])
 ORDER BY indexname;

-- 3. Constraints de idempotencia e inventario --------------------------------
SELECT conrelid::regclass::text AS tabla, conname, pg_get_constraintdef(oid) AS definicion
  FROM pg_constraint
 WHERE conrelid IN ('public.idempotency_key'::regclass,
                    'public.branch_inventory'::regclass)
 ORDER BY 1, 2;

-- 4. Conteos generales --------------------------------------------------------
SELECT (SELECT count(*) FROM product)              AS productos,
       (SELECT count(*) FROM product WHERE is_active) AS productos_activos,
       (SELECT count(*) FROM branch_inventory)      AS filas_inventario,
       (SELECT count(*) FROM sale)                  AS ventas,
       (SELECT count(*) FROM sale_item)             AS partidas,
       (SELECT count(*) FROM inventory_movement)    AS movimientos,
       (SELECT count(*) FROM idempotency_key)       AS claves_idempotencia;

-- 5. Inventario por sucursal (aquí está el "100") ----------------------------
SELECT b.name AS sucursal,
       count(*) AS productos,
       min(bi.quantity_on_hand) AS stock_min,
       max(bi.quantity_on_hand) AS stock_max,
       sum(bi.quantity_on_hand) AS unidades_totales
  FROM branch_inventory bi
  JOIN branch b ON b.id = bi.branch_id
 GROUP BY b.id, b.name
 ORDER BY productos DESC;

-- 6. Detalle de productos y su stock lógico ----------------------------------
SELECT p.sku, p.name, p.is_active, bi.quantity_on_hand, bi.updated_at
  FROM product p
  LEFT JOIN branch_inventory bi ON bi.product_id = p.id
 ORDER BY p.sku;

-- 7. Productos de prueba sembrados por seed_pos_products.py (si los hay) -----
SELECT count(*) AS productos_seed_pos FROM product WHERE sku LIKE 'POS-%';

-- 8. Ocupación visual: es porcentaje, NO unidades -----------------------------
SELECT s.name AS repisa, r.name AS roi, p.sku, r.visual_capacity_units,
       rs.occupancy_pct, rs.status, rs.last_changed_at
  FROM roi r
  JOIN shelf s ON s.id = r.shelf_id
  LEFT JOIN product p ON p.id = r.product_id
  LEFT JOIN roi_state rs ON rs.roi_id = r.id
 WHERE r.is_active
 ORDER BY s.name, r.name;

-- 9. Integridad mínima (ambos conteos deben ser cero) -------------------------
SELECT 'movimientos con tipo desconocido' AS chequeo, count(*) AS filas
  FROM inventory_movement
 WHERE movement_type NOT IN ('sale', 'sale_void', 'return', 'receipt',
                             'adjustment_in', 'adjustment_out')
UNION ALL
SELECT 'ventas sin partidas', count(*) AS filas
  FROM sale s
 WHERE NOT EXISTS (SELECT 1 FROM sale_item si WHERE si.sale_id = s.id);

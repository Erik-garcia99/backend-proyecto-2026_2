-- SelfIQ — Limpieza de datos de prueba de Fase 3 (ROI configurable)
--
-- Elimina la repisa y los productos de prueba creados por seed_fase3_roi.sql
-- para que la cámara vuelva a aparecer sin repisa asociada (y por tanto no
-- pueda asignar ROI hasta vincular una repisa real).
--
-- Ejecutar con:
--   psql "$DATABASE_URL" -f scripts/cleanup_fase3_seed.sql
-- o en Supabase SQL Editor.

BEGIN;

-- 1) Borrar ROI de las repisas de prueba (si las hubiera).
DELETE FROM public.roi
 WHERE shelf_id IN (
       SELECT id FROM public.shelf
        WHERE name = 'Repisa de prueba Fase 3'
           OR name ILIKE '%Fase 3%'
 );

-- 2) Desvincular y borrar las repisas de prueba.
DELETE FROM public.shelf
 WHERE name = 'Repisa de prueba Fase 3'
    OR name ILIKE '%Fase 3%';

-- 3) Borrar los productos de prueba (por SKU).
DELETE FROM public.product
 WHERE sku IN ('SKU-FASE3-A', 'SKU-FASE3-B');

COMMIT;

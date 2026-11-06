-- SelfIQ — Fase 3 (ROI configurable)
-- Versión de configuración de ROI por repisa.
--
-- Idempotente: se puede ejecutar varias veces sin borrar datos.
-- No toca las tablas roi, roi_reading, roi_state ni roi_calibration.
--
-- Uso:
--   psql "$DATABASE_URL" -f backend-proyecto-2026_2/scripts/migrate_fase3_roi.sql
--
-- La versión deseada la incrementa el backend al guardar el conjunto de ROI.
-- La versión aplicada se actualiza cuando el ESP32-CAM responde roi_ack.

BEGIN;

ALTER TABLE public.shelf
    ADD COLUMN IF NOT EXISTS roi_version integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS roi_applied_version integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS roi_applied_at timestamp with time zone;

COMMENT ON COLUMN public.shelf.roi_version IS
    'F-09/S-03: versión deseada de la configuración de ROI. La incrementa el backend al guardar.';
COMMENT ON COLUMN public.shelf.roi_applied_version IS
    'F-09: última versión de ROI confirmada por el nodo ESP32-CAM mediante roi_ack.';
COMMENT ON COLUMN public.shelf.roi_applied_at IS
    'Fecha y hora del último roi_ack aceptado para esta repisa.';

COMMIT;

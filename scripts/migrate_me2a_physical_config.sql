-- ME-2A, idempotente. Reutiliza columnas existentes; no toca geometría,
-- versiones de roi.set, calibraciones, referencias ni datos NVS.
-- Nota para Supabase SQL Editor: se ejecuta completo de una sola vez.
-- Se usa $me2a$ en vez de $$ y el COMMENT no contiene ';' para que ningún
-- separador de sentencias del cliente parta el cuerpo de la función.

ALTER TABLE product_physical_spec
    ADD COLUMN IF NOT EXISTS config_version integer NOT NULL DEFAULT 0;

ALTER TABLE roi
    ADD COLUMN IF NOT EXISTS physical_config_version integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN roi.physical_config_version IS
    'Revisión de configuración cloud ME-2A, no es roi_version ni confirmación de aplicación física';

CREATE OR REPLACE FUNCTION selfiq_physical_config_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $me2a$
BEGIN
    IF ROW(NEW.visual_capacity_units, NEW.threshold_empty_pct, NEW.threshold_low_pct,
           NEW.threshold_medium_pct, NEW.confirm_samples, NEW.virtual_slots_enabled,
           NEW.slots_rows, NEW.slots_cols)
       IS DISTINCT FROM
       ROW(OLD.visual_capacity_units, OLD.threshold_empty_pct, OLD.threshold_low_pct,
           OLD.threshold_medium_pct, OLD.confirm_samples, OLD.virtual_slots_enabled,
           OLD.slots_rows, OLD.slots_cols) THEN
        NEW.physical_config_version := OLD.physical_config_version + 1;
    END IF;
    RETURN NEW;
END;
$me2a$;

DROP TRIGGER IF EXISTS trg_physical_config_revision ON roi;

CREATE TRIGGER trg_physical_config_revision
BEFORE UPDATE ON roi
FOR EACH ROW EXECUTE FUNCTION selfiq_physical_config_revision();

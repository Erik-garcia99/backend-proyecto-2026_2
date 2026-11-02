-- Dispositivos del panel: datos editables y nombre de cada luz de la guía.
-- Idempotente; conserva nodos, estantes y lecturas existentes.
BEGIN;
ALTER TABLE esp32_node ADD COLUMN IF NOT EXISTS location text;
COMMENT ON COLUMN esp32_node.location IS 'Ubicación escrita por el usuario desde el panel; no reemplaza IP ni estado.';
ALTER TABLE led_guide_point ADD COLUMN IF NOT EXISTS name text;
COMMENT ON COLUMN led_guide_point.name IS 'Nombre visible de la luz; led_index sigue siendo la identidad física.';
COMMIT;

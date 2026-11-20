-- Ejecutar antes de configurar varios LED del mismo nodo en un estante.
-- No elimina luces ni asociaciones. La unicidad (node_id, led_index)
-- existente sigue protegiendo la identidad de cada canal físico.
BEGIN;
DROP INDEX IF EXISTS public.led_guide_point_shelf_id_node_id_idx;
CREATE INDEX IF NOT EXISTS led_guide_point_shelf_node_lookup_idx
    ON public.led_guide_point (shelf_id, node_id);
COMMIT;

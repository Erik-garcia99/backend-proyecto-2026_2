-- Ejecutar ANTES de desplegar el backend del mapa. No publica ni migra
-- automáticamente posiciones legacy de shelf.map_* al nuevo borrador.
BEGIN;

CREATE TABLE IF NOT EXISTS public.store_map (
    branch_id uuid PRIMARY KEY REFERENCES public.branch(id) ON DELETE CASCADE DEFERRABLE,
    width_cm numeric(8,2),
    height_cm numeric(8,2),
    version integer NOT NULL DEFAULT 1,
    updated_by uuid REFERENCES public."user"(id) ON DELETE SET NULL DEFERRABLE,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Las dimensiones legacy siguen intactas. Una fila nueva de solo borrador
-- no tiene aún dimensiones publicadas y no debe inventarlas en centímetros.
ALTER TABLE public.store_map ALTER COLUMN width_cm DROP NOT NULL;
ALTER TABLE public.store_map ALTER COLUMN height_cm DROP NOT NULL;
ALTER TABLE public.store_map
    ADD COLUMN IF NOT EXISTS draft_document jsonb,
    ADD COLUMN IF NOT EXISTS draft_version integer NOT NULL DEFAULT 0 CHECK (draft_version >= 0),
    ADD COLUMN IF NOT EXISTS draft_updated_by uuid REFERENCES public."user"(id) ON DELETE SET NULL DEFERRABLE,
    ADD COLUMN IF NOT EXISTS draft_updated_at timestamptz;

COMMENT ON COLUMN public.store_map.draft_document IS
    'Borrador privado del croquis en unidades relativas, independiente de dimensiones y posiciones legacy; no publicado.';

COMMIT;

-- M8 / ME-7: repisas independientes de la cámara y asociación configurada
-- producto <-> repisa. Esta tabla es de PLANIFICACIÓN, no de disponibilidad:
-- la ocupación visual sigue viniendo de ROI/InfluxDB (F-21, C-17).
BEGIN;

CREATE TABLE IF NOT EXISTS public.shelf_product (
    shelf_id uuid NOT NULL REFERENCES public.shelf(id) ON DELETE CASCADE,
    product_id uuid NOT NULL REFERENCES public.product(id) ON DELETE CASCADE,
    branch_id uuid NOT NULL REFERENCES public.branch(id) ON DELETE CASCADE,
    created_by uuid REFERENCES public."user"(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shelf_id, product_id)
);

CREATE INDEX IF NOT EXISTS shelf_product_branch_idx
    ON public.shelf_product(branch_id, product_id);

COMMENT ON TABLE public.shelf_product IS
    'M8: producto previsto en una repisa. Configuración, no disponibilidad visual.';

COMMIT;

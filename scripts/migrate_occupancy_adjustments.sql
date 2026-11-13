-- Tabla de auditoría de ajustes manuales del contador de aforo.
CREATE TABLE IF NOT EXISTS public.occupancy_adjustment (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES public.branch(id) ON DELETE CASCADE,
    node_id UUID NOT NULL REFERENCES public.esp32_node(id) ON DELETE SET NULL,
    value INTEGER NOT NULL CHECK (value >= 0),
    reason TEXT NOT NULL,
    created_by UUID REFERENCES public."user"(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS occupancy_adjustment_branch_created_idx
    ON public.occupancy_adjustment (branch_id, created_at DESC);

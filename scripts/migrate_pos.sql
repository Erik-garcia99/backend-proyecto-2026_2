-- Migración idempotente POS (Fases 0-3)
-- Se puede ejecutar varias veces sin error.

-- Código de barras del producto (el esquema actual no lo tenía).
ALTER TABLE public.product ADD COLUMN IF NOT EXISTS barcode text;

CREATE INDEX IF NOT EXISTS idx_product_org_sku ON public.product (organization_id, sku);
CREATE UNIQUE INDEX IF NOT EXISTS uq_product_org_barcode
    ON public.product (organization_id, barcode)
    WHERE barcode IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_product_name ON public.product (name);
CREATE INDEX IF NOT EXISTS idx_branch_inventory_branch ON public.branch_inventory (branch_id);
CREATE INDEX IF NOT EXISTS idx_inventory_movement_product_date
    ON public.inventory_movement (product_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_sale_branch_date ON public.sale (branch_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_sale_item_sale ON public.sale_item (sale_id);

-- Claves de idempotencia para ventas y demás POST con efecto.
CREATE TABLE IF NOT EXISTS public.idempotency_key (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id uuid NOT NULL REFERENCES public.branch(id),
    key text NOT NULL,
    user_id uuid REFERENCES public.user(id),
    body_hash text NOT NULL,
    response jsonb,
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (branch_id, key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_key_created
    ON public.idempotency_key (created_at);
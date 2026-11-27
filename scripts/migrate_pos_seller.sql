-- Permiso de venta por usuario y sucursal, con registro de quién lo otorgó.
-- Idempotente: se puede ejecutar varias veces.
-- Decisión del operador 23/11/2026: sudo y owner venden siempre; RRHH/owner
-- otorgan o revocan el permiso al resto de usuarios.

CREATE TABLE IF NOT EXISTS public.pos_seller (
    branch_id  uuid NOT NULL REFERENCES public.branch(id),
    user_id    uuid NOT NULL REFERENCES public."user"(id),
    granted_by uuid REFERENCES public."user"(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_by uuid REFERENCES public."user"(id),
    revoked_at timestamptz,
    PRIMARY KEY (branch_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_pos_seller_user_active
    ON public.pos_seller (user_id) WHERE revoked_at IS NULL;

COMMENT ON TABLE public.pos_seller IS
    'Vendedores autorizados del POS por sucursal; baja lógica con revoked_at.';

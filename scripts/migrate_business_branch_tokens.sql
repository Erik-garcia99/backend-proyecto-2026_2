-- Ejecutar sobre bases existentes. Compose también lo aplica en bases nuevas.
BEGIN;

CREATE TABLE IF NOT EXISTS business_registration_token (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES business(id),
    token_hash VARCHAR(255) NOT NULL UNIQUE,
    created_by UUID NOT NULL REFERENCES users(id),
    expires_at TIMESTAMPTZ,
    max_uses INTEGER CHECK (max_uses IS NULL OR max_uses > 0),
    uses INTEGER NOT NULL DEFAULT 0 CHECK (uses >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (max_uses IS NULL OR uses <= max_uses)
);

-- Los tokens anteriores BRANCH_CREATION se convierten en tokens de empresa.
-- Mantener hash, creador, límites y vigencia; no se guarda el texto original.
INSERT INTO business_registration_token
    (business_id, token_hash, created_by, expires_at, max_uses, uses, is_active, created_at)
SELECT b.business_id, t.token_hash, t.created_by, t.expires_at,
       t.max_uses, t.uses, t.is_active, t.created_at
FROM branch_registration_token t JOIN branch b ON b.id = t.branch_id
WHERE t.token_type = 'BRANCH_CREATION'
ON CONFLICT (token_hash) DO NOTHING;

-- Conservar registros anteriores como históricos, sin aceptarlos como tokens de sucursal.
UPDATE branch_registration_token t SET is_active = FALSE
WHERE t.token_type = 'BRANCH_CREATION'
  AND EXISTS (SELECT 1 FROM business_registration_token bt WHERE bt.token_hash = t.token_hash);

CREATE INDEX IF NOT EXISTS idx_business_registration_token_business
    ON business_registration_token (business_id);
COMMIT;

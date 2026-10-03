-- Ejecutar una vez si la base ya existía antes de la funcionalidad de sucursales.
BEGIN;

ALTER TABLE branch ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'America/Mexico_City';
ALTER TABLE branch ADD COLUMN IF NOT EXISTS business_category VARCHAR(100) NOT NULL DEFAULT 'General';

INSERT INTO branch_registration_token (branch_id, token_hash, token_type, created_by, max_uses)
SELECT b.id, encode(digest('token_sucursal_123', 'sha256'), 'hex'), 'BRANCH_CREATION', s.id, NULL
FROM branch b, users s
WHERE b.code = 'PRUEBA' AND s.email = 'sudo@selfiq.local'
ON CONFLICT (token_hash) DO NOTHING;

COMMIT;

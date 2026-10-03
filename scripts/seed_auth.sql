-- Datos iniciales de desarrollo. Ejecutar después de selfiq_sprint1_postgresql.sql.
-- Credenciales: sudo@selfiq.local / Sudo123!
--              admin@selfiq.local / Admin123!
BEGIN;

ALTER TABLE branch ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'America/Mexico_City';
ALTER TABLE branch ADD COLUMN IF NOT EXISTS business_category VARCHAR(100) NOT NULL DEFAULT 'General';

INSERT INTO users (email, password_hash, first_name, last_name, is_active, is_superuser, account_status)
VALUES ('sudo@selfiq.local', crypt('Sudo123!', gen_salt('bf')), 'Sudo', 'Administrator', TRUE, TRUE, 'ACTIVE')
ON CONFLICT (email) DO UPDATE SET is_superuser = TRUE, account_status = 'ACTIVE', is_active = TRUE;

INSERT INTO business (name)
SELECT 'SelfIQ Demo'
WHERE NOT EXISTS (SELECT 1 FROM business WHERE name = 'SelfIQ Demo');

INSERT INTO branch (business_id, name, code, address, is_main)
SELECT id, 'Tienda prueba', 'PRUEBA', 'Dirección de prueba', TRUE
FROM business WHERE name = 'SelfIQ Demo'
ON CONFLICT (business_id, code) DO NOTHING;

INSERT INTO users (email, password_hash, first_name, last_name, is_active, account_status)
VALUES ('admin@selfiq.local', crypt('Admin123!', gen_salt('bf')), 'Admin', 'Tienda', TRUE, 'ACTIVE')
ON CONFLICT (email) DO UPDATE SET account_status = 'ACTIVE', is_active = TRUE;

INSERT INTO user_branch (user_id, branch_id, role_id, membership_status, approved_by, approved_at)
SELECT u.id, b.id, r.id, 'ACTIVE', s.id, NOW()
FROM users u, branch b, role r, users s
WHERE u.email = 'admin@selfiq.local' AND b.code = 'PRUEBA' AND r.name = 'MANAGER'
  AND s.email = 'sudo@selfiq.local'
ON CONFLICT (user_id, branch_id) DO UPDATE SET role_id = EXCLUDED.role_id, membership_status = 'ACTIVE';

INSERT INTO branch_registration_token (branch_id, token_hash, token_type, created_by, max_uses)
SELECT b.id, encode(digest('token_prueba_123', 'sha256'), 'hex'), 'USER_REGISTRATION', s.id, NULL
FROM branch b, users s
WHERE b.code = 'PRUEBA' AND s.email = 'sudo@selfiq.local'
ON CONFLICT (token_hash) DO NOTHING;

-- Token para crear sucursales durante el desarrollo.
INSERT INTO branch_registration_token (branch_id, token_hash, token_type, created_by, max_uses)
SELECT b.id, encode(digest('token_sucursal_123', 'sha256'), 'hex'), 'BRANCH_CREATION', s.id, NULL
FROM branch b, users s
WHERE b.code = 'PRUEBA' AND s.email = 'sudo@selfiq.local'
ON CONFLICT (token_hash) DO NOTHING;

COMMIT;

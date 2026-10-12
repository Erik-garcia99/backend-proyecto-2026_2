-- Bases existentes y nuevas; conserva sucursales, usuarios y tokens.
BEGIN;
ALTER TABLE gateway ADD COLUMN IF NOT EXISTS device_public_key text;
COMMENT ON COLUMN gateway.device_public_key IS 'Identidad Ed25519 del equipo; solo clave pública. Permite corregir token revocado sin reconfigurar Wi-Fi.';
COMMIT;

-- Guía LED: sesiones persistentes, exclusión por canal y capacidades declaradas.
-- Aditiva e idempotente. Conserva solicitudes y asociaciones existentes.
-- Ejecutar en Supabase SQL Editor antes de desplegar el backend de guía.
BEGIN;

-- Capacidades y último estado informados por el nodo guía.
ALTER TABLE esp32_node ADD COLUMN IF NOT EXISTS guide_driver text;
ALTER TABLE esp32_node ADD COLUMN IF NOT EXISTS guide_channels integer;
ALTER TABLE esp32_node ADD COLUMN IF NOT EXISTS guide_channels_map jsonb;
ALTER TABLE esp32_node ADD COLUMN IF NOT EXISTS guide_boot text;
ALTER TABLE esp32_node ADD COLUMN IF NOT EXISTS guide_capabilities_at timestamptz;
ALTER TABLE esp32_node ADD COLUMN IF NOT EXISTS guide_state jsonb;
ALTER TABLE esp32_node ADD COLUMN IF NOT EXISTS guide_state_at timestamptz;
COMMENT ON COLUMN esp32_node.guide_channels IS 'Canales lógicos declarados por guide.capabilities; sin valor no se habilitan guías nuevas.';

-- Extensión de led_guide_request (la tabla ya existía con requested_at/ended_at).
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS session_id uuid;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS node_id uuid;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS led_index smallint;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS gateway_rpi text;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS ttl_ms integer;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS reservation_until timestamptz;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS issued_at timestamptz;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS command_id text;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS delivery text;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS light text;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS closing boolean NOT NULL DEFAULT false;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS intent text;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS reason text;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS requester_user_id uuid;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS shelf_name text;
ALTER TABLE led_guide_request ADD COLUMN IF NOT EXISTS location_reference text;

-- Solicitudes históricas: se cierran para que no reserven canales.
UPDATE led_guide_request
   SET status = 'failed', reason = COALESCE(reason, 'migrated_legacy'),
       delivery = COALESCE(delivery, 'unknown'),
       light = COALESCE(light, 'unknown'),
       issued_at = COALESCE(issued_at, requested_at),
       updated_at = NOW()
 WHERE status = 'requested' AND reason IS DISTINCT FROM 'migrated_legacy';

-- Instantánea del destino para whole historial y para la reserva.
UPDATE led_guide_request r
   SET node_id = p.node_id, led_index = p.led_index
  FROM led_guide_point p
 WHERE r.guide_point_id = p.id AND r.node_id IS NULL;

UPDATE led_guide_request
   SET expires_at = COALESCE(expires_at, requested_at + interval '60 seconds'),
       reservation_until = COALESCE(reservation_until, requested_at + interval '65 seconds'),
       light = COALESCE(light, 'unknown'),
       delivery = COALESCE(delivery, 'unknown'),
       updated_at = COALESCE(updated_at, requested_at)
 WHERE expires_at IS NULL OR reservation_until IS NULL OR light IS NULL OR delivery IS NULL;

-- Una sola reserva abierta por (nodo, canal), también para la prueba staff.
CREATE UNIQUE INDEX IF NOT EXISTS led_guide_request_active_channel
    ON led_guide_request (node_id, led_index)
 WHERE status IN ('pending', 'active') OR closing;

CREATE INDEX IF NOT EXISTS led_guide_request_owner
    ON led_guide_request (customer_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS led_guide_request_expiry
    ON led_guide_request (expires_at) WHERE status IN ('pending', 'active');

COMMIT;

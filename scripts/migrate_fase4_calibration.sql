-- SelfIQ — Fase 4 (calibración de zonas con ESP32-CAM)
-- Esquema de sesiones, referencias y operaciones durables.
--
-- Idempotente: se puede ejecutar varias veces sin borrar datos.
-- No elimina imágenes ni referencias históricas.
--
-- Uso:
--   psql "$DATABASE_URL" -f backend-proyecto-2026_2/scripts/migrate_fase4_calibration.sql
--
-- Orden recomendado: antes de desplegar el backend que usa app/calibrations.py.
-- No escribe binarios en disco: las imágenes viven en public.snapshot.
--
-- Nota de retención: una pareja activa nunca se borra. Las imágenes de
-- calibración se guardan con purpose='calibration' y no se purgan en esta fase.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. ROI: versión de calibración, pareja activa y causa de invalidación.
-- ---------------------------------------------------------------------------

ALTER TABLE public.roi
    ADD COLUMN IF NOT EXISTS calibration_version integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS active_calibration_id uuid,
    ADD COLUMN IF NOT EXISTS calibration_context_hash text,
    ADD COLUMN IF NOT EXISTS calibration_invalidated_reason text;

COMMENT ON COLUMN public.roi.calibration_version IS
    'F-10/F-44: versión monotónica de la pareja de referencias. Se reserva al crear la sesión y no se reutiliza.';
COMMENT ON COLUMN public.roi.active_calibration_id IS
    'F-10: sesión de calibración cuya pareja está activa. NULL si nunca se confirmó una pareja.';
COMMENT ON COLUMN public.roi.calibration_context_hash IS
    'F-10: huella canónica del contexto (SKU, geometría, nodo y versión de zonas) de la pareja activa.';
COMMENT ON COLUMN public.roi.calibration_invalidated_reason IS
    'F-10: motivo por el que la pareja activa quedó needs_recalibration (roi_config_changed, sku_changed, camera_changed).';

-- Una calibración antigua sin pareja registrada no puede considerarse vigente:
-- no hay contexto ni versión que permitan demostrar qué se calibró.
UPDATE public.roi
   SET calibration_status='needs_recalibration',
       calibration_invalidated_reason='migration_fase4',
       updated_at=NOW()
 WHERE calibration_status='calibrated' AND active_calibration_id IS NULL;

-- ---------------------------------------------------------------------------
-- 2. Nodo de cámara: contexto visual que entra en la huella canónica.
-- ---------------------------------------------------------------------------

ALTER TABLE public.esp32_node
    ADD COLUMN IF NOT EXISTS camera_frame_width integer NOT NULL DEFAULT 640,
    ADD COLUMN IF NOT EXISTS camera_frame_height integer NOT NULL DEFAULT 480,
    ADD COLUMN IF NOT EXISTS camera_orientation integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS camera_visual_revision integer NOT NULL DEFAULT 1;

COMMENT ON COLUMN public.esp32_node.camera_frame_width IS
    'F-10: ancho de frame usado por el descriptor de calibración. Cambiarlo exige subir camera_visual_revision.';
COMMENT ON COLUMN public.esp32_node.camera_frame_height IS
    'F-10: alto de frame usado por el descriptor de calibración. Cambiarlo exige subir camera_visual_revision.';
COMMENT ON COLUMN public.esp32_node.camera_orientation IS
    'F-10: rotación aplicada a la imagen (0, 90, 180, 270). Entra en la huella del contexto.';
COMMENT ON COLUMN public.esp32_node.camera_visual_revision IS
    'F-10: revisión de parámetros visuales que no son geometría. Sube al cambiar resolución, orientación o ajustes de imagen.';

-- ---------------------------------------------------------------------------
-- 3. Sesión de calibración: una por cámara abierta a la vez.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.roi_calibration_session (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    roi_id uuid NOT NULL,
    shelf_id uuid NOT NULL,
    camera_node_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    product_id uuid NOT NULL,
    product_sku text NOT NULL,
    product_name text NOT NULL,
    context jsonb NOT NULL,
    context_hash text NOT NULL,
    config_version integer NOT NULL,
    calibration_version integer NOT NULL,
    descriptor_format integer NOT NULL DEFAULT 1,
    state text NOT NULL DEFAULT 'draft',
    session_version integer NOT NULL DEFAULT 1,
    created_by uuid,
    reason text,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    confirmed_at timestamp with time zone,
    closed_at timestamp with time zone,
    error_code text,
    CONSTRAINT roi_calibration_session_state_check CHECK (
        state IN ('draft', 'empty_ready', 'pair_ready', 'committing',
                  'active', 'cancel_pending', 'cancelled', 'expired', 'failed')
    ),
    CONSTRAINT roi_calibration_session_versions_check CHECK (
        config_version >= 0 AND calibration_version >= 1 AND session_version >= 1
    )
);

COMMENT ON TABLE public.roi_calibration_session IS
    'F-10/F-44: sesión de calibración con contexto congelado (SKU, geometría, nodo y config_version). Una abierta por cámara.';
COMMENT ON COLUMN public.roi_calibration_session.context IS
    'F-10: objeto canónico congelado al crear la sesión. La huella se calcula sobre este JSON, no sobre datos mutables.';
COMMENT ON COLUMN public.roi_calibration_session.session_version IS
    'F-10: concurrencia optimista. Toda mutación aceptada la incrementa.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_calibration_session_roi_id_fkey') THEN
        ALTER TABLE public.roi_calibration_session
            ADD CONSTRAINT roi_calibration_session_roi_id_fkey
            FOREIGN KEY (roi_id) REFERENCES public.roi(id) DEFERRABLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_calibration_session_shelf_id_fkey') THEN
        ALTER TABLE public.roi_calibration_session
            ADD CONSTRAINT roi_calibration_session_shelf_id_fkey
            FOREIGN KEY (shelf_id) REFERENCES public.shelf(id) DEFERRABLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_calibration_session_camera_node_id_fkey') THEN
        ALTER TABLE public.roi_calibration_session
            ADD CONSTRAINT roi_calibration_session_camera_node_id_fkey
            FOREIGN KEY (camera_node_id) REFERENCES public.esp32_node(id) DEFERRABLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_calibration_session_branch_id_fkey') THEN
        ALTER TABLE public.roi_calibration_session
            ADD CONSTRAINT roi_calibration_session_branch_id_fkey
            FOREIGN KEY (branch_id) REFERENCES public.branch(id) DEFERRABLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_calibration_session_created_by_fkey') THEN
        ALTER TABLE public.roi_calibration_session
            ADD CONSTRAINT roi_calibration_session_created_by_fkey
            FOREIGN KEY (created_by) REFERENCES public."user"(id) ON DELETE SET NULL DEFERRABLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_active_calibration_id_fkey') THEN
        ALTER TABLE public.roi
            ADD CONSTRAINT roi_active_calibration_id_fkey
            FOREIGN KEY (active_calibration_id) REFERENCES public.roi_calibration_session(id) DEFERRABLE;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_roi_calibration_session_version
    ON public.roi_calibration_session (roi_id, calibration_version);

-- Una sola sesión abierta por cámara. Las terminales salen del índice parcial.
CREATE UNIQUE INDEX IF NOT EXISTS ux_roi_calibration_session_open_camera
    ON public.roi_calibration_session (camera_node_id)
    WHERE state IN ('draft', 'empty_ready', 'pair_ready', 'committing', 'cancel_pending');

CREATE INDEX IF NOT EXISTS ix_roi_calibration_session_roi
    ON public.roi_calibration_session (roi_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_roi_calibration_session_camera
    ON public.roi_calibration_session (camera_node_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Referencias: dos por sesión, históricas y nunca borradas.
-- ---------------------------------------------------------------------------

ALTER TABLE public.roi_calibration
    ADD COLUMN IF NOT EXISTS session_id uuid,
    ADD COLUMN IF NOT EXISTS capture_id uuid,
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'ready',
    ADD COLUMN IF NOT EXISTS descriptor_format integer,
    ADD COLUMN IF NOT EXISTS descriptor_crc text,
    ADD COLUMN IF NOT EXISTS pair_hash text,
    ADD COLUMN IF NOT EXISTS context_hash text,
    ADD COLUMN IF NOT EXISTS image_id uuid,
    ADD COLUMN IF NOT EXISTS image_sha256 text,
    ADD COLUMN IF NOT EXISTS image_bytes integer,
    ADD COLUMN IF NOT EXISTS width integer,
    ADD COLUMN IF NOT EXISTS height integer,
    ADD COLUMN IF NOT EXISTS quality jsonb,
    ADD COLUMN IF NOT EXISTS clock_quality text,
    ADD COLUMN IF NOT EXISTS uptime_ms bigint,
    ADD COLUMN IF NOT EXISTS received_at timestamp with time zone DEFAULT now();

-- Las filas nuevas son borradores: is_current solo se activa al confirmar la pareja.
ALTER TABLE public.roi_calibration ALTER COLUMN is_current SET DEFAULT false;
ALTER TABLE public.roi_calibration ALTER COLUMN captured_at DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_calibration_session_id_fkey') THEN
        ALTER TABLE public.roi_calibration
            ADD CONSTRAINT roi_calibration_session_id_fkey
            FOREIGN KEY (session_id) REFERENCES public.roi_calibration_session(id) DEFERRABLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_calibration_status_check') THEN
        ALTER TABLE public.roi_calibration
            ADD CONSTRAINT roi_calibration_status_check CHECK (
                status IN ('pending', 'stored', 'image_pending', 'ready', 'failed', 'unknown')
            );
    END IF;
    IF to_regclass('public.snapshot') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'roi_calibration_image_id_fkey') THEN
        ALTER TABLE public.roi_calibration
            ADD CONSTRAINT roi_calibration_image_id_fkey
            FOREIGN KEY (image_id) REFERENCES public.snapshot(id) DEFERRABLE;
    END IF;
END $$;

-- Normaliza datos antiguos antes de exigir una sola referencia actual por tipo.
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY roi_id, reference_type
               ORDER BY captured_at DESC NULLS LAST, id DESC
           ) AS position
      FROM public.roi_calibration
     WHERE is_current
)
UPDATE public.roi_calibration AS calibration
   SET is_current=false,
       superseded_at=COALESCE(calibration.superseded_at, NOW())
  FROM ranked
 WHERE calibration.id=ranked.id AND ranked.position > 1;

CREATE UNIQUE INDEX IF NOT EXISTS ux_roi_calibration_session_capture
    ON public.roi_calibration (session_id, capture_id)
    WHERE session_id IS NOT NULL AND capture_id IS NOT NULL;

-- Como máximo una referencia actual por ROI y tipo.
CREATE UNIQUE INDEX IF NOT EXISTS ux_roi_calibration_current
    ON public.roi_calibration (roi_id, reference_type)
    WHERE is_current;

CREATE INDEX IF NOT EXISTS ix_roi_calibration_session
    ON public.roi_calibration (session_id, reference_type, status);

-- ---------------------------------------------------------------------------
-- 5. Operaciones durables e idempotencia.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.calibration_operation (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid,
    roi_id uuid NOT NULL,
    action text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb,
    code text,
    detail text,
    created_by uuid,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT calibration_operation_action_check CHECK (
        action IN ('create', 'capture', 'commit', 'cancel', 'status', 'image')
    ),
    CONSTRAINT calibration_operation_status_check CHECK (
        status IN ('pending', 'sent', 'applied', 'rejected', 'failed', 'unknown')
    )
);

COMMENT ON TABLE public.calibration_operation IS
    'F-10/F-44: operación durable de calibración. Sobrevive reinicios y hace idempotentes los POST.';
COMMENT ON COLUMN public.calibration_operation.idempotency_key IS
    'F-10: Idempotency-Key del cliente. Misma clave + mismo request_hash devuelve el mismo resultado.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'calibration_operation_session_id_fkey') THEN
        ALTER TABLE public.calibration_operation
            ADD CONSTRAINT calibration_operation_session_id_fkey
            FOREIGN KEY (session_id) REFERENCES public.roi_calibration_session(id) DEFERRABLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'calibration_operation_roi_id_fkey') THEN
        ALTER TABLE public.calibration_operation
            ADD CONSTRAINT calibration_operation_roi_id_fkey
            FOREIGN KEY (roi_id) REFERENCES public.roi(id) DEFERRABLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'calibration_operation_created_by_fkey') THEN
        ALTER TABLE public.calibration_operation
            ADD CONSTRAINT calibration_operation_created_by_fkey
            FOREIGN KEY (created_by) REFERENCES public."user"(id) ON DELETE SET NULL DEFERRABLE;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_calibration_operation_idempotency
    ON public.calibration_operation (idempotency_key);

CREATE INDEX IF NOT EXISTS ix_calibration_operation_session
    ON public.calibration_operation (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_calibration_operation_pending
    ON public.calibration_operation (status, updated_at)
    WHERE status IN ('pending', 'sent');

-- ---------------------------------------------------------------------------
-- 6. Snapshot: finalidad y correlación con la calibración.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('public.snapshot') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.snapshot ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT ''snapshot''';
        EXECUTE 'ALTER TABLE public.snapshot ADD COLUMN IF NOT EXISTS calibration_session_id uuid';
        EXECUTE 'ALTER TABLE public.snapshot ADD COLUMN IF NOT EXISTS calibration_capture_id uuid';
        EXECUTE 'ALTER TABLE public.snapshot ADD COLUMN IF NOT EXISTS image_sha256 text';
        EXECUTE 'CREATE INDEX IF NOT EXISTS ix_snapshot_branch_purpose ON public.snapshot (branch_id, purpose, created_at DESC)';
        EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshot_calibration_capture ON public.snapshot (calibration_session_id, calibration_capture_id) WHERE calibration_capture_id IS NOT NULL';
        EXECUTE 'COMMENT ON COLUMN public.snapshot.purpose IS ''F-10/F-12: snapshot (alertas) o calibration (referencia). Las referencias activas no se purgan.''';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 7. Invalidación conservadora desde la propia base de datos.
--    Cualquier cambio de versión de zonas o de cámara deja las calibraciones
--    de esa repisa en needs_recalibration, sin borrar fechas ni historia.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION selfiq_invalidate_shelf_calibrations()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.camera_node_id IS DISTINCT FROM OLD.camera_node_id THEN
        UPDATE public.roi
           SET calibration_status='needs_recalibration',
               calibration_invalidated_reason='camera_changed',
               updated_at=NOW()
         WHERE shelf_id=NEW.id AND calibration_status='calibrated';
    ELSIF NEW.roi_version IS DISTINCT FROM OLD.roi_version
       OR NEW.roi_applied_version IS DISTINCT FROM OLD.roi_applied_version THEN
        UPDATE public.roi
           SET calibration_status='needs_recalibration',
               calibration_invalidated_reason='roi_config_changed',
               updated_at=NOW()
         WHERE shelf_id=NEW.id AND calibration_status='calibrated';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_invalidate_shelf_calibrations ON public.shelf;
CREATE TRIGGER trg_invalidate_shelf_calibrations
    AFTER UPDATE OF roi_version, roi_applied_version, camera_node_id
    ON public.shelf
    FOR EACH ROW
    EXECUTE FUNCTION selfiq_invalidate_shelf_calibrations();

CREATE OR REPLACE FUNCTION selfiq_invalidate_product_calibrations()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.sku IS DISTINCT FROM OLD.sku OR NEW.name IS DISTINCT FROM OLD.name THEN
        UPDATE public.roi
           SET calibration_status='needs_recalibration',
               calibration_invalidated_reason='sku_changed',
               updated_at=NOW()
         WHERE product_id=NEW.id AND calibration_status='calibrated';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_invalidate_product_calibrations ON public.product;
CREATE TRIGGER trg_invalidate_product_calibrations
    AFTER UPDATE OF sku, name
    ON public.product
    FOR EACH ROW
    EXECUTE FUNCTION selfiq_invalidate_product_calibrations();

COMMIT;

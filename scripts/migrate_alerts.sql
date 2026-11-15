-- Antes del despliegue. Idempotente, sin borrar ni reescribir alertas existentes.
BEGIN;
CREATE INDEX IF NOT EXISTS ix_alert_branch_history
    ON alert(branch_id, triggered_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_alert_branch_active
    ON alert(branch_id, dedupe_key) WHERE status IN ('open','acknowledged');
COMMENT ON TABLE alert IS
    'Alertas Must F-07/F-18/F-28/F-29/F-43/S-10. La existencia de la tabla no demuestra integración de productores.';
COMMIT;

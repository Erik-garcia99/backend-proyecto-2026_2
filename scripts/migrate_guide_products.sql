-- Guía LED: asociación propia producto → luz, independiente de la cámara/ROI.
-- Aditiva e idempotente. La cámara conserva sus ROI sin cambios.
BEGIN;

CREATE TABLE IF NOT EXISTS guide_product_light (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  branch_id uuid NOT NULL,
  product_id uuid NOT NULL,
  guide_point_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid,
  CONSTRAINT guide_product_light_pkey PRIMARY KEY (id),
  CONSTRAINT guide_product_light_branch_fkey FOREIGN KEY (branch_id) REFERENCES branch(id),
  CONSTRAINT guide_product_light_product_fkey FOREIGN KEY (product_id) REFERENCES product(id),
  CONSTRAINT guide_product_light_point_fkey FOREIGN KEY (guide_point_id) REFERENCES led_guide_point(id),
  CONSTRAINT guide_product_light_created_by_fkey FOREIGN KEY (created_by) REFERENCES "user"(id),
  CONSTRAINT guide_product_light_unique UNIQUE (branch_id, product_id)
);

COMMENT ON TABLE guide_product_light IS
  'Asociación producto → punto LED de la guía. No sustituye ni consulta roi (cámara).';
COMMENT ON COLUMN guide_product_light.guide_point_id IS
  'Punto LED (led_guide_point). El estante se obtiene desde el punto, no desde un ROI.';

CREATE INDEX IF NOT EXISTS guide_product_light_point_idx
  ON guide_product_light (guide_point_id);
CREATE INDEX IF NOT EXISTS guide_product_light_product_idx
  ON guide_product_light (product_id);

COMMIT;

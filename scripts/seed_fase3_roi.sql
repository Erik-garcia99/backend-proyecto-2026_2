-- SelfIQ — Fase 3 (ROI configurable)
-- Datos mínimos de prueba: una repisa vinculada a la cámara y dos productos.
--
-- Requisitos:
--   1. La migración migrate_fase3_roi.sql ya aplicada.
--   2. Una cámara shelf_cam registrada en la sucursal.
--
-- Cómo ejecutarlo:
--   Supabase → SQL Editor → pegar este archivo → editar las tres constantes
--   marcadas con "EDITA AQUÍ" → Run.
--   También funciona con: psql "$DATABASE_URL" -f <este archivo>
--
-- Para localizar los UUID antes de editar, ejecuta también:
--   backend-proyecto-2026_2/scripts/find_fase3_camera.sql
-- o directamente:
--   SELECT n.id AS camera_node_id, g.branch_id, b.name AS branch_name,
--          n.name AS camera_name, n.is_online, n.last_heartbeat_at
--   FROM public.esp32_node n
--   JOIN public.gateway g ON g.id = n.gateway_id
--   JOIN public.branch b ON b.id = g.branch_id
--   WHERE n.node_type = 'shelf_cam'
--   ORDER BY n.is_online DESC, n.last_heartbeat_at DESC NULLS LAST;
--
-- El script es idempotente: reutiliza la repisa vinculada a la cámara y
-- actualiza los productos si ya existen por (organization_id, sku).

DO $$
DECLARE
    -- ============================================================
    -- EDITA AQUÍ: UUID reales del piloto (Tienda de prueba).
    -- ============================================================
    p_branch      uuid := 'd07018c7-99c1-490f-95b9-dd54589cf60b';
    p_camera      uuid := '5c485efa-cc20-4f9a-91eb-ceeba0f99289';

    -- Opcionales: puedes cambiar nombres y SKU.
    p_shelf_name  text := 'Repisa de prueba Fase 3';
    p_location    text := 'Piso 1';
    p_sku_a       text := 'SKU-FASE3-A';
    p_name_a      text := 'Producto de prueba A';
    p_sku_b       text := 'SKU-FASE3-B';
    p_name_b      text := 'Producto de prueba B';

    v_org         uuid;
    v_shelf       uuid;
    v_product_a   uuid;
    v_product_b   uuid;
BEGIN
    IF p_branch = '00000000-0000-0000-0000-000000000000'
       OR p_camera = '00000000-0000-0000-0000-000000000000' THEN
        RAISE EXCEPTION
            'Edita p_branch y p_camera con los UUID reales antes de ejecutar el seed.';
    END IF;

    -- La cámara debe existir, ser shelf_cam y pertenecer a la sucursal.
    SELECT b.organization_id
      INTO v_org
      FROM public.branch b
      JOIN public.gateway g ON g.branch_id = b.id
      JOIN public.esp32_node n ON n.gateway_id = g.id
     WHERE b.id = p_branch
       AND n.id = p_camera
       AND n.node_type = 'shelf_cam'
       AND b.is_active
       AND g.is_active;

    IF v_org IS NULL THEN
        RAISE EXCEPTION
            'La cámara % no pertenece a la sucursal % o no es de tipo shelf_cam',
            p_camera, p_branch;
    END IF;

    IF p_sku_a = p_sku_b THEN
        RAISE EXCEPTION 'Los SKU de prueba deben ser distintos (%).', p_sku_a;
    END IF;

    -- Una cámara = una repisa (shelf.camera_node_id es UNIQUE).
    SELECT s.id
      INTO v_shelf
      FROM public.shelf s
     WHERE s.camera_node_id = p_camera;

    IF v_shelf IS NULL THEN
        INSERT INTO public.shelf (
            branch_id, camera_node_id, camera_node_type, name, location_label
        )
        VALUES (
            p_branch, p_camera, 'shelf_cam', p_shelf_name, p_location
        )
        RETURNING id INTO v_shelf;
    ELSE
        UPDATE public.shelf
           SET name = p_shelf_name,
               location_label = p_location,
               camera_node_type = 'shelf_cam',
               is_active = true,
               updated_at = NOW()
         WHERE id = v_shelf
           AND branch_id = p_branch;

        IF NOT FOUND THEN
            RAISE EXCEPTION
                'La cámara % ya está vinculada a una repisa de otra sucursal',
                p_camera;
        END IF;
    END IF;

    -- Productos mínimos para asociar a los ROI. No crea categorías ni precios.
    INSERT INTO public.product (organization_id, sku, name)
    VALUES
        (v_org, p_sku_a, p_name_a),
        (v_org, p_sku_b, p_name_b)
    ON CONFLICT (organization_id, sku)
    DO UPDATE SET
        name = EXCLUDED.name,
        is_active = true,
        updated_at = NOW();

    SELECT p.id INTO v_product_a
      FROM public.product p
     WHERE p.organization_id = v_org AND p.sku = p_sku_a;

    SELECT p.id INTO v_product_b
      FROM public.product p
     WHERE p.organization_id = v_org AND p.sku = p_sku_b;

    RAISE NOTICE
        'Seed Fase 3 listo: shelf=% org=% cámara=% productoA=% productoB=%',
        v_shelf, v_org, p_camera, v_product_a, v_product_b;
END;
$$;

-- Verificación rápida (ajusta el UUID de la cámara):
--   SELECT s.id, s.name, s.roi_version, s.roi_applied_version,
--          s.camera_node_id, s.location_label
--   FROM public.shelf s
--   WHERE s.camera_node_id = '<uuid-camara>';
--
-- Limpieza manual cuando ya no se necesiten los datos de prueba:
--   DELETE FROM public.product
--   WHERE organization_id = (
--       SELECT organization_id FROM public.branch WHERE id = '<uuid-sucursal>'
--   ) AND sku IN ('SKU-FASE3-A', 'SKU-FASE3-B');
--   DELETE FROM public.shelf WHERE camera_node_id = '<uuid-camara>';

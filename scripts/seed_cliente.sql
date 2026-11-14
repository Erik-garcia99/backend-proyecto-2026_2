-- SelfIQ — Seed del entorno de pruebas de la app del cliente (CL0)
--
-- Crea una organización y sucursal de QA aisladas, con usuarios, catálogo,
-- repisas, ROIs, mapa, un nodo de guía LED simulado y ventas de ejemplo.
-- Es idempotente: se puede ejecutar varias veces y deja el mismo estado.
--
-- Funciona en psql y en el SQL Editor de Supabase (no usa comandos "\").
--
-- Requisitos:
--   1. Extensión pgcrypto disponible (Supabase la incluye; si no, ver nota abajo).
--   2. Editar la contraseña de prueba en la línea marcada "EDITA AQUÍ".
--      No uses una contraseña real ni reutilizada: el SQL Editor guarda historial.
--
-- Uso en Supabase:
--   SQL Editor → New query → pegar todo este archivo → Run.
--
-- Uso local con psql:
--
--        psql "$DATABASE_URL" -f backend-proyecto-2026_2/scripts/seed_cliente.sql
--
--   Todos los usuarios de QA usan la misma contraseña de prueba.
--
-- Nota si `CREATE EXTENSION pgcrypto` falla:
--   Genera los hashes bcrypt con `backend-proyecto-2026_2/.venv/bin/python` y
--   reemplaza las llamadas `crypt(v_pw, gen_salt('bf', 12))` por los hashes.
--   No guardes contraseñas en el repo (SEC-16).
--
-- IDs fijos del entorno QA:
--   organización  c1e00000-0000-4000-8000-000000000001
--   sucursal      c1e00000-0000-4000-8000-000000000002
--   gateway       c1e00000-0000-4000-8000-000000000003
--   nodo guía     c1e00000-0000-4000-8000-000000000004

CREATE EXTENSION IF NOT EXISTS pgcrypto;
SET search_path = public, extensions;

BEGIN;

DO $$
DECLARE
    -- Organización / sucursal de QA
    v_org              uuid := 'c1e00000-0000-4000-8000-000000000001';
    v_branch           uuid := 'c1e00000-0000-4000-8000-000000000002';
    v_gateway          uuid := 'c1e00000-0000-4000-8000-000000000003';
    v_guide_node       uuid := 'c1e00000-0000-4000-8000-000000000004';

    -- Usuarios
    v_cli_a            uuid := 'c1e00000-0000-4000-8000-000000000301';
    v_cli_b            uuid := 'c1e00000-0000-4000-8000-000000000302';
    v_cli_baja         uuid := 'c1e00000-0000-4000-8000-000000000303';
    v_caja             uuid := 'c1e00000-0000-4000-8000-000000000304';
    v_enc              uuid := 'c1e00000-0000-4000-8000-000000000305';

    -- Categorías
    v_cat_abarrotes    uuid := 'c1e00000-0000-4000-8000-000000000020';
    v_cat_bebidas      uuid := 'c1e00000-0000-4000-8000-000000000021';

    -- Productos
    v_p_ok             uuid := 'c1e00000-0000-4000-8000-000000000101';
    v_p_bajo           uuid := 'c1e00000-0000-4000-8000-000000000102';
    v_p_agot           uuid := 'c1e00000-0000-4000-8000-000000000103';
    v_p_sinroi         uuid := 'c1e00000-0000-4000-8000-000000000104';
    v_p_inact          uuid := 'c1e00000-0000-4000-8000-000000000105';
    v_p_led            uuid := 'c1e00000-0000-4000-8000-000000000106';
    v_p_sinled         uuid := 'c1e00000-0000-4000-8000-000000000107';

    -- Repisas
    v_shelf_ok         uuid := 'c1e00000-0000-4000-8000-000000000010';
    v_shelf_bajo       uuid := 'c1e00000-0000-4000-8000-000000000011';
    v_shelf_agot       uuid := 'c1e00000-0000-4000-8000-000000000012';
    v_shelf_led        uuid := 'c1e00000-0000-4000-8000-000000000013';
    v_shelf_sinled     uuid := 'c1e00000-0000-4000-8000-000000000014';

    -- ROIs
    v_roi_ok           uuid := 'c1e00000-0000-4000-8000-000000000201';
    v_roi_bajo         uuid := 'c1e00000-0000-4000-8000-000000000202';
    v_roi_agot         uuid := 'c1e00000-0000-4000-8000-000000000203';
    v_roi_led          uuid := 'c1e00000-0000-4000-8000-000000000204';
    v_roi_sinled       uuid := 'c1e00000-0000-4000-8000-000000000205';

    -- Guía LED
    v_point            uuid := 'c1e00000-0000-4000-8000-000000000501';

    -- Ventas
    v_sale_ok          uuid := 'c1e00000-0000-4000-8000-000000000401';
    v_sale_void        uuid := 'c1e00000-0000-4000-8000-000000000402';
    v_item_ok_1        uuid := 'c1e00000-0000-4000-8000-000000000411';
    v_item_ok_2        uuid := 'c1e00000-0000-4000-8000-000000000412';
    v_item_void        uuid := 'c1e00000-0000-4000-8000-000000000413';

    v_pw               text := 'CambiaEstaPassword123';  -- EDITA AQUÍ: contraseña de prueba (mín. 8)
BEGIN
    IF v_pw = 'cc0P;x?QLfNw' THEN
        RAISE EXCEPTION 'Edita la contraseña de prueba en el seed antes de ejecutarlo.';
    END IF;
    IF v_pw IS NULL OR length(v_pw) < 8 THEN
        RAISE EXCEPTION 'La contraseña de prueba debe tener al menos 8 caracteres.';
    END IF;

    -- Organización y sucursal -------------------------------------------------
    INSERT INTO public.organization (id, name, slug, plan_type, is_active)
    VALUES (v_org, 'SelfIQ Cliente QA', 'selfiq-cliente-qa', 'pilot', TRUE)
    ON CONFLICT (id) DO UPDATE
       SET name = EXCLUDED.name,
           slug = EXCLUDED.slug,
           plan_type = EXCLUDED.plan_type,
           is_active = TRUE;

    INSERT INTO public.branch (id, organization_id, name, branch_code, address,
                               timezone, business_category, max_capacity_people,
                               occupancy_near_pct, is_active)
    VALUES (v_branch, v_org, 'Sucursal Cliente QA', 'SUC-CLIQA',
            'Av. Prueba 123, Tijuana', 'America/Tijuana', 'General', 10, 70, TRUE)
    ON CONFLICT (id) DO UPDATE
       SET organization_id = EXCLUDED.organization_id,
           name = EXCLUDED.name,
           branch_code = EXCLUDED.branch_code,
           address = EXCLUDED.address,
           timezone = EXCLUDED.timezone,
           business_category = EXCLUDED.business_category,
           max_capacity_people = EXCLUDED.max_capacity_people,
           occupancy_near_pct = EXCLUDED.occupancy_near_pct,
           is_active = TRUE,
           updated_at = NOW();

    -- Usuarios ----------------------------------------------------------------
    -- cli_a: cliente principal de pruebas (D-C16: sin sucursal)
    INSERT INTO public."user" (id, organization_id, branch_id, email, first_name,
                               last_name, phone_number, role, account_status,
                               password_hash, deactivated_at)
    VALUES (v_cli_a, v_org, NULL, 'cli_a@selfiq.test', 'Cliente', 'A',
            '6640000001', 'customer', 'active',
            crypt(v_pw, gen_salt('bf', 12)), NULL)
    ON CONFLICT (id) DO UPDATE
       SET organization_id = EXCLUDED.organization_id,
           branch_id = EXCLUDED.branch_id,
           email = EXCLUDED.email,
           first_name = EXCLUDED.first_name,
           last_name = EXCLUDED.last_name,
           phone_number = EXCLUDED.phone_number,
           role = EXCLUDED.role,
           account_status = EXCLUDED.account_status,
           password_hash = EXCLUDED.password_hash,
           deactivated_at = NULL,
           updated_at = NOW();

    -- cli_b: aislamiento entre clientes (D-C16: sin sucursal)
    INSERT INTO public."user" (id, organization_id, branch_id, email, first_name,
                               last_name, phone_number, role, account_status,
                               password_hash, deactivated_at)
    VALUES (v_cli_b, v_org, NULL, 'cli_b@selfiq.test', 'Cliente', 'B',
            '6640000002', 'customer', 'active',
            crypt(v_pw, gen_salt('bf', 12)), NULL)
    ON CONFLICT (id) DO UPDATE
       SET organization_id = EXCLUDED.organization_id,
           branch_id = EXCLUDED.branch_id,
           email = EXCLUDED.email,
           first_name = EXCLUDED.first_name,
           last_name = EXCLUDED.last_name,
           phone_number = EXCLUDED.phone_number,
           role = EXCLUDED.role,
           account_status = EXCLUDED.account_status,
           password_hash = EXCLUDED.password_hash,
           deactivated_at = NULL,
           updated_at = NOW();

    -- cli_baja: baja lógica, no puede iniciar sesión (D-C16: sin sucursal)
    INSERT INTO public."user" (id, organization_id, branch_id, email, first_name,
                               last_name, phone_number, role, account_status,
                               password_hash, deactivated_at)
    VALUES (v_cli_baja, v_org, NULL, 'cli_baja@selfiq.test', 'Cliente', 'Baja',
            '6640000003', 'customer', 'inactive',
            crypt(v_pw, gen_salt('bf', 12)), NOW())
    ON CONFLICT (id) DO UPDATE
       SET organization_id = EXCLUDED.organization_id,
           branch_id = EXCLUDED.branch_id,
           email = EXCLUDED.email,
           first_name = EXCLUDED.first_name,
           last_name = EXCLUDED.last_name,
           phone_number = EXCLUDED.phone_number,
           role = EXCLUDED.role,
           account_status = EXCLUDED.account_status,
           password_hash = EXCLUDED.password_hash,
           deactivated_at = NOW(),
           updated_at = NOW();

    -- caja: empleado/cajero para el POS web
    INSERT INTO public."user" (id, organization_id, branch_id, email, first_name,
                               last_name, phone_number, role, account_status,
                               password_hash, deactivated_at)
    VALUES (v_caja, v_org, v_branch, 'caja@selfiq.test', 'Caja', 'QA',
            '6640000004', 'employee', 'active',
            crypt(v_pw, gen_salt('bf', 12)), NULL)
    ON CONFLICT (id) DO UPDATE
       SET organization_id = EXCLUDED.organization_id,
           branch_id = EXCLUDED.branch_id,
           email = EXCLUDED.email,
           first_name = EXCLUDED.first_name,
           last_name = EXCLUDED.last_name,
           phone_number = EXCLUDED.phone_number,
           role = EXCLUDED.role,
           account_status = EXCLUDED.account_status,
           password_hash = EXCLUDED.password_hash,
           deactivated_at = NULL,
           updated_at = NOW();

    -- enc: encargado (manager) para mapa, umbrales y anulaciones
    INSERT INTO public."user" (id, organization_id, branch_id, email, first_name,
                               last_name, phone_number, role, account_status,
                               password_hash, deactivated_at)
    VALUES (v_enc, v_org, v_branch, 'enc@selfiq.test', 'Encargado', 'QA',
            '6640000005', 'manager', 'active',
            crypt(v_pw, gen_salt('bf', 12)), NULL)
    ON CONFLICT (id) DO UPDATE
       SET organization_id = EXCLUDED.organization_id,
           branch_id = EXCLUDED.branch_id,
           email = EXCLUDED.email,
           first_name = EXCLUDED.first_name,
           last_name = EXCLUDED.last_name,
           phone_number = EXCLUDED.phone_number,
           role = EXCLUDED.role,
           account_status = EXCLUDED.account_status,
           password_hash = EXCLUDED.password_hash,
           deactivated_at = NULL,
           updated_at = NOW();

    -- Categorías --------------------------------------------------------------
    INSERT INTO public.product_category (id, organization_id, name)
    VALUES (v_cat_abarrotes, v_org, 'Abarrotes')
    ON CONFLICT (organization_id, name) DO UPDATE SET name = EXCLUDED.name
    RETURNING id INTO v_cat_abarrotes;

    INSERT INTO public.product_category (id, organization_id, name)
    VALUES (v_cat_bebidas, v_org, 'Bebidas')
    ON CONFLICT (organization_id, name) DO UPDATE SET name = EXCLUDED.name
    RETURNING id INTO v_cat_bebidas;

    -- Productos ---------------------------------------------------------------
    INSERT INTO public.product (id, organization_id, category_id, sku, name,
                                description, cost_price, sale_price, tax_rate,
                                is_active)
    VALUES (v_p_ok, v_org, v_cat_abarrotes, 'P-OK', 'Café molido 500 g',
            'Producto con ROI al 80 %.', 0, 89.50, 0.16, TRUE)
    ON CONFLICT (organization_id, sku) DO UPDATE
       SET category_id = EXCLUDED.category_id, name = EXCLUDED.name,
           description = EXCLUDED.description, cost_price = EXCLUDED.cost_price,
           sale_price = EXCLUDED.sale_price, tax_rate = EXCLUDED.tax_rate,
           is_active = EXCLUDED.is_active, updated_at = NOW()
    RETURNING id INTO v_p_ok;

    INSERT INTO public.product (id, organization_id, category_id, sku, name,
                                description, cost_price, sale_price, tax_rate,
                                is_active)
    VALUES (v_p_bajo, v_org, v_cat_abarrotes, 'P-BAJO', 'Galletas de avena',
            'Producto con ROI al 20 %.', 0, 35.00, 0.16, TRUE)
    ON CONFLICT (organization_id, sku) DO UPDATE
       SET category_id = EXCLUDED.category_id, name = EXCLUDED.name,
           description = EXCLUDED.description, cost_price = EXCLUDED.cost_price,
           sale_price = EXCLUDED.sale_price, tax_rate = EXCLUDED.tax_rate,
           is_active = EXCLUDED.is_active, updated_at = NOW()
    RETURNING id INTO v_p_bajo;

    INSERT INTO public.product (id, organization_id, category_id, sku, name,
                                description, cost_price, sale_price, tax_rate,
                                is_active)
    VALUES (v_p_agot, v_org, v_cat_abarrotes, 'P-AGOT', 'Leche entera 1 L',
            'Producto con ROI al 0 %.', 0, 28.50, 0.16, TRUE)
    ON CONFLICT (organization_id, sku) DO UPDATE
       SET category_id = EXCLUDED.category_id, name = EXCLUDED.name,
           description = EXCLUDED.description, cost_price = EXCLUDED.cost_price,
           sale_price = EXCLUDED.sale_price, tax_rate = EXCLUDED.tax_rate,
           is_active = EXCLUDED.is_active, updated_at = NOW()
    RETURNING id INTO v_p_agot;

    INSERT INTO public.product (id, organization_id, category_id, sku, name,
                                description, cost_price, sale_price, tax_rate,
                                is_active)
    VALUES (v_p_sinroi, v_org, v_cat_abarrotes, 'P-SINROI', 'Jabón de tocador',
            'Producto activo sin ROI.', 0, 18.00, 0.16, TRUE)
    ON CONFLICT (organization_id, sku) DO UPDATE
       SET category_id = EXCLUDED.category_id, name = EXCLUDED.name,
           description = EXCLUDED.description, cost_price = EXCLUDED.cost_price,
           sale_price = EXCLUDED.sale_price, tax_rate = EXCLUDED.tax_rate,
           is_active = EXCLUDED.is_active, updated_at = NOW()
    RETURNING id INTO v_p_sinroi;

    INSERT INTO public.product (id, organization_id, category_id, sku, name,
                                description, cost_price, sale_price, tax_rate,
                                is_active)
    VALUES (v_p_inact, v_org, v_cat_abarrotes, 'P-INACT', 'Producto descontinuado',
            'Producto con baja lógica.', 0, 10.00, 0.16, FALSE)
    ON CONFLICT (organization_id, sku) DO UPDATE
       SET category_id = EXCLUDED.category_id, name = EXCLUDED.name,
           description = EXCLUDED.description, cost_price = EXCLUDED.cost_price,
           sale_price = EXCLUDED.sale_price, tax_rate = EXCLUDED.tax_rate,
           is_active = EXCLUDED.is_active, updated_at = NOW()
    RETURNING id INTO v_p_inact;

    INSERT INTO public.product (id, organization_id, category_id, sku, name,
                                description, cost_price, sale_price, tax_rate,
                                is_active)
    VALUES (v_p_led, v_org, v_cat_bebidas, 'P-LED', 'Agua natural 600 ml',
            'Producto con ROI y canal LED mapeado.', 0, 15.00, 0.16, TRUE)
    ON CONFLICT (organization_id, sku) DO UPDATE
       SET category_id = EXCLUDED.category_id, name = EXCLUDED.name,
           description = EXCLUDED.description, cost_price = EXCLUDED.cost_price,
           sale_price = EXCLUDED.sale_price, tax_rate = EXCLUDED.tax_rate,
           is_active = EXCLUDED.is_active, updated_at = NOW()
    RETURNING id INTO v_p_led;

    INSERT INTO public.product (id, organization_id, category_id, sku, name,
                                description, cost_price, sale_price, tax_rate,
                                is_active)
    VALUES (v_p_sinled, v_org, v_cat_bebidas, 'P-SINLED', 'Refresco de cola 355 ml',
            'Producto con ROI pero sin canal LED.', 0, 20.00, 0.16, TRUE)
    ON CONFLICT (organization_id, sku) DO UPDATE
       SET category_id = EXCLUDED.category_id, name = EXCLUDED.name,
           description = EXCLUDED.description, cost_price = EXCLUDED.cost_price,
           sale_price = EXCLUDED.sale_price, tax_rate = EXCLUDED.tax_rate,
           is_active = EXCLUDED.is_active, updated_at = NOW()
    RETURNING id INTO v_p_sinled;

    -- Inventario de sucursal (para el POS; no se expone al cliente) ----------
    INSERT INTO public.branch_inventory (branch_id, product_id, quantity_on_hand)
    VALUES
        (v_branch, v_p_ok, 100),
        (v_branch, v_p_bajo, 40),
        (v_branch, v_p_agot, 0),
        (v_branch, v_p_sinroi, 25),
        (v_branch, v_p_inact, 0),
        (v_branch, v_p_led, 200),
        (v_branch, v_p_sinled, 80)
    ON CONFLICT (branch_id, product_id) DO UPDATE
       SET quantity_on_hand = EXCLUDED.quantity_on_hand, updated_at = NOW();

    -- Mapa y repisas ----------------------------------------------------------
    INSERT INTO public.store_map (branch_id, width_cm, height_cm, version, updated_by, updated_at)
    VALUES (v_branch, 1200, 800, 1, v_enc, NOW())
    ON CONFLICT (branch_id) DO UPDATE
       SET width_cm = EXCLUDED.width_cm, height_cm = EXCLUDED.height_cm,
           version = 1, updated_by = EXCLUDED.updated_by, updated_at = NOW();

    INSERT INTO public.shelf (id, branch_id, name, location_label, map_x, map_y,
                              map_width, map_height, map_rotation, is_active)
    VALUES (v_shelf_ok, v_branch, 'Estante A1', 'Pasillo A', 100, 100, 180, 60, 0, TRUE)
    ON CONFLICT (id) DO UPDATE
       SET branch_id = EXCLUDED.branch_id, name = EXCLUDED.name,
           location_label = EXCLUDED.location_label, map_x = EXCLUDED.map_x,
           map_y = EXCLUDED.map_y, map_width = EXCLUDED.map_width,
           map_height = EXCLUDED.map_height, map_rotation = EXCLUDED.map_rotation,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.shelf (id, branch_id, name, location_label, map_x, map_y,
                              map_width, map_height, map_rotation, is_active)
    VALUES (v_shelf_bajo, v_branch, 'Estante A2', 'Pasillo A', 100, 220, 180, 60, 0, TRUE)
    ON CONFLICT (id) DO UPDATE
       SET branch_id = EXCLUDED.branch_id, name = EXCLUDED.name,
           location_label = EXCLUDED.location_label, map_x = EXCLUDED.map_x,
           map_y = EXCLUDED.map_y, map_width = EXCLUDED.map_width,
           map_height = EXCLUDED.map_height, map_rotation = EXCLUDED.map_rotation,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.shelf (id, branch_id, name, location_label, map_x, map_y,
                              map_width, map_height, map_rotation, is_active)
    VALUES (v_shelf_agot, v_branch, 'Estante A3', 'Pasillo A', 100, 340, 180, 60, 0, TRUE)
    ON CONFLICT (id) DO UPDATE
       SET branch_id = EXCLUDED.branch_id, name = EXCLUDED.name,
           location_label = EXCLUDED.location_label, map_x = EXCLUDED.map_x,
           map_y = EXCLUDED.map_y, map_width = EXCLUDED.map_width,
           map_height = EXCLUDED.map_height, map_rotation = EXCLUDED.map_rotation,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.shelf (id, branch_id, name, location_label, map_x, map_y,
                              map_width, map_height, map_rotation, is_active)
    VALUES (v_shelf_led, v_branch, 'Estante B1', 'Pasillo B', 500, 100, 180, 60, 0, TRUE)
    ON CONFLICT (id) DO UPDATE
       SET branch_id = EXCLUDED.branch_id, name = EXCLUDED.name,
           location_label = EXCLUDED.location_label, map_x = EXCLUDED.map_x,
           map_y = EXCLUDED.map_y, map_width = EXCLUDED.map_width,
           map_height = EXCLUDED.map_height, map_rotation = EXCLUDED.map_rotation,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.shelf (id, branch_id, name, location_label, map_x, map_y,
                              map_width, map_height, map_rotation, is_active)
    VALUES (v_shelf_sinled, v_branch, 'Estante B2', 'Pasillo B', 500, 220, 180, 60, 0, TRUE)
    ON CONFLICT (id) DO UPDATE
       SET branch_id = EXCLUDED.branch_id, name = EXCLUDED.name,
           location_label = EXCLUDED.location_label, map_x = EXCLUDED.map_x,
           map_y = EXCLUDED.map_y, map_width = EXCLUDED.map_width,
           map_height = EXCLUDED.map_height, map_rotation = EXCLUDED.map_rotation,
           is_active = TRUE, updated_at = NOW();

    -- ROIs y estados ----------------------------------------------------------
    INSERT INTO public.roi (id, shelf_id, product_id, name, bbox_x, bbox_y,
                            bbox_w, bbox_h, visual_capacity_units,
                            threshold_empty_pct, threshold_low_pct,
                            threshold_medium_pct, confirm_samples,
                            calibration_status, last_calibrated_at, is_active)
    VALUES (v_roi_ok, v_shelf_ok, v_p_ok, 'ROI P-OK', 0.100000, 0.200000,
            0.200000, 0.300000, 10, 5, 25, 60, 3, 'calibrated', NOW(), TRUE)
    ON CONFLICT (id) DO UPDATE
       SET shelf_id = EXCLUDED.shelf_id, product_id = EXCLUDED.product_id,
           name = EXCLUDED.name, bbox_x = EXCLUDED.bbox_x, bbox_y = EXCLUDED.bbox_y,
           bbox_w = EXCLUDED.bbox_w, bbox_h = EXCLUDED.bbox_h,
           visual_capacity_units = EXCLUDED.visual_capacity_units,
           threshold_empty_pct = EXCLUDED.threshold_empty_pct,
           threshold_low_pct = EXCLUDED.threshold_low_pct,
           threshold_medium_pct = EXCLUDED.threshold_medium_pct,
           confirm_samples = EXCLUDED.confirm_samples,
           calibration_status = EXCLUDED.calibration_status,
           last_calibrated_at = EXCLUDED.last_calibrated_at,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.roi (id, shelf_id, product_id, name, bbox_x, bbox_y,
                            bbox_w, bbox_h, visual_capacity_units,
                            threshold_empty_pct, threshold_low_pct,
                            threshold_medium_pct, confirm_samples,
                            calibration_status, last_calibrated_at, is_active)
    VALUES (v_roi_bajo, v_shelf_bajo, v_p_bajo, 'ROI P-BAJO', 0.100000, 0.200000,
            0.200000, 0.300000, 10, 5, 25, 60, 3, 'calibrated', NOW(), TRUE)
    ON CONFLICT (id) DO UPDATE
       SET shelf_id = EXCLUDED.shelf_id, product_id = EXCLUDED.product_id,
           name = EXCLUDED.name, bbox_x = EXCLUDED.bbox_x, bbox_y = EXCLUDED.bbox_y,
           bbox_w = EXCLUDED.bbox_w, bbox_h = EXCLUDED.bbox_h,
           visual_capacity_units = EXCLUDED.visual_capacity_units,
           threshold_empty_pct = EXCLUDED.threshold_empty_pct,
           threshold_low_pct = EXCLUDED.threshold_low_pct,
           threshold_medium_pct = EXCLUDED.threshold_medium_pct,
           confirm_samples = EXCLUDED.confirm_samples,
           calibration_status = EXCLUDED.calibration_status,
           last_calibrated_at = EXCLUDED.last_calibrated_at,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.roi (id, shelf_id, product_id, name, bbox_x, bbox_y,
                            bbox_w, bbox_h, visual_capacity_units,
                            threshold_empty_pct, threshold_low_pct,
                            threshold_medium_pct, confirm_samples,
                            calibration_status, last_calibrated_at, is_active)
    VALUES (v_roi_agot, v_shelf_agot, v_p_agot, 'ROI P-AGOT', 0.100000, 0.200000,
            0.200000, 0.300000, 10, 5, 25, 60, 3, 'calibrated', NOW(), TRUE)
    ON CONFLICT (id) DO UPDATE
       SET shelf_id = EXCLUDED.shelf_id, product_id = EXCLUDED.product_id,
           name = EXCLUDED.name, bbox_x = EXCLUDED.bbox_x, bbox_y = EXCLUDED.bbox_y,
           bbox_w = EXCLUDED.bbox_w, bbox_h = EXCLUDED.bbox_h,
           visual_capacity_units = EXCLUDED.visual_capacity_units,
           threshold_empty_pct = EXCLUDED.threshold_empty_pct,
           threshold_low_pct = EXCLUDED.threshold_low_pct,
           threshold_medium_pct = EXCLUDED.threshold_medium_pct,
           confirm_samples = EXCLUDED.confirm_samples,
           calibration_status = EXCLUDED.calibration_status,
           last_calibrated_at = EXCLUDED.last_calibrated_at,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.roi (id, shelf_id, product_id, name, bbox_x, bbox_y,
                            bbox_w, bbox_h, visual_capacity_units,
                            threshold_empty_pct, threshold_low_pct,
                            threshold_medium_pct, confirm_samples,
                            calibration_status, last_calibrated_at, is_active)
    VALUES (v_roi_led, v_shelf_led, v_p_led, 'ROI P-LED', 0.100000, 0.200000,
            0.200000, 0.300000, 10, 5, 25, 60, 3, 'calibrated', NOW(), TRUE)
    ON CONFLICT (id) DO UPDATE
       SET shelf_id = EXCLUDED.shelf_id, product_id = EXCLUDED.product_id,
           name = EXCLUDED.name, bbox_x = EXCLUDED.bbox_x, bbox_y = EXCLUDED.bbox_y,
           bbox_w = EXCLUDED.bbox_w, bbox_h = EXCLUDED.bbox_h,
           visual_capacity_units = EXCLUDED.visual_capacity_units,
           threshold_empty_pct = EXCLUDED.threshold_empty_pct,
           threshold_low_pct = EXCLUDED.threshold_low_pct,
           threshold_medium_pct = EXCLUDED.threshold_medium_pct,
           confirm_samples = EXCLUDED.confirm_samples,
           calibration_status = EXCLUDED.calibration_status,
           last_calibrated_at = EXCLUDED.last_calibrated_at,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.roi (id, shelf_id, product_id, name, bbox_x, bbox_y,
                            bbox_w, bbox_h, visual_capacity_units,
                            threshold_empty_pct, threshold_low_pct,
                            threshold_medium_pct, confirm_samples,
                            calibration_status, last_calibrated_at, is_active)
    VALUES (v_roi_sinled, v_shelf_sinled, v_p_sinled, 'ROI P-SINLED', 0.100000, 0.200000,
            0.200000, 0.300000, 10, 5, 25, 60, 3, 'calibrated', NOW(), TRUE)
    ON CONFLICT (id) DO UPDATE
       SET shelf_id = EXCLUDED.shelf_id, product_id = EXCLUDED.product_id,
           name = EXCLUDED.name, bbox_x = EXCLUDED.bbox_x, bbox_y = EXCLUDED.bbox_y,
           bbox_w = EXCLUDED.bbox_w, bbox_h = EXCLUDED.bbox_h,
           visual_capacity_units = EXCLUDED.visual_capacity_units,
           threshold_empty_pct = EXCLUDED.threshold_empty_pct,
           threshold_low_pct = EXCLUDED.threshold_low_pct,
           threshold_medium_pct = EXCLUDED.threshold_medium_pct,
           confirm_samples = EXCLUDED.confirm_samples,
           calibration_status = EXCLUDED.calibration_status,
           last_calibrated_at = EXCLUDED.last_calibrated_at,
           is_active = TRUE, updated_at = NOW();

    INSERT INTO public.roi_state (roi_id, occupancy_pct, status, confidence,
                                  last_reading_at, last_changed_at, updated_at)
    VALUES
        (v_roi_ok, 80.00, 'adequate', 0.95, NOW(), NOW(), NOW()),
        (v_roi_bajo, 20.00, 'low', 0.95, NOW(), NOW(), NOW()),
        (v_roi_agot, 0.00, 'empty', 0.95, NOW(), NOW(), NOW()),
        (v_roi_led, 75.00, 'adequate', 0.95, NOW(), NOW(), NOW()),
        (v_roi_sinled, 50.00, 'medium', 0.95, NOW(), NOW(), NOW())
    ON CONFLICT (roi_id) DO UPDATE
       SET occupancy_pct = EXCLUDED.occupancy_pct, status = EXCLUDED.status,
           confidence = EXCLUDED.confidence,
           last_reading_at = EXCLUDED.last_reading_at,
           last_changed_at = EXCLUDED.last_changed_at,
           updated_at = NOW();

    -- Gateway y nodo de guía simulados (para CL4/CL5 sin hardware) -----------
    INSERT INTO public.gateway (id, branch_id, rpi_identifier, mac_address,
                                hostname, is_online, is_active, registered_at)
    VALUES (v_gateway, v_branch, 'rpi-cliente-qa', '02:00:00:00:0C:01',
            'selfiq-cliente-qa', FALSE, TRUE, NOW())
    ON CONFLICT (id) DO UPDATE
       SET branch_id = EXCLUDED.branch_id,
           rpi_identifier = EXCLUDED.rpi_identifier,
           mac_address = EXCLUDED.mac_address,
           hostname = EXCLUDED.hostname,
           is_online = FALSE,
           is_active = TRUE,
           updated_at = NOW();

    INSERT INTO public.esp32_node (id, gateway_id, node_type, mac_address, node_code,
                                   name, registration_status, registered_at,
                                   is_online, power_source)
    VALUES (v_guide_node, v_gateway, 'guide', '02:00:00:00:0C:02', 'GUIDE-QA',
            'Guía LED QA', 'registered', NOW(), FALSE, 'mains')
    ON CONFLICT (id) DO UPDATE
       SET gateway_id = EXCLUDED.gateway_id,
           node_type = EXCLUDED.node_type,
           mac_address = EXCLUDED.mac_address,
           node_code = EXCLUDED.node_code,
           name = EXCLUDED.name,
           registration_status = EXCLUDED.registration_status,
           registered_at = EXCLUDED.registered_at,
           is_online = FALSE,
           updated_at = NOW();

    INSERT INTO public.led_guide_point (id, node_id, node_type, shelf_id, led_index)
    VALUES (v_point, v_guide_node, 'guide', v_shelf_led, 1)
    ON CONFLICT (node_id, led_index) DO UPDATE
       SET node_type = EXCLUDED.node_type,
           shelf_id = EXCLUDED.shelf_id
    RETURNING id INTO v_point;

    -- Ventas de ejemplo ------------------------------------------------------
    INSERT INTO public.sale (id, branch_id, sale_number, cashier_id, customer_id,
                             subtotal, tax, discount, total, payment_method,
                             status, occurred_at)
    VALUES (v_sale_ok, v_branch, 'QA-0001', v_caja, v_cli_a,
            124.50, 19.92, 0, 144.42, 'cash', 'completed', NOW() - INTERVAL '1 day')
    ON CONFLICT (branch_id, sale_number) DO UPDATE
       SET cashier_id = EXCLUDED.cashier_id,
           customer_id = EXCLUDED.customer_id,
           subtotal = EXCLUDED.subtotal,
           tax = EXCLUDED.tax,
           discount = EXCLUDED.discount,
           total = EXCLUDED.total,
           payment_method = EXCLUDED.payment_method,
           status = EXCLUDED.status,
           occurred_at = EXCLUDED.occurred_at,
           updated_at = NOW()
    RETURNING id INTO v_sale_ok;

    INSERT INTO public.sale (id, branch_id, sale_number, cashier_id, customer_id,
                             subtotal, tax, discount, total, payment_method,
                             status, occurred_at)
    VALUES (v_sale_void, v_branch, 'QA-0002', v_caja, v_cli_a,
            35.00, 5.60, 0, 40.60, 'cash', 'voided', NOW() - INTERVAL '2 days')
    ON CONFLICT (branch_id, sale_number) DO UPDATE
       SET cashier_id = EXCLUDED.cashier_id,
           customer_id = EXCLUDED.customer_id,
           subtotal = EXCLUDED.subtotal,
           tax = EXCLUDED.tax,
           discount = EXCLUDED.discount,
           total = EXCLUDED.total,
           payment_method = EXCLUDED.payment_method,
           status = EXCLUDED.status,
           occurred_at = EXCLUDED.occurred_at,
           updated_at = NOW()
    RETURNING id INTO v_sale_void;

    INSERT INTO public.sale_item (id, sale_id, product_id, quantity, unit_price, subtotal)
    VALUES (v_item_ok_1, v_sale_ok, v_p_ok, 1, 89.50, 89.50)
    ON CONFLICT (id) DO UPDATE
       SET sale_id = EXCLUDED.sale_id, product_id = EXCLUDED.product_id,
           quantity = EXCLUDED.quantity, unit_price = EXCLUDED.unit_price,
           subtotal = EXCLUDED.subtotal;

    INSERT INTO public.sale_item (id, sale_id, product_id, quantity, unit_price, subtotal)
    VALUES (v_item_ok_2, v_sale_ok, v_p_bajo, 1, 35.00, 35.00)
    ON CONFLICT (id) DO UPDATE
       SET sale_id = EXCLUDED.sale_id, product_id = EXCLUDED.product_id,
           quantity = EXCLUDED.quantity, unit_price = EXCLUDED.unit_price,
           subtotal = EXCLUDED.subtotal;

    INSERT INTO public.sale_item (id, sale_id, product_id, quantity, unit_price, subtotal)
    VALUES (v_item_void, v_sale_void, v_p_bajo, 1, 35.00, 35.00)
    ON CONFLICT (id) DO UPDATE
       SET sale_id = EXCLUDED.sale_id, product_id = EXCLUDED.product_id,
           quantity = EXCLUDED.quantity, unit_price = EXCLUDED.unit_price,
           subtotal = EXCLUDED.subtotal;

    RAISE NOTICE 'Seed cliente QA aplicado en organización % / sucursal %.', v_org, v_branch;
END
$$;

COMMIT;

-- Resumen del entorno QA
SELECT 'usuarios' AS grupo, count(*) AS total
  FROM public."user" WHERE organization_id = 'c1e00000-0000-4000-8000-000000000001'
UNION ALL
SELECT 'productos', count(*)
  FROM public.product WHERE organization_id = 'c1e00000-0000-4000-8000-000000000001'
UNION ALL
SELECT 'repisas', count(*)
  FROM public.shelf WHERE branch_id = 'c1e00000-0000-4000-8000-000000000002'
UNION ALL
SELECT 'ventas', count(*)
  FROM public.sale WHERE branch_id = 'c1e00000-0000-4000-8000-000000000002';

-- Usuarios QA: cli_a, cli_b, cli_baja, caja, enc @selfiq.test (misma contraseña de prueba).
-- Productos QA: P-OK, P-BAJO, P-AGOT, P-SINROI, P-INACT, P-LED, P-SINLED.

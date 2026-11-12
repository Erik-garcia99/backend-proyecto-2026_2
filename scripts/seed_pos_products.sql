-- Semilla de productos para el POS (ejecutar en Supabase SQL Editor).
-- 1) Asegúrate de haber ejecutado antes scripts/migrate_pos.sql.
-- 2) Reemplaza el UUID de la sucursal en la variable de abajo.

DO $$
DECLARE
  v_branch_id uuid := 'd07018c7-99c1-490f-95b9-dd54589cf60b';  -- <-- REEMPLAZA CON TU BRANCH_ID
  v_org_id    uuid;
  v_amount    int  := 10;  -- cantidad de productos a crear
  n           int;
  v_product   uuid;
BEGIN
  SELECT organization_id INTO v_org_id
  FROM public.branch WHERE id = v_branch_id;

  IF v_org_id IS NULL THEN
    RAISE EXCEPTION 'Sucursal no encontrada: %', v_branch_id;
  END IF;

  -- Crea 3 categorías base si no existen.
  INSERT INTO public.product_category (organization_id, name)
  SELECT v_org_id, c.name
  FROM (VALUES ('Abarrotes'), ('Bebidas'), ('Limpieza')) AS c(name)
  WHERE NOT EXISTS (
    SELECT 1 FROM public.product_category pc
    WHERE pc.organization_id = v_org_id AND pc.name = c.name
  );

  FOR n IN 1..v_amount LOOP
    INSERT INTO public.product (organization_id, sku, name, barcode, sale_price, tax_rate, category_id)
    VALUES (
      v_org_id,
      'POS-' || lpad(n::text, 5, '0'),
      'Producto de prueba ' || n,
      'POS-' || lpad(n::text, 5, '0'),
      10 + (n % 90),
      0.16,
      (SELECT id FROM public.product_category
       WHERE organization_id = v_org_id
         AND name = (ARRAY['Abarrotes','Bebidas','Limpieza'])[(n % 3) + 1])
    )
    ON CONFLICT (organization_id, barcode) DO UPDATE
      SET name = EXCLUDED.name,
          sale_price = EXCLUDED.sale_price,
          tax_rate = EXCLUDED.tax_rate
    RETURNING id INTO v_product;

    INSERT INTO public.branch_inventory (branch_id, product_id, quantity_on_hand)
    VALUES (v_branch_id, v_product, 100)
    ON CONFLICT (branch_id, product_id) DO UPDATE
      SET quantity_on_hand = EXCLUDED.quantity_on_hand,
          updated_at = NOW();
  END LOOP;

  RAISE NOTICE 'Se sembraron % productos en la sucursal %', v_amount, v_branch_id;
END $$;

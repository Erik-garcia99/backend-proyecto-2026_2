-- Asigna categorías a los productos POS existentes (ejecutar en Supabase SQL Editor).
-- Crea 3 categorías para la organización de la sucursal y las asigna a los productos.

DO $$
DECLARE
  v_branch_id uuid := 'd07018c7-99c1-490f-95b9-dd54589cf60b';  -- <-- REEMPLAZA CON TU BRANCH_ID
  v_org_id    uuid;
  cat_ids     uuid[];
BEGIN
  SELECT organization_id INTO v_org_id
  FROM public.branch WHERE id = v_branch_id;

  IF v_org_id IS NULL THEN
    RAISE EXCEPTION 'Sucursal no encontrada: %', v_branch_id;
  END IF;

  -- Crea 3 categorías si no existen (por nombre).
  INSERT INTO public.product_category (organization_id, name)
  SELECT v_org_id, c.name
  FROM (VALUES ('Abarrotes'), ('Bebidas'), ('Limpieza')) AS c(name)
  WHERE NOT EXISTS (
    SELECT 1 FROM public.product_category pc
    WHERE pc.organization_id = v_org_id AND pc.name = c.name
  );

  -- Recupera los IDs de las 3 categorías en orden estable.
  SELECT array_agg(id ORDER BY name) INTO cat_ids
  FROM public.product_category
  WHERE organization_id = v_org_id
    AND name IN ('Abarrotes', 'Bebidas', 'Limpieza');

  -- Asigna categorías a los productos de la organización de forma rotativa.
  WITH numbered AS (
    SELECT id,
           row_number() OVER (ORDER BY sku) - 1 AS rn
    FROM public.product
    WHERE organization_id = v_org_id
  )
  UPDATE public.product p
  SET category_id = cat_ids[(n.rn % array_length(cat_ids, 1)) + 1],
      updated_at = NOW()
  FROM numbered n
  WHERE p.id = n.id;

  RAISE NOTICE 'Categorías asignadas a los productos de la organización %', v_org_id;
END $$;

-- Nombre visible independiente del identificador y hostname del firmware.
BEGIN;
CREATE SEQUENCE IF NOT EXISTS public.gateway_display_number_seq;
ALTER TABLE public.gateway ADD COLUMN IF NOT EXISTS display_name text;
DO $$
DECLARE item record;
BEGIN
  FOR item IN SELECT id FROM public.gateway WHERE display_name IS NULL ORDER BY created_at, id LOOP
    UPDATE public.gateway SET display_name = 'Gateway-' || nextval('public.gateway_display_number_seq') WHERE id = item.id;
  END LOOP;
END $$;
ALTER TABLE public.gateway ALTER COLUMN display_name SET DEFAULT ('Gateway-' || nextval('public.gateway_display_number_seq'));
ALTER TABLE public.gateway ALTER COLUMN display_name SET NOT NULL;
COMMIT;

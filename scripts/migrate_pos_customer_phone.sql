-- POS ↔ Cliente por número de teléfono (decisión 24/11/2026).
-- Idempotente: se puede ejecutar varias veces.
-- No agrega UNIQUE sobre user.phone_number para no romper datos históricos;
-- la unicidad se valida en la aplicación al resolver el cliente.

ALTER TABLE public.sale
  ADD COLUMN IF NOT EXISTS customer_phone text;

CREATE INDEX IF NOT EXISTS idx_sale_customer_phone
  ON public.sale (customer_phone, occurred_at DESC);

COMMENT ON COLUMN public.sale.customer_phone IS
  'Teléfono del cliente capturado en caja; vincula la venta con el usuario de la app móvil. Dato personal (SEC-13/SEC-17).';

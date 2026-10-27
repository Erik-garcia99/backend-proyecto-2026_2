-- Preferencias de notificaciones por usuario (SelfIQ).
-- Aplicar en Supabase/Railway antes de desplegar el backend que las usa.

CREATE TABLE IF NOT EXISTS public.user_preference (
    user_id uuid PRIMARY KEY REFERENCES public."user" (id) ON DELETE CASCADE,
    low_stock boolean NOT NULL DEFAULT true,
    capacity boolean NOT NULL DEFAULT true,
    temperature boolean NOT NULL DEFAULT true,
    offline boolean NOT NULL DEFAULT true,
    sound boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.user_preference ENABLE ROW LEVEL SECURITY;

-- El backend entra como postgres/service_role; la API pública de Supabase
-- no debe leer preferencias de otros usuarios.
REVOKE ALL ON TABLE public.user_preference FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.user_preference TO service_role;

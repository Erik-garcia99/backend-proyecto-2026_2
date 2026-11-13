-- SelfIQ — Crear o actualizar un cliente desde Supabase SQL Editor
--
-- Para qué sirve:
--   Dar de alta un usuario con role='customer' y account_status='active'
--   directamente en la base, sin depender de un entorno local.
--
-- Cuándo usarlo:
--   Mientras CL1 no exista. Hoy la API de Railway solo tiene /auth/register
--   para empleados (exige token de sucursal y deja la cuenta 'pending').
--   Cuando CL1 esté listo, el alta normal será POST /auth/customer-register.
--
-- Seguridad:
--   - Este script pide la contraseña en texto para generar el hash bcrypt con
--     pgcrypto. No guardes el archivo con la contraseña real en el repo.
--   - Usa una contraseña temporal y cámbiala después del primer login.
--   - El SQL Editor guarda historial: no uses una contraseña que reutilices.
--
-- Cómo usarlo:
--   1. Ejecuta la consulta de descubrimiento para copiar organization.slug
--      y branch.branch_code (o los UUID).
--   2. Edita las constantes del bloque DO.
--   3. Ejecuta el bloque completo.
--   4. Verifica con la consulta final y prueba el login contra Railway.

-- ============================================================
-- 1) DESCUBRIMIENTO: ejecuta esto aparte y copia los valores
-- ============================================================
-- select o.id as organization_id, o.name as organization_name, o.slug,
--        b.id as branch_id, b.name as branch_name, b.branch_code
--   from public.organization o
--   join public.branch b on b.organization_id = o.id
--  where o.is_active and b.is_active
--  order by o.name, b.name;

-- ============================================================
-- 2) ALTA DEL CLIENTE: edita las constantes y ejecuta
-- ============================================================
create extension if not exists pgcrypto with schema extensions;
set search_path = public, extensions;

do $do$
declare
  -- ===== EDITA AQUÍ =====
  v_email        text := lower('cliente@ejemplo.com');
  v_password     text := 'CambiaEstaPassword123';
  v_first_name   text := 'Cliente';
  v_last_name    text := 'Demo';
  v_phone        text := null;          -- opcional
  v_org_slug     text := 'selfiq';      -- organization.slug
  v_branch_code  text := 'SUC-001';     -- branch.branch_code
  -- Si tu sucursal no tiene branch_code, deja v_branch_code en null y pega
  -- el UUID en v_branch_id (lo obtienes del descubrimiento).
  v_org_id       uuid := null;          -- opcional: UUID directo
  v_branch_id    uuid := null;          -- opcional: UUID directo
  -- ======================

  v_role         text;
  v_user_id      uuid;
begin
  if length(v_password) < 8 then
    raise exception 'La contraseña debe tener al menos 8 caracteres.';
  end if;

  -- Organización
  if v_org_id is null then
    select id into v_org_id
      from public.organization
     where slug = v_org_slug and is_active;
  else
    perform 1 from public.organization
     where id = v_org_id and is_active;
    if not found then
      raise exception 'La organización % no existe o está inactiva.', v_org_id;
    end if;
  end if;

  if v_org_id is null then
    raise exception 'No existe una organización activa con slug = %.', v_org_slug;
  end if;

  -- Sucursal
  if v_branch_id is null then
    select id into v_branch_id
      from public.branch
     where organization_id = v_org_id
       and branch_code = v_branch_code
       and is_active;
  else
    perform 1 from public.branch
     where id = v_branch_id and organization_id = v_org_id and is_active;
    if not found then
      raise exception 'La sucursal % no existe, está inactiva o no pertenece a la organización.', v_branch_id;
    end if;
  end if;

  if v_branch_id is null then
    raise exception 'No existe una sucursal activa con branch_code = % en la organización %.', v_branch_code, v_org_slug;
  end if;

  -- Alta o actualización
  select id, role into v_user_id, v_role
    from public."user"
   where lower(email) = v_email;

  if v_user_id is null then
    insert into public."user" (
      organization_id, branch_id, email, first_name, last_name, phone_number,
      role, account_status, password_hash, deactivated_at
    ) values (
      v_org_id, v_branch_id, v_email, v_first_name, v_last_name, v_phone,
      'customer', 'active', crypt(v_password, gen_salt('bf', 12)), null
    )
    returning id into v_user_id;
    raise notice 'Cliente creado: % (id %)', v_email, v_user_id;
  else
    if lower(v_role) <> 'customer' then
      raise exception 'El correo % ya existe con rol %. No se convirtió a cliente.', v_email, v_role;
    end if;

    update public."user"
       set organization_id = v_org_id,
           branch_id = v_branch_id,
           first_name = v_first_name,
           last_name = v_last_name,
           phone_number = v_phone,
           account_status = 'active',
           password_hash = crypt(v_password, gen_salt('bf', 12)),
           deactivated_at = null,
           updated_at = now()
     where id = v_user_id;
    raise notice 'Cliente actualizado: % (id %)', v_email, v_user_id;
  end if;
end
$do$;

-- ============================================================
-- 3) VERIFICACIÓN: cambia el correo y ejecuta
-- ============================================================
-- select id, email, role, account_status, organization_id, branch_id, deactivated_at
--   from public."user"
--  where lower(email) = lower('cliente@ejemplo.com');

-- Login esperado contra Railway (200 con role='customer'):
-- curl -s -X POST "$API_URL/auth/login" \
--   -H 'Content-Type: application/json' \
--   -d '{"email":"cliente@ejemplo.com","password":"CambiaEstaPassword123"}'

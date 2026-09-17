-- =====================================================================
--  SHIM LOCAL: lo mínimo de Supabase que necesitan 08/09 para correr en
--  un Postgres normal (WSL). SOLO para la copia local; nunca en Supabase.
--  Crea: roles anon/authenticated/service_role, schema auth con uid() y
--  jwt(), publicación supabase_realtime, y las tablas de `public` que el
--  schema `vendu` referencia (maquinas, motorizados) con las columnas
--  reales de producción (leídas el 17-09-2026 por la API REST).
--  Idempotente.
-- =====================================================================

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN CREATE ROLE authenticator NOLOGIN; END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS auth;

-- auth.uid(): igual que Supabase, lee el claim `sub` del JWT simulado con
--   SELECT set_config('request.jwt.claims', '{"sub":"<uuid>","role":"authenticated"}', true);
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(COALESCE(
    current_setting('request.jwt.claim.sub', true),
    (current_setting('request.jwt.claims', true))::jsonb ->> 'sub'
  ), '')::uuid
$$;

CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    NULLIF(current_setting('request.jwt.claim', true), '')::jsonb,
    NULLIF(current_setting('request.jwt.claims', true), '')::jsonb,
    '{}'::jsonb
  )
$$;

CREATE OR REPLACE FUNCTION auth.role() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    current_setting('request.jwt.claim.role', true),
    (current_setting('request.jwt.claims', true))::jsonb ->> 'role'
  )
$$;

GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
    CREATE PUBLICATION supabase_realtime;
  END IF;
END $$;

-- ---------- public.maquinas (columnas reales de producción) ----------
CREATE TABLE IF NOT EXISTS public.maquinas (
  id               bigint PRIMARY KEY,
  nombre           text NOT NULL,
  motorizado       text,
  lunes            integer DEFAULT 0,
  martes           integer DEFAULT 0,
  miercoles        integer DEFAULT 0,
  jueves           integer DEFAULT 0,
  viernes          integer DEFAULT 0,
  sabado           integer DEFAULT 0,
  observaciones    text,
  estado           text,
  fecha_estado     text,
  llave            text,
  codigo_epay      text,
  direccion        text,
  latitud          double precision,
  longitud         double precision,
  epay_machine_id  bigint,
  epay_uid         text,
  tipo             text NOT NULL DEFAULT 'SNACK',
  ubicacion        text
);
CREATE UNIQUE INDEX IF NOT EXISTS maquinas_epay_machine_id_uq
  ON public.maquinas (epay_machine_id) WHERE epay_machine_id IS NOT NULL;

-- ---------- public.motorizados (lo que usan 01-09 y la app) ----------
CREATE TABLE IF NOT EXISTS public.motorizados (
  id             bigserial PRIMARY KEY,
  nombre         text NOT NULL,
  email          text,
  telefono       text,
  auth_user_id   uuid UNIQUE,
  activo         boolean NOT NULL DEFAULT true,
  es_supervisor  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Permisos como en producción (SQL 02): la app ve/edita su propia ficha.
GRANT SELECT, INSERT, UPDATE ON public.motorizados TO authenticated;
GRANT SELECT, INSERT, UPDATE ON public.maquinas TO anon, authenticated;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;
ALTER TABLE public.motorizados ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mo_select_propio ON public.motorizados;
CREATE POLICY mo_select_propio ON public.motorizados FOR SELECT TO authenticated USING (auth_user_id = auth.uid());
DROP POLICY IF EXISTS mo_insert_propio ON public.motorizados;
CREATE POLICY mo_insert_propio ON public.motorizados FOR INSERT TO authenticated WITH CHECK (auth_user_id = auth.uid());
DROP POLICY IF EXISTS mo_update_propio ON public.motorizados;
CREATE POLICY mo_update_propio ON public.motorizados FOR UPDATE TO authenticated
  USING (auth_user_id = auth.uid()) WITH CHECK (auth_user_id = auth.uid());

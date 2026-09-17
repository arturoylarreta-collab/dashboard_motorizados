-- =====================================================================
--  VENDU — CORRECCIONES FASE 1   ·   ARCHIVO 10   (17-09-2026)
--  Para bases donde 08/09 YA corrieron: ALTERs que `CREATE TABLE IF NOT
--  EXISTS` no aplica. Idempotente. Correr como `postgres` DESPUÉS de
--  volver a correr 08 (funciones) y 09 (RPC).
--
--  1. products.nombre_norm deja de ser UNIQUE (el catálogo de ePay repite
--     nombres; con UNIQUE el sync falla). Queda un índice normal.
--  2. inventory_movements.cantidad > 0 (antes <> 0: un negativo invertía el
--     sentido del movimiento y esquivaba la validación de stock negativo).
--  3. Escalada a supervisor: `public.motorizados.es_supervisor` solo puede
--     cambiarlo el backend (sin JWT) o service_role; la app (rol
--     authenticated, con JWT) NO. Antes cualquier chofer podía ponerse
--     supervisor por PostgREST y ver/operar todo `vendu`.
-- =====================================================================

-- ---------- 1. nombre_norm no único ----------
ALTER TABLE vendu.products DROP CONSTRAINT IF EXISTS products_nombre_norm_key;
CREATE INDEX IF NOT EXISTS vendu_products_nombre_norm_idx ON vendu.products (nombre_norm);

-- ---------- 2. cantidad > 0 ----------
DO $$
DECLARE v_con text;
BEGIN
  FOR v_con IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = 'vendu.inventory_movements'::regclass
       AND contype = 'c' AND pg_get_constraintdef(oid) ILIKE '%cantidad%'
  LOOP
    EXECUTE format('ALTER TABLE vendu.inventory_movements DROP CONSTRAINT %I', v_con);
  END LOOP;
  ALTER TABLE vendu.inventory_movements
    ADD CONSTRAINT inventory_movements_cantidad_positiva CHECK (cantidad > 0);
END $$;

-- ---------- 3. Bloqueo de la escalada a supervisor ----------
CREATE OR REPLACE FUNCTION public.fn_proteger_es_supervisor()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_rol text := COALESCE(
    current_setting('request.jwt.claim.role', true),
    NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role');
BEGIN
  -- Solo importa si el valor cambia (o se inserta en true).
  IF (TG_OP = 'UPDATE' AND NEW.es_supervisor IS DISTINCT FROM OLD.es_supervisor)
     OR (TG_OP = 'INSERT' AND NEW.es_supervisor) THEN
    -- Peticiones de la app llegan por PostgREST con JWT de rol authenticated/anon.
    IF v_rol IN ('authenticated', 'anon') THEN
      RAISE EXCEPTION 'es_supervisor solo lo cambia el backend (rol %, no permitido)', v_rol
        USING ERRCODE = '42501';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_proteger_es_supervisor ON public.motorizados;
CREATE TRIGGER trg_proteger_es_supervisor
BEFORE INSERT OR UPDATE ON public.motorizados
FOR EACH ROW EXECUTE FUNCTION public.fn_proteger_es_supervisor();

-- Fin archivo 10.

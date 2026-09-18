-- =====================================================================
--  VENDU — FUNCIONES PARA LA APP DE MOTORIZADOS   ·   ARCHIVO 13   (18-09-2026)
--  Pedido de Juan: el motorizado trabaja SOLO desde la app:
--    1. recoge en la oficina (oficina -> su bolso)
--    2. ve su bolso (inventario temporal; los bolsos no se mezclan)
--    3. recarga cualquier máquina desde su bolso (uso libre, por canal)
--    4. retira producto de una máquina (máquina -> bolso)
--    5. devuelve a la oficina (bolso -> oficina; el bolso vuelve a cero)
--
--  Todo es RPC SECURITY DEFINER: la app no escribe tablas directamente. El usuario
--  SIEMPRE es auth.uid(); nadie puede tocar el bolso de otro. Cada llamada es
--  todo-o-nada e idempotente (p_key lo genera el teléfono; reintentar no duplica).
--
--  ePay NUNCA se escribe desde el teléfono: la recarga/retiro deja una fila en
--  vendu.epay_cola_recargas y el servidor (con el token de escritura) la aplica.
--  Idempotente. Requiere 08 y 09.
-- =====================================================================

-- ---------- 0. Cola de escrituras en ePay generadas por la app ----------
CREATE TABLE IF NOT EXISTS vendu.epay_cola_recargas (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  clave         text NOT NULL UNIQUE,            -- p_key de la app: una fila por operación
  maquina_id    bigint NOT NULL REFERENCES public.maquinas(id),
  motorizado_id uuid NOT NULL,
  cambios       jsonb NOT NULL,                  -- [{seleccion, slot, product_id, sumar}] (sumar < 0 = retiro)
  estado        text NOT NULL DEFAULT 'PENDIENTE',   -- PENDIENTE | OK | ERROR
  intentos      integer NOT NULL DEFAULT 0,
  lote          text,
  respuesta     jsonb,
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vendu_epay_cola_estado_idx ON vendu.epay_cola_recargas (estado, created_at);
ALTER TABLE vendu.epay_cola_recargas ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS vdz_cola_select ON vendu.epay_cola_recargas;
CREATE POLICY vdz_cola_select ON vendu.epay_cola_recargas FOR SELECT TO authenticated
  USING (motorizado_id = auth.uid() OR vendu.es_supervisor());
GRANT SELECT ON vendu.epay_cola_recargas TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON vendu.epay_cola_recargas TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vendu TO service_role;

-- ---------- 1. Ayudantes internos (no se exponen a la app) ----------
CREATE OR REPLACE FUNCTION vendu._mi_bolso_id()
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_uid uuid := auth.uid();
  v_id  bigint;
BEGIN
  IF v_uid IS NULL THEN
    RAISE EXCEPTION 'No autenticado';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.motorizados WHERE auth_user_id = v_uid) THEN
    RAISE EXCEPTION 'Tu usuario no está registrado como motorizado';
  END IF;
  SELECT id INTO v_id FROM vendu.inventory_locations
   WHERE tipo = 'MOTORIZADO' AND motorizado_id = v_uid;
  IF v_id IS NULL THEN
    INSERT INTO vendu.inventory_locations (tipo, motorizado_id)
    VALUES ('MOTORIZADO', v_uid) RETURNING id INTO v_id;
  END IF;
  RETURN v_id;
END;
$$;

CREATE OR REPLACE FUNCTION vendu._oficina_id()
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE v_id bigint;
BEGIN
  SELECT id INTO v_id FROM vendu.inventory_locations WHERE tipo = 'PRINCIPAL' ORDER BY id LIMIT 1;
  IF v_id IS NULL THEN
    RAISE EXCEPTION 'No existe la ubicación PRINCIPAL (oficina)';
  END IF;
  RETURN v_id;
END;
$$;

CREATE OR REPLACE FUNCTION vendu._maquina_loc_id(p_maquina_id bigint)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE v_id bigint;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.maquinas WHERE id = p_maquina_id) THEN
    RAISE EXCEPTION 'La máquina % no existe', p_maquina_id;
  END IF;
  SELECT id INTO v_id FROM vendu.inventory_locations
   WHERE tipo = 'MAQUINA' AND maquina_id = p_maquina_id;
  IF v_id IS NULL THEN
    INSERT INTO vendu.inventory_locations (tipo, maquina_id)
    VALUES ('MAQUINA', p_maquina_id) RETURNING id INTO v_id;
  END IF;
  RETURN v_id;
END;
$$;

REVOKE ALL ON FUNCTION vendu._mi_bolso_id()          FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION vendu._oficina_id()           FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION vendu._maquina_loc_id(bigint) FROM PUBLIC, anon, authenticated;

-- ---------- 2. Lecturas ----------
-- 2.1 Mi bolso
DROP FUNCTION IF EXISTS vendu.mi_bolso();
CREATE OR REPLACE FUNCTION vendu.mi_bolso()
RETURNS TABLE (product_id bigint, nombre text, codigo_epay text, categoria text, cantidad numeric)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = vendu, public
AS $$
  SELECT p.id, p.nombre, p.codigo_epay, p.categoria, b.quantity
    FROM vendu.inventory_locations l
    JOIN vendu.inventory_balances b ON b.location_id = l.id AND b.quantity > 0
    JOIN vendu.products p ON p.id = b.product_id
   WHERE l.tipo = 'MOTORIZADO' AND l.motorizado_id = auth.uid()
   ORDER BY p.nombre
$$;

-- 2.2 Lo que hay en la oficina para recoger
DROP FUNCTION IF EXISTS vendu.stock_oficina();
CREATE OR REPLACE FUNCTION vendu.stock_oficina()
RETURNS TABLE (product_id bigint, nombre text, codigo_epay text, categoria text, cantidad numeric)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = vendu, public
AS $$
  SELECT p.id, p.nombre, p.codigo_epay, p.categoria, b.quantity
    FROM vendu.inventory_locations l
    JOIN vendu.inventory_balances b ON b.location_id = l.id AND b.quantity > 0
    JOIN vendu.products p ON p.id = b.product_id
   WHERE l.tipo = 'PRINCIPAL' AND auth.uid() IS NOT NULL
   ORDER BY p.nombre
$$;

-- 2.3 Planograma de una máquina + cuánto de cada producto llevo en el bolso
DROP FUNCTION IF EXISTS vendu.planograma_app(bigint);
CREATE OR REPLACE FUNCTION vendu.planograma_app(p_maquina_id bigint)
RETURNS TABLE (seleccion text, slot text, product_id bigint, nombre text, cantidad numeric,
               maximo numeric, espacio numeric, en_mi_bolso numeric, recomendado numeric)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = vendu, public
AS $$
  SELECT ms.seleccion, ms.slot, ms.product_id, COALESCE(p.nombre, '(sin producto)'),
         COALESCE(ms.cantidad, 0), COALESCE(ms.maximo, 0),
         GREATEST(COALESCE(ms.maximo, 0) - COALESCE(ms.cantidad, 0), 0),
         COALESCE((SELECT b.quantity FROM vendu.inventory_locations l
                     JOIN vendu.inventory_balances b ON b.location_id = l.id
                    WHERE l.tipo = 'MOTORIZADO' AND l.motorizado_id = auth.uid()
                      AND b.product_id = ms.product_id), 0),
         COALESCE((SELECT r.cantidad_recomendada FROM vendu.replenishment_recommendations r
                    WHERE r.maquina_id = ms.maquina_id AND r.seleccion = ms.seleccion
                    ORDER BY r.calculado_en DESC LIMIT 1), 0)
    FROM vendu.machine_slots ms
    LEFT JOIN vendu.products p ON p.id = ms.product_id
   WHERE ms.maquina_id = p_maquina_id AND ms.activo AND auth.uid() IS NOT NULL
   ORDER BY ms.seleccion
$$;

-- ---------- 3. Escrituras ----------
-- 3.1 Recoger en la oficina: oficina -> mi bolso.  p_items = [{"product_id":1,"cantidad":6}, ...]
DROP FUNCTION IF EXISTS vendu.recoger_en_oficina(jsonb, text);
CREATE OR REPLACE FUNCTION vendu.recoger_en_oficina(p_items jsonb, p_key text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_bolso   bigint := vendu._mi_bolso_id();
  v_oficina bigint := vendu._oficina_id();
  v_it      record;
  v_n       integer := 0;
  v_total   numeric := 0;
BEGIN
  IF COALESCE(p_key, '') = '' THEN RAISE EXCEPTION 'Falta la clave de la operación'; END IF;
  IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
    RAISE EXCEPTION 'No hay productos que recoger';
  END IF;
  FOR v_it IN
    SELECT (e->>'product_id')::bigint AS pid, SUM((e->>'cantidad')::numeric) AS cant
      FROM jsonb_array_elements(p_items) e GROUP BY 1
  LOOP
    IF v_it.cant IS NULL OR v_it.cant <= 0 OR v_it.cant <> trunc(v_it.cant) THEN
      RAISE EXCEPTION 'Cantidad inválida para el producto %: %', v_it.pid, v_it.cant;
    END IF;
    -- El trigger de saldos rechaza (y deshace todo) si la oficina no tiene suficiente.
    INSERT INTO vendu.inventory_movements
      (tipo, product_id, cantidad, origen_id, destino_id, motorizado_id, usuario, observacion, idempotency_key)
    VALUES ('TRANSFERENCIA_A_MOTORIZADO', v_it.pid, v_it.cant, v_oficina, v_bolso, auth.uid(),
            auth.jwt()->>'email', 'Recogida en oficina desde la app', 'app-rec-' || p_key || '-' || v_it.pid)
    ON CONFLICT (idempotency_key) DO NOTHING;
    v_n := v_n + 1; v_total := v_total + v_it.cant;
  END LOOP;
  RETURN jsonb_build_object('ok', true, 'productos', v_n, 'unidades', v_total);
END;
$$;

-- 3.2 Recargar una máquina desde mi bolso (uso libre, por canal).
--     p_items = [{"seleccion":"11","cantidad":4}, ...]
DROP FUNCTION IF EXISTS vendu.recargar_desde_bolso(bigint, jsonb, text);
CREATE OR REPLACE FUNCTION vendu.recargar_desde_bolso(p_maquina_id bigint, p_items jsonb, p_key text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_bolso   bigint := vendu._mi_bolso_id();
  v_maq     bigint := vendu._maquina_loc_id(p_maquina_id);
  v_it      record;
  v_slot    vendu.machine_slots%ROWTYPE;
  v_cambios jsonb := '[]'::jsonb;
  v_total   numeric := 0;
BEGIN
  IF COALESCE(p_key, '') = '' THEN RAISE EXCEPTION 'Falta la clave de la operación'; END IF;
  IF EXISTS (SELECT 1 FROM vendu.epay_cola_recargas WHERE clave = 'app-rcg-' || p_key) THEN
    RETURN jsonb_build_object('ok', true, 'ya_registrada', true);
  END IF;
  IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
    RAISE EXCEPTION 'No hay canales que recargar';
  END IF;
  FOR v_it IN
    SELECT e->>'seleccion' AS sel, SUM((e->>'cantidad')::numeric) AS cant
      FROM jsonb_array_elements(p_items) e GROUP BY 1
  LOOP
    IF v_it.cant IS NULL OR v_it.cant <= 0 OR v_it.cant <> trunc(v_it.cant) THEN
      RAISE EXCEPTION 'Cantidad inválida en el canal %: %', v_it.sel, v_it.cant;
    END IF;
    SELECT * INTO v_slot FROM vendu.machine_slots
     WHERE maquina_id = p_maquina_id AND seleccion = v_it.sel AND activo FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'El canal % no existe en esta máquina', v_it.sel; END IF;
    IF v_slot.product_id IS NULL THEN RAISE EXCEPTION 'El canal % no tiene producto asignado', v_it.sel; END IF;
    IF v_it.cant > GREATEST(COALESCE(v_slot.maximo,0) - COALESCE(v_slot.cantidad,0), 0) THEN
      RAISE EXCEPTION 'En el canal % solo caben % unidades (quisiste poner %)', v_it.sel,
        GREATEST(COALESCE(v_slot.maximo,0) - COALESCE(v_slot.cantidad,0), 0), v_it.cant;
    END IF;
    -- Sale de MI bolso; si no tengo suficiente, el trigger lo rechaza y no se mueve nada.
    INSERT INTO vendu.inventory_movements
      (tipo, product_id, cantidad, origen_id, destino_id, motorizado_id, maquina_id, seleccion,
       usuario, observacion, idempotency_key)
    VALUES ('RECARGA_MAQUINA', v_slot.product_id, v_it.cant, v_bolso, v_maq, auth.uid(), p_maquina_id,
            v_it.sel, auth.jwt()->>'email', 'Recarga desde la app', 'app-rcg-' || p_key || '-' || v_it.sel);
    -- Se refleja ya en el planograma; la próxima sincronización con ePay pone la verdad.
    UPDATE vendu.machine_slots SET cantidad = COALESCE(cantidad,0) + v_it.cant WHERE id = v_slot.id;
    v_cambios := v_cambios || jsonb_build_object('seleccion', v_it.sel, 'slot', v_slot.slot,
                                                 'product_id', v_slot.product_id, 'sumar', v_it.cant);
    v_total := v_total + v_it.cant;
  END LOOP;
  INSERT INTO vendu.epay_cola_recargas (clave, maquina_id, motorizado_id, cambios)
  VALUES ('app-rcg-' || p_key, p_maquina_id, auth.uid(), v_cambios);
  RETURN jsonb_build_object('ok', true, 'canales', jsonb_array_length(v_cambios), 'unidades', v_total);
END;
$$;

-- 3.3 Retirar producto de una máquina a mi bolso (lo que no se vende, vencido, cambio de producto).
DROP FUNCTION IF EXISTS vendu.retirar_de_maquina(bigint, jsonb, text);
CREATE OR REPLACE FUNCTION vendu.retirar_de_maquina(p_maquina_id bigint, p_items jsonb, p_key text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_bolso   bigint := vendu._mi_bolso_id();
  v_maq     bigint := vendu._maquina_loc_id(p_maquina_id);
  v_it      record;
  v_slot    vendu.machine_slots%ROWTYPE;
  v_cambios jsonb := '[]'::jsonb;
  v_total   numeric := 0;
BEGIN
  IF COALESCE(p_key, '') = '' THEN RAISE EXCEPTION 'Falta la clave de la operación'; END IF;
  IF EXISTS (SELECT 1 FROM vendu.epay_cola_recargas WHERE clave = 'app-ret-' || p_key) THEN
    RETURN jsonb_build_object('ok', true, 'ya_registrada', true);
  END IF;
  IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
    RAISE EXCEPTION 'No hay canales que retirar';
  END IF;
  FOR v_it IN
    SELECT e->>'seleccion' AS sel, SUM((e->>'cantidad')::numeric) AS cant
      FROM jsonb_array_elements(p_items) e GROUP BY 1
  LOOP
    IF v_it.cant IS NULL OR v_it.cant <= 0 OR v_it.cant <> trunc(v_it.cant) THEN
      RAISE EXCEPTION 'Cantidad inválida en el canal %: %', v_it.sel, v_it.cant;
    END IF;
    SELECT * INTO v_slot FROM vendu.machine_slots
     WHERE maquina_id = p_maquina_id AND seleccion = v_it.sel AND activo FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'El canal % no existe en esta máquina', v_it.sel; END IF;
    IF v_slot.product_id IS NULL THEN RAISE EXCEPTION 'El canal % no tiene producto asignado', v_it.sel; END IF;
    IF v_it.cant > COALESCE(v_slot.cantidad, 0) THEN
      RAISE EXCEPTION 'En el canal % solo hay % unidades (quisiste retirar %)', v_it.sel,
        COALESCE(v_slot.cantidad, 0), v_it.cant;
    END IF;
    -- La máquina la manda ePay: su saldo aquí puede no existir; no se exige origen con saldo.
    INSERT INTO vendu.inventory_movements
      (tipo, product_id, cantidad, origen_id, destino_id, motorizado_id, maquina_id, seleccion,
       usuario, observacion, idempotency_key)
    VALUES ('RETIRO_DE_MAQUINA', v_slot.product_id, v_it.cant, NULL, v_bolso, auth.uid(), p_maquina_id,
            v_it.sel, auth.jwt()->>'email', 'Retiro de máquina desde la app', 'app-ret-' || p_key || '-' || v_it.sel);
    UPDATE vendu.machine_slots SET cantidad = COALESCE(cantidad,0) - v_it.cant WHERE id = v_slot.id;
    v_cambios := v_cambios || jsonb_build_object('seleccion', v_it.sel, 'slot', v_slot.slot,
                                                 'product_id', v_slot.product_id, 'sumar', -v_it.cant);
    v_total := v_total + v_it.cant;
  END LOOP;
  INSERT INTO vendu.epay_cola_recargas (clave, maquina_id, motorizado_id, cambios)
  VALUES ('app-ret-' || p_key, p_maquina_id, auth.uid(), v_cambios);
  RETURN jsonb_build_object('ok', true, 'canales', jsonb_array_length(v_cambios), 'unidades', v_total);
END;
$$;

-- 3.4 Devolver a la oficina: mi bolso -> oficina.  p_items NULL o [] = devolver TODO (bolso a cero).
DROP FUNCTION IF EXISTS vendu.devolver_a_oficina(jsonb, text);
CREATE OR REPLACE FUNCTION vendu.devolver_a_oficina(p_items jsonb, p_key text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_bolso   bigint := vendu._mi_bolso_id();
  v_oficina bigint := vendu._oficina_id();
  v_it      record;
  v_n       integer := 0;
  v_total   numeric := 0;
  v_todo    boolean := (p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0);
BEGIN
  IF COALESCE(p_key, '') = '' THEN RAISE EXCEPTION 'Falta la clave de la operación'; END IF;
  IF EXISTS (SELECT 1 FROM vendu.inventory_movements WHERE idempotency_key LIKE 'app-dev-' || p_key || '-%') THEN
    RETURN jsonb_build_object('ok', true, 'ya_registrada', true);
  END IF;
  FOR v_it IN
    SELECT pid, cant FROM (
      SELECT b.product_id AS pid, b.quantity AS cant
        FROM vendu.inventory_balances b WHERE v_todo AND b.location_id = v_bolso AND b.quantity > 0
      UNION ALL
      SELECT (e->>'product_id')::bigint, SUM((e->>'cantidad')::numeric)
        FROM jsonb_array_elements(CASE WHEN v_todo THEN '[]'::jsonb ELSE p_items END) e GROUP BY 1
    ) x
  LOOP
    IF v_it.cant IS NULL OR v_it.cant <= 0 THEN
      RAISE EXCEPTION 'Cantidad inválida para el producto %: %', v_it.pid, v_it.cant;
    END IF;
    INSERT INTO vendu.inventory_movements
      (tipo, product_id, cantidad, origen_id, destino_id, motorizado_id, usuario, observacion, idempotency_key)
    VALUES ('DEVOLUCION_DE_MOTORIZADO', v_it.pid, v_it.cant, v_bolso, v_oficina, auth.uid(),
            auth.jwt()->>'email', 'Devolución a oficina desde la app', 'app-dev-' || p_key || '-' || v_it.pid);
    v_n := v_n + 1; v_total := v_total + v_it.cant;
  END LOOP;
  RETURN jsonb_build_object('ok', true, 'productos', v_n, 'unidades', v_total, 'bolso_en_cero', v_todo);
END;
$$;

GRANT EXECUTE ON FUNCTION vendu.mi_bolso()                               TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.stock_oficina()                          TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.planograma_app(bigint)                   TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.recoger_en_oficina(jsonb, text)          TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.recargar_desde_bolso(bigint, jsonb, text) TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.retirar_de_maquina(bigint, jsonb, text)  TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.devolver_a_oficina(jsonb, text)          TO authenticated;

-- Fin archivo 13.

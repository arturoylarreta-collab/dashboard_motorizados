-- =====================================================================
--  VENDU — RLS + grants `authenticated` + RPC + Realtime  ·   ARCHIVO 9
--  T11: habilita que la app Flutter (rol `authenticated` con la clave
--  publishable) lea/opere SOLO lo suyo sobre `vendu`, sin abrir el sistema.
--
--  Backend y dashboard siguen conectando como `postgres` (BYPASSRLS):
--  el RLS NO los afecta.
--
--  Decisiones T11 confirmadas:
--    * Movimientos SOLO vía RPC SECURITY DEFINER (recargar_maquina,
--      devolver_motorizado); la app jamás inserta inventory_movements.
--    * La orden PROPUESTA la genera el algoritmo de demanda (backend);
--      el motorizado NO crea órdenes. INSERT restringido a supervisor.
--    * Realtime: habilitado para replenishment_orders + order_items.
--    * es_supervisor: solo arturoylarreta (id 18) => true.
--
-- Idempotente (se puede repetir). Correr con conexión directa `postgres`.
-- NOTA: además, el runner aplica (sin pisar config) la exposición del schema
-- ante PostgREST para que la app use `rpc()`:
--   ALTER ROLE authenticator IN DATABASE postgres SET pgrst.db_schemas = <actual + vendu>;
--   NOTIFY pgrst, 'reload config';
-- =====================================================================

-- ---------- 0. GRANTS a `authenticated` ----------
GRANT USAGE ON SCHEMA vendu TO authenticated;
GRANT SELECT ON vendu.products, vendu.product_mappings, vendu.maquinas_epay,
                 vendu.machine_slots, vendu.replenishment_recommendations,
                 vendu.replenishment_orders, vendu.replenishment_order_items,
                 vendu.inventory_locations, vendu.inventory_balances,
                 vendu.inventory_movements, vendu.epay_sales,
                 vendu.inventory_reconciliation, vendu.sync_log,
                 vendu.audit_logs, vendu.order_status_log TO authenticated;
GRANT SELECT, UPDATE ON vendu.replenishment_orders TO authenticated;
GRANT SELECT, UPDATE ON vendu.replenishment_order_items TO authenticated;
GRANT INSERT ON vendu.replenishment_orders TO authenticated;  -- solo supervisor (RLS)
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vendu TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA vendu GRANT SELECT ON TABLES TO authenticated;

-- ---------- 1. Helper de rol ----------
-- (sin DROP: las políticas vdz_* dependen de esta función y el DROP sin CASCADE
--  hacía fallar la segunda corrida del archivo. CREATE OR REPLACE basta.)
CREATE OR REPLACE FUNCTION vendu.es_supervisor()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = vendu, public
AS $$
  SELECT COALESCE((SELECT m.es_supervisor FROM public.motorizados m
                   WHERE m.auth_user_id = auth.uid()), false)
$$;
GRANT EXECUTE ON FUNCTION vendu.es_supervisor() TO authenticated;

-- ---------- 2. Habilitar RLS en vendu ----------
ALTER TABLE vendu.products                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.product_mappings             ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.maquinas_epay                ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.machine_slots                ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.replenishment_recommendations ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.replenishment_orders         ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.replenishment_order_items    ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.inventory_locations          ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.inventory_balances           ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.inventory_movements          ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.epay_sales                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.inventory_reconciliation     ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.sync_log                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.audit_logs                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.order_status_log             ENABLE ROW LEVEL SECURITY;

-- ---------- 3. Políticas (prefijo vdz_) ----------
-- Catálogo / planograma: lectura global autenticado
DROP POLICY IF EXISTS vdz_prod_select ON vendu.products;
CREATE POLICY vdz_prod_select ON vendu.products FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS vdz_pm_select ON vendu.product_mappings;
CREATE POLICY vdz_pm_select ON vendu.product_mappings FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS vdz_me_select ON vendu.maquinas_epay;
CREATE POLICY vdz_me_select ON vendu.maquinas_epay FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS vdz_ms_select ON vendu.machine_slots;
CREATE POLICY vdz_ms_select ON vendu.machine_slots FOR SELECT TO authenticated USING (true);

-- Recomendaciones: supervisor todo; motorizado las máquinas que tiene en orden
DROP POLICY IF EXISTS vdz_rec_select ON vendu.replenishment_recommendations;
CREATE POLICY vdz_rec_select ON vendu.replenishment_recommendations FOR SELECT TO authenticated
  USING (vendu.es_supervisor() OR EXISTS (
    SELECT 1 FROM vendu.replenishment_orders o
    WHERE o.maquina_id = replenishment_recommendations.maquina_id
      AND o.motorizado_id = auth.uid()
      AND o.estado NOT IN ('COMPLETADA','CANCELADA')));

-- Órdenes: cada motorizado ve/avanza las suyas; supervisor todo; insert solo supervisor
DROP POLICY IF EXISTS vdz_ro_select ON vendu.replenishment_orders;
CREATE POLICY vdz_ro_select ON vendu.replenishment_orders FOR SELECT TO authenticated
  USING (vendu.es_supervisor() OR motorizado_id = auth.uid());
DROP POLICY IF EXISTS vdz_ro_update ON vendu.replenishment_orders;
CREATE POLICY vdz_ro_update ON vendu.replenishment_orders FOR UPDATE TO authenticated
  USING (vendu.es_supervisor() OR motorizado_id = auth.uid())
  WITH CHECK (vendu.es_supervisor() OR motorizado_id = auth.uid());
DROP POLICY IF EXISTS vdz_ro_insert ON vendu.replenishment_orders;
CREATE POLICY vdz_ro_insert ON vendu.replenishment_orders FOR INSERT TO authenticated
  WITH CHECK (vendu.es_supervisor());

-- Ítems: a través de la orden del motorizado
DROP POLICY IF EXISTS vdz_roi_select ON vendu.replenishment_order_items;
CREATE POLICY vdz_roi_select ON vendu.replenishment_order_items FOR SELECT TO authenticated
  USING (vendu.es_supervisor() OR EXISTS (
    SELECT 1 FROM vendu.replenishment_orders o
    WHERE o.id = orden_id AND o.motorizado_id = auth.uid()));
DROP POLICY IF EXISTS vdz_roi_update ON vendu.replenishment_order_items;
CREATE POLICY vdz_roi_update ON vendu.replenishment_order_items FOR UPDATE TO authenticated
  USING (vendu.es_supervisor() OR EXISTS (
    SELECT 1 FROM vendu.replenishment_orders o
    WHERE o.id = orden_id AND o.motorizado_id = auth.uid()))
  WITH CHECK (vendu.es_supervisor() OR EXISTS (
    SELECT 1 FROM vendu.replenishment_orders o
    WHERE o.id = orden_id AND o.motorizado_id = auth.uid()));

-- Movimientos: SOLO lectura (la escritura es vía RPC SECURITY DEFINER)
DROP POLICY IF EXISTS vdz_im_select ON vendu.inventory_movements;
CREATE POLICY vdz_im_select ON vendu.inventory_movements FOR SELECT TO authenticated
  USING (vendu.es_supervisor() OR motorizado_id = auth.uid());

-- Ubicaciones/balances: MAQUINA (estado de máquina) + MOTORIZADO propio + supervisor
DROP POLICY IF EXISTS vdz_il_select ON vendu.inventory_locations;
CREATE POLICY vdz_il_select ON vendu.inventory_locations FOR SELECT TO authenticated
  USING (vendu.es_supervisor()
         OR tipo = 'MAQUINA'
         OR (tipo = 'MOTORIZADO' AND motorizado_id = auth.uid()));
DROP POLICY IF EXISTS vdz_ib_select ON vendu.inventory_balances;
CREATE POLICY vdz_ib_select ON vendu.inventory_balances FOR SELECT TO authenticated
  USING (vendu.es_supervisor() OR EXISTS (
    SELECT 1 FROM vendu.inventory_locations l
    WHERE l.id = location_id AND (l.tipo = 'MAQUINA'
                                  OR (l.tipo = 'MOTORIZADO' AND l.motorizado_id = auth.uid()))));

-- Internos/operativos: solo supervisor
DROP POLICY IF EXISTS vdz_es_select ON vendu.epay_sales;
CREATE POLICY vdz_es_select ON vendu.epay_sales FOR SELECT TO authenticated
  USING (vendu.es_supervisor());
DROP POLICY IF EXISTS vdz_ir_select ON vendu.inventory_reconciliation;
CREATE POLICY vdz_ir_select ON vendu.inventory_reconciliation FOR SELECT TO authenticated
  USING (vendu.es_supervisor());
DROP POLICY IF EXISTS vdz_sl_select ON vendu.sync_log;
CREATE POLICY vdz_sl_select ON vendu.sync_log FOR SELECT TO authenticated
  USING (vendu.es_supervisor());
DROP POLICY IF EXISTS vdz_al_select ON vendu.audit_logs;
CREATE POLICY vdz_al_select ON vendu.audit_logs FOR SELECT TO authenticated
  USING (vendu.es_supervisor());
DROP POLICY IF EXISTS vdz_osl_select ON vendu.order_status_log;
CREATE POLICY vdz_osl_select ON vendu.order_status_log FOR SELECT TO authenticated
  USING (vendu.es_supervisor());

-- ---------- 4. RPC (SECURITY DEFINER, search_path fijo) ----------

-- 4.1 Recomendaciones para la app: supervisor todo; motorizado máquinas de sus órdenes activas
DROP FUNCTION IF EXISTS vendu.mis_recomendaciones();
CREATE OR REPLACE FUNCTION vendu.mis_recomendaciones()
RETURNS TABLE (id bigint, maquina_id bigint, maquina_nombre text, product_id bigint,
               seleccion text, cantidad_recomendada numeric, motivo text,
               stock_actual numeric, stock_objetivo numeric, calculado_en timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = vendu, public
AS $$
  SELECT r.id, r.maquina_id, ma.nombre, r.product_id, r.seleccion,
         r.cantidad_recomendada, r.motivo, r.stock_actual, r.stock_objetivo,
         r.calculado_en
    FROM vendu.replenishment_recommendations r
    LEFT JOIN public.maquinas ma ON ma.id = r.maquina_id
   WHERE r.cantidad_recomendada > 0
     AND (vendu.es_supervisor() OR EXISTS (
       SELECT 1 FROM vendu.replenishment_orders o
        WHERE o.maquina_id = r.maquina_id AND o.motorizado_id = auth.uid()
          AND o.estado NOT IN ('COMPLETADA','CANCELADA')))
   ORDER BY r.maquina_id, r.calculado_en DESC, r.product_id
$$;

-- 4.2 Transición de estado de una orden (el trigger valida; aquí se autoriza la pertenencia)
DROP FUNCTION IF EXISTS vendu.cambiar_estado_orden(bigint, text);
CREATE OR REPLACE FUNCTION vendu.cambiar_estado_orden(p_orden_id bigint, p_estado text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_orden vendu.replenishment_orders%ROWTYPE;
BEGIN
  SELECT * INTO v_orden FROM vendu.replenishment_orders WHERE id = p_orden_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Orden % no existe', p_orden_id;
  END IF;
  IF NOT (vendu.es_supervisor() OR v_orden.motorizado_id = auth.uid()) THEN
    RAISE EXCEPTION 'Acceso denegado a la orden %', p_orden_id;
  END IF;
  UPDATE vendu.replenishment_orders
     SET estado = p_estado,
         usuario_aprobador = COALESCE(auth.jwt()->>'email', v_orden.usuario_aprobador),
         updated_at = now()
   WHERE id = p_orden_id;
  SELECT * INTO v_orden FROM vendu.replenishment_orders WHERE id = p_orden_id;
  RETURN to_jsonb(v_orden);
END;
$$;

-- 4.3 Recarga de máquina desde la orden (idempotente por slot, RPC-only)
DROP FUNCTION IF EXISTS vendu.recargar_maquina(bigint, jsonb);
CREATE OR REPLACE FUNCTION vendu.recargar_maquina(p_orden_id bigint,
                                                  p_colocadas jsonb DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_orden vendu.replenishment_orders%ROWTYPE;
  v_motorizado uuid;
  v_origen bigint;
  v_destino bigint;
  v_linea record;
  v_slot record;
  v_target numeric;
  v_ya     numeric;
  v_cant   numeric;
  v_asig   numeric;
  v_mov_id bigint;
  v_key    text;
  v_reparto jsonb := '[]'::jsonb;
  v_colocadas jsonb := COALESCE(p_colocadas, '{}'::jsonb);
  v_sin_capacidad numeric := 0;   -- unidades que NO cupieron en ningún canal
BEGIN
  SELECT * INTO v_orden FROM vendu.replenishment_orders WHERE id = p_orden_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Orden % no existe', p_orden_id;
  END IF;
  IF NOT (vendu.es_supervisor() OR v_orden.motorizado_id = auth.uid()) THEN
    RAISE EXCEPTION 'Acceso denegado a la orden %', p_orden_id;
  END IF;

  -- Idempotencia: ya completada
  IF v_orden.estado = 'COMPLETADA' THEN
    RETURN jsonb_build_object(
      'orden_id', p_orden_id, 'estado', 'COMPLETADA', 'ya_completada', true);
  END IF;

  IF v_orden.estado <> 'EN_MAQUINA' THEN
    RAISE EXCEPTION 'La orden debe estar en EN_MAQUINA (estado actual: %)', v_orden.estado;
  END IF;
  IF v_orden.motorizado_id IS NULL THEN
    RAISE EXCEPTION 'La orden no tiene motorizado asignado';
  END IF;
  v_motorizado := v_orden.motorizado_id;

  SELECT id INTO v_origen  FROM vendu.inventory_locations
   WHERE tipo='MOTORIZADO' AND motorizado_id = v_motorizado;
  SELECT id INTO v_destino FROM vendu.inventory_locations
   WHERE tipo='MAQUINA'    AND maquina_id = v_orden.maquina_id;
  IF v_origen IS NULL OR v_destino IS NULL THEN
    RAISE EXCEPTION 'Faltan ubicaciones de inventario (origen=% destino=%)', v_origen, v_destino;
  END IF;

  FOR v_linea IN
    SELECT * FROM vendu.replenishment_order_items WHERE orden_id = p_orden_id ORDER BY id
  LOOP
    v_target := COALESCE(
      (v_colocadas->>v_linea.product_id::text)::numeric, v_linea.cantidad_llevada);
    IF v_target < 0 OR v_target > v_linea.cantidad_llevada THEN
      RAISE EXCEPTION 'Cantidad colocada inválida producto % (llevada %, colocada %)',
        v_linea.product_id, v_linea.cantidad_llevada, v_target;
    END IF;

    v_ya := COALESCE((SELECT SUM(m.cantidad) FROM vendu.inventory_movements m
                       WHERE m.orden_id = p_orden_id AND m.product_id = v_linea.product_id
                         AND m.tipo = 'RECARGA_MAQUINA'), 0);
    v_cant := round(v_target - v_ya, 2);
    IF v_cant > 0 THEN
      FOR v_slot IN
        SELECT ms.slot, ms.seleccion,
               COALESCE(ms.maximo,0) - COALESCE(ms.cantidad,0) AS cap
          FROM vendu.machine_slots ms
         WHERE ms.maquina_id = v_orden.maquina_id
           AND ms.product_id = v_linea.product_id AND ms.activo
           AND (COALESCE(ms.maximo,0) - COALESCE(ms.cantidad,0)) > 0
         ORDER BY (COALESCE(ms.maximo,0) - COALESCE(ms.cantidad,0)) DESC
      LOOP
        EXIT WHEN v_cant <= 0;
        v_asig := least(v_cant, v_slot.cap);
        v_key  := 'rec-' || p_orden_id || '-' || v_slot.slot;
        v_mov_id := NULL;
        INSERT INTO vendu.inventory_movements
          (tipo, product_id, cantidad, origen_id, destino_id, motorizado_id,
           maquina_id, orden_id, seleccion, usuario, idempotency_key)
        VALUES ('RECARGA_MAQUINA', v_linea.product_id, round(v_asig,2),
                v_origen, v_destino, v_motorizado, v_orden.maquina_id,
                p_orden_id, v_slot.seleccion, auth.jwt()->>'email', v_key)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id INTO v_mov_id;
        IF v_mov_id IS NULL THEN
          SELECT id INTO v_mov_id FROM vendu.inventory_movements
           WHERE idempotency_key = v_key;
        END IF;
        v_reparto := v_reparto || jsonb_build_object(
          'slot', v_slot.slot, 'seleccion', v_slot.seleccion,
          'product_id', v_linea.product_id, 'cantidad', round(v_asig,2),
          'movimiento_id', v_mov_id);
        v_cant := round(v_cant - v_asig, 2);
      END LOOP;
      -- Lo que no cupo en ningún canal NO se da por colocado.
      IF v_cant > 0 THEN
        v_sin_capacidad := v_sin_capacidad + v_cant;
        v_target := round(v_target - v_cant, 2);
      END IF;
    END IF;

    -- Registra lo colocado (total real) y sobrante por ítem
    UPDATE vendu.replenishment_order_items
       SET cantidad_colocada = round(v_target,2),
           sobrante = round(v_linea.cantidad_llevada - v_target, 2)
     WHERE id = v_linea.id;
  END LOOP;

  -- Antes se marcaba COMPLETADA aunque no se hubiera movido ni una unidad.
  IF v_sin_capacidad > 0 AND jsonb_array_length(v_reparto) = 0 THEN
    RAISE EXCEPTION 'No hay capacidad libre en los canales de la máquina % para colocar % unidades; la orden sigue EN_MAQUINA',
      v_orden.maquina_id, v_sin_capacidad;
  END IF;

  UPDATE vendu.replenishment_orders
     SET estado = 'COMPLETADA',
         usuario_aprobador = COALESCE(auth.jwt()->>'email', 'app'),
         updated_at = now()
   WHERE id = p_orden_id;

  RETURN jsonb_build_object('orden_id', p_orden_id, 'estado', 'COMPLETADA',
                            'reparto', v_reparto);
END;
$$;

-- 4.4 Devolución de sobrante del motorizado al principal (idempotente)
DROP FUNCTION IF EXISTS vendu.devolver_motorizado(bigint, numeric, text);
CREATE OR REPLACE FUNCTION vendu.devolver_motorizado(p_product_id bigint,
                                                     p_cantidad numeric,
                                                     p_idempotency_key text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_uid  uuid := auth.uid();
  v_key  text := COALESCE(p_idempotency_key,
                          'dev-' || substr(md5(random()::text || clock_timestamp()::text), 1, 24));
  v_origen bigint;
  v_destino bigint;
  v_mov   vendu.inventory_movements%ROWTYPE;
BEGIN
  IF v_uid IS NULL THEN
    RAISE EXCEPTION 'No autenticado';
  END IF;
  IF p_cantidad <= 0 THEN
    RAISE EXCEPTION 'La cantidad debe ser mayor que 0';
  END IF;
  SELECT id INTO v_origen FROM vendu.inventory_locations
   WHERE tipo='MOTORIZADO' AND motorizado_id = v_uid;
  IF v_origen IS NULL THEN
    RAISE EXCEPTION 'El motorizado no tiene ubicación de inventario';
  END IF;
  SELECT id INTO v_destino FROM vendu.inventory_locations WHERE tipo='PRINCIPAL';
  IF v_destino IS NULL THEN
    -- Sin destino el trigger descontaría del motorizado y no sumaría a nadie.
    RAISE EXCEPTION 'No existe la ubicación PRINCIPAL (oficina); créala antes de devolver';
  END IF;

  INSERT INTO vendu.inventory_movements
    (tipo, product_id, cantidad, origen_id, destino_id, motorizado_id,
     usuario, idempotency_key)
  VALUES ('DEVOLUCION_DE_MOTORIZADO', p_product_id, p_cantidad,
          v_origen, v_destino, v_uid, auth.jwt()->>'email', v_key)
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING * INTO v_mov;
  IF v_mov IS NULL THEN
    SELECT * INTO v_mov FROM vendu.inventory_movements WHERE idempotency_key = v_key;
  END IF;
  RETURN to_jsonb(v_mov);
END;
$$;

GRANT EXECUTE ON FUNCTION vendu.mis_recomendaciones() TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.cambiar_estado_orden(bigint, text) TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.recargar_maquina(bigint, jsonb) TO authenticated;
GRANT EXECUTE ON FUNCTION vendu.devolver_motorizado(bigint, numeric, text) TO authenticated;

-- ---------- 5. Realtime ----------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication_tables
                 WHERE pubname='supabase_realtime' AND schemaname='vendu'
                   AND tablename='replenishment_orders') THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE vendu.replenishment_orders;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_publication_tables
                 WHERE pubname='supabase_realtime' AND schemaname='vendu'
                   AND tablename='replenishment_order_items') THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE vendu.replenishment_order_items;
  END IF;
END $$;

-- ---------- 6. es_supervisor: arturoylarreta (id 18) ----------
UPDATE public.motorizados SET es_supervisor = true
 WHERE id = 18 AND auth_user_id = 'bef34ce3-e405-4e22-81e2-1339221844c5'
   AND NOT es_supervisor;

-- Fin archivo 9. Debe terminar en "Success. No rows returned".
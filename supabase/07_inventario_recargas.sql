-- =====================================================================
--  VENDU — INVENTARIO, RECARGAS Y ÓRDENES   ·   ARCHIVO 7
--  Esquema nuevo sobre Supabase para el sistema logístico integral de
--  máquinas de SNACKS (flota serie V). Idempotente (se puede repetir).
--
--  Modelo de 3 niveles de inventario:
--     INVENTARIO PRINCIPAL  (depósito)
--       → INVENTARIO DEL MOTORIZADO  (transferencia)
--         → INVENTARIO DE LA MÁQUINA  (recarga física)
--  Todo cambio de cantidad genera un movimiento (inventory_movements),
--  que es la fuente auditable de la verdad. Los balances se actualizan
--  por trigger y se pueden reconstruir desde los movimientos.
--
--  Identidad:
--    * Máquina  -> maquinas.id (bigint interno) + maquinas.epay_machine_id
--                  (id del portal ePay) + maquinas.codigo_epay (V01…V79).
--    * Producto -> products.codigo_epay (código del catálogo global ePay)
--                  + product_mappings.epay_producto_id (id interno que el
--                  planograma de ePay usa por slot). El puente es explícito:
--                  NUNCA se une por nombre (Regla #13/#34).
--    * Motorizado -> motorizados.auth_user_id (uuid) = auth.uid() de la app.
-- =====================================================================

-- =====================================================================
--  0. COLUMNAS NUEVAS sobre tablas existentes
-- =====================================================================
ALTER TABLE public.maquinas
  ADD COLUMN IF NOT EXISTS epay_machine_id bigint,
  ADD COLUMN IF NOT EXISTS epay_uid        text,
  ADD COLUMN IF NOT EXISTS tipo            text NOT NULL DEFAULT 'SNACK',
  ADD COLUMN IF NOT EXISTS ubicacion       text;

CREATE UNIQUE INDEX IF NOT EXISTS maquinas_epay_machine_id_uq
  ON public.maquinas (epay_machine_id)
  WHERE epay_machine_id IS NOT NULL;

-- Marca de supervisor para RLS (la app/dashboard la fija a mano).
ALTER TABLE public.motorizados
  ADD COLUMN IF NOT EXISTS es_supervisor boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS motorizados_auth_user_id_uq
  ON public.motorizados (auth_user_id)
  WHERE auth_user_id IS NOT NULL;


-- =====================================================================
--  1. CATÁLOGO DE PRODUCTOS
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.products (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  codigo_epay  text UNIQUE,                     -- 'Codigo' del catálogo global ePay (ej. '148')
  nombre       text NOT NULL,
  nombre_norm  text NOT NULL UNIQUE,            -- nombre normalizado (lower/trim) para emparejar
  categoria    text,
  precio_bs    numeric(14,2),
  precio_usd   numeric(14,2),
  activo       boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
--  2. MAPEO DE PRODUCTOS (planograma producto_id ↔ products)
--  Si llega un producto ePay no mapeado NO se descarta: queda como alerta
--  y el supervisor lo mapea (Regla #34).
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.product_mappings (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  epay_producto_id text NOT NULL UNIQUE,        -- 'producto_id' del planograma (ej. '2708')
  product_id       bigint NOT NULL REFERENCES public.products(id),
  epay_nombre      text,                        -- nombre tal cual vino del planograma (auditoría)
  mapped_by        text NOT NULL DEFAULT 'auto', -- auto | manual
  active           boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- Productos del planograma sin mapear → alerta explícita para el supervisor.
CREATE TABLE IF NOT EXISTS public.unmapped_products (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  epay_producto_id text NOT NULL,
  nombre           text,
  maquina_id       bigint,                      -- dónde se detectó
  seleccion        text,
  visto_en         timestamptz NOT NULL DEFAULT now(),
  resuelto         boolean NOT NULL DEFAULT false
);


-- =====================================================================
--  3. INVENTARIO DE LA MÁQUINA (planograma por slot, sincronizado de ePay)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.machine_slots (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id       bigint NOT NULL REFERENCES public.maquinas(id) ON DELETE CASCADE,
  seleccion        text NOT NULL,               -- tecla de selección (ej. '0a00')
  slot             text,                        -- etiqueta física (ej. 'A0')
  epay_producto_id text,                        -- id interno ePay del producto en el slot
  product_id       bigint REFERENCES public.products(id),
  cantidad         numeric NOT NULL DEFAULT 0,  -- stock actual en el canal
  minimo           numeric NOT NULL DEFAULT 0,
  maximo           numeric NOT NULL DEFAULT 0,
  activo           boolean NOT NULL DEFAULT true,
  estado           text,                        -- ok | vacio | bajo | negativo | inactivo
  synced_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (maquina_id, seleccion)
);

CREATE INDEX IF NOT EXISTS machine_slots_maquina_idx ON public.machine_slots (maquina_id);
CREATE INDEX IF NOT EXISTS machine_slots_product_idx ON public.machine_slots (product_id);


-- =====================================================================
--  4. UBICACIONES DE INVENTARIO (los 3 niveles) + BALANCES
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.inventory_locations (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tipo          text NOT NULL CHECK (tipo IN ('PRINCIPAL','MOTORIZADO','MAQUINA')),
  motorizado_id uuid REFERENCES public.motorizados(auth_user_id) ON DELETE CASCADE,
  maquina_id    bigint REFERENCES public.maquinas(id) ON DELETE CASCADE,
  UNIQUE (tipo, motorizado_id, maquina_id)
);

CREATE TABLE IF NOT EXISTS public.inventory_balances (
  location_id bigint NOT NULL REFERENCES public.inventory_locations(id) ON DELETE CASCADE,
  product_id  bigint NOT NULL REFERENCES public.products(id),
  quantity    numeric NOT NULL DEFAULT 0 CHECK (quantity >= 0),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (location_id, product_id)
);

CREATE INDEX IF NOT EXISTS inventory_balances_product_idx ON public.inventory_balances (product_id);


-- =====================================================================
--  5. ÓRDENES DE RECARGA (entidad de negocio real)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.replenishment_orders (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id       bigint NOT NULL REFERENCES public.maquinas(id),
  motorizado_id    uuid REFERENCES public.motorizados(auth_user_id),
  estado           text NOT NULL DEFAULT 'BORRADOR',
  usuario_aprobador text,
  observaciones    text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.replenishment_order_items (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  orden_id           bigint NOT NULL REFERENCES public.replenishment_orders(id) ON DELETE CASCADE,
  product_id         bigint NOT NULL REFERENCES public.products(id),
  seleccion          text,                     -- slot de la máquina
  cantidad_ordenada  numeric NOT NULL DEFAULT 0,
  cantidad_preparada numeric NOT NULL DEFAULT 0,
  cantidad_entregada numeric NOT NULL DEFAULT 0,
  cantidad_llevada   numeric NOT NULL DEFAULT 0,
  cantidad_colocada  numeric NOT NULL DEFAULT 0,
  sobrante           numeric NOT NULL DEFAULT 0,
  observaciones      text,
  UNIQUE (orden_id, product_id, seleccion)
);

CREATE INDEX IF NOT EXISTS replenishment_orders_motorizado_idx
  ON public.replenishment_orders (motorizado_id);
CREATE INDEX IF NOT EXISTS replenishment_orders_estado_idx
  ON public.replenishment_orders (estado);


-- =====================================================================
--  6. MOVIMIENTOS DE INVENTARIO (la fuente auditable de la verdad)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.inventory_movements (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tipo           text NOT NULL,
  product_id     bigint NOT NULL REFERENCES public.products(id),
  cantidad       numeric NOT NULL CHECK (cantidad <> 0),
  origen_id      bigint REFERENCES public.inventory_locations(id),
  destino_id     bigint REFERENCES public.inventory_locations(id),
  motorizado_id  uuid REFERENCES public.motorizados(auth_user_id),
  maquina_id     bigint REFERENCES public.maquinas(id),
  orden_id       bigint REFERENCES public.replenishment_orders(id),
  seleccion      text,                          -- slot, cuando corresponda
  usuario        text,                          -- quién (auth uid o nombre)
  observacion    text,
  idempotency_key text NOT NULL UNIQUE,         -- evita duplicados (offline/reintentos)
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS inventory_movements_product_idx
  ON public.inventory_movements (product_id);
CREATE INDEX IF NOT EXISTS inventory_movements_orden_idx
  ON public.inventory_movements (orden_id);
CREATE INDEX IF NOT EXISTS inventory_movements_motorizado_idx
  ON public.inventory_movements (motorizado_id);


-- =====================================================================
--  7. RECOMENDACIONES DE RECARGA (explicables, deterministas)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.replenishment_recommendations (
  id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id                bigint NOT NULL REFERENCES public.maquinas(id) ON DELETE CASCADE,
  product_id                bigint NOT NULL REFERENCES public.products(id),
  seleccion                 text,
  consumo_promedio_diario   numeric,
  consumo_promedio_7_dias   numeric,
  consumo_promedio_14_dias  numeric,
  consumo_promedio_30_dias  numeric,
  tendencia                 numeric,           -- % de cambio 14d vs 7d
  stock_actual              numeric,
  dias_cobertura            numeric,
  stock_objetivo            numeric,
  margen_seguridad          numeric,
  cantidad_recomendada      numeric NOT NULL DEFAULT 0,
  motivo                    text,
  calculado_en              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recommendations_maquina_idx
  ON public.replenishment_recommendations (maquina_id, calculado_en DESC);


-- =====================================================================
--  8. VENTAS SINCRONIZADAS DE EPAY (para el motor de demanda)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.epay_sales (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id  bigint NOT NULL REFERENCES public.maquinas(id) ON DELETE CASCADE,
  fecha       date NOT NULL,
  seleccion   text,                            -- tecla de selección vendida
  product_id  bigint REFERENCES public.products(id),
  cantidad    numeric NOT NULL DEFAULT 0,
  monto_bs    numeric(14,2),
  monto_usd   numeric(14,2),
  UNIQUE (maquina_id, fecha, seleccion)
);

CREATE INDEX IF NOT EXISTS epay_sales_maquina_fecha_idx
  ON public.epay_sales (maquina_id, fecha);


-- =====================================================================
--  9. CONCILIACIÓN EPAY vs VENDU
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.inventory_reconciliation (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id         bigint NOT NULL REFERENCES public.maquinas(id) ON DELETE CASCADE,
  product_id         bigint NOT NULL REFERENCES public.products(id),
  inventario_vendu   numeric,                  -- ledger interno
  inventario_epay    numeric,                  -- último planograma ePay
  inventario_esperado numeric,                 -- inicial + recargas - ventas - mermas
  ventas_periodo     numeric,
  diferencia         numeric,
  estado             text NOT NULL DEFAULT 'PENDIENTE_REVISION',
  calculado_en       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reconciliation_maquina_idx
  ON public.inventory_reconciliation (maquina_id, calculado_en DESC);


-- =====================================================================
--  10. BITÁCORA DE SINCRONIZACIÓN (ePay) Y AUDITORÍA
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.sync_log (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fuente     text NOT NULL,                    -- planograma | catalogo | ventas | estatus | mapeo
  estado     text NOT NULL,                    -- OK | ERROR
  detalle    text,
  ultima_sync timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.audit_logs (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  usuario     text,
  accion      text NOT NULL,
  entidad     text,                            -- tabla/entidad afectada
  entidad_id  text,
  antes       jsonb,
  despues     jsonb,
  origen      text,
  destino     text,
  referencia  text,
  maquina_id  bigint,
  product_id  bigint,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_logs_maquina_idx ON public.audit_logs (maquina_id);
CREATE INDEX IF NOT EXISTS audit_logs_created_idx ON public.audit_logs (created_at DESC);

CREATE TABLE IF NOT EXISTS public.order_status_log (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  orden_id      bigint NOT NULL REFERENCES public.replenishment_orders(id) ON DELETE CASCADE,
  estado_desde  text,
  estado_hasta  text NOT NULL,
  usuario       text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
--  11. FUNCIÓN AUXILIAR: ¿el usuario actual es supervisor?
--  SECURITY DEFINER para que el rol authenticated pueda consultarlo en RLS.
-- =====================================================================
CREATE OR REPLACE FUNCTION public.es_supervisor()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.motorizados m
    WHERE m.auth_user_id = auth.uid() AND m.es_supervisor = true
  );
$$;

-- =====================================================================
--  12. TRIGGER DE MOVIMIENTOS: actualiza balances y audita (atómico)
--  Aplica el efecto del movimiento en inventory_balances:
--    * origen  -= cantidad
--    * destino += cantidad
--  Prohíbe inventario negativo (Regla de oro: no se puede quedar en negativo).
--  Escribe la fila de auditoría (quién/qué/cuándo/antes/después).
-- =====================================================================
CREATE OR REPLACE FUNCTION public.fn_apply_inventory_movement()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_antes numeric;
BEGIN
  IF pg_trigger_depth() > 1 THEN
    RETURN NEW;
  END IF;

  -- Origen: descuenta (salida)
  IF NEW.origen_id IS NOT NULL THEN
    SELECT quantity INTO v_antes FROM public.inventory_balances
     WHERE location_id = NEW.origen_id AND product_id = NEW.product_id;
    v_antes := COALESCE(v_antes, 0);
    IF v_antes - NEW.cantidad < 0 THEN
      RAISE EXCEPTION 'Inventario negativo en origen (location_id=%, producto=%): disponible=%, salida=%',
        NEW.origen_id, NEW.product_id, v_antes, NEW.cantidad;
    END IF;
    INSERT INTO public.inventory_balances (location_id, product_id, quantity, updated_at)
    VALUES (NEW.origen_id, NEW.product_id, v_antes - NEW.cantidad, now())
    ON CONFLICT (location_id, product_id)
    DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = now();
  END IF;

  -- Destino: suma (entrada)
  IF NEW.destino_id IS NOT NULL THEN
    SELECT quantity INTO v_antes FROM public.inventory_balances
     WHERE location_id = NEW.destino_id AND product_id = NEW.product_id;
    v_antes := COALESCE(v_antes, 0);
    INSERT INTO public.inventory_balances (location_id, product_id, quantity, updated_at)
    VALUES (NEW.destino_id, NEW.product_id, v_antes + NEW.cantidad, now())
    ON CONFLICT (location_id, product_id)
    DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = now();
  END IF;

  INSERT INTO public.audit_logs (usuario, accion, entidad, entidad_id, antes, despues,
                                 origen, destino, referencia, maquina_id, product_id)
  VALUES (
    NEW.usuario, NEW.tipo, 'inventory_movements', NEW.id::text,
    NULL,
    jsonb_build_object('cantidad', NEW.cantidad, 'producto', NEW.product_id),
    (SELECT tipo::text || ':' || id::text FROM public.inventory_locations WHERE id = NEW.origen_id),
    (SELECT tipo::text || ':' || id::text FROM public.inventory_locations WHERE id = NEW.destino_id),
    NEW.orden_id::text, NEW.maquina_id, NEW.product_id
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_apply_inventory_movement ON public.inventory_movements;
CREATE TRIGGER trg_apply_inventory_movement
AFTER INSERT ON public.inventory_movements
FOR EACH ROW EXECUTE FUNCTION public.fn_apply_inventory_movement();


-- =====================================================================
--  13. TRIGGER DE ESTADOS DE ÓRDEN: transiciones válidas + bitácora
-- =====================================================================
CREATE OR REPLACE FUNCTION public.fn_validate_order_transition()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_desde text := OLD.estado;
  v_hasta text := NEW.estado;
  v_valido boolean := false;
BEGIN
  IF pg_trigger_depth() > 1 THEN
    RETURN NEW;
  END IF;

  -- Transiciones permitidas (solo hacia adelante; CANCELADA desde casi todo)
  v_valido := (v_desde = v_hasta)
    OR (v_desde = 'BORRADOR'               AND v_hasta IN ('PROPUESTA','CANCELADA'))
    OR (v_desde = 'PROPUESTA'              AND v_hasta IN ('APROBADA','CANCELADA'))
    OR (v_desde = 'APROBADA'               AND v_hasta IN ('PREPARANDO','CANCELADA'))
    OR (v_desde = 'PREPARANDO'             AND v_hasta IN ('ENTREGADA_AL_MOTORIZADO','CANCELADA'))
    OR (v_desde = 'ENTREGADA_AL_MOTORIZADO' AND v_hasta IN ('EN_RUTA','CANCELADA'))
    OR (v_desde = 'EN_RUTA'                AND v_hasta IN ('EN_MAQUINA','CANCELADA'))
    OR (v_desde = 'EN_MAQUINA'             AND v_hasta IN ('COMPLETADA','CANCELADA'));

  IF NOT v_valido THEN
    RAISE EXCEPTION 'Transición de orden inválida: % → %', v_desde, v_hasta;
  END IF;

  IF v_desde IS DISTINCT FROM v_hasta THEN
    INSERT INTO public.order_status_log (orden_id, estado_desde, estado_hasta, usuario)
    VALUES (NEW.id, v_desde, v_hasta, coalesce(NEW.usuario_aprobador, 'sistema'));
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_order_transition ON public.replenishment_orders;
CREATE TRIGGER trg_validate_order_transition
BEFORE UPDATE OF estado ON public.replenishment_orders
FOR EACH ROW EXECUTE FUNCTION public.fn_validate_order_transition();


-- =====================================================================
--  14. RLS  (los motorizados solo ven lo suyo; el supervisor ve todo)
-- =====================================================================
ALTER TABLE public.products                       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.product_mappings               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.unmapped_products              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.machine_slots                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inventory_locations            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inventory_balances             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inventory_movements            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.replenishment_orders           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.replenishment_order_items      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.replenishment_recommendations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.epay_sales                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inventory_reconciliation       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_log                       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_logs                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.order_status_log               ENABLE ROW LEVEL SECURITY;

-- --- Catálogo, mappings, slots, recomendaciones: lectura para cualquier autenticado ---
DROP POLICY IF EXISTS prod_select ON public.products;
CREATE POLICY prod_select ON public.products FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS pm_select ON public.product_mappings;
CREATE POLICY pm_select ON public.product_mappings FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS ms_select ON public.machine_slots;
CREATE POLICY ms_select ON public.machine_slots FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS rec_select ON public.replenishment_recommendations;
CREATE POLICY rec_select ON public.replenishment_recommendations
  FOR SELECT TO authenticated USING (public.es_supervisor() OR EXISTS (
    SELECT 1 FROM public.replenishment_orders o
    WHERE o.maquina_id = replenishment_recommendations.maquina_id
      AND o.motorizado_id = auth.uid()
  ));

-- --- Órdenes: cada motorizado ve/edita solo las suyas; supervisor ve todo ---
DROP POLICY IF EXISTS ro_select ON public.replenishment_orders;
CREATE POLICY ro_select ON public.replenishment_orders FOR SELECT TO authenticated
  USING (public.es_supervisor() OR motorizado_id = auth.uid());

DROP POLICY IF EXISTS ro_update ON public.replenishment_orders;
CREATE POLICY ro_update ON public.replenishment_orders FOR UPDATE TO authenticated
  USING (public.es_supervisor() OR motorizado_id = auth.uid())
  WITH CHECK (public.es_supervisor() OR motorizado_id = auth.uid());

DROP POLICY IF EXISTS ro_insert ON public.replenishment_orders;
CREATE POLICY ro_insert ON public.replenishment_orders FOR INSERT TO authenticated
  WITH CHECK (public.es_supervisor());

-- --- Ítems de orden: a través de la orden del motorizado ---
DROP POLICY IF EXISTS roi_select ON public.replenishment_order_items;
CREATE POLICY roi_select ON public.replenishment_order_items FOR SELECT TO authenticated
  USING (public.es_supervisor() OR EXISTS (
    SELECT 1 FROM public.replenishment_orders o
    WHERE o.id = orden_id AND o.motorizado_id = auth.uid()
  ));
DROP POLICY IF EXISTS roi_update ON public.replenishment_order_items;
CREATE POLICY roi_update ON public.replenishment_order_items FOR UPDATE TO authenticated
  USING (public.es_supervisor() OR EXISTS (
    SELECT 1 FROM public.replenishment_orders o
    WHERE o.id = orden_id AND o.motorizado_id = auth.uid()
  ))
  WITH CHECK (public.es_supervisor() OR EXISTS (
    SELECT 1 FROM public.replenishment_orders o
    WHERE o.id = orden_id AND o.motorizado_id = auth.uid()
  ));

-- --- Movimientos: el motorizado crea/ve los suyos; supervisor ve todo ---
DROP POLICY IF EXISTS im_select ON public.inventory_movements;
CREATE POLICY im_select ON public.inventory_movements FOR SELECT TO authenticated
  USING (public.es_supervisor() OR motorizado_id = auth.uid());
DROP POLICY IF EXISTS im_insert ON public.inventory_movements;
CREATE POLICY im_insert ON public.inventory_movements FOR INSERT TO authenticated
  WITH CHECK (public.es_supervisor() OR motorizado_id = auth.uid());

-- --- Balances: el motorizado solo ve su ubicación de inventario ---
DROP POLICY IF EXISTS ib_select ON public.inventory_balances;
CREATE POLICY ib_select ON public.inventory_balances FOR SELECT TO authenticated
  USING (public.es_supervisor() OR EXISTS (
    SELECT 1 FROM public.inventory_locations l
    WHERE l.id = location_id AND l.tipo = 'MOTORIZADO' AND l.motorizado_id = auth.uid()
  ));

-- --- Ubicaciones de inventario: el motorizado ve la suya ---
DROP POLICY IF EXISTS il_select ON public.inventory_locations;
CREATE POLICY il_select ON public.inventory_locations FOR SELECT TO authenticated
  USING (public.es_supervisor() OR motorizado_id = auth.uid() OR tipo = 'MAQUINA');

-- --- Ventas, conciliación, sync, auditoría: solo supervisor (datos sensibles) ---
DROP POLICY IF EXISTS es_select ON public.epay_sales;
CREATE POLICY es_select ON public.epay_sales FOR SELECT TO authenticated
  USING (public.es_supervisor());
DROP POLICY IF EXISTS ir_select ON public.inventory_reconciliation;
CREATE POLICY ir_select ON public.inventory_reconciliation FOR SELECT TO authenticated
  USING (public.es_supervisor());
DROP POLICY IF EXISTS sl_select ON public.sync_log;
CREATE POLICY sl_select ON public.sync_log FOR SELECT TO authenticated
  USING (public.es_supervisor());
DROP POLICY IF EXISTS al_select ON public.audit_logs;
CREATE POLICY al_select ON public.audit_logs FOR SELECT TO authenticated
  USING (public.es_supervisor());

-- --- Productos sin mapear: solo supervisor los gestiona ---
DROP POLICY IF EXISTS up_select ON public.unmapped_products;
CREATE POLICY up_select ON public.unmapped_products FOR SELECT TO authenticated
  USING (public.es_supervisor());
DROP POLICY IF EXISTS up_insert ON public.unmapped_products;
CREATE POLICY up_insert ON public.unmapped_products FOR INSERT TO authenticated
  WITH CHECK (public.es_supervisor());


-- =====================================================================
--  15. REALTIME: la app recibe en vivo nuevas órdenes e ítems
-- =====================================================================
DO $$
BEGIN
  ALTER PUBLICATION supabase_realtime ADD TABLE public.replenishment_orders;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$
BEGIN
  ALTER PUBLICATION supabase_realtime ADD TABLE public.replenishment_order_items;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =====================================================================
--  16. GRANTS PARA EL BACKEND (service_role)
--  El dashboard escribe/lee con la service_role key (rol `service_role`,
--  que SÍ pasa la RLS). La app móvil usa `authenticated`. Con esto el
--  servicio puede leer motorizados/rutas_paradas y operar las tablas nuevas.
-- =====================================================================
GRANT USAGE ON SCHEMA public TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- Fin archivo 7. Debe terminar en "Success. No rows returned".

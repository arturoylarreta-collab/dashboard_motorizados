-- =====================================================================
--  VENDU — SCHEMA AISLADO `vendu`   ·   ARCHIVO 8
--  Todo el sistema de inventario/recargas/órdenes vive en un schema
--  separado para NO tocar `public` (que ya usan los empleados).
--  Cuando esté validado, se promueve `vendu` → `public`.
--
--  Referencias (solo lectura) a:
--    public.maquinas(id bigint)
--    public.motorizados(auth_user_id uuid)   -- identidad del motorizado
--
--  Idempotente (se puede repetir).
-- =====================================================================

-- ---------- 0. LIMPIEZA del schema `public` (tablas vacías que creó 07) ----------
DROP TABLE IF EXISTS public.order_status_log            CASCADE;
DROP TABLE IF EXISTS public.audit_logs                  CASCADE;
DROP TABLE IF EXISTS public.sync_log                    CASCADE;
DROP TABLE IF EXISTS public.inventory_reconciliation    CASCADE;
DROP TABLE IF EXISTS public.epay_sales                  CASCADE;
DROP TABLE IF EXISTS public.replenishment_recommendations CASCADE;
DROP TABLE IF EXISTS public.inventory_movements         CASCADE;
DROP TABLE IF EXISTS public.replenishment_order_items   CASCADE;
DROP TABLE IF EXISTS public.replenishment_orders        CASCADE;
DROP TABLE IF EXISTS public.inventory_balances          CASCADE;
DROP TABLE IF EXISTS public.inventory_locations         CASCADE;
DROP TABLE IF EXISTS public.machine_slots               CASCADE;
DROP TABLE IF EXISTS public.unmapped_products           CASCADE;
DROP TABLE IF EXISTS public.product_mappings            CASCADE;
DROP TABLE IF EXISTS public.products                    CASCADE;

DROP FUNCTION IF EXISTS public.fn_apply_inventory_movement() CASCADE;
DROP FUNCTION IF EXISTS public.fn_validate_order_transition() CASCADE;
DROP FUNCTION IF EXISTS public.es_supervisor() CASCADE;

-- ---------- 1. SCHEMA vendu ----------
CREATE SCHEMA IF NOT EXISTS vendu;

-- ---------- 2. MAPEO DE MÁQUINAS (ePay ↔ public.maquinas) ----------
CREATE TABLE IF NOT EXISTS vendu.maquinas_epay (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id      bigint NOT NULL UNIQUE REFERENCES public.maquinas(id) ON DELETE CASCADE,
  epay_machine_id bigint UNIQUE,
  epay_uid        text,
  codigo          text,
  synced_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------- 3. CATÁLOGO DE PRODUCTOS ----------
CREATE TABLE IF NOT EXISTS vendu.products (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  codigo_epay  text UNIQUE,
  nombre       text NOT NULL,
  nombre_norm  text NOT NULL UNIQUE,
  categoria    text,
  precio_bs    numeric(14,2),
  precio_usd   numeric(14,2),
  activo       boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------- 4. MAPEO DE PRODUCTOS (planograma producto_id ↔ products) ----------
CREATE TABLE IF NOT EXISTS vendu.product_mappings (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  epay_producto_id text NOT NULL UNIQUE,
  product_id       bigint NOT NULL REFERENCES vendu.products(id),
  epay_nombre      text,
  mapped_by        text NOT NULL DEFAULT 'auto',
  active           boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vendu.unmapped_products (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  epay_producto_id text NOT NULL,
  nombre           text,
  maquina_id       bigint,
  seleccion        text,
  visto_en         timestamptz NOT NULL DEFAULT now(),
  resuelto         boolean NOT NULL DEFAULT false
);

-- ---------- 5. INVENTARIO DE MÁQUINA (planograma por slot) ----------
CREATE TABLE IF NOT EXISTS vendu.machine_slots (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id       bigint NOT NULL REFERENCES public.maquinas(id) ON DELETE CASCADE,
  seleccion        text NOT NULL,
  slot             text,
  epay_producto_id text,
  product_id       bigint REFERENCES vendu.products(id),
  cantidad         numeric NOT NULL DEFAULT 0,
  minimo           numeric NOT NULL DEFAULT 0,
  maximo           numeric NOT NULL DEFAULT 0,
  activo           boolean NOT NULL DEFAULT true,
  estado           text,
  synced_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (maquina_id, slot)
);
CREATE INDEX IF NOT EXISTS vendu_machine_slots_maquina_idx ON vendu.machine_slots (maquina_id);
CREATE INDEX IF NOT EXISTS vendu_machine_slots_product_idx ON vendu.machine_slots (product_id);

-- ---------- 6. UBICACIONES + BALANCES DE INVENTARIO ----------
CREATE TABLE IF NOT EXISTS vendu.inventory_locations (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tipo          text NOT NULL CHECK (tipo IN ('PRINCIPAL','MOTORIZADO','MAQUINA')),
  motorizado_id uuid REFERENCES public.motorizados(auth_user_id) ON DELETE CASCADE,
  maquina_id    bigint REFERENCES public.maquinas(id) ON DELETE CASCADE,
  UNIQUE (tipo, motorizado_id, maquina_id)
);

CREATE TABLE IF NOT EXISTS vendu.inventory_balances (
  location_id bigint NOT NULL REFERENCES vendu.inventory_locations(id) ON DELETE CASCADE,
  product_id  bigint NOT NULL REFERENCES vendu.products(id),
  quantity    numeric NOT NULL DEFAULT 0 CHECK (quantity >= 0),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (location_id, product_id)
);
CREATE INDEX IF NOT EXISTS vendu_balances_product_idx ON vendu.inventory_balances (product_id);

-- ---------- 7. ÓRDENES DE RECARGA ----------
CREATE TABLE IF NOT EXISTS vendu.replenishment_orders (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id        bigint NOT NULL REFERENCES public.maquinas(id),
  motorizado_id     uuid REFERENCES public.motorizados(auth_user_id),
  estado            text NOT NULL DEFAULT 'BORRADOR',
  usuario_aprobador text,
  observaciones     text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vendu.replenishment_order_items (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  orden_id           bigint NOT NULL REFERENCES vendu.replenishment_orders(id) ON DELETE CASCADE,
  product_id         bigint NOT NULL REFERENCES vendu.products(id),
  seleccion          text,
  cantidad_ordenada  numeric NOT NULL DEFAULT 0,
  cantidad_preparada numeric NOT NULL DEFAULT 0,
  cantidad_entregada numeric NOT NULL DEFAULT 0,
  cantidad_llevada   numeric NOT NULL DEFAULT 0,
  cantidad_colocada  numeric NOT NULL DEFAULT 0,
  sobrante           numeric NOT NULL DEFAULT 0,
  observaciones      text,
  UNIQUE (orden_id, product_id, seleccion)
);
CREATE INDEX IF NOT EXISTS vendu_orders_motorizado_idx ON vendu.replenishment_orders (motorizado_id);
CREATE INDEX IF NOT EXISTS vendu_orders_estado_idx ON vendu.replenishment_orders (estado);

-- ---------- 8. MOVIMIENTOS DE INVENTARIO ----------
CREATE TABLE IF NOT EXISTS vendu.inventory_movements (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tipo            text NOT NULL,
  product_id      bigint NOT NULL REFERENCES vendu.products(id),
  cantidad        numeric NOT NULL CHECK (cantidad <> 0),
  origen_id       bigint REFERENCES vendu.inventory_locations(id),
  destino_id      bigint REFERENCES vendu.inventory_locations(id),
  motorizado_id   uuid REFERENCES public.motorizados(auth_user_id),
  maquina_id      bigint REFERENCES public.maquinas(id),
  orden_id        bigint REFERENCES vendu.replenishment_orders(id),
  seleccion       text,
  usuario         text,
  observacion     text,
  idempotency_key text NOT NULL UNIQUE,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vendu_movements_product_idx ON vendu.inventory_movements (product_id);
CREATE INDEX IF NOT EXISTS vendu_movements_orden_idx ON vendu.inventory_movements (orden_id);
CREATE INDEX IF NOT EXISTS vendu_movements_motorizado_idx ON vendu.inventory_movements (motorizado_id);

-- ---------- 9. RECOMENDACIONES ----------
CREATE TABLE IF NOT EXISTS vendu.replenishment_recommendations (
  id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id                bigint NOT NULL REFERENCES public.maquinas(id) ON DELETE CASCADE,
  product_id                bigint NOT NULL REFERENCES vendu.products(id),
  seleccion                 text,
  consumo_promedio_diario   numeric,
  consumo_promedio_7_dias   numeric,
  consumo_promedio_14_dias  numeric,
  consumo_promedio_30_dias  numeric,
  tendencia                 numeric,
  stock_actual              numeric,
  dias_cobertura            numeric,
  stock_objetivo            numeric,
  margen_seguridad          numeric,
  cantidad_recomendada      numeric NOT NULL DEFAULT 0,
  motivo                    text,
  calculado_en              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vendu_recommendations_maquina_idx ON vendu.replenishment_recommendations (maquina_id, calculado_en DESC);

-- ---------- 10. VENTAS SINCRONIZADAS ----------
CREATE TABLE IF NOT EXISTS vendu.epay_sales (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id    bigint NOT NULL REFERENCES public.maquinas(id) ON DELETE CASCADE,
  periodo_desde date,          -- inicio del rango agregado (para consumo/día)
  fecha         date NOT NULL, -- fin del rango agregado
  seleccion     text,
  product_id    bigint REFERENCES vendu.products(id),
  cantidad      numeric NOT NULL DEFAULT 0,
  monto_bs      numeric(14,2),
  monto_usd     numeric(14,2),
  UNIQUE (maquina_id, fecha, seleccion)
);
CREATE INDEX IF NOT EXISTS vendu_epay_sales_maquina_fecha_idx ON vendu.epay_sales (maquina_id, fecha);

-- ---------- 11. CONCILIACIÓN ----------
CREATE TABLE IF NOT EXISTS vendu.inventory_reconciliation (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  maquina_id           bigint NOT NULL REFERENCES public.maquinas(id) ON DELETE CASCADE,
  product_id           bigint NOT NULL REFERENCES vendu.products(id),
  inventario_vendu     numeric,
  inventario_epay      numeric,
  inventario_esperado  numeric,
  ventas_periodo       numeric,
  diferencia           numeric,
  estado               text NOT NULL DEFAULT 'PENDIENTE_REVISION',
  calculado_en         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vendu_reconciliation_maquina_idx ON vendu.inventory_reconciliation (maquina_id, calculado_en DESC);

-- ---------- 12. BITÁCORA + AUDITORÍA ----------
CREATE TABLE IF NOT EXISTS vendu.sync_log (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fuente      text NOT NULL,
  estado      text NOT NULL,
  detalle     text,
  ultima_sync timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vendu.audit_logs (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  usuario     text,
  accion      text NOT NULL,
  entidad     text,
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
CREATE INDEX IF NOT EXISTS vendu_audit_maquina_idx ON vendu.audit_logs (maquina_id);
CREATE INDEX IF NOT EXISTS vendu_audit_created_idx ON vendu.audit_logs (created_at DESC);

CREATE TABLE IF NOT EXISTS vendu.order_status_log (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  orden_id      bigint NOT NULL REFERENCES vendu.replenishment_orders(id) ON DELETE CASCADE,
  estado_desde  text,
  estado_hasta  text NOT NULL,
  usuario       text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
--  TRIGGERS
-- =====================================================================

-- Movimientos → balances + auditoría (atómico, sin inventario negativo)
CREATE OR REPLACE FUNCTION vendu.fn_apply_inventory_movement()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_antes numeric;
BEGIN
  IF pg_trigger_depth() > 1 THEN
    RETURN NEW;
  END IF;

  IF NEW.origen_id IS NOT NULL THEN
    SELECT quantity INTO v_antes FROM vendu.inventory_balances
     WHERE location_id = NEW.origen_id AND product_id = NEW.product_id;
    v_antes := COALESCE(v_antes, 0);
    IF v_antes - NEW.cantidad < 0 THEN
      RAISE EXCEPTION 'Inventario negativo en origen (location_id=%, producto=%): disponible=%, salida=%',
        NEW.origen_id, NEW.product_id, v_antes, NEW.cantidad;
    END IF;
    INSERT INTO vendu.inventory_balances (location_id, product_id, quantity, updated_at)
    VALUES (NEW.origen_id, NEW.product_id, v_antes - NEW.cantidad, now())
    ON CONFLICT (location_id, product_id)
    DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = now();
  END IF;

  IF NEW.destino_id IS NOT NULL THEN
    SELECT quantity INTO v_antes FROM vendu.inventory_balances
     WHERE location_id = NEW.destino_id AND product_id = NEW.product_id;
    v_antes := COALESCE(v_antes, 0);
    INSERT INTO vendu.inventory_balances (location_id, product_id, quantity, updated_at)
    VALUES (NEW.destino_id, NEW.product_id, v_antes + NEW.cantidad, now())
    ON CONFLICT (location_id, product_id)
    DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = now();
  END IF;

  INSERT INTO vendu.audit_logs (usuario, accion, entidad, entidad_id, despues, origen, destino, referencia, maquina_id, product_id)
  VALUES (
    NEW.usuario, NEW.tipo, 'inventory_movements', NEW.id::text,
    jsonb_build_object('cantidad', NEW.cantidad, 'producto', NEW.product_id),
    (SELECT tipo::text || ':' || id::text FROM vendu.inventory_locations WHERE id = NEW.origen_id),
    (SELECT tipo::text || ':' || id::text FROM vendu.inventory_locations WHERE id = NEW.destino_id),
    NEW.orden_id::text, NEW.maquina_id, NEW.product_id
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_apply_inventory_movement ON vendu.inventory_movements;
CREATE TRIGGER trg_apply_inventory_movement
AFTER INSERT ON vendu.inventory_movements
FOR EACH ROW EXECUTE FUNCTION vendu.fn_apply_inventory_movement();

-- Transiciones de estado de órdenes
CREATE OR REPLACE FUNCTION vendu.fn_validate_order_transition()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = vendu, public
AS $$
DECLARE
  v_desde text := OLD.estado;
  v_hasta text := NEW.estado;
  v_valido boolean := false;
BEGIN
  IF pg_trigger_depth() > 1 THEN
    RETURN NEW;
  END IF;
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
    INSERT INTO vendu.order_status_log (orden_id, estado_desde, estado_hasta, usuario)
    VALUES (NEW.id, v_desde, v_hasta, coalesce(NEW.usuario_aprobador, 'sistema'));
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_order_transition ON vendu.replenishment_orders;
CREATE TRIGGER trg_validate_order_transition
BEFORE UPDATE OF estado ON vendu.replenishment_orders
FOR EACH ROW EXECUTE FUNCTION vendu.fn_validate_order_transition();

-- =====================================================================
--  GRANTS (el backend usa service_role / postgres; la app `authenticated` se agrega en la fase Flutter)
-- =====================================================================
GRANT USAGE ON SCHEMA vendu TO service_role, postgres;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA vendu TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vendu TO service_role;

-- Fin archivo 8. Debe terminar en "Success. No rows returned".

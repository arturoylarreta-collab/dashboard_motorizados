-- =====================================================================
--  VENDU — FASE 2: registro propio de lo escrito en ePay   ·   ARCHIVO 11
--  (17-09-2026) Idempotente. Correr como `postgres` después de 10.
--
--  El MCP de ePay guarda su propio log de recargas, pero Render gratis no
--  tiene disco: ese log y su idempotencia se pierden en cada redespliegue.
--  Por eso Vendu guarda aquí cada escritura en ePay (recargas de máquina y
--  ajustes del almacén), con el `lote` que devuelve el MCP para poder
--  revertir, y una cola de pendientes para cuando ePay no responde.
-- =====================================================================

-- ---------- 1. La orden recuerda su recarga en ePay ----------
ALTER TABLE vendu.replenishment_orders
  ADD COLUMN IF NOT EXISTS epay_lote        text,
  ADD COLUMN IF NOT EXISTS epay_escrito_en  timestamptz,
  ADD COLUMN IF NOT EXISTS epay_plan        jsonb;   -- última simulación mostrada al usuario

-- ---------- 2. Recargas de máquina escritas en ePay ----------
CREATE TABLE IF NOT EXISTS vendu.epay_recargas (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  orden_id      bigint REFERENCES vendu.replenishment_orders(id),
  maquina_id    bigint NOT NULL REFERENCES public.maquinas(id),
  lote          text,                         -- lo devuelve el MCP; NULL si fue simulación
  modo          text NOT NULL,                -- SIMULACION | ESCRITO | YA_ESCRITO | REVERTIDO | ERROR
  idempotencia  text,
  cambios       jsonb NOT NULL,               -- lo que se envió
  respuesta     jsonb,                        -- lo que devolvió el MCP (plan o detalle)
  error         text,
  usuario       text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vendu_epay_recargas_orden_idx ON vendu.epay_recargas (orden_id);
CREATE INDEX IF NOT EXISTS vendu_epay_recargas_maquina_idx ON vendu.epay_recargas (maquina_id, created_at DESC);

-- ---------- 3. Ajustes del almacén de ePay (entradas y conteos de la oficina) ----------
CREATE TABLE IF NOT EXISTS vendu.epay_almacen_sync (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  movimiento_id  bigint REFERENCES vendu.inventory_movements(id),
  product_id     bigint NOT NULL REFERENCES vendu.products(id),
  producto_epay  text NOT NULL,               -- codigo_epay
  modo           text NOT NULL,               -- sumar | fijar
  cantidad       numeric NOT NULL,
  estado         text NOT NULL DEFAULT 'PENDIENTE',   -- PENDIENTE | OK | ERROR | OMITIDO
  intentos       integer NOT NULL DEFAULT 0,
  respuesta      jsonb,
  error          text,
  usuario        text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vendu_epay_almacen_sync_estado_idx ON vendu.epay_almacen_sync (estado, created_at);

-- El backend (postgres / service_role) es el único que escribe aquí; la app no las ve.
ALTER TABLE vendu.epay_recargas     ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.epay_almacen_sync ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON vendu.epay_recargas, vendu.epay_almacen_sync TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vendu TO service_role;

-- Fin archivo 11.

-- =====================================================================
--  VENDU — PROVEEDORES   ·   ARCHIVO 12   (17-09-2026)
--  Pedido de Juan: al registrar una entrada de mercancía se elige el
--  proveedor y solo aparecen SUS productos. Idempotente.
--
--  vendu.proveedores           catálogo de proveedores (Uriel, TOM, Munchy, Ivanys, ...)
--  vendu.proveedor_productos   qué productos vende cada proveedor (un producto
--                              puede tener varios proveedores)
--  inventory_movements.proveedor_id  de quién vino la entrada
-- =====================================================================

CREATE TABLE IF NOT EXISTS vendu.proveedores (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  nombre      text NOT NULL UNIQUE,
  rif         text,
  contacto    text,
  activo      boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vendu.proveedor_productos (
  proveedor_id  bigint NOT NULL REFERENCES vendu.proveedores(id) ON DELETE CASCADE,
  product_id    bigint NOT NULL REFERENCES vendu.products(id) ON DELETE CASCADE,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (proveedor_id, product_id)
);
CREATE INDEX IF NOT EXISTS vendu_proveedor_productos_product_idx ON vendu.proveedor_productos (product_id);

ALTER TABLE vendu.inventory_movements
  ADD COLUMN IF NOT EXISTS proveedor_id bigint REFERENCES vendu.proveedores(id);

-- Semilla: los proveedores que ya maneja el lector de facturas. Solo los nombres;
-- los productos de cada uno se asignan desde el dashboard (o desde sus perfiles).
INSERT INTO vendu.proveedores (nombre)
VALUES ('Uriel'), ('TOM'), ('Munchy'), ('Ivanys')
ON CONFLICT (nombre) DO NOTHING;

ALTER TABLE vendu.proveedores          ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendu.proveedor_productos  ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON vendu.proveedores, vendu.proveedor_productos TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vendu TO service_role;

-- Fin archivo 12.

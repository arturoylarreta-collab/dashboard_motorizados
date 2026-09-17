"""
tests/fixture_a03_local.py -- Crea en la COPIA LOCAL la máquina de pruebas de
ePay A03 (id ePay 10213, "INACTIVO - Módulo en prueba K") con su planograma
real leído del MCP, para probar recargas contra ePay sin tocar máquinas
reales. Solo A03 y solo el producto "1AA Prueba" están autorizados para
escrituras (regla del 17-09-2026).

Uso (WSL, desde dashboard_motorizados):
  DATABASE_URL=postgresql://vendu:vendu_local@127.0.0.1:5432/vendu_local \
  MCP_URL=... MCP_TOKEN=<token de escritura> python tests/fixture_a03_local.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

URL = os.environ.get("DATABASE_URL", "")
if "127.0.0.1" not in URL and "localhost" not in URL:
    print("Solo corre contra la copia local.")
    sys.exit(2)

from db import DB
from epay_service import EpayService

A03_EPAY_ID = 10213
A03_LOCAL_ID = 9903          # id inventado en public.maquinas de la copia local

db = DB()
svc = EpayService(os.environ["MCP_URL"], os.environ["MCP_TOKEN"])   # token explícito: get_service() leería el de LECTURA de secrets.toml
pl = svc.planograma(A03_EPAY_ID)
slots = pl.get("slots") or []
print("planograma A03:", pl.get("nombre"), "| slots:", len(slots))

db.execute(
    "INSERT INTO public.maquinas (id, nombre, motorizado, codigo_epay, epay_machine_id, epay_uid, tipo) "
    "VALUES (%s, %s, %s, %s, %s, %s, 'SNACK') ON CONFLICT (id) DO UPDATE SET epay_machine_id = EXCLUDED.epay_machine_id",
    (A03_LOCAL_ID, "A03 - PRUEBA (ePay 10213)", "Eduard", "A03-PRUEBA", A03_EPAY_ID, "prueba-a03"))
db.execute(
    "INSERT INTO vendu.maquinas_epay (maquina_id, epay_machine_id, epay_uid, codigo, synced_at) "
    "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (maquina_id) DO UPDATE SET epay_machine_id = EXCLUDED.epay_machine_id, synced_at = now()",
    (A03_LOCAL_ID, A03_EPAY_ID, "prueba-a03", "A03-PRUEBA"))

n = 0
for s in slots:
    pid_epay = str(s.get("producto_id") or "")
    prod = db.one("SELECT p.id FROM vendu.product_mappings pm JOIN vendu.products p ON p.id = pm.product_id "
                  "WHERE pm.epay_producto_id = %s", (pid_epay,)) if pid_epay else None
    product_id = prod["id"] if prod else None
    if product_id is None and s.get("producto"):
        prod = db.one("SELECT id FROM vendu.products WHERE nombre = %s", (s["producto"],))
        product_id = prod["id"] if prod else None
    db.execute(
        "INSERT INTO vendu.machine_slots (maquina_id, slot, seleccion, epay_producto_id, product_id, cantidad, minimo, maximo, activo, estado) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (maquina_id, slot) DO UPDATE SET seleccion = EXCLUDED.seleccion, epay_producto_id = EXCLUDED.epay_producto_id, "
        "product_id = EXCLUDED.product_id, cantidad = EXCLUDED.cantidad, minimo = EXCLUDED.minimo, maximo = EXCLUDED.maximo, "
        "activo = EXCLUDED.activo, estado = EXCLUDED.estado",
        (A03_LOCAL_ID, str(s.get("slot")), s.get("seleccion"), pid_epay or None, product_id,
         float(s.get("cantidad") or 0), float(s.get("minimo") or 0), float(s.get("maximo") or 0),
         bool(s.get("activo", True)), str(s.get("estado") or "")))
    n += 1
print("slots cargados en la copia local:", n)
prueba = db.one("SELECT ms.slot, ms.seleccion, ms.cantidad, ms.maximo, p.id AS product_id, p.nombre FROM vendu.machine_slots ms "
                "JOIN vendu.products p ON p.id = ms.product_id WHERE ms.maquina_id = %s AND p.nombre ILIKE '1AA%%' LIMIT 1",
                (A03_LOCAL_ID,))
print("canal con '1AA Prueba':", prueba)

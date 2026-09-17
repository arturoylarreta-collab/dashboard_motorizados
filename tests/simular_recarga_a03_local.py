"""
tests/simular_recarga_a03_local.py -- Fase 2, prueba SIN escribir en ePay:
crea en la copia local una orden para la máquina de pruebas A03 con el
producto "1AA Prueba", la lleva hasta EN_MAQUINA y pide al MCP la SIMULACIÓN
de la recarga (confirmar=False). Muestra el plan y lo que saldría del
almacén de ePay. No confirma nada: eso requiere el "sí" de Juan.

Uso (WSL): DATABASE_URL=...local MCP_URL=... MCP_TOKEN=<escritura> \
           python tests/simular_recarga_a03_local.py
"""
import json
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
from epay_sync import EpaySync
from inventory import InventoryService
from orders import OrderService

A03_LOCAL_ID = 9903
EDUARD = "11111111-1111-4111-8111-111111111111"
UNIDADES = int(os.environ.get("UNIDADES", "2"))

db = DB()
svc = EpayService(os.environ["MCP_URL"], os.environ["MCP_TOKEN"])   # token explícito: get_service() leería el de LECTURA de secrets.toml
sync = EpaySync(db, svc)
inv = InventoryService(db)
ordenes = OrderService(db)

canal = db.one(
    "SELECT ms.slot, ms.seleccion, ms.cantidad, ms.maximo, p.id AS product_id, p.nombre, p.codigo_epay "
    "FROM vendu.machine_slots ms JOIN vendu.products p ON p.id = ms.product_id "
    "WHERE ms.maquina_id = %s AND p.nombre ILIKE '1AA%%' AND ms.activo ORDER BY ms.slot LIMIT 1", (A03_LOCAL_ID,))
if not canal:
    print("A03 no tiene un canal con '1AA Prueba' en la copia local; corre primero tests/fixture_a03_local.py")
    sys.exit(1)
pid = canal["product_id"]
print(f"canal {canal['slot']} · {canal['nombre']} ({canal['codigo_epay']}) · en ePay {canal['cantidad']:g} de {canal['maximo']:g}")

# Stock en la oficina y orden de prueba
inv.entrada_compra(product_id=pid, cantidad=UNIDADES + 3, usuario="prueba-a03",
                   idempotency_key=f"a03-entrada-{os.getpid()}")
o = ordenes.crear(A03_LOCAL_ID, [{"product_id": pid, "cantidad_ordenada": UNIDADES, "seleccion": canal["seleccion"]}],
                  motorizado_id=EDUARD, observaciones="Prueba fase 2 (A03, solo simulación)", usuario="prueba-a03")
oid = o["id"]
ordenes.cambiar_estado(oid, "PROPUESTA", "prueba-a03")
ordenes.aprobar(oid, "prueba-a03")
ordenes.preparar(oid, "prueba-a03")
ordenes.entregar_al_motorizado(oid, {str(pid): float(UNIDADES)}, "prueba-a03")
ordenes.cambiar_estado(oid, "EN_RUTA", "prueba-a03")
ordenes.cambiar_estado(oid, "EN_MAQUINA", "prueba-a03")
print(f"orden {oid} EN_MAQUINA con {UNIDADES} unidades en el bolso de Eduard")

cambios = sync.cambios_de_orden(oid, {str(pid): float(UNIDADES)})
print("cambios que se enviarían a ePay:", cambios)
plan = sync.plan_recarga(oid, {str(pid): float(UNIDADES)}, usuario="prueba-a03")
print("=== SIMULACIÓN del MCP (no escribe) ===")
print(json.dumps(plan["plan"], ensure_ascii=False, indent=1, default=str)[:1800])
reg = db.one("SELECT modo, lote, created_at FROM vendu.epay_recargas WHERE orden_id = %s ORDER BY id DESC LIMIT 1", (oid,))
print("registro propio:", reg)
print(f"Para escribir de verdad: EpaySync.escribir_recarga({oid}, ...) o el botón 2 del dashboard (requiere el sí de Juan).")

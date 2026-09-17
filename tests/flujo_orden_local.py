"""
tests/flujo_orden_local.py -- Recorre una orden de recarga de punta a punta en
la COPIA LOCAL (Postgres en WSL, schema `vendu`): recomendaciones -> orden ->
aprobar -> preparar -> entregar al motorizado -> en ruta -> en máquina ->
completar y mover. Lo corre dos veces: sin stock en la oficina y con stock.

Nunca contra Supabase de producción: exige que DATABASE_URL apunte a
127.0.0.1 o localhost.

Uso (WSL):  DATABASE_URL=postgresql://vendu:vendu_local@127.0.0.1:5432/vendu_local \
            python tests/flujo_orden_local.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

URL = os.environ.get("DATABASE_URL", "")
if "127.0.0.1" not in URL and "localhost" not in URL:
    print("Solo corre contra la copia local (DATABASE_URL con 127.0.0.1).")
    sys.exit(2)

import recarga
from db import DB
from inventory import InventoryService
from orders import OrderService

EDUARD = "11111111-1111-4111-8111-111111111111"
MAQ = int(os.environ.get("MAQ", "344"))

db = DB()
ordenes = OrderService(db)
inv = InventoryService(db)
fallos = []


def paso(nombre, fn):
    try:
        r = fn()
        print(f"OK    {nombre}: {str(r)[:180]}")
        return r
    except Exception as e:  # noqa: BLE001 - queremos ver cualquier fallo del flujo
        print(f"FALLA {nombre}: {type(e).__name__}: {str(e)[:300]}")
        fallos.append(nombre)
        return None


def balances():
    return db.query(
        "SELECT l.tipo, count(*) AS n, sum(b.quantity) AS q "
        "FROM vendu.inventory_balances b JOIN vendu.inventory_locations l ON l.id = b.location_id "
        "GROUP BY 1 ORDER BY 1")


def flujo(tag, con_stock):
    o = recarga.crear_desde_recomendaciones(db, MAQ, motorizado_id=EDUARD, usuario="prueba")
    oid = o["orden"]["id"]
    items = db.query(
        "SELECT id, product_id, cantidad_ordenada FROM vendu.replenishment_order_items "
        "WHERE orden_id = %s ORDER BY id", (oid,))
    print(f"[{tag}] orden {oid}: {len(items)} ítems, ej. {items[:2]}")
    if con_stock:
        for i in items:
            pid, cant = i["product_id"], float(i["cantidad_ordenada"] or 0) + 5
            paso(f"{tag} entrada_compra p{pid}",
                 lambda pid=pid, cant=cant: inv.entrada_compra(
                     product_id=pid, cantidad=cant, usuario="prueba",
                     idempotency_key=f"test-entrada-{oid}-{pid}"))
    paso(f"{tag} aprobar", lambda: ordenes.aprobar(oid, "prueba"))
    paso(f"{tag} preparar", lambda: ordenes.preparar(oid, "prueba"))
    entregadas = {str(i["product_id"]): float(i["cantidad_ordenada"] or 0) for i in items}
    paso(f"{tag} entregar_al_motorizado", lambda: ordenes.entregar_al_motorizado(oid, entregadas, "prueba"))
    print("      balances tras entregar:", balances())
    paso(f"{tag} EN_RUTA", lambda: ordenes.cambiar_estado(oid, "EN_RUTA", "prueba"))
    paso(f"{tag} EN_MAQUINA", lambda: ordenes.cambiar_estado(oid, "EN_MAQUINA", "prueba"))
    paso(f"{tag} completar_y_mover",
         lambda: recarga.completar_y_mover(db, oid, entregadas, motorizado_id=EDUARD, usuario="prueba"))
    estado = db.one("SELECT estado FROM vendu.replenishment_orders WHERE id = %s", (oid,))["estado"]
    print("      estado final:", estado)
    print("      ítems:", db.query(
        "SELECT cantidad_llevada AS llevada, cantidad_entregada AS entregada, cantidad_colocada AS colocada "
        "FROM vendu.replenishment_order_items WHERE orden_id = %s LIMIT 3", (oid,)))
    print("      movimientos:", db.query(
        "SELECT tipo, count(*) FROM vendu.inventory_movements WHERE orden_id = %s GROUP BY 1", (oid,)))
    print("      balances al final:", balances())
    return estado


def flujo_todo_o_nada(tag):
    """Colocar MÁS de lo llevado debe fallar sin dejar movimientos a medias."""
    o = recarga.crear_desde_recomendaciones(db, MAQ, motorizado_id=EDUARD, usuario="prueba")
    oid = o["orden"]["id"]
    items = db.query("SELECT product_id, cantidad_ordenada FROM vendu.replenishment_order_items WHERE orden_id = %s", (oid,))
    for i in items:
        inv.entrada_compra(product_id=i["product_id"], cantidad=float(i["cantidad_ordenada"]) + 5,
                           usuario="prueba", idempotency_key=f"test-entrada-{oid}-{i['product_id']}")
    ordenes.aprobar(oid, "prueba"); ordenes.preparar(oid, "prueba")
    entregadas = {str(i["product_id"]): float(i["cantidad_ordenada"] or 0) for i in items}
    ordenes.entregar_al_motorizado(oid, entregadas, "prueba")
    ordenes.cambiar_estado(oid, "EN_RUTA", "prueba"); ordenes.cambiar_estado(oid, "EN_MAQUINA", "prueba")
    exceso = {k: v + 100 for k, v in entregadas.items()}          # más de lo llevado
    antes = db.one("SELECT count(*) AS n FROM vendu.inventory_movements WHERE orden_id = %s AND tipo = 'RECARGA_MAQUINA'", (oid,))["n"]
    try:
        recarga.completar_y_mover(db, oid, exceso, motorizado_id=EDUARD, usuario="prueba")
        print(f"FALLA {tag}: completar con exceso NO falló")
        fallos.append(tag)
    except Exception as e:  # noqa: BLE001
        print(f"OK    {tag}: rechazado como se esperaba ({type(e).__name__})")
    despues = db.one("SELECT count(*) AS n FROM vendu.inventory_movements WHERE orden_id = %s AND tipo = 'RECARGA_MAQUINA'", (oid,))["n"]
    estado = db.one("SELECT estado FROM vendu.replenishment_orders WHERE id = %s", (oid,))["estado"]
    ok = (despues == antes) and estado == "EN_MAQUINA"
    print(f"{'OK   ' if ok else 'FALLA'} {tag}: movimientos antes={antes} después={despues}, estado={estado} (esperado: sin movimientos nuevos y EN_MAQUINA)")
    if not ok:
        fallos.append(tag + " (quedaron movimientos a medias)")
    return estado


print("ubicaciones antes:", db.query("SELECT id, tipo, motorizado_id, maquina_id FROM vendu.inventory_locations ORDER BY id LIMIT 6"))
eA = flujo("A-sin-stock", False)
print("=" * 70)
eB = flujo("B-con-stock", True)
print("=" * 70)
eC = flujo_todo_o_nada("C-todo-o-nada")
print("=" * 70)
print("Resultado: A =", eA, "| B =", eB, "| C =", eC, "| pasos fallidos:", fallos)
sys.exit(1 if fallos else 0)

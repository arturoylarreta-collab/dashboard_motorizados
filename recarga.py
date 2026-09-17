"""
Cierre del lazo recomendación → orden → movimiento físico (schema vendu).

RecomendacionesService produce cantidades A NIVEL PRODUCTO (sumando slots).
Este módulo:
  * reparte esa cantidad entre los slots de la máquina por capacidad libre
    (FIFO sobre capacidad_libre = maximo - cantidad, desc),
  * crea la orden desde las recomendaciones más recientes,
  * y al completar genera los movimientos RECARGA_MAQUINA por slot (idempotentes).

Se apoya en OrderService/InventoryService (transiciones y saldos ya validados
por servicio + trigger SQL).
"""

from __future__ import annotations

from typing import Dict, List, Optional

import demand
from db import DB
from inventory import InventoryService
from orders import OrderService


class RecargaError(ValueError):
    """Error de negocio de recarga."""


def repartir_por_slots(db: DB, maquina_id: int,
                       items: List[Dict]) -> List[Dict]:
    """Reparte `items` (product_id + cantidad) entre los slots activos de la máquina.

    Por cada producto, ordena sus slots por capacidad_libre desc y asigna de a
    uno (FIFO). Devuelve líneas {slot, seleccion, product_id, cantidad}.
    No asigna más de la capacidad libre del slot; si sobra cantidad, queda fuera.
    """
    slots = db.query(
        """
        SELECT slot, seleccion, product_id, cantidad, maximo, activo
        FROM vendu.machine_slots
        WHERE maquina_id = %s
        """,
        (maquina_id,),
    )
    por_producto: Dict[int, List[Dict]] = {}
    for s in slots:
        if s["activo"] and s["product_id"] is not None:
            por_producto.setdefault(s["product_id"], []).append(s)

    lineas: List[Dict] = []
    for it in items:
        pid = int(it["product_id"])
        restante = float(it["cantidad"] or 0)
        if restante <= 0:
            continue
        ss = por_producto.get(pid, [])
        ss = [s for s in ss if (float(s["maximo"] or 0)
                                - float(s["cantidad"] or 0)) > 0]
        ss.sort(key=lambda s: float(s["maximo"] or 0)
                - float(s["cantidad"] or 0), reverse=True)
        for s in ss:
            if restante <= 0:
                break
            cap_libre = float(s["maximo"] or 0) - float(s["cantidad"] or 0)
            asig = min(restante, cap_libre)
            lineas.append({
                "slot": s["slot"],
                "seleccion": s["seleccion"],
                "product_id": pid,
                "cantidad": round(asig, 2),
            })
            restante -= asig
    return lineas


def ultimas_recomendaciones(db: DB, maquina_id: int,
                            config: Optional[demand.DemandConfig] = None
                            ) -> List[Dict]:
    """Últimas recomendaciones por producto de una máquina, con cantidad > 0."""
    rows = db.query(
        """
        SELECT DISTINCT ON (product_id)
               product_id, cantidad_recomendada, motivo, calculado_en
        FROM vendu.replenishment_recommendations
        WHERE maquina_id = %s
        ORDER BY product_id, calculado_en DESC
        """,
        (maquina_id,),
    )
    return [r for r in rows if float(r["cantidad_recomendada"] or 0) > 0]


def crear_desde_recomendaciones(db: DB, maquina_id: int,
                                motorizado_id: Optional[str] = None,
                                usuario: Optional[str] = None,
                                config: Optional[demand.DemandConfig] = None
                                ) -> Dict:
    """Crea una orden PROPUESTA a partir de las recomendaciones vigentes.

    Ítems a nivel producto (cantidad_ordenada = cantidad_recomendada).
    Devuelve la orden y el reparto esperado por slot.
    """
    recs = ultimas_recomendaciones(db, maquina_id, config)
    if not recs:
        raise RecargaError("No hay recomendaciones con cantidad > 0 para la máquina.")
    items = [{
        "product_id": r["product_id"],
        "cantidad_ordenada": float(r["cantidad_recomendada"]),
    } for r in recs]

    svc = OrderService(db)
    orden = svc.crear(
        maquina_id, items, motorizado_id=motorizado_id,
        observaciones=f"Generada desde recomendaciones ({len(recs)} ítems)",
        usuario=usuario)
    svc.cambiar_estado(orden["id"], "PROPUESTA", usuario)
    reparto = repartir_por_slots(db, maquina_id, [{
        "product_id": r["product_id"],
        "cantidad": float(r["cantidad_recomendada"]),
    } for r in recs])
    return {"orden": svc.obtener(orden["id"]), "reparto": reparto,
            "items": items}


def completar_y_mover(db: DB, orden_id: int,
                      colocadas: Dict[str, float],
                      motorizado_id: Optional[str] = None,
                      usuario: Optional[str] = None) -> Dict:
    """Genera los RECARGA_MAQUINA por slot y cierra la orden en COMPLETADA.

    `colocadas` mapea product_id → cantidad realmente colocada (total).
    El reparto por slot se recalcula con repartir_por_slots; cada movimiento
    lleva idempotency `rec-<orden>-<slot>` (reintentos no duplican).
    """
    orden = OrderService(db).obtener(orden_id)
    if not motorizado_id:
        motorizado_id = orden.get("motorizado_id")
    if not motorizado_id:
        raise RecargaError(
            "La orden no tiene motorizado: no se pueden registrar los movimientos.")

    reparto = repartir_por_slots(db, orden["maquina_id"], [
        {"product_id": pid, "cantidad": cant}
        for pid, cant in colocadas.items() if float(cant) > 0
    ])

    inv = InventoryService(db)
    movimientos = []
    # Todo o nada: si el cierre de la orden falla (p. ej. colocado > llevado)
    # los movimientos no quedan aplicados a medias (fase 1, 17-09-2026).
    with db.transaccion():
        for l in reparto:
            movimientos.append(inv.recargar_maquina(
                motorizado_id=motorizado_id,
                maquina_id=orden["maquina_id"],
                product_id=l["product_id"],
                cantidad=l["cantidad"],
                orden_id=orden_id,
                seleccion=l["seleccion"],
                usuario=usuario,
                idempotency_key=f"rec-{orden_id}-{l['slot']}",
            ))
        final = OrderService(db).completar(orden_id, colocadas, usuario)
    return {"orden": final, "reparto": reparto, "movimientos": movimientos}
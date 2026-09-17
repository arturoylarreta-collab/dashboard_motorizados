"""
Órdenes de recarga: máquina de estados + reglas de negocio (schema vendu).

La orden es una entidad de negocio real (no una pantalla). Estados y
transiciones reflejan el flujo físico:

    BORRADOR → PROPUESTA → APROBADA → PREPARANDO → ENTREGADA_AL_MOTORIZADO
             → EN_RUTA → EN_MAQUINA → COMPLETADA     (CANCELADA desde casi todo)

Reglas (Regla #22):
  * No saltarse estados críticos.
  * No completar dos veces la misma orden.
  * No cerrar la orden sin cantidades.
  * No colocar más producto del que el motorizado llevó/posee.

La lógica de transición es pura y testeable; la persistencia usa la capa `DB`
(psycopg2, schema `vendu`). El trigger SQL (`trg_validate_order_transition`)
valida estados y escribe `order_status_log` como segunda barrera.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from db import DB
from conciliacion import AuditService

ESTADOS = [
    "BORRADOR", "PROPUESTA", "APROBADA", "PREPARANDO",
    "ENTREGADA_AL_MOTORIZADO", "EN_RUTA", "EN_MAQUINA", "COMPLETADA", "CANCELADA",
]

TRANSICIONES: Dict[str, List[str]] = {
    "BORRADOR": ["PROPUESTA", "CANCELADA"],
    "PROPUESTA": ["APROBADA", "CANCELADA"],
    "APROBADA": ["PREPARANDO", "CANCELADA"],
    "PREPARANDO": ["ENTREGADA_AL_MOTORIZADO", "CANCELADA"],
    "ENTREGADA_AL_MOTORIZADO": ["EN_RUTA", "CANCELADA"],
    "EN_RUTA": ["EN_MAQUINA", "CANCELADA"],
    "EN_MAQUINA": ["COMPLETADA", "CANCELADA"],
    "COMPLETADA": [],
    "CANCELADA": [],
}


class OrdenError(ValueError):
    """Error de negocio de órdenes (se traduce a 400/409)."""


def validar_transicion(desde: str, hasta: str) -> bool:
    """True si la transición `desde → hasta` es válida (idempotente si son iguales)."""
    if desde == hasta:
        return True
    return hasta in TRANSICIONES.get(desde, [])


def exigir_transicion(desde: str, hasta: str) -> None:
    if not validar_transicion(desde, hasta):
        raise OrdenError(f"Transición de orden inválida: {desde} → {hasta}")


def validar_items(items: List[dict]) -> None:
    """Valida las líneas de una orden antes de operar con ellas."""
    if not items:
        raise OrdenError("La orden no tiene productos.")
    for it in items:
        cant_ordenada = float(it.get("cantidad_ordenada") or 0)
        if cant_ordenada < 0:
            raise OrdenError("cantidad_ordenada no puede ser negativa.")
        colocada = float(it.get("cantidad_colocada") or 0)
        llevada = float(it.get("cantidad_llevada") or 0)
        if colocada < 0 or llevada < 0:
            raise OrdenError("Las cantidades colocadas/llevadas no pueden ser negativas.")
        if colocada > llevada and llevada > 0:
            raise OrdenError(
                f"No se puede colocar ({colocada:g}) más de lo llevado ({llevada:g}) "
                f"en el producto {it.get('product_id')}."
            )
        sobrante = llevada - colocada
        if sobrante < 0:
            raise OrdenError(f"Sobrante negativo en el producto {it.get('product_id')}.")


class OrderService:
    """Operaciones sobre vendu.replenishment_orders + order_items."""

    def __init__(self, db: DB, audit: Optional[AuditService] = None):
        self._db = db
        self._audit = audit or AuditService(db)

    # ------------------------------------------------------------------ #
    # Consultas
    # ------------------------------------------------------------------ #
    def obtener(self, orden_id: int) -> dict:
        row = self._db.one(
            "SELECT * FROM vendu.replenishment_orders WHERE id = %s", (orden_id,))
        if not row:
            raise OrdenError(f"Orden {orden_id} no existe.")
        return row

    def items(self, orden_id: int) -> List[dict]:
        return self._db.query(
            "SELECT * FROM vendu.replenishment_order_items "
            "WHERE orden_id = %s ORDER BY id", (orden_id,))

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #
    def crear(self, maquina_id: int, items: List[dict],
              motorizado_id: Optional[str] = None,
              observaciones: Optional[str] = None,
              usuario: Optional[str] = None) -> dict:
        """Crea una orden en BORRADOR con sus ítems (cantidad_ordenada)."""
        validar_items(items)
        id_ = self._db.insert("replenishment_orders", {
            "maquina_id": maquina_id,
            "motorizado_id": motorizado_id,
            "estado": "BORRADOR",
            "observaciones": observaciones,
        })
        lineas = [{
            "orden_id": id_,
            "product_id": it["product_id"],
            "seleccion": it.get("seleccion"),
            "cantidad_ordenada": float(it.get("cantidad_ordenada") or 0),
        } for it in items]
        if lineas:
            self._db.insert_many("replenishment_order_items", lineas)
        self._audit.registrar(
            "ORDEN_CREAR", entidad="replenishment_orders", entidad_id=str(id_),
            despues={"maquina_id": maquina_id, "n_items": len(items)},
            maquina_id=maquina_id, usuario=usuario, referencia="crear")
        return self.obtener(id_)

    def cambiar_estado(self, orden_id: int, nuevo_estado: str,
                       usuario: Optional[str] = None) -> dict:
        """Valida y aplica una transición de estado (el trigger valida de nuevo)."""
        orden = self.obtener(orden_id)
        exigir_transicion(orden["estado"], nuevo_estado)
        self._db.execute(
            "UPDATE vendu.replenishment_orders "
            "SET estado = %s, usuario_aprobador = %s, updated_at = now() "
            "WHERE id = %s",
            (nuevo_estado, usuario, orden_id))
        self._audit.registrar(
            "ORDEN_ESTADO", entidad="replenishment_orders", entidad_id=str(orden_id),
            antes=orden["estado"], despues=nuevo_estado,
            maquina_id=orden["maquina_id"], usuario=usuario)
        return self.obtener(orden_id)

    def aprobar(self, orden_id: int, usuario: Optional[str] = None) -> dict:
        return self.cambiar_estado(orden_id, "APROBADA", usuario)

    def preparar(self, orden_id: int, usuario: Optional[str] = None) -> dict:
        return self.cambiar_estado(orden_id, "PREPARANDO", usuario)

    def _actualizar_items(self, item_id: int, cantidad_entregada: float,
                          cantidad_llevada: float, cantidad_colocada: float,
                          sobrante: float) -> None:
        self._db.execute(
            "UPDATE vendu.replenishment_order_items "
            "SET cantidad_entregada = %s, cantidad_llevada = %s, "
            "    cantidad_colocada = %s, sobrante = %s "
            "WHERE id = %s",
            (cantidad_entregada, cantidad_llevada, cantidad_colocada, sobrante, item_id))

    def entregar_al_motorizado(self, orden_id: int,
                               entregadas: Dict[str, float],
                               usuario: Optional[str] = None) -> dict:
        """Registra lo realmente entregado al motorizado (puede diferir de lo ordenado)."""
        self.cambiar_estado(orden_id, "ENTREGADA_AL_MOTORIZADO", usuario)
        for item in self.items(orden_id):
            pid = str(item["product_id"])
            if pid in entregadas:
                ent = float(entregadas[pid])
                self._actualizar_items(item["id"], ent, ent, 0.0, 0.0)
        return self.obtener(orden_id)

    def completar(self, orden_id: int, colocadas: Dict[str, float],
                  usuario: Optional[str] = None) -> dict:
        """Cierra la orden con las cantidades realmente colocadas en la máquina.

        Calcula el sobrante = llevada - colocada y exige que sea >= 0.
        """
        for item in self.items(orden_id):
            pid = str(item["product_id"])
            if pid in colocadas:
                llevada = float(item.get("cantidad_llevada") or 0)
                colocada = float(colocadas[pid])
                if colocada > llevada:
                    raise OrdenError(
                        f"No se puede colocar ({colocada:g}) más de lo llevado "
                        f"({llevada:g}) en el producto {pid}."
                    )
                self._actualizar_items(item["id"], 0.0, llevada, colocada,
                                      round(llevada - colocada, 2))
        return self.cambiar_estado(orden_id, "COMPLETADA", usuario)

    def cancelar(self, orden_id: int, usuario: Optional[str] = None) -> dict:
        return self.cambiar_estado(orden_id, "CANCELADA", usuario)
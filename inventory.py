"""
Servicio de inventario: movimientos entre los 3 niveles (schema vendu).

Flujo físico y contable (Regla de oro):

    INVENTARIO PRINCIPAL  →  INVENTARIO DEL MOTORIZADO  →  INVENTARIO DE LA MÁQUINA

Todo cambio de cantidad pasa por un `inventory_movements` con:
  * tipo, product_id, cantidad
  * origen/destino (inventory_locations)
  * motorizado, máquina, orden, usuario, observación
  * idempotency_key único (evita duplicados ante reintentos/offline)

El saldo (inventory_balances) lo mantiene el trigger SQL; aquí se garantiza
la trazabilidad, la idempotencia y la auditoría. Los balances se pueden
reconstruir desde movimientos (Regla de oro). La capa de datos es `DB`
(psycopg2, schema `vendu`).
"""

from __future__ import annotations

import uuid
from typing import Optional

from db import DB
from conciliacion import AuditService

TIPOS_MOVIMIENTO = [
    "ENTRADA_COMPRA",           # al depósito / principal
    "TRANSFERENCIA_A_MOTORIZADO",  # principal → motorizado
    "DEVOLUCION_DE_MOTORIZADO",   # motorizado → principal
    "RECARGA_MAQUINA",           # motorizado → máquina
    "MERMA",
    "AJUSTE",
    "VENTA",                    # salida por venta (opcional: si no se deduce de ePay)
    "DEVOLUCION_CLIENTE",
]


class InventarioError(ValueError):
    """Error de negocio de inventario (400)."""


def nueva_idempotency_key(prefijo: str = "mov") -> str:
    return f"{prefijo}-{uuid.uuid4().hex}"


class InventoryService:
    """Movimientos de inventario con idempotencia y trazabilidad."""

    def __init__(self, db: DB, audit: Optional[AuditService] = None):
        self._db = db
        self._audit = audit or AuditService(db)

    # ------------------------------------------------------------------ #
    # Ubicaciones (devuelven el id de inventory_locations)
    # ------------------------------------------------------------------ #
    def ubicacion_principal(self) -> int:
        row = self._db.one(
            "SELECT id FROM vendu.inventory_locations WHERE tipo = 'PRINCIPAL'")
        if row:
            return row["id"]
        return self._db.insert("inventory_locations", {"tipo": "PRINCIPAL"})

    def ubicacion_motorizado(self, motorizado_id: str) -> int:
        row = self._db.one(
            "SELECT id FROM vendu.inventory_locations "
            "WHERE tipo = 'MOTORIZADO' AND motorizado_id = %s", (motorizado_id,))
        if row:
            return row["id"]
        return self._db.insert("inventory_locations", {
            "tipo": "MOTORIZADO", "motorizado_id": motorizado_id,
        })

    def ubicacion_maquina(self, maquina_id: int) -> int:
        row = self._db.one(
            "SELECT id FROM vendu.inventory_locations "
            "WHERE tipo = 'MAQUINA' AND maquina_id = %s", (maquina_id,))
        if row:
            return row["id"]
        return self._db.insert("inventory_locations", {
            "tipo": "MAQUINA", "maquina_id": maquina_id,
        })

    # ------------------------------------------------------------------ #
    # Movimiento genérico (idempotente)
    # ------------------------------------------------------------------ #
    def mover(self, *, tipo: str, product_id: int, cantidad: float,
              origen_id: Optional[int] = None, destino_id: Optional[int] = None,
              motorizado_id: Optional[str] = None, maquina_id: Optional[int] = None,
              orden_id: Optional[int] = None, seleccion: Optional[str] = None,
              usuario: Optional[str] = None, observacion: Optional[str] = None,
              idempotency_key: Optional[str] = None) -> dict:
        if tipo not in TIPOS_MOVIMIENTO:
            raise InventarioError(f"Tipo de movimiento desconocido: {tipo}")
        if cantidad == 0:
            raise InventarioError("La cantidad no puede ser cero.")
        key = idempotency_key or nueva_idempotency_key()

        existente = self._db.one(
            "SELECT * FROM vendu.inventory_movements WHERE idempotency_key = %s",
            (key,))
        if existente:
            return existente

        payload = {
            "tipo": tipo,
            "product_id": product_id,
            "cantidad": cantidad,
            "origen_id": origen_id,
            "destino_id": destino_id,
            "motorizado_id": motorizado_id,
            "maquina_id": maquina_id,
            "orden_id": orden_id,
            "seleccion": seleccion,
            "usuario": usuario,
            "observacion": observacion,
            "idempotency_key": key,
        }
        mov_id = self._db.insert("inventory_movements", payload)
        # El trigger fn_apply_inventory_movement aplica saldos y escribe audit_logs.
        return self._db.one(
            "SELECT * FROM vendu.inventory_movements WHERE id = %s", (mov_id,))

    # ------------------------------------------------------------------ #
    # Operaciones de negocio
    # ------------------------------------------------------------------ #
    def transferir_a_motorizado(self, *, motorizado_id: str, product_id: int,
                                cantidad: float, orden_id: Optional[int] = None,
                                usuario: Optional[str] = None,
                                idempotency_key: Optional[str] = None) -> dict:
        """PRINCIPAL → MOTORIZADO."""
        origen = self.ubicacion_principal()
        destino = self.ubicacion_motorizado(motorizado_id)
        return self.mover(
            tipo="TRANSFERENCIA_A_MOTORIZADO", product_id=product_id, cantidad=cantidad,
            origen_id=origen, destino_id=destino,
            motorizado_id=motorizado_id, orden_id=orden_id, usuario=usuario,
            idempotency_key=idempotency_key,
        )

    def devolver_de_motorizado(self, *, motorizado_id: str, product_id: int,
                               cantidad: float, usuario: Optional[str] = None,
                               idempotency_key: Optional[str] = None) -> dict:
        """MOTORIZADO → PRINCIPAL."""
        origen = self.ubicacion_motorizado(motorizado_id)
        destino = self.ubicacion_principal()
        return self.mover(
            tipo="DEVOLUCION_DE_MOTORIZADO", product_id=product_id, cantidad=cantidad,
            origen_id=origen, destino_id=destino,
            motorizado_id=motorizado_id, usuario=usuario,
            idempotency_key=idempotency_key,
        )

    def recargar_maquina(self, *, motorizado_id: str, maquina_id: int,
                         product_id: int, cantidad: float,
                         orden_id: Optional[int] = None, seleccion: Optional[str] = None,
                         usuario: Optional[str] = None,
                         idempotency_key: Optional[str] = None) -> dict:
        """MOTORIZADO → MÁQUINA (recarga física)."""
        origen = self.ubicacion_motorizado(motorizado_id)
        destino = self.ubicacion_maquina(maquina_id)
        return self.mover(
            tipo="RECARGA_MAQUINA", product_id=product_id, cantidad=cantidad,
            origen_id=origen, destino_id=destino,
            motorizado_id=motorizado_id, maquina_id=maquina_id,
            orden_id=orden_id, seleccion=seleccion, usuario=usuario,
            idempotency_key=idempotency_key,
        )

    def entrada_compra(self, *, product_id: int, cantidad: float,
                       usuario: Optional[str] = None,
                       idempotency_key: Optional[str] = None) -> dict:
        """Entrada de mercancía al depósito (principal)."""
        destino = self.ubicacion_principal()
        return self.mover(
            tipo="ENTRADA_COMPRA", product_id=product_id, cantidad=cantidad,
            destino_id=destino, usuario=usuario,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------ #
    # Reconstrucción desde movimientos (Regla de oro)
    # ------------------------------------------------------------------ #
    def saldo_desde_movimientos(self, location_id: int, product_id: int) -> float:
        """Saldo = Σ(entradas) - Σ(salidas) para una ubicación/producto.

        El balance almacenado puede reconstruirse así, verificando integridad.
        """
        row = self._db.one(
            """
            SELECT COALESCE(SUM(cantidad) FILTER (WHERE destino_id = %s AND product_id = %s), 0) AS entradas,
                   COALESCE(SUM(cantidad) FILTER (WHERE origen_id  = %s AND product_id = %s), 0) AS salidas
            FROM vendu.inventory_movements
            WHERE product_id = %s
            """,
            (location_id, product_id, location_id, product_id, product_id),
        )
        return round(float(row["entradas"]) - float(row["salidas"]), 2)
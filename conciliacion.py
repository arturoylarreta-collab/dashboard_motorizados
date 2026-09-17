"""
Conciliación de inventario (schema vendu).

* La MÁQUINA (ePay) es la fuente de verdad del stock en campo.
* El LIBRO (`inventory_balances`) es el inventario que vendu lleva por
  movimientos (recargas, devoluciones, ventas esperadas).
* `sembrar_inventario_maquinas` adopta por primera vez el snapshot de ePay
  para iniciar el libro (idempotente: solo lugares/productos sin balance).
* `conciliar` compara libro vs ePay y registra diferencias en
  `vendu.inventory_reconciliation`.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from db import DB


class ReconciliationService:
    TOLERANCIA = 1  # unidades de diferencia toleradas sin marcar

    def __init__(self, db: DB):
        self.db = db

    def ubicacion_maquina(self, maquina_id: int) -> int:
        row = self.db.one(
            "SELECT id FROM vendu.inventory_locations "
            "WHERE tipo = 'MAQUINA' AND maquina_id = %s",
            (maquina_id,),
        )
        if row:
            return row["id"]
        return self.db.insert("inventory_locations", {
            "tipo": "MAQUINA", "maquina_id": maquina_id,
        })

    def sembrar_inventario_maquinas(self, aplicar: bool = True) -> dict:
        """Inicializa el libro de cada máquina con el stock que reporta ePay.

        Eficiente: 4 consultas globales + 1 insert en lote (sin loop por máquina).
        """
        epay = self.db.query(
            """
            SELECT ms.maquina_id, ms.product_id, sum(ms.cantidad) AS cantidad
            FROM vendu.machine_slots ms
            WHERE ms.activo AND ms.product_id IS NOT NULL
            GROUP BY ms.maquina_id, ms.product_id
            HAVING sum(ms.cantidad) > 0
            """)
        maquina_ids = {r["maquina_id"] for r in epay}

        locs = self.db.query(
            "SELECT id, maquina_id FROM vendu.inventory_locations "
            "WHERE tipo = 'MAQUINA'")
        loc_por_maq = {r["maquina_id"]: r["id"] for r in locs}
        faltan_ubicaciones = sorted(maquina_ids - set(loc_por_maq))

        balances = self.db.query(
            """
            SELECT l.maquina_id, b.location_id, b.product_id
            FROM vendu.inventory_balances b
            JOIN vendu.inventory_locations l ON l.id = b.location_id
            WHERE l.tipo = 'MAQUINA'
            """)
        ya_sembrado = {(r["maquina_id"], r["product_id"]) for r in balances}

        if not aplicar:
            return {
                "sembradas": 0,
                "ubicaciones_nuevas": len(faltan_ubicaciones),
                "balanzas_nuevas": sum(1 for (m, p) in
                                       {(r["maquina_id"], r["product_id"]) for r in epay}
                                       if (m, p) not in ya_sembrado),
            }

        nuevas_ubicaciones = 0
        for mid in faltan_ubicaciones:
            id_ = self.db.insert("inventory_locations", {
                "tipo": "MAQUINA", "maquina_id": mid,
            })
            loc_por_maq[mid] = id_
            nuevas_ubicaciones += 1

        filas = [{
            "location_id": loc_por_maq[m],
            "product_id": p,
            "quantity": float(c),
        } for (m, p, c) in [(r["maquina_id"], r["product_id"], r["cantidad"]) for r in epay]
          if (m, p) not in ya_sembrado and m in loc_por_maq]

        sembradas = 0
        if filas:
            self.db.upsert("inventory_balances", filas,
                           conflict=["location_id", "product_id"],
                           update=["quantity", "updated_at"])
            sembradas = len(filas)
        return {
            "sembradas": sembradas,
            "ubicaciones_nuevas": nuevas_ubicaciones,
            "balanzas_nuevas": sembradas,
        }

    def conciliar(self, aplicar: bool = True) -> int:
        """Compara libro vs ePay por (máquina, producto) y persiste difs."""
        libro = self.db.query(
            """
            SELECT l.maquina_id, b.product_id, sum(b.quantity) AS qty
            FROM vendu.inventory_balances b
            JOIN vendu.inventory_locations l ON l.id = b.location_id
            WHERE l.tipo = 'MAQUINA' AND l.maquina_id IS NOT NULL
            GROUP BY l.maquina_id, b.product_id
            """)
        epay = self.db.query(
            """
            SELECT ms.maquina_id, ms.product_id, sum(ms.cantidad) AS qty
            FROM vendu.machine_slots ms
            WHERE ms.activo AND ms.product_id IS NOT NULL
            GROUP BY ms.maquina_id, ms.product_id
            """)
        libro_map = {(r["maquina_id"], r["product_id"]): float(r["qty"] or 0)
                     for r in libro}
        epay_map = {(r["maquina_id"], r["product_id"]): float(r["qty"] or 0)
                    for r in epay}

        filas = []
        for key in set(libro_map) | set(epay_map):
            mid, pid = key
            v = libro_map.get(key, 0.0)
            e = epay_map.get(key, 0.0)
            diff = round(v - e, 2)
            estado = "OK" if abs(diff) <= self.TOLERANCIA else "DIFERENCIA"
            filas.append({
                "maquina_id": mid, "product_id": pid,
                "inventario_vendu": v, "inventario_epay": e,
                "diferencia": diff, "estado": estado,
            })

        if aplicar and filas:
            self.db.insert_many("inventory_reconciliation", filas)
        return len(filas)


class AuditService:
    """Bitácora central de mutaciones (vendu.audit_logs)."""

    def __init__(self, db: DB):
        self.db = db

    def registrar(self, accion: str, *, entidad: Optional[str] = None,
                  entidad_id: Optional[str] = None, antes=None, despues=None,
                  usuario: Optional[str] = None, origen: Optional[str] = None,
                  destino: Optional[str] = None, referencia: Optional[str] = None,
                  maquina_id: Optional[int] = None,
                  product_id: Optional[int] = None) -> int:
        def _json(v):
            return json.dumps(v, ensure_ascii=False) if v is not None else None
        fila = {
            "accion": accion,
            "entidad": entidad,
            "entidad_id": entidad_id,
            "antes": _json(antes),
            "despues": _json(despues),
            "usuario": usuario,
            "origen": origen,
            "destino": destino,
            "referencia": referencia,
            "maquina_id": maquina_id,
            "product_id": product_id,
        }
        return self.db.insert("audit_logs", fila)
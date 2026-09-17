"""
Recomendaciones de recarga: `demand.py` sobre los datos sincronizados (schema vendu).

Agrega stock/capacidad de todos los slots activos de un producto en una máquina
(a nivel PRODUCTO), calcula el consumo diario a partir de `epay_sales`
(agregados por rango con periodo_desde..fecha) y persiste en
`vendu.replenishment_recommendations`.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

import demand
from db import DB


class RecomendacionesService:
    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------------------------ #
    # Consumo diario por producto (a partir de los agregados de epay_sales)
    # ------------------------------------------------------------------ #
    def _consumo_por_producto(self, maquina_id: int) -> Dict[int, Dict]:
        rows = self.db.query(
            """
            SELECT product_id, cantidad, fecha, periodo_desde
            FROM vendu.epay_sales
            WHERE maquina_id = %s AND product_id IS NOT NULL
            """,
            (maquina_id,),
        )
        res: Dict[int, Dict] = {}
        for r in rows:
            pid = r["product_id"]
            if pid not in res:
                res[pid] = {"cantidad": 0.0, "desde": None, "hasta": None}
            res[pid]["cantidad"] += float(r["cantidad"] or 0)
            if r["periodo_desde"] and (res[pid]["desde"] is None
                                       or r["periodo_desde"] < res[pid]["desde"]):
                res[pid]["desde"] = r["periodo_desde"]
            if r["fecha"] and (res[pid]["hasta"] is None
                               or r["fecha"] > res[pid]["hasta"]):
                res[pid]["hasta"] = r["fecha"]
        return res

    # ------------------------------------------------------------------ #
    # Cálculo (read-only)
    # ------------------------------------------------------------------ #
    def generar(self, maquina_id: int,
                config: Optional[demand.DemandConfig] = None) -> List[dict]:
        slots = self.db.query(
            """
            SELECT slot, seleccion, product_id, cantidad, minimo, maximo, activo
            FROM vendu.machine_slots
            WHERE maquina_id = %s
            """,
            (maquina_id,),
        )
        por_prod: Dict[int, List[dict]] = {}
        for s in slots:
            if not s["activo"] or s["product_id"] is None:
                continue
            por_prod.setdefault(s["product_id"], []).append(s)

        consumo = self._consumo_por_producto(maquina_id)
        resultados = []
        for pid, ss in por_prod.items():
            stock = sum(float(x["cantidad"] or 0) for x in ss)
            minimo = sum(float(x["minimo"] or 0) for x in ss)
            maximo = sum(float(x["maximo"] or 0) for x in ss)

            serie: List[float] = []
            c = consumo.get(pid)
            if c and c["desde"] and c["hasta"]:
                try:
                    dias = max(1, (date.fromisoformat(str(c["hasta"]))
                                   - date.fromisoformat(str(c["desde"]))).days + 1)
                    diario = c["cantidad"] / dias
                    serie = [diario] * dias
                except ValueError:
                    serie = []

            r = demand.analizar(str(pid), stock, minimo, maximo, serie,
                                config=config)
            d = r.as_dict()
            d["product_id"] = pid
            d["maquina_id"] = maquina_id
            resultados.append(d)
        return resultados

    # ------------------------------------------------------------------ #
    # Persistencia (histórico: se acumula por ejecución)
    # ------------------------------------------------------------------ #
    def guardar(self, maquina_id: int,
                config: Optional[demand.DemandConfig] = None) -> int:
        recs = self.generar(maquina_id, config)
        if not recs:
            return 0
        filas = [{
            "maquina_id": r["maquina_id"],
            "product_id": r["product_id"],
            "seleccion": r["seleccion"],
            "consumo_promedio_diario": r["consumo_promedio_diario"],
            "consumo_promedio_7_dias": r["consumo_promedio_7_dias"],
            "consumo_promedio_14_dias": r["consumo_promedio_14_dias"],
            "consumo_promedio_30_dias": r["consumo_promedio_30_dias"],
            "tendencia": r["tendencia"],
            "stock_actual": r["stock_actual"],
            "dias_cobertura": r["dias_cobertura"],
            "stock_objetivo": r["stock_objetivo"],
            "margen_seguridad": r["margen_seguridad"],
            "cantidad_recomendada": r["cantidad_recomendada"],
            "motivo": r["motivo"],
        } for r in recs]
        return self.db.insert_many("replenishment_recommendations", filas)

    def guardar_para_maquinas(self, aplicar: bool = True,
                              config: Optional[demand.DemandConfig] = None) -> int:
        maquinas = self.db.query(
            "SELECT maquina_id FROM vendu.maquinas_epay ORDER BY maquina_id")
        total = 0
        for m in maquinas:
            if aplicar:
                total += self.guardar(m["maquina_id"], config)
        return total
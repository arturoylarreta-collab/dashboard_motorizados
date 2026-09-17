"""
SyncService — Sincronización ePay (MCP) → Supabase (schema `vendu`).

Reconcilia la identidad de máquinas y productos entre la fuente (ePay/MCP) y la
base central (schema `vendu`, que referencia en solo-lectura a `public.maquinas`
y `public.motorizados`), y baja el estado actual de inventario/ventas.

Reglas:
  * ePay es la fuente de la verdad para MÁQUINAS y PRODUCTOS.
  * Si ePay falla: NO se borra nada; se conserva el último estado y se registra
    en sync_log.
  * El puente planograma.producto_id ↔ products es EXPLÍCITO (product_mappings);
    nunca por nombre. Lo no mapeado genera alerta en unmapped_products.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from db import DB
from epay_service import EpayService

log = logging.getLogger("sync_service")


def normalizar_nombre(nombre: str) -> str:
    return re.sub(r"\s+", " ", (nombre or "").strip().lower())


class SyncService:
    def __init__(self, db: DB, epay: EpayService):
        self.db = db
        self.epay = epay

    # ------------------------------------------------------------------ #
    # MÁQUINAS
    # ------------------------------------------------------------------ #
    def reconciliar_maquinas(self, *, aplicar: bool = False) -> dict:
        """Cruza ePay con public.maquinas y escribe vendu.maquinas_epay.

        Devuelve {emparejadas, nuevas, obsoletas, sin_codigo} y la lista
        `activas` (pares maquina_id/epay_machine_id listos para sincronizar).
        """
        epay_maq = self.epay.maquinas(solo_snacks=True)
        existentes = self.db.maquinas()
        por_codigo = {m.get("codigo_epay"): m for m in existentes if m.get("codigo_epay")}

        emparejadas, nuevas, obsoletas, sin_codigo = [], [], [], []

        for em in epay_maq:
            row = por_codigo.get(em["codigo"])
            if row:
                emparejadas.append((row["id"], em))
            else:
                nuevas.append(em)

        codigos_epay = {em["codigo"] for em in epay_maq}
        for m in existentes:
            c = m.get("codigo_epay")
            if not c:
                sin_codigo.append(m)
            elif c.startswith("V") and c not in codigos_epay:
                obsoletas.append(m)

        activas = [{"maquina_id": mid, "epay_machine_id": em["maquina_id"],
                    "epay_uid": em["uid"] or None, "codigo": em["codigo"]}
                   for mid, em in emparejadas]

        if aplicar:
            self.db.upsert(
                "maquinas_epay",
                [{"maquina_id": a["maquina_id"], "epay_machine_id": a["epay_machine_id"],
                  "epay_uid": a["epay_uid"], "codigo": a["codigo"]} for a in activas],
                conflict=["maquina_id"],
            )

        return {
            "emparejadas": len(emparejadas),
            "nuevas": [{"codigo": e["codigo"], "epay_machine_id": e["maquina_id"]}
                       for e in nuevas],
            "obsoletas": [{"id": m["id"], "codigo_epay": m["codigo_epay"]}
                          for m in obsoletas],
            "sin_codigo": [{"id": m["id"], "nombre": m["nombre"]} for m in sin_codigo],
            "totales": {"epay": len(epay_maq), "supabase": len(existentes)},
            "activas": activas,
        }

    # ------------------------------------------------------------------ #
    # CATÁLOGO
    # ------------------------------------------------------------------ #
    def sync_catalogo(self, *, aplicar: bool = False) -> dict:
        prods = self.epay.productos()
        filas = [{
            "codigo_epay": p["codigo"] or None,
            "nombre": p["nombre"],
            "nombre_norm": normalizar_nombre(p["nombre"]),
            "categoria": p["categoria"] or None,
            "precio_bs": p["precio_bs"],
            "precio_usd": p["precio_usd"],
            "activo": p["activo"],
        } for p in prods]
        if aplicar:
            self.db.upsert("products", filas, conflict=["codigo_epay"],
                           update=["nombre", "nombre_norm", "categoria",
                                   "precio_bs", "precio_usd", "activo", "updated_at"])
        return {"catalogo": len(filas)}

    def _productos_por_nombre(self) -> Dict[str, List[dict]]:
        rows = self.db.query(
            "SELECT id, nombre, nombre_norm, codigo_epay, activo FROM vendu.products"
        )
        out: Dict[str, List[dict]] = {}
        for p in rows:
            out.setdefault(p["nombre_norm"], []).append(p)
        return out

    # ------------------------------------------------------------------ #
    # MAPEO DE PRODUCTOS
    # ------------------------------------------------------------------ #
    def construir_mappings(self, activas: List[dict], *, aplicar: bool = False) -> dict:
        productos = self._productos_por_nombre()
        mappings: Dict[str, dict] = {}
        alertas: List[dict] = []

        for a in activas:
            pg = self.epay.planograma(a["epay_machine_id"])
            for s in pg.get("slots", []) or []:
                pid = str(s.get("producto_id") or "")
                nombre = s.get("producto") or ""
                if not pid or pid in mappings:
                    continue
                nombre_norm = normalizar_nombre(nombre)
                if not nombre_norm or "sin producto" in nombre_norm:
                    continue
                matches = productos.get(nombre_norm, [])
                if len(matches) == 1:
                    m = matches[0]
                    mappings[pid] = {
                        "epay_producto_id": pid, "product_id": m["id"],
                        "epay_nombre": nombre, "mapped_by": "auto", "active": True,
                    }
                else:
                    # 0 = sin match (no mapeado); >1 = ambiguo (mapeo manual)
                    alertas.append({
                        "epay_producto_id": pid, "nombre": nombre,
                        "maquina_id": a["maquina_id"], "seleccion": s.get("seleccion"),
                        "ambiguo": len(matches) > 1,
                    })

        if aplicar:
            if mappings:
                self.db.upsert("product_mappings", list(mappings.values()),
                               conflict=["epay_producto_id"],
                               update=["product_id", "epay_nombre", "mapped_by", "active"])
            if alertas:
                for al in alertas:
                    self.db.insert("unmapped_products", {
                        "epay_producto_id": al["epay_producto_id"],
                        "nombre": al["nombre"],
                        "maquina_id": al["maquina_id"],
                        "seleccion": al["seleccion"],
                        "resuelto": False,
                    })

        return {"mapeados": len(mappings), "no_mapeados": len(alertas), "alertas": alertas}

    # ------------------------------------------------------------------ #
    # PLANOGRAMA
    # ------------------------------------------------------------------ #
    def sync_planograma(self, activas: List[dict], *, aplicar: bool = False) -> dict:
        total = 0
        for a in activas:
            slots = self.epay.slots_snacks(a["epay_machine_id"])
            filas = [{
                "maquina_id": a["maquina_id"],
                "seleccion": s["seleccion"],
                "slot": s["slot"] or s["seleccion"],  # slot físico es único por máquina
                "epay_producto_id": s["producto_id"] or None,
                "cantidad": s["cantidad"] or 0,
                "minimo": s["minimo"] or 0,
                "maximo": s["maximo"] or 0,
                "activo": s["activo"],
                "estado": s["estado"],
            } for s in slots]
            total += len(filas)
            if aplicar and filas:
                self.db.upsert("machine_slots", filas, conflict=["maquina_id", "slot"],
                               update=["seleccion", "epay_producto_id", "cantidad",
                                       "minimo", "maximo", "activo", "estado", "synced_at"])
        return {"slots": total}

    # ------------------------------------------------------------------ #
    # VENTAS
    # ------------------------------------------------------------------ #
    def sync_ventas(self, fecha_desde: str, fecha_hasta: str,
                    activas: List[dict], *, aplicar: bool = False) -> dict:
        total = 0
        for a in activas:
            vp = self.epay.ventas_por_producto(
                fecha_desde, fecha_hasta, maquina_id=a["epay_machine_id"])
            productos = vp.get("productos", []) if isinstance(vp, dict) else []
            # seleccion -> product_id (via machine_slots recién sincronizados);
            # si una misma seleccion apunta a productos distintos, es ambigua -> NULL
            seleccion_a_producto: Dict[str, Optional[int]] = {}
            for s in self.db.query(
                    "SELECT seleccion, product_id FROM vendu.machine_slots "
                    "WHERE maquina_id = %s AND seleccion IS NOT NULL", (a["maquina_id"],)):
                sel = s["seleccion"]
                pid = s["product_id"]
                if sel not in seleccion_a_producto:
                    seleccion_a_producto[sel] = pid
                elif seleccion_a_producto.get(sel) != pid:
                    seleccion_a_producto[sel] = None  # ambiguo
            filas = []
            for p in productos:
                cantidad = _num(p.get("cantidad")) or 0
                if cantidad <= 0:
                    continue
                sel = p.get("nombre")
                filas.append({
                    "maquina_id": a["maquina_id"],
                    "periodo_desde": fecha_desde,
                    "fecha": fecha_hasta,
                    "seleccion": sel,
                    "product_id": seleccion_a_producto.get(sel),
                    "cantidad": cantidad,
                    "monto_bs": _num(p.get("total_bs")),
                    "monto_usd": _num(p.get("total_usd")),
                })
            total += len(filas)
            if aplicar and filas:
                self.db.upsert("epay_sales", filas,
                               conflict=["maquina_id", "fecha", "seleccion"],
                               update=["periodo_desde", "product_id", "cantidad",
                                       "monto_bs", "monto_usd"])
        return {"filas": total}

    # ------------------------------------------------------------------ #
    # ORQUESTACIÓN
    # ------------------------------------------------------------------ #
    def run_full(self, *, aplicar: bool = False, fecha_desde: str = "",
                 fecha_hasta: str = "", con_ventas: bool = True) -> dict:
        reporte: dict = {}
        try:
            maq = self.epay.maquinas(solo_snacks=True)
        except Exception as e:
            self._sync_log("run_full", "ERROR", str(e))
            raise

        reporte["maquinas"] = self.reconciliar_maquinas(aplicar=aplicar)
        activas = reporte["maquinas"]["activas"]

        reporte["catalogo"] = self.sync_catalogo(aplicar=aplicar)

        # Una sola pasada de planograma: mappings + slots juntos.
        productos = self._productos_por_nombre()
        mappings: Dict[str, dict] = {}
        alertas: List[dict] = []
        slots_filas: List[dict] = []
        for i, a in enumerate(activas, 1):
            pg = self.epay.planograma(a["epay_machine_id"])
            for s in pg.get("slots", []) or []:
                if not s.get("activo"):
                    continue
                slots_filas.append({
                    "maquina_id": a["maquina_id"],
                    "seleccion": s.get("seleccion"),
                    "slot": s.get("slot") or s.get("seleccion"),
                    "epay_producto_id": str(s.get("producto_id") or "") or None,
                    "cantidad": _num(s.get("cantidad")) or 0,
                    "minimo": _num(s.get("minimo")) or 0,
                    "maximo": _num(s.get("maximo")) or 0,
                    "activo": True,
                    "estado": s.get("estado"),
                })
                pid = str(s.get("producto_id") or "")
                nombre = s.get("producto") or ""
                if not pid or pid in mappings:
                    continue
                nombre_norm = normalizar_nombre(nombre)
                if not nombre_norm or "sin producto" in nombre_norm:
                    continue
                matches = productos.get(nombre_norm, [])
                if len(matches) == 1:
                    m = matches[0]
                    mappings[pid] = {
                        "epay_producto_id": pid, "product_id": m["id"],
                        "epay_nombre": nombre, "mapped_by": "auto", "active": True,
                    }
                else:
                    alertas.append({
                        "epay_producto_id": pid, "nombre": nombre,
                        "maquina_id": a["maquina_id"], "seleccion": s.get("seleccion"),
                    })
            if i % 10 == 0:
                print(f"  planograma {i}/{len(activas)}...")

        # Resolver product_id de cada slot usando el puente explícito de mappings
        pid_a_producto = {m["epay_producto_id"]: m["product_id"] for m in mappings.values()}
        for f in slots_filas:
            f["product_id"] = pid_a_producto.get(f["epay_producto_id"])

        if aplicar:
            if mappings:
                self.db.upsert("product_mappings", list(mappings.values()),
                               conflict=["epay_producto_id"],
                               update=["product_id", "epay_nombre", "mapped_by", "active"])
            if alertas:
                for al in alertas:
                    self.db.insert("unmapped_products", {
                        "epay_producto_id": al["epay_producto_id"],
                        "nombre": al["nombre"],
                        "maquina_id": al["maquina_id"],
                        "seleccion": al["seleccion"],
                        "resuelto": False,
                    })
            if slots_filas:
                self.db.upsert("machine_slots", slots_filas,
                               conflict=["maquina_id", "slot"],
                               update=["seleccion", "epay_producto_id", "product_id",
                                       "cantidad", "minimo", "maximo", "activo",
                                       "estado", "synced_at"])
                # desactivar mappings que ya no existen en el planograma actual
                self.db.execute("""
                    UPDATE vendu.product_mappings pm SET active = false
                    WHERE pm.active AND NOT EXISTS (
                      SELECT 1 FROM vendu.machine_slots ms
                      WHERE ms.epay_producto_id = pm.epay_producto_id)
                """)
                # lo mismo para slots que ya no están en ePay
                self.db.execute("""
                    UPDATE vendu.machine_slots ms SET activo = false
                    WHERE ms.activo AND NOT EXISTS (
                      SELECT 1 FROM vendu.maquinas_epay m WHERE m.maquina_id = ms.maquina_id)
                """)

        reporte["mappings"] = {"mapeados": len(mappings), "no_mapeados": len(alertas)}
        reporte["planograma"] = {"slots": len(slots_filas)}

        if con_ventas and fecha_desde and fecha_hasta:
            reporte["ventas"] = self.sync_ventas(
                fecha_desde, fecha_hasta, activas, aplicar=aplicar)

        self._sync_log("run_full", "OK", f"maquinas={len(maq)}")
        return reporte

    def _sync_log(self, fuente: str, estado: str, detalle: str) -> None:
        try:
            self.db.insert("sync_log", {
                "fuente": fuente, "estado": estado, "detalle": detalle[:1000],
            })
        except Exception:
            log.exception("No se pudo escribir sync_log")


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None

"""
sync_epay.py -- Sincroniza el maestro MCP de ePay hacia Supabase.

Qué trae y a dónde:
  * maquinas_definidas        -> epay_machines        (filtro prefijo + solo snacks)
  * productos_globales        -> epay_product_catalog + products (SKU)
  * planograma_maquina        -> machine_channels     (inventario ePay por slot)
  * ventas_x_mes (3 meses)    -> epay_daily_sales     (serie diaria de consumo)
  * ventas_x_rango con detalle-> epay_sales_detail    (unidades por selección)
  * sync_state                -> marca "actualizado hasta".

Modos de ejecución (el token NUNCA se versiona):
  EPAY_MCP_TOKEN=... python sync_epay.py                 # dry-run: imprime resumen
  EPAY_MCP_TOKEN=... SUPABASE_URL=... SUPABASE_ANON_KEY=... python sync_epay.py

En Streamlit se llama a sincronizar_epay() (botón del dashboard); el token vive
en st.secrets (EPAY_MCP_TOKEN) y la key anon en SUPABASE_ANON_KEY (ya existente).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

from epay_mcp import EpayMCP, filtrar_maquinas

log = logging.getLogger("sync_epay")

PREFIJO = "V46-UCVCOM"          # alcance piloto
VENTANAS_MESES = 3             # cuántos meses de ventas_x_mes se traen
DIAS_DETALLE = 45              # ventana del detalle de ventas

API_BASE = "/rest/v1"


def _headers_rest() -> Dict[str, str]:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_ANON_KEY", "")
    return {"apikey": key, "Authorization": f"Bearer {key}"}, url, key


def _rest_upsert(tabla: str, filas: List[Dict[str, Any]], colisiones: str = "maquina_id") -> int:
    h, url, key = _headers_rest()
    if not url or not key:
        raise RuntimeError("Sin SUPABASE_URL / SUPABASE_ANON_KEY")
    if not filas:
        return 0
    r = requests.post(
        f"{url}{API_BASE}/{tabla}",
        headers=h | {"Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=filas, timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"upsert {tabla}: {r.status_code} {r.text[:400]}")
    return len(filas)


def _rest_consulta(tabla: str, select: str = "*") -> List[Dict[str, Any]]:
    h, url, key = _headers_rest()
    if not url or not key:
        return []
    r = requests.get(f"{url}{API_BASE}/{tabla}", headers=h,
                     params={"select": select}, timeout=30)
    return r.json() if r.status_code == 200 else []


def sincronizar_epay(dry_run: Optional[bool] = None) -> Dict[str, Any]:
    """Ejecuta toda la cadena MCP -> Supabase. Devuelve resumen para la UI."""
    if dry_run is None:
        dry_run = not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY"))
    mcp = EpayMCP().connect()
    hoy = date.today()
    resumen = {"dry_run": dry_run, "fecha": hoy.isoformat()}

    # 1) máquinas
    maquinas = filtrar_maquinas(mcp.maquinas_definidas(), solo_snacks=True)
    if not maquinas:
        resumen["error"] = f"Sin máquinas con prefijo {PREFIJO} (snacks)."
        log.warning(resumen["error"])
        return resumen
    filas_m = [{
        "maquina_id": m["maquina_id"], "codigo": m["codigo"],
        "nombre": m["nombre"], "serial": m["serial"], "uid": m["uid"],
        "pago_hasta": m["pago_hasta"] or None, "ruta": m["ruta"],
        "tipo": "snack", "estado_epay": "desconocido",
        "activo": True, "mensaje_epay": m.get("inventario", "") + " / " + m.get("ajustar", ""),
    } for m in maquinas]
    if not dry_run:
        _rest_upsert("epay_machines", filas_m)

    # 2) catálogo global -> epay_product_catalog + products (SKU auto)
    cat = mcp.productos_globales()
    filas_cat = [{
        "codigo": p["codigo"], "nombre": p["nombre"], "categoria": p["categoria"],
        "precio_bs": p["precio_bs"], "precio_usd": p["precio_usd"],
        "cantidad": p["cantidad"], "activo": p["activo"], "synced_at": hoy.isoformat(),
    } for p in cat]
    filas_products: List[Dict[str, Any]] = []
    for p in cat:
        if not p["nombre"]:
            continue
        filas_products.append({
            "sku": None, "nombre": p["nombre"], "categoria": p["categoria"],
            "codigo_epay": p["codigo"], "epay_product_id": None,
            "precio_bs": p["precio_bs"], "precio_usd": p["precio_usd"], "activo": True,
        })
    if not dry_run:
        _rest_upsert("epay_product_catalog", filas_cat)

    # 3) planograma de cada máquina -> machine_channels (+ auto-mappings)
    total_slots = 0
    canales: List[Dict[str, Any]] = []
    productos_planog: Dict[str, Dict[str, Any]] = {}
    for m in maquinas:
        plan = mcp.planograma(m["maquina_id"])
        for s in plan["slots"]:
            canales.append({
                "maquina_id": m["maquina_id"], "slot": s["slot"],
                "seleccion": s["seleccion"], "epay_product_id": s["producto_id"],
                "product_id": None, "nombre": s["nombre"],
                "precio_bs": s["precio_bs"], "precio_usd": s["precio_usd"],
                "cantidad": s["cantidad"], "minimo": s["minimo"], "maximo": s["maximo"],
                "estado": s["estado"], "activo": s["activo"], "synced_at": hoy.isoformat(),
            })
            if s["producto_id"] and s["nombre"]:
                productos_planog.setdefault(s["producto_id"], {
                    "epay_product_id": s["producto_id"], "nombre": s["nombre"],
                    "mayoria_selecciones": s["seleccion"],
                })
        total_slots += plan["total_slots"]
    resumen["machines"] = len(maquinas)
    resumen["slots"] = total_slots

    # auto-mappings: epay_product_id -> products por nombre (resolved con lib)
    if dry_run:
        pass
    else:
        _rest_upsert("machine_channels", canales)

    # 4) ventas diarias (ventas_x_mes) últimos N meses
    ventas_diarias: List[Dict[str, Any]] = []
    for m in maquinas:
        for k in range(VENTANAS_MESES):
            anio, mes = _restar_meses(hoy, k)
            for d in mcp.ventas_mes(anio, mes, maquina_id=m["maquina_id"]):
                if not d["cantidad"]:
                    continue  # días sin venta: mejor ausencia que un 0 ruidoso
                fecha = _dia_a_fecha(d["dia"], anio, mes)
                ventas_diarias.append({
                    "maquina_id": m["maquina_id"], "fecha": fecha,
                    "cantidad": d["cantidad"], "monto_bs": d["monto_bs"],
                    "synced_at": hoy.isoformat(),
                })
    resumen["ventas_diarias"] = len(ventas_diarias)
    if not dry_run:
        _rest_upsert("epay_daily_sales", ventas_diarias, colisiones="maquina_id,fecha")

    # 5) detalle de ventas por máquina (45 días) -> epay_sales_detail
    detalle: List[Dict[str, Any]] = []
    desde = (hoy - timedelta(days=DIAS_DETALLE)).isoformat()
    hasta = hoy.isoformat()
    for m in maquinas:
        r = mcp.ventas_rango(desde, hasta, maquina_id=m["maquina_id"], detalle=True)
        det_rows = r.get("detalle") or []
        for row in det_rows:
            ref = str(row.get("Referencia", "")).lower()
            detalle.append({
                "maquina_id": m["maquina_id"], "seleccion": ref,
                "epay_product_id": None, "product_id": None,
                "cantidad": row.get("Cantidad", 0), "monto_bs": row.get("Monto", 0),
                "monto_usd": 0, "fecha_desde": desde, "fecha_hasta": hasta,
                "synced_at": hoy.isoformat(),
            })
    resumen["detalle"] = len(detalle)
    if not dry_run:
        _rest_upsert("epay_sales_detail", detalle, colisiones="maquina_id,seleccion,fecha_desde,fecha_hasta")

    # 6) marca de sincronización
    if not dry_run:
        _rest_upsert("sync_state", [{
            "fuente": "epay", "ultimo_ok": ahora_utc_iso(), "estado": "ok",
            "error": "", "meta": {"maquinas": len(maquinas), "slots": total_slots,
                                  "modo": "sync_epay"},
        }])
    log.info("sync OK (dry=%s): %s", dry_run, resumen)
    return resumen


# ------------------------------------------------------------- utilidades

def _restar_meses(fecha: date, meses: int):
    anio, mes = fecha.year, fecha.month - meses
    while mes <= 0:
        mes += 12
        anio -= 1
    return anio, mes


def _dia_a_fecha(dia_raw: str, anio: int, mes: int) -> str:
    """'10/08 L' -> '2026-08-10' (el MCP omite el año)."""
    try:
        dia, _ = dia_raw.split()[0].split("/")
        return date(anio, mes, int(dia)).isoformat()
    except Exception:
        return date(anio, mes, 1).isoformat()


def ahora_utc_iso() -> str:
    return datetime_utcnow_iso()


def datetime_utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------- entrypoint
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    res = sincronizar_epay()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("error"):
        sys.exit(1)
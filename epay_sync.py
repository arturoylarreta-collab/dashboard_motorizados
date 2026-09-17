"""
epay_sync.py -- Lo que Vendu ESCRIBE en ePay, con registro propio (fase 2, 17-09-2026).

Reglas (Plan 1, secciones 2 y 4):
  * Recarga de máquina: al cerrar una orden, primero SIMULAR (`plan_recarga`),
    mostrarle el plan al usuario, y con su visto bueno ESCRIBIR
    (`escribir_recarga`) con `idempotencia = orden-<id>`. Si ePay falla, la
    orden NO se cierra. El `lote` que devuelve el MCP se guarda en la orden
    (vendu.replenishment_orders.epay_lote) y en vendu.epay_recargas, porque el
    log del MCP se borra en cada redespliegue de Render.
  * Almacén central: cada ENTRADA o CONTEO de la oficina se refleja en el
    almacén de ePay al instante (`cambiar_stock_almacen`, sumar/fijar). Si ePay
    no responde, queda PENDIENTE en vendu.epay_almacen_sync y se reintenta.
    Una recarga NUNCA toca el almacén de ePay: ePay ya lo descuenta al recargar.
  * Cuadre: almacén de ePay = oficina en Vendu + bolsos en ruta.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from db import DB
from epay_service import EpayError, EpayService
from inventory import InventoryService
from orders import OrderService
from recarga import repartir_por_slots


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class EpaySync:
    def __init__(self, db: DB, svc: EpayService):
        self.db = db
        self.svc = svc
        self.inv = InventoryService(db)

    # ------------------------------------------------------------------ #
    # Recarga de máquina (orden -> ePay)
    # ------------------------------------------------------------------ #
    def epay_id(self, maquina_id: int) -> int:
        """Id de la máquina en ePay (Codigo(sys)). Vendu usa public.maquinas.id, que
        es OTRO número; el puente está en vendu.maquinas_epay (lo llena el sync)."""
        fila = self.db.one(
            "SELECT epay_machine_id FROM vendu.maquinas_epay WHERE maquina_id = %s", (maquina_id,))
        if not fila or not fila.get("epay_machine_id"):
            fila = self.db.one(
                "SELECT epay_machine_id FROM public.maquinas WHERE id = %s", (maquina_id,))
        if not fila or not fila.get("epay_machine_id"):
            raise EpayError(
                f"La máquina {maquina_id} no está emparejada con ePay (sin epay_machine_id); "
                "corre la sincronización primero.")
        return int(fila["epay_machine_id"])

    def cambios_de_orden(self, orden_id: int, colocadas: Dict[str, float]) -> List[Dict[str, Any]]:
        """Convierte lo colocado por producto en cambios por canal (`sumar` = unidades
        colocadas en ese canal), con el mismo reparto por slot que usa Vendu."""
        orden = OrderService(self.db).obtener(orden_id)
        reparto = repartir_por_slots(self.db, orden["maquina_id"], [
            {"product_id": int(pid), "cantidad": float(c)} for pid, c in colocadas.items() if float(c) > 0])
        cambios: Dict[str, int] = {}
        for linea in reparto:
            unidades = int(round(float(linea["cantidad"])))
            if unidades > 0:
                cambios[str(linea["slot"])] = cambios.get(str(linea["slot"]), 0) + unidades
        return [{"canal": canal, "sumar": n} for canal, n in cambios.items()]

    def plan_recarga(self, orden_id: int, colocadas: Dict[str, float],
                     usuario: Optional[str] = None) -> Dict[str, Any]:
        """Simulación en ePay (no escribe). Guarda el plan en la orden."""
        orden = OrderService(self.db).obtener(orden_id)
        cambios = self.cambios_de_orden(orden_id, colocadas)
        if not cambios:
            raise EpayError("La orden no tiene unidades que colocar (o los canales no tienen capacidad).")
        try:
            plan = self.svc.recargar_maquina(self.epay_id(orden["maquina_id"]), cambios, confirmar=False,
                                             nota=f"Simulación orden {orden_id}")
        except EpayError as e:
            self._registrar_recarga(orden, None, "ERROR", cambios, None, str(e), usuario)
            raise
        self._registrar_recarga(orden, None, "SIMULACION", cambios, plan, None, usuario)
        self.db.execute("UPDATE vendu.replenishment_orders SET epay_plan = %s::jsonb WHERE id = %s",
                        (_json(plan), orden_id))
        return {"cambios": cambios, "plan": plan}

    def escribir_recarga(self, orden_id: int, colocadas: Dict[str, float],
                         usuario: Optional[str] = None) -> Dict[str, Any]:
        """Escribe la recarga en ePay (idempotente por orden). Devuelve lote y canales ok."""
        orden = OrderService(self.db).obtener(orden_id)
        cambios = self.cambios_de_orden(orden_id, colocadas)
        if not cambios:
            raise EpayError("La orden no tiene unidades que colocar.")
        clave = f"orden-{orden_id}"
        nota = f"Orden {orden_id} · máquina {orden['maquina_id']} · motorizado {orden.get('motorizado_id') or '-'} · {usuario or 'dashboard'}"
        try:
            res = self.svc.recargar_maquina(self.epay_id(orden["maquina_id"]), cambios, confirmar=True,
                                            idempotencia=clave, nota=nota)
        except EpayError as e:
            self._registrar_recarga(orden, None, "ERROR", cambios, None, str(e), usuario, clave)
            raise
        modo = str(res.get("modo", ""))
        lote = res.get("lote")
        estado = "YA_ESCRITO" if "YA ESCRITO" in modo.upper() else "ESCRITO"
        detalle = res.get("detalle") or []
        fallidos = [d for d in detalle if d.get("ok") is False]
        canales_ok = {str(d.get("canal")) for d in detalle if d.get("ok") is not False}
        self._registrar_recarga(orden, lote, estado, cambios, res, None, usuario, clave)
        self.db.execute(
            "UPDATE vendu.replenishment_orders SET epay_lote = %s, epay_escrito_en = now() WHERE id = %s",
            (lote, orden_id))
        if fallidos:
            raise EpayError(
                f"ePay rechazó {len(fallidos)} canal(es): "
                + "; ".join(f"canal {d.get('canal')}: {d.get('error') or d.get('leido')}" for d in fallidos)
                + f". Lote {lote}: se puede revertir desde la orden.")
        return {"lote": lote, "modo": modo, "estado": estado, "detalle": detalle,
                "canales_ok": canales_ok, "cambios": cambios}

    def revertir_recarga(self, orden_id: int, usuario: Optional[str] = None) -> Dict[str, Any]:
        orden = OrderService(self.db).obtener(orden_id)
        lote = orden.get("epay_lote")
        if not lote:
            raise EpayError("La orden no tiene lote de ePay que revertir.")
        res = self.svc.revertir_recarga(lote, confirmar=True)
        self._registrar_recarga(orden, lote, "REVERTIDO", [], res, None, usuario)
        return res

    def _registrar_recarga(self, orden: dict, lote: Optional[str], modo: str, cambios: Any,
                           respuesta: Any, error: Optional[str], usuario: Optional[str],
                           idempotencia: Optional[str] = None) -> None:
        self.db.execute(
            "INSERT INTO vendu.epay_recargas (orden_id, maquina_id, lote, modo, idempotencia, cambios, respuesta, error, usuario) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)",
            (orden["id"], orden["maquina_id"], lote, modo, idempotencia, _json(cambios),
             _json(respuesta) if respuesta is not None else None, error, usuario))

    # ------------------------------------------------------------------ #
    # Almacén central (oficina -> almacén de ePay)
    # ------------------------------------------------------------------ #
    def _codigo_epay(self, product_id: int) -> str:
        p = self.db.one("SELECT codigo_epay, nombre FROM vendu.products WHERE id = %s", (product_id,))
        if not p or not p.get("codigo_epay"):
            raise EpayError(f"El producto {product_id} no tiene código de ePay; no se puede sincronizar el almacén.")
        return str(p["codigo_epay"])

    def registrar_entrada(self, *, product_id: int, cantidad: float, usuario: Optional[str] = None,
                          nota: Optional[str] = None, idempotency_key: Optional[str] = None,
                          escribir_epay: bool = True, proveedor_id: Optional[int] = None) -> Dict[str, Any]:
        """ENTRADA_COMPRA en Vendu + `sumar` en el almacén de ePay (una sola vez por movimiento).
        `proveedor_id`: de quién vino la mercancía (vendu.proveedores, SQL 12)."""
        mov = self.inv.entrada_compra(product_id=product_id, cantidad=cantidad, usuario=usuario,
                                      idempotency_key=idempotency_key)
        if proveedor_id and mov and mov.get("id"):
            self.db.execute("UPDATE vendu.inventory_movements SET proveedor_id = %s WHERE id = %s AND proveedor_id IS NULL",
                            (proveedor_id, mov["id"]))
            mov["proveedor_id"] = proveedor_id
        sync = None
        if escribir_epay:
            sync = self.sincronizar_almacen(movimiento_id=mov["id"], product_id=product_id,
                                            modo="sumar", cantidad=cantidad, usuario=usuario, nota=nota)
        return {"movimiento": mov, "epay": sync}

    def conteo_oficina(self, *, product_id: int, contado: float, usuario: Optional[str] = None,
                       nota: Optional[str] = None, escribir_epay: bool = True) -> Dict[str, Any]:
        """Conteo físico de la oficina: AJUSTE en Vendu hasta `contado` y `fijar` en ePay
        con contado + lo que hay en los bolsos en ruta (regla de cuadre)."""
        principal = self.inv.ubicacion_principal()
        fila = self.db.one(
            "SELECT COALESCE(quantity, 0) AS q FROM vendu.inventory_balances WHERE location_id = %s AND product_id = %s",
            (principal, product_id)) or {"q": 0}
        saldo = float(fila["q"] or 0)
        delta = round(float(contado) - saldo, 2)
        mov = None
        if abs(delta) > 1e-9:
            mov = self.inv.mover(
                tipo="AJUSTE", product_id=product_id, cantidad=abs(delta),
                origen_id=principal if delta < 0 else None,
                destino_id=principal if delta > 0 else None,
                usuario=usuario, observacion=f"Conteo de oficina: {saldo:g} -> {contado:g}. {nota or ''}".strip(),
                idempotency_key=None)
        bolsos = float(self.db.one(
            "SELECT COALESCE(SUM(b.quantity), 0) AS q FROM vendu.inventory_balances b "
            "JOIN vendu.inventory_locations l ON l.id = b.location_id "
            "WHERE l.tipo = 'MOTORIZADO' AND b.product_id = %s", (product_id,))["q"] or 0)
        sync = None
        if escribir_epay:
            sync = self.sincronizar_almacen(movimiento_id=mov["id"] if mov else None, product_id=product_id,
                                            modo="fijar", cantidad=float(contado) + bolsos,
                                            usuario=usuario, nota=nota or "Conteo de oficina")
        return {"saldo_anterior": saldo, "contado": contado, "ajuste": delta, "bolsos_en_ruta": bolsos,
                "movimiento": mov, "epay": sync}

    def sincronizar_almacen(self, *, movimiento_id: Optional[int], product_id: int, modo: str,
                            cantidad: float, usuario: Optional[str] = None,
                            nota: Optional[str] = None) -> Dict[str, Any]:
        codigo = self._codigo_epay(product_id)
        fila_id = self.db.insert("epay_almacen_sync", {
            "movimiento_id": movimiento_id, "product_id": product_id, "producto_epay": codigo,
            "modo": modo, "cantidad": cantidad, "estado": "PENDIENTE", "usuario": usuario})
        return self._intentar_sync(fila_id, codigo, modo, cantidad, nota)

    def _intentar_sync(self, fila_id: int, codigo: str, modo: str, cantidad: float,
                       nota: Optional[str]) -> Dict[str, Any]:
        try:
            res = self.svc.cambiar_stock_almacen(codigo, confirmar=True, nota=nota,
                                                 **({"sumar": cantidad} if modo == "sumar" else {"fijar": cantidad}))
            self.db.execute(
                "UPDATE vendu.epay_almacen_sync SET estado = 'OK', respuesta = %s::jsonb, error = NULL, "
                "intentos = intentos + 1, updated_at = now() WHERE id = %s", (_json(res), fila_id))
            return {"id": fila_id, "estado": "OK", "respuesta": res}
        except EpayError as e:
            self.db.execute(
                "UPDATE vendu.epay_almacen_sync SET estado = 'ERROR', error = %s, intentos = intentos + 1, "
                "updated_at = now() WHERE id = %s", (str(e)[:800], fila_id))
            return {"id": fila_id, "estado": "ERROR", "error": str(e)}

    def pendientes_almacen(self) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT s.*, p.nombre AS producto FROM vendu.epay_almacen_sync s "
            "JOIN vendu.products p ON p.id = s.product_id "
            "WHERE s.estado IN ('PENDIENTE', 'ERROR') ORDER BY s.created_at")

    def reintentar_pendientes(self) -> Dict[str, int]:
        ok = err = 0
        for fila in self.pendientes_almacen():
            r = self._intentar_sync(fila["id"], fila["producto_epay"], fila["modo"], float(fila["cantidad"]), None)
            ok += r["estado"] == "OK"
            err += r["estado"] != "OK"
        return {"ok": ok, "error": err}

    # ------------------------------------------------------------------ #
    # Cuadre oficina + bolsos vs almacén de ePay
    # ------------------------------------------------------------------ #
    def comparar_almacen(self) -> List[Dict[str, Any]]:
        vendu = self.db.query(
            "SELECT p.id AS product_id, p.codigo_epay, p.nombre, "
            "  COALESCE(SUM(CASE WHEN l.tipo = 'PRINCIPAL' THEN b.quantity END), 0) AS oficina, "
            "  COALESCE(SUM(CASE WHEN l.tipo = 'MOTORIZADO' THEN b.quantity END), 0) AS bolsos "
            "FROM vendu.products p "
            "LEFT JOIN vendu.inventory_balances b ON b.product_id = p.id "
            "LEFT JOIN vendu.inventory_locations l ON l.id = b.location_id AND l.tipo IN ('PRINCIPAL', 'MOTORIZADO') "
            "WHERE p.codigo_epay IS NOT NULL GROUP BY p.id, p.codigo_epay, p.nombre")
        epay = {str(r.get("codigo")): r for r in self.svc.stock_almacen(solo_activos=False)}
        filas = []
        for v in vendu:
            e = epay.get(str(v["codigo_epay"]))
            stock_epay = float(e.get("stock") or 0) if e else None
            total = float(v["oficina"] or 0) + float(v["bolsos"] or 0)
            if stock_epay is None and total == 0:
                continue
            filas.append({
                "product_id": v["product_id"], "codigo_epay": v["codigo_epay"], "producto": v["nombre"],
                "oficina_vendu": float(v["oficina"] or 0), "bolsos_en_ruta": float(v["bolsos"] or 0),
                "total_vendu": total, "almacen_epay": stock_epay,
                "diferencia": (round(total - stock_epay, 2) if stock_epay is not None else None),
                "estado": ("SIN_EPAY" if stock_epay is None else ("OK" if abs(total - stock_epay) < 0.5 else "DIFERENCIA")),
            })
        filas.sort(key=lambda f: (f["estado"] == "OK", -(abs(f["diferencia"] or 0))))
        return filas

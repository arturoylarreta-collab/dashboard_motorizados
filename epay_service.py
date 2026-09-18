"""
EpayService — Cliente del MCP de ePay.uno (fuente única de datos).

Reemplaza la lógica de scraping de `epay_scraper.py` para TODO lo que no sea
estatus: catálogo, planograma por máquina, inventario, ventas y consumo.
(El scraper se conserva únicamente como fallback de estatus online/offline.)

Protocolo MCP (Streamable HTTP, respuestas SSE):
  1. POST "initialize" -> el header `mcp-session-id` queda en la respuesta.
  2. POST "notifications/initialized" (mismo session-id).
  3. En adelante cada `tools/call` incluye ese `mcp-session-id` + el token.

Configuración (NUNCA en el repo; en st.secrets o variables de entorno):
  MCP_URL    = servidor del MCP de Vendu en Railway + MCP_PATH (desde 18-09-2026; antes Render)
               (epayuno-mcp-production.up.railway.app/<MCP_PATH>; la ruta y los tokens
               están en las variables del servicio en Railway)
               El MCP de Neptuno (aeropuerto) vive en
               epay-neptuno-mcp-production.up.railway.app (NEPTUNO_MCP_URL / NEPTUNO_MCP_TOKEN).
  MCP_TOKEN  = token de lectura (53 tools) o de escritura (57 tools), Authorization: Bearer

Garantías:
  * Re-intenta con backoff ante timeouts/errores de red.
  * Re-establece la sesión MCP si expira (el server puede invalidar el id).
  * Normaliza las respuestas a dicts/listas limpias (sin HTML ni claves raras).
  * Filtra la flota operativa: solo máquinas de SNACKS con código `V` + número
    (V01..V79). Se excluyen café (C), disponibles (A/D), "VENDU NUEVA", etc.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("epay_service")

# Máquinas de snacks: código que empieza por 'V' seguido de dígitos.
_RE_SNACK = re.compile(r"^V\d", re.IGNORECASE)

# Keys que devuelve `maquinas_definidas` en el reporte web.
_K_CODIGO = "Codigo(sys)"
_K_NOMBRE = "Nombre"
_K_INVENTARIO = "Inventario"


def _sse_payload(text: str) -> str:
    """Une todas las líneas `data:` de una respuesta SSE en un solo JSON."""
    return "\n".join(
        line[5:].strip()
        for line in text.splitlines()
        if line.startswith("data:")
    )


class EpayError(Exception):
    """Error de negocio o de protocolo del servicio ePay."""


class EpayService:
    """Cliente del MCP ePay.uno. Reutiliza una `requests.Session` persistente."""

    DEFAULT_TIMEOUT = (30, 180)  # (connect, read) en segundos
    MAX_INTENTOS = 3

    def __init__(self, url: str, token: str, timeout: tuple = DEFAULT_TIMEOUT):
        if not url or not token:
            raise EpayError("MCP_URL y MCP_TOKEN son obligatorios.")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()
        self._session_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Protocolo MCP
    # ------------------------------------------------------------------ #
    def _headers(self, with_session: bool) -> Dict[str, str]:
        h = {
            "x-epay-token": self.token,
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if with_session and self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    def _post(self, body: Dict[str, Any], with_session: bool) -> requests.Response:
        return self._session.post(
            self.url,
            headers=self._headers(with_session),
            data=json.dumps(body),
            timeout=self.timeout,
        )

    def initialize(self) -> None:
        """Establece (o re-establece) la sesión MCP. Idempotente."""
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "vendu-dashboard", "version": "1"},
                },
            },
            with_session=False,
        )
        sid = resp.headers.get("mcp-session-id")
        if not sid:
            raise EpayError("El MCP no devolvió 'mcp-session-id' en initialize.")
        self._session_id = sid
        # Notificación de arranque (no espera respuesta).
        try:
            self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                with_session=True,
            )
        except requests.RequestException:
            # No es bloqueante: algunos servers no la exigen.
            pass

    def call(self, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Llama una tool del MCP y devuelve su resultado parseado.

        Reintenta ante errores de red (timeouts) y re-establece la sesión si el
        server la invalida. Levanta EpayError si la tool falla o el resultado
        viene marcado como error.
        """
        arguments = arguments or {}
        ultimo_error: Optional[Exception] = None

        for intento in range(1, self.MAX_INTENTOS + 1):
            try:
                if self._session_id is None:
                    self.initialize()
                resp = self._post(
                    {
                        "jsonrpc": "2.0",
                        "id": 1000 + intento,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
                    with_session=True,
                )
                # El MCP responde UTF-8; `resp.text` adivinaba latin-1 y rompía los acentos.
                payload = json.loads(_sse_payload(resp.content.decode("utf-8", "replace")))
                result = payload.get("result") or {}
                if result.get("isError"):
                    raise EpayError(
                        f"Tool '{tool}' devolvió error: {result}"
                    )
                return self._normalize_result(result)
            except (requests.RequestException, json.JSONDecodeError) as e:
                ultimo_error = e
                log.warning(
                    "[epay] intento %d/%d en '%s' falló: %s",
                    intento, self.MAX_INTENTOS, tool, e,
                )
                self._session_id = None  # forzar re-initialize
                if intento < self.MAX_INTENTOS:
                    time.sleep(1.5 * intento)  # backoff 1.5s, 3s
        raise EpayError(f"Tool '{tool}' agotó reintentos: {ultimo_error}")

    @staticmethod
    def _normalize_result(result: Dict[str, Any]) -> Any:
        """Extrae el contenido útil de una respuesta MCP.

        El resultado puede venir en `structuredContent` o como lista de
        bloques `content` (type=text) con JSON dentro.
        """
        if "structuredContent" in result and result["structuredContent"] is not None:
            return result["structuredContent"]
        content = result.get("content") or []
        parsed = []
        for c in content:
            if c.get("type") != "text":
                continue
            text = c.get("text", "")
            try:
                parsed.append(json.loads(text))
            except json.JSONDecodeError:
                parsed.append(text)
        if len(parsed) == 1:
            return parsed[0]
        return parsed

    # ------------------------------------------------------------------ #
    # Métodos de negocio normalizados
    # ------------------------------------------------------------------ #
    def maquinas(self, solo_snacks: bool = True) -> List[Dict[str, Any]]:
        """Lista de máquinas normalizada (maquinas_definidas).

        Campos: maquina_id(int), codigo, nombre, uid, ruta, pago_hasta,
        inventario (int de canales bajos, o None si "OK"), es_snack(bool).
        """
        raw = self.call("maquinas_definidas")
        datos = raw.get("datos", []) if isinstance(raw, dict) else []
        filas = []
        for r in datos:
            codigo_raw = str(r.get(_K_CODIGO, "") or "")
            nombre = str(r.get(_K_NOMBRE, "") or "")
            m = re.search(r"\((\d+)\)\s*$", codigo_raw)
            maquina_id = int(m.group(1)) if m else None
            codigo = codigo_raw[: m.start()].strip() if m else codigo_raw.strip()
            uid = ""
            um = re.search(r"U(\d+)", nombre)
            if um:
                uid = "U" + um.group(1)
            inv = str(r.get(_K_INVENTARIO, "") or "").strip()
            inventario_bajo = None
            if inv and inv.upper() != "OK" and inv.isdigit():
                inventario_bajo = int(inv)
            es_snack = bool(_RE_SNACK.match(codigo))
            filas.append({
                "maquina_id": maquina_id,
                "codigo": codigo,
                "nombre": nombre,
                "uid": uid,
                "ruta": str(r.get("Ruta", "") or "").strip(),
                "pago_hasta": str(r.get("Pago Hasta", "") or "").strip(),
                "inventario_bajo": inventario_bajo,
                "es_snack": es_snack,
            })
        if solo_snacks:
            filas = [f for f in filas if f["es_snack"]]
        return filas

    def planograma(self, maquina_id: int) -> Dict[str, Any]:
        """Planograma de UNA máquina: slots con producto, stock, min/max."""
        raw = self.call("planograma_maquina", {"maquina_id": str(maquina_id)})
        if isinstance(raw, dict) and "slots" in raw:
            return raw
        return raw

    def productos(self) -> List[Dict[str, Any]]:
        """Catálogo global de productos (código, nombre, categoría, precios)."""
        raw = self.call("productos_globales")
        datos = raw.get("datos", []) if isinstance(raw, dict) else []
        out = []
        for r in datos:
            out.append({
                "codigo": str(r.get("Codigo", "") or ""),
                "nombre": str(r.get("Nombre", "") or ""),
                "categoria": str(r.get("Categoria", "") or ""),
                "precio_bs": _num(r.get("Precio")),
                "precio_usd": _num(r.get("Precio USD")),
                "cantidad": _num(r.get("Cantidad")),
                "activo": str(r.get("Activo", "") or "").strip().lower() == "activo",
            })
        return out

    def inventario_bajo(self, maquina_id: Optional[int] = None) -> Any:
        if maquina_id is not None:
            return self.call("inventario_bajo", {"maquina_id": str(maquina_id)})
        return self.call("inventario_bajo")

    def estatus_maquinas(self, filtro: str = "offline") -> Any:
        return self.call("estatus_maquinas", {"filtro": filtro})

    def ventas_por_maquina(self, fecha_desde: str, fecha_hasta: str,
                           maquina_id: Optional[int] = None,
                           moneda: str = "bs") -> Any:
        args = {"fecha_desde": fecha_desde, "fecha_hasta": fecha_hasta,
                "moneda": moneda}
        if maquina_id is not None:
            args["maquina_id"] = str(maquina_id)
        return self.call("ventas_por_maquina", args)

    def ventas_por_producto(self, fecha_desde: str, fecha_hasta: str,
                            maquina_id: Optional[int] = None,
                            ubicacion: Optional[str] = None,
                            categoria: Optional[str] = None) -> Any:
        args = {"fecha_desde": fecha_desde, "fecha_hasta": fecha_hasta}
        if maquina_id is not None:
            args["maquina_id"] = str(maquina_id)
        if ubicacion:
            args["ubicacion"] = ubicacion
        if categoria:
            args["categoria"] = categoria
        return self.call("ventas_por_producto", args)

    def ventas_por_ubicacion(self, fecha_desde: str, fecha_hasta: str,
                             ubicacion: Optional[str] = None) -> Any:
        args = {"fecha_desde": fecha_desde, "fecha_hasta": fecha_hasta}
        if ubicacion:
            args["ubicacion"] = ubicacion
        return self.call("ventas_por_ubicacion", args)

    def ventas_de_producto(self, producto: str, fecha_desde: str, fecha_hasta: str,
                           desglose: Optional[str] = None,
                           maquina_id: Optional[int] = None) -> Any:
        args = {"producto": producto, "fecha_desde": fecha_desde,
                "fecha_hasta": fecha_hasta}
        if desglose:
            args["desglose"] = desglose
        if maquina_id is not None:
            args["maquina_id"] = str(maquina_id)
        return self.call("ventas_de_producto", args)

    def listar_ubicaciones(self, fecha_desde: Optional[str] = None,
                           fecha_hasta: Optional[str] = None) -> Any:
        args = {}
        if fecha_desde:
            args["fecha_desde"] = fecha_desde
        if fecha_hasta:
            args["fecha_hasta"] = fecha_hasta
        return self.call("listar_ubicaciones", args)

    # ------------------------------------------------------------------ #
    # Helpers para el motor de recarga
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # Tools de inventario del MCP de Vendu (17-09-2026). Las de escritura
    # (recargar_maquina, revertir_recarga, cambiar_stock_almacen) SIMULAN por
    # defecto (confirmar=False) y solo existen con el token de escritura.
    # Contrato: vault EPAYUNO/HANDOFF-DASHBOARD-tools-MCP-epayuno-2026-09-17.
    # ------------------------------------------------------------------ #
    def resumen_recarga_maquinas(self, maquina_ids: List[int], dias: int = 14) -> Any:
        """Stock, faltante, ventas/día y cobertura por canal de hasta 60 máquinas (1 llamada)."""
        return self.call("resumen_recarga_maquinas",
                         {"maquina_ids": [str(m) for m in maquina_ids], "dias": int(dias)})

    def recargar_maquina(self, maquina_id: int, cambios: List[Dict[str, Any]], *,
                         confirmar: bool = False, idempotencia: Optional[str] = None,
                         nota: Optional[str] = None, forzar: bool = False) -> Dict[str, Any]:
        """Registra en ePay la recarga (o el conteo) por canal.

        cambios: [{"canal": "11", "sumar": 3}] (unidades colocadas) o
                 [{"canal": "11", "fijar": 8}] (conteo final). Enteros.
        confirmar=False -> simulación (plan + efecto en el almacén), no escribe.
        confirmar=True  -> escribe, relee y devuelve {modo, lote, detalle[], revertir}.
        """
        args: Dict[str, Any] = {"maquina_id": str(maquina_id), "cambios": cambios,
                                "confirmar": bool(confirmar), "forzar": bool(forzar)}
        if idempotencia:
            args["idempotencia"] = idempotencia
        if nota:
            args["nota"] = nota
        raw = self.call("recargar_maquina", args)
        return raw if isinstance(raw, dict) else {"resultado": raw}

    def revertir_recarga(self, lote: str, *, confirmar: bool = False) -> Dict[str, Any]:
        raw = self.call("revertir_recarga", {"lote": lote, "confirmar": bool(confirmar)})
        return raw if isinstance(raw, dict) else {"resultado": raw}

    def historial_recargas(self, maquina_id: Optional[int] = None, limite: int = 20) -> Any:
        args: Dict[str, Any] = {"limite": int(limite)}
        if maquina_id:
            args["maquina_id"] = str(maquina_id)
        return self.call("historial_recargas", args)

    def stock_almacen(self, texto: Optional[str] = None, solo_negativos: bool = False,
                      solo_activos: bool = True) -> List[Dict[str, Any]]:
        """Almacén central de ePay (campo Cantidad por producto)."""
        args: Dict[str, Any] = {"solo_negativos": bool(solo_negativos),
                                "solo_activos": bool(solo_activos)}
        if texto:
            args["texto"] = texto
        raw = self.call("stock_almacen", args)
        return raw.get("datos", []) if isinstance(raw, dict) else []

    def cambiar_stock_almacen(self, producto: str, *, sumar: Optional[float] = None,
                              fijar: Optional[float] = None, confirmar: bool = False,
                              nota: Optional[str] = None) -> Dict[str, Any]:
        """Entrada de mercancía (sumar) o conteo (fijar) del almacén de ePay.
        NUNCA para una recarga: ePay ya descuenta al recargar la máquina."""
        if (sumar is None) == (fijar is None):
            raise EpayError("Indica exactamente uno: sumar o fijar.")
        args: Dict[str, Any] = {"producto": str(producto), "confirmar": bool(confirmar)}
        if sumar is not None:
            args["sumar"] = float(sumar)
        else:
            args["fijar"] = float(fijar)
        if nota:
            args["nota"] = nota
        raw = self.call("cambiar_stock_almacen", args)
        return raw if isinstance(raw, dict) else {"resultado": raw}

    def movimientos_producto(self, producto: str, tipo: Optional[str] = None,
                             maquina_id: Optional[int] = None, sin_ventas: bool = True) -> Any:
        """Últimos movimientos de un producto en ePay (VE venta, TI recarga, cambios de canal)."""
        args: Dict[str, Any] = {"producto": str(producto), "sin_ventas": bool(sin_ventas)}
        if tipo:
            args["tipo"] = tipo
        if maquina_id:
            args["maquina_id"] = str(maquina_id)
        return self.call("movimientos_producto", args)

    def slots_snacks(self, maquina_id: int) -> List[Dict[str, Any]]:
        """Slots activos de una máquina snack, normalizados para el motor.

        Devuelve por slot: seleccion, slot, producto_id, producto, cantidad,
        minimo, maximo, estado. Omite slots inactivos.
        """
        pg = self.planograma(maquina_id)
        slots = pg.get("slots", []) if isinstance(pg, dict) else []
        return [
            {
                "seleccion": s.get("seleccion"),
                "slot": s.get("slot"),
                "producto_id": str(s.get("producto_id", "") or ""),
                "producto": s.get("producto"),
                "cantidad": _num(s.get("cantidad")),
                "minimo": _num(s.get("minimo")),
                "maximo": _num(s.get("maximo")),
                "estado": s.get("estado"),
                "activo": bool(s.get("activo")),
            }
            for s in slots
            if s.get("activo")
        ]


def _num(v: Any) -> Optional[float]:
    """Convierte un valor con comas/signo a float; None si no es numérico."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_service() -> EpayService:
    """Construye el EpayService desde st.secrets (Streamlit) o el entorno."""
    try:
        import streamlit as st

        url = st.secrets.get("MCP_URL", "")
        token = st.secrets.get("MCP_TOKEN", "")
    except Exception:
        url = token = ""
    if not url:
        import os

        url = os.getenv("MCP_URL", "")
        token = os.getenv("MCP_TOKEN", "")
    return EpayService(url, token)

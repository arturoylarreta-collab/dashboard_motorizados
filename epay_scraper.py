"""
Estatus online/offline de las máquinas en ePay.uno.

Misma lógica que la tool `estatus_maquinas` del MCP ePay.uno (cuenta principal
Vendu, C:\\EPAYUNO\\epayuno-mcp\\src\\api-client.ts):

  1. Login automático: POST /login.php con user, pass, location=/cliente.php y
     enviar=Entrar (el portal exige isset($_POST['enviar'])). La cookie
     PHPSESSID queda en la sesión de requests; re-login automático si expira.
  2. GET /reportes.php, sección "Estatus equipos". Cada máquina es un
     <td class="col-md-1 btn-danger|btn-success"> CODIGO <a href="maquinas.php?id=N">
     ... <small>NOMBRE</small>. btn-success = reportando hace < 1 h (verde);
     btn-danger = offline (rojo).

Credenciales: st.secrets["EPAY_USER"] / ["EPAY_PASS"] (Streamlit Cloud →
Settings → Secrets). NUNCA en el repo. Alternativa: EPAY_PHPSESSID manual.
"""

from __future__ import annotations

import html
import re
import time
from typing import Optional

import requests

BASE_URL = "https://epay.uno"
LOGIN_URL = f"{BASE_URL}/login.php"
ESTATUS_URL = f"{BASE_URL}/reportes.php"
UA = "Mozilla/5.0 (VenduDashboard/1.0; +epay-estatus)"
TIMEOUT = 20

# Regex idéntico al del MCP (webEstatusEquipos)
_RE_ESTATUS = re.compile(
    r'<td class="col-md-1 (btn-danger|btn-success)">\s*([^<]*?)\s*'
    r'<a href="maquinas\.php\?id=(\d+)"[\s\S]*?<small>([\s\S]*?)</small>',
    re.IGNORECASE,
)


def _es_pagina_login(texto: str) -> bool:
    low = texto.lower()
    return 'type="password"' in low and "login.php" in low


def _login(session: requests.Session, user: str, password: str) -> bool:
    """Inicia sesión y deja el PHPSESSID autenticado en la sesión. No imprime la clave."""
    try:
        # 1. GET para sembrar el PHPSESSID
        session.get(LOGIN_URL, timeout=TIMEOUT, allow_redirects=False)
        # 2. POST de credenciales
        resp = session.post(
            LOGIN_URL,
            data={
                "user": user,
                "pass": password,
                "location": "/cliente.php",
                "enviar": "Entrar",
            },
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        print(f"[epay] error de red en login: {e}")
        return False

    if 300 <= resp.status_code < 400:
        return True
    if resp.status_code == 200:
        return not _es_pagina_login(resp.text)
    return False


def _parsear_estatus(html_txt: str) -> dict:
    """Devuelve {codigo: {...}} a partir del HTML de reportes.php."""
    resultado: dict[str, dict] = {}
    for clase, codigo, maquina_id, nombre in _RE_ESTATUS.findall(html_txt):
        codigo = html.unescape(codigo).strip()
        nombre = html.unescape(nombre).strip()
        estado = "ACTIVA" if clase.lower() == "btn-success" else "INACTIVA"
        info = {
            "codigo": codigo,
            "maquina_id": maquina_id,
            "estado": estado,
            "descripcion": nombre,
            "color_badge": "🟢" if estado == "ACTIVA" else "🔴",
        }
        # Clave principal: el código (V07-CASH17). Si viene vacío, el nombre.
        resultado[codigo or nombre] = info
    return resultado


def extraer_estatus_epay(
    user: Optional[str] = None,
    password: Optional[str] = None,
    phpsessid: Optional[str] = None,
) -> dict:
    """
    Estatus de todas las máquinas de la cuenta ePay.uno.

    Retorna {codigo: {"codigo","maquina_id","estado","descripcion","color_badge"}}
    más la clave especial "__meta__" con totales y hora de consulta.
    Devuelve {} si no hay credenciales válidas o falla la red (los puntos quedan ⚪).
    """
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"})

    autenticado = False
    if user and password:
        autenticado = _login(session, user, password)
        if not autenticado:
            print("[epay] login rechazado: revisa EPAY_USER / EPAY_PASS")
            return {}
    elif phpsessid and phpsessid != "tu_session_id_aqui":
        session.cookies.set("PHPSESSID", phpsessid, domain="epay.uno")
    else:
        return {}

    try:
        resp = session.get(ESTATUS_URL, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[epay] error al consultar reportes.php: {e}")
        return {}

    if _es_pagina_login(resp.text):
        # Sesión caducada (modo PHPSESSID) o login no aplicó
        print("[epay] reportes.php devolvió la página de login: sesión inválida")
        return {}

    datos = _parsear_estatus(resp.text)
    if not datos and "Estatus equipos" not in resp.text:
        print("[epay] reportes.php sin sección 'Estatus equipos' (¿permisos?)")
        return {}

    online = sum(1 for v in datos.values() if v["estado"] == "ACTIVA")
    datos["__meta__"] = {
        "total": len(datos),
        "online": online,
        "offline": len(datos) - online,
        "consultado": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return datos


# ---------------------------------------------------------
# USO STANDALONE:  EPAY_USER=... EPAY_PASS=... python epay_scraper.py
# ---------------------------------------------------------
if __name__ == "__main__":
    import os

    datos = extraer_estatus_epay(os.getenv("EPAY_USER"), os.getenv("EPAY_PASS"))
    meta = datos.pop("__meta__", {})
    print(f"\n{meta}\n")
    for codigo, info in sorted(datos.items()):
        print(f"{info['color_badge']} {codigo:<12} {info['estado']:<9} {info['descripcion']}")

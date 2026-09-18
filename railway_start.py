"""
Arranque en Railway (18-09-2026).

Streamlit lee sus claves de `.streamlit/secrets.toml`, pero Railway las entrega
como variables de entorno. Este script arma el secrets.toml DENTRO del contenedor
(nunca en el repo) a partir de las variables del servicio y luego lanza el tablero
en el puerto que asigna Railway.

Variables del servicio en Railway:
  SUPABASE_URL, SUPABASE_KEY   tablero (app.py)
  SUPERVISOR_PIN               PIN de supervisor
  DATABASE_URL                 vista Inventario (schema vendu). O bien por piezas:
                               DB_HOST, DB_PORT, DB_USER, DB_NAME + DB_PASSWORD
  MCP_URL, MCP_TOKEN, MCP_WRITE_TOKEN          MCP de ePay de Vendu (Caracas)
  NEPTUNO_MCP_URL, NEPTUNO_MCP_TOKEN           MCP de Neptuno (opcional)
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

CLAVES = [
    "SUPABASE_URL", "SUPABASE_KEY", "SUPERVISOR_PIN", "DATABASE_URL",
    "MCP_URL", "MCP_TOKEN", "MCP_WRITE_TOKEN",
    "NEPTUNO_MCP_URL", "NEPTUNO_MCP_TOKEN", "NEPTUNO_MCP_WRITE_TOKEN",
    "EPAY_USER", "EPAY_PASS",
]


def _database_url() -> str:
    """DATABASE_URL directo, o armado por piezas para que la clave se cargue aparte."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    clave = os.getenv("DB_PASSWORD", "").strip()
    host = os.getenv("DB_HOST", "").strip()
    if not (clave and host):
        return ""
    usuario = os.getenv("DB_USER", "postgres").strip()
    puerto = os.getenv("DB_PORT", "5432").strip()
    base = os.getenv("DB_NAME", "postgres").strip()
    return f"postgresql://{quote(usuario, safe='')}:{quote(clave, safe='')}@{host}:{puerto}/{base}?sslmode=require"


def main() -> None:
    valores = {k: os.getenv(k, "").strip() for k in CLAVES}
    valores["DATABASE_URL"] = _database_url()
    if valores["DATABASE_URL"]:
        os.environ["DATABASE_URL"] = valores["DATABASE_URL"]  # db.py lo lee del entorno

    destino = Path(__file__).parent / ".streamlit" / "secrets.toml"
    destino.parent.mkdir(exist_ok=True)
    if destino.exists() and not os.getenv("RAILWAY_ENVIRONMENT"):
        # En una PC no se pisa el secrets.toml que ya tenga la persona.
        print("secrets.toml ya existe y no es Railway: se deja como está.", flush=True)
    else:
        # json.dumps produce una cadena válida también para TOML (comillas y escapes).
        lineas = [f"{k} = {json.dumps(v)}" for k, v in valores.items() if v]
        destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        print("secrets.toml armado con:", ", ".join(k for k, v in valores.items() if v), flush=True)
    faltan = [k for k in ("SUPABASE_URL", "SUPABASE_KEY") if not valores[k]]
    if faltan:
        print("AVISO: faltan", faltan, "- el tablero no podrá leer Supabase.", flush=True)
    if not valores["DATABASE_URL"]:
        print("AVISO: sin DATABASE_URL/DB_PASSWORD - la vista Inventario mostrará un aviso.", flush=True)

    puerto = os.getenv("PORT", "8501")
    os.execvp(sys.executable, [
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--server.port", puerto, "--server.address", "0.0.0.0",
        "--server.headless", "true", "--browser.gatherUsageStats", "false",
    ])


if __name__ == "__main__":
    main()

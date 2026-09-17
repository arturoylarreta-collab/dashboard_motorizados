"""
seed_local.py -- Siembra la copia LOCAL con datos reales de máquinas (JSON
bajado de Supabase por REST, solo lectura) y motorizados de prueba con UUID
fijo (en producción los choferes aún no tienen auth_user_id).

Uso (WSL):  DATABASE_URL=postgresql://vendu:vendu_local@127.0.0.1:5432/vendu_local \
            python seed_local.py /ruta/sb_maquinas_all.json
Idempotente: upsert por id.
"""
import json
import os
import sys

import psycopg2
from psycopg2.extras import execute_values

DB = os.environ["DATABASE_URL"]
ruta = sys.argv[1]
with open(ruta, encoding="utf-8") as fh:
    filas = json.load(fh)

COLS = ["id", "nombre", "motorizado", "lunes", "martes", "miercoles", "jueves", "viernes", "sabado",
        "observaciones", "estado", "fecha_estado", "llave", "codigo_epay", "direccion", "latitud",
        "longitud", "epay_machine_id", "epay_uid", "tipo", "ubicacion"]

# Motorizados de prueba. Arturo = id 18 y supervisor (SQL 09 lo asume).
MOTORIZADOS = [
    (18, "arturoylarreta", "arturoylarreta@gmail.com", "bef34ce3-e405-4e22-81e2-1339221844c5", True),
    (1, "Eduard", "eduard@vendu.local", "11111111-1111-4111-8111-111111111111", False),
    (2, "Freduard", "freduard@vendu.local", "22222222-2222-4222-8222-222222222222", False),
    (3, "Alejandro", "alejandro@vendu.local", "33333333-3333-4333-8333-333333333333", False),
    (4, "Gustavo", "gustavo@vendu.local", "44444444-4444-4444-8444-444444444444", False),
]

with psycopg2.connect(DB) as cx, cx.cursor() as cur:
    valores = [tuple(f.get(c) for c in COLS) for f in filas]
    execute_values(cur,
        f"INSERT INTO public.maquinas ({', '.join(COLS)}) VALUES %s "
        f"ON CONFLICT (id) DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS if c != "id"),
        valores)
    execute_values(cur,
        "INSERT INTO public.motorizados (id, nombre, email, auth_user_id, es_supervisor) VALUES %s "
        "ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre, email = EXCLUDED.email, "
        "auth_user_id = EXCLUDED.auth_user_id, es_supervisor = EXCLUDED.es_supervisor",
        MOTORIZADOS)
    cur.execute("SELECT setval('public.motorizados_id_seq', (SELECT max(id) FROM public.motorizados))")
    cur.execute("SELECT count(*), count(codigo_epay) FROM public.maquinas")
    print("maquinas:", cur.fetchone())
    cur.execute("SELECT count(*), count(*) FILTER (WHERE es_supervisor) FROM public.motorizados")
    print("motorizados (total, supervisores):", cur.fetchone())

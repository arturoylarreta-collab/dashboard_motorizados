"""
Prueba local de las funciones de la app (SQL 13): dos motorizados a la vez, como rol `authenticated`.
Exige DATABASE_URL local. Sale con código 1 si algo falla. No toca ePay.
"""
import json, os, sys, uuid
import psycopg2

URL = os.getenv("DATABASE_URL", "")
if "127.0.0.1" not in URL and "localhost" not in URL:
    sys.exit("Esta prueba solo corre contra la base LOCAL.")

con = psycopg2.connect(URL); con.autocommit = True; cur = con.cursor()
fallos = []

def ok(nombre, cond, extra=""):
    print(("  OK   " if cond else "  FALLA") + " " + nombre + (" · " + str(extra) if extra else ""))
    if not cond: fallos.append(nombre)

def como(uid, sql, params=()):
    """Ejecuta como la app: rol authenticated + JWT simulado, todo en una transacción."""
    cur.execute("BEGIN")
    try:
        cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": uid, "role": "authenticated", "email": uid[:8] + "@prueba"}),))
        cur.execute("SET LOCAL ROLE authenticated")
        cur.execute(sql, params)
        filas = cur.fetchall() if cur.description else []
        cur.execute("COMMIT"); return filas
    except Exception:
        cur.execute("ROLLBACK"); raise

def falla(uid, sql, params, contiene):
    try:
        como(uid, sql, params); return False, "no falló"
    except Exception as e:
        return contiene.lower() in str(e).lower(), str(e).splitlines()[0][:110]

# --- preparación (como backend) ---
cur.execute("SELECT auth_user_id::text FROM public.motorizados WHERE auth_user_id IS NOT NULL ORDER BY id LIMIT 2")
A, B = [r[0] for r in cur.fetchall()]
cur.execute("""SELECT ms.maquina_id, ms.seleccion, ms.product_id, ms.cantidad, ms.maximo FROM vendu.machine_slots ms
               WHERE ms.activo AND ms.product_id IS NOT NULL AND ms.maximo - ms.cantidad >= 3 ORDER BY ms.maquina_id, ms.seleccion LIMIT 1""")
MAQ, SEL, PID, CANT0, MAX0 = cur.fetchone()
cur.execute("SELECT id FROM vendu.inventory_locations WHERE tipo='PRINCIPAL' ORDER BY id LIMIT 1")
fila = cur.fetchone()
if not fila:
    cur.execute("INSERT INTO vendu.inventory_locations (tipo) VALUES ('PRINCIPAL') RETURNING id"); fila = cur.fetchone()
OFI = fila[0]
cur.execute("""INSERT INTO vendu.inventory_movements (tipo, product_id, cantidad, destino_id, usuario, idempotency_key)
               VALUES ('ENTRADA_COMPRA', %s, 20, %s, 'prueba', %s)""", (PID, OFI, "prueba-app-" + uuid.uuid4().hex))
def saldo(loc_sql, params):
    cur.execute("SELECT COALESCE((SELECT b.quantity FROM vendu.inventory_balances b JOIN vendu.inventory_locations l ON l.id=b.location_id WHERE " + loc_sql + " AND b.product_id=%s),0)", params + (PID,))
    return float(cur.fetchone()[0])
ofi0 = saldo("l.tipo='PRINCIPAL'", ())
print(f"Máquina {MAQ}, canal {SEL}, producto {PID}; oficina tiene {ofi0}")

k = uuid.uuid4().hex[:10]
# 1. Recoger en oficina (A lleva 6, B lleva 4), en simultáneo
como(A, "SELECT vendu.recoger_en_oficina(%s::jsonb, %s)", (json.dumps([{"product_id": PID, "cantidad": 6}]), k + "a"))
como(B, "SELECT vendu.recoger_en_oficina(%s::jsonb, %s)", (json.dumps([{"product_id": PID, "cantidad": 4}]), k + "b"))
ok("oficina bajó 10", saldo("l.tipo='PRINCIPAL'", ()) == ofi0 - 10)
ba = como(A, "SELECT product_id, cantidad FROM vendu.mi_bolso()"); bb = como(B, "SELECT product_id, cantidad FROM vendu.mi_bolso()")
ok("bolso de A = 6 y de B = 4 (no se mezclan)", dict(ba).get(PID) == 6 and dict(bb).get(PID) == 4, (ba, bb))
# 2. Reintento con la misma clave no duplica
como(A, "SELECT vendu.recoger_en_oficina(%s::jsonb, %s)", (json.dumps([{"product_id": PID, "cantidad": 6}]), k + "a"))
ok("reintento no duplica", dict(como(A, "SELECT product_id, cantidad FROM vendu.mi_bolso()")).get(PID) == 6)
# 3. No puede recoger más de lo que hay
f, m = falla(A, "SELECT vendu.recoger_en_oficina(%s::jsonb, %s)", (json.dumps([{"product_id": PID, "cantidad": 99999}]), k + "x"), "negativo")
ok("recoger de más se rechaza", f, m)
# 4. Recargar: A pone 2 en el canal; B intenta poner 5 teniendo 4 -> rechazado, nada se mueve
como(A, "SELECT vendu.recargar_desde_bolso(%s, %s::jsonb, %s)", (MAQ, json.dumps([{"seleccion": SEL, "cantidad": 2}]), k + "r1"))
cur.execute("SELECT cantidad FROM vendu.machine_slots WHERE maquina_id=%s AND seleccion=%s", (MAQ, SEL))
ok("planograma sube 2", float(cur.fetchone()[0]) == float(CANT0) + 2)
ok("bolso de A baja a 4", dict(como(A, "SELECT product_id, cantidad FROM vendu.mi_bolso()")).get(PID) == 4)
f, m = falla(B, "SELECT vendu.recargar_desde_bolso(%s, %s::jsonb, %s)", (MAQ, json.dumps([{"seleccion": SEL, "cantidad": 5}]), k + "r2"), "")
ok("B no puede poner más de lo que lleva o cabe", f, m)
ok("bolso de B intacto (4)", dict(como(B, "SELECT product_id, cantidad FROM vendu.mi_bolso()")).get(PID) == 4)
f, m = falla(A, "SELECT vendu.recargar_desde_bolso(%s, %s::jsonb, %s)", (MAQ, json.dumps([{"seleccion": SEL, "cantidad": 9999}]), k + "r3"), "caben")
ok("no cabe -> rechazado", f, m)
cur.execute("SELECT estado, cambios FROM vendu.epay_cola_recargas WHERE clave=%s", ("app-rcg-" + k + "r1",))
fila = cur.fetchone(); ok("queda en cola para ePay (PENDIENTE, +2)", fila and fila[0] == "PENDIENTE" and fila[1][0]["sumar"] == 2, fila)
# 5. Retirar 1 de la máquina al bolso de A
como(A, "SELECT vendu.retirar_de_maquina(%s, %s::jsonb, %s)", (MAQ, json.dumps([{"seleccion": SEL, "cantidad": 1}]), k + "t1"))
ok("retiro: bolso de A sube a 5", dict(como(A, "SELECT product_id, cantidad FROM vendu.mi_bolso()")).get(PID) == 5)
# 6. A no ve el bolso de B por tabla (RLS) ni la cola de B
otros = como(A, "SELECT count(*) FROM vendu.inventory_balances b JOIN vendu.inventory_locations l ON l.id=b.location_id WHERE l.tipo='MOTORIZADO' AND l.motorizado_id <> %s::uuid", (A,))
ok("A no ve bolsos ajenos", otros[0][0] == 0, otros)
# 7. La app no puede escribir tablas directo
f, m = falla(A, "INSERT INTO vendu.inventory_movements (tipo, product_id, cantidad, idempotency_key) VALUES ('AJUSTE', %s, 1, %s)", (PID, "hack-" + k), "")
ok("insert directo bloqueado", f, m)
# 8. Devolver todo: bolsos a cero, oficina recupera
como(A, "SELECT vendu.devolver_a_oficina(NULL, %s)", (k + "d1",)); como(B, "SELECT vendu.devolver_a_oficina('[]'::jsonb, %s)", (k + "d2",))
ok("bolsos en cero", not como(A, "SELECT * FROM vendu.mi_bolso()") and not como(B, "SELECT * FROM vendu.mi_bolso()"))
ok("oficina = inicial - 2 colocadas + 1 retirada", saldo("l.tipo='PRINCIPAL'", ()) == ofi0 - 2 + 1, saldo("l.tipo='PRINCIPAL'", ()))
plano = como(A, "SELECT seleccion, cantidad, espacio, en_mi_bolso FROM vendu.planograma_app(%s) LIMIT 3", (MAQ,))
ok("planograma_app responde", len(plano) > 0, plano[:2])

# limpieza del planograma (la sync real lo pisa igual)
cur.execute("UPDATE vendu.machine_slots SET cantidad=%s WHERE maquina_id=%s AND seleccion=%s", (CANT0, MAQ, SEL))
print("\nRESULTADO:", "TODO OK" if not fallos else f"{len(fallos)} FALLA(S): {fallos}")
sys.exit(1 if fallos else 0)

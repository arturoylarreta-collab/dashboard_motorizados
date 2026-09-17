"""
Capa de acceso a la base de datos (schema `vendu`) por conexión directa Postgres.

El schema `vendu` NO está expuesto vía PostgREST (para aislarlo de `public`),
así que el backend usa psycopg2 con la DATABASE_URL. Proporciona helpers
genéricos (query, one, upsert en lote) para los servicios.

Diseño de conexión: una conexión persistente por proceso en autocommit, con
keepalives TCP y reconexión automática ante fallos transitorios. (El uso de
apertura por consulta o de pool producía cuelgues intermitentes contra el
pooler de Supabase; una única conexión conmutó 30/30 operaciones.)
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

_SCHEMA = "vendu"

_CONNECT_KWARGS = dict(
    connect_timeout=15,
    keepalives=1, keepalives_idle=30,
    keepalives_interval=5, keepalives_count=3,
    options="-c statement_timeout=60000 -c lock_timeout=15000",
)


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL no está configurada.")
    return url


class DB:
    """Acceso a Postgres con una conexión persistente (autocommit)."""

    def __init__(self, url: Optional[str] = None):
        self.url = url or get_database_url()
        if self.url.startswith("postgres://"):
            self.url = self.url.replace("postgres://", "postgresql://", 1)
        self._conn: Optional[Any] = None

    def _connect(self) -> Any:
        if self._conn is None or self._conn.closed:
            conn = psycopg2.connect(self.url, **_CONNECT_KWARGS)
            conn.autocommit = True
            self._conn = conn
        return self._conn

    def _close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _rollback(self) -> None:
        try:
            if self._conn is not None and not self._conn.closed:
                self._conn.rollback()
        except Exception:
            self._close()

    @staticmethod
    def _transitorio(exc: Exception) -> bool:
        return isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))

    # ------------------------------------------------------------------ #
    # Consultas (con 1 reintento ante error transitorio: son seguras de re-ejecutar)
    # ------------------------------------------------------------------ #
    def query(self, sql: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        try:
            conn = self._connect()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as ex:
            if self._transitorio(ex):
                self._close()
                conn = self._connect()
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, params)
                    return [dict(r) for r in cur.fetchall()]
            raise

    def one(self, sql: str, params: Optional[tuple] = None) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Optional[tuple] = None) -> None:
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(sql, params)
        except Exception as ex:
            if self._transitorio(ex):
                self._close()
                raise
            raise

    # ------------------------------------------------------------------ #
    # Inserts / upserts
    # ------------------------------------------------------------------ #
    def insert(self, table: str, row: Dict[str, Any], returning: str = "id") -> Any:
        cols = list(row.keys())
        values = [row[c] for c in cols]
        colsql = ", ".join(f'"{c}"' for c in cols)
        ph = ", ".join(["%s"] * len(cols))
        sql = f'INSERT INTO {_SCHEMA}.{table} ({colsql}) VALUES ({ph}) RETURNING {returning}'
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(sql, values)
                return cur.fetchone()[0]
        except Exception as ex:
            if self._transitorio(ex):
                self._close()
            raise

    def insert_many(self, table: str, rows: Iterable[Dict[str, Any]]) -> int:
        """Insert en lote sin conflicto (para tablas sin UNIQUE)."""
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        colsql = ", ".join(f'"{c}"' for c in cols)
        values = [[r.get(c) for c in cols] for r in rows]
        sql = f"INSERT INTO {_SCHEMA}.{table} ({colsql}) VALUES %s"
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                execute_values(cur, sql, values, page_size=500)
        except Exception as ex:
            if self._transitorio(ex):
                self._close()
            raise
        return len(rows)

    def upsert(self, table: str, rows: Iterable[Dict[str, Any]],
               conflict: List[str], update: Optional[List[str]] = None) -> int:
        """Upsert en lote. `conflict` = columnas de ON CONFLICT (target).
        `update` = columnas a actualizar (por defecto, todas menos las de conflict).
        Devuelve el número de filas insertadas/actualizadas.
        """
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        update = update if update is not None else [c for c in cols if c not in conflict]
        set_sql = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update)
        conflict_sql = ", ".join(f'"{c}"' for c in conflict)
        sql = (
            f"INSERT INTO {_SCHEMA}.{table} ({', '.join('\"'+c+'\"' for c in cols)}) "
            f"VALUES %s "
            f"ON CONFLICT ({conflict_sql}) DO UPDATE SET {set_sql}"
        )
        values = [[r.get(c) for c in cols] for r in rows]
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                execute_values(cur, sql, values, page_size=500)
        except Exception as ex:
            if self._transitorio(ex):
                self._close()
            raise
        return len(rows)

    # ------------------------------------------------------------------ #
    # Esquema / helpers de dominio
    # ------------------------------------------------------------------ #
    def maquinas(self) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT id, nombre, codigo_epay, motorizado FROM public.maquinas ORDER BY id"
        )

    def motorizados(self) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT id, nombre, email, auth_user_id, activo FROM public.motorizados ORDER BY id"
        )
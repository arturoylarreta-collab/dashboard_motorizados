"""Panel operativo VENDU (schema vendu).

Dashboard Streamlit para el supervisor: KPIs, inventario en 3 niveles,
planograma, recomendaciones de recarga, órdenes, conciliación, auditoría y
sincronización con ePay. Usa la capa `DB` (psycopg2 directo a `vendu`) — la
clave publishable NO ve este schema.

Correr con:  streamlit run dashboard_vendu.py
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from db import DB
from conciliacion import ReconciliationService
from inventory import InventoryService
from orders import OrderService, TRANSICIONES
from recomendaciones import RecomendacionesService
from sync_service import SyncService
from epay_service import get_service
import recarga


st.set_page_config(
    page_title="VENDU — Panel Operativo",
    page_icon=":material/inventory_2:",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --------------------------------------------------------------------------- #
# Conexión y utilidades
# --------------------------------------------------------------------------- #
@st.cache_resource
def get_db() -> DB:
    url = st.secrets.get("DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
    if not url:
        st.error("Falta DATABASE_URL en .streamlit/secrets.toml.")
        st.stop()
    return DB(url=url)


@st.cache_resource
def get_reconciliation() -> ReconciliationService:
    return ReconciliationService(get_db())


@st.cache_resource
def get_orders() -> OrderService:
    return OrderService(get_db())


@st.cache_resource
def get_inventory() -> InventoryService:
    return InventoryService(get_db())


@st.cache_resource
def get_recomendaciones() -> RecomendacionesService:
    return RecomendacionesService(get_db())


@st.cache_resource
def get_catalogo_epay():
    return get_service()


def query(sql: str, params=None) -> pd.DataFrame:
    rows = get_db().query(sql, params)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Cargadores (cache TTL: los datos de ePay cambian solo al sincronizar)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=180, show_spinner=False)
def cargar_maquinas():
    df = query(
        """
        SELECT m.id, m.nombre, m.codigo_epay, m.motorizado, m.tipo, m.ubicacion,
               me.epay_machine_id, me.epay_uid, me.synced_at
        FROM public.maquinas m
        LEFT JOIN vendu.maquinas_epay me ON me.maquina_id = m.id
        ORDER BY m.id
        """
    )
    if df.empty:
        return df
    return df.astype({
        c: "float" for c in ("id", "epay_machine_id")
        if c in df.columns
    }, errors="ignore")


@st.cache_data(ttl=180, show_spinner=False)
def cargar_productos():
    df = query(
        """
        SELECT id, codigo_epay, nombre, categoria, precio_bs, precio_usd, activo
        FROM vendu.products
        ORDER BY nombre
        """
    )
    if df.empty:
        return df
    return df.astype({
        c: "float" for c in ("id", "precio_bs", "precio_usd")
        if c in df.columns
    }, errors="ignore")


@st.cache_data(ttl=180, show_spinner=False)
def cargar_slots():
    df = query(
        """
        SELECT ms.maquina_id, m.nombre AS maquina_nombre, ms.slot, ms.seleccion,
               ms.epay_producto_id, ms.product_id, p.nombre AS producto,
               ms.cantidad, ms.minimo, ms.maximo, ms.activo, ms.estado
        FROM vendu.machine_slots ms
        LEFT JOIN public.maquinas m ON m.id = ms.maquina_id
        LEFT JOIN vendu.products p ON p.id = ms.product_id
        WHERE ms.activo
        ORDER BY ms.maquina_id, ms.slot
        """
    )
    if df.empty:
        return df
    cols = [c for c in ("cantidad", "minimo", "maximo", "maquina_id", "product_id")
            if c in df.columns and df[c].notna().any()]
    return df.astype({c: "float" for c in cols}, errors="ignore")


@st.cache_data(ttl=180, show_spinner=False)
def cargar_balances():
    df = query(
        """
        SELECT l.id AS location_id, l.tipo, l.motorizado_id, l.maquina_id,
               COALESCE(m.nombre, mo.nombre, '—') AS ubicacion,
               b.product_id, p.nombre AS producto, p.codigo_epay,
               b.quantity, b.updated_at
        FROM vendu.inventory_balances b
        JOIN vendu.inventory_locations l ON l.id = b.location_id
        LEFT JOIN public.maquinas m ON m.id = l.maquina_id
        LEFT JOIN public.motorizados mo ON mo.auth_user_id = l.motorizado_id
        LEFT JOIN vendu.products p ON p.id = b.product_id
        ORDER BY l.tipo, ubicacion, p.nombre
        """
    )
    if df.empty:
        return df
    return df.astype({
        c: "float" for c in ("location_id", "maquina_id", "product_id", "quantity")
        if c in df.columns
    }, errors="ignore")


@st.cache_data(ttl=300, show_spinner=False)
def cargar_ventas(desde: date, hasta: date):
    df = query(
        """
        SELECT es.maquina_id, m.nombre AS maquina_nombre, es.product_id,
               p.nombre AS producto, sum(es.cantidad) AS cantidad,
               sum(es.monto_bs) AS monto_bs
        FROM vendu.epay_sales es
        LEFT JOIN public.maquinas m ON m.id = es.maquina_id
        LEFT JOIN vendu.products p ON p.id = es.product_id
        WHERE es.fecha BETWEEN %s AND %s
        GROUP BY es.maquina_id, m.nombre, es.product_id, p.nombre
        """,
        (desde, hasta),
    )
    if df.empty:
        return df
    res = df.astype({
        c: "float" for c in ("maquina_id", "product_id", "cantidad", "monto_bs")
        if c in df.columns and df[c].notna().any()
    }, errors="ignore")
    res["producto"] = res["producto"].fillna("(sin mapear)")
    res["maquina_nombre"] = res["maquina_nombre"].fillna("(sin máquina)")
    return res


@st.cache_data(ttl=300, show_spinner=False)
def cargar_recomendaciones():
    df = query(
        """
        SELECT * FROM (
          SELECT r.id, r.maquina_id, m.nombre AS maquina_nombre, r.product_id,
                 p.nombre AS producto, r.consumo_promedio_diario,
                 r.consumo_promedio_7_dias, r.consumo_promedio_14_dias,
                 r.consumo_promedio_30_dias, r.tendencia, r.stock_actual,
                 r.dias_cobertura, r.stock_objetivo, r.margen_seguridad,
                 r.cantidad_recomendada, r.motivo, r.calculado_en,
                 row_number() OVER (
                   PARTITION BY r.maquina_id ORDER BY r.calculado_en DESC) AS rn
          FROM vendu.replenishment_recommendations r
          LEFT JOIN public.maquinas m ON m.id = r.maquina_id
          LEFT JOIN vendu.products p ON p.id = r.product_id
        ) t WHERE rn = 1
        ORDER BY t.maquina_id, t.producto
        """
    )
    if df.empty:
        return df
    res = df.drop(columns=["rn"], errors="ignore")
    cols = [c for c in (
        "consumo_promedio_diario", "consumo_promedio_7_dias",
        "consumo_promedio_14_dias", "consumo_promedio_30_dias",
        "tendencia", "stock_actual", "dias_cobertura", "stock_objetivo",
        "margen_seguridad", "cantidad_recomendada")
        if c in res.columns and res[c].notna().any()]
    return res.astype({c: "float" for c in cols}, errors="ignore")


@st.cache_data(ttl=120, show_spinner=False)
def cargar_ordenes():
    df = query(
        """
        SELECT o.id, o.maquina_id, m.nombre AS maquina_nombre, o.motorizado_id,
               mo.nombre AS motorizado, o.estado, o.usuario_aprobador,
               o.observaciones, o.created_at, o.updated_at, count(i.id) AS n_items
        FROM vendu.replenishment_orders o
        LEFT JOIN public.maquinas m ON m.id = o.maquina_id
        LEFT JOIN public.motorizados mo ON mo.auth_user_id = o.motorizado_id
        LEFT JOIN vendu.replenishment_order_items i ON i.orden_id = o.id
        GROUP BY o.id, m.nombre, mo.nombre
        ORDER BY o.id DESC
        """
    )
    if df.empty:
        return df
    return df.astype({"id": "int", "maquina_id": "int", "n_items": "int"},
                     errors="ignore")


@st.cache_data(ttl=120, show_spinner=False)
def cargar_items(orden_id: int):
    df = query(
        """
        SELECT i.id, i.product_id, p.nombre AS producto, i.seleccion,
               i.cantidad_ordenada, i.cantidad_preparada, i.cantidad_entregada,
               i.cantidad_llevada, i.cantidad_colocada, i.sobrante
        FROM vendu.replenishment_order_items i
        LEFT JOIN vendu.products p ON p.id = i.product_id
        WHERE i.orden_id = %s
        ORDER BY i.id
        """,
        (orden_id,),
    )
    if df.empty:
        return df
    cols = [c for c in df.columns if c not in ("id", "product_id", "producto", "seleccion")]
    return df.astype({c: "float" for c in cols}, errors="ignore")


@st.cache_data(ttl=300, show_spinner=False)
def cargar_reconciliacion():
    df = query(
        """
        SELECT r.maquina_id, m.nombre AS maquina_nombre, r.product_id,
               p.nombre AS producto, r.inventario_vendu, r.inventario_epay,
               r.diferencia, r.estado, r.calculado_en
        FROM vendu.inventory_reconciliation r
        LEFT JOIN public.maquinas m ON m.id = r.maquina_id
        LEFT JOIN vendu.products p ON p.id = r.product_id
        WHERE r.calculado_en = (
              SELECT max(calculado_en) FROM vendu.inventory_reconciliation)
        ORDER BY r.estado, m.nombre, p.nombre
        """
    )
    if df.empty:
        return df
    cols = [c for c in ("inventario_vendu", "inventario_epay", "diferencia")
            if c in df.columns and df[c].notna().any()]
    return df.astype({c: "float" for c in cols}, errors="ignore")


@st.cache_data(ttl=60, show_spinner=False)
def cargar_audit(n: int = 200):
    df = query(
        """
        SELECT a.id, a.created_at, a.usuario, a.accion, a.entidad, a.entidad_id,
               a.antes, a.despues, a.origen, a.destino, a.referencia,
               a.maquina_id, a.product_id
        FROM vendu.audit_logs a
        ORDER BY a.id DESC
        LIMIT %s
        """,
        (int(n),),
    )
    if df.empty:
        return df
    def _parse_json(v):
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v

    def _as_celda(v):
        p = _parse_json(v)
        if isinstance(p, (dict, list)):
            return json.dumps(p, ensure_ascii=False)
        return p

    for c in ("antes", "despues"):
        df[c] = df[c].apply(_as_celda)
    return df


@st.cache_data(ttl=60, show_spinner=False)
def cargar_status_log(n: int = 100):
    df = query(
        """
        SELECT s.id, s.orden_id, s.estado_desde, s.estado_hasta, s.usuario, s.created_at
        FROM vendu.order_status_log s
        ORDER BY s.id DESC
        LIMIT %s
        """,
        (int(n),),
    )
    if df.empty:
        return df
    return df.astype({"orden_id": "int"}, errors="ignore")


@st.cache_data(ttl=120, show_spinner=False)
def cargar_sync_log(n: int = 15):
    df = query(
        """
        SELECT s.id, s.fuente, s.estado, s.detalle, s.ultima_sync
        FROM vendu.sync_log s
        ORDER BY s.id DESC
        LIMIT %s
        """,
        (int(n),),
    )
    return df


def verificar_balances(location_id: int):
    df = query(
        """
        SELECT b.product_id, p.nombre AS producto, b.quantity AS libro,
               (SELECT COALESCE(sum(cantidad) FILTER (WHERE destino_id = b.location_id), 0)
                     - COALESCE(sum(cantidad) FILTER (WHERE origen_id  = b.location_id), 0)
                FROM vendu.inventory_movements
                WHERE product_id = b.product_id) AS desde_movimientos
        FROM vendu.inventory_balances b
        JOIN vendu.products p ON p.id = b.product_id
        WHERE b.location_id = %s
        ORDER BY p.nombre
        """,
        (location_id,),
    )
    if df.empty:
        return df
    df = df.astype({"libro": "float", "desde_movimientos": "float"}, errors="ignore")
    df["diferencia"] = df["libro"] - df["desde_movimientos"]
    return df


# --------------------------------------------------------------------------- #
# Secciones
# --------------------------------------------------------------------------- #
def sec_kpis(desde: date, hasta: date):
    st.title(":material/monitoring: KPIs y Resumen")
    st.caption(f"Ventas analizadas del **{desde}** al **{hasta}**.")

    maq = cargar_maquinas()
    slots = cargar_slots()
    ventas = cargar_ventas(desde, hasta)
    recs = cargar_recomendaciones()
    reconc = cargar_reconciliacion()

    sincronizadas = int(maq["epay_machine_id"].notna().sum()) if not maq.empty else 0
    n_slots = len(slots)
    bajos = slots[slots["minimo"] > 0]
    n_bajos = int((bajos["cantidad"] < bajos["minimo"]).sum()) if not bajos.empty else 0
    n_ventas = int(ventas["cantidad"].sum(skipna=True)) if not ventas.empty else 0
    n_dif = int((reconc["estado"] == "DIFERENCIA").sum()) if not reconc.empty else 0
    n_recs = int((recs["cantidad_recomendada"] > 0).sum()) if not recs.empty else 0

    with st.container(horizontal=True):
        st.metric("Máquinas sincronizadas", f"{sincronizadas}", border=True)
        st.metric("Slots activos", f"{n_slots}", border=True,
                  chart_data=[n_slots, n_bajos], chart_type="bar")
        st.metric("Slots bajo mínimo", f"{n_bajos}", border=True,
                  delta=f"{n_bajos / n_slots:.0%}" if n_slots else None,
                  delta_color="inverse")
        st.metric("Unidades vendidas", f"{n_ventas:,.0f}", border=True)
        st.metric("Diferencias conciliación", f"{n_dif}", border=True,
                  delta_color="inverse")
        st.metric("Recomendaciones > 0", f"{n_recs}", border=True)

    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            st.subheader("Top productos por unidades")
            top = (ventas.groupby("producto", as_index=False)["cantidad"].sum()
                     .sort_values("cantidad", ascending=False).head(12))
            st.bar_chart(top, x="producto", y="cantidad", horizontal=True)
    with c2:
        with st.container(border=True):
            st.subheader("Ventas por máquina")
            por_maq = (ventas.groupby("maquina_nombre", as_index=False)["cantidad"].sum()
                        .sort_values("cantidad", ascending=False))
            st.bar_chart(por_maq, x="maquina_nombre", y="cantidad")

    with st.container(border=True):
        st.subheader(":material/warning: Alertas de stock bajo mínimo")
        if bajos.empty:
            st.info("Sin slots bajo mínimo.")
        else:
            alertas = bajos[bajos["cantidad"] < bajos["minimo"]]
            alertas = alertas.assign(
                faltante=alertas["minimo"] - alertas["cantidad"])
            alertas = alertas.sort_values("faltante", ascending=False)
            st.dataframe(
                alertas[["maquina_nombre", "slot", "producto", "cantidad",
                         "minimo", "faltante"]].head(25),
                column_config={
                    "cantidad": st.column_config.NumberColumn(format="%.0f"),
                    "minimo": st.column_config.NumberColumn(format="%.0f"),
                    "faltante": st.column_config.NumberColumn(format="%.0f"),
                },
                hide_index=True,
            )

    con_rango = ventas["maquina_id"].dropna().unique()
    sin_ventas = maq[maq["epay_machine_id"].notna()]
    sin_ventas = sin_ventas[~sin_ventas["id"].isin(con_rango)]
    if not sin_ventas.empty:
        st.warning(
            "Máquinas sin ventas registradas en el rango: "
            + ", ".join(sin_ventas["nombre"].tolist()) + "."
        )


def sec_inventario():
    st.title(":material/inventory_2: Inventario (3 niveles)")
    bal = cargar_balances()
    if bal.empty:
        st.info("Sin balances de inventario. Sembrá el inventario en «Conciliación».")
        return

    nivel = st.pills(
        "Nivel a ver", ["Todos", "PRINCIPAL", "MOTORIZADO", "MÁQUINA"],
        default="Todos", key="inv_nivel", label_visibility="collapsed")

    df = bal
    if nivel != "Todos":
        df = bal[bal["tipo"] == nivel]

    if nivel in ("MOTORIZADO", "MÁQUINA", "Todos"):
        opciones = sorted(df["ubicacion"].unique()) if not df.empty else []
        sel = st.multiselect("Ubicaciones:", opciones, default=opciones,
                             placeholder="Todas")
        if sel:
            df = df[df["ubicacion"].isin(sel)]

    resumen = (df.groupby(["tipo", "ubicacion"], as_index=False)["quantity"]
                 .sum()
                 .assign(tipo=lambda t: t["tipo"].str.replace("_", " ").str.title()))
    resumen.columns = ["Nivel", "Ubicación / Estación", "Total unidades"]

    c1, c2 = st.columns([1, 2])
    with c1:
        with st.container(border=True):
            st.markdown("**Resumen por nivel**")
            st.dataframe(resumen, hide_index=True, width="stretch")
    with c2:
        with st.container(border=True):
            st.markdown("**Saldo por producto**")
            st.dataframe(
                df.assign(tipo=df["tipo"].str.replace("_", " ").str.title())[
                    ["tipo", "ubicacion", "producto", "codigo_epay", "quantity",
                     "updated_at"]],
                column_config={
                    "quantity": st.column_config.NumberColumn("Cantidad",
                                                              format="%.1f"),
                    "updated_at": st.column_config.DatetimeColumn(
                        "Actualizado", format="DD/MM/YYYY HH:mm"),
                    "codigo_epay": st.column_config.TextColumn("Código ePay"),
                },
                hide_index=True,
                width="stretch",
            )

    with st.expander(":material/rule: Verificar libro contra movimientos"):
        locs = bal.drop_duplicates("location_id")[["location_id", "tipo", "ubicacion"]]
        if not locs.empty:
            etiqueta = [
                f"{r['tipo'].replace('_', ' ').title()} · {r['ubicacion']}"
                for _, r in locs.iterrows()
            ]
            idx = st.selectbox("Ubicación:", range(len(locs)), format_func=lambda i: etiqueta[i])
            location_id = int(locs.iloc[idx]["location_id"])
            chequeo = verificar_balances(location_id)
            st.caption("El libro debe cuadrar con los movimientos (regla de oro).")
            if not chequeo.empty:
                malos = chequeo[chequeo["diferencia"].abs() > 0.01]
                if malos.empty:
                    st.success("Libro cuadrado con movimientos ✔")
                else:
                    st.dataframe(malos, hide_index=True)
                    st.warning("Existen diferencias entre el libro y los movimientos.")

    with st.container(border=True):
        st.subheader(":material/history: Últimos movimientos")
        mov = query(
            """
            SELECT m.created_at, m.tipo, p.nombre AS producto, m.cantidad,
                   ol.tipo AS origen, od.tipo AS destino,
                   m.motorizado_id, m.maquina_id, m.orden_id, m.usuario
            FROM vendu.inventory_movements m
            LEFT JOIN vendu.products p ON p.id = m.product_id
            LEFT JOIN vendu.inventory_locations ol ON ol.id = m.origen_id
            LEFT JOIN vendu.inventory_locations od ON od.id = m.destino_id
            ORDER BY m.id DESC
            LIMIT 50
            """
        )
        if mov.empty:
            st.info("Sin movimientos aún.")
        else:
            mov["tipo"] = mov["tipo"].str.replace("_", " ").str.title()
            mov["origen"] = mov["origen"].fillna("—")
            mov["destino"] = mov["destino"].fillna("—")
            st.dataframe(
                mov[["created_at", "tipo", "producto", "cantidad",
                     "origen", "destino", "usuario"]],
                column_config={
                    "created_at": st.column_config.DatetimeColumn(
                        "Fecha", format="DD/MM/YYYY HH:mm"),
                    "cantidad": st.column_config.NumberColumn(format="%.2f"),
                },
                hide_index=True, width="stretch",
            )


def sec_planograma():
    st.title(":material/view_column: Planograma por máquina")
    slots = cargar_slots()
    maq = cargar_maquinas()
    sincronizadas = maq[maq["epay_machine_id"].notna()].sort_values("nombre")
    if sincronizadas.empty:
        st.info("Sin máquinas sincronizadas. Ejecutá la sincronización primero.")
        return

    opciones = {
        f"{r['nombre']} ({r['codigo_epay']})": int(r["id"])
        for _, r in sincronizadas.iterrows()
    }
    nombre_sel = st.selectbox("Máquina:", list(opciones), label_visibility="collapsed")
    maquina_id = opciones[nombre_sel]
    df = slots[slots["maquina_id"] == maquina_id]

    bajos = df[(df["minimo"] > 0) & (df["cantidad"] < df["minimo"])]
    ocupados = df[df["producto"].notna()]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Slots totales", len(df), border=True)
    c2.metric("Ocupados", len(ocupados), border=True)
    c3.metric("Bajo mínimo", len(bajos), border=True, delta_color="inverse")
    c4.metric("Sin mapear", int(df["product_id"].isna().sum()), border=True)

    solo_alertas = st.toggle("Solo alertas (bajo mínimo o sin mapear)", value=False)
    if solo_alertas:
        df = df[(df["product_id"].isna()) | ((df["minimo"] > 0) & (df["cantidad"] < df["minimo"]))]

    if df.empty:
        st.info("Sin slots para mostrar.")
        return

    def _resalto(r):
        fondo = "#3d2c00" if (r["minimo"] > 0 and r["cantidad"] < r["minimo"]) else ""
        return [f"background-color: {fondo}" if fondo else ""] * len(r)

    styled = (df.style
                .apply(_resalto, axis=1)
                .format({"cantidad": "{:.0f}", "minimo": "{:.0f}",
                         "maximo": "{:.0f}"}))
    st.dataframe(
        styled,
        column_config={
            "slot": st.column_config.TextColumn("Slot", pinned=True),
            "producto": st.column_config.TextColumn("Producto"),
            "cantidad": st.column_config.NumberColumn(format="%.0f"),
            "minimo": st.column_config.NumberColumn(format="%.0f"),
            "maximo": st.column_config.NumberColumn(format="%.0f"),
            "estado": st.column_config.TextColumn("Estado"),
            "seleccion": st.column_config.TextColumn("Selección"),
        },
        hide_index=True, width="stretch",
    )


def sec_recomendaciones():
    st.title(":material/lightbulb: Recomendaciones de recarga")

    if st.button(":material/autorenew: Calcular recomendaciones ahora",
                 type="primary"):
        with st.spinner("Calculando demanda para todas las máquinas..."):
            n = get_recomendaciones().guardar_para_maquinas(aplicar=True)
        st.success(f"Recomendaciones calculadas para {n} filas.")
        st.cache_data.clear()
        st.rerun()

    recs = cargar_recomendaciones()
    if recs.empty:
        st.info("Todavía no hay recomendaciones calculadas.")
        return

    maq = cargar_maquinas()
    opciones = sorted(recs["maquina_nombre"].dropna().unique())
    maq_sel = st.multiselect(
        "Máquinas:", opciones, default=opciones[:8],
        placeholder="Todas")

    df = recs[recs["maquina_nombre"].isin(maq_sel)] if maq_sel else recs
    pendientes = df[df["cantidad_recomendada"] > 0]

    c1, c2 = st.columns([3, 2])
    with c1:
        with st.container(border=True):
            st.subheader("Pendientes de recarga")
            if pendientes.empty:
                st.info("Nada recomendado en la selección.")
            else:
                st.dataframe(
                    pendientes.sort_values("cantidad_recomendada", ascending=False)
                    [["maquina_nombre", "producto", "stock_actual",
                      "consumo_promedio_diario", "dias_cobertura",
                      "cantidad_recomendada", "motivo"]],
                    column_config={
                        "stock_actual": st.column_config.NumberColumn("Stock",
                                                                      format="%.1f"),
                        "consumo_promedio_diario": st.column_config.NumberColumn(
                            "Consumo/día", format="%.2f"),
                        "dias_cobertura": st.column_config.NumberColumn("Cobertura",
                                                                        format="%.1f"),
                        "cantidad_recomendada": st.column_config.NumberColumn(
                            "Recomendado", format="%.1f"),
                    },
                    hide_index=True, width="stretch",
                )
    with c2:
        with st.container(border=True):
            st.subheader("Última cálculo por máquina")
            ultimo = recs.groupby("maquina_nombre")["calculado_en"].max()
            st.dataframe(ultimo.rename("Calculado en"), hide_index=False,
                         column_config={
                             "Calculado en": st.column_config.DatetimeColumn(
                                 format="DD/MM/YYYY HH:mm"),
                         })

    if not df.empty:
        with st.expander("Ver detalle completo (incluye cobertura holgada)"):
            st.dataframe(
                df[["maquina_nombre", "producto", "stock_actual", "stock_objetivo",
                    "consumo_promedio_diario", "consumo_promedio_7_dias",
                    "tendencia", "dias_cobertura", "margen_seguridad",
                    "cantidad_recomendada", "motivo"]],
                column_config={
                    "tendencia": st.column_config.NumberColumn(format="percent"),
                    "consumo_promedio_diario": st.column_config.NumberColumn(
                        format="%.2f"),
                    "cantidad_recomendada": st.column_config.NumberColumn(
                        format="%.1f"),
                },
                hide_index=True, width="stretch",
            )


def sec_ordenes():
    st.title(":material/orders: Órdenes de recarga")
    ordenes = cargar_ordenes()
    if not ordenes.empty:
        estados = ["Todos"] + list(ordenes["estado"].unique())
        stado = st.selectbox("Estado:", estados, key="ord_estado")
        df = ordenes if stado == "Todos" else ordenes[ordenes["estado"] == stado]
        df = df.sort_values("id", ascending=False)
        st.subheader(
            f"{len(df)} orden(es)"
            + (f" en estado **{stado}**" if stado != "Todos" else ""))
        opciones_orden: dict = {}
        for _, r in df.iterrows():
            etiqueta = (
                f"#{int(r.id)} · {r['maquina_nombre']} · {r['estado']}"
                + (f" · {r['motorizado']}" if r.get("motorizado") else "")
            )
            opciones_orden[etiqueta] = int(r.id)
        if opciones_orden:
            seleccion = st.selectbox("Seleccionar orden:", list(opciones_orden))
            _detalle_orden(opciones_orden[seleccion])
    else:
        st.info("Sin órdenes aún.")

    st.divider()
    _crear_orden()
    _crear_desde_recs()


def _crear_desde_recs():
    with st.container(border=True):
        st.subheader(":material/auto_awesome: Crear orden desde recomendaciones")
        maq = cargar_maquinas()
        sincronizadas = maq[maq["epay_machine_id"].notna()].sort_values("nombre")
        if sincronizadas.empty:
            return
        opciones_maq = {r["nombre"]: int(r["id"])
                         for _, r in sincronizadas.iterrows()}
        motos = query(
            "SELECT nombre, auth_user_id FROM public.motorizados "
            "WHERE auth_user_id IS NOT NULL ORDER BY nombre")
        opciones_mot = {r["nombre"]: str(r["auth_user_id"])
                        for _, r in motos.iterrows()}

        with st.form("form_rec", clear_on_submit=True):
            c1, c2 = st.columns(2)
            nombre_maq = c1.selectbox("Máquina:", list(opciones_maq))
            nombre_mot = c2.selectbox(
                "Motorizado asignado:",
                ["(sin asignar)"] + list(opciones_mot or []))
            enviar = st.form_submit_button(
                ":material/auto_awesome: Generar", type="primary")

        if enviar:
            try:
                res = recarga.crear_desde_recomendaciones(
                    get_db(), opciones_maq[nombre_maq],
                    motorizado_id=(None if nombre_mot == "(sin asignar)"
                                   else opciones_mot[nombre_mot]),
                    usuario="dashboard")
                orden, reparto = res["orden"], res["reparto"]
                if reparto:
                    st.caption("Reparto esperado por slot")
                    st.dataframe(pd.DataFrame(reparto),
                                 hide_index=True, width="stretch")
                st.success(f"Orden #{orden['id']} creada en PROPUESTA "
                           f"({len(res['items'])} ítems).")
                st.cache_data.clear()
                st.rerun()
            except Exception as ex:
                st.error(f"No se pudo generar la orden: {ex}")


def _detalle_orden(orden_id: int):
    orden = get_orders().obtener(orden_id)
    items = cargar_items(orden_id)
    with st.container(border=True):
        st.markdown(f"**Orden #{orden_id}** — estado `{orden['estado']}`")
        col_info, col_acc = st.columns([2, 2])
        with col_info:
            if items.empty:
                st.caption("Sin ítems.")
            else:
                st.dataframe(
                    items[["producto", "seleccion", "cantidad_ordenada",
                           "cantidad_llevada", "cantidad_colocada", "sobrante"]],
                    column_config={
                        c: st.column_config.NumberColumn(format="%.0f")
                        for c in ("cantidad_ordenada", "cantidad_llevada",
                                  "cantidad_colocada", "sobrante")
                    },
                    hide_index=True, width="stretch",
                )
        with col_acc:
            st.caption("Transiciones permitidas")
            permitidas = [e for e in TRANSICIONES.get(orden["estado"], [])
                          if e not in ("CANCELADA",)]
            if orden["estado"] in ("COMPLETADA", "CANCELADA"):
                st.caption("Orden cerrada.")
            for e in permitidas:
                if st.button(
                        f":material/arrow_forward: → {e.replace('_', ' ').title()}",
                        key=f"trans_{orden_id}_{e}"):
                    try:
                        if e == "ENTREGADA_AL_MOTORIZADO":
                            entregadas = {
                                str(r["product_id"]): float(r["cantidad_ordenada"])
                                for _, r in items.iterrows()}
                            get_orders().entregar_al_motorizado(
                                orden_id, entregadas, usuario="dashboard")
                        elif e == "COMPLETADA":
                            colocadas = {
                                str(r["product_id"]): float(r["cantidad_llevada"])
                                for _, r in items.iterrows()}
                            if orden["motorizado_id"]:
                                res = recarga.completar_y_mover(
                                    get_db(), orden_id, colocadas,
                                    usuario="dashboard")
                            else:
                                get_orders().completar(
                                    orden_id, colocadas, usuario="dashboard")
                        else:
                            get_orders().cambiar_estado(
                                orden_id, e, usuario="dashboard")
                        st.toast(f"Orden #{orden_id} → {e}", icon=":material/check:")
                        st.cache_data.clear()
                        st.rerun()
                    except Exception as ex:
                        st.error(f"No se pudo cambiar a {e}: {ex}")
            if orden["estado"] not in ("COMPLETADA", "CANCELADA", "EN_RUTA"):
                if st.button(":material/cancel: Cancelar",
                             key=f"cancel_{orden_id}"):
                    try:
                        get_orders().cancelar(orden_id, usuario="dashboard")
                        st.cache_data.clear()
                        st.rerun()
                    except Exception as ex:
                        st.error(f"No se pudo cancelar: {ex}")


def _crear_orden():
    with st.container(border=True):
        st.subheader(":material/add_circle: Crear orden manual")
        maq = cargar_maquinas()
        sincronizadas = maq[maq["epay_machine_id"].notna()].sort_values("nombre")
        if sincronizadas.empty:
            return
        opciones_maq = {r["nombre"]: int(r["id"]) for _, r in sincronizadas.iterrows()}
        motos = query(
            "SELECT nombre, auth_user_id FROM public.motorizados "
            "WHERE auth_user_id IS NOT NULL ORDER BY nombre")
        opciones_mot = {r["nombre"]: str(r["auth_user_id"])
                        for _, r in motos.iterrows()}

        productos = cargar_productos()
        activos = productos[productos["activo"] != False]  # noqa: E712
        opciones_prod = {
            f"{r['nombre']} ({r['codigo_epay']})": int(r["id"])
            for _, r in activos.iterrows()}

        with st.form("form_crear_orden", clear_on_submit=True):
            c1, c2 = st.columns(2)
            nombre_maq = c1.selectbox("Máquina:", list(opciones_maq))
            nombre_mot = c2.selectbox(
                "Motorizado asignado:",
                ["(sin asignar)"] + list(opciones_mot or []))
            observaciones = st.text_input("Observaciones:")

            default_items = pd.DataFrame({
                "producto": pd.Series(dtype="string"),
                "cantidad_ordenada": pd.Series(dtype="float"),
            })
            items_df = st.data_editor(
                default_items,
                key="editor_items",
                num_rows="dynamic",
                column_config={
                    "producto": st.column_config.SelectboxColumn(
                        "Producto", options=list(opciones_prod), required=True),
                    "cantidad_ordenada": st.column_config.NumberColumn(
                        "Cantidad a ordenar", min_value=0, step=1, format="%.0f"),
                },
            )
            enviar = st.form_submit_button(
                ":material/add: Crear en BORRADOR", type="primary")

        if enviar:
            filas = items_df.dropna(subset=["producto", "cantidad_ordenada"])
            filas = filas[filas["cantidad_ordenada"] > 0]
            if filas.empty:
                st.warning("Agregá al menos un producto con cantidad > 0.")
            else:
                items = [{
                    "product_id": opciones_prod[r["producto"]],
                    "cantidad_ordenada": float(r["cantidad_ordenada"]),
                } for _, r in filas.iterrows()]
                motorizado_id = None if nombre_mot == "(sin asignar)" \
                    else opciones_mot[nombre_mot]
                try:
                    crea = get_orders().crear(
                        opciones_maq[nombre_maq], items,
                        motorizado_id=motorizado_id,
                        observaciones=observaciones or None,
                        usuario="dashboard")
                    get_orders().cambiar_estado(crea["id"], "PROPUESTA",
                                                usuario="dashboard")
                    st.success(f"Orden #{crea['id']} creada en PROPUESTA.")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as ex:
                    st.error(f"No se pudo crear la orden: {ex}")


def sec_conciliacion():
    st.title(":material/balance: Conciliación de inventario")
    reconc = cargar_reconciliacion()
    if not reconc.empty:
        ultimo = reconc["calculado_en"].max()
        n_ok = int((reconc["estado"] == "OK").sum())
        n_dif = int((reconc["estado"] == "DIFERENCIA").sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("Última corrida", ultimo.strftime("%d/%m/%Y %H:%M"), border=True)
        c2.metric("OK", n_ok, border=True)
        c3.metric("DIFERENCIAS", n_dif, border=True, delta_color="inverse")
    else:
        st.caption("No se ha corrido ninguna conciliación todavía.")

    st.divider()
    modo = st.radio(
        "Modo de las acciones:",
        ["Simulación (no guarda)", "Aplicar cambios"],
        horizontal=True, key="conc_modo")
    aplicar = modo.startswith("Aplicar")

    b1, b2, b3 = st.columns(3)
    if b1.button(":material/upload_file: Sembrar inventario (primera vez)"):
        with st.spinner("Sembrando libro desde ePay..."):
            res = get_reconciliation().sembrar_inventario_maquinas(aplicar=aplicar)
        msg = (f"{res['balanzas_nuevas']} balances, "
               f"{res['ubicaciones_nuevas']} ubicaciones nuevas")
        st.success(msg) if aplicar else st.info(f"[Simulación] {msg}")
        st.cache_data.clear()
        st.rerun()
    if b2.button(":material/rule: Conciliar ahora"):
        with st.spinner("Conciliando libro vs ePay..."):
            n = get_reconciliation().conciliar(aplicar=aplicar)
        msg = f"{n} filas comparadas."
        st.success(msg) if aplicar else st.info(f"[Simulación] {msg}")
        st.cache_data.clear()
        st.rerun()

    if not reconc.empty:
        st.subheader("Resultado de la última corrida")
        stado_f = st.selectbox("Estado:", ["Todos", "OK", "DIFERENCIA"],
                               key="conc_filtro")
        df = reconc if stado_f == "Todos" else reconc[reconc["estado"] == stado_f]

        def _color(est):
            return "background-color: #3d2c00" if est == "DIFERENCIA" else ""

        styled = df.style.map(_color, subset=["estado"])
        st.dataframe(
            styled,
            column_config={
                "inventario_vendu": st.column_config.NumberColumn("Libro vendu",
                                                                  format="%.0f"),
                "inventario_epay": st.column_config.NumberColumn("ePay",
                                                                 format="%.0f"),
                "diferencia": st.column_config.NumberColumn("Diferencia",
                                                            format="%.0f"),
                "calculado_en": st.column_config.DatetimeColumn(
                    "Calculado", format="DD/MM/YYYY HH:mm"),
            },
            hide_index=True, width="stretch",
        )
    else:
        st.info("Sin datos de conciliación.")


def sec_auditoria():
    st.title(":material/receipt_long: Auditoría")
    tab1, tab2 = st.tabs([":material/history: Bitácora", ":material/timeline: Órdenes"])
    with tab1:
        audit = cargar_audit(200)
        if audit.empty:
            st.info("Sin registros de auditoría.")
        else:
            st.dataframe(
                audit[["created_at", "usuario", "accion", "entidad", "entidad_id",
                       "maquina_id", "product_id", "origen", "destino",
                       "referencia", "antes", "despues"]],
                column_config={
                    "created_at": st.column_config.DatetimeColumn(
                        "Fecha", format="DD/MM/YYYY HH:mm:ss"),
                    "antes": st.column_config.JsonColumn("Antes"),
                    "despues": st.column_config.JsonColumn("Después"),
                },
                hide_index=True, width="stretch",
            )
    with tab2:
        log = cargar_status_log(100)
        if log.empty:
            st.info("Sin transiciones de órdenes.")
        else:
            st.dataframe(
                log[["created_at", "orden_id", "estado_desde", "estado_hasta",
                     "usuario"]],
                column_config={
                    "created_at": st.column_config.DatetimeColumn(
                        "Fecha", format="DD/MM/YYYY HH:mm:ss"),
                    "estado_desde": st.column_config.TextColumn("Desde"),
                    "estado_hasta": st.column_config.TextColumn("Hasta"),
                },
                hide_index=True, width="stretch",
            )


def sec_sincronizacion():
    st.title(":material/sync: Sincronización con ePay")
    sinc = cargar_sync_log(15)
    st.subheader("Últimos registros de sincronización")
    if not sinc.empty:
        st.dataframe(
            sinc[["ultima_sync", "fuente", "estado", "detalle"]],
            column_config={
                "ultima_sync": st.column_config.DatetimeColumn(
                    "Última", format="DD/MM/YYYY HH:mm:ss"),
                "estado": st.column_config.TextColumn("Estado"),
            },
            hide_index=True, width="stretch",
        )

    maq = cargar_maquinas()
    sincronizadas = int(maq["epay_machine_id"].notna().sum()) if not maq.empty else 0
    st.caption(f"Máquinas sincronizadas: **{sincronizadas}**.")

    st.divider()
    st.warning(
        "El sync completo baja catálogo, planograma, mapeos y ventas de ePay; "
        "puede tardar **~3-4 min**. La frecuencia recomendada es 2-4 veces al día "
        "o tras cada jornada de recargas."
    )
    fecha_f = st.date_input("Rango de ventas a bajar:", value=_rango_30d())
    modo = st.radio(
        "Modo:",
        ["Simulación (no guarda)", "Aplicar cambios"],
        horizontal=True, key="sync_modo")
    aplicar = modo.startswith("Aplicar")
    if st.button(":material/cloud_sync: Sincronizar ahora", type="primary"):
        desde, hasta = fecha_f
        with st.spinner("Sincronizando (catálogo → planograma → ventas)... "):
            reporte = get_catalogo_epay()
            svc = SyncService(get_db(), reporte)
            try:
                r = svc.run_full(
                    aplicar=aplicar,
                    fecha_desde=desde.isoformat(),
                    fecha_hasta=hasta.isoformat(),
                    con_ventas=True)
            except Exception as ex:
                st.error(f"Error durante el sync: {ex}")
                return
        st.success(f"Sync completado (modo {'real' if aplicar else 'simulación'}).")
        _mostrar_reporte(r)
        st.cache_data.clear()
        st.rerun()


def _mostrar_reporte(r: dict):
    c1, c2, c3, c4, c5 = st.columns(5)
    maq = r.get("maquinas", {})
    c1.metric("Máquinas", maq.get("emparejadas", 0), border=True)
    c2.metric("Catálogo", r.get("catalogo", {}).get("catalogo", 0), border=True)
    c3.metric("Mapeados", r.get("mappings", {}).get("mapeados", 0), border=True)
    c4.metric("Slots", r.get("planograma", {}).get("slots", 0), border=True)
    c5.metric("Ventas", r.get("ventas", {}).get("filas", 0), border=True)

    nuevas = maq.get("nuevas", [])
    obsoletas = maq.get("obsoletas", [])
    if nuevas:
        st.warning(
            "Nuevas en ePay (no están en public.maquinas, hay que crearlas): "
            + ", ".join(f"{x['codigo']}" for x in nuevas) + ".")
    if obsoletas:
        st.info(
            "Ya no están en ePay (obsoletas): "
            + ", ".join(f"{x['codigo_epay']}" for x in obsoletas) + ".")


def _rango_30d() -> tuple:
    hoy = date.today()
    return (hoy - timedelta(days=29), hoy)


# --------------------------------------------------------------------------- #
# Navegación
# --------------------------------------------------------------------------- #
SECCIONES_NAV = {
    "kpis": ":material/monitoring: KPIs y Resumen",
    "inventario": ":material/inventory_2: Inventario",
    "planograma": ":material/view_column: Planograma",
    "recomendaciones": ":material/lightbulb: Recomendaciones",
    "ordenes": ":material/orders: Órdenes",
    "conciliacion": ":material/balance: Conciliación",
    "auditoria": ":material/receipt_long: Auditoría",
    "sincronizacion": ":material/sync: Sincronización",
}

with st.sidebar:
    st.markdown("# :material/package_2: **VENDU**")
    st.caption("Panel operativo — inventario y recargas")
    seccion = st.radio(
        "Sección",
        list(SECCIONES_NAV),
        format_func=lambda k: SECCIONES_NAV[k],
        label_visibility="collapsed",
    )
    rango = st.date_input("Rango de análisis", value=_rango_30d())
    st.divider()
    st.caption("Datos: schema `vendu` · Supabase")

if seccion == "kpis":
    desde, hasta = rango if len(rango) == 2 else _rango_30d()
    sec_kpis(desde, hasta)
elif seccion == "inventario":
    sec_inventario()
elif seccion == "planograma":
    sec_planograma()
elif seccion == "recomendaciones":
    sec_recomendaciones()
elif seccion == "ordenes":
    sec_ordenes()
elif seccion == "conciliacion":
    sec_conciliacion()
elif seccion == "auditoria":
    sec_auditoria()
elif seccion == "sincronizacion":
    sec_sincronizacion()
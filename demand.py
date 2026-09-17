"""
Motor de análisis de demanda (determinista y explicable).

NO es una IA de caja negra: cada recomendación se puede explicar con números
(consumo promedio, stock actual, cobertura, objetivo, margen) y un motivo en
lenguaje natural.

Entradas (por máquina/producto/slot):
  * stock_actual  -> machine_slots.cantidad
  * minimo/maximo -> machine_slots (capacidad física del canal)
  * consumo       -> lista de unidades vendidas por día (epay_sales),
                     de la más antigua a la más reciente.

Salidas: métricas de consumo + cantidad_recomendada + motivo.

Fórmula (configurable):
    consumo_esperado = consumo_promedio_diario × días_hasta_próxima_visita
    margen_seguridad = consumo_promedio_diario × días_de_margen
    recomendación = max(0, min(capacidad_libre, consumo_esperado + margen - stock_actual))

La recomendación nunca excede la capacidad física del canal (maximo - stock_actual)
y nunca es negativa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class DemandConfig:
    """Parámetros configurables del motor (no hardcodear intervalos)."""
    dias_hasta_proxima_visita: int = 3      # frecuencia esperada de visita (días)
    dias_margen_seguridad: float = 1.0      # días extra de stock como colchón
    dias_ventana_corta: int = 7
    dias_ventana_media: int = 14
    dias_ventana_larga: int = 30
    umbral_alta_rotacion: float = 3.0       # consumo/día a partir del cual es "alta rotación"
    usar_objetivo_maximo: bool = True       # True: objetivo = maximo del canal


@dataclass
class DemandResult:
    product_id: str
    seleccion: Optional[str]
    stock_actual: float
    minimo: float
    maximo: float
    consumo_promedio_diario: float
    consumo_promedio_7_dias: Optional[float]
    consumo_promedio_14_dias: Optional[float]
    consumo_promedio_30_dias: Optional[float]
    tendencia: Optional[float]
    dias_cobertura: Optional[float]
    stock_objetivo: float
    margen_seguridad: float
    cantidad_recomendada: float
    motivo: str
    alerta: Optional[str] = None

    def as_dict(self) -> Dict:
        return self.__dict__


def promedio_ultimos(consumo: List[float], dias: int) -> Optional[float]:
    """Promedio diario sobre los últimos `dias` días. None si no hay datos."""
    if not consumo or dias <= 0:
        return None
    ventana = consumo[-dias:]
    return round(sum(ventana) / len(ventana), 3)


def calcular_tendencia(avg_corta: Optional[float], avg_larga: Optional[float]) -> Optional[float]:
    """Tendencia relativa: (corta - larga) / larga. None si falta algún dato."""
    if avg_larga in (None, 0) or avg_corta is None:
        return None
    return round((avg_corta - avg_larga) / avg_larga, 4)


def analizar(product_id: str,
             stock_actual: float,
             minimo: float,
             maximo: float,
             consumo: List[float],
             seleccion: Optional[str] = None,
             config: Optional[DemandConfig] = None) -> DemandResult:
    """Analiza un (máquina, producto) y produce la recomendación explicable."""
    cfg = config or DemandConfig()
    stock_actual = float(stock_actual or 0)
    minimo = float(minimo or 0)
    maximo = float(maximo or 0)

    avg7 = promedio_ultimos(consumo, cfg.dias_ventana_corta)
    avg14 = promedio_ultimos(consumo, cfg.dias_ventana_media)
    avg30 = promedio_ultimos(consumo, cfg.dias_ventana_larga)

    # El promedio "maestro" usa la ventana media si hay datos, si no la corta,
    # si no la larga. Prioridad: la más informativa disponible.
    consumo_diario = avg14 if avg14 is not None else (avg7 if avg7 is not None else (avg30 or 0.0))

    tendencia = calcular_tendencia(avg7, avg14)

    # Cobertura: cuántos días aguanta el stock actual al ritmo de consumo.
    dias_cobertura = None
    if consumo_diario > 0:
        dias_cobertura = round(stock_actual / consumo_diario, 1)

    # Objetivo de stock: por defecto la capacidad máxima del canal.
    stock_objetivo = maximo if (cfg.usar_objetivo_maximo and maximo > 0) else max(maximo, minimo)

    margen_seguridad = round(consumo_diario * cfg.dias_margen_seguridad, 2)

    consumo_esperado = consumo_diario * cfg.dias_hasta_proxima_visita

    # Capacidad libre del canal (no se puede colocar más de lo que cabe).
    capacidad_libre = max(0.0, maximo - stock_actual)

    cantidad_recomendada = max(
        0.0,
        min(capacidad_libre, consumo_esperado + margen_seguridad - stock_actual),
    )
    # Se recargan UNIDADES enteras: la necesidad se redondea hacia arriba
    # (mejor sobrar una que faltar) sin pasar de la capacidad libre del canal.
    cantidad_recomendada = float(min(math.ceil(cantidad_recomendada - 1e-9), math.floor(capacidad_libre + 1e-9)))
    cantidad_recomendada = max(0.0, cantidad_recomendada)

    motivo, alerta = _motivo(
        stock_actual, minimo, consumo_diario, dias_cobertura,
        cantidad_recomendada, stock_objetivo, cfg,
    )

    return DemandResult(
        product_id=product_id,
        seleccion=seleccion,
        stock_actual=round(stock_actual, 2),
        minimo=round(minimo, 2),
        maximo=round(maximo, 2),
        consumo_promedio_diario=round(consumo_diario, 3),
        consumo_promedio_7_dias=avg7,
        consumo_promedio_14_dias=avg14,
        consumo_promedio_30_dias=avg30,
        tendencia=tendencia,
        dias_cobertura=dias_cobertura,
        stock_objetivo=round(stock_objetivo, 2),
        margen_seguridad=margen_seguridad,
        cantidad_recomendada=cantidad_recomendada,
        motivo=motivo,
        alerta=alerta,
    )


def _motivo(stock_actual: float, minimo: float, consumo_diario: float,
            dias_cobertura: Optional[float], recomendacion: float,
            stock_objetivo: float, cfg: DemandConfig) -> tuple[str, Optional[str]]:
    """Construye el motivo en lenguaje natural (explicabilidad)."""
    if consumo_diario >= cfg.umbral_alta_rotacion:
        if stock_actual < minimo:
            return (
                "Producto de alta rotación y stock por debajo del mínimo del canal. "
                f"El inventario actual ({stock_actual:g}) no cubre el período esperado "
                f"hasta la próxima visita."
            ), "ALTA_ROTACION"
        if recomendacion > 0:
            return (
                "Producto de alta rotación. Se recomienda reponer para cubrir el consumo "
                f"esperado hasta la próxima visita y mantener el objetivo de {stock_objetivo:g}."
            ), None
        return (
            f"Producto de alta rotación, pero el stock actual ({stock_actual:g}) ya cubre "
            "el período esperado. No requiere recarga."
        ), None

    if stock_actual < minimo:
        return (
            f"Stock por debajo del mínimo del canal ({minimo:g}). "
            f"Cobertura estimada: {dias_cobertura if dias_cobertura is not None else 'n/d'} días."
        ), "BAJO_MINIMO"

    if dias_cobertura is not None and dias_cobertura < cfg.dias_hasta_proxima_visita:
        return (
            f"El inventario actual no cubre el período esperado hasta la próxima visita "
            f"(cobertura {dias_cobertura:g} días < {cfg.dias_hasta_proxima_visita} días)."
        ), "COBERTURA_INSUFICIENTE"

    if recomendacion > 0:
        return (
            f"Reposición para alcanzar el objetivo de {stock_objetivo:g} unidades."
        ), None

    return "Sin necesidad de recarga en este período.", None


def recomendar_maquina(slots: List[dict], config: Optional[DemandConfig] = None) -> List[DemandResult]:
    """Recomendación para todos los slots activos de una máquina.

    `slots` son los slots normalizados de EpayService.slots_snacks() con un
    campo extra `consumo` (lista de unidades diarias) por slot.
    """
    cfg = config or DemandConfig()
    resultados = []
    for s in slots:
        if not s.get("activo", True):
            continue
        r = analizar(
            product_id=str(s.get("producto_id") or s.get("producto") or ""),
            stock_actual=float(s.get("cantidad") or 0),
            minimo=float(s.get("minimo") or 0),
            maximo=float(s.get("maximo") or 0),
            consumo=s.get("consumo") or [],
            seleccion=s.get("seleccion"),
            config=cfg,
        )
        resultados.append(r)
    return resultados

-- =====================================================================
--  PUENTE maquinas → rutas_paradas   ·   ARCHIVO 3 de 3
--  Datos, backfill de hoy y verificación. Ejecutar DESPUÉS de 1 y 2.
-- =====================================================================

-- 3.1 La app exige activo = true; dejar a todos activos
UPDATE public.motorizados SET activo = true WHERE activo IS DISTINCT FROM true;

-- 3.2 Nombres del dashboard que NO se pueden traducir a un motorizado de la app.
--     Si aquí sale "Eduard", "Freduard", "Alejandro" o "Gustavo", corregir
--     motorizados.nombre (o email) para que coincida y volver a correr 3.3.
SELECT DISTINCT mq.motorizado AS texto_en_dashboard, count(*) AS maquinas
FROM public.maquinas mq
LEFT JOIN public.motorizados mo
       ON mo.auth_user_id IS NOT NULL
      AND ( lower(trim(mo.email))  = lower(trim(mq.motorizado))
         OR lower(trim(mo.nombre)) = lower(trim(mq.motorizado)) )
WHERE mo.id IS NULL
GROUP BY mq.motorizado
ORDER BY 1;

-- 3.3 Backfill: un UPDATE sin cambios dispara el trigger fila por fila
--     y genera las paradas de HOY para todas las máquinas programadas.
UPDATE public.maquinas SET nombre = nombre;

-- 3.4 Verificación: lo que cada motorizado verá hoy en la app
SELECT mo.nombre AS motorizado, mo.email, rp.orden, rp.nombre_cliente, rp.estado, rp.fecha
FROM public.rutas_paradas rp
JOIN public.motorizados mo ON mo.auth_user_id = rp.motorizado_id
WHERE rp.fecha = (now() AT TIME ZONE 'America/Caracas')::date
ORDER BY mo.nombre, rp.orden;

-- Si 3.4 sale vacío pero 3.2 no mostró nada: hoy es domingo o ninguna máquina
-- tiene el día de hoy marcado en el cronograma (lunes..sabado = 1).

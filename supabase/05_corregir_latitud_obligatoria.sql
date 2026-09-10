-- =====================================================================
--  ARCHIVO 5 · CORRECCIÓN: la parada del día no se creaba
--  Error real (10-09-2026, al asignar una máquina en el Panel de Gestión):
--    null value in column "latitud" of relation "rutas_paradas"
--    violates not-null constraint
--  rutas_paradas exigía latitud/longitud obligatorias y las máquinas del
--  dashboard todavía no tienen coordenadas. La app funciona sin ellas
--  (navega por dirección o solo muestra el nombre), así que se vuelven
--  opcionales. Idempotente: se puede correr varias veces.
-- =====================================================================

-- 5.1 Coordenadas y ubicación PostGIS pasan a ser opcionales
ALTER TABLE public.rutas_paradas ALTER COLUMN latitud  DROP NOT NULL;
ALTER TABLE public.rutas_paradas ALTER COLUMN longitud DROP NOT NULL;
ALTER TABLE public.rutas_paradas ALTER COLUMN ubicacion DROP NOT NULL;
ALTER TABLE public.rutas_paradas ALTER COLUMN direccion DROP NOT NULL;

-- 5.2 Aprender las coordenadas de cada máquina con la foto de entrega:
--     la app guarda lat/lng al subir la evidencia. Si la máquina no tiene
--     coordenadas, se toman de ahí, y la parada queda también con ellas.
--     Así, con el tiempo, todas las máquinas tendrán ubicación sin cargarla a mano.
CREATE OR REPLACE FUNCTION public.fn_evidencia_completa_parada()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_maquina bigint;
BEGIN
  IF NEW.parada_id IS NULL
     OR NEW.parada_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
    RETURN NEW;
  END IF;

  UPDATE public.rutas_paradas
     SET estado = 'completada',
         fecha_completado = COALESCE(fecha_completado, NEW.created_at, now()),
         latitud  = COALESCE(latitud,  NEW.latitud),
         longitud = COALESCE(longitud, NEW.longitud)
   WHERE id = NEW.parada_id::uuid
   RETURNING maquina_id INTO v_maquina;

  IF v_maquina IS NOT NULL AND NEW.latitud IS NOT NULL AND NEW.longitud IS NOT NULL THEN
    UPDATE public.maquinas
       SET latitud  = COALESCE(latitud,  NEW.latitud),
           longitud = COALESCE(longitud, NEW.longitud)
     WHERE id = v_maquina;
  END IF;
  RETURN NEW;
END;
$$;

-- 5.3 Generar ahora las paradas de HOY que no se pudieron crear
UPDATE public.maquinas SET nombre = nombre;

-- 5.4 Verificación: lo que cada motorizado ve hoy en la app
SELECT mo.nombre AS motorizado, mo.email, rp.orden, rp.nombre_cliente, rp.estado, rp.fecha
FROM public.rutas_paradas rp
JOIN public.motorizados mo ON mo.auth_user_id = rp.motorizado_id
WHERE rp.fecha = (now() AT TIME ZONE 'America/Caracas')::date
ORDER BY mo.nombre, rp.orden;

-- 5.5 Nombres del dashboard que aún no coinciden con ningún motorizado de la app
--     (si aparecen Eduard, Freduard, Alejandro o Gustavo, corregir motorizados.nombre)
SELECT DISTINCT mq.motorizado AS texto_en_dashboard, count(*) AS maquinas
FROM public.maquinas mq
LEFT JOIN public.motorizados mo
       ON mo.auth_user_id IS NOT NULL
      AND ( lower(trim(mo.email))  = lower(trim(mq.motorizado))
         OR lower(trim(mo.nombre)) = lower(trim(mq.motorizado)) )
WHERE mo.id IS NULL
GROUP BY mq.motorizado ORDER BY 1;

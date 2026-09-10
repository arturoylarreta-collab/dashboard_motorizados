-- =====================================================================
--  ARCHIVO 6 · GPS: que el dashboard vea las posiciones que envía la app
--  La app escribe en ubicaciones_motorizados (user_id, latitud, longitud,
--  created_at). El dashboard leía otra tabla ("ubicaciones", vacía) y por eso
--  la pestaña "App Motorizados" nunca mostró nada. Estas dos vistas exponen
--  lo necesario, con el NOMBRE del motorizado en vez de su id.
--  Idempotente.
--
--  Nota de seguridad: el dashboard usa la llave pública (anon), así que estas
--  vistas son legibles por cualquiera que tenga esa llave. Solo exponen
--  posición y nombre, no datos de cuenta. Cuando el dashboard pase a una
--  llave privada, quitar el GRANT a anon.
-- =====================================================================

-- 6.1 Última posición conocida de cada motorizado
CREATE OR REPLACE VIEW public.vista_ultima_posicion AS
SELECT DISTINCT ON (u.user_id)
       COALESCE(mo.nombre, mo.email, u.user_id::text) AS motorizado,
       u.user_id,
       u.latitud,
       u.longitud,
       u.created_at                                          AS ultima_senal,
       ROUND(EXTRACT(EPOCH FROM (now() - u.created_at)) / 60) AS hace_minutos
FROM public.ubicaciones_motorizados u
LEFT JOIN public.motorizados mo ON mo.auth_user_id = u.user_id
WHERE u.latitud IS NOT NULL AND u.longitud IS NOT NULL
ORDER BY u.user_id, u.created_at DESC;

-- 6.2 Recorrido de HOY (hora de Caracas) de todos los motorizados
CREATE OR REPLACE VIEW public.vista_recorrido_hoy AS
SELECT COALESCE(mo.nombre, mo.email, u.user_id::text) AS motorizado,
       u.user_id,
       u.latitud,
       u.longitud,
       u.created_at
FROM public.ubicaciones_motorizados u
LEFT JOIN public.motorizados mo ON mo.auth_user_id = u.user_id
WHERE u.latitud IS NOT NULL AND u.longitud IS NOT NULL
  AND (u.created_at AT TIME ZONE 'America/Caracas')::date
      = (now() AT TIME ZONE 'America/Caracas')::date
ORDER BY u.created_at DESC;

-- 6.3 Permisos de lectura para el dashboard (anon) y la app (authenticated)
GRANT SELECT ON public.vista_ultima_posicion TO anon, authenticated;
GRANT SELECT ON public.vista_recorrido_hoy   TO anon, authenticated;

-- 6.4 Índice para que las vistas sean rápidas cuando haya miles de puntos
CREATE INDEX IF NOT EXISTS ubicaciones_motorizados_user_fecha_idx
  ON public.ubicaciones_motorizados (user_id, created_at DESC);

-- 6.5 Verificación (vacío hasta que un motorizado arranque el rastreo)
SELECT * FROM public.vista_ultima_posicion;

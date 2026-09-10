-- =====================================================================
--  PUENTE maquinas → rutas_paradas   ·   ARCHIVO 2 de 3
--  Permisos para la app (rol authenticated), RLS y Realtime. Idempotente.
--  Cada motorizado solo ve y edita lo suyo. El dashboard (anon) no toca
--  estas tablas: lo hacen los triggers SECURITY DEFINER del archivo 1.
-- =====================================================================

GRANT USAGE ON SCHEMA public TO authenticated;
GRANT SELECT, UPDATE          ON public.rutas_paradas           TO authenticated;
GRANT SELECT, INSERT, UPDATE  ON public.motorizados             TO authenticated;
GRANT SELECT, INSERT          ON public.evidencias_entregas     TO authenticated;
GRANT SELECT, INSERT          ON public.ubicaciones_motorizados TO authenticated;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;

ALTER TABLE public.rutas_paradas           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.motorizados             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evidencias_entregas     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ubicaciones_motorizados ENABLE ROW LEVEL SECURITY;

-- rutas_paradas: el motorizado ve/actualiza sus paradas
DROP POLICY IF EXISTS rp_select_propias ON public.rutas_paradas;
CREATE POLICY rp_select_propias ON public.rutas_paradas
  FOR SELECT TO authenticated USING (motorizado_id = auth.uid());

DROP POLICY IF EXISTS rp_update_propias ON public.rutas_paradas;
CREATE POLICY rp_update_propias ON public.rutas_paradas
  FOR UPDATE TO authenticated
  USING (motorizado_id = auth.uid()) WITH CHECK (motorizado_id = auth.uid());

-- motorizados: cada usuario ve/crea/edita su propia ficha (login_screen hace upsert)
DROP POLICY IF EXISTS mo_select_propio ON public.motorizados;
CREATE POLICY mo_select_propio ON public.motorizados
  FOR SELECT TO authenticated USING (auth_user_id = auth.uid());

DROP POLICY IF EXISTS mo_insert_propio ON public.motorizados;
CREATE POLICY mo_insert_propio ON public.motorizados
  FOR INSERT TO authenticated WITH CHECK (auth_user_id = auth.uid());

DROP POLICY IF EXISTS mo_update_propio ON public.motorizados;
CREATE POLICY mo_update_propio ON public.motorizados
  FOR UPDATE TO authenticated
  USING (auth_user_id = auth.uid()) WITH CHECK (auth_user_id = auth.uid());

-- evidencias_entregas: solo inserta/ve las suyas
DROP POLICY IF EXISTS ev_insert_propias ON public.evidencias_entregas;
CREATE POLICY ev_insert_propias ON public.evidencias_entregas
  FOR INSERT TO authenticated WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS ev_select_propias ON public.evidencias_entregas;
CREATE POLICY ev_select_propias ON public.evidencias_entregas
  FOR SELECT TO authenticated USING (user_id = auth.uid());

-- ubicaciones_motorizados: GPS propio
DROP POLICY IF EXISTS ub_insert_propias ON public.ubicaciones_motorizados;
CREATE POLICY ub_insert_propias ON public.ubicaciones_motorizados
  FOR INSERT TO authenticated WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS ub_select_propias ON public.ubicaciones_motorizados;
CREATE POLICY ub_select_propias ON public.ubicaciones_motorizados
  FOR SELECT TO authenticated USING (user_id = auth.uid());

-- Realtime: la app recibe el aviso cuando el dashboard le asigna una parada
DO $$
BEGIN
  ALTER PUBLICATION supabase_realtime ADD TABLE public.rutas_paradas;
EXCEPTION WHEN duplicate_object THEN
  NULL;  -- ya estaba publicada
END $$;

-- Fin archivo 2. Debe terminar en "Success. No rows returned".

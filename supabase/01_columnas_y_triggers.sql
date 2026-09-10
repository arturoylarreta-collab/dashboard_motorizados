-- =====================================================================
--  PUENTE maquinas → rutas_paradas   ·   ARCHIVO 1 de 3
--  Columnas + funciones + triggers. Idempotente (se puede repetir).
--  Escrito contra el esquema real del 2026-09-09:
--    rutas_paradas.id uuid · motorizado_id uuid · fecha_completado timestamptz
--    maquinas.id bigint · lunes..sabado integer · fecha_estado text
--    motorizados.auth_user_id uuid · activo boolean · email text
--    evidencias_entregas.parada_id text (guarda el uuid de la parada)
-- =====================================================================

-- ---------- 1. COLUMNAS NUEVAS ----------
ALTER TABLE public.rutas_paradas
  ADD COLUMN IF NOT EXISTS maquina_id bigint,
  ADD COLUMN IF NOT EXISTS fecha date NOT NULL
      DEFAULT ((now() AT TIME ZONE 'America/Caracas')::date);

-- Una parada por máquina y día. Las filas cargadas a mano (maquina_id NULL) no chocan.
CREATE UNIQUE INDEX IF NOT EXISTS rutas_paradas_maquina_fecha_uq
  ON public.rutas_paradas (maquina_id, fecha);

CREATE INDEX IF NOT EXISTS rutas_paradas_motorizado_fecha_idx
  ON public.rutas_paradas (motorizado_id, fecha);

-- Opcionales: si se llenan, la app recibe dirección y coordenadas para Maps/Waze
ALTER TABLE public.maquinas
  ADD COLUMN IF NOT EXISTS direccion text,
  ADD COLUMN IF NOT EXISTS latitud   double precision,
  ADD COLUMN IF NOT EXISTS longitud  double precision;


-- ---------- 2. TRIGGER  maquinas → rutas_paradas ----------
CREATE OR REPLACE FUNCTION public.fn_sync_maquina_a_parada()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER          -- el dashboard entra con la llave anon; esto corre como dueño
SET search_path = public
AS $$
DECLARE
  c_respetar_cronograma CONSTANT boolean := true;  -- false = crear la parada aunque hoy no toque
  v_ahora      timestamp := now() AT TIME ZONE 'America/Caracas';
  v_hoy        date      := v_ahora::date;
  v_dow        int       := EXTRACT(ISODOW FROM v_ahora);  -- 1=lunes … 6=sábado, 7=domingo
  v_programada boolean;
  v_uid        uuid;
  v_estado     text;
  v_cambio_estado boolean := false;
BEGIN
  IF pg_trigger_depth() > 1 THEN
    RETURN COALESCE(NEW, OLD);
  END IF;

  IF TG_OP = 'DELETE' THEN
    DELETE FROM public.rutas_paradas
     WHERE maquina_id = OLD.id AND fecha >= v_hoy AND estado IS DISTINCT FROM 'completada';
    RETURN OLD;
  END IF;

  v_programada := CASE v_dow
                    WHEN 1 THEN COALESCE(NEW.lunes,0)     = 1
                    WHEN 2 THEN COALESCE(NEW.martes,0)    = 1
                    WHEN 3 THEN COALESCE(NEW.miercoles,0) = 1
                    WHEN 4 THEN COALESCE(NEW.jueves,0)    = 1
                    WHEN 5 THEN COALESCE(NEW.viernes,0)   = 1
                    WHEN 6 THEN COALESCE(NEW.sabado,0)    = 1
                    ELSE false
                  END;

  -- Texto del dashboard ("Eduard", "arturoylarreta@gmail.com") → usuario de la app
  SELECT mo.auth_user_id INTO v_uid
    FROM public.motorizados mo
   WHERE NEW.motorizado IS NOT NULL
     AND mo.auth_user_id IS NOT NULL
     AND ( lower(trim(mo.email))  = lower(trim(NEW.motorizado))
        OR lower(trim(mo.nombre)) = lower(trim(NEW.motorizado)) )
   ORDER BY (lower(trim(mo.email)) = lower(trim(NEW.motorizado))) DESC, mo.id
   LIMIT 1;

  IF v_uid IS NULL OR (c_respetar_cronograma AND NOT v_programada) THEN
    DELETE FROM public.rutas_paradas
     WHERE maquina_id = NEW.id AND fecha = v_hoy AND estado IS DISTINCT FROM 'completada';
    RETURN NEW;
  END IF;

  v_estado := CASE upper(COALESCE(NEW.estado,''))
                WHEN 'COMPLETADO' THEN 'completada'
                WHEN 'EN_RUTA'    THEN 'en_proceso'
                ELSE 'pendiente'
              END;
  v_cambio_estado := (TG_OP = 'UPDATE' AND NEW.estado IS DISTINCT FROM OLD.estado);

  INSERT INTO public.rutas_paradas
         (id, maquina_id, fecha, motorizado_id, nombre_cliente, direccion, latitud, longitud,
          orden, estado, fecha_completado, created_at)
  VALUES (gen_random_uuid(), NEW.id, v_hoy, v_uid, NEW.nombre, NEW.direccion, NEW.latitud, NEW.longitud,
          (SELECT COALESCE(MAX(orden), 0) + 1
             FROM public.rutas_paradas
            WHERE motorizado_id = v_uid AND fecha = v_hoy),
          v_estado,
          CASE WHEN v_estado = 'completada' THEN now() END,
          now())
  ON CONFLICT (maquina_id, fecha) DO UPDATE
     SET motorizado_id    = EXCLUDED.motorizado_id,
         nombre_cliente   = EXCLUDED.nombre_cliente,
         direccion        = COALESCE(EXCLUDED.direccion, rutas_paradas.direccion),
         latitud          = COALESCE(EXCLUDED.latitud,   rutas_paradas.latitud),
         longitud         = COALESCE(EXCLUDED.longitud,  rutas_paradas.longitud),
         -- el estado solo se pisa si el supervisor lo cambió en el dashboard
         estado           = CASE WHEN v_cambio_estado THEN EXCLUDED.estado
                                 ELSE rutas_paradas.estado END,
         fecha_completado = CASE WHEN v_cambio_estado THEN EXCLUDED.fecha_completado
                                 ELSE rutas_paradas.fecha_completado END;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_maquina_a_parada ON public.maquinas;
CREATE TRIGGER trg_sync_maquina_a_parada
AFTER INSERT OR UPDATE OR DELETE ON public.maquinas
FOR EACH ROW EXECUTE FUNCTION public.fn_sync_maquina_a_parada();
-- El dashboard hace cada día "UPDATE maquinas SET estado='PENDIENTE', fecha_estado=hoy"
-- al abrirse (cargar_maquinas). Ese UPDATE dispara el trigger y crea las paradas del día.


-- ---------- 3. TRIGGER INVERSO  rutas_paradas → maquinas ----------
CREATE OR REPLACE FUNCTION public.fn_sync_parada_a_maquina()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF pg_trigger_depth() > 1 OR NEW.maquina_id IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.estado IS NOT DISTINCT FROM OLD.estado THEN
    RETURN NEW;
  END IF;

  UPDATE public.maquinas
     SET estado = CASE lower(COALESCE(NEW.estado,''))
                    WHEN 'completada' THEN 'COMPLETADO'
                    WHEN 'en_proceso' THEN 'EN_RUTA'
                    WHEN 'en_ruta'    THEN 'EN_RUTA'
                    ELSE 'PENDIENTE'
                  END,
         fecha_estado = to_char(NEW.fecha, 'YYYY-MM-DD')
   WHERE id = NEW.maquina_id;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_parada_a_maquina ON public.rutas_paradas;
CREATE TRIGGER trg_sync_parada_a_maquina
AFTER UPDATE OF estado ON public.rutas_paradas
FOR EACH ROW EXECUTE FUNCTION public.fn_sync_parada_a_maquina();


-- ---------- 4. FOTO DE ENTREGA → parada completada ----------
-- La app inserta en evidencias_entregas con parada_id (texto con el uuid) pero
-- nunca marca la parada. Esto cierra el ciclo y, vía el trigger anterior,
-- pone la máquina en COMPLETADO en el tablero.
CREATE OR REPLACE FUNCTION public.fn_evidencia_completa_parada()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF NEW.parada_id IS NULL
     OR NEW.parada_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
    RETURN NEW;
  END IF;

  UPDATE public.rutas_paradas
     SET estado = 'completada',
         fecha_completado = COALESCE(fecha_completado, NEW.created_at, now())
   WHERE id = NEW.parada_id::uuid;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_evidencia_completa_parada ON public.evidencias_entregas;
CREATE TRIGGER trg_evidencia_completa_parada
AFTER INSERT ON public.evidencias_entregas
FOR EACH ROW EXECUTE FUNCTION public.fn_evidencia_completa_parada();

-- Fin archivo 1. Debe terminar en "Success. No rows returned".

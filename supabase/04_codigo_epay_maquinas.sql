-- =====================================================================
--  ARCHIVO 4 · Códigos reales de ePay.uno para cada máquina del dashboard
--  Fuente: tool estatus_maquinas del MCP ePay.uno (cuenta principal Vendu),
--  consultada el 2026-09-09. Hoy la columna codigo_epay tiene "nan" o vacío,
--  por eso los puntos del tablero salen grises aunque ePay responda.
--  Idempotente. Se puede correr antes o después de los archivos 1-3.
-- =====================================================================

-- Limpiar basura previa ("nan" es un NaN de pandas guardado como texto)
UPDATE public.maquinas SET codigo_epay = ''
 WHERE codigo_epay IS NULL OR lower(codigo_epay) IN ('nan', 'none', 'null');

UPDATE public.maquinas SET codigo_epay = v.codigo
FROM (VALUES
  ('Unimet PB',        'V16-UNIPBB'),
  ('Unimet LAB',       'V17-UNILAB'),
  ('Unimet EM',        'V18-UNIEM'),
  ('UCV ING',          'V25-UCV'),
  ('UCV COMP',         'V46-UCVCOM'),
  ('UCAB CONVERT',     'V36-CONVER'),
  ('UCAB LAB',         'V01-UCLABS'),
  ('UCAB P1',          'V03-UCAP1'),
  ('UCAB MEZ',         'V02-UCAME'),
  ('UCAB M3',          'V39-UCABCA'),
  ('USM',              'V32-USMOD'),
  ('MONTAÑA',          'V44-MONTAN'),
  ('EURO S1',          'V27-EUROS1'),
  ('EURO S2',          'V28-EUROS2'),
  ('TAMACO',           'V12-TAMACO'),
  ('TAMACA',           'V48-TAMACA'),
  ('HUMBOLDT',         'V56-HUMBLT'),
  ('GOLD DATA',        'V63-GOLDDT'),
  ('PAGO DIRECTO',     'V67-PAGODI'),
  ('CUBITT',           'V70-CUBITT'),
  ('KURIOS',           'V73-KURIOS'),
  ('CASHEA P17',       'V07-CASH17'),
  ('CASHEA P18',       'V55-CASH18'),
  ('DICAM',            'V74-DICAM'),
  ('FISA',             'V57-FISA'),
  ('DOMESA',           'V64-DOMESA'),
  ('TU GRUERO',        'V65-GRUERO'),
  ('UNION RADIO',      'V52-UNIRAD'),
  ('FORUM P7',         'V41-FOR07'),
  ('FORUM P15',        'V24-FOR15'),
  ('BANGENTE',         'V50-BAGCN'),
  ('PROVINCIAL',       'V40-PROVIN'),
  ('TRANRED',          'V51-TRANRE'),
  ('ROBIN',            'V05-ROBIN'),
  ('DUNCAN',           'V53-DUNCAN'),
  ('ADROMEDA',         'V43-ANDROM'),
  ('PEGASO',           'V68-PEGASO'),
  ('TIO AMMI 1',       'V62-AMMI1'),
  ('TIO AMMI 2',       'V66-AMMI2'),
  ('RS1 RECEP',        'V58-RSGU01'),
  ('RS2 COMED',        'V59-RSGU02'),
  ('WECONNECT',        'V72-WECO04'),
  ('CEMENTERIO',       'V08-CEMEN'),
  ('HEBRAICA',         'V34-HEBPAD'),
  ('POLICLINICA P3',   'V38-PMET3'),
  ('POLICLINICA P4',   'V37-PMET4'),
  ('FLORESTA EM',      'V23-FLOEM'),
  ('FLORESTA P3',      'V22-FLOP3'),
  ('AVILA ADULT',      'V10-AVILA1'),
  ('AVILA PEDT',       'V42-AVIPED'),
  ('SANATRIX',         'V13-SANAT'),
  ('VENE CHACAO',      'V31-VENCHA'),
  ('VENE ALTAMIRA',    'V09-VENALT'),
  ('VENE CANDELARIA',  'V45-VENCAN'),
  ('FLORIDA',          'V26-FLORID'),
  ('CCS S1',           'V60-CCCSS1'),
  ('CCS S2',           'V61-CCCSS2'),
  ('FENIX',            'V71-FENIXS'),
  ('OFICENTRO 1',      'V11-OFICEN'),
  ('OFICENTRO 2',      'V69-OFIC02'),
  ('Asimetrix',        'V49-ASIMEX'),
  ('BDV 1',            'V75-BDV1'),
  ('BDV 2',            'V76-BDV2')
) AS v(nombre, codigo)
WHERE lower(trim(public.maquinas.nombre)) = lower(trim(v.nombre));

-- Sin código en ePay (quedan ⚪ a propósito): 'Máquina de Prueba' y
-- 'CALLCENTER DRCC' (no existe ninguna máquina con ese nombre en la cuenta).
-- Si CALLCENTER DRCC corresponde a alguna V-xx, asignarla en el Panel de Gestión.

-- Verificación
SELECT id, nombre, motorizado, codigo_epay
FROM public.maquinas
ORDER BY (codigo_epay = ''), nombre;

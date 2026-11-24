# SeMá · Subastas BOE Analytics (provincia de Sevilla)

Base de datos diaria y auditable de subastas de la provincia de Sevilla, construida **solo con fuentes
oficiales o autorizadas**.

## Qué hace

1. **Cada día** descarga el sumario del BOE mediante la API oficial de datos abiertos
   (`/datosabiertos/api/boe/sumario/AAAAMMDD`). Localiza los anuncios de subasta de las secciones IV y V y se
   queda con los de Sevilla. Para cada uno descarga el texto del anuncio y extrae el identificador del Portal
   (`SUB-…`) y los datos que el anuncio traiga (valor de subasta, tasación, depósito, localidad, referencia
   catastral…).
2. **Cada lunes** (o si hace más de 7 días) descarga el CSV oficial de subastas judiciales del Ministerio de
   Justicia (datos.gob.es). Hoy ese CSV no trae provincia ni identificador, así que solo se guarda como
   estadística nacional (tabla `justicia_resumen`). Si algún día incluye provincia, el script filtra Sevilla solo.
3. Guarda todo en `datos/sema.sqlite` y lo exporta también a CSV (`datos/export/`, se abren en Excel).
4. Genera un informe en `datos/informes/informe_AAAA-MM-DD.md` (y `datos/ultimo_informe.md`).

## Subastech: resultados, remates y zonas (nivel C)

El BOE no publica cómo termina cada subasta. Para eso se usa **Subastech** (app.subastech.com), un servicio de
terceros que recoge las subastas del BOE con su resultado (adjudicada o desierta), el remate (precio de cierre) y la
fecha de cierre. Sus datos se guardan aparte, en `datos/subastech.sqlite`, con `nivel_verificacion = "C"`.

- **Cómo se obtienen.** Subastech solo se consulta con su conector desde una sesión de Claude: este script no llama a
  Subastech. `python sema.py subastech` lee las respuestas **literales** del conector en el registro de la sesión de
  Claude Code (`~/.claude/projects/*/*.jsonl`). Así ningún dato se copia a mano. Cada respuesta se guarda en bruto en
  `datos/brutos/subastech/AAAA-MM-DD/` junto con la llamada que la produjo y su SHA-256.
- **Qué llamadas hacer.** `python sema.py subastech --plan` lista las llamadas al conector que tocan: todas las activas
  de la provincia, los últimos remates, las primeras páginas de adjudicadas y desiertas, y las páginas del histórico que
  falten. Subastech sirve como máximo 25 páginas de 20 lotes sin cuenta.
- **Historial.** Si un lote cambia (por ejemplo, de «Abierta» a «Adjudicada»), el valor anterior queda en
  `cambios_historicos`. Una respuesta más antigua que la ya aplicada solo rellena huecos y nunca deshace datos nuevos.
- **Reconstruir.** `python sema.py subastech --recargar` vuelve a aplicar los brutos que falten en la base. Sobre una
  carpeta vacía, rehace la base entera.
- **Tablas y vistas.**
  - `subastech_lotes`: un lote por fila, con estado, valores, remate y fechas.
  - `subastech_respuestas`: cada respuesta del conector.
  - `cambios_historicos` y `ejecuciones`.
  - `v_cierres`: adjudicadas y desiertas, con el remate en % del valor de subasta y de la tasación, y en €/m².
  - `v_zonas`: lotes, activas, adjudicadas, desiertas y % de adjudicación por municipio.
- **Exportación.** Todo se exporta a `datos/export/subastech_*.csv`. El informe está en `datos/ultimo_informe_subastech.md`.
- **Importes dudosos.** Los lotes de más de 15.000 €/m² de valor de subasta se marcan como probable error de importe y
  se excluyen de las medianas.
- **Lotes que dejan de salir.** Los lotes que dejan de aparecer entre las activas sin tener resultado se avisan en el
  informe. No se les asigna ningún estado.

Una tarea programada de Claude hace la carga diaria: sigue el plan, ejecuta `python sema.py subastech` y sube los
archivos de Subastech a `main`. No toca `datos/sema.sqlite`, así que nunca choca con la ejecución del BOE en GitHub
Actions. Después deja los datos del panel en Google Drive (ver abajo).

## Panel en Claude (datos leídos de Google Drive)

El panel «Subastas de inmuebles en Sevilla» es un artefacto de Claude con tres pestañas: las subastas activas (en
directo con el conector de Subastech), el histórico de cierres y un explorador de todas las tablas de la base. El
panel no guarda copia de los datos: los lee en directo, con el conector de Google Drive de quien lo abre, de la carpeta
«SeMá · Subastas Sevilla (datos del panel)». Este repositorio sigue siendo la fuente de verdad.

- `python sema.py panel` regenera `datos/panel/` (no se sube a GitHub) a partir de las dos bases: un
  `sema-panel-<tabla>.json` legible y un `sema-panel-<tabla>.b64.txt`, que es el mismo JSON comprimido con gzip y en
  base64. Drive solo devuelve el texto de los archivos, y así llega intacto y pesa menos.
- Tablas: `lotes` (histórico agrupado), `fichas` (`subastech_lotes`), `cambios`, `cargas` (`subastech_respuestas`),
  `boe_expedientes` y `boe_anuncios` (de `datos/sema.sqlite`). Los límites municipales (`sema-panel-limites.b64.txt`)
  se subieron una vez y no cambian.
- La tarea diaria sube a la carpeta los seis archivos nuevos y deja solo la copia anterior de cada uno, que el panel
  usa mientras Drive prepara el contenido del nuevo.

## Cómo se asegura la fiabilidad

- Cada dato lleva `fuente`, `url_fuente`, `momento_consulta` y `nivel_verificacion`
  (A1 = BOE oficial, A2 = CSV oficial de Justicia, B = tercero, C = manual).
- Cada respuesta descargada se guarda en bruto en `datos/brutos/` con su hash SHA-256 en la tabla
  `respuestas_brutas`.
- Ningún dato se sobrescribe sin dejar rastro: cada cambio queda en `cambios_historicos`.
- La razón por la que un anuncio se asocia a Sevilla queda guardada en `motivo_sevilla`
  (`unidad_gestora_41`, `menciona_sevilla`, `emisor_municipio:…`, `titulo_municipio:…`).
- La lista de municipios de la provincia se carga de Catastro (servicio oficial OVC). Si falla, el informe lo avisa.

## Qué NO hace (a propósito)

- No accede al Portal de Subastas (subastas.boe.es) ni automatiza ningún inicio de sesión.
- Respeta el robots.txt de cada dominio. Si una URL no está permitida, no se descarga y el informe lo avisa.
- No inventa datos: lo que la fuente no trae queda vacío.
- `precio_cierre` queda vacío hasta tener una fuente autorizada: la API de subastasboeapi.com (cuando llegue el
  token y su documentación) o una autorización de reutilización de la Agencia Estatal BOE.

## Límites conocidos

- Los anuncios de la AEAT en el BOE solo traen el identificador `SUB-AT-…` y un enlace al Portal. No dicen el
  tipo de bien, el valor ni la ubicación. «ANDALUCÍA 41» es la unidad gestora de Sevilla, no la ubicación del bien.
- El CSV de Justicia solo incluye subastas judiciales, sin provincia ni identificador. En la primera descarga
  (21/11/2026) tenía 81.645 subastas publicadas entre el 02/01/2021 y el 17/11/2026.
- Todos los anuncios de subasta del BOE (judiciales, notariales, AEAT, OPAEF) tienen el mismo formato breve: tipo de
  subasta, número de referencia, enlace al Portal, y lugar y fecha de firma. No incluyen el tipo de bien ni los
  importes; las tablas `lotes` e `inmuebles` se quedan preparadas para cuando haya una fuente autorizada.
- Las subastas judiciales de Sevilla solo se recogen cuando el BOE publica un edicto (sección IV) cuyo texto
  habla de una subasta. No hay una fuente oficial abierta que dé su ubicación.
- En el BOE real, muchos títulos son solo el emisor («SEVILLA», «NOTARÍA DE …»). Para esos casos el script abre
  el texto (edictos de tribunales de Sevilla y anuncios de notarías) para saber si es una subasta.

## Ejecución

Automática con GitHub Actions (`.github/workflows/sema-diario.yml`): dos pasadas al día, también con el PC apagado.
Los resultados se guardan en el propio repositorio, que debe ser **privado** porque los anuncios en bruto
pueden contener nombres de personas.

En local (Windows, macOS o Linux, Python 3.10+ sin dependencias):

```
python sema.py run                      # días pendientes (la primera vez, los últimos 30)
python sema.py run --desde 2026-10-01   # recargar un rango
python sema.py run --forzar-justicia    # incluir el CSV de Justicia hoy
python sema.py subastech --plan         # llamadas al conector de Subastech que tocan hoy
python sema.py subastech                # carga las respuestas del conector de la sesión de Claude más reciente
python sema.py panel                    # regenera los archivos del panel (datos/panel)
python -m unittest discover -s tests    # pruebas sin red
```

## Estructura de la base de datos

`ejecuciones`, `respuestas_brutas`, `dias_procesados`, `anuncios_boe`, `expedientes`, `lotes`, `inmuebles`,
`justicia_filas`, `cambios_historicos`, `municipios_sevilla`.

En `datos/subastech.sqlite`: `subastech_lotes`, `subastech_respuestas`, `cambios_historicos`, `ejecuciones` y las vistas
`v_cierres` y `v_zonas`.

Fuente de los datos: [Agencia Estatal Boletín Oficial del Estado](https://www.boe.es) ·
[Ministerio de Justicia en datos.gob.es](https://datos.gob.es).

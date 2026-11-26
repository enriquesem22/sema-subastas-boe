# SeMá Deal Finder · subastas del Portal del BOE

Base de datos auditable de las subastas que se celebran en el **Portal de Subastas del BOE**, por provincia del bien
(de momento, Sevilla), y su panel en Claude. Cada dato dice de qué página oficial sale y cuándo se consultó.

> **Estado (25-11-2026).** Método nuevo en marcha (abajo). Los datos del método anterior (sumario del BOE, Deal Finder
> por anuncios, Subastech) se borraron y su programación está en pausa; su código y su documentación siguen más abajo,
> en «Método anterior». Lo borrado puede recuperarse del historial de Git.

## Método nuevo: cómo funciona

| Paso | Programa | Qué hace | Resultado |
|---|---|---|---|
| 1 | `portal_codigos.py` | Búsqueda avanzada del Portal: subastas «Celebrándose» con el bien en la provincia | `datos/portal/codigos/<prov>/<momento>/` |
| 2 | `portal_fichas.py` | Ficha completa de cada código: pestañas, lotes, documentos y anuncio del BOE | `datos/portal/fichas/<SUB>/<momento>/` |
| 3 | `portal_base.py` | Base de datos y tablas del panel, sin hacer peticiones | `datos/subastas.sqlite`, `datos/panel/` |
| 4 | `portal_geo.py` | Coordenadas de cada inmueble (Catastro y CartoCiudad) y nueva base | `datos/portal/geo/` |
| 5 | Tarea programada de Claude | Subir `datos/panel/sema-panel-sp_*.b64.txt` a la carpeta de Drive del panel y la copia de la base `datos/panel/subastas.sqlite.gz` a la carpeta «Sema Deal Finder» | Panel y copia en Drive |

**Cada día:**
- **GitHub, de madrugada** (pasos 1 a 4): el flujo está programado a las 01:17 y a las 04:47 UTC. Son dos intentos
  porque GitHub puede retrasar mucho los programados; si el primero ya descargó ese día, el segundo no repite nada.
- **Claude, a las 13:13 (hora de Madrid)** (paso 5): la tarea programada «SeMá · Subida diaria a Drive» comprueba que
  la descarga del día está en `main` (si no, espera hasta una hora), rehace la base y sube las tablas del panel y la
  copia de la base. Si la descarga del día no llega, sube la última que haya y lo avisa. Una tarea programada de Claude
  no puede lanzar flujos de GitHub (no tiene sus herramientas), por eso cada parte tiene su propio horario.

Los pasos 1 a 4 los hace el flujo de GitHub **«Portal de Subastas (códigos y fichas)»** (`.github/workflows/portal.yml`):
Actions → el flujo → *Run workflow*, con el código de provincia (41 Sevilla, 28 Madrid, 08 Barcelona, 21 Huelva). Solo se
lanza a mano y guarda el resultado en el repositorio; la opción «Solo ubicar los inmuebles» hace solo los pasos 3 y 4,
sin pedir nada al Portal. En el PC: `py portal_codigos.py --provincia 41`, `py portal_fichas.py`, `py portal_base.py` y
`py portal_geo.py`. Desde la nube de Claude no se llega a subastas.boe.es, al Catastro ni a CartoCiudad.

### Paso 1 · Códigos (`portal_codigos.py`, basado en `buscar_codigos_sevilla.py`)

Búsqueda avanzada `https://subastas.boe.es/subastas_ava.php`: estado `EJ` («Celebrándose»), `BIEN.COD_PROVINCIA` (la
provincia **donde está el bien**, no la sede de la autoridad), 500 por página y en orden de SUB; sigue las páginas
siguientes si hay más.

- **Comprobaciones.** HTTP 200 en HTML y sin redirección; página del buscador oficial; hora del servidor a menos de
  5 minutos de la consulta; «Resultados X a Y de Z» igual a las filas mostradas y páginas sin saltos; un SUB y el estado
  «Celebrándose» en cada fila; ningún código repetido. Si algo falla, no guarda nada.
- **Resultado.** `codigos.txt` (un SUB por línea), `listado.json` (consulta, momento, total, códigos y SHA-256 de cada
  página) y la página original `pagina-N.html.gz`.

### Paso 2 · Fichas (`portal_fichas.py`, basado en `descargar_subasta_boe.py`)

Para cada código de la última búsqueda abre `https://subastas.boe.es/ds.php?id=SUB` y recorre la ficha siguiendo solo
enlaces del mismo SUB: Información general, Autoridad gestora, Bienes (y cada lote), Pujas (y cada lote) y los documentos
(`verDocumento.php`, `verCertificadoCierre.php`). De la fila «Anuncio BOE» descarga el anuncio oficial en www.boe.es
(XML y PDF) y comprueba que cita el SUB.

- **Comprobaciones.** El h2 de cada página lleva el SUB pedido y cada pestaña sus encabezados («Datos de la subasta»,
  «Autoridad gestora», «Bien», «Pujas»); un PDF empieza por `%PDF-` y acaba en `%%EOF`; cada redirección se valida.
- **Cortesía.** Peticiones de una en una con 1 s de pausa; si el Portal responde 401, 403 o 429 se para todo el lote.
- **Reanuda.** Un SUB que ya tiene ficha de la misma búsqueda no se vuelve a pedir (`--forzar` para repetir).
- **Sin copias repetidas.** Una pestaña idéntica (mismo SHA-256) a la de la ficha anterior del mismo SUB no se guarda
  otra vez: `ficha.json` apunta a la copia ya guardada (`igual_que_anterior`). Así la descarga diaria solo añade al
  repositorio lo que cambia (pujas, estado, subastas nuevas).
- **Resultado.** `ficha.json` (pestañas con sus tablas, ubicaciones de los bienes, adjuntos, anuncios y pendientes, cada
  uno con URL, momento y SHA-256), `pendientes.json` y `portal/*.html.gz`. Los adjuntos van a
  `datos/portal/documentos/<sha256>.<ext>` y los anuncios a `datos/portal/anuncios/`, una sola vez aunque se repitan.
  Resumen de cada lote en `datos/portal/fichas/_ejecuciones/`.
- **Pendientes habituales.** «acceso_restringido»: el Portal solo enseña a usuarios registrados la información
  complementaria y el importe de las pujas (no se inicia sesión). Adjuntos de más de 25 MB: no se guardan en el
  repositorio (quedan pendientes con su tamaño; se pueden abrir desde la ficha del Portal).

### Paso 3 · Base de datos (`portal_base.py`)

Rehace `datos/subastas.sqlite` entera a partir de lo guardado, sin peticiones: `capturas` y `en_captura` (historial de
búsquedas), `fichas`, `campos` (todos los pares etiqueta → valor de cada pestaña, con lote, bien, sección, archivo y
URL), `subastas` (una fila por SUB), `lotes`, `bienes` (municipio oficial con código INE del IGN), `documentos`,
`anuncios` y `pendientes`. Importes como número en euros y con su texto original; fechas en la ISO que da el Portal.
En las subastas con varios lotes el Portal no da importes de conjunto: quedan vacíos y `valor_lotes_min` /
`valor_lotes_max` dan el menor y el mayor valor de sus lotes. La pestaña «Acreedor» (nombre y NIF) se guarda en la base
pero **no va al panel**.

Exporta al panel `datos/panel/sema-panel-sp_<tabla>.json` y su copia `.b64.txt` (gzip + base64): `sp_subastas`,
`sp_lotes`, `sp_bienes`, `sp_campos`, `sp_documentos`, `sp_pendientes` y `sp_capturas`. Y deja la copia de la base
`datos/panel/subastas.sqlite.gz` (copia coherente con la API de SQLite, comprobada con `integrity_check`, gzip), que se
guarda en la carpeta de Drive «Sema Deal Finder» como `subastas_AAAA-MM-DD.sqlite.gz` (las 7 últimas fechas). Va
comprimida porque el conector de Drive admite unos 3 MB por envío; el 25-11-2026 ocupa 96 KB (0,73 MB sin comprimir).
Se abre descomprimiéndola (doble clic en Mac, 7-Zip en Windows) con DB Browser for SQLite.

### Paso 4 · Ubicación de los inmuebles (`portal_geo.py`)

Para cada inmueble, fuentes oficiales por este orden:

1. **Catastro**, por la referencia catastral de la ficha (14 primeros caracteres): servicio JSON
   `ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCoordenadas.svc/json/Consulta_CPMRC?SRS=EPSG:4326&RefCat=…`
   → el punto de la parcela (precisión «parcela»). Si fallara, el servicio clásico `OVCCoordenadas.asmx` (parámetro `RC`).
2. **CartoCiudad (IGN)**, `www.cartociudad.es/geocoder/api/geocoder/findJsonp`: primero con la referencia catastral y
   luego con la dirección (con código postal, sin él y solo «calle número», quitando escalera, planta, puerta…) →
   precisión «direccion». Solo se acepta si el municipio que devuelve (`muniCode`, INE) es el del bien.
3. Si nada sirve, el bien queda sin coordenadas y con el motivo; el panel lo dibuja con punto hueco en el centro de
   su municipio («aproximada»). No se inventan coordenadas.

Cada respuesta se guarda tal cual en `datos/portal/geo/respuestas/` (URL, momento, HTTP y SHA-256 en `indice.json`;
los errores, con su cuerpo) y no se repite; `py portal_geo.py --sin-red` vuelve a leerlas sin red. Resultado en
`datos/portal/geo/ubicaciones.json`, que `portal_base.py` añade a `bienes` (lat, lon, precisión, fuente, URL, texto
o motivo). El 25-11-2026: 21 inmuebles por parcela catastral, 13 por dirección y 3 sin ubicación exacta (dos fincas
rústicas de Morón sin referencia y un «km 509» de Carmona). Contraste: Pino Montano 118 sale a 11 m entre el Catastro
y CartoCiudad.

### Panel «SeMá Deal Finder»

Lee esas tablas de la carpeta de Drive del panel con el conector de Google Drive de quien lo abre (más los límites
municipales `sema-panel-limites`). Barra de provincia; filtro por tipo de bien (vivienda, garaje, local comercial, nave industrial, finca rústica,
trastero, solar, vehículos y otros); resumen (celebrándose, por administración, con pujas, concluyen
en 7 días); mapa de Sevilla con un punto por inmueble (color por administración, número si coinciden varios, hueco si
la ubicación es aproximada, zoom) y subastas por municipio; listado con filtros (bien, administración, municipio, texto, orden) y, al
pulsar una subasta, su ficha: importes, lotes, bienes, documentos con enlace oficial y todos los datos de la ficha.
La provincia, el tipo y el municipio se aplican a **cada bien con su propia ubicación** (pestaña Bienes de su ficha:
dirección, código postal, municipio y provincia): una subasta aparece si alguno de sus bienes cumple el filtro, y el
listado y el mapa muestran solo esos bienes. Un bien sin ubicación (un vehículo) cuenta en la provincia en cuya
búsqueda salió la subasta.

### Decisiones

- **Acceso automatizado al Portal.** Por decisión de Enrique (25-11-2026) ya no rige la regla de no extraer datos del
  Portal de forma automatizada. Se hace con pausa, de una en una y con un `User-Agent` que identifica el proyecto.
- **Anuncio del BOE.** `portal_fichas.py` lee el XML del anuncio en `www.boe.es/diario_boe/xml.php`, como el script
  original, aunque el robots.txt de www.boe.es no lo permite a programas (los programas del método anterior usan la
  página HTML oficial en su lugar).
- No se inicia sesión en el Portal ni se usa la cuenta de postor de Enrique.

### Pruebas

`python -m unittest discover -s tests`. Las de `portal_codigos` y `portal_base` usan páginas **reales** del Portal
guardadas en `tests/fixtures` (Sevilla, 25-11-2026); las de `portal_fichas` usan un Portal simulado.

---

# Método anterior (en pausa, datos borrados)

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

**En pausa desde el 24/11/2026** (el Deal Finder usa solo el BOE). Mientras estuvo activa, una tarea programada de
Claude hacía la carga diaria: seguía el plan, ejecutaba `python sema.py subastech` y subía los archivos de Subastech a
`main`. No toca `datos/sema.sqlite`, así que nunca choca con la ejecución del BOE en GitHub
Actions. Después deja los datos del panel en Google Drive (ver abajo).

## SeMá Deal Finder: solo BOE (diario oficial y Portal de Subastas)

Desde el 24/11/2026 los datos del Deal Finder salen **solo** del BOE. Provincias: Madrid, Barcelona, Sevilla y
Huelva, con todos sus municipios, seleccionadas por la **ubicación del bien**. Base aparte: `datos/dealfinder.sqlite`
(`dealfinder.py`, sin dependencias; `python sema.py dealfinder`).

- **Detección diaria (automática).** Sumario de la API de datos abiertos con `Accept: application/json` (si el JSON no
  es válido, el XML oficial). Se comprueba que la fecha del sumario es la pedida, se recorren todos los diarios y los
  `item` de las secciones IV (`4`), V.B (`5B`) y V.C (`5C`) dentro de `departamento`, `epigrafe` o `texto`, lleguen como
  objeto o como lista. De cada anuncio se pide su `url_xml`; el robots.txt de www.boe.es no permite a programas leer
  `xml.php`, así que se lee la página HTML oficial del mismo anuncio (`url_html`, `txt.php`), con el mismo texto, y un
  aviso por ejecución lo resume (la fecha de actualización solo consta en el XML). Se guardan el `BOE-B`,
  el título, el organismo, las fechas de publicación y actualización, los enlaces oficiales y cada código SUB citado en
  el texto o en los enlaces (admite sufijos alfanuméricos, p. ej. `SUB-AT-2026-26R2886001460`). Cada anuncio se
  relaciona con cada SUB (`anuncio_sub`); un SUB con varios anuncios es un solo expediente. Las respuestas en bruto se
  guardan en un archivo por día (`datos/brutos/dealfinder/AAAA/MM/AAAA-MM-DD.jsonl.gz`, con URL, momento y SHA-256).
- **Incidencias** (`incidencias`): días sin sumario, sumarios con otra fecha, errores de descarga (se reintentan en las
  siguientes ejecuciones sin duplicar nada), anuncios que hablan de una subasta sin SUB, fichas cuyo SUB no coincide y
  PDF no válidos.
- **Ubicación.** Los anuncios del BOE con SUB solo traen el SUB, la autoridad, su referencia y el lugar de firma: la
  ubicación del bien queda **pendiente** hasta leer la ficha. La provincia de la sede de la autoridad se guarda aparte,
  como pista para elegir qué fichas consultar, y nunca selecciona. Algunos anuncios sin SUB (Seguridad Social, Hacienda,
  FOGASA) sí escriben la ubicación del inmueble y se usan con el fragmento citado. Si un expediente tiene bienes en
  varias provincias, `v_bienes` dice cuáles son de cada una. No cuentan las frases sobre la oficina de la autoridad
  («ofertas en la Secretaría … sita en Madrid») ni los nombres de calle («Avenida de Salamanca»); al cambiar estas
  reglas, las ubicaciones guardadas se vuelven a analizar y los cambios quedan en `cambios_historicos`.
- **Ficha del Portal.** En este método anterior, las fichas no se descargaban: las pestañas y los PDF que se
  guarden a mano en `datos/portal/entrada` (instrucciones en su `LEEME.md`) se
  leen con su evidencia: cada dato en `campos` con URL, fecha de consulta y pestaña y fila (o documento y página).
  Importes en euros con el texto de origen, distinguiendo cero, vacío y «Sin puja mínima»; fechas ISO con desfase;
  la cuenta expediente como texto; la puja como actual o final según el estado; el importe de adjudicación solo si lo
  acredita un documento de la autoridad gestora. Discrepancias entre fuentes oficiales: `v_discrepancias`.
- **Pendientes** (`pendientes`, se rehace en cada ejecución): por expediente y lote del ámbito, qué dato falta, dónde
  consta, qué fuente oficial se revisó y por qué no se pudo comprobar, y si hay un valor de Subastech sin cotejar.
- **Subastech.** No se consulta. Sus valores ya cargados solo se cotejan con la fuente oficial (`cotejo_subastech`:
  coincide, difiere o sin cotejar) y nunca se presentan como verificados en el BOE. La carga diaria de Subastech está
  en pausa hasta nueva autorización.

## Panel anterior (sustituido el 25-11-2026 por el del método nuevo)

El panel es un artefacto de Claude. Arriba tiene una barra de provincia (Todas, Madrid, Barcelona, Sevilla, Huelva)
que filtra toda la página, y tres pestañas:

- **Deal Finder (BOE):** expedientes de `datos/dealfinder.sqlite` con su ubicación (confirmada en la ficha, según el
  anuncio o pendiente), la sede de la autoridad como pista, datos pendientes e incidencias.
- **Histórico Subastech (sin cotejar):** los cierres de Subastech ya cargados (solo Sevilla, nivel C), marcados como
  no verificados en el BOE. No se consulta Subastech.
- **Base de datos:** explorador de todas las tablas exportadas.

El panel no guarda copia de los datos: los lee en directo, con el conector de Google Drive de quien lo abre, de la
carpeta «SeMá · Subastas Sevilla (datos del panel)». Este repositorio es la fuente de verdad.

- `python sema.py panel` regenera `datos/panel/` (no se sube a GitHub) a partir de las bases, sin descargar nada:
  un `sema-panel-<tabla>.json` legible y un `sema-panel-<tabla>.b64.txt`, que es el mismo JSON comprimido con gzip y
  en base64 (Drive solo devuelve el texto de los archivos; así llega intacto y pesa menos).
- Tablas del Deal Finder: `df_expedientes`, `df_pendientes`, `df_anuncios`, `df_incidencias`, `df_sumarios`,
  `df_bienes`, `df_campos`, `df_discrepancias` y `df_cotejo`. Del BOE (versión anterior, Sevilla): `boe_expedientes` y
  `boe_anuncios`. De Subastech (no cambian mientras esté en pausa): `lotes`, `fichas`, `cambios` y `cargas`. Los
  límites municipales (`sema-panel-limites.b64.txt`) no cambian.
- La tarea programada «SeMá · Panel del Deal Finder a Drive (diario, solo BOE)» (10:13, hora de Madrid) sube cada día
  las tablas del Deal Finder y del BOE y deja solo la copia anterior de cada una, que el panel usa mientras Drive
  prepara el contenido de la nueva. Necesita el conector de Google Drive en la tarea.
- La misma tarea guarda una copia de la base del Deal Finder en la carpeta de Drive «Sema Deal Finder»:
  `dealfinder_AAAA-MM-DD.sqlite.gz`, de las 7 últimas fechas. `python sema.py panel` la prepara en
  `datos/panel/dealfinder.sqlite.gz`: copia coherente con la API de SQLite, comprobada con `integrity_check` y
  comprimida con gzip. Va comprimida porque el conector de Drive admite unos 3 MB por envío y la base ya pasa de 8 MB.
  La copia crece unos 0,85 MB al mes y llegará a ese límite hacia febrero o marzo de 2027. Desde 2,5 MB la tarea
  avisa, y por encima de 3 MB deja de subirla. Entonces habrá que subirla desde GitHub Actions con una credencial de
  Google.

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

- No automatiza ningún inicio de sesión (Cl@ve o certificado) ni usa la cuenta de postor de Enrique.
- `sema.py` y `dealfinder.py` respetan el robots.txt de www.boe.es: si una URL no está permitida, no se descarga y el
  informe lo avisa. El Portal de Subastas se consulta con `portal_codigos.py` (ver arriba).
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
python sema.py dealfinder               # Deal Finder: anuncios del BOE y fichas guardadas en datos/portal/entrada
python sema.py dealfinder --solo-portal # solo relee las fichas guardadas (sin red)
python -m unittest discover -s tests    # pruebas sin red
```

## Estructura de la base de datos

`ejecuciones`, `respuestas_brutas`, `dias_procesados`, `anuncios_boe`, `expedientes`, `lotes`, `inmuebles`,
`justicia_filas`, `cambios_historicos`, `municipios_sevilla`.

En `datos/subastech.sqlite`: `subastech_lotes`, `subastech_respuestas`, `cambios_historicos`, `ejecuciones` y las vistas
`v_cierres` y `v_zonas`.

Fuente de los datos: [Agencia Estatal Boletín Oficial del Estado](https://www.boe.es) ·
[Ministerio de Justicia en datos.gob.es](https://datos.gob.es).

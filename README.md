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
  (21/11/2026) cubría publicaciones del 01/01/2022 al 31/12/2024.
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
python -m unittest discover -s tests    # pruebas sin red
```

## Estructura de la base de datos

`ejecuciones`, `respuestas_brutas`, `dias_procesados`, `anuncios_boe`, `expedientes`, `lotes`, `inmuebles`,
`justicia_filas`, `cambios_historicos`, `municipios_sevilla`.

Fuente de los datos: [Agencia Estatal Boletín Oficial del Estado](https://www.boe.es) ·
[Ministerio de Justicia en datos.gob.es](https://datos.gob.es).

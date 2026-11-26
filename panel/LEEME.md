# Panel «SeMá Deal Finder» (artefacto de Claude)

- `index.html`: la página del panel tal como está publicada en el artefacto
  (https://claude.ai/artifact/H4SSBTA2mTkgTDaMDKAoKb). Es una copia para tenerla versionada: el panel no la lee de aquí.
- `fuentes.json`: de dónde lee cada tabla (consulta a Google Drive) y qué significa cada filtro.

## Qué muestra la ficha de cada inmueble

Al elegir una subasta, cada bien inmueble tiene, además de los datos de su pestaña «Bienes» del Portal:

- **Comprobación de la dirección** (paso 4, `portal_geo.py`): «✓ Coincide con la dirección oficial (…)» o
  «⚠ …» con el motivo (otra calle u otro número). Si no hay dirección oficial con la que compararla, lo dice.
- **Punto en el mapa**: si está en la parcela de su referencia catastral, en su dirección comprobada en CartoCiudad,
  solo en su calle (aproximado) o sin punto exacto (en el centro de su municipio).
- **Ver en**: Google Maps en su punto, Google Maps con la dirección tal como la escribe la ficha, y el visor del
  Catastro con su referencia catastral. Si las dos de Google no caen en el mismo sitio, la ficha y la fuente oficial
  no coinciden: mirar el aviso ⚠.
- **Mapa de detalle** (paso 5, `portal_mapas.py`): unos 300 m alrededor del punto, con el inmueble marcado en el
  centro, con los botones «Catastro» (parcelas, edificios y números de portal) y «Foto aérea» (ortofoto PNOA del IGN).

El panel no puede cargar mapas en vivo de otros sitios (Google Maps o el visor del Catastro), así que lleva esas
imágenes oficiales ya descargadas y los enlaces para abrir el mapa completo.

## Cómo se actualizan los datos

1. El flujo de GitHub «Portal de Subastas (códigos y fichas)» descarga de madrugada códigos, fichas, ubicaciones y
   mapas, y lo guarda en el repositorio.
2. La tarea programada de Claude «SeMá · Subida diaria a Drive (Portal de Subastas)» rehace la base
   (`portal_base.py`), prepara los mapas (`portal_mapas.py --sin-red`) y sube a Drive:
   - `datos/panel/sema-panel-sp_*.b64.txt` a la carpeta del panel;
   - `datos/panel/mapas/sema-mapa-*.b64.txt` a su subcarpeta `mapas` (solo los nuevos o cambiados; retira los de
     subastas que ya no están activas).

Si se cambia la página, se publica en el artefacto y se copia aquí.

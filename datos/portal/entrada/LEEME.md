# Fichas del Portal de Subastas (consultas puntuales)

El Deal Finder (método anterior) lee de esta carpeta las fichas del Portal guardadas a mano en el navegador: ubicación
del bien, importes, estado, fechas, lotes, bienes, pujas y documentos. Desde el 25-11-2026 el Portal también se puede
consultar de forma automatizada (ver `portal_codigos.py` en el README); esta carpeta sigue sirviendo para fichas
guardadas a mano.

## Cómo guardar una consulta

1. Crea una carpeta con la fecha de la consulta y el SUB: `AAAA-MM-DD/SUB-XX-AAAA-NNNNNN/`.
   Ejemplo: `2026-11-25/SUB-JA-2026-239881/`.
2. Abre la ficha en el Portal y guarda cada pestaña con «Guardar como → Página web, solo HTML»:
   `general.html`, `autoridad.html`, `bienes.html`, `pujas.html`. Si hay lotes, guarda la pestaña de cada lote
   con el número en el nombre: `bienes_lote1.html`, `bienes_lote2.html`…
3. Descarga a la misma carpeta los PDF de «Información complementaria» y de «Pujas» (edicto, certificación
   registral de cargas, certificado de cierre…). Si un PDF es de un lote, pon `lote2` en el nombre.
4. Opcional, pero recomendado: un `consulta.json` en la carpeta con la URL y la hora exacta:
   `{"url": "https://subastas.boe.es/detalleSubasta.php?idSub=SUB-…&ver=1", "fecha_consulta": "2026-11-25T10:05:00+01:00"}`.
   Para un archivo concreto, `nombre_del_archivo.json` con los mismos campos.
5. Sube los archivos al repositorio (o pásamelos y los subo yo). En la siguiente ejecución, o con
   `python sema.py dealfinder --solo-portal`, se leen y se rellenan los datos con su evidencia.

## Qué comprueba

- Que el encabezado de la página dice el mismo SUB que la carpeta. Si no coincide, la página no se usa y queda
  una incidencia `ficha_sub_no_coincide`.
- Que cada PDF es un PDF válido. Si es un escaneo, se pasa OCR y las líneas dudosas quedan marcadas para revisar.
- Cada dato queda con la URL, la fecha de consulta y la pestaña y fila (o el documento y la página) de donde sale.
  Si dos fuentes oficiales discrepan, se muestran las dos (vista `v_discrepancias`).

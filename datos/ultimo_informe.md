# SeMá · Informe 2026-11-23

Fuente de los datos: [Agencia Estatal Boletín Oficial del Estado](https://www.boe.es) y [Ministerio de Justicia (datos.gob.es)](https://datos.gob.es). Niveles: A1 = BOE oficial · A2 = CSV oficial de Justicia · B = tercero · C = manual.

## Resumen de esta ejecución
- Elementos del sumario leídos: 328
- Registros nuevos (todas las tablas): 3 · Cambios detectados: 0
- Anuncios de subasta acumulados: 487 en España, 99 asociados a Sevilla (0 de ellos sin texto descargado)

## Subastas judiciales en toda España (CSV de Justicia, nivel A2)
- 81645 subastas publicadas entre 2021-01-02 y 2026-11-17.
- Por estado: Subasta Finalizada: 61350, Puja concluida pendiente de finalización: 12787, Cancelada: 6697, Celebrándose: 676, Suspendida: 68, Publicada: 66
- Por tipo: Apremio: 75075, Voluntario: 4712, Judicial Concursal: 1858
- El CSV no trae provincia ni identificador de subasta, así que **no permite saber cuáles son de Sevilla**. Se usa solo como referencia nacional.

## Días procesados
| Fecha | Resultado | Elementos | Subastas (España) | Sevilla |
|---|---|---|---|---|
| 2026-11-23 | procesado | 328 | 2 | 1 |

## Expedientes de Sevilla en la base (acumulado)
| Prefijo del identificador | Expedientes |
|---|---|
| AT | 63 |
| JA | 34 |
| RC | 1 |
| NN | 1 |

| Tipo de subasta (literal del anuncio) | Expedientes |
|---|---|
| administrativa de la Agencia Estatal de Administración Tributaria | 63 |
| judicial en vía de apremio | 34 |
| notarial | 1 |
| administrativa | 1 |

| Autoridad gestora (literal del BOE) | Expedientes |
|---|---|
| MINISTERIO DE HACIENDA · U.R. SUBASTAS ANDALUCÍA 41 | 63 |
| TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL · SEVILLA | 19 |
| TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN · CARMONA | 6 |
| TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN · LEBRIJA | 3 |
| TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN · MORON DE LA FRONTERA | 2 |
| TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN · MARCHENA | 2 |
| TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN · UTRERA | 1 |
| TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN · ALCALA DE GUADAIRA | 1 |
| ANUNCIOS PARTICULARES · NOTARÍA DE FRANCISCO JAVIER LÓPEZ CANO | 1 |
| ADMINISTRACIÓN LOCAL · DIPUTACIÓN PROVINCIAL DE SEVILLA ( OPAEF) | 1 |

## Nuevos o actualizados en esta ejecución (1)
- SUB-JA-2026-267835 · judicial en vía de apremio · TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN · ALCALA DE GUADAIRA · BOE 2026-11-23 · [fuente](https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-38466)

## Cambios detectados (0)
- Ninguno

## Cobertura frente al conjunto de control
Sin conjunto de control (añade `control/control_lotes.csv` con los identificadores SUB-… de referencia).

## Avisos
- Ninguno

## Límites conocidos
- El precio de cierre y las pujas solo están en el Portal de Subastas, que este sistema no consulta sin autorización. `precio_cierre` queda vacío hasta tener una fuente autorizada.
- En los anuncios de la AEAT, «ANDALUCIA 41» es la unidad gestora de Sevilla: no garantiza que el bien esté en la provincia. Esos anuncios solo traen el identificador y el enlace al Portal.
- Las subastas judiciales de Sevilla solo se recogen si el BOE publica un edicto con texto. El CSV de Justicia no trae provincia, así que no hay fuente oficial abierta con su ubicación.

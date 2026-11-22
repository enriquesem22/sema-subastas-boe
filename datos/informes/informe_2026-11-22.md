# SeMá · Informe 2026-11-22

Fuente de los datos: [Agencia Estatal Boletín Oficial del Estado](https://www.boe.es) y [Ministerio de Justicia (datos.gob.es)](https://datos.gob.es). Niveles: A1 = BOE oficial · A2 = CSV oficial de Justicia · B = tercero · C = manual.

## Resumen de esta ejecución
- Elementos del sumario leídos: 285
- Registros nuevos (todas las tablas): 0 · Cambios detectados: 0
- Anuncios de subasta acumulados: 485 en España, 98 asociados a Sevilla (0 de ellos sin texto descargado)

## Días procesados
| Fecha | Resultado | Elementos | Subastas (España) | Sevilla |
|---|---|---|---|---|
| 2026-11-21 | procesado | 285 | 80 | 35 |

## Expedientes de Sevilla en la base (acumulado)
| Prefijo del identificador | Expedientes |
|---|---|
| AT | 63 |
| JA | 33 |
| RC | 1 |
| NN | 1 |

| Tipo de subasta (literal del anuncio) | Expedientes |
|---|---|
| administrativa de la Agencia Estatal de Administración Tributaria | 63 |
| judicial en vía de apremio | 33 |
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
| ANUNCIOS PARTICULARES · NOTARÍA DE FRANCISCO JAVIER LÓPEZ CANO | 1 |
| ADMINISTRACIÓN LOCAL · DIPUTACIÓN PROVINCIAL DE SEVILLA ( OPAEF) | 1 |

## Nuevos o actualizados en esta ejecución (0)

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

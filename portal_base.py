"""Base de datos de las subastas del Portal a partir de lo descargado (método nuevo, paso 3).

No hace ninguna petición: lee lo que guardaron portal_codigos.py (datos/portal/codigos) y portal_fichas.py
(datos/portal/fichas, anuncios y documentos) y lo vuelca en datos/subastas.sqlite. La base se rehace entera en cada
ejecución: es un derivado, y la fuente de verdad son las páginas guardadas (cada fila dice de qué archivo y URL sale).

Tablas
- capturas        cada búsqueda de códigos (momento, provincia, total, URL de la consulta, SHA-256 de las páginas).
- en_captura      qué SUB salió en cada búsqueda: historial de cuándo estuvo «Celebrándose».
- fichas          cada descarga de ficha (SUB, carpeta, momento, captura de códigos, peticiones, pendientes).
- campos          TODOS los pares etiqueta → valor de las tablas de cada pestaña (última ficha de cada SUB), con su
                  pestaña, lote, bien, sección, archivo y URL. Es la información completa y trazable.
- subastas        una fila por SUB con los datos principales (pestañas 1 y 2, resumen de bienes, lotes y pujas).
                  activa = sale en la última búsqueda de su provincia; provincias_busqueda = provincias (código INE)
                  en cuya búsqueda «provincia del bien» ha salido.
- lotes           una fila por lote (o una sola si la subasta no tiene lotes): importes y puja.
- bienes          una fila por bien: clase, descripción, referencia catastral, dirección, municipio (con su código
                  INE de la lista del IGN), situación posesoria, cargas, inscripción… y, si portal_geo.py la encontró,
                  su ubicación (lat, lon, precisión «parcela» del Catastro o «direccion» de CartoCiudad, fuente y URL)
                  o el motivo de no tenerla.
- documentos, anuncios, pendientes   lo que trae ficha.json.

En las subastas con varios lotes el Portal no da importes de conjunto («Ver valor de subasta en cada lote…»): esos
campos de `subastas` quedan vacíos y valor_lotes_min / valor_lotes_max dan el menor y el mayor valor de subasta de
sus lotes (derivado; los valores oficiales están en `lotes`).

Importes: columna numérica en euros (None si el texto no es un importe, p. ej. «Sin puja mínima» o «No consta»)
y columna *_texto con el texto tal cual. Fechas: la ISO que el propio Portal escribe entre paréntesis.

Además deja en datos/panel/subastas.sqlite.gz una copia comprimida y comprobada de la base, que se guarda en la carpeta
de Drive «Sema Deal Finder» (las 7 últimas fechas).

Uso: python portal_base.py [--raiz datos/portal] [--base datos/subastas.sqlite] [--panel datos/panel]
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

import portal_codigos

BASE = Path(__file__).resolve().parent
RAIZ = BASE / "datos" / "portal"
BASE_DATOS = BASE / "datos" / "subastas.sqlite"
PANEL = BASE / "datos" / "panel"
GEO = BASE / "datos" / "geo" / "municipios_es.json"
FUENTE = "Agencia Estatal Boletín Oficial del Estado · Portal de Subastas (https://subastas.boe.es) y BOE (https://www.boe.es)"

# etiqueta de la pestaña (sin mayúsculas ni tildes) → columna
MAPA_GENERAL = {"identificador": "identificador", "tipo de subasta": "tipo_subasta", "cuenta expediente": "cuenta_expediente",
                "fecha de inicio": "fecha_inicio_texto", "fecha de conclusion": "fecha_conclusion_texto",
                "cantidad reclamada": "cantidad_reclamada_texto", "lotes": "lotes_texto", "anuncio boe": "anuncio_boe",
                "valor subasta": "valor_subasta_texto", "tasacion": "tasacion_texto", "puja minima": "puja_minima_texto",
                "tramos entre pujas": "tramos_texto", "importe del deposito": "deposito_texto",
                "forma adjudicacion": "forma_adjudicacion"}
MAPA_AUTORIDAD = {"codigo": "autoridad_codigo", "descripcion": "autoridad", "direccion": "autoridad_direccion",
                  "telefono": "autoridad_telefono", "fax": "autoridad_fax", "correo electronico": "autoridad_email"}
MAPA_LOTE = {"valor subasta": "valor_subasta", "valor de tasacion": "tasacion", "tasacion": "tasacion",
             "importe del deposito": "deposito", "puja minima": "puja_minima", "tramos entre pujas": "tramos",
             "cantidad reclamada": "cantidad_reclamada"}
MAPA_BIEN = {"descripcion": "descripcion", "referencia catastral": "referencia_catastral", "idufir": "idufir",
             "referencia registral": "referencia_registral", "direccion": "direccion", "codigo postal": "codigo_postal",
             "localidad": "localidad", "provincia": "provincia", "situacion posesoria": "situacion_posesoria",
             "visitable": "visitable", "vivienda habitual": "vivienda_habitual", "cargas": "cargas",
             "titulo juridico": "titulo_juridico", "inscripcion registral": "inscripcion_registral",
             "informacion adicional": "informacion_adicional", "marca": "marca", "modelo": "modelo",
             "matricula": "matricula"}
IMPORTES_LOTE = ("valor_subasta", "tasacion", "deposito", "puja_minima", "tramos", "cantidad_reclamada")
GRUPO_SUB = {"JA": "Judicial", "AT": "Agencia Tributaria"}
PESTANA_PERSONAL = "4"  # «Acreedor»: nombre, NIF y dirección del acreedor. Se guarda en la base, no va al panel.

ESQUEMA = f"""
CREATE TABLE capturas (captura TEXT PRIMARY KEY, provincia TEXT, fecha_captura TEXT, total INTEGER, url_consulta TEXT,
                       sha256 TEXT, ultima INTEGER);
CREATE TABLE en_captura (captura TEXT, sub TEXT, orden INTEGER, PRIMARY KEY (captura, sub));
CREATE TABLE fichas (carpeta TEXT PRIMARY KEY, sub TEXT, consultado_utc TEXT, captura_codigos TEXT, peticiones INTEGER,
                     paginas INTEGER, adjuntos INTEGER, anuncios INTEGER, pendientes INTEGER, ultima INTEGER);
CREATE TABLE campos (sub TEXT, pestana TEXT, lote TEXT, bien INTEGER, seccion TEXT, etiqueta TEXT, valor TEXT,
                     orden INTEGER, archivo TEXT, url TEXT, consultado_utc TEXT);
CREATE TABLE subastas (sub TEXT PRIMARY KEY, grupo TEXT, tipo_subasta TEXT, activa INTEGER, provincias_busqueda TEXT,
                       vista_en TEXT,
                       primera_vez TEXT, ultima_vez TEXT, fecha_inicio TEXT, fecha_conclusion TEXT,
                       lotes_texto TEXT, n_lotes INTEGER, {', '.join(f'{c} REAL, {c}_texto TEXT' for c in IMPORTES_LOTE)},
                       valor_lotes_min REAL, valor_lotes_max REAL,
                       anuncio_boe TEXT, cuenta_expediente TEXT, forma_adjudicacion TEXT,
                       autoridad TEXT, autoridad_codigo TEXT, autoridad_direccion TEXT, autoridad_telefono TEXT,
                       autoridad_fax TEXT, autoridad_email TEXT,
                       n_bienes INTEGER, clases_bien TEXT, es_inmueble INTEGER, subtipos TEXT, municipios TEXT,
                       municipios_ine TEXT, provincias TEXT, codigos_postales TEXT, descripcion TEXT,
                       pujas TEXT, con_pujas INTEGER, n_documentos INTEGER, n_pendientes INTEGER,
                       acceso_restringido INTEGER, url_ficha TEXT, consultado_utc TEXT, carpeta TEXT);
CREATE TABLE lotes (sub TEXT, lote TEXT, {', '.join(f'{c} REAL, {c}_texto TEXT' for c in IMPORTES_LOTE)},
                    puja TEXT, puja_importe REAL, n_bienes INTEGER, archivo TEXT, url TEXT, PRIMARY KEY (sub, lote));
CREATE TABLE bienes (sub TEXT, lote TEXT, bien INTEGER, clase TEXT, subtipo TEXT,
                     {', '.join(f'{c} TEXT' for c in dict.fromkeys(MAPA_BIEN.values()))},
                     cargas_importe REAL, municipio TEXT, municipio_ine TEXT, archivo TEXT, url TEXT,
                     lat REAL, lon REAL, ubicacion_precision TEXT, ubicacion_fuente TEXT, ubicacion_url TEXT,
                     ubicacion_texto TEXT, ubicacion_motivo TEXT,
                     PRIMARY KEY (sub, lote, bien));
CREATE TABLE documentos (sub TEXT, etiqueta TEXT, lote TEXT, archivo TEXT, url TEXT, sha256 TEXT, tipo_mime TEXT,
                         bytes INTEGER, consultado_utc TEXT);
CREATE TABLE anuncios (sub TEXT, anuncio TEXT, publicado TEXT, url TEXT, url_xml TEXT, archivo_xml TEXT,
                       archivo_pdf TEXT, aviso TEXT, PRIMARY KEY (sub, anuncio));
CREATE TABLE pendientes (sub TEXT, tipo TEXT, etiqueta TEXT, url TEXT, motivo TEXT);
"""
RE_LOTE = re.compile(r"Lote\s+(\d+)\b", re.I)
RE_BIEN = re.compile(r"Bien\s+(\d+)(?:\s*[-–]\s*(.+))?", re.I)
RE_CLASE = re.compile(r"(.+?)\s*\((.+)\)\s*$")
RE_IMPORTE = re.compile(r"^-?\d{1,3}(?:\.\d{3})*(?:,\d+)?\s*€?$|^-?\d+(?:,\d+)?\s*€?$")
RE_ISO = re.compile(r"ISO:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+(?:[+-][0-9:]+|Z)?)")
TEXTOS_PUJA_IGNORADOS = ("advertencia", "para participar", "la subasta contiene varios lotes", "selecciona el numero")


# --------------------------------------------------------------------------- utilidades

def norm(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(s.casefold().split())


def texto(e) -> str:
    return " ".join(" ".join(e.itertext()).split())


def importe(valor: str | None) -> float | None:
    """'123.456,78 €' → 123456.78. Lo que no es un importe («Sin puja mínima», «No consta») da None."""
    if not valor:
        return None
    v = valor.replace("\xa0", " ").strip()
    if not RE_IMPORTE.match(v):
        return None
    return float(v.replace("€", "").replace(".", "").replace(",", ".").strip())


def fecha_iso(valor: str | None) -> str | None:
    m = RE_ISO.search(valor or "")
    return m[1] if m else None


def leer_html(ruta: Path):
    datos = ruta.read_bytes()
    if ruta.suffix == ".gz":
        datos = gzip.decompress(datos)
    return portal_codigos.PaginaPortal().leer(datos.decode("utf-8-sig"))


class Municipios:
    """Nombre de localidad del Portal → municipio oficial (IGN) dentro de su provincia."""

    def __init__(self, ruta: Path = GEO):
        geo = json.loads(ruta.read_text(encoding="utf-8"))
        self.provincias = {norm(n): c for c, n in geo["provincias"].items()}
        self.indice: dict[tuple[str, str], tuple[str, str]] = {}
        for ine, nombre in geo["municipios"]:
            for variante in self.variantes(nombre):
                self.indice.setdefault((ine[:2], variante), (ine, nombre))

    @staticmethod
    def variantes(nombre: str):
        n = norm(nombre)
        yield n
        for parte in re.split(r"\s*/\s*", n):
            yield parte
        if (m := re.fullmatch(r"(.+?),?\s*\((la|el|los|las|l')\)", n)) or (m := re.fullmatch(r"(.+?),\s*(la|el|los|las)", n)):
            yield f"{m[2]} {m[1]}"

    def buscar(self, localidad: str | None, provincia: str | None):
        cod = self.provincias.get(norm(provincia))
        if not localidad or not cod:
            return None, None
        for v in self.variantes(localidad):
            if (cod, v) in self.indice:
                return self.indice[(cod, v)]
        return None, None


# --------------------------------------------------------------------------- lectura de las páginas guardadas

def campos_de_pagina(arbol, lote_pagina: str | None):
    """Recorre #contenido en orden y devuelve cada fila th → td con su contexto: lote (de la página o de un «Lote N»),
    bien («Bien N - …») y sección (el último encabezado h3/h4/h5)."""
    principal = arbol.find(".//*[@id='contenido']")
    if principal is None:
        return []
    filas, ctx = [], {"lote": lote_pagina, "bien": None, "seccion": None}

    def recorrer(e):
        if e.tag in {"script", "style"}:
            return
        if e.tag in {"h3", "h4", "h5"}:
            t = texto(e)
            if (m := RE_BIEN.fullmatch(t)):
                ctx["bien"] = int(m[1])
            elif (m := RE_LOTE.match(t)):
                ctx["lote"], ctx["bien"] = m[1], None
            else:
                ctx["bien"] = None
            ctx["seccion"] = t
        if e.tag == "tr":
            celdas = [c for c in e if c.tag in {"th", "td"}]
            if len(celdas) >= 2 and celdas[0].tag == "th" and any(c.tag == "td" for c in celdas):
                filas.append(dict(lote=ctx["lote"], bien=ctx["bien"], seccion=ctx["seccion"], etiqueta=texto(celdas[0]),
                                  valor=" | ".join(texto(c) for c in celdas[1:])))
            return
        for h in e:
            recorrer(h)

    recorrer(principal)
    return filas


def pujas_de_pagina(arbol, lote_pagina: str | None):
    """Pestaña Pujas: lista de {lote, texto, importe}. La tabla «Pujas máximas actuales» da una fila por lote; si no,
    el texto bajo «Puja máxima actual…» («La subasta no ha recibido pujas», «…debe acceder como usuario registrado»)."""
    principal = arbol.find(".//*[@id='contenido']")
    if principal is None:
        return []
    salida = []
    for tabla in principal.iter("table"):
        for fila in tabla.iter("tr"):
            celdas = [texto(c) for c in fila if c.tag == "td"]
            if len(celdas) >= 2 and re.fullmatch(r"\d+", celdas[0]):
                salida.append(dict(lote=celdas[0], texto=celdas[1], importe=importe(celdas[1])))
    if salida:
        return salida
    textos = [texto(p) for p in principal.iter("p") if texto(p) and not norm(texto(p)).startswith(TEXTOS_PUJA_IGNORADOS)]
    return [dict(lote=lote_pagina or "", texto=" ".join(textos), importe=None)] if textos else []


# --------------------------------------------------------------------------- construcción

def construir(raiz: Path, ruta_base: Path, geo: Municipios | None = None) -> dict:
    geo = geo or Municipios()
    ruta_ubic = raiz / "geo" / "ubicaciones.json"
    ubicaciones = json.loads(ruta_ubic.read_text(encoding="utf-8")) if ruta_ubic.exists() else {}
    tmp = ruta_base.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    ruta_base.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(ESQUEMA)
    vistas: dict[str, list[str]] = defaultdict(list)  # SUB → capturas en que salió
    ultima_por_prov: dict[str, str] = {}
    for listado in sorted((raiz / "codigos").glob("*/*/listado.json")):
        d = json.loads(listado.read_text(encoding="utf-8"))
        captura = f"{listado.parent.parent.name}/{listado.parent.name}"
        ultima_por_prov[listado.parent.parent.name] = captura
        con.execute("INSERT INTO capturas VALUES (?,?,?,?,?,?,0)",
                    (captura, d.get("cod_provincia"), d.get("fecha_captura"), d.get("total_resultados"),
                     d.get("url_consulta"), ",".join(p.get("sha256", "") for p in d.get("paginas", []))))
        for i, s in enumerate(d.get("subastas", []), 1):
            con.execute("INSERT INTO en_captura VALUES (?,?,?)", (captura, s["sub"], i))
            vistas[s["sub"]].append(captura)
    con.executemany("UPDATE capturas SET ultima=1 WHERE captura=?", [(c,) for c in ultima_por_prov.values()])
    activas = set(ultima_por_prov.values())
    for dir_sub in sorted(p for p in (raiz / "fichas").glob("SUB-*") if p.is_dir()):
        carpetas = sorted(p for p in dir_sub.iterdir() if p.is_dir() and (p / "ficha.json").exists())
        for i, carpeta in enumerate(carpetas):
            f = json.loads((carpeta / "ficha.json").read_text(encoding="utf-8"))
            ultima = i == len(carpetas) - 1
            con.execute("INSERT INTO fichas VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (str(carpeta.relative_to(raiz)), f["sub"], f.get("consultado_utc"), f.get("captura_codigos"),
                         f.get("peticiones"), len(f.get("paginas", [])), len(f.get("adjuntos", [])),
                         len(f.get("anuncios", [])), len(f.get("pendientes", [])), int(ultima)))
            if ultima:
                cargar_ficha(con, raiz, carpeta, f, geo, vistas.get(f["sub"], []), activas, ubicaciones)
    con.commit()
    resumen = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
               for t in ("capturas", "fichas", "subastas", "lotes", "bienes", "campos", "documentos", "anuncios", "pendientes")}
    con.execute("VACUUM")
    con.close()
    tmp.replace(ruta_base)
    return resumen


def cargar_ficha(con, raiz: Path, carpeta: Path, f: dict, geo: Municipios, vistas: list[str], activas: set[str],
                 ubicaciones: dict | None = None):
    sub = f["sub"]
    rel = str(carpeta.relative_to(raiz))
    vistos, campos = set(), []
    pujas: dict[str, dict] = {}
    for pagina in f.get("paginas", []):
        ruta = raiz / pagina["archivo"]
        if not ruta.exists():
            continue
        arbol = leer_html(ruta)
        for c in campos_de_pagina(arbol, pagina.get("lote")):
            clave = (pagina["pestana"], c["lote"], c["bien"], c["seccion"], c["etiqueta"], c["valor"])
            if clave in vistos:  # la pestaña Bienes sin lote repite el lote 1
                continue
            vistos.add(clave)
            campos.append(dict(c, pestana=pagina["pestana"], archivo=pagina["archivo"], url=pagina.get("url_final"),
                               consultado_utc=pagina.get("consultado_utc")))
        if pagina["pestana"] == "5":
            for p in pujas_de_pagina(arbol, pagina.get("lote")):
                if p["lote"] not in pujas or pagina.get("lote"):  # la página de cada lote manda sobre la tabla
                    pujas[p["lote"]] = dict(p, archivo=pagina["archivo"], url=pagina.get("url_final"))
    for orden, c in enumerate(campos, 1):
        con.execute("INSERT INTO campos VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sub, c["pestana"], c["lote"], c["bien"], c["seccion"], c["etiqueta"], c["valor"], orden, c["archivo"],
                     c["url"], c["consultado_utc"]))

    def primero(pestana, mapa):
        out = {}
        for c in campos:
            col = mapa.get(norm(c["etiqueta"]))
            if c["pestana"] == pestana and col and col not in out:
                out[col] = c["valor"]
        return out

    general, autoridad = primero("1", MAPA_GENERAL), primero("2", MAPA_AUTORIDAD)

    # bienes
    bienes: dict[tuple, dict] = {}
    for c in campos:
        if c["pestana"] != "3" or c["bien"] is None:
            continue
        b = bienes.setdefault((c["lote"] or "", c["bien"]), dict(archivo=c["archivo"], url=c["url"], seccion=c["seccion"]))
        col = MAPA_BIEN.get(norm(c["etiqueta"]))
        if col and col not in b:
            b[col] = c["valor"]
    for (lote, n), b in bienes.items():
        cab = RE_BIEN.fullmatch(b["seccion"] or "")
        clase_sub = RE_CLASE.fullmatch(cab[2]) if cab and cab[2] else None
        clase, subtipo = (clase_sub[1], clase_sub[2]) if clase_sub else ((cab[2] if cab else None), None)
        ine, oficial = geo.buscar(b.get("localidad"), b.get("provincia"))
        b.update(clase=clase, subtipo=subtipo, municipio=oficial, municipio_ine=ine, cargas_importe=importe(b.get("cargas")))
        u = (ubicaciones or {}).get(f"{sub}|{lote}|{n}") or {}
        b.update(lat=u.get("lat"), lon=u.get("lon"), ubicacion_precision=u.get("precision"), ubicacion_fuente=u.get("fuente"),
                 ubicacion_url=u.get("url"), ubicacion_texto=u.get("texto"), ubicacion_motivo=u.get("motivo"))
        cols = ["clase", "subtipo", *dict.fromkeys(MAPA_BIEN.values()), "cargas_importe", "municipio", "municipio_ine",
                "archivo", "url", "lat", "lon", "ubicacion_precision", "ubicacion_fuente", "ubicacion_url",
                "ubicacion_texto", "ubicacion_motivo"]
        con.execute(f"INSERT INTO bienes (sub, lote, bien, {', '.join(cols)}) VALUES ({', '.join('?' * (len(cols) + 3))})",
                    (sub, lote, n, *[b.get(c) for c in cols]))

    # lotes: los de la pestaña Bienes («Datos relacionados con la subasta del lote N») o uno solo con la pestaña 1
    lotes: dict[str, dict] = {}
    for c in campos:
        if c["pestana"] == "3" and c["bien"] is None and c["lote"]:
            col = MAPA_LOTE.get(norm(c["etiqueta"]))
            l = lotes.setdefault(c["lote"], dict(archivo=c["archivo"], url=c["url"]))
            if col and f"{col}_texto" not in l:
                l[f"{col}_texto"] = c["valor"]
    if not lotes:
        lotes[""] = {f"{k}_texto": general.get(f"{k}_texto") for k in IMPORTES_LOTE}
        primera = next((p for p in f.get("paginas", []) if p["pestana"] == "1"), {})
        lotes[""].update(archivo=primera.get("archivo"), url=primera.get("url_final"))
    valores_lote = [v for l in lotes.values() if (v := importe(l.get("valor_subasta_texto"))) is not None]
    for lote, l in lotes.items():
        p = pujas.get(lote) or (pujas.get("") if len(lotes) == 1 else None) or {}
        con.execute("INSERT INTO lotes VALUES (?,?," + ",".join("?" * (2 * len(IMPORTES_LOTE))) + ",?,?,?,?,?)",
                    (sub, lote, *[v for k in IMPORTES_LOTE for v in (importe(l.get(f"{k}_texto")), l.get(f"{k}_texto"))],
                     p.get("texto"), p.get("importe"), sum(1 for (bl, _) in bienes if bl == lote),
                     l.get("archivo"), l.get("url")))

    # documentos, anuncios, pendientes
    for d in f.get("adjuntos", []):
        con.execute("INSERT INTO documentos VALUES (?,?,?,?,?,?,?,?,?)",
                    (sub, d.get("etiqueta"), d.get("lote"), d.get("archivo"), d.get("url_final") or d.get("url"),
                     d.get("sha256"), d.get("tipo_mime"), d.get("bytes"), d.get("consultado_utc")))
    for a in f.get("anuncios", []):
        arch = {Path(x["archivo"]).suffix: x for x in a.get("archivos", [])}
        con.execute("INSERT OR REPLACE INTO anuncios VALUES (?,?,?,?,?,?,?,?)",
                    (sub, a["anuncio"], a.get("publicado"), f"https://www.boe.es/diario_boe/txt.php?id={a['anuncio']}",
                     (arch.get(".xml") or {}).get("url_final"), (arch.get(".xml") or {}).get("archivo"),
                     (arch.get(".pdf") or {}).get("archivo"), a.get("aviso")))
    pend = f.get("pendientes", [])
    for p in pend:
        con.execute("INSERT INTO pendientes VALUES (?,?,?,?,?)",
                    (sub, p.get("tipo"), p.get("etiqueta") or p.get("anuncio"), p.get("url"),
                     p.get("motivo") or json.dumps({k: v for k, v in p.items() if k != "tipo"}, ensure_ascii=False)))

    # subasta (fila plana)
    def unicos(clave):
        return ", ".join(dict.fromkeys(str(b[clave]) for b in bienes.values() if b.get(clave))) or None

    textos_puja = [p["texto"] for p in pujas.values() if p.get("texto")]
    con_pujas = any(p.get("importe") or "ha recibido alguna puja" in norm(p.get("texto")) for p in pujas.values())
    desc = next((b.get("descripcion") for b in bienes.values() if b.get("descripcion")), None)
    fila = dict(
        sub=sub, grupo=GRUPO_SUB.get(sub.split("-")[1], "Otras administraciones"), tipo_subasta=general.get("tipo_subasta"),
        activa=int(bool(set(vistas) & activas)), vista_en=",".join(vistas) or None,
        provincias_busqueda=",".join(dict.fromkeys(v.split("/")[0] for v in vistas)) or None,
        primera_vez=vistas[0] if vistas else None, ultima_vez=vistas[-1] if vistas else None,
        fecha_inicio=fecha_iso(general.get("fecha_inicio_texto")), fecha_conclusion=fecha_iso(general.get("fecha_conclusion_texto")),
        lotes_texto=general.get("lotes_texto"), n_lotes=len(lotes),
        valor_lotes_min=min(valores_lote, default=None), valor_lotes_max=max(valores_lote, default=None),
        **{k: v for c in IMPORTES_LOTE for k, v in ((c, importe(general.get(f"{c}_texto"))), (f"{c}_texto", general.get(f"{c}_texto")))},
        anuncio_boe=general.get("anuncio_boe"), cuenta_expediente=general.get("cuenta_expediente"),
        forma_adjudicacion=general.get("forma_adjudicacion"), **autoridad,
        n_bienes=len(bienes), clases_bien=unicos("clase"), es_inmueble=int(any(b.get("clase") == "Inmueble" for b in bienes.values())),
        subtipos=unicos("subtipo"), municipios=unicos("municipio") or unicos("localidad"), municipios_ine=unicos("municipio_ine"),
        provincias=unicos("provincia"), codigos_postales=unicos("codigo_postal"),
        descripcion=(desc[:400] + "…") if desc and len(desc) > 400 else desc,
        pujas=" · ".join(dict.fromkeys(textos_puja)) or None, con_pujas=int(con_pujas),
        n_documentos=len(f.get("adjuntos", [])), n_pendientes=len(pend),
        acceso_restringido=int(any(p.get("tipo") == "acceso_restringido" for p in pend)),
        url_ficha=f"https://subastas.boe.es/detalleSubasta.php?idSub={sub}", consultado_utc=f.get("consultado_utc"), carpeta=rel)
    cols = list(fila)
    con.execute(f"INSERT INTO subastas ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [fila[c] for c in cols])


# --------------------------------------------------------------------------- tablas del panel

PANEL_SQL = {
    "sp_subastas": "SELECT * FROM subastas ORDER BY activa DESC, fecha_conclusion, sub",
    "sp_lotes": "SELECT * FROM lotes ORDER BY sub, CAST(lote AS INTEGER)",
    "sp_bienes": "SELECT * FROM bienes ORDER BY sub, CAST(lote AS INTEGER), bien",
    "sp_campos": f"SELECT sub, pestana, lote, bien, seccion, etiqueta, valor, orden, url FROM campos "
                 f"WHERE pestana <> '{PESTANA_PERSONAL}' ORDER BY sub, orden",
    # documentos del Portal y anuncios del BOE, con su URL oficial (la copia guardada está en el repositorio)
    "sp_documentos": "SELECT sub, 'Documento del Portal' AS tipo, etiqueta, lote, url, tipo_mime, bytes FROM documentos "
                     "UNION ALL SELECT sub, 'Anuncio del BOE', anuncio || ' · publicado el ' || publicado, NULL, url, "
                     "'text/html', NULL FROM anuncios ORDER BY 1, 2 DESC, 3",
    "sp_pendientes": "SELECT * FROM pendientes ORDER BY sub, tipo",
    "sp_capturas": "SELECT captura, provincia, fecha_captura, total, url_consulta, ultima FROM capturas ORDER BY captura",
}


def exportar_panel(ruta_base: Path, destino: Path) -> dict:
    """Cada tabla del panel como JSON {tabla, generado, fuente, columns, rows} y su copia gzip+base64 para Drive."""
    destino.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{ruta_base}?mode=ro", uri=True)
    generado = portal_codigos.ahora_madrid().isoformat(timespec="seconds")
    tamanos = {}
    for nombre, sql in PANEL_SQL.items():
        cur = con.execute(sql)
        doc = dict(tabla=nombre, generado=generado, fuente=FUENTE, columns=[c[0] for c in cur.description],
                   rows=[list(r) for r in cur.fetchall()])
        txt = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        (destino / f"sema-panel-{nombre}.json").write_text(txt, encoding="utf-8")
        b64 = base64.b64encode(gzip.compress(txt.encode("utf-8"), 9, mtime=0)).decode("ascii")
        (destino / f"sema-panel-{nombre}.b64.txt").write_text(b64, encoding="ascii")
        tamanos[nombre] = dict(filas=len(doc["rows"]), bytes_b64=len(b64))
    con.close()
    return tamanos


def copia_comprimida(origen: Path, destino: Path) -> dict:
    """Copia de la base para la carpeta de Drive «Sema Deal Finder»: hecha con la API de copia de SQLite (coherente),
    comprobada con integrity_check y comprimida con gzip sin fecha en la cabecera (mismo contenido, mismo archivo). Va
    comprimida porque el conector de Drive admite unos 3 MB por envío."""
    import shutil
    import tempfile
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        copia = Path(tmp) / origen.name
        src, dst = sqlite3.connect(f"file:{origen}?mode=ro", uri=True), sqlite3.connect(copia)
        try:
            src.backup(dst)
            estado = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            src.close()
            dst.close()
        if estado != "ok":
            raise RuntimeError(f"{origen.name} no pasa integrity_check: {estado}")
        with open(copia, "rb") as f, open(destino, "wb") as salida, gzip.GzipFile(origen.name, "wb", 9, salida, mtime=0) as gz:
            shutil.copyfileobj(f, gz)
        sin_comprimir = copia.stat().st_size
    datos = destino.read_bytes()
    return dict(archivo=str(destino), bytes=len(datos), bytes_sin_comprimir=sin_comprimir,
                sha256=hashlib.sha256(datos).hexdigest())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Rehace datos/subastas.sqlite, las tablas del panel y la copia comprimida "
                                            "de la base para Drive, a partir de lo descargado del Portal. No hace peticiones.")
    p.add_argument("--raiz", type=Path, default=RAIZ)
    p.add_argument("--base", type=Path, default=BASE_DATOS)
    p.add_argument("--panel", type=Path, default=PANEL)
    a = p.parse_args(argv)
    resumen = {"base": construir(a.raiz, a.base), "panel": exportar_panel(a.base, a.panel)}
    resumen["copia_base"] = copia_comprimida(a.base, a.panel / "subastas.sqlite.gz")
    print(json.dumps(resumen, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

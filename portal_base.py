"""Base de datos de las subastas del Portal a partir de lo descargado (método nuevo, paso 3).

No hace ninguna petición: lee lo que guardaron portal_codigos.py (datos/portal/codigos) y portal_fichas.py
(datos/portal/fichas, anuncios y documentos) y lo vuelca en datos/subastas.sqlite, que se rehace entera en cada
ejecución (es un derivado: la fuente de verdad son las páginas guardadas).

Tablas:
- capturas: cada búsqueda de códigos (momento, provincia, total, URL de la consulta).
- en_captura: qué SUB salió en cada búsqueda (historial de cuándo estuvo «Celebrándose»).
- fichas: cada descarga de ficha (SUB, carpeta, momento, captura de códigos, peticiones, pendientes).
- campos: TODOS los pares etiqueta → valor de las tablas de cada pestaña, con su pestaña, lote, bien, sección,
  archivo y URL. Es la información completa y trazable de cada subasta.
- bienes: cada bien (lote y número) con sus campos principales.
- documentos, anuncios, pendientes: lo que trae ficha.json.
- subastas: una fila por SUB con los datos principales, sacados de `campos` según MAPA_SUBASTA (vista plana para el
  panel). Los importes se guardan como número (euros) y además con el texto original.

Uso: python portal_base.py [--raiz datos/portal] [--base datos/subastas.sqlite]
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
from pathlib import Path

import portal_codigos

BASE = Path(__file__).resolve().parent
RAIZ = BASE / "datos" / "portal"
BASE_DATOS = BASE / "datos" / "subastas.sqlite"

ESQUEMA = """
CREATE TABLE capturas (captura TEXT PRIMARY KEY, provincia TEXT, fecha_captura TEXT, total INTEGER, url_consulta TEXT,
                       sha256 TEXT);
CREATE TABLE en_captura (captura TEXT, sub TEXT, orden INTEGER, PRIMARY KEY (captura, sub));
CREATE TABLE fichas (carpeta TEXT PRIMARY KEY, sub TEXT, consultado_utc TEXT, captura_codigos TEXT, peticiones INTEGER,
                     paginas INTEGER, adjuntos INTEGER, anuncios INTEGER, pendientes INTEGER, ultima INTEGER);
CREATE TABLE campos (sub TEXT, carpeta TEXT, pestana TEXT, lote TEXT, bien INTEGER, seccion TEXT, etiqueta TEXT,
                     valor TEXT, orden INTEGER, archivo TEXT, url TEXT, consultado_utc TEXT);
CREATE TABLE bienes (sub TEXT, lote TEXT, bien INTEGER, tipo TEXT, descripcion TEXT, referencia_catastral TEXT,
                     direccion TEXT, codigo_postal TEXT, localidad TEXT, provincia TEXT, archivo TEXT, url TEXT,
                     PRIMARY KEY (sub, lote, bien));
CREATE TABLE documentos (sub TEXT, etiqueta TEXT, lote TEXT, archivo TEXT, url TEXT, sha256 TEXT, tipo_mime TEXT,
                         bytes INTEGER, consultado_utc TEXT);
CREATE TABLE anuncios (sub TEXT, anuncio TEXT, publicado TEXT, url_xml TEXT, archivo_xml TEXT, archivo_pdf TEXT,
                       aviso TEXT, PRIMARY KEY (sub, anuncio));
CREATE TABLE pendientes (sub TEXT, tipo TEXT, etiqueta TEXT, url TEXT, motivo TEXT);
"""
RE_LOTE = re.compile(r"Lote\s+(\d+)\b", re.I)
RE_BIEN = re.compile(r"Bien\s+(\d+)(?:\s*[-–]\s*(.+))?", re.I)
RE_IMPORTE = re.compile(r"^-?[\d.]+(?:,\d+)?\s*€?$")


def texto(e) -> str:
    return " ".join(" ".join(e.itertext()).split())


def importe(valor: str | None) -> float | None:
    """'123.456,78 €' → 123456.78. Lo que no es un importe (p. ej. «Sin puja mínima») da None."""
    if not valor:
        return None
    v = valor.replace("\xa0", " ").strip()
    if not RE_IMPORTE.match(v):
        return None
    return float(v.replace("€", "").replace(".", "").replace(",", ".").strip())


def leer_html(ruta: Path):
    datos = ruta.read_bytes()
    if ruta.suffix == ".gz":
        datos = gzip.decompress(datos)
    return portal_codigos.PaginaPortal().leer(datos.decode("utf-8-sig"))


def campos_de_pagina(arbol, lote_pagina: str | None):
    """Recorre #contenido en orden y devuelve cada fila de tabla th → td con su contexto (lote, bien, sección)."""
    principal = arbol.find(".//*[@id='contenido']")
    if principal is None:
        return []
    filas, estado = [], {"lote": lote_pagina, "bien": None, "seccion": None}

    def recorrer(e):
        if e.tag in {"script", "style"}:
            return
        if e.tag in {"h3", "h4", "h5"}:
            t = texto(e)
            if (m := RE_BIEN.fullmatch(t)):
                estado["bien"] = int(m[1])
            elif (m := RE_LOTE.match(t)):
                estado["lote"], estado["bien"] = m[1], None
            estado["seccion"] = t
        if e.tag == "tr":
            celdas = [c for c in e if c.tag in {"th", "td"}]
            if len(celdas) >= 2 and celdas[0].tag == "th":
                filas.append(dict(lote=estado["lote"], bien=estado["bien"], seccion=estado["seccion"],
                                  etiqueta=texto(celdas[0]), valor=" | ".join(texto(c) for c in celdas[1:])))
            return
        for h in e:
            recorrer(h)

    recorrer(principal)
    return filas


def construir(raiz: Path, ruta_base: Path) -> dict:
    tmp = ruta_base.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(ESQUEMA)
    # capturas de códigos
    for listado in sorted((raiz / "codigos").glob("*/*/listado.json")):
        d = json.loads(listado.read_text(encoding="utf-8"))
        captura = f"{listado.parent.parent.name}/{listado.parent.name}"
        con.execute("INSERT INTO capturas VALUES (?,?,?,?,?,?)",
                    (captura, d.get("cod_provincia"), d.get("fecha_captura"), d.get("total_resultados"),
                     d.get("url_consulta"), ",".join(p.get("sha256", "") for p in d.get("paginas", []))))
        con.executemany("INSERT INTO en_captura VALUES (?,?,?)",
                        [(captura, s["sub"], i) for i, s in enumerate(d.get("subastas", []), 1)])
    # fichas: todas se registran; los campos, bienes, etc. solo de la última de cada SUB
    for dir_sub in sorted(p for p in (raiz / "fichas").glob("SUB-*") if p.is_dir()):
        carpetas = sorted(p for p in dir_sub.iterdir() if p.is_dir() and (p / "ficha.json").exists())
        for i, carpeta in enumerate(carpetas):
            f = json.loads((carpeta / "ficha.json").read_text(encoding="utf-8"))
            rel = str(carpeta.relative_to(raiz))
            ultima = i == len(carpetas) - 1
            con.execute("INSERT INTO fichas VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rel, f["sub"], f.get("consultado_utc"), f.get("captura_codigos"), f.get("peticiones"),
                         len(f.get("paginas", [])), len(f.get("adjuntos", [])), len(f.get("anuncios", [])),
                         len(f.get("pendientes", [])), int(ultima)))
            if ultima:
                cargar_ficha(con, raiz, rel, f)
    con.commit()
    resumen = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
               for t in ("capturas", "fichas", "campos", "bienes", "documentos", "anuncios", "pendientes")}
    con.execute("VACUUM")
    con.close()
    tmp.replace(ruta_base)
    return resumen


def cargar_ficha(con, raiz: Path, carpeta: str, f: dict):
    sub = f["sub"]
    for pagina in f.get("paginas", []):
        ruta = raiz / pagina["archivo"]
        if not ruta.exists():
            continue
        for orden, c in enumerate(campos_de_pagina(leer_html(ruta), pagina.get("lote")), 1):
            con.execute("INSERT INTO campos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sub, carpeta, pagina["pestana"], c["lote"], c["bien"], c["seccion"], c["etiqueta"],
                         c["valor"], orden, pagina["archivo"], pagina.get("url_final"), pagina.get("consultado_utc")))
    for u in f.get("ubicaciones_bienes", []):
        con.execute("INSERT OR REPLACE INTO bienes (sub, lote, bien, tipo, direccion, codigo_postal, localidad, provincia,"
                    " archivo, url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sub, u.get("lote"), u.get("bien"), u.get("tipo"), u.get("direccion"), u.get("codigo_postal"),
                     u.get("localidad"), u.get("provincia"), u["fuente"]["archivo"], u["fuente"]["url"]))
    for d in f.get("adjuntos", []):
        con.execute("INSERT INTO documentos VALUES (?,?,?,?,?,?,?,?,?)",
                    (sub, d.get("etiqueta"), d.get("lote"), d.get("archivo"), d.get("url_final") or d.get("url"),
                     d.get("sha256"), d.get("tipo_mime"), d.get("bytes"), d.get("consultado_utc")))
    for a in f.get("anuncios", []):
        archivos = {Path(x["archivo"]).suffix: x for x in a.get("archivos", [])}
        con.execute("INSERT OR REPLACE INTO anuncios VALUES (?,?,?,?,?,?,?)",
                    (sub, a["anuncio"], a.get("publicado"), (archivos.get(".xml") or {}).get("url_final"),
                     (archivos.get(".xml") or {}).get("archivo"), (archivos.get(".pdf") or {}).get("archivo"),
                     a.get("aviso")))
    for p in f.get("pendientes", []):
        con.execute("INSERT INTO pendientes VALUES (?,?,?,?,?)",
                    (sub, p.get("tipo"), p.get("etiqueta") or p.get("anuncio"), p.get("url"),
                     p.get("motivo") or json.dumps({k: v for k, v in p.items() if k != "tipo"}, ensure_ascii=False)))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Rehace datos/subastas.sqlite a partir de lo descargado del Portal.")
    p.add_argument("--raiz", type=Path, default=RAIZ)
    p.add_argument("--base", type=Path, default=BASE_DATOS)
    a = p.parse_args(argv)
    print(json.dumps(construir(a.raiz, a.base), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

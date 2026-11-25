"""SeMá Deal Finder: subastas de inmuebles con datos solo del BOE.

Fuentes, y ninguna más:
- Diario oficial del BOE: la API de datos abiertos del sumario
  (https://www.boe.es/datosabiertos/api/boe/sumario/AAAAMMDD) y cada anuncio: su XML (url_xml) si el
  robots.txt del BOE lo permite y, si no, su página HTML oficial (url_html), con el mismo texto. Se descargan
  de forma automática, de una en una y con pausa.
- Portal de Subastas del BOE (subastas.boe.es): fichas y documentos obtenidos por consultas puntuales,
  que se guardan a mano en datos/portal/entrada, o por un canal autorizado por la AEBOE. Este módulo no
  descarga nada del Portal: su robots.txt no permite el acceso automatizado.

Lo que no consta en esas fuentes se deja pendiente, con el dato que falta, el SUB y lote, la fuente
oficial revisada y el motivo. Los valores de Subastech (datos/subastech.sqlite) nunca se presentan como
verificados en el BOE: solo se cotejan con lo que diga la fuente oficial.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import gzip
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import sema
from sema import ahora, norm, sha256

VERSION = "1.0.0"
PROVINCIAS_AMBITO = {"28": "Madrid", "08": "Barcelona", "41": "Sevilla", "21": "Huelva"}
SECCIONES = ("4", "5B", "5C")
URL_BOE = "https://www.boe.es"
MAX_XML_POR_EJECUCION = int(os.environ.get("SEMA_DF_MAX_XML", "1500"))
MAX_INTENTOS = 8
DIAS_PRIMERA_CARGA = sema.DIAS_PRIMERA_CARGA
RE_SUB = re.compile(r"(?<![A-Z0-9])SUB-[A-Z]{2}-\d{4}-[A-Z0-9]+", re.I)
RE_REFERENCIA = re.compile(r"con\s+n[úu]mero\s+de\s+([a-záéíóú]+)\s+(.{1,60}?)\s*\.(?:\s|$)", re.I)
RE_FECHA_LARGA = re.compile(r"\b(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(\d{4})\b", re.I)
MESES = {m: i for i, m in enumerate(["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO",
                                       "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"], 1)}
RE_INMUEBLE = re.compile(r"\b(INMUEBLES?|FINCAS?|VIVIENDAS?|LOCAL(ES)? COMERCIAL(ES)?|SOLAR(ES)?|PARCELAS?|NAVES?|"
                         r"GARAJES?|TRASTEROS?|URBANAS?|RUSTICAS?|EDIFICIOS?|PISOS?|TERRENOS?)\b")
FUENTE_DIARIO = "BOE · diario oficial (API de datos abiertos del sumario)"
FUENTE_ANUNCIO = {"xml": "BOE · diario oficial (XML del anuncio)", "html": "BOE · diario oficial (página HTML del anuncio)"}
FUENTE_PORTAL = "BOE · Portal de Subastas (consulta puntual)"
# Incidencias que son fallos y se reintentan; las demás (dia_sin_sumario, subasta_sin_sub) son registros
TIPOS_ERROR = {"error_sumario", "fecha_no_coincide", "error_descarga", "id_no_coincide", "ficha_sub_no_coincide", "pdf_no_valido"}

ESQUEMA = """
CREATE TABLE IF NOT EXISTS ejecuciones (
  id_ejecucion INTEGER PRIMARY KEY AUTOINCREMENT, inicio TEXT, fin TEXT, version TEXT, parametros TEXT,
  n_leidos INTEGER DEFAULT 0, n_nuevos INTEGER DEFAULT 0, n_cambios INTEGER DEFAULT 0,
  avisos TEXT, errores TEXT, estado TEXT
);
CREATE TABLE IF NOT EXISTS respuestas_brutas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, id_ejecucion INTEGER, url TEXT,
  momento_consulta TEXT, http_status INTEGER, sha256 TEXT, ruta TEXT, bytes INTEGER
);
CREATE TABLE IF NOT EXISTS cambios_historicos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tabla TEXT, clave TEXT, campo TEXT,
  valor_anterior TEXT, valor_nuevo TEXT, detectado_en TEXT, id_ejecucion INTEGER, fuente TEXT
);
-- Un registro por día pedido a la API del sumario
CREATE TABLE IF NOT EXISTS sumarios (
  fecha TEXT PRIMARY KEY, estado TEXT, formato TEXT, http_status INTEGER, fecha_en_sumario TEXT,
  diarios TEXT, n_items INTEGER, n_items_secciones INTEGER, url TEXT, sha256 TEXT, ruta TEXT,
  intentos INTEGER DEFAULT 0, momento_consulta TEXT, id_ejecucion INTEGER
);
-- Cada anuncio de las secciones IV, V.B y V.C, con su XML
CREATE TABLE IF NOT EXISTS anuncios (
  id_boe TEXT PRIMARY KEY, fecha_publicacion TEXT, diario_numero TEXT, seccion_codigo TEXT,
  seccion_nombre TEXT, departamento TEXT, departamento_codigo TEXT, epigrafe TEXT, grupo_texto TEXT,
  titulo_sumario TEXT, control TEXT, url_html TEXT, url_pdf TEXT, url_xml TEXT,
  xml_estado TEXT, xml_intentos INTEGER DEFAULT 0, texto_desde TEXT,
  titulo TEXT, organismo TEXT, fecha_actualizacion TEXT, fecha_actualizacion_texto TEXT,
  pagina_inicial TEXT, pagina_final TEXT, enlaces_oficiales TEXT, texto TEXT,
  menciona_subasta INTEGER, es_inmueble INTEGER, n_sub INTEGER, ids_subasta TEXT,
  tipo_subasta_texto TEXT, referencia_tipo TEXT, referencia_texto TEXT,
  lugar_firma TEXT, fecha_firma TEXT, sede_provincia TEXT, sede_motivo TEXT,
  ubicacion_estado TEXT, ubicacion_provincias TEXT, ubicacion_municipios TEXT, ubicacion_evidencia TEXT,
  fuente TEXT, url_fuente TEXT, momento_consulta TEXT, nivel_verificacion TEXT, id_ejecucion INTEGER
);
CREATE TABLE IF NOT EXISTS anuncio_sub (
  id_boe TEXT, id_subasta TEXT, en_titulo INTEGER, en_texto INTEGER, en_enlace INTEGER,
  url_enlace TEXT, fragmento TEXT, momento_consulta TEXT,
  PRIMARY KEY (id_boe, id_subasta)
);
-- Un expediente por SUB (todos los anuncios que lo citan) o por anuncio sin SUB que trate de inmuebles
CREATE TABLE IF NOT EXISTS expedientes (
  id_expediente TEXT PRIMARY KEY, id_subasta TEXT, prefijo TEXT, con_ficha_portal INTEGER,
  anuncios TEXT, n_anuncios INTEGER, primer_anuncio TEXT, fecha_primer_anuncio TEXT,
  ultimo_anuncio TEXT, fecha_ultimo_anuncio TEXT, url_portal_publicada TEXT,
  organismo TEXT, tipo_subasta_anuncio TEXT, referencia_anuncio TEXT, lugar_firma TEXT,
  sede_provincia TEXT, sede_motivo TEXT,
  ubicacion_estado TEXT, provincias_bien TEXT, ambito TEXT, ficha_consultada INTEGER DEFAULT 0,
  fuente TEXT, url_fuente TEXT, momento_consulta TEXT, nivel_verificacion TEXT
);
CREATE TABLE IF NOT EXISTS incidencias (
  tipo TEXT, clave TEXT, fecha TEXT, url TEXT, detalle TEXT,
  primera_vez TEXT, ultima_vez TEXT, veces INTEGER DEFAULT 1, resuelta_en TEXT,
  PRIMARY KEY (tipo, clave)
);
-- Portal de Subastas: archivos guardados en consultas puntuales
CREATE TABLE IF NOT EXISTS capturas (
  id_captura INTEGER PRIMARY KEY AUTOINCREMENT, archivo TEXT UNIQUE, sha256 TEXT, clase TEXT,
  url TEXT, fecha_consulta TEXT, id_subasta_esperado TEXT, id_subasta_encabezado TEXT, coincide INTEGER,
  n_lote TEXT, estado TEXT, detalle TEXT, procesado_en TEXT
);
-- Un dato por fila, con su evidencia: URL, fecha de consulta y pestaña/fila o página de origen
CREATE TABLE IF NOT EXISTS campos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, id_subasta TEXT, n_lote TEXT, n_bien TEXT, campo TEXT,
  valor TEXT, valor_num REAL, clase_valor TEXT, texto_origen TEXT, url TEXT, fecha_consulta TEXT,
  origen TEXT, id_captura INTEGER, revisar INTEGER DEFAULT 0, nota TEXT, fuente TEXT
);
CREATE INDEX IF NOT EXISTS campos_sub ON campos (id_subasta, campo);
CREATE TABLE IF NOT EXISTS documentos (
  id_captura INTEGER PRIMARY KEY, id_subasta TEXT, n_lote TEXT, clase TEXT, titulo TEXT, url TEXT,
  pdf_valido INTEGER, paginas INTEGER, metodo_texto TEXT, revisar INTEGER, detalle TEXT
);
-- Se regeneran en cada ejecución. pendientes: una fila por expediente, lote y motivo, con la lista de datos
-- que faltan; dónde consta cada dato está en datos_ficha.
CREATE TABLE IF NOT EXISTS pendientes (
  id_expediente TEXT, id_subasta TEXT, n_lote TEXT, datos TEXT, n_datos INTEGER, motivo TEXT,
  fuente_revisada TEXT, con_valor_subastech TEXT, generado_en TEXT
);
CREATE TABLE IF NOT EXISTS datos_ficha (dato TEXT PRIMARY KEY, donde_consta TEXT, en_ficha_publica INTEGER);
CREATE TABLE IF NOT EXISTS cotejo_subastech (
  id_subasta TEXT, n_lote TEXT, campo TEXT, valor_subastech TEXT, valor_boe TEXT, resultado TEXT,
  evidencia_boe TEXT, url_subastech TEXT, generado_en TEXT
);
DROP VIEW IF EXISTS v_bienes;
CREATE VIEW v_bienes AS
  SELECT id_subasta, n_lote, n_bien,
         MAX(CASE WHEN campo='tipo_bien' THEN valor END) AS tipo_bien,
         MAX(CASE WHEN campo='provincia' THEN valor END) AS provincia,
         MAX(CASE WHEN campo='localidad' THEN valor END) AS localidad,
         MAX(CASE WHEN campo='direccion' THEN valor END) AS direccion,
         MAX(CASE WHEN campo='codigo_postal' THEN valor END) AS codigo_postal,
         MAX(CASE WHEN campo='referencia_catastral' THEN valor END) AS referencia_catastral,
         MAX(CASE WHEN campo='idufir' THEN valor END) AS idufir,
         MAX(CASE WHEN campo='inscripcion_registral' THEN valor END) AS inscripcion_registral,
         MAX(CASE WHEN campo='vivienda_habitual' THEN valor END) AS vivienda_habitual,
         MAX(CASE WHEN campo='situacion_posesoria' THEN valor END) AS situacion_posesoria,
         MAX(CASE WHEN campo='visitable' THEN valor END) AS visitable,
         MAX(url) AS url, MAX(fecha_consulta) AS fecha_consulta
  FROM campos WHERE n_bien <> '' GROUP BY id_subasta, n_lote, n_bien;
DROP VIEW IF EXISTS v_discrepancias;
CREATE VIEW v_discrepancias AS
  SELECT a.id_subasta, a.n_lote, a.n_bien, a.campo,
         a.valor AS valor_1, a.origen AS origen_1, a.url AS url_1, a.fecha_consulta AS fecha_1,
         b.valor AS valor_2, b.origen AS origen_2, b.url AS url_2, b.fecha_consulta AS fecha_2
  FROM campos a JOIN campos b
    ON a.id_subasta = b.id_subasta AND IFNULL(a.n_lote,'') = IFNULL(b.n_lote,'')
   AND IFNULL(a.n_bien,'') = IFNULL(b.n_bien,'') AND a.campo = b.campo AND a.id < b.id
   AND a.id_captura <> b.id_captura
  WHERE IFNULL(a.valor_num, a.valor) IS NOT IFNULL(b.valor_num, b.valor)
    AND a.campo NOT LIKE 'fila:%' AND a.campo NOT IN ('asiento_texto', 'descripcion_bien_edicto', 'condicion_edicto');
"""


# --------------------------------------------------------------------------- utilidades
def lista(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def txt(x) -> str:
    """Texto de un nodo JSON: cadena, número u objeto con el texto en 'texto' (p. ej. url_pdf)."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, dict):
        for k in ("texto", "#text", "text", "valor", "value", "url"):
            if k in x:
                return txt(x[k])
    return ""


def iso_dia(aaaammdd: str | None) -> str | None:
    s = (aaaammdd or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s or None


def iso_fecha_larga(texto: str | None) -> str | None:
    m = RE_FECHA_LARGA.search(texto or "")
    if not m or norm(m.group(2)) not in MESES:
        return None
    try:
        return dt.date(int(m.group(3)), MESES[norm(m.group(2))], int(m.group(1))).isoformat()
    except ValueError:
        return None


def absoluta(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(URL_BOE + "/", url.strip())


def contexto(texto: str, ini: int, fin: int, margen: int = 90) -> str:
    a, b = max(0, ini - margen), min(len(texto), fin + margen)
    return ("…" if a else "") + re.sub(r"\s+", " ", texto[a:b]).strip() + ("…" if b < len(texto) else "")


def incidencia(con, tipo: str, clave: str, detalle: str, fecha: str | None = None, url: str | None = None):
    m = ahora()
    con.execute(
        "INSERT INTO incidencias (tipo, clave, fecha, url, detalle, primera_vez, ultima_vez) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(tipo, clave) DO UPDATE SET ultima_vez=excluded.ultima_vez, veces=veces+1,"
        " detalle=excluded.detalle, url=COALESCE(excluded.url, url), resuelta_en=NULL",
        (tipo, clave, fecha, url, detalle, m, m))


def resolver(con, tipo: str, clave: str):
    con.execute("UPDATE incidencias SET resuelta_en=? WHERE tipo=? AND clave=? AND resuelta_en IS NULL",
                (ahora(), tipo, clave))


# --------------------------------------------------------------------------- geografía (IGN)
def clave_geo(nombre: str | None) -> str:
    t = norm(nombre).replace("·", "").replace("'", " ").replace("’", " ").replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


ARTICULOS = r"(EL|LA|LOS|LAS|L|ELS|LES|SA|SES|ES|S|O|A|AS|OS)"


class Geo:
    """Municipios y provincias de España (IGN). Sirve para la sede de las autoridades y para leer
    la ubicación del bien cuando un documento oficial la escribe."""

    def __init__(self, ruta: Path | None = None):
        ruta = ruta or sema.BASE / "datos" / "geo" / "municipios_es.json"
        d = json.loads(ruta.read_text(encoding="utf-8"))
        self.provincias: dict[str, str] = d["provincias"]
        self.exacto: dict[str, set] = {}     # nombre -> {(ine, nombre oficial)}, con variantes de artículo
        self.en_texto: dict[str, set] = {}   # solo formas seguras para buscar dentro de un texto
        for ine, nombre in d["municipios"]:
            for parte in nombre.split("/"):
                base = clave_geo(parte)
                if not base:
                    continue
                formas = {base}
                m = re.match(ARTICULOS + r"\s+(.+)$", base)
                if m:
                    formas |= {f"{m.group(2)} {m.group(1)}", f"{m.group(2)} ({m.group(1)})", f"{m.group(2)}, {m.group(1)}"}
                for f in formas:
                    self.exacto.setdefault(clave_geo(f), set()).add((ine, nombre))
                if len(base) >= 4:
                    self.en_texto.setdefault(base, set()).add((ine, nombre))
        self.prov_nombre: dict[str, str] = {}
        for cod, nombre in self.provincias.items():
            for parte in re.split(r"[/]", nombre):
                self.prov_nombre[clave_geo(parte)] = cod
        for alias, cod in {"LA CORUNA": "15", "CORUNA": "15", "A CORUNA": "15", "GERONA": "17", "LERIDA": "25",
                           "ORENSE": "32", "BALEARES": "07", "ILLES BALEARS": "07", "ISLAS BALEARES": "07",
                           "VIZCAYA": "48", "GUIPUZCOA": "20", "ALAVA": "01", "CASTELLON": "12",
                           "ALICANTE": "03", "VALENCIA": "46"}.items():
            if alias not in self.prov_nombre and cod in self.provincias:
                self.prov_nombre[alias] = cod
        self._re_muni = None
        self._re_prov = re.compile(r"\b(" + "|".join(sorted(map(re.escape, self.prov_nombre), key=len, reverse=True))
                                   + r")\b")

    def municipio_exacto(self, nombre: str | None) -> set:
        return self.exacto.get(clave_geo(nombre), set())

    def provincia_de(self, nombre: str | None) -> tuple[str | None, str]:
        """Provincia de un lugar escrito tal cual (sede, lugar de firma). Devuelve (código, motivo)."""
        c = clave_geo(nombre)
        if not c:
            return None, ""
        if c in self.prov_nombre:
            return self.prov_nombre[c], f"«{nombre}» es una provincia"
        munis = self.municipio_exacto(nombre)
        provs = {ine[:2] for ine, _ in munis}
        if len(provs) == 1:
            ine, oficial = sorted(munis)[0]
            return provs.pop(), f"«{nombre}» es el municipio {oficial} ({ine})"
        if len(provs) > 1:
            return None, f"«{nombre}» es un municipio de varias provincias ({', '.join(sorted(provs))})"
        return None, ""

    def buscar(self, fragmento: str) -> tuple[set, set]:
        """Provincias y municipios citados dentro de un fragmento ya acotado a la ubicación."""
        c = clave_geo(fragmento)
        provs = {self.prov_nombre[m.group(1)] for m in self._re_prov.finditer(c)}
        if self._re_muni is None:
            self._re_muni = re.compile(r"\b(" + "|".join(sorted(map(re.escape, self.en_texto), key=len, reverse=True))
                                       + r")\b")
        munis = set()
        for m in self._re_muni.finditer(c):
            munis |= self.en_texto[m.group(1)]
        return provs, munis


# --------------------------------------------------------------------------- sumario
def items_sumario_json(data: bytes) -> tuple[str | None, str | None, list[dict]]:
    """Devuelve (código de estado, fecha del sumario AAAAMMDD, items de las secciones IV, V.B y V.C).
    Recorre todos los diarios y encuentra los item dentro de departamento, epigrafe o texto, lleguen como
    objeto o como lista."""
    doc = json.loads(data.decode("utf-8"))
    estado = txt((doc.get("status") or {}).get("code")) or None
    sumario = (doc.get("data") or {}).get("sumario") or {}
    fecha = txt((sumario.get("metadatos") or {}).get("fecha_publicacion")) or None
    items: list[dict] = []
    for diario in lista(sumario.get("diario")):
        if not isinstance(diario, dict):
            continue
        numero = txt(diario.get("numero"))
        for sec in lista(diario.get("seccion")):
            if not isinstance(sec, dict):
                continue
            codigo = txt(sec.get("codigo")).upper()
            if codigo not in SECCIONES:
                continue
            ctx = {"diario_numero": numero, "seccion_codigo": codigo, "seccion_nombre": txt(sec.get("nombre")),
                   "departamento": None, "departamento_codigo": None, "epigrafe": None, "grupo_texto": None}
            _recorrer_json(sec, ctx, items)
    return estado, fecha, items


def _recorrer_json(nodo: dict, ctx: dict, items: list):
    for clave, valor in nodo.items():
        if clave == "item":
            for it in lista(valor):
                if isinstance(it, dict):
                    pdf = it.get("url_pdf")
                    pags = (txt(pdf.get("pagina_inicial")) or None, txt(pdf.get("pagina_final")) or None) if isinstance(pdf, dict) else (None, None)
                    items.append(_item(ctx, it.get("identificador"), it.get("control"), it.get("titulo"),
                                       it.get("url_html"), pdf, it.get("url_xml"), pags))
        elif isinstance(valor, (dict, list)):
            for hijo in lista(valor):
                if not isinstance(hijo, dict):
                    continue
                c = dict(ctx)
                if clave == "departamento":
                    c.update(departamento=txt(hijo.get("nombre")) or None,
                             departamento_codigo=txt(hijo.get("codigo")) or None, epigrafe=None, grupo_texto=None)
                elif clave == "epigrafe":
                    c.update(epigrafe=txt(hijo.get("nombre")) or None)
                elif clave == "texto":
                    c.update(grupo_texto=txt(hijo.get("nombre")) or "texto")
                _recorrer_json(hijo, c, items)


def items_sumario_xml(data: bytes) -> tuple[str | None, str | None, list[dict]]:
    root = sema.quitar_ns(ET.fromstring(data))
    estado = (root.findtext("./status/code") or "").strip() or None
    fecha = (root.findtext("./data/sumario/metadatos/fecha_publicacion") or "").strip() or None
    items: list[dict] = []
    for diario in root.iter("diario"):
        for sec in diario.iter("seccion"):
            codigo = (sec.get("codigo") or "").upper()
            if codigo not in SECCIONES:
                continue
            ctx = {"diario_numero": diario.get("numero"), "seccion_codigo": codigo,
                   "seccion_nombre": sec.get("nombre"), "departamento": None, "departamento_codigo": None,
                   "epigrafe": None, "grupo_texto": None}
            _recorrer_xml(sec, ctx, items)
    return estado, fecha, items


def _recorrer_xml(nodo, ctx: dict, items: list):
    for hijo in nodo:
        if hijo.tag == "item":
            pdf = hijo.find("url_pdf")
            pags = (pdf.get("pagina_inicial"), pdf.get("pagina_final")) if pdf is not None else (None, None)
            items.append(_item(ctx, hijo.findtext("identificador"), hijo.findtext("control"),
                               hijo.findtext("titulo"), hijo.findtext("url_html"), hijo.findtext("url_pdf"),
                               hijo.findtext("url_xml"), pags))
            continue
        c = dict(ctx)
        if hijo.tag == "departamento":
            c.update(departamento=hijo.get("nombre"), departamento_codigo=hijo.get("codigo"), epigrafe=None,
                     grupo_texto=None)
        elif hijo.tag == "epigrafe":
            c.update(epigrafe=hijo.get("nombre"))
        elif hijo.tag == "texto":
            c.update(grupo_texto=hijo.get("nombre") or "texto")
        _recorrer_xml(hijo, c, items)


def _item(ctx, ident, control, titulo, url_html, url_pdf, url_xml, paginas=(None, None)) -> dict:
    return {**ctx, "id_boe": txt(ident), "control": txt(control) or None, "titulo_sumario": txt(titulo),
            "url_html": absoluta(txt(url_html)), "url_pdf": absoluta(txt(url_pdf)), "url_xml": absoluta(txt(url_xml)),
            "pagina_inicial": paginas[0], "pagina_final": paginas[1]}


# --------------------------------------------------------------------------- anuncio
def leer_anuncio_xml(data: bytes) -> dict:
    root = sema.quitar_ns(ET.fromstring(data))
    doc = root if root.tag == "documento" else root.find(".//documento")
    if doc is None:
        doc = root
    md: dict[str, str] = {}
    meta = doc.find("metadatos")
    if meta is not None:
        for c in meta:
            md[c.tag] = (c.text or "").strip()
            if c.get("codigo"):
                md[c.tag + "_codigo"] = c.get("codigo")
    nodo = doc.find(".//texto")
    parrafos = [" ".join("".join(p.itertext()).split()) for p in nodo.iter("p")] if nodo is not None else []
    texto = "\n".join(p for p in parrafos if p)
    if not texto and nodo is not None:
        texto = " ".join(" ".join(nodo.itertext()).split())
    enlaces = [a.get("href") for a in doc.iter("a") if a.get("href")]
    return {"fecha_actualizacion": doc.get("fecha_actualizacion"), "metadatos": md, "texto": texto,
            "enlaces": enlaces}


def leer_anuncio_html(data: bytes) -> dict:
    """Página HTML oficial del anuncio (txt.php). Solo cuenta el cuerpo del anuncio, no los menús del BOE."""
    crudo = data.decode("utf-8", errors="replace")
    m = re.search(r'(?is)<div id="textoxslt">(.*?)<!-- #textoxslt -->', crudo)
    cuerpo = m.group(1) if m else crudo
    return {"fecha_actualizacion": None, "metadatos": {}, "texto": sema.texto_de_html(data).strip(),
            "enlaces": [html.unescape(h) for h in re.findall(r'href="([^"]+)"', cuerpo)] if m else []}


def enlaces_oficiales(it: dict, enlaces: list[str], texto: str) -> list[str]:
    vistos, salida = set(), []
    en_texto = [u.rstrip(".,;)") for u in re.findall(r"https?://[^\s<>\"']+", texto or "")]
    for u in [it.get("url_html"), it.get("url_pdf"), it.get("url_xml")] + list(enlaces) + en_texto:
        if not u:
            continue
        u = absoluta(html.unescape(u))
        if u and u not in vistos and urllib.parse.urlparse(u).scheme in ("http", "https"):
            vistos.add(u)
            salida.append(u)
    return salida


def subs_de(titulo: str, texto: str, enlaces: list[str]) -> dict[str, dict]:
    res: dict[str, dict] = {}

    def anota(sub, donde, frag=None, url=None):
        r = res.setdefault(sub.upper(), {"en_titulo": 0, "en_texto": 0, "en_enlace": 0, "url": None, "fragmento": None})
        r[donde] = 1
        r["url"] = r["url"] or url
        r["fragmento"] = r["fragmento"] or frag

    for donde, fuente in (("en_titulo", titulo or ""), ("en_texto", texto or "")):
        f = urllib.parse.unquote(fuente)
        for m in RE_SUB.finditer(f):
            anota(m.group(0), donde, contexto(f, m.start(), m.end()))
    for u in enlaces:
        for m in RE_SUB.finditer(urllib.parse.unquote(u)):
            anota(m.group(0), "en_enlace", url=u)
    return res


RE_UBICACION = [
    ("verbo", re.compile(r"\b(?:sit[oa]s?|situad[oa]s?|ubicad[oa]s?|radicad[oa]s?|emplazad[oa]s?|localizad[oa]s?)\s+en\s+"
                         r"(?P<frag>[^.;:\n]{3,220})", re.I)),
    ("termino_municipal", re.compile(r"\bt[ée]rmino\s+municipal\s+de\s+(?P<frag>[^.;:\n]{2,90})", re.I)),
    ("provincia", re.compile(r"(?:inmuebles?|fincas?|bien(?:es)?|propiedad|viviendas?|locales?|solar(?:es)?|parcelas?)"
                             r"[^.;\n]{0,80}?\ben\s+la\s+provincia\s+de\s+(?P<frag>[A-ZÁÉÍÓÚÑ][^.;,:\n()]{2,40})", re.I)),
    ("rotulo", re.compile(r"^\s*(?:Localidad|Municipio|Provincia)\s*:\s*(?P<frag>[^\n]{2,90})$", re.I | re.M)),
]


# Frases que describen la oficina de la autoridad (dónde presentar ofertas, su sede), no el bien
RE_OFICINA = re.compile(r"(registro|sede|oficinas?|delegaci[oó]n|secretar[ií]a|direcci[oó]n (general|provincial)|tesorer[ií]a|"
                        r"unidad|subdirecci[oó]n|dependencias|domicilio|presentaci[oó]n|horario|servicio de|secci[oó]n de|"
                        r"gerencia|ayuntamiento de|juzgado|tribunal|notar[ií]a|fondo de garant[ií]a|fogasa)[^.;]{0,70}$", re.I)
# Nombres de vía: «Avenida de Salamanca» no dice que el bien esté en Salamanca
RE_VIA = re.compile(r"\b(avenida|avda\.?|calle|c/|plaza|pza\.?|paseo|carretera|ctra\.?|camino|ronda|traves[ií]a|"
                    r"urbanizaci[oó]n|pol[ií]gono|barrio|glorieta|v[ií]a)\s+(?:de\s+(?:la\s+|los\s+|las\s+|el\s+)?|del\s+)?"
                    r"[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ'·-]*(?:\s+(?:de\s+|del\s+|la\s+|las\s+|los\s+|y\s+)?[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ'·-]*)*", re.I)


def ubicacion_en_texto(geo: Geo, titulo: str, texto: str) -> list[dict]:
    """Ubicación del bien solo cuando el documento oficial la escribe (no la sede ni la oficina de la autoridad)."""
    salida = []
    for nombre_fuente, fuente in (("título", titulo or ""), ("texto", texto or "")):
        for metodo, rx in RE_UBICACION:
            for m in rx.finditer(fuente):
                previo = fuente[max(0, m.start() - 90):m.start()]
                of = RE_OFICINA.search(previo)
                if of and not re.search(r"propiedad\s+(de|del)\s+(la\s+|el\s+|los\s+)?$", previo[:of.start()], re.I):
                    continue  # habla de la oficina de la autoridad; «propiedad de la Tesorería…» sí es el bien
                frag = RE_VIA.sub(" ", m.group("frag"))
                provs, munis = geo.buscar(frag)
                provs_m = {ine[:2] for ine, _ in munis}
                if provs:
                    munis = {x for x in munis if x[0][:2] in provs}
                    decididas = provs
                elif len(provs_m) == 1:
                    decididas = provs_m
                else:
                    decididas = set()
                if not decididas and not munis:
                    continue
                salida.append({"provincias": sorted(decididas), "municipios": sorted(n for _, n in munis),
                               "ambiguo": bool(munis) and not decididas, "metodo": metodo, "en": nombre_fuente,
                               "fragmento": contexto(fuente, m.start(), m.end(), 40)})
    return salida


def sede_autoridad(geo: Geo, it: dict, lugar_firma: str | None) -> tuple[str | None, str]:
    """Provincia de la sede de la autoridad. Es solo una pista: no es la ubicación del bien."""
    emisor = it.get("titulo_sumario") or ""
    m = re.search(r"\bSUBASTAS?\b.*?\b(\d{2})\s*$", norm(emisor))
    if m and m.group(1) in geo.provincias:
        return m.group(1), f"unidad gestora «{emisor}» (código de provincia {m.group(1)})"
    if sema.es_titulo_emisor(emisor):
        for candidato in (emisor, re.sub(r"^.*\bSUBASTAS?\b\.?\s*(SEDE\s+)?", "", norm(emisor))):
            cod, motivo = geo.provincia_de(candidato)
            if cod:
                return cod, f"emisor: {motivo}"
    if lugar_firma:
        cod, motivo = geo.provincia_de(lugar_firma)
        if cod:
            return cod, f"lugar de firma: {motivo}"
        if motivo:
            return None, f"lugar de firma: {motivo}"
    return None, "el anuncio no permite saber la provincia de la sede"


def analizar_anuncio(geo: Geo, it: dict, leido: dict) -> dict:
    md, texto = leido["metadatos"], leido["texto"] or ""
    titulo = md.get("titulo") or it.get("titulo_sumario") or ""
    enlaces = enlaces_oficiales(it, leido["enlaces"], texto)
    subs = subs_de(titulo + " " + (it.get("titulo_sumario") or ""), texto, enlaces)
    datos = sema.datos_anuncio(texto)
    ref = RE_REFERENCIA.search(texto)
    cab = norm(" ".join([titulo, it.get("titulo_sumario") or "", it.get("epigrafe") or ""]))
    menciona = "SUBASTA" in cab or "SUBASTA" in norm(texto) or bool(subs)
    inmueble = 1 if RE_INMUEBLE.search(norm(titulo + " " + texto)) else (None if subs else 0)
    ubic = ubicacion_en_texto(geo, titulo, texto) if menciona else []
    provs = sorted({p for u in ubic for p in u["provincias"]})
    munis = sorted({mm for u in ubic for mm in u["municipios"]})
    sede, sede_motivo = sede_autoridad(geo, it, datos.get("lugar_firma"))
    organismo = " · ".join(x for x in (md.get("departamento") or it.get("departamento"),
                                       it.get("epigrafe"), it.get("titulo_sumario") if sema.es_titulo_emisor(
                                           it.get("titulo_sumario")) else None) if x)
    fa = leido.get("fecha_actualizacion")
    fa_iso = None
    if fa and re.fullmatch(r"\d{14}", fa):
        fa_iso = f"{fa[:4]}-{fa[4:6]}-{fa[6:8]}T{fa[8:10]}:{fa[10:12]}:{fa[12:]}"
    elif fa and re.fullmatch(r"\d{8}", fa):
        fa_iso = iso_dia(fa)
    if md.get("identificador") and md["identificador"] != it["id_boe"]:
        raise ValueError(f"el XML dice {md['identificador']} y el sumario {it['id_boe']}")
    return {
        "titulo": titulo, "organismo": organismo or None, "fecha_actualizacion": fa_iso,
        "fecha_actualizacion_texto": fa, "pagina_inicial": md.get("pagina_inicial"),
        "pagina_final": md.get("pagina_final"), "enlaces_oficiales": json.dumps(enlaces, ensure_ascii=False),
        "texto": texto[:8000] if menciona else None, "menciona_subasta": int(menciona), "es_inmueble": inmueble, "n_sub": len(subs),
        "ids_subasta": ",".join(sorted(subs)) or None,
        "tipo_subasta_texto": datos.get("tipo_anuncio"),
        "referencia_tipo": ref.group(1).lower() if ref else None, "referencia_texto": ref.group(2).strip() if ref else None,
        "lugar_firma": datos.get("lugar_firma"), "fecha_firma": iso_fecha_larga(datos.get("fecha_firma")) or datos.get("fecha_firma"),
        "sede_provincia": sede, "sede_motivo": sede_motivo,
        "ubicacion_estado": ("segun_anuncio_boe" if provs else ("ambigua_en_anuncio" if munis else "pendiente")),
        "ubicacion_provincias": ",".join(provs) or None, "ubicacion_municipios": "; ".join(munis) or None,
        "ubicacion_evidencia": json.dumps(ubic, ensure_ascii=False) if ubic else None,
        "_subs": subs,
    }


def reanalizar_ubicaciones(con, geo: Geo, id_ej: int) -> set[str]:
    """Vuelve a leer la ubicación de los anuncios de subasta ya guardados (título y texto) con las reglas actuales.
    Los cambios quedan en cambios_historicos. Devuelve los expedientes afectados."""
    afectados: set[str] = set()
    for r in con.execute("SELECT id_boe, titulo, texto, ids_subasta, ubicacion_estado, ubicacion_provincias FROM anuncios"
                         " WHERE menciona_subasta=1 AND xml_estado='ok'").fetchall():
        ubic = ubicacion_en_texto(geo, r["titulo"] or "", r["texto"] or "")
        provs = sorted({x for u in ubic for x in u["provincias"]})
        munis = sorted({x for u in ubic for x in u["municipios"]})
        estado = "segun_anuncio_boe" if provs else ("ambigua_en_anuncio" if munis else "pendiente")
        nuevos = {"ubicacion_estado": estado, "ubicacion_provincias": ",".join(provs) or None,
                  "ubicacion_municipios": "; ".join(munis) or None,
                  "ubicacion_evidencia": json.dumps(ubic, ensure_ascii=False) if ubic else None}
        if (r["ubicacion_estado"], r["ubicacion_provincias"]) == (estado, nuevos["ubicacion_provincias"]):
            continue
        for campo, valor in nuevos.items():  # también cuando un valor pasa a vacío
            viejo = con.execute(f"SELECT {campo} FROM anuncios WHERE id_boe=?", (r["id_boe"],)).fetchone()[0]
            if viejo != valor:
                con.execute("INSERT INTO cambios_historicos (tabla, clave, campo, valor_anterior, valor_nuevo, detectado_en,"
                            " id_ejecucion, fuente) VALUES (?,?,?,?,?,?,?,?)",
                            ("anuncios", r["id_boe"], campo, viejo, valor, ahora(), id_ej, "BOE (reanálisis de la ubicación)"))
                con.execute(f"UPDATE anuncios SET {campo}=? WHERE id_boe=?", (valor, r["id_boe"]))
        afectados |= set((r["ids_subasta"] or "").split(",")) - {""} or {"BOE:" + r["id_boe"]}
    con.commit()
    return afectados


def mantenimiento(con):
    """Arreglos idempotentes de datos guardados por versiones anteriores:
    - el texto solo se guarda en los anuncios de subasta (el bruto sigue en datos/brutos);
    - de las páginas HTML, solo cuentan los enlaces del propio anuncio, no los de los menús del BOE;
    - un domingo sin BOE es lo esperado: su registro queda resuelto."""
    con.execute("UPDATE anuncios SET texto=NULL WHERE IFNULL(menciona_subasta,0)=0 AND texto IS NOT NULL")
    for r in con.execute("SELECT id_boe, url_html, url_pdf, url_xml, texto, enlaces_oficiales FROM anuncios"
                         " WHERE texto_desde='html' AND enlaces_oficiales IS NOT NULL").fetchall():
        enl = json.loads(r["enlaces_oficiales"])
        buenos = [u for u in enl if u in (r["url_html"], r["url_pdf"], r["url_xml"]) or (r["texto"] and u in r["texto"])]
        if len(buenos) != len(enl):
            con.execute("UPDATE anuncios SET enlaces_oficiales=? WHERE id_boe=?", (json.dumps(buenos, ensure_ascii=False), r["id_boe"]))
    con.execute("UPDATE incidencias SET resuelta_en=? WHERE tipo='dia_sin_sumario' AND resuelta_en IS NULL"
                " AND strftime('%w', clave)='0' AND clave < date('now', '-1 day')", (ahora(),))
    con.commit()


# --------------------------------------------------------------------------- red
class HttpDiario(sema.Http):
    """Como sema.Http (robots.txt, pausa, registro en respuestas_brutas), pero guarda las respuestas de cada
    día en un único archivo datos/brutos/dealfinder/AAAA/MM/AAAA-MM-DD.jsonl.gz (una línea JSON por
    respuesta, con su URL, momento, HTTP y SHA-256), para no llenar el repositorio de archivos sueltos."""

    def __init__(self, con, id_ej, dir_brutos, avisos):
        super().__init__(con, id_ej, dir_brutos, avisos)
        self._pendiente: dict[Path, list[str]] = {}

    def get(self, url: str, accept: str = "*/*", nombre: str | None = None):
        if not self.permitido(url):
            p = urllib.parse.urlparse(url)
            aviso = f"robots.txt no permite {p.netloc}{p.path}: esas URLs no se descargan"
            if aviso not in self.avisos:
                self.avisos.append(aviso)
            return -1, None
        status, data = self._descargar(url, accept)
        momento = ahora()
        huella = sha256(data) if data else None
        ruta_txt = None
        if data is not None and status == 200:
            previa = self.con.execute("SELECT ruta FROM respuestas_brutas WHERE url=? AND sha256=? AND ruta IS NOT NULL"
                                      " LIMIT 1", (url, huella)).fetchone()
            if previa:
                ruta_txt = previa["ruta"]
            else:
                hoy = dt.date.today()
                ruta = self.dir_brutos / f"{hoy:%Y}" / f"{hoy:%m}" / f"{hoy:%Y-%m-%d}.jsonl.gz"
                try:
                    contenido, cod = data.decode("utf-8"), "utf-8"
                except UnicodeDecodeError:
                    contenido, cod = base64.b64encode(data).decode("ascii"), "base64"
                self._pendiente.setdefault(ruta, []).append(json.dumps(
                    {"url": url, "momento_consulta": momento, "http": status, "sha256": huella, "nombre": nombre,
                     "accept": accept, "codificacion": cod, "contenido": contenido}, ensure_ascii=False))
                ruta_txt = str(ruta.relative_to(self.dir_brutos.parent))
        self.con.execute("INSERT INTO respuestas_brutas (id_ejecucion, url, momento_consulta, http_status, sha256, ruta, bytes)"
                         " VALUES (?,?,?,?,?,?,?)", (self.id_ej, url, momento, status, huella, ruta_txt, len(data) if data else 0))
        time.sleep(sema.PAUSA)
        return status, data

    def volcar(self):
        """Escribe lo descargado (un bloque gzip por ejecución y día, que se añade al archivo del día)."""
        for ruta, lineas in self._pendiente.items():
            ruta.parent.mkdir(parents=True, exist_ok=True)
            with open(ruta, "ab") as f:
                f.write(gzip.compress(("\n".join(lineas) + "\n").encode("utf-8"), 9))
        self._pendiente.clear()


def leer_brutos(ruta: Path) -> list[dict]:
    with gzip.open(ruta, "rt", encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


# --------------------------------------------------------------------------- ejecución diaria
def dias_a_procesar(con, hasta: dt.date, desde: dt.date | None) -> list[dt.date]:
    if desde is not None:
        return [desde + dt.timedelta(days=i) for i in range((hasta - desde).days + 1)]
    cerrados = {r["fecha"] for r in con.execute("SELECT fecha FROM sumarios WHERE estado IN ('procesado','sin_sumario')")}
    inicio = hasta - dt.timedelta(days=DIAS_PRIMERA_CARGA)
    dias = [inicio + dt.timedelta(days=i) for i in range((hasta - inicio).days + 1)]
    return [d for d in dias if d.isoformat() not in cerrados or d >= hasta - dt.timedelta(days=1)]


def procesar_sumario(con, http, fecha: dt.date, id_ej: int, stats: dict, avisos: list[str]):
    f = fecha.strftime("%Y%m%d")
    url = sema.API_SUMARIO.format(fecha=f)
    previo = con.execute("SELECT intentos FROM sumarios WHERE fecha=?", (fecha.isoformat(),)).fetchone()
    intentos = (previo["intentos"] if previo else 0) + 1
    reciente = fecha >= dt.date.today() - dt.timedelta(days=1)

    def guarda(estado, formato=None, status=None, fecha_sum=None, diarios=None, n=None, n_sec=None, data=None):
        fila = con.execute("SELECT ruta FROM respuestas_brutas WHERE url=? ORDER BY id DESC LIMIT 1", (url,)).fetchone()
        con.execute("INSERT OR REPLACE INTO sumarios VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fecha.isoformat(), estado, formato, status, fecha_sum, diarios, n, n_sec, url,
                     sha256(data) if data else None, fila["ruta"] if fila else None, intentos, ahora(), id_ej))

    status, data = http.get(url, accept="application/json", nombre=f"sumario_{f}.json")
    formato = "json"
    estado = fecha_sum = None
    items: list[dict] = []
    if status == 200 and data:
        try:
            estado, fecha_sum, items = items_sumario_json(data)
        except (ValueError, UnicodeDecodeError) as e:  # JSON no válido: se usa el XML oficial
            avisos.append(f"Sumario {fecha}: JSON no válido ({e.__class__.__name__}); se usa el XML")
            status, data = http.get(url, accept="application/xml", nombre=f"sumario_{f}.xml")
            formato = "xml"
            if status == 200 and data:
                try:
                    estado, fecha_sum, items = items_sumario_xml(data)
                except ET.ParseError as e2:
                    estado = None
                    avisos.append(f"Sumario {fecha}: tampoco el XML es válido ({e2})")
    if status == 404 or estado == "404":
        detalle = ("El BOE no publica los domingos" if fecha.weekday() == 6 else
                   "La API respondió que no hay sumario para esta fecha")
        if reciente:
            detalle += " (fecha reciente: se volverá a pedir)"
        incidencia(con, "dia_sin_sumario", fecha.isoformat(), detalle, fecha.isoformat(), url)
        if fecha.weekday() == 6 and not reciente:
            resolver(con, "dia_sin_sumario", fecha.isoformat())  # es lo esperado: queda como registro, no como problema
        guarda("pendiente" if reciente else "sin_sumario", formato, 404)
        return
    if status != 200 or not data or estado != "200":
        motivo = f"HTTP {status}" if status != 200 or not data else f"código de la API {estado}"
        if status == -1:
            motivo = "robots.txt no lo permite"
        incidencia(con, "error_sumario", fecha.isoformat(), f"No se pudo leer el sumario ({motivo}); se reintentará",
                   fecha.isoformat(), url)
        stats["dias_fallidos"] = stats.get("dias_fallidos", 0) + 1
        guarda("error", formato, status, data=data)
        return
    if fecha_sum != f:
        incidencia(con, "fecha_no_coincide", fecha.isoformat(),
                   f"Se pidió {f} y el sumario dice {fecha_sum}: no se usa; se reintentará", fecha.isoformat(), url)
        guarda("fecha_no_coincide", formato, status, fecha_sum, data=data)
        return
    resolver(con, "error_sumario", fecha.isoformat())
    resolver(con, "dia_sin_sumario", fecha.isoformat())
    resolver(con, "fecha_no_coincide", fecha.isoformat())
    momento = ahora()
    diarios = sorted({i["diario_numero"] for i in items if i["diario_numero"]})
    vistos = set()
    for it in items:
        if not it["id_boe"] or it["id_boe"] in vistos:
            continue
        vistos.add(it["id_boe"])
        existe = con.execute("SELECT 1 FROM anuncios WHERE id_boe=?", (it["id_boe"],)).fetchone()
        if existe:
            continue  # ya registrado: el reintento no duplica
        con.execute(
            "INSERT INTO anuncios (id_boe, fecha_publicacion, diario_numero, seccion_codigo, seccion_nombre,"
            " departamento, departamento_codigo, epigrafe, grupo_texto, titulo_sumario, control, url_html, url_pdf,"
            " url_xml, pagina_inicial, pagina_final, xml_estado, fuente, url_fuente, momento_consulta, nivel_verificacion,"
            " id_ejecucion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pendiente',?,?,?,?,?)",
            (it["id_boe"], fecha.isoformat(), it["diario_numero"], it["seccion_codigo"], it["seccion_nombre"],
             it["departamento"], it["departamento_codigo"], it["epigrafe"], it["grupo_texto"], it["titulo_sumario"],
             it["control"], it["url_html"], it["url_pdf"], it["url_xml"], it["pagina_inicial"], it["pagina_final"],
             FUENTE_DIARIO, url, momento, "A1", id_ej))
        stats["nuevos"] += 1
    stats["leidos"] += len(vistos)
    guarda("procesado", formato, status, fecha_sum, ",".join(diarios), None, len(vistos), data)


def procesar_xml_pendientes(con, http, geo: Geo, id_ej: int, stats: dict, avisos: list[str], limite: int) -> set[str]:
    filas = con.execute("SELECT * FROM anuncios WHERE xml_estado IN ('pendiente','error') AND xml_intentos < ?"
                        " ORDER BY fecha_publicacion DESC, id_boe LIMIT ?", (MAX_INTENTOS, limite)).fetchall()
    tocados: set[str] = set()
    alternativas: dict[str, int] = {}
    for n, a in enumerate(filas, 1):
        if n % 100 == 0:  # se guarda el avance: un corte (p. ej. por tiempo) no pierde lo ya leído
            con.commit()
            if hasattr(http, "volcar"):
                http.volcar()
        it = dict(a)
        leido, desde, fallos = None, None, []
        for url, tipo in ((it["url_xml"], "xml"), (it["url_html"], "html")):
            if not url:
                continue
            st, data = http.get(url, accept="application/xml" if tipo == "xml" else "text/html",
                                nombre=f"{it['id_boe']}.{tipo}")
            if st == 200 and data:
                try:
                    leido = leer_anuncio_xml(data) if tipo == "xml" else leer_anuncio_html(data)
                    desde = tipo
                    break
                except ET.ParseError as e:
                    fallos.append(f"{tipo}: XML no válido ({e})")
            else:
                fallos.append(f"{tipo}: " + ("robots.txt no lo permite" if st == -1 else f"HTTP {st}"))
        intentos = (it["xml_intentos"] or 0) + 1
        if leido is None:
            con.execute("UPDATE anuncios SET xml_estado='error', xml_intentos=? WHERE id_boe=?", (intentos, it["id_boe"]))
            incidencia(con, "error_descarga", it["id_boe"], "; ".join(fallos) + f" (intento {intentos} de {MAX_INTENTOS})",
                       it["fecha_publicacion"], it["url_xml"] or it["url_html"])
            stats["errores_descarga"] = stats.get("errores_descarga", 0) + 1
            continue
        try:
            d = analizar_anuncio(geo, it, leido)
        except ValueError as e:
            con.execute("UPDATE anuncios SET xml_estado='error', xml_intentos=? WHERE id_boe=?", (intentos, it["id_boe"]))
            incidencia(con, "id_no_coincide", it["id_boe"], str(e), it["fecha_publicacion"], it["url_xml"])
            continue
        if fallos:
            clave = "; ".join(fallos)
            alternativas[clave] = alternativas.get(clave, 0) + 1
        subs = d.pop("_subs")
        momento = ahora()
        meta = {"fuente": FUENTE_ANUNCIO[desde], "url_fuente": it["url_xml"] if desde == "xml" else it["url_html"],
                "momento_consulta": momento, "nivel_verificacion": "A1", "id_ejecucion": id_ej}
        r = sema.upsert(con, "anuncios", {"id_boe": it["id_boe"]}, d, meta, id_ej, "BOE")
        if r == "cambio":
            stats["cambios"] += 1
        con.execute("UPDATE anuncios SET xml_estado='ok', xml_intentos=?, texto_desde=? WHERE id_boe=?",
                    (intentos, desde, it["id_boe"]))
        resolver(con, "error_descarga", it["id_boe"])
        for sub, s in subs.items():
            con.execute("INSERT OR REPLACE INTO anuncio_sub VALUES (?,?,?,?,?,?,?,?)",
                        (it["id_boe"], sub, s["en_titulo"], s["en_texto"], s["en_enlace"], s["url"], s["fragmento"], momento))
            tocados.add(sub)
        if d["menciona_subasta"] and not subs:
            ubic = (f"; ubicación según el anuncio: {d['ubicacion_municipios'] or ''} "
                    f"({d['ubicacion_provincias'] or 'provincia sin determinar'})") if d["ubicacion_evidencia"] else ""
            incidencia(con, "subasta_sin_sub", it["id_boe"],
                       f"{'Inmuebles. ' if d['es_inmueble'] else ''}{d['titulo'][:300]}{ubic}",
                       it["fecha_publicacion"], it["url_html"])
            tocados.add("BOE:" + it["id_boe"])
        stats["xml_leidos"] = stats.get("xml_leidos", 0) + 1
    for clave, n in alternativas.items():
        avisos.append(f"{n} anuncios sin XML ({clave}): su texto se leyó de la página HTML oficial del BOE (txt.php), "
                      "que publica el mismo anuncio; la fecha de actualización solo consta en el XML")
    return tocados


def ambito_de(provincias_bien: list[str], sede: str | None) -> str:
    dentro = [p for p in provincias_bien if p in PROVINCIAS_AMBITO]
    if dentro:
        return "ubicacion_en_ambito"
    if provincias_bien:
        return "ubicacion_fuera"
    if sede in PROVINCIAS_AMBITO:
        return "sede_en_ambito_ubicacion_pendiente"
    return "fuera_o_sin_datos"


def actualizar_expedientes(con, ids: set[str] | None, id_ej: int, stats: dict, geo: Geo):
    """Rehace los expedientes tocados (o todos) a partir de sus anuncios y de las fichas del Portal."""
    if ids is None:
        ids = {r["id_subasta"] for r in con.execute("SELECT DISTINCT id_subasta FROM anuncio_sub")}
        ids |= {"BOE:" + r["clave"] for r in con.execute("SELECT clave FROM incidencias WHERE tipo='subasta_sin_sub'")}
        ids |= {r["id_subasta"] for r in con.execute("SELECT DISTINCT id_subasta FROM campos WHERE id_subasta IS NOT NULL")}
    for idx in sorted(ids):
        if idx.startswith("BOE:"):
            anuncios = con.execute("SELECT * FROM anuncios WHERE id_boe=?", (idx[4:],)).fetchall()
            if not anuncios or not anuncios[0]["es_inmueble"]:
                continue
            sub, url_portal = None, None
        else:
            anuncios = con.execute("SELECT a.*, s.url_enlace FROM anuncio_sub s JOIN anuncios a USING(id_boe)"
                                   " WHERE s.id_subasta=? ORDER BY a.fecha_publicacion, a.id_boe", (idx,)).fetchall()
            sub = idx
            url_portal = next((a["url_enlace"] for a in anuncios if a["url_enlace"]), None)
        provs_ficha = sorted({cod for (v,) in con.execute(
            "SELECT valor FROM campos WHERE id_subasta=? AND campo='provincia'", (sub or idx,)) for cod in [geo.provincia_de(v)[0]] if cod})
        provs_anuncio = sorted({p for a in anuncios for p in (a["ubicacion_provincias"] or "").split(",") if p})
        if provs_ficha:
            ubic_estado, provs = "confirmada_ficha_portal", provs_ficha
        elif provs_anuncio:
            ubic_estado, provs = "segun_anuncio_boe", provs_anuncio
        else:
            ubic_estado, provs = "pendiente", []
        primero = anuncios[0] if anuncios else None
        ultimo = anuncios[-1] if anuncios else None
        sede = next((a["sede_provincia"] for a in anuncios if a["sede_provincia"]), None)
        sede_motivo = next((a["sede_motivo"] for a in anuncios if a["sede_provincia"]), primero["sede_motivo"] if primero else None)
        refs = sorted({f"{a['referencia_tipo']} {a['referencia_texto']}" for a in anuncios if a["referencia_texto"]})
        ficha = con.execute("SELECT COUNT(*) FROM capturas WHERE id_subasta_encabezado=? AND estado='ok'",
                            (sub or idx,)).fetchone()[0]
        valores = {
            "id_subasta": sub, "prefijo": sub.split("-")[1] if sub else None, "con_ficha_portal": 1 if sub else 0,
            "anuncios": ",".join(a["id_boe"] for a in anuncios) or None, "n_anuncios": len(anuncios),
            "primer_anuncio": primero["id_boe"] if primero else None,
            "fecha_primer_anuncio": primero["fecha_publicacion"] if primero else None,
            "ultimo_anuncio": ultimo["id_boe"] if ultimo else None,
            "fecha_ultimo_anuncio": ultimo["fecha_publicacion"] if ultimo else None,
            "url_portal_publicada": url_portal, "organismo": primero["organismo"] if primero else None,
            "tipo_subasta_anuncio": next((a["tipo_subasta_texto"] for a in anuncios if a["tipo_subasta_texto"]), None),
            "referencia_anuncio": " | ".join(refs) or None,
            "lugar_firma": next((a["lugar_firma"] for a in anuncios if a["lugar_firma"]), None),
            "sede_provincia": sede, "sede_motivo": sede_motivo, "ubicacion_estado": ubic_estado,
            # "" y no None: si una provincia deja de constar, el cambio se registra (None no borra en upsert)
            "provincias_bien": ",".join(provs), "ambito": ambito_de(provs, sede), "ficha_consultada": int(ficha > 0),
        }
        meta = {"fuente": FUENTE_PORTAL if ficha else FUENTE_DIARIO,
                "url_fuente": primero["url_html"] if primero else None, "momento_consulta": ahora(),
                "nivel_verificacion": "A1"}
        r = sema.upsert(con, "expedientes", {"id_expediente": idx}, valores, meta, id_ej, "BOE")
        if r == "nuevo":
            stats["expedientes_nuevos"] = stats.get("expedientes_nuevos", 0) + 1


# --------------------------------------------------------------------------- Portal de Subastas (consultas puntuales)
class _Nodo:
    __slots__ = ("tag", "attrs", "hijos", "padre")

    def __init__(self, tag, attrs, padre):
        self.tag, self.attrs, self.hijos, self.padre = tag, attrs, [], padre


class _Arbol(HTMLParser):
    VACIOS = {"br", "img", "input", "meta", "link", "hr", "col", "area", "base", "wbr", "source"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = _Nodo("raiz", {}, None)
        self.actual = self.raiz
        self.comentarios: list[str] = []

    def handle_starttag(self, tag, attrs):
        n = _Nodo(tag, dict(attrs), self.actual)
        self.actual.hijos.append(n)
        if tag not in self.VACIOS:
            self.actual = n

    def handle_endtag(self, tag):
        n = self.actual
        while n is not None and n.tag != tag:
            n = n.padre
        if n is not None and n.padre is not None:
            self.actual = n.padre

    def handle_data(self, data):
        self.actual.hijos.append(data)

    def handle_comment(self, data):
        self.comentarios.append(data)


def _texto(n) -> str:
    if isinstance(n, str):
        return n
    if n.tag in ("script", "style"):
        return ""
    sep = " " if n.tag not in ("br",) else "\n"
    return sep.join(_texto(h) for h in n.hijos)


def _limpio(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _enlaces(n) -> list[tuple[str, str]]:
    out = []
    if isinstance(n, str):
        return out
    if n.tag == "a" and n.attrs.get("href"):
        out.append((_limpio(_texto(n)), n.attrs["href"]))
    for h in n.hijos:
        out.extend(_enlaces(h))
    return out


ROT_GENERAL = {
    "IDENTIFICADOR": "identificador", "TIPO DE SUBASTA": "tipo_subasta", "CUENTA EXPEDIENTE": "cuenta_expediente",
    "FECHA DE INICIO": "fecha_inicio", "FECHA DE CONCLUSION": "fecha_conclusion",
    "CANTIDAD RECLAMADA": "cantidad_reclamada", "LOTES": "lotes_texto", "ANUNCIO BOE": "anuncio_boe",
    "VALOR SUBASTA": "valor_subasta", "VALOR DE SUBASTA": "valor_subasta", "TASACION": "tasacion",
    "VALOR DE TASACION": "tasacion", "PUJA MINIMA": "puja_minima", "TRAMOS ENTRE PUJAS": "tramos_pujas",
    "IMPORTE DEL DEPOSITO": "deposito", "DEPOSITO": "deposito", "NUMERO DE PROCEDIMIENTO": "numero_procedimiento",
    "PROCEDIMIENTO": "numero_procedimiento", "FORMA ADJUDICACION": "forma_adjudicacion",
    "FORMA DE ADJUDICACION": "forma_adjudicacion",
}
ROT_AUTORIDAD = {"CODIGO": "autoridad_codigo", "DESCRIPCION": "autoridad_descripcion", "DIRECCION": "autoridad_direccion",
                 "TELEFONO": "autoridad_telefono", "FAX": "autoridad_fax", "CORREO ELECTRONICO": "autoridad_correo"}
ROT_BIEN = {
    "DESCRIPCION": "descripcion", "DIRECCION": "direccion", "CODIGO POSTAL": "codigo_postal", "LOCALIDAD": "localidad",
    "PROVINCIA": "provincia", "REFERENCIA CATASTRAL": "referencia_catastral", "IDUFIR": "idufir", "CRU": "idufir",
    "IDUFIR/CRU": "idufir", "INSCRIPCION REGISTRAL": "inscripcion_registral", "VIVIENDA HABITUAL": "vivienda_habitual",
    "SITUACION POSESORIA": "situacion_posesoria", "VISITABLE": "visitable", "CARGAS": "cargas_texto",
    "INFORMACION ADICIONAL": "informacion_adicional", "TITULO JURIDICO": "titulo_juridico", "CUOTA": "cuota",
}
CAMPOS_IMPORTE = {"cantidad_reclamada", "valor_subasta", "tasacion", "puja_minima", "tramos_pujas", "deposito",
                  "puja_actual", "puja_final", "puja_maxima"}
CAMPOS_FECHA = {"fecha_inicio", "fecha_conclusion"}
ESTADOS = [  # (estado, patrón sobre el aviso de la ficha)
    ("finalizada_por_autoridad", r"FINALIZADA POR (LA )?AUTORIDAD"),
    ("suspendida", r"\bSUSPENDIDA\b"),
    ("cancelada", r"\bCANCELADA\b"),
    ("pujas_concluidas", r"\bCONCLUIDA\b|PUJAS CONCLUIDAS"),
    ("en_curso", r"CELEBRANDOSE|\bEN CURSO\b|ABIERTA LA PUJA|SUBASTA ABIERTA"),
    ("proxima_apertura", r"PROXIMA APERTURA|PENDIENTE DE APERTURA|PROXIMA A ABRIR|AUN NO SE HA ABIERTO"),
]


def valor_importe(texto: str) -> tuple[float | None, str]:
    """Importe en euros y su clase: importe, cero, vacio, sin_puja_minima o texto."""
    t = _limpio(texto)
    if not t:
        return None, "vacio"
    if "SIN PUJA MINIMA" in norm(t):
        return None, "sin_puja_minima"
    m = re.search(r"(-?\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|-?\d+(?:,\d{1,2})?)\s*(?:€|EUR|EUROS)?", t)
    if not m:
        return None, "texto"
    v = float(m.group(1).replace(".", "").replace(",", "."))
    return v, ("cero" if v == 0 else "importe")


def valor_fecha(texto: str) -> str | None:
    t = _limpio(texto)
    m = re.search(r"ISO:\s*(\d{4}-\d{2}-\d{2}T[\d:]{5,8}(?:[+-]\d{2}:\d{2}|Z)?)", t)
    if m:
        return m.group(1)
    m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?\s*(CEST|CET)?", t)
    if not m:
        return None
    d, mo, a, h, mi, s, z = m.groups()
    if not h:
        return f"{a}-{mo}-{d}"
    off = {"CET": "+01:00", "CEST": "+02:00"}.get(z or "", "")
    return f"{a}-{mo}-{d}T{h}:{mi}:{s or '00'}{off}"


def _meta_archivo(ruta: Path) -> dict:
    """URL y fecha de consulta: de un JSON al lado (archivo.json o consulta.json) o del propio archivo."""
    for cand in (ruta.with_suffix(ruta.suffix + ".json"), ruta.with_suffix(".json"), ruta.parent / "consulta.json"):
        if cand.exists() and cand != ruta:
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except ValueError:
                pass
    return {}


def _fecha_de_ruta(ruta: Path) -> str | None:
    for parte in [ruta.name] + [p.name for p in ruta.parents]:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", parte)
        if m:
            return m.group(1)
    return None


def _sub_de_ruta(ruta: Path) -> str | None:
    for parte in [ruta.name] + [p.name for p in ruta.parents]:
        m = RE_SUB.search(parte)
        if m:
            return m.group(0).upper()
    return None


def leer_ficha_html(data: bytes, url: str | None = None) -> dict:
    """Lee una página de la ficha del Portal (cualquier pestaña) guardada desde el navegador."""
    crudo = data.decode("utf-8", errors="replace")
    if "charset=iso-8859-1" in crudo[:3000].lower() or "charset=windows-1252" in crudo[:3000].lower():
        crudo = data.decode("cp1252", errors="replace")
    p = _Arbol()
    p.feed(crudo)
    if not url:
        for c in p.comentarios:
            m = re.search(r"saved from url=\(\d+\)(\S+)", c)
            if m:
                url = m.group(1).strip()
                break
    texto_pagina = _limpio(_texto(p.raiz))
    filas: list[dict] = []
    m_lote = re.search(r"[?&]idLote=(\d+)", url or "")
    ctx = {"seccion": None, "lote": m_lote.group(1) if m_lote else None, "bien": None, "tipo_bien": None}

    def visita(n):
        if isinstance(n, str):
            return
        titular = n.tag in ("h1", "h2", "h3", "h4", "h5", "h6", "caption", "legend")
        corto = n.tag in ("strong", "b", "div", "p", "span", "th") and len(_limpio(_texto(n))) < 120
        if titular or corto:
            t = _limpio(_texto(n))
            nt = norm(t)
            if not titular:
                pass
            elif nt.startswith("INFORMACION GENERAL"):
                ctx.update(seccion="general", bien=None)
            elif nt.startswith("AUTORIDAD GESTORA"):
                ctx.update(seccion="autoridad", bien=None)
            elif nt.startswith("BIENES"):
                ctx.update(seccion="bienes", bien=None)
            elif nt.startswith("PUJAS"):
                ctx.update(seccion="pujas", bien=None)
            elif nt.startswith("INFORMACION COMPLEMENTARIA"):
                ctx.update(seccion="complementaria", bien=None)
            m = re.match(r"LOTE\s+(\d+)\s*$", nt) if not titular else re.match(r"LOTE\s+(\d+)\b", nt)
            if m and not (m_lote and not titular):
                ctx.update(lote=m.group(1), bien=None)
            m = re.match(r"BIEN\s+(\d+)\s*(?:[-–:]\s*(.+))?$", nt)
            if m and not any(not isinstance(h, str) and re.match(r"(?i)\s*bien\s+\d", _texto(h)) for h in n.hijos):
                ctx.update(bien=m.group(1), seccion="bienes")
                tipo = re.match(r"(?i)bien\s+\d+\s*[-–:]\s*(.+)$", t)
                ctx["tipo_bien"] = tipo.group(1).strip() if tipo else None
                filas.append({"rotulo": "TIPO DE BIEN (ENCABEZADO)", "valor": ctx["tipo_bien"] or "", "enlaces": [],
                              **{k: ctx[k] for k in ("seccion", "lote", "bien")}, "origen": f"encabezado «{t}»"})
        if n.tag == "tr":
            celdas = [h for h in n.hijos if not isinstance(h, str) and h.tag in ("th", "td")]
            if len(celdas) >= 2 and (celdas[0].tag == "th" or len(celdas) == 2):
                rot = _limpio(_texto(celdas[0])).rstrip(":")
                val = _limpio(" ".join(_texto(c) for c in celdas[1:]))
                filas.append({"rotulo": norm(rot), "rotulo_original": rot, "valor": val,
                              "enlaces": [e for c in celdas[1:] for e in _enlaces(c)],
                              **{k: ctx[k] for k in ("seccion", "lote", "bien")}, "origen": f"fila «{rot}»"})
                return
        if n.tag == "dl":
            dts = [h for h in n.hijos if not isinstance(h, str) and h.tag in ("dt", "dd")]
            for a, b in zip(dts, dts[1:]):
                if a.tag == "dt" and b.tag == "dd":
                    rot = _limpio(_texto(a)).rstrip(":")
                    filas.append({"rotulo": norm(rot), "rotulo_original": rot, "valor": _limpio(_texto(b)),
                                  "enlaces": _enlaces(b), **{k: ctx[k] for k in ("seccion", "lote", "bien")},
                                  "origen": f"fila «{rot}»"})
            return
        for h in n.hijos:
            visita(h)

    visita(p.raiz)
    m = re.search(r"\bSubasta\s+(SUB-[A-Z]{2}-\d{4}-[A-Z0-9]+)", texto_pagina, re.I)
    encabezado = m.group(1).upper() if m else None
    ident = next((f["valor"] for f in filas if f["rotulo"] == "IDENTIFICADOR"), None)
    if ident and RE_SUB.search(ident):
        encabezado = encabezado or RE_SUB.search(ident).group(0).upper()
    if not encabezado:
        m = RE_SUB.search(texto_pagina)
        encabezado = m.group(0).upper() if m else None
    return {"url": url, "encabezado": encabezado, "filas": filas, "texto": texto_pagina,
            "enlaces": _enlaces(p.raiz)}


def campos_de_ficha(ficha: dict) -> list[dict]:
    """Convierte las filas de la ficha en datos con su origen. No supone nada que la ficha no diga."""
    salida = []
    es_autoridad = any(f["rotulo"] == "CODIGO" for f in ficha["filas"]) and not any(
        f["rotulo"] == "TIPO DE SUBASTA" for f in ficha["filas"])
    for f in ficha["filas"]:
        rot, val = f["rotulo"], f["valor"]
        lote, bien = f.get("lote") or "", f.get("bien") or ""
        pest = {"general": "Información general", "autoridad": "Autoridad gestora", "bienes": "Bienes",
                "pujas": "Pujas", "complementaria": "Información complementaria"}.get(f.get("seccion") or "", "")
        if rot == "TIPO DE BIEN (ENCABEZADO)":
            campo = "tipo_bien"
        elif bien and rot in ROT_BIEN:
            campo = ROT_BIEN[rot]
        elif (f.get("seccion") == "autoridad" or es_autoridad) and rot in ROT_AUTORIDAD:
            campo, pest = ROT_AUTORIDAD[rot], "Autoridad gestora"
        elif rot in ROT_GENERAL:
            campo = ROT_GENERAL[rot]
        elif "PUJA MAXIMA" in rot or "PUJA MAS ALTA" in rot:
            campo = "puja_maxima"
            pest = "Pujas"
        elif rot in ROT_BIEN and not bien:
            campo = ROT_BIEN[rot]
        else:
            campo = "fila:" + (f.get("rotulo_original") or rot)
        d = {"n_lote": lote, "n_bien": bien, "campo": campo, "texto_origen": val,
             "origen": " · ".join(x for x in ("Portal de Subastas", pest, f"lote {lote}" if lote else "",
                                                f"bien {bien}" if bien else "", f["origen"]) if x),
             "valor": val or None, "valor_num": None, "clase_valor": "texto" if val else "vacio", "revisar": 0, "nota": None}
        if campo in CAMPOS_IMPORTE:
            d["valor_num"], d["clase_valor"] = valor_importe(val)
            d["valor"] = None if d["valor_num"] is None else f"{d['valor_num']:.2f}"
        elif campo in CAMPOS_FECHA:
            iso = valor_fecha(val)
            d["valor"], d["clase_valor"] = (iso, "fecha") if iso else (val or None, "texto" if val else "vacio")
            if val and "ISO:" not in val:
                d["nota"] = "La ficha no da la fecha ISO; desfase deducido de CET/CEST si consta"
        elif campo == "cuenta_expediente":
            d["valor"] = val.strip() or None  # se conserva como texto
        salida.append(d)
        for texto_enl, href in f.get("enlaces") or []:
            if re.search(r"(?i)\.pdf\b|descarga|documento|edicto|certific", href + " " + texto_enl):
                salida.append({"n_lote": lote, "n_bien": bien, "campo": "documento_enlace", "valor": texto_enl or href,
                               "valor_num": None, "clase_valor": "enlace", "texto_origen": href,
                               "origen": f"Portal de Subastas · {pest or 'ficha'} · enlace «{texto_enl}»", "revisar": 0,
                               "nota": None})
    # Estado: aviso de la ficha, junto al encabezado «Subasta SUB-…» (no en los menús de la página)
    tn = norm(ficha["texto"])
    pos = tn.find(ficha["encabezado"] or "\x00")
    cabecera = tn[max(0, pos - 150): pos + 700] if pos >= 0 else ""
    hallados = [(e, re.search(p, cabecera)) for e, p in ESTADOS]
    hallados = [(e, m) for e, m in hallados if m]
    if hallados:
        e, m = hallados[0]
        frag = contexto(cabecera, m.start(), m.end(), 60)
        salida.append({"n_lote": "", "n_bien": "", "campo": "estado", "valor": e, "valor_num": None, "clase_valor": "estado",
                       "texto_origen": frag, "origen": "Portal de Subastas · aviso de estado de la ficha",
                       "revisar": int(len(hallados) > 1), "nota": ("Varios avisos posibles: " + ", ".join(x for x, _ in hallados))
                       if len(hallados) > 1 else None})
    # Pujas: «sin pujas» y tipo de puja según el estado
    if re.search(r"SIN PUJAS|NO HAY PUJAS|NO SE HAN (REALIZADO|RECIBIDO) PUJAS|NO HA RECIBIDO PUJAS", tn):
        m = re.search(r"SIN PUJAS|NO HAY PUJAS|NO SE HAN (REALIZADO|RECIBIDO) PUJAS|NO HA RECIBIDO PUJAS", tn)
        salida.append({"n_lote": "", "n_bien": "", "campo": "resultado_pujas", "valor": "sin pujas", "valor_num": None,
                       "clase_valor": "texto", "texto_origen": contexto(tn, m.start(), m.end(), 40),
                       "origen": "Portal de Subastas · Pujas", "revisar": 0, "nota": "Importe vacío: la ficha dice que no hay pujas"})
    estado = next((d["valor"] for d in salida if d["campo"] == "estado"), None)
    for d in salida:
        if d["campo"] == "puja_maxima":
            if estado == "en_curso":
                d["campo"] = "puja_actual"
            elif estado in ("pujas_concluidas", "finalizada_por_autoridad"):
                d["campo"] = "puja_final"
            else:
                d["nota"] = "Puja máxima sin estado claro de la subasta en la misma página"
    return salida


# ---- PDF (edicto, certificación de cargas, certificado de cierre, otros)
def texto_pdf(ruta: Path) -> tuple[list[str], str, list[str]]:
    """Texto por página. Devuelve (páginas, método, avisos). Primero el texto del PDF; si es un escaneo, OCR."""
    avisos: list[str] = []
    paginas: list[str] = []
    if shutil.which("pdftotext"):
        n = _n_paginas(ruta)
        for i in range(1, (n or 0) + 1):
            r = subprocess.run(["pdftotext", "-layout", "-f", str(i), "-l", str(i), str(ruta), "-"],
                               capture_output=True, timeout=120)
            paginas.append(r.stdout.decode("utf-8", errors="replace"))
    else:
        try:
            import pypdf  # opcional
            paginas = [pg.extract_text() or "" for pg in pypdf.PdfReader(str(ruta)).pages]
        except Exception as e:  # sin extractor de texto
            avisos.append(f"No hay extractor de texto de PDF instalado ({e.__class__.__name__})")
            return [], "sin_texto", avisos
    if paginas and sum(len(p.strip()) for p in paginas) >= 40 * len(paginas):
        return paginas, "texto", avisos
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        avisos.append("El PDF parece escaneado y no hay OCR instalado (tesseract): queda pendiente de lectura")
        return paginas, "sin_texto", avisos
    ocr = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(1, len(paginas) + 1 if paginas else (_n_paginas(ruta) or 0) + 1):
            base = Path(tmp) / f"p{i}"
            subprocess.run(["pdftoppm", "-r", "300", "-f", str(i), "-l", str(i), "-png", "-singlefile", str(ruta), str(base)],
                           capture_output=True, timeout=300)
            r = subprocess.run(["tesseract", str(base) + ".png", "-", "-l", "spa", "tsv"], capture_output=True, timeout=300)
            lineas: dict[tuple, list] = {}
            for fila in r.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
                c = fila.split("\t")
                if len(c) == 12 and c[11].strip():
                    lineas.setdefault((c[2], c[3], c[4]), []).append((c[11], float(c[10])))
            texto = []
            for palabras in lineas.values():
                conf = sum(p[1] for p in palabras) / len(palabras)
                linea = " ".join(p[0] for p in palabras)
                texto.append(("[OCR DUDOSO] " if conf < 75 else "") + linea)
            ocr.append("\n".join(texto))
    return ocr, "ocr", avisos


def _n_paginas(ruta: Path) -> int | None:
    if shutil.which("pdfinfo"):
        r = subprocess.run(["pdfinfo", str(ruta)], capture_output=True, timeout=60)
        m = re.search(r"Pages:\s+(\d+)", r.stdout.decode("utf-8", errors="replace"))
        if m:
            return int(m.group(1))
    try:
        import pypdf
        return len(pypdf.PdfReader(str(ruta)).pages)
    except Exception:
        return None


def clase_documento(nombre: str, primera: str) -> str:
    t = norm(nombre + " " + primera[:3000])
    if re.search(r"CERTIFICADO DE CIERRE|ACTA DE CIERRE|CIERRE DE (LA )?SUBASTA", t):
        return "certificado_cierre"
    if re.search(r"CERTIFICACION", t) and re.search(r"CARGAS|REGISTRAL|DOMINIO|REGISTRO DE LA PROPIEDAD", t):
        return "certificacion_cargas"
    if re.search(r"DECRETO", t) and re.search(r"ADJUDICA", t):
        return "resolucion_adjudicacion"
    if re.search(r"\bEDICTO\b", t):
        return "edicto"
    return "otro"


RE_IMP = r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})\s*(?:€|euros|EUROS)"


def campos_de_pdf(clase: str, paginas: list[str]) -> list[dict]:
    salida = []

    def add(campo, valor, pagina, frag, num=None, revisar=0, nota=None, clase_valor="texto"):
        salida.append({"n_lote": "", "n_bien": "", "campo": campo, "valor": valor, "valor_num": num,
                       "clase_valor": clase_valor, "texto_origen": frag, "origen": f"{clase} · página {pagina}",
                       "revisar": int(revisar or "[OCR DUDOSO]" in (frag or "")), "nota": nota})

    for i, pag in enumerate(paginas, 1):
        plano = re.sub(r"[ \t]+", " ", pag)
        if clase == "edicto":
            for m in re.finditer(r"(?i)\b(?:procedimiento|autos)\b[^.\n]{0,60}?(\d{1,6}\s*/\s*\d{2,4})", plano):
                add("numero_procedimiento", re.sub(r"\s", "", m.group(1)), i, contexto(plano, m.start(), m.end(), 40))
            for m in re.finditer(r"(?i)\b(pleno dominio|nuda propiedad|usufructo|derecho de superficie|"
                                 r"(?:mitad|cuota|participaci[oó]n) indivisa|\d{1,3}(?:[.,]\d+)?\s*%\s*(?:de|del)\s+"
                                 r"(?:pleno dominio|la nuda propiedad|la propiedad|usufructo))", plano):
                add("derecho_subastado", m.group(1).strip(), i, contexto(plano, m.start(), m.end(), 60))
            for m in re.finditer(r"(?i)(valorad[oa]s?|tasad[oa]s?|valor(?:aci[oó]n)?\s+de\s+(?:tasaci[oó]n|subasta)|"
                                 r"tipo\s+de\s+(?:la\s+)?subasta)[^\d\n]{0,60}?" + RE_IMP, plano):
                v, _ = valor_importe(m.group(2))
                add("valoracion_edicto", f"{v:.2f}" if v is not None else None, i,
                    contexto(plano, m.start(), m.end(), 40), num=v, clase_valor="importe")
            for par in re.split(r"\n\s*\n", pag):
                if re.search(r"\b(URBANA|R[ÚU]STICA|FINCA|VIVIENDA|LOCAL|PARCELA|SOLAR|NAVE|GARAJE)\b", par):
                    add("descripcion_bien_edicto", _limpio(par)[:1500], i, _limpio(par)[:300])
            for m in re.finditer(r"[^.\n]*\bcondici[oó]n(?:es)?\b[^.\n]*\.", plano, re.I):
                add("condicion_edicto", _limpio(m.group(0))[:600], i, _limpio(m.group(0))[:300])
        elif clase == "certificacion_cargas":
            for m in re.finditer(r"(?i)\b(?:C\.?\s?R\.?\s?U\.?|IDUFIR)\s*[:.]?\s*(\d{14})\b", plano):
                add("idufir", m.group(1), i, contexto(plano, m.start(), m.end(), 40))
            for m in re.finditer(r"(?i)\bfinca\s+(?:registral\s+)?(?:n[º°.oú]*\s*|n[úu]mero\s+)?(\d{1,7}(?:/\d+)?)\b", plano):
                add("finca_registral", m.group(1), i, contexto(plano, m.start(), m.end(), 40))
            for m in re.finditer(r"(?i)(?:expido|firmo|certifico|libro la presente)[^.]{0,160}?(\d{1,2}\s+de\s+[a-záéíóú]+\s+de\s+\d{4})", plano):
                add("fecha_certificacion", iso_fecha_larga(m.group(1)) or m.group(1), i, contexto(plano, m.start(), m.end(), 30))
            for linea in pag.splitlines():
                if re.match(r"\s*(?:[-–•·]\s*)?(?:HIPOTECA|EMBARGO|ANOTACI[ÓO]N|AFECCI[ÓO]N|SERVIDUMBRE|CONDICI[ÓO]N RESOLUTORIA|"
                            r"ARRENDAMIENTO|USUFRUCTO|NOTA MARGINAL|CARGAS?\b|INSCRIPCI[ÓO]N\s+\d|LETRA\s+[A-Z]\b)", linea, re.I):
                    add("asiento_texto", _limpio(linea)[:400], i, _limpio(linea)[:400],
                        nota="Contenido literal del documento; sin interpretar")
        elif clase == "certificado_cierre":
            for m in re.finditer(r"(?i)\b(desierta|sin pujas|con pujas|adjudicad[oa]|concluida|suspendida|cancelada)\b", plano):
                add("resultado_cierre", m.group(1).lower(), i, contexto(plano, m.start(), m.end(), 60))
            for m in re.finditer(r"(\d{2}[-/]\d{2}[-/]\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?(?:\s*CES?T)?)", plano):
                add("fecha_cierre", valor_fecha(m.group(1)) or m.group(1), i, contexto(plano, m.start(), m.end(), 50))
            for m in re.finditer(r"(?P<rot>[A-ZÁÉÍÓÚa-záéíóúñ][A-ZÁÉÍÓÚa-záéíóúñ ]{3,50}?)\s*:?\s*" + RE_IMP, plano):
                v, _ = valor_importe(m.group(2))
                add("importe_certificado_cierre", f"{v:.2f}" if v is not None else None, i,
                    contexto(plano, m.start(), m.end(), 20), num=v, clase_valor="importe",
                    nota=f"Denominación en el documento: «{_limpio(m.group('rot'))}»")
        elif clase == "resolucion_adjudicacion":
            for m in re.finditer(r"(?i)(?:se\s+adjudica|adjudic\w+)[^.]{0,200}?" + RE_IMP, plano):
                v, _ = valor_importe(m.group(1))
                add("importe_adjudicacion", f"{v:.2f}" if v is not None else None, i,
                    contexto(plano, m.start(), m.end(), 30), num=v, revisar=1, clase_valor="importe",
                    nota="Leído de una resolución de adjudicación: revisar que la dicte la autoridad gestora")
    return salida


def procesar_portal(con, dir_datos: Path, stats: dict, avisos: list[str]) -> set[str]:
    """Lee las fichas y documentos del Portal guardados en datos/portal/entrada (consultas puntuales)."""
    entrada = dir_datos / "portal" / "entrada"
    tocados: set[str] = set()
    if not entrada.exists():
        return tocados
    for ruta in sorted(entrada.rglob("*")):
        if not ruta.is_file() or ruta.suffix.lower() not in (".html", ".htm", ".pdf"):
            continue
        rel = str(ruta.relative_to(dir_datos))
        data = ruta.read_bytes()
        huella = sha256(data)
        previa = con.execute("SELECT id_captura, sha256 FROM capturas WHERE archivo=?", (rel,)).fetchone()
        if previa and previa["sha256"] == huella:
            continue
        meta = _meta_archivo(ruta)
        esperado = (meta.get("sub") or _sub_de_ruta(ruta) or "").upper() or None
        fecha_consulta = meta.get("fecha_consulta") or _fecha_de_ruta(ruta)
        url = meta.get("url")
        lote = str(meta.get("lote") or "") or None
        if lote is None:
            m = re.search(r"(?i)lote[\s_-]*(\d+)", ruta.name)
            lote = m.group(1) if m else None
        if previa:
            con.execute("DELETE FROM campos WHERE id_captura=?", (previa["id_captura"],))
            con.execute("DELETE FROM documentos WHERE id_captura=?", (previa["id_captura"],))
            con.execute("DELETE FROM capturas WHERE id_captura=?", (previa["id_captura"],))
        clase = "pdf" if ruta.suffix.lower() == ".pdf" else "html"
        cur = con.execute("INSERT INTO capturas (archivo, sha256, clase, url, fecha_consulta, id_subasta_esperado, n_lote,"
                          " procesado_en) VALUES (?,?,?,?,?,?,?,?)",
                          (rel, huella, clase, url, fecha_consulta, esperado, lote, ahora()))
        idc = cur.lastrowid
        faltan = [x for x, v in (("URL", url), ("fecha de consulta", fecha_consulta)) if not v]
        if clase == "html":
            ficha = leer_ficha_html(data, url)
            url = ficha["url"] or url
            enc = ficha["encabezado"]
            esperado = esperado or (RE_SUB.search(urllib.parse.unquote(url or "")).group(0).upper()
                                    if url and RE_SUB.search(urllib.parse.unquote(url)) else None)
            coincide = None if not (enc and esperado) else int(enc == esperado)
            if not enc:
                estado, detalle = "sin_sub", "La página no muestra el SUB en el encabezado: no se usa"
            elif coincide == 0:
                estado, detalle = "sub_no_coincide", f"Se esperaba {esperado} y el encabezado dice {enc}: no se usa"
                incidencia(con, "ficha_sub_no_coincide", rel, detalle, url=url)
            else:
                estado, detalle = "ok", ("Sin SUB esperado en el nombre: se usa el del encabezado" if coincide is None else None)
                for d in campos_de_ficha(ficha):
                    _guardar_campo(con, enc, d, idc, url, fecha_consulta)
                tocados.add(enc)
            faltan = [x for x, v in (("URL", url), ("fecha de consulta", fecha_consulta)) if not v]
            notas = [x for x in (detalle, "Falta " + " y ".join(faltan) if faltan else None) if x]
            con.execute("UPDATE capturas SET url=?, id_subasta_esperado=?, id_subasta_encabezado=?, coincide=?, estado=?,"
                        " detalle=? WHERE id_captura=?", (url, esperado, enc, coincide, estado, "; ".join(notas) or None, idc))
        else:
            valido = data[:5] == b"%PDF-" and _n_paginas(ruta) is not None
            paginas, metodo, av = (texto_pdf(ruta) if valido else
                                   ([], "no_valido", ["El archivo no es un PDF válido (cabecera o estructura)"]))
            primera = paginas[0] if paginas else ""
            clase_doc = clase_documento(ruta.name, primera) if valido else "no_valido"
            texto_total = "\n".join(paginas)
            m = RE_SUB.search(texto_total)
            sub = esperado or (m.group(0).upper() if m else None)
            if m and esperado and m.group(0).upper() != esperado:
                av.append(f"El PDF cita {m.group(0).upper()} y la carpeta dice {esperado}")
            if lote is None:
                mm = re.search(r"(?i)\blote\s*(?:n[º°.]?\s*)?(\d+)\b", primera)
                lote = mm.group(1) if mm else None
            revisar = int(metodo in ("ocr", "sin_texto") or bool(av))
            con.execute("INSERT OR REPLACE INTO documentos VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (idc, sub, lote, clase_doc, ruta.name, url, int(valido), (_n_paginas(ruta) or len(paginas)) if valido else 0,
                         metodo, revisar, "; ".join(av) or None))
            estado = "ok" if valido and sub else ("pdf_no_valido" if not valido else "sin_sub")
            if not valido:
                incidencia(con, "pdf_no_valido", rel, "; ".join(av) or "PDF no válido", url=url)
            if valido and sub:
                for d in campos_de_pdf(clase_doc, paginas):
                    d["n_lote"] = lote or ""
                    _guardar_campo(con, sub, d, idc, url, fecha_consulta)
                tocados.add(sub)
            con.execute("UPDATE capturas SET id_subasta_encabezado=?, estado=?, detalle=? WHERE id_captura=?",
                        (sub, estado, "; ".join(av + (["Falta " + " y ".join(faltan)] if faltan else [])) or None, idc))
        stats["capturas"] = stats.get("capturas", 0) + 1
    return tocados


def _guardar_campo(con, sub: str, d: dict, idc: int, url: str | None, fecha: str | None):
    nota = d.get("nota")
    if not url:
        nota = "; ".join(x for x in [nota, "La URL no consta en el archivo guardado"] if x)
    con.execute("INSERT INTO campos (id_subasta, n_lote, n_bien, campo, valor, valor_num, clase_valor, texto_origen, url,"
                " fecha_consulta, origen, id_captura, revisar, nota, fuente) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sub, d.get("n_lote") or "", d.get("n_bien") or "", d["campo"], d.get("valor"), d.get("valor_num"),
                 d.get("clase_valor"), (d.get("texto_origen") or "")[:2000], url, fecha, d["origen"], idc,
                 d.get("revisar", 0), nota, FUENTE_PORTAL))


# --------------------------------------------------------------------------- pendientes y cotejo
DATOS_FICHA = [  # (dato, dónde consta en el Portal, ¿lo muestra la ficha pública?)
    ("ubicacion_bien", "Bienes: Dirección, Código Postal, Localidad y Provincia de cada bien", True),
    ("tipo_subasta", "Información general: Tipo de subasta", True),
    ("cuenta_expediente", "Información general: Cuenta expediente", True),
    ("estado", "aviso de estado de la ficha", True),
    ("fecha_inicio", "Información general: Fecha de inicio", True),
    ("fecha_conclusion", "Información general: Fecha de conclusión", True),
    ("valor_subasta", "Información general o lote: Valor subasta", True),
    ("tasacion", "Información general o lote: Tasación", True),
    ("cantidad_reclamada", "Información general: Cantidad reclamada", True),
    ("puja_minima", "Información general o lote: Puja mínima", True),
    ("tramos_pujas", "Información general o lote: Tramos entre pujas", True),
    ("deposito", "Información general o lote: Importe del depósito", True),
    ("autoridad_codigo", "Autoridad gestora: Código", True),
    ("autoridad_descripcion", "Autoridad gestora: Descripción", True),
    ("lotes", "Información general: Lotes y enlaces de lote", True),
    ("bienes", "Bienes: bloques Bien 1, Bien 2…", True),
    ("puja", "Pujas: puja máxima o «sin pujas»", True),
    ("documentos", "Información complementaria: edicto, certificación de cargas, certificado de cierre", True),
    ("importe_adjudicacion", "documento oficial de la autoridad gestora (no lo publica la ficha)", False),
]
CAMPOS_POR_DATO = {
    "ubicacion_bien": ("provincia", "localidad"), "lotes": ("lotes_texto",), "bienes": ("tipo_bien", "descripcion"),
    "puja": ("puja_actual", "puja_final", "puja_maxima", "resultado_pujas"),
    "documentos": ("documento_enlace",),
}
DATO_EXPEDIENTE = {"tipo_subasta", "cuenta_expediente", "estado", "autoridad_codigo", "autoridad_descripcion", "lotes",
                   "cantidad_reclamada", "fecha_inicio", "fecha_conclusion", "documentos"}
SUBASTECH_CAMPO = {"valor_subasta": "valor_subasta", "tasacion": "tasacion", "deposito": "deposito",
                   "puja_minima": "puja_minima", "fecha_conclusion": "fecha_fin", "estado": "estado",
                   "ubicacion_bien": "municipio", "puja": "precio_cierre"}


def _subastech(dir_datos: Path) -> dict[str, list]:
    ruta = dir_datos / "subastech.sqlite"
    if not ruta.exists():
        return {}
    c = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        filas = c.execute("SELECT * FROM subastech_lotes WHERE id_subasta IS NOT NULL").fetchall()
    except sqlite3.Error:
        filas = []
    c.close()
    out: dict[str, list] = {}
    for r in filas:
        out.setdefault(r["id_subasta"].upper(), []).append(r)
    return out


PENDIENTES_DDL = """CREATE TABLE pendientes (
  id_expediente TEXT, id_subasta TEXT, n_lote TEXT, datos TEXT, n_datos INTEGER, motivo TEXT,
  fuente_revisada TEXT, con_valor_subastech TEXT, generado_en TEXT)"""


def regenerar_pendientes(con, dir_datos: Path):
    con.execute("DROP TABLE IF EXISTS pendientes")  # tabla derivada: se rehace entera (y con su forma actual)
    con.execute(PENDIENTES_DDL)
    con.execute("DELETE FROM datos_ficha")
    con.executemany("INSERT INTO datos_ficha VALUES (?,?,?)", [(d, w, int(p)) for d, w, p in DATOS_FICHA])
    m = ahora()
    sx = _subastech(dir_datos)
    exps = con.execute("SELECT * FROM expedientes WHERE ambito IN ('ubicacion_en_ambito','sede_en_ambito_ubicacion_pendiente')").fetchall()
    filas = []
    for e in exps:
        anuncio = con.execute("SELECT url_html, url_xml, referencia_tipo, referencia_texto, texto_desde FROM anuncios WHERE id_boe=?",
                              (e["primer_anuncio"],)).fetchone()
        revisada = ""
        if anuncio:
            revisada = anuncio["url_html"] if anuncio["texto_desde"] == "html" else (anuncio["url_xml"] or anuncio["url_html"])
        if not e["id_subasta"]:
            filas.append((e["id_expediente"], None, "", "importes, lotes y bienes", 1,
                          "El anuncio no tiene SUB ni ficha en el Portal; los datos están en su texto y aún no se extraen "
                          "de forma estructurada", revisada, None, m))
            continue
        sub = e["id_subasta"]
        consultada = con.execute("SELECT GROUP_CONCAT(DISTINCT url) FROM capturas WHERE id_subasta_encabezado=? AND estado='ok'",
                                 (sub,)).fetchone()[0]
        tiene: dict[str, set] = {}
        for r in con.execute("SELECT DISTINCT n_lote, campo FROM campos WHERE id_subasta=?", (sub,)):
            tiene.setdefault(r["n_lote"] or "", set()).add(r["campo"])
        lotes = sorted((k for k in tiene if k), key=lambda x: (len(x), x)) or [""]
        todos = set().union(*tiene.values()) if tiene else set()
        ref = f" y su referencia ({anuncio['referencia_tipo']} {anuncio['referencia_texto']})" if anuncio and anuncio["referencia_texto"] else ""
        if consultada:
            motivo_pub = "La ficha guardada no muestra este dato"
        else:
            motivo_pub = (f"El anuncio {e['primer_anuncio']} solo publica el SUB, la autoridad{ref}; el dato está en la ficha "
                          "del Portal, que solo se incorpora con una consulta puntual guardada en datos/portal/entrada o con "
                          "un canal autorizado por la AEBOE")
        motivo_adj = ("Solo se registra si lo acredita un documento oficial de la autoridad gestora; no se ha incorporado "
                      "ninguno (Subastech no lo acredita)")
        grupos: dict[tuple, list] = {}
        for dato, _donde, publico in DATOS_FICHA:
            buscados = set(CAMPOS_POR_DATO.get(dato, (dato,)))
            por_exp = dato in DATO_EXPEDIENTE
            for lt in ([""] if por_exp else lotes):
                hay = tiene.get(lt, set()) | tiene.get("", set()) | (todos if por_exp else set())
                if buscados & hay:
                    continue
                grupos.setdefault((lt, motivo_pub if publico else motivo_adj), []).append(dato)
        fuente = (consultada + " ; " if consultada else "") + revisada
        for (lt, motivo), datos in grupos.items():
            con_sx = [d for d in datos if SUBASTECH_CAMPO.get(d) and any(
                r[SUBASTECH_CAMPO[d]] not in (None, "") for r in sx.get(sub, []))]
            filas.append((e["id_expediente"], sub, lt, ", ".join(datos), len(datos), motivo, fuente,
                          ", ".join(con_sx) or None, m))
    con.executemany("INSERT INTO pendientes VALUES (?,?,?,?,?,?,?,?,?)", filas)


def regenerar_cotejo(con, dir_datos: Path):
    con.execute("DELETE FROM cotejo_subastech")
    m = ahora()
    pares = [("valor_subasta", "valor_subasta"), ("tasacion", "tasacion"), ("deposito", "deposito"),
             ("puja_minima", "puja_minima"), ("municipio", "localidad"), ("codigo_postal", "codigo_postal"),
             ("precio_cierre", "puja_final"), ("fecha_fin", "fecha_conclusion")]
    filas = []
    for sub, lotes in _subastech(dir_datos).items():
        for r in lotes:
            lote = str(r["n_lote"]) if r["n_lote"] not in (None, "") else ""
            for col_sx, campo in pares:
                vs = r[col_sx]
                if vs in (None, ""):
                    continue
                ofi = con.execute("SELECT valor, valor_num, origen, url, fecha_consulta FROM campos WHERE id_subasta=? AND campo=?"
                                  " AND (n_lote=? OR n_lote='') ORDER BY n_lote DESC, id DESC LIMIT 1", (sub, campo, lote)).fetchone()
                if ofi is None:
                    res, vb, ev = "sin_cotejar", None, None
                else:
                    vb = ofi["valor"]
                    if ofi["valor_num"] is not None and isinstance(vs, (int, float)):
                        res = "coincide" if abs(float(vs) - ofi["valor_num"]) < 0.01 else "difiere"
                    elif campo == "fecha_conclusion":
                        res = "coincide" if str(vs)[:10] == str(vb or "")[:10] else "difiere"
                    else:
                        res = "coincide" if clave_geo(str(vs)) == clave_geo(str(vb)) else "difiere"
                    ev = f"{ofi['origen']} · {ofi['url'] or 'URL no consta'} · consultado {ofi['fecha_consulta'] or 'sin fecha'}"
                filas.append((sub, lote, campo, str(vs), vb, res, ev, r["url_ficha"], m))
    con.executemany("INSERT INTO cotejo_subastech VALUES (?,?,?,?,?,?,?,?,?)", filas)


# --------------------------------------------------------------------------- informe y exportación
def informe(con, id_ej: int, stats: dict, avisos: list[str], errores: list[str], dir_datos: Path) -> Path:
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    L = [f"# SeMá Deal Finder · ejecución {id_ej}", "",
         f"Generado {ahora()}. Fuentes: diario oficial del BOE (API de datos abiertos del sumario y el texto oficial de "
         "cada anuncio, https://www.boe.es) y fichas del Portal de Subastas guardadas en consultas puntuales. Ningún dato procede de Subastech.", ""]
    L += ["## Esta ejecución", "",
          f"- Anuncios nuevos en las secciones IV, V.B y V.C: {stats.get('nuevos', 0)}",
          f"- XML de anuncios leídos: {stats.get('xml_leidos', 0)}; con error: {stats.get('errores_descarga', 0)}",
          f"- XML aún pendientes: {q('SELECT COUNT(*) FROM anuncios WHERE xml_estado<>?', 'ok')[0][0]}",
          f"- Expedientes nuevos: {stats.get('expedientes_nuevos', 0)}; archivos del Portal leídos: {stats.get('capturas', 0)}", ""]
    L += ["## Expedientes por provincia del ámbito", "", "| Provincia | Ubicación del bien confirmada | Según anuncio BOE | Solo sede de la autoridad (ubicación pendiente) |",
          "|---|---|---|---|"]
    for cod, nombre in PROVINCIAS_AMBITO.items():
        conf = q("SELECT COUNT(*) FROM expedientes WHERE ubicacion_estado='confirmada_ficha_portal' AND (','||provincias_bien||',') LIKE ?", f"%,{cod},%")[0][0]
        anun = q("SELECT COUNT(*) FROM expedientes WHERE ubicacion_estado='segun_anuncio_boe' AND (','||provincias_bien||',') LIKE ?", f"%,{cod},%")[0][0]
        sede = q("SELECT COUNT(*) FROM expedientes WHERE ambito='sede_en_ambito_ubicacion_pendiente' AND sede_provincia=?", cod)[0][0]
        L.append(f"| {nombre} | {conf} | {anun} | {sede} |")
    L += ["", "La provincia de la sede es solo una pista para elegir qué fichas consultar: no se usa para seleccionar.", ""]
    L += ["## Incidencias", ""]
    filas = q("SELECT tipo, SUM(resuelta_en IS NULL) abiertas, COUNT(*) total FROM incidencias GROUP BY tipo ORDER BY abiertas DESC")
    errores = [r for r in filas if r["tipo"] in TIPOS_ERROR]
    registros = [r for r in filas if r["tipo"] not in TIPOS_ERROR]
    L += ["Errores por resolver (se reintentan en las siguientes ejecuciones):"]
    L += [f"- {r['tipo']}: {r['abiertas']}" for r in errores if r["abiertas"]] or ["- Ninguno"]
    L += ["", "Registros para tener constancia (no son fallos):"]
    L += [f"- {r['tipo']}: {r['total']}" for r in registros] or ["- Ninguno"]
    L += ["", "## Datos pendientes", ""]
    cuenta: dict[str, set] = {}
    for r in q("SELECT id_expediente, datos FROM pendientes"):
        for d in (r["datos"] or "").split(", "):
            cuenta.setdefault(d, set()).add(r["id_expediente"])
    L += [f"- {d}: {len(s)} expedientes" for d, s in sorted(cuenta.items(), key=lambda x: -len(x[1]))] or ["- Ninguno"]
    disc = q("SELECT COUNT(*) FROM v_discrepancias")[0][0]
    cot = ", ".join(f"{r['resultado']} {r['n']}" for r in q(
        "SELECT resultado, COUNT(*) n FROM cotejo_subastech GROUP BY resultado")) or "sin datos"
    L += ["", f"Discrepancias entre fuentes oficiales: {disc}.",
          f"Cotejo de los valores de Subastech con la fuente oficial: {cot}.", ""]
    if avisos or errores:
        L += ["## Avisos", ""] + [f"- {a}" for a in (avisos[:50] + [f"ERROR {e}" for e in errores])]
    ruta = dir_datos / "ultimo_informe_dealfinder.md"
    ruta.write_text("\n".join(L) + "\n", encoding="utf-8")
    return ruta


PANEL = {
    "df_expedientes": "SELECT id_expediente, id_subasta, ambito, ubicacion_estado, provincias_bien, sede_provincia, sede_motivo,"
                      " organismo, tipo_subasta_anuncio, referencia_anuncio, lugar_firma, fecha_primer_anuncio, primer_anuncio,"
                      " n_anuncios, anuncios, fecha_ultimo_anuncio, url_portal_publicada, url_fuente, ficha_consultada, momento_consulta"
                      " FROM expedientes WHERE ambito IN ('ubicacion_en_ambito','sede_en_ambito_ubicacion_pendiente')"
                      " ORDER BY fecha_primer_anuncio DESC, id_expediente",
    "df_anuncios": "SELECT id_boe, fecha_publicacion, seccion_codigo, organismo, titulo, ids_subasta, tipo_subasta_texto,"
                   " referencia_tipo, referencia_texto, lugar_firma, fecha_firma, sede_provincia, ubicacion_estado,"
                   " ubicacion_provincias, ubicacion_municipios, es_inmueble, fecha_actualizacion, xml_estado, texto_desde,"
                   " url_html, url_pdf, url_xml FROM anuncios WHERE fecha_publicacion >= date('now', '-120 day') AND ("
                   "(menciona_subasta=1 AND (sede_provincia IN ('28','08','41','21') OR n_sub=0"
                   " OR (','||IFNULL(ubicacion_provincias,'')||',') GLOB '*,28,*' OR (','||IFNULL(ubicacion_provincias,'')||',') GLOB '*,08,*'"
                   " OR (','||IFNULL(ubicacion_provincias,'')||',') GLOB '*,41,*' OR (','||IFNULL(ubicacion_provincias,'')||',') GLOB '*,21,*'))"
                   " OR (xml_estado<>'ok' AND fecha_publicacion >= date('now', '-30 day')))"
                   " ORDER BY fecha_publicacion DESC, id_boe",
    "df_pendientes": "SELECT id_expediente, id_subasta, n_lote, n_datos, datos AS datos_pendientes, motivo,"
                     " con_valor_subastech AS con_valor_subastech_sin_cotejar, fuente_revisada FROM pendientes"
                     " ORDER BY id_expediente, n_lote, motivo",
    "df_incidencias": "SELECT tipo, clave, fecha, detalle, url, primera_vez, ultima_vez, veces, resuelta_en FROM incidencias"
                      " ORDER BY resuelta_en IS NOT NULL, ultima_vez DESC",
    "df_bienes": "SELECT * FROM v_bienes ORDER BY id_subasta, n_lote, CAST(n_bien AS INTEGER)",
    "df_discrepancias": "SELECT * FROM v_discrepancias ORDER BY id_subasta, campo",
    "df_campos": "SELECT id_subasta, n_lote, n_bien, campo, valor, clase_valor, texto_origen, origen, url, fecha_consulta, revisar, nota"
                 " FROM campos ORDER BY id_subasta, n_lote, n_bien, campo",
    "df_cotejo": "SELECT id_subasta, n_lote, campo, valor_subastech, valor_boe, resultado, evidencia_boe, url_subastech FROM cotejo_subastech"
                 " ORDER BY resultado, id_subasta",
    "df_sumarios": "SELECT fecha, estado, formato, http_status, fecha_en_sumario, diarios, n_items_secciones, intentos, momento_consulta, url"
                   " FROM sumarios ORDER BY fecha DESC",
}


def exportar(con, dir_datos: Path, geo: Geo | None = None):
    dest_csv = dir_datos / "export"
    dest_csv.mkdir(parents=True, exist_ok=True)
    dest = dir_datos / "panel"
    dest.mkdir(parents=True, exist_ok=True)
    generado = ahora()
    geo = geo or Geo()
    nombre_prov = lambda cods: ", ".join(geo.provincias.get(c, c) for c in (cods or "").split(",") if c) or None  # noqa: E731
    for nombre, sql in PANEL.items():
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        if nombre == "df_expedientes":  # nombres de provincia para el panel
            ib, isd = cols.index("provincias_bien"), cols.index("sede_provincia")
            cols += ["provincias_bien_nombres", "sede_provincia_nombre"]
            rows = [r + [nombre_prov(r[ib]), nombre_prov(r[isd])] for r in rows]
        with open(dest_csv / f"{nombre}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(cols)
            w.writerows(rows)
        doc = {"tabla": nombre, "generado": generado, "fuente": "BOE (https://www.boe.es)", "columns": cols, "rows": rows}
        texto = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        (dest / f"sema-panel-{nombre}.json").write_text(texto, encoding="utf-8")
        (dest / f"sema-panel-{nombre}.b64.txt").write_text(
            base64.b64encode(gzip.compress(texto.encode("utf-8"), 9, mtime=0)).decode("ascii"), encoding="ascii")


def copia_comprimida(origen: Path, destino: Path) -> dict:
    """Copia de la base para guardarla en Drive: se hace con la API de copia de SQLite (coherente aunque otra
    conexión esté escribiendo), se comprueba con integrity_check y se comprime con gzip sin fecha en la cabecera,
    así que el mismo contenido da siempre el mismo archivo. El conector de Drive admite unos 3 MB por envío y la
    base sin comprimir ya pasa de 8 MB."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        copia = Path(tmp) / origen.name
        src = sqlite3.connect(f"file:{origen}?mode=ro", uri=True)
        dst = sqlite3.connect(copia)
        try:
            src.backup(dst)
            estado = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            src.close()
            dst.close()
        if estado != "ok":
            raise RuntimeError(f"{origen.name} no pasa integrity_check: {estado}")
        with open(copia, "rb") as f, open(destino, "wb") as salida, \
                gzip.GzipFile(origen.name, "wb", 9, salida, mtime=0) as gz:
            shutil.copyfileobj(f, gz)
        bytes_base = copia.stat().st_size
    datos = destino.read_bytes()
    return {"archivo": str(destino), "bytes": len(datos), "bytes_sin_comprimir": bytes_base, "sha256": sha256(datos)}


def ejecutar(desde: dt.date | None, hasta: dt.date | None, dir_datos: Path, http_factory=None,
             solo_portal: bool = False) -> dict:
    con = sema.conectar(dir_datos / "dealfinder.sqlite", ESQUEMA)
    avisos: list[str] = []
    errores: list[str] = []
    stats = {"leidos": 0, "nuevos": 0, "cambios": 0}
    cur = con.execute("INSERT INTO ejecuciones (inicio, version, parametros, estado) VALUES (?,?,?,?)",
                      (ahora(), VERSION, json.dumps({"desde": str(desde), "hasta": str(hasta), "solo_portal": solo_portal}),
                       "en_curso"))
    id_ej = cur.lastrowid
    con.commit()
    geo = Geo()
    tocados: set[str] = set()
    estado = "ok"
    mantenimiento(con)
    tocados |= reanalizar_ubicaciones(con, geo, id_ej)
    try:
        if not solo_portal:
            http = (http_factory or HttpDiario)(con, id_ej, dir_datos / "brutos" / "dealfinder", avisos)
            dias = dias_a_procesar(con, hasta or dt.date.today(), desde)
            for dia in dias:
                try:
                    procesar_sumario(con, http, dia, id_ej, stats, avisos)
                    con.commit()
                    if hasattr(http, "volcar"):
                        http.volcar()
                except Exception as e:
                    con.rollback()
                    errores.append(f"sumario {dia}: {e.__class__.__name__}: {e}")
            try:
                tocados |= procesar_xml_pendientes(con, http, geo, id_ej, stats, avisos, MAX_XML_POR_EJECUCION)
                con.commit()
                if hasattr(http, "volcar"):
                    http.volcar()
            except Exception as e:
                con.rollback()
                errores.append(f"anuncios: {e.__class__.__name__}: {e}")
            if hasattr(http, "volcar"):
                http.volcar()
            if dias and stats.get("dias_fallidos", 0) == len(dias):
                estado = "sin_conexion"
        try:
            tocados |= procesar_portal(con, dir_datos, stats, avisos)
            con.commit()
        except Exception as e:
            con.rollback()
            errores.append(f"portal: {e.__class__.__name__}: {e}")
        # también los SUB que quedaron sin expediente (p. ej. si una ejecución anterior se cortó)
        tocados |= {r[0] for r in con.execute("SELECT DISTINCT id_subasta FROM anuncio_sub WHERE id_subasta NOT IN"
                                              " (SELECT id_expediente FROM expedientes)")}
        tocados |= {"BOE:" + r[0] for r in con.execute("SELECT clave FROM incidencias WHERE tipo='subasta_sin_sub' AND"
                                                       " 'BOE:' || clave NOT IN (SELECT id_expediente FROM expedientes)")}
        actualizar_expedientes(con, None if solo_portal else tocados, id_ej, stats, geo)
        regenerar_pendientes(con, dir_datos)
        regenerar_cotejo(con, dir_datos)
        con.commit()
    except Exception as e:
        con.rollback()
        errores.append(f"general: {e.__class__.__name__}: {e}")
        estado = "fallida"
    if errores and estado == "ok":
        estado = "con_errores"
    con.execute("UPDATE ejecuciones SET fin=?, n_leidos=?, n_nuevos=?, n_cambios=?, avisos=?, errores=?, estado=? WHERE id_ejecucion=?",
                (ahora(), stats["leidos"], stats["nuevos"], stats["cambios"], json.dumps(avisos, ensure_ascii=False),
                 json.dumps(errores, ensure_ascii=False), estado, id_ej))
    con.commit()
    ruta = informe(con, id_ej, stats, avisos, errores, dir_datos)
    exportar(con, dir_datos, geo)
    con.execute("VACUUM")  # la base va al repositorio en cada ejecución: sin huecos
    con.close()
    return {"id_ejecucion": id_ej, "estado": estado, "informe": str(ruta), **stats, "avisos": avisos[:30], "errores": errores}

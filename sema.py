#!/usr/bin/env python3
"""SeMá · Subastas BOE Analytics (provincia de Sevilla).

Base de datos diaria y auditable de subastas a partir de fuentes oficiales:
  - A1: API de datos abiertos del BOE (sumario diario) y los anuncios que enlaza.
  - A2: CSV semanal de subastas judiciales del Ministerio de Justicia (datos.gob.es).

Reglas:
  - No accede a subastas.boe.es (portal de subastas) ni automatiza ningún login.
  - Respeta robots.txt de cada dominio antes de descargar.
  - No inventa datos: lo que no viene en la fuente queda vacío.
  - Guarda cada respuesta en bruto con su hash SHA-256 para poder auditarla.

Solo usa la biblioteca estándar de Python (3.10+).
Uso:  python sema.py run [--desde AAAA-MM-DD] [--hasta AAAA-MM-DD] [--forzar-justicia]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from pathlib import Path

VERSION = "1.0.0"
BASE = Path(__file__).resolve().parent
UA = "SeMa-Subastas-BOE/1.0 (uso personal; reutilizacion de datos abiertos del BOE)"
API_SUMARIO = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"
CSV_JUSTICIA = "https://catalogodatos.justicia.es/csv/Subastas_Judiciales.csv"
CATASTRO_MUNICIPIOS = (
    "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCallejero.asmx/"
    "ConsultaMunicipio?Provincia=SEVILLA&Municipio="
)
# Endpoints oficiales de datos abiertos: si su robots.txt no se puede leer, se permite el acceso.
ENDPOINTS_DATOS_ABIERTOS = ("/datosabiertos/api/", "/csv/", "/ovcservweb/")
PAUSA = float(os.environ.get("SEMA_PAUSA", "1.0"))
DIAS_PRIMERA_CARGA = int(os.environ.get("SEMA_DIAS_PRIMERA_CARGA", "30"))

RE_SUB = re.compile(r"SUB-[A-Z]{2}-\d{4}-[A-Z0-9]+")
RE_FECHA = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{1,2} de [a-záéíóú]+ de \d{4}\b", re.I)
RE_IMPORTE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)\s*(?:€|EUROS?)")

# Rótulos que se buscan en el texto de los anuncios (comparación sin tildes y en mayúsculas).
ROTULOS = {
    "valor_subasta": ("VALOR DE SUBASTA", "VALOR SUBASTA"),
    "tasacion": ("VALOR DE TASACION", "TASACION"),
    "deposito": ("IMPORTE DEL DEPOSITO", "DEPOSITO"),
    "puja_minima": ("PUJA MINIMA",),
    "municipio": ("LOCALIDAD", "MUNICIPIO"),
    "codigo_postal": ("CODIGO POSTAL",),
    "direccion": ("DIRECCION",),
    "referencia_catastral": ("REFERENCIA CATASTRAL",),
    "tipo_bien": ("TIPO DE BIEN",),
    "fecha_inicio": ("FECHA DE INICIO",),
    "fecha_conclusion": ("FECHA DE CONCLUSION", "FECHA DE FIN"),
}
RE_TIPO_ANUNCIO = re.compile(r"Anuncio de subasta ([^.\n]{3,80}?)(?: con número| con n\.º|\.|$)", re.I | re.M)
RE_FIRMA = re.compile(r"^\s*([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñü .'()-]{1,60}?),\s*(\d{1,2} de [a-záéíóú]+ de \d{4})\s*\.?-", re.M)
CAMPOS_IMPORTE = {"valor_subasta", "tasacion", "deposito", "puja_minima"}


# --------------------------------------------------------------------------- utilidades
def norm(texto: str | None) -> str:
    """Mayúsculas, sin tildes y con espacios simples."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", texto)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().upper()


def ahora() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def importe(texto: str | None) -> float | None:
    if not texto:
        return None
    m = RE_IMPORTE.search(norm(texto))
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def localname(tag: str) -> str:
    return tag.split("}", 1)[-1]


def quitar_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        el.tag = localname(el.tag)
    return root


def texto_de_html(data: bytes) -> str:
    t = data.decode("utf-8", errors="replace")
    m = re.search(r'(?is)<div id="textoxslt">(.*?)<!-- #textoxslt -->', t)
    if m:  # cuerpo del anuncio en las páginas txt.php del BOE (sin menús ni pie)
        t = m.group(1)
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(t)


def texto_de_xml_anuncio(data: bytes) -> str:
    root = quitar_ns(ET.fromstring(data))
    nodo = root.find(".//texto")
    base = nodo if nodo is not None else root
    parrafos = ["".join(p.itertext()).strip() for p in base.iter("p")]
    if any(parrafos):
        return "\n".join(x for x in parrafos if x)
    return "\n".join(t.strip() for t in base.itertext() if t.strip())


# --------------------------------------------------------------------------- reglas de negocio
def seccion_relevante(codigo: str | None, nombre: str | None) -> bool:
    """Solo secciones IV (Administración de Justicia) y V (Anuncios)."""
    c = (codigo or "").strip().upper()
    n = norm(nombre)
    if c.startswith("4") or c.startswith("5"):
        return True
    return n.startswith("IV.") or n.startswith("V.") or n.startswith("IV ") or n.startswith("V ")


def es_subasta(*textos: str | None) -> bool:
    crudo = " ".join(t or "" for t in textos)
    return "SUBASTA" in norm(crudo) or bool(RE_SUB.search(crudo))


def motivo_sevilla(departamento: str | None, epigrafe: str | None, titulo: str | None,
                   municipios: set[str]) -> str | None:
    """Devuelve por qué un anuncio se asocia a Sevilla, o None. Queda guardado para auditarlo.

    - unidad_gestora_41: la unidad que gestiona es la de Sevilla (no implica que el bien esté allí).
    - menciona_sevilla: el emisor o el título menciona Sevilla.
    - emisor_municipio:<NOMBRE>: el departamento o epígrafe ES un municipio de la provincia (p. ej. un juzgado).
    - titulo_municipio:<NOMBRE>: el título nombra un municipio de la provincia (coincidencia de texto: revisar).
    """
    cab = norm(" ".join(x or "" for x in (departamento, epigrafe, titulo)))
    if re.search(r"ANDALUCIA\s*41\b", cab):
        return "unidad_gestora_41"
    if "SEVILLA" in cab:
        return "menciona_sevilla"
    t = norm(titulo)
    emisores = [norm(departamento), norm(epigrafe)] + ([t] if es_titulo_emisor(titulo) else [])
    for emisor in emisores:
        if emisor and emisor in municipios:
            return f"emisor_municipio:{emisor}"
    if es_titulo_emisor(titulo):
        return None  # título corto que no es exactamente un municipio de Sevilla (p. ej. «HERRERA DUQUE»)
    for m in sorted(municipios, key=len, reverse=True):
        patron = r"(?<!\bEL )(?<!\bLA )(?<!\bLOS )(?<!\bLAS )\b" + re.escape(m) + r"\b(?!\s+DE\s+[A-Z])"
        if len(m) >= 5 and re.search(patron, t):
            return f"titulo_municipio:{m}"
    return None


def es_titulo_emisor(titulo: str | None) -> bool:
    """En el BOE muchos títulos son solo el emisor: «SEVILLA», «CARMONA», «NOTARÍA DE …», «U.R. SUBASTAS …»."""
    t = norm(titulo)
    return 0 < len(t) <= 60 and not re.match(r"(ANUNCIO|RESOLUCION|EDICTO|CORRECCION|ORDEN)\b", t)


def datos_anuncio(texto: str) -> dict:
    """Lo que traen todos los anuncios de subasta del BOE: tipo, lugar y fecha de firma."""
    d = {}
    m = RE_TIPO_ANUNCIO.search(texto)
    if m:
        d["tipo_anuncio"] = m.group(1).strip(" ,;:")
    m = RE_FIRMA.search(texto)
    if m:
        d["lugar_firma"], d["fecha_firma"] = m.group(1).strip(), m.group(2).strip()
    return d


def extraer_rotulos(texto: str) -> dict:
    """Busca 'Rótulo: valor' línea a línea. Devuelve solo lo encontrado (texto literal)."""
    encontrados: dict[str, str] = {}
    for linea in texto.splitlines():
        if ":" not in linea:
            continue
        rotulo, _, valor = linea.partition(":")
        rotulo = norm(rotulo).strip(" .-")
        valor = re.sub(r"\s+", " ", valor).strip()
        if not valor:
            continue
        for campo, opciones in ROTULOS.items():
            if campo in encontrados:
                continue
            if any(rotulo == o or rotulo.endswith(" " + o) for o in opciones):
                if campo.startswith("fecha") and not RE_FECHA.search(valor):
                    break  # p. ej. «La subasta se iniciará en la fecha indicada…»: no es una fecha
                encontrados[campo] = valor[:300]
                break
    return encontrados


# --------------------------------------------------------------------------- base de datos
ESQUEMA = """
CREATE TABLE IF NOT EXISTS ejecuciones (
  id_ejecucion INTEGER PRIMARY KEY AUTOINCREMENT,
  inicio TEXT, fin TEXT, version TEXT, parametros TEXT,
  n_leidos INTEGER DEFAULT 0, n_nuevos INTEGER DEFAULT 0, n_cambios INTEGER DEFAULT 0,
  avisos TEXT, errores TEXT, estado TEXT
);
CREATE TABLE IF NOT EXISTS respuestas_brutas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, id_ejecucion INTEGER, url TEXT,
  momento_consulta TEXT, http_status INTEGER, sha256 TEXT, ruta TEXT, bytes INTEGER
);
CREATE TABLE IF NOT EXISTS dias_procesados (
  fecha TEXT PRIMARY KEY, resultado TEXT, n_items INTEGER, n_subastas INTEGER,
  n_sevilla INTEGER, id_ejecucion INTEGER, momento_consulta TEXT
);
CREATE TABLE IF NOT EXISTS anuncios_boe (
  id_boe TEXT PRIMARY KEY, fecha_publicacion TEXT, seccion_codigo TEXT, seccion_nombre TEXT,
  departamento TEXT, epigrafe TEXT, titulo TEXT, url_pdf TEXT, url_html TEXT, url_xml TEXT,
  motivo_sevilla TEXT, ids_subasta TEXT, texto_descargado INTEGER DEFAULT 0,
  fuente TEXT, url_fuente TEXT, momento_consulta TEXT, nivel_verificacion TEXT, id_ejecucion INTEGER
);
CREATE TABLE IF NOT EXISTS expedientes (
  id_subasta TEXT PRIMARY KEY, prefijo TEXT, autoridad_gestora TEXT, id_anuncio_boe TEXT,
  fecha_publicacion_boe TEXT, fecha_inicio TEXT, fecha_conclusion TEXT, estado TEXT,
  motivo_sevilla TEXT, nota TEXT, tipo_anuncio TEXT, lugar_firma TEXT, fecha_firma TEXT,
  fuente TEXT, url_fuente TEXT, momento_consulta TEXT, nivel_verificacion TEXT
);
CREATE TABLE IF NOT EXISTS lotes (
  id_subasta TEXT, n_lote TEXT, valor_subasta REAL, tasacion REAL, deposito REAL,
  puja_minima REAL, precio_cierre REAL,
  fuente TEXT, url_fuente TEXT, momento_consulta TEXT, nivel_verificacion TEXT,
  PRIMARY KEY (id_subasta, n_lote)
);
CREATE TABLE IF NOT EXISTS inmuebles (
  id_subasta TEXT, n_lote TEXT, tipo_bien TEXT, municipio TEXT, codigo_postal TEXT,
  direccion TEXT, referencia_catastral TEXT, superficie TEXT,
  fuente TEXT, url_fuente TEXT, momento_consulta TEXT, nivel_verificacion TEXT,
  PRIMARY KEY (id_subasta, n_lote)
);
CREATE TABLE IF NOT EXISTS justicia_filas (
  clave TEXT PRIMARY KEY, id_subasta TEXT, motivo_sevilla TEXT, datos_json TEXT,
  sha_fila TEXT, momento_consulta TEXT, id_ejecucion INTEGER
);
CREATE TABLE IF NOT EXISTS justicia_resumen (
  momento_consulta TEXT PRIMARY KEY, n_filas INTEGER, fecha_publicada_min TEXT, fecha_publicada_max TEXT,
  por_estado TEXT, por_tipo TEXT, por_procedimiento TEXT, sha256_csv TEXT, id_ejecucion INTEGER
);
CREATE TABLE IF NOT EXISTS cambios_historicos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tabla TEXT, clave TEXT, campo TEXT,
  valor_anterior TEXT, valor_nuevo TEXT, detectado_en TEXT, id_ejecucion INTEGER, fuente TEXT
);
CREATE TABLE IF NOT EXISTS municipios_sevilla (
  nombre TEXT PRIMARY KEY, fuente TEXT, momento_consulta TEXT
);
"""


def conectar(ruta: Path) -> sqlite3.Connection:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    migrar(con)
    return con


def migrar(con: sqlite3.Connection):
    """Añade a una base existente las columnas nuevas del esquema (nunca borra nada)."""
    tmp = sqlite3.connect(":memory:")
    tmp.executescript(ESQUEMA)
    for (tabla,) in tmp.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        esperadas = [(r[1], r[2]) for r in tmp.execute(f"PRAGMA table_info({tabla})")]
        actuales = {r[1] for r in con.execute(f"PRAGMA table_info({tabla})")}
        for col, tipo in esperadas:
            if col not in actuales:
                con.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
    tmp.close()
    con.commit()


def upsert(con, tabla: str, claves: dict, valores: dict, meta: dict, id_ej: int, fuente: str) -> str:
    """Inserta o actualiza. Cada cambio de valor queda en cambios_historicos.
    Un valor None nunca borra un dato existente (ausencia de dato != dato nuevo)."""
    where = " AND ".join(f"{k}=?" for k in claves)
    fila = con.execute(f"SELECT * FROM {tabla} WHERE {where}", tuple(claves.values())).fetchone()
    if fila is None:
        cols = {**claves, **{k: v for k, v in valores.items()}, **meta}
        con.execute(
            f"INSERT INTO {tabla} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(cols.values()),
        )
        return "nuevo"
    clave_txt = "|".join(str(v) for v in claves.values())
    cambios = {}
    for campo, nuevo in valores.items():
        if nuevo is None:
            continue
        viejo = fila[campo]
        if viejo is None or str(viejo) != str(nuevo):
            if viejo is not None:
                con.execute(
                    "INSERT INTO cambios_historicos (tabla, clave, campo, valor_anterior, valor_nuevo,"
                    " detectado_en, id_ejecucion, fuente) VALUES (?,?,?,?,?,?,?,?)",
                    (tabla, clave_txt, campo, str(viejo), str(nuevo), ahora(), id_ej, fuente),
                )
            cambios[campo] = nuevo
    if not cambios:
        return "igual"
    sets = {**cambios, **meta}
    con.execute(
        f"UPDATE {tabla} SET {','.join(f'{k}=?' for k in sets)} WHERE {where}",
        tuple(sets.values()) + tuple(claves.values()),
    )
    hubo_cambio_real = any(fila[c] is not None for c in cambios)
    return "cambio" if hubo_cambio_real else "completado"


# --------------------------------------------------------------------------- red
class Http:
    """Descarga respetando robots.txt, con pausa entre peticiones y copia en bruto auditada."""

    def __init__(self, con, id_ej: int, dir_brutos: Path, avisos: list[str]):
        self.con, self.id_ej, self.dir_brutos, self.avisos = con, id_ej, dir_brutos, avisos
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def permitido(self, url: str) -> bool:
        p = urllib.parse.urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(host + "/robots.txt")
            try:
                req = urllib.request.Request(host + "/robots.txt", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    rp.parse(r.read().decode("utf-8", errors="replace").splitlines())
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    rp.disallow_all = True
                else:
                    rp.allow_all = True
            except Exception:  # robots.txt inaccesible
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        if rp is None:
            return any(seg in p.path for seg in ENDPOINTS_DATOS_ABIERTOS)
        return rp.can_fetch(UA, url)

    def get(self, url: str, accept: str = "*/*", nombre: str | None = None) -> tuple[int, bytes | None]:
        if not self.permitido(url):
            p = urllib.parse.urlparse(url)
            aviso = f"robots.txt no permite {p.netloc}{p.path}: esas URLs no se descargan"
            if aviso not in self.avisos:
                self.avisos.append(aviso)
            return -1, None
        status, data = self._descargar(url, accept)
        momento = ahora()
        ruta_txt = None
        huella = sha256(data) if data else None
        if data is not None and status == 200:
            previa = self.con.execute(
                "SELECT ruta FROM respuestas_brutas WHERE url=? AND sha256=? AND ruta IS NOT NULL LIMIT 1",
                (url, huella)).fetchone()
            if previa:  # mismo contenido ya guardado: no se duplica el archivo
                ruta_txt = previa["ruta"]
            else:
                hoy = dt.date.today()
                carpeta = self.dir_brutos / f"{hoy:%Y}" / f"{hoy:%m}" / f"{hoy:%d}"
                carpeta.mkdir(parents=True, exist_ok=True)
                nombre = nombre or re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("//", 1)[-1])[:150]
                ruta = carpeta / nombre
                if len(data) > 1_000_000:  # archivos grandes (CSV de Justicia) comprimidos
                    ruta = ruta.with_name(ruta.name + ".gz")
                    ruta.write_bytes(gzip.compress(data))
                else:
                    ruta.write_bytes(data)
                ruta_txt = str(ruta.relative_to(self.dir_brutos.parent))
        self.con.execute(
            "INSERT INTO respuestas_brutas (id_ejecucion, url, momento_consulta, http_status, sha256,"
            " ruta, bytes) VALUES (?,?,?,?,?,?,?)",
            (self.id_ej, url, momento, status, huella, ruta_txt, len(data) if data else 0),
        )
        time.sleep(PAUSA)
        return status, data

    def _descargar(self, url: str, accept: str) -> tuple[int, bytes | None]:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:  # red caída, DNS, timeout...
            self.avisos.append(f"error de red en {url}: {e.__class__.__name__}: {e}")
            return 0, None


# --------------------------------------------------------------------------- BOE
def parse_sumario_xml(data: bytes) -> tuple[str | None, list[dict]]:
    """Estructura documentada por la AEBOE (APIsumarioBOE.pdf):
    response/data/sumario/diario/seccion/departamento/epigrafe/item
    (identificador, titulo, url_pdf, url_html, url_xml)."""
    root = quitar_ns(ET.fromstring(data))
    codigo = root.findtext("./status/code")
    if codigo and codigo.strip() != "200":
        return codigo.strip(), []
    items = []

    def mk(sec, dep, epi, it):
        return {
            "id_boe": (it.findtext("identificador") or "").strip(),
            "titulo": (it.findtext("titulo") or "").strip(),
            "url_pdf": (it.findtext("url_pdf") or "").strip(),
            "url_html": (it.findtext("url_html") or "").strip(),
            "url_xml": (it.findtext("url_xml") or "").strip(),
            "seccion_codigo": sec.get("codigo"),
            "seccion_nombre": sec.get("nombre"),
            "departamento": dep.get("nombre") if dep is not None else None,
            "epigrafe": epi.get("nombre") if epi is not None else None,
        }

    for sec in root.iter("seccion"):
        for dep in sec.findall("departamento"):
            for hijo in dep:
                if hijo.tag == "epigrafe":
                    for it in hijo.iter("item"):
                        items.append(mk(sec, dep, hijo, it))
                elif hijo.tag == "item":
                    items.append(mk(sec, dep, None, hijo))
    return "200", [i for i in items if i["id_boe"]]


def cargar_municipios(con, http: Http | None, id_ej: int, avisos: list[str]) -> set[str]:
    existentes = {r["nombre"] for r in con.execute("SELECT nombre FROM municipios_sevilla")}
    if existentes or http is None:
        return existentes
    status, data = http.get(CATASTRO_MUNICIPIOS, accept="application/xml", nombre="catastro_municipios_sevilla.xml")
    nombres = parse_municipios_catastro(data) if status == 200 and data else set()
    if not nombres:
        avisos.append("No se pudo cargar la lista oficial de municipios de Sevilla (Catastro): "
                      "solo se detectan anuncios que mencionen 'Sevilla' o la unidad 'ANDALUCIA 41'.")
        return set()
    m = ahora()
    con.executemany("INSERT OR IGNORE INTO municipios_sevilla VALUES (?,?,?)",
                    [(n, "Catastro OVC ConsultaMunicipio", m) for n in sorted(nombres)])
    return nombres


def parse_municipios_catastro(data: bytes | None) -> set[str]:
    if not data:
        return set()
    try:
        root = quitar_ns(ET.fromstring(data))
    except ET.ParseError:
        return set()
    nombres = set()
    for muni in root.iter("muni"):
        nm = norm(muni.findtext("nm"))
        if not nm:
            continue
        nombres.add(nm)
        m = re.match(r"^(.*)\s*\((EL|LA|LOS|LAS)\)$", nm)  # "PALACIOS Y VILLAFRANCA (LOS)"
        if m:
            nombres.add(f"{m.group(2)} {m.group(1).strip()}")
            nombres.add(m.group(1).strip())
    return nombres


def procesar_dia(con, http: Http, fecha: dt.date, id_ej: int, municipios: set[str], stats: dict, avisos: list[str]):
    f = fecha.strftime("%Y%m%d")
    status, data = http.get(API_SUMARIO.format(fecha=f), accept="application/xml", nombre=f"sumario_BOE_{f}.xml")
    momento = ahora()
    reciente = fecha >= dt.date.today() - dt.timedelta(days=1)

    def sin_publicacion():
        # Un día reciente sin sumario puede ser que aún no se haya publicado: no se da por cerrado.
        if not reciente:
            con.execute("INSERT OR REPLACE INTO dias_procesados VALUES (?,?,?,?,?,?,?)",
                        (fecha.isoformat(), "sin_publicacion", 0, 0, 0, id_ej, momento))

    if status == 404:
        sin_publicacion()
        return
    if status != 200 or not data:
        avisos.append(f"Sumario {fecha}: no descargado (HTTP {status}); se reintentará en la próxima ejecución")
        stats["dias_fallidos"] = stats.get("dias_fallidos", 0) + 1
        return
    try:
        codigo, items = parse_sumario_xml(data)
    except ET.ParseError as e:
        avisos.append(f"Sumario {fecha}: XML no válido ({e}); se reintentará")
        return
    if codigo == "404":
        sin_publicacion()
        return
    if codigo != "200":
        avisos.append(f"Sumario {fecha}: la API respondió con código {codigo}; se reintentará")
        return
    stats["leidos"] += len(items)
    n_sub = n_sev = 0
    for it in items:
        sec = (it["seccion_codigo"] or "").upper()
        if not seccion_relevante(sec, it["seccion_nombre"]):
            continue
        subasta_en_cabecera = es_subasta(it["departamento"], it["epigrafe"], it["titulo"])
        motivo = motivo_sevilla(it["departamento"], it["epigrafe"], it["titulo"], municipios)
        # En el BOE real muchos títulos son solo el emisor («SEVILLA», «NOTARÍA DE …», «U.R. SUBASTAS …»).
        # Para esos casos hay que abrir el texto para saber si es una subasta.
        titulo_emisor = es_titulo_emisor(it["titulo"])
        revisar_texto = (not subasta_en_cabecera) and titulo_emisor and (
            (sec.startswith("4") and motivo) or sec == "5C" or (sec.startswith("5") and motivo))
        if not subasta_en_cabecera and not revisar_texto:
            continue
        ids = sorted(set(RE_SUB.findall(it["titulo"])))
        rotulos: dict = {}
        descargado = 0
        texto = None
        if motivo or revisar_texto:
            texto = descargar_texto(http, it)
            if texto:
                descargado = 1
            elif subasta_en_cabecera:
                avisos.append(f"{it['id_boe']}: texto del anuncio no disponible; solo datos del sumario")
        if not subasta_en_cabecera and not (texto and es_subasta(texto)):
            continue  # el texto no trata de una subasta
        datos = datos_anuncio(texto) if texto else {}
        if not motivo and datos.get("lugar_firma"):
            lugar = norm(datos["lugar_firma"])
            if lugar == "SEVILLA" or lugar in municipios:
                motivo = f"lugar_firma:{lugar}"
        if not motivo and texto and re.search(r"\bSEVILLA\b", norm(texto)):
            motivo = "texto_menciona_sevilla"
        n_sub += 1
        if texto and motivo:
            ids = sorted(set(ids) | set(RE_SUB.findall(texto)))
            rotulos = extraer_rotulos(texto)
        if motivo:
            n_sev += 1
        url_fuente = it["url_html"] or it["url_pdf"]
        meta = {"fuente": "BOE API sumario", "url_fuente": url_fuente, "momento_consulta": momento,
                "nivel_verificacion": "A1", "id_ejecucion": id_ej}
        r = upsert(con, "anuncios_boe", {"id_boe": it["id_boe"]}, {
            "fecha_publicacion": fecha.isoformat(), "seccion_codigo": it["seccion_codigo"],
            "seccion_nombre": it["seccion_nombre"], "departamento": it["departamento"],
            "epigrafe": it["epigrafe"], "titulo": it["titulo"], "url_pdf": it["url_pdf"],
            "url_html": it["url_html"], "url_xml": it["url_xml"], "motivo_sevilla": motivo,
            "ids_subasta": ",".join(ids) or None, "texto_descargado": descargado}, meta, id_ej, "BOE")
        contar(stats, r)
        if not motivo:
            continue
        for id_sub in ids:
            nota = ("Sevilla = sede de la unidad gestora; la ubicación del bien no consta en el anuncio"
                    if motivo == "unidad_gestora_41" else None)
            meta_e = {"fuente": "BOE anuncio" if descargado else "BOE API sumario", "url_fuente": url_fuente,
                      "momento_consulta": momento, "nivel_verificacion": "A1"}
            r = upsert(con, "expedientes", {"id_subasta": id_sub}, {
                "prefijo": id_sub.split("-")[1], "autoridad_gestora": autoridad(it),
                "id_anuncio_boe": it["id_boe"], "fecha_publicacion_boe": fecha.isoformat(),
                "fecha_inicio": rotulos.get("fecha_inicio"), "fecha_conclusion": rotulos.get("fecha_conclusion"),
                "estado": "anunciada_en_BOE", "motivo_sevilla": motivo, "nota": nota,
                "tipo_anuncio": datos.get("tipo_anuncio"), "lugar_firma": datos.get("lugar_firma"),
                "fecha_firma": datos.get("fecha_firma")}, meta_e, id_ej, "BOE")
            contar(stats, r)
            if len(ids) == 1 and rotulos:
                lote = {k: importe(rotulos.get(k)) for k in CAMPOS_IMPORTE}
                if any(v is not None for v in lote.values()):
                    contar(stats, upsert(con, "lotes", {"id_subasta": id_sub, "n_lote": "unico_o_no_especificado"},
                                         lote, meta_e, id_ej, "BOE"))
                inm = {k: rotulos.get(k) for k in ("tipo_bien", "municipio", "codigo_postal", "direccion",
                                                     "referencia_catastral")}
                if any(inm.values()):
                    contar(stats, upsert(con, "inmuebles", {"id_subasta": id_sub, "n_lote": "unico_o_no_especificado"},
                                         inm, meta_e, id_ej, "BOE"))
    con.execute("INSERT OR REPLACE INTO dias_procesados VALUES (?,?,?,?,?,?,?)",
                (fecha.isoformat(), "procesado", len(items), n_sub, n_sev, id_ej, momento))


def descargar_texto(http: Http, it: dict) -> str | None:
    for url, tipo in ((it["url_html"], "html"), (it["url_xml"], "xml")):
        if not url:
            continue
        st, cuerpo = http.get(url, nombre=f"{it['id_boe']}.{tipo}")
        if st == 200 and cuerpo:
            if tipo == "xml":
                try:
                    return texto_de_xml_anuncio(cuerpo)
                except ET.ParseError:
                    pass
            return texto_de_html(cuerpo)
    return None


def autoridad(it: dict) -> str | None:
    """Emisor tal cual aparece en el BOE. Si el título es solo el emisor (p. ej. «U.R. SUBASTAS ANDALUCÍA 41»),
    se añade al departamento."""
    if it["epigrafe"]:
        return f"{it['departamento']} · {it['epigrafe']}" if it["departamento"] else it["epigrafe"]
    if it["titulo"] and len(it["titulo"]) <= 80:
        return f"{it['departamento']} · {it['titulo']}" if it["departamento"] else it["titulo"]
    return it["departamento"]


def contar(stats: dict, resultado: str):
    if resultado == "nuevo":
        stats["nuevos"] += 1
    elif resultado == "cambio":
        stats["cambios"] += 1


# --------------------------------------------------------------------------- Justicia
def decodificar(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse_csv(data: bytes) -> tuple[list[str], list[dict]]:
    txt = decodificar(data)
    muestra = txt[:50000]
    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=";,\t|")
    except csv.Error:
        class dialecto(csv.excel):  # noqa: N801
            delimiter = ";"
    lector = csv.DictReader(io.StringIO(txt), dialect=dialecto)
    filas = [dict(f) for f in lector]
    return list(lector.fieldnames or []), filas


def procesar_justicia(con, http: Http, id_ej: int, stats: dict, avisos: list[str], dir_datos: Path):
    status, data = http.get(CSV_JUSTICIA, accept="text/csv", nombre="Subastas_Judiciales.csv")
    momento = ahora()
    if status != 200 or not data:
        avisos.append(f"CSV de Justicia no descargado (HTTP {status}); se reintentará")
        return
    cabeceras, filas = parse_csv(data)
    cols_prov = [c for c in cabeceras if "PROVINCIA" in norm(c)]
    col_lote = next((c for c in cabeceras if "LOTE" in norm(c)), None)
    (dir_datos / "mapeo_justicia.md").write_text(
        "# Columnas del CSV de Justicia (detectadas automáticamente)\n\n"
        f"Consulta: {momento}\n\nFilas totales: {len(filas)}\n\n"
        + "\n".join(f"- `{c}`" + (" ← provincia" if c in cols_prov else "") + (" ← lote" if c == col_lote else "")
                    for c in cabeceras) + "\n", encoding="utf-8")
    hay_ids = any(RE_SUB.search(" ".join(str(v) for v in f.values() if v)) for f in filas[:5000])
    if not cols_prov and not hay_ids:
        # El CSV publicado no trae ni provincia ni identificador: solo sirve como estadística nacional.
        def fecha(x):
            try:
                return dt.datetime.strptime((x or "").strip(), "%d/%m/%Y").date()
            except ValueError:
                return None
        col_fecha = next((c for c in cabeceras if "PUBLICAD" in norm(c)), None)
        fechas = [d for d in (fecha(f.get(col_fecha)) for f in filas) if d] if col_fecha else []

        def reparto(col_txt):
            col = next((c for c in cabeceras if norm(c) == col_txt), None)
            if not col:
                return {}
            cuenta: dict[str, int] = {}
            for f in filas:
                cuenta[f.get(col) or "(vacío)"] = cuenta.get(f.get(col) or "(vacío)", 0) + 1
            return dict(sorted(cuenta.items(), key=lambda kv: -kv[1]))
        resumen = {"n_filas": len(filas),
                   "fecha_publicada_min": min(fechas).isoformat() if fechas else None,
                   "fecha_publicada_max": max(fechas).isoformat() if fechas else None,
                   "por_estado": reparto("ESTADO"), "por_tipo": reparto("TIPOSUBASTA"),
                   "por_procedimiento": reparto("PROCEDIMIENTO")}
        con.execute("INSERT OR REPLACE INTO justicia_resumen VALUES (?,?,?,?,?,?,?,?,?)",
                    (momento, resumen["n_filas"], resumen["fecha_publicada_min"], resumen["fecha_publicada_max"],
                     json.dumps(resumen["por_estado"], ensure_ascii=False),
                     json.dumps(resumen["por_tipo"], ensure_ascii=False),
                     json.dumps(resumen["por_procedimiento"], ensure_ascii=False), sha256(data), id_ej))
        stats["justicia_total"] = len(filas)
        stats["justicia_resumen"] = resumen
        return
    if not cols_prov:
        avisos.append("CSV de Justicia: no hay columna de provincia; se filtra Sevilla por texto en toda la fila")
    contador: dict[str, int] = {}
    n_sev = 0
    for fila in filas:
        valores = " ".join(str(v) for v in fila.values() if v)
        if cols_prov:
            prov = norm(" ".join(fila.get(c) or "" for c in cols_prov))
            motivo = "provincia" if ("SEVILLA" in prov or re.search(r"\b41\b", prov)) else None
        else:
            motivo = "texto" if "SEVILLA" in norm(valores) else None
        if not motivo:
            continue
        n_sev += 1
        ids = RE_SUB.findall(valores)
        id_sub = ids[0] if ids else None
        base = id_sub or "sin_id"
        if col_lote and fila.get(col_lote):
            clave = f"{base}#lote={fila[col_lote]}"
        else:
            contador[base] = contador.get(base, 0) + 1
            clave = f"{base}#{contador[base]}"
        datos = json.dumps(fila, ensure_ascii=False, sort_keys=True)
        fila_db = con.execute("SELECT datos_json, sha_fila FROM justicia_filas WHERE clave=?", (clave,)).fetchone()
        sha_f = sha256(datos.encode())
        if fila_db is None:
            con.execute("INSERT INTO justicia_filas VALUES (?,?,?,?,?,?,?)",
                        (clave, id_sub, motivo, datos, sha_f, momento, id_ej))
            stats["nuevos"] += 1
        elif fila_db["sha_fila"] != sha_f:
            viejo = json.loads(fila_db["datos_json"])
            for k in sorted(set(viejo) | set(fila)):
                if str(viejo.get(k)) != str(fila.get(k)):
                    con.execute(
                        "INSERT INTO cambios_historicos (tabla, clave, campo, valor_anterior, valor_nuevo,"
                        " detectado_en, id_ejecucion, fuente) VALUES (?,?,?,?,?,?,?,?)",
                        ("justicia_filas", clave, k, str(viejo.get(k)), str(fila.get(k)), momento, id_ej,
                         "Justicia CSV"))
            con.execute("UPDATE justicia_filas SET datos_json=?, sha_fila=?, momento_consulta=?, id_ejecucion=?"
                        " WHERE clave=?", (datos, sha_f, momento, id_ej, clave))
            stats["cambios"] += 1
        if id_sub and con.execute("SELECT 1 FROM expedientes WHERE id_subasta=?", (id_sub,)).fetchone() is None:
            contar(stats, upsert(con, "expedientes", {"id_subasta": id_sub}, {
                "prefijo": id_sub.split("-")[1], "estado": "en_CSV_Justicia", "motivo_sevilla": f"justicia_{motivo}"},
                {"fuente": "Justicia CSV", "url_fuente": CSV_JUSTICIA, "momento_consulta": momento,
                 "nivel_verificacion": "A2"}, id_ej, "Justicia CSV"))
    stats["justicia_total"] = len(filas)
    stats["justicia_sevilla"] = n_sev


def toca_justicia(con, hoy: dt.date, forzar: bool) -> bool:
    if forzar or hoy.weekday() == 0:
        return True
    ult = con.execute("SELECT MAX(momento_consulta) m FROM respuestas_brutas WHERE url=? AND http_status=200",
                      (CSV_JUSTICIA,)).fetchone()["m"]
    if not ult:
        return True
    return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ult)).days >= 7


# --------------------------------------------------------------------------- informe
def cobertura(con, base: Path) -> str:
    ruta = base / "control" / "control_lotes.csv"
    if not ruta.exists():
        return "Sin conjunto de control (añade `control/control_lotes.csv` con los identificadores SUB-… de referencia)."
    ids = set(RE_SUB.findall(ruta.read_text(encoding="utf-8", errors="replace")))
    if not ids:
        return "El conjunto de control no contiene identificadores SUB-…"
    en_bd = {r["id_subasta"] for r in con.execute("SELECT id_subasta FROM expedientes")}
    hall = ids & en_bd
    falt = sorted(ids - en_bd)
    txt = f"{len(hall)} de {len(ids)} identificadores de control encontrados ({100 * len(hall) / len(ids):.0f} %)."
    if falt:
        txt += " No encontrados: " + ", ".join(falt[:40]) + (" …" if len(falt) > 40 else "")
    return txt


def informe(con, id_ej: int, stats: dict, avisos: list[str], base: Path, dir_datos: Path) -> Path:
    hoy = dt.date.today().isoformat()
    q = lambda s, *a: con.execute(s, a).fetchall()  # noqa: E731
    por_prefijo = q("SELECT prefijo, COUNT(*) n FROM expedientes GROUP BY prefijo ORDER BY n DESC")
    por_tipo = q("SELECT COALESCE(tipo_anuncio,'(sin dato)') tipo, COUNT(*) n FROM expedientes GROUP BY 1 ORDER BY n DESC")
    por_autoridad = q("SELECT autoridad_gestora, COUNT(*) n FROM expedientes WHERE autoridad_gestora IS NOT NULL "
                      "GROUP BY autoridad_gestora ORDER BY n DESC LIMIT 15")
    nuevos = q("SELECT e.id_subasta, e.autoridad_gestora, e.fecha_publicacion_boe, e.url_fuente, e.nota, e.tipo_anuncio "
               "FROM expedientes e WHERE e.momento_consulta >= (SELECT inicio FROM ejecuciones WHERE id_ejecucion=?) "
               "ORDER BY e.fecha_publicacion_boe DESC", id_ej)
    cambios = q("SELECT tabla, clave, campo, valor_anterior, valor_nuevo FROM cambios_historicos WHERE id_ejecucion=?",
                id_ej)
    dias = q("SELECT fecha, resultado, n_items, n_subastas, n_sevilla FROM dias_procesados WHERE id_ejecucion=? "
             "ORDER BY fecha", id_ej)
    sin_texto = q("SELECT COUNT(*) n FROM anuncios_boe WHERE motivo_sevilla IS NOT NULL AND texto_descargado=0")[0]["n"]
    total_sev = q("SELECT COUNT(*) n FROM anuncios_boe WHERE motivo_sevilla IS NOT NULL")[0]["n"]
    total_esp = q("SELECT COUNT(*) n FROM anuncios_boe")[0]["n"]

    L = [f"# SeMá · Informe {hoy}", "",
         "Fuente de los datos: [Agencia Estatal Boletín Oficial del Estado](https://www.boe.es) y "
         "[Ministerio de Justicia (datos.gob.es)](https://datos.gob.es). "
         "Niveles: A1 = BOE oficial · A2 = CSV oficial de Justicia · B = tercero · C = manual.", "",
         "## Resumen de esta ejecución",
         f"- Elementos del sumario leídos: {stats['leidos']}",
         f"- Registros nuevos (todas las tablas): {stats['nuevos']} · Cambios detectados: {stats['cambios']}",
         f"- Anuncios de subasta acumulados: {total_esp} en España, {total_sev} asociados a Sevilla"
         f" ({sin_texto} de ellos sin texto descargado)", ""]
    if "justicia_resumen" in stats:
        r = stats["justicia_resumen"]
        top = lambda d: ", ".join(f"{k}: {v}" for k, v in list(d.items())[:6])  # noqa: E731
        L += ["## Subastas judiciales en toda España (CSV de Justicia, nivel A2)",
              f"- {r['n_filas']} subastas publicadas entre {r['fecha_publicada_min']} y {r['fecha_publicada_max']}.",
              f"- Por estado: {top(r['por_estado'])}",
              f"- Por tipo: {top(r['por_tipo'])}",
              "- El CSV no trae provincia ni identificador de subasta, así que **no permite saber cuáles son de "
              "Sevilla**. Se usa solo como referencia nacional.", ""]
    elif "justicia_total" in stats:
        L += [f"- CSV de Justicia: {stats['justicia_total']} filas, {stats.get('justicia_sevilla', 0)} de Sevilla", ""]
    if dias:
        L += ["## Días procesados", "| Fecha | Resultado | Elementos | Subastas (España) | Sevilla |", "|---|---|---|---|---|"]
        L += [f"| {d['fecha']} | {d['resultado']} | {d['n_items']} | {d['n_subastas']} | {d['n_sevilla']} |" for d in dias]
        L.append("")
    L += ["## Expedientes de Sevilla en la base (acumulado)", "| Prefijo del identificador | Expedientes |", "|---|---|"]
    L += [f"| {r['prefijo']} | {r['n']} |" for r in por_prefijo] or ["| — | 0 |"]
    L.append("")
    if por_tipo:
        L += ["| Tipo de subasta (literal del anuncio) | Expedientes |", "|---|---|"]
        L += [f"| {r['tipo']} | {r['n']} |" for r in por_tipo]
        L.append("")
    if por_autoridad:
        L += ["| Autoridad gestora (literal del BOE) | Expedientes |", "|---|---|"]
        L += [f"| {r['autoridad_gestora']} | {r['n']} |" for r in por_autoridad]
        L.append("")
    sin_id = q("SELECT id_boe, fecha_publicacion, epigrafe, departamento, titulo, url_fuente, motivo_sevilla "
               "FROM anuncios_boe WHERE id_ejecucion=? AND motivo_sevilla IS NOT NULL AND ids_subasta IS NULL "
               "ORDER BY fecha_publicacion DESC", id_ej)
    L += [f"## Nuevos o actualizados en esta ejecución ({len(nuevos)})"]
    L += [f"- {r['id_subasta']}" + (f" · {r['tipo_anuncio']}" if r["tipo_anuncio"] else "")
          + (f" · {r['autoridad_gestora']}" if r["autoridad_gestora"] else "")
          + (f" · BOE {r['fecha_publicacion_boe']}" if r["fecha_publicacion_boe"] else "")
          + f" · [fuente]({r['url_fuente']})" + (f" · _{r['nota']}_" if r["nota"] else "") for r in nuevos[:100]]
    if len(nuevos) > 100:
        L.append(f"- … y {len(nuevos) - 100} más (ver `datos/export/expedientes.csv`)")
    if sin_id:
        L += ["", f"## Otros anuncios de subasta de Sevilla sin identificador del Portal ({len(sin_id)})",
              "_Por ejemplo, enajenaciones de patrimonio público. Motivo de asociación entre paréntesis._"]
        L += [f"- {r['id_boe']} · {r['fecha_publicacion']} · {r['epigrafe'] or r['departamento'] or ''} · "
              f"{(r['titulo'] or '')[:140]} ({r['motivo_sevilla']}) · [anuncio]({r['url_fuente']})" for r in sin_id[:60]]
    L += ["", f"## Cambios detectados ({len(cambios)})"]
    L += [f"- {c['tabla']} · {c['clave']} · {c['campo']}: «{c['valor_anterior']}» → «{c['valor_nuevo']}»"
          for c in cambios[:100]] or ["- Ninguno"]
    L += ["", "## Cobertura frente al conjunto de control", cobertura(con, base), "",
          "## Avisos"]
    L += [f"- {a}" for a in avisos] or ["- Ninguno"]
    L += ["", "## Límites conocidos",
          "- El precio de cierre y las pujas solo están en el Portal de Subastas, que este sistema no consulta "
          "sin autorización. `precio_cierre` queda vacío hasta tener una fuente autorizada.",
          "- En los anuncios de la AEAT, «ANDALUCIA 41» es la unidad gestora de Sevilla: no garantiza que el bien "
          "esté en la provincia. Esos anuncios solo traen el identificador y el enlace al Portal.",
          "- Las subastas judiciales de Sevilla solo se recogen si el BOE publica un edicto con texto. El CSV de "
          "Justicia no trae provincia, así que no hay fuente oficial abierta con su ubicación.", ""]
    dir_inf = dir_datos / "informes"
    dir_inf.mkdir(parents=True, exist_ok=True)
    ruta = dir_inf / f"informe_{hoy}.md"
    ruta.write_text("\n".join(L), encoding="utf-8")
    (dir_datos / "ultimo_informe.md").write_text("\n".join(L), encoding="utf-8")
    return ruta


def exportar_csv(con, dir_datos: Path):
    dest = dir_datos / "export"
    dest.mkdir(parents=True, exist_ok=True)
    for tabla in ("expedientes", "lotes", "inmuebles", "anuncios_boe", "cambios_historicos", "ejecuciones",
                  "dias_procesados", "justicia_filas", "justicia_resumen"):
        filas = con.execute(f"SELECT * FROM {tabla}").fetchall()
        with open(dest / f"{tabla}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            cols = [d[0] for d in con.execute(f"SELECT * FROM {tabla} LIMIT 0").description]
            w.writerow(cols)
            w.writerows([tuple(r) for r in filas])


# --------------------------------------------------------------------------- ejecución
def ejecutar(desde: dt.date | None, hasta: dt.date | None, forzar_justicia: bool,
             dir_datos: Path, http_factory=Http, base: Path = BASE) -> dict:
    con = conectar(dir_datos / "sema.sqlite")
    avisos: list[str] = []
    errores: list[str] = []
    stats = {"leidos": 0, "nuevos": 0, "cambios": 0}
    cur = con.execute("INSERT INTO ejecuciones (inicio, version, parametros, estado) VALUES (?,?,?,?)",
                      (ahora(), VERSION, json.dumps({"desde": str(desde), "hasta": str(hasta),
                                                    "forzar_justicia": forzar_justicia}), "en_curso"))
    id_ej = cur.lastrowid
    con.commit()
    http = http_factory(con, id_ej, dir_datos / "brutos", avisos)
    hoy = dt.date.today()
    hasta = hasta or hoy
    if desde is not None:  # rango pedido a mano: se procesa entero
        dias = [desde + dt.timedelta(days=i) for i in range((hasta - desde).days + 1)]
    else:  # automático: días pendientes de la ventana + siempre ayer y hoy (idempotente)
        hechos = {r["fecha"] for r in con.execute("SELECT fecha FROM dias_procesados")}
        inicio = hasta - dt.timedelta(days=DIAS_PRIMERA_CARGA)
        dias = [inicio + dt.timedelta(days=i) for i in range((hasta - inicio).days + 1)]
        dias = [d for d in dias if d.isoformat() not in hechos or d >= hasta - dt.timedelta(days=1)]
    try:
        municipios = cargar_municipios(con, http, id_ej, avisos)
        for dia in dias:
            try:
                procesar_dia(con, http, dia, id_ej, municipios, stats, avisos)
                con.commit()
            except Exception as e:  # un día con error no para el resto
                con.rollback()
                errores.append(f"{dia}: {e.__class__.__name__}: {e}")
        if toca_justicia(con, hoy, forzar_justicia):
            try:
                procesar_justicia(con, http, id_ej, stats, avisos, dir_datos)
                con.commit()
            except Exception as e:
                con.rollback()
                errores.append(f"Justicia: {e.__class__.__name__}: {e}")
        # Fuente B (subastasboeapi.com): se activa cuando exista token y documentación de su API.
        if os.environ.get("SUBASTASBOEAPI_TOKEN"):
            avisos.append("Hay token de subastasboeapi, pero la integración no está activada: falta su "
                          "documentación oficial para no suponer endpoints.")
        if dias and stats.get("dias_fallidos", 0) == len(dias):
            estado = "sin_conexion"  # no se pudo descargar ningún sumario: la ejecución se marca como fallida
        else:
            estado = "ok" if not errores else "con_errores"
    except Exception as e:
        errores.append(f"general: {e.__class__.__name__}: {e}")
        estado = "fallida"
    con.execute("UPDATE ejecuciones SET fin=?, n_leidos=?, n_nuevos=?, n_cambios=?, avisos=?, errores=?, estado=?"
                " WHERE id_ejecucion=?", (ahora(), stats["leidos"], stats["nuevos"], stats["cambios"],
                                          json.dumps(avisos, ensure_ascii=False), json.dumps(errores, ensure_ascii=False),
                                          estado, id_ej))
    con.commit()
    ruta_inf = informe(con, id_ej, stats, avisos + [f"ERROR {e}" for e in errores], base, dir_datos)
    exportar_csv(con, dir_datos)
    con.close()
    return {"id_ejecucion": id_ej, "estado": estado, "informe": str(ruta_inf), **stats,
            "avisos": avisos, "errores": errores}


def main(argv=None):
    p = argparse.ArgumentParser(description="SeMá · Subastas BOE Analytics (Sevilla)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Procesa los días pendientes y genera el informe")
    r.add_argument("--desde", type=dt.date.fromisoformat)
    r.add_argument("--hasta", type=dt.date.fromisoformat)
    r.add_argument("--forzar-justicia", action="store_true")
    r.add_argument("--datos", type=Path, default=Path(os.environ.get("SEMA_DATOS", BASE / "datos")))
    a = p.parse_args(argv)
    res = ejecutar(a.desde, a.hasta, a.forzar_justicia, a.datos)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["estado"] in ("ok", "con_errores") else 1


if __name__ == "__main__":
    sys.exit(main())

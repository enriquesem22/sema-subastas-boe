"""Ficha completa de cada subasta del Portal de Subastas del BOE (método nuevo, paso 2).

Basado en descargar_subasta_boe.py (carpeta de Drive «App_Info BOE subasta», 25-11-2026), con la misma forma de
recorrer y comprobar la ficha. Para cada código SUB:
1. Abre https://subastas.boe.es/ds.php?id=SUB (redirige a la ficha) y lee la pestaña «Información general».
2. Sigue los enlaces de la propia ficha: pestañas (2 Autoridad gestora, 3 Bienes, 5 Pujas), la página de cada lote
   (idLote) y los documentos (verDocumento.php, verCertificadoCierre.php). Solo URLs del mismo SUB.
3. De la fila «Anuncio BOE» saca los BOE-B y descarga el anuncio oficial: su XML (www.boe.es/diario_boe/xml.php) y su
   PDF (www.boe.es/boe/dias/…), comprobando que el anuncio cita el SUB.
Cada página se comprueba antes de guardarla: el h2 debe llevar el SUB pedido y la pestaña sus encabezados («Datos de
la subasta», «Autoridad gestora», «Bien», «Pujas»). Un PDF debe empezar por %PDF- y terminar en %%EOF. Lo que falla
queda en «pendientes» con el motivo; si el sitio responde 401, 403 o 429 se detienen todas sus peticiones.

Cambios respecto al script original (para usarlo en lote dentro del proyecto):
- Los códigos salen de la última captura de portal_codigos.py (o se pasan a mano). Un mismo cliente HTTP para todos,
  con 1 s entre peticiones; si el Portal corta el acceso, se para el lote entero.
- Reanuda: un SUB que ya tiene ficha de la misma captura de códigos no se vuelve a descargar (salvo --forzar).
- Las pestañas se guardan comprimidas (.html.gz) y solo si han cambiado: si una pestaña es idéntica (mismo SHA-256) a
  la de la ficha anterior del mismo SUB, ficha.json apunta a esa copia (igual_que_anterior). Los adjuntos van a datos/portal/documentos/<sha256>.<ext> y los
  anuncios a datos/portal/anuncios/, una sola vez: si una ficha anterior del mismo SUB ya tenía ese documento
  (misma ruta, idDoc y lote) o el anuncio ya está guardado, no se vuelve a pedir.
- ficha.json no repite el texto completo de cada pestaña (está en el HTML guardado): guarda encabezados y tablas.
- La URL del PDF del anuncio se acepta también relativa (/boe/dias/…).

Resultado de cada SUB en datos/portal/fichas/<SUB>/<AAAAMMDD_HHMMSS>/: ficha.json, pendientes.json y portal/ (las
pestañas y fuentes.json). Resumen de cada lote en datos/portal/fichas/_ejecuciones/<AAAAMMDD_HHMMSS>.json.

Uso: python portal_fichas.py [SUB ...] [--provincia 41] [--forzar] [--max N] [--resumen ARCHIVO.md]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

import portal_codigos

BASE = Path(__file__).resolve().parent
RAIZ = BASE / "datos" / "portal"
BOE = "https://www.boe.es"
PORTAL = "https://subastas.boe.es"
DETALLE = "/detalleSubasta.php"
PESTANAS = {"1": "general", "2": "autoridad", "3": "bienes", "5": "pujas"}
NOMBRE_PESTANA = {"1": "Información general", "2": "Autoridad gestora", "3": "Bienes", "5": "Pujas"}
ENCABEZADO_PESTANA = {"1": "datos de la subasta", "2": "autoridad gestora", "3": "bien", "5": "pujas"}
RUTAS_DOC = {"/verDocumento.php", "/verCertificadoCierre.php"}
ANUNCIO = re.compile(r"\bBOE-B-\d{4}-\d+\b")
SUB_ID = re.compile(r"(?<![A-Z0-9])SUB-[A-Z0-9]{2,8}-\d{4}-[A-Z0-9]+(?![A-Z0-9])", re.I)
FUENTE = "Basado en datos de la Agencia Estatal Boletín Oficial del Estado"
AGENTE = "SeMa-Deal-Finder/1.0 (fichas de las subastas activas)"
PAUSA = 1.0  # segundos entre peticiones (el original usaba 0,7)
MAX_ADJUNTO = 25 * 1024 * 1024  # un adjunto mayor no se guarda en el repositorio: queda pendiente
CAMPOS_UBICACION = {"dirección": "direccion", "localidad": "localidad", "provincia": "provincia",
                    "código postal": "codigo_postal"}


class AccesoDetenido(Exception):
    """El sitio respondió 401, 403 o 429: no se le hacen más peticiones en este lote."""


class SinRedirecciones(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# --------------------------------------------------------------------------- utilidades

def texto(elemento, separador=" ") -> str:
    return separador.join(t.strip() for t in elemento.itertext() if t.strip())


def utc_ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def slug(valor: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", valor).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", ascii_.casefold()).strip("_")[:55]


def escribir(ruta: Path, datos: bytes):
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    tmp.write_bytes(datos)
    tmp.replace(ruta)


def escribir_json(ruta: Path, valor):
    escribir(ruta, (json.dumps(valor, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def comprobar_pdf(contenido: bytes):
    if not contenido.startswith(b"%PDF-") or b"%%EOF" not in contenido[-2048:]:
        raise ValueError("La respuesta no tiene la cabecera y el fin de un PDF.")


def extension_documento(contenido: bytes, mime: str) -> str:
    if contenido.startswith(b"%PDF-"):
        comprobar_pdf(contenido)
        return ".pdf"
    if mime in {"text/html", "application/xhtml+xml", "application/json"} or re.match(
            rb"\s*<(?:!doctype\s+html|html|\?xml)", contenido[:512], re.I):
        raise ValueError("El enlace devolvió una página web o un mensaje en vez del adjunto.")
    if mime == "application/pdf":
        raise ValueError("El archivo anunciado como PDF no contiene un PDF válido.")
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/tiff": ".tiff",
            "application/zip": ".zip", "text/plain": ".txt"}.get(mime, ".bin")


# --------------------------------------------------------------------------- URLs válidas

def clave_portal(url: str, ref: str):
    """("ficha", pestaña, lote) o (ruta del documento, idDoc, lote) si la URL es del Portal y del mismo SUB."""
    partes = urlsplit(url)
    valores = parse_qs(partes.query)
    if (partes.scheme, partes.netloc) != ("https", "subastas.boe.es") or valores.get("idSub") != [ref]:
        return None
    lote = valores.get("idLote", [""])[0]
    if partes.path == DETALLE:
        return ("ficha", valores.get("ver", ["1"])[0], lote)
    if partes.path in RUTAS_DOC:
        return (partes.path, valores.get("idDoc", [""])[0], lote)
    return None


def url_portal_valida(url: str, ref: str) -> bool:
    partes = urlsplit(url)
    if (partes.scheme, partes.netloc) != ("https", "subastas.boe.es"):
        return False
    if partes.path == "/ds.php":
        return parse_qs(partes.query).get("id") == [ref]
    clave = clave_portal(url, ref)
    return clave is not None and (clave[0] != "/verDocumento.php" or bool(clave[1]))


def url_boe_valida(url: str) -> bool:
    partes = urlsplit(url)
    return (partes.scheme == "https" and partes.netloc in {"www.boe.es", "boe.es"}
            and (partes.path == "/diario_boe/xml.php" or partes.path.startswith("/boe/dias/")))


def clave_estable(clave):
    """Las pestañas 1 y 2 son las mismas para todos los lotes."""
    if clave[0] == "ficha" and clave[1] in {"1", "2"}:
        return (clave[0], clave[1], "")
    return clave


# --------------------------------------------------------------------------- lectura de las pestañas

def ubicaciones_bienes(principal, lote_consulta: str) -> dict:
    """Dirección, localidad, provincia y código postal de cada bien (pestaña Bienes), por lote y número de bien."""
    seccion = principal.find(".//*[@id='idBloqueDatos3']")
    if seccion is None:
        return {}
    ubicaciones = {}
    for bloque in seccion.iter("div"):
        lote = re.fullmatch(r"idBloqueLote(\d+)", bloque.get("id", ""))
        if lote is None:
            continue
        lote_anunciado = next((m[1] for h3 in bloque.iter("h3")
                               if (m := re.match(r"Lote\s+(\d+)\b", texto(h3), re.I))), None)
        for item in bloque.iter("div"):
            titulo = item.find("h4")
            if titulo is None:
                continue
            cabecera = re.fullmatch(r"Bien\s+(\d+)(?:\s*[-–]\s*(.+))?", texto(titulo), re.I)
            if cabecera is None:
                continue
            ubicacion = dict.fromkeys(CAMPOS_UBICACION.values())
            hijos = list(item)
            tabla = next((h for h in hijos[hijos.index(titulo) + 1:] if h.tag in {"table", "h4", "h5"}), None)
            if tabla is not None and tabla.tag == "table":
                for fila in tabla.findall("tr") + tabla.findall("tbody/tr"):
                    etiqueta, valor = fila.find("th"), fila.find("td")
                    campo = CAMPOS_UBICACION.get(" ".join(texto(etiqueta).casefold().split())) if etiqueta is not None else None
                    if campo is not None and valor is not None:
                        ubicacion[campo] = texto(valor) or None
            n_bien = int(cabecera[1])
            ubicaciones[lote[1], n_bien] = dict(lote=lote_anunciado or lote_consulta or None, bien=n_bien,
                                                tipo=cabecera[2], **ubicacion)
    return ubicaciones


def analizar_pestana(contenido: bytes, url: str, ref: str):
    """Comprueba la pestaña y devuelve (datos, enlaces del mismo SUB, ubicaciones de los bienes)."""
    pagina = portal_codigos.PaginaPortal().leer(contenido.decode("utf-8-sig"))
    principal = pagina.find(".//*[@id='contenido']")
    h2 = principal.find(".//h2") if principal is not None else None
    if h2 is None or SUB_ID.findall(texto(h2)) != [ref]:
        raise ValueError("El HTML no contiene el encabezado SUB esperado.")
    for item in principal.iter():
        if item.tag in {"script", "style"}:
            item.text = None
    encabezados = [texto(i) for i in principal.iter() if i.tag in {"h3", "h4", "h5"}]
    actual = clave_portal(url, ref)
    pestana = actual[1] if actual else "1"
    if pestana in ENCABEZADO_PESTANA and not any(ENCABEZADO_PESTANA[pestana] in e.casefold() for e in encabezados):
        raise ValueError(f"El HTML no contiene los encabezados esperados de la pestaña {pestana}.")
    enlaces = {}
    for a in principal.iter("a"):
        href = a.get("href")
        if href:
            destino = urljoin(url, href)
            clave = clave_portal(destino, ref)
            if clave and (clave[0] != "ficha" or "ver" in parse_qs(urlsplit(destino).query) or clave[2]):
                enlaces[clave] = dict(url=destino, etiqueta=texto(a))
    tablas = [[[texto(celda) for celda in fila if celda.tag in {"th", "td"}] for fila in tabla.iter("tr")]
              for tabla in principal.iter("table")]
    restringido = any("para consultar" in frase and "iniciar sesión" in frase
                      for i in principal.iter() if i.tag in {"p", "li"} if (frase := texto(i).casefold()))
    ubicaciones = ubicaciones_bienes(principal, actual[2]) if pestana == "3" and actual else {}
    return dict(encabezados=encabezados, tablas=tablas, aviso_acceso=restringido), enlaces, ubicaciones


# --------------------------------------------------------------------------- HTTP

class Cliente:
    """GET de una en una con pausa, sin redirecciones automáticas (cada salto se valida) y con cookies de sesión."""

    def __init__(self, abrir=None, pausa=PAUSA, dormir=time.sleep, reloj=time.monotonic):
        self._opener = build_opener(SinRedirecciones(), HTTPCookieProcessor())
        self.abrir = abrir or self._abrir
        self.pausa, self.dormir, self.reloj = pausa, dormir, reloj
        self.ultima: float | None = None
        self.detenidos: dict[str, str] = {}
        self.peticiones = 0

    def _abrir(self, url: str, accept: str):
        peticion = Request(url, headers={"Accept": accept, "User-Agent": AGENTE})
        try:
            r = self._opener.open(peticion, timeout=30)
        except HTTPError as e:
            r = e
        with r:
            return r.code, r.headers.get("Location"), r.headers.get("Content-Type", ""), r.read()

    def get(self, url: str, accept: str, ref: str | None = None):
        actual = urldefrag(url)[0]
        momento = utc_ahora()
        for _ in range(5):
            partes = urlsplit(actual)
            if not (url_portal_valida(actual, ref) if ref else url_boe_valida(actual)):
                raise ValueError("El enlace o la redirección queda fuera de las rutas oficiales del expediente.")
            if partes.netloc in self.detenidos:
                raise AccesoDetenido(self.detenidos[partes.netloc])
            if self.ultima is not None:
                self.dormir(max(0.0, self.pausa - (self.reloj() - self.ultima)))
            self.ultima = self.reloj()
            self.peticiones += 1
            estado, destino, tipo, cuerpo = self.abrir(actual, accept)
            if estado in {301, 302, 303, 307, 308} and destino:
                siguiente = urljoin(actual, destino)
                if ref and urlsplit(siguiente).path == "/acceso.php":
                    raise ValueError("El Portal pide identificación para este recurso.")
                actual = urldefrag(siguiente)[0]
                continue
            if estado in {401, 403, 429}:
                mensaje = f"{partes.netloc} devolvió HTTP {estado}. Se detienen sus peticiones."
                self.detenidos[partes.netloc] = mensaje
                raise AccesoDetenido(mensaje)
            if estado != 200:
                raise ValueError(f"El sitio devolvió HTTP {estado}.")
            if not cuerpo:
                raise ValueError("Respuesta vacía (HTTP 200).")
            mime = (tipo or "").split(";")[0].lower().strip()
            return cuerpo, dict(url=urldefrag(url)[0], url_final=actual, consultado_utc=momento, tipo_mime=mime,
                                sha256=sha256(cuerpo), bytes=len(cuerpo))
        raise ValueError("Demasiadas redirecciones.")


# --------------------------------------------------------------------------- anuncio oficial del BOE

def anuncio_oficial(cliente: Cliente, ident: str, ref: str, raiz: Path):
    """XML y PDF del anuncio en www.boe.es, guardados una vez en datos/portal/anuncios. Devuelve (registro, aviso)."""
    carpeta = raiz / "anuncios"
    carpeta.mkdir(parents=True, exist_ok=True)
    ficha = carpeta / f"{ident}.json"
    if ficha.exists() and (carpeta / f"{ident}.xml").exists():
        registro = json.loads(ficha.read_text(encoding="utf-8"))
        contenido = (carpeta / f"{ident}.xml").read_bytes()
        reutilizado = True
    else:
        contenido, meta = cliente.get(f"{BOE}/diario_boe/xml.php?id={ident}", "application/xml")
        registro, reutilizado = None, False
    raiz_xml = ET.fromstring(contenido)
    if raiz_xml.tag != "documento" or raiz_xml.findtext("metadatos/identificador") != ident:
        raise ValueError("El XML no corresponde al anuncio esperado.")
    sello = raiz_xml.findtext("metadatos/fecha_publicacion", "")
    if not re.fullmatch(r"\d{8}", sello):
        raise ValueError("El XML carece de una fecha de publicación válida.")
    dia = datetime.strptime(sello, "%Y%m%d").date()
    cuerpo = raiz_xml.find("texto")
    if cuerpo is None:
        raise ValueError("El XML no contiene el nodo de texto.")
    parrafos = " ".join("".join(h.itertext()) for h in cuerpo) if len(cuerpo) else "".join(cuerpo.itertext())
    buscable = unquote(parrafos + " " + " ".join(v for e in cuerpo.iter() for v in e.attrib.values()))
    if ref not in {m.upper() for m in SUB_ID.findall(buscable)}:
        raise ValueError("El anuncio no contiene el SUB consultado.")
    if reutilizado:
        return {**registro, "reutilizado": True}, registro.get("aviso")
    escribir(carpeta / f"{ident}.xml", contenido)
    registro = dict(anuncio=ident, publicado=dia.isoformat(), actualizado_fuente=raiz_xml.get("fecha_actualizacion", ""),
                    archivos=[dict(archivo=f"anuncios/{ident}.xml", **meta)], aviso=None)
    url_pdf = urljoin(BOE + "/", raiz_xml.findtext("metadatos/url_pdf", "").strip())
    esperada = f"{BOE}/boe/dias/{dia:%Y/%m/%d}/pdfs/{ident}.pdf"
    if url_pdf not in {esperada, esperada.replace("://www.boe.es/", "://boe.es/")}:
        registro["aviso"] = "El XML no contiene la URL PDF esperada."
    else:
        try:
            pdf, meta_pdf = cliente.get(url_pdf, "application/pdf")
            comprobar_pdf(pdf)
            escribir(carpeta / f"{ident}.pdf", pdf)
            registro["archivos"].append(dict(archivo=f"anuncios/{ident}.pdf", **meta_pdf))
        except (ValueError, OSError, AccesoDetenido, HTTPException) as e:
            registro["aviso"] = str(e)
    escribir_json(ficha, registro)
    return {**registro, "reutilizado": False}, registro["aviso"]


# --------------------------------------------------------------------------- ficha de un SUB

def paginas_previas(raiz: Path, ref: str) -> dict:
    """Páginas ya guardadas en fichas anteriores del mismo SUB, por (pestaña, lote): {sha256, archivo}."""
    previas = {}
    for f in sorted((raiz / "fichas" / ref).glob("*/ficha.json")):
        try:
            for pg in json.loads(f.read_text(encoding="utf-8")).get("paginas", []):
                if pg.get("sha256") and (raiz / pg["archivo"]).exists():
                    previas[(pg["pestana"], pg.get("lote") or "")] = pg
        except (OSError, ValueError):
            continue
    return previas


def documentos_previos(raiz: Path, ref: str) -> dict:
    """Adjuntos ya guardados en fichas anteriores del mismo SUB, por (ruta, idDoc, lote)."""
    previos = {}
    for f in sorted((raiz / "fichas" / ref).glob("*/ficha.json")):
        try:
            for d in json.loads(f.read_text(encoding="utf-8")).get("adjuntos", []):
                if d.get("clave") and (raiz / d["archivo"]).exists():
                    previos[tuple(d["clave"])] = d
        except (OSError, ValueError):
            continue
    return previos


def descargar_ficha(cliente: Cliente, ref: str, raiz: Path, momento: datetime, captura: str | None = None) -> dict:
    """Descarga la ficha completa de un SUB. Escribe la carpeta entera al final (todo o nada) y devuelve el resumen."""
    destino = raiz / "fichas" / ref / momento.strftime("%Y%m%d_%H%M%S")
    obra = destino.with_name(destino.name + ".tmp")
    if obra.exists():
        shutil.rmtree(obra)
    (obra / "portal").mkdir(parents=True)
    (raiz / "documentos").mkdir(parents=True, exist_ok=True)
    previos = documentos_previos(raiz, ref)
    previas = paginas_previas(raiz, ref)
    peticiones_antes = cliente.peticiones
    paginas, adjuntos, anuncios, pendientes = [], [], [], []
    anuncios_vistos: set[str] = set()
    ubicaciones = {}
    origen = f"{PORTAL}/ds.php?id={ref}"
    cola = deque([(("ficha", "1", ""), origen, NOMBRE_PESTANA["1"])])
    programadas = {("ficha", "1", "")}
    while cola:
        clave, url, etiqueta = cola.popleft()
        try:
            if clave[0] == "ficha":
                contenido, meta = cliente.get(url, "text/html", ref)
                datos, enlaces, ubic = analizar_pestana(contenido, meta["url_final"], ref)
                pestana, lote = clave[1], clave[2]
                sufijo = f"_lote_{slug(lote) or sha256(lote.encode())[:12]}" if lote else ""
                nombre = f"{PESTANAS.get(pestana, 'pestana_' + slug(pestana))}{sufijo}.html.gz"
                anterior = previas.get((pestana, lote))
                if anterior and anterior["sha256"] == meta["sha256"]:
                    # la página no ha cambiado desde la ficha anterior: se apunta a la copia ya guardada
                    archivo, igual = anterior["archivo"], True
                else:
                    escribir(obra / "portal" / nombre, gzip.compress(contenido, 9, mtime=0))
                    archivo, igual = f"fichas/{ref}/{destino.name}/portal/{nombre}", False
                paginas.append(dict(archivo=archivo, igual_que_anterior=igual, etiqueta=etiqueta, pestana=pestana,
                                    lote=lote or None, **meta, **datos))
                for k, u in ubic.items():
                    if k not in ubicaciones or lote:
                        ubicaciones[k] = dict(**u, fuente=dict(pestana="Bienes", archivo=archivo, url=meta["url_final"]))
                if datos["aviso_acceso"]:
                    pendientes.append(dict(tipo="acceso_restringido", url=meta["url_final"], etiqueta=etiqueta,
                                           motivo="La página indica que parte de la información requiere identificación."))
                if pestana == "1":
                    for tabla in datos["tablas"]:
                        for fila in tabla:
                            if fila and fila[0].casefold().strip() == "anuncio boe":
                                anuncios_vistos.update(ANUNCIO.findall(" ".join(fila[1:])))
                for clave_enlace, enlace in enlaces.items():
                    k = clave_estable(clave_enlace)
                    if k not in programadas and url_portal_valida(enlace["url"], ref):
                        programadas.add(k)
                        cola.append((k, enlace["url"], enlace["etiqueta"]))
            else:
                previo = previos.get(tuple(clave))
                if previo:
                    adjuntos.append({**previo, "etiqueta": etiqueta, "reutilizado": True})
                    continue
                contenido, meta = cliente.get(url, "*/*", ref)
                if len(contenido) > MAX_ADJUNTO:
                    raise ValueError(f"Adjunto de {len(contenido) // 1048576} MB: no se guarda en el repositorio.")
                extension = extension_documento(contenido, meta["tipo_mime"])
                nombre = f"documentos/{meta['sha256']}{extension}"
                if not (raiz / nombre).exists():
                    escribir(raiz / nombre, contenido)
                adjuntos.append(dict(archivo=nombre, etiqueta=etiqueta, lote=clave[2] or None, clave=list(clave),
                                     reutilizado=False, **meta))
        except (ValueError, OSError, AccesoDetenido, HTTPException) as e:
            pendientes.append(dict(tipo="descarga_portal", url=url, etiqueta=etiqueta, motivo=str(e)))
            if isinstance(e, AccesoDetenido):
                pendientes.extend(dict(tipo="consulta_interrumpida", url=u, etiqueta=et, motivo=str(e)) for _, u, et in cola)
                cola.clear()
    encontradas = {p["pestana"] for p in paginas}
    pendientes.extend(dict(tipo="pestana_pendiente", etiqueta=NOMBRE_PESTANA[n]) for n in PESTANAS if n not in encontradas)
    lotes_anunciados = next((int(m[1]) for p in paginas if p["pestana"] == "1" for t in p["tablas"] for f in t
                             if f and f[0].casefold().strip() == "lotes"
                             if (m := re.fullmatch(r"\s*(\d+)(?:\s+lotes?)?\s*", " ".join(f[1:]), re.I))), None)
    lotes = {p["lote"] for p in paginas if p["pestana"] == "3" and p["lote"]}
    if lotes_anunciados and len(lotes) < lotes_anunciados:
        pendientes.append(dict(tipo="lotes_pendientes", anunciados=lotes_anunciados, descargados=len(lotes)))
    if paginas and not anuncios_vistos:
        pendientes.append(dict(tipo="anuncio_pendiente", motivo="La ficha no mostró un identificador BOE-B."))
    for ident in sorted(anuncios_vistos):
        try:
            registro, aviso = anuncio_oficial(cliente, ident, ref, raiz)
            anuncios.append(registro)
            if aviso:
                pendientes.append(dict(tipo="pdf_anuncio_pendiente", anuncio=ident, motivo=aviso))
        except (ValueError, ET.ParseError, OSError, AccesoDetenido, HTTPException) as e:
            pendientes.append(dict(tipo="anuncio_pendiente", anuncio=ident, motivo=str(e)))
    ficha = dict(sub=ref, fuente=FUENTE, sede=BOE, url=origen, consultado_utc=utc_ahora(), captura_codigos=captura,
                 peticiones=cliente.peticiones - peticiones_antes, lotes_identificados=sorted(lotes, key=lambda x: (len(x), x)),
                 ubicaciones_bienes=list(ubicaciones.values()), paginas=paginas, adjuntos=adjuntos, anuncios=anuncios,
                 pendientes=pendientes)
    escribir_json(obra / "ficha.json", ficha)
    escribir_json(obra / "pendientes.json", pendientes)
    escribir_json(obra / "portal" / "fuentes.json", [dict(archivo=Path(i["archivo"]).name, url=i["url_final"])
                                                     for i in [*paginas, *adjuntos] if i.get("url_final")])
    if destino.exists():
        shutil.rmtree(destino)
    obra.rename(destino)
    estado = "fallida" if not paginas else "con_pendientes" if pendientes else "completa"
    return dict(sub=ref, estado=estado, carpeta=str(destino.relative_to(raiz)), peticiones=ficha["peticiones"],
                paginas=len(paginas),
                adjuntos=len(adjuntos), adjuntos_nuevos=sum(not a["reutilizado"] for a in adjuntos),
                anuncios=len(anuncios), pendientes=len(pendientes))


# --------------------------------------------------------------------------- lote

def ultima_captura(raiz: Path, provincia: str) -> Path | None:
    capturas = sorted(p.parent for p in (raiz / "codigos" / provincia).glob("*/listado.json"))
    return capturas[-1] if capturas else None


def ya_descargada(raiz: Path, ref: str, captura: str) -> bool:
    for f in (raiz / "fichas" / ref).glob("*/ficha.json"):
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("captura_codigos") == captura:
                return True
        except (OSError, ValueError):
            continue
    return False


def lote(codigos: list[str], raiz: Path, captura: str | None, cliente: Cliente | None = None, forzar=False,
         reloj=portal_codigos.ahora_madrid) -> dict:
    cliente = cliente or Cliente()
    inicio = reloj()
    resultados = []
    for ref in codigos:
        if "subastas.boe.es" in cliente.detenidos:
            resultados.append(dict(sub=ref, estado="no_consultada", motivo=cliente.detenidos["subastas.boe.es"]))
            continue
        if captura and not forzar and ya_descargada(raiz, ref, captura):
            resultados.append(dict(sub=ref, estado="ya_descargada"))
            continue
        print(f"{ref}: descargando…", flush=True)
        r = descargar_ficha(cliente, ref, raiz, reloj(), captura)
        print(f"{ref}: {r['estado']} · {r['paginas']} páginas, {r['adjuntos']} adjuntos, {r['anuncios']} anuncios, "
              f"{r['pendientes']} pendientes", flush=True)
        resultados.append(r)
    fin = reloj()
    resumen = dict(inicio=inicio.isoformat(timespec="seconds"), fin=fin.isoformat(timespec="seconds"),
                   captura_codigos=captura, total=len(codigos), peticiones=cliente.peticiones,
                   detenido=cliente.detenidos.get("subastas.boe.es"),
                   por_estado={e: sum(r["estado"] == e for r in resultados) for e in sorted({r["estado"] for r in resultados})},
                   resultados=resultados)
    carpeta = raiz / "fichas" / "_ejecuciones"
    carpeta.mkdir(parents=True, exist_ok=True)
    escribir_json(carpeta / f"{inicio:%Y%m%d_%H%M%S}.json", resumen)
    return resumen


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Descarga la ficha completa de cada subasta del Portal de Subastas del BOE.")
    p.add_argument("sub", nargs="*", help="Códigos SUB. Si no se indican, los de la última captura de portal_codigos.py.")
    p.add_argument("--provincia", default="41", help="Provincia de la captura de códigos (por defecto 41, Sevilla).")
    p.add_argument("--raiz", type=Path, default=RAIZ, help="Carpeta datos/portal.")
    p.add_argument("--forzar", action="store_true", help="Vuelve a descargar aunque ya haya ficha de esta captura.")
    p.add_argument("--max", type=int, help="Descarga como mucho N fichas (para pruebas).")
    p.add_argument("--resumen", type=Path, help="Añade un resumen en Markdown a este archivo (p. ej. $GITHUB_STEP_SUMMARY).")
    a = p.parse_args(argv)
    captura = None
    if a.sub:
        codigos = [s.strip().upper() for s in a.sub]
        malos = [s for s in codigos if not SUB_ID.fullmatch(s)]
        if malos:
            print(f"Códigos SUB no válidos: {', '.join(malos)}", file=sys.stderr)
            return 2
    else:
        carpeta = ultima_captura(a.raiz, a.provincia.zfill(2))
        if carpeta is None:
            print("No hay ninguna captura de códigos: ejecuta antes portal_codigos.py.", file=sys.stderr)
            return 1
        captura = f"{carpeta.parent.name}/{carpeta.name}"
        codigos = [l.strip() for l in (carpeta / "codigos.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.max is not None:
        codigos = codigos[:a.max]
    try:
        r = lote(codigos, a.raiz, captura, forzar=a.forzar)
    except (OSError, URLError) as e:
        print(f"El lote se detuvo. {e}", file=sys.stderr)
        return 1
    print(f"Fichas: {r['por_estado']} · {r['peticiones']} peticiones." + (f" Detenido: {r['detenido']}" if r["detenido"] else ""))
    if a.resumen:
        with open(a.resumen, "a", encoding="utf-8") as f:
            f.write(f"## Fichas del Portal ({captura or 'códigos indicados'})\n\n"
                    f"{r['total']} subastas · {r['peticiones']} peticiones · " +
                    ", ".join(f"{k}: {v}" for k, v in r["por_estado"].items()) + "\n\n" +
                    (f"**Detenido:** {r['detenido']}\n\n" if r["detenido"] else "") +
                    "| SUB | Estado | Páginas | Adjuntos | Anuncios | Pendientes |\n|---|---|---|---|---|---|\n" +
                    "".join(f"| {x['sub']} | {x['estado']} | {x.get('paginas', '')} | {x.get('adjuntos', '')} | "
                            f"{x.get('anuncios', '')} | {x.get('pendientes', '')} |\n" for x in r["resultados"]) + "\n")
    if r["detenido"] or (r["resultados"] and all(x["estado"] in {"fallida", "no_consultada"} for x in r["resultados"])):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

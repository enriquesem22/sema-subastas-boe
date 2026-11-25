"""Códigos SUB de las subastas que se están celebrando en el Portal de Subastas del BOE, por provincia del bien.

Basado en buscar_codigos_sevilla.py (carpeta de Drive «App_Codigo subastas Sevilla», 25-11-2026), con la misma
consulta y las mismas comprobaciones. Hace la búsqueda avanzada del Portal (https://subastas.boe.es/subastas_ava.php)
con estado EJ («Celebrándose»), la provincia donde está el bien (BIEN.COD_PROVINCIA: 41 Sevilla, 28 Madrid,
08 Barcelona, 21 Huelva…), 500 resultados por página y orden por SUB.

Antes de guardar comprueba que:
- la respuesta es HTTP 200, HTML, sin redirección, y la página es el buscador oficial («Subastas. Búsqueda avanzada»);
- la hora del servidor coincide (±5 min) con la de la consulta, para no guardar una copia en caché;
- el recuento «Resultados X a Y de Z» cuadra con las filas mostradas y las páginas siguen sin saltos;
- cada fila tiene un código SUB y el estado «Celebrándose», y no hay códigos repetidos.
Si algo no cuadra, no guarda nada y termina con error.

El acceso automatizado al Portal se hace por decisión de Enrique (25-11-2026), con peticiones de una en una y pausa
entre páginas. Resultado en datos/portal/codigos/<provincia>/<AAAAMMDD_HHMMSS>/: codigos.txt (un SUB por línea),
listado.json (consulta, momento, total y códigos) y cada página original (pagina-N.html.gz) con su SHA-256.

Uso: python portal_codigos.py [--provincia 41] [--salida-raiz datos/portal/codigos] [--resumen ARCHIVO.md]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree.ElementTree import TreeBuilder
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE = Path(__file__).resolve().parent
DESTINO = BASE / "datos" / "portal" / "codigos"
BUSCADOR = "https://subastas.boe.es/subastas_ava.php"
FILTROS = {
    "campo[2]": "SUBASTA.ESTADO.CODIGO",
    "dato[2]": "EJ",
    "campo[8]": "BIEN.COD_PROVINCIA",  # provincia donde está el bien, no la sede de la autoridad
    "page_hits": "500",
    "sort_field[0]": "SUBASTA.ID_SUBASTA_ORDENAR",
    "sort_order[0]": "asc",
    "accion": "Buscar",
}
PAUSA_ENTRE_PAGINAS = 2.0  # segundos
MAX_PAGINAS = 20
RECUENTO = re.compile(r"Resultados\s+(\d+)\s+a\s+(\d+)\s+de\s+(\d+)", re.I)
TITULO = re.compile(r"SUBASTA\s+(SUB-[A-Z0-9]{2,8}-\d{4}-[A-Z0-9]+)(?:\s+\(\d+ lotes?\))?", re.I)
ACTIVA = re.compile(r"Estado:\s*Celebrándose(?:\s*-|$)", re.I)
SIN_RESULTADOS = "No se han encontrado documentos que satisfagan sus criterios de búsqueda"
VACIAS = set("area base br col embed hr img input link meta param source track wbr".split())


class ErrorPortal(ValueError):
    """La respuesta del Portal no se puede dar por buena: no se guarda nada."""


class PaginaPortal(HTMLParser):
    """HTML del Portal a un árbol ElementTree, cerrando las etiquetas que el HTML deja abiertas."""

    def __init__(self):
        super().__init__()
        self.arbol = TreeBuilder()
        self.arbol.start("document", {})
        self.abiertas: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.arbol.start(tag, dict(attrs))
        if tag in VACIAS:
            self.arbol.end(tag)
        else:
            self.abiertas.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VACIAS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.abiertas:
            while (abierta := self.abiertas.pop()) != tag:
                self.arbol.end(abierta)
            self.arbol.end(tag)

    def handle_data(self, data):
        self.arbol.data(data)

    def leer(self, contenido: str):
        self.feed(contenido)
        self.close()
        for tag in reversed(self.abiertas):
            self.arbol.end(tag)
        self.arbol.end("document")
        return self.arbol.close()


class SinRedirecciones(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def texto(elemento) -> str:
    return " ".join(" ".join(elemento.itertext()).split())


def con_clase(elemento, tag: str, clase: str):
    return (n for n in elemento.iter(tag) if clase in (n.get("class") or "").split())


def ahora_madrid() -> datetime:
    try:
        return datetime.now(ZoneInfo("Europe/Madrid"))
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()


def analizar_pagina(contenido: str):
    """Devuelve (primera, ultima, total, codigos, enlace_siguiente) de una página de resultados."""
    pagina = PaginaPortal().leer(contenido)
    seccion = pagina.find(".//*[@id='contenido']")
    titulo = seccion.find(".//h2") if seccion is not None else None
    if titulo is None or texto(titulo) != "Subastas. Búsqueda avanzada":
        raise ErrorPortal("La respuesta no corresponde al buscador oficial de subastas.")
    filas = [
        fila
        for listado in con_clase(seccion, "div", "listadoResult")
        for fila in listado.findall("ul/li")
        if "resultado-busqueda" in (fila.get("class") or "").split()
    ]
    recuentos = {
        tuple(map(int, m.groups()))
        for paginar in con_clase(seccion, "div", "paginar")
        for p in paginar.iter("p")
        if (m := RECUENTO.fullmatch(texto(p)))
    }
    if not filas and not recuentos and any(texto(p).rstrip(".") == SIN_RESULTADOS for p in seccion.iter("p")):
        return 1, 0, 0, [], None
    if len(recuentos) != 1:
        raise ErrorPortal("El Portal no devolvió un recuento reconocible.")
    primera, ultima, total = recuentos.pop()
    if not 1 <= primera <= ultima <= total or len(filas) != ultima - primera + 1:
        raise ErrorPortal(f"El Portal anuncia las filas {primera} a {ultima} de {total} y muestra {len(filas)}.")
    codigos = []
    for fila in filas:
        h3 = fila.find("h3")
        m = TITULO.fullmatch(texto(h3)) if h3 is not None else None
        if m is None:
            raise ErrorPortal("Una fila del listado no contiene un código SUB reconocible.")
        if not any(ACTIVA.match(texto(p)) for p in fila.findall("p")):
            raise ErrorPortal(f"El Portal no confirma el estado Celebrándose para {m[1]}.")
        codigos.append(m[1].upper())
    siguiente = None
    if ultima < total:
        siguiente = next((a.get("href") for paginar in con_clase(seccion, "div", "paginar2")
                          for a in paginar.iter("a") if texto(a) == "Pág. siguiente"), None)
        if not siguiente:
            raise ErrorPortal("El Portal indica que quedan resultados, pero falta el enlace a la página siguiente.")
    return primera, ultima, total, codigos, siguiente


def descargar(url: str, agente: str) -> tuple[bytes, str, str | None]:
    """GET sin redirecciones. Devuelve (cuerpo, juego de caracteres, cabecera Date)."""
    peticion = Request(url, headers={"User-Agent": agente, "Cache-Control": "no-cache"})
    try:
        with build_opener(SinRedirecciones()).open(peticion, timeout=30) as r:
            if r.status != 200:
                raise ErrorPortal(f"El Portal devolvió HTTP {r.status}.")
            if r.headers.get_content_type() != "text/html":
                raise ErrorPortal("El Portal no devolvió una página HTML.")
            return r.read(), r.headers.get_content_charset() or "utf-8", r.headers.get("Date")
    except HTTPError as e:
        raise ErrorPortal(f"El Portal devolvió HTTP {e.code}.") from e


def consultar(provincia: str, http=descargar, reloj=ahora_madrid, pausa=time.sleep) -> dict:
    """Hace la búsqueda completa (todas las páginas) y devuelve la captura sin escribir nada."""
    url_busqueda = f"{BUSCADOR}?{urlencode({**FILTROS, 'dato[8]': provincia})}"
    agente = f"SeMa-Deal-Finder/1.0 (consulta de codigos SUB activos, provincia {provincia})"
    url, codigos, paginas, total_esperado, momento = url_busqueda, [], [], None, None
    while True:
        if paginas:
            pausa(PAUSA_ENTRE_PAGINAS)
        cuerpo, juego, fecha_servidor = http(url, agente)
        recibido = reloj()
        momento = momento or recibido
        if fecha_servidor:
            hora = parsedate_to_datetime(fecha_servidor)
            if hora.tzinfo is None or abs(recibido - hora) > timedelta(minutes=5):
                raise ErrorPortal("La hora de la respuesta del Portal no coincide con la consulta actual.")
        primera, ultima, total, de_pagina, siguiente = analizar_pagina(cuerpo.decode(juego))
        if primera != len(codigos) + 1 or (total_esperado is not None and total != total_esperado):
            raise ErrorPortal("La paginación o el total del Portal cambió durante la consulta. Vuelve a ejecutarla.")
        total_esperado = total
        codigos.extend(de_pagina)
        paginas.append({"url": url, "momento": recibido.isoformat(timespec="seconds"), "fecha_servidor": fecha_servidor,
                        "sha256": hashlib.sha256(cuerpo).hexdigest(), "bytes": len(cuerpo), "contenido": cuerpo})
        if ultima == total:
            break
        if len(paginas) >= MAX_PAGINAS:
            raise ErrorPortal(f"Más de {MAX_PAGINAS} páginas de resultados: revisa la consulta.")
        url = urljoin(BUSCADOR, siguiente)
        if urlsplit(url)._replace(query="", fragment="").geturl() != BUSCADOR:
            raise ErrorPortal("El enlace de paginación no pertenece al buscador oficial.")
    if len(set(codigos)) != total_esperado:
        raise ErrorPortal("Las referencias únicas no coinciden con el total del Portal.")
    return {"momento": momento, "url_consulta": url_busqueda, "codigos": codigos, "paginas": paginas}


def guardar(captura: dict, provincia: str, nombre: str, raiz: Path) -> Path:
    """Escribe la captura en una carpeta nueva de forma atómica (todo o nada)."""
    momento = captura["momento"]
    destino = raiz.resolve() / provincia / momento.strftime("%Y%m%d_%H%M%S")
    destino.parent.mkdir(parents=True, exist_ok=True)
    listado = {
        "fecha_captura": momento.isoformat(timespec="seconds"),
        "provincia_bien": nombre,
        "cod_provincia": provincia,
        "estado": "Celebrándose",
        "tipo_bien": "Todos",
        "fuente": "BOE · Portal de Subastas (búsqueda avanzada)",
        "url_fuente": BUSCADOR,
        "url_consulta": captura["url_consulta"],
        "total_resultados": len(captura["codigos"]),
        "paginas": [{**{k: v for k, v in p.items() if k != "contenido"}, "archivo": f"pagina-{i}.html.gz"}
                    for i, p in enumerate(captura["paginas"], 1)],
        "subastas": [{"sub": c} for c in captura["codigos"]],
    }
    with TemporaryDirectory(dir=destino.parent) as tmp:
        tmp = Path(tmp)
        (tmp / "codigos.txt").write_text("".join(f"{c}\n" for c in captura["codigos"]), encoding="utf-8")
        (tmp / "listado.json").write_text(json.dumps(listado, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for i, p in enumerate(captura["paginas"], 1):
            (tmp / f"pagina-{i}.html.gz").write_bytes(gzip.compress(p["contenido"], 9, mtime=0))
        tmp.rename(destino)
    return destino


def nombre_provincia(codigo: str) -> str | None:
    try:
        geo = json.loads((BASE / "datos" / "geo" / "municipios_es.json").read_text(encoding="utf-8"))
        return geo["provincias"].get(codigo)
    except (OSError, ValueError, KeyError):
        return {"41": "Sevilla", "28": "Madrid", "08": "Barcelona", "21": "Huelva"}.get(codigo)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Códigos SUB de las subastas celebrándose en el Portal de Subastas del BOE, "
                                            "por provincia del bien.")
    p.add_argument("--provincia", default="41", help="Código de provincia con dos cifras (41 Sevilla, 28 Madrid, "
                                                      "08 Barcelona, 21 Huelva). Por defecto, 41.")
    p.add_argument("--salida-raiz", type=Path, default=DESTINO, help="Carpeta raíz de las capturas.")
    p.add_argument("--resumen", type=Path, help="Añade un resumen en Markdown a este archivo (p. ej. $GITHUB_STEP_SUMMARY).")
    a = p.parse_args(argv)
    provincia = a.provincia.strip().zfill(2)
    nombre = nombre_provincia(provincia) if re.fullmatch(r"\d{2}", provincia) else None
    if not nombre:
        print(f"Código de provincia no válido: {a.provincia!r}.", file=sys.stderr)
        return 2
    try:
        captura = consultar(provincia)
        destino = guardar(captura, provincia, nombre, a.salida_raiz)
    except (OSError, ValueError, HTTPException, URLError) as e:
        print(f"La consulta no se completó. {e}", file=sys.stderr)
        if a.resumen:
            with open(a.resumen, "a", encoding="utf-8") as f:
                f.write(f"## Códigos SUB activos · {nombre}\n\nLa consulta no se completó: {e}\n")
        return 1
    m, codigos = captura["momento"], captura["codigos"]
    print(f"{nombre} a las {m:%H:%M:%S} del {m:%d/%m/%Y}. Códigos encontrados: {len(codigos)}.")
    if codigos:
        print("\n" + "".join(f"{c}\n" for c in codigos))
    print(f"{destino / 'codigos.txt'}\n{destino / 'listado.json'}")
    if a.resumen:
        with open(a.resumen, "a", encoding="utf-8") as f:
            f.write(f"## Códigos SUB activos · {nombre}\n\n{m:%d/%m/%Y %H:%M:%S} (hora de Madrid) · estado «Celebrándose» · "
                    f"{len(codigos)} subastas · fuente: [Portal de Subastas del BOE]({captura['url_consulta']})\n\n")
            f.write("".join(f"- {c}\n" for c in codigos) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

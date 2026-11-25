"""Ubicación (coordenadas) de cada inmueble de las subastas (método nuevo, paso 4).

Fuentes oficiales, por este orden, para cada inmueble de datos/subastas.sqlite:
1. Catastro (Dirección General del Catastro), coordenadas por referencia catastral (servicio Consulta_CPMRC, con los
   14 primeros caracteres de la referencia). Se prueban, por este orden, su servicio JSON
   (OVCWcfCallejero/COVCCoordenadas.svc/json), el clásico por GET (OVCSWLocalizacionRC/OVCCoordenadas.asmx) y el
   clásico por POST → el punto de la parcela catastral (precisión «parcela»).
2. CartoCiudad (Instituto Geográfico Nacional), geocodificador (findJsonp): primero con la referencia catastral y
   luego con la dirección (con código postal, sin él y solo calle y número) → precisión «parcela» o «direccion».
   Solo se acepta si el municipio que devuelve (muniCode, código INE) es el del bien.
3. Si ninguna sirve, el bien queda sin coordenadas y con el motivo. El panel lo dibuja en el centro de su municipio y
   lo marca como aproximado (no se inventan coordenadas en la base).

Cada respuesta se guarda tal cual en datos/portal/geo/respuestas/ (con URL, momento, estado HTTP y SHA-256 en
indice.json) y no se vuelve a pedir: la lectura de las respuestas se puede rehacer sin red (--sin-red). Peticiones de
una en una, con 1 s de pausa y un User-Agent que identifica el proyecto.

Resultado: datos/portal/geo/ubicaciones.json ({"SUB|lote|bien": {lat, lon, precision, fuente, url, texto, ...}}).
Al terminar rehace la base y las tablas del panel (portal_base.py), que añade esas columnas a `bienes`.

Uso: python portal_geo.py [--sin-red] [--raiz datos/portal] [--base datos/subastas.sqlite]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import portal_base

BASE = Path(__file__).resolve().parent
RAIZ = BASE / "datos" / "portal"
CATASTRO = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCoordenadas.asmx/Consulta_CPMRC"
CATASTRO_JSON = "https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCoordenadas.svc/json/Consulta_CPMRC"
CARTOCIUDAD = "https://www.cartociudad.es/geocoder/api/geocoder/findJsonp"
HOSTS = {"ovc.catastro.meh.es", "www.cartociudad.es"}
AGENTE = "SeMa-Deal-Finder/1.0 (ubicacion de los inmuebles de subastas publicas)"
PAUSA = 1.0
FUENTE_CATASTRO = "Dirección General del Catastro · coordenadas de la parcela (Consulta_CPMRC)"
FUENTE_CARTOCIUDAD = "CartoCiudad (IGN) · geocodificador de direcciones"
FUENTE_CARTOCIUDAD_RC = "CartoCiudad (IGN) · búsqueda por referencia catastral"
# España peninsular, Baleares, Canarias, Ceuta y Melilla
LAT, LON = (27.0, 44.5), (-18.5, 4.6)


def utc_ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rc14(valor: str | None) -> str | None:
    """Los 14 primeros caracteres de una referencia catastral (parcela), o None si el texto no lo es («NO CONSTA»)."""
    t = re.sub(r"[^0-9A-Z]", "", (valor or "").upper())
    return t[:14] if len(t) >= 14 and t[:5].isdigit() and sum(c.isdigit() for c in t[:14]) >= 9 else None


def limpiar_direccion(d: str | None) -> str | None:
    """Quita lo que confunde a un geocodificador: «nº», escalera, planta, puerta, bloque, edificio…"""
    if not d:
        return None
    s = re.sub(r"\bn\.?\s*[ºo°]\.?\s*", " ", d, flags=re.I)
    s = re.sub(r"^\s*edificio\s+", "", s, flags=re.I)
    s = re.split(r",?\s*-?\s*\b(?:escalera|esc\b\.?|planta|piso|puerta|pta\b\.?|bloque|bq\b\.?|bj\b|bajo|portal|local|"
                 r"edificio|izda|izq|dcha|der)\b", s, flags=re.I)[0]
    s = re.sub(r",\s*(?=\d)", " ", s)  # «Calle Jesús, 62» → «Calle Jesús 62»
    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    return s or None


# --------------------------------------------------------------------------- lectura de las respuestas

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _pares_json(o):
    """Todos los pares clave → valor simple de un JSON anidado, en orden."""
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                yield from _pares_json(v)
            else:
                yield k, v
    elif isinstance(o, list):
        for v in o:
            yield from _pares_json(v)


def leer_catastro(datos: bytes) -> dict:
    """Respuesta de Consulta_CPMRC (XML o JSON) → {lat, lon, texto} o {error}. No depende del espacio de nombres."""
    valores: dict[str, str] = {}
    errores = []
    texto = datos.decode("utf-8-sig", "replace").strip()
    if texto.startswith(("{", "[")):
        try:
            pares = list(_pares_json(json.loads(texto)))
        except ValueError as e:
            return {"error": f"JSON no válido: {e}"}
    else:
        try:
            pares = [(_local(e.tag), e.text) for e in ET.fromstring(datos).iter()]
        except ET.ParseError as e:
            return {"error": f"XML no válido: {e}"}
    for nombre, valor in pares:
        v = str(valor).strip() if valor is not None else ""
        if nombre in {"xcen", "ycen", "ldt", "srs"} and nombre not in valores and v:
            valores[nombre] = v
        if nombre == "des" and v:
            errores.append(v)
    if "xcen" in valores and "ycen" in valores:
        try:
            lon, lat = float(valores["xcen"]), float(valores["ycen"])
        except ValueError:
            return {"error": "Coordenadas no numéricas"}
        return {"lat": lat, "lon": lon, "texto": valores.get("ldt"), "srs": valores.get("srs")}
    return {"error": "; ".join(errores) or "La respuesta no trae coordenadas"}


def leer_cartociudad(datos: bytes) -> dict:
    """Respuesta de findJsonp (JSONP o JSON) → {lat, lon, municipio, tipo, texto, estado} o {error}."""
    t = datos.decode("utf-8-sig", "replace").strip()
    m = re.fullmatch(r"[\w$.]*\s*\((.*)\)\s*;?", t, re.S)
    if m:
        t = m[1].strip()
    if not t or t in {"null", "{}", "[]"}:
        return {"error": "Sin resultado"}
    try:
        r = json.loads(t)
    except ValueError as e:
        return {"error": f"JSON no válido: {e}"}
    if isinstance(r, list):
        r = r[0] if r else {}
    if not isinstance(r, dict) or r.get("lat") is None or r.get("lng") is None:
        return {"error": str((r or {}).get("stateMsg") or "Sin coordenadas")}
    texto = " ".join(str(x) for x in (r.get("address"), r.get("portalNumber"), r.get("postalCode")) if x not in (None, "", 0))
    return {"lat": float(r["lat"]), "lon": float(r["lng"]), "municipio": r.get("muni"), "municipio_ine": r.get("muniCode"),
            "provincia": r.get("province"), "tipo": r.get("type"), "texto": texto or None,
            "referencia": r.get("refCatastral"), "estado": r.get("stateMsg") or r.get("state")}


def en_espana(lat, lon) -> bool:
    return LAT[0] <= lat <= LAT[1] and LON[0] <= lon <= LON[1]


# --------------------------------------------------------------------------- caché de respuestas

class Respuestas:
    """Respuestas guardadas por URL: datos/portal/geo/respuestas/<sha256 de la URL>.<ext> e indice.json."""

    def __init__(self, carpeta: Path, red: bool = True, abrir=None, dormir=time.sleep):
        self.carpeta = carpeta
        self.carpeta.mkdir(parents=True, exist_ok=True)
        self.ruta_indice = carpeta / "indice.json"
        self.indice = json.loads(self.ruta_indice.read_text(encoding="utf-8")) if self.ruta_indice.exists() else {}
        self.red, self.abrir, self.dormir = red, abrir or self._abrir, dormir
        self.ultima: float | None = None
        self.peticiones = 0

    @staticmethod
    def _abrir(url: str, datos: bytes | None = None):
        cab = {"User-Agent": AGENTE}
        if datos is not None:
            cab["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            with urlopen(Request(url, data=datos, headers=cab), timeout=30) as r:
                if urlsplit(r.geturl()).netloc not in HOSTS:
                    raise ValueError(f"Redirección fuera de los servicios oficiales: {r.geturl()}")
                return r.status, r.read()
        except HTTPError as err:
            return err.code, err.read()

    def obtener(self, url: str, ext: str, datos: bytes | None = None) -> tuple[bytes | None, dict]:
        """Respuesta 200 guardada o nueva. Un error no se da por bueno (se reintenta otro día), pero su cuerpo se guarda."""
        clave = url if datos is None else f"POST {url} {datos.decode()}"
        e = self.indice.get(clave)
        if e and e.get("archivo") and (self.carpeta / e["archivo"]).exists():
            return (self.carpeta / e["archivo"]).read_bytes(), e
        if not self.red:
            return None, {"motivo": "sin red y sin respuesta guardada"}
        if self.ultima is not None:
            self.dormir(max(0.0, PAUSA - (time.monotonic() - self.ultima)))
        self.ultima = time.monotonic()
        self.peticiones += 1
        momento = utc_ahora()
        try:
            estado, cuerpo = self.abrir(url) if datos is None else self.abrir(url, datos)
        except (URLError, OSError, ValueError) as err:
            self.indice[clave] = {"url": url, "momento": momento, "http": None, "motivo": str(err)}
            return None, self.indice[clave]
        entrada = {"url": url, "momento": momento, "http": estado}
        if datos is not None:
            entrada["post"] = datos.decode()
        nombre = hashlib.sha256(clave.encode()).hexdigest()[:24]
        if estado == 200 and cuerpo:
            (self.carpeta / (nombre + ext)).write_bytes(cuerpo)
            entrada.update(archivo=nombre + ext, sha256=hashlib.sha256(cuerpo).hexdigest(), bytes=len(cuerpo))
        else:
            entrada["motivo"] = f"HTTP {estado}"
            if cuerpo:
                (self.carpeta / (nombre + ".error")).write_bytes(cuerpo[:20000])
                entrada["archivo_error"] = nombre + ".error"
        self.indice[clave] = entrada
        return (cuerpo if entrada.get("archivo") else None), entrada

    def guardar(self):
        self.ruta_indice.write_text(json.dumps(self.indice, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- ubicación de cada inmueble

def variantes_direccion(bien: dict) -> list[str]:
    """Formas de la dirección para CartoCiudad: con código postal, sin él y solo «calle número»."""
    d = limpiar_direccion(bien.get("direccion"))
    mun = bien.get("municipio") or bien.get("localidad")
    if not d or not mun:
        return []
    cp = (bien.get("codigo_postal") or "").strip()
    out = [f"{d}, {cp} {mun}" if re.fullmatch(r"\d{5}", cp) else None, f"{d}, {mun}"]
    if (corta := re.match(r"(.*?[A-Za-zÁÉÍÓÚÑáéíóúñ].*?\d+)\b", d)) and corta[1] != d:
        out.append(f"{corta[1]}, {mun}")
    return list(dict.fromkeys(x for x in out if x))


def mismo_municipio(r: dict, bien: dict) -> bool:
    if r.get("municipio_ine") and bien.get("municipio_ine"):
        return str(r["municipio_ine"]) == str(bien["municipio_ine"])
    return portal_base.norm(r.get("municipio")) == portal_base.norm(bien.get("municipio") or bien.get("localidad"))


def ubicar(bien: dict, resp: Respuestas) -> dict:
    """Devuelve la ubicación del bien con su fuente, o {"motivo": ...} con lo que se intentó."""
    intentos = []
    ref = rc14(bien.get("referencia_catastral"))
    if ref:
        consultas = [
            (f"{CATASTRO_JSON}?{urlencode({'SRS': 'EPSG:4326', 'RefCat': ref})}", ".json", None),
            (f"{CATASTRO}?{urlencode({'Provincia': '', 'Municipio': '', 'SRS': 'EPSG:4326', 'RefCat': ref})}", ".xml", None),
            (CATASTRO, ".xml", urlencode({'Provincia': '', 'Municipio': '', 'SRS': 'EPSG:4326', 'RefCat': ref}).encode()),
        ]
        for url, ext, cuerpo in consultas:
            datos, meta = resp.obtener(url, ext, cuerpo)
            r = leer_catastro(datos) if datos else {"error": meta.get("motivo")}
            if "lat" in r and en_espana(r["lat"], r["lon"]):
                return dict(lat=r["lat"], lon=r["lon"], precision="parcela", fuente=FUENTE_CATASTRO, url=url,
                            metodo="POST" if cuerpo else "GET", texto=r.get("texto"), referencia=ref,
                            consultado=meta.get("momento"), sha256=meta.get("sha256"))
            intentos.append(f"Catastro {'POST' if cuerpo else 'GET'} {urlsplit(url).path.rsplit('/', 2)[-2]} ({ref}): "
                            f"{r.get('error') or 'coordenadas fuera de España'}")
        url = f"{CARTOCIUDAD}?{urlencode({'q': ref, 'limit': 1})}"
        datos, meta = resp.obtener(url, ".json")
        r = leer_cartociudad(datos) if datos else {"error": meta.get("motivo")}
        if "lat" in r and en_espana(r["lat"], r["lon"]) and mismo_municipio(r, bien):
            return dict(lat=r["lat"], lon=r["lon"], precision="parcela", fuente=FUENTE_CARTOCIUDAD_RC, url=url,
                        texto=r.get("texto"), tipo=r.get("tipo"), referencia=ref, consultado=meta.get("momento"),
                        sha256=meta.get("sha256"))
        intentos.append(f"CartoCiudad por referencia ({ref}): " + (r.get("error") or
                        ("otro municipio (" + str(r.get("municipio")) + ")" if "lat" in r else "sin coordenadas")))
    else:
        intentos.append("Sin referencia catastral válida en la ficha")
    variantes = variantes_direccion(bien)
    for q in variantes:
        url = f"{CARTOCIUDAD}?{urlencode({'q': q, 'limit': 1})}"
        datos, meta = resp.obtener(url, ".json")
        r = leer_cartociudad(datos) if datos else {"error": meta.get("motivo")}
        if "lat" in r and en_espana(r["lat"], r["lon"]):
            if mismo_municipio(r, bien):
                return dict(lat=r["lat"], lon=r["lon"], precision="direccion", fuente=FUENTE_CARTOCIUDAD, url=url,
                            texto=r.get("texto"), tipo=r.get("tipo"), estado=r.get("estado"), consulta=q,
                            referencia=r.get("referencia"), consultado=meta.get("momento"), sha256=meta.get("sha256"))
            intentos.append(f"CartoCiudad («{q}»): devuelve otro municipio ({r.get('municipio')})")
        else:
            intentos.append(f"CartoCiudad («{q}»): {r.get('error') or 'coordenadas fuera de España'}")
    if not variantes:
        intentos.append("Sin dirección o municipio para buscarla")
    return {"motivo": " · ".join(intentos)}


def ejecutar(raiz: Path, ruta_base: Path, red: bool = True, resp: Respuestas | None = None) -> dict:
    resp = resp or Respuestas(raiz / "geo" / "respuestas", red=red)
    con = sqlite3.connect(f"file:{ruta_base}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    bienes = [dict(r) for r in con.execute(
        "SELECT sub, lote, bien, clase, referencia_catastral, direccion, codigo_postal, localidad, municipio, municipio_ine, "
        "provincia "
        "FROM bienes WHERE clase = 'Inmueble' ORDER BY sub, lote, bien")]
    con.close()
    ubicaciones = {}
    for b in bienes:
        clave = f"{b['sub']}|{b['lote'] or ''}|{b['bien']}"
        ubicaciones[clave] = ubicar(b, resp)
        resp.guardar()
    (raiz / "geo" / "ubicaciones.json").write_text(json.dumps(ubicaciones, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                                   encoding="utf-8")
    cuenta = {"inmuebles": len(bienes), "peticiones": resp.peticiones}
    for u in ubicaciones.values():
        k = u.get("precision", "sin_ubicacion")
        cuenta[k] = cuenta.get(k, 0) + 1
    return cuenta


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Coordenadas de cada inmueble (Catastro y CartoCiudad) y nueva base.")
    p.add_argument("--sin-red", action="store_true", help="Solo relee las respuestas ya guardadas.")
    p.add_argument("--raiz", type=Path, default=RAIZ)
    p.add_argument("--base", type=Path, default=portal_base.BASE_DATOS)
    p.add_argument("--panel", type=Path, default=portal_base.PANEL)
    a = p.parse_args(argv)
    if not a.base.exists():
        print("Falta datos/subastas.sqlite: ejecuta antes portal_base.py.", file=sys.stderr)
        return 1
    print(json.dumps({"ubicaciones": ejecutar(a.raiz, a.base, red=not a.sin_red)}, ensure_ascii=False))
    return portal_base.main(["--raiz", str(a.raiz), "--base", str(a.base), "--panel", str(a.panel)])


if __name__ == "__main__":
    raise SystemExit(main())

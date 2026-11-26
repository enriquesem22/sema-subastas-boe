"""Ubicación (coordenadas) de cada inmueble de las subastas (método nuevo, paso 4).

Fuentes oficiales, por este orden, para cada inmueble de datos/subastas.sqlite:
1. Catastro (Dirección General del Catastro), coordenadas por referencia catastral (servicio Consulta_CPMRC, con los
   14 primeros caracteres de la referencia). Se usa su servicio JSON (OVCWcfCallejero/COVCCoordenadas.svc/json,
   parámetro RefCat), el que funciona; si fallara, el clásico (OVCSWLocalizacionRC/OVCCoordenadas.asmx, parámetro RC)
   por GET y por POST → el punto de la parcela catastral (precisión «parcela»).
2. CartoCiudad (Instituto Geográfico Nacional), geocodificador (findJsonp): primero con la referencia catastral y
   luego con la dirección (con código postal, sin él y solo calle y número). Solo se acepta si el municipio que devuelve
   (muniCode, código INE) es el del bien y si coinciden la calle (nombre y tipo de vía) y el número de la ficha
   (precisión «direccion»); con la calle bien y otro número queda como precisión «calle»; con otra calle se descarta.

Comprobación de la dirección: en cada bien se compara la dirección de su ficha con la oficial (la que el Catastro da
para su referencia catastral, o la que devuelve CartoCiudad): comprobacion = «coincide», «numero_distinto»,
«calle_distinta» o «sin_comprobar», con el detalle.
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
import difflib
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
            "tip_via": r.get("tip_via"), "calle": r.get("address"), "numero": r.get("portalNumber"), "cp": r.get("postalCode"),
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

# --------------------------------------------------------------------------- comprobación de la dirección

# tipos de vía (abreviatura del Catastro o de la ficha → nombre común) y palabras que no identifican la calle
TIPOS_VIA = {"cl": "calle", "c": "calle", "calle": "calle", "av": "avenida", "avda": "avenida", "avenida": "avenida",
             "pz": "plaza", "pza": "plaza", "plaza": "plaza", "rd": "ronda", "ronda": "ronda", "ur": "urbanizacion",
             "urb": "urbanizacion", "urbanizacion": "urbanizacion", "cr": "carretera", "ctra": "carretera",
             "carretera": "carretera", "ps": "paseo", "paseo": "paseo", "pj": "pasaje", "pasaje": "pasaje", "ml": "muelle",
             "muelle": "muelle", "tr": "travesia", "travesia": "travesia", "cm": "camino", "camino": "camino",
             "gl": "glorieta", "glorieta": "glorieta", "pa": "paraje", "paraje": "paraje", "bo": "barrio", "barrio": "barrio",
             "pg": "poligono", "poligono": "poligono", "parcela": "parcela", "dehesa": "dehesa", "cjon": "callejon",
             "callejon": "callejon", "res": "residencial", "residencial": "residencial", "edificio": "edificio",
             "ed": "edificio", "bulevar": "bulevar", "via": "via", "cuesta": "cuesta", "sector": "sector"}
VACIAS = {"de", "del", "la", "las", "los", "el", "y", "e", "o", "n", "no", "num", "numero", "sn", "bajo", "bj", "esc", "escalera",
          "planta", "pl", "puerta", "pt", "pta", "bloque", "bq", "piso", "izda", "izq", "izquierda", "dcha", "der", "derecha",
          "local", "portal", "letra", "es", "km", "hoy", "con", "acceso", "por", "sita", "situado", "situada"}
NUMEROS_ESCRITOS = {"uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7",
                    "ocho": "8", "nueve": "9", "diez": "10"}


def _tokens(texto: str | None, quitar: set[str]) -> list[str]:
    t = portal_base.norm(re.sub(r"\([^)]*\)", " ", texto or ""))
    return [w for w in re.findall(r"[a-z]+|\d+", t) if w not in quitar]


def partes_direccion(texto: str | None, municipio: str | None = None, provincia: str | None = None) -> dict:
    """Tipo de vía, palabras del nombre de la calle y números de una dirección escrita."""
    quitar = set(portal_base.norm(municipio).split()) | set(portal_base.norm(provincia).split())
    tokens = _tokens(texto, quitar)
    tipo = next((TIPOS_VIA[w] for w in tokens if w in TIPOS_VIA and w not in {"edificio", "ed", "parcela"}), None)
    palabras = [w for w in tokens if not w.isdigit() and w not in TIPOS_VIA and w not in VACIAS and len(w) >= 3]
    return {"tipo": tipo, "palabras": palabras, "tokens": tokens}


def _parecida(a: str, b: str) -> bool:
    if a == b or (len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a))):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def numeros_tras_calle(tokens: list[str], palabras_calle: list[str]) -> set[str]:
    """Números de portal de la ficha: el primero tras el nombre de la calle, el que sigue a «nº» y los de un rango (66-68)."""
    numeros = set()
    ultimo = max((i for i, w in enumerate(tokens) if any(_parecida(w, o) for o in palabras_calle)), default=-1)
    tras = [w for w in tokens[ultimo + 1:] if w.isdigit()]
    if tras:
        numeros.add(tras[0].lstrip("0") or "0")
        i = tokens.index(tras[0], ultimo + 1)
        if i + 1 < len(tokens) and tokens[i + 1].isdigit():  # «66-68» queda como dos números seguidos
            a, b = int(tras[0]), int(tokens[i + 1])
            if 0 < b - a <= 10:
                numeros.update(str(n) for n in range(a, b + 1))
    for i, w in enumerate(tokens[:-1]):
        if w in {"n", "no", "num", "numero"} and tokens[i + 1].isdigit():  # «Nº» queda como «n» al quitar el símbolo
            numeros.add(tokens[i + 1].lstrip("0") or "0")
    if not numeros:
        numeros = {w.lstrip("0") or "0" for w in tokens if w.isdigit()}
    return numeros


def comparar_direccion(ficha: str | None, calle: str | None, numero, tipo: str | None = None,
                       municipio: str | None = None, provincia: str | None = None) -> tuple[str, str]:
    """Compara la dirección de la ficha con la oficial (calle, número y tipo de vía).
    Devuelve (estado, detalle) con estado «coincide», «numero_distinto», «calle_distinta» o «sin_comprobar»."""
    if not ficha or not calle:
        return "sin_comprobar", "No hay dirección oficial con la que compararla."
    f = partes_direccion(ficha, municipio, provincia)
    o = partes_direccion(calle, municipio, provincia)
    texto_oficial = " ".join(str(x) for x in (calle, numero) if x not in (None, ""))
    # finca rústica: polígono y parcela
    pf = re.search(r"poligono\s+(\d+)\D+parcela\s+(\d+)", " ".join(f["tokens"]))
    po = re.search(r"poligono\s+(\d+)\D+parcela\s+(\d+)", " ".join(o["tokens"]))
    if pf and po:
        return ("coincide", "Mismo polígono y parcela.") if pf.groups() == po.groups() else \
            ("calle_distinta", f"La ficha dice polígono {pf[1]} parcela {pf[2]} y la fuente oficial, polígono {po[1]} parcela {po[2]}.")
    nombre = [NUMEROS_ESCRITOS.get(w, w) for w in o["palabras"]]
    nombre = [w for w in nombre if not w.isdigit()]
    if not nombre:
        return "sin_comprobar", f"La dirección oficial («{texto_oficial}») no trae nombre de calle."
    encontradas = [w for w in nombre if any(_parecida(w, x) for x in f["palabras"])]
    if len(encontradas) / len(nombre) < 0.5 or not any(len(w) >= 4 for w in encontradas):
        return "calle_distinta", f"La ficha dice «{ficha}» y la fuente oficial, «{texto_oficial}»: no es la misma calle."
    tipo_oficial = TIPOS_VIA.get(portal_base.norm(tipo)) if tipo else o["tipo"]
    if f["tipo"] and tipo_oficial and f["tipo"] != tipo_oficial and {f["tipo"], tipo_oficial} <= {
            "calle", "avenida", "plaza", "ronda", "paseo", "carretera", "urbanizacion", "camino", "travesia", "glorieta"}:
        return "calle_distinta", f"La ficha dice {f['tipo']} y la fuente oficial, {tipo_oficial} («{texto_oficial}»): son vías distintas."
    num_oficial = str(numero).lstrip("0") if numero not in (None, "", 0) else None
    if num_oficial is None:
        num_oficial = next((w.lstrip("0") for w in o["tokens"] if w.isdigit()), None)
    if num_oficial is None:
        return "coincide", "Misma calle (la fuente oficial no da número)."
    numeros = numeros_tras_calle(f["tokens"], nombre)
    if num_oficial in numeros:
        return "coincide", "Misma calle y número."
    return "numero_distinto", (f"Misma calle, pero la ficha dice el nº {', '.join(sorted(numeros, key=int)) or '—'} "
                               f"y la fuente oficial, el nº {num_oficial} («{texto_oficial}»).")


def distancia_numero(ficha: str | None, calle: str | None, numero, municipio=None, provincia=None) -> int:
    """Diferencia entre el número de la ficha y el de la fuente oficial (9999 si no se puede calcular)."""
    try:
        nombre = [w for w in partes_direccion(calle, municipio, provincia)["palabras"] if not w.isdigit()]
        nums = numeros_tras_calle(partes_direccion(ficha, municipio, provincia)["tokens"], nombre)
        return min(abs(int(n) - int(numero)) for n in nums)
    except (ValueError, TypeError):
        return 9999


def partes_ldt(ldt: str | None) -> tuple[str | None, str | None, str | None]:
    """«CL JESUS 62 MAIRENA DEL ALCOR (SEVILLA)» → ('CL', 'CL JESUS', '62'): tipo, calle y número del Catastro."""
    if not ldt:
        return None, None, None
    m = re.match(r"\s*([A-ZÑ]{1,4})\s+(.*?)\s+(\d+)\b", ldt)
    if m and m[1].lower() in TIPOS_VIA:
        return m[1], f"{m[1]} {m[2]}", m[3]
    return None, ldt, None


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
    """Ubicación del bien con su fuente y la comprobación de su dirección, o {"motivo": ...} con lo que se intentó.

    - Catastro (por la referencia catastral de la ficha): el punto es el de su parcela; además se compara la dirección de
      la ficha con la que el Catastro da para esa referencia (comprobacion).
    - CartoCiudad (por dirección): solo se acepta si coinciden calle y número; si coincide la calle y no el número, se
      guarda como precisión «calle» (punto en la calle, número aproximado); si la calle es otra, se descarta.
    """
    intentos = []
    mun, prov = bien.get("municipio") or bien.get("localidad"), bien.get("provincia")
    ref = rc14(bien.get("referencia_catastral"))
    if ref:
        consultas = [
            (f"{CATASTRO_JSON}?{urlencode({'SRS': 'EPSG:4326', 'RefCat': ref})}", ".json", None),
            # el servicio clásico llama RC al parámetro (con RefCat responde «Missing parameter: RC»)
            (f"{CATASTRO}?{urlencode({'Provincia': '', 'Municipio': '', 'SRS': 'EPSG:4326', 'RC': ref})}", ".xml", None),
            (CATASTRO, ".xml", urlencode({'Provincia': '', 'Municipio': '', 'SRS': 'EPSG:4326', 'RC': ref}).encode()),
        ]
        for url, ext, cuerpo in consultas:
            datos, meta = resp.obtener(url, ext, cuerpo)
            r = leer_catastro(datos) if datos else {"error": meta.get("motivo")}
            if "lat" in r and en_espana(r["lat"], r["lon"]):
                tipo, calle, numero = partes_ldt(r.get("texto"))
                estado, detalle = comparar_direccion(bien.get("direccion"), calle, numero, tipo, mun, prov)
                return dict(lat=r["lat"], lon=r["lon"], precision="parcela", fuente=FUENTE_CATASTRO, url=url,
                            metodo="POST" if cuerpo else "GET", texto=r.get("texto"), referencia=ref,
                            direccion_oficial=r.get("texto"), comprobacion=estado, comprobacion_detalle=detalle,
                            consultado=meta.get("momento"), sha256=meta.get("sha256"))
            intentos.append(f"Catastro {'POST' if cuerpo else 'GET'} {urlsplit(url).path.rsplit('/', 2)[-2]} ({ref}): "
                            f"{r.get('error') or 'coordenadas fuera de España'}")
        url = f"{CARTOCIUDAD}?{urlencode({'q': ref, 'limit': 1})}"
        datos, meta = resp.obtener(url, ".json")
        r = leer_cartociudad(datos) if datos else {"error": meta.get("motivo")}
        if "lat" in r and en_espana(r["lat"], r["lon"]) and mismo_municipio(r, bien):
            estado, detalle = comparar_direccion(bien.get("direccion"), r.get("calle"), r.get("numero"), r.get("tip_via"), mun, prov)
            return dict(lat=r["lat"], lon=r["lon"], precision="parcela", fuente=FUENTE_CARTOCIUDAD_RC, url=url,
                        texto=r.get("texto"), tipo=r.get("tipo"), referencia=ref, direccion_oficial=r.get("texto"),
                        comprobacion=estado, comprobacion_detalle=detalle, consultado=meta.get("momento"),
                        sha256=meta.get("sha256"))
        intentos.append(f"CartoCiudad por referencia ({ref}): " + (r.get("error") or
                        ("otro municipio (" + str(r.get("municipio")) + ")" if "lat" in r else "sin coordenadas")))
    else:
        intentos.append("Sin referencia catastral válida en la ficha")
    variantes = variantes_direccion(bien)
    en_la_calle = None  # primer resultado con la calle correcta pero otro número
    for q in variantes:
        url = f"{CARTOCIUDAD}?{urlencode({'q': q, 'limit': 1})}"
        datos, meta = resp.obtener(url, ".json")
        r = leer_cartociudad(datos) if datos else {"error": meta.get("motivo")}
        if "lat" not in r or not en_espana(r["lat"], r["lon"]):
            intentos.append(f"CartoCiudad («{q}»): {r.get('error') or 'coordenadas fuera de España'}")
            continue
        if not mismo_municipio(r, bien):
            intentos.append(f"CartoCiudad («{q}»): devuelve otro municipio ({r.get('municipio')})")
            continue
        estado, detalle = comparar_direccion(bien.get("direccion"), r.get("calle"), r.get("numero"), r.get("tip_via"), mun, prov)
        u = dict(lat=r["lat"], lon=r["lon"], fuente=FUENTE_CARTOCIUDAD, url=url, texto=r.get("texto"), tipo=r.get("tipo"),
                 estado=r.get("estado"), consulta=q, referencia=r.get("referencia"),
                 direccion_oficial=" ".join(str(x) for x in (r.get("tip_via"), r.get("calle"), r.get("numero"), r.get("cp"))
                                            if x not in (None, "")),
                 comprobacion=estado, comprobacion_detalle=detalle, consultado=meta.get("momento"), sha256=meta.get("sha256"))
        if estado == "coincide":
            return dict(u, precision="direccion")
        if estado == "numero_distinto":
            # de los resultados en la misma calle, el de número más cercano al de la ficha
            d = distancia_numero(bien.get("direccion"), r.get("calle"), r.get("numero"), mun, prov)
            if en_la_calle is None or d < en_la_calle["_distancia"]:
                en_la_calle = dict(u, precision="calle", _distancia=d)
        intentos.append(f"CartoCiudad («{q}»): {detalle}")
    if en_la_calle:
        en_la_calle.pop("_distancia")
        return en_la_calle
    if not variantes:
        intentos.append("Sin dirección o municipio para buscarla")
    return {"motivo": " · ".join(intentos), "comprobacion": "sin_comprobar"}


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

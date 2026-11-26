"""Mapas de detalle de cada inmueble (método nuevo, paso 5).

Para cada inmueble con punto (portal_geo.py) descarga dos imágenes de unos 300 m alrededor, de servicios oficiales de
mapas (WMS, EPSG:4326):
- Catastro (Dirección General del Catastro): cartografía catastral con parcelas, edificios y números de portal.
  https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx (capa «Catastro»).
- PNOA (Instituto Geográfico Nacional): ortofoto, la foto aérea oficial. https://www.ign.es/wms-inspire/pnoa-ma
  (capa «OI.OrthoimageCoverage»).
El inmueble queda en el centro exacto de la imagen (el panel dibuja ahí la marca). Cada imagen se guarda en
datos/portal/geo/mapas/<SUB>_<lote>_<bien>_<capa>.<ext> y en indice.json con su URL, momento, SHA-256 y el punto. No se
vuelve a pedir mientras el punto no cambie. Si Pillow está instalado, las imágenes se reducen (paleta de colores para
el Catastro y JPEG para la ortofoto) para que pesen poco en el repositorio y en Drive.

También deja para el panel, en datos/panel/: cada imagen en base64 (mapas/sema-mapa-<SUB>_<lote>_<bien>_<capa>.b64.txt,
que el panel lee de Drive solo para la subasta elegida) y la tabla sema-panel-sp_mapas con el origen de cada imagen
(URL del WMS, momento, fuente y metros que abarca).

Uso: python portal_mapas.py [--sin-red] [--raiz datos/portal] [--base datos/subastas.sqlite]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import portal_base

BASE = Path(__file__).resolve().parent
ANCHO, ALTO = 640, 420           # píxeles
METROS_ANCHO = 300               # metros que abarca el ancho de la imagen
PAUSA = 1.0
AGENTE = "SeMa-Deal-Finder/1.0 (mapas de detalle de inmuebles en subasta)"
CAPAS = {
    "catastro": dict(url="https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx", capa="Catastro",
                     formato="image/png", ext=".png",
                     fuente="Dirección General del Catastro · cartografía catastral (WMS)"),
    "orto": dict(url="https://www.ign.es/wms-inspire/pnoa-ma", capa="OI.OrthoimageCoverage", formato="image/jpeg",
                 ext=".jpg", fuente="Instituto Geográfico Nacional · ortofoto PNOA (WMS)"),
}
HOSTS = {urlsplit(c["url"]).netloc for c in CAPAS.values()}
FIRMAS = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif"}


def utc_ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def caja(lat: float, lon: float) -> tuple[float, float, float, float]:
    """(lon_min, lat_min, lon_max, lat_max) de METROS_ANCHO × (METROS_ANCHO·ALTO/ANCHO) con el punto en el centro."""
    medio_x = METROS_ANCHO / 2
    medio_y = medio_x * ALTO / ANCHO
    dlat = medio_y / 111_320
    dlon = medio_x / (111_320 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def url_mapa(capa: str, lat: float, lon: float) -> str:
    c = CAPAS[capa]
    x0, y0, x1, y1 = caja(lat, lon)
    return c["url"] + "?" + urlencode({
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": c["capa"], "STYLES": "",
        "SRS": "EPSG:4326", "BBOX": f"{x0:.7f},{y0:.7f},{x1:.7f},{y1:.7f}", "WIDTH": ANCHO, "HEIGHT": ALTO,
        "FORMAT": c["formato"]})


def tipo_imagen(datos: bytes) -> str | None:
    return next((t for firma, t in FIRMAS.items() if datos.startswith(firma)), None)


def reducir(datos: bytes, capa: str) -> tuple[bytes, str]:
    """Con Pillow: paleta de 64 colores (PNG) para el Catastro y JPEG calidad 70 para la ortofoto. Sin Pillow, igual."""
    try:
        from PIL import Image
    except ImportError:
        return datos, CAPAS[capa]["ext"]
    img = Image.open(io.BytesIO(datos))
    out = io.BytesIO()
    if capa == "catastro":
        img.convert("RGB").quantize(colors=64).save(out, format="PNG", optimize=True)
        return out.getvalue(), ".png"
    img.convert("RGB").save(out, format="JPEG", quality=70, optimize=True)
    return out.getvalue(), ".jpg"


class Cliente:
    def __init__(self, abrir=None, dormir=time.sleep):
        self.abrir, self.dormir = abrir or self._abrir, dormir
        self.ultima: float | None = None
        self.peticiones = 0

    @staticmethod
    def _abrir(url: str):
        try:
            with urlopen(Request(url, headers={"User-Agent": AGENTE}), timeout=60) as r:
                if urlsplit(r.geturl()).netloc not in HOSTS:
                    raise ValueError(f"Redirección fuera de los servicios oficiales: {r.geturl()}")
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def get(self, url: str):
        if self.ultima is not None:
            self.dormir(max(0.0, PAUSA - (time.monotonic() - self.ultima)))
        self.ultima = time.monotonic()
        self.peticiones += 1
        return self.abrir(url)


def nombre_base(sub: str, lote: str | None, bien) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", f"{sub}_{lote or 0}_{bien}")


def ejecutar(raiz: Path, ruta_base: Path, panel: Path, red: bool = True, cliente: Cliente | None = None) -> dict:
    carpeta = raiz / "geo" / "mapas"
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_indice = carpeta / "indice.json"
    indice = json.loads(ruta_indice.read_text(encoding="utf-8")) if ruta_indice.exists() else {}
    cliente = cliente or Cliente()
    con = sqlite3.connect(f"file:{ruta_base}?mode=ro", uri=True)
    bienes = con.execute("SELECT b.sub, b.lote, b.bien, b.lat, b.lon FROM bienes b JOIN subastas s USING (sub) "
                         "WHERE b.clase = 'Inmueble' AND b.lat IS NOT NULL AND s.activa = 1 ORDER BY 1, 2, 3").fetchall()
    con.close()
    destino_panel = panel / "mapas"
    destino_panel.mkdir(parents=True, exist_ok=True)
    cuenta = {"inmuebles": len(bienes), "nuevas": 0, "guardadas": 0, "errores": 0}
    filas = []
    for sub, lote, bien, lat, lon in bienes:
        base = nombre_base(sub, lote, bien)
        for capa in CAPAS:
            clave = f"{base}_{capa}"
            e = indice.get(clave)
            punto = [round(lat, 7), round(lon, 7)]
            if not (e and e.get("punto") == punto and (carpeta / e.get("archivo", "-")).exists()):
                if not red:
                    continue
                url = url_mapa(capa, lat, lon)
                momento = utc_ahora()
                try:
                    estado, tipo, datos = cliente.get(url)
                except (URLError, OSError, ValueError) as err:
                    indice[clave] = dict(url=url, momento=momento, punto=punto, error=str(err))
                    cuenta["errores"] += 1
                    continue
                if estado != 200 or not tipo_imagen(datos):
                    indice[clave] = dict(url=url, momento=momento, punto=punto, http=estado,
                                         error=f"HTTP {estado}, {tipo}: {datos[:200].decode('utf-8', 'replace')}")
                    cuenta["errores"] += 1
                    continue
                reducida, ext = reducir(datos, capa)
                archivo = clave + ext
                (carpeta / archivo).write_bytes(reducida)
                indice[clave] = dict(archivo=archivo, url=url, momento=momento, punto=punto, fuente=CAPAS[capa]["fuente"],
                                     sha256_original=hashlib.sha256(datos).hexdigest(), bytes_original=len(datos),
                                     bytes=len(reducida), metros_ancho=METROS_ANCHO, ancho=ANCHO, alto=ALTO)
                cuenta["nuevas"] += 1
            e = indice[clave]
            datos = (carpeta / e["archivo"]).read_bytes()
            (destino_panel / f"sema-mapa-{clave}.b64.txt").write_text(base64.b64encode(datos).decode("ascii"), encoding="ascii")
            filas.append([clave, sub, lote, bien, capa, e["fuente"], e["url"], e["momento"], e["metros_ancho"],
                          tipo_imagen(datos), len(datos)])
            cuenta["guardadas"] += 1
        ruta_indice.write_text(json.dumps(indice, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    cuenta["peticiones"] = cliente.peticiones
    doc = dict(tabla="sp_mapas", generado=portal_base.portal_codigos.ahora_madrid().isoformat(timespec="seconds"),
               fuente="Catastro (WMS) e IGN, ortofoto PNOA (WMS)",
               columns=["clave", "sub", "lote", "bien", "capa", "fuente", "url", "momento", "metros_ancho", "mime", "bytes"],
               rows=filas)
    txt = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    (panel / "sema-panel-sp_mapas.json").write_text(txt, encoding="utf-8")
    import gzip
    (panel / "sema-panel-sp_mapas.b64.txt").write_text(
        base64.b64encode(gzip.compress(txt.encode("utf-8"), 9, mtime=0)).decode("ascii"), encoding="ascii")
    return cuenta


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Mapas de detalle (Catastro y ortofoto PNOA) de cada inmueble con punto.")
    p.add_argument("--sin-red", action="store_true", help="No pide nada: solo prepara los archivos del panel.")
    p.add_argument("--raiz", type=Path, default=portal_base.RAIZ)
    p.add_argument("--base", type=Path, default=portal_base.BASE_DATOS)
    p.add_argument("--panel", type=Path, default=portal_base.PANEL)
    a = p.parse_args(argv)
    if not a.base.exists():
        print("Falta datos/subastas.sqlite: ejecuta antes portal_base.py.", file=sys.stderr)
        return 1
    print(json.dumps({"mapas": ejecutar(a.raiz, a.base, a.panel, red=not a.sin_red)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

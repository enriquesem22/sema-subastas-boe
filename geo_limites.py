"""Límites municipales de una provincia para el mapa del panel (datos/geo/<provincia>-municipios.json).

Fuente: Instituto Geográfico Nacional (IGN), Equipamiento Geográfico de Referencia Nacional, tal como lo publica el
paquete es-atlas 0.6.0 (licencia MIT del paquete), archivo es/municipalities.json (TopoJSON). Se descarga con
`npm pack es-atlas@0.6.0` y se descomprime (tar xzf es-atlas-0.6.0.tgz); no hace ninguna petición.

Salida: un FeatureCollection con un Feature por municipio (id y properties.ine = código INE de 5 cifras,
properties.nombre) y, al final, el contorno de la provincia (id = código de 2 cifras, properties.provincia = true).
Coordenadas en grados (EPSG:4326) redondeadas a 4 decimales (unos 10 m). Es el formato que lee el panel; el de Sevilla
(datos/geo/sevilla-municipios.json, el mismo que sema-panel-limites.b64.txt en Drive) sale idéntico con este programa.

Para el panel se sube a Drive como sema-panel-limites[-<provincia>].b64.txt: el JSON comprimido con gzip (sin fecha en
la cabecera) y en base64, en una sola línea (--b64).

Uso: python geo_limites.py RUTA/es-atlas/package/es/municipalities.json --provincia 21 [--salida ARCHIVO.json] [--b64 ARCHIVO.b64.txt]
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
FUENTE = "Instituto Geográfico Nacional (IGN), Equipamiento Geográfico de Referencia Nacional, vía es-atlas 0.6.0"
NOMBRES_ARCHIVO = {"41": "sevilla", "21": "huelva", "28": "madrid", "08": "barcelona"}


def arcos(topo: dict) -> list[list[tuple[float, float]]]:
    """Arcos del TopoJSON en grados (deshace la cuantización y la codificación por diferencias)."""
    t = topo.get("transform")
    salida = []
    for arco in topo["arcs"]:
        if t:
            (sx, sy), (tx, ty) = t["scale"], t["translate"]
            x = y = 0
            puntos = []
            for dx, dy in arco:
                x += dx
                y += dy
                puntos.append((x * sx + tx, y * sy + ty))
        else:
            puntos = [tuple(p) for p in arco]
        salida.append(puntos)
    return salida


def anillo(indices: list[int], lista: list) -> list[list[float]]:
    """Une los arcos de un anillo como topojson-client: cada arco empieza donde acaba el anterior."""
    puntos: list = []
    for i in indices:
        a = lista[i] if i >= 0 else lista[~i][::-1]
        if puntos:
            puntos.pop()
        puntos.extend(a)
    while len(puntos) < 4:  # un anillo de un arco de dos puntos (topojson-client hace lo mismo)
        puntos.append(puntos[0])
    return [[round(x, 4), round(y, 4)] for x, y in puntos]


def geometria(g: dict, lista: list) -> dict:
    if g["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [anillo(r, lista) for r in g["arcs"]]}
    if g["type"] == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[anillo(r, lista) for r in p] for p in g["arcs"]]}
    raise ValueError(f"Geometría no prevista: {g['type']}")


def limites(topo: dict, provincia: str) -> dict:
    lista = arcos(topo)
    feats = []
    for g in topo["objects"]["municipalities"]["geometries"]:
        ine = str(g.get("id", ""))
        if ine[:2] == provincia:
            feats.append({"type": "Feature", "id": ine, "properties": {"ine": ine, "nombre": g["properties"]["name"]},
                          "geometry": geometria(g, lista)})
    prov = next((g for g in topo["objects"]["provinces"]["geometries"] if str(g.get("id")) == provincia), None)
    if not feats or prov is None:
        raise ValueError(f"La provincia {provincia} no está en el archivo.")
    feats.append({"type": "Feature", "id": provincia, "properties": {"nombre": prov["properties"]["name"], "provincia": True},
                  "geometry": geometria(prov, lista)})
    return {"type": "FeatureCollection", "fuente": FUENTE, "features": feats}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Límites municipales de una provincia (IGN vía es-atlas) para el mapa del panel.")
    p.add_argument("topojson", type=Path, help="es-atlas/package/es/municipalities.json")
    p.add_argument("--provincia", required=True, help="Código de provincia con dos cifras (41 Sevilla, 21 Huelva…).")
    p.add_argument("--salida", type=Path, help="Por defecto, datos/geo/<provincia>-municipios.json.")
    p.add_argument("--b64", type=Path, help="Escribe también la copia gzip+base64 para Drive en este archivo.")
    a = p.parse_args(argv)
    cod = a.provincia.strip().zfill(2)
    doc = limites(json.loads(a.topojson.read_text(encoding="utf-8")), cod)
    texto = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    salida = a.salida or BASE / "datos" / "geo" / f"{NOMBRES_ARCHIVO.get(cod, cod)}-municipios.json"
    salida.write_text(texto, encoding="utf-8")
    if a.b64:
        a.b64.write_text(base64.b64encode(gzip.compress(texto.encode("utf-8"), 9, mtime=0)).decode("ascii"), encoding="ascii")
    print(f"{doc['features'][-1]['properties']['nombre']}: {len(doc['features']) - 1} municipios → {salida}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

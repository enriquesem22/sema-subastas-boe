"""Calles de OpenStreetMap de una provincia para el mapa del panel (solo sirven para dibujarlo).

Es el mismo programa con el que se hicieron las calles de Sevilla (está dentro de sus archivos, en «meta»), con la
provincia como parámetro. Convierte la respuesta JSON de Overpass API en dos archivos compactos:
  - calles_red.json: carreteras y calles principales (autovía a terciaria), se cargan al abrir.
  - calles.json: el resto de calles (residenciales, peatonales, caminos asfaltados con nombre...),
    se cargan al acercar el mapa.
Formato de cada archivo: {origen:[lon,lat], escala, tipos, nombres, vias, meta}
  vias: [tipo, nombre, x0, y0, dx1, dy1, ...] con coordenadas en 1/escala grados respecto a origen;
  nombre es el índice en `nombres` o -1. Los tramos con el mismo nombre y tipo se unen y se
  simplifican (Douglas-Peucker, 1,5 m).

Datos: © colaboradores de OpenStreetMap, licencia ODbL. La consulta a Overpass (CONSULTA) pide las vías
highway=motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|pedestrian|road (y sus
enlaces) y las service con nombre dentro del área ISO3166-2 de la provincia.

Uso: python -I calles_osm.py --provincia 21 --consulta              (escribe la consulta de Overpass)
     python -I calles_osm.py --provincia 21 <overpass.json> <carpeta_salida>
Requiere shapely.
"""
import argparse
import json
import math
import sys
from collections import defaultdict

from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge

TIPOS = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
         'living_street', 'pedestrian', 'service', 'road']
RED = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary'}
ESCALA = 100000                   # 1e-5 grados (unos 0,9-1,1 m)
MY = 110950                       # metros por grado de latitud
TOLERANCIA_M = 1.5
# provincia: (nombre, código ISO 3166-2, esquina suroeste aproximada [lon, lat], latitud media)
PROVINCIAS = {
    '41': ('Sevilla', 'ES-SE', (-6.6, 36.8), 37.4),
    '21': ('Huelva', 'ES-H', (-7.6, 36.7), 37.5),
}
CONSULTA = """[out:json][timeout:600];
area["ISO3166-2"="{iso}"]["boundary"="administrative"]->.p;
(
  way(area.p)["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|pedestrian|road)(_link)?$"];
  way(area.p)["highway"="service"]["name"];
);
out tags geom;
"""


def tipo_de(h):
    h = h.replace('_link', '')
    return h if h in TIPOS else None


def main(provincia, entrada, salida):
    nombre_prov, iso, (lon0, lat0), lat_media = PROVINCIAS[provincia]
    mx = 111320 * math.cos(math.radians(lat_media))   # metros por grado de longitud

    def a_metros(coords):
        return [((lon - lon0) * mx, (lat - lat0) * MY) for lon, lat in coords]

    with open(entrada, encoding='utf-8') as f:
        osm = json.load(f)
    fecha_osm = (osm.get('osm3s') or {}).get('timestamp_osm_base', '')
    grupos = defaultdict(list)
    for el in osm.get('elements', []):
        if el.get('type') != 'way' or not el.get('geometry'):
            continue
        tags = el.get('tags') or {}
        t = tipo_de(tags.get('highway', ''))
        if t is None:
            continue
        nombre = (tags.get('name') or tags.get('ref') or '').strip()
        pts = [(g['lon'], g['lat']) for g in el['geometry'] if g]
        if len(pts) < 2:
            continue
        grupos[(t, nombre)].append(LineString(a_metros(pts)))

    salidas = {'calles_red': [], 'calles': []}
    for (t, nombre), lineas in grupos.items():
        unidas = linemerge(MultiLineString(lineas)) if len(lineas) > 1 else lineas[0]
        partes = list(unidas.geoms) if hasattr(unidas, 'geoms') else [unidas]
        destino = 'calles_red' if t in RED else 'calles'
        for ln in partes:
            s = ln.simplify(TOLERANCIA_M, preserve_topology=False)
            if s.is_empty or s.length < 1:
                continue
            salidas[destino].append((t, nombre, list(s.coords)))

    with open(__file__, encoding='utf-8') as f:
        codigo = f.read()
    for clave, vias in salidas.items():
        nombres, idx = [], {}
        filas = []
        # las más importantes primero, para que se dibujen y rotulen antes
        vias.sort(key=lambda v: (TIPOS.index(v[0]), v[1]))
        for t, nombre, coords in vias:
            n = -1
            if nombre:
                if nombre not in idx:
                    idx[nombre] = len(nombres)
                    nombres.append(nombre)
                n = idx[nombre]
            fila = [TIPOS.index(t), n]
            px = py = None
            for x, y in coords:
                ix = round(x / mx * ESCALA)
                iy = round(y / MY * ESCALA)
                if px is None:
                    fila += [ix, iy]
                elif ix != px or iy != py:
                    fila += [ix - px, iy - py]
                else:
                    continue
                px, py = ix, iy
            if len(fila) >= 6:
                filas.append(fila)
        doc = {
            'origen': [lon0, lat0], 'escala': ESCALA, 'tipos': TIPOS, 'nombres': nombres, 'vias': filas,
            'meta': [
                {'label': 'Fuente', 'value': '© colaboradores de OpenStreetMap, licencia ODbL (openstreetmap.org/copyright)'},
                {'label': 'Datos de OpenStreetMap a fecha de', 'value': fecha_osm},
                {'label': 'Consulta', 'value': 'Overpass API: vías highway=' + '|'.join(TIPOS) + f' (y sus enlaces; service solo con nombre) dentro de la provincia de {nombre_prov} (ISO3166-2 {iso})'},
                {'label': 'Contenido', 'value': ('Carreteras y calles principales (autovía a terciaria)' if clave == 'calles_red' else 'Resto de calles: urbanas, residenciales, peatonales, caminos sin clasificar y de servicio con nombre') + f'; {len(filas)} tramos, {len(nombres)} nombres'},
                {'label': 'calles_osm.py', 'value': codigo, 'type': 'code', 'lang': 'python'},
            ],
        }
        ruta = f'{salida}/{clave}.json'
        with open(ruta, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
        print(clave, len(filas), 'tramos', len(nombres), 'nombres')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Calles de OpenStreetMap de una provincia para el mapa del panel.')
    p.add_argument('--provincia', default='41', choices=sorted(PROVINCIAS))
    p.add_argument('--consulta', action='store_true', help='Solo escribe la consulta de Overpass para la provincia.')
    p.add_argument('entrada', nargs='?')
    p.add_argument('salida', nargs='?')
    a = p.parse_args()
    if a.consulta:
        sys.stdout.write(CONSULTA.format(iso=PROVINCIAS[a.provincia][1]))
    elif a.entrada and a.salida:
        main(a.provincia, a.entrada, a.salida)
    else:
        p.error('faltan la respuesta de Overpass y la carpeta de salida (o --consulta)')

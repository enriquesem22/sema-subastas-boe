"""Pruebas sin red de portal_mapas.py: las respuestas de los WMS del Catastro y del IGN están SIMULADAS (imágenes
generadas aquí). Las reales quedan en datos/portal/geo/mapas tras la primera ejecución en GitHub."""
import base64
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portal_mapas as pm  # noqa: E402


def imagen(formato):
    try:
        from PIL import Image
    except ImportError:
        return b"\x89PNG\r\n\x1a\n" + b"0" * 100 if formato == "PNG" else b"\xff\xd8\xff" + b"0" * 100
    out = io.BytesIO()
    Image.new("RGB", (pm.ANCHO, pm.ALTO), (200, 180, 150)).save(out, format=formato)
    return out.getvalue()


class Abrir:
    def __init__(self):
        self.pedidas = []

    def __call__(self, url):
        self.pedidas.append(url)
        if "catastro" in url:
            return 200, "image/png", imagen("PNG")
        if "ign.es" in url:
            return 200, "image/jpeg", imagen("JPEG")
        return 404, "text/html", b"no"


class PortalMapasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.raiz, self.base, self.panel = t / "portal", t / "subastas.sqlite", t / "panel"
        con = sqlite3.connect(self.base)
        con.executescript("CREATE TABLE subastas (sub TEXT, activa INTEGER); CREATE TABLE bienes (sub TEXT, lote TEXT, "
                          "bien INTEGER, clase TEXT, lat REAL, lon REAL);"
                          "INSERT INTO subastas VALUES ('SUB-JA-2026-1', 1), ('SUB-AT-2026-2', 1);"
                          "INSERT INTO bienes VALUES ('SUB-JA-2026-1', '1', 1, 'Inmueble', 37.371, -5.754),"
                          "('SUB-JA-2026-1', '2', 1, 'Inmueble', NULL, NULL), ('SUB-AT-2026-2', '', 1, 'Vehículo', NULL, NULL);")
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_caja_centrada_y_url(self):
        x0, y0, x1, y1 = pm.caja(37.371, -5.754)
        self.assertAlmostEqual((x0 + x1) / 2, -5.754, places=7)
        self.assertAlmostEqual((y0 + y1) / 2, 37.371, places=7)
        q = parse_qs(urlsplit(pm.url_mapa("catastro", 37.371, -5.754)).query)
        self.assertEqual((q["LAYERS"], q["SRS"], q["REQUEST"]), (["Catastro"], ["EPSG:4326"], ["GetMap"]))
        self.assertEqual(urlsplit(pm.url_mapa("orto", 37.371, -5.754)).netloc, "www.ign.es")

    def test_descarga_una_vez_y_prepara_el_panel(self):
        a = Abrir()
        c = pm.ejecutar(self.raiz, self.base, self.panel, cliente=pm.Cliente(abrir=a, dormir=lambda s: None))
        self.assertEqual((c["inmuebles"], c["nuevas"], c["errores"]), (1, 2, 0))  # solo el inmueble con punto
        archivo = self.panel / "mapas" / "sema-mapa-SUB-JA-2026-1_1_1_catastro.b64.txt"
        self.assertEqual(pm.tipo_imagen(base64.b64decode(archivo.read_text())), "image/png")
        tabla = json.loads((self.panel / "sema-panel-sp_mapas.json").read_text())
        self.assertEqual(sorted(r[4] for r in tabla["rows"]), ["catastro", "orto"])
        # otra vez: no se pide nada, el punto no ha cambiado
        a2 = Abrir()
        c2 = pm.ejecutar(self.raiz, self.base, self.panel, cliente=pm.Cliente(abrir=a2, dormir=lambda s: None))
        self.assertEqual((c2["nuevas"], a2.pedidas), (0, []))

    def test_error_del_servicio(self):
        def abrir(url):
            return 500, "text/xml", b"<ServiceException>fallo</ServiceException>"
        c = pm.ejecutar(self.raiz, self.base, self.panel, cliente=pm.Cliente(abrir=abrir, dormir=lambda s: None))
        self.assertEqual((c["nuevas"], c["errores"]), (0, 2))
        indice = json.loads((self.raiz / "geo" / "mapas" / "indice.json").read_text())
        self.assertIn("ServiceException", indice["SUB-JA-2026-1_1_1_catastro"]["error"])


if __name__ == "__main__":
    unittest.main()

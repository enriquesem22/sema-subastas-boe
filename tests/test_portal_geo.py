"""Pruebas sin red de portal_geo.py.

Las respuestas del Catastro (Consulta_CPMRC) y de CartoCiudad (findJsonp) son SIMULADAS según el formato documentado
de cada servicio. Las reales quedan guardadas en datos/portal/geo/respuestas tras la primera ejecución en GitHub, y
el lector se puede volver a pasar sobre ellas sin red (python portal_geo.py --sin-red).
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portal_base  # noqa: E402
import portal_geo as pg  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "portal"
XML_OK = (b'<?xml version="1.0" encoding="utf-8"?><consulta_coordenadas xmlns="http://www.catastro.meh.es/">'
          b'<control><cucoor>1</cucoor><cuerr>0</cuerr></control><coordenadas><coord><pc><pc1>6297019</pc1>'
          b'<pc2>TG5369N</pc2></pc><geo><xcen>-5.70512</xcen><ycen>37.37124</ycen><srs>EPSG:4326</srs></geo>'
          b'<ldt>CL JESUS 62 MAIRENA DEL ALCOR (SEVILLA)</ldt></coord></coordenadas></consulta_coordenadas>')
XML_ERR = (b'<consulta_coordenadas xmlns="http://www.catastro.meh.es/"><control><cucoor>0</cucoor><cuerr>1</cuerr>'
           b'</control><lerr><err><cod>11</cod><des>LA REFERENCIA CATASTRAL NO EXISTE</des></err></lerr></consulta_coordenadas>')


def jsonp(**campos):
    return ("callback(" + json.dumps(campos, ensure_ascii=False) + ")").encode()


class Abrir:
    def __init__(self, respuestas):
        self.respuestas, self.pedidas = respuestas, []

    def __call__(self, url, datos=None):
        self.pedidas.append(url)
        for trozo, cuerpo in self.respuestas.items():
            if trozo in url:
                return 200, cuerpo
        return 404, b""


class PortalGeoTest(unittest.TestCase):
    def test_referencia_catastral(self):
        self.assertEqual(pg.rc14("6297019TG5369N0002YX"), "6297019TG5369N")
        self.assertEqual(pg.rc14("9720403QB3392S0008ZZ."), "9720403QB3392S")
        self.assertEqual(pg.rc14("41900A019000170000IZ"), "41900A01900017")
        self.assertIsNone(pg.rc14("NO CONSTA"))
        self.assertIsNone(pg.rc14(None))

    def test_direccion(self):
        self.assertEqual(pg.limpiar_direccion("Calle Costa del sol nº2, bloque 2, planta 1ª, puerta C"), "Calle Costa del sol 2")
        self.assertEqual(pg.limpiar_direccion("EDIFICIO C/ RODRIGO DE TRIANA Nº 66-68"), "C/ RODRIGO DE TRIANA 66-68")
        self.assertEqual(pg.limpiar_direccion("Calle Jesús, nº 62, escalera 1, planta baja"), "Calle Jesús 62")
        self.assertEqual(pg.limpiar_direccion("CALLE NAOS 12 - ESC. 1 -2º IZDA"), "CALLE NAOS 12")
        self.assertEqual(pg.limpiar_direccion("CL SANTA ANA 1 BJ 2A"), "CL SANTA ANA 1")
        self.assertEqual(pg.variantes_direccion({"direccion": "CALLE CACERES 4", "municipio": "San Juan de Aznalfarache",
                                                 "codigo_postal": "41920"}),
                         ["CALLE CACERES 4, 41920 San Juan de Aznalfarache", "CALLE CACERES 4, San Juan de Aznalfarache"])

    def test_lectores(self):
        self.assertEqual(pg.leer_catastro(XML_OK)["lat"], 37.37124)
        js = json.dumps({"Consulta_CPMRCResult": {"control": {"cucoor": 1, "cuerr": 0}, "coordenadas": {"coord": [
            {"pc": {"pc1": "6297019", "pc2": "TG5369N"}, "geo": {"xcen": -5.7, "ycen": 37.3, "srs": "EPSG:4326"},
             "ldt": "CL JESUS 62"}]}}}).encode()
        self.assertEqual((pg.leer_catastro(js)["lat"], pg.leer_catastro(js)["texto"]), (37.3, "CL JESUS 62"))
        self.assertIn("NO EXISTE", pg.leer_catastro(XML_ERR)["error"])
        r = pg.leer_cartociudad(jsonp(muni="Marchena", province="Sevilla", type="portal", address="HUERTA GAVIRA",
                                      portalNumber=25, lat=37.33, lng=-5.42, stateMsg="Resultado exacto de la búsqueda"))
        self.assertEqual((r["lat"], r["lon"], r["municipio"], r["texto"]), (37.33, -5.42, "Marchena", "HUERTA GAVIRA 25"))
        self.assertTrue(pg.mismo_municipio({"municipio_ine": "41060", "municipio": "X"}, {"municipio_ine": "41060"}))
        self.assertFalse(pg.mismo_municipio({"municipio_ine": "41091", "municipio": "Sevilla"},
                                            {"municipio_ine": "41058", "municipio": "Sevilla"}))
        self.assertIn("error", pg.leer_cartociudad(b"callback({})"))

    def test_ubicacion_y_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp) / "portal"
            import shutil
            shutil.copytree(FIX, raiz)
            base = Path(tmp) / "subastas.sqlite"
            portal_base.construir(raiz, base)
            abrir = Abrir({"RefCat=6297019TG5369N": XML_OK, "RefCat=": XML_ERR,
                           "cartociudad": jsonp(muni="Alcalá del Río", lat=37.52, lng=-5.98, type="portal", address="X")})
            resp = pg.Respuestas(raiz / "geo" / "respuestas", abrir=abrir, dormir=lambda s: None)
            cuenta = pg.ejecutar(raiz, base, resp=resp)
            # los 4 lotes son pisos del mismo edificio: misma parcela, una sola consulta al Catastro
            self.assertEqual(cuenta["parcela"], 4)
            self.assertEqual(sum("RefCat=6297019TG5369N" in x for x in abrir.pedidas), 1)
            u = json.loads((raiz / "geo" / "ubicaciones.json").read_text())
            self.assertEqual(u["SUB-JA-2026-267391|2|1"]["precision"], "parcela")
            self.assertEqual(u["SUB-JA-2026-267391|1|1"]["texto"], "CL JESUS 62 MAIRENA DEL ALCOR (SEVILLA)")
            # SUB-JA-2026-257246 no tiene referencia («NOCONSTA»): se busca por dirección en CartoCiudad
            self.assertEqual(u["SUB-JA-2026-257246||1"]["precision"], "direccion")
            n = len(abrir.pedidas)
            # una segunda vez no pide nada: todo sale de las respuestas guardadas
            resp2 = pg.Respuestas(raiz / "geo" / "respuestas", red=False)
            pg.ejecutar(raiz, base, resp=resp2)
            self.assertEqual((resp2.peticiones, len(abrir.pedidas)), (0, n))
            portal_base.construir(raiz, base)
            con = sqlite3.connect(base)
            fila = con.execute("SELECT lat, lon, ubicacion_precision FROM bienes WHERE sub='SUB-JA-2026-267391' AND lote='1'").fetchone()
            con.close()
            self.assertEqual(fila, (37.37124, -5.70512, "parcela"))

    def test_otro_municipio_no_vale_y_referencia_inexistente(self):
        with tempfile.TemporaryDirectory() as tmp:
            abrir = Abrir({"catastro": XML_ERR, "cartociudad": jsonp(muni="Utrera", lat=37.18, lng=-5.78)})
            resp = pg.Respuestas(Path(tmp), abrir=abrir, dormir=lambda s: None)
            r = pg.ubicar({"referencia_catastral": "1111111TG1111N0001AA", "direccion": "Calle Real 1",
                           "municipio": "Marchena", "provincia": "Sevilla"}, resp)
            self.assertIn("NO EXISTE", r["motivo"])
            self.assertIn("otro municipio", r["motivo"])
            self.assertNotIn("lat", r)


if __name__ == "__main__":
    unittest.main()

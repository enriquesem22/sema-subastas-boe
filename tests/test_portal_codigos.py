"""Pruebas sin red de portal_codigos.py.

Las páginas son SINTÉTICAS: siguen la estructura que espera el lector del script original (div#contenido con el h2
«Subastas. Búsqueda avanzada», div.paginar con «Resultados X a Y de Z», div.listadoResult > ul > li.resultado-busqueda
con h3 «SUBASTA SUB-…» y un p «Estado: …», y div.paginar2 con «Pág. siguiente»). No son copias del Portal: hay que
validar el lector con la primera respuesta real, que queda guardada en pagina-N.html.gz.
"""
import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portal_codigos as pc  # noqa: E402

MOMENTO = datetime(2026, 11, 25, 19, 30, 5, tzinfo=ZoneInfo("Europe/Madrid"))


def fila(sub, estado="Celebrándose - [Conclusión prevista: 10/12/2026 a las 18:00:00]", lotes=""):
    return (f'<li class="resultado-busqueda"><h3>SUBASTA {sub}{lotes}</h3><p>Expediente: 123/2025</p>'
            f'<p>Estado: {estado}</p><a href="subastas_ava.php?accion=Mas&id={sub}">Más...</a></li>')


def pagina(filas, primera=1, total=None, siguiente=None, titulo="Subastas. Búsqueda avanzada", vacia=False):
    total = len(filas) if total is None else total
    ultima = primera + len(filas) - 1
    recuento = "" if vacia else f'<div class="paginar"><p>Resultados {primera} a {ultima} de {total}</p></div>'
    enlace = f'<div class="paginar2"><ul><li><a href="{siguiente}">Pág. siguiente</a></li></ul></div>' if siguiente else ""
    cuerpo = ("<p>No se han encontrado documentos que satisfagan sus criterios de búsqueda.</p>" if vacia else
              f'<div class="listadoResult"><ul>{"".join(filas)}</ul></div>')
    return (f'<html><head><meta charset="utf-8"><title>Portal de Subastas</title></head><body><div id="menu"><ul><li>Inicio'
            f'</ul></div><div id="contenido"><h2>{titulo}</h2>{recuento}{cuerpo}{enlace}</div></body></html>').encode("utf-8")


class FakeHttp:
    def __init__(self, paginas, desfase=timedelta(0)):
        self.paginas, self.pedidas, self.desfase = list(paginas), [], desfase

    def __call__(self, url, agente):
        self.pedidas.append((url, agente))
        return self.paginas.pop(0), "utf-8", format_datetime(MOMENTO + self.desfase)


def consultar(http, provincia="41"):
    return pc.consultar(provincia, http=http, reloj=lambda: MOMENTO, pausa=lambda s: None)


class PortalCodigosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name) / "codigos"

    def tearDown(self):
        self.tmp.cleanup()

    def test_una_pagina_y_lo_que_se_guarda(self):
        html = pagina([fila("SUB-JA-2026-111111"), fila("SUB-AT-2026-26R4186001001", lotes=" (3 lotes)")])
        http = FakeHttp([html])
        captura = consultar(http)
        self.assertEqual(captura["codigos"], ["SUB-JA-2026-111111", "SUB-AT-2026-26R4186001001"])
        url, agente = http.pedidas[0]
        self.assertTrue(url.startswith(pc.BUSCADOR + "?"))
        self.assertIn("dato%5B8%5D=41", url)
        self.assertIn("dato%5B2%5D=EJ", url)
        self.assertIn("provincia 41", agente)
        d = pc.guardar(captura, "41", "Sevilla", self.raiz)
        self.assertEqual(d.name, "20261125_193005")
        self.assertEqual((d / "codigos.txt").read_text(), "SUB-JA-2026-111111\nSUB-AT-2026-26R4186001001\n")
        listado = json.loads((d / "listado.json").read_text())
        self.assertEqual(listado["total_resultados"], 2)
        self.assertEqual(listado["provincia_bien"], "Sevilla")
        self.assertEqual(listado["fecha_captura"], "2026-11-25T19:30:05+01:00")
        self.assertEqual(listado["paginas"][0]["sha256"], hashlib.sha256(html).hexdigest())
        self.assertEqual(gzip.decompress((d / "pagina-1.html.gz").read_bytes()), html)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["codigos.txt", "listado.json", "pagina-1.html.gz"])
        self.assertEqual([p.name for p in d.parent.iterdir()], ["20261125_193005"])  # sin restos temporales

    def test_sin_resultados(self):
        captura = consultar(FakeHttp([pagina([], vacia=True)]))
        self.assertEqual(captura["codigos"], [])
        d = pc.guardar(captura, "21", "Huelva", self.raiz)
        self.assertEqual((d / "codigos.txt").read_text(), "")

    def test_paginas_siguientes(self):
        sig = "subastas_ava.php?accion=Mas&page_hits=500&pagina=2"
        p1 = pagina([fila("SUB-JA-2026-1"), fila("SUB-JA-2026-2")], total=3, siguiente=sig)
        p2 = pagina([fila("SUB-JA-2026-3")], primera=3, total=3)
        http = FakeHttp([p1, p2])
        captura = consultar(http)
        self.assertEqual(captura["codigos"], ["SUB-JA-2026-1", "SUB-JA-2026-2", "SUB-JA-2026-3"])
        self.assertEqual(http.pedidas[1][0], "https://subastas.boe.es/" + sig)
        self.assertEqual(len(captura["paginas"]), 2)

    def test_lo_que_no_cuadra_no_se_guarda(self):
        casos = {
            "no es el buscador": [pagina([fila("SUB-JA-2026-1")], titulo="Error")],
            "recuento distinto": [pagina([fila("SUB-JA-2026-1")], total=2)],
            "fila sin SUB": [pagina(['<li class="resultado-busqueda"><h3>SUBASTA</h3><p>Estado: Celebrándose</p></li>'])],
            "no se celebra": [pagina([fila("SUB-JA-2026-1", estado="Suspendida")])],
            "repetido": [pagina([fila("SUB-JA-2026-1"), fila("SUB-JA-2026-1")])],
            "falta la página siguiente": [pagina([fila("SUB-JA-2026-1")], total=2)],
            "enlace fuera del buscador": [pagina([fila("SUB-JA-2026-1")], total=2, siguiente="https://otra.web/x.php?a=1")],
            "salto en la paginación": [pagina([fila("SUB-JA-2026-1")], total=3, siguiente="subastas_ava.php?p=2"),
                                       pagina([fila("SUB-JA-2026-3")], primera=3, total=3)],
        }
        for motivo, paginas in casos.items():
            with self.subTest(motivo):
                with self.assertRaises(pc.ErrorPortal):
                    consultar(FakeHttp(paginas))

    def test_hora_del_servidor_distinta(self):
        with self.assertRaises(pc.ErrorPortal):
            consultar(FakeHttp([pagina([fila("SUB-JA-2026-1")])], desfase=timedelta(minutes=10)))

    def test_provincia_no_valida(self):
        self.assertEqual(pc.main(["--provincia", "99", "--salida-raiz", str(self.raiz)]), 2)
        self.assertEqual(pc.nombre_provincia("08"), "Barcelona")


if __name__ == "__main__":
    unittest.main()

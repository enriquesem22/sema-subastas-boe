"""Pruebas sin red de portal_fichas.py.

El Portal y el BOE están SIMULADOS: las páginas siguen la estructura que espera el lector del script original
(div#contenido, h2 con el SUB, h3 «Datos de la subasta» / «Autoridad gestora» / «Pujas», bloque idBloqueDatos3 con
idBloqueLoteN y h4 «Bien N - …»). No son copias del Portal: la validación con páginas reales se hace con la primera
descarga en GitHub, que queda guardada en datos/portal/fichas.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portal_fichas as pf  # noqa: E402

REF = "SUB-JA-2026-111111"
P = "https://subastas.boe.es"
DET = f"{P}/detalleSubasta.php?idSub={REF}"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
MOMENTO = datetime(2026, 11, 25, 20, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))


RELOJ = {"hora": "20:08:49"}  # el Portal pone en la cabecera la fecha y hora de cada consulta


def html(cuerpo, ref=REF):
    h = RELOJ["hora"]
    return (f'<html><body><div id="menu"><a href="/index.php">Inicio</a><span id="fechaOficial">%72529000, %25 253 %2026</span>'
            f'<span id="horaOficial">%{h}</span><script>var hoy = new Date(2026,11-1,25,{h.replace(":", ",")});</script>'
            f'</div><div id="contenido">'
            f'<h2>Subasta {ref}</h2><ul class="navlist">'
            f'<li><a href="./detalleSubasta.php?idSub={ref}&ver=1">Información general</a></li>'
            f'<li><a href="./detalleSubasta.php?idSub={ref}&ver=2">Autoridad gestora</a></li>'
            f'<li><a href="./detalleSubasta.php?idSub={ref}&ver=3">Bienes</a></li>'
            f'<li><a href="./detalleSubasta.php?idSub={ref}&ver=5">Pujas</a></li></ul>{cuerpo}</div>'
            f'<script>var x = "<a href=\\"./detalleSubasta.php?idSub={ref}&ver=9\\">";</script></body></html>').encode()


GENERAL = html('<h3>Datos de la subasta</h3><table>'
               f'<tr><th>Identificador</th><td>{REF}</td></tr>'
               '<tr><th>Tipo de subasta</th><td>JUDICIAL EN VIA DE APREMIO</td></tr>'
               '<tr><th>Lotes</th><td>2</td></tr>'
               '<tr><th>Anuncio BOE</th><td>BOE-B-2026-11111</td></tr></table>'
               f'<p><a href="./verDocumento.php?idSub={REF}&idDoc=77">Edicto</a></p>'
               '<p><a href="https://otra.web/doc.pdf">Fuera del Portal</a></p>')
AUTORIDAD = html('<h3>Autoridad gestora</h3><table><tr><th>Descripción</th><td>Juzgado de Primera Instancia nº 1 de Sevilla</td></tr></table>')
BIENES = html('<h3>Bienes</h3>'
              f'<p><a href="./detalleSubasta.php?idSub={REF}&ver=3&idLote=1">Lote 1</a> '
              f'<a href="./detalleSubasta.php?idSub={REF}&ver=3&idLote=2">Lote 2</a></p>')


def lote_html(n, localidad):
    return html(f'<div id="idBloqueDatos3"><div id="idBloqueLote{n}"><h3>Lote {n}</h3>'
                f'<div><h4>Bien 1 - Inmueble (Vivienda)</h4><table>'
                f'<tr><th>Dirección</th><td>CALLE REAL {n}</td></tr><tr><th>Código Postal</th><td>4100{n}</td></tr>'
                f'<tr><th>Localidad</th><td>{localidad}</td></tr><tr><th>Provincia</th><td>Sevilla</td></tr>'
                f'</table></div></div></div>')


PUJAS = html('<h3>Pujas</h3><p>Para consultar las pujas debe iniciar sesión.</p>')
XML = (b'<?xml version="1.0" encoding="UTF-8"?><documento fecha_actualizacion="20261120120000"><metadatos>'
       b'<identificador>BOE-B-2026-11111</identificador><fecha_publicacion>20261120</fecha_publicacion>'
       b'<url_pdf>/boe/dias/2026/11/20/pdfs/BOE-B-2026-11111.pdf</url_pdf></metadatos>'
       b'<texto><p>Se anuncia la subasta ' + REF.encode() + b' en el Portal de Subastas.</p></texto></documento>')


def portal():
    return {
        f"{P}/ds.php?id={REF}": (302, f"detalleSubasta.php?idSub={REF}&ver=1", "text/html", b""),
        f"{DET}&ver=1": (200, None, "text/html; charset=utf-8", GENERAL),
        f"{DET}&ver=2": (200, None, "text/html", AUTORIDAD),
        f"{DET}&ver=3": (200, None, "text/html", BIENES),
        f"{DET}&ver=5": (200, None, "text/html", PUJAS),
        f"{DET}&ver=3&idLote=1": (200, None, "text/html", lote_html(1, "Utrera")),
        f"{DET}&ver=3&idLote=2": (200, None, "text/html", lote_html(2, "Écija")),
        f"{P}/verDocumento.php?idSub={REF}&idDoc=77": (200, None, "application/pdf", PDF),
        "https://www.boe.es/diario_boe/xml.php?id=BOE-B-2026-11111": (200, None, "application/xml", XML),
        "https://www.boe.es/boe/dias/2026/11/20/pdfs/BOE-B-2026-11111.pdf": (200, None, "application/pdf", PDF),
    }


class Abrir:
    def __init__(self, respuestas):
        self.respuestas, self.pedidas = respuestas, []

    def __call__(self, url, accept):
        self.pedidas.append(url)
        if url not in self.respuestas:
            return 404, None, "text/html", b"no"
        return self.respuestas[url]


def cliente(respuestas):
    a = Abrir(respuestas)
    return pf.Cliente(abrir=a, dormir=lambda s: None), a


class PortalFichasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name) / "portal"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ficha_completa(self):
        c, a = cliente(portal())
        r = pf.descargar_ficha(c, REF, self.raiz, MOMENTO, "41/20261125_193643")
        self.assertEqual(r["estado"], "con_pendientes")  # solo por las pujas, que piden identificación
        d = self.raiz / r["carpeta"]
        ficha = json.loads((d / "ficha.json").read_text())
        self.assertEqual([p["pestana"] for p in ficha["paginas"]], ["1", "2", "3", "5", "3", "3"])
        self.assertEqual(ficha["lotes_identificados"], ["1", "2"])
        self.assertEqual({(u["lote"], u["localidad"], u["codigo_postal"]) for u in ficha["ubicaciones_bienes"]},
                         {("1", "Utrera", "41001"), ("2", "Écija", "41002")})
        self.assertEqual([p["tipo"] for p in ficha["pendientes"]], ["acceso_restringido"])
        self.assertEqual(ficha["captura_codigos"], "41/20261125_193643")
        self.assertEqual(ficha["peticiones"], len(a.pedidas))
        adj = ficha["adjuntos"][0]
        self.assertEqual((adj["etiqueta"], adj["archivo"]), ("Edicto", f"documentos/{pf.sha256(PDF)}.pdf"))
        self.assertTrue((self.raiz / adj["archivo"]).exists())
        self.assertEqual([x["archivo"] for x in ficha["anuncios"][0]["archivos"]],
                         ["anuncios/BOE-B-2026-11111.xml", "anuncios/BOE-B-2026-11111.pdf"])
        self.assertTrue((d / "portal" / "bienes_lote_1.html.gz").exists())
        self.assertNotIn("https://otra.web/doc.pdf", a.pedidas)  # nada fuera del Portal o del BOE
        self.assertFalse(any("ver=9" in u for u in a.pedidas))   # los enlaces dentro de <script> no cuentan
        self.assertEqual(sorted(x.name for x in d.parent.iterdir()), [d.name])  # sin carpeta .tmp

    def test_segunda_vez_no_repite_adjuntos_ni_anuncios(self):
        c, a = cliente(portal())
        pf.descargar_ficha(c, REF, self.raiz, MOMENTO, "41/a")
        RELOJ["hora"] = "21:00:50"  # otra consulta: solo cambia el reloj de la cabecera
        self.addCleanup(RELOJ.update, {"hora": "20:08:49"})
        c2, a2 = cliente(portal())
        r = pf.descargar_ficha(c2, REF, self.raiz, MOMENTO.replace(hour=21), "41/b")
        self.assertEqual(r["adjuntos_nuevos"], 0)
        self.assertFalse(any("verDocumento" in u or "boe.es/" in u and "www.boe.es" in u for u in a2.pedidas))
        ficha = json.loads((self.raiz / r["carpeta"] / "ficha.json").read_text())
        self.assertTrue(ficha["adjuntos"][0]["reutilizado"] and ficha["anuncios"][0]["reutilizado"])
        # las pestañas no han cambiado: no se guardan otra vez y ficha.json apunta a las de la primera descarga
        self.assertTrue(all(p["igual_que_anterior"] for p in ficha["paginas"]))
        self.assertEqual(list((self.raiz / r["carpeta"] / "portal").glob("*.html.gz")), [])
        self.assertTrue(all((self.raiz / p["archivo"]).exists() and "20261125_200000" in p["archivo"] for p in ficha["paginas"]))

    def test_pagina_que_cambia_se_guarda(self):
        c, _ = cliente(portal())
        pf.descargar_ficha(c, REF, self.raiz, MOMENTO, "41/a")
        resp = portal()
        resp[f"{DET}&ver=5"] = (200, None, "text/html", html("<h3>Pujas</h3><p>La subasta ha recibido alguna puja.</p>"))
        c2, _ = cliente(resp)
        r = pf.descargar_ficha(c2, REF, self.raiz, MOMENTO.replace(hour=21), "41/b")
        ficha = json.loads((self.raiz / r["carpeta"] / "ficha.json").read_text())
        cambiadas = [p["pestana"] for p in ficha["paginas"] if not p["igual_que_anterior"]]
        self.assertEqual(cambiadas, ["5"])
        self.assertEqual([x.name for x in (self.raiz / r["carpeta"] / "portal").glob("*.html.gz")], ["pujas.html.gz"])

    def test_429_detiene_el_lote(self):
        resp = portal()
        resp[f"{DET}&ver=3"] = (429, None, "text/html", b"despacio")
        otro = "SUB-AT-2026-26R4186001460"
        c, a = cliente(resp)
        r = pf.lote([REF, otro], self.raiz, "41/x", cliente=c, reloj=lambda: MOMENTO)
        self.assertEqual([x["estado"] for x in r["resultados"]], ["con_pendientes", "no_consultada"])
        self.assertIn("429", r["detenido"])
        ficha = json.loads((self.raiz / r["resultados"][0]["carpeta"] / "ficha.json").read_text())
        self.assertIn("consulta_interrumpida", {p["tipo"] for p in ficha["pendientes"]})
        self.assertFalse(any(otro in u for u in a.pedidas))
        self.assertTrue((self.raiz / "fichas" / "_ejecuciones" / "20261125_200000.json").exists())

    def test_reanuda_sin_repetir(self):
        c, _ = cliente(portal())
        pf.lote([REF], self.raiz, "41/x", cliente=c, reloj=lambda: MOMENTO)
        c2, a2 = cliente(portal())
        r = pf.lote([REF], self.raiz, "41/x", cliente=c2, reloj=lambda: MOMENTO)
        self.assertEqual(r["resultados"][0]["estado"], "ya_descargada")
        self.assertEqual(a2.pedidas, [])

    def test_pagina_de_otro_sub_o_login(self):
        resp = portal()
        resp[f"{DET}&ver=2"] = (200, None, "text/html", html("<h3>Autoridad gestora</h3>", ref="SUB-JA-2026-999"))
        resp[f"{DET}&ver=5"] = (302, "/acceso.php?url=x", "text/html", b"")
        c, _ = cliente(resp)
        r = pf.descargar_ficha(c, REF, self.raiz, MOMENTO)
        ficha = json.loads((self.raiz / r["carpeta"] / "ficha.json").read_text())
        motivos = {p.get("etiqueta"): p.get("motivo") for p in ficha["pendientes"] if p["tipo"] == "descarga_portal"}
        self.assertIn("encabezado SUB", motivos["Autoridad gestora"])
        self.assertIn("identificación", motivos["Pujas"])

    def test_sin_ficha(self):
        c, _ = cliente({})
        r = pf.descargar_ficha(c, REF, self.raiz, MOMENTO)
        self.assertEqual(r["estado"], "fallida")

    def test_codigos_de_la_ultima_captura(self):
        for nombre, codigos in (("20261124_100000", "SUB-JA-2026-1\n"), ("20261125_193643", f"{REF}\n")):
            d = self.raiz / "codigos" / "41" / nombre
            d.mkdir(parents=True)
            (d / "codigos.txt").write_text(codigos)
            (d / "listado.json").write_text("{}")
        self.assertEqual(pf.ultima_captura(self.raiz, "41").name, "20261125_193643")
        self.assertEqual(pf.main(["SUB-MAL", "--raiz", str(self.raiz)]), 2)


if __name__ == "__main__":
    unittest.main()

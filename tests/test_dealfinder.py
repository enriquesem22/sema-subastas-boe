"""Pruebas sin red de SeMá Deal Finder.

- Los sumarios de tests/fixtures son extractos REALES de sumarios del BOE guardados por el proyecto (solo se
  han quitado items). Se convierten a la forma JSON de la API (un nodo único llega como objeto y varios como
  lista).
- Los textos de los anuncios de Carmona, la AEAT de Sevilla y la notaría son los REALES (tests/fixtures/*.txt);
  el envoltorio XML se construye aquí. Los demás anuncios usan su título real y un texto mínimo.
- Las páginas del Portal de Subastas y el PDF son SINTÉTICOS: siguen los rótulos de la ayuda oficial del
  Portal, pero no son fichas reales. Hay que validar el lector con la primera ficha real que se guarde.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

os.environ["SEMA_PAUSA"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sema  # noqa: E402
import dealfinder as df  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def xml_a_json(e):
    hijos = list(e)
    if not hijos:
        return {**e.attrib, "texto": (e.text or "").strip()} if e.attrib else (e.text or "").strip()
    d = dict(e.attrib)
    for h in hijos:
        v = xml_a_json(h)
        if h.tag in d:
            d[h.tag] = (d[h.tag] if isinstance(d[h.tag], list) else [d[h.tag]]) + [v]
        else:
            d[h.tag] = v
    return d


def sumario_json(fecha: str, fecha_dice: str | None = None) -> bytes:
    root = ET.parse(FIX / f"sumario_{fecha}.xml").getroot()
    doc = xml_a_json(root)
    if fecha_dice:
        doc["data"]["sumario"]["metadatos"]["fecha_publicacion"] = fecha_dice
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def anuncio_xml(id_boe, titulo, texto, departamento="MINISTERIO", codigo="1", fecha="20261024"):
    parrafos = []
    for linea in texto.splitlines():
        linea = escape(linea)
        if "https://" in linea:
            pre, url = linea.split("https://", 1)
            linea = f'{pre}<a href="https://{url}">https://{url}</a>'
        parrafos.append(f'<p class="parrafo">{linea}</p>')
    return (f'<?xml version="1.0" encoding="UTF-8"?><documento fecha_actualizacion="{fecha}003012"><metadatos>'
            f'<identificador>{id_boe}</identificador><departamento codigo="{codigo}">{escape(departamento)}</departamento>'
            f'<titulo>{escape(titulo)}</titulo><fecha_publicacion>{fecha}</fecha_publicacion><diario_numero>264</diario_numero>'
            f'<pagina_inicial>1</pagina_inicial><pagina_final>1</pagina_final>'
            f'<url_pdf>/boe/dias/2026/10/24/pdfs/{id_boe}.pdf</url_pdf></metadatos>'
            f'<texto>{"".join(parrafos)}</texto></documento>').encode("utf-8")


def real(id_boe):
    return (FIX / f"{id_boe}.txt").read_text(encoding="utf-8")


XML = "https://www.boe.es/diario_boe/xml.php?id="
HTML = "https://www.boe.es/diario_boe/txt.php?id="


def respuestas():
    r = {
        sema.API_SUMARIO.format(fecha="20261024"): sumario_json("20261024"),
        sema.API_SUMARIO.format(fecha="20261026"): sumario_json("20261026"),
        sema.API_SUMARIO.format(fecha="20261028"): sumario_json("20261028"),
        sema.API_SUMARIO.format(fecha="20261027"): sumario_json("20261028", fecha_dice="20261028"),  # fecha que no coincide
        XML + "BOE-B-2026-34558": anuncio_xml("BOE-B-2026-34558", "Anuncio de CARMONA", real("BOE-B-2026-34558"),
                                              "TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN", "9602"),
        XML + "BOE-B-2026-34705": anuncio_xml("BOE-B-2026-34705", "Anuncio de U.R. SUBASTAS ANDALUCÍA 41",
                                              real("BOE-B-2026-34705"), "MINISTERIO DE HACIENDA", "5140"),
        # la notaría: el XML falla y se lee la página HTML del BOE
        HTML + "BOE-B-2026-34802": ('<div id="textoxslt">' + "".join(f"<p>{escape(x)}</p>" for x in real(
            "BOE-B-2026-34802").splitlines()) + "</div><!-- #textoxslt -->").encode("utf-8"),
        XML + "BOE-B-2026-34797": anuncio_xml("BOE-B-2026-34797", "Anuncio de información pública", "Información pública."),
        XML + "BOE-B-2026-34912": anuncio_xml(
            "BOE-B-2026-34912", "Anuncio del Fondo de Garantía Salarial O.A. (FOGASA) por el que se convoca la venta en "
            "subasta pública al alza mediante presentación inicial de ofertas en sobre cerrado, en un lote, de la finca "
            "registral 17341 ubicada en el municipio de Azpeitia, en la provincia de Gipuzkoa, propiedad del FOGASA, "
            "(Nº de procedimiento: Subasta-2/2026).", "Se convoca subasta.", fecha="20261026"),
        XML + "BOE-B-2026-35250": anuncio_xml(
            "BOE-B-2026-35250", "Resolución de la Delegación de Economía y Hacienda de Huelva, por la que se convoca "
            "tercera subasta de varios inmuebles propiedad del Estado, sitos en la provincia de Huelva.",
            "Se convoca subasta de inmuebles.", fecha="20261028"),
        XML + "BOE-B-2026-35273": anuncio_xml(
            "BOE-B-2026-35273", "Resolución de la Dirección Provincial de la Tesorería General de la Seguridad Social de "
            "Madrid por la que se anuncia subasta pública mediante procedimiento abierto de inmuebles, propiedad de la "
            "Tesorería General de la Seguridad Social, ubicados en la carretera M-119 (Camarma-Valdeavero), nº 21, de "
            "Alcalá de Henares (Madrid).", "Se anuncia subasta.", fecha="20261028"),
    }
    for i in ("BOE-B-2026-34859", "BOE-B-2026-34909", "BOE-B-2026-35191", "BOE-B-2026-35248"):
        r[XML + i] = anuncio_xml(i, "Anuncio de otro asunto", "Otro asunto sin relación.")
    return r


class FakeHttp(df.HttpDiario):
    respuestas: dict = {}
    pedidas: list = []

    def permitido(self, url):
        return "subastas.boe.es" not in url

    def _descargar(self, url, accept):
        FakeHttp.pedidas.append((url, accept))
        if url in self.respuestas:
            return 200, self.respuestas[url]
        if XML + "BOE-B-2026-34802" == url:
            return 500, None
        return 404, None


SUB_CARMONA = "SUB-JA-2026-239881"


def ficha_general(sub=SUB_CARMONA, estado="Subasta celebrándose [Conclusión prevista: 30/11/2026 a las 18:00:00]"):
    return f"""<html><!-- saved from url=(0071)https://subastas.boe.es/detalleSubasta.php?idSub={sub}&ver=1 -->
<head><meta charset="utf-8"></head><body><div id="menu">Portal de Subastas · Buscar · Mis subastas · Ayuda</div>
<h2>Subasta {sub}</h2><div class="caja">{estado}</div>
<ul class="navlist"><li><a class="current">Información general</a></li><li><a>Autoridad gestora</a></li>
<li><a>Bienes</a></li><li><a>Pujas</a></li></ul>
<table>
<tr><th>Identificador</th><td><strong>{sub}</strong></td></tr>
<tr><th>Tipo de subasta</th><td><strong>JUDICIAL EN VIA DE APREMIO</strong></td></tr>
<tr><th>Cuenta expediente</th><td>2927 0000 06 0609 17</td></tr>
<tr><th>Fecha de inicio</th><td>10-11-2026 18:00:00 CET  (ISO: 2026-11-10T18:00:00+01:00)</td></tr>
<tr><th>Fecha de conclusión</th><td>30-11-2026 18:00:00 CET</td></tr>
<tr><th>Cantidad reclamada</th><td>0,00 €</td></tr>
<tr><th>Lotes</th><td>Sin lotes</td></tr>
<tr><th>Anuncio BOE</th><td><a href="https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-34558">BOE-B-2026-34558</a></td></tr>
<tr><th>Valor subasta</th><td>150.000,00 €</td></tr>
<tr><th>Tasación</th><td></td></tr>
<tr><th>Puja mínima</th><td>Sin puja mínima</td></tr>
<tr><th>Tramos entre pujas</th><td>3.000,00 €</td></tr>
<tr><th>Importe del depósito</th><td>7.500,00 €</td></tr>
</table>
<h3>Información complementaria</h3><p><a href="https://subastas.boe.es/reg/descargaDocumento.php?id=1">Edicto</a></p>
</body></html>""".encode("utf-8")


def ficha_bienes(sub=SUB_CARMONA):
    return f"""<html><body><h2>Subasta {sub}</h2><div class="caja">Subasta celebrándose</div>
<h4>Bien 1 - Inmueble (Vivienda)</h4>
<table>
<tr><th>Descripción</th><td>URBANA. Vivienda en planta baja con 90 m2 construidos.</td></tr>
<tr><th>Dirección</th><td>CALLE PRUEBA 1</td></tr>
<tr><th>Código Postal</th><td>41410</td></tr>
<tr><th>Localidad</th><td>Carmona</td></tr>
<tr><th>Provincia</th><td>Sevilla</td></tr>
<tr><th>Referencia catastral</th><td>1234567TG5513S0001AB</td></tr>
<tr><th>IDUFIR</th><td>41011000123456</td></tr>
<tr><th>Situación posesoria</th><td>No consta</td></tr>
<tr><th>Visitable</th><td>No consta</td></tr>
</table>
<h4>Bien 2 - Inmueble (Garaje)</h4>
<table><tr><th>Localidad</th><td>Huelva</td></tr><tr><th>Provincia</th><td>Huelva</td></tr></table>
</body></html>""".encode("utf-8")


def ficha_pujas(sub=SUB_CARMONA):
    return f"""<html><body><h2>Subasta {sub}</h2><div class="caja">Subasta celebrándose</div>
<table><tr><th>Puja máxima</th><td>152.000,00 €</td></tr></table></body></html>""".encode("utf-8")


def pdf_simple(lineas: list[str]) -> bytes:
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    contenido = "BT /F1 11 Tf 50 800 Td 14 TL " + " ".join(f"({esc(x)}) '" for x in lineas) + " ET"
    cuerpo = contenido.encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(cuerpo)).encode() + b" >>\nstream\n" + cuerpo + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


class DealFinderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.datos = Path(self.tmp.name) / "datos"
        FakeHttp.respuestas = respuestas()
        FakeHttp.pedidas = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_df(self, desde=date(2026, 10, 24), hasta=date(2026, 10, 28), **kw):
        return df.ejecutar(desde, hasta, self.datos, http_factory=FakeHttp, **kw)

    def db(self):
        con = sqlite3.connect(self.datos / "dealfinder.sqlite")
        con.row_factory = sqlite3.Row
        return con

    # ---- sumario
    def test_json_y_xml_dan_los_mismos_items(self):
        for fecha in ("20261024", "20261026", "20261028"):
            _, fj, ij = df.items_sumario_json(sumario_json(fecha))
            _, fx, ix = df.items_sumario_xml((FIX / f"sumario_{fecha}.xml").read_bytes())
            self.assertEqual(fj, fecha)
            self.assertEqual(fx, fecha)
            self.assertEqual(ij, ix)
            self.assertTrue(all(i["seccion_codigo"] in ("4", "5B", "5C") for i in ij))  # 5A fuera
        _, _, items = df.items_sumario_json(sumario_json("20261024"))
        carmona = next(i for i in items if i["id_boe"] == "BOE-B-2026-34558")
        self.assertEqual(carmona["departamento"], "TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL Y DE INSTRUCCIÓN")
        self.assertEqual(carmona["url_pdf"], "https://www.boe.es/boe/dias/2026/10/24/pdfs/BOE-B-2026-34558.pdf")

    def test_items_dentro_de_texto_y_nodos_objeto_o_lista(self):
        doc = {"status": {"code": "200"}, "data": {"sumario": {"metadatos": {"fecha_publicacion": "20261124"},
               "diario": {"numero": "1", "seccion": [
                   {"codigo": "5B", "nombre": "V.B", "departamento": {"codigo": "1", "nombre": "DEP",
                    "texto": {"item": [{"identificador": "BOE-B-1"}, {"identificador": "BOE-B-2"}]},
                    "epigrafe": [{"nombre": "EPI", "item": {"identificador": "BOE-B-3", "url_pdf": {"texto": "/x.pdf"}}}]}},
                   {"codigo": "1", "departamento": {"item": {"identificador": "BOE-A-1"}}}]}}}}
        _, fecha, items = df.items_sumario_json(json.dumps(doc).encode())
        self.assertEqual(fecha, "20261124")
        self.assertEqual(sorted(i["id_boe"] for i in items), ["BOE-B-1", "BOE-B-2", "BOE-B-3"])
        b3 = next(i for i in items if i["id_boe"] == "BOE-B-3")
        self.assertEqual((b3["epigrafe"], b3["url_pdf"]), ("EPI", "https://www.boe.es/x.pdf"))
        self.assertEqual(next(i for i in items if i["id_boe"] == "BOE-B-1")["grupo_texto"], "texto")

    # ---- ejecución completa
    def test_ejecucion_detecta_relaciona_y_registra_incidencias(self):
        r = self.run_df()
        self.assertEqual(r["estado"], "ok", r)
        con = self.db()
        est = {x["fecha"]: x["estado"] for x in con.execute("SELECT fecha, estado FROM sumarios")}
        self.assertEqual(est["2026-10-24"], "procesado")
        self.assertEqual(est["2026-10-25"], "sin_sumario")  # domingo: la API responde 404
        self.assertEqual(est["2026-10-27"], "fecha_no_coincide")
        self.assertTrue(all(a == "application/json" for u, a in FakeHttp.pedidas if "sumario/" in u))
        inc = {(x["tipo"], x["clave"]) for x in con.execute("SELECT tipo, clave FROM incidencias")}
        self.assertIn(("dia_sin_sumario", "2026-10-25"), inc)
        self.assertIn(("fecha_no_coincide", "2026-10-27"), inc)
        # SUB con sufijo alfanumérico, en el texto y en el enlace
        s = con.execute("SELECT * FROM anuncio_sub WHERE id_boe='BOE-B-2026-34705'").fetchone()
        self.assertEqual(s["id_subasta"], "SUB-AT-2026-26R4186002046")
        self.assertEqual((s["en_texto"], s["en_enlace"]), (1, 1))
        a = con.execute("SELECT * FROM anuncios WHERE id_boe='BOE-B-2026-34705'").fetchone()
        self.assertEqual((a["referencia_tipo"], a["referencia_texto"]), ("referencia", "S2026R4186002046"))
        self.assertEqual(a["fecha_actualizacion"], "2026-10-24T00:30:12")
        self.assertEqual(a["sede_provincia"], "41")
        self.assertEqual(a["ubicacion_estado"], "pendiente")
        self.assertIn("https://subastas.boe.es/ds.php?id=SUB-AT-2026-26R4186002046", json.loads(a["enlaces_oficiales"]))
        # Carmona: la sede está en Sevilla, pero la ubicación del bien queda pendiente
        e = con.execute("SELECT * FROM expedientes WHERE id_expediente='SUB-JA-2026-239881'").fetchone()
        self.assertEqual((e["ambito"], e["ubicacion_estado"], e["sede_provincia"]),
                         ("sede_en_ambito_ubicacion_pendiente", "pendiente", "41"))
        self.assertEqual(e["referencia_anuncio"], "identificación 2927000006060917")
        p = con.execute("SELECT * FROM pendientes WHERE id_subasta='SUB-JA-2026-239881' AND datos LIKE '%ubicacion_bien%'").fetchone()
        self.assertIn("BOE-B-2026-34558", p["motivo"])
        self.assertIn(XML + "BOE-B-2026-34558", p["fuente_revisada"])
        # notaría: el XML falló (HTTP 500) y se leyó la página HTML del BOE
        n = con.execute("SELECT * FROM anuncios WHERE id_boe='BOE-B-2026-34802'").fetchone()
        self.assertEqual((n["xml_estado"], n["texto_desde"], n["ids_subasta"]), ("ok", "html", "SUB-NN-2026-1646099"))
        self.assertTrue(all("boe.es" in u for u in json.loads(n["enlaces_oficiales"])))
        self.assertIsNone(con.execute("SELECT texto FROM anuncios WHERE id_boe='BOE-B-2026-34797'").fetchone()[0])
        self.assertEqual(n["pagina_inicial"], "56454")  # del sumario: la página HTML no la da
        # anuncios sin SUB que tratan de inmuebles, con la ubicación que escribe el propio anuncio
        madrid = con.execute("SELECT * FROM expedientes WHERE id_expediente='BOE:BOE-B-2026-35273'").fetchone()
        self.assertEqual((madrid["ambito"], madrid["provincias_bien"]), ("ubicacion_en_ambito", "28"))
        huelva = con.execute("SELECT * FROM expedientes WHERE id_expediente='BOE:BOE-B-2026-35250'").fetchone()
        self.assertEqual((huelva["ambito"], huelva["provincias_bien"]), ("ubicacion_en_ambito", "21"))
        self.assertEqual(huelva["sede_provincia"], None)
        fogasa = con.execute("SELECT * FROM expedientes WHERE id_expediente='BOE:BOE-B-2026-34912'").fetchone()
        self.assertEqual((fogasa["ambito"], fogasa["provincias_bien"]), ("ubicacion_fuera", "20"))
        self.assertIn(("subasta_sin_sub", "BOE-B-2026-35273"), inc)
        # un anuncio que no es de subasta se guarda, pero no crea expediente ni incidencia
        otro = con.execute("SELECT menciona_subasta FROM anuncios WHERE id_boe='BOE-B-2026-34797'").fetchone()
        self.assertEqual(otro[0], 0)
        self.assertNotIn(("subasta_sin_sub", "BOE-B-2026-34797"), inc)
        self.assertTrue((self.datos / "panel" / "sema-panel-df_expedientes.b64.txt").exists())
        # brutos: un archivo por día con cada respuesta y su SHA-256
        packs = list((self.datos / "brutos" / "dealfinder").rglob("*.jsonl.gz"))
        self.assertEqual(len(packs), 1)
        regs = df.leer_brutos(packs[0])
        r0 = next(x for x in regs if x["url"] == XML + "BOE-B-2026-34705")
        self.assertEqual(sema.sha256(r0["contenido"].encode("utf-8")), r0["sha256"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM cambios_historicos WHERE campo IN ('xml_estado','xml_intentos')").fetchone()[0], 0)
        con.close()

    def test_reintento_sin_duplicados(self):
        FakeHttp.respuestas.pop(XML + "BOE-B-2026-34705")
        self.run_df(desde=date(2026, 10, 24), hasta=date(2026, 10, 24))
        con = self.db()
        self.assertEqual(con.execute("SELECT xml_estado FROM anuncios WHERE id_boe='BOE-B-2026-34705'").fetchone()[0], "error")
        self.assertIsNone(con.execute("SELECT resuelta_en FROM incidencias WHERE tipo='error_descarga' AND clave='BOE-B-2026-34705'").fetchone()[0])
        n1 = con.execute("SELECT COUNT(*) FROM anuncios").fetchone()[0]
        con.close()
        FakeHttp.respuestas = respuestas()
        self.run_df(desde=date(2026, 10, 24), hasta=date(2026, 10, 24))
        con = self.db()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM anuncios").fetchone()[0], n1)
        self.assertEqual(con.execute("SELECT xml_estado FROM anuncios WHERE id_boe='BOE-B-2026-34705'").fetchone()[0], "ok")
        self.assertIsNotNone(con.execute("SELECT resuelta_en FROM incidencias WHERE tipo='error_descarga' AND clave='BOE-B-2026-34705'").fetchone()[0])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM anuncio_sub WHERE id_boe='BOE-B-2026-34705'").fetchone()[0], 1)
        con.close()

    # ---- Portal de Subastas (consultas puntuales)
    def poner(self, rel, data):
        ruta = self.datos / "portal" / "entrada" / rel
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(data)
        return ruta

    def test_ficha_del_portal_completa_y_confirma_la_ubicacion(self):
        self.run_df()
        carpeta = f"2026-11-25/{SUB_CARMONA}/"
        self.poner(carpeta + "general.html", ficha_general())
        self.poner(carpeta + "bienes.html", ficha_bienes())
        self.poner(carpeta + "pujas.html", ficha_pujas())
        self.poner("2026-11-25/SUB-JA-2026-000001/equivocada.html", ficha_general())
        r = df.ejecutar(None, None, self.datos, solo_portal=True)
        self.assertEqual(r["estado"], "ok", r)
        con = self.db()
        c = {x["campo"]: x for x in con.execute("SELECT * FROM campos WHERE id_subasta=? AND n_bien=''", (SUB_CARMONA,))}
        self.assertEqual(c["cuenta_expediente"]["valor"], "2927 0000 06 0609 17")
        self.assertEqual(c["fecha_inicio"]["valor"], "2026-11-10T18:00:00+01:00")
        self.assertEqual(c["fecha_conclusion"]["valor"], "2026-11-30T18:00:00+01:00")
        self.assertEqual((c["valor_subasta"]["valor_num"], c["valor_subasta"]["clase_valor"]), (150000.0, "importe"))
        self.assertEqual(c["cantidad_reclamada"]["clase_valor"], "cero")
        self.assertEqual(c["tasacion"]["clase_valor"], "vacio")
        self.assertEqual((c["puja_minima"]["valor"], c["puja_minima"]["clase_valor"]), (None, "sin_puja_minima"))
        self.assertEqual(c["estado"]["valor"], "en_curso")
        self.assertEqual(c["puja_actual"]["valor_num"], 152000.0)
        self.assertEqual(c["fecha_inicio"]["fecha_consulta"], "2026-11-25")
        self.assertTrue(c["fecha_inicio"]["url"].startswith("https://subastas.boe.es/detalleSubasta.php?idSub="))
        self.assertIn("fila «Cuenta expediente»", c["cuenta_expediente"]["origen"])
        bienes = {(x["n_bien"], x["campo"]): x["valor"] for x in con.execute(
            "SELECT * FROM campos WHERE id_subasta=? AND n_bien<>''", (SUB_CARMONA,))}
        self.assertEqual(bienes[("1", "provincia")], "Sevilla")
        self.assertEqual(bienes[("1", "tipo_bien")], "Inmueble (Vivienda)")
        self.assertEqual(bienes[("2", "provincia")], "Huelva")
        e = con.execute("SELECT * FROM expedientes WHERE id_expediente=?", (SUB_CARMONA,)).fetchone()
        self.assertEqual((e["ubicacion_estado"], e["provincias_bien"], e["ambito"], e["ficha_consultada"]),
                         ("confirmada_ficha_portal", "21,41", "ubicacion_en_ambito", 1))
        mal = con.execute("SELECT * FROM capturas WHERE archivo LIKE '%equivocada%'").fetchone()
        self.assertEqual((mal["estado"], mal["coincide"]), ("sub_no_coincide", 0))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM campos WHERE id_captura=?", (mal["id_captura"],)).fetchone()[0], 0)
        pend = {d for x in con.execute("SELECT datos FROM pendientes WHERE id_subasta=?", (SUB_CARMONA,)) for d in x["datos"].split(", ")}
        self.assertNotIn("ubicacion_bien", pend)
        self.assertNotIn("cuenta_expediente", pend)
        self.assertIn("autoridad_codigo", pend)
        self.assertIn("importe_adjudicacion", pend)
        # sin cambios en los archivos no se vuelven a leer
        r2 = df.ejecutar(None, None, self.datos, solo_portal=True)
        self.assertEqual(r2.get("capturas", 0), 0)
        con.close()

    @unittest.skipUnless(shutil.which("pdftotext"), "hace falta pdftotext")
    def test_edicto_en_pdf_con_su_pagina(self):
        self.run_df()
        self.poner(f"2026-11-25/{SUB_CARMONA}/edicto.pdf", pdf_simple([
            "EDICTO", f"Subasta {SUB_CARMONA}",
            "En el procedimiento de ejecucion hipotecaria 123/2025 se saca a subasta el pleno dominio",
            "de la finca valorada en 150.000,00 euros.", "", "URBANA. Vivienda sita en Carmona."]))
        self.poner(f"2026-11-25/{SUB_CARMONA}/roto.pdf", b"esto no es un pdf")
        df.ejecutar(None, None, self.datos, solo_portal=True)
        con = self.db()
        d = con.execute("SELECT * FROM documentos WHERE titulo='edicto.pdf'").fetchone()
        self.assertEqual((d["clase"], d["pdf_valido"], d["metodo_texto"], d["paginas"]), ("edicto", 1, "texto", 1))
        c = {x["campo"]: x for x in con.execute("SELECT * FROM campos WHERE id_captura=?", (d["id_captura"],))}
        self.assertEqual(c["numero_procedimiento"]["valor"], "123/2025")
        self.assertEqual(c["derecho_subastado"]["valor"], "pleno dominio")
        self.assertEqual(c["valoracion_edicto"]["valor_num"], 150000.0)
        self.assertEqual(c["valoracion_edicto"]["origen"], "edicto · página 1")
        roto = con.execute("SELECT * FROM documentos WHERE titulo='roto.pdf'").fetchone()
        self.assertEqual(roto["pdf_valido"], 0)
        self.assertIn(("pdf_no_valido",), [tuple(x) for x in con.execute("SELECT tipo FROM incidencias")])
        con.close()

    def test_subastech_solo_se_coteja(self):
        self.datos.mkdir(parents=True, exist_ok=True)
        sx = sema.conectar(self.datos / "subastech.sqlite", sema.SUBASTECH_ESQUEMA)
        sx.execute("INSERT INTO subastech_lotes (id_subastech, id_subasta, n_lote, valor_subasta, municipio, url_ficha)"
                   " VALUES ('X1', ?, NULL, 150000.0, 'Carmona', 'https://app.subastech.com/x')", (SUB_CARMONA,))
        sx.execute("INSERT INTO subastech_lotes (id_subastech, id_subasta, n_lote, valor_subasta, url_ficha)"
                   " VALUES ('X2', 'SUB-AT-2026-26R4186002046', NULL, 99.0, 'https://app.subastech.com/y')")
        sx.commit()
        sx.close()
        self.run_df()
        con = self.db()
        p = con.execute("SELECT con_valor_subastech FROM pendientes WHERE id_subasta='SUB-AT-2026-26R4186002046'"
                        " AND datos LIKE '%valor_subasta%'").fetchone()
        self.assertIn("valor_subasta", p[0])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM campos").fetchone()[0], 0)  # nunca se copia como dato del BOE
        con.close()
        self.poner(f"2026-11-25/{SUB_CARMONA}/general.html", ficha_general())
        self.poner(f"2026-11-25/{SUB_CARMONA}/bienes.html", ficha_bienes())
        df.ejecutar(None, None, self.datos, solo_portal=True)
        con = self.db()
        res = {(x["id_subasta"], x["campo"]): x["resultado"] for x in con.execute("SELECT * FROM cotejo_subastech")}
        self.assertEqual(res[(SUB_CARMONA, "valor_subasta")], "coincide")
        self.assertEqual(res[("SUB-AT-2026-26R4186002046", "valor_subasta")], "sin_cotejar")
        con.close()

    # ---- reglas sueltas
    def test_sede_no_es_ubicacion(self):
        geo = df.Geo()
        it = {"titulo_sumario": "U.R. SUBASTAS MADRID"}
        self.assertEqual(df.sede_autoridad(geo, it, None)[0], "28")
        self.assertEqual(df.sede_autoridad(geo, {"titulo_sumario": "U.R. SUBASTAS CATALUÑA"}, "Barcelona")[0], "08")
        self.assertEqual(df.sede_autoridad(geo, {"titulo_sumario": "NOTARÍA DE ANA PÉREZ"}, "Huelva")[0], "21")
        self.assertEqual(df.sede_autoridad(geo, {"titulo_sumario": "SANT FELIU DE LLOBREGAT"}, None)[0], "08")
        self.assertEqual(df.ubicacion_en_texto(geo, "Anuncio de subasta judicial", "Carmona, 1 de octubre de 2026.- El Letrado."), [])

    def test_codigo_sub(self):
        t = "Dirección electrónica: https://subastas.boe.es/ds.php?id=SUB-AT-2026-26R2886001460. Y SUB-JA-2026-1."
        self.assertEqual(sorted(df.subs_de("", t, [])), ["SUB-AT-2026-26R2886001460", "SUB-JA-2026-1"])
        self.assertEqual(list(df.subs_de("", "", ["https://subastas.boe.es/ds.php?id%3DSUB-NN-2026-1646099"])),
                         ["SUB-NN-2026-1646099"])


if __name__ == "__main__":
    unittest.main()

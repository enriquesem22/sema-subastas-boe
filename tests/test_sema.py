"""Pruebas sin red: simulan las respuestas de la API del BOE, el CSV de Justicia y Catastro.

Los datos de prueba son SINTÉTICOS (siguen la estructura documentada por la AEBOE en
APIsumarioBOE.pdf), no son anuncios reales.
"""
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["SEMA_PAUSA"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sema  # noqa: E402

FECHA = dt.date(2026, 11, 21)

SUMARIO = """<?xml version="1.0" encoding="utf-8"?>
<response>
  <status><code>200</code><text>ok</text></status>
  <data><sumario>
    <metadatos><publicacion>BOE</publicacion><fecha_publicacion>20261121</fecha_publicacion></metadatos>
    <diario numero="280">
      <seccion codigo="3" nombre="III. Otras disposiciones">
        <departamento codigo="1" nombre="MINISTERIO DE CULTURA">
          <epigrafe nombre="Patrimonio histórico. Derecho de tanteo">
            <item><identificador>BOE-A-2026-24414</identificador>
              <titulo>Orden sobre lotes subastados por una sala de Sevilla</titulo>
              <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-24414</url_html></item>
          </epigrafe>
        </departamento>
      </seccion>
      <seccion codigo="4" nombre="IV. Administración de Justicia">
        <departamento codigo="2" nombre="JUZGADOS DE PRIMERA INSTANCIA E INSTRUCCIÓN">
          <epigrafe nombre="UTRERA">
            <item><identificador>BOE-B-2026-00001</identificador>
              <titulo>Edicto de subasta judicial</titulo>
              <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00001</url_html>
              <url_xml>https://www.boe.es/diario_boe/xml.php?id=BOE-B-2026-00001</url_xml></item>
          </epigrafe>
        </departamento>
        <departamento codigo="6" nombre="TRIBUNALES DE INSTANCIA. SECCIÓN CIVIL">
          <item><identificador>BOE-B-2026-00020</identificador>
            <titulo>SEVILLA</titulo>
            <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00020</url_html></item>
          <item><identificador>BOE-B-2026-00021</identificador>
            <titulo>SEVILLA</titulo>
            <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00021</url_html></item>
        </departamento>
        <departamento codigo="7" nombre="JUZGADOS DE PRIMERA INSTANCIA E INSTRUCCIÓN">
          <epigrafe nombre="SEVILLA">
            <item><identificador>BOE-B-2026-00003</identificador>
              <titulo>Edicto de subasta (solo XML)</titulo>
              <url_xml>https://www.boe.es/diario_boe/xml.php?id=BOE-B-2026-00003</url_xml></item>
          </epigrafe>
        </departamento>
      </seccion>
      <seccion codigo="5A" nombre="V. Anuncios. A. Contratación del Sector Público">
        <departamento codigo="3" nombre="ADMINISTRACIÓN LOCAL">
          <epigrafe nombre="AYUNTAMIENTO DE SEVILLA">
            <item><identificador>BOE-B-2026-00002</identificador>
              <titulo>Enajenación de una parcela municipal mediante subasta</titulo>
              <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00002</url_html></item>
          </epigrafe>
        </departamento>
      </seccion>
      <seccion codigo="5B" nombre="V. Anuncios. B. Otros anuncios oficiales">
        <departamento codigo="4" nombre="MINISTERIO DE HACIENDA">
          <item><identificador>BOE-B-2026-38437</identificador>
            <titulo>U.R. SUBASTAS ANDALUCÍA 41</titulo>
            <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-38437</url_html></item>
          <item><identificador>BOE-B-2026-38436</identificador>
            <titulo>U.R. SUBASTAS CATALUÑA SUB-AT-2026-99X0000000001</titulo>
            <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-38436</url_html></item>
        </departamento>
      </seccion>
      <seccion codigo="5C" nombre="V. Anuncios. C. Anuncios particulares">
        <departamento codigo="5" nombre="ANUNCIOS PARTICULARES">
          <item><identificador>BOE-B-2026-00010</identificador>
            <titulo>NOTARÍA DE PRUEBA UNO</titulo>
            <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00010</url_html></item>
          <item><identificador>BOE-B-2026-00011</identificador>
            <titulo>NOTARÍA DE PRUEBA DOS</titulo>
            <url_html>https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00011</url_html></item>
        </departamento>
      </seccion>
    </diario>
  </sumario></data>
</response>""".encode()

ANUNCIO_AEAT = ("<html><body><p>Identificador: SUB-AT-2026-26R4186001325</p>"
                "<p>Sevilla, 16 de noviembre de 2026</p></body></html>").encode()


def anuncio_juzgado(valor="120.000,00"):
    return (f"<html><body><p>Identificador de la subasta: SUB-JA-2026-000111</p>"
            f"<p>Valor de subasta: {valor} €</p><p>Tasación: 150.000,00 €</p>"
            f"<p>Localidad: Utrera</p><p>Referencia catastral: 1234567TG1234N0001AB</p>"
            f"<p>Tipo de bien: Vivienda</p></body></html>").encode()


CATASTRO = """<?xml version="1.0" encoding="utf-8"?>
<consulta_municipiero xmlns="http://www.catastro.meh.es/"><municipiero>
<muni><nm>UTRERA</nm></muni><muni><nm>PALACIOS Y VILLAFRANCA (LOS)</nm></muni><muni><nm>SEVILLA</nm></muni>
</municipiero></consulta_municipiero>""".encode()


def csv_justicia(valor333="50000"):
    txt = ("Identificador subasta;Provincia;Tipo de bien;Valor subasta\r\n"
           "SUB-JA-2026-000111;Sevilla;Vivienda;120000\r\n"
           "SUB-JA-2026-000222;Madrid;Garaje;9000\r\n"
           f"SUB-JA-2026-000333;SEVILLA;Local comercial;{valor333}\r\n")
    return txt.encode("cp1252")


class FakeHttp(sema.Http):
    respuestas: dict = {}

    def permitido(self, url):  # simula un robots.txt que bloquea xml.php
        return "xml.php" not in url

    def _descargar(self, url, accept):
        if url in self.respuestas:
            return 200, self.respuestas[url]
        if "datosabiertos/api/boe/sumario/" in url:
            return 404, None
        return 404, None


def respuestas_base():
    return {
        sema.API_SUMARIO.format(fecha="20261121"): SUMARIO,
        "https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-38437": ANUNCIO_AEAT,
        "https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00001": anuncio_juzgado(),
        "https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00002": b"<p>Subasta de parcela</p>",
        "https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00010":
            ('<div id="textoxslt"><p>Anuncio de subasta notarial con número de protocolo 1 / 2026.</p>'
             '<p>Dirección electrónica: https://subastas.boe.es/ds.php?id=SUB-NN-2026-0000001</p>'
             '<p>Sevilla, 19 de octubre de 2026.- El Notario.</p></div><!-- #textoxslt -->').encode(),
        "https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00011": b"<p>Extravio de una poliza.</p>",
        "https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00020":
            "<p>Edicto. Se anuncia la subasta SUB-JA-2026-000444 del inmueble.</p>".encode(),
        "https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00021": b"<p>Edicto de declaracion de concurso.</p>",
        sema.CATASTRO_MUNICIPIOS: CATASTRO,
        sema.CSV_JUSTICIA: csv_justicia(),
    }


class TestReglas(unittest.TestCase):
    def test_motivo_sevilla(self):
        m = {"UTRERA", "LOS PALACIOS Y VILLAFRANCA"}
        self.assertEqual(sema.motivo_sevilla("MINISTERIO", "U.R. SUBASTAS ANDALUCÍA 41", "x", m), "unidad_gestora_41")
        self.assertIsNone(sema.motivo_sevilla("MINISTERIO", "U.R. SUBASTAS ANDALUCIA 29", "x", m))
        self.assertEqual(sema.motivo_sevilla("JUZGADOS", "UTRERA", "Edicto", m), "emisor_municipio:UTRERA")
        largo = ("Resolución de la Tesorería General de la Seguridad Social por la que se anuncia subasta "
                 "de un inmueble en Los Palacios y Villafranca")
        self.assertEqual(sema.motivo_sevilla("X", None, largo, m), "titulo_municipio:LOS PALACIOS Y VILLAFRANCA")
        # Casos reales que daban falso positivo
        self.assertIsNone(sema.motivo_sevilla("TRIBUNALES DE INSTANCIA", None, "HERRERA DUQUE", m | {"HERRERA"}))
        self.assertIsNone(sema.motivo_sevilla("OTROS ENTES", None, "Anuncio de la Comunidad de Regantes Las Pilas "
                                              "sobre convocatoria de Junta.", m | {"PILAS"}))
        self.assertEqual(sema.motivo_sevilla("TRIBUNALES DE INSTANCIA", None, "UTRERA", m), "emisor_municipio:UTRERA")
        self.assertEqual(sema.motivo_sevilla("X", "AYUNTAMIENTO DE SEVILLA", "t", m), "menciona_sevilla")

    def test_secciones_y_subasta(self):
        self.assertTrue(sema.seccion_relevante("4", "IV. Administración de Justicia"))
        self.assertTrue(sema.seccion_relevante("5B", "V. Anuncios. B. Otros anuncios oficiales"))
        self.assertFalse(sema.seccion_relevante("3", "III. Otras disposiciones"))
        self.assertTrue(sema.es_subasta(None, None, "Anuncio de SUBASTA"))
        self.assertTrue(sema.es_subasta(None, None, "Ref. SUB-AT-2026-26R4186001325"))
        self.assertFalse(sema.es_subasta(None, None, "Convocatoria de plazas"))

    def test_importe_y_municipios(self):
        self.assertEqual(sema.importe("120.000,00 €"), 120000.0)
        self.assertEqual(sema.importe("9.500 euros"), 9500.0)
        self.assertIsNone(sema.importe("No consta"))
        n = sema.parse_municipios_catastro(CATASTRO)
        self.assertIn("LOS PALACIOS Y VILLAFRANCA", n)
        self.assertIn("UTRERA", n)

    def test_datos_anuncio_formato_real(self):
        html = ('<div id="textoxslt"><p class="parrafo">Anuncio de subasta judicial en vía de apremio con número '
                'de identificación 4090000006026924.</p><p class="parrafo">Dirección electrónica: '
                'https://subastas.boe.es/ds.php?id=SUB-JA-2026-265153</p><p class="parrafo_2">Sevilla, 24 de julio '
                'de 2026.- El Letrado de la Administración de Justicia.</p></div><!-- #textoxslt --><p>Menú</p>')
        t = sema.texto_de_html(html.encode())
        self.assertNotIn("Menú", t)
        d = sema.datos_anuncio(t)
        self.assertEqual(d["tipo_anuncio"], "judicial en vía de apremio")
        self.assertEqual((d["lugar_firma"], d["fecha_firma"]), ("Sevilla", "24 de julio de 2026"))
        self.assertNotIn("fecha_inicio", sema.extraer_rotulos(
            "Fecha de inicio de la subasta: La subasta se iniciará en la fecha indicada."))

    def test_rotulos(self):
        r = sema.extraer_rotulos("Valor de subasta: 1.000,00 €\nTasación: 2.000 €\nLocalidad: Écija")
        self.assertEqual(r["valor_subasta"], "1.000,00 €")
        self.assertEqual(r["tasacion"], "2.000 €")
        self.assertEqual(r["municipio"], "Écija")


class TestFlujo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.datos = Path(self.tmp.name) / "datos"
        FakeHttp.respuestas = respuestas_base()

    def tearDown(self):
        self.tmp.cleanup()

    def correr(self):
        return sema.ejecutar(FECHA, FECHA, True, self.datos, http_factory=FakeHttp, base=Path(self.tmp.name))

    def db(self):
        con = sqlite3.connect(self.datos / "sema.sqlite")
        con.row_factory = sqlite3.Row
        return con

    def test_ejecucion_completa_idempotente_y_cambios(self):
        r1 = self.correr()
        self.assertEqual(r1["estado"], "ok", r1)
        con = self.db()
        exp = {r["id_subasta"]: r for r in con.execute("SELECT * FROM expedientes")}
        # AEAT Sevilla: ID sacado del texto del anuncio, con la nota de cautela
        self.assertIn("SUB-AT-2026-26R4186001325", exp)
        self.assertEqual(exp["SUB-AT-2026-26R4186001325"]["motivo_sevilla"], "unidad_gestora_41")
        self.assertIn("ubicación del bien no consta", exp["SUB-AT-2026-26R4186001325"]["nota"])
        # Judicial de Utrera: xml.php bloqueado por robots -> se usa el HTML
        self.assertEqual(exp["SUB-JA-2026-000111"]["motivo_sevilla"], "emisor_municipio:UTRERA")
        lote = con.execute("SELECT * FROM lotes WHERE id_subasta='SUB-JA-2026-000111'").fetchone()
        self.assertEqual(lote["valor_subasta"], 120000.0)
        self.assertEqual(lote["tasacion"], 150000.0)
        self.assertIsNone(lote["precio_cierre"])
        inm = con.execute("SELECT * FROM inmuebles WHERE id_subasta='SUB-JA-2026-000111'").fetchone()
        self.assertEqual(inm["municipio"], "Utrera")
        self.assertEqual(inm["referencia_catastral"], "1234567TG1234N0001AB")
        self.assertEqual(exp["SUB-AT-2026-26R4186001325"]["autoridad_gestora"],
                         "MINISTERIO DE HACIENDA · U.R. SUBASTAS ANDALUCÍA 41")
        # Edicto de tribunal de Sevilla con título «SEVILLA»: subasta detectada por el texto
        self.assertIn("SUB-JA-2026-000444", exp)
        self.assertNotIn("BOE-B-2026-00021", {r["id_boe"] for r in con.execute("SELECT id_boe FROM anuncios_boe")})
        # Notaría: subasta detectada por el texto y Sevilla por la mención en el texto
        notaria = con.execute("SELECT motivo_sevilla FROM anuncios_boe WHERE id_boe='BOE-B-2026-00010'").fetchone()
        self.assertEqual(notaria["motivo_sevilla"], "lugar_firma:SEVILLA")
        self.assertEqual(exp["SUB-NN-2026-0000001"]["tipo_anuncio"], "notarial")
        self.assertIsNone(con.execute("SELECT 1 FROM anuncios_boe WHERE id_boe='BOE-B-2026-00011'").fetchone())
        # Justicia: solo Sevilla
        self.assertIn("SUB-JA-2026-000333", exp)
        self.assertEqual(exp["SUB-JA-2026-000333"]["nivel_verificacion"], "A2")
        self.assertNotIn("SUB-JA-2026-000222", exp)
        # Cataluña y sección III fuera
        self.assertNotIn("SUB-AT-2026-99X0000000001", exp)
        ids_anuncios = {r["id_boe"] for r in con.execute("SELECT id_boe FROM anuncios_boe")}
        self.assertNotIn("BOE-A-2026-24414", ids_anuncios)
        self.assertIn("BOE-B-2026-00002", ids_anuncios)  # enajenación sin ID del Portal
        # Trazabilidad: respuestas en bruto con hash
        brutas = con.execute("SELECT COUNT(*) n FROM respuestas_brutas WHERE sha256 IS NOT NULL").fetchone()["n"]
        self.assertGreaterEqual(brutas, 5)
        # Anuncio solo disponible en XML bloqueado por robots: se guarda el dato del sumario y se avisa
        self.assertTrue(any("robots.txt no permite" in a for a in r1["avisos"]))
        self.assertTrue(any("BOE-B-2026-00003" in a for a in r1["avisos"]))
        sin_texto = con.execute("SELECT texto_descargado FROM anuncios_boe WHERE id_boe='BOE-B-2026-00003'").fetchone()
        self.assertEqual(sin_texto["texto_descargado"], 0)
        informe = Path(r1["informe"]).read_text(encoding="utf-8")
        self.assertIn("SUB-AT-2026-26R4186001325", informe)
        self.assertIn("BOE-B-2026-00002", informe)
        self.assertTrue((self.datos / "export" / "expedientes.csv").exists())
        self.assertTrue((self.datos / "mapeo_justicia.md").exists())
        con.close()

        # Segunda ejecución igual: nada nuevo ni cambiado
        r2 = self.correr()
        self.assertEqual((r2["nuevos"], r2["cambios"]), (0, 0), r2)

        # Cambia el valor de subasta en el BOE y una fila del CSV: queda en el histórico
        FakeHttp.respuestas["https://www.boe.es/diario_boe/txt.php?id=BOE-B-2026-00001"] = anuncio_juzgado("110.000,00")
        FakeHttp.respuestas[sema.CSV_JUSTICIA] = csv_justicia("45000")
        r3 = self.correr()
        self.assertGreaterEqual(r3["cambios"], 2, r3)
        con = self.db()
        c = con.execute("SELECT * FROM cambios_historicos WHERE tabla='lotes'").fetchone()
        self.assertEqual((c["campo"], c["valor_anterior"], c["valor_nuevo"]), ("valor_subasta", "120000.0", "110000.0"))
        cj = con.execute("SELECT * FROM cambios_historicos WHERE tabla='justicia_filas'").fetchone()
        self.assertEqual((cj["valor_anterior"], cj["valor_nuevo"]), ("50000", "45000"))
        con.close()

    def test_dia_antiguo_sin_boe_se_cierra_y_reciente_no(self):
        FakeHttp.respuestas = {}  # ningún sumario disponible: la API responde 404
        antiguo = dt.date(2026, 1, 4)
        sema.ejecutar(antiguo, antiguo, False, self.datos, http_factory=FakeHttp, base=Path(self.tmp.name))
        hoy = dt.date.today()
        sema.ejecutar(hoy, hoy, False, self.datos, http_factory=FakeHttp, base=Path(self.tmp.name))
        con = self.db()
        hechos = {r["fecha"]: r["resultado"] for r in con.execute("SELECT * FROM dias_procesados")}
        self.assertEqual(hechos.get(antiguo.isoformat()), "sin_publicacion")
        self.assertNotIn(hoy.isoformat(), hechos)
        con.close()


class TestJusticiaSinProvincia(unittest.TestCase):
    def test_resumen_nacional(self):
        tmp = tempfile.TemporaryDirectory()
        datos = Path(tmp.name) / "datos"
        csv_real = ('"CantidadReclamada";"TipoSubasta";"Procedimiento";"importeSubasta";"Estado";"Fecha_Publicada"\r\n'
                    '"1000";"Apremio";"Ejecucion";"900";"Subasta Finalizada";"01/01/2022"\r\n'
                    '"2000";"Voluntario";"Ejecucion";"1900";"Cancelada";"31/12/2024"\r\n').encode("utf-8")
        FakeHttp.respuestas = {sema.API_SUMARIO.format(fecha="20261121"): SUMARIO, sema.CSV_JUSTICIA: csv_real}
        r = sema.ejecutar(FECHA, FECHA, True, datos, http_factory=FakeHttp, base=Path(tmp.name))
        self.assertEqual(r["justicia_resumen"]["n_filas"], 2)
        self.assertEqual(r["justicia_resumen"]["fecha_publicada_max"], "2024-12-31")
        self.assertIn("no permite saber cuáles son de Sevilla", Path(r["informe"]).read_text(encoding="utf-8"))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

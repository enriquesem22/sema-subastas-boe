"""Pruebas sin red de portal_base.py con fichas REALES del Portal.

tests/fixtures/portal es una copia, tal cual, de lo que descargó la ejecución del 25-11-2026 en GitHub para tres
subastas: SUB-JA-2026-267391 (judicial, 4 lotes, pujas por lote), SUB-JA-2026-257246 (judicial, pujas con importe
solo para usuarios registrados, pestaña «Acreedor») y SUB-AT-2026-26R4186002041 (Agencia Tributaria, un vehículo),
más la captura de códigos de esa ejecución.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portal_base as pb  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "portal"


class PortalBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name) / "subastas.sqlite"
        cls.resumen = pb.construir(FIX, cls.base)
        cls.panel = pb.exportar_panel(cls.base, Path(cls.tmp.name) / "panel")
        cls.con = sqlite3.connect(cls.base)
        cls.con.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def fila(self, sql, *args):
        return self.con.execute(sql, args).fetchone()

    def test_resumen(self):
        self.assertEqual(self.resumen["subastas"], 3)
        self.assertEqual(self.resumen["capturas"], 1)
        self.assertEqual(self.fila("SELECT count(*) FROM subastas WHERE activa=1")[0], 3)

    def test_subasta_con_lotes(self):
        s = self.fila("SELECT * FROM subastas WHERE sub='SUB-JA-2026-267391'")
        self.assertEqual((s["grupo"], s["tipo_subasta"], s["n_lotes"]), ("Judicial", "JUDICIAL EN VÍA DE APREMIO", 4))
        self.assertIsNone(s["valor_subasta"])  # el Portal remite a cada lote
        self.assertTrue(s["valor_subasta_texto"].startswith("Ver valor de subasta en cada lote"))
        self.assertEqual((s["valor_lotes_min"], s["valor_lotes_max"]), (200200.0, 210900.0))
        self.assertEqual((s["municipios"], s["municipios_ine"], s["es_inmueble"]), ("Mairena del Alcor", "41058", 1))
        self.assertEqual(s["fecha_conclusion"], "2026-11-30T18:00:00+01:00")
        self.assertEqual(s["provincias_busqueda"], "41")
        self.assertEqual(s["cantidad_reclamada"], 500951.66)
        l2 = self.fila("SELECT * FROM lotes WHERE sub='SUB-JA-2026-267391' AND lote='2'")
        self.assertEqual((l2["valor_subasta"], l2["deposito"], l2["puja_minima_texto"], l2["puja"]),
                         (204200.0, 10210.0, "Sin puja mínima", "Sin puja"))
        b = self.fila("SELECT * FROM bienes WHERE sub='SUB-JA-2026-267391' AND lote='1' AND bien=1")
        self.assertEqual((b["clase"], b["subtipo"], b["referencia_catastral"], b["codigo_postal"]),
                         ("Inmueble", "Vivienda", "6297019TG5369N0002YX", "41510"))

    def test_pujas_ocultas_y_acreedor(self):
        s = self.fila("SELECT * FROM subastas WHERE sub='SUB-JA-2026-257246'")
        self.assertEqual(s["con_pujas"], 1)
        self.assertIn("debe acceder como usuario registrado", s["pujas"])
        self.assertEqual(s["valor_subasta"], 222958.0)
        self.assertEqual(s["municipios_ine"], "41005")  # «ALCALÁ DEL RIO» → Alcalá del Río
        # la pestaña del acreedor está en la base, pero no va al panel
        self.assertGreater(self.fila("SELECT count(*) FROM campos WHERE sub='SUB-JA-2026-257246' AND pestana='4'")[0], 0)
        doc = json.loads((Path(self.tmp.name) / "panel" / "sema-panel-sp_campos.json").read_text())
        self.assertNotIn("4", {r[doc["columns"].index("pestana")] for r in doc["rows"]})

    def test_vehiculo(self):
        s = self.fila("SELECT * FROM subastas WHERE sub='SUB-AT-2026-26R4186002041'")
        self.assertEqual((s["grupo"], s["es_inmueble"], s["clases_bien"], s["municipios"]),
                         ("Agencia Tributaria", 0, "Vehículo", None))
        self.assertEqual(s["acceso_restringido"], 1)
        b = self.fila("SELECT * FROM bienes WHERE sub='SUB-AT-2026-26R4186002041'")
        self.assertEqual((b["marca"], b["subtipo"]), ("MAZDA", "Turismos"))

    def test_cada_dato_dice_de_donde_sale(self):
        sin_origen = self.fila("SELECT count(*) FROM campos WHERE archivo IS NULL OR url NOT LIKE 'https://subastas.boe.es/%'")[0]
        self.assertEqual(sin_origen, 0)
        for t, n in self.panel.items():
            self.assertGreater(n["filas"], 0, t)

    def test_copia_comprimida_de_la_base(self):
        import gzip
        a = pb.copia_comprimida(self.base, Path(self.tmp.name) / "a.sqlite.gz")
        b = pb.copia_comprimida(self.base, Path(self.tmp.name) / "b.sqlite.gz")
        self.assertEqual(a["sha256"], b["sha256"])  # mismo contenido, mismo archivo
        restaurada = Path(self.tmp.name) / "restaurada.sqlite"
        restaurada.write_bytes(gzip.decompress(Path(a["archivo"]).read_bytes()))
        con = sqlite3.connect(restaurada)
        self.assertEqual(con.execute("SELECT count(*) FROM subastas").fetchone()[0], 3)
        con.close()

    def test_importes_y_municipios(self):
        self.assertEqual(pb.importe("1.234,56 €"), 1234.56)
        self.assertEqual(pb.importe("0,00 €"), 0.0)
        self.assertIsNone(pb.importe("Sin puja mínima"))
        self.assertIsNone(pb.importe("No consta"))
        geo = pb.Municipios()
        self.assertEqual(geo.buscar("RINCONADA (LA)", "Sevilla"), ("41081", "La Rinconada"))
        self.assertEqual(geo.buscar("sevilla", "Sevilla")[0], "41091")
        self.assertEqual(geo.buscar("ISLA CRISTINA", "Huelva")[0], "21042")
        self.assertEqual(geo.buscar("Utrera", None), (None, None))


class DosProvinciasTest(unittest.TestCase):
    """Una búsqueda de Huelva (21) posterior que también trae una subasta de la de Sevilla (41): las filas de Sevilla
    no cambian y el historial de la subasta compartida sigue el orden de fecha, no el de provincia."""

    def test_busquedas_por_fecha(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp) / "portal"
            shutil.copytree(FIX, raiz)
            sevilla = json.loads((raiz / "codigos" / "41" / "20261125_200830" / "listado.json").read_text(encoding="utf-8"))
            huelva = dict(sevilla, fecha_captura="2026-11-26T22:06:31+01:00", provincia_bien="Huelva", cod_provincia="21",
                          total_resultados=1, subastas=[{"sub": "SUB-JA-2026-267391"}])
            (raiz / "codigos" / "21" / "20261126_220631").mkdir(parents=True)
            (raiz / "codigos" / "21" / "20261126_220631" / "listado.json").write_text(json.dumps(huelva), encoding="utf-8")
            solo = Path(tmp) / "solo_sevilla.sqlite"
            pb.construir(FIX, solo)
            dos = Path(tmp) / "dos.sqlite"
            pb.construir(raiz, dos)
            a, b = sqlite3.connect(solo), sqlite3.connect(dos)
            q = "SELECT * FROM subastas WHERE sub <> 'SUB-JA-2026-267391' ORDER BY sub"
            self.assertEqual(a.execute(q).fetchall(), b.execute(q).fetchall())
            for t in ("lotes", "bienes", "campos"):
                self.assertEqual(a.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall(),
                                 b.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall())
            s = b.execute("SELECT primera_vez, ultima_vez, vista_en, provincias_busqueda, activa FROM subastas "
                          "WHERE sub = 'SUB-JA-2026-267391'").fetchone()
            self.assertEqual(s, ("41/20261125_200830", "21/20261126_220631", "41/20261125_200830,21/20261126_220631",
                                 "41,21", 1))
            self.assertEqual(b.execute("SELECT count(*) FROM capturas WHERE ultima = 1").fetchone()[0], 2)
            a.close()
            b.close()


if __name__ == "__main__":
    unittest.main()

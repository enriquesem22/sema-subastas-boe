"""Pruebas sin red de la carga de Subastech.

Los lotes de prueba son SINTÉTICOS: siguen la forma de las respuestas del conector de Subastech
(buscar_subastas / resultados_recientes), pero no son subastas reales.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["SEMA_PAUSA"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sema  # noqa: E402


def item(**k):
    base = {"id": "AAAAAAAAA1", "type": "Vivienda", "title": "Calle Prueba", "address": "Calle Prueba",
            "municipio": "Utrera", "provincia": "Sevilla", "cp": "41710", "ref": "SUB-JA-2026-000001",
            "municipalitySlug": "utrera", "end": "2026-11-30T17:00:00.000Z", "daysLeft": 6,
            "valueAuction": 100000.0, "valueM2": 1000.0, "minimumBid": None, "deposit": 5000.0, "appraisal": 0,
            "occupied": None, "discount": None, "m2": 100, "status": "Abierta", "lat": 37.18, "lon": -5.78,
            "lot": None, "finalBid": None, "resultStatus": None, "closedAt": None, "phase": "activa",
            "url": "https://app.subastech.com/subasta/sevilla/utrera/prueba-AAAAAAAAA1", "fuente": "BOE"}
    base.update(k)
    return base


def linea_uso(uid, herramienta, args, ts):
    return json.dumps({"timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": uid, "name": f"mcp__Subastech__{herramienta}", "input": args}]}})


def linea_resultado(uid, datos, ts):
    return json.dumps({"timestamp": ts, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": uid, "content": [{"type": "text", "text": json.dumps(datos)}]}]}})


class SubastechTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def transcripcion(self, nombre, eventos):
        ruta = self.dir / nombre
        lineas = []
        for uid, herramienta, args, datos, ts in eventos:
            lineas += [linea_uso(uid, herramienta, args, ts), linea_resultado(uid, datos, ts)]
        lineas.append(json.dumps({"timestamp": "x", "message": {"content": [
            {"type": "tool_use", "id": "otra", "name": "Bash", "input": {}}]}}))  # se ignora
        ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        return ruta

    def db(self):
        con = sqlite3.connect(self.dir / "datos" / "subastech.sqlite")
        con.row_factory = sqlite3.Row
        return con

    def test_mapeo_de_un_lote(self):
        v = sema.lote_subastech(item(status="Pròxima subhasta", lot={"n": 2, "total": 4}, occupied=True,
                                     ref="TGSS-410818000925", url="javascript:alert(1)"))
        self.assertEqual(v["estado"], "Próxima")
        self.assertEqual((v["n_lote"], v["total_lotes"]), (2, 4))
        self.assertEqual(v["origen"], "Seguridad Social")
        self.assertEqual(v["ocupada"], "si")
        self.assertIsNone(v["url_ficha"])  # solo se guardan enlaces https
        self.assertEqual(v["tasacion"], 0.0)  # el 0 de la fuente se guarda tal cual, no se inventa
        self.assertEqual(sema.origen_por_referencia("SUB-AT-2026-26R4186001465"), "AEAT")
        self.assertEqual(sema.origen_por_referencia("SUB-RC-2026-4107100105"), "Otras administraciones")

    def test_carga_cierre_e_historial(self):
        args_act = {"provincia": ["sevilla"], "estado": "activas", "por_pagina": 20, "pagina": 1}
        args_adj = {"provincia": ["sevilla"], "estado": "adjudicadas", "por_pagina": 20, "pagina": 1}
        t = self.transcripcion("s1.jsonl", [
            ("u1", "buscar_subastas", args_act,
             {"items": [item(), item(id="BBBBBBBBB2", ref="SUB-AT-2026-1", municipio="Camas")], "total": 2,
              "paginas": 1}, "2026-11-21T10:00:00.000Z"),
            ("u2", "buscar_subastas", args_adj,
             {"items": [item(status="Adjudicada", phase="historica", finalBid=80000.0, resultStatus="con_pujas",
                             closedAt="2026-11-30T18:00:00.000Z", daysLeft=None)], "total": 408, "paginas": 21},
             "2026-12-01T10:00:00.000Z"),
        ])
        res = sema.ejecutar_subastech([t], False, self.dir / "datos")
        self.assertEqual(res["estado"], "ok")
        self.assertEqual((res["respuestas"], res["lotes_nuevos"]), (2, 2))
        con = self.db()
        r = con.execute("SELECT * FROM subastech_lotes WHERE id_subastech='AAAAAAAAA1'").fetchone()
        self.assertEqual((r["estado"], r["precio_cierre"], r["nivel_verificacion"]), ("Adjudicada", 80000.0, "C"))
        cambios = {(c["campo"], c["valor_anterior"], c["valor_nuevo"]) for c in
                   con.execute("SELECT * FROM cambios_historicos WHERE clave='AAAAAAAAA1'")}
        self.assertIn(("estado", "Abierta", "Adjudicada"), cambios)
        v = con.execute("SELECT pct_sobre_valor FROM v_cierres WHERE id_subastech='AAAAAAAAA1'").fetchone()
        self.assertEqual(v["pct_sobre_valor"], 80.0)
        z = con.execute("SELECT * FROM v_zonas WHERE municipio='Utrera'").fetchone()
        self.assertEqual((z["subastas"], z["fichas"], z["adjudicadas"]), (1, 1, 1))
        # Cada respuesta queda en bruto con su hash y se puede reconstruir la base desde los brutos.
        brutos = list((self.dir / "datos" / "brutos" / "subastech").rglob("*.json"))
        self.assertEqual(len(brutos), 2)
        guardado = json.loads(brutos[0].read_text(encoding="utf-8"))
        self.assertEqual(sema.sha256(guardado["respuesta"].encode("utf-8")), guardado["sha256_respuesta"])
        informe = (self.dir / "datos" / "ultimo_informe_subastech.md").read_text(encoding="utf-8")
        self.assertIn("Cierres incorporados en esta carga: 1", informe)
        self.assertIn("https://www.boe.es", informe)
        con.close()
        # Repetir la carga no duplica nada.
        res2 = sema.ejecutar_subastech([t], False, self.dir / "datos")
        self.assertEqual((res2["respuestas"], res2["ya_cargadas"], res2["cambios"]), (0, 2, 0))

    def test_respuesta_antigua_no_deshace_datos_nuevos(self):
        args = {"provincia": ["sevilla"], "estado": "adjudicadas", "por_pagina": 20}
        nueva = self.transcripcion("nueva.jsonl", [
            ("n1", "buscar_subastas", args, {"items": [item(status="Adjudicada", phase="historica",
                                                            finalBid=90000.0)], "total": 1},
             "2026-12-02T10:00:00.000Z")])
        sema.ejecutar_subastech([nueva], False, self.dir / "datos")
        vieja = self.transcripcion("vieja.jsonl", [
            ("v1", "buscar_subastas", {"provincia": ["sevilla"], "estado": "activas"},
             {"items": [item()], "total": 1}, "2026-11-20T10:00:00.000Z")])
        sema.ejecutar_subastech([vieja], False, self.dir / "datos")
        con = self.db()
        r = con.execute("SELECT estado, fase FROM subastech_lotes").fetchone()
        self.assertEqual((r["estado"], r["fase"]), ("Adjudicada", "historica"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM cambios_historicos").fetchone()[0], 0)
        con.close()

    def test_recargar_desde_brutos(self):
        t = self.transcripcion("s.jsonl", [
            ("r1", "resultados_recientes", {"provincia": "sevilla", "limite": 15},
             {"items": [item(status="Desierta", phase="historica", resultStatus="sin_pujas")]},
             "2026-11-24T09:00:00.000Z")])
        sema.ejecutar_subastech([t], False, self.dir / "datos")
        otra = self.dir / "otra"
        (otra / "brutos").mkdir(parents=True)
        os.rename(self.dir / "datos" / "brutos" / "subastech", otra / "brutos" / "subastech")
        res = sema.ejecutar_subastech([], True, otra)
        self.assertEqual((res["respuestas"], res["lotes_nuevos"]), (1, 1))

    def test_fincas_de_un_mismo_lote_cuentan_como_uno(self):
        a = {"provincia": ["sevilla"], "estado": "adjudicadas", "por_pagina": 20, "pagina": 1}
        repetida = dict(ref="SUB-AT-2026-9", status="Adjudicada", phase="historica", valueAuction=706280.41,
                        finalBid=575628.04, municipio="La Roda de Andalucía")
        t = self.transcripcion("s.jsonl", [
            ("g1", "buscar_subastas", a, {"items": [item(id=f"ROD{i:07d}", **repetida) for i in range(5)]
                                          + [item(id="OTRO000001", ref="SUB-JA-2026-7", status="Adjudicada",
                                                  phase="historica", finalBid=50000.0)], "total": 6},
             "2026-11-24T10:00:00.000Z")])
        sema.ejecutar_subastech([t], False, self.dir / "datos")
        con = self.db()
        unidades = sema.agrupar_lotes(con.execute("SELECT * FROM subastech_lotes").fetchall())
        self.assertEqual(sorted(n for _, n in unidades), [1, 5])
        f = con.execute("SELECT MAX(fichas_mismo_lote) FROM v_cierres WHERE id_subasta='SUB-AT-2026-9'").fetchone()[0]
        self.assertEqual(f, 5)
        informe = (self.dir / "datos" / "ultimo_informe_subastech.md").read_text(encoding="utf-8")
        self.assertIn("6 fichas de Subastech → 2 lotes", informe)
        self.assertIn("(5 fincas)", informe)
        con.close()

    def test_activo_que_desaparece_del_listado(self):
        a = {"provincia": ["sevilla"], "estado": "activas", "por_pagina": 20, "pagina": 1}
        t = self.transcripcion("s.jsonl", [
            ("d1", "buscar_subastas", a, {"items": [item(), item(id="BBBBBBBBB2", ref="TGSS-1")], "total": 2},
             "2026-11-21T10:00:00.000Z"),
            ("d2", "buscar_subastas", a, {"items": [item()], "total": 1}, "2026-11-24T10:00:00.000Z")])
        sema.ejecutar_subastech([t], False, self.dir / "datos")
        con = self.db()
        self.assertEqual([r["id_subastech"] for r in sema.activos_no_listados(con)], ["BBBBBBBBB2"])
        plan = [x for x in sema.plan_subastech(con) if x["herramienta"] == "buscar_por_codigo"]
        self.assertEqual(plan[0]["argumentos"], {"codigos": ["TGSS-1"]})
        informe = (self.dir / "datos" / "ultimo_informe_subastech.md").read_text(encoding="utf-8")
        self.assertIn("ya no salen en el último listado", informe)
        con.close()

    def test_plan_pide_el_historico_que_falta(self):
        con = sema.conectar(self.dir / "s.sqlite", sema.SUBASTECH_ESQUEMA)
        self.assertEqual([p["argumentos"].get("estado") for p in sema.plan_subastech(con)],
                         ["activas", None, "adjudicadas", "desiertas"])
        for estado, total in (("activas", 50), ("adjudicadas", 408), ("desiertas", 2)):
            con.execute("INSERT INTO subastech_respuestas (id_llamada, herramienta, estado_consulta, total, "
                        "momento_consulta) VALUES (?,?,?,?,?)", (estado, "buscar_subastas", estado, total, "2026"))
        plan = sema.plan_subastech(con)
        paginas = lambda e: [p["argumentos"]["pagina"] for p in plan if p["argumentos"].get("estado") == e]  # noqa
        self.assertEqual(paginas("activas"), [1, 2, 3])
        self.assertEqual(paginas("adjudicadas"), list(range(1, 22)))
        self.assertEqual(paginas("desiertas"), [1, 2])
        con.close()


if __name__ == "__main__":
    unittest.main()

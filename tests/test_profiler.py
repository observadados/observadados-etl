"""
Testes do profiler v2 (contrato em docs/profile-schema.md).

    python -m unittest discover -s tests -v

Os testes com os CSVs grandes de MG só rodam se os arquivos existirem
(variável OBSERVADADOS_MG_DIR ou o caminho padrão abaixo).
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.profiler import MAX_JSON_BYTES, build_profile, profile_file  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
MG_DIR = Path(os.environ.get("OBSERVADADOS_MG_DIR", r"C:\Users\andre\www\observadados\app\api"))

TYPES = {"integer", "numeric", "categorical", "text", "date", "datetime", "boolean", "empty"}
COMMON = {"name", "position", "type", "missing", "missing_pct", "unique", "unique_is_estimate", "description"}


def validate_v2(test: unittest.TestCase, p: dict) -> None:
    """Validação estrutural do perfil v2 segundo o contrato."""
    test.assertEqual(p["schema_version"], 2)
    for k in ("generated_at", "generator", "file", "rows", "rows_exact", "columns",
              "profiled_rows", "sampled", "sample", "fields", "warnings"):
        test.assertIn(k, p)
    test.assertEqual(set(p["file"]), {"name", "format", "size_bytes", "encoding", "delimiter", "sheet"})
    test.assertIn(p["file"]["format"], {"csv", "xlsx", "xls"})
    test.assertEqual(len(p["fields"]), p["columns"])
    test.assertEqual(p["sample"]["columns"], [f["name"] for f in p["fields"]])
    test.assertLessEqual(len(p["sample"]["rows"]), 100)
    for row in p["sample"]["rows"]:
        test.assertEqual(len(row), p["columns"])
        for v in row:
            test.assertTrue(v is None or (isinstance(v, str) and len(v) <= 200))
    for i, f in enumerate(p["fields"]):
        test.assertTrue(COMMON <= set(f), f)
        test.assertEqual(f["position"], i)
        test.assertIn(f["type"], TYPES)
        test.assertTrue(0 <= f["missing_pct"] <= 100)
        t = f["type"]
        if t in ("integer", "numeric"):
            test.assertEqual(set(f["stats"]), {"min", "max", "mean", "median", "std", "q1", "q3"})
            for v in f["stats"].values():
                test.assertIsInstance(v, (int, float))
            h = f["histogram"]
            test.assertEqual(len(h["bins"]), len(h["counts"]) + 1)
            test.assertLessEqual(len(h["counts"]), 20)
            test.assertEqual(sum(h["counts"]), p["profiled_rows"] - f["missing"] - _ignored(p, f))
        if t in ("categorical", "boolean", "text"):
            test.assertLessEqual(len(f["top"]), 10)
            counts = [x["count"] for x in f["top"]]
            test.assertEqual(counts, sorted(counts, reverse=True))
            test.assertIn("other_pct", f)
        if t == "text":
            test.assertEqual(set(f["length"]), {"min", "max", "mean"})
        if t in ("date", "datetime"):
            test.assertEqual(set(f["range"]), {"min", "max"})
            tl = f["timeline"]
            test.assertIn(tl["granularity"], {"year", "month"})
            test.assertEqual(len(tl["labels"]), len(tl["counts"]))
        if t == "empty":
            test.assertEqual(set(f), COMMON)
    raw = json.dumps(p, ensure_ascii=False, allow_nan=False)
    test.assertLessEqual(len(raw.encode("utf-8")), MAX_JSON_BYTES)


def _ignored(p: dict, f: dict) -> int:
    for w in p["warnings"]:
        if w.startswith(f"Coluna {f['name']}: ") and "não numéricos" in w:
            return int(w.split(": ")[1].split()[0])
    return 0


class ProfilerSmallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        p = self.dir / name
        p.write_bytes(text.encode(encoding))
        return p

    def test_contract_example(self):
        p = build_profile(self.write(
            "exemplo.csv",
            "data_fato;municipio;qtde\n2024-01-03;Belo Horizonte;2\n2024-02-10;Contagem;1\n2025-03-01;;5\n",
        ))
        validate_v2(self, p)
        self.assertEqual((p["rows"], p["columns"], p["file"]["delimiter"]), (3, 3, ";"))
        d, m, q = p["fields"]
        self.assertEqual(d["type"], "date")
        self.assertEqual(d["range"], {"min": "2024-01-03", "max": "2025-03-01"})
        self.assertEqual(d["timeline"]["granularity"], "month")
        self.assertEqual(m["type"], "categorical")
        self.assertEqual((m["missing"], m["missing_pct"], m["unique"]), (1, 33.33, 2))
        self.assertEqual([t["pct"] for t in m["top"]], [50, 50])
        self.assertEqual(m["other_pct"], 0)
        self.assertEqual(q["type"], "integer")
        s = q["stats"]
        self.assertEqual((s["min"], s["max"], s["median"], s["q1"], s["q3"]), (1, 5, 2, 1.5, 3.5))
        self.assertAlmostEqual(s["mean"], 2.67, places=2)
        self.assertAlmostEqual(s["std"], 2.08, places=2)
        self.assertEqual(p["sample"]["rows"][2], ["2025-03-01", None, "5"])
        self.assertEqual(p["warnings"], ["Coluna municipio tem 33,33% de valores ausentes"])

    def test_brazilian_numbers_and_cp1252(self):
        body = "Município;Valor;Taxa\n" + "".join(
            f"São João {i % 3};{i}.234,5{i % 10};{i},5\n" for i in range(1, 300)
        )
        p = build_profile(self.write("br.csv", body, encoding="cp1252"))
        validate_v2(self, p)
        self.assertEqual(p["file"]["encoding"], "cp1252")
        self.assertEqual(p["fields"][0]["name"], "Município")
        self.assertEqual(p["fields"][0]["top"][0]["value"][:8], "São João")
        valor, taxa = p["fields"][1], p["fields"][2]
        self.assertEqual(valor["type"], "numeric")
        self.assertAlmostEqual(valor["stats"]["min"], 1234.51, places=2)
        self.assertEqual(taxa["type"], "numeric")
        self.assertEqual(taxa["stats"]["max"], 299.5)

    def test_types_bool_empty_text_datetime(self):
        rows = "".join(
            f"{'SIM' if i % 2 else 'NÃO'};;descrição livre número {i};"
            f"{(i % 28) + 1:02d}/01/2024 {i % 24:02d}:15\n"
            for i in range(200)
        )
        p = build_profile(self.write("t.csv", "flag;vazio;obs;quando\n" + rows))
        validate_v2(self, p)
        types = [f["type"] for f in p["fields"]]
        self.assertEqual(types, ["boolean", "empty", "text", "datetime"])
        self.assertIn("Coluna vazio está totalmente vazia", p["warnings"])
        self.assertEqual(p["fields"][3]["range"]["min"], "2024-01-01T00:15:00")

    def test_mixed_date_formats(self):
        rows = "".join(f"{(i % 28) + 1:02d}/03/2022{' 00:00' if i % 3 else ''}\n" for i in range(100))
        p = build_profile(self.write("d.csv", "Data Fato\n" + rows))
        self.assertEqual(p["fields"][0]["type"], "date")

    def test_size_limit_wide_file(self):
        cols = 400
        header = ";".join(f"coluna_com_nome_longo_{i}" for i in range(cols))
        body = "".join(";".join(f"valor textual {r}-{c} " * 8 for c in range(cols)) + "\n" for r in range(120))
        p = build_profile(self.write("wide.csv", header + "\n" + body))
        validate_v2(self, p)

    def test_profile_file_error_returns_empty_json(self):
        self.assertEqual(profile_file(self.dir / "nao-existe.csv"), "{}")


class FixtureTests(unittest.TestCase):
    def test_fixtures_are_valid_v2(self):
        files = sorted(FIXTURES.glob("*.json"))
        self.assertTrue(files, "nenhuma fixture em tests/fixtures")
        for f in files:
            with self.subTest(fixture=f.name):
                validate_v2(self, json.loads(f.read_text(encoding="utf-8")))


@unittest.skipUnless(MG_DIR.exists(), f"CSVs de MG não encontrados em {MG_DIR}")
class MinasGeraisLargeFiles(unittest.TestCase):
    def _run(self, name: str) -> dict:
        path = MG_DIR / name
        if not path.exists():
            self.skipTest(f"{name} ausente")
        t = time.perf_counter()
        p = build_profile(path)
        print(f"\n  {name}: {p['rows']} linhas em {time.perf_counter() - t:.1f}s", file=sys.stderr)
        validate_v2(self, p)
        self.assertFalse(p["sampled"])
        return p

    def test_crimes_violentos_2022_2025(self):
        p = self._run("Crimes Violentos - Jan 2022 a Nov 2025.csv")
        self.assertEqual(p["file"]["encoding"], "cp1252")
        by = {f["name"]: f for f in p["fields"]}
        self.assertEqual(by["Data Fato"]["type"], "date")
        self.assertEqual(by["Ano Fato"]["type"], "integer")
        self.assertEqual(by["Natureza Principal Completa"]["type"], "categorical")

    def test_banco_crimes_violentos_2012_2018(self):
        p = self._run("Banco Crimes Violentos 2012 a 2018 - Atualizado Novembro 2025.csv")
        self.assertEqual(p["file"]["encoding"], "utf-8")
        self.assertEqual(p["fields"][0]["name"], "Registros")  # BOM removido
        self.assertGreater(p["rows"], 900_000)


if __name__ == "__main__":
    unittest.main()

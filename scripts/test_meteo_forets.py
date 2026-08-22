#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "generer_meteo_forets",
    HERE / "generer_meteo_forets.py"
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


class MeteoForetsTests(unittest.TestCase):
    def test_normalize_codes(self):
        self.assertEqual(module.normalize_dep_code("1"), "01")
        self.assertEqual(module.normalize_dep_code("09"), "09")
        self.assertEqual(module.normalize_dep_code("2a"), "2A")
        self.assertEqual(module.normalize_dep_code("2B"), "2B")
        self.assertEqual(module.normalize_dep_code("95"), "95")

    def test_parse_reference_time_local(self):
        dt = module.parse_reference_time("2026-08-21 17:00:00")
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M"), "2026-08-21 17:00")
        self.assertIsNotNone(dt.utcoffset())

    def test_parse_archive_and_reemission(self):
        csv_text = """Reference_time;dep_code;niveau_j1;niveau_j2;nom_dep
2026-08-20T17:00:00+02:00;59;2;3;Nord
2026-08-20T17:00:00+02:00;62;1;2;Pas-de-Calais
2026-08-20T17:00:00+02:00;2A;1;1;Corse-du-Sud
2026-08-20T17:00:00+02:00;2B;1;1;Haute-Corse
2026-08-21T17:00:00+02:00;59;3;4;Nord
2026-08-21T17:00:00+02:00;62;2;3;Pas-de-Calais
2026-08-21T17:00:00+02:00;2A;1;2;Corse-du-Sud
2026-08-21T17:00:00+02:00;2B;1;2;Haute-Corse
2026-08-21T18:00:00+02:00;59;4;4;Nord
2026-08-21T18:00:00+02:00;62;2;3;Pas-de-Calais
2026-08-21T18:00:00+02:00;2A;1;2;Corse-du-Sud
2026-08-21T18:00:00+02:00;2B;1;2;Haute-Corse
"""

        latest, history, archive = module.parse_archive(csv_text)

        self.assertEqual(latest["bulletin_date"], "2026-08-21")
        self.assertEqual(len(history), 2)
        self.assertEqual(len(archive), 2)

        nord = next(
            department
            for department in latest["departments"]
            if department["code"] == "59"
        )

        self.assertEqual(nord["j1"], 4)
        self.assertEqual(nord["j2"], 4)

        self.assertEqual(latest["counts"]["j1"]["4"], 1)
        self.assertEqual(latest["counts"]["j2"]["4"], 1)

    def test_choose_csv_urls_has_fallbacks(self):
        urls = module.choose_csv_urls({"resources": []}, now_year=2026)
        self.assertIn("mdf_2026.csv.gz", urls[0])
        self.assertTrue(any("mdf_2025.csv.gz" in url for url in urls))

    def test_header_variants(self):
        csv_text = """Reference Time,code_dep,j1,j2,nom_departement
2026-08-21T17:00:00+02:00,59,2,3,Nord
2026-08-21T17:00:00+02:00,62,1,2,Pas-de-Calais
2026-08-21T17:00:00+02:00,2A,1,1,Corse-du-Sud
2026-08-21T17:00:00+02:00,2B,1,1,Haute-Corse
"""
        latest, history, archive = module.parse_archive(csv_text)
        self.assertEqual(latest["bulletin_date"], "2026-08-21")
        self.assertEqual(len(latest["departments"]), 4)

    def test_decode_utf16_bom(self):
        raw = "Reference_time;dep_code;niveau_j1;niveau_j2;nom_dep\\n".encode("utf-16")
        text = module.decode_text_content(raw)
        self.assertIn("Reference_time", text)


if __name__ == "__main__":
    unittest.main()

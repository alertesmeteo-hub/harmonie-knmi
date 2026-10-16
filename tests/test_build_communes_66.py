#!/usr/bin/env python3
"""Tests de build_communes_66.py : statistiques d'ensemble jour par jour membre par membre. Aucun appel réseau."""

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_communes_66.py"
SPEC = importlib.util.spec_from_file_location("build_communes_66", MODULE_PATH)
b = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(b)

UTC = timezone.utc
# 16 oct. 2026, 12 h UTC : le jour 0 (heure de Paris) finit le 16 à 22 h UTC, le jour 1 le 17 à 22 h UTC.
MAINTENANT = datetime(2026, 10, 16, 12, tzinfo=UTC)
TEMPS = [MAINTENANT + timedelta(hours=6 * k) for k in range(7)]  # 16 12 h … 18 00 h UTC


def membre(cumuls: list[float], temp: float) -> "b.Serie":
    return b.Serie(TEMPS, {"total": cumuls, "temp": [temp] * len(TEMPS)})


def ensemble_piege() -> list:
    """10 membres : 8 donnent 3 mm le 17 ; 2 ont donné 50 mm le 16 (avant la journée du 17) et rien le 17."""
    secs_le_16 = [membre([0, 0, 0, 3, 3, 3, 3], 10 + i) for i in range(8)]
    arroses_le_16 = [membre([0, 50, 50, 50, 50, 50, 50], 18 + i) for i in range(2)]
    return secs_le_16 + arroses_le_16


class Quantiles(unittest.TestCase):
    def test_interpolation_lineaire_comme_numpy(self):
        self.assertAlmostEqual(b.quantile(list(range(51)), 0.1), 5.0)
        self.assertAlmostEqual(b.quantile(list(range(51)), 0.9), 45.0)
        self.assertAlmostEqual(b.quantile([2.0, 1.0], 0.9), 1.9)
        self.assertEqual(b.quantile([7.0], 0.1), 7.0)


class StatistiquesJourParJour(unittest.TestCase):
    def setUp(self):
        self.jours = b.bornes_jours(MAINTENANT)
        self.par_membre = [b.quotidien(s, self.jours, MAINTENANT) for s in ensemble_piege()]
        self.stats = b.stats_membres(self.par_membre)

    def test_p90_de_la_pluie_du_jour_et_non_difference_des_p90_des_cumuls(self):
        # l'ancien calcul (quantile des cumuls, puis différence) : 0 mm au p90 le 17, alors que 8 membres sur 10 donnent 3 mm
        p90_cumul = b.Serie(TEMPS, {"total": [b.quantile([s.total[k] for s in ensemble_piege()], 0.9) for k in range(len(TEMPS))]})
        self.assertEqual(b.quotidien(p90_cumul, self.jours, MAINTENANT)["pluie"][1], 0.0)
        # jour par jour membre par membre : [3 × 8, 0, 0]
        self.assertEqual(self.stats["p90"]["pluie"][1], 3.0)
        self.assertEqual(self.stats["p10"]["pluie"][1], 0.0)
        self.assertEqual(self.stats["moyenne"]["pluie"][1], 2.4)
        # la veille (jour 0, de 12 h UTC à minuit heure de Paris) : 2 membres sur 10 à 50 mm
        self.assertEqual(self.stats["p90"]["pluie"][0], 50.0)
        self.assertEqual(self.stats["moyenne"]["pluie"][0], 10.0)

    def test_tmax_tmin_par_membre(self):
        # températures constantes 10 à 19 °C : Tmax du 17 = celle de chaque membre
        self.assertEqual(self.stats["moyenne"]["tmax"][1], 14.5)
        self.assertEqual(self.stats["p10"]["tmax"][1], 10.9)
        self.assertEqual(self.stats["p90"]["tmax"][1], 18.1)
        self.assertEqual(self.stats["p10"]["tmin"][1], 10.9)

    def test_jour_hors_echeance_ou_sans_rafale(self):
        # au-delà des données (jour 2 et suivants) et pour la rafale (absente) : pas de statistique
        for k in ("moyenne", "p10", "p90"):
            self.assertTrue(all(v is None for v in self.stats[k]["pluie"][2:]))
            self.assertTrue(all(v is None for v in self.stats[k]["rafale"]))

    def test_un_membre_sans_valeur_pas_de_statistique(self):
        par_membre = [dict(q, pluie=list(q["pluie"])) for q in self.par_membre]
        par_membre[3]["pluie"][1] = None
        self.assertIsNone(b.stats_membres(par_membre)["p90"]["pluie"][1])

    def test_rafale_en_km_h_entiers(self):
        par_membre = [{"pluie": [None], "tmax": [None], "tmin": [None], "rafale": [60 + i]} for i in range(10)]
        stats = b.stats_membres(par_membre)
        self.assertEqual(stats["p90"]["rafale"], [68])
        self.assertIsInstance(stats["p90"]["rafale"][0], int)


class LectureMembres(unittest.TestCase):
    def reponse(self, statut, contenu=None):
        class R:
            status_code = statut

            def raise_for_status(self):
                if statut >= 400:
                    raise RuntimeError(f"HTTP {statut}")

            def json(self):
                return contenu

        return R()

    def test_depot_sans_membres(self):
        with patch.object(b.S, "get", return_value=self.reponse(404)):
            self.assertIsNone(b.lire_membres("GEFS-25-km"))

    def test_series_par_point_partagees_par_les_communes(self):
        d = {
            "members": 2,
            "run_time": "2026-10-16T12:00:00Z",
            "times": ["2026-10-16T12:00:00Z", "2026-10-16T18:00:00Z"],
            "columns": ["temperature_c", "precipitation_total_mm"],
            "points": [[1, 42.5, 2.75, None], [2, 42.75, 3.0, None]],
            "communes": [["66136", "Perpignan", ["66000"], 1, 42.7, 2.9, 1], ["66025", "Bourg-Madame", ["66760"], 1, 42.4, 1.9, 0]],
            "values": [[[[10.0, 0.0], [20.0, 0.0]], [[11.0, 0.5], [21.0, 1.5]]], [[[12.0, 0.0], [22.0, 0.0]], [[13.0, 0.0], [23.0, 4.0]]]],
        }
        with patch.object(b.S, "get", return_value=self.reponse(200, d)):
            series, point_de, run = b.lire_membres("AIFS-ENS-25-km")
        self.assertEqual(run, "2026-10-16T12:00:00Z")
        self.assertEqual(point_de, {"66136": 1, "66025": 0})
        self.assertEqual(len(series), 2)
        self.assertEqual(series[1][1].c["temp"], [22.0, 23.0])
        self.assertEqual(series[1][1].total, [0.0, 4.0])
        self.assertEqual(series[0][0].total, [0.0, 0.5])

    def test_fichier_incomplet_refuse(self):
        d = {"members": 3, "times": ["2026-10-16T12:00:00Z"], "columns": ["temperature_c", "precipitation_total_mm"], "points": [[1, 0, 0, None]], "communes": [], "values": [[[[1.0, 0.0]]]]}
        with patch.object(b.S, "get", return_value=self.reponse(200, d)), self.assertRaises(ValueError):
            b.lire_membres("AIFS-ENS-25-km")


if __name__ == "__main__":
    unittest.main()

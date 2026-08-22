#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test de non-régression sur un bulletin réel (22/08/2026, 12h45).

    python tests/test_parse.py

Vérifie que l'extraction retrouve exactement ce que montre le PDF de référence.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from parse_vjs_pdf import parse  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "bulletin-2026-08-22.pdf"
CALIBRATION = ROOT / "data" / "geo" / "france-map-calibration.json"

# Départements en « moyenne » sur la carte J+2, relevés sur le PDF.
J2_MEDIUM = {
    "09", "11", "12", "19", "23", "24", "30", "31", "32", "33", "34",
    "40", "46", "47", "48", "64", "65", "66", "81", "82", "87",
}
J3_LOW = {"04", "05", "06", "11", "13", "30", "34", "48", "66", "83", "84"}

failures = []


def check(condition, message):
    if condition:
        print("  ok   %s" % message)
    else:
        print("  FAIL %s" % message)
        failures.append(message)


def main():
    data = parse(FIXTURE, CALIBRATION)
    cards = {c["label"]: c for c in data["cards"]}

    print("En-tête")
    check(data["schema_version"] == 3, "schéma v3")
    check(data["bulletin"]["issued_at"].startswith("2026-08-22T12:45"), "émis le 22/08/2026 à 12h45")
    check("dégradation orageuse" in data["summaries"]["j2_j3"], "synthèse J+2/J+3 lue")
    check("vigilance orange" in data["summaries"]["j4_j7"], "synthèse J+4→J+7 lue")

    print("Légende")
    colours = {e["key"]: e["colour"] for e in data["legend"]["levels"]}
    check(colours["low"] == "#dddddd", "couleur « faible » = #dddddd")
    check(colours["medium"] == "#d08636", "couleur « moyenne » = #d08636")
    check(colours["high"] == "#8e440d", "couleur « élevée » = #8e440d")
    check(data["legend"]["phenomena"][0] == "vent", "ordre des phénomènes conservé")

    print("J+2 (départements)")
    j2 = cards["J+2"]
    medium = {c for c, v in j2["departments"].items() if v["level"] == "medium"}
    low = {c for c, v in j2["departments"].items() if v["level"] == "low"}
    check(len(j2["departments"]) == 96, "96 départements")
    check(medium == J2_MEDIUM, "zone « moyenne » exacte (%d départements)" % len(medium))
    check(len(low) == 37, "37 départements en « faible » (%d)" % len(low))
    check(j2["departments"]["31"]["phenomena"] == ["orages"], "Haute-Garonne : orages")
    check(j2["departments"]["59"]["level"] == "none", "Nord : quasi nulle")
    check(j2["departments"]["06"]["name"] == "Alpes-Maritimes", "nom français pour le 06")

    print("J+3 (départements)")
    j3 = cards["J+3"]
    check({c for c, v in j3["departments"].items() if v["level"] == "low"} == J3_LOW,
          "zone « faible » exacte")
    check(j3["max_level"] == "low", "niveau maximal = faible")

    print("J+4 / J+5 (quarts de la France)")
    check(cards["J+4"]["granularity"] == "quadrant", "J+4 par quart")
    check(cards["J+4"]["zones"]["SE"]["level"] == "none", "J+4 quart sud-est : quasi nulle")
    check(all(cards["J+4"]["zones"][z]["level"] == "low" for z in ("NO", "NE", "SO")),
          "J+4 : les trois autres quarts en faible")
    j5 = cards["J+5"]["zones"]
    check(j5["SE"]["level"] == "medium", "J+5 quart sud-est : moyenne")
    check({p["phenomenon"] for p in j5["SE"]["phenomena"]} == {"pluie_inondation", "orages"},
          "J+5 sud-est : pluie-inondation et orages")

    print("J+6 et J+7 (national)")
    j67 = cards["J+6 et J+7"]
    check(j67["granularity"] == "national", "granularité nationale")
    check(j67["national"]["level"] == "none", "aucun risque annoncé")
    check("departments" not in j67, "pas de valeurs départementales inventées")

    print()
    if failures:
        print("%d contrôle(s) en échec." % len(failures))
        return 1
    print("Tous les contrôles passent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

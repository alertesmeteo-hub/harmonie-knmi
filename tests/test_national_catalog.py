#!/usr/bin/env python3
"""Contrats du catalogue national utilisé par les cartes interactives."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from update_harmonie_france import load_catalog, write_map_places  # noqa: E402


class NationalCatalogTest(unittest.TestCase):
    def test_catalog_and_map_places_are_complete(self) -> None:
        catalog = load_catalog(ROOT / "config" / "communes-france.json")

        self.assertEqual(len(catalog.departments), 96)
        self.assertGreaterEqual(len(catalog.model_indexes), 10_000)
        commune_count = sum(
            len(department.communes)
            for department in catalog.departments.values()
        )
        self.assertEqual(commune_count, 34_746)

        with tempfile.TemporaryDirectory(prefix="harmonie-places-test-") as temporary:
            destination = Path(temporary) / "communes.json"
            count = write_map_places(catalog, destination)
            payload = json.loads(destination.read_text(encoding="utf-8"))

        # Les six communes à population nulle restent dans les prévisions,
        # mais sont volontairement absentes de la couche de libellés.
        self.assertEqual(count, 34_740)
        self.assertEqual(payload["count"], count)
        self.assertEqual(payload["columns"], [
            "name",
            "population",
            "latitude",
            "longitude",
        ])
        self.assertEqual(len(payload["places"]), count)
        self.assertEqual(payload["places"][0][0], "Paris")
        populations = [place[1] for place in payload["places"]]
        self.assertEqual(populations, sorted(populations, reverse=True))


if __name__ == "__main__":
    unittest.main()

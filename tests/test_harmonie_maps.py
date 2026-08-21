#!/usr/bin/env python3
"""Tests autonomes du rendu cartographique, sans archive KNMI."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harmonie_maps import HarmonieMapRenderer, LAYER_SPECS  # noqa: E402


class HarmonieMapRendererTest(unittest.TestCase):
    def test_render_step_and_manifest(self) -> None:
        latitudes, longitudes = np.meshgrid(
            np.linspace(42.0, 50.5, 6),
            np.linspace(-4.5, 8.5, 7),
            indexing="ij",
        )
        latitudes = latitudes.ravel()
        longitudes = longitudes.ravel()
        point_count = len(latitudes)
        fields = {
            "temperature_c": np.linspace(-5, 35, point_count),
            "wind_chill_c": np.linspace(-12, 20, point_count),
            "dewpoint_c": np.linspace(-10, 24, point_count),
            "humidex": np.linspace(-5, 45, point_count),
            "precipitation_mm": np.linspace(0, 12, point_count),
            "precipitation_total_mm": np.linspace(0, 40, point_count),
            "wind_speed_kmh": np.linspace(0, 70, point_count),
            "wind_gust_kmh": np.linspace(10, 110, point_count),
            "pressure_hpa": np.linspace(985, 1030, point_count),
            "cloud_cover_pct": np.linspace(0, 100, point_count),
            "cloud_low_pct": np.linspace(0, 80, point_count),
            "cloud_mid_pct": np.linspace(10, 90, point_count),
            "cloud_high_pct": np.linspace(20, 100, point_count),
            "humidity_pct": np.linspace(20, 100, point_count),
            "visibility_km": np.linspace(0.5, 40, point_count),
        }
        for spec in LAYER_SPECS:
            fields.setdefault(
                spec.field,
                np.linspace(spec.stops[0][0], spec.stops[-1][0], point_count),
            )
        fields["temperature_c"][0] = np.nan

        with tempfile.TemporaryDirectory(prefix="harmonie-map-test-") as temporary:
            output = Path(temporary) / "maps"
            renderer = HarmonieMapRenderer(
                latitudes,
                longitudes,
                output,
                width=240,
                height=180,
                source_max_distance=2.0,
                france_latitudes=latitudes,
                france_longitudes=longitudes,
                france_departments=[
                    "OUEST" if longitude < 2 else "EST"
                    for longitude in longitudes
                ],
                boundary_directory=ROOT / "config" / "natural-earth",
            )
            valid_time = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
            renderer.render_step(lead_hour=6, valid_time=valid_time, fields=fields)
            manifest = renderer.write_manifest(
                generated_at="2026-08-21T06:00:00Z",
                run_time="2026-08-21T06:00:00Z",
                places_path="maps/communes.json",
            )

            self.assertEqual(len(manifest["layers"]), len(LAYER_SPECS))
            self.assertEqual(manifest["steps"][0]["lead_hour"], 6)
            self.assertTrue((output / "fond.webp").is_file())
            self.assertTrue((output / "frontieres.svg").is_file())
            self.assertTrue((output / "temperature" / "006.webp").is_file())
            self.assertTrue((output / "pluie_1h" / "006.webp").is_file())

            with Image.open(output / "temperature" / "006.webp") as image:
                self.assertEqual(image.size, (240, 180))
                self.assertEqual(image.mode, "RGBA")
            with (output / "index.json").open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["projection"], "EPSG:3857")
            self.assertEqual(saved["schema_version"], 5)
            self.assertEqual(saved["module_version"], "3.4.0")
            self.assertEqual(saved["overlay"], "maps/frontieres.svg")
            self.assertEqual(saved["places"], "maps/communes.json")
            self.assertEqual(saved["layers"]["rafales"]["group"], "Vent")
            self.assertEqual(saved["steps"][0]["valid_time"], "2026-08-21T12:00:00Z")

            overlay = (output / "frontieres.svg").read_text(encoding="utf-8")
            self.assertIn('vector-effect="non-scaling-stroke"', overlay)


if __name__ == "__main__":
    unittest.main()

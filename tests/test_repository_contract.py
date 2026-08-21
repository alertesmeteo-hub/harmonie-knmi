#!/usr/bin/env python3
"""Garde-fous légers sur l'archive livrée et son workflow GitHub."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTest(unittest.TestCase):
    def test_supported_github_actions_are_used(self) -> None:
        workflow = (ROOT / ".github/workflows/update-harmonie.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn("actions/setup-python@v6", workflow)
        self.assertNotRegex(workflow, r"actions/(?:checkout|setup-python)@v7")

    def test_pyshp_is_not_a_runtime_dependency(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        renderer = (ROOT / "scripts/harmonie_maps.py").read_text(encoding="utf-8")
        self.assertNotIn("pyshp", requirements.lower())
        self.assertNotRegex(renderer, r"(?m)^\s*(?:from\s+shapefile|import\s+shapefile)")

    def test_module_versions_are_synchronized(self) -> None:
        expected = "3.5.0"
        renderer = (ROOT / "scripts/harmonie_maps.py").read_text(encoding="utf-8")
        pipeline = (ROOT / "scripts/update_harmonie_france.py").read_text(
            encoding="utf-8"
        )
        plugin = (
            ROOT / "wordpress/harmonie-knmi-widget/harmonie-knmi-widget.php"
        ).read_text(encoding="utf-8")
        readme = (ROOT / "wordpress/harmonie-knmi-widget/readme.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'MODULE_VERSION = "{expected}"', renderer)
        self.assertIn(f'NATIONAL_PIPELINE_VERSION = "{expected}"', pipeline)
        self.assertRegex(plugin, rf"(?m)^ \* Version: {re.escape(expected)}$")
        self.assertIn(f"define('HKW_VERSION', '{expected}');", plugin)
        self.assertRegex(readme, rf"(?m)^Stable tag: {re.escape(expected)}$")

    def test_interactive_map_assets_are_packaged(self) -> None:
        plugin_root = ROOT / "wordpress/harmonie-knmi-widget"
        for relative in (
            "assets/harmonie-map.js",
            "assets/harmonie-map.css",
            "assets/harmonie-knmi.js",
            "assets/harmonie-knmi.css",
        ):
            self.assertTrue((plugin_root / relative).is_file(), relative)
        plugin = (plugin_root / "harmonie-knmi-widget.php").read_text(
            encoding="utf-8"
        )
        map_script = (plugin_root / "assets/harmonie-map.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("data-hkm-probe", plugin)
        self.assertIn("var maxScale = 64;", map_script)
        self.assertIn("DecompressionStream", map_script)


if __name__ == "__main__":
    unittest.main()

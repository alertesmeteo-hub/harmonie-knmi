#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("MDF_DATA_DIR", "data"))


def load(name: str):
    path = DATA_DIR / name
    assert path.exists(), f"Fichier manquant: {path}"

    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload


latest = load("meteo_forets_latest.json")
history = load("meteo_forets_history.json")
archive = load("meteo_forets_archive.json")
geo = load("departements.geojson")

assert latest.get("ok") is True, "latest.ok absent ou faux"
assert latest.get("reference_time"), "reference_time absent"
datetime.fromisoformat(latest["reference_time"].replace("Z", "+00:00"))

departments = latest.get("departments")
assert isinstance(departments, list), "departments doit être une liste"
assert 94 <= len(departments) <= 96, (
    f"Nombre de départements inattendu: {len(departments)}"
)

codes = set()

for department in departments:
    code = department["code"]

    assert code not in codes, f"Département dupliqué: {code}"
    codes.add(code)

    assert department["name"], f"Nom vide pour {code}"
    assert department["j1"] in (1, 2, 3, 4), f"J+1 invalide pour {code}"
    assert department["j2"] in (1, 2, 3, 4), f"J+2 invalide pour {code}"

assert "2A" in codes and "2B" in codes, "Corse 2A/2B absente"

counts = latest["counts"]

for day in ("j1", "j2"):
    total = sum(
        int(counts[day].get(str(level), 0))
        for level in range(1, 5)
    )
    assert total == len(departments), (
        f"Comptage {day} incohérent: {total} != {len(departments)}"
    )

assert isinstance(history, list) and history, "Historique vide"
assert isinstance(archive, list) and archive, "Archive JSON vide"
assert len(history) == len(archive), "Historique et archive désynchronisés"

assert history[-1]["date"] == latest["bulletin_date"], (
    "Dernière date historique différente du dernier bulletin"
)

assert archive[-1]["date"] == latest["bulletin_date"], (
    "Dernière archive différente du dernier bulletin"
)

assert (
    archive[-1]["reference_time"] == latest["reference_time"]
), "Reference_time latest/archive incohérent"

assert isinstance(geo, dict), "GeoJSON doit être un objet"
assert isinstance(geo.get("features"), list), "GeoJSON.features absent"
assert geo["features"], "GeoJSON vide"

geo_codes = set()

for feature in geo["features"]:
    props = feature.get("properties") or {}
    code = str(
        props.get("code")
        or props.get("CODE")
        or props.get("code_dep")
        or props.get("CODE_DEPT")
        or ""
    ).upper()

    if code.isdigit() and 1 <= int(code) <= 95:
        code = f"{int(code):02d}"

    if code:
        geo_codes.add(code)

assert "2A" in geo_codes and "2B" in geo_codes, "Corse absente du GeoJSON"

missing_geometry = sorted(codes - geo_codes)
assert not missing_geometry, (
    "Départements sans géométrie: " + ", ".join(missing_geometry)
)

print("Contrôle OK")
print("Dernier bulletin :", latest["reference_time"])
print("Départements :", len(departments))
print("Jours historiques :", len(history))
print("GeoJSON features :", len(geo["features"]))

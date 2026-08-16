#!/usr/bin/env python3
"""Construit le catalogue compact des communes françaises pour HARMONIE.

Ce script est un outil de maintenance : il télécharge le référentiel officiel
des communes, rattache chaque commune métropolitaine au point de grille N55 le
plus proche et écrit ``config/communes-france.json``. Le catalogue ne doit être
reconstruit que lors d'un changement du référentiel communal ou de la grille.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from eccodes import codes_grib_find_nearest, codes_grib_new_from_file, codes_release

from update_harmonie import grid_signature, normalize_longitude, safe_get, safe_get_long


LOGGER = logging.getLogger("harmonie.catalogue")
CATALOG_VERSION = "1.0.0"
COMMUNES_URL = (
    "https://geo.api.gouv.fr/communes"
    "?fields=nom%2Ccode%2CcodesPostaux%2CcodeDepartement%2Ccentre%2Cpopulation"
    "&format=json&geometry=centre"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grib", required=True, help="Fichier GRIB N55 de référence")
    parser.add_argument(
        "--output",
        default="config/communes-france.json",
        help="Catalogue JSON à produire",
    )
    parser.add_argument("--communes-url", default=COMMUNES_URL)
    return parser.parse_args()


def metropolitan_department(code: Any) -> bool:
    value = str(code or "").upper()
    if value in {"2A", "2B"}:
        return True
    return value.isdigit() and 1 <= int(value) <= 95


def load_communes(url: str) -> list[dict[str, Any]]:
    LOGGER.info("Téléchargement du référentiel officiel des communes")
    response = requests.get(
        url,
        timeout=(20, 180),
        headers={"User-Agent": "alertesmeteo-hub/harmonie-knmi"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Réponse inattendue de geo.api.gouv.fr")

    communes = [
        item
        for item in payload
        if metropolitan_department(item.get("codeDepartement"))
        and isinstance(item.get("centre", {}).get("coordinates"), list)
        and len(item["centre"]["coordinates"]) >= 2
    ]
    communes.sort(
        key=lambda item: (
            str(item.get("codeDepartement", "")),
            str(item.get("nom", "")).casefold(),
            str(item.get("code", "")),
        )
    )
    if len(communes) < 34_000:
        raise RuntimeError(
            f"Référentiel communal incomplet : {len(communes)} communes seulement"
        )
    LOGGER.info("Communes métropolitaines et corses : %s", len(communes))
    return communes


def model_grid_metadata(gid: int) -> dict[str, Any]:
    signature = grid_signature(gid)
    return {
        "grid_type": signature[0],
        "ni": signature[1],
        "nj": signature[2],
        "number_of_points": signature[3],
        "latitude_first": signature[4],
        "longitude_first": signature[5],
        "latitude_last": signature[6],
        "longitude_last": signature[7],
        "latitude_southern_pole": safe_get(
            gid, "latitudeOfSouthernPoleInDegrees", None
        ),
        "longitude_southern_pole": safe_get(
            gid, "longitudeOfSouthernPoleInDegrees", None
        ),
        "uv_relative_to_grid": safe_get_long(gid, "uvRelativeToGrid", None),
    }


def build_catalog(grib: Path, communes: list[dict[str, Any]]) -> dict[str, Any]:
    points_by_index: dict[int, tuple[float, float]] = {}
    mapped_communes: list[tuple[dict[str, Any], int]] = []

    with grib.open("rb") as handle:
        gid = codes_grib_new_from_file(handle)
        if gid is None:
            raise RuntimeError("Le fichier GRIB de référence est vide")
        try:
            grid = model_grid_metadata(gid)
            for position, commune in enumerate(communes, start=1):
                longitude, latitude = commune["centre"]["coordinates"][:2]
                nearest = codes_grib_find_nearest(
                    gid,
                    float(latitude),
                    float(longitude),
                    False,
                    1,
                )[0]
                model_index = int(nearest.index)
                points_by_index.setdefault(
                    model_index,
                    (
                        round(float(nearest.lat), 5),
                        round(normalize_longitude(float(nearest.lon)), 5),
                    ),
                )
                mapped_communes.append((commune, model_index))
                if position % 1_000 == 0:
                    LOGGER.info("Rattachement à la grille : %s/%s", position, len(communes))
        finally:
            codes_release(gid)

    sorted_points = sorted(points_by_index.items())
    point_identifier = {
        model_index: identifier
        for identifier, (model_index, _) in enumerate(sorted_points)
    }
    points = [
        [model_index, coordinates[0], coordinates[1]]
        for model_index, coordinates in sorted_points
    ]

    compact_communes: list[list[Any]] = []
    for commune, model_index in mapped_communes:
        longitude, latitude = commune["centre"]["coordinates"][:2]
        compact_communes.append(
            [
                str(commune.get("code", "")),
                str(commune.get("nom", "")),
                str(commune.get("codeDepartement", "")).upper(),
                sorted({str(item) for item in commune.get("codesPostaux", [])}),
                int(commune.get("population") or 0),
                round(float(latitude), 5),
                round(float(longitude), 5),
                point_identifier[model_index],
            ]
        )

    LOGGER.info("Points de grille uniques : %s", len(points))
    return {
        "schema_version": 1,
        "catalog_version": CATALOG_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_url": COMMUNES_URL,
        "model_grid": grid,
        "columns": {
            "points": ["model_index", "latitude", "longitude"],
            "communes": [
                "code_insee",
                "name",
                "department",
                "postal_codes",
                "population",
                "latitude",
                "longitude",
                "point_id",
            ],
        },
        "points": points,
        "communes": compact_communes,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    temporary.replace(path)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    args = parse_args()
    communes = load_communes(args.communes_url)
    payload = build_catalog(Path(args.grib), communes)
    output = Path(args.output)
    write_json_atomic(output, payload)
    LOGGER.info("Catalogue écrit : %s (%.1f Mo)", output, output.stat().st_size / 1e6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

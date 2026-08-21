#!/usr/bin/env python3
"""Produit les prévisions HARMONIE pour toutes les communes métropolitaines.

Les communes qui partagent le même point de grille partagent aussi une seule
série de valeurs. Les fichiers sont découpés par département afin que le widget
WordPress ne télécharge que les données utiles à la commune choisie.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sys
import tarfile
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests
from eccodes import (
    codes_get_double_array,
    codes_get_double_elements,
    codes_grib_new_from_file,
    codes_release,
)

import update_harmonie as base
from harmonie_maps import DEFAULT_BOUNDS, HarmonieMapRenderer


LOGGER = logging.getLogger("harmonie.france")
NATIONAL_PIPELINE_VERSION = "3.5.0"
DEFAULT_CURRENT_METADATA_URL = (
    "https://raw.githubusercontent.com/alertesmeteo-hub/"
    "harmonie-knmi/data/index.json"
)

# Colonnes d'une valeur horaire compacte. Les libellés et icônes de temps sont
# reconstruits par le widget pour éviter de les répéter plusieurs millions de
# fois dans les fichiers JSON.
VALUE_COLUMNS = (
    "temperature_c",
    "humidity_pct",
    "precipitation_mm",
    "cloud_cover_pct",
    "wind_speed_kmh",
    "wind_direction_deg",
    "wind_gust_kmh",
    "pressure_hpa",
    "visibility_km",
    "condition_code",
)

CONDITION_CODES = {
    0: "unknown",
    1: "clear",
    2: "partly_cloudy",
    3: "cloudy",
    4: "overcast",
    5: "rain",
    6: "heavy_rain",
    7: "snow",
    8: "fog",
    9: "windy",
}

REQUIRED_PARAMETERS = {
    "pressure_pa",
    "surface_pressure_pa",
    "geopotential_500_raw",
    "geopotential_850_raw",
    "surface_temperature_k",
    "temperature_k",
    "temperature_500_k",
    "temperature_850_k",
    "dewpoint_k",
    "visibility_m",
    "wind_u_ms",
    "wind_v_ms",
    "wind_u_300_ms",
    "wind_v_300_ms",
    "wind_u_500_ms",
    "wind_v_500_ms",
    "wind_u_850_ms",
    "wind_v_850_ms",
    "humidity_pct",
    "humidity_500_pct",
    "humidity_850_pct",
    "precipitation_raw_mm",
    "snow_water_equivalent_mm",
    "snow_depth_m",
    "snow_raw_mm",
    "graupel_raw_mm",
    "cloud_pct",
    "cloud_low_pct",
    "cloud_mid_pct",
    "cloud_high_pct",
    "cloud_base_m",
    "mixed_layer_depth_m",
    "net_shortwave_jm2",
    "net_longwave_jm2",
    "global_radiation_jm2",
    "sensible_heat_jm2",
    "latent_heat_jm2",
    "gust_u_ms",
    "gust_v_ms",
}


@dataclass(frozen=True)
class DepartmentData:
    code: str
    global_point_ids: np.ndarray
    points: list[list[Any]]
    communes: list[list[Any]]


@dataclass(frozen=True)
class NationalCatalog:
    version: str
    grid: dict[str, Any]
    model_indexes: list[int]
    point_latitudes: np.ndarray
    point_longitudes: np.ndarray
    point_departments: list[str]
    departments: dict[str, DepartmentData]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        default="config/communes-france.json",
        help="Catalogue compact construit par build_commune_catalog.py",
    )
    parser.add_argument(
        "--output-dir",
        default="build/national",
        help="Dossier de publication à produire",
    )
    parser.add_argument(
        "--archive",
        help="Archive TAR locale à décoder, sans appel à l'API KNMI",
    )
    parser.add_argument(
        "--current-metadata-url",
        default=DEFAULT_CURRENT_METADATA_URL,
        help="index.json publié, utilisé pour éviter un retraitement inutile",
    )
    parser.add_argument(
        "--forecast-hours",
        type=int,
        default=48,
        help="Dernière échéance à conserver (1 à 60 heures)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retraite une archive même si elle est déjà publiée",
    )
    return parser.parse_args()


def load_catalog(path: Path) -> NationalCatalog:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != 1:
        raise RuntimeError("Version du catalogue communal non prise en charge")

    raw_points = payload.get("points") or []
    raw_communes = payload.get("communes") or []
    if len(raw_points) < 10_000 or len(raw_communes) < 34_000:
        raise RuntimeError("Le catalogue France est incomplet")

    model_indexes = [int(point[0]) for point in raw_points]
    point_latitudes = np.asarray([float(point[1]) for point in raw_points])
    point_longitudes = np.asarray([float(point[2]) for point in raw_points])
    department_votes: dict[int, Counter[str]] = defaultdict(Counter)

    communes_by_department: dict[str, list[list[Any]]] = {}
    for commune in raw_communes:
        if not isinstance(commune, list) or len(commune) < 8:
            raise RuntimeError("Entrée communale invalide dans le catalogue")
        department_code = str(commune[2])
        communes_by_department.setdefault(department_code, []).append(commune)
        department_votes[int(commune[7])][department_code] += 1

    point_departments = [
        department_votes[point_id].most_common(1)[0][0]
        if department_votes[point_id]
        else ""
        for point_id in range(len(raw_points))
    ]

    departments: dict[str, DepartmentData] = {}
    for department_code, communes in sorted(communes_by_department.items()):
        global_ids = sorted({int(commune[7]) for commune in communes})
        local_identifier = {
            global_id: position for position, global_id in enumerate(global_ids)
        }
        compact_communes = [
            [
                str(commune[0]),
                str(commune[1]),
                list(commune[3]),
                int(commune[4]),
                float(commune[5]),
                float(commune[6]),
                local_identifier[int(commune[7])],
            ]
            for commune in communes
        ]
        compact_points = [
            [
                model_indexes[global_id],
                round(float(point_latitudes[global_id]), 5),
                round(float(point_longitudes[global_id]), 5),
            ]
            for global_id in global_ids
        ]
        departments[department_code] = DepartmentData(
            code=department_code,
            global_point_ids=np.asarray(global_ids, dtype=np.int64),
            points=compact_points,
            communes=compact_communes,
        )

    if len(departments) != 96:
        raise RuntimeError(
            f"Nombre inattendu de départements métropolitains : {len(departments)}"
        )
    LOGGER.info(
        "Catalogue chargé : %s communes, %s points, %s départements",
        len(raw_communes),
        len(raw_points),
        len(departments),
    )
    return NationalCatalog(
        version=str(payload.get("catalog_version", "1")),
        grid=dict(payload.get("model_grid") or {}),
        model_indexes=model_indexes,
        point_latitudes=point_latitudes,
        point_longitudes=point_longitudes,
        point_departments=point_departments,
        departments=departments,
    )


def write_map_places(catalog: NationalCatalog, destination: Path) -> int:
    """Publie les communes utilisées par la couche de libellés du zoom."""

    places = [
        [
            str(commune[1]),
            int(commune[3]),
            round(float(commune[4]), 5),
            round(float(commune[5]), 5),
        ]
        for department in catalog.departments.values()
        for commune in department.communes
        if int(commune[3]) > 0
    ]
    places.sort(key=lambda place: (-place[1], place[0]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": 1,
                "columns": ["name", "population", "latitude", "longitude"],
                "count": len(places),
                "places": places,
            },
            handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        handle.write("\n")
    return len(places)


class NationalGrid:
    def __init__(self, catalog: NationalCatalog):
        self.catalog = catalog
        self._rotations: np.ndarray | None = None
        self._validated = False

    def validate(self, gid: int) -> None:
        if self._validated:
            return
        signature = base.grid_signature(gid)
        actual = {
            "grid_type": signature[0],
            "ni": signature[1],
            "nj": signature[2],
            "number_of_points": signature[3],
            "latitude_first": signature[4],
            "longitude_first": signature[5],
            "latitude_last": signature[6],
            "longitude_last": signature[7],
        }
        for key, expected in self.catalog.grid.items():
            if key not in actual or expected is None:
                continue
            value = actual[key]
            if isinstance(expected, float):
                matches = value is not None and math.isclose(
                    float(value), expected, abs_tol=1.0e-6
                )
            else:
                matches = value == expected
            if not matches:
                raise RuntimeError(
                    f"La grille HARMONIE a changé ({key}: {value!r}, "
                    f"catalogue: {expected!r}). Reconstruisez le catalogue."
                )
        point_count = int(actual.get("number_of_points") or 0)
        if max(self.catalog.model_indexes) >= point_count:
            raise RuntimeError("Un indice du catalogue dépasse la grille HARMONIE")
        self._validated = True

    def extract(self, gid: int) -> np.ndarray:
        self.validate(gid)
        values = np.asarray(
            codes_get_double_elements(gid, "values", self.catalog.model_indexes),
            dtype=np.float64,
        )
        values[~np.isfinite(values) | (np.abs(values) > 1.0e20)] = np.nan
        return values

    def rotations(self, gid: int) -> np.ndarray:
        self.validate(gid)
        if self._rotations is not None:
            return self._rotations
        relative = base.safe_get_long(gid, "uvRelativeToGrid", None)
        grid_type = str(base.safe_get(gid, "gridType", ""))
        if relative == 0 or (relative is None and "rotated" not in grid_type):
            self._rotations = np.zeros(len(self.catalog.model_indexes))
            return self._rotations

        latitudes = codes_get_double_array(gid, "latitudes")
        longitudes = codes_get_double_array(gid, "longitudes")
        rotations = np.empty(len(self.catalog.model_indexes), dtype=np.float64)
        for position, model_index in enumerate(self.catalog.model_indexes):
            rotations[position] = base.calculate_grid_north_bearing(
                gid,
                model_index,
                latitudes,
                longitudes,
            )
        self._rotations = rotations
        return rotations


class MapSourceGrid:
    """Sélectionne la grille HARMONIE native sur l'Europe de l'Ouest."""

    def __init__(self, validator: NationalGrid):
        self.validator = validator
        self.model_indexes: np.ndarray | None = None
        self.latitudes: np.ndarray | None = None
        self.longitudes: np.ndarray | None = None

    def prepare(self, gid: int) -> None:
        if self.model_indexes is not None:
            return
        self.validator.validate(gid)
        latitudes = np.asarray(codes_get_double_array(gid, "latitudes"))
        longitudes = np.asarray(codes_get_double_array(gid, "longitudes"))
        longitudes = (longitudes + 180.0) % 360.0 - 180.0
        bounds = DEFAULT_BOUNDS
        selected = (
            np.isfinite(latitudes)
            & np.isfinite(longitudes)
            & (latitudes >= float(bounds["south"]) - 0.5)
            & (latitudes <= float(bounds["north"]) + 0.5)
            & (longitudes >= float(bounds["west"]) - 0.5)
            & (longitudes <= float(bounds["east"]) + 0.5)
        )
        indexes = np.flatnonzero(selected)
        if len(indexes) < 20_000:
            raise RuntimeError(
                "La grille HARMONIE Europe de l'Ouest est anormalement petite"
            )
        self.model_indexes = indexes.astype(np.int64, copy=False)
        self.latitudes = latitudes[indexes].astype(np.float64, copy=False)
        self.longitudes = longitudes[indexes].astype(np.float64, copy=False)
        LOGGER.info(
            "Grille cartographique native : %s points HARMONIE",
            len(indexes),
        )

    @property
    def point_count(self) -> int:
        return 0 if self.model_indexes is None else len(self.model_indexes)

    def extract(self, gid: int) -> np.ndarray:
        self.prepare(gid)
        assert self.model_indexes is not None
        full_values = np.asarray(codes_get_double_array(gid, "values"))
        values = full_values[self.model_indexes].astype(np.float64, copy=False)
        values[~np.isfinite(values) | (np.abs(values) > 1.0e20)] = np.nan
        return values


def empty_values(point_count: int) -> np.ndarray:
    return np.full(point_count, np.nan, dtype=np.float64)


def parse_grib_file(
    path: Path,
    grid: NationalGrid,
    map_grid: MapSourceGrid,
    lead_hint: int,
    run_hint: datetime | None,
) -> dict[str, Any]:
    point_count = len(grid.catalog.model_indexes)
    step: dict[str, Any] = {
        "lead_hint": lead_hint,
        "run_time": run_hint,
        "valid_time": None,
        "precip_start_step": None,
        "precip_end_step": None,
        "values": {},
        "map_values": {},
        "rotations": None,
    }
    with path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                name = base.parameter_name(gid)
                if name not in REQUIRED_PARAMETERS:
                    continue
                if step["run_time"] is None:
                    step["run_time"] = base.date_time_from_grib(
                        gid, "dataDate", "dataTime"
                    )
                if step["valid_time"] is None:
                    step["valid_time"] = base.date_time_from_grib(
                        gid, "validityDate", "validityTime"
                    )
                step["values"][name] = grid.extract(gid)
                step["map_values"][name] = map_grid.extract(gid)
                if name == "precipitation_raw_mm":
                    step["precip_start_step"] = base.safe_get_long(gid, "startStep")
                    step["precip_end_step"] = base.safe_get_long(gid, "endStep")
                if name == "wind_u_ms":
                    step["rotations"] = grid.rotations(gid)
            finally:
                codes_release(gid)

    if "temperature_k" not in step["values"]:
        raise RuntimeError(f"Température HARMONIE absente de {path.name}")
    if step["valid_time"] is None and step["run_time"] is not None:
        step["valid_time"] = step["run_time"] + timedelta(hours=lead_hint)
    if step["valid_time"] is None:
        raise RuntimeError(f"Échéance temporelle absente de {path.name}")
    for name in REQUIRED_PARAMETERS:
        step["values"].setdefault(name, empty_values(point_count))
        step["map_values"].setdefault(name, empty_values(map_grid.point_count))
    if step["rotations"] is None:
        step["rotations"] = np.zeros(point_count)
    return step


def rounded(values: np.ndarray, decimals: int) -> np.ndarray:
    return np.round(values, decimals)


def transform_step(
    step: dict[str, Any],
    previous_cumulative: np.ndarray | None,
) -> tuple[dict[str, np.ndarray], np.ndarray | None]:
    raw = step["values"]
    temperature = rounded(raw["temperature_k"] - 273.15, 1)
    surface_temperature = rounded(raw["surface_temperature_k"] - 273.15, 1)
    temperature_500 = rounded(raw["temperature_500_k"] - 273.15, 1)
    temperature_850 = rounded(raw["temperature_850_k"] - 273.15, 1)
    dewpoint = rounded(raw["dewpoint_k"] - 273.15, 1)
    humidity = rounded(np.clip(raw["humidity_pct"] * 100.0, 0, 100), 0)
    cloud = rounded(np.clip(raw["cloud_pct"] * 100.0, 0, 100), 0)
    cloud_low = rounded(np.clip(raw["cloud_low_pct"] * 100.0, 0, 100), 0)
    cloud_mid = rounded(np.clip(raw["cloud_mid_pct"] * 100.0, 0, 100), 0)
    cloud_high = rounded(np.clip(raw["cloud_high_pct"] * 100.0, 0, 100), 0)
    humidity_500 = rounded(np.clip(raw["humidity_500_pct"] * 100.0, 0, 100), 0)
    humidity_850 = rounded(np.clip(raw["humidity_850_pct"] * 100.0, 0, 100), 0)
    pressure = rounded(raw["pressure_pa"] / 100.0, 0)
    surface_pressure = rounded(raw["surface_pressure_pa"] / 100.0, 0)
    geopotential_500 = rounded(raw["geopotential_500_raw"] / 9.80665, 0)
    geopotential_850 = rounded(raw["geopotential_850_raw"] / 9.80665, 0)
    visibility = rounded(raw["visibility_m"] / 1000.0, 1)
    cloud_base = rounded(raw["cloud_base_m"], 0)
    mixed_layer_depth = rounded(raw["mixed_layer_depth_m"], 0)
    snow_water_equivalent = rounded(
        np.maximum(raw["snow_water_equivalent_mm"], 0.0),
        1,
    )
    snow_depth = rounded(np.maximum(raw["snow_depth_m"], 0.0) * 100.0, 1)
    snow = rounded(np.maximum(raw["snow_raw_mm"], 0.0), 1)
    graupel = rounded(np.maximum(raw["graupel_raw_mm"], 0.0), 1)
    net_shortwave = rounded(raw["net_shortwave_jm2"] / 1_000_000.0, 2)
    net_longwave = rounded(raw["net_longwave_jm2"] / 1_000_000.0, 2)
    global_radiation = rounded(raw["global_radiation_jm2"] / 1_000_000.0, 2)
    sensible_heat = rounded(raw["sensible_heat_jm2"] / 1_000_000.0, 2)
    latent_heat = rounded(raw["latent_heat_jm2"] / 1_000_000.0, 2)

    precipitation_raw = np.maximum(raw["precipitation_raw_mm"], 0.0)
    if step.get("precip_start_step") == 0 and step.get("precip_end_step") is not None:
        if previous_cumulative is None:
            precipitation = precipitation_raw.copy()
        else:
            precipitation = np.maximum(precipitation_raw - previous_cumulative, 0.0)
        previous_cumulative = precipitation_raw.copy()
    else:
        precipitation = precipitation_raw.copy()
    precipitation = rounded(precipitation, 1)

    u = raw["wind_u_ms"]
    v = raw["wind_v_ms"]
    angle = np.radians(step["rotations"])
    east = u * np.cos(angle) + v * np.sin(angle)
    north = -u * np.sin(angle) + v * np.cos(angle)
    wind_speed = rounded(np.hypot(u, v) * 3.6, 0)
    wind_direction = rounded(np.degrees(np.arctan2(-east, -north)) % 360.0, 0)
    gust_speed = rounded(
        np.hypot(raw["gust_u_ms"], raw["gust_v_ms"]) * 3.6,
        0,
    )
    wind_speed_300 = rounded(
        np.hypot(raw["wind_u_300_ms"], raw["wind_v_300_ms"]) * 3.6,
        0,
    )
    wind_speed_500 = rounded(
        np.hypot(raw["wind_u_500_ms"], raw["wind_v_500_ms"]) * 3.6,
        0,
    )
    wind_speed_850 = rounded(
        np.hypot(raw["wind_u_850_ms"], raw["wind_v_850_ms"]) * 3.6,
        0,
    )

    wind_chill = temperature.copy()
    wind_chill_valid = (
        np.isfinite(temperature)
        & np.isfinite(wind_speed)
        & (temperature <= 10.0)
        & (wind_speed >= 4.8)
    )
    wind_factor = np.power(np.maximum(wind_speed, 0.0), 0.16)
    wind_chill[wind_chill_valid] = (
        13.12
        + 0.6215 * temperature[wind_chill_valid]
        - 11.37 * wind_factor[wind_chill_valid]
        + 0.3965
        * temperature[wind_chill_valid]
        * wind_factor[wind_chill_valid]
    )
    wind_chill = rounded(wind_chill, 1)

    dewpoint_kelvin = np.clip(dewpoint + 273.15, 173.15, 333.15)
    vapour_pressure = 6.11 * np.exp(
        5417.7530 * (1.0 / 273.16 - 1.0 / dewpoint_kelvin)
    )
    humidex = rounded(temperature + 0.5555 * (vapour_pressure - 10.0), 1)

    condition = np.zeros(len(temperature), dtype=np.int16)
    condition[np.isfinite(cloud) & (cloud <= 20)] = 1
    condition[np.isfinite(cloud) & (cloud > 20) & (cloud <= 55)] = 2
    condition[np.isfinite(cloud) & (cloud > 55) & (cloud <= 85)] = 3
    condition[np.isfinite(cloud) & (cloud > 85)] = 4
    condition[np.isfinite(gust_speed) & (gust_speed >= 70)] = 9
    condition[np.isfinite(precipitation) & (precipitation >= 0.1)] = 5
    condition[np.isfinite(precipitation) & (precipitation >= 5.0)] = 6
    condition[
        np.isfinite(precipitation)
        & (precipitation >= 0.1)
        & np.isfinite(temperature)
        & (temperature <= 1.0)
    ] = 7
    condition[np.isfinite(visibility) & (visibility < 1.0)] = 8

    return (
        {
            "temperature_c": temperature,
            "surface_temperature_c": surface_temperature,
            "temperature_500_c": temperature_500,
            "temperature_850_c": temperature_850,
            "wind_chill_c": wind_chill,
            "dewpoint_c": dewpoint,
            "humidex": humidex,
            "humidity_pct": humidity,
            "precipitation_mm": precipitation,
            "cloud_cover_pct": cloud,
            "cloud_low_pct": cloud_low,
            "cloud_mid_pct": cloud_mid,
            "cloud_high_pct": cloud_high,
            "humidity_500_pct": humidity_500,
            "humidity_850_pct": humidity_850,
            "wind_speed_kmh": wind_speed,
            "wind_speed_300_kmh": wind_speed_300,
            "wind_speed_500_kmh": wind_speed_500,
            "wind_speed_850_kmh": wind_speed_850,
            "wind_direction_deg": wind_direction,
            "wind_gust_kmh": gust_speed,
            "pressure_hpa": pressure,
            "surface_pressure_hpa": surface_pressure,
            "geopotential_500_m": geopotential_500,
            "geopotential_850_m": geopotential_850,
            "visibility_km": visibility,
            "cloud_base_m": cloud_base,
            "mixed_layer_depth_m": mixed_layer_depth,
            "snow_water_equivalent_mm": snow_water_equivalent,
            "snow_depth_cm": snow_depth,
            "snow_mm": snow,
            "graupel_mm": graupel,
            "net_shortwave_mjm2": net_shortwave,
            "net_longwave_mjm2": net_longwave,
            "global_radiation_mjm2": global_radiation,
            "sensible_heat_mjm2": sensible_heat,
            "latent_heat_mjm2": latent_heat,
            "condition_code": condition,
        },
        previous_cumulative,
    )


def json_number(value: Any, *, integer: bool = False) -> int | float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if integer:
        return int(round(number))
    return number


def compact_rows(
    transformed: dict[str, np.ndarray],
    point_ids: np.ndarray,
) -> list[list[int | float | None]]:
    rows: list[list[int | float | None]] = []
    for point_id in point_ids:
        position = int(point_id)
        rows.append(
            [
                json_number(transformed["temperature_c"][position]),
                json_number(transformed["humidity_pct"][position], integer=True),
                json_number(transformed["precipitation_mm"][position]),
                json_number(transformed["cloud_cover_pct"][position], integer=True),
                json_number(transformed["wind_speed_kmh"][position], integer=True),
                json_number(
                    transformed["wind_direction_deg"][position], integer=True
                ),
                json_number(transformed["wind_gust_kmh"][position], integer=True),
                json_number(transformed["pressure_hpa"][position], integer=True),
                json_number(transformed["visibility_km"][position]),
                json_number(transformed["condition_code"][position], integer=True),
            ]
        )
    return rows


def safe_output_directory(path: Path) -> Path:
    resolved = path.resolve()
    forbidden = {Path("/").resolve(), Path.cwd().resolve(), Path.home().resolve()}
    if resolved in forbidden or len(resolved.parts) < 3:
        raise RuntimeError(f"Dossier de sortie dangereux : {resolved}")
    return resolved


def already_published(url: str, source_filename: str) -> bool:
    if not url:
        return False
    try:
        response = requests.get(
            url,
            timeout=(10, 30),
            headers={"User-Agent": "alertesmeteo-hub/harmonie-knmi"},
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        model = payload.get("model") or {}
        return (
            payload.get("status") == "ok"
            and model.get("source_file") == source_filename
            and model.get("pipeline_version") == NATIONAL_PIPELINE_VERSION
        )
    except (requests.RequestException, ValueError, TypeError):
        return False


def archive_members(
    archive: Path, forecast_hours: int
) -> list[tuple[int, datetime | None, tarfile.TarInfo]]:
    if not tarfile.is_tarfile(archive):
        raise RuntimeError("Le fichier reçu n'est pas une archive TAR valide")
    with tarfile.open(archive, mode="r:*") as tar:
        members: list[tuple[int, datetime | None, tarfile.TarInfo]] = []
        for member in tar.getmembers():
            if not member.isfile() or member.size <= 0:
                continue
            information = base.member_information(member)
            if information is None:
                continue
            lead, run = information
            if 0 <= lead <= forecast_hours:
                members.append((lead, run, member))
    members.sort(key=lambda item: (item[0], item[2].name))
    if not members:
        raise RuntimeError("Aucune échéance HARMONIE nationale trouvée")
    return members


def decode_national_archive(
    archive: Path,
    catalog: NationalCatalog,
    forecast_hours: int,
    source: dict[str, Any],
    working_directory: Path,
) -> Path:
    members = archive_members(archive, forecast_hours)
    LOGGER.info("Échéances GRIB nationales à traiter : %s", len(members))

    result_directory = working_directory / "result"
    forecast_directory = working_directory / "forecast-lines"
    result_directory.mkdir(parents=True, exist_ok=True)
    forecast_directory.mkdir(parents=True, exist_ok=True)
    line_handles = {
        code: (forecast_directory / f"{code}.ndjson").open("w", encoding="utf-8")
        for code in catalog.departments
    }

    grid = NationalGrid(catalog)
    map_grid = MapSourceGrid(grid)
    map_renderer: HarmonieMapRenderer | None = None
    previous_cumulative: np.ndarray | None = None
    map_previous_cumulative: np.ndarray | None = None
    map_precipitation_total: np.ndarray | None = None
    model_run: datetime | None = None
    temporary_grib = working_directory / "current.grib"

    try:
        with tarfile.open(archive, mode="r:*") as tar:
            for position, (lead, run, member) in enumerate(members, start=1):
                source_member = tar.extractfile(member)
                if source_member is None:
                    raise RuntimeError(f"Impossible de lire {member.name}")
                with source_member, temporary_grib.open("wb") as destination:
                    shutil.copyfileobj(
                        source_member,
                        destination,
                        length=base.DOWNLOAD_CHUNK_BYTES,
                    )
                LOGGER.info(
                    "Décodage national %s/%s : +%02dh",
                    position,
                    len(members),
                    lead,
                )
                step = parse_grib_file(temporary_grib, grid, map_grid, lead, run)
                if model_run is None:
                    model_run = step.get("run_time")
                transformed, previous_cumulative = transform_step(
                    step, previous_cumulative
                )
                map_step = {
                    "values": step["map_values"],
                    "rotations": np.zeros(map_grid.point_count),
                    "precip_start_step": step.get("precip_start_step"),
                    "precip_end_step": step.get("precip_end_step"),
                }
                map_transformed, map_previous_cumulative = transform_step(
                    map_step,
                    map_previous_cumulative,
                )
                if map_precipitation_total is None:
                    map_precipitation_total = np.zeros(
                        map_grid.point_count,
                        dtype=np.float64,
                    )
                map_precipitation_total += np.nan_to_num(
                    map_transformed["precipitation_mm"],
                    nan=0.0,
                    posinf=0.0,
                    neginf=0.0,
                )
                map_fields = dict(map_transformed)
                map_fields["precipitation_total_mm"] = (
                    map_precipitation_total.copy()
                )
                valid_time: datetime = step["valid_time"]
                if map_renderer is None:
                    assert map_grid.latitudes is not None
                    assert map_grid.longitudes is not None
                    map_renderer = HarmonieMapRenderer(
                        map_grid.latitudes,
                        map_grid.longitudes,
                        result_directory / "maps",
                        france_latitudes=catalog.point_latitudes,
                        france_longitudes=catalog.point_longitudes,
                        france_departments=catalog.point_departments,
                        boundary_directory=(
                            Path(__file__).resolve().parents[1]
                            / "config"
                            / "natural-earth"
                        ),
                    )
                map_renderer.render_step(
                    lead_hour=lead,
                    valid_time=valid_time,
                    fields=map_fields,
                )
                iso_time = valid_time.isoformat().replace("+00:00", "Z")
                for code, department in catalog.departments.items():
                    line = [
                        iso_time,
                        compact_rows(transformed, department.global_point_ids),
                    ]
                    json.dump(
                        line,
                        line_handles[code],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    line_handles[code].write("\n")
                temporary_grib.unlink(missing_ok=True)
    finally:
        for handle in line_handles.values():
            handle.close()
        temporary_grib.unlink(missing_ok=True)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    model = {
        "name": "HARMONIE-AROME Cy43",
        "provider": "KNMI",
        "dataset": base.DATASET_NAME,
        "version": base.DATASET_VERSION,
        "pipeline_version": NATIONAL_PIPELINE_VERSION,
        "catalog_version": catalog.version,
        "domain": "Europe (DINI/N55)",
        "resolution_km": 5.5,
        "forecast_hours_requested": forecast_hours,
        "run_time": (
            model_run.isoformat().replace("+00:00", "Z")
            if model_run is not None
            else None
        ),
        "source_file": source.get("filename"),
        "source_size_bytes": source.get("size"),
        "source_created": source.get("created"),
        "source_url": (
            "https://dataplatform.knmi.nl/dataset/"
            "harmonie-arome-cy43-p3-1-0"
        ),
        "license": "CC BY 4.0",
    }
    if map_renderer is None:
        raise RuntimeError("Aucune carte HARMONIE n'a été produite")
    places_path = result_directory / "maps" / "communes.json"
    places_count = write_map_places(catalog, places_path)
    LOGGER.info("Couche cartographique : %s communes publiées", places_count)
    map_manifest = map_renderer.write_manifest(
        generated_at=generated_at,
        run_time=model["run_time"],
        places_path="maps/communes.json",
    )

    departments_directory = result_directory / "departements"
    departments_directory.mkdir(parents=True, exist_ok=True)
    department_index: dict[str, Any] = {}
    total_size = 0
    for code, department in catalog.departments.items():
        destination = departments_directory / f"{code}.json"
        with destination.open("w", encoding="utf-8") as output:
            output.write("{")
            output.write('"schema_version":2,"status":"ok",')
            output.write('"generated_at":')
            json.dump(generated_at, output)
            output.write(',"department":')
            json.dump(code, output)
            output.write(',"columns":')
            json.dump(
                {
                    "points": ["model_index", "latitude", "longitude"],
                    "communes": [
                        "code_insee",
                        "name",
                        "postal_codes",
                        "population",
                        "latitude",
                        "longitude",
                        "point_id",
                    ],
                    "values": list(VALUE_COLUMNS),
                },
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            output.write(',"points":')
            json.dump(
                department.points,
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            output.write(',"communes":')
            json.dump(
                department.communes,
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            output.write(',"forecast":[')
            first = True
            with (forecast_directory / f"{code}.ndjson").open(
                "r", encoding="utf-8"
            ) as lines:
                for line in lines:
                    if not line.strip():
                        continue
                    if not first:
                        output.write(",")
                    output.write(line.strip())
                    first = False
            output.write("]}\n")

        size = destination.stat().st_size
        total_size += size
        department_index[code] = {
            "file": f"departements/{code}.json",
            "communes": len(department.communes),
            "points": len(department.points),
            "size_bytes": size,
        }

    index = {
        "schema_version": 2,
        "status": "ok",
        "generated_at": generated_at,
        "model": model,
        "coverage": {
            "label": "France métropolitaine et Corse",
            "communes": sum(
                len(department.communes)
                for department in catalog.departments.values()
            ),
            "departments": len(catalog.departments),
        },
        "condition_codes": CONDITION_CODES,
        "search": {
            "provider": "API Découpage administratif — République française",
            "endpoint": "https://geo.api.gouv.fr/communes",
        },
        "maps": {
            "status": "ok",
            "module_version": map_manifest["module_version"],
            "manifest": "maps/index.json",
            "layers": len(map_manifest["layers"]),
            "steps": len(map_manifest["steps"]),
        },
        "departments": department_index,
        "total_department_bytes": total_size,
    }
    with (result_directory / "index.json").open("w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")

    LOGGER.info(
        "Production nationale : %.1f Mo répartis dans %s départements",
        total_size / 1e6,
        len(catalog.departments),
    )
    return result_directory


def publish_local_result(source: Path, output: Path) -> None:
    target = safe_output_directory(output)
    temporary = target.with_name(target.name + ".new")
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(source, temporary)
    if target.exists():
        shutil.rmtree(target)
    temporary.replace(target)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    if not 1 <= args.forecast_hours <= 60:
        raise ValueError("forecast-hours doit être compris entre 1 et 60")

    catalog = load_catalog(Path(args.catalog))
    temporary_archive: Path | None = None
    source: dict[str, Any]

    if args.archive:
        archive = Path(args.archive).resolve()
        source = {
            "filename": archive.name,
            "size": archive.stat().st_size,
            "created": datetime.fromtimestamp(
                archive.stat().st_mtime, timezone.utc
            ).isoformat(),
        }
    else:
        api_key = os.getenv("KNMI_API_KEY", "").strip()
        if not api_key:
            api_key = base.PUBLIC_ANONYMOUS_KEY
            LOGGER.warning(
                "Utilisation de la clé KNMI anonyme publique ; configurez le "
                "secret KNMI_API_KEY pour la production à long terme."
            )
        session = base.api_session(api_key)
        source = base.latest_archive_metadata(session)
        source_filename = str(source.get("filename", ""))
        if not source_filename:
            raise RuntimeError("Nom de l'archive KNMI absent")
        if not args.force and already_published(
            args.current_metadata_url, source_filename
        ):
            LOGGER.info("Cette archive nationale est déjà publiée : %s", source_filename)
            return 0

        temporary_handle = tempfile.NamedTemporaryFile(
            prefix="harmonie-france-", suffix=".tar", delete=False
        )
        temporary_handle.close()
        temporary_archive = Path(temporary_handle.name)
        archive = temporary_archive
        download_url = base.temporary_download_url(session, source_filename)
        expected_size = int(source.get("size") or 0) or None
        base.download_archive(download_url, archive, expected_size)

    try:
        with tempfile.TemporaryDirectory(prefix="harmonie-national-build-") as temporary:
            result = decode_national_archive(
                archive,
                catalog,
                args.forecast_hours,
                source,
                Path(temporary),
            )
            publish_local_result(result, Path(args.output_dir))
    finally:
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)
    LOGGER.info("Fichiers nationaux prêts dans %s", args.output_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        LOGGER.exception("Échec de la mise à jour HARMONIE France")
        raise SystemExit(1)

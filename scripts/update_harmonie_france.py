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


LOGGER = logging.getLogger("harmonie.france")
NATIONAL_PIPELINE_VERSION = "2.4.0"
DEFAULT_CURRENT_METADATA_URL = (
    "https://raw.githubusercontent.com/alertesmeteo-hub/"
    "harmonie-knmi/data/index.json"
)

# Colonnes d'une valeur horaire compacte. Les libellés et icônes de temps sont
# reconstruits par le widget pour éviter de les répéter plusieurs millions de
# fois dans les fichiers JSON.
VALUE_COLUMNS = (
    # Tableau général — conserver les 10 premières positions pour compatibilité.
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
    # Tableau orages — diagnostics directs ou calculés à partir de HARMONIE P3.
    "thunder_risk_code",
    "cape_jkg",
    "cin_jkg",
    "lcl_m",
    "k_index",
    "total_totals",
    "shear_0_1_ms",
    "shear_0_3_ms",
    "shear_0_6_ms",
    "srh_0_1_m2s2",
    "srh_0_3_m2s2",
    "lightning_score",
    "hail_risk_code",
    "heavy_rain_risk_code",
    "severe_wind_risk_code",
    "storm_type_code",
    "temperature_500_c",
    "humidity_700_pct",
    "freezing_level_m",
    "minus20_level_m",
    "omega_500_pas",
    "convective_precipitation_mm",
    "graupel_mm",
    "dewpoint_c",
    # Tableau neige — sorties directes P3 + diagnostics de tenue.
    "snow_risk_code",
    "snowfall_mm",
    "snow_fresh_cm",
    "snow_depth_cm",
    "snow_water_equivalent_mm",
    "surface_temperature_c",
    "snow_stick_risk_code",
    "snow_phase_code",
    "temperature_850_c",
)

PRESSURE_LEVELS = (925, 850, 700, 500, 300)
STANDARD_LEVEL_HEIGHTS_M = {
    925: 750.0,
    850: 1500.0,
    700: 3000.0,
    500: 5600.0,
    300: 9200.0,
}


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

SURFACE_PARAMETERS = {
    "pressure_pa",
    "surface_pressure_pa",
    "surface_geopotential_m2s2",
    "temperature_k",
    "surface_temperature_k",
    "dewpoint_k",
    "visibility_m",
    "wind_u_ms",
    "wind_v_ms",
    "humidity_pct",
    "precipitation_raw_mm",
    "cloud_pct",
    "cloud_low_pct",
    "cloud_mid_pct",
    "cloud_high_pct",
    "gust_u_ms",
    "gust_v_ms",
    "snowfall_raw_mm",
    "snow_depth_m",
    "snow_water_equivalent_mm",
}

PRESSURE_PARAMETERS = {
    f"{prefix}_{level}_{suffix}"
    for level in PRESSURE_LEVELS
    for prefix, suffix in (
        ("temperature", "k"),
        ("humidity", "pct"),
        ("wind_u", "ms"),
        ("wind_v", "ms"),
        ("geopotential", "m2s2"),
        ("omega", "pas"),
    )
}

OPTIONAL_CONVECTIVE_PARAMETERS = {
    "convective_precipitation_raw_mm",
    "graupel_raw_mm",
}

REQUIRED_PARAMETERS = (
    SURFACE_PARAMETERS | PRESSURE_PARAMETERS | OPTIONAL_CONVECTIVE_PARAMETERS
)



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

    communes_by_department: dict[str, list[list[Any]]] = {}
    for commune in raw_communes:
        if not isinstance(commune, list) or len(commune) < 8:
            raise RuntimeError("Entrée communale invalide dans le catalogue")
        communes_by_department.setdefault(str(commune[2]), []).append(commune)

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
        departments=departments,
    )


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


def empty_values(point_count: int) -> np.ndarray:
    return np.full(point_count, np.nan, dtype=np.float64)


def national_parameter_name(gid: int) -> str | None:
    """Reconnaît les champs P3 utiles au tableau général et au diagnostic orageux.

    Les paramètres de surface continuent d'utiliser le mapping historique de
    ``update_harmonie.py``. Les niveaux isobares sont reconnus par leur code
    GRIB1 et leur niveau. Quelques diagnostics convectifs sont aussi détectés
    par ``shortName``/``name`` lorsque le fichier KNMI les expose directement.
    """

    surface_name = base.parameter_name(gid)
    if surface_name is not None:
        return surface_name

    code = base.safe_get_long(gid, "indicatorOfParameter")
    level_type = base.safe_get_long(gid, "indicatorOfTypeOfLevel")
    level = base.safe_get_long(gid, "level")

    if level_type == 100 and level in PRESSURE_LEVELS:
        mapping = {
            6: ("geopotential", "m2s2"),
            11: ("temperature", "k"),
            33: ("wind_u", "ms"),
            34: ("wind_v", "ms"),
            39: ("omega", "pas"),
            52: ("humidity", "pct"),
        }
        if code in mapping:
            prefix, suffix = mapping[code]
            return f"{prefix}_{level}_{suffix}"

    short_name = str(base.safe_get(gid, "shortName", "") or "").strip().lower()
    long_name = str(base.safe_get(gid, "name", "") or "").strip().lower()
    label = f"{short_name} {long_name}"
    if short_name in {"cp", "acpcp"} or "convective precipitation" in label:
        return "convective_precipitation_raw_mm"
    if "graupel" in label or short_name in {"grpl", "graupel"}:
        return "graupel_raw_mm"
    return None


def normalize_relative_humidity(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64).copy()
    finite = values[np.isfinite(values)]
    if finite.size and np.nanpercentile(np.abs(finite), 95) <= 1.5:
        values *= 100.0
    return np.clip(values, 0.0, 100.0)


def dewpoint_from_temperature_rh(
    temperature_c: np.ndarray,
    humidity_pct: np.ndarray,
) -> np.ndarray:
    """Point de rosée par la formule de Magnus, en degrés Celsius."""

    temperature_c = np.asarray(temperature_c, dtype=np.float64)
    rh = np.clip(np.asarray(humidity_pct, dtype=np.float64), 0.1, 100.0)
    a = 17.625
    b = 243.04
    gamma = np.log(rh / 100.0) + (a * temperature_c) / (b + temperature_c)
    dewpoint = (b * gamma) / (a - gamma)
    dewpoint[~np.isfinite(temperature_c) | ~np.isfinite(humidity_pct)] = np.nan
    return dewpoint



def saturation_vapor_pressure_hpa(temperature_c: np.ndarray) -> np.ndarray:
    """Pression de vapeur saturante (Magnus), hPa."""
    t = np.asarray(temperature_c, dtype=np.float64)
    return 6.112 * np.exp((17.67 * t) / (t + 243.5))


def mixing_ratio_kgkg(
    temperature_c: np.ndarray,
    humidity_pct: np.ndarray,
    pressure_hpa: np.ndarray,
) -> np.ndarray:
    """Rapport de mélange à partir de T, RH et pression."""
    t = np.asarray(temperature_c, dtype=np.float64)
    rh = np.clip(np.asarray(humidity_pct, dtype=np.float64), 0.0, 100.0)
    p = np.asarray(pressure_hpa, dtype=np.float64)
    e = saturation_vapor_pressure_hpa(t) * rh / 100.0
    e = np.minimum(e, np.maximum(p - 0.1, 0.1))
    w = 0.622 * e / np.maximum(p - e, 0.1)
    w[~np.isfinite(t) | ~np.isfinite(rh) | ~np.isfinite(p) | (p <= 0)] = np.nan
    return w


def saturation_mixing_ratio_kgkg(
    temperature_k: np.ndarray,
    pressure_hpa: np.ndarray,
) -> np.ndarray:
    t_c = np.asarray(temperature_k, dtype=np.float64) - 273.15
    p = np.asarray(pressure_hpa, dtype=np.float64)
    e = saturation_vapor_pressure_hpa(t_c)
    e = np.minimum(e, np.maximum(p - 0.1, 0.1))
    return 0.622 * e / np.maximum(p - e, 0.1)


def approximate_cape_cin_p3(
    temperature_c: np.ndarray,
    dewpoint_c: np.ndarray,
    surface_pressure_hpa: np.ndarray,
    t_levels: dict[int, np.ndarray],
    rh_levels: dict[int, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """Estime SBCAPE/CIN à partir du profil P3 clairsemé.

    P3 Europe ne contient pas de CAPE/CIN directs. On intègre donc la flottabilité
    d'une parcelle de surface sur un profil vertical interpolé entre 2 m et les
    niveaux 925/850/700/500/300 hPa. Le résultat est un diagnostic approché,
    volontairement signalé comme tel dans le widget.
    """
    t0_c = np.asarray(temperature_c, dtype=np.float64)
    td0_c = np.asarray(dewpoint_c, dtype=np.float64)
    ps = np.asarray(surface_pressure_hpa, dtype=np.float64)
    valid = np.isfinite(t0_c) & np.isfinite(td0_c) & np.isfinite(ps) & (ps > 700.0)
    for level in (925, 850, 700, 500, 300):
        valid &= np.isfinite(t_levels[level]) & np.isfinite(rh_levels[level])

    # Pour les rares points où PSRF ou le profil P3 serait absent, le calcul restera NaN.
    t0_k = t0_c + 273.15
    td0_k = td0_c + 273.15
    lcl_m = np.clip(125.0 * (t0_c - td0_c), 0.0, 5000.0)
    rh_surface = np.full_like(t0_c, 100.0)
    # Le rapport de mélange de la parcelle est fixé par le Td de surface.
    e0 = saturation_vapor_pressure_hpa(td0_c)
    w0 = 0.622 * e0 / np.maximum(ps - e0, 0.1)

    heights = np.array([0.0, 750.0, 1500.0, 3000.0, 5600.0, 9200.0])
    pressure_scalars = [None, 925.0, 850.0, 700.0, 500.0, 300.0]
    temp_anchors = [t0_c, t_levels[925], t_levels[850], t_levels[700], t_levels[500], t_levels[300]]
    rh_anchors = [rh_surface, rh_levels[925], rh_levels[850], rh_levels[700], rh_levels[500], rh_levels[300]]

    def environment_at_height(z: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        idx = int(np.searchsorted(heights, z, side="right") - 1)
        idx = max(0, min(idx, len(heights) - 2))
        z0, z1 = heights[idx], heights[idx + 1]
        f = (z - z0) / (z1 - z0)
        te = temp_anchors[idx] + f * (temp_anchors[idx + 1] - temp_anchors[idx])
        rhe = rh_anchors[idx] + f * (rh_anchors[idx + 1] - rh_anchors[idx])
        p0 = ps if idx == 0 else np.full_like(ps, pressure_scalars[idx])
        p1 = np.full_like(ps, pressure_scalars[idx + 1])
        pe = np.exp(np.log(np.maximum(p0, 1.0)) + f * (np.log(p1) - np.log(np.maximum(p0, 1.0))))
        return te, np.clip(rhe, 0.0, 100.0), pe

    parcel_t = t0_k.copy()
    cape = np.zeros_like(t0_c)
    cin = np.zeros_like(t0_c)
    lfc_found = np.zeros_like(t0_c, dtype=bool)

    env_t0, env_rh0, p_prev = environment_at_height(0.0)
    env_w0 = mixing_ratio_kgkg(env_t0, env_rh0, p_prev)
    env_q0 = env_w0 / (1.0 + env_w0)
    parcel_q0 = w0 / (1.0 + w0)
    tv_env_prev = (env_t0 + 273.15) * (1.0 + 0.61 * env_q0)
    tv_par_prev = parcel_t * (1.0 + 0.61 * parcel_q0)
    b_prev = 9.80665 * (tv_par_prev - tv_env_prev) / tv_env_prev

    dz = 250.0
    z = 0.0
    while z < 9000.0:
        z_next = z + dz
        env_t, env_rh, p_next = environment_at_height(z_next)

        dry_dz = np.clip(lcl_m - z, 0.0, dz)
        moist_dz = dz - dry_dz
        parcel_t = parcel_t - 0.0098 * dry_dz

        if np.any(moist_dz > 0.0):
            ws = saturation_mixing_ratio_kgkg(parcel_t, p_next)
            # Pseudoadiabatique : formule standard de gradient humide.
            lv = 2.5e6
            rd = 287.05
            cp = 1004.0
            eps = 0.622
            numerator = 9.80665 * (1.0 + lv * ws / (rd * parcel_t))
            denominator = cp + (lv * lv * ws * eps) / (rd * parcel_t * parcel_t)
            gamma_m = np.clip(numerator / denominator, 0.003, 0.0098)
            parcel_t = parcel_t - gamma_m * moist_dz

        env_w = mixing_ratio_kgkg(env_t, env_rh, p_next)
        env_q = env_w / (1.0 + env_w)
        parcel_ws = saturation_mixing_ratio_kgkg(parcel_t, p_next)
        parcel_w = np.where(z_next <= lcl_m, w0, parcel_ws)
        parcel_q = parcel_w / (1.0 + parcel_w)

        tv_env = (env_t + 273.15) * (1.0 + 0.61 * env_q)
        tv_par = parcel_t * (1.0 + 0.61 * parcel_q)
        b = 9.80665 * (tv_par - tv_env) / tv_env
        b_avg = 0.5 * (b_prev + b)

        can_find_lfc = valid & (z_next >= lcl_m) & np.isfinite(b) & (b > 0.0)
        newly_lfc = (~lfc_found) & can_find_lfc
        before_lfc = valid & (~lfc_found)
        cin += np.where(before_lfc & np.isfinite(b_avg) & (b_avg < 0.0), b_avg * dz, 0.0)
        lfc_found |= newly_lfc
        cape += np.where(lfc_found & np.isfinite(b_avg) & (b_avg > 0.0), b_avg * dz, 0.0)

        b_prev = b
        p_prev = p_next
        z = z_next

    cape = np.where(valid, np.maximum(cape, 0.0), np.nan)
    cin = np.where(valid, np.minimum(cin, 0.0), np.nan)
    cin = np.where(np.isfinite(cin), np.maximum(cin, -1000.0), np.nan)
    # P3 ne comporte que quelques niveaux isobares : une intégrale quasi nulle
    # n'est pas assez robuste pour être affichée comme « 0 ». On la publie
    # comme donnée indisponible / non significative ; le widget affichera « — ».
    cape = np.where(np.isfinite(cape) & (cape < 25.0), np.nan, cape)
    cin = np.where(np.isfinite(cin) & (np.abs(cin) < 5.0), np.nan, cin)
    cin = np.where(np.isfinite(cape), cin, np.nan)
    return cape, cin

def rotate_uv(
    u: np.ndarray,
    v: np.ndarray,
    angle_rad: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    east = u * np.cos(angle_rad) + v * np.sin(angle_rad)
    north = -u * np.sin(angle_rad) + v * np.cos(angle_rad)
    return east, north


def linear_vector_at_height(
    low_u: np.ndarray,
    low_v: np.ndarray,
    low_height: float,
    high_u: np.ndarray,
    high_v: np.ndarray,
    high_height: float,
    target_height: float,
) -> tuple[np.ndarray, np.ndarray]:
    fraction = (target_height - low_height) / (high_height - low_height)
    return (
        low_u + fraction * (high_u - low_u),
        low_v + fraction * (high_v - low_v),
    )


def crossing_height(
    temperatures: list[np.ndarray],
    heights: list[float],
    target_temperature: float,
) -> np.ndarray:
    """Première altitude où le profil thermique coupe une isotherme."""

    result = np.full_like(temperatures[0], np.nan, dtype=np.float64)
    for index in range(len(temperatures) - 1):
        t0 = temperatures[index]
        t1 = temperatures[index + 1]
        valid = np.isfinite(t0) & np.isfinite(t1) & ~np.isfinite(result)
        crossing = valid & ((t0 - target_temperature) * (t1 - target_temperature) <= 0)
        crossing &= np.abs(t1 - t0) > 1.0e-6
        if not np.any(crossing):
            continue
        fraction = (target_temperature - t0) / (t1 - t0)
        height = heights[index] + fraction * (heights[index + 1] - heights[index])
        result[crossing] = height[crossing]
    return result


def approximate_srh(
    u_profile: list[np.ndarray],
    v_profile: list[np.ndarray],
    storm_u: np.ndarray,
    storm_v: np.ndarray,
) -> np.ndarray:
    """SRH discrète sur le profil P3 ; valeur absolue en m²/s².

    Le profil P3 étant plus clairsemé qu'un radiosondage/P5, cette SRH est un
    diagnostic d'organisation et non une reproduction exacte d'un calcul sur
    90 niveaux modèle.
    """

    result = np.zeros_like(storm_u, dtype=np.float64)
    count = np.zeros_like(storm_u, dtype=np.int16)
    for index in range(len(u_profile) - 1):
        u0, u1 = u_profile[index], u_profile[index + 1]
        v0, v1 = v_profile[index], v_profile[index + 1]
        valid = (
            np.isfinite(u0) & np.isfinite(u1) & np.isfinite(v0) & np.isfinite(v1)
            & np.isfinite(storm_u) & np.isfinite(storm_v)
        )
        term = (u1 - storm_u) * (v0 - storm_v) - (u0 - storm_u) * (v1 - storm_v)
        result += np.where(valid, term, 0.0)
        count += valid.astype(np.int16)
    result[count == 0] = np.nan
    return np.abs(result)


def risk_code(score: np.ndarray) -> np.ndarray:
    result = np.zeros(score.shape, dtype=np.int16)
    result[np.isfinite(score) & (score >= 20)] = 1
    result[np.isfinite(score) & (score >= 40)] = 2
    result[np.isfinite(score) & (score >= 60)] = 3
    result[np.isfinite(score) & (score >= 80)] = 4
    return result

def parse_grib_file(
    path: Path,
    grid: NationalGrid,
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
        "rotations": None,
    }
    with path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                name = national_parameter_name(gid)
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
    temperature_calc = raw["temperature_k"] - 273.15
    humidity_calc = normalize_relative_humidity(raw["humidity_pct"])
    temperature = rounded(temperature_calc, 0)
    humidity = rounded(humidity_calc, 0)
    cloud = rounded(normalize_relative_humidity(raw["cloud_pct"]), 0)
    pressure = rounded(raw["pressure_pa"] / 100.0, 0)
    visibility = rounded(raw["visibility_m"] / 1000.0, 1)
    surface_pressure = raw["surface_pressure_pa"] / 100.0
    surface_temperature = rounded(raw["surface_temperature_k"] - 273.15, 0)
    model_altitude_m = rounded(raw["surface_geopotential_m2s2"] / 9.80665, 0)
    snowfall = rounded(np.maximum(raw["snowfall_raw_mm"], 0.0), 1)
    snow_depth_cm = rounded(np.maximum(raw["snow_depth_m"], 0.0) * 100.0, 1)
    snow_water_equivalent = rounded(np.maximum(raw["snow_water_equivalent_mm"], 0.0), 1)

    dewpoint_direct = raw["dewpoint_k"] - 273.15
    dewpoint_derived = dewpoint_from_temperature_rh(temperature_calc, humidity_calc)
    dewpoint_calc = np.where(np.isfinite(dewpoint_direct), dewpoint_direct, dewpoint_derived)
    dewpoint = rounded(dewpoint_calc, 1)
    lcl = rounded(np.clip(125.0 * (temperature_calc - dewpoint_calc), 0.0, 5000.0), 0)

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

    convective_precipitation = rounded(
        np.maximum(raw["convective_precipitation_raw_mm"], 0.0), 1
    )
    graupel = rounded(np.maximum(raw["graupel_raw_mm"], 0.0), 2)

    angle = np.radians(step["rotations"])
    east, north = rotate_uv(raw["wind_u_ms"], raw["wind_v_ms"], angle)
    wind_speed = rounded(np.hypot(raw["wind_u_ms"], raw["wind_v_ms"]) * 3.6, 0)
    wind_direction = rounded(np.degrees(np.arctan2(-east, -north)) % 360.0, 0)
    gust_speed = rounded(
        np.hypot(raw["gust_u_ms"], raw["gust_v_ms"]) * 3.6,
        0,
    )

    condition = np.zeros(len(temperature), dtype=np.int16)
    condition[np.isfinite(cloud) & (cloud <= 20)] = 1
    condition[np.isfinite(cloud) & (cloud > 20) & (cloud <= 55)] = 2
    condition[np.isfinite(cloud) & (cloud > 55) & (cloud <= 85)] = 3
    condition[np.isfinite(cloud) & (cloud > 85)] = 4
    condition[np.isfinite(gust_speed) & (gust_speed >= 70)] = 9
    condition[np.isfinite(precipitation) & (precipitation >= 0.1)] = 5
    condition[np.isfinite(precipitation) & (precipitation >= 5.0)] = 6
    direct_snow = np.isfinite(snowfall) & (snowfall >= 0.05)
    fallback_snow = (
        ~np.isfinite(snowfall)
        & np.isfinite(precipitation) & (precipitation >= 0.1)
        & np.isfinite(temperature_calc) & (temperature_calc <= 1.0)
    )
    condition[direct_snow | fallback_snow] = 7
    condition[np.isfinite(visibility) & (visibility < 1.0)] = 8

    # Profils P3 sur niveaux isobares.
    t_levels: dict[int, np.ndarray] = {}
    rh_levels: dict[int, np.ndarray] = {}
    u_levels: dict[int, np.ndarray] = {}
    v_levels: dict[int, np.ndarray] = {}
    for level in PRESSURE_LEVELS:
        t_levels[level] = raw[f"temperature_{level}_k"] - 273.15
        rh_levels[level] = normalize_relative_humidity(raw[f"humidity_{level}_pct"])
        u_levels[level], v_levels[level] = rotate_uv(
            raw[f"wind_u_{level}_ms"], raw[f"wind_v_{level}_ms"], angle
        )

    td850 = dewpoint_from_temperature_rh(t_levels[850], rh_levels[850])
    td700 = dewpoint_from_temperature_rh(t_levels[700], rh_levels[700])
    k_index = (
        t_levels[850] - t_levels[500]
        + td850
        - (t_levels[700] - td700)
    )
    total_totals = t_levels[850] + td850 - 2.0 * t_levels[500]

    # Cisaillements avec interpolation aux hauteurs standard P3.
    u_1km, v_1km = linear_vector_at_height(
        u_levels[925], v_levels[925], STANDARD_LEVEL_HEIGHTS_M[925],
        u_levels[850], v_levels[850], STANDARD_LEVEL_HEIGHTS_M[850], 1000.0,
    )
    u_3km, v_3km = u_levels[700], v_levels[700]
    u_6km, v_6km = linear_vector_at_height(
        u_levels[500], v_levels[500], STANDARD_LEVEL_HEIGHTS_M[500],
        u_levels[300], v_levels[300], STANDARD_LEVEL_HEIGHTS_M[300], 6000.0,
    )
    shear_0_1 = np.hypot(u_1km - east, v_1km - north)
    shear_0_3 = np.hypot(u_3km - east, v_3km - north)
    shear_0_6 = np.hypot(u_6km - east, v_6km - north)

    # Mouvement de Bunkers droit simplifié et SRH sur le profil P3.
    profile_u = np.vstack([
        east, u_levels[925], u_levels[850], u_levels[700], u_levels[500], u_6km
    ])
    profile_v = np.vstack([
        north, v_levels[925], v_levels[850], v_levels[700], v_levels[500], v_6km
    ])
    valid_u = np.isfinite(profile_u)
    valid_v = np.isfinite(profile_v)
    count_u = valid_u.sum(axis=0)
    count_v = valid_v.sum(axis=0)
    mean_u = np.full(profile_u.shape[1], np.nan, dtype=np.float64)
    mean_v = np.full(profile_v.shape[1], np.nan, dtype=np.float64)
    np.divide(np.nansum(profile_u, axis=0), count_u, out=mean_u, where=count_u > 0)
    np.divide(np.nansum(profile_v, axis=0), count_v, out=mean_v, where=count_v > 0)
    shear_u = u_6km - east
    shear_v = v_6km - north
    shear_mag = np.hypot(shear_u, shear_v)
    right_u = np.zeros_like(shear_mag)
    right_v = np.zeros_like(shear_mag)
    np.divide(7.5 * shear_v, shear_mag, out=right_u, where=shear_mag > 0.1)
    np.divide(7.5 * shear_u, shear_mag, out=right_v, where=shear_mag > 0.1)
    storm_u = mean_u + right_u
    storm_v = mean_v - right_v
    srh_0_1 = approximate_srh(
        [east, u_levels[925], u_1km],
        [north, v_levels[925], v_1km],
        storm_u,
        storm_v,
    )
    srh_0_3 = approximate_srh(
        [east, u_levels[925], u_levels[850], u_3km],
        [north, v_levels[925], v_levels[850], v_3km],
        storm_u,
        storm_v,
    )

    temperature_profile = [
        temperature,
        t_levels[925],
        t_levels[850],
        t_levels[700],
        t_levels[500],
        t_levels[300],
    ]
    height_profile = [0.0, 750.0, 1500.0, 3000.0, 5600.0, 9200.0]
    freezing_level = crossing_height(temperature_profile, height_profile, 0.0)
    minus20_level = crossing_height(temperature_profile, height_profile, -20.0)

    cape, cin = approximate_cape_cin_p3(
        temperature_calc,
        dewpoint_calc,
        surface_pressure,
        t_levels,
        rh_levels,
    )
    omega_500 = raw["omega_500_pas"].copy()

    # Score convectif : CAPE/CIN estimés sur P3 + indices K/TT.
    score = np.zeros(len(temperature), dtype=np.float64)
    score += np.where(np.isfinite(cape), np.clip(cape / 50.0, 0.0, 30.0), 0.0)
    score += np.where(np.isfinite(k_index), np.clip((k_index - 15.0) * 1.25, 0.0, 25.0), 0.0)
    score += np.where(np.isfinite(total_totals), np.clip((total_totals - 38.0) * 1.25, 0.0, 20.0), 0.0)
    score += np.where(np.isfinite(rh_levels[700]) & (rh_levels[700] >= 60.0), 5.0, 0.0)
    score += np.where(np.isfinite(rh_levels[700]) & (rh_levels[700] >= 75.0), 5.0, 0.0)
    score += np.where(np.isfinite(precipitation) & (precipitation >= 0.2), 5.0, 0.0)
    score += np.where(np.isfinite(precipitation) & (precipitation >= 2.0), 5.0, 0.0)
    score += np.where(np.isfinite(precipitation) & (precipitation >= 5.0), 5.0, 0.0)
    score += np.where(np.isfinite(shear_0_6) & (shear_0_6 >= 15.0), 5.0, 0.0)
    score += np.where(np.isfinite(shear_0_6) & (shear_0_6 >= 22.0), 5.0, 0.0)
    score += np.where(np.isfinite(omega_500) & (omega_500 <= -0.15), 5.0, 0.0)
    score = np.clip(score, 0.0, 100.0)
    thunder_risk = risk_code(score)

    lightning_score = np.zeros(len(temperature), dtype=np.float64)
    lightning_score += np.where(np.isfinite(cape), np.clip(cape / 40.0, 0.0, 35.0), 0.0)
    lightning_score += np.where(np.isfinite(k_index), np.clip((k_index - 18.0) * 1.4, 0.0, 25.0), 0.0)
    lightning_score += np.where(np.isfinite(total_totals), np.clip((total_totals - 40.0) * 1.4, 0.0, 20.0), 0.0)
    lightning_score += np.where(np.isfinite(convective_precipitation) & (convective_precipitation > 0.1), 10.0, 0.0)
    lightning_score += np.where(np.isfinite(graupel) & (graupel > 0.0), 10.0, 0.0)
    lightning_score += np.where(np.isfinite(precipitation) & (precipitation >= 2.0), 5.0, 0.0)
    lightning_score = np.clip(lightning_score, 0.0, 100.0)

    hail_points = np.zeros(len(temperature), dtype=np.int16)
    hail_points += (np.isfinite(t_levels[500]) & (t_levels[500] <= -16.0)).astype(np.int16)
    hail_points += (np.isfinite(t_levels[500]) & (t_levels[500] <= -20.0)).astype(np.int16)
    hail_points += (np.isfinite(total_totals) & (total_totals >= 45.0)).astype(np.int16)
    hail_points += (np.isfinite(total_totals) & (total_totals >= 50.0)).astype(np.int16)
    hail_points += (np.isfinite(shear_0_6) & (shear_0_6 >= 15.0)).astype(np.int16)
    hail_points += (np.isfinite(shear_0_6) & (shear_0_6 >= 20.0)).astype(np.int16)
    hail_points += (np.isfinite(cape) & (cape >= 1000.0)).astype(np.int16)
    hail_points += (np.isfinite(graupel) & (graupel > 0.0)).astype(np.int16) * 2
    hail_risk = np.zeros(len(temperature), dtype=np.int16)
    hail_risk[hail_points >= 2] = 1
    hail_risk[hail_points >= 4] = 2
    hail_risk[hail_points >= 6] = 3

    heavy_rain_risk = np.zeros(len(temperature), dtype=np.int16)
    heavy_rain_risk[np.isfinite(precipitation) & (precipitation >= 2.0)] = 1
    heavy_rain_risk[np.isfinite(precipitation) & (precipitation >= 5.0)] = 2
    heavy_rain_risk[np.isfinite(precipitation) & (precipitation >= 15.0)] = 3
    moisture_boost = (
        np.isfinite(k_index) & (k_index >= 30.0)
        & np.isfinite(rh_levels[700]) & (rh_levels[700] >= 75.0)
        & np.isfinite(precipitation) & (precipitation >= 1.0)
    )
    heavy_rain_risk[moisture_boost] = np.maximum(heavy_rain_risk[moisture_boost], 2)

    severe_wind_risk = np.zeros(len(temperature), dtype=np.int16)
    severe_wind_risk[np.isfinite(gust_speed) & (gust_speed >= 50.0)] = 1
    severe_wind_risk[np.isfinite(gust_speed) & (gust_speed >= 70.0)] = 2
    severe_wind_risk[np.isfinite(gust_speed) & (gust_speed >= 90.0)] = 3
    organized_wind = (
        (thunder_risk >= 2) & np.isfinite(shear_0_6) & (shear_0_6 >= 20.0)
    )
    severe_wind_risk[organized_wind] = np.maximum(severe_wind_risk[organized_wind], 2)

    storm_type = np.zeros(len(temperature), dtype=np.int16)
    storm_type[thunder_risk >= 1] = 1  # cellules isolées / faible organisation
    multicell = (thunder_risk >= 2) & np.isfinite(shear_0_6) & (shear_0_6 >= 12.0)
    storm_type[multicell] = 2
    line_mcs = (
        (thunder_risk >= 3)
        & np.isfinite(shear_0_6) & (shear_0_6 >= 18.0)
        & np.isfinite(precipitation) & (precipitation >= 3.0)
    )
    storm_type[line_mcs] = 3
    strong_instability = (
        (np.isfinite(cape) & (cape >= 800.0))
        | (np.isfinite(total_totals) & (total_totals >= 50.0))
    )
    supercell = (
        (thunder_risk >= 3)
        & np.isfinite(shear_0_6) & (shear_0_6 >= 20.0)
        & np.isfinite(srh_0_3) & (srh_0_3 >= 150.0)
        & strong_instability
    )
    storm_type[supercell] = 4

    # --- Diagnostic neige P3 ---
    # Estimation de neige fraîche à partir de l'équivalent en eau HGTY.
    snow_ratio_cm_per_mm = np.select(
        [temperature_calc <= -8.0, temperature_calc <= -3.0, temperature_calc <= 1.0, temperature_calc <= 2.0],
        [1.5, 1.2, 1.0, 0.7],
        default=0.4,
    )
    snow_fresh_cm = rounded(snowfall * snow_ratio_cm_per_mm, 1)

    snow_risk = np.zeros(len(temperature), dtype=np.int16)
    snow_risk[np.isfinite(snowfall) & (snowfall >= 0.05)] = 1
    snow_risk[np.isfinite(snowfall) & (snowfall >= 0.5)] = 2
    snow_risk[np.isfinite(snowfall) & (snowfall >= 1.5)] = 3
    snow_risk[np.isfinite(snowfall) & (snowfall >= 3.0)] = 4
    cold_boost = (
        np.isfinite(snowfall) & (snowfall >= 0.2)
        & np.isfinite(temperature_calc) & (temperature_calc <= 0.5)
    )
    snow_risk[cold_boost] = np.minimum(4, snow_risk[cold_boost] + 1)
    low_iso_boost = (
        np.isfinite(snowfall) & (snowfall >= 0.2)
        & np.isfinite(freezing_level) & (freezing_level <= 500.0)
    )
    snow_risk[low_iso_boost] = np.maximum(snow_risk[low_iso_boost], 2)

    snow_stick = np.zeros(len(temperature), dtype=np.int16)
    snow_stick[np.isfinite(snowfall) & (snowfall >= 0.05)] = 1
    stick_moderate = (
        np.isfinite(snowfall) & (snowfall >= 0.2)
        & np.isfinite(surface_temperature) & (surface_temperature <= 1.0)
    )
    snow_stick[stick_moderate] = 2
    stick_high = (
        np.isfinite(snowfall) & (snowfall >= 0.5)
        & np.isfinite(surface_temperature) & (surface_temperature <= 0.0)
    )
    snow_stick[stick_high] = 3
    snow_stick[np.isfinite(snow_depth_cm) & (snow_depth_cm >= 1.0)] = np.maximum(
        snow_stick[np.isfinite(snow_depth_cm) & (snow_depth_cm >= 1.0)], 2
    )

    snow_phase = np.zeros(len(temperature), dtype=np.int16)  # 0 aucun, 1 pluie, 2 mixte, 3 neige
    precip_present = np.isfinite(precipitation) & (precipitation >= 0.05)
    snow_phase[precip_present] = 1
    snow_fraction = np.zeros(len(temperature), dtype=np.float64)
    np.divide(
        snowfall,
        np.maximum(precipitation, 0.01),
        out=snow_fraction,
        where=np.isfinite(snowfall) & np.isfinite(precipitation),
    )
    snow_phase[precip_present & (snow_fraction >= 0.15)] = 2
    snow_phase[precip_present & (snow_fraction >= 0.65)] = 3
    snow_phase[np.isfinite(snowfall) & (snowfall >= 0.05) & ~precip_present] = 3

    return (
        {
            "temperature_c": temperature,
            "humidity_pct": humidity,
            "precipitation_mm": precipitation,
            "cloud_cover_pct": cloud,
            "wind_speed_kmh": wind_speed,
            "wind_direction_deg": wind_direction,
            "wind_gust_kmh": gust_speed,
            "pressure_hpa": pressure,
            "visibility_km": visibility,
            "condition_code": condition,
            "thunder_risk_code": thunder_risk,
            "cape_jkg": rounded(cape, 0),
            "cin_jkg": rounded(cin, 0),
            "lcl_m": lcl,
            "k_index": rounded(k_index, 1),
            "total_totals": rounded(total_totals, 1),
            "shear_0_1_ms": rounded(shear_0_1, 1),
            "shear_0_3_ms": rounded(shear_0_3, 1),
            "shear_0_6_ms": rounded(shear_0_6, 1),
            "srh_0_1_m2s2": rounded(srh_0_1, 0),
            "srh_0_3_m2s2": rounded(srh_0_3, 0),
            "lightning_score": rounded(lightning_score, 0),
            "hail_risk_code": hail_risk,
            "heavy_rain_risk_code": heavy_rain_risk,
            "severe_wind_risk_code": severe_wind_risk,
            "storm_type_code": storm_type,
            "temperature_500_c": rounded(t_levels[500], 1),
            "humidity_700_pct": rounded(rh_levels[700], 0),
            "freezing_level_m": rounded(freezing_level, 0),
            "minus20_level_m": rounded(minus20_level, 0),
            "omega_500_pas": rounded(omega_500, 2),
            "convective_precipitation_mm": convective_precipitation,
            "graupel_mm": graupel,
            "dewpoint_c": dewpoint,
            "snow_risk_code": snow_risk,
            "snowfall_mm": snowfall,
            "snow_fresh_cm": snow_fresh_cm,
            "snow_depth_cm": snow_depth_cm,
            "snow_water_equivalent_mm": snow_water_equivalent,
            "surface_temperature_c": surface_temperature,
            "snow_stick_risk_code": snow_stick,
            "snow_phase_code": snow_phase,
            "temperature_850_c": rounded(t_levels[850], 0),
            "model_altitude_m": model_altitude_m,
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
    integer_columns = {
        "temperature_c",
        "humidity_pct",
        "cloud_cover_pct",
        "wind_speed_kmh",
        "wind_direction_deg",
        "wind_gust_kmh",
        "pressure_hpa",
        "condition_code",
        "thunder_risk_code",
        "cape_jkg",
        "cin_jkg",
        "lcl_m",
        "srh_0_1_m2s2",
        "srh_0_3_m2s2",
        "lightning_score",
        "hail_risk_code",
        "heavy_rain_risk_code",
        "severe_wind_risk_code",
        "storm_type_code",
        "humidity_700_pct",
        "freezing_level_m",
        "minus20_level_m",
        "surface_temperature_c",
        "temperature_850_c",
        "snow_risk_code",
        "snow_stick_risk_code",
        "snow_phase_code",
    }
    for point_id in point_ids:
        position = int(point_id)
        row: list[int | float | None] = []
        for column in VALUE_COLUMNS:
            row.append(
                json_number(
                    transformed[column][position],
                    integer=column in integer_columns,
                )
            )
        rows.append(row)
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
    previous_cumulative: np.ndarray | None = None
    model_run: datetime | None = None
    model_altitudes: np.ndarray | None = None
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
                step = parse_grib_file(temporary_grib, grid, lead, run)
                if model_run is None:
                    model_run = step.get("run_time")
                transformed, previous_cumulative = transform_step(
                    step, previous_cumulative
                )
                if model_altitudes is None:
                    model_altitudes = transformed["model_altitude_m"].copy()
                valid_time: datetime = step["valid_time"]
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
        "storm_diagnostics": "P3 pressure levels + sparse CAPE/CIN diagnostic + direct snow diagnostics",
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
                    "points": ["model_index", "latitude", "longitude", "model_altitude_m"],
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
            enhanced_points = []
            for point, global_id in zip(department.points, department.global_point_ids):
                altitude = json_number(model_altitudes[int(global_id)], integer=True)
                enhanced_points.append(point + [altitude])
            json.dump(
                enhanced_points,
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
        "storm_risk_codes": {0: "minimal", 1: "low", 2: "moderate", 3: "strong", 4: "severe"},
        "storm_type_codes": {0: "none", 1: "isolated", 2: "multicell", 3: "line_mcs", 4: "supercell_potential"},
        "snow_risk_codes": {0: "none", 1: "low", 2: "moderate", 3: "strong", 4: "very_strong"},
        "snow_stick_risk_codes": {0: "none", 1: "low", 2: "moderate", 3: "high"},
        "snow_phase_codes": {0: "none", 1: "rain", 2: "mixed", 3: "snow"},
        "search": {
            "provider": "API Découpage administratif — République française",
            "endpoint": "https://geo.api.gouv.fr/communes",
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
        try:
            source = base.latest_archive_metadata(session)
        except base.KNMIRateLimitError as exc:
            LOGGER.warning(
                "%s Conservation de la dernière production nationale publiée ; "
                "aucune donnée existante n'est supprimée.",
                exc,
            )
            return 0
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

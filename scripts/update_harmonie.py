#!/usr/bin/env python3
"""Télécharge et extrait des prévisions ponctuelles HARMONIE-AROME KNMI.

Le script interroge directement l'Open Data API du KNMI, télécharge la dernière
archive du jeu HARMONIE-AROME Cy43 P3, lit les messages GRIB1 avec ecCodes et
écrit un petit JSON destiné au module WordPress fourni avec ce dépôt.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import shutil
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from eccodes import (
    codes_get,
    codes_get_double_array,
    codes_get_double_elements,
    codes_get_long,
    codes_grib_find_nearest,
    codes_grib_new_from_file,
    codes_is_defined,
    codes_release,
)


LOGGER = logging.getLogger("harmonie")

API_BASE = "https://api.dataplatform.knmi.nl/open-data/v1"
DATASET_NAME = "harmonie_arome_cy43_p3"
DATASET_VERSION = "1.0"
PIPELINE_VERSION = "1.3.1"

# Clé anonyme publiée par le KNMI, valable jusqu'au 1er août 2027. Une clé
# personnelle placée dans le secret GitHub KNMI_API_KEY la remplace aussitôt.
PUBLIC_ANONYMOUS_KEY = (
    "eyJvcmciOiI1ZTU1NGUxOTI3NGE5NjAwMDEyYTNlYjEiLCJpZCI6"
    "IjUzYTg1ZDBhMmQ5YzRkYzJiYWNlNzQ4NTQ2Zjk4ODExIiwiaCI6"
    "Im11cm11cjEyOCJ9"
)

MAX_ARCHIVE_BYTES = 8_000_000_000
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
MAX_API_ATTEMPTS = 9
MAX_DOWNLOAD_ATTEMPTS = 4
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


class KNMIError(RuntimeError):
    """Erreur explicite provenant de l'API KNMI."""


class KNMIRateLimitError(KNMIError):
    """Quota / limitation temporaire KNMI (HTTP 429)."""

MEMBER_RE = re.compile(
    r"HA43_[A-Z0-9]+_(?P<run>\d{12})_(?P<lead>\d{5})_GB(?:\.[A-Za-z0-9]+)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Location:
    slug: str
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    code: int
    level_type: int
    level: int
    tri: int | None = None


PARAMETERS = (
    ParameterSpec("pressure_pa", 1, 103, 0, 0),
    ParameterSpec("surface_pressure_pa", 1, 105, 0, 0),
    ParameterSpec("surface_geopotential_m2s2", 6, 105, 0, 0),
    ParameterSpec("temperature_k", 11, 105, 2, 0),
    ParameterSpec("surface_temperature_k", 11, 105, 0, 0),
    ParameterSpec("dewpoint_k", 17, 105, 2, 0),
    ParameterSpec("visibility_m", 20, 105, 0, 0),
    ParameterSpec("wind_u_ms", 33, 105, 10, 0),
    ParameterSpec("wind_v_ms", 34, 105, 10, 0),
    ParameterSpec("humidity_pct", 52, 105, 2, 0),
    ParameterSpec("precipitation_raw_mm", 61, 105, 0, 4),
    ParameterSpec("cloud_pct", 71, 105, 0, 0),
    ParameterSpec("cloud_low_pct", 73, 105, 0, 0),
    ParameterSpec("cloud_mid_pct", 74, 105, 0, 0),
    ParameterSpec("cloud_high_pct", 75, 105, 0, 0),
    ParameterSpec("snow_water_equivalent_mm", 65, 105, 0, 0),
    ParameterSpec("snow_depth_m", 66, 105, 0, 0),
    ParameterSpec("snowfall_raw_mm", 184, 105, 0, 4),
    ParameterSpec("graupel_raw_mm", 201, 105, 0, 4),
    ParameterSpec("gust_u_ms", 162, 105, 10, 2),
    ParameterSpec("gust_v_ms", 163, 105, 10, 2),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/locations.json")
    parser.add_argument("--output", default="data/harmonie.json")
    parser.add_argument(
        "--archive",
        help="Archive .tar locale à décoder, sans appel à l'API (tests).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retraite une archive déjà mentionnée dans le JSON existant.",
    )
    parser.add_argument(
        "--cache-dir",
        help=(
            "Répertoire persistant des archives KNMI. Par défaut : "
            "<dossier du JSON>/.harmonie-cache"
        ),
    )
    return parser.parse_args()


def load_config(path: Path) -> tuple[list[Location], int]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    forecast_hours = int(raw.get("forecast_hours", 48))
    if not 1 <= forecast_hours <= 60:
        raise ValueError("forecast_hours doit être compris entre 1 et 60")

    locations: list[Location] = []
    seen: set[str] = set()
    for item in raw.get("locations", []):
        location = Location(
            slug=str(item["slug"]).strip().lower(),
            name=str(item["name"]).strip(),
            latitude=float(item["latitude"]),
            longitude=float(item["longitude"]),
        )
        if not location.slug or location.slug in seen:
            raise ValueError(f"Slug de ville absent ou dupliqué : {location.slug!r}")
        if not -90 <= location.latitude <= 90:
            raise ValueError(f"Latitude invalide pour {location.name}")
        if not -180 <= location.longitude <= 180:
            raise ValueError(f"Longitude invalide pour {location.name}")
        seen.add(location.slug)
        locations.append(location)

    if not locations:
        raise ValueError("La configuration ne contient aucune ville")
    return locations, forecast_hours


def api_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": api_key,
            "Accept": "application/json",
            "User-Agent": (
                "alertesmeteo-hub/harmonie-knmi "
                "(+https://github.com/alertesmeteo-hub/harmonie-knmi)"
            ),
        }
    )
    return session


def request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: requests.Response | None = None
    for attempt in range(1, MAX_API_ATTEMPTS + 1):
        try:
            response = session.get(url, params=params, timeout=(20, 90))
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt >= MAX_API_ATTEMPTS:
                raise
            delay = retry_delay_seconds(None, attempt)
            LOGGER.warning(
                "Connexion KNMI interrompue (%s). Nouvelle tentative %s/%s "
                "dans %s s.",
                exc,
                attempt + 1,
                MAX_API_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
            continue

        if (
            response.status_code in RETRYABLE_HTTP_STATUS
            and attempt < MAX_API_ATTEMPTS
        ):
            delay = retry_delay_seconds(response, attempt)
            LOGGER.warning(
                "KNMI répond HTTP %s. Nouvelle tentative %s/%s dans %s s.",
                response.status_code,
                attempt + 1,
                MAX_API_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
            continue
        break

    if response is None:
        raise KNMIError("Aucune réponse reçue de l'API KNMI")
    if response.headers.get("X-KNMI-Deprecation"):
        LOGGER.warning("KNMI : %s", response.headers["X-KNMI-Deprecation"])
    if response.status_code == 429:
        delay = retry_delay_seconds(response, MAX_API_ATTEMPTS)
        raise KNMIRateLimitError(
            "Quota KNMI temporairement atteint (HTTP 429) après "
            f"{MAX_API_ATTEMPTS} tentatives ; prochaine fenêtre estimée dans "
            f"environ {delay} s."
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise KNMIError(
            f"Réponse HTTP KNMI {response.status_code} pour {response.url}"
        ) from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Réponse JSON KNMI inattendue")
    if payload.get("error"):
        raise RuntimeError(f"Erreur KNMI : {payload['error']}")
    return payload


def retry_delay_seconds(
    response: requests.Response | None,
    attempt: int,
) -> int:
    """Calcule l'attente après un quota partagé ou une panne temporaire."""

    announced: float | None = None
    if response is not None:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after:
            try:
                announced = float(retry_after)
            except ValueError:
                try:
                    retry_date = parsedate_to_datetime(retry_after)
                    if retry_date.tzinfo is None:
                        retry_date = retry_date.replace(tzinfo=timezone.utc)
                    announced = (
                        retry_date - datetime.now(timezone.utc)
                    ).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    announced = None

        if announced is None:
            reset_header = response.headers.get("X-RateLimit-Reset", "").strip()
            try:
                reset_value = float(reset_header)
                announced = (
                    reset_value - time.time()
                    if reset_value > 1_000_000_000
                    else reset_value
                )
            except ValueError:
                announced = None

    base_delay = announced if announced is not None else min(
        600.0,
        30.0 * (2 ** (attempt - 1)),
    )
    # Un léger décalage évite que tous les utilisateurs de la clé anonyme
    # retentent exactement à la même seconde.
    return max(2, min(1800, int(max(0.0, base_delay) + random.uniform(2, 12))))


def latest_archive_metadata(session: requests.Session) -> dict[str, Any]:
    endpoint = (
        f"{API_BASE}/datasets/{DATASET_NAME}/versions/"
        f"{DATASET_VERSION}/files"
    )
    payload = request_json(
        session,
        endpoint,
        params={"maxKeys": 10, "orderBy": "created", "sorting": "desc"},
    )
    files = payload.get("files") or []
    for item in files:
        filename = str(item.get("filename", ""))
        if filename.lower().endswith(".tar"):
            return item
    raise RuntimeError("Le KNMI n'a retourné aucune archive .tar HARMONIE")


def temporary_download_url(
    session: requests.Session,
    filename: str,
) -> str:
    endpoint = (
        f"{API_BASE}/datasets/{DATASET_NAME}/versions/{DATASET_VERSION}/files/"
        f"{quote(filename, safe='')}/url"
    )
    payload = request_json(session, endpoint)
    url = payload.get("temporaryDownloadUrl")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError("URL temporaire KNMI absente ou invalide")
    return url


def archive_is_valid(path: Path, expected_size: int | None = None) -> bool:
    """Valide rapidement une archive déjà présente dans le cache."""

    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0 or size > MAX_ARCHIVE_BYTES:
        return False
    if expected_size is not None and size != expected_size:
        return False
    try:
        with tarfile.open(path, mode="r:*") as tar:
            first = next((m for m in tar if m.isfile()), None)
            return first is not None
    except (OSError, tarfile.TarError):
        return False


def latest_cached_archive(cache_dir: Path) -> Path | None:
    """Retourne la plus récente archive HARMONIE locale utilisable."""

    candidates = sorted(
        cache_dir.glob("*.tar"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )
    for candidate in candidates:
        if archive_is_valid(candidate):
            return candidate
    return None


def prune_archive_cache(cache_dir: Path, keep: Path) -> None:
    """Conserve une seule grosse archive afin de ne pas remplir le disque."""

    for candidate in cache_dir.glob("*.tar"):
        if candidate.resolve() == keep.resolve():
            continue
        try:
            candidate.unlink()
            LOGGER.info("Ancienne archive de cache supprimée : %s", candidate.name)
        except OSError as exc:
            LOGGER.warning("Impossible de supprimer %s : %s", candidate, exc)


def download_archive(url: str, target: Path, expected_size: int | None) -> None:
    """Télécharge l'archive de manière atomique, avec reprises sur erreurs transitoires."""

    if expected_size and expected_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError(
            f"Archive trop volumineuse ({expected_size / 1_000_000_000:.1f} Go)"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    last_error: Exception | None = None

    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        total = 0
        try:
            partial.unlink(missing_ok=True)
            with requests.get(
                url,
                stream=True,
                timeout=(30, 300),
                headers={"User-Agent": "alertesmeteo-hub/harmonie-knmi"},
            ) as response:
                if (
                    response.status_code in RETRYABLE_HTTP_STATUS
                    and attempt < MAX_DOWNLOAD_ATTEMPTS
                ):
                    delay = retry_delay_seconds(response, attempt)
                    LOGGER.warning(
                        "Téléchargement KNMI : HTTP %s. Nouvelle tentative %s/%s "
                        "dans %s s.",
                        response.status_code,
                        attempt + 1,
                        MAX_DOWNLOAD_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                announced = int(response.headers.get("Content-Length") or 0)
                if announced > MAX_ARCHIVE_BYTES:
                    raise RuntimeError(
                        "Archive annoncée trop volumineuse "
                        f"({announced / 1_000_000_000:.1f} Go)"
                    )
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(
                        chunk_size=DOWNLOAD_CHUNK_BYTES
                    ):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise RuntimeError(
                                "Limite de sécurité de téléchargement dépassée"
                            )
                        handle.write(chunk)

            if total == 0:
                raise RuntimeError("L'archive HARMONIE téléchargée est vide")
            if expected_size is not None and total != expected_size:
                raise RuntimeError(
                    "Téléchargement incomplet : "
                    f"{total} octets reçus au lieu de {expected_size}"
                )
            os.replace(partial, target)
            LOGGER.info("Archive téléchargée : %.1f Mo", total / 1_000_000)
            return
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError, OSError) as exc:
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt >= MAX_DOWNLOAD_ATTEMPTS:
                break
            delay = retry_delay_seconds(None, attempt)
            LOGGER.warning(
                "Téléchargement KNMI interrompu (%s). Nouvelle tentative %s/%s "
                "dans %s s.",
                exc,
                attempt + 1,
                MAX_DOWNLOAD_ATTEMPTS,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Impossible de télécharger l'archive KNMI après {MAX_DOWNLOAD_ATTEMPTS} tentatives"
    ) from last_error


def existing_output_is_usable(path: Path) -> bool:
    """Vrai si le JSON déjà publié peut être conservé sans le remplacer."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload.get("status") == "ok" and bool(payload.get("locations"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def already_processed(output: Path, source_filename: str) -> bool:
    try:
        with output.open("r", encoding="utf-8") as handle:
            current = json.load(handle)
        return (
            current.get("status") == "ok"
            and current.get("model", {}).get("source_file") == source_filename
            and current.get("model", {}).get("pipeline_version")
            == PIPELINE_VERSION
        )
    except (OSError, ValueError, TypeError):
        return False


def safe_get(gid: int, key: str, default: Any = None) -> Any:
    try:
        if not codes_is_defined(gid, key):
            return default
        return codes_get(gid, key)
    except Exception:
        return default


def safe_get_long(gid: int, key: str, default: int | None = None) -> int | None:
    try:
        if not codes_is_defined(gid, key):
            return default
        return int(codes_get_long(gid, key))
    except Exception:
        return default


def finite_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or abs(result) > 1.0e20:
        return None
    return result


def grid_signature(gid: int) -> tuple[Any, ...]:
    return (
        safe_get(gid, "gridType", "unknown"),
        safe_get_long(gid, "Ni", -1),
        safe_get_long(gid, "Nj", -1),
        safe_get_long(gid, "numberOfPoints", -1),
        safe_get(gid, "latitudeOfFirstGridPointInDegrees", None),
        safe_get(gid, "longitudeOfFirstGridPointInDegrees", None),
        safe_get(gid, "latitudeOfLastGridPointInDegrees", None),
        safe_get(gid, "longitudeOfLastGridPointInDegrees", None),
    )


class PointResolver:
    """Mémorise les indices de grille les plus proches pour chaque domaine."""

    def __init__(self, locations: Iterable[Location]):
        self.locations = list(locations)
        self._indexes: dict[tuple[Any, ...], list[int]] = {}
        self._points: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
        self._rotations: dict[tuple[tuple[Any, ...], str], float] = {}
        self._coordinates: dict[tuple[Any, ...], tuple[Any, Any]] = {}
        self.primary_signature: tuple[Any, ...] | None = None

    def _initialize(self, gid: int, signature: tuple[Any, ...]) -> None:
        # ecCodes peut faire basculer les clés calculées latitudes/longitudes
        # vers les coordonnées de la grille tournée après un appel à
        # find_nearest. On mémorise donc d'abord les coordonnées géographiques.
        if "rotated" in str(safe_get(gid, "gridType", "")):
            self._coordinates[signature] = (
                codes_get_double_array(gid, "latitudes"),
                codes_get_double_array(gid, "longitudes"),
            )

        indexes: list[int] = []
        points: dict[str, dict[str, Any]] = {}
        for location in self.locations:
            nearest = codes_grib_find_nearest(
                gid,
                location.latitude,
                location.longitude,
                False,
                1,
            )[0]
            indexes.append(int(nearest.index))
            points[location.slug] = {
                "latitude": round(float(nearest.lat), 5),
                "longitude": round(normalize_longitude(float(nearest.lon)), 5),
                "distance_km": round(float(nearest.distance), 3),
                "index": int(nearest.index),
            }
        self._indexes[signature] = indexes
        self._points[signature] = points
        if self.primary_signature is None:
            self.primary_signature = signature

    def extract(self, gid: int) -> dict[str, float | None]:
        signature = grid_signature(gid)
        if signature not in self._indexes:
            self._initialize(gid, signature)
        values = codes_get_double_elements(gid, "values", self._indexes[signature])
        return {
            location.slug: finite_or_none(value)
            for location, value in zip(self.locations, values)
        }

    def points(self) -> dict[str, dict[str, Any]]:
        if self.primary_signature is None:
            return {}
        return self._points[self.primary_signature]

    def rotation(self, gid: int, slug: str) -> float:
        signature = grid_signature(gid)
        if signature not in self._indexes:
            self._initialize(gid, signature)
        cache_key = (signature, slug)
        if cache_key not in self._rotations:
            location_pos = next(
                index
                for index, location in enumerate(self.locations)
                if location.slug == slug
            )
            point_index = self._indexes[signature][location_pos]
            relative = safe_get_long(gid, "uvRelativeToGrid", None)
            grid_type = str(safe_get(gid, "gridType", ""))
            if relative == 0 or (
                relative is None and "rotated" not in grid_type
            ):
                bearing = 0.0
            else:
                try:
                    if signature not in self._coordinates:
                        self._coordinates[signature] = (
                            codes_get_double_array(gid, "latitudes"),
                            codes_get_double_array(gid, "longitudes"),
                        )
                    latitudes, longitudes = self._coordinates[signature]
                    bearing = calculate_grid_north_bearing(
                        gid,
                        point_index,
                        latitudes,
                        longitudes,
                    )
                except Exception as exc:
                    LOGGER.warning("Rotation U/V non calculable : %s", exc)
                    bearing = 0.0
            self._rotations[cache_key] = bearing
        return self._rotations[cache_key]


def normalize_longitude(longitude: float) -> float:
    return ((longitude + 180.0) % 360.0) - 180.0


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lon = math.radians(normalize_longitude(lon2 - lon1))
    y = math.sin(delta_lon) * math.cos(phi2)
    x = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lon)
    )
    return math.degrees(math.atan2(y, x)) % 360.0


def calculate_grid_north_bearing(
    gid: int,
    index: int,
    latitudes: Any,
    longitudes: Any,
) -> float:
    """Angle du nord de la grille par rapport au nord géographique.

    Les composantes U/V de P3 peuvent être relatives à la grille tournée.
    L'angle calculé permet de restituer une direction météorologique correcte.
    En cas de métadonnées insuffisantes, 0° conserve les composantes natives.
    """

    ni = safe_get_long(gid, "Ni", 0) or 0
    nj = safe_get_long(gid, "Nj", 0) or 0
    point_count = safe_get_long(gid, "numberOfPoints", 0) or 0
    j_consecutive = bool(safe_get_long(gid, "jPointsAreConsecutive", 0))
    j_positive = bool(safe_get_long(gid, "jScansPositively", 0))
    if ni < 2 or nj < 2 or point_count < 4:
        return 0.0

    step = 1 if j_consecutive else ni
    axis_position = index % nj if j_consecutive else index // ni
    reverse = False
    if j_positive:
        if axis_position < nj - 1 and index + step < point_count:
            neighbour = index + step
        else:
            neighbour = index - step
            reverse = True
    else:
        if axis_position > 0 and index - step >= 0:
            neighbour = index - step
        else:
            neighbour = index + step
            reverse = True

    lat1 = float(latitudes[index])
    lon1 = float(longitudes[index])
    lat2 = float(latitudes[neighbour])
    lon2 = float(longitudes[neighbour])

    bearing = initial_bearing(lat1, lon1, lat2, lon2)
    if reverse:
        bearing = (bearing + 180.0) % 360.0
    return bearing


def parameter_name(gid: int) -> str | None:
    code = safe_get_long(gid, "indicatorOfParameter")
    level_type = safe_get_long(gid, "indicatorOfTypeOfLevel")
    level = safe_get_long(gid, "level")
    tri = safe_get_long(gid, "timeRangeIndicator")
    for spec in PARAMETERS:
        if (
            code == spec.code
            and level_type == spec.level_type
            and level == spec.level
            and (spec.tri is None or tri == spec.tri)
        ):
            return spec.name
    return None


def date_time_from_grib(gid: int, date_key: str, time_key: str) -> datetime | None:
    date_value = safe_get_long(gid, date_key)
    time_value = safe_get_long(gid, time_key)
    if date_value is None or time_value is None:
        return None
    try:
        return datetime.strptime(
            f"{date_value:08d}{time_value:04d}",
            "%Y%m%d%H%M",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def empty_step(lead_hint: int, run_hint: datetime | None) -> dict[str, Any]:
    return {
        "lead_hint": lead_hint,
        "run_time": run_hint,
        "valid_time": None,
        "precip_start_step": None,
        "precip_end_step": None,
        "values": {},
        "rotations": {},
    }


def parse_grib_file(
    path: Path,
    locations: list[Location],
    resolver: PointResolver,
    lead_hint: int,
    run_hint: datetime | None,
) -> dict[str, Any]:
    step = empty_step(lead_hint, run_hint)
    selected_messages = 0
    with path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                name = parameter_name(gid)
                if name is None:
                    continue
                selected_messages += 1
                if step["run_time"] is None:
                    step["run_time"] = date_time_from_grib(gid, "dataDate", "dataTime")
                if step["valid_time"] is None:
                    step["valid_time"] = date_time_from_grib(
                        gid,
                        "validityDate",
                        "validityTime",
                    )

                values = resolver.extract(gid)
                for location in locations:
                    step["values"].setdefault(location.slug, {})[name] = values[
                        location.slug
                    ]

                if name == "precipitation_raw_mm":
                    step["precip_start_step"] = safe_get_long(gid, "startStep")
                    step["precip_end_step"] = safe_get_long(gid, "endStep")
                if name == "wind_u_ms":
                    for location in locations:
                        step["rotations"][location.slug] = resolver.rotation(
                            gid,
                            location.slug,
                        )
            finally:
                codes_release(gid)

    if selected_messages == 0:
        raise RuntimeError(f"Aucun paramètre HARMONIE attendu dans {path.name}")
    if step["valid_time"] is None and step["run_time"] is not None:
        step["valid_time"] = step["run_time"] + timedelta(hours=lead_hint)
    return step


def merge_steps(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key in ("run_time", "valid_time", "precip_start_step", "precip_end_step"):
        if target.get(key) is None and incoming.get(key) is not None:
            target[key] = incoming[key]
    target["rotations"].update(incoming.get("rotations", {}))
    for slug, values in incoming.get("values", {}).items():
        target["values"].setdefault(slug, {}).update(values)


def member_information(member: tarfile.TarInfo) -> tuple[int, datetime | None] | None:
    match = MEMBER_RE.search(Path(member.name).name)
    if not match:
        return None
    # Le KNMI code l'échéance sous la forme HHHMM : 00100 = +1 h,
    # 04800 = +48 h et 06000 = +60 h.
    lead_code = int(match.group("lead"))
    lead, minutes = divmod(lead_code, 100)
    if minutes != 0:
        LOGGER.debug("Échéance non horaire ignorée : %05d", lead_code)
        return None
    try:
        run = datetime.strptime(match.group("run"), "%Y%m%d%H%M").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        run = None
    return lead, run


def decode_archive(
    archive: Path,
    locations: list[Location],
    forecast_hours: int,
) -> tuple[list[dict[str, Any]], PointResolver]:
    if not tarfile.is_tarfile(archive):
        raise RuntimeError("Le fichier KNMI reçu n'est pas une archive TAR valide")

    resolver = PointResolver(locations)
    combined: dict[int, dict[str, Any]] = {}
    with tarfile.open(archive, mode="r:*") as tar, tempfile.TemporaryDirectory(
        prefix="harmonie-grib-"
    ) as temporary_directory:
        members: list[tuple[int, datetime | None, tarfile.TarInfo]] = []
        for member in tar.getmembers():
            if not member.isfile() or member.size <= 0:
                continue
            information = member_information(member)
            if information is None:
                continue
            lead, run = information
            if 0 <= lead <= forecast_hours:
                members.append((lead, run, member))

        members.sort(key=lambda item: (item[0], item[2].name))
        if not members:
            raise RuntimeError(
                "Aucun fichier HA43_N55 correspondant aux échéances demandées"
            )

        LOGGER.info("Échéances GRIB à traiter : %s", len(members))
        temporary_grib = Path(temporary_directory) / "current.grib"
        for position, (lead, run, member) in enumerate(members, start=1):
            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError(f"Impossible de lire {member.name}")
            with source, temporary_grib.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=DOWNLOAD_CHUNK_BYTES)

            LOGGER.info(
                "Décodage %s/%s : +%02dh",
                position,
                len(members),
                lead,
            )
            parsed = parse_grib_file(
                temporary_grib,
                locations,
                resolver,
                lead,
                run,
            )
            if lead not in combined:
                combined[lead] = empty_step(lead, run)
            merge_steps(combined[lead], parsed)
            temporary_grib.unlink(missing_ok=True)

    steps = [combined[lead] for lead in sorted(combined)]
    valid_steps = [step for step in steps if step.get("valid_time") is not None]
    if not valid_steps:
        raise RuntimeError("Aucune échéance HARMONIE valide n'a été décodée")
    return valid_steps, resolver


def round_or_none(value: float | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def clamp(value: float | None, low: float, high: float) -> float | None:
    if value is None:
        return None
    return min(high, max(low, value))


def fraction_to_percentage(value: Any) -> float | None:
    """Convertit les proportions 0–1 réellement encodées par HARMONIE P3."""

    fraction = finite_or_none(value)
    if fraction is None:
        return None
    return fraction * 100.0


def compass_direction(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    names = (
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSO",
        "SO",
        "OSO",
        "O",
        "ONO",
        "NO",
        "NNO",
    )
    return names[int((degrees + 11.25) // 22.5) % 16]


def wind_values(
    u: float | None,
    v: float | None,
    rotation_degrees: float,
) -> tuple[float | None, float | None]:
    if u is None or v is None:
        return None, None
    speed = math.hypot(u, v) * 3.6
    angle = math.radians(rotation_degrees)
    east = u * math.cos(angle) + v * math.sin(angle)
    north = -u * math.sin(angle) + v * math.cos(angle)
    # Direction météorologique : direction d'où vient le vent.
    direction = math.degrees(math.atan2(-east, -north)) % 360.0
    return speed, direction


def condition_for(item: dict[str, Any]) -> dict[str, str]:
    precip = item.get("precipitation_mm") or 0.0
    cloud = item.get("cloud_cover_pct")
    visibility = item.get("visibility_km")
    temperature = item.get("temperature_c")
    gust = item.get("wind_gust_kmh") or 0.0

    if visibility is not None and visibility < 1.0:
        return {"code": "fog", "label": "Brouillard", "icon": "🌫️"}
    if precip >= 0.1 and temperature is not None and temperature <= 1.0:
        return {"code": "snow", "label": "Neige", "icon": "❄️"}
    if precip >= 5.0:
        return {"code": "heavy_rain", "label": "Forte pluie", "icon": "🌧️"}
    if precip >= 0.1:
        return {"code": "rain", "label": "Pluie", "icon": "🌦️"}
    if gust >= 70.0:
        return {"code": "windy", "label": "Très venteux", "icon": "💨"}
    if cloud is None:
        return {"code": "unknown", "label": "Indéterminé", "icon": "•"}
    if cloud <= 20:
        return {"code": "clear", "label": "Dégagé", "icon": "☀️"}
    if cloud <= 55:
        return {"code": "partly_cloudy", "label": "Peu nuageux", "icon": "🌤️"}
    if cloud <= 85:
        return {"code": "cloudy", "label": "Nuageux", "icon": "⛅"}
    return {"code": "overcast", "label": "Couvert", "icon": "☁️"}


def hourly_precipitation(
    raw: float | None,
    start_step: int | None,
    end_step: int | None,
    previous_cumulative: float | None,
) -> tuple[float | None, float | None]:
    if raw is None:
        return None, previous_cumulative
    raw = max(0.0, raw)
    if start_step == 0 and end_step is not None:
        if previous_cumulative is None:
            hourly = raw
        else:
            hourly = max(0.0, raw - previous_cumulative)
        return hourly, raw
    return raw, previous_cumulative


def build_output(
    steps: list[dict[str, Any]],
    resolver: PointResolver,
    locations: list[Location],
    forecast_hours: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    steps.sort(key=lambda item: item["valid_time"])
    model_run = next(
        (item["run_time"] for item in steps if item.get("run_time") is not None),
        None,
    )
    points = resolver.points()
    output_locations: dict[str, Any] = {}

    for location in locations:
        forecasts: list[dict[str, Any]] = []
        previous_cumulative: float | None = None
        for step in steps:
            values = step["values"].get(location.slug, {})
            temperature_k = finite_or_none(values.get("temperature_k"))
            dewpoint_k = finite_or_none(values.get("dewpoint_k"))
            pressure_pa = finite_or_none(values.get("pressure_pa"))
            visibility_m = finite_or_none(values.get("visibility_m"))
            wind_speed, wind_direction = wind_values(
                finite_or_none(values.get("wind_u_ms")),
                finite_or_none(values.get("wind_v_ms")),
                float(step["rotations"].get(location.slug, 0.0)),
            )
            gust_u = finite_or_none(values.get("gust_u_ms"))
            gust_v = finite_or_none(values.get("gust_v_ms"))
            gust_speed = (
                math.hypot(gust_u, gust_v) * 3.6
                if gust_u is not None and gust_v is not None
                else None
            )
            precipitation, previous_cumulative = hourly_precipitation(
                finite_or_none(values.get("precipitation_raw_mm")),
                step.get("precip_start_step"),
                step.get("precip_end_step"),
                previous_cumulative,
            )

            valid_time: datetime = step["valid_time"]
            lead_hours = (
                int(round((valid_time - model_run).total_seconds() / 3600))
                if model_run is not None
                else int(step.get("lead_hint", 0))
            )
            item = {
                "time": valid_time.isoformat().replace("+00:00", "Z"),
                "lead_hours": lead_hours,
                "temperature_c": round_or_none(
                    temperature_k - 273.15 if temperature_k is not None else None
                ),
                "dewpoint_c": round_or_none(
                    dewpoint_k - 273.15 if dewpoint_k is not None else None
                ),
                "humidity_pct": round_or_none(
                    clamp(
                        fraction_to_percentage(values.get("humidity_pct")),
                        0,
                        100,
                    ),
                    0,
                ),
                "precipitation_mm": round_or_none(precipitation, 1),
                "cloud_cover_pct": round_or_none(
                    clamp(
                        fraction_to_percentage(values.get("cloud_pct")),
                        0,
                        100,
                    ),
                    0,
                ),
                "cloud_low_pct": round_or_none(
                    clamp(
                        fraction_to_percentage(values.get("cloud_low_pct")),
                        0,
                        100,
                    ),
                    0,
                ),
                "cloud_mid_pct": round_or_none(
                    clamp(
                        fraction_to_percentage(values.get("cloud_mid_pct")),
                        0,
                        100,
                    ),
                    0,
                ),
                "cloud_high_pct": round_or_none(
                    clamp(
                        fraction_to_percentage(values.get("cloud_high_pct")),
                        0,
                        100,
                    ),
                    0,
                ),
                "wind_speed_kmh": int(math.ceil(wind_speed)) if wind_speed is not None and math.isfinite(wind_speed) else None,
                "wind_direction_deg": round_or_none(wind_direction, 0),
                "wind_direction": compass_direction(wind_direction),
                "wind_gust_kmh": int(math.ceil(gust_speed)) if gust_speed is not None and math.isfinite(gust_speed) else None,
                "pressure_hpa": round_or_none(
                    pressure_pa / 100.0 if pressure_pa is not None else None,
                    0,
                ),
                "visibility_km": round_or_none(
                    visibility_m / 1000.0 if visibility_m is not None else None,
                    1,
                ),
            }
            item["condition"] = condition_for(item)
            if item["temperature_c"] is not None:
                forecasts.append(item)

        output_locations[location.slug] = {
            "name": location.name,
            "requested_point": {
                "latitude": location.latitude,
                "longitude": location.longitude,
            },
            "model_point": points.get(location.slug),
            "forecast": forecasts,
        }

    if not any(item["forecast"] for item in output_locations.values()):
        raise RuntimeError("Aucune température ponctuelle n'a pu être extraite")

    return {
        "schema_version": 1,
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "model": {
            "name": "HARMONIE-AROME Cy43",
            "provider": "KNMI",
            "dataset": DATASET_NAME,
            "version": DATASET_VERSION,
            "pipeline_version": PIPELINE_VERSION,
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
        },
        "units": {
            "temperature": "°C",
            "humidity": "%",
            "precipitation": "mm/h",
            "cloud_cover": "%",
            "wind": "km/h",
            "pressure": "hPa",
            "visibility": "km",
        },
        "locations": output_locations,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(temporary, path)


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    output_path = Path(args.output).resolve()
    cache_dir = (
        Path(args.cache_dir).resolve()
        if args.cache_dir
        else output_path.parent / ".harmonie-cache"
    )
    locations, forecast_hours = load_config(config_path)

    api_key = os.environ.get("KNMI_API_KEY", "").strip()
    if not api_key:
        api_key = PUBLIC_ANONYMOUS_KEY
        LOGGER.warning(
            "Utilisation de la clé KNMI anonyme publique ; configurez le secret "
            "KNMI_API_KEY pour la production à long terme."
        )

    if args.archive:
        archive_path = Path(args.archive).resolve()
        if not archive_path.is_file():
            raise FileNotFoundError(archive_path)
        source = {
            "filename": archive_path.name,
            "size": archive_path.stat().st_size,
            "created": None,
        }
    else:
        cache_dir.mkdir(parents=True, exist_ok=True)
        session = api_session(api_key)
        try:
            source = latest_archive_metadata(session)
        except KNMIRateLimitError as exc:
            if existing_output_is_usable(output_path):
                LOGGER.warning(
                    "%s Conservation du JSON HARMONIE déjà publié ; "
                    "les données ne sont PAS déclarées indisponibles.",
                    exc,
                )
                return 0
            cached = latest_cached_archive(cache_dir)
            if cached is None:
                raise
            LOGGER.warning(
                "%s Aucun JSON exploitable ; utilisation de l'archive locale %s.",
                exc,
                cached.name,
            )
            archive_path = cached
            source = {
                "filename": cached.name,
                "size": cached.stat().st_size,
                "created": None,
                "cache_fallback": True,
            }
        else:
            source_filename = str(source.get("filename", ""))
            if not source_filename:
                raise RuntimeError("Nom de l'archive KNMI absent")
            LOGGER.info(
                "Dernière archive : %s (%s octets)",
                source_filename,
                source.get("size", "taille inconnue"),
            )
            if not args.force and already_processed(output_path, source_filename):
                LOGGER.info("Cette archive est déjà publiée ; aucune modification.")
                return 0

            expected_size_raw = source.get("size")
            expected_size = (
                int(expected_size_raw)
                if expected_size_raw is not None
                else None
            )
            archive_path = cache_dir / source_filename
            if archive_is_valid(archive_path, expected_size):
                LOGGER.info(
                    "Archive déjà présente dans le cache : %s",
                    archive_path,
                )
            else:
                archive_path.unlink(missing_ok=True)
                url = temporary_download_url(session, source_filename)
                download_archive(url, archive_path, expected_size)
                if not archive_is_valid(archive_path, expected_size):
                    archive_path.unlink(missing_ok=True)
                    raise RuntimeError(
                        "L'archive KNMI téléchargée n'est pas exploitable"
                    )
                prune_archive_cache(cache_dir, archive_path)

    steps, resolver = decode_archive(
        archive_path,
        locations,
        forecast_hours,
    )
    output = build_output(
        steps,
        resolver,
        locations,
        forecast_hours,
        source,
    )
    write_json_atomic(output_path, output)
    LOGGER.info(
        "JSON écrit : %s (%s villes)",
        output_path,
        len(output["locations"]),
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        sys.exit(main())
    except Exception:
        LOGGER.exception("Échec de la mise à jour HARMONIE")
        sys.exit(1)

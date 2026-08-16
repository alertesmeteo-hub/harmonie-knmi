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
NATIONAL_PIPELINE_VERSION = "2.0.0"
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
    "temperature_k",
    "visibility_m",
    "wind_u_ms",
    "wind_v_ms",
    "humidity_pct",
    "precipitation_raw_mm",
    "cloud_pct",
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
    temperature = rounded(raw["temperature_k"] - 273.15, 1)
    humidity = rounded(np.clip(raw["humidity_pct"] * 100.0, 0, 100), 0)
    cloud = rounded(np.clip(raw["cloud_pct"] * 100.0, 0, 100), 0)
    pressure = rounded(raw["pressure_pa"] / 100.0, 0)
    visibility = rounded(raw["visibility_m"] / 1000.0, 1)

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
            "humidity_pct": humidity,
            "precipitation_mm": precipitation,
            "cloud_cover_pct": cloud,
            "wind_speed_kmh": wind_speed,
            "wind_direction_deg": wind_direction,
            "wind_gust_kmh": gust_speed,
            "pressure_hpa": pressure,
            "visibility_km": visibility,
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
    previous_cumulative: np.ndarray | None = None
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
                step = parse_grib_file(temporary_grib, grid, lead, run)
                if model_run is None:
                    model_run = step.get("run_time")
                transformed, previous_cumulative = transform_step(
                    step, previous_cumulative
                )
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

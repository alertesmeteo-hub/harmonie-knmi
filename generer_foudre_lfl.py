#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Statistiques Foudre / Orages
EUMETSAT MTG Lightning Imager — LFL Lightning Flashes
Version 1.0.0

Collection :
EO:EUM:DAT:0691
LI Lightning Flashes - MTG - 0 degree

Principe :
- recherche des produits LFL récents dans EUMETSAT Data Store ;
- téléchargement via EUMDAC ;
- extraction des flashes (flash_time, latitude, longitude) ;
- attribution commune / département avec les contours Etalab ;
- cumul incrémental jour / mois / année ;
- déduplication par identifiant de produit ;
- activité récente conservée 180 minutes.

Attention :
LI détecte la foudre totale depuis l'espace.
Les positions ont une résolution de l'ordre de 4 km et peuvent être
affectées par la parallaxe du sommet nuageux. Les statistiques par
commune sont donc des attributions géographiques satellitaires et
ne doivent pas être assimilées à un réseau d'impacts au sol.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import eumdac
import numpy as np
import requests
from netCDF4 import Dataset, num2date
from shapely.geometry import Point, shape
from shapely.strtree import STRtree


VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "mtg-li-lfl-france-stats-20260820"

COLLECTION_ID = "EO:EUM:DAT:0691"

OUTPUT = Path("observations_foudre_lfl.json")
CACHE = Path("cache_foudre_lfl.json")

COMMUNES_GZ = (
    "https://etalab-datasets.geo.data.gouv.fr/"
    "contours-administratifs/latest/geojson/"
    "communes-1000m.geojson.gz"
)

DEPARTEMENTS_GZ = (
    "https://etalab-datasets.geo.data.gouv.fr/"
    "contours-administratifs/latest/geojson/"
    "departements-1000m.geojson.gz"
)

HTTP_TIMEOUT = 90

# Recherche normale : 45 minutes pour absorber les retards de cron.
NORMAL_LOOKBACK_MINUTES = 45

# Premier lancement : on reconstruit le jour en cours.
FIRST_RUN_MAX_HOURS = 24

# Le Data Store archive les produits LI en chunks de 10 minutes.
PRODUCT_HISTORY_HOURS = 72

# Activité cartographique récente.
RECENT_HOURS = 3

# France métropolitaine + Corse, marge légère autour des frontières.
FRANCE_BBOX = {
    "south": 41.0,
    "north": 51.6,
    "west": -5.6,
    "east": 10.0,
}

PARIS = ZoneInfo("Europe/Paris")
UTC = timezone.utc

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-foudre-lfl/{VERSION}",
})


# ------------------------------------------------------------
# Utilitaires
# ------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    return dt.astimezone(UTC).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)

        return dt.astimezone(UTC)

    except Exception:
        return None


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def inside_france_bbox(
    lat: float,
    lon: float,
) -> bool:

    return (
        FRANCE_BBOX["south"] <= lat <= FRANCE_BBOX["north"]
        and FRANCE_BBOX["west"] <= lon <= FRANCE_BBOX["east"]
    )


def period_keys(
    dt_utc: datetime,
) -> Tuple[str, str, str]:

    local = dt_utc.astimezone(PARIS)

    return (
        local.strftime("%Y"),
        local.strftime("%Y-%m"),
        local.strftime("%Y-%m-%d"),
    )


def empty_rank_period() -> dict:
    return {
        "total": 0,
        "departments": {},
        "communes": {},
    }


def empty_day() -> dict:
    return {
        **empty_rank_period(),
        "hourly": {
            f"{hour:02d}": 0
            for hour in range(24)
        },
        "five_minute": {
            f"{hour:02d}:{minute:02d}": 0
            for hour in range(24)
            for minute in range(0, 60, 5)
        },
    }


def fresh_state(
    now: datetime,
) -> dict:

    year_key, month_key, day_key = period_keys(now)

    return {
        "schema_version": 1,
        "module_version": VERSION,
        "tracking_started_at": iso(now),

        "year_key": year_key,
        "month_key": month_key,
        "day_key": day_key,

        "year": empty_rank_period(),
        "month": empty_rank_period(),
        "day": empty_day(),

        "monthly_year": {
            f"{m:02d}": 0
            for m in range(1, 13)
        },

        "daily_month": {},

        "recent_minutes": {},
        "recent_cells": {},

        "processed_products": {},
        "diagnostics": {
            "products_processed_total": 0,
            "flashes_read_total": 0,
            "flashes_in_bbox_total": 0,
            "flashes_attributed_total": 0,
            "flashes_unassigned_total": 0,
        },
    }


def load_state(
    now: datetime,
) -> Tuple[dict, bool]:

    if not CACHE.exists():
        return fresh_state(now), True

    try:
        data = json.loads(
            CACHE.read_text(encoding="utf-8")
        )

        if not isinstance(data, dict):
            raise ValueError("cache non objet")

        if not data.get("tracking_started_at"):
            data["tracking_started_at"] = iso(now)

        return data, False

    except Exception as exc:
        print("[WARN] Cache ignoré :", exc)
        return fresh_state(now), True


def reset_periods(
    state: dict,
    now: datetime,
) -> None:

    year_key, month_key, day_key = period_keys(now)

    if state.get("year_key") != year_key:
        state["year_key"] = year_key
        state["year"] = empty_rank_period()
        state["monthly_year"] = {
            f"{m:02d}": 0
            for m in range(1, 13)
        }

    if state.get("month_key") != month_key:
        state["month_key"] = month_key
        state["month"] = empty_rank_period()
        state["daily_month"] = {}

    if state.get("day_key") != day_key:
        state["day_key"] = day_key
        state["day"] = empty_day()


def prune_state(
    state: dict,
    now: datetime,
) -> None:

    product_cutoff = now - timedelta(
        hours=PRODUCT_HISTORY_HOURS
    )

    processed = state.setdefault(
        "processed_products",
        {}
    )

    state["processed_products"] = {
        pid: value
        for pid, value in processed.items()
        if (
            parse_iso(value)
            and parse_iso(value) >= product_cutoff
        )
    }

    recent_cutoff = now - timedelta(
        hours=RECENT_HOURS
    )

    minutes = state.setdefault(
        "recent_minutes",
        {}
    )

    kept_minutes = {}

    for key, count in minutes.items():
        dt = parse_iso(
            key + ":00Z"
            if len(key) == 16
            else key
        )

        if dt and dt >= recent_cutoff:
            kept_minutes[key] = safe_int(count)

    state["recent_minutes"] = kept_minutes

    cells = state.setdefault(
        "recent_cells",
        {}
    )

    kept_cells = {}

    for key, item in cells.items():
        last = parse_iso(
            item.get("last_time")
        )

        if last and last >= recent_cutoff:
            kept_cells[key] = item

    state["recent_cells"] = kept_cells


# ------------------------------------------------------------
# Contours administratifs
# ------------------------------------------------------------

def download_gzip_json(
    url: str,
) -> dict:

    response = session.get(
        url,
        timeout=HTTP_TIMEOUT,
    )

    response.raise_for_status()

    raw = gzip.decompress(
        response.content
    )

    return json.loads(
        raw.decode("utf-8")
    )


def department_code_from_commune(
    code: str,
) -> str:

    code = str(code or "").upper()

    if code.startswith(("2A", "2B")):
        return code[:2]

    if code.startswith(("97", "98")):
        return code[:3]

    return code[:2]


class AdministrativeIndex:
    def __init__(self):
        print(
            "Téléchargement contours communes Etalab..."
        )

        communes = download_gzip_json(
            COMMUNES_GZ
        )

        print(
            "Téléchargement noms départements Etalab..."
        )

        departments = download_gzip_json(
            DEPARTEMENTS_GZ
        )

        self.department_names = {}

        for feature in departments.get(
            "features",
            []
        ):
            props = feature.get(
                "properties"
            ) or {}

            code = str(
                props.get("code")
                or ""
            ).upper()

            name = str(
                props.get("nom")
                or code
            )

            if code:
                self.department_names[
                    code
                ] = name

        self.geometries = []
        self.infos = []

        for feature in communes.get(
            "features",
            []
        ):
            props = feature.get(
                "properties"
            ) or {}

            code = str(
                props.get("code")
                or ""
            ).upper()

            name = str(
                props.get("nom")
                or code
            )

            geometry = feature.get(
                "geometry"
            )

            if not code or not geometry:
                continue

            try:
                geom = shape(geometry)

            except Exception:
                continue

            if geom.is_empty:
                continue

            dep_code = department_code_from_commune(
                code
            )

            self.geometries.append(
                geom
            )

            self.infos.append({
                "code": code,
                "name": name,
                "department_code": dep_code,
                "department_name": (
                    self.department_names.get(
                        dep_code,
                        dep_code,
                    )
                ),
            })

        self.tree = STRtree(
            self.geometries
        )

        print(
            "Communes indexées :",
            len(self.geometries),
        )


    def locate(
        self,
        lat: float,
        lon: float,
    ) -> Optional[dict]:

        point = Point(
            lon,
            lat,
        )

        try:
            candidates = self.tree.query(
                point
            )

        except Exception:
            return None

        for candidate in candidates:
            try:
                idx = int(candidate)
                geom = self.geometries[
                    idx
                ]

            except Exception:
                # Compatibilité au cas où une version retourne
                # directement la géométrie.
                geom = candidate

                try:
                    idx = self.geometries.index(
                        geom
                    )
                except Exception:
                    continue

            try:
                if geom.covers(point):
                    return self.infos[idx]

            except Exception:
                continue

        return None


# ------------------------------------------------------------
# EUMETSAT / EUMDAC
# ------------------------------------------------------------

def credentials() -> Tuple[str, str]:

    key = os.getenv(
        "EUMETSAT_CONSUMER_KEY",
        ""
    ).strip()

    secret = os.getenv(
        "EUMETSAT_CONSUMER_SECRET",
        ""
    ).strip()

    if not key or not secret:
        raise RuntimeError(
            "Secrets EUMETSAT absents : "
            "EUMETSAT_CONSUMER_KEY / "
            "EUMETSAT_CONSUMER_SECRET"
        )

    return key, secret


def product_id(
    product: Any,
) -> str:

    for attr in (
        "product_id",
        "id",
        "identifier",
    ):
        value = getattr(
            product,
            attr,
            None,
        )

        if value:
            return str(value)

    return str(product)


def search_products(
    start: datetime,
    end: datetime,
) -> List[Any]:

    key, secret = credentials()

    token = eumdac.AccessToken(
        (key, secret)
    )

    print(
        "Token EUMETSAT valide jusqu'à :",
        token.expiration,
    )

    datastore = eumdac.DataStore(
        token
    )

    collection = datastore.get_collection(
        COLLECTION_ID
    )

    # Les exemples EUMETSAT utilisent des datetime naïfs UTC.
    start_naive = start.astimezone(
        UTC
    ).replace(
        tzinfo=None
    )

    end_naive = end.astimezone(
        UTC
    ).replace(
        tzinfo=None
    )

    print(
        "Recherche LFL :",
        start_naive,
        "->",
        end_naive,
    )

    products = list(
        collection.search(
            dtstart=start_naive,
            dtend=end_naive,
        )
    )

    def sort_key(item: Any):
        value = getattr(
            item,
            "sensing_start",
            None,
        )

        return value or datetime.min

    products.sort(
        key=sort_key
    )

    print(
        "Produits trouvés :",
        len(products),
    )

    return products


def download_product(
    product: Any,
    directory: Path,
) -> Path:

    pid = product_id(
        product
    )

    safe = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        pid,
    )[-180:]

    target = directory / (
        safe + ".bin"
    )

    print(
        "Téléchargement :",
        pid,
    )

    with product.open() as src, target.open(
        "wb"
    ) as dst:
        shutil.copyfileobj(
            src,
            dst,
        )

    return target


def find_body_netcdfs(
    downloaded: Path,
    workdir: Path,
) -> List[Path]:

    if zipfile.is_zipfile(
        downloaded
    ):
        extract_dir = workdir / (
            downloaded.stem
            + "_extracted"
        )

        extract_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        with zipfile.ZipFile(
            downloaded
        ) as z:
            z.extractall(
                extract_dir
            )

        body = [
            p
            for p in extract_dir.rglob("*")
            if (
                p.is_file()
                and "BODY" in p.name.upper()
                and p.suffix.lower() in (
                    ".nc",
                    ".nc4",
                    ".bin",
                )
            )
        ]

        if not body:
            body = [
                p
                for p in extract_dir.rglob("*")
                if (
                    p.is_file()
                    and p.suffix.lower() in (
                        ".nc",
                        ".nc4",
                    )
                )
            ]

        return body

    # Le Data Store peut aussi renvoyer directement un netCDF.
    return [
        downloaded
    ]


# ------------------------------------------------------------
# Lecture NetCDF LFL
# ------------------------------------------------------------

def find_variable(
    group: Any,
    name: str,
):

    if name in group.variables:
        return group.variables[
            name
        ]

    for subgroup in group.groups.values():
        found = find_variable(
            subgroup,
            name,
        )

        if found is not None:
            return found

    return None


def to_python_datetime(
    value: Any,
) -> Optional[datetime]:

    if value is None:
        return None

    if isinstance(
        value,
        datetime,
    ):
        dt = value

    else:
        try:
            dt = datetime(
                int(value.year),
                int(value.month),
                int(value.day),
                int(value.hour),
                int(value.minute),
                int(value.second),
                int(
                    getattr(
                        value,
                        "microsecond",
                        0,
                    )
                ),
            )

        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=UTC
        )

    return dt.astimezone(
        UTC
    )


def read_lfl_netcdf(
    path: Path,
) -> List[dict]:

    flashes = []

    with Dataset(
        path,
        "r",
    ) as ds:

        lat_var = find_variable(
            ds,
            "latitude",
        )

        lon_var = find_variable(
            ds,
            "longitude",
        )

        time_var = find_variable(
            ds,
            "flash_time",
        )

        if (
            lat_var is None
            or lon_var is None
            or time_var is None
        ):
            print(
                "[WARN] Variables LFL absentes dans",
                path.name,
            )
            return []

        lat = np.ma.asarray(
            lat_var[:]
        )

        lon = np.ma.asarray(
            lon_var[:]
        )

        raw_time = np.ma.asarray(
            time_var[:]
        )

        n = min(
            lat.size,
            lon.size,
            raw_time.size,
        )

        if n == 0:
            return []

        try:
            decoded_times = num2date(
                raw_time[:n],
                units=time_var.units,
                calendar=getattr(
                    time_var,
                    "calendar",
                    "standard",
                ),
                only_use_cftime_datetimes=False,
            )

        except Exception as exc:
            print(
                "[WARN] flash_time non décodable :",
                exc,
            )
            return []

        lat_flat = lat[:n].reshape(-1)
        lon_flat = lon[:n].reshape(-1)
        time_flat = np.asarray(
            decoded_times
        ).reshape(-1)

        lat_mask = np.ma.getmaskarray(
            lat[:n]
        ).reshape(-1)

        lon_mask = np.ma.getmaskarray(
            lon[:n]
        ).reshape(-1)

        for i in range(n):
            if lat_mask[i] or lon_mask[i]:
                continue

            try:
                la = float(
                    lat_flat[i]
                )

                lo = float(
                    lon_flat[i]
                )

            except Exception:
                continue

            if not (
                math.isfinite(la)
                and math.isfinite(lo)
            ):
                continue

            if lo > 180:
                lo -= 360

            dt = to_python_datetime(
                time_flat[i]
            )

            if dt is None:
                continue

            flashes.append({
                "time": dt,
                "lat": la,
                "lon": lo,
            })

    return flashes


def read_product_flashes(
    downloaded: Path,
    workdir: Path,
) -> List[dict]:

    result = []

    for nc in find_body_netcdfs(
        downloaded,
        workdir,
    ):
        try:
            result.extend(
                read_lfl_netcdf(
                    nc
                )
            )

        except Exception as exc:
            print(
                "[WARN] Lecture BODY impossible :",
                nc.name,
                exc,
            )

    return result


# ------------------------------------------------------------
# Cumul statistique
# ------------------------------------------------------------

def increment_rank(
    period: dict,
    admin: dict,
) -> None:

    period["total"] = (
        safe_int(
            period.get("total")
        )
        + 1
    )

    dep_code = admin[
        "department_code"
    ]

    deps = period.setdefault(
        "departments",
        {}
    )

    item = deps.setdefault(
        dep_code,
        {
            "code": dep_code,
            "name": admin[
                "department_name"
            ],
            "count": 0,
        },
    )

    item["count"] = (
        safe_int(
            item.get("count")
        )
        + 1
    )

    commune_code = admin[
        "code"
    ]

    communes = period.setdefault(
        "communes",
        {}
    )

    item = communes.setdefault(
        commune_code,
        {
            "code": commune_code,
            "name": admin[
                "name"
            ],
            "department_code": dep_code,
            "department_name": admin[
                "department_name"
            ],
            "count": 0,
        },
    )

    item["count"] = (
        safe_int(
            item.get("count")
        )
        + 1
    )


def update_recent(
    state: dict,
    dt: datetime,
    lat: float,
    lon: float,
) -> None:

    minute_key = dt.astimezone(
        UTC
    ).strftime(
        "%Y-%m-%dT%H:%M"
    )

    minutes = state.setdefault(
        "recent_minutes",
        {}
    )

    minutes[minute_key] = (
        safe_int(
            minutes.get(
                minute_key
            )
        )
        + 1
    )

    # Cellule ~5 km pour la carte de densité.
    lat_cell = round(
        lat / 0.05
    ) * 0.05

    lon_cell = round(
        lon / 0.05
    ) * 0.05

    key = (
        f"{lat_cell:.2f},"
        f"{lon_cell:.2f}"
    )

    cells = state.setdefault(
        "recent_cells",
        {}
    )

    cell = cells.setdefault(
        key,
        {
            "lat": round(
                lat_cell,
                2,
            ),
            "lon": round(
                lon_cell,
                2,
            ),
            "count": 0,
            "last_time": iso(dt),
        },
    )

    cell["count"] = (
        safe_int(
            cell.get("count")
        )
        + 1
    )

    cell["last_time"] = iso(
        dt
    )


def add_flash(
    state: dict,
    dt: datetime,
    admin: dict,
    lat: float,
    lon: float,
    current_year: str,
    current_month: str,
    current_day: str,
) -> None:

    local = dt.astimezone(
        PARIS
    )

    year_key = local.strftime(
        "%Y"
    )

    month_key = local.strftime(
        "%Y-%m"
    )

    day_key = local.strftime(
        "%Y-%m-%d"
    )

    # Année courante.
    if year_key == current_year:
        increment_rank(
            state["year"],
            admin,
        )

        month_num = local.strftime(
            "%m"
        )

        state["monthly_year"][
            month_num
        ] = (
            safe_int(
                state["monthly_year"].get(
                    month_num
                )
            )
            + 1
        )

    # Mois courant.
    if month_key == current_month:
        increment_rank(
            state["month"],
            admin,
        )

        daily = state.setdefault(
            "daily_month",
            {}
        )

        daily[day_key] = (
            safe_int(
                daily.get(
                    day_key
                )
            )
            + 1
        )

    # Jour courant.
    if day_key == current_day:
        increment_rank(
            state["day"],
            admin,
        )

        hour = local.strftime(
            "%H"
        )

        state["day"][
            "hourly"
        ][hour] = (
            safe_int(
                state["day"][
                    "hourly"
                ].get(hour)
            )
            + 1
        )

        minute = (
            local.minute // 5
        ) * 5

        five = (
            f"{local.hour:02d}:"
            f"{minute:02d}"
        )

        state["day"][
            "five_minute"
        ][five] = (
            safe_int(
                state["day"][
                    "five_minute"
                ].get(five)
            )
            + 1
        )

    update_recent(
        state,
        dt,
        lat,
        lon,
    )


# ------------------------------------------------------------
# Sorties
# ------------------------------------------------------------

def top_items(
    mapping: dict,
    limit: int = 20,
) -> List[dict]:

    items = [
        dict(item)
        for item in mapping.values()
    ]

    items.sort(
        key=lambda x: (
            -safe_int(
                x.get("count")
            ),
            str(
                x.get("name")
                or ""
            ),
        )
    )

    return items[:limit]


def recent_series(
    state: dict,
    now: datetime,
) -> Tuple[List[dict], int]:

    # 180 minutes en pas de 5 minutes.
    start = now.replace(
        second=0,
        microsecond=0,
    ) - timedelta(
        minutes=179
    )

    bins = []

    total_60 = 0

    minutes = state.get(
        "recent_minutes",
        {}
    )

    cursor = start.replace(
        minute=(
            start.minute // 5
        ) * 5
    )

    end = now.replace(
        second=0,
        microsecond=0,
    )

    while cursor <= end:
        count = 0

        for offset in range(5):
            minute_dt = cursor + timedelta(
                minutes=offset
            )

            key = minute_dt.strftime(
                "%Y-%m-%dT%H:%M"
            )

            count += safe_int(
                minutes.get(
                    key
                )
            )

        local = cursor.astimezone(
            PARIS
        )

        bins.append({
            "time": iso(cursor),
            "label": local.strftime(
                "%H:%M"
            ),
            "count": count,
        })

        cursor += timedelta(
            minutes=5
        )

    cutoff_60 = now - timedelta(
        minutes=60
    )

    for key, count in minutes.items():
        dt = parse_iso(
            key + ":00Z"
        )

        if dt and dt >= cutoff_60:
            total_60 += safe_int(
                count
            )

    return bins, total_60


def period_output(
    period: dict,
) -> dict:

    return {
        "total": safe_int(
            period.get("total")
        ),
        "top_departments": top_items(
            period.get(
                "departments",
                {}
            ),
            30,
        ),
        "top_communes": top_items(
            period.get(
                "communes",
                {}
            ),
            30,
        ),
    }


def build_output(
    state: dict,
    now: datetime,
    products_found: int,
    products_new: int,
    flashes_read_run: int,
    flashes_bbox_run: int,
    flashes_attributed_run: int,
) -> dict:

    recent, last_60 = recent_series(
        state,
        now,
    )

    tracking = parse_iso(
        state.get(
            "tracking_started_at"
        )
    )

    current_year, current_month, current_day = period_keys(
        now
    )

    year_start_local = datetime(
        int(current_year),
        1,
        1,
        tzinfo=PARIS,
    ).astimezone(UTC)

    month_start_local = datetime.strptime(
        current_month + "-01",
        "%Y-%m-%d",
    ).replace(
        tzinfo=PARIS
    ).astimezone(UTC)

    day_start_local = datetime.strptime(
        current_day,
        "%Y-%m-%d",
    ).replace(
        tzinfo=PARIS
    ).astimezone(UTC)

    def partial_since(
        period_start: datetime,
    ) -> bool:

        return bool(
            tracking
            and tracking > (
                period_start
                + timedelta(
                    minutes=15
                )
            )
        )

    recent_cells = [
        dict(item)
        for item in state.get(
            "recent_cells",
            {}
        ).values()
    ]

    recent_cells.sort(
        key=lambda x: (
            -safe_int(
                x.get("count")
            )
        )
    )

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "status": "ok",

        "generated_at": iso(now),
        "tracking_started_at": state.get(
            "tracking_started_at"
        ),

        "title": (
            "Statistiques foudre "
            "MTG Lightning Imager"
        ),

        "source": {
            "provider": "EUMETSAT",
            "satellite": "Meteosat-12 / MTG-I1",
            "sensor": "Lightning Imager (LI)",
            "product": "Lightning Flashes (LFL)",
            "collection_id": COLLECTION_ID,
            "spatial_resolution": "~4 km",
            "licence": "CC-BY-4.0",
            "data_type": (
                "Foudre totale détectée "
                "optiquement depuis l'espace"
            ),
        },

        "coverage": {
            "area": "France métropolitaine et Corse",
            "bbox": FRANCE_BBOX,
            "administrative_boundaries": (
                "Etalab contours-administratifs "
                "communes-1000m"
            ),
            "warning": (
                "Attribution commune/département "
                "approximative compte tenu de la "
                "résolution LI (~4 km) et de la parallaxe."
            ),
        },

        "periods": {
            "today": {
                **period_output(
                    state["day"]
                ),
                "key": current_day,
                "partial": partial_since(
                    day_start_local
                ),
            },

            "month": {
                **period_output(
                    state["month"]
                ),
                "key": current_month,
                "partial": partial_since(
                    month_start_local
                ),
            },

            "year": {
                **period_output(
                    state["year"]
                ),
                "key": current_year,
                "partial": partial_since(
                    year_start_local
                ),
            },
        },

        "last_60_minutes": last_60,

        "series": {
            "hourly_today": [
                {
                    "label": f"{hour}h",
                    "count": safe_int(
                        state["day"][
                            "hourly"
                        ].get(hour)
                    ),
                }
                for hour in [
                    f"{h:02d}"
                    for h in range(24)
                ]
            ],

            "five_minute_today": [
                {
                    "label": key,
                    "count": safe_int(
                        value
                    ),
                }
                for key, value in state[
                    "day"
                ][
                    "five_minute"
                ].items()
            ],

            "recent_5min_180min": recent,

            "daily_current_month": [
                {
                    "date": key,
                    "label": key[-2:],
                    "count": safe_int(
                        value
                    ),
                }
                for key, value in sorted(
                    state.get(
                        "daily_month",
                        {}
                    ).items()
                )
            ],

            "monthly_current_year": [
                {
                    "month": month,
                    "label": month,
                    "count": safe_int(
                        state.get(
                            "monthly_year",
                            {}
                        ).get(month)
                    ),
                }
                for month in [
                    f"{m:02d}"
                    for m in range(
                        1,
                        13
                    )
                ]
            ],
        },

        "recent_activity": {
            "hours": RECENT_HOURS,
            "cells": recent_cells[
                :3000
            ],
        },

        "diagnostics": {
            "products_found_this_run": (
                products_found
            ),
            "products_new_this_run": (
                products_new
            ),
            "flashes_read_this_run": (
                flashes_read_run
            ),
            "flashes_in_france_bbox_this_run": (
                flashes_bbox_run
            ),
            "flashes_attributed_this_run": (
                flashes_attributed_run
            ),
            "products_cached": len(
                state.get(
                    "processed_products",
                    {}
                )
            ),
            **state.get(
                "diagnostics",
                {}
            ),
        },
    }

    return output


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main() -> int:
    print(
        f"=== Foudre LFL v{VERSION} ==="
    )
    print("Build :", BUILD_ID)
    print("Collection :", COLLECTION_ID)

    now = now_utc()

    state, first_run = load_state(
        now
    )

    reset_periods(
        state,
        now,
    )

    prune_state(
        state,
        now,
    )

    if first_run:
        local_now = now.astimezone(
            PARIS
        )

        local_midnight = local_now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        start = local_midnight.astimezone(
            UTC
        ) - timedelta(
            minutes=10
        )

        earliest = now - timedelta(
            hours=FIRST_RUN_MAX_HOURS
        )

        if start < earliest:
            start = earliest

        print(
            "Premier lancement : reconstruction "
            "du jour en cours."
        )

    else:
        start = now - timedelta(
            minutes=NORMAL_LOOKBACK_MINUTES
        )

    products = search_products(
        start,
        now,
    )

    processed = state.setdefault(
        "processed_products",
        {}
    )

    new_products = [
        product
        for product in products
        if product_id(
            product
        ) not in processed
    ]

    print(
        "Nouveaux produits :",
        len(new_products),
    )

    admin_index = None

    if new_products:
        admin_index = AdministrativeIndex()

    current_year, current_month, current_day = period_keys(
        now
    )

    products_new = 0
    flashes_read_run = 0
    flashes_bbox_run = 0
    flashes_attributed_run = 0

    with tempfile.TemporaryDirectory(
        prefix="lfl_"
    ) as temp_dir:

        temp_dir = Path(
            temp_dir
        )

        for product in new_products:
            pid = product_id(
                product
            )

            try:
                downloaded = download_product(
                    product,
                    temp_dir,
                )

                flashes = read_product_flashes(
                    downloaded,
                    temp_dir,
                )

                flashes_read_run += len(
                    flashes
                )

                state["diagnostics"][
                    "flashes_read_total"
                ] = (
                    safe_int(
                        state["diagnostics"].get(
                            "flashes_read_total"
                        )
                    )
                    + len(flashes)
                )

                for flash in flashes:
                    lat = flash["lat"]
                    lon = flash["lon"]
                    dt = flash["time"]

                    if not inside_france_bbox(
                        lat,
                        lon,
                    ):
                        continue

                    flashes_bbox_run += 1

                    state["diagnostics"][
                        "flashes_in_bbox_total"
                    ] = (
                        safe_int(
                            state["diagnostics"].get(
                                "flashes_in_bbox_total"
                            )
                        )
                        + 1
                    )

                    admin = admin_index.locate(
                        lat,
                        lon,
                    )

                    if admin is None:
                        state["diagnostics"][
                            "flashes_unassigned_total"
                        ] = (
                            safe_int(
                                state["diagnostics"].get(
                                    "flashes_unassigned_total"
                                )
                            )
                            + 1
                        )
                        continue

                    flashes_attributed_run += 1

                    state["diagnostics"][
                        "flashes_attributed_total"
                    ] = (
                        safe_int(
                            state["diagnostics"].get(
                                "flashes_attributed_total"
                            )
                        )
                        + 1
                    )

                    add_flash(
                        state,
                        dt,
                        admin,
                        lat,
                        lon,
                        current_year,
                        current_month,
                        current_day,
                    )

                # On marque le produit uniquement après lecture complète.
                processed[pid] = iso(
                    now
                )

                products_new += 1

                state["diagnostics"][
                    "products_processed_total"
                ] = (
                    safe_int(
                        state["diagnostics"].get(
                            "products_processed_total"
                        )
                    )
                    + 1
                )

                try:
                    downloaded.unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass

            except Exception as exc:
                print(
                    "[WARN] Produit non traité :",
                    pid,
                    exc,
                )

    prune_state(
        state,
        now,
    )

    state["schema_version"] = 1
    state["module_version"] = VERSION
    state["generated_at"] = iso(
        now
    )

    CACHE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    output = build_output(
        state,
        now,
        products_found=len(products),
        products_new=products_new,
        flashes_read_run=flashes_read_run,
        flashes_bbox_run=flashes_bbox_run,
        flashes_attributed_run=flashes_attributed_run,
    )

    OUTPUT.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    print()
    print("=== CONTROLE ===")
    print("Version :", VERSION)
    print("Schema :", SCHEMA_VERSION)
    print("Produits nouveaux :", products_new)
    print("Flashes lus :", flashes_read_run)
    print(
        "Dans bbox France :",
        flashes_bbox_run,
    )
    print(
        "Attribués commune :",
        flashes_attributed_run,
    )
    print(
        "Aujourd'hui :",
        output["periods"]["today"]["total"],
    )
    print(
        "Mois :",
        output["periods"]["month"]["total"],
    )
    print(
        "Année :",
        output["periods"]["year"]["total"],
    )
    print(
        "60 dernières minutes :",
        output["last_60_minutes"],
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(
            main()
        )

    except Exception as exc:
        print(
            "ERREUR FATALE :",
            exc,
            file=sys.stderr,
        )
        raise

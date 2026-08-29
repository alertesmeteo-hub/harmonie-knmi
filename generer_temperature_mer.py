#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Température de la mer DENSE
Version 1.1.0

Objectif :
- récupérer le maximum de bouées / bateaux avec température de mer ;
- source principale : Météo-France SHIP / BUOY ;
- complément : NOAA / NDBC latest observations ;
- récupérer 4 fichiers quotidiens Météo-France à chaque run :
  aujourd'hui + J-1 + J-2 + J-3 ;
- conserver un cache glissant 72 h.

Vues :
- Température actuelle
- Variation 24 h
- Minimum 24 h
- Maximum 24 h
- Minimum 72 h
- Maximum 72 h
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


VERSION = "1.1.0"
SCHEMA_VERSION = 2
BUILD_ID = "temperature-mer-dense-ship-buoy-20260820"

OUTPUT = Path("observations_temperature_mer.json")
CACHE = Path("cache_temperature_mer_72h.json")

MF_BASE = (
    "https://donneespubliques.meteofrance.fr/"
    "donnees_libres/Txt/Marine"
)

NDBC_LATEST = (
    "https://www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt"
)

HTTP_TIMEOUT = 90

# 4 fichiers quotidiens couvrent aujourd'hui + 3 jours précédents.
MF_DAYS_TO_DOWNLOAD = 4

HISTORY_HOURS = 72
CACHE_MARGIN_HOURS = 8

# Un point reste affiché sur la carte "actuelle" s'il a transmis
# dans les 12 dernières heures.
MAX_CURRENT_AGE_HOURS = 12

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-temperature-mer-dense/{VERSION}",
})


# ------------------------------------------------------------
# Utilitaires
# ------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None

    return dt.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None

    s = str(value).strip()

    try:
        dt = datetime.fromisoformat(
            s.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        pass

    for fmt in (
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
        "%Y%m%d%H",
    ):
        try:
            return datetime.strptime(
                s,
                fmt,
            ).replace(tzinfo=timezone.utc)

        except Exception:
            pass

    return None


def fnum(value: Any) -> Optional[float]:
    if value in (
        None,
        "",
        "MM",
        "////",
        "999",
        "9999",
        "99999",
        "999999",
    ):
        return None

    try:
        n = float(
            str(value).strip().replace(",", ".")
        )
    except (TypeError, ValueError):
        return None

    if not math.isfinite(n):
        return None

    return n


def first(
    row: dict,
    names: Iterable[str],
) -> Any:

    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]

    low = {
        str(k).strip().lower(): v
        for k, v in row.items()
    }

    for name in names:
        value = low.get(str(name).lower())

        if value not in (None, ""):
            return value

    return None


def normalize_coord(
    value: Any,
    limit: float,
) -> Optional[float]:

    n = fnum(value)

    if n is None:
        return None

    # Certains exports anciens peuvent encoder des centièmes
    # de degré. On corrige uniquement si la valeur brute
    # dépasse la plage géographique normale.
    if abs(n) > limit:
        if abs(n / 100.0) <= limit:
            n = n / 100.0
        elif abs(n / 1000.0) <= limit:
            n = n / 1000.0

    if abs(n) > limit:
        return None

    return n


def kelvin_or_celsius_to_c(
    value: Any,
) -> Optional[float]:

    n = fnum(value)

    if n is None:
        return None

    # tmer Météo-France est documenté en kelvins.
    if n > 100:
        n -= 273.15

    if n < -3 or n > 45:
        return None

    return round(n, 1)


def celsius(
    value: Any,
) -> Optional[float]:

    n = fnum(value)

    if n is None:
        return None

    if n < -3 or n > 45:
        return None

    return round(n, 1)


def parse_delimited(
    raw: bytes,
) -> List[dict]:

    text = None

    for enc in (
        "utf-8-sig",
        "utf-8",
        "latin-1",
    ):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            pass

    if text is None:
        text = raw.decode(
            "utf-8",
            errors="replace",
        )

    sample = text[:15000]

    delimiter = (
        ";"
        if sample.count(";") >= sample.count(",")
        else ","
    )

    rows = []

    for row in csv.DictReader(
        io.StringIO(text),
        delimiter=delimiter,
    ):
        clean = {}

        for key, value in row.items():
            if key is None:
                continue

            clean[str(key).strip()] = (
                value.strip()
                if isinstance(value, str)
                else value
            )

        rows.append(clean)

    return rows


# ------------------------------------------------------------
# Zones conservées dans le JSON
# ------------------------------------------------------------

def in_target_area(
    lat: float,
    lon: float,
) -> bool:
    """
    Zone très large autour de la France + territoires français.

    L'objectif est de conserver bien plus de bouées/bateaux que
    la v1.0.0 tout en évitant d'embarquer inutilement tous les
    océans du globe dans le JSON WordPress.
    """

    boxes = (
        # Europe / Atlantique NE / Manche / Mer du Nord /
        # Méditerranée, avec marge importante.
        (30.0, 66.0, -32.0, 32.0),

        # Caraïbes / Antilles
        (8.0, 25.0, -75.0, -50.0),

        # Guyane / Atlantique équatorial ouest
        (-5.0, 12.0, -65.0, -40.0),

        # Saint-Pierre-et-Miquelon / NW Atlantique
        (40.0, 55.0, -70.0, -45.0),

        # Réunion / Mayotte / océan Indien occidental
        (-32.0, 2.0, 30.0, 75.0),

        # Nouvelle-Calédonie / Pacifique SO
        (-35.0, -10.0, 145.0, 180.0),

        # Polynésie française
        (-35.0, 2.0, -170.0, -120.0),

        # TAAF / sud océan Indien
        (-58.0, -30.0, 35.0, 90.0),
    )

    for min_lat, max_lat, min_lon, max_lon in boxes:
        if (
            min_lat <= lat <= max_lat
            and min_lon <= lon <= max_lon
        ):
            return True

    return False


# ------------------------------------------------------------
# Plateformes
# ------------------------------------------------------------

def canonical_id(
    value: Any,
) -> Optional[str]:

    if value is None:
        return None

    sid = str(value).strip().upper()

    if sid.endswith(".0") and sid[:-2].isdigit():
        sid = sid[:-2]

    sid = sid.replace(" ", "")

    return sid or None


def platform_type(
    sid: str,
) -> str:
    """
    Les indicatifs numériques des messages marins sont
    majoritairement des bouées/stations marines OMM.
    Les indicatifs alphanumériques sont généralement des
    indicatifs de navires ou stations mobiles.
    """

    if sid.isdigit():
        return "Bouée"

    return "Bateau / station marine"


def default_name(
    sid: str,
) -> str:

    if platform_type(sid) == "Bouée":
        return f"Bouée {sid}"

    return f"Bateau / station {sid}"


# ------------------------------------------------------------
# Cache
# ------------------------------------------------------------

def load_cache() -> dict:
    if not CACHE.exists():
        return {
            "schema_version": 1,
            "samples": [],
        }

    try:
        data = json.loads(
            CACHE.read_text(encoding="utf-8")
        )

        samples = data.get("samples")

        if not isinstance(samples, list):
            samples = []

        return {
            "schema_version": 1,
            "samples": samples,
        }

    except Exception as exc:
        print("[WARN] Cache ignoré :", exc)

        return {
            "schema_version": 1,
            "samples": [],
        }


def source_priority(
    source: str,
) -> int:

    if source == "Météo-France":
        return 20

    if source == "NOAA/NDBC":
        return 10

    return 0


def merge_samples(
    samples: List[dict],
) -> List[dict]:
    """
    Déduplique une même plateforme / même instant.
    Météo-France est prioritaire si la même mesure existe
    également chez NOAA/NDBC.
    """

    unique = {}

    for sample in samples:
        sid = canonical_id(sample.get("id"))
        dt = parse_datetime(sample.get("time"))

        if not sid or dt is None:
            continue

        key = (
            sid,
            iso(dt),
        )

        previous = unique.get(key)

        if (
            previous is None
            or source_priority(
                str(sample.get("source"))
            )
            > source_priority(
                str(previous.get("source"))
            )
        ):
            copy = dict(sample)
            copy["id"] = sid
            copy["time"] = iso(dt)
            unique[key] = copy

    return list(unique.values())


def clean_cache(
    cache: dict,
    ref_time: datetime,
) -> dict:

    cutoff = ref_time - timedelta(
        hours=HISTORY_HOURS + CACHE_MARGIN_HOURS
    )

    kept = []

    for sample in cache.get("samples", []):
        dt = parse_datetime(sample.get("time"))

        if dt is None or dt < cutoff:
            continue

        kept.append(sample)

    cache["samples"] = merge_samples(kept)

    return cache


# ------------------------------------------------------------
# Météo-France SHIP / BUOY
# ------------------------------------------------------------

def mf_url(
    day: datetime,
) -> str:

    return (
        f"{MF_BASE}/bouees."
        f"{day.strftime('%Y%m%d')}.csv"
    )


def download_mf_day(
    day: datetime,
) -> Optional[requests.Response]:

    url = mf_url(day)

    try:
        response = session.get(
            url,
            timeout=HTTP_TIMEOUT,
        )

    except requests.RequestException as exc:
        print(
            "[WARN] Météo-France",
            day.strftime("%Y-%m-%d"),
            exc,
        )
        return None

    if (
        response.status_code == 200
        and response.content
    ):
        print(
            "Météo-France",
            day.strftime("%Y-%m-%d"),
            ":",
            len(response.content),
            "octets",
        )

        return response

    print(
        "[INFO] Météo-France",
        day.strftime("%Y-%m-%d"),
        "HTTP",
        response.status_code,
    )

    return None


def mf_position(
    row: dict,
) -> Tuple[
    Optional[float],
    Optional[float],
]:

    lat = normalize_coord(
        first(
            row,
            (
                "lat",
                "latitude",
                "lat_deg",
                "latitude_deg",
                "y",
            ),
        ),
        90.0,
    )

    lon = normalize_coord(
        first(
            row,
            (
                "lon",
                "longitude",
                "long",
                "lon_deg",
                "longitude_deg",
                "x",
            ),
        ),
        180.0,
    )

    if lat is None or lon is None:
        return None, None

    return lat, lon


def parse_mf(
    response: requests.Response,
) -> List[dict]:

    rows = parse_delimited(
        response.content
    )

    samples = []

    for row in rows:
        sid = canonical_id(
            first(
                row,
                (
                    "numer_sta",
                    "station",
                    "id_station",
                    "indicatif",
                    "callsign",
                    "id",
                ),
            )
        )

        if not sid:
            continue

        temp = kelvin_or_celsius_to_c(
            first(
                row,
                (
                    "tmer",
                    "TMER",
                    "sst",
                    "SST",
                ),
            )
        )

        if temp is None:
            continue

        dt = parse_datetime(
            first(
                row,
                (
                    "date",
                    "DATE",
                    "datetime",
                    "time",
                ),
            )
        )

        if dt is None:
            continue

        lat, lon = mf_position(row)

        if lat is None or lon is None:
            continue

        if not in_target_area(lat, lon):
            continue

        name = first(
            row,
            (
                "nom",
                "name",
                "nom_station",
                "libelle",
                "station_name",
            ),
        )

        samples.append({
            "id": sid,
            "name": (
                str(name).strip()
                if name
                else default_name(sid)
            ),
            "platform_type": platform_type(sid),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "time": iso(dt),
            "sea_temp_c": temp,
            "source": "Météo-France",
        })

    print(
        "  ->",
        len(samples),
        "mesure(s) tmer cartographiable(s)",
    )

    return samples


# ------------------------------------------------------------
# NOAA / NDBC
# ------------------------------------------------------------

def download_ndbc() -> Optional[requests.Response]:

    try:
        response = session.get(
            NDBC_LATEST,
            timeout=HTTP_TIMEOUT,
        )

    except requests.RequestException as exc:
        print("[WARN] NOAA/NDBC :", exc)
        return None

    if (
        response.status_code == 200
        and response.text
    ):
        print(
            "NOAA/NDBC latest_obs :",
            len(response.content),
            "octets",
        )

        return response

    print(
        "[WARN] NOAA/NDBC HTTP",
        response.status_code,
    )

    return None


def parse_ndbc(
    response: requests.Response,
) -> List[dict]:

    lines = [
        line.rstrip()
        for line in response.text.splitlines()
        if line.strip()
    ]

    if not lines:
        return []

    header = None
    start = 0

    for i, line in enumerate(lines[:10]):
        if (
            line.startswith("#")
            and "LAT" in line
            and "LON" in line
            and "WTMP" in line
        ):
            header = line.lstrip("#").split()
            start = i + 1
            break

    if header is None:
        print(
            "[WARN] En-tête latest_obs NDBC non reconnu."
        )
        return []

    samples = []

    for line in lines[start:]:
        if line.startswith("#"):
            continue

        parts = line.split()

        if len(parts) < len(header):
            continue

        row = dict(
            zip(
                header,
                parts[:len(header)],
            )
        )

        sid = canonical_id(
            row.get("STN")
            or row.get("#STN")
        )

        if not sid:
            continue

        lat = normalize_coord(
            row.get("LAT"),
            90.0,
        )

        lon = normalize_coord(
            row.get("LON"),
            180.0,
        )

        if (
            lat is None
            or lon is None
            or not in_target_area(lat, lon)
        ):
            continue

        temp = celsius(
            row.get("WTMP")
        )

        if temp is None:
            continue

        # Rafales en mer : latest_obs NDBC fournit aussi WDIR/WSPD/GST
        # (vent moyen et rafale, m/s) sur la même ligne que la température
        # de l'eau — jusqu'ici récupérées mais jamais publiées.
        wspd_ms = fnum(row.get("WSPD"))
        gst_ms = fnum(row.get("GST"))
        wdir_deg = fnum(row.get("WDIR"))
        wind_speed_kmh = round(wspd_ms * 3.6, 1) if wspd_ms is not None else None
        wind_gust_kmh = round(gst_ms * 3.6, 1) if gst_ms is not None else None

        try:
            dt = datetime(
                int(row["YYYY"]),
                int(row["MM"]),
                int(row["DD"]),
                int(row["hh"]),
                int(row["mm"]),
                tzinfo=timezone.utc,
            )
        except Exception:
            continue

        samples.append({
            "id": sid,
            "name": default_name(sid),
            "platform_type": platform_type(sid),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "time": iso(dt),
            "sea_temp_c": temp,
            "wind_speed_kmh": wind_speed_kmh,
            "wind_gust_kmh": wind_gust_kmh,
            "wind_direction_deg": wdir_deg,
            "source": "NOAA/NDBC",
        })

    print(
        "  ->",
        len(samples),
        "mesure(s) WTMP dans les zones suivies",
    )

    return samples


# ------------------------------------------------------------
# Calculs
# ------------------------------------------------------------

def choose_nearest(
    samples: List[dict],
    target: datetime,
    tolerance_hours: float,
) -> Optional[dict]:

    best = None
    best_delta = None

    for sample in samples:
        dt = parse_datetime(
            sample.get("time")
        )

        if dt is None:
            continue

        delta = abs(
            (dt - target).total_seconds()
        )

        if (
            best_delta is None
            or delta < best_delta
        ):
            best = sample
            best_delta = delta

    if (
        best is None
        or best_delta is None
        or best_delta > tolerance_hours * 3600
    ):
        return None

    return best


def extreme(
    samples: List[dict],
    mode: str,
) -> Tuple[
    Optional[float],
    Optional[str],
]:

    values = []

    for sample in samples:
        temp = fnum(
            sample.get("sea_temp_c")
        )

        if temp is None:
            continue

        values.append(
            (
                temp,
                sample.get("time"),
            )
        )

    if not values:
        return None, None

    chosen = (
        min(values, key=lambda x: x[0])
        if mode == "min"
        else max(values, key=lambda x: x[0])
    )

    return (
        round(chosen[0], 1),
        chosen[1],
    )


def main() -> int:
    print(
        f"=== Température de la mer DENSE v{VERSION} ==="
    )
    print("Build :", BUILD_ID)

    now = utcnow()

    cache = load_cache()

    imported = []

    # 4 fichiers Météo-France = fenêtre 72 h pratiquement
    # complète dès le premier lancement.
    mf_total = 0

    for days_back in range(
        MF_DAYS_TO_DOWNLOAD
    ):
        day = now - timedelta(
            days=days_back
        )

        response = download_mf_day(day)

        if response is None:
            continue

        rows = parse_mf(response)

        imported.extend(rows)
        mf_total += len(rows)

    # Complément NOAA/NDBC
    ndbc_response = download_ndbc()

    if ndbc_response is not None:
        ndbc_rows = parse_ndbc(
            ndbc_response
        )
    else:
        ndbc_rows = []

    imported.extend(ndbc_rows)

    print()
    print(
        "Import courant :",
        mf_total,
        "Météo-France +",
        len(ndbc_rows),
        "NOAA/NDBC",
    )

    cache["samples"].extend(
        imported
    )

    # Référence temporelle = observation la plus récente
    # réellement disponible.
    all_times = [
        parse_datetime(s.get("time"))
        for s in cache["samples"]
    ]
    all_times = [
        dt
        for dt in all_times
        if dt is not None
    ]

    ref_time = (
        max(all_times)
        if all_times
        else now
    )

    cache = clean_cache(
        cache,
        ref_time,
    )

    cache["generated_at"] = iso(now)
    cache["latest_observation_at"] = iso(
        ref_time
    )

    CACHE.write_text(
        json.dumps(
            cache,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    print(
        "Cache 72 h :",
        len(cache["samples"]),
        "échantillon(s)",
    )

    by_station: Dict[
        str,
        List[dict],
    ] = defaultdict(list)

    for sample in cache["samples"]:
        sid = canonical_id(
            sample.get("id")
        )

        if sid:
            by_station[sid].append(
                sample
            )

    start_24 = ref_time - timedelta(
        hours=24
    )

    start_72 = ref_time - timedelta(
        hours=72
    )

    current_cutoff = ref_time - timedelta(
        hours=MAX_CURRENT_AGE_HOURS
    )

    stations = []

    for sid, samples in by_station.items():
        samples.sort(
            key=lambda s: (
                parse_datetime(
                    s.get("time")
                )
                or datetime.min.replace(
                    tzinfo=timezone.utc
                )
            )
        )

        latest = samples[-1]

        latest_time = parse_datetime(
            latest.get("time")
        )

        if latest_time is None:
            continue

        # Conserve davantage de plateformes que la v1.0,
        # tout en évitant les données franchement obsolètes.
        if latest_time < current_cutoff:
            continue

        current = fnum(
            latest.get("sea_temp_c")
        )

        if current is None:
            continue

        s24 = [
            s
            for s in samples
            if (
                parse_datetime(s.get("time"))
                and parse_datetime(s.get("time"))
                > start_24
            )
        ]

        s72 = [
            s
            for s in samples
            if (
                parse_datetime(s.get("time"))
                and parse_datetime(s.get("time"))
                > start_72
            )
        ]

        old = choose_nearest(
            samples,
            latest_time - timedelta(
                hours=24
            ),
            tolerance_hours=4.0,
        )

        old_temp = (
            fnum(old.get("sea_temp_c"))
            if old
            else None
        )

        variation = (
            round(
                current - old_temp,
                1,
            )
            if old_temp is not None
            else None
        )

        min24, min24time = extreme(
            s24,
            "min",
        )

        max24, max24time = extreme(
            s24,
            "max",
        )

        min72, min72time = extreme(
            s72,
            "min",
        )

        max72, max72time = extreme(
            s72,
            "max",
        )

        age_minutes = int(
            max(
                0,
                (
                    ref_time - latest_time
                ).total_seconds()
                / 60,
            )
        )

        ptype = (
            latest.get("platform_type")
            or platform_type(sid)
        )

        stations.append({
            "id": sid,
            "name": (
                latest.get("name")
                or default_name(sid)
            ),
            "platform_type": ptype,
            "lat": latest.get("lat"),
            "lon": latest.get("lon"),
            "source": latest.get("source"),

            "sea_temp_c": round(
                current,
                1,
            ),
            "sea_temp_time": latest.get(
                "time"
            ),
            "age_minutes": age_minutes,

            "wind_speed_kmh": latest.get("wind_speed_kmh"),
            "wind_gust_kmh": latest.get("wind_gust_kmh"),
            "wind_direction_deg": latest.get("wind_direction_deg"),

            "temp_24h_ago_c": (
                round(old_temp, 1)
                if old_temp is not None
                else None
            ),
            "temp_24h_ago_time": (
                old.get("time")
                if old
                else None
            ),
            "variation_24h_c": variation,

            "min_24h_c": min24,
            "min_24h_time": min24time,

            "max_24h_c": max24,
            "max_24h_time": max24time,

            "min_72h_c": min72,
            "min_72h_time": min72time,

            "max_72h_c": max72,
            "max_72h_time": max72time,
        })

    stations.sort(
        key=lambda st: (
            -st["sea_temp_c"],
            st["name"],
        )
    )

    def values(
        field: str,
    ) -> List[float]:

        result = []

        for station in stations:
            value = fnum(
                station.get(field)
            )

            if value is not None:
                result.append(value)

        return result


    def metric(
        field: str,
        label: str,
    ) -> dict:

        vals = values(field)

        return {
            "label": label,
            "stations": len(vals),
            "min": (
                round(min(vals), 1)
                if vals
                else None
            ),
            "max": (
                round(max(vals), 1)
                if vals
                else None
            ),
        }


    metrics = {
        "sea_temp_c": metric(
            "sea_temp_c",
            "Température de la mer actuelle",
        ),
        "variation_24h_c": metric(
            "variation_24h_c",
            "Variation sur 24 h",
        ),
        "min_24h_c": metric(
            "min_24h_c",
            "Minimum sur 24 h",
        ),
        "max_24h_c": metric(
            "max_24h_c",
            "Maximum sur 24 h",
        ),
        "min_72h_c": metric(
            "min_72h_c",
            "Minimum sur 72 h",
        ),
        "max_72h_c": metric(
            "max_72h_c",
            "Maximum sur 72 h",
        ),
    }

    source_counts = defaultdict(int)
    platform_counts = defaultdict(int)

    for station in stations:
        source_counts[
            station.get("source")
            or "Inconnue"
        ] += 1

        platform_counts[
            station.get("platform_type")
            or "Autre"
        ] += 1

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "status": "ok",

        "generated_at": iso(now),
        "latest_observation_at": iso(
            ref_time
        ),

        "title": (
            "Température de la mer — "
            "bouées & bateaux"
        ),
        "unit": "°C",

        "coverage": {
            "mode": (
                "Météo-France SHIP/BUOY "
                "+ NOAA/NDBC + cache 72h"
            ),
            "mf_daily_files_requested": (
                MF_DAYS_TO_DOWNLOAD
            ),
            "history_hours": HISTORY_HOURS,
            "current_max_age_hours": (
                MAX_CURRENT_AGE_HOURS
            ),
            "samples_cached": len(
                cache["samples"]
            ),
            "sources_current": dict(
                source_counts
            ),
            "platforms_current": dict(
                platform_counts
            ),
        },

        "metrics": metrics,

        "stations_total": len(stations),

        "source": {
            "primary": (
                "Météo-France — messages "
                "SHIP et BUOY, paramètre tmer"
            ),
            "complement": (
                "NOAA/NDBC — latest_obs, WTMP"
            ),
            "mf_url_pattern": (
                MF_BASE
                + "/bouees.YYYYMMDD.csv"
            ),
            "ndbc_url": NDBC_LATEST,
        },

        "stations": stations,
    }

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
    print("=== CONTRÔLE FINAL ===")
    print("Version :", VERSION)
    print("Schema :", SCHEMA_VERSION)
    print("Build :", BUILD_ID)
    print(
        "Plateformes actuelles :",
        len(stations),
    )
    print(
        "Types :",
        dict(platform_counts),
    )
    print(
        "Sources :",
        dict(source_counts),
    )

    for field, item in metrics.items():
        print(
            field,
            ":",
            item["stations"],
            "point(s) ; min",
            item["min"],
            "; max",
            item["max"],
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

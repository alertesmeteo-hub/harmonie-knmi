#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Température de la mer
Version 1.0.0

Source principale :
- Météo-France, observations en mer SHIP / BUOY
- paramètre tmer (K)

Secours :
- NOAA/NDBC latest observations, champ WTMP (°C)

Architecture stable :
- téléchargement du fichier marin courant ;
- cache glissant de 72 h ;
- calcul température actuelle, variation 24 h,
  min/max 24 h et min/max 72 h.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "temperature-mer-cache-72h-20260820"

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
HISTORY_HOURS = 72
CACHE_MARGIN_HOURS = 6

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-temperature-mer/{VERSION}",
})


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None

    s = str(value).strip()

    # ISO
    try:
        dt = datetime.fromisoformat(
            s.replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Météo-France AAAAMMDDHHMISS
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
    if value in (None, "", "MM", "999", "9999", "99999"):
        return None

    try:
        n = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None

    if not math.isfinite(n):
        return None

    return n


def first(row: dict, names: Iterable[str]) -> Any:
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


def kelvin_to_c(value: Any) -> Optional[float]:
    n = fnum(value)

    if n is None:
        return None

    # Le champ Météo-France tmer est documenté en K.
    # Si la source renvoie déjà une valeur plausible en °C,
    # on l'accepte également.
    if n > 100:
        n -= 273.15

    if n < -3 or n > 45:
        return None

    return round(n, 1)


def celsius(value: Any) -> Optional[float]:
    n = fnum(value)

    if n is None or n < -3 or n > 45:
        return None

    return round(n, 1)


def parse_delimited(raw: bytes) -> List[dict]:
    text = None

    for enc in ("utf-8-sig", "utf-8", "latin-1"):
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

    sample = text[:10000]
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
# Géographie
# ------------------------------------------------------------

def in_french_area(
    lat: float,
    lon: float,
) -> bool:
    """
    France métropolitaine + principaux territoires ultramarins.
    Boîtes volontairement larges pour inclure les bouées au large.
    """

    boxes = (
        # Métropole, Manche, Atlantique, Méditerranée, Corse
        (39.0, 53.5, -12.0, 14.0),

        # Antilles
        (13.0, 20.0, -66.5, -58.0),

        # Guyane
        (0.0, 9.0, -57.0, -48.0),

        # Réunion / Mayotte
        (-25.0, -10.0, 38.0, 60.0),

        # Saint-Pierre-et-Miquelon
        (45.0, 49.0, -59.0, -53.0),

        # Nouvelle-Calédonie
        (-27.0, -17.0, 155.0, 173.0),

        # Polynésie française
        (-32.0, -5.0, -160.0, -130.0),
    )

    for min_lat, max_lat, min_lon, max_lon in boxes:
        if (
            min_lat <= lat <= max_lat
            and min_lon <= lon <= max_lon
        ):
            return True

    return False


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


def clean_cache(
    cache: dict,
    ref_time: datetime,
) -> dict:

    cutoff = ref_time - timedelta(
        hours=HISTORY_HOURS + CACHE_MARGIN_HOURS
    )

    unique = {}

    for sample in cache.get("samples", []):
        dt = parse_iso(sample.get("time"))

        if dt is None or dt < cutoff:
            continue

        key = (
            str(sample.get("id")),
            str(sample.get("time")),
            str(sample.get("source")),
        )

        unique[key] = sample

    cache["samples"] = list(unique.values())

    return cache


# ------------------------------------------------------------
# Météo-France SHIP / BUOY
# ------------------------------------------------------------

def marine_url(day: datetime) -> str:
    return (
        f"{MF_BASE}/bouees."
        f"{day.strftime('%Y%m%d')}.csv"
    )


def download_mf_day(
    day: datetime,
) -> Optional[requests.Response]:

    url = marine_url(day)

    try:
        response = session.get(
            url,
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        print("[WARN] Météo-France :", exc)
        return None

    if response.status_code == 200 and response.content:
        print(
            "Météo-France :",
            day.strftime("%Y-%m-%d"),
            len(response.content),
            "octets",
        )
        return response

    print(
        "[INFO] Fichier Météo-France indisponible :",
        response.status_code,
        url,
    )

    return None


def mf_station_id(row: dict) -> Optional[str]:
    value = first(
        row,
        (
            "numer_sta",
            "id_station",
            "station",
            "indicatif",
            "id",
        ),
    )

    if value is None:
        return None

    value = str(value).strip()

    return value or None


def mf_lat_lon(
    row: dict,
) -> Tuple[Optional[float], Optional[float]]:

    lat = fnum(
        first(
            row,
            (
                "lat",
                "latitude",
                "LAT",
                "LATITUDE",
            ),
        )
    )

    lon = fnum(
        first(
            row,
            (
                "lon",
                "longitude",
                "LON",
                "LONGITUDE",
            ),
        )
    )

    if (
        lat is None
        or lon is None
        or not (-90 <= lat <= 90)
        or not (-180 <= lon <= 180)
    ):
        return None, None

    return lat, lon


def parse_mf_response(
    response: requests.Response,
) -> List[dict]:

    rows = parse_delimited(response.content)
    samples = []

    for row in rows:
        sid = mf_station_id(row)

        if not sid:
            continue

        temp = kelvin_to_c(
            first(row, ("tmer", "TMER"))
        )

        if temp is None:
            continue

        dt = parse_iso(
            first(row, ("date", "DATE"))
        )

        if dt is None:
            continue

        lat, lon = mf_lat_lon(row)

        # Certaines versions historiques du fichier SHIP/BUOY
        # peuvent ne pas exposer la position en colonnes CSV.
        # Ces lignes ne sont pas cartographiables et sont donc
        # laissées au fallback NDBC.
        if lat is None or lon is None:
            continue

        if not in_french_area(lat, lon):
            continue

        name = first(
            row,
            (
                "nom",
                "name",
                "nom_station",
                "libelle",
            ),
        )

        samples.append({
            "id": f"MF-{sid}",
            "name": (
                str(name).strip()
                if name
                else f"Bouée {sid}"
            ),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "time": iso(dt),
            "sea_temp_c": temp,
            "source": "Météo-France",
        })

    return samples


# ------------------------------------------------------------
# NOAA / NDBC fallback
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

    if response.status_code == 200 and response.text:
        return response

    print(
        "[WARN] NOAA/NDBC HTTP",
        response.status_code,
    )

    return None


def parse_ndbc_latest(
    response: requests.Response,
) -> List[dict]:
    """
    latest_obs.txt :
    #STN LAT LON YYYY MM DD hh mm WDIR WSPD GST WVHT ...
    ... ATMP WTMP ...
    """

    lines = [
        line.rstrip()
        for line in response.text.splitlines()
        if line.strip()
    ]

    if not lines:
        return []

    header_index = None
    header = None

    for i, line in enumerate(lines[:5]):
        if line.startswith("#") and "LAT" in line and "LON" in line:
            header_index = i
            header = line.lstrip("#").split()
            break

    if header is None:
        return []

    samples = []

    for line in lines[header_index + 1:]:
        if line.startswith("#"):
            continue

        parts = line.split()

        if len(parts) < len(header):
            continue

        row = dict(zip(header, parts))

        sid = str(
            row.get("STN")
            or row.get("#STN")
            or ""
        ).strip()

        if not sid:
            continue

        lat = fnum(row.get("LAT"))
        lon = fnum(row.get("LON"))

        if (
            lat is None
            or lon is None
            or not in_french_area(lat, lon)
        ):
            continue

        temp = celsius(row.get("WTMP"))

        if temp is None:
            continue

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
            "id": f"NDBC-{sid}",
            "name": f"Bouée {sid}",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "time": iso(dt),
            "sea_temp_c": temp,
            "source": "NOAA/NDBC",
        })

    return samples


# ------------------------------------------------------------
# Calculs
# ------------------------------------------------------------

def choose_nearest(
    samples: List[dict],
    target: datetime,
    tolerance_hours: float = 2.5,
) -> Optional[dict]:

    best = None
    best_delta = None

    for sample in samples:
        dt = parse_iso(sample.get("time"))

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
) -> Tuple[Optional[float], Optional[str]]:

    vals = []

    for sample in samples:
        temp = fnum(sample.get("sea_temp_c"))

        if temp is None:
            continue

        vals.append(
            (
                temp,
                sample.get("time"),
            )
        )

    if not vals:
        return None, None

    chosen = (
        min(vals, key=lambda x: x[0])
        if mode == "min"
        else max(vals, key=lambda x: x[0])
    )

    return round(chosen[0], 1), chosen[1]


def main() -> int:
    print(
        f"=== Température de la mer v{VERSION} ==="
    )
    print("Build :", BUILD_ID)

    now = utcnow()
    cache = clean_cache(
        load_cache(),
        now,
    )

    new_samples = []

    # Aujourd'hui + veille :
    # permet le fonctionnement juste après minuit UTC.
    for days_back in (0, 1):
        day = now - timedelta(days=days_back)
        response = download_mf_day(day)

        if response is None:
            continue

        new_samples.extend(
            parse_mf_response(response)
        )

    mf_count = len(new_samples)

    # Secours / complément NDBC.
    ndbc_response = download_ndbc()

    if ndbc_response is not None:
        ndbc_samples = parse_ndbc_latest(
            ndbc_response
        )
        new_samples.extend(ndbc_samples)
    else:
        ndbc_samples = []

    print(
        "Nouveaux échantillons :",
        len(new_samples),
        "(Météo-France:",
        mf_count,
        "/ NOAA-NDBC:",
        len(ndbc_samples),
        ")",
    )

    # Ajout + déduplication.
    cache["samples"].extend(new_samples)

    latest_dt = now

    if cache["samples"]:
        times = [
            parse_iso(s.get("time"))
            for s in cache["samples"]
        ]
        times = [dt for dt in times if dt]

        if times:
            latest_dt = max(times)

    cache = clean_cache(
        cache,
        latest_dt,
    )

    cache["generated_at"] = iso(now)
    cache["latest_observation_at"] = iso(latest_dt)

    CACHE.write_text(
        json.dumps(
            cache,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    by_station: Dict[str, List[dict]] = defaultdict(list)

    for sample in cache["samples"]:
        sid = str(sample.get("id") or "").strip()

        if sid:
            by_station[sid].append(sample)

    start_24 = latest_dt - timedelta(hours=24)
    start_72 = latest_dt - timedelta(hours=72)

    stations = []

    for sid, samples in by_station.items():
        samples.sort(
            key=lambda s: (
                parse_iso(s.get("time"))
                or datetime.min.replace(
                    tzinfo=timezone.utc
                )
            )
        )

        latest = samples[-1]
        latest_time = parse_iso(latest.get("time"))

        if latest_time is None:
            continue

        # Écarte les stations devenues trop anciennes.
        if latest_time < latest_dt - timedelta(hours=6):
            continue

        current = fnum(latest.get("sea_temp_c"))

        if current is None:
            continue

        s24 = [
            s
            for s in samples
            if parse_iso(s.get("time"))
            and parse_iso(s.get("time")) > start_24
        ]

        s72 = [
            s
            for s in samples
            if parse_iso(s.get("time"))
            and parse_iso(s.get("time")) > start_72
        ]

        old = choose_nearest(
            samples,
            latest_time - timedelta(hours=24),
            tolerance_hours=3.0,
        )

        old_temp = (
            fnum(old.get("sea_temp_c"))
            if old
            else None
        )

        variation = (
            round(current - old_temp, 1)
            if old_temp is not None
            else None
        )

        min24, min24time = extreme(s24, "min")
        max24, max24time = extreme(s24, "max")
        min72, min72time = extreme(s72, "min")
        max72, max72time = extreme(s72, "max")

        stations.append({
            "id": sid,
            "name": latest.get("name") or sid,
            "lat": latest.get("lat"),
            "lon": latest.get("lon"),
            "source": latest.get("source"),

            "sea_temp_c": round(current, 1),
            "sea_temp_time": latest.get("time"),

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
        key=lambda s: (
            -s["sea_temp_c"],
            s["name"],
        )
    )

    def values(field: str) -> List[float]:
        vals = []

        for station in stations:
            value = fnum(station.get(field))

            if value is not None:
                vals.append(value)

        return vals

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

    source_counts = {}

    for station in stations:
        source = station.get("source") or "Inconnue"
        source_counts[source] = (
            source_counts.get(source, 0) + 1
        )

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "status": "ok",

        "generated_at": iso(now),
        "latest_observation_at": iso(latest_dt),

        "title": "Température de la mer",
        "unit": "°C",

        "coverage": {
            "mode": "cache_glissant_72h",
            "history_hours": HISTORY_HOURS,
            "samples_cached": len(cache["samples"]),
            "sources_current": source_counts,
            "note": (
                "Au premier lancement, les historiques 24 h et 72 h "
                "se remplissent progressivement."
            ),
        },

        "metrics": metrics,
        "stations_total": len(stations),

        "source": {
            "primary": (
                "Météo-France — observations en mer SHIP/BUOY, tmer"
            ),
            "fallback": (
                "NOAA/NDBC — latest observations, WTMP"
            ),
            "mf_url_pattern": (
                MF_BASE + "/bouees.YYYYMMDD.csv"
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
    print("=== CONTRÔLE ===")
    print("Version :", VERSION)
    print("Schema :", SCHEMA_VERSION)
    print("Build :", BUILD_ID)
    print("Stations :", len(stations))
    print("Sources :", source_counts)

    for field, item in metrics.items():
        print(
            field,
            ":",
            item["stations"],
            "station(s)",
            "min",
            item["min"],
            "max",
            item["max"],
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            "ERREUR FATALE :",
            exc,
            file=sys.stderr,
        )
        raise

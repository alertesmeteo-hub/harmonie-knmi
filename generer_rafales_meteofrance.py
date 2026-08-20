#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Vent & Rafales Météo-France
Version 1.2.0 STABLE

Principe :
- un seul paquet horaire Météo-France est téléchargé à chaque run ;
- un cache glissant conserve les observations des 72 dernières heures ;
- les maxima rafales / vent moyen 24 h et 72 h sont calculés depuis ce cache.

Cela évite de refaire 72 appels API à chaque heure.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


VERSION = "1.2.0"
SCHEMA_VERSION = 3
BUILD_ID = "vent-rafales-cache-72h-stable-20260820"

PACKAGE_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)

STATIONS_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPObs/v2/liste-stations"
)

OUTPUT = Path("observations_rafales.json")
CACHE = Path("cache_vent_rafales_72h.json")

HTTP_TIMEOUT = 90
LATEST_RETRIES_HOURS = 4
REQUEST_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.50"))

HISTORY_HOURS = 72
CACHE_MARGIN_HOURS = 3

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-vent-rafales/{VERSION}",
})


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
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fnum(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        n = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def first(row: dict, names: Iterable[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]

    low = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = low.get(str(name).lower())
        if value not in (None, ""):
            return value

    return None


def station_id(row: dict) -> Optional[str]:
    value = first(
        row,
        (
            "geo_id_insee",
            "NUM_POSTE",
            "num_poste",
            "numer_sta",
            "id_station",
        ),
    )

    if value is None:
        return None

    sid = str(value).strip()

    if sid.endswith(".0") and sid[:-2].isdigit():
        sid = sid[:-2]

    return sid or None


def parse_csv(raw: bytes) -> List[dict]:
    text = None

    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            pass

    if text is None:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:10000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","

    out = []

    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        clean = {}

        for key, value in row.items():
            if key is None:
                continue

            clean[str(key).strip()] = (
                value.strip()
                if isinstance(value, str)
                else value
            )

        out.append(clean)

    return out


def get_secret(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(f"Secret GitHub absent : {name}")

    value = value.replace("\r", "").replace("\n", "").strip()

    for prefix in ("apikey:", "apiKey:", "Bearer ", "bearer "):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()

    return value


def headers(key: str) -> dict:
    return {
        "apikey": key,
        "accept": "*/*",
    }


def speed_kmh(value_ms: Any) -> Optional[float]:
    value = fnum(value_ms)

    if value is None:
        return None

    if value < 0 or value > 150:
        return None

    return round(value * 3.6, 1)


def direction(value: Any) -> Optional[int]:
    n = fnum(value)

    if n is None:
        return None

    return int(round(n)) % 360


def is_sapc(name: str) -> bool:
    words = re.sub(
        r"[^A-Z0-9]+",
        " ",
        str(name).upper(),
    ).split()

    return "SAPC" in words


def extract_gust(
    row: dict,
) -> Tuple[Optional[float], Optional[int], Optional[str]]:

    candidates = (
        (("raf", "RAF"), ("ddraf", "DDRAF"), "raf"),
        (("fxi3s", "FXI3S"), ("dxi3s", "DXI3S"), "FXI3S"),
        (("fxi", "FXI"), ("dxi", "DXI"), "FXI"),
        (("fxy", "FXY"), ("dxy", "DXY"), "FXY"),
    )

    for speed_names, dir_names, label in candidates:
        kmh = speed_kmh(first(row, speed_names))

        if kmh is not None:
            return (
                kmh,
                direction(first(row, dir_names)),
                label,
            )

    return None, None, None


def extract_mean_wind(
    row: dict,
) -> Tuple[Optional[float], Optional[int]]:

    return (
        speed_kmh(first(row, ("ff", "FF"))),
        direction(first(row, ("dd", "DD"))),
    )


def package_request(
    key: str,
    hour: datetime,
) -> Optional[requests.Response]:

    target = hour.replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    for attempt in range(3):
        response = session.get(
            PACKAGE_URL,
            params={
                "date": iso(target),
                "format": "csv",
            },
            headers=headers(key),
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 200:
            return response

        if response.status_code in (400, 404):
            return None

        if response.status_code == 429:
            wait = 15 + attempt * 10

            print(
                f"[WARN] HTTP 429 pour {iso(target)} ; "
                f"nouvel essai dans {wait}s."
            )

            time.sleep(wait)
            continue

        if response.status_code == 401:
            raise RuntimeError("Package Observations : HTTP 401.")

        if response.status_code == 403:
            raise RuntimeError("Package Observations : HTTP 403.")

        response.raise_for_status()

    raise RuntimeError(
        f"Package Observations indisponible : {iso(target)}"
    )


def find_latest_package(
    key: str,
) -> Tuple[datetime, requests.Response]:

    base = utcnow().replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    for back in range(LATEST_RETRIES_HOURS):
        hour = base - timedelta(hours=back)

        print("Recherche paquet :", iso(hour))

        response = package_request(key, hour)

        if response is not None:
            print("Dernier paquet disponible :", iso(hour))
            return hour, response

        time.sleep(REQUEST_DELAY)

    raise RuntimeError(
        "Aucun paquet horaire disponible entre H et H-3."
    )


def load_station_names(
    key: str,
) -> Dict[str, str]:

    response = session.get(
        STATIONS_URL,
        headers=headers(key),
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code != 200:
        print(
            "[WARN] liste-stations HTTP",
            response.status_code,
        )
        return {}

    content_type = (
        response.headers.get("content-type") or ""
    ).lower()

    if "json" in content_type:
        try:
            payload = response.json()

            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = (
                    payload.get("data")
                    or payload.get("records")
                    or payload.get("results")
                    or []
                )
            else:
                rows = []

        except Exception:
            rows = parse_csv(response.content)

    else:
        rows = parse_csv(response.content)

    names = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        sid = station_id(row)

        if not sid:
            continue

        name = first(
            row,
            (
                "nom_usuel",
                "NOM_USUEL",
                "nom",
                "NOM",
                "name",
                "libelle",
            ),
        )

        if name:
            names[sid] = str(name).strip()

    return names


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

        if not isinstance(data, dict):
            raise ValueError("cache non objet")

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
    latest_hour: datetime,
) -> dict:

    # On garde 72 h + une petite marge pour les éventuels décalages.
    cutoff = latest_hour - timedelta(
        hours=HISTORY_HOURS + CACHE_MARGIN_HOURS
    )

    cleaned = []

    for sample in cache.get("samples", []):
        dt = parse_iso(sample.get("time"))

        if dt is None or dt < cutoff:
            continue

        cleaned.append(sample)

    # Déduplication par (station, time)
    unique = {}

    for sample in cleaned:
        key = (
            str(sample.get("id")),
            str(sample.get("time")),
        )
        unique[key] = sample

    cache["samples"] = list(unique.values())

    return cache


def add_latest_package_to_cache(
    cache: dict,
    response: requests.Response,
    latest_hour: datetime,
) -> int:

    rows = parse_csv(response.content)
    added = 0

    # Supprime les éventuels samples de la même heure avant réinsertion.
    latest_iso = iso(latest_hour)

    cache["samples"] = [
        s
        for s in cache.get("samples", [])
        if s.get("time") != latest_iso
    ]

    for row in rows:
        sid = station_id(row)

        if not sid:
            continue

        lat = fnum(first(row, ("lat", "LAT", "latitude")))
        lon = fnum(first(row, ("lon", "LON", "longitude")))

        if (
            lat is None
            or lon is None
            or not (-90 <= lat <= 90)
            or not (-180 <= lon <= 180)
        ):
            continue

        validity = parse_iso(
            first(
                row,
                (
                    "validity_time",
                    "reference_time",
                    "date",
                ),
            )
        ) or latest_hour

        gust, gust_dir, gust_source = extract_gust(row)
        mean_wind, mean_dir = extract_mean_wind(row)

        if gust is None and mean_wind is None:
            continue

        cache["samples"].append({
            "id": sid,
            "time": iso(validity),
            "lat": round(lat, 6),
            "lon": round(lon, 6),

            "gust_kmh": gust,
            "gust_direction_deg": gust_dir,
            "gust_source": gust_source,

            "mean_wind_kmh": mean_wind,
            "mean_wind_direction_deg": mean_dir,
        })

        added += 1

    return added


def choose_max(
    samples: List[dict],
    value_field: str,
    dir_field: str,
) -> Tuple[
    Optional[float],
    Optional[int],
    Optional[str],
]:

    best = None

    for sample in samples:
        value = fnum(sample.get(value_field))

        if value is None:
            continue

        if best is None or value > best[0]:
            best = (
                value,
                sample.get(dir_field),
                sample.get("time"),
            )

    if best is None:
        return None, None, None

    return (
        round(best[0], 1),
        best[1],
        best[2],
    )


def main() -> int:
    print(
        f"=== Vent & Rafales Météo-France v{VERSION} ==="
    )
    print("Build :", BUILD_ID)

    package_key = get_secret(
        "METEOFRANCE_PACKAGE_OBS_KEY"
    )
    obs_key = get_secret(
        "METEOFRANCE_OBS_TOKEN"
    )

    # Seulement 1 paquet récent.
    latest_hour, response = find_latest_package(
        package_key
    )

    cache = clean_cache(
        load_cache(),
        latest_hour,
    )

    added = add_latest_package_to_cache(
        cache,
        response,
        latest_hour,
    )

    cache = clean_cache(
        cache,
        latest_hour,
    )

    cache["generated_at"] = iso(utcnow())
    cache["latest_observation_at"] = iso(latest_hour)

    CACHE.write_text(
        json.dumps(
            cache,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    print("Samples ajoutés :", added)
    print(
        "Samples présents dans le cache :",
        len(cache["samples"]),
    )

    names = load_station_names(obs_key)

    by_station: Dict[str, List[dict]] = {}

    for sample in cache["samples"]:
        sid = str(sample.get("id") or "").strip()

        if not sid:
            continue

        by_station.setdefault(sid, []).append(sample)

    start_24 = latest_hour - timedelta(hours=24)
    start_72 = latest_hour - timedelta(hours=72)

    stations = []
    excluded_sapc = 0

    for sid, samples in by_station.items():
        samples.sort(
            key=lambda s: parse_iso(s.get("time"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )

        name = names.get(sid, sid)

        if is_sapc(name):
            excluded_sapc += 1
            continue

        latest_candidates = [
            s
            for s in samples
            if parse_iso(s.get("time"))
            and abs(
                (
                    parse_iso(s.get("time"))
                    - latest_hour
                ).total_seconds()
            ) <= 3600
        ]

        if not latest_candidates:
            # La station reste dans les vues historiques,
            # même si elle n'a pas publié au dernier paquet.
            latest_sample = samples[-1]
        else:
            latest_sample = latest_candidates[-1]

        samples_24 = [
            s
            for s in samples
            if parse_iso(s.get("time"))
            and parse_iso(s.get("time")) > start_24
        ]

        samples_72 = [
            s
            for s in samples
            if parse_iso(s.get("time"))
            and parse_iso(s.get("time")) > start_72
        ]

        gust24, gust24dir, gust24time = choose_max(
            samples_24,
            "gust_kmh",
            "gust_direction_deg",
        )

        gust72, gust72dir, gust72time = choose_max(
            samples_72,
            "gust_kmh",
            "gust_direction_deg",
        )

        mean24, mean24dir, mean24time = choose_max(
            samples_24,
            "mean_wind_kmh",
            "mean_wind_direction_deg",
        )

        mean72, mean72dir, mean72time = choose_max(
            samples_72,
            "mean_wind_kmh",
            "mean_wind_direction_deg",
        )

        stations.append({
            "id": sid,
            "name": name,
            "lat": latest_sample.get("lat"),
            "lon": latest_sample.get("lon"),

            "latest_gust_kmh": latest_sample.get("gust_kmh"),
            "latest_direction_deg": latest_sample.get(
                "gust_direction_deg"
            ),
            "latest_gust_time": latest_sample.get("time"),

            "gust_24h_kmh": gust24,
            "gust_24h_direction_deg": gust24dir,
            "gust_24h_time": gust24time,

            "gust_72h_kmh": gust72,
            "gust_72h_direction_deg": gust72dir,
            "gust_72h_time": gust72time,

            "latest_mean_wind_kmh": latest_sample.get(
                "mean_wind_kmh"
            ),
            "latest_mean_wind_direction_deg": latest_sample.get(
                "mean_wind_direction_deg"
            ),
            "latest_mean_wind_time": latest_sample.get("time"),

            "mean_wind_24h_max_kmh": mean24,
            "mean_wind_24h_direction_deg": mean24dir,
            "mean_wind_24h_time": mean24time,

            "mean_wind_72h_max_kmh": mean72,
            "mean_wind_72h_direction_deg": mean72dir,
            "mean_wind_72h_time": mean72time,
        })

    stations.sort(
        key=lambda st: (
            -(
                fnum(st.get("latest_gust_kmh"))
                if fnum(st.get("latest_gust_kmh")) is not None
                else -1
            ),
            st["name"],
        )
    )

    def vals(field: str) -> List[float]:
        out = []

        for st in stations:
            value = fnum(st.get(field))

            if value is not None:
                out.append(value)

        return out

    def vmax(field: str) -> Optional[float]:
        values = vals(field)
        return round(max(values), 1) if values else None

    def count(field: str) -> int:
        return len(vals(field))

    metric_labels = {
        "latest_gust_kmh": "Dernières rafales",
        "gust_24h_kmh": "Rafales sur 24 h",
        "gust_72h_kmh": "Rafales sur 72 h",
        "latest_mean_wind_kmh": "Vent moyen actuel",
        "mean_wind_24h_max_kmh": "Vent moyen max sur 24 h",
        "mean_wind_72h_max_kmh": "Vent moyen max sur 72 h",
    }

    metrics = {}

    for field, label in metric_labels.items():
        metrics[field] = {
            "label": label,
            "max": vmax(field),
            "stations": count(field),
        }

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "status": "ok",

        "generated_at": iso(utcnow()),
        "latest_observation_at": iso(latest_hour),

        "title": "Vent et rafales",
        "unit": "km/h",

        "coverage": {
            "mode": "cache_glissant_72h",
            "api_packages_this_run": 1,
            "samples_cached": len(cache["samples"]),
            "cache_history_hours": HISTORY_HOURS,
            "first_run_note": (
                "Lors du premier lancement, les vues 24 h / 72 h "
                "se remplissent progressivement. Après 24 h puis 72 h, "
                "elles disposent de la fenêtre complète."
            ),
        },

        "metrics": metrics,

        "stations_total": len(stations),
        "stations_excluded_sapc": excluded_sapc,

        "source": {
            "provider": "Météo-France",
            "api": "Package Observations V2",
            "gust": "raf / FXI3S / FXI / FXY",
            "mean_wind": "ff",
            "direction_mean_wind": "dd",
            "speed_conversion": "m/s × 3.6 = km/h",
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

    for field in metric_labels:
        print(
            field,
            ":",
            metrics[field]["stations"],
            "station(s) ; max",
            metrics[field]["max"],
            "km/h",
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

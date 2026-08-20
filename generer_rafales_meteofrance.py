#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Carte Vent & Rafales Météo-France
Version 1.1.1

Vues :
- Dernières rafales
- Rafales max sur 24 h
- Rafales max sur 72 h
- Vent moyen actuel
- Vent moyen max sur 24 h
- Vent moyen max sur 72 h

Rafales :
- priorité raf / ddraf
- puis FXI3S / DXI3S
- puis FXI / DXI
- puis FXY / DXY

Vent moyen :
- ff pour la vitesse
- dd pour la direction

Toutes les vitesses sont converties de m/s vers km/h.
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


VERSION = "1.1.1"
SCHEMA_VERSION = 2

PACKAGE_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)

STATIONS_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPObs/v2/liste-stations"
)

OUTPUT = Path("observations_rafales.json")

HTTP_TIMEOUT = 90
REQUEST_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.30"))
LATEST_RETRIES_HOURS = 4
HISTORY_HOURS = 72

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
                value.strip() if isinstance(value, str) else value
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

    if not value:
        raise RuntimeError(f"Secret {name} vide.")

    return value


def headers(key: str) -> dict:
    return {
        "apikey": key,
        "accept": "*/*",
    }


def is_sapc(name: str) -> bool:
    words = re.sub(
        r"[^A-Z0-9]+",
        " ",
        str(name).upper(),
    ).split()
    return "SAPC" in words


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


def extract_gust(row: dict) -> Tuple[
    Optional[float],
    Optional[int],
    Optional[str]
]:
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
    row: dict
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
            wait = 12 + attempt * 8
            print(
                f"[WARN] HTTP 429 {iso(target)} ; "
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
        f"Quota API dépassé pour {iso(target)}."
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
        "Aucun paquet disponible entre H et H-3."
    )


def load_station_names(key: str) -> Dict[str, str]:
    print("Chargement liste-stations ...")

    response = session.get(
        STATIONS_URL,
        headers=headers(key),
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code != 200:
        print(
            "[WARN] liste-stations indisponible :",
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

    print("Noms récupérés :", len(names))
    return names


def load_history(
    key: str,
) -> Tuple[datetime, Dict[str, dict], int]:

    latest_hour, latest_response = find_latest_package(key)

    stations: Dict[str, dict] = defaultdict(
        lambda: {
            "lat": None,
            "lon": None,

            "latest_gust_kmh": None,
            "latest_direction_deg": None,
            "latest_gust_time": None,
            "latest_source_field": None,

            "gust_24h_kmh": None,
            "gust_24h_direction_deg": None,
            "gust_24h_time": None,

            "gust_72h_kmh": None,
            "gust_72h_direction_deg": None,
            "gust_72h_time": None,

            "latest_mean_wind_kmh": None,
            "latest_mean_wind_direction_deg": None,
            "latest_mean_wind_time": None,

            "mean_wind_24h_max_kmh": None,
            "mean_wind_24h_direction_deg": None,
            "mean_wind_24h_time": None,

            "mean_wind_72h_max_kmh": None,
            "mean_wind_72h_direction_deg": None,
            "mean_wind_72h_time": None,
        }
    )

    loaded = 0

    for offset in range(HISTORY_HOURS):
        hour = latest_hour - timedelta(hours=offset)

        if offset == 0:
            response = latest_response
        else:
            response = package_request(key, hour)
            time.sleep(REQUEST_DELAY)

        if response is None:
            print("[INFO] Paquet indisponible :", iso(hour))
            continue

        loaded += 1
        rows = parse_csv(response.content)

        print(
            f"Paquet {iso(hour)} : "
            f"{len(rows)} ligne(s)"
        )

        for row in rows:
            sid = station_id(row)
            if not sid:
                continue

            item = stations[sid]

            lat = fnum(first(row, ("lat", "LAT", "latitude")))
            lon = fnum(first(row, ("lon", "LON", "longitude")))

            if (
                lat is not None
                and lon is not None
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                item["lat"] = round(lat, 6)
                item["lon"] = round(lon, 6)

            validity = parse_iso(
                first(
                    row,
                    (
                        "validity_time",
                        "reference_time",
                        "date",
                    ),
                )
            ) or hour

            # RAFALe
            gust, gust_dir, gust_source = extract_gust(row)

            if gust is not None:
                if offset == 0:
                    item["latest_gust_kmh"] = gust
                    item["latest_direction_deg"] = gust_dir
                    item["latest_gust_time"] = iso(validity)
                    item["latest_source_field"] = gust_source

                if offset < 24:
                    if (
                        item["gust_24h_kmh"] is None
                        or gust > item["gust_24h_kmh"]
                    ):
                        item["gust_24h_kmh"] = gust
                        item["gust_24h_direction_deg"] = gust_dir
                        item["gust_24h_time"] = iso(validity)

                if (
                    item["gust_72h_kmh"] is None
                    or gust > item["gust_72h_kmh"]
                ):
                    item["gust_72h_kmh"] = gust
                    item["gust_72h_direction_deg"] = gust_dir
                    item["gust_72h_time"] = iso(validity)

            # VENT MOYEN
            mean_wind, mean_dir = extract_mean_wind(row)

            if mean_wind is not None:
                if offset == 0:
                    item["latest_mean_wind_kmh"] = mean_wind
                    item["latest_mean_wind_direction_deg"] = mean_dir
                    item["latest_mean_wind_time"] = iso(validity)

                if offset < 24:
                    if (
                        item["mean_wind_24h_max_kmh"] is None
                        or mean_wind > item["mean_wind_24h_max_kmh"]
                    ):
                        item["mean_wind_24h_max_kmh"] = mean_wind
                        item["mean_wind_24h_direction_deg"] = mean_dir
                        item["mean_wind_24h_time"] = iso(validity)

                if (
                    item["mean_wind_72h_max_kmh"] is None
                    or mean_wind > item["mean_wind_72h_max_kmh"]
                ):
                    item["mean_wind_72h_max_kmh"] = mean_wind
                    item["mean_wind_72h_direction_deg"] = mean_dir
                    item["mean_wind_72h_time"] = iso(validity)

    return latest_hour, dict(stations), loaded


def main() -> int:
    print(
        f"=== Carte Vent & Rafales Météo-France v{VERSION} ==="
    )

    package_key = get_secret("METEOFRANCE_PACKAGE_OBS_KEY")
    obs_key = get_secret("METEOFRANCE_OBS_TOKEN")

    latest_hour, data, packages_loaded = load_history(
        package_key
    )

    names = load_station_names(obs_key)

    stations = []
    excluded_sapc = 0

    for sid, item in data.items():
        if item.get("lat") is None or item.get("lon") is None:
            continue

        name = names.get(sid, sid)

        if is_sapc(name):
            excluded_sapc += 1
            continue

        stations.append({
            "id": sid,
            "name": name,
            "lat": item["lat"],
            "lon": item["lon"],

            "latest_gust_kmh": item["latest_gust_kmh"],
            "latest_direction_deg": item["latest_direction_deg"],
            "latest_gust_time": item["latest_gust_time"],

            "gust_24h_kmh": item["gust_24h_kmh"],
            "gust_24h_direction_deg": item["gust_24h_direction_deg"],
            "gust_24h_time": item["gust_24h_time"],

            "gust_72h_kmh": item["gust_72h_kmh"],
            "gust_72h_direction_deg": item["gust_72h_direction_deg"],
            "gust_72h_time": item["gust_72h_time"],

            "latest_mean_wind_kmh": item["latest_mean_wind_kmh"],
            "latest_mean_wind_direction_deg": item["latest_mean_wind_direction_deg"],
            "latest_mean_wind_time": item["latest_mean_wind_time"],

            "mean_wind_24h_max_kmh": item["mean_wind_24h_max_kmh"],
            "mean_wind_24h_direction_deg": item["mean_wind_24h_direction_deg"],
            "mean_wind_24h_time": item["mean_wind_24h_time"],

            "mean_wind_72h_max_kmh": item["mean_wind_72h_max_kmh"],
            "mean_wind_72h_direction_deg": item["mean_wind_72h_direction_deg"],
            "mean_wind_72h_time": item["mean_wind_72h_time"],
        })

    stations.sort(
        key=lambda st: (
            -(
                st["latest_gust_kmh"]
                if st["latest_gust_kmh"] is not None
                else -1
            ),
            st["name"],
        )
    )

    def vals(field: str) -> List[float]:
        result = []
        for st in stations:
            value = fnum(st.get(field))
            if value is not None:
                result.append(value)
        return result

    def vmax(field: str) -> Optional[float]:
        values = vals(field)
        return round(max(values), 1) if values else None

    def count(field: str) -> int:
        return len(vals(field))

    metric_fields = {
        "latest_gust_kmh": "Dernières rafales",
        "gust_24h_kmh": "Rafales sur 24 h",
        "gust_72h_kmh": "Rafales sur 72 h",
        "latest_mean_wind_kmh": "Vent moyen actuel",
        "mean_wind_24h_max_kmh": "Vent moyen max sur 24 h",
        "mean_wind_72h_max_kmh": "Vent moyen max sur 72 h",
    }

    metrics = {}

    for field, label in metric_fields.items():
        metrics[field] = {
            "label": label,
            "max": vmax(field),
            "stations": count(field),
        }

    metrics["latest_gust_kmh"]["long_label"] = (
        "Rafale maximale du dernier relevé horaire"
    )
    metrics["gust_24h_kmh"]["long_label"] = (
        "Rafale maximale observée sur les 24 dernières heures"
    )
    metrics["gust_72h_kmh"]["long_label"] = (
        "Rafale maximale observée sur les 72 dernières heures"
    )
    metrics["latest_mean_wind_kmh"]["long_label"] = (
        "Vent moyen observé au dernier relevé horaire"
    )
    metrics["mean_wind_24h_max_kmh"]["long_label"] = (
        "Vent moyen maximal observé sur les 24 dernières heures"
    )
    metrics["mean_wind_72h_max_kmh"]["long_label"] = (
        "Vent moyen maximal observé sur les 72 dernières heures"
    )

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": "vent-rafales-6-metrics-20260820",
        "status": "ok",

        "generated_at": iso(utcnow()),
        "latest_observation_at": iso(latest_hour),

        "title": "Vent et rafales",
        "unit": "km/h",

        "coverage": {
            "packages_requested": HISTORY_HOURS,
            "packages_loaded": packages_loaded,
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
    print("=== TERMINÉ ===")
    print("Module :", VERSION)
    print("Build : vent-rafales-6-metrics-20260820")
    print("Stations :", len(stations))
    print("Paquets :", packages_loaded, "/", HISTORY_HOURS)

    for field in metric_fields:
        print(
            field,
            ":",
            metrics[field]["stations"],
            "stations ; max",
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

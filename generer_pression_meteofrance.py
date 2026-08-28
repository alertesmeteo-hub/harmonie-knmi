#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Pression atmosphérique Météo-France
Version 1.0.0

Même principe que generer_rafales_meteofrance.py : un seul paquet horaire
Météo-France est téléchargé à chaque run, un cache glissant conserve les
observations des 72 dernières heures, et les variations de pression sur
3 h / 12 h / 24 h sont calculées depuis ce cache (valeur la plus proche de
l'échéance visée, à 40 minutes près).

Champs Package Observations utilisés (confirmés par tests/test_generateur_observations.py
de ce même dépôt) : pmer (pression mer réduite, Pa) et pres (pression station, Pa).
"""

from __future__ import annotations

import csv
import gzip
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


VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "pression-api-mf-cache-72h-20260828"

PACKAGE_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)

STATIONS_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPObs/v2/liste-stations"
)

OUTPUT = Path("observations_pression.json")
CACHE = Path("cache_pression_72h.json")

HTTP_TIMEOUT = 90
LATEST_RETRIES_HOURS = 4
REQUEST_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.50"))

HISTORY_HOURS = 72
CACHE_MARGIN_HOURS = 3
VARIATION_TOLERANCE_MINUTES = 40

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-pression/{VERSION}",
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


def department_code(sid: str) -> Optional[str]:
    digits = re.sub(r"\D", "", str(sid))

    if len(digits) < 2:
        return None

    code = digits[:2]

    if code == "20":
        return "20"

    try:
        number = int(code)
    except ValueError:
        return None

    return code if 1 <= number <= 95 else None


def parse_csv(raw: bytes) -> List[dict]:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

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


def hpa(value_pa: Any) -> Optional[float]:
    # Les paquets Météo-France livrent pmer/pres en pascals (ex. 101325).
    # On ne suppose pas systématiquement le pascal : si la valeur brute
    # ressemble déjà à des hPa (plage plausible), on la garde telle quelle,
    # pour rester robuste à un éventuel changement de format côté API.
    value = fnum(value_pa)

    if value is None:
        return None

    if value > 2000:
        value = value / 100.0

    if value < 800 or value > 1100:
        return None

    return round(value, 1)


def extract_pressure(row: dict) -> Tuple[Optional[float], Optional[float]]:
    pmer = hpa(first(row, ("pmer", "PMER")))
    pres = hpa(first(row, ("pres", "PRES")))
    return pmer, pres


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
        try:
            response = session.get(
                PACKAGE_URL,
                params={
                    "date": iso(target),
                    "format": "csv",
                },
                headers=headers(key),
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt == 2:
                raise RuntimeError(
                    f"Erreur réseau Météo-France pour {iso(target)} : {exc}"
                ) from exc
            time.sleep(5 + attempt * 5)
            continue

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


def load_station_meta(
    key: str,
) -> Dict[str, dict]:

    try:
        response = session.get(
            STATIONS_URL,
            headers=headers(key),
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        print("[WARN] liste-stations indisponible :", exc)
        return {}

    if response.status_code != 200:
        print(
            "[WARN] liste-stations HTTP",
            response.status_code,
       ")
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

    stations = {}

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

        if not name:
            name = sid

        altitude = fnum(first(row, ("altitude", "ALTITUDE", "alti", "ALTI")))

        stations[sid] = {
            "name": str(name).strip(),
            "department_code": department_code(sid),
            "altitude_m": round(altitude, 1) if altitude is not None else None,
        }

    return stations


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

    cutoff = latest_hour - timedelta(
        hours=HISTORY_HOURS + CACHE_MARGIN_HOURS
    )

    cleaned = []

    for sample in cache.get("samples", []):
        dt = parse_iso(sample.get("time"))

        if dt is None or dt < cutoff:
            continue

        cleaned.append(sample)

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

        pmer, pres = extract_pressure(row)

        if pmer is None and pres is None:
            continue

        cache["samples"].append({
            "id": sid,
            "time": iso(validity),
            "lat": round(lat, 6),
            "lon": round(lon, 6),

            "pressure_msl_hpa": pmer,
            "pressure_station_hpa": pres,
        })

        added += 1

    return added


def closest_sample_to(
    samples: List[dict],
    target: datetime,
    tolerance_minutes: int,
) -> Optional[dict]:

    best = None
    best_delta = None

    for sample in samples:
        dt = parse_iso(sample.get("time"))

        if dt is None:
            continue

        delta = abs((dt - target).total_seconds()) / 60.0

        if delta > tolerance_minutes:
            continue

        if best_delta is None or delta < best_delta:
            best = sample
            best_delta = delta

    return best


def main() -> int:
    print(
        f"=== Pression atmosphérique Météo-France v{VERSION} ==="
    )
    print("Build :", BUILD_ID)

    package_key = get_secret(
        "METEOFRANCE_PACKAGE_OBS_KEY"
    )
    try:
        obs_key = get_secret(
            "METEOFRANCE_OBS_TOKEN"
        )
    except RuntimeError:
        print(
            "[WARN] METEOFRANCE_OBS_TOKEN absent : "
            "utilisation de METEOFRANCE_PACKAGE_OBS_KEY pour liste-stations."
        )
        obs_key = package_key

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

    station_meta = load_station_meta(obs_key)

    by_station: Dict[str, List[dict]] = {}

    for sample in cache["samples"]:
        sid = str(sample.get("id") or "").strip()

        if not sid:
            continue

        by_station.setdefault(sid, []).append(sample)

    stations = []

    for sid, samples in by_station.items():
        samples.sort(
            key=lambda s: parse_iso(s.get("time"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )

        info = station_meta.get(sid) or {}
        name = info.get("name") or sid

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

        latest_sample = samples[-1]
        current_sample = latest_candidates[-1] if latest_candidates else {}

        # Uniquement la pression mer (pmer) : la pression station brute d'un
        # poste d'altitude (ex. Mende 932 m -> ~913 hPa, Embrun 873 m ->
        # ~917 hPa) n'est pas comparable à une pression mer et créait de
        # fausses dépressions extrêmes sur la carte si on la substituait.
        # Un poste sans pmer est donc exclu de cette carte (sa pression
        # station reste disponible séparément dans pressure_station_hpa).
        current_pressure = current_sample.get("pressure_msl_hpa")

        if current_pressure is None:
            continue

        var3 = var12 = var24 = None

        for hours_back, field in ((3, "var3"), (12, "var12"), (24, "var24")):
            target = latest_hour - timedelta(hours=hours_back)
            past = closest_sample_to(samples, target, VARIATION_TOLERANCE_MINUTES)

            if past is None:
                continue

            past_pressure = past.get("pressure_msl_hpa")

            if past_pressure is None:
                continue

            delta = round(current_pressure - past_pressure, 1)

            if field == "var3":
                var3 = delta
            elif field == "var12":
                var12 = delta
            else:
                var24 = delta

        stations.append({
            "id": sid,
            "name": name,
            "department_code": info.get("department_code") or department_code(sid),
            "altitude_m": info.get("altitude_m"),
            "lat": latest_sample.get("lat"),
            "lon": latest_sample.get("lon"),

            "pressure": current_pressure,
            "pressure_station_hpa": current_sample.get("pressure_station_hpa"),
            "time": current_sample.get("time"),

            "var3": var3,
            "var12": var12,
            "var24": var24,
        })

    stations.sort(key=lambda st: st["name"])

    def vals(field: str) -> List[float]:
        out = []

        for st in stations:
            value = fnum(st.get(field))

            if value is not None:
                out.append(value)

        return out

    metrics = {}

    for field, label in (
        ("pressure", "Pression atmosphérique"),
        ("var3", "Variation 3 h"),
        ("var12", "Variation 12 h"),
        ("var24", "Variation 24 h"),
    ):
        values = vals(field)
        metrics[field] = {
            "label": label,
            "min": round(min(values), 1) if values else None,
            "max": round(max(values), 1) if values else None,
            "stations": len(values),
        }

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "status": "ok",

        "generated_at": iso(utcnow()),
        "latest_observation_at": iso(latest_hour),

        "title": "Pression atmosphérique",
        "unit": "hPa",

        "coverage": {
            "mode": "cache_glissant_72h",
            "api_packages_this_run": 1,
            "samples_cached": len(cache["samples"]),
            "cache_history_hours": HISTORY_HOURS,
            "first_run_note": (
                "Lors du premier lancement, les variations 3 h / 12 h / 24 h "
                "se remplissent progressivement au fil des runs suivants."
            ),
        },

        "metrics": metrics,

        "stations_total": len(stations),

        "source": {
            "provider": "Météo-France",
            "api": "Package Observations V2",
            "pressure_msl": "pmer",
            "pressure_station": "pres",
            "unit_conversion": "Pa / 100 = hPa",
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

    for field, item in metrics.items():
        print(
            field,
            ":",
            item["stations"],
            "station(s) ; min",
            item["min"],
            "; max",
            item["max"],
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

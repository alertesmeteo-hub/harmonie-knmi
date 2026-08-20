#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Météo Climat Pro — Carte Températures & Records
Version 1.1.0

Ajouts v1.1.0
-------------
- Humidex
- Ressenti au vent / refroidissement éolien
- Variation de température sur 24 h
- Température minimale sur 12 h
- Température minimale sur 24 h
- Température maximale sur 12 h
- Température maximale sur 24 h

Le module conserve aussi :
- Température maximale du jour
- Record du mois
- Record du mois battu / égalé ?
- Record absolu
- Record absolu battu / égalé ?

Sources :
- Package Observations V2 : t, td, ff, tx, tn
- DPObs V2 /liste-stations : noms des stations
- Données climatologiques mensuelles Météo-France : TXAB / TXDAT

Secrets :
- METEOFRANCE_PACKAGE_OBS_KEY
- METEOFRANCE_OBS_TOKEN

Les API Keys sont envoyées dans l'en-tête "apikey".
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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


VERSION = "1.1.0"
SCHEMA_VERSION = 2

PACKAGE_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)

DPOBS_STATIONS_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPObs/v2/liste-stations"
)

MF_S3_MENS = (
    "https://meteofrance.s3.sbg.io.cloud.ovh.net/"
    "data/synchro_ftp/BASE/MENS"
)

OUTPUT = Path("observations_temperature.json")
CACHE = Path("cache_temperature_records.json")

HTTP_TIMEOUT = 90
PACKAGE_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.25"))
PACKAGE_RETRIES_HOURS = 4

# 25 paquets : H ... H-24 pour calculer la variation exacte à 24 h.
OBS_HISTORY_HOURS = 25

HISTORICAL_CACHE_DAYS = 35
CURRENT_MONTH_REFRESH_HOURS = 6
RECORD_EPSILON = 0.05

EXCLUDE_SAPC = True

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-temperature-records/{VERSION}",
})


# ---------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
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
        result = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def celsius(value: Any) -> Optional[float]:
    value = fnum(value)
    if value is None:
        return None

    # Package Observations : températures généralement en kelvins.
    if value > 100:
        value -= 273.15

    if value < -100 or value > 70:
        return None

    return round(value, 1)


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
            "id_station",
            "numer_sta",
        ),
    )
    if value is None:
        return None

    sid = str(value).strip()
    if sid.endswith(".0") and sid[:-2].isdigit():
        sid = sid[:-2]

    return sid or None


def parse_csv(raw: bytes) -> List[dict]:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    text = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:10000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","

    rows = []
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        clean = {}
        for key, value in row.items():
            if key is None:
                continue
            clean[str(key).strip()] = (
                value.strip() if isinstance(value, str) else value
            )
        rows.append(clean)

    return rows


def get_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Secret GitHub absent : {name}")

    value = value.replace("\r", "").replace("\n", "").strip()

    for prefix in ("apikey:", "apiKey:", "Bearer ", "bearer "):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()

    if not value:
        raise RuntimeError(f"Secret {name} vide après nettoyage.")

    return value


def api_headers(key: str) -> dict:
    return {
        "apikey": key,
        "accept": "*/*",
    }


def is_sapc(name: str) -> bool:
    if not EXCLUDE_SAPC:
        return False

    words = re.sub(
        r"[^A-Z0-9]+",
        " ",
        str(name).upper(),
    ).split()

    return "SAPC" in words


def quality_ok(value: Any) -> bool:
    if value in (None, ""):
        return True

    try:
        q = int(float(str(value).replace(",", ".")))
    except Exception:
        return True

    return q != 2


def month_day_date(
    aaaamm: Any,
    day_value: Any,
) -> Optional[str]:

    ym = re.sub(r"\D", "", str(aaaamm or ""))
    if len(ym) < 6:
        return None
    ym = ym[:6]

    d = re.sub(r"\D", "", str(day_value or ""))
    if not d:
        return None

    if len(d) >= 8:
        candidate = d[:8]
    else:
        try:
            day = int(d)
        except ValueError:
            return None
        candidate = f"{ym}{day:02d}"

    try:
        dt = datetime.strptime(candidate, "%Y%m%d")
    except ValueError:
        return None

    return dt.strftime("%Y%m%d")


def ym_int(value: Any) -> Optional[int]:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 6:
        return None

    try:
        return int(digits[:6])
    except ValueError:
        return None


def update_record(
    current_value: Optional[float],
    current_date: Optional[str],
    candidate_value: Optional[float],
    candidate_date: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:

    if candidate_value is None:
        return current_value, current_date

    if current_value is None or candidate_value > current_value + RECORD_EPSILON:
        return round(candidate_value, 1), candidate_date

    if abs(candidate_value - current_value) <= RECORD_EPSILON:
        if (
            candidate_date
            and (
                not current_date
                or candidate_date < current_date
            )
        ):
            return current_value, candidate_date

    return current_value, current_date


# ---------------------------------------------------------------------
# Humidex et refroidissement éolien
# ---------------------------------------------------------------------

def humidex_value(
    temperature_c: Optional[float],
    dewpoint_c: Optional[float],
) -> Optional[float]:
    """
    Formule standard d'Environnement et Changement climatique Canada.

    L'indice est affiché pour les conditions actuelles si :
    - T >= 20 °C
    - Humidex >= T + 1
    """
    if temperature_c is None or dewpoint_c is None:
        return None

    if temperature_c < 20.0:
        return None

    dewpoint_k = dewpoint_c + 273.15
    if dewpoint_k <= 0:
        return None

    e = 6.11 * math.exp(
        5417.7530 * (
            (1.0 / 273.15)
            - (1.0 / dewpoint_k)
        )
    )

    humidex = temperature_c + 0.5555 * (e - 10.0)

    if humidex < temperature_c + 1.0:
        return None

    return round(humidex, 1)


def wind_chill_value(
    temperature_c: Optional[float],
    wind_ms: Optional[float],
) -> Optional[float]:
    """
    Refroidissement éolien standard.
    ff est converti de m/s vers km/h.

    Le module ne publie l'indice que lorsque T <= 10 °C
    et V >= 5 km/h.
    """
    if temperature_c is None or wind_ms is None:
        return None

    wind_kmh = wind_ms * 3.6

    if temperature_c > 10.0 or wind_kmh < 5.0:
        return None

    power = wind_kmh ** 0.16

    value = (
        13.12
        + 0.6215 * temperature_c
        - 11.37 * power
        + 0.3965 * temperature_c * power
    )

    return round(value, 1)


# ---------------------------------------------------------------------
# Package Observations V2
# ---------------------------------------------------------------------

def package_request(
    key: str,
    hour: datetime,
) -> Optional[requests.Response]:

    request_hour = hour.replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    response = session.get(
        PACKAGE_URL,
        params={
            "date": iso(request_hour),
            "format": "csv",
        },
        headers=api_headers(key),
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code == 200:
        return response

    if response.status_code in (400, 404):
        return None

    if response.status_code == 401:
        raise RuntimeError(
            "Package Observations : HTTP 401, clé invalide."
        )

    if response.status_code == 403:
        raise RuntimeError(
            "Package Observations : HTTP 403, droits insuffisants."
        )

    if response.status_code == 429:
        raise RuntimeError(
            "Package Observations : HTTP 429, quota dépassé."
        )

    response.raise_for_status()
    return None


def find_latest_package(
    key: str,
) -> Tuple[datetime, requests.Response]:

    base = utcnow().replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    for back in range(PACKAGE_RETRIES_HOURS):
        hour = base - timedelta(hours=back)

        print("Recherche paquet :", iso(hour))

        response = package_request(key, hour)

        if response is not None:
            print("Dernier paquet disponible :", iso(hour))
            return hour, response

        time.sleep(PACKAGE_DELAY)

    raise RuntimeError(
        "Aucun paquet horaire disponible entre H et H-3."
    )


def load_live_metrics(
    key: str,
) -> Tuple[datetime, Dict[str, dict]]:

    latest_hour, latest_response = find_latest_package(key)

    stations: Dict[str, dict] = defaultdict(
        lambda: {
            "lat": None,
            "lon": None,

            "date": None,
            "temperature": None,
            "dewpoint": None,
            "humidity": None,
            "wind_ms": None,

            "tx_today": None,
            "tx_today_time": None,

            "temp_24h_ago": None,

            "min_12h": None,
            "min_12h_time": None,
            "min_24h": None,
            "min_24h_time": None,

            "max_12h": None,
            "max_12h_time": None,
            "max_24h": None,
            "max_24h_time": None,

            "hours_12h": 0,
            "hours_24h": 0,
        }
    )

    current_day = latest_hour.date()

    for offset in range(OBS_HISTORY_HOURS):
        target = latest_hour - timedelta(hours=offset)

        if offset == 0:
            response = latest_response
        else:
            response = package_request(key, target)
            time.sleep(PACKAGE_DELAY)

        if response is None:
            print("[INFO] Paquet absent :", iso(target))
            continue

        rows = parse_csv(response.content)
        print(f"Paquet {iso(target)} : {len(rows)} lignes")

        for row in rows:
            sid = station_id(row)
            if not sid:
                continue

            validity = parse_iso(
                first(
                    row,
                    ("validity_time", "reference_time", "date"),
                )
            ) or target

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

            t = celsius(first(row, ("t", "T")))
            td = celsius(first(row, ("td", "TD")))
            rh = fnum(first(row, ("u", "U")))
            ff = fnum(first(row, ("ff", "FF")))

            tx = celsius(first(row, ("tx", "TX")))
            tn = celsius(first(row, ("tn", "TN")))

            # Les extrêmes horaires sont privilégiés ; sinon T sert de filet.
            max_candidate = tx if tx is not None else t
            min_candidate = tn if tn is not None else t

            # Relevé courant = paquet H.
            if offset == 0:
                item["date"] = iso(validity)
                item["temperature"] = t
                item["dewpoint"] = td
                item["humidity"] = (
                    round(rh, 1)
                    if rh is not None and 0 <= rh <= 100
                    else None
                )
                item["wind_ms"] = (
                    round(ff, 2)
                    if ff is not None and ff >= 0
                    else None
                )

            # Variation à 24 h = T(H) - T(H-24)
            if offset == 24 and t is not None:
                item["temp_24h_ago"] = t

            # Minimum/maximum sur 12 h :
            # H à H-11 = 12 paquets.
            if offset < 12:
                if min_candidate is not None:
                    item["hours_12h"] += 1
                    if (
                        item["min_12h"] is None
                        or min_candidate < item["min_12h"]
                    ):
                        item["min_12h"] = min_candidate
                        item["min_12h_time"] = iso(validity)

                if max_candidate is not None:
                    if (
                        item["max_12h"] is None
                        or max_candidate > item["max_12h"]
                    ):
                        item["max_12h"] = max_candidate
                        item["max_12h_time"] = iso(validity)

            # Minimum/maximum sur 24 h :
            # H à H-23 = 24 paquets.
            if offset < 24:
                if min_candidate is not None:
                    item["hours_24h"] += 1
                    if (
                        item["min_24h"] is None
                        or min_candidate < item["min_24h"]
                    ):
                        item["min_24h"] = min_candidate
                        item["min_24h_time"] = iso(validity)

                if max_candidate is not None:
                    if (
                        item["max_24h"] is None
                        or max_candidate > item["max_24h"]
                    ):
                        item["max_24h"] = max_candidate
                        item["max_24h_time"] = iso(validity)

            # Max du jour calendaire UTC, comme dans la v1.0.
            if validity.date() == current_day and max_candidate is not None:
                if (
                    item["tx_today"] is None
                    or max_candidate > item["tx_today"]
                ):
                    item["tx_today"] = max_candidate
                    item["tx_today_time"] = iso(validity)

    # Calculs dérivés.
    for item in stations.values():
        current = fnum(item.get("temperature"))
        old = fnum(item.get("temp_24h_ago"))

        item["variation_24h"] = (
            round(current - old, 1)
            if current is not None and old is not None
            else None
        )

        item["humidex"] = humidex_value(
            current,
            fnum(item.get("dewpoint")),
        )

        item["wind_chill"] = wind_chill_value(
            current,
            fnum(item.get("wind_ms")),
        )

        if item["min_12h"] is not None:
            item["min_12h"] = round(item["min_12h"], 1)
        if item["min_24h"] is not None:
            item["min_24h"] = round(item["min_24h"], 1)
        if item["max_12h"] is not None:
            item["max_12h"] = round(item["max_12h"], 1)
        if item["max_24h"] is not None:
            item["max_24h"] = round(item["max_24h"], 1)

    return latest_hour, dict(stations)


# ---------------------------------------------------------------------
# DPObs V2 : noms
# ---------------------------------------------------------------------

def load_station_names(
    key: str,
) -> Dict[str, str]:

    print("Chargement /DPObs/v2/liste-stations ...")

    response = session.get(
        DPOBS_STATIONS_URL,
        headers=api_headers(key),
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code != 200:
        print(
            f"[WARN] liste-stations HTTP {response.status_code}; "
            "les identifiants seront utilisés comme noms."
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

    print(f"Noms récupérés : {len(names)} station(s).")
    return names


# ---------------------------------------------------------------------
# Climatologie records
# ---------------------------------------------------------------------

def department_candidates(
    sid: str,
) -> List[str]:

    digits = re.sub(r"\D", "", sid)

    for code in (
        "971", "972", "973", "974", "975",
        "976", "977", "978", "984",
        "986", "987", "988",
    ):
        if digits.startswith(code):
            return [code]

    if digits.startswith("20"):
        return ["2A", "2B", "20"]

    return [digits[:2]] if len(digits) >= 2 else []


def monthly_urls(
    dep: str,
    now: datetime,
) -> List[str]:

    return [
        f"{MF_S3_MENS}/MENSQ_{dep}_avant-1949.csv.gz",
        (
            f"{MF_S3_MENS}/MENSQ_{dep}_previous-"
            f"1950-{now.year - 2}.csv.gz"
        ),
        (
            f"{MF_S3_MENS}/MENSQ_{dep}_latest-"
            f"{now.year - 1}-{now.year}.csv.gz"
        ),
    ]


def latest_monthly_url(
    dep: str,
    now: datetime,
) -> str:

    return (
        f"{MF_S3_MENS}/MENSQ_{dep}_latest-"
        f"{now.year - 1}-{now.year}.csv.gz"
    )


def download_rows(
    url: str,
) -> Optional[List[dict]]:

    try:
        response = session.get(
            url,
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:
        print(f"[WARN] téléchargement impossible {url}: {exc}")
        return None

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        print(f"[WARN] HTTP {response.status_code}: {url}")
        return None

    try:
        return parse_csv(response.content)
    except Exception as exc:
        print(f"[WARN] fichier illisible {url}: {exc}")
        return None


# ---------------------------------------------------------------------
# Cache records
# ---------------------------------------------------------------------

def load_cache() -> dict:
    if not CACHE.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "stations": {},
        }

    try:
        data = json.loads(
            CACHE.read_text(encoding="utf-8")
        )

        if not isinstance(data, dict):
            raise ValueError("cache non objet")

        data.setdefault("stations", {})
        return data

    except Exception as exc:
        print(f"[WARN] cache ignoré : {exc}")
        return {
            "schema_version": SCHEMA_VERSION,
            "stations": {},
        }


def cache_dt(
    cache: dict,
    key: str,
) -> Optional[datetime]:
    return parse_iso(cache.get(key))


def age_hours(
    dt: Optional[datetime],
) -> float:
    if dt is None:
        return 999999.0
    return (utcnow() - dt).total_seconds() / 3600.0


def build_historical_records(
    cache: dict,
    station_ids: List[str],
    now: datetime,
) -> None:

    current_ym = now.year * 100 + now.month
    month_id = f"{now.year:04d}-{now.month:02d}"

    stations_cache = cache.setdefault("stations", {})

    known = sum(
        1
        for sid in station_ids
        if stations_cache.get(sid, {}).get("record_absolute_old") is not None
    )

    coverage = known / max(1, len(station_ids))

    stale = (
        cache.get("record_baseline_month_id") != month_id
        or age_hours(cache_dt(cache, "records_generated_at"))
        >= HISTORICAL_CACHE_DAYS * 24
        or coverage < 0.75
    )

    if not stale:
        print("Cache historique des records encore valide.")
        return

    print("Construction des records historiques...")

    wanted_by_dep: Dict[str, set] = defaultdict(set)

    for sid in station_ids:
        for dep in department_candidates(sid):
            wanted_by_dep[dep].add(sid)

    month_record_value = {sid: None for sid in station_ids}
    month_record_date = {sid: None for sid in station_ids}

    abs_record_value = {sid: None for sid in station_ids}
    abs_record_date = {sid: None for sid in station_ids}

    seen_rows = set()

    for dep, wanted in sorted(wanted_by_dep.items()):
        for url in monthly_urls(dep, now):
            rows = download_rows(url)

            if rows is None:
                continue

            print(
                f"Records {dep} / {Path(url).name}: "
                f"{len(rows)} lignes"
            )

            for row in rows:
                sid = station_id(row)
                if sid not in wanted:
                    continue

                ym = ym_int(first(row, ("AAAAMM", "DATE", "date")))
                if ym is None:
                    continue

                if ym >= current_ym:
                    continue

                txab = fnum(first(row, ("TXAB", "txab")))
                if txab is None or txab < -100 or txab > 70:
                    continue

                if not quality_ok(first(row, ("QTXAB", "qtxab"))):
                    continue

                row_key = (sid, ym)
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)

                txdate = month_day_date(
                    ym,
                    first(row, ("TXDAT", "txdat")),
                )

                (
                    abs_record_value[sid],
                    abs_record_date[sid],
                ) = update_record(
                    abs_record_value[sid],
                    abs_record_date[sid],
                    txab,
                    txdate,
                )

                if ym % 100 == now.month:
                    (
                        month_record_value[sid],
                        month_record_date[sid],
                    ) = update_record(
                        month_record_value[sid],
                        month_record_date[sid],
                        txab,
                        txdate,
                    )

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})

        entry["record_month_old"] = month_record_value[sid]
        entry["record_month_old_date"] = month_record_date[sid]
        entry["record_absolute_old"] = abs_record_value[sid]
        entry["record_absolute_old_date"] = abs_record_date[sid]

    cache["record_baseline_month_id"] = month_id
    cache["records_generated_at"] = iso(utcnow())


def update_current_month_climate(
    cache: dict,
    station_ids: List[str],
    now: datetime,
) -> None:

    current_ym = now.year * 100 + now.month
    month_id = f"{now.year:04d}-{now.month:02d}"

    stale = (
        cache.get("current_month_id") != month_id
        or age_hours(cache_dt(cache, "current_month_generated_at"))
        >= CURRENT_MONTH_REFRESH_HOURS
    )

    if not stale:
        print("Cache TXAB du mois en cours encore valide.")
        return

    print("Actualisation TXAB du mois en cours...")

    wanted_by_dep: Dict[str, set] = defaultdict(set)

    for sid in station_ids:
        for dep in department_candidates(sid):
            wanted_by_dep[dep].add(sid)

    month_value = {sid: None for sid in station_ids}
    month_date = {sid: None for sid in station_ids}

    for dep, wanted in sorted(wanted_by_dep.items()):
        url = latest_monthly_url(dep, now)
        rows = download_rows(url)

        if rows is None:
            continue

        print(f"Mois courant {dep}: {len(rows)} lignes")

        for row in rows:
            sid = station_id(row)
            if sid not in wanted:
                continue

            ym = ym_int(first(row, ("AAAAMM", "DATE", "date")))
            if ym != current_ym:
                continue

            txab = fnum(first(row, ("TXAB", "txab")))
            if txab is None or txab < -100 or txab > 70:
                continue

            if not quality_ok(first(row, ("QTXAB", "qtxab"))):
                continue

            txdate = month_day_date(
                ym,
                first(row, ("TXDAT", "txdat")),
            )

            (
                month_value[sid],
                month_date[sid],
            ) = update_record(
                month_value[sid],
                month_date[sid],
                txab,
                txdate,
            )

    stations_cache = cache.setdefault("stations", {})

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})
        entry["current_month_txab"] = month_value[sid]
        entry["current_month_txab_date"] = month_date[sid]

    cache["current_month_id"] = month_id
    cache["current_month_generated_at"] = iso(utcnow())


# ---------------------------------------------------------------------
# Comparaison des records
# ---------------------------------------------------------------------

def record_status(
    current_value: Optional[float],
    old_record: Optional[float],
) -> str:

    if current_value is None or old_record is None:
        return "indisponible"

    if current_value > old_record + RECORD_EPSILON:
        return "battu"

    if abs(current_value - old_record) <= RECORD_EPSILON:
        return "egale"

    return "non"


def current_record(
    old_value: Optional[float],
    old_date: Optional[str],
    month_value: Optional[float],
    month_date: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:

    if month_value is None:
        return old_value, old_date

    if old_value is None:
        return month_value, month_date

    if month_value > old_value + RECORD_EPSILON:
        return month_value, month_date

    if abs(month_value - old_value) <= RECORD_EPSILON:
        return old_value, old_date

    return old_value, old_date


MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


# ---------------------------------------------------------------------
# Sortie
# ---------------------------------------------------------------------

def main() -> int:
    print(
        f"=== Carte Températures & Records v{VERSION} ==="
    )

    package_key = get_secret("METEOFRANCE_PACKAGE_OBS_KEY")
    obs_key = get_secret("METEOFRANCE_OBS_TOKEN")

    # 1. Observations + métriques 12/24 h
    latest_hour, live = load_live_metrics(package_key)

    # 2. Noms
    names = load_station_names(obs_key)

    raw_ids = sorted(
        sid
        for sid, data in live.items()
        if data.get("lat") is not None
        and data.get("lon") is not None
    )

    excluded_sapc = [
        sid
        for sid in raw_ids
        if is_sapc(names.get(sid, sid))
    ]
    excluded_set = set(excluded_sapc)

    station_ids = [
        sid
        for sid in raw_ids
        if sid not in excluded_set
    ]

    print(
        f"Stations live : {len(raw_ids)} | "
        f"SAPC exclues : {len(excluded_sapc)} | "
        f"retenues : {len(station_ids)}"
    )

    # 3. Records
    cache = load_cache()

    build_historical_records(
        cache,
        station_ids,
        latest_hour,
    )

    update_current_month_climate(
        cache,
        station_ids,
        latest_hour,
    )

    cache["schema_version"] = SCHEMA_VERSION
    cache["module_version"] = VERSION

    CACHE.write_text(
        json.dumps(
            cache,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    # 4. Fusion
    stations = []

    today_key = latest_hour.strftime("%Y%m%d")
    month_label = MONTHS_FR[latest_hour.month - 1]

    count_month_battu = 0
    count_month_egale = 0
    count_abs_battu = 0
    count_abs_egale = 0

    for sid in station_ids:
        obs = live[sid]
        clim = cache.get("stations", {}).get(sid, {})

        tx_today = fnum(obs.get("tx_today"))

        month_climate_value = fnum(
            clim.get("current_month_txab")
        )
        month_climate_date = clim.get(
            "current_month_txab_date"
        )

        current_month_value = month_climate_value
        current_month_date = month_climate_date

        if tx_today is not None:
            if (
                current_month_value is None
                or tx_today > current_month_value + RECORD_EPSILON
            ):
                current_month_value = round(tx_today, 1)
                current_month_date = today_key

        old_month = fnum(clim.get("record_month_old"))
        old_month_date = clim.get("record_month_old_date")

        old_abs = fnum(clim.get("record_absolute_old"))
        old_abs_date = clim.get("record_absolute_old_date")

        month_status = record_status(
            current_month_value,
            old_month,
        )
        abs_status = record_status(
            current_month_value,
            old_abs,
        )

        (
            record_month_value,
            record_month_date,
        ) = current_record(
            old_month,
            old_month_date,
            current_month_value,
            current_month_date,
        )

        (
            record_abs_value,
            record_abs_date,
        ) = current_record(
            old_abs,
            old_abs_date,
            current_month_value,
            current_month_date,
        )

        month_delta = (
            round(current_month_value - old_month, 1)
            if (
                current_month_value is not None
                and old_month is not None
                and month_status == "battu"
            )
            else 0.0 if month_status == "egale" else None
        )

        abs_delta = (
            round(current_month_value - old_abs, 1)
            if (
                current_month_value is not None
                and old_abs is not None
                and abs_status == "battu"
            )
            else 0.0 if abs_status == "egale" else None
        )

        if month_status == "battu":
            count_month_battu += 1
        elif month_status == "egale":
            count_month_egale += 1

        if abs_status == "battu":
            count_abs_battu += 1
        elif abs_status == "egale":
            count_abs_egale += 1

        wind_ms = fnum(obs.get("wind_ms"))

        stations.append({
            "id": sid,
            "name": names.get(sid, sid),
            "lat": obs["lat"],
            "lon": obs["lon"],
            "date": obs.get("date"),

            "temperature": obs.get("temperature"),
            "dewpoint": obs.get("dewpoint"),
            "humidity": obs.get("humidity"),
            "wind_ms": (
                round(wind_ms, 2)
                if wind_ms is not None
                else None
            ),
            "wind_kmh": (
                round(wind_ms * 3.6, 1)
                if wind_ms is not None
                else None
            ),

            "humidex": obs.get("humidex"),
            "wind_chill": obs.get("wind_chill"),

            "temp_24h_ago": obs.get("temp_24h_ago"),
            "variation_24h": obs.get("variation_24h"),

            "min_12h": obs.get("min_12h"),
            "min_12h_time": obs.get("min_12h_time"),
            "min_24h": obs.get("min_24h"),
            "min_24h_time": obs.get("min_24h_time"),

            "max_12h": obs.get("max_12h"),
            "max_12h_time": obs.get("max_12h_time"),
            "max_24h": obs.get("max_24h"),
            "max_24h_time": obs.get("max_24h_time"),

            "hours_12h": int(obs.get("hours_12h") or 0),
            "hours_24h": int(obs.get("hours_24h") or 0),

            "tx_today": (
                round(tx_today, 1)
                if tx_today is not None
                else None
            ),
            "tx_today_time": obs.get("tx_today_time"),

            "month_current_max": (
                round(current_month_value, 1)
                if current_month_value is not None
                else None
            ),
            "month_current_max_date": current_month_date,

            "record_month_old": (
                round(old_month, 1)
                if old_month is not None
                else None
            ),
            "record_month_old_date": old_month_date,

            "record_month": (
                round(record_month_value, 1)
                if record_month_value is not None
                else None
            ),
            "record_month_date": record_month_date,

            "record_month_status": month_status,
            "record_month_delta": month_delta,

            "record_absolute_old": (
                round(old_abs, 1)
                if old_abs is not None
                else None
            ),
            "record_absolute_old_date": old_abs_date,

            "record_absolute": (
                round(record_abs_value, 1)
                if record_abs_value is not None
                else None
            ),
            "record_absolute_date": record_abs_date,

            "record_absolute_status": abs_status,
            "record_absolute_delta": abs_delta,
        })

    stations.sort(
        key=lambda st: (
            -(
                st["tx_today"]
                if st["tx_today"] is not None
                else -999
            ),
            st["name"],
        )
    )

    def vmax(field: str) -> Optional[float]:
        values = [fnum(st.get(field)) for st in stations]
        values = [v for v in values if v is not None]
        return round(max(values), 1) if values else None

    def vmin(field: str) -> Optional[float]:
        values = [fnum(st.get(field)) for st in stations]
        values = [v for v in values if v is not None]
        return round(min(values), 1) if values else None

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "status": "ok",
        "generated_at": iso(utcnow()),
        "latest_observation_at": iso(latest_hour),

        "title": "Températures, ressentis et records",
        "unit": "°C",

        "current_month": {
            "id": f"{latest_hour.year:04d}-{latest_hour.month:02d}",
            "label": f"{month_label} {latest_hour.year}",
            "month_name": month_label,
            "climate_generated_at": cache.get(
                "current_month_generated_at"
            ),
        },

        "metrics": {
            "tx_today": {
                "label": "Temp. max jour",
                "long_label": "Température maximale observée aujourd'hui",
                "max": vmax("tx_today"),
            },

            "record_month": {
                "label": "Record du mois",
                "long_label": f"Record de chaleur pour un mois de {month_label}",
                "max": vmax("record_month"),
            },

            "record_month_status": {
                "label": "Record mois battu ?",
                "long_label": (
                    f"Records de {month_label} battus ou égalés "
                    f"en {latest_hour.year}"
                ),
                "battus": count_month_battu,
                "egales": count_month_egale,
            },

            "record_absolute": {
                "label": "Record absolu",
                "long_label": "Record absolu de température maximale",
                "max": vmax("record_absolute"),
            },

            "record_absolute_status": {
                "label": "Record abs. battu ?",
                "long_label": (
                    "Records absolus battus ou égalés "
                    "pendant le mois en cours"
                ),
                "battus": count_abs_battu,
                "egales": count_abs_egale,
            },

            "humidex": {
                "label": "Humidex",
                "long_label": "Indice Humidex actuel",
                "max": vmax("humidex"),
            },

            "wind_chill": {
                "label": "Ressenti au vent",
                "long_label": "Refroidissement éolien actuel",
                "min": vmin("wind_chill"),
            },

            "variation_24h": {
                "label": "Variation 24 h",
                "long_label": (
                    "Variation de température depuis la même heure hier"
                ),
                "min": vmin("variation_24h"),
                "max": vmax("variation_24h"),
            },

            "min_12h": {
                "label": "Min. 12 h",
                "long_label": "Température minimale sur les 12 dernières heures",
                "min": vmin("min_12h"),
            },

            "min_24h": {
                "label": "Min. 24 h",
                "long_label": "Température minimale sur les 24 dernières heures",
                "min": vmin("min_24h"),
            },

            "max_12h": {
                "label": "Max. 12 h",
                "long_label": "Température maximale sur les 12 dernières heures",
                "max": vmax("max_12h"),
            },

            "max_24h": {
                "label": "Max. 24 h",
                "long_label": "Température maximale sur les 24 dernières heures",
                "max": vmax("max_24h"),
            },
        },

        "stations_total": len(stations),
        "stations_excluded_sapc": len(excluded_sapc),

        "record_counts": {
            "month_battus": count_month_battu,
            "month_egales": count_month_egale,
            "absolute_battus": count_abs_battu,
            "absolute_egales": count_abs_egale,
        },

        "source": {
            "live": "Météo-France - Package Observations V2",
            "fields": "t, td, u, ff, tx, tn",
            "records": (
                "Météo-France - données climatologiques "
                "de base mensuelles"
            ),
            "record_parameter": (
                "TXAB = maximum absolu mensuel des TX quotidiennes"
            ),
            "record_date_parameter": "TXDAT = jour du TXAB",
            "humidex_method": (
                "Formule standard Environnement et Changement "
                "climatique Canada, à partir de T et du point de rosée"
            ),
            "wind_chill_method": (
                "Formule standard de refroidissement éolien "
                "avec vent à 10 m"
            ),
            "variation_24h_method": "T(H) - T(H-24)",
            "extremes_method": (
                "Extrêmes des champs horaires TN/TX, "
                "avec T comme valeur de secours"
            ),
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
    print(f"JSON : {OUTPUT}")
    print(f"Stations : {len(stations)}")
    print(f"SAPC exclues : {len(excluded_sapc)}")
    print(f"Max du jour : {output['metrics']['tx_today']['max']} °C")
    print(
        "Variation 24h :",
        output["metrics"]["variation_24h"]["min"],
        "à",
        output["metrics"]["variation_24h"]["max"],
        "°C"
    )
    print(
        "Min 24h / Max 24h :",
        output["metrics"]["min_24h"]["min"],
        "/",
        output["metrics"]["max_24h"]["max"],
        "°C"
    )
    print(
        "Records du mois battus / égalés : "
        f"{count_month_battu} / {count_month_egale}"
    )
    print(
        "Records absolus battus / égalés : "
        f"{count_abs_battu} / {count_abs_egale}"
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERREUR FATALE : {exc}", file=sys.stderr)
        raise

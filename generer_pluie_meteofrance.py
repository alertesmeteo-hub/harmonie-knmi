#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Météo Climat Pro — Carte pluie Météo-France
Version 2.1.0

Produit :
  observations_pluie.json

Vues :
  - rr24              : cumul des 24 dernières heures à partir de RR1
  - rr_month_current  : cumul du mois en cours
  - rr_month_mean     : cumul moyen du mois sur 1991-2020
  - rr_year_mean      : cumul annuel moyen sur 1991-2020

Sources :
  - Météo-France DPPaquetObs V2 pour les observations horaires
  - Météo-France DPObs V2 /liste-stations pour les noms
  - Données climatologiques quotidiennes Météo-France pour le mois en cours
  - Données climatologiques mensuelles Météo-France pour les normales 1991-2020

Secrets GitHub :
  METEOFRANCE_PACKAGE_OBS_KEY
  METEOFRANCE_OBS_TOKEN

IMPORTANT :
  Ces deux secrets contiennent des API Keys du portail Météo-France.
  Elles sont envoyées dans l'en-tête HTTP "apikey".
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


VERSION = "2.1.0"

PACKAGE_BASE = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)

DPObs_STATIONS_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPObs/v2/liste-stations"
)

MF_S3_QUOT = (
    "https://meteofrance.s3.sbg.io.cloud.ovh.net/"
    "data/synchro_ftp/BASE/QUOT"
)

MF_S3_MENS = (
    "https://meteofrance.s3.sbg.io.cloud.ovh.net/"
    "data/synchro_ftp/BASE/MENS"
)

OUTPUT = Path("observations_pluie.json")
CACHE = Path("cache_pluie_climatologie.json")

NORMAL_START = 1991
NORMAL_END = 2020

HTTP_TIMEOUT = 90
PACKAGE_RETRIES_HOURS = 4

# On accepte un RR24 si au moins 22 des 24 heures sont présentes.
# Le nombre d'heures est conservé dans le JSON pour contrôle.
RR24_MIN_VALID_HOURS = 22

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-carte-pluie/{VERSION}",
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
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fnum(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        x = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def clean_rain(value: Any) -> Optional[float]:
    x = fnum(value)
    if x is None:
        return None
    # Valeurs négatives / sentinelles / aberrantes ignorées.
    if x < 0 or x > 10000:
        return None
    return x


def first(row: dict, names: Iterable[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]

    lowered = {
        str(k).strip().lower(): v
        for k, v in row.items()
    }
    for name in names:
        value = lowered.get(str(name).lower())
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

    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s or None


def parse_delimited(raw: bytes) -> List[dict]:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    text = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass

    if text is None:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:10000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    rows = []
    for row in reader:
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


def get_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Secret GitHub absent : {name}"
        )

    # Nettoyage au cas où "apikey:" ou "Bearer" a été copié.
    value = value.replace("\r", "").replace("\n", "").strip()

    for prefix in ("apikey:", "apiKey:", "Bearer ", "bearer "):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()

    if not value:
        raise RuntimeError(
            f"Secret {name} vide après nettoyage."
        )
    return value


def apikey_headers(key: str) -> dict:
    return {
        "apikey": key,
        "accept": "*/*",
    }


# ---------------------------------------------------------------------
# Package Observations V2
# ---------------------------------------------------------------------

def package_request(
    key: str,
    date_hour: datetime,
) -> Optional[requests.Response]:

    date_text = iso(
        date_hour.replace(
            minute=0,
            second=0,
            microsecond=0,
        )
    )

    response = session.get(
        PACKAGE_BASE,
        params={
            "date": date_text,
            "format": "csv",
        },
        headers=apikey_headers(key),
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
            "Package Observations : HTTP 403, "
            "abonnement ou droits insuffisants."
        )

    if response.status_code == 429:
        raise RuntimeError(
            "Package Observations : HTTP 429, quota dépassé."
        )

    response.raise_for_status()
    return None


def find_latest_package_hour(
    key: str,
) -> Tuple[datetime, requests.Response]:

    base = utcnow().replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    for back in range(PACKAGE_RETRIES_HOURS):
        candidate = base - timedelta(hours=back)
        print(
            "Recherche paquet :",
            iso(candidate),
        )
        response = package_request(key, candidate)
        if response is not None:
            print(
                "Dernier paquet disponible :",
                iso(candidate),
            )
            return candidate, response

    raise RuntimeError(
        "Aucun paquet horaire disponible entre H et H-3."
    )


def load_24_hour_packages(
    key: str,
) -> Tuple[
    datetime,
    Dict[str, dict],
    Dict[str, dict],
]:

    latest_hour, first_response = find_latest_package_hour(key)

    # sid -> métadonnées géographiques
    station_geo: Dict[str, dict] = {}

    # sid -> données agrégées
    agg: Dict[str, dict] = defaultdict(
        lambda: {
            "rr24_sum": 0.0,
            "rr24_hours": 0,
            "today_sum": 0.0,
            "today_hours": 0,
            "latest_date": None,
        }
    )

    current_utc_day = latest_hour.date()

    for offset in range(24):
        target = latest_hour - timedelta(hours=offset)

        if offset == 0:
            response = first_response
        else:
            response = package_request(key, target)

        if response is None:
            print(
                f"[WARN] Paquet absent : {iso(target)}"
            )
            continue

        rows = parse_delimited(response.content)

        print(
            f"Paquet {iso(target)} : "
            f"{len(rows)} lignes"
        )

        for row in rows:
            sid = station_id(row)
            if not sid:
                continue

            lat = fnum(first(row, ("lat", "LAT", "latitude")))
            lon = fnum(first(row, ("lon", "LON", "longitude")))

            if (
                lat is not None
                and lon is not None
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                station_geo[sid] = {
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                }

            rr1 = clean_rain(first(row, ("rr1", "RR1")))
            if rr1 is None:
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
            )

            if validity is None:
                validity = target

            item = agg[sid]
            item["rr24_sum"] += rr1
            item["rr24_hours"] += 1

            if validity.date() == current_utc_day:
                item["today_sum"] += rr1
                item["today_hours"] += 1

            old = parse_iso(item["latest_date"])
            if old is None or validity > old:
                item["latest_date"] = iso(validity)

        # 24 appels seulement, sous la limite du service.
        time.sleep(0.10)

    return latest_hour, station_geo, agg


# ---------------------------------------------------------------------
# DPObs V2 : noms des stations
# ---------------------------------------------------------------------

def load_station_names(
    obs_key: str,
) -> Dict[str, str]:

    print("Chargement /DPObs/v2/liste-stations ...")

    response = session.get(
        DPObs_STATIONS_URL,
        headers=apikey_headers(obs_key),
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code != 200:
        print(
            f"[WARN] liste-stations HTTP "
            f"{response.status_code}; "
            "les identifiants seront utilisés comme noms."
        )
        return {}

    content_type = (
        response.headers.get("content-type") or ""
    ).lower()

    rows: List[dict]

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
            rows = parse_delimited(response.content)
    else:
        rows = parse_delimited(response.content)

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

    print(
        f"Noms récupérés : {len(names)} station(s)."
    )
    return names


# ---------------------------------------------------------------------
# Départements et fichiers climatologiques
# ---------------------------------------------------------------------

def department_candidates(
    sid: str,
) -> List[str]:

    digits = re.sub(r"\D", "", sid)

    # DROM / COM
    for code in (
        "971", "972", "973", "974", "975",
        "976", "977", "978", "984",
        "986", "987", "988",
    ):
        if digits.startswith(code):
            return [code]

    if digits.startswith("20"):
        # Les identifiants historiques corses utilisent 20.
        return ["2A", "2B", "20"]

    return [digits[:2]] if len(digits) >= 2 else []


def current_daily_url(
    dep: str,
    now: datetime,
) -> str:

    return (
        f"{MF_S3_QUOT}/"
        f"Q_{dep}_latest-"
        f"{now.year - 1}-{now.year}_RR-T-Vent.csv.gz"
    )


def historic_monthly_url(
    dep: str,
    now: datetime,
) -> str:

    return (
        f"{MF_S3_MENS}/"
        f"MENSQ_{dep}_previous-"
        f"1950-{now.year - 2}.csv.gz"
    )


def download_climate_rows(
    url: str,
) -> Optional[List[dict]]:

    try:
        response = session.get(
            url,
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:
        print(
            f"[WARN] téléchargement impossible {url}: {exc}"
        )
        return None

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        print(
            f"[WARN] HTTP {response.status_code}: {url}"
        )
        return None

    try:
        return parse_delimited(response.content)
    except Exception as exc:
        print(
            f"[WARN] CSV illisible {url}: {exc}"
        )
        return None


# ---------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------

def load_cache() -> dict:
    if not CACHE.exists():
        return {
            "schema_version": 3,
            "stations": {},
        }

    try:
        payload = json.loads(
            CACHE.read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict):
            raise ValueError("cache non objet")
        payload.setdefault("stations", {})
        return payload
    except Exception as exc:
        print(
            f"[WARN] cache ignoré : {exc}"
        )
        return {
            "schema_version": 3,
            "stations": {},
        }


def cache_datetime(
    cache: dict,
    key: str,
) -> Optional[datetime]:
    return parse_iso(cache.get(key))


def hours_since(
    dt: Optional[datetime],
) -> float:
    if dt is None:
        return 999999.0
    return (
        utcnow() - dt
    ).total_seconds() / 3600.0


# ---------------------------------------------------------------------
# Mois en cours
# ---------------------------------------------------------------------

def update_current_month(
    cache: dict,
    station_ids: List[str],
    now: datetime,
) -> None:

    month_id = f"{now.year:04d}-{now.month:02d}"

    stale = (
        cache.get("current_month_id") != month_id
        or hours_since(
            cache_datetime(
                cache,
                "current_month_generated_at",
            )
        ) >= 8
    )

    if not stale:
        print(
            "Cache du mois en cours encore valide."
        )
        return

    print(
        "Actualisation des données quotidiennes "
        "du mois en cours..."
    )

    wanted = set(station_ids)
    by_dep: Dict[str, set] = defaultdict(set)

    for sid in station_ids:
        for dep in department_candidates(sid):
            by_dep[dep].add(sid)

    totals: Dict[str, float] = defaultdict(float)
    last_day: Dict[str, str] = {}
    seen_days = set()

    prefix = f"{now.year:04d}{now.month:02d}"

    for dep, dep_stations in sorted(by_dep.items()):
        url = current_daily_url(dep, now)
        rows = download_climate_rows(url)

        if rows is None:
            continue

        print(
            f"Quotidien {dep}: {len(rows)} lignes"
        )

        for row in rows:
            sid = station_id(row)
            if sid not in dep_stations:
                continue

            date_value = first(
                row,
                ("AAAAMMJJ", "DATE", "date"),
            )
            date_digits = re.sub(
                r"\D",
                "",
                str(date_value or ""),
            )[:8]

            if (
                len(date_digits) != 8
                or not date_digits.startswith(prefix)
            ):
                continue

            rr = clean_rain(
                first(
                    row,
                    (
                        "RR",
                        "rr",
                        "PRECIP",
                        "PRECIPITATION",
                    ),
                )
            )
            if rr is None:
                continue

            key = (sid, date_digits)
            if key in seen_days:
                continue
            seen_days.add(key)

            totals[sid] += rr

            if date_digits > last_day.get(sid, ""):
                last_day[sid] = date_digits

    stations_cache = cache.setdefault(
        "stations",
        {},
    )

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})
        entry["month_daily_total"] = (
            round(totals[sid], 1)
            if sid in totals
            else None
        )
        entry["month_daily_through"] = (
            last_day.get(sid)
        )

    cache["current_month_id"] = month_id
    cache["current_month_generated_at"] = iso(
        utcnow()
    )


# ---------------------------------------------------------------------
# Normales 1991-2020
# ---------------------------------------------------------------------

def update_normals(
    cache: dict,
    station_ids: List[str],
    now: datetime,
) -> None:

    stations_cache = cache.setdefault(
        "stations",
        {},
    )

    known = sum(
        1
        for sid in station_ids
        if stations_cache.get(sid, {}).get(
            "normal_year"
        ) is not None
    )

    coverage = known / max(1, len(station_ids))

    stale = (
        hours_since(
            cache_datetime(
                cache,
                "normals_generated_at",
            )
        ) >= 24 * 45
        or coverage < 0.80
    )

    if not stale:
        print(
            "Cache des normales 1991-2020 "
            "encore valide."
        )
        return

    print(
        "Calcul des normales 1991-2020..."
    )

    by_dep: Dict[str, set] = defaultdict(set)

    for sid in station_ids:
        for dep in department_candidates(sid):
            by_dep[dep].add(sid)

    # station -> année -> mois -> RR
    values: Dict[
        str,
        Dict[int, Dict[int, float]],
    ] = defaultdict(
        lambda: defaultdict(dict)
    )

    seen = set()

    for dep, dep_stations in sorted(by_dep.items()):
        url = historic_monthly_url(dep, now)
        rows = download_climate_rows(url)

        if rows is None:
            continue

        print(
            f"Mensuel {dep}: {len(rows)} lignes"
        )

        for row in rows:
            sid = station_id(row)
            if sid not in dep_stations:
                continue

            date_value = first(
                row,
                ("AAAAMM", "DATE", "date"),
            )
            digits = re.sub(
                r"\D",
                "",
                str(date_value or ""),
            )

            if len(digits) < 6:
                continue

            try:
                year = int(digits[:4])
                month = int(digits[4:6])
            except ValueError:
                continue

            if not (
                NORMAL_START <= year <= NORMAL_END
                and 1 <= month <= 12
            ):
                continue

            rr = clean_rain(
                first(
                    row,
                    (
                        "RR",
                        "rr",
                        "PRECIP",
                        "PRECIPITATION",
                    ),
                )
            )
            if rr is None:
                continue

            dedup = (sid, year, month)
            if dedup in seen:
                continue
            seen.add(dedup)

            values[sid][year][month] = rr

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})

        month_normals = {}
        month_counts = {}

        for month in range(1, 13):
            month_values = [
                months[month]
                for year, months
                in values.get(sid, {}).items()
                if month in months
            ]

            month_counts[str(month)] = len(
                month_values
            )

            # Seuil de disponibilité minimal.
            if len(month_values) >= 20:
                month_normals[str(month)] = round(
                    sum(month_values)
                    / len(month_values),
                    1,
                )
            else:
                month_normals[str(month)] = None

        annual_values = []

        for year, months in values.get(
            sid,
            {},
        ).items():
            if all(
                month in months
                for month in range(1, 13)
            ):
                annual_values.append(
                    sum(
                        months[month]
                        for month in range(1, 13)
                    )
                )

        entry["normal_months"] = month_normals
        entry["normal_month_counts"] = month_counts
        entry["normal_year"] = (
            round(
                sum(annual_values)
                / len(annual_values),
                1,
            )
            if len(annual_values) >= 20
            else None
        )
        entry["normal_year_count"] = len(
            annual_values
        )

    cache["normals_generated_at"] = iso(
        utcnow()
    )
    cache["normal_period"] = (
        f"{NORMAL_START}-{NORMAL_END}"
    )


# ---------------------------------------------------------------------
# Construction du mois en cours en quasi temps réel
# ---------------------------------------------------------------------

def month_total_with_today(
    sid: str,
    cache_entry: dict,
    hourly: dict,
    latest_hour: datetime,
) -> Tuple[Optional[float], Optional[str]]:

    daily_total = fnum(
        cache_entry.get("month_daily_total")
    )
    daily_through = str(
        cache_entry.get("month_daily_through")
        or ""
    )

    latest_day = latest_hour.strftime("%Y%m%d")
    previous_day = (
        latest_hour.date() - timedelta(days=1)
    ).strftime("%Y%m%d")

    today_sum = fnum(
        hourly.get("today_sum")
    )
    today_hours = int(
        hourly.get("today_hours") or 0
    )

    # Cas idéal : données quotidiennes disponibles jusqu'à hier.
    if daily_total is not None:
        if daily_through == previous_day:
            if today_sum is not None and today_hours > 0:
                return (
                    round(
                        daily_total + today_sum,
                        1,
                    ),
                    latest_day,
                )
            return round(daily_total, 1), daily_through

        # Si le quotidien contient déjà aujourd'hui,
        # ne surtout pas ajouter RR1 une seconde fois.
        if daily_through == latest_day:
            return round(daily_total, 1), daily_through

        # Quotidien en retard de plus d'un jour :
        # on garde uniquement le cumul contrôlé.
        return round(daily_total, 1), (
            daily_through or None
        )

    # En début de mois ou si le fichier quotidien n'est
    # pas encore disponible, on peut au minimum fournir aujourd'hui.
    if (
        latest_hour.day == 1
        and today_sum is not None
        and today_hours > 0
    ):
        return round(today_sum, 1), latest_day

    return None, None


# ---------------------------------------------------------------------
# Sortie
# ---------------------------------------------------------------------

MONTHS_FR = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def main() -> int:

    print(
        f"=== Carte pluie Météo-France v{VERSION} ==="
    )

    package_key = get_secret(
        "METEOFRANCE_PACKAGE_OBS_KEY"
    )
    obs_key = get_secret(
        "METEOFRANCE_OBS_TOKEN"
    )

    # 1. Observations horaires
    latest_hour, station_geo, hourly = (
        load_24_hour_packages(package_key)
    )

    station_ids = sorted(station_geo.keys())

    print(
        f"{len(station_ids)} station(s) "
        "présente(s) dans les paquets."
    )

    # 2. Noms
    names = load_station_names(obs_key)

    # 3. Cache climatologique
    cache = load_cache()

    update_current_month(
        cache,
        station_ids,
        latest_hour,
    )

    update_normals(
        cache,
        station_ids,
        latest_hour,
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

    # 4. JSON final
    month_key = str(latest_hour.month)
    stations = []

    for sid in station_ids:
        geo = station_geo[sid]
        h = hourly.get(sid, {})
        clim = cache.get(
            "stations",
            {},
        ).get(sid, {})

        valid_hours = int(
            h.get("rr24_hours") or 0
        )

        rr24 = None

        if valid_hours >= RR24_MIN_VALID_HOURS:
            rr24 = round(
                float(h.get("rr24_sum") or 0.0),
                1,
            )

        rr_month_current, month_through = (
            month_total_with_today(
                sid,
                clim,
                h,
                latest_hour,
            )
        )

        month_norms = (
            clim.get("normal_months")
            or {}
        )

        rr_month_mean = fnum(
            month_norms.get(month_key)
        )

        rr_year_mean = fnum(
            clim.get("normal_year")
        )

        stations.append({
            "id": sid,
            "name": names.get(sid, sid),
            "lat": geo["lat"],
            "lon": geo["lon"],
            "date": h.get("latest_date"),
            "rr24": rr24,
            "rr24_hours": valid_hours,
            "rr24_complete": valid_hours == 24,
            "rr_month_current": (
                round(rr_month_current, 1)
                if rr_month_current is not None
                else None
            ),
            "rr_month_current_through": (
                month_through
            ),
            "rr_month_mean": (
                round(rr_month_mean, 1)
                if rr_month_mean is not None
                else None
            ),
            "rr_year_mean": (
                round(rr_year_mean, 1)
                if rr_year_mean is not None
                else None
            ),
            "normal_month_years": (
                (
                    clim.get(
                        "normal_month_counts"
                    )
                    or {}
                ).get(month_key)
            ),
            "normal_year_years": (
                clim.get("normal_year_count")
            ),
        })

    def max_field(field: str) -> float:
        vals = [
            fnum(st.get(field))
            for st in stations
        ]
        vals = [
            x for x in vals
            if x is not None
        ]
        return round(max(vals), 1) if vals else 0.0

    month_label = (
        f"{MONTHS_FR[latest_hour.month - 1]} "
        f"{latest_hour.year}"
    )

    output = {
        "schema_version": 3,
        "module_version": VERSION,
        "status": "ok",
        "generated_at": iso(utcnow()),
        "latest_observation_at": iso(
            latest_hour
        ),
        "title": "Cumuls de précipitations",
        "unit": "mm",
        "normal_period": (
            f"{NORMAL_START}-{NORMAL_END}"
        ),
        "current_month": {
            "id": (
                f"{latest_hour.year:04d}-"
                f"{latest_hour.month:02d}"
            ),
            "label": month_label,
            "generated_at": cache.get(
                "current_month_generated_at"
            ),
        },
        "metrics": {
            "rr24": {
                "label": "24 h",
                "long_label": (
                    "Cumuls de précipitations "
                    "sur les 24 dernières heures"
                ),
                "max": max_field("rr24"),
            },
            "rr_month_current": {
                "label": "Mois en cours",
                "long_label": (
                    f"Cumul depuis le 1er "
                    f"{MONTHS_FR[latest_hour.month - 1]}"
                ),
                "max": max_field(
                    "rr_month_current"
                ),
            },
            "rr_month_mean": {
                "label": "Moy. du mois",
                "long_label": (
                    f"Cumul moyen de "
                    f"{MONTHS_FR[latest_hour.month - 1]} "
                    f"({NORMAL_START}-{NORMAL_END})"
                ),
                "max": max_field(
                    "rr_month_mean"
                ),
            },
            "rr_year_mean": {
                "label": "Moy. annuelle",
                "long_label": (
                    f"Cumul moyen annuel "
                    f"({NORMAL_START}-{NORMAL_END})"
                ),
                "max": max_field(
                    "rr_year_mean"
                ),
            },
        },
        "stations_total": len(stations),
        "stations_rr24": sum(
            1
            for st in stations
            if st["rr24"] is not None
        ),
        "source": {
            "observations": (
                "Météo-France - "
                "Package Observations V2"
            ),
            "rr24_method": (
                "Somme de RR1 sur 24 paquets horaires"
            ),
            "current_month": (
                "Météo-France - "
                "données climatologiques quotidiennes "
                "+ RR1 du jour si nécessaire"
            ),
            "normals": (
                "Météo-France - "
                "données climatologiques mensuelles"
            ),
            "normal_period": (
                f"{NORMAL_START}-{NORMAL_END}"
            ),
        },
        "stations": stations,
    }

    stations.sort(
        key=lambda st: (
            -(
                st["rr24"]
                if st["rr24"] is not None
                else -1
            ),
            st["name"],
        )
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
    print("=== TERMINÉ ===")
    print(
        f"JSON : {OUTPUT}"
    )
    print(
        f"Stations : {len(stations)}"
    )
    print(
        "Stations RR24 exploitables : "
        f"{output['stations_rr24']}"
    )
    print(
        "Cumul 24 h maximal : "
        f"{output['metrics']['rr24']['max']} mm"
    )
    print(
        "Cumul mois maximal : "
        f"{output['metrics']['rr_month_current']['max']} mm"
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"ERREUR FATALE : {exc}",
            file=sys.stderr,
        )
        raise

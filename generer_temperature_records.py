#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Météo Climat Pro — Températures, ressentis & records
Version 1.2.0

CORRECTIONS MAJEURES
--------------------
- Ajout de la TEMPÉRATURE RÉELLE / ACTUELLE comme métrique principale.
- Min/Max 12 h et 24 h calculées sur les températures horaires réelles `t`.
- Variation 24 h : comparaison avec le relevé le plus proche de H-24
  (tolérance 90 minutes).
- Humidex calculé à partir de t + td.
- Ressenti au vent :
    * wind chill officiel si T <= 10 °C et vent >= 4,8 km/h ;
    * sinon la température réelle est conservée comme "ressenti neutre"
      afin que la carte ne soit pas vide hors conditions froides.
- Stations SAPC exclues.
- Records de chaleur conservés via TXAB / TXDAT.
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


VERSION = "1.2.0"
SCHEMA_VERSION = 3

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
PACKAGE_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.15"))
PACKAGE_RETRIES_HOURS = 4

# H à H-25 : permet de trouver H-24 même si une heure manque.
OBS_HISTORY_HOURS = 26
VARIATION_TARGET_HOURS = 24
VARIATION_TOLERANCE_MINUTES = 90

HISTORICAL_CACHE_DAYS = 35
CURRENT_MONTH_REFRESH_HOURS = 6
RECORD_EPSILON = 0.05

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-temperature-records/{VERSION}",
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


def celsius(value: Any) -> Optional[float]:
    n = fnum(value)
    if n is None:
        return None
    if n > 100:
        n -= 273.15
    if n < -100 or n > 70:
        return None
    return round(n, 1)


def first(row: dict, names: Iterable[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]

    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = lower.get(str(name).lower())
        if value not in (None, ""):
            return value
    return None


def station_id(row: dict) -> Optional[str]:
    value = first(
        row,
        ("geo_id_insee", "NUM_POSTE", "num_poste", "id_station", "numer_sta"),
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


def api_headers(key: str) -> dict:
    return {"apikey": key, "accept": "*/*"}


def is_sapc(name: str) -> bool:
    words = re.sub(r"[^A-Z0-9]+", " ", str(name).upper()).split()
    return "SAPC" in words


def quality_ok(value: Any) -> bool:
    if value in (None, ""):
        return True
    try:
        return int(float(str(value).replace(",", "."))) != 2
    except Exception:
        return True


def ym_int(value: Any) -> Optional[int]:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 6:
        return None
    try:
        return int(digits[:6])
    except ValueError:
        return None


def month_day_date(aaaamm: Any, day_value: Any) -> Optional[str]:
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
            candidate = f"{ym}{int(d):02d}"
        except ValueError:
            return None

    try:
        datetime.strptime(candidate, "%Y%m%d")
    except ValueError:
        return None
    return candidate


# ------------------------------------------------------------
# Humidex / ressenti au vent
# ------------------------------------------------------------

def calc_humidex(
    temperature_c: Optional[float],
    dewpoint_c: Optional[float],
) -> Optional[float]:
    if temperature_c is None or dewpoint_c is None:
        return None

    # Domaine d'usage pratique : chaleur.
    if temperature_c < 20.0:
        return None

    td_k = dewpoint_c + 273.15
    if td_k <= 0:
        return None

    vapour_pressure = 6.11 * math.exp(
        5417.7530 * ((1.0 / 273.15) - (1.0 / td_k))
    )
    humidex = temperature_c + 0.5555 * (vapour_pressure - 10.0)

    if humidex < temperature_c + 1.0:
        return None

    return round(humidex, 1)


def calc_wind_chill(
    temperature_c: Optional[float],
    wind_ms: Optional[float],
) -> Tuple[Optional[float], bool]:
    """
    Retourne (ressenti, formule_wind_chill_appliquee).

    Hors domaine standard du refroidissement éolien, on renvoie
    la température réelle pour que la vue "Ressenti au vent" reste
    exploitable et non vide.
    """
    if temperature_c is None:
        return None, False

    if wind_ms is None:
        return round(temperature_c, 1), False

    wind_kmh = wind_ms * 3.6

    if temperature_c <= 10.0 and wind_kmh >= 4.8:
        p = wind_kmh ** 0.16
        wc = (
            13.12
            + 0.6215 * temperature_c
            - 11.37 * p
            + 0.3965 * temperature_c * p
        )
        return round(wc, 1), True

    return round(temperature_c, 1), False


# ------------------------------------------------------------
# Package Observations V2
# ------------------------------------------------------------

def package_request(
    key: str,
    hour: datetime,
) -> Optional[requests.Response]:

    hour = hour.replace(minute=0, second=0, microsecond=0)

    response = session.get(
        PACKAGE_URL,
        params={"date": iso(hour), "format": "csv"},
        headers=api_headers(key),
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code == 200:
        return response

    if response.status_code in (400, 404):
        return None

    if response.status_code == 401:
        raise RuntimeError("Package Observations : HTTP 401.")
    if response.status_code == 403:
        raise RuntimeError("Package Observations : HTTP 403.")
    if response.status_code == 429:
        raise RuntimeError("Package Observations : HTTP 429.")

    response.raise_for_status()
    return None


def find_latest_package(
    key: str,
) -> Tuple[datetime, requests.Response]:

    base = utcnow().replace(minute=0, second=0, microsecond=0)

    for back in range(PACKAGE_RETRIES_HOURS):
        hour = base - timedelta(hours=back)
        print("Recherche paquet :", iso(hour))

        response = package_request(key, hour)
        if response is not None:
            print("Dernier paquet disponible :", iso(hour))
            return hour, response

        time.sleep(PACKAGE_DELAY)

    raise RuntimeError("Aucun paquet disponible entre H et H-3.")


def load_live_history(
    key: str,
) -> Tuple[datetime, Dict[str, dict]]:

    latest_hour, latest_response = find_latest_package(key)

    stations: Dict[str, dict] = defaultdict(
        lambda: {
            "lat": None,
            "lon": None,
            "samples": [],
            "latest_date": None,
            "temperature": None,
            "dewpoint": None,
            "humidity": None,
            "wind_ms": None,
        }
    )

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
                first(row, ("validity_time", "reference_time", "date"))
            ) or target

            t = celsius(first(row, ("t", "T")))
            if t is None:
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

            sample = {
                "dt": validity,
                "temperature": t,
            }
            item["samples"].append(sample)

            old = parse_iso(item["latest_date"])
            if old is None or validity > old:
                item["latest_date"] = iso(validity)
                item["temperature"] = t
                item["dewpoint"] = celsius(first(row, ("td", "TD")))

                rh = fnum(first(row, ("u", "U")))
                item["humidity"] = (
                    round(rh, 1)
                    if rh is not None and 0 <= rh <= 100
                    else None
                )

                ff = fnum(first(row, ("ff", "FF")))
                item["wind_ms"] = (
                    round(ff, 2)
                    if ff is not None and ff >= 0
                    else None
                )

    # Calcul métriques par station
    target_24 = latest_hour - timedelta(hours=24)

    for item in stations.values():
        samples = sorted(item["samples"], key=lambda s: s["dt"])

        # Fenêtres glissantes exactes sur timestamps.
        start_12 = latest_hour - timedelta(hours=12)
        start_24 = latest_hour - timedelta(hours=24)

        vals_12 = [
            s for s in samples
            if start_12 < s["dt"] <= latest_hour
        ]
        vals_24 = [
            s for s in samples
            if start_24 < s["dt"] <= latest_hour
        ]

        def min_sample(values):
            return min(values, key=lambda s: s["temperature"]) if values else None

        def max_sample(values):
            return max(values, key=lambda s: s["temperature"]) if values else None

        mn12 = min_sample(vals_12)
        mn24 = min_sample(vals_24)
        mx12 = max_sample(vals_12)
        mx24 = max_sample(vals_24)

        item["min_12h"] = mn12["temperature"] if mn12 else None
        item["min_12h_time"] = iso(mn12["dt"]) if mn12 else None
        item["min_24h"] = mn24["temperature"] if mn24 else None
        item["min_24h_time"] = iso(mn24["dt"]) if mn24 else None

        item["max_12h"] = mx12["temperature"] if mx12 else None
        item["max_12h_time"] = iso(mx12["dt"]) if mx12 else None
        item["max_24h"] = mx24["temperature"] if mx24 else None
        item["max_24h_time"] = iso(mx24["dt"]) if mx24 else None

        item["hours_12h"] = len(vals_12)
        item["hours_24h"] = len(vals_24)

        # Max du jour calendaire UTC à partir de T réelle.
        today_vals = [
            s for s in samples
            if s["dt"].date() == latest_hour.date()
        ]
        tx_today_sample = max_sample(today_vals)
        item["tx_today"] = (
            tx_today_sample["temperature"]
            if tx_today_sample else None
        )
        item["tx_today_time"] = (
            iso(tx_today_sample["dt"])
            if tx_today_sample else None
        )

        # H-24 : point le plus proche dans une tolérance de 90 min.
        old_sample = None
        best_delta = None

        for s in samples:
            delta = abs((s["dt"] - target_24).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta = delta
                old_sample = s

        if (
            old_sample is not None
            and best_delta is not None
            and best_delta <= VARIATION_TOLERANCE_MINUTES * 60
        ):
            item["temp_24h_ago"] = old_sample["temperature"]
            item["temp_24h_ago_time"] = iso(old_sample["dt"])
        else:
            item["temp_24h_ago"] = None
            item["temp_24h_ago_time"] = None

        current = fnum(item.get("temperature"))
        old_temp = fnum(item.get("temp_24h_ago"))

        item["variation_24h"] = (
            round(current - old_temp, 1)
            if current is not None and old_temp is not None
            else None
        )

        item["humidex"] = calc_humidex(
            current,
            fnum(item.get("dewpoint")),
        )

        wind_feels, applied = calc_wind_chill(
            current,
            fnum(item.get("wind_ms")),
        )
        item["wind_feels_like"] = wind_feels
        item["wind_chill_applied"] = applied

        item.pop("samples", None)

    return latest_hour, dict(stations)


# ------------------------------------------------------------
# Noms stations
# ------------------------------------------------------------

def load_station_names(key: str) -> Dict[str, str]:
    print("Chargement liste-stations ...")

    response = session.get(
        DPOBS_STATIONS_URL,
        headers=api_headers(key),
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code != 200:
        print("[WARN] liste-stations indisponible :", response.status_code)
        return {}

    content_type = (response.headers.get("content-type") or "").lower()

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
            ("nom_usuel", "NOM_USUEL", "nom", "NOM", "name", "libelle"),
        )
        if name:
            names[sid] = str(name).strip()

    print("Noms récupérés :", len(names))
    return names


# ------------------------------------------------------------
# Climatologie des records
# ------------------------------------------------------------

def department_candidates(sid: str) -> List[str]:
    digits = re.sub(r"\D", "", sid)

    for code in (
        "971", "972", "973", "974", "975",
        "976", "977", "978", "984", "986", "987", "988",
    ):
        if digits.startswith(code):
            return [code]

    if digits.startswith("20"):
        return ["2A", "2B", "20"]

    return [digits[:2]] if len(digits) >= 2 else []


def monthly_urls(dep: str, now: datetime) -> List[str]:
    return [
        f"{MF_S3_MENS}/MENSQ_{dep}_avant-1949.csv.gz",
        f"{MF_S3_MENS}/MENSQ_{dep}_previous-1950-{now.year - 2}.csv.gz",
        f"{MF_S3_MENS}/MENSQ_{dep}_latest-{now.year - 1}-{now.year}.csv.gz",
    ]


def latest_monthly_url(dep: str, now: datetime) -> str:
    return (
        f"{MF_S3_MENS}/"
        f"MENSQ_{dep}_latest-{now.year - 1}-{now.year}.csv.gz"
    )


def download_rows(url: str) -> Optional[List[dict]]:
    try:
        response = session.get(url, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        print("[WARN] téléchargement :", exc)
        return None

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        print("[WARN]", response.status_code, url)
        return None

    try:
        return parse_csv(response.content)
    except Exception as exc:
        print("[WARN] CSV :", exc)
        return None


def load_cache() -> dict:
    if not CACHE.exists():
        return {"schema_version": SCHEMA_VERSION, "stations": {}}

    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("stations", {})
        return data
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "stations": {}}


def age_hours(value: Any) -> float:
    dt = parse_iso(value)
    if dt is None:
        return 999999.0
    return (utcnow() - dt).total_seconds() / 3600.0


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
        if candidate_date and (not current_date or candidate_date < current_date):
            return current_value, candidate_date

    return current_value, current_date


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
        or age_hours(cache.get("records_generated_at")) >= HISTORICAL_CACHE_DAYS * 24
        or coverage < 0.75
    )

    if not stale:
        print("Cache records historique valide.")
        return

    wanted = defaultdict(set)
    for sid in station_ids:
        for dep in department_candidates(sid):
            wanted[dep].add(sid)

    month_val = {sid: None for sid in station_ids}
    month_date = {sid: None for sid in station_ids}
    abs_val = {sid: None for sid in station_ids}
    abs_date = {sid: None for sid in station_ids}

    seen = set()

    for dep, dep_ids in sorted(wanted.items()):
        for url in monthly_urls(dep, now):
            rows = download_rows(url)
            if rows is None:
                continue

            for row in rows:
                sid = station_id(row)
                if sid not in dep_ids:
                    continue

                ym = ym_int(first(row, ("AAAAMM", "DATE", "date")))
                if ym is None or ym >= current_ym:
                    continue

                txab = fnum(first(row, ("TXAB", "txab")))
                if txab is None or not (-100 < txab < 70):
                    continue

                if not quality_ok(first(row, ("QTXAB", "qtxab"))):
                    continue

                key = (sid, ym)
                if key in seen:
                    continue
                seen.add(key)

                txdate = month_day_date(
                    ym,
                    first(row, ("TXDAT", "txdat")),
                )

                abs_val[sid], abs_date[sid] = update_record(
                    abs_val[sid], abs_date[sid], txab, txdate
                )

                if ym % 100 == now.month:
                    month_val[sid], month_date[sid] = update_record(
                        month_val[sid], month_date[sid], txab, txdate
                    )

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})
        entry["record_month_old"] = month_val[sid]
        entry["record_month_old_date"] = month_date[sid]
        entry["record_absolute_old"] = abs_val[sid]
        entry["record_absolute_old_date"] = abs_date[sid]

    cache["record_baseline_month_id"] = month_id
    cache["records_generated_at"] = iso(utcnow())


def update_current_month_climate(
    cache: dict,
    station_ids: List[str],
    now: datetime,
) -> None:

    month_id = f"{now.year:04d}-{now.month:02d}"
    current_ym = now.year * 100 + now.month

    stale = (
        cache.get("current_month_id") != month_id
        or age_hours(cache.get("current_month_generated_at"))
        >= CURRENT_MONTH_REFRESH_HOURS
    )

    if not stale:
        return

    wanted = defaultdict(set)
    for sid in station_ids:
        for dep in department_candidates(sid):
            wanted[dep].add(sid)

    values = {sid: None for sid in station_ids}
    dates = {sid: None for sid in station_ids}

    for dep, dep_ids in sorted(wanted.items()):
        rows = download_rows(latest_monthly_url(dep, now))
        if rows is None:
            continue

        for row in rows:
            sid = station_id(row)
            if sid not in dep_ids:
                continue

            ym = ym_int(first(row, ("AAAAMM", "DATE", "date")))
            if ym != current_ym:
                continue

            txab = fnum(first(row, ("TXAB", "txab")))
            if txab is None or not (-100 < txab < 70):
                continue

            if not quality_ok(first(row, ("QTXAB", "qtxab"))):
                continue

            values[sid], dates[sid] = update_record(
                values[sid],
                dates[sid],
                txab,
                month_day_date(ym, first(row, ("TXDAT", "txdat"))),
            )

    stations_cache = cache.setdefault("stations", {})
    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})
        entry["current_month_txab"] = values[sid]
        entry["current_month_txab_date"] = dates[sid]

    cache["current_month_id"] = month_id
    cache["current_month_generated_at"] = iso(utcnow())


def record_status(
    current_value: Optional[float],
    old_value: Optional[float],
) -> str:

    if current_value is None or old_value is None:
        return "indisponible"
    if current_value > old_value + RECORD_EPSILON:
        return "battu"
    if abs(current_value - old_value) <= RECORD_EPSILON:
        return "egale"
    return "non"


def current_record(
    old_value: Optional[float],
    old_date: Optional[str],
    current_value: Optional[float],
    current_date: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:

    if current_value is None:
        return old_value, old_date
    if old_value is None:
        return current_value, current_date
    if current_value > old_value + RECORD_EPSILON:
        return current_value, current_date
    return old_value, old_date


MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main() -> int:
    print(f"=== Températures & Records v{VERSION} ===")

    package_key = get_secret("METEOFRANCE_PACKAGE_OBS_KEY")
    obs_key = get_secret("METEOFRANCE_OBS_TOKEN")

    latest_hour, live = load_live_history(package_key)
    names = load_station_names(obs_key)

    raw_ids = sorted(
        sid
        for sid, data in live.items()
        if data.get("lat") is not None and data.get("lon") is not None
    )

    excluded = [
        sid for sid in raw_ids if is_sapc(names.get(sid, sid))
    ]
    excluded_set = set(excluded)
    station_ids = [sid for sid in raw_ids if sid not in excluded_set]

    cache = load_cache()
    build_historical_records(cache, station_ids, latest_hour)
    update_current_month_climate(cache, station_ids, latest_hour)

    cache["schema_version"] = SCHEMA_VERSION
    cache["module_version"] = VERSION

    CACHE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    today_key = latest_hour.strftime("%Y%m%d")
    month_name = MONTHS_FR[latest_hour.month - 1]

    count_month_battu = 0
    count_month_egale = 0
    count_abs_battu = 0
    count_abs_egale = 0

    stations = []

    for sid in station_ids:
        obs = live[sid]
        clim = cache.get("stations", {}).get(sid, {})

        current_month_value = fnum(clim.get("current_month_txab"))
        current_month_date = clim.get("current_month_txab_date")

        tx_today = fnum(obs.get("tx_today"))
        if (
            tx_today is not None
            and (
                current_month_value is None
                or tx_today > current_month_value + RECORD_EPSILON
            )
        ):
            current_month_value = tx_today
            current_month_date = today_key

        old_month = fnum(clim.get("record_month_old"))
        old_month_date = clim.get("record_month_old_date")
        old_abs = fnum(clim.get("record_absolute_old"))
        old_abs_date = clim.get("record_absolute_old_date")

        month_status = record_status(current_month_value, old_month)
        abs_status = record_status(current_month_value, old_abs)

        record_month, record_month_date = current_record(
            old_month, old_month_date, current_month_value, current_month_date
        )
        record_abs, record_abs_date = current_record(
            old_abs, old_abs_date, current_month_value, current_month_date
        )

        month_delta = (
            round(current_month_value - old_month, 1)
            if month_status == "battu"
            and current_month_value is not None
            and old_month is not None
            else 0.0 if month_status == "egale" else None
        )

        abs_delta = (
            round(current_month_value - old_abs, 1)
            if abs_status == "battu"
            and current_month_value is not None
            and old_abs is not None
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
            "date": obs.get("latest_date"),

            # Direct
            "temperature": obs.get("temperature"),
            "dewpoint": obs.get("dewpoint"),
            "humidity": obs.get("humidity"),
            "wind_ms": wind_ms,
            "wind_kmh": round(wind_ms * 3.6, 1) if wind_ms is not None else None,

            # Ressentis
            "humidex": obs.get("humidex"),
            "wind_feels_like": obs.get("wind_feels_like"),
            "wind_chill_applied": bool(obs.get("wind_chill_applied")),

            # Evolution
            "temp_24h_ago": obs.get("temp_24h_ago"),
            "temp_24h_ago_time": obs.get("temp_24h_ago_time"),
            "variation_24h": obs.get("variation_24h"),

            # Extrêmes glissants
            "min_12h": obs.get("min_12h"),
            "min_12h_time": obs.get("min_12h_time"),
            "min_24h": obs.get("min_24h"),
            "min_24h_time": obs.get("min_24h_time"),
            "max_12h": obs.get("max_12h"),
            "max_12h_time": obs.get("max_12h_time"),
            "max_24h": obs.get("max_24h"),
            "max_24h_time": obs.get("max_24h_time"),
            "hours_12h": obs.get("hours_12h"),
            "hours_24h": obs.get("hours_24h"),

            # Max du jour
            "tx_today": tx_today,
            "tx_today_time": obs.get("tx_today_time"),

            # Records
            "month_current_max": current_month_value,
            "month_current_max_date": current_month_date,

            "record_month_old": old_month,
            "record_month_old_date": old_month_date,
            "record_month": record_month,
            "record_month_date": record_month_date,
            "record_month_status": month_status,
            "record_month_delta": month_delta,

            "record_absolute_old": old_abs,
            "record_absolute_old_date": old_abs_date,
            "record_absolute": record_abs,
            "record_absolute_date": record_abs_date,
            "record_absolute_status": abs_status,
            "record_absolute_delta": abs_delta,
        })

    stations.sort(
        key=lambda st: (
            -(st["temperature"] if st["temperature"] is not None else -999),
            st["name"],
        )
    )

    def values(field: str) -> List[float]:
        out = []
        for st in stations:
            value = fnum(st.get(field))
            if value is not None:
                out.append(value)
        return out

    def vmax(field: str) -> Optional[float]:
        vals = values(field)
        return round(max(vals), 1) if vals else None

    def vmin(field: str) -> Optional[float]:
        vals = values(field)
        return round(min(vals), 1) if vals else None

    def count(field: str) -> int:
        return len(values(field))

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
            "label": f"{month_name} {latest_hour.year}",
            "month_name": month_name,
        },

        "metrics": {
            "temperature": {
                "label": "Température réelle",
                "long_label": "Température réelle observée au dernier relevé",
                "min": vmin("temperature"),
                "max": vmax("temperature"),
                "stations": count("temperature"),
            },
            "tx_today": {
                "label": "Max du jour",
                "long_label": "Température maximale observée depuis 00 UTC",
                "max": vmax("tx_today"),
                "stations": count("tx_today"),
            },
            "humidex": {
                "label": "Humidex",
                "long_label": "Indice Humidex calculé au dernier relevé",
                "max": vmax("humidex"),
                "stations": count("humidex"),
            },
            "wind_feels_like": {
                "label": "Ressenti au vent",
                "long_label": "Ressenti au vent / refroidissement éolien",
                "min": vmin("wind_feels_like"),
                "max": vmax("wind_feels_like"),
                "stations": count("wind_feels_like"),
            },
            "variation_24h": {
                "label": "Variation 24 h",
                "long_label": "Écart avec le relevé le plus proche de la même heure hier",
                "min": vmin("variation_24h"),
                "max": vmax("variation_24h"),
                "stations": count("variation_24h"),
            },
            "min_12h": {
                "label": "Min. 12 h",
                "long_label": "Température minimale réelle sur les 12 dernières heures",
                "min": vmin("min_12h"),
                "stations": count("min_12h"),
            },
            "min_24h": {
                "label": "Min. 24 h",
                "long_label": "Température minimale réelle sur les 24 dernières heures",
                "min": vmin("min_24h"),
                "stations": count("min_24h"),
            },
            "max_12h": {
                "label": "Max. 12 h",
                "long_label": "Température maximale réelle sur les 12 dernières heures",
                "max": vmax("max_12h"),
                "stations": count("max_12h"),
            },
            "max_24h": {
                "label": "Max. 24 h",
                "long_label": "Température maximale réelle sur les 24 dernières heures",
                "max": vmax("max_24h"),
                "stations": count("max_24h"),
            },
            "record_month": {
                "label": "Record du mois",
                "long_label": f"Record de chaleur pour un mois de {month_name}",
                "max": vmax("record_month"),
            },
            "record_month_status": {
                "label": "Record mois battu ?",
                "long_label": f"Records de {month_name} battus ou égalés",
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
                "long_label": "Records absolus battus ou égalés pendant le mois en cours",
                "battus": count_abs_battu,
                "egales": count_abs_egale,
            },
        },

        "stations_total": len(stations),
        "stations_excluded_sapc": len(excluded),

        "record_counts": {
            "month_battus": count_month_battu,
            "month_egales": count_month_egale,
            "absolute_battus": count_abs_battu,
            "absolute_egales": count_abs_egale,
        },

        "source": {
            "live": "Météo-France - Package Observations V2",
            "live_fields": "t, td, u, ff",
            "records": "Météo-France - données climatologiques mensuelles",
            "variation_24h_method": "T actuelle - T la plus proche de H-24 (tolérance 90 min)",
            "extremes_method": "Min/Max des températures horaires réelles t",
            "humidex_method": "T + point de rosée",
            "wind_feels_like_method": (
                "Wind chill standard si applicable, sinon température réelle"
            ),
        },

        "stations": stations,
    }

    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    print()
    print("=== TERMINÉ ===")
    print("Module :", VERSION)
    print("Stations :", len(stations))
    for field in (
        "temperature",
        "variation_24h",
        "min_12h",
        "min_24h",
        "max_12h",
        "max_24h",
        "humidex",
        "wind_feels_like",
    ):
        print(
            f"{field}:",
            output["metrics"][field].get("stations", 0),
            "station(s)"
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

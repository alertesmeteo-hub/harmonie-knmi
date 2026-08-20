#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Classements pluie France
Version 1.0.0

Produit un JSON pour 5 tableaux :
1. Pluie 1 h
2. Pluie 24 h glissantes
3. Pluie depuis 06 UTC + records quotidiens 06-06 UTC
4. Pluie 48 h glissantes
5. Pluie 72 h glissantes

Les observations horaires Météo-France n'étant conservées que 24 h côté API,
le workflow maintient un cache glissant 96 h sur la branche observations.
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
from zoneinfo import ZoneInfo

import requests

VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "classements-pluie-5-tableaux-records-20260820"

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

OUTPUT = Path("classements_pluie.json")
CACHE = Path("cache_classements_pluie_96h.json")
RECORD_CACHE = Path("cache_records_pluie.json")

PARIS = ZoneInfo("Europe/Paris")
HTTP_TIMEOUT = 90
PACKAGE_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.15"))
MAX_HTTP_ATTEMPTS = 4
CACHE_HOURS = 96
API_BACKFILL_HOURS = 24
LATEST_PACKAGE_LOOKBACK_HOURS = 4
RECORD_EPSILON = 0.05
HISTORICAL_RECORD_CACHE_DAYS = 35
CURRENT_RECORD_REFRESH_HOURS = 6

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-classements-pluie/{VERSION}",
})

DEPARTMENTS = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes", "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes",
    "09": "Ariège", "10": "Aube", "11": "Aude", "12": "Aveyron",
    "13": "Bouches-du-Rhône", "14": "Calvados", "15": "Cantal", "16": "Charente",
    "17": "Charente-Maritime", "18": "Cher", "19": "Corrèze", "20": "Corse",
    "21": "Côte-d'Or", "22": "Côtes-d'Armor", "23": "Creuse", "24": "Dordogne",
    "25": "Doubs", "26": "Drôme", "27": "Eure", "28": "Eure-et-Loir",
    "29": "Finistère", "30": "Gard", "31": "Haute-Garonne", "32": "Gers",
    "33": "Gironde", "34": "Hérault", "35": "Ille-et-Vilaine", "36": "Indre",
    "37": "Indre-et-Loire", "38": "Isère", "39": "Jura", "40": "Landes",
    "41": "Loir-et-Cher", "42": "Loire", "43": "Haute-Loire", "44": "Loire-Atlantique",
    "45": "Loiret", "46": "Lot", "47": "Lot-et-Garonne", "48": "Lozère",
    "49": "Maine-et-Loire", "50": "Manche", "51": "Marne", "52": "Haute-Marne",
    "53": "Mayenne", "54": "Meurthe-et-Moselle", "55": "Meuse", "56": "Morbihan",
    "57": "Moselle", "58": "Nièvre", "59": "Nord", "60": "Oise",
    "61": "Orne", "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin", "68": "Haut-Rhin",
    "69": "Rhône", "70": "Haute-Saône", "71": "Saône-et-Loire", "72": "Sarthe",
    "73": "Savoie", "74": "Haute-Savoie", "75": "Paris", "76": "Seine-Maritime",
    "77": "Seine-et-Marne", "78": "Yvelines", "79": "Deux-Sèvres", "80": "Somme",
    "81": "Tarn", "82": "Tarn-et-Garonne", "83": "Var", "84": "Vaucluse",
    "85": "Vendée", "86": "Vienne", "87": "Haute-Vienne", "88": "Vosges",
    "89": "Yonne", "90": "Territoire de Belfort", "91": "Essonne", "92": "Hauts-de-Seine",
    "93": "Seine-Saint-Denis", "94": "Val-de-Marne", "95": "Val-d'Oise",
}

MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)
WEEKDAYS_FR = (
    "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def rain_mm(value: Any) -> Optional[float]:
    n = fnum(value)
    if n is None or n < 0 or n > 1000:
        return None
    return round(n, 2)


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
    value = first(row, ("geo_id_insee", "NUM_POSTE", "num_poste", "id_station", "numer_sta"))
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
    return code if code in DEPARTMENTS else None


def is_metropole_station(sid: str) -> bool:
    return department_code(sid) is not None


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


def format_record_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 8:
        return str(value)
    return f"{digits[6:8]}/{digits[4:6]}/{digits[:4]}"


def parse_csv(raw: bytes) -> List[dict]:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:10000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows: List[dict] = []
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        clean = {}
        for key, value in row.items():
            if key is None:
                continue
            clean[str(key).strip()] = value.strip() if isinstance(value, str) else value
        rows.append(clean)
    return rows


def clean_secret(value: str) -> str:
    value = (value or "").replace("\r", "").replace("\n", "").strip()
    for prefix in ("apikey:", "apiKey:", "Bearer ", "bearer "):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    return value


def get_package_key() -> str:
    for name in ("METEOFRANCE_PACKAGE_OBS_KEY", "METEOFRANCE_OBS_TOKEN"):
        value = clean_secret(os.getenv(name, ""))
        if value:
            return value
    raise RuntimeError("Secret Météo-France absent : METEOFRANCE_PACKAGE_OBS_KEY ou METEOFRANCE_OBS_TOKEN")


def get_obs_key(package_key: str) -> str:
    return clean_secret(os.getenv("METEOFRANCE_OBS_TOKEN", "")) or package_key


def api_headers(key: str) -> dict:
    return {"apikey": key, "accept": "*/*"}


def package_request(key: str, hour_utc: datetime) -> Optional[requests.Response]:
    hour_utc = hour_utc.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    last_status = None
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            response = session.get(
                PACKAGE_URL,
                params={"date": iso(hour_utc), "format": "csv"},
                headers=api_headers(key),
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt == MAX_HTTP_ATTEMPTS:
                raise RuntimeError(f"Erreur réseau paquet {iso(hour_utc)} : {exc}") from exc
            time.sleep(min(8, 2 ** attempt))
            continue
        last_status = response.status_code
        if response.status_code == 200:
            return response
        if response.status_code in (400, 404):
            return None
        if response.status_code in (408, 425, 429, 500, 502, 503, 504) and attempt < MAX_HTTP_ATTEMPTS:
            time.sleep(min(12, 2 ** attempt))
            continue
        if response.status_code == 401:
            raise RuntimeError("Package Observations : HTTP 401 (clé invalide ou expirée).")
        if response.status_code == 403:
            raise RuntimeError("Package Observations : HTTP 403 (accès non autorisé).")
        response.raise_for_status()
    raise RuntimeError(f"Paquet {iso(hour_utc)} indisponible, HTTP {last_status}")


def find_latest_package(key: str) -> Tuple[datetime, requests.Response]:
    now_hour = utcnow().replace(minute=0, second=0, microsecond=0)
    for offset in range(LATEST_PACKAGE_LOOKBACK_HOURS):
        hour = now_hour - timedelta(hours=offset)
        response = package_request(key, hour)
        if response is not None:
            return hour, response
        time.sleep(PACKAGE_DELAY)
    raise RuntimeError("Aucun paquet horaire disponible entre H et H-3.")


def simplify_package(rows: List[dict], fallback_hour: datetime) -> List[dict]:
    by_station: Dict[str, dict] = {}
    for row in rows:
        sid = station_id(row)
        if not sid or not is_metropole_station(sid):
            continue
        rr1 = rain_mm(first(row, ("rr1", "RR1")))
        if rr1 is None:
            continue
        validity = parse_iso(first(row, ("validity_time", "reference_time", "date"))) or fallback_hour
        lat = fnum(first(row, ("lat", "LAT", "latitude")))
        lon = fnum(first(row, ("lon", "LON", "longitude")))
        item = {
            "id": sid,
            "validity_time": iso(validity),
            "rr1": rr1,
        }
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            item["lat"] = round(lat, 6)
            item["lon"] = round(lon, 6)
        by_station[sid] = item
    return list(by_station.values())


def load_hour_cache() -> dict:
    if not CACHE.exists():
        return {"schema_version": 1, "hours": {}}
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("cache non objet")
        if not isinstance(data.get("hours"), dict):
            data["hours"] = {}
        return data
    except Exception as exc:
        print(f"::warning::Cache pluie 96 h illisible, recréé : {exc}")
        return {"schema_version": 1, "hours": {}}


def update_hour_cache(key: str) -> Tuple[dict, datetime]:
    cache = load_hour_cache()
    hours = cache.setdefault("hours", {})
    latest_hour, latest_response = find_latest_package(key)
    api_start = latest_hour - timedelta(hours=API_BACKFILL_HOURS - 1)
    targets: List[datetime] = []
    current = api_start
    while current <= latest_hour:
        k = iso(current)
        if k not in hours or current >= latest_hour - timedelta(hours=1):
            targets.append(current)
        current += timedelta(hours=1)

    for target in targets:
        print("Paquet pluie :", iso(target))
        response = latest_response if target == latest_hour else package_request(key, target)
        if target != latest_hour:
            time.sleep(PACKAGE_DELAY)
        if response is None:
            print("::warning::Paquet absent :", iso(target))
            continue
        simplified = simplify_package(parse_csv(response.content), target)
        hours[iso(target)] = simplified
        print("  stations RR1 métropole :", len(simplified))

    cutoff = latest_hour - timedelta(hours=CACHE_HOURS - 1)
    pruned = {}
    for key_hour, records in hours.items():
        dt = parse_iso(key_hour)
        if dt is not None and dt >= cutoff and isinstance(records, list):
            pruned[iso(dt.replace(minute=0, second=0, microsecond=0))] = records

    cache = {
        "schema_version": 1,
        "module_version": VERSION,
        "updated_at": iso(utcnow()),
        "latest_observation_at": iso(latest_hour),
        "hours": dict(sorted(pruned.items())),
    }
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return cache, latest_hour


def load_station_meta(key: str) -> Dict[str, dict]:
    print("Chargement liste-stations…")
    try:
        response = session.get(DPOBS_STATIONS_URL, headers=api_headers(key), timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        print("::warning::liste-stations indisponible :", exc)
        return {}
    if response.status_code != 200:
        print("::warning::liste-stations HTTP", response.status_code)
        return {}
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            payload = response.json()
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = payload.get("data") or payload.get("records") or payload.get("results") or []
            else:
                rows = []
        except Exception:
            rows = parse_csv(response.content)
    else:
        rows = parse_csv(response.content)

    out: Dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = station_id(row)
        if not sid or not is_metropole_station(sid):
            continue
        name = first(row, ("nom_usuel", "NOM_USUEL", "nom", "NOM", "name", "libelle"))
        name = str(name).strip() if name else sid
        if is_sapc(name):
            continue
        dep = department_code(sid)
        out[sid] = {
            "name": name,
            "department_code": dep,
            "department_name": DEPARTMENTS.get(dep or "", dep or ""),
        }
    print("Stations nommées :", len(out))
    return out


def department_candidates(sid: str) -> List[str]:
    digits = re.sub(r"\D", "", sid)
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
    return f"{MF_S3_MENS}/MENSQ_{dep}_latest-{now.year - 1}-{now.year}.csv.gz"


def download_rows(url: str) -> Optional[List[dict]]:
    try:
        response = session.get(url, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        print("[WARN] téléchargement climatologie :", exc)
        return None
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        print("[WARN]", response.status_code, url)
        return None
    try:
        return parse_csv(response.content)
    except Exception as exc:
        print("[WARN] CSV climatologie :", exc)
        return None


def load_record_cache() -> dict:
    if not RECORD_CACHE.exists():
        return {"schema_version": 1, "stations": {}}
    try:
        data = json.loads(RECORD_CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("stations", {})
        return data
    except Exception:
        return {"schema_version": 1, "stations": {}}


def age_hours(value: Any) -> float:
    dt = parse_iso(value)
    if dt is None:
        return 999999.0
    return (utcnow() - dt).total_seconds() / 3600.0


def update_record(current_value: Optional[float], current_date: Optional[str],
                  candidate_value: Optional[float], candidate_date: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    if candidate_value is None:
        return current_value, current_date
    if current_value is None or candidate_value > current_value + RECORD_EPSILON:
        return round(candidate_value, 1), candidate_date
    if abs(candidate_value - current_value) <= RECORD_EPSILON:
        if candidate_date and (not current_date or candidate_date < current_date):
            return current_value, candidate_date
    return current_value, current_date


def build_historical_records(cache: dict, station_ids: List[str], now: datetime) -> None:
    current_ym = now.year * 100 + now.month
    month_id = f"{now.year:04d}-{now.month:02d}"
    stations_cache = cache.setdefault("stations", {})
    known = sum(1 for sid in station_ids if stations_cache.get(sid, {}).get("record_absolute_old") is not None)
    coverage = known / max(1, len(station_ids))
    stale = (
        cache.get("record_baseline_month_id") != month_id
        or age_hours(cache.get("records_generated_at")) >= HISTORICAL_RECORD_CACHE_DAYS * 24
        or coverage < 0.70
    )
    if not stale:
        print("Cache records pluie historique valide.")
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
            print("Records pluie", dep, ":", len(rows), "lignes")
            for row in rows:
                sid = station_id(row)
                if sid not in dep_ids:
                    continue
                ym = ym_int(first(row, ("AAAAMM", "DATE", "date")))
                if ym is None or ym >= current_ym:
                    continue
                rrab = rain_mm(first(row, ("RRAB", "rrab")))
                if rrab is None or not quality_ok(first(row, ("QRRAB", "qrrab"))):
                    continue
                key = (sid, ym)
                if key in seen:
                    continue
                seen.add(key)
                rrdate = month_day_date(ym, first(row, ("RRDAT", "rrdat")))
                abs_val[sid], abs_date[sid] = update_record(abs_val[sid], abs_date[sid], rrab, rrdate)
                if ym % 100 == now.month:
                    month_val[sid], month_date[sid] = update_record(
                        month_val[sid], month_date[sid], rrab, rrdate
                    )

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})
        entry["record_month_old"] = month_val[sid]
        entry["record_month_old_date"] = month_date[sid]
        entry["record_absolute_old"] = abs_val[sid]
        entry["record_absolute_old_date"] = abs_date[sid]

    cache["record_baseline_month_id"] = month_id
    cache["records_generated_at"] = iso(utcnow())


def update_current_month_records(cache: dict, station_ids: List[str], now: datetime) -> None:
    month_id = f"{now.year:04d}-{now.month:02d}"
    current_ym = now.year * 100 + now.month
    stale = (
        cache.get("current_month_id") != month_id
        or age_hours(cache.get("current_month_generated_at")) >= CURRENT_RECORD_REFRESH_HOURS
    )
    if not stale:
        return

    wanted = defaultdict(set)
    for sid in station_ids:
        for dep in department_candidates(sid):
            wanted[dep].add(sid)

    current_val = {sid: None for sid in station_ids}
    current_date = {sid: None for sid in station_ids}

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
            rrab = rain_mm(first(row, ("RRAB", "rrab")))
            if rrab is None or not quality_ok(first(row, ("QRRAB", "qrrab"))):
                continue
            rrdate = month_day_date(ym, first(row, ("RRDAT", "rrdat")))
            current_val[sid], current_date[sid] = update_record(
                current_val[sid], current_date[sid], rrab, rrdate
            )

    stations_cache = cache.setdefault("stations", {})
    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})
        entry["current_month_rrab"] = current_val[sid]
        entry["current_month_rrab_date"] = current_date[sid]
    cache["current_month_id"] = month_id
    cache["current_month_generated_at"] = iso(utcnow())


def prepare_records(station_ids: List[str], latest: datetime) -> dict:
    cache = load_record_cache()
    build_historical_records(cache, station_ids, latest)
    update_current_month_records(cache, station_ids, latest)
    cache["schema_version"] = 1
    cache["module_version"] = VERSION
    cache["updated_at"] = iso(utcnow())
    RECORD_CACHE.write_text(
        json.dumps(cache, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    return cache


def hour_records(cache: dict) -> Dict[datetime, List[dict]]:
    out: Dict[datetime, List[dict]] = {}
    for key, rows in (cache.get("hours") or {}).items():
        dt = parse_iso(key)
        if dt is not None and isinstance(rows, list):
            out[dt.replace(minute=0, second=0, microsecond=0)] = rows
    return out


def local_label(dt: datetime) -> str:
    loc = dt.astimezone(PARIS)
    return f"{WEEKDAYS_FR[loc.weekday()]} {loc.day} {MONTHS_FR[loc.month - 1]} {loc.year} à {loc:%H:%M}"


def compact_local(dt: datetime) -> str:
    loc = dt.astimezone(PARIS)
    return f"{loc.day:02d}/{loc.month:02d}/{loc.year} {loc:%H:%M}"


def period_label(start: datetime, end: datetime) -> str:
    return f"{start:%d/%m/%Y %H:%M} UTC → {end:%d/%m/%Y %H:%M} UTC"


def last_06_boundary(hour: datetime) -> datetime:
    candidate = hour.replace(hour=6, minute=0, second=0, microsecond=0)
    if candidate > hour:
        candidate -= timedelta(days=1)
    return candidate


def combined_record(entry: dict, month: int) -> Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    month_val = fnum(entry.get("record_month_old"))
    month_date = entry.get("record_month_old_date")
    current_val = fnum(entry.get("current_month_rrab"))
    current_date = entry.get("current_month_rrab_date")
    if current_val is not None:
        month_val, month_date = update_record(month_val, month_date, current_val, current_date)

    abs_val = fnum(entry.get("record_absolute_old"))
    abs_date = entry.get("record_absolute_old_date")
    if current_val is not None:
        abs_val, abs_date = update_record(abs_val, abs_date, current_val, current_date)
    return month_val, month_date, abs_val, abs_date


def record_status(value: float, record: Optional[float]) -> Tuple[Optional[str], Optional[float]]:
    if record is None:
        return None, None
    delta = round(value - record, 1)
    if value > record + RECORD_EPSILON:
        return "battu", delta
    if abs(value - record) <= RECORD_EPSILON:
        return "egale", 0.0
    return None, delta


def build_table(cache_hours: Dict[datetime, List[dict]], meta: Dict[str, dict],
                start: datetime, end: datetime, label: str, expected: int,
                minimum_samples: int, record_cache: Optional[dict] = None) -> dict:
    selected_hours = sorted(dt for dt in cache_hours if start < dt <= end)
    by_station: Dict[str, dict] = {}

    for dt in selected_hours:
        for obs in cache_hours[dt]:
            sid = str(obs.get("id") or "").strip()
            if not sid or not is_metropole_station(sid):
                continue
            info = meta.get(sid) or {}
            name = info.get("name") or sid
            if is_sapc(name):
                continue
            rr = rain_mm(obs.get("rr1"))
            if rr is None:
                continue
            item = by_station.setdefault(sid, {
                "sum": 0.0,
                "samples": 0,
                "lat": fnum(obs.get("lat")),
                "lon": fnum(obs.get("lon")),
                "last_dt": dt,
            })
            item["sum"] += rr
            item["samples"] += 1
            if dt > item["last_dt"]:
                item["last_dt"] = dt
            if item.get("lat") is None:
                item["lat"] = fnum(obs.get("lat"))
            if item.get("lon") is None:
                item["lon"] = fnum(obs.get("lon"))

    rows = []
    record_stations = (record_cache or {}).get("stations") or {}
    for sid, item in by_station.items():
        if item["samples"] < minimum_samples:
            continue
        info = meta.get(sid) or {}
        dep = info.get("department_code") or department_code(sid)
        name = info.get("name") or sid
        value = round(item["sum"], 1)
        row = {
            "id": sid,
            "name": name,
            "department_code": dep,
            "department_name": info.get("department_name") or DEPARTMENTS.get(dep or "", dep or ""),
            "value": value,
            "samples": int(item["samples"]),
            "expected_samples": int(expected),
            "complete": item["samples"] >= expected,
            "obs_time_utc": iso(item["last_dt"]),
            "obs_time_local": compact_local(item["last_dt"]),
        }
        if item.get("lat") is not None:
            row["lat"] = item["lat"]
        if item.get("lon") is not None:
            row["lon"] = item["lon"]

        if record_cache is not None:
            rec = record_stations.get(sid) or {}
            month_val, month_date, abs_val, abs_date = combined_record(rec, end.month)
            month_status, month_delta = record_status(value, month_val)
            abs_status, abs_delta = record_status(value, abs_val)
            row.update({
                "record_month": month_val,
                "record_month_date": format_record_date(month_date),
                "record_month_status": month_status,
                "record_month_delta": month_delta,
                "record_absolute": abs_val,
                "record_absolute_date": format_record_date(abs_date),
                "record_absolute_status": abs_status,
                "record_absolute_delta": abs_delta,
            })
        rows.append(row)

    rows.sort(key=lambda r: (-r["value"], r["name"], r["id"]))
    values = [r["value"] for r in rows]
    return {
        "label": label,
        "period_start_utc": iso(start),
        "period_end_utc": iso(end),
        "period_label": period_label(start, end),
        "coverage_hours": len(selected_hours),
        "expected_hours": expected,
        "minimum_station_samples": minimum_samples,
        "complete": len(selected_hours) >= expected,
        "stations": len(rows),
        "max": round(max(values), 1) if values else None,
        "min": round(min(values), 1) if values else None,
        "rows": rows,
    }


def minimum_samples(expected: int) -> int:
    if expected <= 0:
        return 0
    if expected <= 3:
        return expected
    return max(1, int(math.ceil(expected * 0.90)))


def build_tables(cache: dict, meta: Dict[str, dict], latest: datetime, record_cache: dict) -> dict:
    hours = hour_records(cache)

    def rolling(n: int, key: str, label: str) -> Tuple[str, dict]:
        start = latest - timedelta(hours=n)
        table = build_table(hours, meta, start, latest, label, n, minimum_samples(n))
        return key, table

    start_1 = latest - timedelta(hours=1)
    one_hour = build_table(hours, meta, start_1, latest, "Classement pluie 1 h", 1, 1)

    since6_start = last_06_boundary(latest)
    since6_expected = int(round((latest - since6_start).total_seconds() / 3600))
    since6 = build_table(
        hours, meta, since6_start, latest,
        "Classement pluie depuis 06 UTC + records",
        since6_expected,
        minimum_samples(since6_expected),
        record_cache=record_cache,
    )
    since6["provisional"] = True
    since6["record_reference_period"] = "RR quotidien Météo-France 06–06 UTC"
    since6["target_period_end_utc"] = iso(since6_start + timedelta(hours=24))

    tables = {
        "rain_1h": one_hour,
        "rain_24h": rolling(24, "rain_24h", "Classement pluie 24 h glissantes")[1],
        "rain_since_06": since6,
        "rain_48h": rolling(48, "rain_48h", "Classement pluie 48 h glissantes")[1],
        "rain_72h": rolling(72, "rain_72h", "Classement pluie 72 h glissantes")[1],
    }
    return tables


def main() -> int:
    print(f"=== Classements pluie v{VERSION} ===")
    package_key = get_package_key()
    obs_key = get_obs_key(package_key)
    cache, latest = update_hour_cache(package_key)
    meta = load_station_meta(obs_key)

    station_ids = sorted({
        str(r.get("id") or "").strip()
        for rows in (cache.get("hours") or {}).values()
        if isinstance(rows, list)
        for r in rows
        if r.get("id") and is_metropole_station(str(r.get("id")))
    })
    record_cache = prepare_records(station_ids, latest)
    tables = build_tables(cache, meta, latest, record_cache)

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "status": "ok",
        "generated_at": iso(utcnow()),
        "latest_observation_at": iso(latest),
        "latest_observation_local": local_label(latest),
        "timezone_local": "Europe/Paris",
        "scope": "France métropolitaine",
        "unit": "mm",
        "title": "Classements des précipitations en France",
        "source": {
            "provider": "Météo-France",
            "api": "DPPaquetObs v2 - paquets horaires stations",
            "field": "rr1",
            "rr1_unit": "kg/m² = mm",
            "hourly_retention": "24 h côté API ; cache local glissant 96 h",
            "daily_record_field": "RRAB / RRDAT des données climatologiques mensuelles",
            "daily_record_period": "06 UTC → 06 UTC",
        },
        "tables": tables,
        "cache": {
            "hours_retained": len(cache.get("hours") or {}),
            "target_hours": CACHE_HOURS,
        },
        "records": {
            "baseline_month_id": record_cache.get("record_baseline_month_id"),
            "historical_generated_at": record_cache.get("records_generated_at"),
            "current_month_generated_at": record_cache.get("current_month_generated_at"),
        },
    }

    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )

    print("=== TABLEAUX PLUIE ===")
    for key, table in tables.items():
        print(
            key,
            ":",
            table.get("stations"),
            "station(s), max =",
            table.get("max"),
            "mm, couverture =",
            f"{table.get('coverage_hours')}/{table.get('expected_hours')} h",
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

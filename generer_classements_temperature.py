#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Classements et observations France
Version 1.1.0

Produit un JSON pour les tableaux de températures et les paramètres
atmosphériques issus des paquets horaires Météo-France :
1. Classement températures par heure locale
2. Tn provisoire
3. Tn 18-06 UTC
4. Tn 06-18 UTC
5. Tn finales (18 UTC J-1 -> 18 UTC J)
6. Tx provisoire
7. Tx 06-18 UTC
8. Tx 18-06 UTC
9. Tx finales (06 UTC J -> 06 UTC J+1)

Les observations horaires Météo-France n'étant conservées que 24 h côté API,
le workflow maintient un cache glissant 72 h sur la branche observations.
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
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

VERSION = "1.1.0"
SCHEMA_VERSION = 2
BUILD_ID = "classements-observations-mf-complet-20260821"

PACKAGE_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)
DPOBS_STATIONS_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPObs/v2/liste-stations"
)

OUTPUT = Path("classements_temperature.json")
CACHE = Path("cache_classements_temperature_72h.json")
QUALITY_CACHE = Path("cache_qualite_sites_mf.json")
CLIMATE_CACHE = Path("cache_normales_records_mf.json")

QUALITY_PDF_URL = (
    "https://donneespubliques.meteofrance.fr/"
    "metadonnees_publiques/fiches/fiche_{station_id}.pdf"
)
CLIMATE_DATA_URL = (
    "https://donneespubliques.meteofrance.fr/"
    "FichesClim/FICHECLIM_{station_id}.data"
)

PARIS = ZoneInfo("Europe/Paris")
HTTP_TIMEOUT = 90
PACKAGE_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.15"))
MAX_HTTP_ATTEMPTS = 4
CACHE_HOURS = 72
API_BACKFILL_HOURS = 24
HOURLY_TABLE_HOURS = 24
LATEST_PACKAGE_LOOKBACK_HOURS = 4
METADATA_BATCH_SIZE = max(0, int(os.getenv("MF_METADATA_BATCH_SIZE", "60")))
METADATA_DELAY = max(0.0, float(os.getenv("MF_METADATA_DELAY", "0.10")))

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-classements-temperature/{VERSION}",
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


def celsius(value: Any) -> Optional[float]:
    n = fnum(value)
    if n is None:
        return None
    if n > 100:
        n -= 273.15
    if n < -100 or n > 70:
        return None
    return round(n, 1)


def pressure_hpa(value: Any) -> Optional[float]:
    n = fnum(value)
    if n is None:
        return None
    if n > 2000:
        n /= 100.0
    if n < 700 or n > 1150:
        return None
    return round(n, 1)


def percent(value: Any) -> Optional[float]:
    n = fnum(value)
    if n is None or n < 0 or n > 100:
        return None
    return round(n, 1)


def speed_kmh(value: Any) -> Optional[float]:
    n = fnum(value)
    if n is None or n < 0 or n > 150:
        return None
    return round(n * 3.6, 1)


def visibility_km(value: Any) -> Optional[float]:
    n = fnum(value)
    if n is None or n < 0:
        return None
    if n > 200:
        n /= 1000.0
    return round(n, 1)


def snow_depth_cm(value: Any) -> Optional[float]:
    n = fnum(value)
    if n is None or n < 0:
        return None
    # ht_neige est diffusée en mètres dans les paquets SYNOP.
    if n <= 20:
        n *= 100.0
    if n > 2000:
        return None
    return round(n, 1)


def sunshine_minutes(row: dict) -> Optional[float]:
    raw_fraction = first(row, ("ssfrai", "SSFRAl", "sunshine_fraction"))
    fraction = fnum(raw_fraction)
    if fraction is not None:
        if 0 <= fraction <= 1:
            return round(fraction * 60.0, 1)
        if 0 <= fraction <= 100:
            return round(fraction * 0.6, 1)

    raw_duration = first(row, (
        "insolh", "INSOLH", "insolation", "sunshine_duration",
        "duree_insolation", "sunshine_minutes",
    ))
    duration = fnum(raw_duration)
    if duration is None or duration < 0:
        return None
    if duration <= 1:
        duration *= 60.0
    elif duration > 60:
        duration /= 60.0
    return round(min(duration, 60.0), 1)


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
    if sid.isdigit() and len(sid) <= 8:
        sid = sid.zfill(8)
    return sid or None


def department_code(sid: str) -> Optional[str]:
    s = str(sid).strip().upper()
    digits = re.sub(r"\D", "", s)
    if len(digits) < 2:
        return None
    code = digits[:2]
    if code in DEPARTMENTS:
        return code
    return None


def is_metropole_station(sid: str) -> bool:
    return department_code(sid) is not None


def is_sapc(name: str) -> bool:
    words = re.sub(r"[^A-Z0-9]+", " ", str(name).upper()).split()
    return "SAPC" in words


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


def get_api_key() -> str:
    for name in ("METEOFRANCE_PACKAGE_OBS_KEY", "METEOFRANCE_OBS_TOKEN"):
        value = os.getenv(name, "").strip()
        if not value:
            continue
        value = value.replace("\r", "").replace("\n", "").strip()
        for prefix in ("apikey:", "apiKey:", "Bearer ", "bearer "):
            if value.startswith(prefix):
                value = value[len(prefix):].strip()
        if value:
            return value
    raise RuntimeError("Secret Météo-France absent : METEOFRANCE_PACKAGE_OBS_KEY ou METEOFRANCE_OBS_TOKEN")


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
        if response.status_code in (408, 425, 429, 500, 502, 503, 504):
            if attempt < MAX_HTTP_ATTEMPTS:
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

        validity = parse_iso(first(row, ("validity_time", "reference_time", "date"))) or fallback_hour
        t = celsius(first(row, ("t", "T")))
        tn = celsius(first(row, ("tn", "TN")))
        tx = celsius(first(row, ("tx", "TX")))
        td = celsius(first(row, ("td", "TD", "dew_point", "point_rosee")))
        humidity = percent(first(row, ("u", "U", "humidity", "humidite")))
        pmer = pressure_hpa(first(row, ("pmer", "PMER", "pressure_msl")))
        pressure = pressure_hpa(first(row, ("pres", "PRES", "pressure")))
        wind_speed = speed_kmh(first(row, ("ff", "FF", "wind_speed")))
        wind_direction = fnum(first(row, ("dd", "DD", "wind_direction")))
        visibility = visibility_km(first(row, ("vv", "VV", "visibility")))
        snow_depth = snow_depth_cm(first(row, (
            "ht_neige", "HT_NEIGE", "snow_depth", "hauteur_neige",
        )))
        sunshine = sunshine_minutes(row)

        if all(value is None for value in (
            t, tn, tx, td, humidity, pmer, pressure, wind_speed,
            visibility, snow_depth, sunshine,
        )):
            continue

        lat = fnum(first(row, ("lat", "LAT", "latitude")))
        lon = fnum(first(row, ("lon", "LON", "longitude")))

        item = {
            "id": sid,
            "validity_time": iso(validity),
            "t": t,
            "tn": tn,
            "tx": tx,
            "dew_point": td,
            "humidity": humidity,
            "pressure_msl": pmer,
            "pressure": pressure,
            "wind_speed": wind_speed,
            "wind_direction": (
                round(wind_direction, 0)
                if wind_direction is not None and 0 <= wind_direction <= 360
                else None
            ),
            "visibility_km": visibility,
            "snow_depth_cm": snow_depth,
            "sunshine_1h_minutes": sunshine,
        }
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            item["lat"] = round(lat, 6)
            item["lon"] = round(lon, 6)
        by_station[sid] = item

    return list(by_station.values())


def load_cache() -> dict:
    if not CACHE.exists():
        return {"schema_version": SCHEMA_VERSION, "hours": {}}
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"schema_version": SCHEMA_VERSION, "hours": {}}
        if int(data.get("schema_version") or 0) < SCHEMA_VERSION:
            print("Cache ancien : reconstruction des 24 dernières heures.")
            return {"schema_version": SCHEMA_VERSION, "hours": {}}
        if not isinstance(data.get("hours"), dict):
            data["hours"] = {}
        return data
    except Exception as exc:
        print(f"::warning::Cache illisible, recréé : {exc}")
        return {"schema_version": SCHEMA_VERSION, "hours": {}}


def update_cache(key: str) -> Tuple[dict, datetime]:
    cache = load_cache()
    hours = cache.setdefault("hours", {})
    latest_hour, latest_response = find_latest_package(key)

    # L'API ne garantit que 24 h de rétention. On complète tout ce qui manque
    # dans cette fenêtre et on rafraîchit les 2 dernières heures à chaque run.
    api_start = latest_hour - timedelta(hours=API_BACKFILL_HOURS - 1)
    targets: List[datetime] = []
    current = api_start
    while current <= latest_hour:
        k = iso(current)
        if k not in hours or current >= latest_hour - timedelta(hours=1):
            targets.append(current)
        current += timedelta(hours=1)

    for target in targets:
        print("Paquet :", iso(target))
        if target == latest_hour:
            response = latest_response
        else:
            response = package_request(key, target)
            time.sleep(PACKAGE_DELAY)
        if response is None:
            print("::warning::Paquet absent :", iso(target))
            continue
        rows = parse_csv(response.content)
        simplified = simplify_package(rows, target)
        hours[iso(target)] = simplified
        print("  stations métropole :", len(simplified))

    cutoff = latest_hour - timedelta(hours=CACHE_HOURS)
    pruned = {}
    for key_hour, records in hours.items():
        dt = parse_iso(key_hour)
        if dt is not None and dt >= cutoff:
            pruned[iso(dt.replace(minute=0, second=0, microsecond=0))] = records

    cache = {
        "schema_version": SCHEMA_VERSION,
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
        altitude = fnum(first(row, (
            "altitude", "ALTITUDE", "alt", "elevation", "altitude_m",
        )))
        out[sid] = {
            "name": name,
            "department_code": dep,
            "department_name": DEPARTMENTS.get(dep or "", dep or ""),
            "altitude_m": round(altitude, 0) if altitude is not None else None,
        }
    print("Stations nommées :", len(out))
    return out


def load_json_cache(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "stations": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("stations"), dict):
            raise ValueError("structure incorrecte")
        return payload
    except Exception as exc:
        print(f"::warning::{path.name} illisible, recréé : {exc}")
        return {"schema_version": 1, "stations": {}}


def save_json_cache(path: Path, stations: dict) -> None:
    payload = {
        "schema_version": 1,
        "module_version": VERSION,
        "updated_at": iso(utcnow()),
        "stations": stations,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )


def ascii_text(value: Any) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFD", str(value or ""))
        if unicodedata.category(char) != "Mn"
    ).lower()


def climate_series(lines: List[str], heading: str) -> List[Optional[float]]:
    wanted = ascii_text(heading)
    for index, line in enumerate(lines):
        if wanted not in ascii_text(line):
            continue
        for candidate in lines[index + 1:index + 7]:
            parts = [part.strip() for part in candidate.split(";")]
            values = parts[1:14] if len(parts) >= 14 else []
            if len(values) != 13:
                continue
            parsed: List[Optional[float]] = []
            for value in values:
                if value == ".":
                    parsed.append(0.0)
                elif value in ("", "-"):
                    parsed.append(None)
                else:
                    parsed.append(fnum(value))
            if sum(value is not None for value in parsed) >= 6:
                return parsed
    return []


def parse_climate_data(text: str) -> dict:
    lines = text.replace("\r", "").split("\n")
    high = climate_series(lines, "La température la plus élevée")
    normal_max = climate_series(lines, "Température maximale (Moyenne")
    normal_min = climate_series(lines, "Température minimale (Moyenne")
    low = climate_series(lines, "La température la plus basse")
    if not any((high, normal_max, normal_min, low)):
        return {}
    return {
        "record_tmax_monthly": high[:12] if high else [],
        "record_tmax_absolute": high[12] if len(high) >= 13 else None,
        "normal_tmax_monthly": normal_max[:12] if normal_max else [],
        "normal_tmin_monthly": normal_min[:12] if normal_min else [],
        "record_tmin_monthly": low[:12] if low else [],
        "record_tmin_absolute": low[12] if len(low) >= 13 else None,
    }


def parse_quality_text(text: str) -> dict:
    # La fiche commence par le tableau « QUALITE DU SITE ». On s'arrête avant
    # « CLASSE MESURES » afin de ne pas confondre classe de site et classe du
    # capteur, qui sont deux notions différentes chez Météo-France.
    normalized = text.replace("\r", "")
    upper = ascii_text(normalized)
    start = upper.find("qualite du site")
    if start >= 0:
        normalized = normalized[start:]
        upper = upper[start:]
    stop = upper.find("classe mesures")
    if stop >= 0:
        normalized = normalized[:stop]

    aliases = {
        "humidite": "humidity",
        "pluie": "rain",
        "ray_glo_diff": "radiation",
        "rayonnement": "radiation",
        "temperature": "temperature",
        "vent": "wind",
    }
    output = {}
    for raw_line in normalized.split("\n"):
        line = ascii_text(raw_line).strip()
        match = re.match(
            r"^(humidite|pluie|ray_glo_diff|rayonnement|temperature|vent)\s+([1-5])\b",
            line,
        )
        if not match:
            continue
        key = aliases[match.group(1)]
        if key not in output:
            output[key] = int(match.group(2))
    return output


def metadata_is_due(item: Any, max_age_days: int = 180) -> bool:
    if not isinstance(item, dict):
        return True
    checked = parse_iso(item.get("checked_at"))
    return checked is None or checked < utcnow() - timedelta(days=max_age_days)


def decode_response_text(response: requests.Response) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return response.content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return response.content.decode("utf-8", errors="replace")


def enrich_official_metadata(meta: Dict[str, dict], latest: datetime) -> Dict[str, dict]:
    quality_cache = load_json_cache(QUALITY_CACHE)
    climate_cache = load_json_cache(CLIMATE_CACHE)
    quality_items = quality_cache["stations"]
    climate_items = climate_cache["stations"]

    candidates = [
        sid for sid in sorted(meta)
        if metadata_is_due(quality_items.get(sid))
        or metadata_is_due(climate_items.get(sid))
    ][:METADATA_BATCH_SIZE]

    if candidates:
        print("Métadonnées officielles à compléter :", len(candidates))

    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
        if candidates:
            print("::warning::pypdf absent : classes de qualité non actualisées.")

    for index, sid in enumerate(candidates, start=1):
        checked_at = iso(utcnow())

        if metadata_is_due(climate_items.get(sid)):
            try:
                response = session.get(
                    CLIMATE_DATA_URL.format(station_id=sid),
                    timeout=HTTP_TIMEOUT,
                )
                climate = parse_climate_data(decode_response_text(response)) if response.status_code == 200 else {}
                climate_items[sid] = {
                    "checked_at": checked_at,
                    "status": "ok" if climate else "unavailable",
                    "data": climate,
                }
            except requests.RequestException as exc:
                print(f"::warning::Fiche climatologique {sid} : {exc}")

        if PdfReader is not None and metadata_is_due(quality_items.get(sid)):
            try:
                response = session.get(
                    QUALITY_PDF_URL.format(station_id=sid),
                    timeout=HTTP_TIMEOUT,
                )
                quality = {}
                if response.status_code == 200 and response.content.startswith(b"%PDF"):
                    reader = PdfReader(io.BytesIO(response.content))
                    pages = []
                    for page in reader.pages[:3]:
                        pages.append(page.extract_text() or "")
                    quality = parse_quality_text("\n".join(pages))
                quality_items[sid] = {
                    "checked_at": checked_at,
                    "status": "ok" if quality else "unavailable",
                    "site_quality": quality,
                }
            except Exception as exc:
                print(f"::warning::Qualité du site {sid} : {exc}")

        if METADATA_DELAY and index < len(candidates):
            time.sleep(METADATA_DELAY)

    save_json_cache(QUALITY_CACHE, quality_items)
    save_json_cache(CLIMATE_CACHE, climate_items)

    month_index = latest.month - 1
    for sid, info in meta.items():
        quality = quality_items.get(sid) or {}
        if quality.get("site_quality"):
            info["site_quality"] = quality["site_quality"]

        climate_entry = climate_items.get(sid) or {}
        climate = climate_entry.get("data") or {}
        if climate:
            def month_value(key: str) -> Optional[float]:
                values = climate.get(key) or []
                return values[month_index] if len(values) > month_index else None

            info["climate"] = {
                "normal_tmax_month": month_value("normal_tmax_monthly"),
                "normal_tmin_month": month_value("normal_tmin_monthly"),
                "record_month_tmax": month_value("record_tmax_monthly"),
                "record_month_tmin": month_value("record_tmin_monthly"),
                "record_absolute_tmax": climate.get("record_tmax_absolute"),
                "record_absolute_tmin": climate.get("record_tmin_absolute"),
                "reference_period": "1991-2020",
            }

    print(
        "Qualités disponibles :",
        sum(bool((quality_items.get(sid) or {}).get("site_quality")) for sid in meta),
    )
    print(
        "Normales disponibles :",
        sum(bool((climate_items.get(sid) or {}).get("data")) for sid in meta),
    )
    return meta


def local_dt_label(dt: datetime) -> str:
    loc = dt.astimezone(PARIS)
    return f"{WEEKDAYS_FR[loc.weekday()]} {loc.day} {MONTHS_FR[loc.month - 1]} {loc.year} à {loc:%H:%M}"


def compact_date_label(dt: datetime) -> str:
    loc = dt.astimezone(PARIS)
    return f"{loc.day:02d}/{loc.month:02d}/{loc.year} {loc:%H:%M}"


def period_label(start: datetime, end: datetime, provisional: bool = False) -> str:
    suffix = " (en cours)" if provisional else ""
    return f"{start:%d/%m/%Y %H:%M} UTC → {end:%d/%m/%Y %H:%M} UTC{suffix}"


def last_boundary(hour: datetime, boundary_hour: int) -> datetime:
    candidate = hour.replace(hour=boundary_hour, minute=0, second=0, microsecond=0)
    if candidate > hour:
        candidate -= timedelta(days=1)
    return candidate


def next_boundary(hour: datetime, boundary_hour: int) -> datetime:
    candidate = hour.replace(hour=boundary_hour, minute=0, second=0, microsecond=0)
    if candidate <= hour:
        candidate += timedelta(days=1)
    return candidate


def hour_records(cache: dict) -> Dict[datetime, List[dict]]:
    out: Dict[datetime, List[dict]] = {}
    for key, rows in (cache.get("hours") or {}).items():
        dt = parse_iso(key)
        if dt is not None and isinstance(rows, list):
            out[dt.replace(minute=0, second=0, microsecond=0)] = rows
    return out


def enrich_row(sid: str, value: float, obs_dt: datetime, samples: int, expected: int,
               meta: Dict[str, dict], lat: Optional[float] = None, lon: Optional[float] = None) -> dict:
    info = meta.get(sid) or {}
    dep = info.get("department_code") or department_code(sid)
    name = info.get("name") or sid
    if is_sapc(name):
        return {}
    row = {
        "id": sid,
        "name": name,
        "department_code": dep,
        "department_name": info.get("department_name") or DEPARTMENTS.get(dep or "", dep or ""),
        "network": "Météo-France",
        "altitude_m": info.get("altitude_m"),
        "site_quality": info.get("site_quality") or {},
        "value": round(float(value), 1),
        "obs_time_utc": iso(obs_dt),
        "obs_time_local": compact_date_label(obs_dt),
        "samples": int(samples),
        "expected_samples": int(expected),
    }
    if lat is not None:
        row["lat"] = lat
    if lon is not None:
        row["lon"] = lon
    for key, value in (info.get("climate") or {}).items():
        if value is not None:
            row[key] = value
    return row


def build_extreme_table(cache_hours: Dict[datetime, List[dict]], meta: Dict[str, dict],
                        start: datetime, end: datetime, mode: str,
                        label: str, provisional: bool = False) -> dict:
    # Les extrêmes horaires TN/TX se rapportent à l'heure écoulée : on retient
    # start < validity_time <= end, soit exactement 12 ou 24 tranches horaires.
    selected_hours = sorted(dt for dt in cache_hours if start < dt <= end)
    expected = max(1, int(round((end - start).total_seconds() / 3600)))

    by_station: Dict[str, dict] = {}
    for dt in selected_hours:
        for obs in cache_hours[dt]:
            sid = str(obs.get("id") or "").strip()
            if not sid or not is_metropole_station(sid):
                continue
            if sid in meta and is_sapc(meta[sid].get("name", "")):
                continue

            if mode == "min":
                candidate = fnum(obs.get("tn"))
                if candidate is None:
                    candidate = fnum(obs.get("t"))
            else:
                candidate = fnum(obs.get("tx"))
                if candidate is None:
                    candidate = fnum(obs.get("t"))
            if candidate is None:
                continue

            item = by_station.setdefault(sid, {
                "value": candidate,
                "dt": dt,
                "samples": 0,
                "lat": fnum(obs.get("lat")),
                "lon": fnum(obs.get("lon")),
            })
            item["samples"] += 1
            if item.get("lat") is None:
                item["lat"] = fnum(obs.get("lat"))
            if item.get("lon") is None:
                item["lon"] = fnum(obs.get("lon"))

            better = candidate < item["value"] if mode == "min" else candidate > item["value"]
            if better:
                item["value"] = candidate
                item["dt"] = dt

    rows = []
    for sid, item in by_station.items():
        row = enrich_row(
            sid, item["value"], item["dt"], item["samples"], expected,
            meta, item.get("lat"), item.get("lon")
        )
        if row:
            rows.append(row)

    reverse = mode == "max"
    rows.sort(key=lambda r: ((-r["value"]) if reverse else r["value"], r["name"], r["id"]))

    values = [r["value"] for r in rows]
    coverage = len(selected_hours)
    return {
        "label": label,
        "mode": mode,
        "provisional": bool(provisional),
        "period_start_utc": iso(start),
        "period_end_utc": iso(end),
        "period_label": period_label(start, end, provisional),
        "coverage_hours": coverage,
        "expected_hours": expected,
        "complete": coverage >= expected,
        "stations": len(rows),
        "min": round(min(values), 1) if values else None,
        "max": round(max(values), 1) if values else None,
        "rows": rows,
    }


def build_hourly_tables(cache_hours: Dict[datetime, List[dict]], meta: Dict[str, dict], latest: datetime) -> List[dict]:
    hours = sorted((dt for dt in cache_hours if dt <= latest), reverse=True)[:HOURLY_TABLE_HOURS]
    output = []
    for dt in hours:
        rows = []
        for obs in cache_hours[dt]:
            sid = str(obs.get("id") or "").strip()
            if not sid or not is_metropole_station(sid):
                continue
            t = fnum(obs.get("t"))
            if t is None:
                continue
            info = meta.get(sid) or {}
            name = info.get("name") or sid
            if is_sapc(name):
                continue
            row = enrich_row(
                sid, t, dt, 1, 1, meta,
                fnum(obs.get("lat")), fnum(obs.get("lon"))
            )
            if row:
                for key in (
                    "dew_point", "humidity", "pressure_msl", "pressure",
                    "wind_speed", "wind_direction", "visibility_km",
                    "snow_depth_cm", "sunshine_1h_minutes",
                ):
                    value = obs.get(key)
                    if value is not None:
                        row[key] = value
                rows.append(row)
        rows.sort(key=lambda r: (-r["value"], r["name"], r["id"]))
        vals = [r["value"] for r in rows]
        output.append({
            "utc": iso(dt),
            "local": dt.astimezone(PARIS).isoformat(timespec="minutes"),
            "local_label": local_dt_label(dt),
            "stations": len(rows),
            "min": round(min(vals), 1) if vals else None,
            "max": round(max(vals), 1) if vals else None,
            "rows": rows,
        })
    return output


def build_tables(cache: dict, meta: Dict[str, dict], latest: datetime) -> Tuple[dict, List[dict]]:
    hours = hour_records(cache)

    # Tn provisoire : fenêtre OMM en cours, depuis le dernier 18 UTC.
    tn_start = last_boundary(latest, 18)
    tn_end_boundary = next_boundary(tn_start, 18)
    tn_prov = build_extreme_table(
        hours, meta, tn_start, latest, "min",
        "Températures minimales provisoires (Tn provisoire)", True
    )
    tn_prov["target_period_end_utc"] = iso(tn_end_boundary)

    # Tx provisoire : fenêtre climatologique en cours, depuis le dernier 06 UTC.
    tx_start = last_boundary(latest, 6)
    tx_end_boundary = next_boundary(tx_start, 6)
    tx_prov = build_extreme_table(
        hours, meta, tx_start, latest, "max",
        "Températures maximales provisoires (Tx provisoire)", True
    )
    tx_prov["target_period_end_utc"] = iso(tx_end_boundary)

    end06 = last_boundary(latest, 6)
    end18 = last_boundary(latest, 18)

    tables = {
        "tn_provisoire": tn_prov,
        "tn_18_06": build_extreme_table(
            hours, meta, end06 - timedelta(hours=12), end06, "min",
            "Températures minimales 18–06 UTC"
        ),
        "tn_06_18": build_extreme_table(
            hours, meta, end18 - timedelta(hours=12), end18, "min",
            "Températures minimales 06–18 UTC"
        ),
        "tn_finales": build_extreme_table(
            hours, meta, end18 - timedelta(hours=24), end18, "min",
            "Températures minimales Tn finales"
        ),
        "tx_provisoire": tx_prov,
        "tx_06_18": build_extreme_table(
            hours, meta, end18 - timedelta(hours=12), end18, "max",
            "Températures maximales 06–18 UTC"
        ),
        "tx_18_06": build_extreme_table(
            hours, meta, end06 - timedelta(hours=12), end06, "max",
            "Températures maximales 18–06 UTC"
        ),
        "tx_finales": build_extreme_table(
            hours, meta, end06 - timedelta(hours=24), end06, "max",
            "Températures maximales Tx finales"
        ),
    }
    hourly = build_hourly_tables(hours, meta, latest)
    return tables, hourly


def main() -> int:
    print(f"=== Classements températures v{VERSION} ===")
    key = get_api_key()
    cache, latest = update_cache(key)
    meta = load_station_meta(key)
    meta = enrich_official_metadata(meta, latest)
    tables, hourly = build_tables(cache, meta, latest)

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "status": "ok",
        "generated_at": iso(utcnow()),
        "latest_observation_at": iso(latest),
        "timezone_local": "Europe/Paris",
        "scope": "France métropolitaine",
        "unit": "°C",
        "title": "Classements des observations en France",
        "source": {
            "provider": "Météo-France",
            "api": "DPPaquetObs v2 - paquets horaires stations",
            "fields": (
                "t, tn, tx, td, u, pmer, pres, ff, dd, vv, "
                "ht_neige, insolh/ssfrai"
            ),
            "climatology": "Fiches climatologiques Météo-France 1991-2020",
            "site_quality": "Fiches de postes Météo-France, classe par paramètre",
            "hourly_retention": "24 h côté API ; cache local glissant 72 h",
            "tn_final_period": "18 UTC J-1 à 18 UTC J",
            "tx_final_period": "06 UTC J à 06 UTC J+1",
            "extreme_method": "TN/TX horaire prioritaire ; T utilisée en secours",
        },
        "hourly": hourly,
        "tables": tables,
        "cache": {
            "hours_retained": len(cache.get("hours") or {}),
            "target_hours": CACHE_HOURS,
            "site_quality_stations": sum(bool(info.get("site_quality")) for info in meta.values()),
            "climatology_stations": sum(bool(info.get("climate")) for info in meta.values()),
            "metadata_batch_size": METADATA_BATCH_SIZE,
        },
    }

    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )

    print("=== TERMINÉ ===")
    print("Dernier relevé :", output["latest_observation_at"])
    print("Heures cache :", output["cache"]["hours_retained"])
    print("Heures horaires affichables :", len(hourly))
    for key_name, table in tables.items():
        print(
            key_name,
            ":",
            table["stations"],
            "stations ;",
            table["coverage_hours"],
            "/",
            table["expected_hours"],
            "h",
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

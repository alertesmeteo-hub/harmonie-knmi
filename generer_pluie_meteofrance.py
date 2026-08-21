#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Météo Climat Pro — Carte pluie Météo-France
Version 2.4.1

Nouveautés v2.3.0
-----------------
- Ajout des cumuls glissants 48 h et 72 h.
- Ajout du cumul de la saison météorologique en cours.
- Ajout du cumul de l'année en cours.
- Ajout du record quotidien de précipitations et de sa date.
- Conservation du mois en cours et des normales 1991-2020.
- Exclusion des stations dont le nom contient le marqueur SAPC.
- Assemblage prudent des données quotidiennes et horaires sans créer de trou.

Produit :
  observations_pluie.json

Vues :
  - rr24               : cumul des 24 dernières heures à partir de RR1
  - rr48               : cumul des 48 dernières heures à partir de RR1
  - rr72               : cumul des 72 dernières heures à partir de RR1
  - rr_month_current   : cumul du mois en cours
  - rr_season_current  : cumul de la saison météorologique en cours
  - rr_year_current    : cumul de l'année en cours
  - rr_daily_record    : record quotidien de précipitations par station
  - rr_month_mean      : cumul moyen du mois sur 1991-2020
  - rr_year_mean       : cumul annuel moyen sur 1991-2020

Secrets GitHub :
  METEOFRANCE_PACKAGE_OBS_KEY
  METEOFRANCE_OBS_TOKEN

Les deux secrets sont des API Keys Météo-France et sont envoyés via
l'en-tête HTTP "apikey".
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


VERSION = "2.4.1"
CACHE_SCHEMA = 5

PACKAGE_BASE = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)

DPOBS_STATIONS_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPObs/v2/liste-stations"
)

MF_S3_QUOT = (
    "https://meteofrance.s3.sbg.io.cloud.ovh.net/"
    "data/synchro_ftp/BASE/QUOT"
)

DATAGOUV_HOURLY_DATASET_API = "https://www.data.gouv.fr/api/1/datasets/donnees-climatologiques-de-base-horaires/"

MF_S3_MENS = (
    "https://meteofrance.s3.sbg.io.cloud.ovh.net/"
    "data/synchro_ftp/BASE/MENS"
)

OUTPUT = Path("observations_pluie.json")
CACHE = Path("cache_pluie_climatologie.json")
HOURLY_CACHE = Path("cache_pluie_horaire.json")

NORMAL_START = 1991
NORMAL_END = 2020

HTTP_TIMEOUT = 35

# L'API autorise 50 appels/minute. Le script temporise les appels historiques.
PACKAGE_REQUEST_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.35"))

# Recherche du dernier paquet disponible.
PACKAGE_RETRIES_HOURS = 4

# Pour RR24 : on accepte 22 valeurs horaires minimum sur 24.
RR24_MIN_VALID_HOURS = 22
RR48_MIN_VALID_HOURS = 44
RR72_MIN_VALID_HOURS = 66

# Historique demandé au Package Obs pour essayer de combler J-1/J-2.
# Le script arrête rapidement si les heures les plus anciennes ne sont
# plus disponibles.
PACKAGE_HISTORY_HOURS = 72
PACKAGE_STOP_AFTER_OLD_MISSING = 4

# Un jour reconstitué avec RR1 est considéré exploitable avec au moins 22 h.
FULL_DAY_MIN_VALID_HOURS = 22

# Rafraîchissement du cumul mensuel contrôlé.
CURRENT_MONTH_REFRESH_HOURS = 2
RECORDS_REFRESH_DAYS = 180

# Exclusion demandée.
EXCLUDE_SAPC = True

# Les archives historiques (records + normales) sont lourdes à télécharger.
# Elles sont construites uniquement par le workflow dédié.
BUILD_ARCHIVES = os.getenv("MF_BUILD_ARCHIVES", "0").strip().lower() in {
    "1", "true", "yes", "oui", "on"
}

# Les traitements lourds sont désactivés dans le workflow horaire.
BOOTSTRAP_HOURLY = os.getenv("MF_BOOTSTRAP_HOURLY", "0").strip().lower() in {
    "1", "true", "yes", "oui", "on"
}
REFRESH_DAILY = os.getenv("MF_REFRESH_DAILY", "0").strip().lower() in {
    "1", "true", "yes", "oui", "on"
}

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
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_yyyymmdd(value: Any) -> Optional[date]:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


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
    if x < 0 or x > 10000:
        return None
    return x


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


def parse_delimited(raw: bytes) -> List[dict]:
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


def apikey_headers(key: str) -> dict:
    return {"apikey": key, "accept": "*/*"}


def is_sapc_name(name: str) -> bool:
    if not EXCLUDE_SAPC:
        return False
    normalized = re.sub(r"[^A-Z0-9]+", " ", str(name).upper()).strip()
    return "SAPC" in normalized.split()



# Villes/repères prioritaires par département pour conserver une bonne
# couverture géographique quand l'utilisateur limite le nombre de stations.
DEPARTMENT_CITY_HINTS = {
    "01": ["BOURG", "AMBERIEU"], "02": ["LAON", "SAINT QUENTIN"],
    "03": ["MOULINS", "VICHY"], "04": ["DIGNE"], "05": ["GAP"],
    "06": ["NICE", "CANNES"], "07": ["PRIVAS", "AUBENAS"],
    "08": ["CHARLEVILLE", "SEDAN"], "09": ["FOIX", "PAMIERS"],
    "10": ["TROYES"], "11": ["CARCASSONNE", "NARBONNE"],
    "12": ["RODEZ", "MILLAU"], "13": ["MARSEILLE", "MARIGNANE", "AIX"],
    "14": ["CAEN"], "15": ["AURILLAC"], "16": ["ANGOULEME", "COGNAC"],
    "17": ["LA ROCHELLE", "ROCHEFORT"], "18": ["BOURGES"],
    "19": ["BRIVE", "TULLE"], "2A": ["AJACCIO"], "2B": ["BASTIA", "CALVI"],
    "21": ["DIJON"], "22": ["SAINT BRIEUC"], "23": ["GUERET"],
    "24": ["PERIGUEUX", "BERGERAC"], "25": ["BESANCON"],
    "26": ["VALENCE", "MONTELIMAR"], "27": ["EVREUX"],
    "28": ["CHARTRES"], "29": ["BREST", "QUIMPER"], "30": ["NIMES"],
    "31": ["TOULOUSE"], "32": ["AUCH"], "33": ["BORDEAUX", "MERIGNAC"],
    "34": ["MONTPELLIER", "BEZIERS"], "35": ["RENNES"],
    "36": ["CHATEAUROUX"], "37": ["TOURS"], "38": ["GRENOBLE"],
    "39": ["LONS", "DOLE"], "40": ["MONT DE MARSAN", "DAX"],
    "41": ["BLOIS"], "42": ["SAINT ETIENNE"], "43": ["LE PUY"],
    "44": ["NANTES"], "45": ["ORLEANS"], "46": ["CAHORS"],
    "47": ["AGEN"], "48": ["MENDE"], "49": ["ANGERS"],
    "50": ["SAINT LO", "CHERBOURG"], "51": ["REIMS", "CHALONS"],
    "52": ["CHAUMONT", "LANGRES"], "53": ["LAVAL"],
    "54": ["NANCY"], "55": ["BAR LE DUC", "VERDUN"],
    "56": ["VANNES", "LORIENT"], "57": ["METZ"], "58": ["NEVERS"],
    "59": ["LILLE", "DUNKERQUE", "DOUAI"], "60": ["BEAUVAIS", "CREIL"],
    "61": ["ALENCON"], "62": ["ARRAS", "LE TOUQUET", "CALAIS"],
    "63": ["CLERMONT"], "64": ["PAU", "BIARRITZ"], "65": ["TARBES"],
    "66": ["PERPIGNAN"], "67": ["STRASBOURG"], "68": ["MULHOUSE", "COLMAR"],
    "69": ["LYON", "BRON"], "70": ["VESOUL"], "71": ["MACON"],
    "72": ["LE MANS"], "73": ["CHAMBERY"], "74": ["ANNECY"],
    "75": ["PARIS"], "76": ["ROUEN", "DIEPPE"], "77": ["MELUN"],
    "78": ["VERSAILLES", "TRAPPES"], "79": ["NIORT"],
    "80": ["AMIENS", "ABBEVILLE"], "81": ["ALBI", "CASTRES"],
    "82": ["MONTAUBAN"], "83": ["TOULON", "HYERES"], "84": ["AVIGNON"],
    "85": ["LA ROCHE", "LES SABLES"], "86": ["POITIERS"],
    "87": ["LIMOGES"], "88": ["EPINAL"], "89": ["AUXERRE"],
    "90": ["BELFORT"], "91": ["EVRY", "ORLY"], "92": ["PARIS"],
    "93": ["LE BOURGET", "PARIS"], "94": ["ORLY", "PARIS"],
    "95": ["PONTOISE", "ROISSY"],
}

def normalize_name(value: str) -> str:
    import unicodedata
    txt = unicodedata.normalize("NFKD", str(value or ""))
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch)).upper()
    return re.sub(r"[^A-Z0-9]+", " ", txt).strip()

def department_code(sid: str) -> str:
    d = re.sub(r"\D", "", str(sid or ""))
    if d.startswith("20") and len(d) >= 3:
        # Les identifiants corses ne permettent pas toujours de séparer 2A/2B.
        return "20"
    return d[:2] if len(d) >= 2 else ""

def choose_department_anchors(station_ids: List[str], names: Dict[str, str]) -> set:
    by_dep: Dict[str, List[str]] = defaultdict(list)
    for sid in station_ids:
        dep = department_code(sid)
        if dep:
            by_dep[dep].append(sid)
    anchors = set()
    for dep, ids in by_dep.items():
        hints = DEPARTMENT_CITY_HINTS.get(dep, [])
        best = None
        best_score = 9999
        for sid in ids:
            n = normalize_name(names.get(sid, sid))
            score = 500
            for i, hint in enumerate(hints):
                if normalize_name(hint) in n:
                    score = i
                    break
            if best is None or (score, n, sid) < (best_score, normalize_name(names.get(best, best)), best):
                best, best_score = sid, score
        if best:
            anchors.add(best)
    return anchors

# ---------------------------------------------------------------------
# Cache horaire glissant (80 h)
# ---------------------------------------------------------------------

def load_hourly_cache() -> dict:
    if not HOURLY_CACHE.exists():
        return {"schema_version": 1, "hours": {}}
    try:
        data = json.loads(HOURLY_CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("cache non objet")
        data.setdefault("hours", {})
        return data
    except Exception as exc:
        print(f"[WARN] cache horaire ignoré : {exc}")
        return {"schema_version": 1, "hours": {}}


def save_hourly_cache(cache: dict, latest_hour: datetime) -> None:
    cutoff = latest_hour - timedelta(hours=79)
    keep = {}
    for key, value in (cache.get("hours") or {}).items():
        dt = parse_iso(key)
        if dt is not None and cutoff <= dt <= latest_hour + timedelta(hours=1):
            keep[iso(dt)] = value
    cache["schema_version"] = 1
    cache["module_version"] = VERSION
    cache["latest_hour"] = iso(latest_hour)
    cache["hours"] = keep
    HOURLY_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def parse_hourly_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    iso_dt = parse_iso(text)
    if iso_dt is not None:
        return iso_dt
    digits = re.sub(r"\D", "", text)
    for fmt, length in (("%Y%m%d%H%M", 12), ("%Y%m%d%H", 10), ("%Y%m%d", 8)):
        if len(digits) >= length:
            try:
                return datetime.strptime(digits[:length], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def hourly_cache_depth(cache: dict, latest_hour: datetime) -> int:
    ages = []
    for key in (cache.get("hours") or {}):
        dt = parse_iso(key)
        if dt is None:
            continue
        age = int((latest_hour - dt).total_seconds() // 3600)
        if 0 <= age < 80:
            ages.append(age)
    return (max(ages) + 1) if ages else 0


def bootstrap_hourly_cache_from_datagouv(cache: dict, station_ids: List[str], latest_hour: datetime) -> None:
    """Amorce le cache 72 h avec les données horaires climatologiques.

    Ce secours n'est utilisé que lorsque la branche observations ne possède
    pas encore assez d'historique. Ensuite le cache est alimenté par l'API
    temps réel à chaque exécution horaire.
    """
    if hourly_cache_depth(cache, latest_hour) >= 72:
        return
    print("Cache 72 h incomplet : amorçage via Météo-France / data.gouv.fr...")
    try:
        r = session.get(DATAGOUV_HOURLY_DATASET_API, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        resources = r.json().get("resources", [])
    except Exception as exc:
        print(f"[WARN] catalogue horaire data.gouv.fr indisponible : {exc}")
        return

    deps = sorted({department_code(sid) for sid in station_ids if department_code(sid) and department_code(sid) != "20"})
    by_dep_ids = {dep: {sid for sid in station_ids if department_code(sid) == dep} for dep in deps}
    resource_by_dep = {}
    for dep in deps:
        needle = f"HOR_departement_{dep}_periode_".lower()
        candidates = []
        for res in resources:
            title = str(res.get("title") or "")
            url = str(res.get("url") or "")
            if needle in title.lower() and url:
                # Priorité au fichier qui contient l'année courante.
                score = 0 if str(latest_hour.year) in title else 1
                candidates.append((score, title, url))
        if candidates:
            candidates.sort()
            resource_by_dep[dep] = candidates[0][2]

    cutoff = latest_hour - timedelta(hours=79)

    def fetch_dep(dep_url):
        dep, url = dep_url
        try:
            rr = requests.get(url, timeout=120, headers={"User-Agent": f"alertes-meteo-carte-pluie/{VERSION}"})
            rr.raise_for_status()
            return dep, parse_delimited(rr.content), None
        except Exception as exc:
            return dep, None, exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_dep, item) for item in resource_by_dep.items()]
        for future in as_completed(futures):
            dep, rows, exc = future.result()
            if exc is not None or rows is None:
                print(f"[WARN] amorçage horaire {dep} impossible : {exc}")
                continue
            wanted = by_dep_ids.get(dep, set())
            kept = 0
            for row in rows:
                sid = station_id(row)
                if sid not in wanted:
                    continue
                hdt = parse_hourly_datetime(first(row, ("AAAAMMJJHH", "AAAAMMJJHHMM", "DATE", "date", "validity_time")))
                if hdt is None or hdt < cutoff or hdt > latest_hour:
                    continue
                rr1 = clean_rain(first(row, ("RR1", "rr1", "RR", "rr")))
                if rr1 is None:
                    continue
                key = iso(hdt.replace(minute=0, second=0, microsecond=0))
                cache.setdefault("hours", {}).setdefault(key, {})[sid] = round(rr1, 3)
                kept += 1
            print(f"Amorçage {dep} : {kept} valeurs horaires récentes conservées")


# ---------------------------------------------------------------------
# Package Observations V2
# ---------------------------------------------------------------------

def package_request(
    key: str,
    date_hour: datetime,
) -> Optional[requests.Response]:

    date_text = iso(
        date_hour.replace(minute=0, second=0, microsecond=0)
    )

    response = session.get(
        PACKAGE_BASE,
        params={"date": date_text, "format": "csv"},
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
            "Package Observations : HTTP 403, abonnement/droits insuffisants."
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

    base = utcnow().replace(minute=0, second=0, microsecond=0)

    for back in range(PACKAGE_RETRIES_HOURS):
        candidate = base - timedelta(hours=back)
        print("Recherche paquet :", iso(candidate))
        response = package_request(key, candidate)
        if response is not None:
            print("Dernier paquet disponible :", iso(candidate))
            return candidate, response
        time.sleep(PACKAGE_REQUEST_DELAY)

    raise RuntimeError(
        "Aucun paquet horaire disponible entre H et H-3."
    )


def latest_cached_hour(cache: dict) -> Optional[datetime]:
    values = []
    for key in (cache.get("hours") or {}):
        dt = parse_iso(key)
        if dt is not None:
            values.append(dt)
    return max(values) if values else None


def load_package_history(
    key: str,
) -> Tuple[datetime, Dict[str, dict], Dict[str, dict]]:
    """Met à jour le cache RR1 sans re-télécharger 24 h à chaque run.

    - Premier lancement / cache vide : amorce les 24 dernières heures via
      Package Observations (profondeur maximale officielle 24 h).
    - Lancements suivants : récupère uniquement les heures nouvelles + 2 h
      de recouvrement pour absorber les retards/corrections.
    - Les 48/72 h sont ensuite reconstruits depuis cache_pluie_horaire.json.
    """
    latest_hour, first_response = find_latest_package_hour(key)
    hourly_cache = load_hourly_cache()
    cache_hours = hourly_cache.setdefault("hours", {})
    station_geo: Dict[str, dict] = {}

    previous_latest = latest_cached_hour(hourly_cache)
    if previous_latest is None:
        fetch_count = 24
        print("Cache horaire vide : amorçage léger des 24 dernières heures via Package Obs.")
    else:
        gap = max(0, int((latest_hour - previous_latest).total_seconds() // 3600))
        # 2 h de recouvrement + heures réellement manquées. Toujours <= 24 h.
        fetch_count = min(24, max(3, gap + 2))
        print(
            f"Cache déjà présent jusqu'à {iso(previous_latest)} : "
            f"récupération de {fetch_count} paquet(s) récent(s)."
        )

    # Ne jamais demander plus de 24 h au Package Observations.
    for offset in range(fetch_count):
        target = latest_hour - timedelta(hours=offset)
        response = first_response if offset == 0 else package_request(key, target)
        if offset:
            time.sleep(PACKAGE_REQUEST_DELAY)
        if response is None:
            print(f"[INFO] Paquet indisponible : {iso(target)}")
            continue

        rows = parse_delimited(response.content)
        print(f"Paquet {iso(target)} : {len(rows)} lignes")

        for row in rows:
            sid = station_id(row)
            if not sid:
                continue
            lat = fnum(first(row, ("lat", "LAT", "latitude")))
            lon = fnum(first(row, ("lon", "LON", "longitude")))
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                station_geo[sid] = {"lat": round(lat, 6), "lon": round(lon, 6)}
            rr1 = clean_rain(first(row, ("rr1", "RR1")))
            if rr1 is None:
                continue
            validity = parse_iso(first(row, ("validity_time", "reference_time", "date"))) or target
            vkey = iso(validity.replace(minute=0, second=0, microsecond=0))
            cache_hours.setdefault(vkey, {})[sid] = round(rr1, 3)

    # Dans le workflow normal on ne télécharge JAMAIS les gros fichiers
    # climatologiques nationaux. Le workflow 48/72 h dédié est matriciel.
    if BOOTSTRAP_HOURLY:
        print("[INFO] MF_BOOTSTRAP_HOURLY ignoré en v2.4.1 : utiliser le workflow matriciel dédié.")

    depth_before_save = hourly_cache_depth(hourly_cache, latest_hour)
    save_hourly_cache(hourly_cache, latest_hour)
    saved_cache = load_hourly_cache()
    depth_after_save = hourly_cache_depth(saved_cache, latest_hour)
    print(f"Cache horaire avant sauvegarde : {depth_before_save} h | après : {depth_after_save} h")
    if depth_before_save >= 48 and depth_after_save < 48:
        raise RuntimeError(
            f"Régression cache horaire : {depth_before_save} h avant sauvegarde, "
            f"{depth_after_save} h après sauvegarde"
        )

    # Utiliser le cache réellement sauvegardé pour les agrégations RR24/48/72.
    cache_hours = saved_cache.get("hours") or {}

    agg: Dict[str, dict] = defaultdict(lambda: {
        "rr24_sum": 0.0, "rr24_hours": 0,
        "rr48_sum": 0.0, "rr48_hours": 0,
        "rr72_sum": 0.0, "rr72_hours": 0,
        "today_sum": 0.0, "today_hours": 0,
        "latest_date": None,
        "day_sums": defaultdict(float), "day_hours": defaultdict(int),
    })
    latest_day = latest_hour.date()
    for hkey, bucket in cache_hours.items():
        hdt = parse_iso(hkey)
        if hdt is None:
            continue
        age = int((latest_hour - hdt).total_seconds() // 3600)
        if age < 0 or age >= 72:
            continue
        for sid, raw_rr in (bucket or {}).items():
            rr1 = clean_rain(raw_rr)
            if rr1 is None:
                continue
            item = agg[sid]
            dkey = yyyymmdd(hdt.date())
            item["day_sums"][dkey] += rr1
            item["day_hours"][dkey] += 1
            if age < 24:
                item["rr24_sum"] += rr1; item["rr24_hours"] += 1
            if age < 48:
                item["rr48_sum"] += rr1; item["rr48_hours"] += 1
            if age < 72:
                item["rr72_sum"] += rr1; item["rr72_hours"] += 1
            if hdt.date() == latest_day:
                item["today_sum"] += rr1; item["today_hours"] += 1
            old_dt = parse_iso(item["latest_date"])
            if old_dt is None or hdt > old_dt:
                item["latest_date"] = iso(hdt)

    for item in agg.values():
        item["day_sums"] = {k: round(v, 3) for k, v in item["day_sums"].items()}
        item["day_hours"] = dict(item["day_hours"])
    return latest_hour, station_geo, agg


# ---------------------------------------------------------------------
# DPObs V2 : noms
# ---------------------------------------------------------------------

def load_station_names(obs_key: str) -> Dict[str, str]:
    print("Chargement /DPObs/v2/liste-stations ...")

    response = session.get(
        DPOBS_STATIONS_URL,
        headers=apikey_headers(obs_key),
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code != 200:
        print(
            f"[WARN] liste-stations HTTP {response.status_code}; "
            "identifiants utilisés comme noms."
        )
        return {}

    content_type = (response.headers.get("content-type") or "").lower()

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

    names: Dict[str, str] = {}
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

    print(f"Noms récupérés : {len(names)} station(s).")
    return names


# ---------------------------------------------------------------------
# Fichiers climatologiques
# ---------------------------------------------------------------------

def department_candidates(sid: str) -> List[str]:
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


def current_daily_url(dep: str, now: datetime) -> str:
    return (
        f"{MF_S3_QUOT}/"
        f"Q_{dep}_latest-{now.year - 1}-{now.year}_RR-T-Vent.csv.gz"
    )


def historic_daily_url(dep: str, now: datetime) -> str:
    return (
        f"{MF_S3_QUOT}/"
        f"Q_{dep}_previous-1950-{now.year - 2}_RR-T-Vent.csv.gz"
    )


def historic_monthly_url(dep: str, now: datetime) -> str:
    return (
        f"{MF_S3_MENS}/"
        f"MENSQ_{dep}_previous-1950-{now.year - 2}.csv.gz"
    )


def download_climate_rows(url: str) -> Optional[List[dict]]:
    try:
        response = session.get(url, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        print(f"[WARN] téléchargement impossible {url}: {exc}")
        return None

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        print(f"[WARN] HTTP {response.status_code}: {url}")
        return None

    try:
        return parse_delimited(response.content)
    except Exception as exc:
        print(f"[WARN] CSV illisible {url}: {exc}")
        return None


# ---------------------------------------------------------------------
# Périodes calendaires / saisons météorologiques
# ---------------------------------------------------------------------

def current_season_info(day: date) -> dict:
    if day.month in (12, 1, 2):
        start_year = day.year if day.month == 12 else day.year - 1
        end_year = start_year + 1
        return {
            "id": f"hiver-{start_year}-{end_year}",
            "label": f"Hiver {start_year}-{end_year}",
            "start": date(start_year, 12, 1),
        }
    if day.month in (3, 4, 5):
        return {
            "id": f"printemps-{day.year}",
            "label": f"Printemps {day.year}",
            "start": date(day.year, 3, 1),
        }
    if day.month in (6, 7, 8):
        return {
            "id": f"ete-{day.year}",
            "label": f"Été {day.year}",
            "start": date(day.year, 6, 1),
        }
    return {
        "id": f"automne-{day.year}",
        "label": f"Automne {day.year}",
        "start": date(day.year, 9, 1),
    }


# ---------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------

def load_cache() -> dict:
    if not CACHE.exists():
        return {
            "schema_version": CACHE_SCHEMA,
            "stations": {},
        }

    try:
        payload = json.loads(CACHE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cache non objet")
        payload.setdefault("stations", {})
        payload["schema_version"] = CACHE_SCHEMA
        return payload
    except Exception as exc:
        print(f"[WARN] cache ignoré : {exc}")
        return {
            "schema_version": CACHE_SCHEMA,
            "stations": {},
        }


def cache_datetime(cache: dict, key: str) -> Optional[datetime]:
    return parse_iso(cache.get(key))


def hours_since(dt: Optional[datetime]) -> float:
    if dt is None:
        return 999999.0
    return (utcnow() - dt).total_seconds() / 3600.0


# ---------------------------------------------------------------------
# Mois en cours : base quotidienne
# ---------------------------------------------------------------------

def update_daily_records(
    cache: dict,
    station_ids: List[str],
    now: datetime,
) -> None:
    """Construit le record quotidien absolu à partir des archives QUOT.

    Le gros historique (1950 -> année-2) est mis en cache et n'est rafraîchi
    que périodiquement. Les deux dernières années sont ensuite comparées lors
    de chaque mise à jour des cumuls courants.
    """

    stations_cache = cache.setdefault("stations", {})
    known = sum(
        1
        for sid in station_ids
        if stations_cache.get(sid, {}).get("record_archive_checked") is True
    )
    coverage = known / max(1, len(station_ids))

    stale = (
        hours_since(cache_datetime(cache, "records_generated_at"))
        >= 24 * RECORDS_REFRESH_DAYS
        or cache.get("records_archive_end_year") != now.year - 2
        or coverage < 0.80
    )

    if not stale:
        print("Cache des records quotidiens encore valide.")
        return

    print("Calcul des records quotidiens historiques...")

    by_dep: Dict[str, set] = defaultdict(set)
    for sid in station_ids:
        for dep in department_candidates(sid):
            by_dep[dep].add(sid)

    maxima: Dict[str, float] = {}
    max_dates: Dict[str, str] = {}
    seen = set()

    for dep, dep_stations in sorted(by_dep.items()):
        url = historic_daily_url(dep, now)
        rows = download_climate_rows(url)
        if rows is None:
            continue

        print(f"Historique quotidien {dep}: {len(rows)} lignes")

        for row in rows:
            sid = station_id(row)
            if sid not in dep_stations:
                continue

            day = parse_yyyymmdd(first(row, ("AAAAMMJJ", "DATE", "date")))
            rr = clean_rain(first(row, ("RR", "rr", "PRECIP", "PRECIPITATION")))
            if day is None or rr is None:
                continue

            key = (sid, yyyymmdd(day))
            if key in seen:
                continue
            seen.add(key)

            old = maxima.get(sid)
            if old is None or rr > old:
                maxima[sid] = rr
                max_dates[sid] = yyyymmdd(day)

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})
        entry["record_daily_archive"] = (
            round(maxima[sid], 1) if sid in maxima else None
        )
        entry["record_daily_archive_date"] = max_dates.get(sid)
        entry["record_archive_checked"] = True

    cache["records_generated_at"] = iso(utcnow())
    cache["records_archive_end_year"] = now.year - 2


def update_current_month(
    cache: dict,
    station_ids: List[str],
    now: datetime,
) -> None:
    """Met à jour mois, saison, année et les records des deux dernières années."""

    month_id = f"{now.year:04d}-{now.month:02d}"
    season = current_season_info(now.date())
    previous_day_key = yyyymmdd(now.date() - timedelta(days=1))

    last_global_through = str(cache.get("current_month_global_through") or "")
    lagging = bool(last_global_through and last_global_through < previous_day_key)

    stale = (
        cache.get("current_month_id") != month_id
        or cache.get("current_season_id") != season["id"]
        or cache.get("current_year_id") != now.year
        or cache.get("current_month_logic_version") != 3
        or lagging
        or hours_since(cache_datetime(cache, "current_month_generated_at"))
        >= CURRENT_MONTH_REFRESH_HOURS
    )

    if not stale:
        print("Cache des cumuls courants encore valide.")
        return

    print("Actualisation des données quotidiennes mois/saison/année...")

    by_dep: Dict[str, set] = defaultdict(set)
    for sid in station_ids:
        for dep in department_candidates(sid):
            by_dep[dep].add(sid)

    month_totals: Dict[str, float] = defaultdict(float)
    year_totals: Dict[str, float] = defaultdict(float)
    season_totals: Dict[str, float] = defaultdict(float)

    month_last: Dict[str, str] = {}
    year_last: Dict[str, str] = {}
    season_last: Dict[str, str] = {}

    recent_record: Dict[str, float] = {}
    recent_record_date: Dict[str, str] = {}
    seen_days = set()

    for dep, dep_stations in sorted(by_dep.items()):
        url = current_daily_url(dep, now)
        rows = download_climate_rows(url)
        if rows is None:
            continue

        print(f"Quotidien récent {dep}: {len(rows)} lignes")

        for row in rows:
            sid = station_id(row)
            if sid not in dep_stations:
                continue

            day = parse_yyyymmdd(first(row, ("AAAAMMJJ", "DATE", "date")))
            rr = clean_rain(first(row, ("RR", "rr", "PRECIP", "PRECIPITATION")))
            if day is None or rr is None or day > now.date():
                continue

            day_key = yyyymmdd(day)
            dedup = (sid, day_key)
            if dedup in seen_days:
                continue
            seen_days.add(dedup)

            # Record récent (les fichiers latest couvrent les deux dernières années).
            if sid not in recent_record or rr > recent_record[sid]:
                recent_record[sid] = rr
                recent_record_date[sid] = day_key

            if day.year == now.year:
                year_totals[sid] += rr
                if day_key > year_last.get(sid, ""):
                    year_last[sid] = day_key

            if season["start"] <= day <= now.date():
                season_totals[sid] += rr
                if day_key > season_last.get(sid, ""):
                    season_last[sid] = day_key

            if day.year == now.year and day.month == now.month:
                month_totals[sid] += rr
                if day_key > month_last.get(sid, ""):
                    month_last[sid] = day_key

    stations_cache = cache.setdefault("stations", {})

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})

        entry["month_daily_total"] = (
            round(month_totals[sid], 1) if sid in month_totals else None
        )
        entry["month_daily_through"] = month_last.get(sid)

        entry["season_daily_total"] = (
            round(season_totals[sid], 1) if sid in season_totals else None
        )
        entry["season_daily_through"] = season_last.get(sid)
        entry["season_id"] = season["id"]

        entry["year_daily_total"] = (
            round(year_totals[sid], 1) if sid in year_totals else None
        )
        entry["year_daily_through"] = year_last.get(sid)
        entry["year_id"] = now.year

        archive_value = fnum(entry.get("record_daily_archive"))
        archive_date = entry.get("record_daily_archive_date")
        recent_value = fnum(recent_record.get(sid))
        recent_date = recent_record_date.get(sid)

        # Ne jamais présenter le maximum des deux dernières années comme un
        # record absolu si l'historique n'a pas encore été initialisé.
        if entry.get("record_archive_checked") is not True:
            entry["record_daily"] = None
            entry["record_daily_date"] = None
        elif archive_value is None and recent_value is None:
            entry["record_daily"] = None
            entry["record_daily_date"] = None
        elif archive_value is None or (
            recent_value is not None and recent_value > archive_value
        ):
            entry["record_daily"] = round(recent_value, 1)
            entry["record_daily_date"] = recent_date
        else:
            entry["record_daily"] = round(archive_value, 1)
            entry["record_daily_date"] = archive_date

    valid_through = [d for d in month_last.values() if d]
    cache["current_month_global_through"] = max(valid_through) if valid_through else None
    cache["current_month_id"] = month_id
    cache["current_season_id"] = season["id"]
    cache["current_season_label"] = season["label"]
    cache["current_season_start"] = yyyymmdd(season["start"])
    cache["current_year_id"] = now.year
    cache["current_month_logic_version"] = 3
    cache["current_month_generated_at"] = iso(utcnow())


# ---------------------------------------------------------------------
# Normales 1991-2020
# ---------------------------------------------------------------------

def update_normals(
    cache: dict,
    station_ids: List[str],
    now: datetime,
) -> None:

    stations_cache = cache.setdefault("stations", {})

    known = sum(
        1
        for sid in station_ids
        if stations_cache.get(sid, {}).get("normal_year") is not None
    )
    coverage = known / max(1, len(station_ids))

    stale = (
        hours_since(cache_datetime(cache, "normals_generated_at"))
        >= 24 * 45
        or coverage < 0.80
    )

    if not stale:
        print("Cache des normales 1991-2020 encore valide.")
        return

    print("Calcul des normales 1991-2020...")

    by_dep: Dict[str, set] = defaultdict(set)
    for sid in station_ids:
        for dep in department_candidates(sid):
            by_dep[dep].add(sid)

    values: Dict[str, Dict[int, Dict[int, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    seen = set()

    for dep, dep_stations in sorted(by_dep.items()):
        url = historic_monthly_url(dep, now)
        rows = download_climate_rows(url)

        if rows is None:
            continue

        print(f"Mensuel {dep}: {len(rows)} lignes")

        for row in rows:
            sid = station_id(row)
            if sid not in dep_stations:
                continue

            raw_date = first(row, ("AAAAMM", "DATE", "date"))
            digits = re.sub(r"\D", "", str(raw_date or ""))
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
                    ("RR", "rr", "PRECIP", "PRECIPITATION"),
                )
            )
            if rr is None:
                continue

            key = (sid, year, month)
            if key in seen:
                continue
            seen.add(key)

            values[sid][year][month] = rr

    for sid in station_ids:
        entry = stations_cache.setdefault(sid, {})

        month_normals = {}
        month_counts = {}

        for month in range(1, 13):
            vals = [
                months[month]
                for _, months in values.get(sid, {}).items()
                if month in months
            ]

            month_counts[str(month)] = len(vals)
            month_normals[str(month)] = (
                round(sum(vals) / len(vals), 1)
                if len(vals) >= 20
                else None
            )

        annual_values = []
        for _, months in values.get(sid, {}).items():
            if all(m in months for m in range(1, 13)):
                annual_values.append(
                    sum(months[m] for m in range(1, 13))
                )

        entry["normal_months"] = month_normals
        entry["normal_month_counts"] = month_counts
        entry["normal_year"] = (
            round(sum(annual_values) / len(annual_values), 1)
            if len(annual_values) >= 20
            else None
        )
        entry["normal_year_count"] = len(annual_values)

    cache["normals_generated_at"] = iso(utcnow())
    cache["normal_period"] = f"{NORMAL_START}-{NORMAL_END}"


# ---------------------------------------------------------------------
# Mois en cours : assemblage sans trou
# ---------------------------------------------------------------------

def month_total_live(
    cache_entry: dict,
    hourly: dict,
    latest_hour: datetime,
) -> Tuple[Optional[float], Optional[str], bool, str]:

    latest_day = latest_hour.date()
    latest_day_key = yyyymmdd(latest_day)

    daily_total = fnum(cache_entry.get("month_daily_total"))
    daily_through = parse_yyyymmdd(
        cache_entry.get("month_daily_through")
    )

    day_sums = hourly.get("day_sums") or {}
    day_hours = hourly.get("day_hours") or {}

    today_sum = fnum(hourly.get("today_sum"))
    today_hours = int(hourly.get("today_hours") or 0)

    # Si le fichier quotidien inclut déjà aujourd'hui, il est prioritaire.
    if daily_total is not None and daily_through == latest_day:
        return (
            round(daily_total, 1),
            latest_day_key,
            True,
            "quotidien_controle",
        )

    # Sans base quotidienne, on ne peut reconstituer le mois entier que
    # pendant les tout premiers jours du mois.
    if daily_total is None or daily_through is None:
        month_start = latest_day.replace(day=1)
        total = 0.0
        cursor = month_start

        while cursor < latest_day:
            key = yyyymmdd(cursor)
            hours = int(day_hours.get(key) or 0)
            value = fnum(day_sums.get(key))

            if value is None or hours < FULL_DAY_MIN_VALID_HOURS:
                return None, None, False, "base_quotidienne_absente"

            total += value
            cursor += timedelta(days=1)

        if today_sum is not None and today_hours > 0:
            total += today_sum
            return (
                round(total, 1),
                latest_day_key,
                True,
                "paquets_horaires_uniquement",
            )

        if cursor == latest_day and latest_day.day == 1:
            return 0.0, latest_day_key, True, "debut_du_mois"

        return None, None, False, "base_quotidienne_absente"

    total = float(daily_total)
    through = daily_through

    # Si le quotidien est en retard, on ne saute aucun jour.
    cursor = daily_through + timedelta(days=1)

    while cursor < latest_day:
        key = yyyymmdd(cursor)
        hours = int(day_hours.get(key) or 0)
        value = fnum(day_sums.get(key))

        if value is None or hours < FULL_DAY_MIN_VALID_HOURS:
            # Pas de bricolage : on conserve uniquement la partie certaine.
            return (
                round(total, 1),
                yyyymmdd(through),
                False,
                "quotidien_en_attente",
            )

        total += value
        through = cursor
        cursor += timedelta(days=1)

    # La chaîne est continue jusqu'à hier : on peut ajouter aujourd'hui.
    if cursor == latest_day:
        if today_sum is not None and today_hours > 0:
            total += today_sum
            return (
                round(total, 1),
                latest_day_key,
                True,
                "quotidien_plus_paquets_horaires",
            )

        # Aucun RR1 disponible aujourd'hui : cumul certain jusqu'à hier.
        return (
            round(total, 1),
            yyyymmdd(through),
            False,
            "quotidien_sans_observation_du_jour",
        )

    return (
        round(total, 1),
        yyyymmdd(through),
        False,
        "quotidien_en_attente",
    )


# ---------------------------------------------------------------------
# Sortie
# ---------------------------------------------------------------------

MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)



def cached_period_total_live(
    cache_entry: dict,
    hourly: dict,
    latest_hour: datetime,
    total_key: str,
    through_key: str,
    period_start: date,
) -> Tuple[Optional[float], Optional[str], bool, str]:
    """Assemble un cumul quotidien avec les dernières heures sans créer de trou."""

    latest_day = latest_hour.date()
    latest_day_key = yyyymmdd(latest_day)
    daily_total = fnum(cache_entry.get(total_key))
    daily_through = parse_yyyymmdd(cache_entry.get(through_key))
    day_sums = hourly.get("day_sums") or {}
    day_hours = hourly.get("day_hours") or {}
    today_sum = fnum(hourly.get("today_sum"))
    today_hours = int(hourly.get("today_hours") or 0)

    if daily_total is not None and daily_through == latest_day:
        return round(daily_total, 1), latest_day_key, True, "quotidien_controle"

    if daily_total is None or daily_through is None:
        # Reconstruction possible seulement si tout le début de période est
        # encore contenu dans les 72 h de paquets chargés.
        if (latest_day - period_start).days > 2:
            return None, None, False, "base_quotidienne_absente"

        total = 0.0
        cursor = period_start
        while cursor < latest_day:
            key = yyyymmdd(cursor)
            hours = int(day_hours.get(key) or 0)
            value = fnum(day_sums.get(key))
            if value is None or hours < FULL_DAY_MIN_VALID_HOURS:
                return None, None, False, "base_quotidienne_absente"
            total += value
            cursor += timedelta(days=1)

        if today_sum is not None and today_hours > 0:
            total += today_sum
            return round(total, 1), latest_day_key, True, "paquets_horaires_uniquement"

        return None, None, False, "base_quotidienne_absente"

    total = float(daily_total)
    through = daily_through
    cursor = daily_through + timedelta(days=1)

    while cursor < latest_day:
        key = yyyymmdd(cursor)
        hours = int(day_hours.get(key) or 0)
        value = fnum(day_sums.get(key))
        if value is None or hours < FULL_DAY_MIN_VALID_HOURS:
            return round(total, 1), yyyymmdd(through), False, "quotidien_en_attente"
        total += value
        through = cursor
        cursor += timedelta(days=1)

    if cursor == latest_day:
        if today_sum is not None and today_hours > 0:
            total += today_sum
            return round(total, 1), latest_day_key, True, "quotidien_plus_paquets_horaires"
        return round(total, 1), yyyymmdd(through), False, "quotidien_sans_observation_du_jour"

    return round(total, 1), yyyymmdd(through), False, "quotidien_en_attente"

def main() -> int:
    print(f"=== Carte pluie Météo-France v{VERSION} ===")

    package_key = get_secret("METEOFRANCE_PACKAGE_OBS_KEY")
    obs_key = get_secret("METEOFRANCE_OBS_TOKEN")

    # 1. Paquets horaires
    latest_hour, station_geo, hourly = load_package_history(package_key)

    # 2. Noms
    names = load_station_names(obs_key)

    raw_station_ids = sorted(station_geo.keys())

    excluded_sapc_ids = [
        sid
        for sid in raw_station_ids
        if is_sapc_name(names.get(sid, sid))
    ]

    station_ids = [
        sid
        for sid in raw_station_ids
        if sid not in set(excluded_sapc_ids)
    ]

    department_anchors = choose_department_anchors(station_ids, names)
    print(f"Repères départementaux : {len(department_anchors)} station(s).")

    print(
        f"Stations paquets : {len(raw_station_ids)} | "
        f"SAPC exclues : {len(excluded_sapc_ids)} | "
        f"retenues : {len(station_ids)}"
    )

    # 3. Cache climatologique
    cache = load_cache()

    if BUILD_ARCHIVES:
        print("Mode archives : construction/rafraîchissement des records et normales.")
        update_daily_records(cache, station_ids, latest_hour)
        update_normals(cache, station_ids, latest_hour)
    else:
        print(
            "Mode rapide : archives historiques conservées depuis le cache. "
            "Utiliser le workflow dédié pour les initialiser/rafraîchir."
        )

    if REFRESH_DAILY:
        update_current_month(cache, station_ids, latest_hour)
    else:
        print(
            "Mode rapide : téléchargement quotidien mois/saison/année désactivé. "
            "Le cache quotidien existant est conservé."
        )

    cache["schema_version"] = CACHE_SCHEMA
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

    # 4. Stations finales
    month_key = str(latest_hour.month)
    latest_day_key = yyyymmdd(latest_hour.date())
    season = current_season_info(latest_hour.date())
    year_start = date(latest_hour.year, 1, 1)

    stations = []

    for sid in station_ids:
        geo = station_geo[sid]
        h = hourly.get(sid, {})
        clim = cache.get("stations", {}).get(sid, {})

        valid_hours = int(h.get("rr24_hours") or 0)
        valid_hours48 = int(h.get("rr48_hours") or 0)
        valid_hours72 = int(h.get("rr72_hours") or 0)

        rr24 = (
            round(float(h.get("rr24_sum") or 0.0), 1)
            if valid_hours >= RR24_MIN_VALID_HOURS
            else None
        )
        rr48 = (
            round(float(h.get("rr48_sum") or 0.0), 1)
            if valid_hours48 >= RR48_MIN_VALID_HOURS
            else None
        )
        rr72 = (
            round(float(h.get("rr72_sum") or 0.0), 1)
            if valid_hours72 >= RR72_MIN_VALID_HOURS
            else None
        )

        (
            rr_month_current,
            month_through,
            month_complete,
            month_method,
        ) = month_total_live(
            clim,
            h,
            latest_hour,
        )

        (
            rr_season_current,
            season_through,
            season_complete,
            season_method,
        ) = cached_period_total_live(
            clim,
            h,
            latest_hour,
            "season_daily_total",
            "season_daily_through",
            season["start"],
        )

        (
            rr_year_current,
            year_through,
            year_complete,
            year_method,
        ) = cached_period_total_live(
            clim,
            h,
            latest_hour,
            "year_daily_total",
            "year_daily_through",
            year_start,
        )

        rr_daily_record = fnum(clim.get("record_daily"))
        rr_daily_record_date = clim.get("record_daily_date")

        month_norms = clim.get("normal_months") or {}
        rr_month_mean = fnum(month_norms.get(month_key))
        rr_year_mean = fnum(clim.get("normal_year"))

        stations.append({
            "id": sid,
            "name": names.get(sid, sid),
            "department": department_code(sid),
            "department_anchor": sid in department_anchors,
            "lat": geo["lat"],
            "lon": geo["lon"],
            "date": h.get("latest_date"),
            "rr24": rr24,
            "rr24_hours": valid_hours,
            "rr24_complete": valid_hours == 24,
            "rr48": rr48,
            "rr48_hours": valid_hours48,
            "rr48_complete": valid_hours48 == 48,
            "rr72": rr72,
            "rr72_hours": valid_hours72,
            "rr72_complete": valid_hours72 == 72,

            "rr_month_current": (
                round(rr_month_current, 1)
                if rr_month_current is not None
                else None
            ),
            "rr_month_current_through": month_through,
            "rr_month_current_complete": bool(month_complete),
            "rr_month_current_method": month_method,

            "rr_season_current": (
                round(rr_season_current, 1)
                if rr_season_current is not None
                else None
            ),
            "rr_season_current_through": season_through,
            "rr_season_current_complete": bool(season_complete),
            "rr_season_current_method": season_method,

            "rr_year_current": (
                round(rr_year_current, 1)
                if rr_year_current is not None
                else None
            ),
            "rr_year_current_through": year_through,
            "rr_year_current_complete": bool(year_complete),
            "rr_year_current_method": year_method,

            "rr_daily_record": (
                round(rr_daily_record, 1)
                if rr_daily_record is not None
                else None
            ),
            "rr_daily_record_date": rr_daily_record_date,

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
                (clim.get("normal_month_counts") or {}).get(month_key)
            ),
            "normal_year_years": clim.get("normal_year_count"),
        })

    stations.sort(
        key=lambda st: (
            -(st["rr24"] if st["rr24"] is not None else -1),
            st["name"],
        )
    )

    def max_field(field: str) -> float:
        vals = [fnum(st.get(field)) for st in stations]
        vals = [v for v in vals if v is not None]
        return round(max(vals), 1) if vals else 0.0

    through_values = sorted(
        {
            st["rr_month_current_through"]
            for st in stations
            if st.get("rr_month_current_through")
        }
    )

    through_min = through_values[0] if through_values else None
    through_max = through_values[-1] if through_values else None

    live_month_stations = sum(
        1
        for st in stations
        if (
            st.get("rr_month_current_complete")
            and st.get("rr_month_current_through") == latest_day_key
        )
    )

    stale_month_stations = sum(
        1
        for st in stations
        if (
            st.get("rr_month_current") is not None
            and st.get("rr_month_current_through") != latest_day_key
        )
    )

    month_label = (
        f"{MONTHS_FR[latest_hour.month - 1]} {latest_hour.year}"
    )

    output = {
        "schema_version": 5,
        "module_version": VERSION,
        "status": "ok",
        "generated_at": iso(utcnow()),
        "latest_observation_at": iso(latest_hour),
        "title": "Cumuls de précipitations",
        "unit": "mm",
        "normal_period": f"{NORMAL_START}-{NORMAL_END}",

        "current_month": {
            "id": f"{latest_hour.year:04d}-{latest_hour.month:02d}",
            "label": month_label,
            "generated_at": cache.get("current_month_generated_at"),
            "through_min": through_min,
            "through_max": through_max,
            "latest_day": latest_day_key,
            "stations_live": live_month_stations,
            "stations_stale": stale_month_stations,
        },

        "current_season": {
            "id": season["id"],
            "label": season["label"],
            "start": yyyymmdd(season["start"]),
            "latest_day": latest_day_key,
        },

        "current_year": {
            "year": latest_hour.year,
            "start": yyyymmdd(year_start),
            "latest_day": latest_day_key,
        },

        "metrics": {
            "rr24": {
                "label": "24 h",
                "long_label": (
                    "Cumuls de précipitations sur les 24 dernières heures"
                ),
                "max": max_field("rr24"),
            },
            "rr48": {
                "label": "48 h",
                "long_label": (
                    "Cumuls de précipitations sur les 48 dernières heures"
                ),
                "max": max_field("rr48"),
            },
            "rr72": {
                "label": "72 h",
                "long_label": (
                    "Cumuls de précipitations sur les 72 dernières heures"
                ),
                "max": max_field("rr72"),
            },
            "rr_month_current": {
                "label": "Mois en cours",
                "long_label": (
                    f"Cumul depuis le 1er "
                    f"{MONTHS_FR[latest_hour.month - 1]}"
                ),
                "max": max_field("rr_month_current"),
            },
            "rr_season_current": {
                "label": "Saison en cours",
                "long_label": f"Cumul de la saison en cours - {season['label']}",
                "max": max_field("rr_season_current"),
            },
            "rr_year_current": {
                "label": "Année en cours",
                "long_label": f"Cumul depuis le 1er janvier {latest_hour.year}",
                "max": max_field("rr_year_current"),
            },
            "rr_daily_record": {
                "label": "Records",
                "long_label": "Record quotidien de précipitations par station",
                "max": max_field("rr_daily_record"),
            },
            "rr_month_mean": {
                "label": "Moy. du mois",
                "long_label": (
                    f"Cumul moyen de {MONTHS_FR[latest_hour.month - 1]} "
                    f"({NORMAL_START}-{NORMAL_END})"
                ),
                "max": max_field("rr_month_mean"),
            },
            "rr_year_mean": {
                "label": "Moy. annuelle",
                "long_label": (
                    f"Cumul moyen annuel ({NORMAL_START}-{NORMAL_END})"
                ),
                "max": max_field("rr_year_mean"),
            },
        },

        "stations_total": len(stations),
        "stations_rr24": sum(
            1 for st in stations if st["rr24"] is not None
        ),
        "stations_rr48": sum(
            1 for st in stations if st["rr48"] is not None
        ),
        "stations_rr72": sum(
            1 for st in stations if st["rr72"] is not None
        ),
        "stations_records": sum(
            1 for st in stations if st["rr_daily_record"] is not None
        ),
        "stations_excluded_sapc": len(excluded_sapc_ids),
        "hourly_cache_depth_hours": hourly_cache_depth(load_hourly_cache(), latest_hour),
        "hourly_cache_ready_48h": hourly_cache_depth(load_hourly_cache(), latest_hour) >= 48,
        "hourly_cache_ready_72h": hourly_cache_depth(load_hourly_cache(), latest_hour) >= 72,

        "source": {
            "observations": (
                "Météo-France - Package Observations V2"
            ),
            "rr24_method": (
                "Somme de RR1 sur les 24 paquets horaires les plus récents"
            ),
            "rr48_method": (
                "Somme de RR1 sur un cache glissant persistant des 48 dernières heures"
            ),
            "rr72_method": (
                "Somme de RR1 sur un cache glissant persistant des 72 dernières heures"
            ),
            "current_month": (
                "Données climatologiques quotidiennes Météo-France, "
                "complétées uniquement si la continuité horaire est vérifiée"
            ),
            "current_season_year": (
                "Météo-France - données climatologiques quotidiennes, "
                "complétées par les observations horaires du jour"
            ),
            "records": (
                "Météo-France - archives climatologiques quotidiennes depuis 1950 "
                "ou depuis l'ouverture de la station, selon disponibilité"
            ),
            "normals": (
                "Météo-France - données climatologiques mensuelles"
            ),
            "normal_period": f"{NORMAL_START}-{NORMAL_END}",
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
    print(f"SAPC exclues : {len(excluded_sapc_ids)}")
    print(f"Stations RR24 exploitables : {output['stations_rr24']}")
    print(f"Stations RR48 exploitables : {output['stations_rr48']}")
    print(f"Stations RR72 exploitables : {output['stations_rr72']}")
    print(f"Stations avec record : {output['stations_records']}")
    print(f"Stations mois à jour : {live_month_stations}")
    print(f"Stations mois en attente : {stale_month_stations}")
    print(f"Cumul 24 h maximal : {output['metrics']['rr24']['max']} mm")
    print(f"Cumul 48 h maximal : {output['metrics']['rr48']['max']} mm")
    print(f"Cumul 72 h maximal : {output['metrics']['rr72']['max']} mm")
    print(
        f"Cumul mois maximal : "
        f"{output['metrics']['rr_month_current']['max']} mm"
    )
    print(
        f"Cumul saison maximal : "
        f"{output['metrics']['rr_season_current']['max']} mm"
    )
    print(
        f"Cumul année maximal : "
        f"{output['metrics']['rr_year_current']['max']} mm"
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERREUR FATALE : {exc}", file=sys.stderr)
        raise

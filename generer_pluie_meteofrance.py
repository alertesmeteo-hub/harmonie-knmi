#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Carte pluie Météo-France
========================

Produit un JSON unique pour le module WordPress :
- cumul de précipitations sur 24 h (DPObs / observations)
- cumul du mois en cours (données climatologiques quotidiennes ouvertes)
- cumul moyen du mois 1991-2020
- cumul moyen annuel 1991-2020

Authentification DPObs V2 :
1) METEOFRANCE_OBS_TOKEN (Bearer déjà généré), ou
2) METEOFRANCE_API_KEY (compatibilité avec un ancien nom de secret), ou
3) METEOFRANCE_APPLICATION_ID (permet de générer automatiquement un Bearer).

Les moyennes 1991-2020 ne sont PAS recalculées à chaque exécution :
elles sont conservées dans cache_pluie_climatologie.json.
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

OBS_V2 = "https://public-api.meteofrance.fr/public/DPObs/v2"
OBS_V1 = "https://public-api.meteofrance.fr/public/DPObs/v1"
TOKEN_URL = "https://portail-api.meteofrance.fr/token"

# Jeux officiels Météo-France publiés sur data.gouv.fr
DATASET_MONTHLY_MAIN = "6569b3d7d193b4daf2b43edc"
DATASET_MONTHLY_COMPLEMENT = "6791045ba9116b0a49e6a720"
DATASET_DAILY_MAIN = "6569b51ae64326786e4e8e1a"
DATASET_DAILY_COMPLEMENT = "679103e271c55090cfe86871"

DATAGOUV_DATASET_API = "https://www.data.gouv.fr/api/1/datasets/{dataset_id}/"
TABULAR_API = "https://tabular-api.data.gouv.fr/api/resources/{resource_id}/data/csv/"

OUTPUT = Path("observations_pluie.json")
CACHE = Path("cache_pluie_climatologie.json")

NORMAL_START = 1991
NORMAL_END = 2020

# Actualisation des données climatologiques.
CURRENT_MONTH_CACHE_HOURS = 8
NORMALS_CACHE_DAYS = 45

HTTP_TIMEOUT = 60
UA = "alertes-meteo-carte-pluie/2.0 (+https://alertes-meteo.com/)"

session = requests.Session()
session.headers.update({"User-Agent": UA})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def finite_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        x = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def clean_rr(value: Any) -> Optional[float]:
    x = finite_float(value)
    if x is None:
        return None
    # Écarte les valeurs sentinelles ou physiquement aberrantes.
    if x < 0 or x > 10000:
        return None
    return x


def env_token() -> Optional[str]:
    for key in ("METEOFRANCE_OBS_TOKEN", "METEOFRANCE_API_KEY"):
        value = os.getenv(key, "").strip()
        if value:
            if value.lower().startswith("bearer "):
                value = value[7:].strip()
            return value
    return None


def get_bearer_token() -> str:
    direct = env_token()
    if direct:
        return direct

    application_id = os.getenv("METEOFRANCE_APPLICATION_ID", "").strip()
    if not application_id:
        raise RuntimeError(
            "Secret Météo-France absent. Définissez METEOFRANCE_OBS_TOKEN "
            "ou METEOFRANCE_APPLICATION_ID dans GitHub."
        )

    r = session.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {application_id}"},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Le serveur Météo-France n'a pas renvoyé access_token.")
    return token


def mf_get(path: str, token: str, params: Optional[dict] = None) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/csv, */*",
    }

    last_error = None
    # V2 en priorité, V1 seulement comme filet de sécurité.
    for base in (OBS_V2, OBS_V1):
        url = base + path
        try:
            r = session.get(url, headers=headers, params=params or {}, timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                last_error = RuntimeError(f"{url} -> HTTP 404")
                continue
            r.raise_for_status()
            return r
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Échec DPObs pour {path}: {last_error}")


def unwrap_records(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "records", "result", "results", "features"):
            value = payload.get(key)
            if isinstance(value, list):
                if key == "features":
                    out = []
                    for feat in value:
                        if not isinstance(feat, dict):
                            continue
                        props = dict(feat.get("properties") or {})
                        geom = feat.get("geometry") or {}
                        coords = geom.get("coordinates")
                        if isinstance(coords, list) and len(coords) >= 2:
                            props.setdefault("lon", coords[0])
                            props.setdefault("lat", coords[1])
                        out.append(props)
                    return out
                return [x for x in value if isinstance(x, dict)]
    return []


def station_id_of(row: dict) -> Optional[str]:
    for key in ("numer_sta", "id_station", "NUM_POSTE", "num_poste", "geo_id_insee"):
        value = row.get(key)
        if value is not None and str(value).strip():
            s = str(value).strip()
            # Préserve les zéros initiaux si déjà présents.
            if s.endswith(".0") and s[:-2].isdigit():
                s = s[:-2]
            return s
    return None


def first_value(row: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def parse_csv_bytes(raw: bytes, content_encoding: str = "") -> List[dict]:
    if raw[:2] == b"\x1f\x8b" or "gzip" in (content_encoding or "").lower():
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass

    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:8192]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    out = []
    for row in reader:
        clean = {}
        for k, v in row.items():
            if k is None:
                continue
            clean[str(k).strip()] = v.strip() if isinstance(v, str) else v
        out.append(clean)
    return out


def load_station_metadata(token: str) -> Dict[str, dict]:
    """
    Récupère la liste des stations DPObs V2.
    Le service peut renvoyer du CSV ou du JSON suivant la route/version.
    """
    try:
        r = mf_get("/liste-stations", token)
    except Exception as exc:
        print(f"[AVERTISSEMENT] Liste des stations indisponible : {exc}")
        return {}

    content_type = (r.headers.get("content-type") or "").lower()
    rows: List[dict]
    if "json" in content_type:
        try:
            rows = unwrap_records(r.json())
        except Exception:
            rows = []
    else:
        rows = parse_csv_bytes(r.content, r.headers.get("content-encoding", ""))

    mapping: Dict[str, dict] = {}
    for row in rows:
        sid = station_id_of(row)
        if not sid:
            continue

        lat = finite_float(first_value(row, ("lat", "LAT", "latitude", "Latitude")))
        lon = finite_float(first_value(row, ("lon", "LON", "longitude", "Longitude")))
        name = first_value(row, ("nom_usuel", "NOM_USUEL", "nom", "Nom", "name", "libelle"))

        mapping[sid] = {
            "name": str(name).strip() if name else sid,
            "lat": lat,
            "lon": lon,
        }
    return mapping


def load_synop(token: str, metadata: Dict[str, dict]) -> Tuple[List[dict], Optional[datetime], str]:
    """
    Charge le paquet SYNOP national. Une seule requête utile pour la carte 24 h.
    """
    r = mf_get("/synop", token, params={"format": "json"})
    try:
        rows = unwrap_records(r.json())
    except Exception as exc:
        raise RuntimeError(f"Réponse SYNOP non JSON : {exc}")

    stations: Dict[str, dict] = {}
    latest_dt = None

    for row in rows:
        sid = station_id_of(row)
        if not sid:
            continue

        rr24 = clean_rr(first_value(row, ("rr24", "RR24", "rr_24")))
        lat = finite_float(first_value(row, ("lat", "LAT", "latitude")))
        lon = finite_float(first_value(row, ("lon", "LON", "longitude")))

        meta = metadata.get(sid, {})
        if lat is None:
            lat = finite_float(meta.get("lat"))
        if lon is None:
            lon = finite_float(meta.get("lon"))

        if lat is None or lon is None:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        dt = None
        for key in ("date", "validity_time", "reference_time", "insert_time"):
            dt = parse_iso(row.get(key))
            if dt:
                break

        name = first_value(row, ("nom", "nom_usuel", "NOM_USUEL", "name"))
        if not name:
            name = meta.get("name") or sid

        old = stations.get(sid)
        old_dt = parse_iso(old.get("date")) if old else None
        if old is None or (dt and (old_dt is None or dt > old_dt)):
            stations[sid] = {
                "id": sid,
                "name": str(name),
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "date": iso(dt),
                "rr24": round(rr24, 1) if rr24 is not None else None,
            }

        if dt and (latest_dt is None or dt > latest_dt):
            latest_dt = dt

    # Le nom de la route utilisée est conservé dans le JSON.
    return list(stations.values()), latest_dt, r.url


def station_department(sid: str) -> str:
    """
    Déduit le code de département des identifiants de stations Météo-France.
    971/972/... utilisent trois chiffres ; métropole généralement deux.
    """
    digits = re.sub(r"\D", "", sid)
    if digits.startswith(("971", "972", "973", "974", "975", "976", "977", "978", "984", "986", "987", "988")):
        return digits[:3]
    return digits[:2]


_DATASET_CACHE: Dict[str, List[dict]] = {}


def dataset_resources(dataset_id: str) -> List[dict]:
    if dataset_id in _DATASET_CACHE:
        return _DATASET_CACHE[dataset_id]

    r = session.get(
        DATAGOUV_DATASET_API.format(dataset_id=dataset_id),
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    resources = r.json().get("resources", [])
    resources = [x for x in resources if isinstance(x, dict)]
    _DATASET_CACHE[dataset_id] = resources
    return resources


def resource_period(title: str) -> Optional[Tuple[int, int]]:
    # Exemples attendus : 1950-2023, latest-2025-2026, avant_1950-1949...
    pairs = re.findall(r"(?<!\d)(\d{4})-(\d{4})(?!\d)", title)
    if not pairs:
        return None
    a, b = pairs[-1]
    return int(a), int(b)


def resource_matches_department(title: str, dep: str) -> bool:
    t = title.lower().replace("é", "e")
    variants = (
        f"departement_{dep}",
        f"departement-{dep}",
        f"departement {dep}",
        f"dep_{dep}",
        f"dep-{dep}",
    )
    return any(v in t for v in variants)


def pick_resources(
    dataset_ids: Iterable[str],
    dep: str,
    start_year: int,
    end_year: int,
    daily_rr_only: bool = False,
) -> List[dict]:
    selected = []
    seen = set()

    for dataset_id in dataset_ids:
        for res in dataset_resources(dataset_id):
            if str(res.get("type", "main")).lower() not in ("main", ""):
                continue
            title = str(res.get("title") or "")
            if not resource_matches_department(title, dep):
                continue

            if daily_rr_only:
                low = title.lower()
                # Le jeu quotidien sépare actuellement RR-T-Vent et autres paramètres.
                # Si le titre précise le sous-groupe, on ne garde que RR.
                if ("rr-t-vent" not in low and "rr_t_vent" not in low
                        and "autres-parametres" in low):
                    continue

            period = resource_period(title)
            if period:
                p0, p1 = period
                if p1 < start_year or p0 > end_year:
                    continue

            rid = res.get("id")
            if rid and rid not in seen:
                selected.append(res)
                seen.add(rid)

    return selected


def tabular_rows(resource: dict, station_ids: List[str]) -> List[dict]:
    rid = resource.get("id")
    if not rid:
        return []

    params = {"NUM_POSTE__in": ",".join(station_ids)}
    url = TABULAR_API.format(resource_id=rid)

    try:
        r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        rows = parse_csv_bytes(r.content, r.headers.get("content-encoding", ""))
        if rows:
            return rows
    except Exception as exc:
        print(f"[INFO] Tabular API indisponible pour {rid}: {exc}")

    # Filet de sécurité : téléchargement du fichier ressource.
    direct_url = resource.get("url")
    if not direct_url:
        return []

    r = session.get(direct_url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    all_rows = parse_csv_bytes(r.content, r.headers.get("content-encoding", ""))
    wanted = set(station_ids)
    return [row for row in all_rows if station_id_of(row) in wanted]


def date_digits(value: Any) -> str:
    if value is None:
        return ""
    # Accepte AAAAMMJJ, AAAAMM ou ISO.
    s = re.sub(r"\D", "", str(value))
    return s


def current_month_totals(station_ids: List[str], now: datetime) -> Tuple[Dict[str, float], Dict[str, str]]:
    by_dep: Dict[str, List[str]] = defaultdict(list)
    for sid in station_ids:
        by_dep[station_department(sid)].append(sid)

    totals: Dict[str, float] = defaultdict(float)
    latest_day: Dict[str, str] = {}
    year = now.year
    month_key = f"{year:04d}{now.month:02d}"

    for dep, ids in sorted(by_dep.items()):
        resources = pick_resources(
            (DATASET_DAILY_MAIN, DATASET_DAILY_COMPLEMENT),
            dep,
            year,
            year,
            daily_rr_only=True,
        )
        if not resources:
            print(f"[INFO] Aucun fichier quotidien trouvé pour département {dep}")
            continue

        for res in resources:
            try:
                rows = tabular_rows(res, ids)
            except Exception as exc:
                print(f"[AVERTISSEMENT] Quotidien dep {dep}: {exc}")
                continue

            for row in rows:
                sid = station_id_of(row)
                if sid not in ids:
                    continue

                d = date_digits(first_value(row, ("AAAAMMJJ", "DATE", "date")))
                if len(d) < 8 or not d.startswith(month_key):
                    continue

                rr = clean_rr(first_value(row, ("RR", "rr", "PRECIP", "PRECIPITATION")))
                if rr is None:
                    continue

                # Protection contre les doublons si plusieurs ressources se chevauchent.
                day = d[:8]
                dedup_key = f"{sid}:{day}"
                # stock temporaire attaché à la fonction
                if not hasattr(current_month_totals, "_seen"):
                    current_month_totals._seen = set()
                if dedup_key in current_month_totals._seen:
                    continue
                current_month_totals._seen.add(dedup_key)

                totals[sid] += rr
                if day > latest_day.get(sid, ""):
                    latest_day[sid] = day

    # Nettoyage de l'attribut temporaire pour les tests / appels successifs.
    if hasattr(current_month_totals, "_seen"):
        delattr(current_month_totals, "_seen")

    return {k: round(v, 1) for k, v in totals.items()}, latest_day


def compute_normals(station_ids: List[str]) -> Dict[str, dict]:
    by_dep: Dict[str, List[str]] = defaultdict(list)
    for sid in station_ids:
        by_dep[station_department(sid)].append(sid)

    # sid -> year -> month -> rr
    values: Dict[str, Dict[int, Dict[int, float]]] = defaultdict(lambda: defaultdict(dict))

    for dep, ids in sorted(by_dep.items()):
        resources = pick_resources(
            (DATASET_MONTHLY_MAIN, DATASET_MONTHLY_COMPLEMENT),
            dep,
            NORMAL_START,
            NORMAL_END,
            daily_rr_only=False,
        )
        if not resources:
            print(f"[INFO] Aucun fichier mensuel 1991-2020 trouvé pour département {dep}")
            continue

        for res in resources:
            try:
                rows = tabular_rows(res, ids)
            except Exception as exc:
                print(f"[AVERTISSEMENT] Mensuel dep {dep}: {exc}")
                continue

            for row in rows:
                sid = station_id_of(row)
                if sid not in ids:
                    continue

                d = date_digits(first_value(row, ("AAAAMM", "DATE", "date")))
                if len(d) < 6:
                    continue

                try:
                    year = int(d[:4])
                    month = int(d[4:6])
                except ValueError:
                    continue

                if not (NORMAL_START <= year <= NORMAL_END and 1 <= month <= 12):
                    continue

                rr = clean_rr(first_value(row, ("RR", "rr", "PRECIP", "PRECIPITATION")))
                if rr is None:
                    continue

                values[sid][year][month] = rr

    normals: Dict[str, dict] = {}

    for sid in station_ids:
        years = values.get(sid, {})
        month_normals: Dict[str, Optional[float]] = {}
        month_counts: Dict[str, int] = {}

        for month in range(1, 13):
            vals = [
                months[month]
                for year, months in years.items()
                if NORMAL_START <= year <= NORMAL_END and month in months
            ]
            month_counts[str(month)] = len(vals)
            # 15 ans minimum pour éviter une "moyenne" trop fragile.
            month_normals[str(month)] = round(sum(vals) / len(vals), 1) if len(vals) >= 15 else None

        valid_months = [month_normals[str(m)] for m in range(1, 13)]
        normal_year = None
        if all(v is not None for v in valid_months):
            normal_year = round(sum(float(v) for v in valid_months), 1)

        normals[sid] = {
            "normal_months": month_normals,
            "normal_month_counts": month_counts,
            "normal_year": normal_year,
        }

    return normals


def load_cache() -> dict:
    if not CACHE.exists():
        return {"schema_version": 2, "stations": {}}
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("cache non objet")
        data.setdefault("stations", {})
        return data
    except Exception as exc:
        print(f"[AVERTISSEMENT] Cache ignoré : {exc}")
        return {"schema_version": 2, "stations": {}}


def cache_age_hours(cache: dict, key: str) -> float:
    dt = parse_iso(cache.get(key))
    if not dt:
        return 10**9
    return (utcnow() - dt).total_seconds() / 3600.0


def update_climate_cache(cache: dict, station_ids: List[str], now: datetime) -> dict:
    cache.setdefault("stations", {})
    month_id = f"{now.year:04d}-{now.month:02d}"

    current_month_stale = (
        cache.get("current_month_id") != month_id
        or cache_age_hours(cache, "current_month_generated_at") >= CURRENT_MONTH_CACHE_HOURS
    )

    known_normals = sum(
        1 for sid in station_ids
        if cache["stations"].get(sid, {}).get("normal_year") is not None
    )
    coverage = known_normals / max(1, len(station_ids))
    normals_stale = (
        cache_age_hours(cache, "normals_generated_at") >= NORMALS_CACHE_DAYS * 24
        or coverage < 0.80
    )

    if current_month_stale:
        print("Actualisation du cumul du mois en cours...")
        totals, latest_days = current_month_totals(station_ids, now)
        for sid in station_ids:
            entry = cache["stations"].setdefault(sid, {})
            entry["month_current"] = totals.get(sid)
            entry["month_current_through"] = latest_days.get(sid)
        cache["current_month_id"] = month_id
        cache["current_month_generated_at"] = iso(utcnow())

    if normals_stale:
        print("Calcul / actualisation des moyennes 1991-2020...")
        normals = compute_normals(station_ids)
        for sid, data in normals.items():
            entry = cache["stations"].setdefault(sid, {})
            entry.update(data)
        cache["normals_generated_at"] = iso(utcnow())
        cache["normal_period"] = f"{NORMAL_START}-{NORMAL_END}"

    CACHE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return cache


def month_name_fr(month: int) -> str:
    names = (
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    )
    return names[month - 1]


def main() -> int:
    now = utcnow()
    print("=== Carte pluie Météo-France ===")
    print("Authentification DPObs...")
    token = get_bearer_token()

    print("Chargement des métadonnées stations...")
    metadata = load_station_metadata(token)

    print("Chargement du paquet SYNOP...")
    obs, latest_obs, obs_url = load_synop(token, metadata)
    if not obs:
        raise RuntimeError("Aucune station récupérée depuis DPObs.")

    station_ids = sorted({s["id"] for s in obs})
    print(f"{len(station_ids)} station(s) d'observation récupérée(s).")

    cache = load_cache()
    cache = update_climate_cache(cache, station_ids, now)

    current_month = str(now.month)
    stations = []
    for station in obs:
        sid = station["id"]
        clim = cache.get("stations", {}).get(sid, {})
        month_norms = clim.get("normal_months") or {}

        item = dict(station)
        item["rr_month_current"] = clim.get("month_current")
        item["rr_month_current_through"] = clim.get("month_current_through")
        item["rr_month_mean"] = month_norms.get(current_month)
        item["rr_year_mean"] = clim.get("normal_year")
        item["normal_month_years"] = (clim.get("normal_month_counts") or {}).get(current_month)
        stations.append(item)

    stations.sort(
        key=lambda s: (
            -(s.get("rr24") if s.get("rr24") is not None else -1),
            s.get("name") or "",
        )
    )

    def vmax(field: str) -> float:
        vals = [finite_float(s.get(field)) for s in stations]
        vals = [v for v in vals if v is not None]
        return round(max(vals), 1) if vals else 0.0

    month_label = f"{month_name_fr(now.month)} {now.year}"

    output = {
        "schema_version": 2,
        "status": "ok",
        "generated_at": iso(now),
        "latest_observation_at": iso(latest_obs),
        "title": "Cumuls de précipitations",
        "unit": "mm",
        "current_month": {
            "id": f"{now.year:04d}-{now.month:02d}",
            "label": month_label,
            "generated_at": cache.get("current_month_generated_at"),
        },
        "normal_period": f"{NORMAL_START}-{NORMAL_END}",
        "metrics": {
            "rr24": {
                "label": "24 h",
                "long_label": "Cumuls de précipitations sur 24 h",
                "max": vmax("rr24"),
            },
            "rr_month_current": {
                "label": "Mois en cours",
                "long_label": f"Cumul depuis le 1er {month_name_fr(now.month)}",
                "max": vmax("rr_month_current"),
            },
            "rr_month_mean": {
                "label": "Moy. du mois",
                "long_label": f"Cumul moyen de {month_name_fr(now.month)} ({NORMAL_START}-{NORMAL_END})",
                "max": vmax("rr_month_mean"),
            },
            "rr_year_mean": {
                "label": "Moy. annuelle",
                "long_label": f"Cumul moyen annuel ({NORMAL_START}-{NORMAL_END})",
                "max": vmax("rr_year_mean"),
            },
        },
        "stations_total": len(stations),
        "source": {
            "observations": "Météo-France - API Données d'observation (DPObs)",
            "observations_endpoint": obs_url,
            "climatology": "Météo-France - Données climatologiques de base via data.gouv.fr",
            "normal_method": "Moyennes calculées station par station sur 1991-2020",
        },
        "stations": stations,
    }

    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    print(f"JSON généré : {OUTPUT}")
    print(f"24 h max : {output['metrics']['rr24']['max']} mm")
    print(f"Mois max : {output['metrics']['rr_month_current']['max']} mm")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERREUR FATALE : {exc}", file=sys.stderr)
        raise

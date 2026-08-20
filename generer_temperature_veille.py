#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — Températures minimales / maximales de la veille
Version 1.0.0

Le script récupère les paquets horaires Météo-France couvrant exactement la
journée civile précédente en Europe/Paris. Pour chaque station :
- minimale : minimum des champs TN horaires, avec T en secours ;
- maximale : maximum des champs TX horaires, avec T en secours.

La fenêtre est convertie en UTC avant interrogation de l'API, ce qui permet de
gérer correctement les changements heure d'été / heure d'hiver (23, 24 ou 25 h).
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
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import requests


VERSION = "1.0.0"
SCHEMA_VERSION = 1

PACKAGE_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)
DPOBS_STATIONS_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPObs/v2/liste-stations"
)

OUTPUT = Path("observations_temperature_veille.json")
PARIS = ZoneInfo("Europe/Paris")
HTTP_TIMEOUT = 90
PACKAGE_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "1.15"))
MAX_HTTP_ATTEMPTS = 4

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-temperature-veille/{VERSION}",
})


# -----------------------------------------------------------------------------
# Utilitaires
# -----------------------------------------------------------------------------

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
    # Les observations peuvent être exprimées en kelvins.
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

    rows = []
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        clean = {}
        for key, value in row.items():
            if key is None:
                continue
            clean[str(key).strip()] = value.strip() if isinstance(value, str) else value
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
        raise RuntimeError(f"Secret {name} vide.")
    return value


def api_headers(key: str) -> dict:
    return {"apikey": key, "accept": "*/*"}


def is_sapc(name: str) -> bool:
    words = re.sub(r"[^A-Z0-9]+", " ", str(name).upper()).split()
    return "SAPC" in words


def fr_date_label(d: date) -> str:
    weekdays = (
        "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"
    )
    months = (
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    )
    return f"{weekdays[d.weekday()]} {d.day} {months[d.month - 1]} {d.year}"


# -----------------------------------------------------------------------------
# Météo-France
# -----------------------------------------------------------------------------

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


def load_station_names(key: str) -> Dict[str, str]:
    print("Chargement liste-stations…")
    response = session.get(
        DPOBS_STATIONS_URL,
        headers=api_headers(key),
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code != 200:
        print("::warning::liste-stations indisponible :", response.status_code)
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

    names: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = station_id(row)
        if not sid:
            continue
        name = first(row, ("nom_usuel", "NOM_USUEL", "nom", "NOM", "name", "libelle"))
        if name:
            names[sid] = str(name).strip()
    print("Noms récupérés :", len(names))
    return names


def previous_local_day_window(now_utc: Optional[datetime] = None):
    if now_utc is None:
        now_utc = utcnow()
    now_local = now_utc.astimezone(PARIS)
    target_date = now_local.date() - timedelta(days=1)
    next_date = target_date + timedelta(days=1)

    start_local = datetime.combine(target_date, dtime.min, tzinfo=PARIS)
    end_local = datetime.combine(next_date, dtime.min, tzinfo=PARIS)
    return target_date, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def load_yesterday(key: str):
    target_date, start_utc, end_utc = previous_local_day_window()

    print("Journée locale :", fr_date_label(target_date))
    print("Fenêtre UTC :", iso(start_utc), "→", iso(end_utc))

    stations = defaultdict(lambda: {
        "lat": None,
        "lon": None,
        "tmin_yesterday": None,
        "tmin_time": None,
        "tmax_yesterday": None,
        "tmax_time": None,
        "sample_times": set(),
        "tn_count": 0,
        "tx_count": 0,
        "fallback_t_count": 0,
    })

    expected_hours = 0
    available_hours = 0
    current = start_utc

    while current < end_utc:
        expected_hours += 1
        print("Paquet :", iso(current))
        response = package_request(key, current)
        if response is None:
            print("::warning::Paquet absent :", iso(current))
            current += timedelta(hours=1)
            time.sleep(PACKAGE_DELAY)
            continue

        available_hours += 1
        rows = parse_csv(response.content)
        print("  lignes :", len(rows))

        for row in rows:
            sid = station_id(row)
            if not sid:
                continue

            validity = parse_iso(first(row, ("validity_time", "reference_time", "date"))) or current
            validity_local = validity.astimezone(PARIS)
            if validity_local.date() != target_date:
                continue

            t = celsius(first(row, ("t", "T")))
            tn = celsius(first(row, ("tn", "TN")))
            tx = celsius(first(row, ("tx", "TX")))

            # Un paquet sans T/TN/TX n'apporte rien au module.
            if t is None and tn is None and tx is None:
                continue

            item = stations[sid]

            lat = fnum(first(row, ("lat", "LAT", "latitude")))
            lon = fnum(first(row, ("lon", "LON", "longitude")))
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                item["lat"] = round(lat, 6)
                item["lon"] = round(lon, 6)

            min_candidate = tn if tn is not None else t
            max_candidate = tx if tx is not None else t

            if tn is not None:
                item["tn_count"] += 1
            if tx is not None:
                item["tx_count"] += 1
            if (tn is None or tx is None) and t is not None:
                item["fallback_t_count"] += 1

            if min_candidate is not None and (
                item["tmin_yesterday"] is None or min_candidate < item["tmin_yesterday"]
            ):
                item["tmin_yesterday"] = min_candidate
                item["tmin_time"] = iso(validity)

            if max_candidate is not None and (
                item["tmax_yesterday"] is None or max_candidate > item["tmax_yesterday"]
            ):
                item["tmax_yesterday"] = max_candidate
                item["tmax_time"] = iso(validity)

            item["sample_times"].add(iso(validity))

        current += timedelta(hours=1)
        time.sleep(PACKAGE_DELAY)

    return target_date, start_utc, end_utc, expected_hours, available_hours, dict(stations)


# -----------------------------------------------------------------------------
# Production JSON
# -----------------------------------------------------------------------------

def main() -> int:
    print(f"=== Températures de la veille v{VERSION} ===")

    package_key = get_secret("METEOFRANCE_PACKAGE_OBS_KEY")
    obs_key = os.getenv("METEOFRANCE_OBS_TOKEN", "").strip()
    if not obs_key:
        # La liste des noms n'est pas indispensable : même clé en secours.
        obs_key = package_key

    target_date, start_utc, end_utc, expected_hours, available_hours, raw = load_yesterday(package_key)
    names = load_station_names(obs_key)

    stations = []
    excluded_sapc = 0

    for sid, obs in raw.items():
        if obs.get("lat") is None or obs.get("lon") is None:
            continue
        name = names.get(sid, sid)
        if is_sapc(name):
            excluded_sapc += 1
            continue

        tmin = fnum(obs.get("tmin_yesterday"))
        tmax = fnum(obs.get("tmax_yesterday"))
        if tmin is None and tmax is None:
            continue

        stations.append({
            "id": sid,
            "name": name,
            "lat": obs["lat"],
            "lon": obs["lon"],
            "tmin_yesterday": round(tmin, 1) if tmin is not None else None,
            "tmin_time": obs.get("tmin_time"),
            "tmax_yesterday": round(tmax, 1) if tmax is not None else None,
            "tmax_time": obs.get("tmax_time"),
            "samples": len(obs.get("sample_times") or []),
            "tn_samples": int(obs.get("tn_count") or 0),
            "tx_samples": int(obs.get("tx_count") or 0),
            "fallback_t_samples": int(obs.get("fallback_t_count") or 0),
        })

    stations.sort(key=lambda st: (st["name"], st["id"]))

    def vals(field: str) -> List[float]:
        return [float(st[field]) for st in stations if st.get(field) is not None]

    def vmin(field: str) -> Optional[float]:
        x = vals(field)
        return round(min(x), 1) if x else None

    def vmax(field: str) -> Optional[float]:
        x = vals(field)
        return round(max(x), 1) if x else None

    output = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "status": "ok",
        "generated_at": iso(utcnow()),
        "date_local": target_date.isoformat(),
        "date_label": fr_date_label(target_date),
        "period_start_utc": iso(start_utc),
        "period_end_utc": iso(end_utc),
        "timezone": "Europe/Paris",
        "title": "Températures minimales et maximales de la veille",
        "unit": "°C",
        "package_hours_expected": expected_hours,
        "package_hours_available": available_hours,
        "metrics": {
            "tmin_yesterday": {
                "label": "Minimales de la veille",
                "long_label": "Températures minimales observées durant la journée civile précédente",
                "min": vmin("tmin_yesterday"),
                "max": vmax("tmin_yesterday"),
                "stations": len(vals("tmin_yesterday")),
            },
            "tmax_yesterday": {
                "label": "Maximales de la veille",
                "long_label": "Températures maximales observées durant la journée civile précédente",
                "min": vmin("tmax_yesterday"),
                "max": vmax("tmax_yesterday"),
                "stations": len(vals("tmax_yesterday")),
            },
        },
        "stations_total": len(stations),
        "stations_excluded_sapc": excluded_sapc,
        "source": {
            "provider": "Météo-France",
            "api": "DPPaquetObs v2 - paquets horaires stations",
            "method": "Minimum de TN horaires et maximum de TX horaires ; T utilisée en secours si TN/TX absente",
            "calendar_day": "Journée civile Europe/Paris",
        },
        "stations": stations,
    }

    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    print("=== TERMINÉ ===")
    print("Date :", output["date_label"])
    print("Paquets :", available_hours, "/", expected_hours)
    print("Stations :", output["stations_total"])
    print("Minimales :", output["metrics"]["tmin_yesterday"]["stations"])
    print("Maximales :", output["metrics"]["tmax_yesterday"]["stations"])
    print("Extrême mini France :", output["metrics"]["tmin_yesterday"]["min"], "°C")
    print("Extrême maxi France :", output["metrics"]["tmax_yesterday"]["max"], "°C")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

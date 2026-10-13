#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alertes-Meteo.com — Carte interactive des records de température
Version 1.0.0

Fonctions :
- bootstrap compact des historiques de records par département à partir des
  données quotidiennes climatologiques Météo-France ;
- génération de la carte LIVE du jour à partir des paquets horaires ;
- génération / reconstruction d'une archive mensuelle à partir des données
  quotidiennes contrôlées.

Une couche récente (cache/recent.json : données quotidiennes contrôlées ; cache/provisional.json et provisional/ : paquets
horaires des derniers jours) complète les caches, figés à leur date de construction, pour que les records d'un même mois,
d'une même quinzaine ou de l'année s'enchaînent d'un jour à l'autre.

Le cache historique conserve uniquement les CHANGEMENTS de records (progression),
pas toutes les observations quotidiennes. Cela permet de déterminer le record
qui existait AVANT n'importe quelle date sans stocker des dizaines de millions
 de lignes.
"""
from __future__ import annotations

import argparse
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

VERSION = "1.1.0"
SCHEMA_VERSION = 1
BUILD_ID = "records-temperature-map-20260820"

OUT_ROOT = Path("records_temperature")
CACHE_DIR = OUT_ROOT / "cache"
ARCHIVE_DIR = OUT_ROOT / "archive"
LIVE_FILE = OUT_ROOT / "live.json"
INDEX_FILE = OUT_ROOT / "index.json"
META_FILE = CACHE_DIR / "stations-meta.json"  # fiches des stations, version compacte
RECENT_FILE = CACHE_DIR / "recent.json"  # valeurs quotidiennes contrôlées postérieures à la construction des caches
PROVISIONAL_FILE = CACHE_DIR / "provisional.json"  # extrêmes provisoires issus des paquets horaires
PROVISIONAL_DIR = OUT_ROOT / "provisional"  # jours récents pas encore couverts par les données contrôlées

DATASET_ID = "6569b51ae64326786e4e8e1a"
DATASET_API = f"https://www.data.gouv.fr/api/1/datasets/{DATASET_ID}/"
STATIONS_META_URL = "https://www.data.gouv.fr/fr/datasets/r/1fe544d8-4615-4642-a307-5956a7d90922"

PACKAGE_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)

HTTP_TIMEOUT = 120
META_MAX_AGE_DAYS = 7  # le fichier des fiches de stations (≈ 200 Mo) n'est relu qu'une fois par semaine
RECENT_MARGIN_DAYS = 10  # recouvrement avec la période déjà couverte par les caches
PROVISIONAL_KEEP_DAYS = 6
MIN_STATIONS_JOUR = 200  # en dessous, un jour récent est considéré sans relevés horaires conservés
BACKFILL_MAX_HOURS = 72  # reprise maximale des paquets horaires manquants
RECENT_MAX_DAYS = 240  # au-delà, les caches sont trop anciens : à reconstruire
NEAR_DELTA_C = 1.0
EQUAL_EPS = 0.05
PACKAGE_LOOKBACK_HOURS = 4
LIVE_HISTORY_HOURS = 32
PACKAGE_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "0.20"))

# Métropole : 01-95. Le jeu quotidien de Météo-France range la Corse sous le code « 20 » (pas 2A / 2B).
DEPARTMENTS = [f"{x:02d}" for x in range(1, 96)]

MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

# Régions administratives métropolitaines. Utilisées uniquement pour le filtre UI.
REGION_BY_DEP = {}
def _region(name: str, deps: str) -> None:
    for d in deps.split(): REGION_BY_DEP[d] = name
_region("Auvergne-Rhône-Alpes", "01 03 07 15 26 38 42 43 63 69 73 74")
_region("Bourgogne-Franche-Comté", "21 25 39 58 70 71 89 90")
_region("Bretagne", "22 29 35 56")
_region("Centre-Val de Loire", "18 28 36 37 41 45")
_region("Corse", "2A 2B 20")
_region("Grand Est", "08 10 51 52 54 55 57 67 68 88")
_region("Hauts-de-France", "02 59 60 62 80")
_region("Île-de-France", "75 77 78 91 92 93 94 95")
_region("Normandie", "14 27 50 61 76")
_region("Nouvelle-Aquitaine", "16 17 19 23 24 33 40 47 64 79 86 87")
_region("Occitanie", "09 11 12 30 31 32 34 46 48 65 66 81 82")
_region("Pays de la Loire", "44 49 53 72 85")
_region("Provence-Alpes-Côte d'Azur", "04 05 06 13 83 84")

session = requests.Session()
session.headers.update({"User-Agent": f"alertes-meteo-records-temperature/{VERSION}"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None: return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fnum(v: Any) -> Optional[float]:
    if v in (None, "", "mq", "nan", "NaN"): return None
    try: x = float(str(v).replace(",", "."))
    except (ValueError, TypeError): return None
    return x if math.isfinite(x) else None


def celsius(v: Optional[float]) -> Optional[float]:
    """DPPaquetObs V2 renvoie des kelvins ; les historiques de records sont en °C."""
    if v is None: return None
    return round(v - 273.15, 1) if v > 150 else v


def fint(v: Any) -> Optional[int]:
    x = fnum(v)
    return int(x) if x is not None else None


def first(row: dict, names: Iterable[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        v = low.get(str(name).strip().lower())
        if v not in (None, ""): return v
    return None


def station_id(row: dict) -> Optional[str]:
    v = first(row, ("NUM_POSTE", "num_poste", "geo_id_insee", "id_station", "Id_station"))
    if v is None: return None
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit(): s = s[:-2]
    digits = re.sub(r"\D", "", s)
    return digits.zfill(8) if digits else None


def quality_ok(v: Any) -> bool:
    if v in (None, ""): return True
    try: return int(float(str(v).replace(",", "."))) != 2
    except Exception: return True


def parse_date_value(v: Any) -> Optional[date]:
    digits = re.sub(r"\D", "", str(v or ""))
    if len(digits) < 8: return None
    try: return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError: return None


def parse_iso(v: Any) -> Optional[datetime]:
    if not v: return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_delimited(raw: bytes) -> List[dict]:
    if raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError: pass
    if text is None: text = raw.decode("utf-8", errors="replace")
    sample = text[:10000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    out = []
    for row in reader:
        out.append({str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k is not None})
    return out


def get_bytes(url: str, *, headers: Optional[dict] = None, retries: int = 3) -> Optional[bytes]:
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
            if r.status_code == 404: return None
            if r.status_code == 429:
                time.sleep(2 + attempt * 3); continue
            r.raise_for_status()
            return r.content
        except Exception as exc:
            last = exc
            time.sleep(1 + attempt * 2)
    print(f"[WARN] téléchargement impossible {url}: {last}")
    return None


def get_json(url: str) -> Any:
    raw = get_bytes(url)
    if raw is None: return None
    try: return json.loads(raw.decode("utf-8-sig"))
    except Exception:
        try: return json.loads(raw.decode("latin-1"))
        except Exception as exc:
            print("[WARN] JSON invalide", url, exc); return None


def department_of_station(sid: str) -> str:
    if sid.startswith("20"):
        # La métadonnée / ligne QUOT permettra souvent de distinguer 2A/2B via le fichier source.
        return "20"
    return sid[:2]


def _fiche_day(v: Any) -> Optional[str]:
    """« 1924-11-01 00:00:00 » -> « 1924-11-01 » (None si absent ou invalide)."""
    s = str(v or "").strip()[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def _fiche_current(entries: Any) -> Optional[dict]:
    """Entrée en vigueur d'une liste datée (positions, types de poste) : celle sans date de fin, sinon la plus récente."""
    items = [e for e in (entries or []) if isinstance(e, dict)]
    if not items: return None
    open_items = [e for e in items if not str(e.get("dateFin") or "").strip()]
    pool = open_items or items
    return max(pool, key=lambda e: (str(e.get("dateFin") or "9999"), str(e.get("dateDebut") or "")))


def parse_station_fiches(rows: Any) -> Dict[str, dict]:
    """Fiches des stations de Météo-France (METADONNEES_STATION/fiches.json) -> métadonnées compactes par numéro de poste.

    Année d'ouverture = début de la fiche ; type de poste = type en vigueur ; réseau principal = types 0, 1 et 2.
    Le nom n'est pas repris : celui des données quotidiennes (NOM_USUEL) reste la référence."""
    out: Dict[str, dict] = {}
    for fiche in rows if isinstance(rows, list) else []:
        if not isinstance(fiche, dict): continue
        digits = re.sub(r"\D", "", str(fiche.get("id") or ""))
        if not digits: continue
        opened = _fiche_day(fiche.get("dateDebut"))
        closed = _fiche_day(fiche.get("dateFin"))
        typ_entry = _fiche_current(fiche.get("typesPoste"))
        typ = fint(typ_entry.get("type")) if typ_entry else None
        pos = _fiche_current(fiche.get("positions")) or {}
        out[digits.zfill(8)] = {
            "lat": fnum(pos.get("latitude")), "lon": fnum(pos.get("longitude")), "altitude": fnum(pos.get("altitude")),
            "opened": opened, "closed": closed,
            "opening_year": int(opened[:4]) if opened else None,
            "type_station": typ,
            "principal": typ in (0, 1, 2),
        }
    return out


def download_station_metadata(refresh: bool = True) -> Dict[str, dict]:
    """Métadonnées compactes des stations (cache/stations-meta.json).

    Le fichier source fait environ 200 Mo : il n'est relu qu'une fois par semaine, et seulement si `refresh` est vrai
    (le LIVE horaire se contente de la copie publiée)."""
    stored = load_json_file(META_FILE)
    stations = stored.get("stations") if isinstance(stored, dict) else None
    at = parse_iso(stored.get("generated_at")) if isinstance(stored, dict) else None
    if stations and (not refresh or (at and utcnow() - at < timedelta(days=META_MAX_AGE_DAYS))):
        return stations
    if not refresh:
        return {}
    meta = parse_station_fiches(get_json(STATIONS_META_URL))
    if not meta:
        print("[WARN] métadonnées stations indisponibles" + (" : copie précédente conservée" if stations else ""))
        return stations or {}
    save_json(META_FILE, {"schema_version": 1, "generated_at": iso(utcnow()), "source": "Météo-France, fiches des stations", "stations": meta})
    actives = sum(1 for m in meta.values() if not m["closed"])
    print("Métadonnées stations :", len(meta), "fiches,", actives, "stations ouvertes, dont", sum(1 for m in meta.values() if not m["closed"] and m["principal"]), "du réseau principal")
    return meta

def discover_daily_resources() -> Dict[str, List[dict]]:
    payload = get_json(DATASET_API)
    if not isinstance(payload, dict):
        raise RuntimeError("Impossible de lire le catalogue data.gouv des données quotidiennes.")
    by_dep: Dict[str, List[dict]] = defaultdict(list)
    for res in payload.get("resources") or []:
        title = str(res.get("title") or "")
        url = str(res.get("url") or "")
        text = title + " " + url
        if "RR-T-Vent" not in text: continue
        m = re.search(r"Q_([0-9]{2,3}|2A|2B)_([^/]+?)_RR-T-Vent\.csv\.gz", url, re.I)
        if not m:
            m2 = re.search(r"departement[_ -]?(2A|2B|\d{2,3}).*?(\d{4}[-–]\d{4}|previous|latest)", title, re.I)
            if not m2: continue
            dep = m2.group(1).upper()
            period = m2.group(2)
        else:
            dep = m.group(1).upper(); period = m.group(2)
        by_dep[dep].append({"url": url, "title": title, "period": period})
    for dep in by_dep:
        by_dep[dep].sort(key=lambda r: ("latest" in r["period"].lower(), r["period"]))
    print("Départements avec ressources QUOT :", len(by_dep))
    return dict(by_dep)


def seq_update(seq: List[dict], d: date, value: float, mode: str) -> None:
    if not seq:
        seq.append({"date": d.isoformat(), "value": round(value, 1)}); return
    prev = float(seq[-1]["value"])
    better = value > prev + EQUAL_EPS if mode == "max" else value < prev - EQUAL_EPS
    equal = abs(value - prev) <= EQUAL_EPS
    if better:
        seq.append({"date": d.isoformat(), "value": round(value, 1)})
    elif equal:
        # L'égalité ne change pas la référence historique : on conserve
        # la date du premier record pour l'ancienneté.
        return


def _ensure_station(cache: dict, sid: str, meta: dict, row: dict, dep: str) -> dict:
    stations = cache.setdefault("stations", {})
    st = stations.setdefault(sid, {
        "id": sid,
        "name": meta.get("name") or str(first(row, ("NOM_USUEL", "nom_usuel")) or sid),
        "lat": meta.get("lat") if meta.get("lat") is not None else fnum(first(row, ("LAT", "lat"))),
        "lon": meta.get("lon") if meta.get("lon") is not None else fnum(first(row, ("LON", "lon"))),
        "altitude": meta.get("altitude") if meta.get("altitude") is not None else fnum(first(row, ("ALTI", "alti"))),
        "opened": meta.get("opened"), "opening_year": meta.get("opening_year"),
        "type_station": meta.get("type_station"), "principal": bool(meta.get("principal")),
        "department_code": dep,
        "region": REGION_BY_DEP.get(dep, "France"),
        "heat": {"absolute": [], "monthly": {}, "fortnight": {}, "daily": {}},
        "cold": {"absolute": [], "monthly": {}, "fortnight": {}, "daily": {}},
        "tropical": {"absolute": [], "monthly": {}, "fortnight": {}, "daily": {}},
    })
    # Actualisation métadonnées quand disponibles.
    for k in ("name", "lat", "lon", "altitude", "opened", "opening_year", "type_station", "principal"):
        if meta.get(k) not in (None, ""): st[k] = meta[k]
    st["department_code"] = dep
    st["region"] = REGION_BY_DEP.get(dep, "France")
    return st


def _update_histories(st: dict, d: date, tx: Optional[float], tn: Optional[float]) -> None:
    mm = f"{d.month:02d}"
    md = f"{d.month:02d}-{d.day:02d}"
    fortnight = f"{d.month:02d}-{1 if d.day <= 14 else 2}"
    if tx is not None and -80 < tx < 65:
        for key in ("absolute",): seq_update(st["heat"][key], d, tx, "max")
        for group, sub in (("monthly", mm), ("fortnight", fortnight), ("daily", md)):
            seq = st["heat"][group].setdefault(sub, []); seq_update(seq, d, tx, "max")
    if tn is not None and -90 < tn < 55:
        seq_update(st["cold"]["absolute"], d, tn, "min")
        for group, sub in (("monthly", mm), ("fortnight", fortnight), ("daily", md)):
            seq = st["cold"][group].setdefault(sub, []); seq_update(seq, d, tn, "min")
        # Nuit tropicale : on suit le record de TN élevée, mais on n'affichera que TN >= 20 °C.
        seq_update(st["tropical"]["absolute"], d, tn, "max")
        for group, sub in (("monthly", mm), ("fortnight", fortnight), ("daily", md)):
            seq = st["tropical"][group].setdefault(sub, []); seq_update(seq, d, tn, "max")


def build_department_cache(dep: str, resources: List[dict], metadata: Dict[str, dict]) -> dict:
    cache = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID,
        "department": dep, "generated_at": iso(utcnow()), "stations": {}, "rows_read": 0,
    }
    seen = set()
    for res in resources:
        print(f"[{dep}] {res['period']} — téléchargement")
        raw = get_bytes(res["url"])
        if raw is None: continue
        rows = parse_delimited(raw)
        print(f"[{dep}] {res['period']} — {len(rows)} lignes")
        for row in rows:
            sid = station_id(row)
            d = parse_date_value(first(row, ("AAAAMMJJ", "DATE", "date")))
            if not sid or not d: continue
            key = (sid, d.isoformat())
            if key in seen: continue
            seen.add(key)
            tx = fnum(first(row, ("TX", "tx"))) if quality_ok(first(row, ("QTX", "qtx"))) else None
            tn = fnum(first(row, ("TN", "tn"))) if quality_ok(first(row, ("QTN", "qtn"))) else None
            if tx is None and tn is None: continue
            st = _ensure_station(cache, sid, metadata.get(sid, {}), row, dep)
            _update_histories(st, d, tx, tn)
            cache["rows_read"] += 1
    cache["stations_total"] = len(cache["stations"])
    cache["last_date"] = max((e[-1]["date"] for st in cache["stations"].values() for typ in ("heat","cold") for e in [st[typ]["absolute"]] if e), default=None)
    return cache


def cache_path(dep: str) -> Path:
    return CACHE_DIR / f"dep-{dep}.json"


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def load_json_file(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return None


def bootstrap_next(n: int) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    resources = discover_daily_resources()
    missing = [d for d in DEPARTMENTS if not cache_path(d).exists() and d in resources]
    targets = missing[:max(0, n)]
    print("Bootstrap départements :", targets or "aucun")
    if targets:
        metadata = download_station_metadata()
        for dep in targets:
            cache = build_department_cache(dep, resources[dep], metadata)
            save_json(cache_path(dep), cache)
            print(f"[{dep}] cache écrit : {cache['stations_total']} stations")
    write_index()


def all_station_caches() -> Dict[str, dict]:
    out = {}
    for path in sorted(CACHE_DIR.glob("dep-*.json")):
        data = load_json_file(path)
        if not isinstance(data, dict): continue
        for sid, st in (data.get("stations") or {}).items(): out[sid] = st
    return out


def seq_before(seq: List[dict], d: date) -> Optional[dict]:
    # Les séquences sont chronologiques. On cherche le dernier état strictement antérieur.
    target = d.isoformat(); found = None
    for item in seq or []:
        if str(item.get("date")) >= target: break
        found = item
    return found


class RecentLayer:
    """Valeurs quotidiennes (TX, TN) postérieures à la construction des caches historiques.

    Les caches sont figés à leur date de construction : sans cette couche, un record du mois battu le 3 serait comparé,
    le 5, à l'ancien record d'avant le 3 (et l'on annoncerait un « record » à 34 °C alors que 35 °C ont été relevés le 3).
    Les valeurs viennent des données quotidiennes contrôlées (cache/recent.json) puis, pour les derniers jours pas encore
    publiés, des paquets horaires (cache/provisional.json)."""

    def __init__(self, days: Optional[Dict[str, Dict[str, list]]] = None):
        self.by_station: Dict[str, List[Tuple[str, Optional[float], Optional[float]]]] = defaultdict(list)
        for day in sorted(days or {}):
            for sid, v in ((days or {})[day] or {}).items():
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    self.by_station[sid].append((day, fnum(v[0]), fnum(v[1])))

    def best_before(self, sid: str, kind: str, scope: str, d: date) -> Optional[dict]:
        """Meilleure valeur (record) de la période, parmi les jours strictement antérieurs à `d` ; à égalité, la plus ancienne."""
        target = d.isoformat()
        best: Optional[Tuple[float, str]] = None
        for day, tx, tn in self.by_station.get(sid, ()):
            if day >= target: break
            if scope == "monthly" and day[5:7] != target[5:7]: continue
            if scope == "fortnight" and (day[5:7] != target[5:7] or (int(day[8:10]) <= 14) != (d.day <= 14)): continue
            if scope == "daily" and day[5:10] != target[5:10]: continue
            v = tx if kind == "heat" else tn
            if v is None: continue
            if kind == "cold":
                if best is None or v < best[0] - EQUAL_EPS: best = (v, day)
            elif best is None or v > best[0] + EQUAL_EPS:
                best = (v, day)
        return {"value": round(best[0], 1), "date": best[1]} if best else None


def better_ref(base: Optional[dict], extra: Optional[dict], mode: str) -> Optional[dict]:
    """Garde le record le plus fort ; à égalité, le plus ancien (celui de `base`)."""
    if not extra: return base
    if not base: return extra
    bv, ev = float(base["value"]), float(extra["value"])
    if mode == "min": return extra if ev < bv - EQUAL_EPS else base
    return extra if ev > bv + EQUAL_EPS else base


def merge_days(base: Optional[Dict[str, Dict[str, list]]], extra: Optional[Dict[str, Dict[str, list]]]) -> Dict[str, Dict[str, list]]:
    """Réunit deux jeux {date: {station: [TX, TN]}} ; une valeur contrôlée (`base`) l'emporte sur une valeur provisoire."""
    out: Dict[str, Dict[str, list]] = {day: dict(vals) for day, vals in (base or {}).items()}
    for day, vals in (extra or {}).items():
        cell = out.setdefault(day, {})
        for sid, v in vals.items():
            cell.setdefault(sid, v)
    return out


def record_ref(st: dict, kind: str, scope: str, d: date, recent: Optional[RecentLayer] = None) -> Optional[dict]:
    node = st.get(kind) or {}
    if scope == "absolute": seq = node.get("absolute") or []
    elif scope == "monthly": seq = (node.get("monthly") or {}).get(f"{d.month:02d}", [])
    elif scope == "fortnight": seq = (node.get("fortnight") or {}).get(f"{d.month:02d}-{1 if d.day <= 14 else 2}", [])
    elif scope == "daily": seq = (node.get("daily") or {}).get(f"{d.month:02d}-{d.day:02d}", [])
    else: return None
    base = seq_before(seq, d)
    if recent is None: return base
    return better_ref(base, recent.best_before(str(st.get("id")), kind, scope, d), "min" if kind == "cold" else "max")


def classify(st: dict, d: date, value: float, kind: str, recent: Optional[RecentLayer] = None) -> dict:
    mode = "min" if kind == "cold" else "max"
    refs = {scope: record_ref(st, kind, scope, d, recent) for scope in ("absolute", "monthly", "fortnight", "daily")}
    flags = []
    near = []
    deltas = {}
    for scope, ref in refs.items():
        if not ref: continue
        rv = float(ref["value"])
        delta = (rv - value) if mode == "max" else (value - rv)
        deltas[scope] = round(delta, 2)
        is_record = value >= rv - EQUAL_EPS if mode == "max" else value <= rv + EQUAL_EPS
        if is_record: flags.append(scope)
        elif scope in ("absolute", "monthly", "daily") and 0 < delta <= NEAR_DELTA_C + EQUAL_EPS:
            near.append(scope)
    return {
        "flags": flags, "near": near,
        "records": {k: ({"value": v.get("value"), "date": v.get("date")} if v else None) for k, v in refs.items()},
        "deltas": deltas,
    }


def station_view(st: dict, meta: Optional[Dict[str, dict]]) -> dict:
    """Station du cache complétée par les métadonnées à jour (réseau principal, année d'ouverture) : les caches sont figés."""
    m = (meta or {}).get(str(st.get("id"))) or {}
    typ = m.get("type_station")
    return {
        "opened": m.get("opened") or st.get("opened"),
        "opening_year": m.get("opening_year") or st.get("opening_year"),
        "type_station": typ if typ is not None else st.get("type_station"),
        "principal": bool(m.get("principal")) if typ is not None else bool(st.get("principal")),
        "altitude": st.get("altitude") if st.get("altitude") is not None else m.get("altitude"),
        "lat": st.get("lat") if st.get("lat") is not None else m.get("lat"),
        "lon": st.get("lon") if st.get("lon") is not None else m.get("lon"),
    }


def event_from_value(st: dict, d: date, value: Optional[float], kind: str, provisional: bool = False,
                     meta: Optional[Dict[str, dict]] = None, recent: Optional[RecentLayer] = None) -> Optional[dict]:
    if value is None: return None
    if kind == "tropical" and value < 20.0: return None
    cls = classify(st, d, value, kind, recent)
    if not cls["flags"] and not cls["near"]:
        return None
    v = station_view(st, meta)
    return {
        "id": st.get("id"), "name": st.get("name"), "lat": v["lat"], "lon": v["lon"],
        "altitude": v["altitude"], "opening_year": v["opening_year"], "opened": v["opened"],
        "type_station": v["type_station"], "principal": v["principal"],
        "department_code": st.get("department_code"), "region": st.get("region") or "France",
        "value": round(float(value), 1), "kind": kind, "flags": cls["flags"], "near": cls["near"],
        "records": cls["records"], "deltas": cls["deltas"], "provisional": provisional,
    }


def make_day_payload(d: date, station_values: Dict[str, dict], caches: Dict[str, dict], *, provisional: bool, source: str,
                     meta: Optional[Dict[str, dict]] = None, recent: Optional[RecentLayer] = None) -> dict:
    heat, cold, tropical = [], [], []
    all_values = []
    for sid, vals in station_values.items():
        st = caches.get(sid)
        if not st: continue
        tx = fnum(vals.get("tx")); tn = fnum(vals.get("tn"))
        e = event_from_value(st, d, tx, "heat", provisional, meta, recent)
        if e: heat.append(e)
        e = event_from_value(st, d, tn, "cold", provisional, meta, recent)
        if e: cold.append(e)
        e = event_from_value(st, d, tn, "tropical", provisional, meta, recent)
        if e: tropical.append(e)
        all_values.append({"id": sid, "tx": tx, "tn": tn})
    heat.sort(key=lambda x: (-x["value"], x["name"] or ""))
    cold.sort(key=lambda x: (x["value"], x["name"] or ""))
    tropical.sort(key=lambda x: (-x["value"], x["name"] or ""))
    return {
        "date": d.isoformat(), "provisional": provisional, "source": source,
        "fortnight_label": "1re quinzaine" if d.day <= 14 else "2e quinzaine",
        "events": {"heat": heat, "cold": cold, "tropical": tropical},
        "counts": {
            "heat": len(heat), "cold": len(cold), "tropical": len(tropical),
            "heat_records": sum(bool(e["flags"]) for e in heat),
            "cold_records": sum(bool(e["flags"]) for e in cold),
            "tropical_records": sum(bool(e["flags"]) for e in tropical),
        },
        "stations_with_values": len(all_values),
    }


def api_key() -> str:
    for name in ("METEOFRANCE_PACKAGE_OBS_KEY", "METEOFRANCE_OBS_TOKEN"):
        v = os.getenv(name, "").strip()
        if v:
            for p in ("apikey:", "Bearer ", "bearer "):
                if v.startswith(p): v = v[len(p):].strip()
            return v
    raise RuntimeError("Secret METEOFRANCE_PACKAGE_OBS_KEY absent")


def package_request(key: str, h: datetime) -> Optional[bytes]:
    params = {"date": iso(h.replace(minute=0, second=0, microsecond=0)), "format": "csv"}
    headers = {"apikey": key, "accept": "*/*"}
    try:
        r = session.get(PACKAGE_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        print("[WARN] paquet", iso(h), exc); return None
    if r.status_code == 200: return r.content
    if r.status_code in (400, 404): return None
    if r.status_code == 429: raise RuntimeError("Météo-France HTTP 429 — quota dépassé")
    r.raise_for_status(); return None


def find_latest_hour(key: str) -> Tuple[datetime, bytes]:
    base = utcnow().replace(minute=0, second=0, microsecond=0)
    for back in range(PACKAGE_LOOKBACK_HOURS):
        h = base - timedelta(hours=back)
        raw = package_request(key, h)
        if raw is not None: return h, raw
    raise RuntimeError("Aucun paquet horaire Météo-France disponible H à H-3")


def window_dates(h: datetime) -> Tuple[str, str]:
    """Dates (ISO) des fenêtres climatologiques de l'heure `h` (UTC) : TX de 6 h à 6 h UTC, TN de 18 h la veille à 18 h UTC."""
    return (h - timedelta(hours=6)).date().isoformat(), (h + timedelta(hours=6)).date().isoformat()


def accumulate_hour(days: Dict[str, Dict[str, list]], h: datetime, rows: List[dict], known: Any) -> None:
    """Fusionne les extrêmes d'un paquet horaire dans `days` ({date: {station: [TX, TN]}} ; TX = maximum, TN = minimum)."""
    tx_day, tn_day = window_dates(h)
    for row in rows:
        sid = station_id(row)
        if not sid or sid not in known: continue
        t = celsius(fnum(first(row, ("t", "T"))))
        tx = celsius(fnum(first(row, ("tx", "TX"))))
        tn = celsius(fnum(first(row, ("tn", "TN"))))
        # TX/TN horaires, T en secours.
        if tx is None: tx = t
        if tn is None: tn = t
        if tx is not None:
            cell = days.setdefault(tx_day, {}).setdefault(sid, [None, None])
            cell[0] = tx if cell[0] is None else max(cell[0], tx)
        if tn is not None:
            cell = days.setdefault(tn_day, {}).setdefault(sid, [None, None])
            cell[1] = tn if cell[1] is None else min(cell[1], tn)


def uncovered_days(prov_days: Dict[str, Dict[str, list]], through: Optional[str], today: date) -> List[str]:
    """Jours entre les données contrôlées (exclues) et aujourd'hui (exclu) dont les relevés horaires n'ont pas été conservés."""
    day = date.fromisoformat(through) + timedelta(days=1) if through else today - timedelta(days=2)
    out: List[str] = []
    while day < today:
        if len(prov_days.get(day.isoformat(), {})) < MIN_STATIONS_JOUR: out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def load_recent() -> dict:
    data = load_json_file(RECENT_FILE)
    return data if isinstance(data, dict) else {}


def write_provisional_days(prov_days: Dict[str, Dict[str, list]], through: Optional[str], today: date, caches: Dict[str, dict],
                           meta: Dict[str, dict], layer: RecentLayer) -> List[str]:
    """Écrit provisional/AAAA-MM-JJ.json pour les jours récents que les données quotidiennes contrôlées ne couvrent pas encore.

    Un fichier n'est réécrit que si son contenu change ; ceux dont le jour est désormais contrôlé sont supprimés."""
    PROVISIONAL_DIR.mkdir(parents=True, exist_ok=True)
    wanted = [day for day in sorted(prov_days) if day < today.isoformat() and not (through and day <= through)]
    for day in wanted:
        values = {sid: {"tx": cell[0], "tn": cell[1]} for sid, cell in prov_days[day].items()}
        payload_day = make_day_payload(date.fromisoformat(day), values, caches, provisional=True,
                                       source="Météo-France DPPaquetObs V2", meta=meta, recent=layer)
        path = PROVISIONAL_DIR / f"{day}.json"
        old = load_json_file(path)
        if isinstance(old, dict) and old.get("day") == payload_day: continue
        save_json(path, {"schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID, "status": "ok",
                         "generated_at": iso(utcnow()), "day": payload_day})
    for path in PROVISIONAL_DIR.glob("????-??-??.json"):
        if path.stem not in wanted: path.unlink()
    return wanted


def generate_live() -> None:
    caches = all_station_caches()
    if not caches:
        print("[WARN] aucun cache historique : live vide")
    meta = download_station_metadata(refresh=False)
    key = api_key()
    latest, raw0 = find_latest_hour(key)
    # Date climatologique d'affichage : date UTC du dernier paquet.
    d = latest.date()
    controlled = load_recent()
    through = controlled.get("through")
    state = load_json_file(PROVISIONAL_FILE)
    prov_days: Dict[str, Dict[str, list]] = state["days"] if isinstance(state, dict) and isinstance(state.get("days"), dict) else {}
    # TN du jour : depuis 18 h UTC la veille ; TX du jour : depuis 06 h UTC. Jusqu'à 6 h UTC, la fenêtre TX de la veille
    # (6 h → 6 h UTC) est encore ouverte : on remonte jusqu'à son début pour la compléter.
    earliest = datetime(d.year, d.month, d.day, 18, tzinfo=timezone.utc) - timedelta(days=1)
    if latest.hour < 6:
        earliest = min(earliest, datetime(d.year, d.month, d.day, 6, tzinfo=timezone.utc) - timedelta(days=1))
    # Jours récents sans relevés horaires conservés (premier lancement, état perdu) : reprise des paquets manquants,
    # depuis 18 h UTC la veille du premier jour manquant (début de sa TN), au plus BACKFILL_MAX_HOURS heures en arrière.
    missing = uncovered_days(prov_days, through, d)
    if missing:
        first = date.fromisoformat(missing[0])
        earliest = min(earliest, datetime(first.year, first.month, first.day, 18, tzinfo=timezone.utc) - timedelta(days=1))
        earliest = max(earliest, latest - timedelta(hours=BACKFILL_MAX_HOURS))
        print("Jours sans relevés horaires conservés :", missing, "-> reprise depuis", iso(earliest))
    hours = int((latest - earliest).total_seconds() // 3600) + 1
    read = 0
    for offset in range(min(BACKFILL_MAX_HOURS + 1, max(LIVE_HISTORY_HOURS, hours))):
        h = latest - timedelta(hours=offset)
        if h < earliest: break
        try:
            raw = raw0 if offset == 0 else package_request(key, h)
        except RuntimeError as exc:  # quota dépassé : on garde ce qui a été lu, les heures manquantes seront reprises au passage suivant
            print("[WARN]", exc, "- lecture interrompue à", iso(h))
            break
        if raw is None: continue
        accumulate_hour(prov_days, h, parse_delimited(raw), caches)
        read += 1
        time.sleep(PACKAGE_DELAY)
    print("Paquets horaires lus :", read, "sur", hours, "heures demandées")
    cutoff = (d - timedelta(days=PROVISIONAL_KEEP_DAYS)).isoformat()
    prov_days = {day: vals for day, vals in prov_days.items() if day >= cutoff and not (through and day <= through)}
    save_json(PROVISIONAL_FILE, {"schema_version": 1, "generated_at": iso(utcnow()), "days": prov_days})
    layer = RecentLayer(merge_days(controlled.get("days"), prov_days))
    values = {sid: {"tx": cell[0], "tn": cell[1]} for sid, cell in prov_days.get(d.isoformat(), {}).items()}
    day = make_day_payload(d, values, caches, provisional=True, source="Météo-France DPPaquetObs V2", meta=meta, recent=layer)
    previous = write_provisional_days(prov_days, through, d, caches, meta, layer)
    payload = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID,
        "status": "ok", "generated_at": iso(utcnow()), "latest_observation_at": iso(latest),
        "coverage_departments": len(list(CACHE_DIR.glob("dep-*.json"))), "coverage_target": len(DEPARTMENTS),
        "recent_through": through, "provisional_days": previous,
        "day": day,
    }
    save_json(LIVE_FILE, payload)
    print("Live écrit :", d, "événements", sum(day["counts"].get(k, 0) for k in ("heat", "cold", "tropical")),
          "| jours provisoires :", previous or "aucun", "| données contrôlées jusqu'au", through)
    write_index()


def resource_for_year(resources: List[dict], year: int) -> Optional[dict]:
    # On déduit la période du nom de fichier / titre.
    candidates = []
    for r in resources:
        text = r.get("period", "") + " " + r.get("title", "") + " " + r.get("url", "")
        nums = [int(x) for x in re.findall(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2})(?!\d)", text)]
        if len(nums) >= 2 and min(nums) <= year <= max(nums): candidates.append(r)
        elif "latest" in text.lower() and year >= utcnow().year - 1: candidates.append(r)
        elif "previous" in text.lower() and 1950 <= year <= utcnow().year - 2: candidates.append(r)
    if not candidates: return None
    # latest prioritaire pour années récentes.
    candidates.sort(key=lambda r: ("latest" not in (r.get("period", "") + r.get("title", "")).lower(), len(r.get("url", ""))))
    return candidates[0]


def recent_since_date() -> date:
    """Premier jour à relire : la plus ancienne construction de cache, moins une marge (les caches sont figés à leur date de construction)."""
    built = []
    for path in CACHE_DIR.glob("dep-*.json"):
        try:
            with path.open("r", encoding="utf-8") as fh: head = fh.read(800)
        except OSError:
            continue
        m = re.search(r'"generated_at":\s*"([^"]+)"', head)
        dt = parse_iso(m.group(1)) if m else None
        if dt: built.append(dt)
    base = min(built) if built else utcnow()
    since = (base - timedelta(days=RECENT_MARGIN_DAYS)).date()
    limit = (utcnow() - timedelta(days=RECENT_MAX_DAYS)).date()
    if since < limit:
        print(f"[WARN] caches construits il y a plus de {RECENT_MAX_DAYS} jours : couche récente limitée au {limit} ; reconstruire les caches (une fois par an)")
        since = limit
    return since


def last_complete_day(days: Dict[str, Dict[str, list]]) -> Optional[str]:
    """Dernier jour couvert par au moins la moitié des stations du jour le mieux fourni : le suivant est encore incomplet."""
    if not days: return None
    biggest = max(len(v) for v in days.values())
    ok = [day for day, v in days.items() if len(v) >= 0.5 * biggest]
    return max(ok) if ok else None


def generate_archive(year: int, month: int) -> None:
    if not (1800 <= year <= utcnow().year and 1 <= month <= 12):
        raise ValueError("année/mois invalide")
    caches = all_station_caches()
    meta = download_station_metadata()
    resources = discover_daily_resources()
    since = recent_since_date()
    values_by_day: Dict[date, Dict[str, dict]] = defaultdict(dict)
    # Valeurs quotidiennes contrôlées postérieures aux caches : elles complètent les records d'un mois à l'autre.
    recent_days: Dict[str, Dict[str, list]] = defaultdict(dict)
    for dep in DEPARTMENTS:
        if not cache_path(dep).exists(): continue
        res = resource_for_year(resources.get(dep, []), year)
        if not res: continue
        raw = get_bytes(res["url"])
        if raw is None: continue
        for row in parse_delimited(raw):
            d = parse_date_value(first(row, ("AAAAMMJJ", "DATE", "date")))
            if not d: continue
            in_month = d.year == year and d.month == month
            if not in_month and d < since: continue
            sid = station_id(row)
            if not sid or sid not in caches: continue
            tx = fnum(first(row, ("TX", "tx"))) if quality_ok(first(row, ("QTX", "qtx"))) else None
            tn = fnum(first(row, ("TN", "tn"))) if quality_ok(first(row, ("QTN", "qtn"))) else None
            if in_month: values_by_day[d][sid] = {"tx": tx, "tn": tn}
            if d >= since and (tx is not None or tn is not None): recent_days[d.isoformat()][sid] = [tx, tn]
    layer = RecentLayer(recent_days)
    days = {}
    for d in sorted(values_by_day):
        days[str(d.day)] = make_day_payload(d, values_by_day[d], caches, provisional=False, source="Météo-France climatologie quotidienne",
                                            meta=meta, recent=layer)
    payload = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID,
        "status": "ok", "generated_at": iso(utcnow()), "year": year, "month": month,
        "month_label": f"{MONTHS_FR[month-1]} {year}", "days": days,
        "coverage_departments": len(list(CACHE_DIR.glob("dep-*.json"))), "coverage_target": len(DEPARTMENTS),
    }
    out = ARCHIVE_DIR / f"{year:04d}-{month:02d}.json"
    save_json(out, payload)
    print("Archive écrite :", out, "jours", len(days))
    if recent_days and year >= since.year:
        through = last_complete_day(recent_days)
        save_json(RECENT_FILE, {"schema_version": 1, "generated_at": iso(utcnow()), "since": since.isoformat(), "through": through,
                                "days": recent_days})
        print("Couche récente écrite :", len(recent_days), "jours depuis", since, "| contrôlés jusqu'au", through)
    write_index()


def write_index() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    months = []
    if ARCHIVE_DIR.exists():
        for p in sorted(ARCHIVE_DIR.glob("????-??.json")):
            m = re.match(r"(\d{4})-(\d{2})", p.stem)
            if m: months.append({"year": int(m.group(1)), "month": int(m.group(2)), "file": f"archive/{p.name}"})
    live = load_json_file(LIVE_FILE)
    recent = load_json_file(RECENT_FILE)
    meta = load_json_file(META_FILE)
    provisional = sorted(p.stem for p in PROVISIONAL_DIR.glob("????-??-??.json")) if PROVISIONAL_DIR.exists() else []
    payload = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID,
        "status": "ok", "generated_at": iso(utcnow()),
        "available_months": months,
        "live_file": "live.json" if LIVE_FILE.exists() else None,
        "latest_date": (((live or {}).get("day") or {}).get("date")),
        "recent_through": (recent or {}).get("through") if isinstance(recent, dict) else None,
        "provisional_days": provisional,
        "stations_meta_at": (meta or {}).get("generated_at") if isinstance(meta, dict) else None,
        "coverage_departments": len(list(CACHE_DIR.glob("dep-*.json"))), "coverage_target": len(DEPARTMENTS),
        "near_delta_c": NEAR_DELTA_C,
        "principal_types": [0, 1, 2],
    }
    save_json(INDEX_FILE, payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap-next", type=int, default=0, help="Construit N caches départementaux manquants")
    ap.add_argument("--live", action="store_true", help="Génère le JSON live du jour")
    ap.add_argument("--archive", help="Génère une archive mensuelle YYYY-MM ou 'current'")
    ap.add_argument("--index", action="store_true")
    args = ap.parse_args()

    OUT_ROOT.mkdir(parents=True, exist_ok=True); CACHE_DIR.mkdir(parents=True, exist_ok=True); ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    PROVISIONAL_DIR.mkdir(parents=True, exist_ok=True)
    if args.bootstrap_next > 0: bootstrap_next(args.bootstrap_next)
    if args.live: generate_live()
    if args.archive:
        if args.archive == "current": y, m = utcnow().year, utcnow().month
        else:
            mm = re.fullmatch(r"(\d{4})-(\d{2})", args.archive)
            if not mm: raise ValueError("--archive doit être YYYY-MM ou current")
            y, m = int(mm.group(1)), int(mm.group(2))
        generate_archive(y, m)
    if args.index or not (args.bootstrap_next or args.live or args.archive): write_index()
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

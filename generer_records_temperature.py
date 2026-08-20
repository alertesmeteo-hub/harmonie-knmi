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

VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "records-temperature-map-20260820"

OUT_ROOT = Path("records_temperature")
CACHE_DIR = OUT_ROOT / "cache"
ARCHIVE_DIR = OUT_ROOT / "archive"
LIVE_FILE = OUT_ROOT / "live.json"
INDEX_FILE = OUT_ROOT / "index.json"

DATASET_ID = "6569b51ae64326786e4e8e1a"
DATASET_API = f"https://www.data.gouv.fr/api/1/datasets/{DATASET_ID}/"
STATIONS_META_URL = "https://www.data.gouv.fr/fr/datasets/r/1fe544d8-4615-4642-a307-5956a7d90922"

PACKAGE_URL = (
    "https://public-api.meteofrance.fr/public/"
    "DPPaquetObs/v2/paquet/stations/horaire"
)

HTTP_TIMEOUT = 120
NEAR_DELTA_C = 1.0
EQUAL_EPS = 0.05
PACKAGE_LOOKBACK_HOURS = 4
LIVE_HISTORY_HOURS = 30
PACKAGE_DELAY = float(os.getenv("MF_PACKAGE_DELAY", "0.20"))

# Métropole : 01-95 hors 20 + Corse 2A/2B.
DEPARTMENTS = [f"{x:02d}" for x in range(1, 96) if x != 20] + ["2A", "2B"]

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
_region("Corse", "2A 2B")
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


def download_station_metadata() -> Dict[str, dict]:
    payload = get_json(STATIONS_META_URL)
    if payload is None:
        print("[WARN] métadonnées stations indisponibles")
        return {}
    rows = []
    if isinstance(payload, list): rows = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("features"), list):
            for f in payload["features"]:
                if isinstance(f, dict):
                    p = dict(f.get("properties") or {})
                    geom = f.get("geometry") or {}
                    coords = geom.get("coordinates") if isinstance(geom, dict) else None
                    if isinstance(coords, list) and len(coords) >= 2:
                        p.setdefault("LON", coords[0]); p.setdefault("LAT", coords[1])
                    rows.append(p)
        elif isinstance(payload.get("features"), dict):
            for f in payload["features"].values():
                if isinstance(f, dict): rows.append(f.get("properties") or f)
        else:
            for k in ("data", "records", "results"):
                if isinstance(payload.get(k), list): rows = payload[k]; break
    out = {}
    for row in rows:
        if not isinstance(row, dict): continue
        sid = station_id(row)
        if not sid: continue
        opened = str(first(row, ("DATOUVR", "datouvr", "date_ouverture", "DATE_OUVERTURE")) or "")[:10]
        closed = str(first(row, ("DATFERM", "datferm", "date_fermeture", "DATE_FERMETURE")) or "")[:10]
        typ = fint(first(row, ("TYPE_POSTE_ACTUEL", "type_poste_actuel", "TYPE_POSTE", "type_poste")))
        out[sid] = {
            "name": str(first(row, ("NOM_USUEL", "nom_usuel", "nom", "name")) or sid).strip(),
            "commune": str(first(row, ("COMMUNE", "commune")) or "").strip(),
            "lat": fnum(first(row, ("LAT", "lat", "latitude"))),
            "lon": fnum(first(row, ("LON", "lon", "longitude"))),
            "altitude": fnum(first(row, ("ALTI", "alti", "altitude"))),
            "opened": opened or None,
            "closed": closed or None,
            "opening_year": int(opened[:4]) if re.match(r"^\d{4}", opened) else None,
            "type_station": typ,
            "principal": typ in (0, 1, 2),
        }
    print("Métadonnées stations :", len(out))
    return out


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
    metadata = download_station_metadata()
    resources = discover_daily_resources()
    missing = [d for d in DEPARTMENTS if not cache_path(d).exists() and d in resources]
    targets = missing[:max(0, n)]
    print("Bootstrap départements :", targets or "aucun")
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


def record_ref(st: dict, kind: str, scope: str, d: date) -> Optional[dict]:
    node = st.get(kind) or {}
    if scope == "absolute": seq = node.get("absolute") or []
    elif scope == "monthly": seq = (node.get("monthly") or {}).get(f"{d.month:02d}", [])
    elif scope == "fortnight": seq = (node.get("fortnight") or {}).get(f"{d.month:02d}-{1 if d.day <= 14 else 2}", [])
    elif scope == "daily": seq = (node.get("daily") or {}).get(f"{d.month:02d}-{d.day:02d}", [])
    else: return None
    return seq_before(seq, d)


def classify(st: dict, d: date, value: float, kind: str) -> dict:
    mode = "min" if kind == "cold" else "max"
    refs = {scope: record_ref(st, kind, scope, d) for scope in ("absolute", "monthly", "fortnight", "daily")}
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


def event_from_value(st: dict, d: date, value: Optional[float], kind: str, provisional: bool = False) -> Optional[dict]:
    if value is None: return None
    if kind == "tropical" and value < 20.0: return None
    cls = classify(st, d, value, kind)
    if not cls["flags"] and not cls["near"]:
        return None
    return {
        "id": st.get("id"), "name": st.get("name"), "lat": st.get("lat"), "lon": st.get("lon"),
        "altitude": st.get("altitude"), "opening_year": st.get("opening_year"), "opened": st.get("opened"),
        "type_station": st.get("type_station"), "principal": bool(st.get("principal")),
        "department_code": st.get("department_code"), "region": st.get("region") or "France",
        "value": round(float(value), 1), "kind": kind, "flags": cls["flags"], "near": cls["near"],
        "records": cls["records"], "deltas": cls["deltas"], "provisional": provisional,
    }


def make_day_payload(d: date, station_values: Dict[str, dict], caches: Dict[str, dict], *, provisional: bool, source: str) -> dict:
    heat, cold, tropical = [], [], []
    all_values = []
    for sid, vals in station_values.items():
        st = caches.get(sid)
        if not st: continue
        tx = fnum(vals.get("tx")); tn = fnum(vals.get("tn"))
        e = event_from_value(st, d, tx, "heat", provisional)
        if e: heat.append(e)
        e = event_from_value(st, d, tn, "cold", provisional)
        if e: cold.append(e)
        e = event_from_value(st, d, tn, "tropical", provisional)
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


def generate_live() -> None:
    caches = all_station_caches()
    if not caches:
        print("[WARN] aucun cache historique : live vide")
    key = api_key()
    latest, raw0 = find_latest_hour(key)
    # Date climatologique d'affichage : date UTC du dernier paquet.
    d = latest.date()
    values: Dict[str, dict] = defaultdict(lambda: {"tx": None, "tn": None})
    tx_start = datetime(d.year, d.month, d.day, 6, tzinfo=timezone.utc)
    tn_start = datetime(d.year, d.month, d.day, 18, tzinfo=timezone.utc) - timedelta(days=1)
    for offset in range(LIVE_HISTORY_HOURS):
        h = latest - timedelta(hours=offset)
        if h < min(tx_start, tn_start): break
        raw = raw0 if offset == 0 else package_request(key, h)
        if raw is None: continue
        for row in parse_delimited(raw):
            sid = station_id(row)
            if not sid or sid not in caches: continue
            # TX/TN horaires, T en secours. Les fenêtres provisoires reprennent
            # les périodes utilisées par les extrêmes journaliers :
            # TX à partir de 06 UTC, TN à partir de 18 UTC la veille.
            t = fnum(first(row, ("t", "T")))
            tx = fnum(first(row, ("tx", "TX")))
            tn = fnum(first(row, ("tn", "TN")))
            if tx is None: tx = t
            if tn is None: tn = t
            if h >= tx_start and tx is not None:
                old = values[sid]["tx"]; values[sid]["tx"] = tx if old is None else max(old, tx)
            if h >= tn_start and tn is not None:
                old = values[sid]["tn"]; values[sid]["tn"] = tn if old is None else min(old, tn)
        time.sleep(PACKAGE_DELAY)
    day = make_day_payload(d, values, caches, provisional=True, source="Météo-France DPPaquetObs V2")
    payload = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID,
        "status": "ok", "generated_at": iso(utcnow()), "latest_observation_at": iso(latest),
        "coverage_departments": len(list(CACHE_DIR.glob("dep-*.json"))), "coverage_target": len(DEPARTMENTS),
        "day": day,
    }
    save_json(LIVE_FILE, payload)
    print("Live écrit :", d, "événements", sum(day["counts"].get(k, 0) for k in ("heat","cold","tropical")))
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


def generate_archive(year: int, month: int) -> None:
    if not (1800 <= year <= utcnow().year and 1 <= month <= 12):
        raise ValueError("année/mois invalide")
    caches = all_station_caches()
    resources = discover_daily_resources()
    values_by_day: Dict[date, Dict[str, dict]] = defaultdict(dict)
    for dep in DEPARTMENTS:
        if not cache_path(dep).exists(): continue
        res = resource_for_year(resources.get(dep, []), year)
        if not res: continue
        raw = get_bytes(res["url"])
        if raw is None: continue
        for row in parse_delimited(raw):
            d = parse_date_value(first(row, ("AAAAMMJJ", "DATE", "date")))
            if not d or d.year != year or d.month != month: continue
            sid = station_id(row)
            if not sid or sid not in caches: continue
            tx = fnum(first(row, ("TX", "tx"))) if quality_ok(first(row, ("QTX", "qtx"))) else None
            tn = fnum(first(row, ("TN", "tn"))) if quality_ok(first(row, ("QTN", "qtn"))) else None
            values_by_day[d][sid] = {"tx": tx, "tn": tn}
    days = {}
    for d in sorted(values_by_day):
        days[str(d.day)] = make_day_payload(d, values_by_day[d], caches, provisional=False, source="Météo-France climatologie quotidienne")
    payload = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID,
        "status": "ok", "generated_at": iso(utcnow()), "year": year, "month": month,
        "month_label": f"{MONTHS_FR[month-1]} {year}", "days": days,
        "coverage_departments": len(list(CACHE_DIR.glob("dep-*.json"))), "coverage_target": len(DEPARTMENTS),
    }
    out = ARCHIVE_DIR / f"{year:04d}-{month:02d}.json"
    save_json(out, payload)
    print("Archive écrite :", out, "jours", len(days))
    write_index()


def write_index() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    months = []
    if ARCHIVE_DIR.exists():
        for p in sorted(ARCHIVE_DIR.glob("????-??.json")):
            m = re.match(r"(\d{4})-(\d{2})", p.stem)
            if m: months.append({"year": int(m.group(1)), "month": int(m.group(2)), "file": f"archive/{p.name}"})
    live = load_json_file(LIVE_FILE)
    payload = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID,
        "status": "ok", "generated_at": iso(utcnow()),
        "available_months": months,
        "live_file": "live.json" if LIVE_FILE.exists() else None,
        "latest_date": (((live or {}).get("day") or {}).get("date")),
        "coverage_departments": len(list(CACHE_DIR.glob("dep-*.json"))), "coverage_target": len(DEPARTMENTS),
        "near_delta_c": NEAR_DELTA_C,
        "principal_types": [0,1,2],
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

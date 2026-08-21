#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Construit une partie du cache RR1 glissant 80 h, 100 % Météo-France.

La source d'amorçage est le jeu officiel data.gouv.fr
« Données climatologiques de base - horaires ». Le script recherche l'URL
réellement publiée pour le département et la période couvrant l'année en
cours, puis ne conserve que les 80 dernières heures. L'URL S3 Météo-France
n'est qu'un repli.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import itertools
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from generer_pluie_meteofrance import clean_rain, first, fnum, iso, parse_hourly_datetime, station_id

VERSION = "2.6.0"
CATALOG = "https://www.data.gouv.fr/api/1/datasets/donnees-climatologiques-de-base-horaires/"
BASE_HOR = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/HOR"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_dep(dep: str) -> str:
    dep = str(dep or "").strip().upper()
    if dep in {"2A", "2B"}:
        return dep
    digits = re.sub(r"\D", "", dep)
    return digits.zfill(2) if digits else dep


def direct_recent_urls(dep: str, year: int) -> list[tuple[str, str]]:
    period = f"{year - 1}-{year}"
    # Nom documenté pour les lots récents. On garde deux extensions en repli,
    # mais le catalogue data.gouv est toujours essayé en premier.
    names = [
        f"H_{dep}_latest-{period}.csv.gz",
        f"H_{dep}_latest-{period}.csv",
    ]
    return [(f"{BASE_HOR}/{name}", name) for name in names]


def _resource_haystack(r: dict) -> str:
    return " ".join(
        str(r.get(k) or "")
        for k in ("title", "url", "description", "format")
    ).lower()


def choose_resource(resources: list[dict], dep: str, year: int) -> Optional[dict]:
    """Choisit la ressource horaire officielle la plus récente pour un département.

    Accepte les deux écritures rencontrées dans les métadonnées :
      - HOR_departement_59_periode_2025-2026
      - H_59_latest-2025-2026.csv.gz
    et, en dernier recours, un lot du département contenant l'année courante.
    """
    dep_l = dep.lower()
    period = f"{year - 1}-{year}"
    exact = (
        f"hor_departement_{dep_l}_periode_{period}",
        f"hor-departement-{dep_l}-periode-{period}",
        f"h_{dep_l}_latest-{period}",
        f"h-{dep_l}-latest-{period}",
    )
    dep_tokens = (
        f"hor_departement_{dep_l}_periode_",
        f"hor-departement-{dep_l}-periode-",
        f"h_{dep_l}_latest-",
        f"h-{dep_l}-latest-",
    )
    scored = []
    for r in resources:
        url = str(r.get("url") or "")
        if not url:
            continue
        hay = _resource_haystack(r)
        ascii_hay = unicodedata.normalize("NFKD", hay).encode("ascii", "ignore").decode("ascii")
        compact = re.sub(r"[^a-z0-9]+", "", ascii_hay)
        dep_recognized = (
            any(t in hay for t in dep_tokens)
            or f"departement{dep_l.lower()}" in compact
            or f"h{dep_l.lower()}latest" in compact
        )
        if not dep_recognized:
            continue
        years = [int(x) for x in re.findall(r"(?:19|20)\d{2}", hay)]
        period_compact = re.sub(r"\D", "", period)
        has_exact = any(t in hay for t in exact) or period_compact in compact
        covers_year = year in years
        newest = max(years) if years else 0
        # exact période courante, puis lot contenant année courante, puis plus récent
        score = (0 if has_exact else 1 if covers_year else 2, -newest)
        modified = str(r.get("last_modified") or r.get("latest") or r.get("created_at") or "")
        scored.append((score, modified, r))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=False)
    best_score = scored[0][0]
    best = [x for x in scored if x[0] == best_score]
    best.sort(key=lambda x: x[1], reverse=True)
    return best[0][2]


def iter_rows_stream(response: requests.Response):
    response.raw.decode_content = False
    raw = response.raw
    ctype = (response.headers.get("content-type") or "").lower()
    enc = (response.headers.get("content-encoding") or "").lower()
    url = str(response.url).lower()
    is_gzip = "gzip" in ctype or enc == "gzip" or url.endswith(".gz")
    binary = gzip.GzipFile(fileobj=raw) if is_gzip else raw
    text = io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="")
    first_line = text.readline()
    if not first_line:
        return
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader = csv.DictReader(itertools.chain([first_line], text), delimiter=delimiter)
    for row in reader:
        yield {
            str(k).strip(): (v.strip() if isinstance(v, str) else v)
            for k, v in row.items()
            if k is not None
        }


def load_catalog(session: requests.Session) -> list[dict]:
    r = session.get(CATALOG, timeout=(15, 90))
    r.raise_for_status()
    resources = r.json().get("resources") or []
    print("Ressources catalogue :", len(resources))
    return resources


def consume_url(session, url, label, cutoff, now, hours, meta, dep):
    print("  essai :", label)
    with session.get(url, timeout=(20, 300), stream=True) as rr:
        rr.raise_for_status()
        kept = 0
        rows_seen = 0
        min_dt = max_dt = file_min_dt = file_max_dt = None
        seen_hours = set()
        cols_reported = False
        for row in iter_rows_stream(rr):
            rows_seen += 1
            if not cols_reported:
                print("  colonnes :", ", ".join(list(row.keys())[:14]))
                cols_reported = True
            dt = parse_hourly_datetime(first(row, ("AAAAMMJJHH", "AAAAMMJJHHMM", "DATE", "date", "validity_time")))
            if dt is None:
                continue
            file_min_dt = dt if file_min_dt is None or dt < file_min_dt else file_min_dt
            file_max_dt = dt if file_max_dt is None or dt > file_max_dt else file_max_dt
            if dt < cutoff or dt > now + timedelta(hours=1):
                continue
            sid = station_id(row)
            if not sid:
                continue
            rain = clean_rain(first(row, ("RR1", "rr1", "RR", "rr")))
            if rain is None:
                continue
            hdt = dt.replace(minute=0, second=0, microsecond=0)
            key = iso(hdt)
            hours.setdefault(key, {})[sid] = round(rain, 3)
            seen_hours.add(key)
            kept += 1
            min_dt = hdt if min_dt is None or hdt < min_dt else min_dt
            max_dt = hdt if max_dt is None or hdt > max_dt else max_dt

            lat = fnum(first(row, ("LAT", "lat", "latitude")))
            lon = fnum(first(row, ("LON", "lon", "longitude")))
            name = first(row, ("NOM_USUEL", "nom_usuel", "NOM", "nom", "name"))
            alti = fnum(first(row, ("ALTI", "alti", "altitude")))
            m = meta.setdefault(sid, {"department": dep})
            if name:
                m["name"] = str(name).strip()
            if lat is not None and -90 <= lat <= 90:
                m["lat"] = round(lat, 6)
            if lon is not None and -180 <= lon <= 180:
                m["lon"] = round(lon, 6)
            if alti is not None:
                m["altitude"] = round(alti, 1)

        print("  lignes lues :", rows_seen, "| RR1 conservées :", kept, "| heures utiles :", len(seen_hours))
        if file_max_dt is not None:
            print("  couverture fichier :", iso(file_min_dt), "->", iso(file_max_dt))
        return kept, min_dt, max_dt, len(seen_hours), file_max_dt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--departments", default=os.getenv("MF_BOOTSTRAP_DEPARTMENTS", ""))
    ap.add_argument("--output", default=os.getenv("MF_BOOTSTRAP_OUTPUT", "cache_pluie_horaire_part.json"))
    args = ap.parse_args()

    deps = [normalize_dep(x) for x in re.split(r"[,;\s]+", args.departments) if x.strip()]
    if not deps:
        print("Aucun département demandé.", file=sys.stderr)
        return 2

    now = utcnow().replace(minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(hours=80)
    session = requests.Session()
    session.headers.update({"User-Agent": f"alertes-meteo-carte-pluie-bootstrap/{VERSION}"})

    print("Version :", VERSION)
    print("Départements :", ", ".join(deps))
    print("Fenêtre demandée :", iso(cutoff), "->", iso(now))
    print("Source : Météo-France / data.gouv.fr uniquement")

    try:
        resources = load_catalog(session)
    except Exception as exc:
        print("[WARN] catalogue data.gouv indisponible :", exc)
        resources = []

    hours: dict[str, dict[str, float]] = {}
    meta: dict[str, dict] = {}
    total_values = 0
    successful_departments = 0

    for dep in deps:
        print(f"[{dep}]")
        kept = 0
        dmin = dmax = None
        tried = set()
        source_used = None

        res = choose_resource(resources, dep, now.year)
        catalog_url = str((res or {}).get("url") or "")
        catalog_label = str((res or {}).get("title") or catalog_url.rsplit("/", 1)[-1])
        if catalog_url:
            tried.add(catalog_url)
            try:
                k, dmin, dmax, _, file_max = consume_url(session, catalog_url, catalog_label, cutoff, now, hours, meta, dep)
                kept += k
                if k:
                    source_used = catalog_label
                elif file_max:
                    print(f"[WARN] {dep}: ressource trouvée mais aucune RR1 récente; dernière heure {iso(file_max)}")
            except Exception as exc:
                print(f"[WARN] {dep}: téléchargement catalogue impossible : {exc}")
        else:
            print(f"[WARN] {dep}: aucune ressource de période courante trouvée dans le catalogue")

        if kept == 0:
            for url, label in direct_recent_urls(dep, now.year):
                if url in tried:
                    continue
                try:
                    k, dmin, dmax, _, file_max = consume_url(session, url, label, cutoff, now, hours, meta, dep)
                    kept += k
                    if k:
                        source_used = label
                        break
                    if file_max:
                        print(f"[WARN] {dep}: URL directe sans RR1 récente; dernière heure {iso(file_max)}")
                except Exception as exc:
                    print(f"[WARN] {dep}: {label} impossible : {exc}")

        total_values += kept
        if kept:
            successful_departments += 1
            print(f"[{dep}] OK : {kept} RR1 | {iso(dmin)} -> {iso(dmax)} | {source_used}")
        else:
            print(f"[WARN] {dep}: aucune RR1 exploitable sur la fenêtre")

    out = {
        "schema_version": 2,
        "module_version": VERSION,
        "generated_at": iso(utcnow()),
        "source": "Météo-France - données climatologiques horaires",
        "hours": hours,
        "stations_meta": meta,
    }
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    counts = {}
    for bucket in hours.values():
        for sid in (bucket or {}):
            counts[sid] = counts.get(sid, 0) + 1
    c44 = sum(v >= 44 for v in counts.values())
    c66 = sum(v >= 66 for v in counts.values())
    print("Départements avec données :", successful_departments, "/", len(deps))
    print("Heures distinctes :", len(hours), "| valeurs :", total_values)
    print("Stations >=44 h :", c44, "| stations >=66 h :", c66)

    # Un lot peut contenir un département temporairement indisponible, mais il
    # doit apporter suffisamment d'historique pour être utile à la fusion.
    if len(hours) < 66 or c44 == 0:
        print("[ERREUR] lot horaire insuffisant; aucune publication de ce lot.", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

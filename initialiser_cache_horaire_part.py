#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Construit une PARTIE du cache horaire 80 h depuis Météo-France/data.gouv.fr.

v2.4.1 : utilise le nom réellement documenté pour le lot récent des
deux dernières années : H_XX_latest-YYYY-YYYY.csv.gz, hébergé sur le
stockage officiel Météo-France. Le catalogue data.gouv.fr sert uniquement
de repli si l'URL directe n'est pas disponible.
Le CSV gzip est lu en flux afin d'éviter les gros pics mémoire dans Actions.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from generer_pluie_meteofrance import clean_rain, first, iso, parse_hourly_datetime, station_id

VERSION = "2.4.1"
CATALOG = "https://www.data.gouv.fr/api/1/datasets/donnees-climatologiques-de-base-horaires/"
BASE_HOR = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/HOR"


def utcnow():
    return datetime.now(timezone.utc)


def normalize_dep(dep: str) -> str:
    dep = str(dep or "").strip().upper()
    if dep in {"2A", "2B"}:
        return dep
    digits = re.sub(r"\D", "", dep)
    return digits.zfill(2) if digits else dep


def direct_recent_urls(dep: str, year: int) -> list[tuple[str, str]]:
    """URL officielle du fichier récent couvrant les deux dernières années.

    Le guide data.gouv/Météo-France documente le schéma
    H_XX_latest-YYYY-YYYY.csv.gz (ex. H_01_latest-2023-2024.csv.gz).
    """
    period = f"{year - 1}-{year}"
    filename = f"H_{dep}_latest-{period}.csv.gz"
    return [(f"{BASE_HOR}/{filename}", filename)]


def choose_resource(resources: list[dict], dep: str, year: int) -> Optional[dict]:
    """Retrouve le lot récent du département dans le catalogue data.gouv.

    Le titre de ressource est actuellement de forme
    HOR_departement_XX_periode_YYYY-YYYY, tandis que le fichier cible
    sous-jacent peut être H_XX_latest-YYYY-YYYY.csv.gz. On accepte donc
    les deux représentations.
    """
    dep_l = dep.lower()
    period = f"{year - 1}-{year}"
    exact_tokens = (
        f"h_{dep_l}_latest-{period}",
        f"h-{dep_l}-latest-{period}",
        f"hor_departement_{dep_l}_periode_{period}",
        f"hor-departement-{dep_l}-periode-{period}",
    )
    generic_tokens = (
        f"h_{dep_l}_latest-",
        f"h-{dep_l}-latest-",
        f"hor_departement_{dep_l}_periode_",
        f"hor-departement-{dep_l}-periode-",
    )
    candidates = []
    for r in resources:
        title = str(r.get("title") or "")
        url = str(r.get("url") or "")
        description = str(r.get("description") or "")
        if not url:
            continue
        hay = " ".join((title, url, description)).lower()
        if not any(t in hay for t in generic_tokens):
            continue
        exact = any(t in hay for t in exact_tokens)
        years = [int(x) for x in re.findall(r"(?:19|20)\d{2}", hay)]
        newest = max(years) if years else 0
        updated = str(r.get("last_modified") or r.get("latest") or r.get("created_at") or "")
        candidates.append((0 if exact else 100, -newest, updated, title or url, r))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[3]))
    best_score, best_year = candidates[0][0], candidates[0][1]
    best = [x for x in candidates if x[0] == best_score and x[1] == best_year]
    best.sort(key=lambda x: x[2], reverse=True)
    return best[0][-1]


def iter_rows_stream(response: requests.Response):
    response.raw.decode_content = False
    raw = response.raw
    ctype = (response.headers.get("content-type") or "").lower()
    enc = (response.headers.get("content-encoding") or "").lower()
    url = str(response.url).lower()
    is_gzip = "gzip" in ctype or enc == "gzip" or url.endswith(".gz")
    binary = gzip.GzipFile(fileobj=raw) if is_gzip else raw
    text = io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="")
    reader = csv.DictReader(text, delimiter=";")
    for row in reader:
        yield {
            str(k).strip(): (v.strip() if isinstance(v, str) else v)
            for k, v in row.items()
            if k is not None
        }


def load_catalog(session: requests.Session) -> list[dict]:
    try:
        r = session.get(CATALOG, timeout=(15, 60))
        r.raise_for_status()
        resources = r.json().get("resources") or []
        print("Ressources catalogue (repli) :", len(resources))
        return resources
    except Exception as exc:
        print("[WARN] catalogue data.gouv indisponible :", exc)
        return []


def consume_url(session, url, label, cutoff, now, hours):
    """Lit une ressource et injecte ses RR1 des 80 dernières heures."""
    print("  essai :", label)
    with session.get(url, timeout=(20, 240), stream=True) as rr:
        rr.raise_for_status()
        kept = 0
        rows_seen = 0
        min_dt = None
        max_dt = None
        file_min_dt = None
        file_max_dt = None
        cols_reported = False
        seen_hours = set()
        for row in iter_rows_stream(rr):
            rows_seen += 1
            if not cols_reported:
                print("  colonnes :", ", ".join(list(row.keys())[:12]))
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
        print("  lignes lues :", rows_seen, "| RR1 conservées :", kept, "| heures utiles :", len(seen_hours))
        if file_max_dt is not None:
            print("  couverture fichier :", iso(file_min_dt), "->", iso(file_max_dt))
        return True, kept, min_dt, max_dt, len(seen_hours), file_max_dt


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

    print("Départements :", ", ".join(deps))
    print("Fenêtre conservée :", iso(cutoff), "->", iso(now))
    print("Mode : catalogue officiel data.gouv en priorité, URL directe Météo-France en secours")

    # Charger le catalogue une seule fois par lot : il fournit l'URL exacte
    # actuellement publiée pour chaque département et évite de dépendre
    # d'une convention de nom de fichier susceptible d'évoluer.
    resources: Optional[list[dict]] = load_catalog(session)
    hours: dict[str, dict[str, float]] = {}
    total_values = 0
    successful_departments = 0

    for dep in deps:
        print(f"[{dep}]")
        kept = 0
        min_dt = max_dt = None
        source_used = None
        tried_urls = set()

        # 1) catalogue officiel data.gouv : source d'autorité pour l'URL exacte
        res = choose_resource(resources or [], dep, now.year)
        catalog_url = str((res or {}).get("url") or "")
        catalog_label = str((res or {}).get("title") or catalog_url.rsplit("/", 1)[-1])
        if catalog_url:
            tried_urls.add(catalog_url)
            try:
                ok, k1, dmin1, dmax1, hcount1, file_max1 = consume_url(
                    session, catalog_url, catalog_label, cutoff, now, hours
                )
                if ok and k1:
                    kept += k1
                    min_dt = dmin1
                    max_dt = dmax1
                    source_used = catalog_label
                elif ok and file_max1 is not None:
                    print(f"[WARN] {dep}: ressource catalogue lisible mais aucune RR1 récente; dernière heure {iso(file_max1)}")
            except Exception as exc:
                print(f"[WARN] {dep}: ressource catalogue impossible : {exc}")
        else:
            print(f"[WARN] {dep}: aucune ressource récente trouvée dans le catalogue")

        # 2) URL directe en secours seulement si le catalogue n'a rien fourni
        if kept == 0:
            for candidate_url, candidate_label in direct_recent_urls(dep, now.year):
                if candidate_url in tried_urls:
                    continue
                tried_urls.add(candidate_url)
                try:
                    ok, k2, dmin2, dmax2, hcount2, file_max2 = consume_url(
                        session, candidate_url, candidate_label, cutoff, now, hours
                    )
                    if ok and k2:
                        kept += k2
                        min_dt = dmin2
                        max_dt = dmax2
                        source_used = candidate_label
                        break
                    if ok and file_max2 is not None:
                        print(f"[WARN] {dep}: URL directe lisible mais aucune RR1 récente; dernière heure {iso(file_max2)}")
                except Exception as exc:
                    print(f"[WARN] {dep}: {candidate_label} impossible : {exc}")

        if source_used:
            print(f"[{dep}] source retenue : {source_used}")

        total_values += kept
        if kept:
            successful_departments += 1
            print(f"[{dep}] OK : {kept} RR1 | {iso(min_dt)} -> {iso(max_dt)}")
        else:
            print(f"[WARN] {dep}: aucune RR1 dans les 80 dernières heures")

    out = {
        "schema_version": 1,
        "module_version": VERSION,
        "generated_at": iso(utcnow()),
        "hours": hours,
    }
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("Départements avec données :", successful_departments, "/", len(deps))
    print("Heures distinctes :", len(hours), "| valeurs :", total_values, "| fichier :", args.output)

    # Validation forte : ne jamais publier un lot qui n'apporte pas réellement
    # assez d'historique pour participer au calcul 48 h.
    counts = {}
    for bucket in hours.values():
        for sid in (bucket or {}):
            counts[sid] = counts.get(sid, 0) + 1
    stations44 = sum(v >= 44 for v in counts.values())
    print("Stations avec >=44 RR1 dans ce lot :", stations44)
    if len(hours) < 48 or stations44 == 0:
        print("[ERREUR] lot historique insuffisant : il ne sera pas publié.", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

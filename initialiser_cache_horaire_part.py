#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Construit une PARTIE du cache horaire 80 h depuis data.gouv/Météo-France.

Conçu pour GitHub Actions matrix : un job ne traite qu'un petit groupe de
Départements afin d'éviter les timeouts/SIGTERM 143.
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

VERSION = "2.3.4"
CATALOG = "https://www.data.gouv.fr/api/1/datasets/donnees-climatologiques-de-base-horaires/"


def utcnow():
    return datetime.now(timezone.utc)


def normalize_dep(dep: str) -> str:
    dep = str(dep or "").strip().upper()
    if dep in {"2A", "2B"}:
        return dep
    digits = re.sub(r"\D", "", dep)
    return digits.zfill(2) if digits else dep


def choose_resource(resources: list[dict], dep: str, year: int) -> Optional[dict]:
    needles = [
        f"HOR_departement_{dep}_periode_".lower(),
        f"HOR_departement_{dep.lower()}_periode_".lower(),
    ]
    candidates = []
    for r in resources:
        title = str(r.get("title") or "")
        url = str(r.get("url") or "")
        if not url:
            continue
        low = title.lower()
        if not any(n in low for n in needles):
            continue
        # priorité absolue au lot qui contient l'année courante
        score = 0 if str(year) in title else 10
        # ensuite aux fichiers csv.gz
        if not (url.lower().endswith(".gz") or "csv.gz" in low):
            score += 1
        updated = str(r.get("last_modified") or r.get("latest") or r.get("created_at") or "")
        candidates.append((score, updated, title, r))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=False)
    best_score = min(x[0] for x in candidates)
    best = [x for x in candidates if x[0] == best_score]
    best.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return best[0][3]


def iter_rows_stream(response: requests.Response):
    response.raw.decode_content = False
    raw = response.raw
    is_gzip = False
    ctype = (response.headers.get("content-type") or "").lower()
    enc = (response.headers.get("content-encoding") or "").lower()
    url = str(response.url).lower()
    if "gzip" in ctype or enc == "gzip" or url.endswith(".gz"):
        is_gzip = True
    binary = gzip.GzipFile(fileobj=raw) if is_gzip else raw
    text = io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="")
    # Météo-France utilise le point-virgule sur ces fichiers.
    reader = csv.DictReader(text, delimiter=";")
    for row in reader:
        yield {str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k is not None}


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

    r = session.get(CATALOG, timeout=(15, 45))
    r.raise_for_status()
    resources = r.json().get("resources") or []
    print("Ressources catalogue :", len(resources))

    hours: dict[str, dict[str, float]] = {}
    total_values = 0

    for dep in deps:
        res = choose_resource(resources, dep, now.year)
        if not res:
            print(f"[WARN] {dep}: aucune ressource horaire récente trouvée")
            continue
        title = str(res.get("title") or "")
        url = str(res.get("url") or "")
        print(f"[{dep}] {title}")
        try:
            with session.get(url, stream=True, timeout=(20, 180)) as rr:
                rr.raise_for_status()
                kept = 0
                for row in iter_rows_stream(rr):
                    dt = parse_hourly_datetime(first(row, ("AAAAMMJJHH", "AAAAMMJJHHMM", "DATE", "date", "validity_time")))
                    if dt is None or dt < cutoff or dt > now + timedelta(hours=1):
                        continue
                    sid = station_id(row)
                    if not sid:
                        continue
                    rain = clean_rain(first(row, ("RR1", "rr1", "RR", "rr")))
                    if rain is None:
                        continue
                    key = iso(dt.replace(minute=0, second=0, microsecond=0))
                    hours.setdefault(key, {})[sid] = round(rain, 3)
                    kept += 1
                total_values += kept
                print(f"[{dep}] {kept} valeurs conservées")
        except Exception as exc:
            print(f"[WARN] {dep}: téléchargement/lecture impossible: {exc}")

    out = {
        "schema_version": 1,
        "module_version": VERSION,
        "generated_at": iso(utcnow()),
        "hours": hours,
    }
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("Heures :", len(hours), "| valeurs :", total_values, "| fichier :", args.output)
    return 0 if hours else 3


if __name__ == "__main__":
    raise SystemExit(main())

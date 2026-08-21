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

VERSION = "2.3.5"
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
    """Choisit le fichier horaire récent d'un département.

    data.gouv ne renseigne pas toujours ``title`` de façon exploitable. Le
    nom HOR_departement_XX... peut se trouver uniquement dans l'URL du
    fichier. On inspecte donc titre + URL + identifiant/description et on
    privilégie le lot contenant l'année courante (fichier des deux dernières
    années, mis à jour quotidiennement par Météo-France).
    """
    dep_l = dep.lower()
    dep_patterns = (
        f"hor_departement_{dep_l}_periode_",
        f"hor-departement-{dep_l}-periode-",
        f"hor_departement_{dep_l}",
        f"hor-departement-{dep_l}",
    )
    candidates = []
    for r in resources:
        title = str(r.get("title") or "")
        url = str(r.get("url") or "")
        description = str(r.get("description") or "")
        rid = str(r.get("id") or "")
        fmt = str(r.get("format") or "")
        if not url:
            continue
        hay = " ".join((title, url, description, rid)).lower()
        if not any(pat in hay for pat in dep_patterns):
            continue

        # Priorité au fichier récent couvrant l'année courante.
        score = 100
        if str(year) in hay:
            score -= 60
        if str(year - 1) in hay and str(year) in hay:
            score -= 20
        if "periode" in hay or "period" in hay:
            score -= 5
        if url.lower().endswith(".gz") or "csv.gz" in hay or "gzip" in fmt.lower():
            score -= 5

        # Éviter si possible les anciennes décennies volumineuses.
        old_years = re.findall(r"(?:19|20)\d{2}", hay)
        if old_years and max(map(int, old_years)) < year - 1:
            score += 80

        updated = str(
            r.get("last_modified")
            or r.get("latest")
            or r.get("created_at")
            or ""
        )
        size = r.get("filesize") or r.get("file_size") or r.get("size") or 0
        try:
            size = int(size)
        except Exception:
            size = 0
        candidates.append((score, -len(updated), size, updated, title or url.rsplit('/', 1)[-1], r))

    if not candidates:
        return None

    # score faible = meilleur. À score égal : ressource la plus récente,
    # puis fichier le plus petit (généralement le lot 2 ans, non la décennie).
    candidates.sort(key=lambda x: (x[0], -int(re.sub(r"\D", "", x[3])[:14] or "0"), x[2], x[4]))
    return candidates[0][-1]


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
        label = title or url.rsplit('/', 1)[-1]
        print(f"[{dep}] ressource: {label}")
        try:
            rr = session.get(url, timeout=(20, 180))
            rr.raise_for_status()
            # parse_delimited détecte le gzip par sa signature binaire. C'est
            # plus fiable que de se fier au suffixe de l'URL data.gouv.
            from generer_pluie_meteofrance import parse_delimited
            rows = parse_delimited(rr.content)
            kept = 0
            min_dt = None
            max_dt = None
            for row in rows:
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
                min_dt = dt if min_dt is None or dt < min_dt else min_dt
                max_dt = dt if max_dt is None or dt > max_dt else max_dt
            total_values += kept
            if kept:
                print(f"[{dep}] {kept} valeurs conservées | {iso(min_dt)} -> {iso(max_dt)}")
            else:
                print(f"[WARN] {dep}: fichier trouvé mais aucune RR1 dans les 80 dernières heures")
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

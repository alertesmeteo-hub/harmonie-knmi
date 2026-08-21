#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from generer_pluie_meteofrance import iso, parse_iso, utcnow

VERSION = "2.6.0"


def load(path: Path):
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {"hours": {}}
    except Exception:
        return {"hours": {}}


def merge_one(target_hours: dict, target_meta: dict, data: dict):
    for h, bucket in (data.get("hours") or {}).items():
        if not isinstance(bucket, dict):
            continue
        target_hours.setdefault(h, {}).update(bucket)
    for sid, meta in (data.get("stations_meta") or {}).items():
        if isinstance(meta, dict):
            target_meta.setdefault(str(sid), {}).update(meta)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="parts")
    ap.add_argument("--existing", default="cache_pluie_horaire.json")
    ap.add_argument("--output", default="cache_pluie_horaire.json")
    args = ap.parse_args()

    merged = {}
    meta = {}
    existing = Path(args.existing)
    if existing.exists():
        merge_one(merged, meta, load(existing))

    files = sorted(Path(args.parts).rglob("*.json"))
    print("Parties trouvées :", len(files))
    for p in files:
        merge_one(merged, meta, load(p))

    dts = [parse_iso(k) for k in merged]
    dts = [x for x in dts if x is not None]
    if not dts:
        print("[ERREUR] aucune heure à fusionner")
        return 2

    latest = max(dts)
    cutoff = latest - timedelta(hours=79)
    keep = {
        k: v for k, v in merged.items()
        if parse_iso(k) is not None and cutoff <= parse_iso(k) <= latest
    }

    # Ne conserver que les métadonnées des stations réellement présentes.
    present = {sid for bucket in keep.values() for sid in (bucket or {})}
    meta = {sid: m for sid, m in meta.items() if sid in present}

    out = {
        "schema_version": 2,
        "module_version": VERSION,
        "generated_at": iso(utcnow()),
        "latest_hour": iso(latest),
        "source": "Météo-France uniquement",
        "hours": dict(sorted(keep.items())),
        "stations_meta": meta,
    }
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    counts = {}
    for bucket in keep.values():
        for sid in (bucket or {}):
            counts[sid] = counts.get(sid, 0) + 1
    c44 = sum(v >= 44 for v in counts.values())
    c66 = sum(v >= 66 for v in counts.values())
    print("Cache fusionné :", len(keep), "heures |", sum(len(v) for v in keep.values()), "valeurs")
    print("Stations >=44 h :", c44, "| stations >=66 h :", c66)
    print("Métadonnées stations :", len(meta))
    if len(keep) < 66 or c44 == 0:
        print("[ERREUR] cache fusionné insuffisant pour préparer 48/72 h")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Historique des images radar observées (3 h glissantes) pour l'animation de la carte radar.

La branche radar-data est écrasée à chaque passage et ne gardait que les 3 dernières images (10 min).
Ce script reprend l'historique publié au passage précédent (dossier --precedent, copie de radar-data),
y ajoute les images observées du passage courant (build/radar/map/obs_*.png, horodatées par
map/index.json) sous un nom daté, retire celles de plus de --heures, et écrit map/historique/index.json.

Usage : python scripts/radar_historique.py build/radar [--precedent <copie de radar-data>] [--heures 3]
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path


def heure(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("build", type=Path)
    ap.add_argument("--precedent", type=Path)
    ap.add_argument("--heures", type=float, default=3.0)
    args = ap.parse_args()

    carte = args.build / "map"
    index = json.loads((carte / "index.json").read_text(encoding="utf-8"))
    dest = carte / "historique"
    dest.mkdir(parents=True, exist_ok=True)

    # 1) images déjà publiées
    if args.precedent and (args.precedent / "map" / "historique").is_dir():
        for f in (args.precedent / "map" / "historique").glob("obs_*.png"):
            shutil.copy2(f, dest / f.name)
    # 2) images observées de ce passage
    for fr in index.get("frames", []):
        if fr.get("kind") == "observation":
            t = heure(fr["time"])
            shutil.copy2(carte / Path(fr["file"]).name, dest / f"obs_{t:%Y%m%dT%H%MZ}.png")
    # 3) fenêtre glissante
    fin = heure(index["radar_time"])
    limite = fin - timedelta(hours=args.heures)
    images = []
    for f in sorted(dest.glob("obs_*.png")):
        t = datetime.strptime(f.stem[4:], "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
        if t < limite or t > fin + timedelta(minutes=5):
            f.unlink()
        else:
            images.append({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "file": f"map/historique/{f.name}"})
    (dest / "index.json").write_text(
        json.dumps({"schema_version": 1, "radar_time": index["radar_time"], "heures": args.heures, "pas_minutes": 5,
                    "bounds": index.get("render", {}).get("bounds"), "frames": images}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"historique radar : {len(images)} images de {images[0]['time'] if images else '—'} à {images[-1]['time'] if images else '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

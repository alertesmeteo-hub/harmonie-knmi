#!/usr/bin/env python3
"""Archive les observations radar pluie sur la branche radar-archive (jamais le nowcast).

Pour rester leger (la mosaique France fait ~190 Ko par image), on n'archive que la
fenetre des Pyrenees-Orientales (~10 Ko), toutes les 15 min. Retention :
  - jours avec de la pluie sur le departement : 366 jours ;
  - jours sans pluie sur le departement      : 30 jours.
Chaque jour a un index.json (frames + drapeau pluie) et pluie/days.json liste les jours
pour l'interface /radar/archives. Idempotent ; migre aussi les anciennes images pleine taille.
"""
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

RETENTION_RAIN_DAYS = 366
RETENTION_DRY_DAYS = 30
STEP_MINUTES = 15

# Mosaique nationale : EPSG:4326, [[41, -6], [52, 10.5]], 1200 x 800.
SRC_BOUNDS = (41.0, -6.0, 52.0, 10.5)  # lat_min, lon_min, lat_max, lon_max
SRC_W, SRC_H = 1200, 800
# Fenetre archivee (marge autour du 66) et zone servant a detecter la pluie.
CROP = (42.0, 1.4, 43.25, 3.5)
DEPT = (42.33, 1.72, 42.92, 3.18)


def px(lat: float, lon: float) -> tuple[float, float]:
    la0, lo0, la1, lo1 = SRC_BOUNDS
    return (lon - lo0) / (lo1 - lo0) * SRC_W, (la1 - lat) / (la1 - la0) * SRC_H


def box(b: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    x0, y1 = px(b[0], b[1])
    x1, y0 = px(b[2], b[3])
    return int(np.floor(x0)), int(np.floor(y0)), int(np.ceil(x1)), int(np.ceil(y1))


CROP_BOX = box(CROP)
DEPT_BOX = box(DEPT)


def archive_image(src: Path, dest: Path) -> bool:
    """Ecrit la version recadree de src ; renvoie True s'il pleut sur le departement."""
    im = Image.open(src).convert("RGBA")
    if im.size != (SRC_W, SRC_H):
        raise ValueError(f"taille inattendue {im.size} pour {src}")
    alpha = np.asarray(im)[..., 3]
    x0, y0, x1, y1 = DEPT_BOX
    rain = bool((alpha[y0:y1, x0:x1] > 0).any())
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.crop(CROP_BOX).save(dest, optimize=True)
    return rain


def load_day(day_dir: Path) -> dict:
    path = day_dir / "index.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"frames": {}}


def save_day(day_dir: Path, data: dict) -> None:
    data["rain"] = any(data["frames"].values())
    (day_dir / "index.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    build_dir = Path(sys.argv[1])
    archive_dir = Path(sys.argv[2])
    pluie_dir = archive_dir / "pluie"
    changed = False

    index = json.loads((build_dir / "map" / "index.json").read_text(encoding="utf-8"))
    added = 0
    for frame in (f for f in index["frames"] if f["kind"] == "observation"):
        t = datetime.fromisoformat(frame["time"].replace("Z", "+00:00"))
        if t.minute % STEP_MINUTES:
            continue
        day_dir = pluie_dir / f"{t.year:04d}" / f"{t.month:02d}" / f"{t.day:02d}"
        key = f"{t.hour:02d}{t.minute:02d}"
        data = load_day(day_dir)
        if key in data["frames"]:
            continue
        data["frames"][key] = archive_image(build_dir / frame["file"], day_dir / f"{key}.png")
        save_day(day_dir, data)
        added += 1

    # Migration : anciennes images pleine taille (1200 x 800) ou hors pas de 15 min.
    migrated = 0
    if pluie_dir.exists():
        for png in sorted(pluie_dir.glob("*/*/*/*.png")):
            day_dir = png.parent
            data = load_day(day_dir)
            key = png.stem
            if key in data["frames"]:
                continue
            if int(key[2:]) % STEP_MINUTES:
                png.unlink()
                migrated += 1
                continue
            tmp = png.with_suffix(".tmp.png")
            data["frames"][key] = archive_image(png, tmp)
            tmp.replace(png)
            save_day(day_dir, data)
            migrated += 1

    # Retention + liste des jours.
    now = datetime.now(timezone.utc)
    removed = 0
    days = []
    if pluie_dir.exists():
        for day_dir in sorted(pluie_dir.glob("*/*/*")):
            if not day_dir.is_dir():
                continue
            try:
                date = datetime(int(day_dir.parts[-3]), int(day_dir.parts[-2]), int(day_dir.parts[-1]), tzinfo=timezone.utc)
            except ValueError:
                continue
            data = load_day(day_dir)
            rain = any(data["frames"].values())
            age = (now - date).days
            if age > RETENTION_RAIN_DAYS or (not rain and age > RETENTION_DRY_DAYS):
                shutil.rmtree(day_dir)
                removed += 1
                continue
            days.append({"date": date.strftime("%Y-%m-%d"), "rain": rain, "frames": len(data["frames"])})
        for d in sorted(pluie_dir.glob("*/*"), reverse=True) + sorted(pluie_dir.glob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        listing = {
            "crop_bounds": [[CROP[0], CROP[1]], [CROP[2], CROP[3]]],
            "step_minutes": STEP_MINUTES,
            "days": days,
        }
        text = json.dumps(listing, separators=(",", ":"))
        path = pluie_dir / "days.json"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed = True

    print(f"{added} image(s) archivee(s), {migrated} migree(s), {removed} jour(s) purge(s).")
    return 0 if (added or migrated or removed or changed) else 1


if __name__ == "__main__":
    sys.exit(main())

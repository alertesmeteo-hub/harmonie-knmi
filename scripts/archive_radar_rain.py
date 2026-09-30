#!/usr/bin/env python3
"""Archive les observations radar pluie (jamais le nowcast, qui est du calcul derive,
pas une observation) sur une branche a part (radar-archive), en plus de la branche
radar-data qui reste ecrasee a chaque run pour l'affichage en direct. Idempotent :
ignore les frames deja archivees (meme horodatage). Purge au-dela de RETENTION_DAYS
pour garder une fenetre glissante d'1 an plutot qu'une croissance illimitee.
"""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION_DAYS = 366


def main() -> int:
    build_dir = Path(sys.argv[1])
    archive_dir = Path(sys.argv[2])

    index = json.loads((build_dir / "map" / "index.json").read_text(encoding="utf-8"))
    observations = [f for f in index["frames"] if f["kind"] == "observation"]

    added = 0
    for frame in observations:
        t = datetime.fromisoformat(frame["time"].replace("Z", "+00:00"))
        dest_dir = archive_dir / "pluie" / f"{t.year:04d}" / f"{t.month:02d}" / f"{t.day:02d}"
        dest = dest_dir / f"{t.hour:02d}{t.minute:02d}.png"
        if dest.exists():
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(build_dir / frame["file"], dest)
        added += 1

    removed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    pluie_dir = archive_dir / "pluie"
    if pluie_dir.exists():
        for year_dir in sorted(pluie_dir.iterdir()):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir()):
                    if not day_dir.is_dir():
                        continue
                    try:
                        day_date = datetime(int(year_dir.name), int(month_dir.name), int(day_dir.name), tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if day_date < cutoff:
                        shutil.rmtree(day_dir)
                        removed += 1
                if month_dir.exists() and not any(month_dir.iterdir()):
                    month_dir.rmdir()
            if year_dir.exists() and not any(year_dir.iterdir()):
                year_dir.rmdir()

    print(
        f"{added} image(s) archivee(s) sur {len(observations)} observation(s), "
        f"{removed} jour(s) purge(s) (retention {RETENTION_DAYS} j)."
    )
    return 0 if (added or removed) else 1


if __name__ == "__main__":
    sys.exit(main())

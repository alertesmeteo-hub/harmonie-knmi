#!/usr/bin/env python3
"""Pluie observée par le radar, commune par commune (Pyrénées-Orientales), sur 1 h à 72 h glissantes.

Deux temps :
  1. sample  : appelé par update_radar_nowcast.py avec les mosaïques 5 min du paquet ; écrit la lame
               d'eau 5 min (mm) au village (moyenne 3 x 3 pixels de 500 m autour de la mairie).
  2. merge   : fusionne ces échantillons avec l'état précédent (72 h de pas de 5 min, stocké en
               creux), puis publie cumuls/66.json : cumuls glissants par commune et série horaire.

Les pas manquants (paquet raté) sont compensés au prorata si la fenêtre est couverte à 70 % au moins ;
sinon le cumul est marqué incomplet.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

FENETRES_H = (1, 3, 6, 12, 24, 48, 72)
RETENTION_H = 73
STEP_MIN = 5
CATALOGUE = Path(__file__).resolve().parent.parent / "config" / "communes-66-mairies.json"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_communes() -> list[list]:
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))["communes"]


def sample(frames, out_path: Path) -> None:
    """frames : objets RadarFrame (timestamp, rain_rate mm/h, grid)."""
    import numpy as np

    communes = load_communes()
    data = {}
    for f in frames:
        h, w = f.rain_rate.shape
        vals = []
        for _code, _nom, lat, lon in communes:
            row, col = f.grid.pixel(lon, lat)
            r, c = int(round(row)), int(round(col))
            if r < 1 or c < 1 or r > h - 2 or c > w - 2:
                vals.append(None)
                continue
            mmh = float(np.mean(f.rain_rate[r - 1:r + 2, c - 1:c + 2]))
            vals.append(round(mmh / 12.0, 2))  # mm en 5 min
        data[iso(f.timestamp)] = vals
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"communes": [c[0] for c in communes], "frames": data}, separators=(",", ":")), encoding="utf-8")


def merge(samples_path: Path, state_path: Path | None, out_dir: Path) -> None:
    communes = load_communes()
    codes = [c[0] for c in communes]
    pos = {c: i for i, c in enumerate(codes)}
    # état : {ts: {index_commune: mm}} en creux (seules les valeurs > 0)
    etat: dict[str, dict[str, float]] = {}
    if state_path and state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
            old_codes = prev.get("communes", codes)
            for ts, vals in prev.get("frames", {}).items():
                etat[ts] = {str(pos[old_codes[int(i)]]): v for i, v in vals.items() if old_codes[int(i)] in pos}
        except (ValueError, KeyError, IndexError) as e:
            print("État précédent illisible, on repart de zéro :", e)
    new = json.loads(samples_path.read_text(encoding="utf-8"))
    for ts, vals in new["frames"].items():
        etat[ts] = {str(pos[code]): v for code, v in zip(new["communes"], vals) if v and code in pos}

    latest = max(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in etat)
    limite = latest - timedelta(hours=RETENTION_H)
    etat = {t: v for t, v in etat.items() if datetime.fromisoformat(t.replace("Z", "+00:00")) > limite}
    temps = sorted(etat)
    dts = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in temps]

    n = len(codes)
    cumuls = [[0.0] * len(FENETRES_H) for _ in range(n)]
    couverture = {}
    for k, fh in enumerate(FENETRES_H):
        debut = latest - timedelta(hours=fh)
        idx = [i for i, d in enumerate(dts) if d > debut]
        attendus = fh * 60 // STEP_MIN
        presents = len(idx)
        facteur = attendus / presents if presents and presents / attendus >= 0.7 else 1.0
        couverture[str(fh)] = round(min(1.0, presents / attendus), 2)
        for i in idx:
            for ci, v in etat[temps[i]].items():
                cumuls[int(ci)][k] += v
        for row in cumuls:
            row[k] = round(row[k] * facteur, 1)

    # série horaire sur 72 h : heure UTC pleine, cumul des pas de 5 min de l'heure qui se termine
    heures = [(latest.replace(minute=0, second=0) - timedelta(hours=h)) for h in range(71, -1, -1)]
    serie = [[0.0] * 72 for _ in range(n)]
    pas_par_heure = [0] * 72
    for d, t in zip(dts, temps):
        fin = d.replace(minute=0, second=0) + (timedelta(hours=1) if d.minute or d.second else timedelta())
        h = int((fin - heures[0]) / timedelta(hours=1)) - 1
        if 0 <= h < 72:
            pas_par_heure[h] += 1
            for ci, v in etat[t].items():
                serie[int(ci)][h] += v
    for row in serie:
        for h in range(72):
            # heure passée incomplète (paquet raté) : compensée au prorata ; l'heure en cours reste telle quelle
            if h < 71 and 8 <= pas_par_heure[h] < 12:
                row[h] *= 12 / pas_par_heure[h]
            row[h] = round(row[h], 1)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "etat-66.json").write_text(json.dumps({
        "communes": codes,
        "frames": {t: {i: round(v, 2) for i, v in etat[t].items()} for t in temps},
    }, separators=(",", ":")), encoding="utf-8")
    payload = {
        "schema_version": 1,
        "generated_at": iso(datetime.now(timezone.utc)),
        "radar_time": iso(latest),
        "source": "Météo-France, mosaïque radar lame d'eau 5 min (500 m), moyenne 1,5 km autour de la mairie",
        "fenetres_h": list(FENETRES_H),
        "couverture": couverture,
        "heures": [iso(h + timedelta(hours=1)) for h in heures],
        "heures_completes": [p >= 8 for p in pas_par_heure],
        "communes": {
            c[0]: {"nom": c[1], "cumuls": cumuls[i], "horaire": serie[i]}
            for i, c in enumerate(communes)
        },
    }
    (out_dir / "66.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    top = sorted(((cumuls[i][4], c[1]) for i, c in enumerate(communes)), reverse=True)[:3]
    print(f"Cumuls radar 66 au {iso(latest)} : {len(temps)} pas de 5 min ; 24 h maxi : {top}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--samples", required=True)
    p.add_argument("--state")
    p.add_argument("--out-dir", required=True)
    a = p.parse_args()
    merge(Path(a.samples), Path(a.state) if a.state else None, Path(a.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

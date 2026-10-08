#!/usr/bin/env python3
"""Prévisions multi-modèles commune par commune (Pyrénées-Orientales).

Lit les fichiers departements/66.json publiés par chaque dépôt modèle de l'organisation et écrit,
pour chacune des 226 communes, un petit fichier communes/<INSEE>.json :
  - quotidien : pluie, Tmax, Tmin et rafale maxi par jour (heure de Paris) et par modèle, sur 15 jours ;
  - ensembles : moyenne, p10 et p90 des ensembles GEFS, AIGEFS et PEARP ;
  - horaire   : 48 h heure par heure (meilleur modèle disponible à chaque heure) pour la frise du
                risque, avec la pluie horaire de chaque modèle à maille fine pour mesurer l'accord.
Ces fichiers alimentent la fiche « Ma commune », la frise horaire et l'indice de confiance du site.
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

RAW = "https://raw.githubusercontent.com/alertesmeteo-hub/"
PARIS = ZoneInfo("Europe/Paris")
JOURS = 15
HEURES = 48
S = requests.Session()
S.headers["User-Agent"] = "alertes-meteo-communes-66/1.0"

# id, dépôt, nom affiché, portée utile (h) pour le quotidien
DETERMINISTES = [
    ("arome", "arome-meteofrance", "AROME 1,3 km (Météo-France)"),
    ("aromeifs", "AROME-IFS-2.5-km", "AROME-IFS 2,5 km"),
    ("harmonie", "harmonie", "HARMONIE 5,5 km (KNMI)"),
    ("harmoniedmi", "harmonie-dmi-2km", "HARMONIE 2 km (DMI)"),
    ("arpege", "arpege-meteo-france", "ARPEGE 0,1° (Météo-France)"),
    ("arpege025", "ARPEGE-0.25", "ARPEGE 0,25° (Météo-France)"),
    ("iconeu", "ICON-EU-7-km", "ICON-EU 7 km (DWD)"),
    ("iconglobal", "ICON-GLOBAL-13-km", "ICON Global 13 km (DWD)"),
    ("ukmo", "UKMO-GLOBAL-10-km", "UKMO 10 km (Met Office)"),
    ("cep", "cep", "ECMWF IFS (CEP)"),
    ("aifs", "aifs", "ECMWF AIFS (IA)"),
    ("gfs", "gfs", "GFS 25 km (NOAA)"),
]
ENSEMBLES = [
    ("pearp", "PEARP-25-km", "PEARP 35 membres (Météo-France)"),
    ("gefs", "GEFS-25-km", "GEFS 31 membres (NOAA)"),
    ("aigefs", "AIGEFS-25-km", "AIGEFS 31 membres (NOAA, IA)"),
]
# ordre de préférence pour la frise horaire (maille fine d'abord)
PRIORITE_HORAIRE = ["arome", "aromeifs", "harmonie", "arpege", "iconeu", "iconglobal", "ukmo", "cep", "gfs"]
MODELES_FINS = ["arome", "aromeifs", "harmonie", "harmoniedmi", "arpege", "iconeu"]


def get(url: str):
    r = S.get(url, timeout=120)
    r.raise_for_status()
    return r.json()


def parse_t(v) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


class Serie:
    """Série temporelle d'une commune pour un modèle : temps UTC + colonnes."""

    def __init__(self, temps: list[datetime], cols: dict[str, list]):
        self.t = temps
        self.c = cols
        tot = cols.get("total")
        if tot is not None and all(v is None for v in tot):
            tot = None
        if tot is not None:
            # cumul depuis le début du run : on comble les trous par la dernière valeur connue
            last, plein = 0.0, []
            for v in tot:
                last = v if isinstance(v, (int, float)) else last
                plein.append(last)
            tot = plein
        if tot is None and cols.get("pluie") is not None:
            # pluie par pas (accumulée depuis le pas précédent) -> cumul depuis le début du run
            acc, tot = 0.0, []
            for v in cols["pluie"]:
                acc += v or 0.0
                tot.append(acc)
        self.total = tot

    def cumul_a(self, t: datetime) -> float | None:
        if not self.total or t < self.t[0] or t > self.t[-1]:
            return None
        i = bisect_left(self.t, t)
        if self.t[i] == t:
            return self.total[i]
        t0, t1 = self.t[i - 1], self.t[i]
        v0, v1 = self.total[i - 1] or 0.0, self.total[i] or 0.0
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)

    def pluie_entre(self, a: datetime, b: datetime) -> float | None:
        va, vb = self.cumul_a(a), self.cumul_a(b)
        if va is None or vb is None:
            return None
        return max(0.0, vb - va)

    def valeurs_entre(self, nom: str, a: datetime, b: datetime) -> list[float]:
        col = self.c.get(nom)
        if not col:
            return []
        return [v for t, v in zip(self.t, col) if a < t <= b and isinstance(v, (int, float))]

    def a_l_heure(self, nom: str, t: datetime):
        col = self.c.get(nom)
        if not col:
            return None
        i = bisect_left(self.t, t)
        if i < len(self.t) and self.t[i] == t:
            return col[i]
        return None


def lire_departement(repo: str, statistique: str | None = None) -> tuple[dict[str, Serie], str]:
    """Séries par commune (INSEE) et heure du run, pour le format « points + communes »."""
    d = get(f"{RAW}{repo}/data/departements/66.json")
    run = ""
    try:
        run = get(f"{RAW}{repo}/data/index.json").get("model", {}).get("run_time", "")
    except Exception:  # noqa: BLE001
        pass
    cols = d["columns"]
    noms = cols["values"] if isinstance(cols, dict) else cols
    pos = {n: noms.index(n) for n in noms}
    forecast = d["forecast"] if statistique is None else d["forecast_statistics"][statistique]
    temps = [parse_t(f[0]) for f in forecast]
    out = {}
    for c in d["communes"]:
        code, pid = c[0], c[-1]

        def col(nom):
            if nom not in pos:
                return None
            return [f[1][pid][pos[nom]] if pid < len(f[1]) else None for f in forecast]

        out[code] = Serie(temps, {
            "total": col("precipitation_total_mm"),
            "pluie": col("precipitation_mm"),
            "temp": col("temperature_c"),
            "rafale": col("wind_gust_kmh"),
            "cape": col("cape_jkg"),
            "neige": col("snow_fresh_cm"),
        })
    return out, run


def lire_icon_eu() -> tuple[dict[str, Serie], str]:
    d = get(f"{RAW}ICON-EU-7-km/data/departements/66.json")
    noms = d["columns"]
    pos = {n: noms.index(n) for n in noms}
    temps = [parse_t(t) for t in d["time"]]
    limite = d.get("interpolated_after_hour") or len(temps)
    temps = temps[:limite]
    out = {}
    for code, c in d["communes"].items():
        vals = c["values"][:limite]
        out[code] = Serie(temps, {
            "pluie": [v[pos["precipitation"]] for v in vals],
            "temp": [v[pos["temperature_2m"]] for v in vals],
            "rafale": [v[pos["wind_gusts_10m"]] for v in vals],
        })
    return out, d.get("model_run", "")


def bornes_jours(maintenant: datetime) -> list[tuple[datetime, datetime, str]]:
    j0 = maintenant.astimezone(PARIS).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for k in range(JOURS):
        a = (j0 + timedelta(days=k)).astimezone(timezone.utc)
        b = (j0 + timedelta(days=k + 1)).astimezone(timezone.utc)
        out.append((a, b, (j0 + timedelta(days=k)).date().isoformat()))
    return out


def quotidien(s: Serie, jours, maintenant: datetime) -> dict[str, list]:
    pluie, tmax, tmin, rafale = [], [], [], []
    for k, (a, b, _) in enumerate(jours):
        debut = max(a, maintenant) if k == 0 else a
        complet = s.t and s.t[0] <= debut and s.t[-1] >= b
        p = s.pluie_entre(debut, b) if complet else None
        pluie.append(None if p is None else round(p, 1))
        temps = s.valeurs_entre("temp", a, b) if complet else []
        tmax.append(round(max(temps), 1) if len(temps) >= 3 else None)
        tmin.append(round(min(temps), 1) if len(temps) >= 3 else None)
        raf = s.valeurs_entre("rafale", a, b) if complet else []
        rafale.append(round(max(raf)) if raf else None)
    return {"pluie": pluie, "tmax": tmax, "tmin": tmin, "rafale": rafale}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="build/communes")
    a = p.parse_args()
    maintenant = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    jours = bornes_jours(maintenant)

    modeles: dict[str, dict[str, Serie]] = {}
    infos: dict[str, dict] = {}

    def charger(m):
        mid, repo, nom = m
        try:
            series, run = lire_icon_eu() if mid == "iconeu" else lire_departement(repo)
            return mid, nom, series, run, None
        except Exception as e:  # noqa: BLE001
            return mid, nom, None, "", str(e)

    with ThreadPoolExecutor(max_workers=6) as pool:
        for mid, nom, series, run, err in pool.map(charger, DETERMINISTES):
            if series is None:
                print(f"  {mid} indisponible : {err}", flush=True)
                continue
            modeles[mid] = series
            infos[mid] = {"nom": nom, "run": run, "type": "deterministe"}

    ensembles: dict[str, dict[str, dict[str, Serie]]] = {}
    for mid, repo, nom in ENSEMBLES:
        try:
            moy, run = lire_departement(repo)
            p10, _ = lire_departement(repo, "p10")
            p90, _ = lire_departement(repo, "p90")
            ensembles[mid] = {"moyenne": moy, "p10": p10, "p90": p90}
            infos[mid] = {"nom": nom, "run": run, "type": "ensemble"}
        except Exception as e:  # noqa: BLE001
            print(f"  {mid} indisponible : {e}", flush=True)

    if len(modeles) < 4:
        print(f"Seulement {len(modeles)} modèles lisibles : pas de publication", file=sys.stderr)
        return 1

    heures = [maintenant + timedelta(hours=h) for h in range(1, HEURES + 1)]
    codes = sorted(next(iter(modeles.values())).keys())
    out_dir = Path(a.out_dir) / "communes"
    out_dir.mkdir(parents=True, exist_ok=True)
    genere = maintenant.strftime("%Y-%m-%dT%H:%M:%SZ")

    for code in codes:
        q = {mid: quotidien(series[code], jours, maintenant) for mid, series in modeles.items() if code in series}
        ens = {}
        for mid, e in ensembles.items():
            if code not in e["moyenne"]:
                continue
            ens[mid] = {k: quotidien(e[k][code], jours, maintenant) for k in ("moyenne", "p10", "p90")}

        # horaire : meilleur modèle à chaque heure
        h = {"pluie": [], "rafale": [], "cape": [], "neige": [], "temp": [], "source": []}
        for t in heures:
            choisi = None
            for mid in PRIORITE_HORAIRE:
                s = modeles.get(mid, {}).get(code)
                if s and s.t and s.t[0] <= t - timedelta(hours=1) and s.t[-1] >= t and s.a_l_heure("temp", t) is not None:
                    choisi = (mid, s)
                    break
            if not choisi:
                for k in h:
                    h[k].append(None)
                continue
            mid, s = choisi
            pl = s.pluie_entre(t - timedelta(hours=1), t)
            h["pluie"].append(None if pl is None else round(pl, 1))
            raf = s.a_l_heure("rafale", t)
            h["rafale"].append(None if raf is None else round(raf))
            cape = s.a_l_heure("cape", t)
            h["cape"].append(None if cape is None else round(cape))
            ne = s.a_l_heure("neige", t)
            h["neige"].append(None if ne is None else round(ne, 1))
            h["temp"].append(round(s.a_l_heure("temp", t), 1))
            h["source"].append(mid)

        pluie_modeles = {}
        for mid in MODELES_FINS:
            s = modeles.get(mid, {}).get(code)
            if not s:
                continue
            vals = [s.pluie_entre(t - timedelta(hours=1), t) for t in heures]
            if any(v is not None for v in vals):
                pluie_modeles[mid] = [None if v is None else round(v, 1) for v in vals]

        payload = {
            "schema_version": 1,
            "code": code,
            "generated_at": genere,
            "jours": [j[2] for j in jours],
            "quotidien": q,
            "ensembles": ens,
            "heures": [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in heures],
            "horaire": h,
            "pluie_horaire_modeles": pluie_modeles,
        }
        (out_dir / f"{code}.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    (Path(a.out_dir) / "index.json").write_text(json.dumps({
        "schema_version": 1,
        "generated_at": genere,
        "communes": len(codes),
        "modeles": infos,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(codes)} communes ; modèles : {', '.join(sorted(infos))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

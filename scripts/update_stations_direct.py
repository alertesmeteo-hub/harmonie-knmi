#!/usr/bin/env python3
"""Stations en direct : Pyrénées-Orientales (Météo-France) et Catalogne nord (Meteocat, réseau XEMA).

Meteocat publie ses stations automatiques en données ouvertes (analisi.transparenciacatalunya.cat,
jeu nzvn-apee, pas de 30 min, ~20 min de retard, sans clé). On garde les stations en service à moins
de RAYON_KM d'une commune du 66 : les orages et les pluies qui arrivent par le sud y passent 30 à 60 min
avant d'atteindre le département. Les stations du 66 viennent des fichiers déjà publiés sur la branche
observations (température, rafales, pluie horaire Météo-France).

Publie stations-66.json : dernière température, pluie sur 30 min à 72 h, rafale maximale de la dernière
heure, et pour Meteocat les 12 dernières heures au pas de 30 min.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SOCRATA = "https://analisi.transparenciacatalunya.cat/resource/"
STATIONS_XEMA = SOCRATA + "yqwd-vj5e.json"
DONNEES_XEMA = SOCRATA + "nzvn-apee.json"
OBS = "https://raw.githubusercontent.com/alertesmeteo-hub/harmonie-knmi/observations/"
CATALOGUE = Path(__file__).resolve().parent.parent / "config" / "communes-66-mairies.json"
RAYON_KM = 45
VARIABLES = {"32": "t", "35": "ppt", "30": "vent", "31": "dir", "50": "rafale10", "53": "rafale6", "56": "rafale2"}
FENETRES_H = [0.5, 1, 3, 6, 24, 48, 72]
S = requests.Session()
S.headers["User-Agent"] = "alertes-meteo-stations-direct/1.0 (+https://app.alertes-meteo.com)"


def get(url: str, params: dict | None = None, timeout: int = 120):
    for essai in range(4):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            print(f"HTTP {r.status_code} {url} {r.text[:200]}", flush=True)
        except requests.RequestException as e:
            print(f"{url} : {e}", flush=True)
        time.sleep(5 * (essai + 1))
    raise RuntimeError(f"source injoignable : {url}")


def dist(a: float, b: float, c: float, d: float) -> float:
    r = math.pi / 180
    h = math.sin((c - a) * r / 2) ** 2 + math.cos(a * r) * math.cos(c * r) * math.sin((d - b) * r / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def meteocat(communes: list, maintenant: datetime) -> tuple[list[dict], str | None]:
    meta = get(STATIONS_XEMA, {"$limit": 2000})
    proches = {}
    for s in meta:
        if s.get("codi_estat_ema") != "2":
            continue
        la, lo = float(s["latitud"]), float(s["longitud"])
        dm, plus_proche = min((dist(la, lo, c[2], c[3]), c[1]) for c in communes)
        if dm <= RAYON_KM:
            proches[s["codi_estacio"]] = {"id": s["codi_estacio"], "nom": s["nom_estacio"], "lat": la, "lon": lo,
                                          "alt": float(s.get("altitud") or 0), "comarca": s.get("nom_comarca", ""),
                                          "km_66": round(dm), "commune_proche": plus_proche}
    debut = (maintenant - timedelta(hours=73)).strftime("%Y-%m-%dT%H:%M:%S")
    codes = ",".join(f"'{c}'" for c in proches)
    vars_ = ",".join(f"'{v}'" for v in VARIABLES)
    rows = get(DONNEES_XEMA, {
        "$where": f"codi_estacio in({codes}) AND codi_variable in({vars_}) AND data_lectura >= '{debut}'",
        "$select": "codi_estacio,codi_variable,data_lectura,valor_lectura",
        "$limit": 100000,
    })
    series: dict[str, dict[str, dict[datetime, float]]] = {}
    derniere = None
    for r in rows:
        try:
            v = float(r["valor_lectura"])
        except (TypeError, ValueError):
            continue
        # la lecture de hh:00 couvre hh:00-hh:30 (UTC) : on la date à la fin de la période
        t = datetime.fromisoformat(r["data_lectura"]).replace(tzinfo=timezone.utc) + timedelta(minutes=30)
        series.setdefault(r["codi_estacio"], {}).setdefault(VARIABLES[r["codi_variable"]], {})[t] = v
        if derniere is None or t > derniere:
            derniere = t
    out = []
    for code, s in proches.items():
        d = series.get(code)
        if not d:
            continue
        fin = max(max(x) for x in d.values())
        if fin < maintenant - timedelta(hours=6):
            continue  # station muette
        ppt = d.get("ppt", {})
        pluie, couverture = {}, {}
        for h in FENETRES_H:
            ts = [t for t in ppt if t > fin - timedelta(hours=h)]
            pluie[str(h)] = round(sum(ppt[t] for t in ts), 1) if ppt else None
            couverture[str(h)] = round(len(ts) / (h * 2), 2) if ppt else 0
        raf = d.get("rafale10") or d.get("rafale6") or d.get("rafale2") or {}
        raf_1h = [raf[t] for t in raf if t > fin - timedelta(hours=1)]
        t_last = max(d["t"]) if d.get("t") else None
        pas = [fin - timedelta(minutes=30 * k) for k in range(23, -1, -1)]
        out.append({
            **s, "source": "meteocat",
            "a": iso(fin),
            "t": d["t"][t_last] if t_last else None,
            "pluie": pluie,
            "couverture": couverture,
            "rafale_1h": round(max(raf_1h) * 3.6) if raf_1h else None,
            "vent": round(d["vent"][max(d["vent"])] * 3.6) if d.get("vent") else None,
            "dir": d["dir"][max(d["dir"])] if d.get("dir") else None,
            "serie": {
                "pas_min": 30,
                "pluie": [ppt.get(t) for t in pas],
                "rafale": [round(raf[t] * 3.6) if t in raf else None for t in pas],
            },
        })
    return out, iso(derniere) if derniere else None


def meteo_france() -> tuple[list[dict], str | None]:
    pluie = get(OBS + "observations_pluie.json")
    temp = get(OBS + "observations_temperature.json")
    raf = get(OBS + "observations_rafales.json")
    st: dict[str, dict] = {}
    for s in pluie["stations"]:
        if s.get("department") == "66":
            st[s["id"]] = {"id": s["id"], "nom": s["name"].title(), "lat": s["lat"], "lon": s["lon"], "source": "meteofrance",
                           "pluie": {"1": s.get("rr1"), "24": s.get("rr24"), "48": s.get("rr48"), "72": s.get("rr72")},
                           "pluie_a": s.get("date")}
    for s in temp["stations"]:
        if str(s.get("id", "")).startswith("66") and s.get("temperature") is not None:
            e = st.setdefault(s["id"], {"id": s["id"], "nom": s["name"].title(), "lat": s["lat"], "lon": s["lon"], "source": "meteofrance", "pluie": {}})
            e["t"], e["a"] = s["temperature"], s.get("date")
    for s in raf["stations"]:
        if s.get("department_code") == "66":
            e = st.setdefault(s["id"], {"id": s["id"], "nom": s["name"].title(), "lat": s["lat"], "lon": s["lon"], "source": "meteofrance", "pluie": {}})
            e["rafale_1h"] = round(s["latest_gust_kmh"]) if s.get("latest_gust_kmh") is not None else None
            e["rafale_a"] = s.get("latest_gust_time")
            e["vent"] = round(s["latest_mean_wind_kmh"]) if s.get("latest_mean_wind_kmh") is not None else None
            e["dir"] = s.get("latest_direction_deg")
            e["alt"] = s.get("altitude_m")
    latest = max((x.get("a") or x.get("pluie_a") or "" for x in st.values()), default=None)
    return list(st.values()), latest


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="build/direct")
    a = p.parse_args()
    maintenant = datetime.now(timezone.utc)
    communes = json.loads(CATALOGUE.read_text(encoding="utf-8"))["communes"]
    cat, cat_at = meteocat(communes, maintenant)
    try:
        mf, mf_at = meteo_france()
    except RuntimeError as e:
        print(f"Météo-France indisponible : {e}", flush=True)
        mf, mf_at = [], None
    if len(cat) < 10:
        print(f"Seulement {len(cat)} stations Meteocat : pas de publication", file=sys.stderr)
        return 1
    od = Path(a.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    (od / "stations-66.json").write_text(json.dumps({
        "schema_version": 1,
        "generated_at": iso(maintenant),
        "meteocat_a": cat_at,
        "meteofrance_a": mf_at,
        "sources": {
            "meteocat": "Servei Meteorològic de Catalunya, réseau XEMA (données ouvertes, Generalitat de Catalunya)",
            "meteofrance": "Météo-France (observations horaires)",
        },
        "stations": cat + mf,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    fortes = sorted(((x["pluie"].get("1") or 0, x["nom"]) for x in cat), reverse=True)[:3]
    print(f"{len(cat)} stations Meteocat (dernière mesure {cat_at}), {len(mf)} Météo-France ; pluie 1 h maxi : {fortes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Hauteurs et débits des rivières des Pyrénées-Orientales (Hub'Eau, temps réel) sur 72 h.

Pour chaque station en service du département : dernière mesure, tendance sur 1 h et 6 h, maximum
sur 72 h, série horaire, et crues historiques de référence (fiche station Vigicrues, rafraîchies une
fois par jour). Publie hydro/66.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HUBEAU = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/"
VIGICRUES = "https://www.vigicrues.gouv.fr/services/station.json/index.php?CdStationHydro="
UA = {"User-Agent": "Mozilla/5.0 (compatible; AlertesMeteo-hydro/1.0; +https://app.alertes-meteo.com)"}
S = requests.Session()
S.headers.update(UA)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url: str, params: dict | None = None, timeout: int = 60) -> dict:
    for essai in range(4):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code in (200, 206):
                return r.json()
            print(f"HTTP {r.status_code} {url} {r.text[:150]}", flush=True)
        except requests.RequestException as e:
            print(f"{url} : {e}", flush=True)
        time.sleep(5 * (essai + 1))
    raise RuntimeError(f"Hub'Eau injoignable : {url}")


def stations() -> list[dict]:
    d = get(HUBEAU + "referentiel/stations", {"code_departement": "66", "en_service": "true", "format": "json", "size": 500})
    out = []
    for s in d["data"]:
        if s.get("type_station") not in ("STD", "DEB", "LIMNI", "LIMIFIC"):
            continue
        lat, lon = s.get("latitude_station"), s.get("longitude_station")
        if lat is None or lon is None:
            continue
        if lat < 10 < lon:  # quelques fiches ont latitude et longitude inversées
            lat, lon = lon, lat
        nom = s["libelle_station"]
        out.append({
            "code": s["code_station"],
            "nom": nom,
            "riviere": s.get("libelle_cours_eau") or "",
            "commune": s.get("code_commune_station") or "",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
        })
    return out


def observations(codes: list[str], grandeur: str, debut: datetime) -> dict[str, list[tuple[datetime, float]]]:
    series: dict[str, list[tuple[datetime, float]]] = {}
    for i in range(0, len(codes), 15):
        lot = codes[i:i + 15]
        d = get(HUBEAU + "observations_tr", {
            "code_entite": ",".join(lot), "grandeur_hydro": grandeur, "date_debut_obs": iso(debut),
            "size": 20000, "fields": "code_station,date_obs,resultat_obs",
        }, timeout=90)
        for o in d.get("data", []):
            if o.get("resultat_obs") is None:
                continue
            t = datetime.fromisoformat(o["date_obs"].replace("Z", "+00:00"))
            series.setdefault(o["code_station"], []).append((t, float(o["resultat_obs"])))
        time.sleep(0.5)
    for v in series.values():
        v.sort()
    return series


def resume(serie: list[tuple[datetime, float]], facteur: float, heures: list[datetime], dec: int) -> dict | None:
    """facteur : mm -> m (H) ou l/s -> m3/s (Q)."""
    if not serie:
        return None
    last_t, last_v = serie[-1]

    def valeur_a(t: datetime, tol=timedelta(minutes=40)) -> float | None:
        best = min(serie, key=lambda x: abs(x[0] - t))
        return best[1] if abs(best[0] - t) <= tol else None

    def tendance(h: int) -> float | None:
        v = valeur_a(last_t - timedelta(hours=h))
        return None if v is None else round((last_v - v) * facteur, dec)

    vmax = max(serie, key=lambda x: x[1])
    return {
        "derniere": round(last_v * facteur, dec),
        "a": iso(last_t),
        "tendance_1h": tendance(1),
        "tendance_6h": tendance(6),
        "max_72h": round(vmax[1] * facteur, dec),
        "max_72h_a": iso(vmax[0]),
        "horaire": [None if (v := valeur_a(h, timedelta(minutes=35))) is None else round(v * facteur, dec) for h in heures],
    }


def crues_historiques(code: str) -> list[dict]:
    try:
        r = S.get(VIGICRUES + code, timeout=30)
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            return []
        d = r.json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for c in (d.get("VigilanceCrues") or {}).get("CruesHistoriques") or []:
        h, q = c.get("ValHauteur"), c.get("ValDebit")
        out.append({"nom": c.get("LbUsuel", ""), "h": h if h else None, "q": q if q else None})
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="build/hydro")
    p.add_argument("--previous-url", default="")
    a = p.parse_args()
    now = datetime.now(timezone.utc)
    debut = now - timedelta(hours=73)
    st = stations()
    codes = [s["code"] for s in st]
    print(f"{len(st)} stations en service", flush=True)
    h = observations(codes, "H", debut)
    q = observations(codes, "Q", debut)
    heures = [now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=k) for k in range(71, -1, -1)]

    prev = {}
    if a.previous_url:
        try:
            old = S.get(a.previous_url, timeout=30).json()
            if datetime.fromisoformat(old["historiques_maj"].replace("Z", "+00:00")) > now - timedelta(hours=24):
                prev = {s["code"]: s.get("crues_historiques", []) for s in old["stations"]}
        except Exception:  # noqa: BLE001
            prev = {}
    if prev:
        hist = prev
        hist_maj = old["historiques_maj"]
    else:
        with ThreadPoolExecutor(max_workers=6) as pool:
            hist = dict(zip(codes, pool.map(crues_historiques, codes)))
        hist_maj = iso(now)

    out = []
    for s in st:
        rh = resume(h.get(s["code"], []), 0.001, heures, 2)
        rq = resume(q.get(s["code"], []), 0.001, heures, 2)
        if not rh and not rq:
            continue
        out.append({**s, "h": rh, "q": rq, "crues_historiques": hist.get(s["code"], [])})
    if len(out) < 10:
        print(f"Seulement {len(out)} stations avec des mesures : pas de publication", file=sys.stderr)
        return 1
    od = Path(a.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    (od / "66.json").write_text(json.dumps({
        "schema_version": 1,
        "generated_at": iso(now),
        "historiques_maj": hist_maj,
        "source": "Hub'Eau hydrométrie temps réel (Vigicrues / SCHAPI) ; crues historiques : fiches station Vigicrues",
        "unites": {"h": "m", "q": "m3/s"},
        "heures": [iso(x) for x in heures],
        "stations": out,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    hausse = sorted((x for x in out if x["h"] and x["h"]["tendance_1h"]), key=lambda x: -x["h"]["tendance_1h"])[:3]
    print(f"{len(out)} stations publiées ; plus fortes hausses 1 h : {[(x['nom'], x['h']['tendance_1h']) for x in hausse]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

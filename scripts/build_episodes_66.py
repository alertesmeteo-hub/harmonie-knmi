#!/usr/bin/env python3
"""Épisodes météo marquants des Pyrénées-Orientales, avec leur bilan, depuis octobre 2001.

Un épisode = des jours consécutifs en vigilance orange ou rouge (historique des vigilances, dépôt
vigilance-meteo) ou avec au moins SEUIL_PLUIE_MM mesurés à un pluviomètre du département (données
quotidiennes Météo-France du dépôt climato). Pour chaque épisode :
  - la vigilance jour par jour ;
  - la pluie mesurée à chaque pluviomètre (cumul de l'épisode), les extrêmes de température ;
  - le maximum atteint par chaque rivière (Hub'Eau, hauteur maximale journalière) comparé aux crues
    historiques de la station ;
  - quand l'épisode est couvert par les archives (depuis fin septembre 2026) : la pluie radar par
    commune et le nombre d'impacts de foudre.
Publie index.json et episodes/<début>.json. Les épisodes terminés depuis plus de 5 jours ne sont
pas recalculés : on reprend la version déjà publiée.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
from PIL import Image

RAW = "https://raw.githubusercontent.com/alertesmeteo-hub/"
VIGILANCE = RAW + "vigilance-meteo/historique-data/departements/66.json"
CLIMATO_STATIONS = RAW + "climato/data/stations.json.gz"
HYDRO = RAW + "harmonie-knmi/hydro-data/66.json"
RADAR = RAW + "harmonie-knmi/radar-archive/pluie/"
FOUDRE = RAW + "harmonie-knmi/foudre-archives/archives/"
PUBLIE = RAW + "harmonie-knmi/episodes-data/"
HUBEAU_ELAB = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/obs_elab"
CATALOGUE = Path(__file__).resolve().parent.parent / "config" / "communes-66-mairies.json"

SEUIL_PLUIE_MM = 80.0
FIGE_APRES_JOURS = 5
BBOX_66 = (42.30, 1.70, 42.95, 3.20)  # lat min, lon min, lat max, lon max
# radar archivé : recadrage du 66 dans la mosaïque EPSG:4326 1200 x 800 sur [[41, -6], [52, 10.5]]
MOSAIQUE = (41.0, -6.0, 52.0, 10.5, 1200, 800)
CROP_X0, CROP_Y0 = 538, 636
CLASSES = [((108, 207, 255), 0.12), ((41, 143, 255), 0.35), ((33, 210, 92), 0.75), ((149, 224, 45), 1.5), ((255, 220, 39), 3.5),
           ((255, 137, 26), 7.5), ((241, 53, 53), 15.0), ((213, 24, 174), 30.0), ((126, 52, 210), 50.0)]
NOMS = {"2009-01-24": "Tempête Klaus", "2020-01-20": "Tempête Gloria", "2020-01-21": "Tempête Gloria", "2020-01-22": "Tempête Gloria", "2026-02-12": "Tempête Nils"}
S = requests.Session()
S.headers["User-Agent"] = "alertes-meteo-episodes/1.0"


def get_json(url: str, timeout: int = 60):
    for essai in range(3):
        try:
            r = S.get(url, timeout=timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"  {url} : {e}", flush=True)
            time.sleep(3 * (essai + 1))
    return None


def jours_entre(a: str, b: str) -> list[str]:
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    return [(d0 + timedelta(days=k)).isoformat() for k in range((d1 - d0).days + 1)]


def lire_climato(dossier: Path) -> tuple[dict, dict[str, dict[str, tuple]]]:
    """Métadonnées des postes du 66 et données quotidiennes {poste: {date: (rr, tx, tn)}}."""
    meta = json.loads(gzip.decompress(S.get(CLIMATO_STATIONS, timeout=120).content))
    postes = {s["num_poste"]: s for s in meta["stations"] if str(s["num_poste"]).startswith("66")}
    donnees: dict[str, dict[str, tuple]] = {}
    for poste in postes:
        d = dossier / "stations" / poste
        if not d.is_dir():
            continue
        jours = {}
        for f in d.glob("*.json.gz"):
            try:
                an = json.loads(gzip.decompress(f.read_bytes()))
            except (OSError, ValueError):
                continue
            if an.get("year", 0) < 2001:
                continue
            for j in an.get("days", []):
                jours[j["date"]] = (j.get("rr"), j.get("tx"), j.get("tn"))
        if jours:
            donnees[poste] = jours
    print(f"{len(donnees)} postes du 66 avec données quotidiennes depuis 2001", flush=True)
    return postes, donnees


def detecter(vigi: dict, climato: dict[str, dict[str, tuple]]) -> list[tuple[str, str]]:
    jours = {j[0] for j in vigi["jours"] if j[1] >= 3}
    pluie_max: dict[str, float] = defaultdict(float)
    for jours_poste in climato.values():
        for d, (rr, _tx, _tn) in jours_poste.items():
            if isinstance(rr, (int, float)) and rr > pluie_max[d]:
                pluie_max[d] = rr
    jours |= {d for d, v in pluie_max.items() if v >= SEUIL_PLUIE_MM}
    eps: list[list[str]] = []
    for d in sorted(jours):
        if eps and (date.fromisoformat(d) - date.fromisoformat(eps[-1][1])).days <= 1:
            eps[-1][1] = d
        else:
            eps.append([d, d])
    return [(a, b) for a, b in eps]


def titre(debut: str, phen: list[str], couleur: int) -> str:
    if debut in NOMS:
        return NOMS[debut]
    p = set(phen)
    if p & {"Pluie-inondation", "Crues"}:
        return "Pluies intenses et crues" if "Crues" in p else "Pluies intenses"
    if "Orages" in p:
        return "Orages violents" if couleur == 4 else "Orages"
    if "Vent violent" in p:
        return "Tempête" if couleur == 4 else "Coup de vent"
    for nom, t in (("Neige-verglas", "Neige et verglas"), ("Canicule", "Canicule"), ("Grand froid", "Grand froid"),
                   ("Avalanches", "Risque d'avalanches"), ("Vagues-submersion", "Vagues-submersion")):
        if nom in p:
            return t
    return "Fortes pluies" if couleur < 3 else "Épisode en vigilance orange"


def radar_communes(debut: str, fin: str, jours_dispo: set[str]) -> dict | None:
    jours = jours_entre(debut, fin)
    if not all(j in jours_dispo for j in jours):
        return None
    communes = json.loads(CATALOGUE.read_text(encoding="utf-8"))["communes"]
    la0, lo0, la1, lo1, w, h = MOSAIQUE
    px = [(int((lo - lo0) / (lo1 - lo0) * w) - CROP_X0, int((la1 - la) / (la1 - la0) * h) - CROP_Y0) for _c, _n, la, lo in communes]
    pal = np.array([c for c, _ in CLASSES], dtype=np.float32)
    taux = np.array([t for _, t in CLASSES], dtype=np.float32)
    total = np.zeros(len(communes), dtype=np.float64)
    images = 0
    for j in jours:
        idx = get_json(f"{RADAR}{j.replace('-', '/')}/index.json")
        if not idx:
            return None
        for cle, pluie in idx.get("frames", {}).items():
            if not pluie:
                images += 1
                continue
            r = S.get(f"{RADAR}{j.replace('-', '/')}/{cle}.png", timeout=60)
            if r.status_code != 200:
                continue
            a = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGBA"), dtype=np.float32)
            hh, ww = a.shape[:2]
            vals = []
            for x, y in px:
                if not (1 <= x < ww - 1 and 1 <= y < hh - 1):
                    vals.append(0.0)
                    continue
                bloc = a[y - 1:y + 2, x - 1:x + 2].reshape(-1, 4)
                mm = 0.0
                for p in bloc:
                    if p[3] < 20:
                        continue
                    k = int(np.argmin(((pal - p[:3]) ** 2).sum(axis=1)))
                    mm += taux[k] * 0.25  # image toutes les 15 min
                vals.append(mm / 9)
            total += np.array(vals)
            images += 1
    if images < len(jours) * 60:
        return None
    return {c[0]: round(float(v), 1) for c, v in zip(communes, total)}


def foudre(debut: str, fin: str) -> dict | None:
    par_jour = {}
    for j in jours_entre(debut, fin):
        d = get_json(f"{FOUDRE}{j}.json")
        if d is None:
            return None if not par_jour else par_jour | {"_incomplet": True}
        n = 0
        for hr in d.get("hours", []):
            for la, lo, c in hr.get("cells", []):
                if BBOX_66[0] <= la <= BBOX_66[2] and BBOX_66[1] <= lo <= BBOX_66[3]:
                    n += c
        par_jour[j] = n
    return par_jour


MEDIANES: dict[str, float] = {}
RECORDS: dict[str, dict] = {}  # plus haut maximum journalier depuis 2001 : {"h": m, "date": iso}


def _hix_station(code: str, depuis: str) -> tuple[str, dict[str, float]]:
    jours: dict[str, float] = {}
    url, params = HUBEAU_ELAB, {"code_entite": code, "grandeur_hydro_elab": "HIXnJ", "date_debut_obs_elab": depuis,
                                 "size": 20000, "fields": "date_obs_elab,resultat_obs_elab"}
    while url:
        d = None
        for essai in range(3):
            try:
                r = S.get(url, params=params, timeout=120)
                if r.status_code in (200, 206):
                    d = r.json()
                    break
            except requests.RequestException:
                pass
            time.sleep(3 * (essai + 1))
        if not d:
            break
        for o in d.get("data", []):
            if o.get("resultat_obs_elab") is not None:
                jours[o["date_obs_elab"][:10]] = o["resultat_obs_elab"] / 1000.0
        url, params = d.get("next"), None
    return code, jours


def charger_hix(hydro: dict | None, depuis: str = "2001-10-01", medianes: dict[str, float] | None = None) -> dict[str, dict[str, float]]:
    """Hauteur instantanée maximale journalière (m) de chaque station depuis `depuis` (Hub'Eau, ~10 s par station)."""
    if not hydro:
        return {}
    from concurrent.futures import ThreadPoolExecutor

    out: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for code, jours in pool.map(lambda s: _hix_station(s["code"], depuis), hydro["stations"]):
            if jours:
                out[code] = jours
    for code, jours in out.items():
        d, h = max(jours.items(), key=lambda x: x[1])
        if code not in RECORDS or h > RECORDS[code]["h"]:
            RECORDS[code] = {"h": round(h, 2), "date": d}
    if medianes:
        MEDIANES.update(medianes)
    else:
        # niveau habituel de la station (médiane des maxima journaliers) : certaines échelles ne partent pas de 0
        for code, jours in out.items():
            vals = sorted(jours.values())
            MEDIANES[code] = vals[len(vals) // 2]
    print(f"Hauteurs maximales journalières depuis le {depuis} : {len(out)} stations", flush=True)
    return out


def rivieres(debut: str, fin: str, hydro: dict | None, hix: dict[str, dict[str, float]]) -> list[dict]:
    if not hydro:
        return []
    stations = {s["code"]: s for s in hydro["stations"]}
    fin2 = (date.fromisoformat(fin) + timedelta(days=2)).isoformat()
    jours = jours_entre(debut, fin2)
    out = []
    for code, serie in hix.items():
        vals = [(serie[j], j) for j in jours if j in serie]
        if not vals:
            continue
        m, quand = max(vals)
        base = MEDIANES.get(code, 0.0)
        s = stations[code]
        ref = max((c for c in s.get("crues_historiques", []) if c.get("h")), key=lambda c: c["h"], default=None)
        # part de la crue de référence atteinte, mesurée au-dessus du niveau habituel
        part = round(max(0.0, m - base) / (ref["h"] - base), 2) if ref and ref["h"] > base + 0.2 else None
        out.append({"code": code, "nom": s["nom"], "riviere": s["riviere"], "h_max": round(m, 2), "le": quand,
                    "montee": round(m - base, 2), "niveau_habituel": round(base, 2),
                    "ref": {"nom": ref["nom"], "h": ref["h"]} if ref else None,
                    "part_ref": part})
    return sorted(out, key=lambda x: (-(x["part_ref"] or 0), -x["montee"]))


def calculer(debut: str, fin: str, vigi: dict, postes: dict, climato: dict, hydro, hix, radar_jours: set[str]) -> dict:
    vj = {j[0]: j for j in vigi["jours"]}
    contexte = jours_entre((date.fromisoformat(debut) - timedelta(days=1)).isoformat(), (date.fromisoformat(fin) + timedelta(days=1)).isoformat())
    vigilance = [{"date": d, "couleur": vj[d][1] if d in vj else 1, "phenomenes": [[n, c] for n, c in vj[d][2]] if d in vj else []} for d in contexte]
    couleur = max((v["couleur"] for v in vigilance if debut <= v["date"] <= fin), default=1)
    phen = []
    for v in vigilance:
        if debut <= v["date"] <= fin:
            for n, c in v["phenomenes"]:
                if (c == 0 or c >= 3 or v["couleur"] >= 3) and n not in phen:
                    phen.append(n)

    jours = jours_entre(debut, fin)
    pluvios = []
    tx_max = tn_min = None
    for poste, js in climato.items():
        rr = [js[d][0] for d in jours if d in js and isinstance(js[d][0], (int, float))]
        if rr:
            m = postes[poste]
            pluvios.append({"poste": poste, "nom": m["nom"], "lat": m["lat"], "lon": m["lon"], "alt": m.get("alti"),
                            "total": round(sum(rr), 1), "jours": len(rr), "max_jour": round(max(rr), 1)})
        for d in jours:
            if d in js:
                _rr, tx, tn = js[d]
                if isinstance(tx, (int, float)) and (tx_max is None or tx > tx_max["v"]):
                    tx_max = {"v": tx, "nom": postes[poste]["nom"], "date": d}
                if isinstance(tn, (int, float)) and (tn_min is None or tn < tn_min["v"]):
                    tn_min = {"v": tn, "nom": postes[poste]["nom"], "date": d}
    pluvios.sort(key=lambda p: -p["total"])
    totaux = [p["total"] for p in pluvios]

    riv = rivieres(debut, fin, hydro, hix)
    rad = radar_communes(debut, fin, radar_jours)
    fo = foudre(debut, fin) if fin >= "2026-10-01" else None  # archives foudre depuis octobre 2026
    return {
        "id": debut,
        "debut": debut,
        "fin": fin,
        "jours": len(jours),
        "titre": titre(debut, phen, couleur),
        "couleur": couleur,
        "phenomenes": phen,
        "vigilance": vigilance,
        "pluie": {
            "postes": len(pluvios),
            "max": pluvios[0] if pluvios else None,
            "moyenne": round(sum(totaux) / len(totaux), 1) if totaux else None,
            "n50": sum(1 for t in totaux if t >= 50),
            "n100": sum(1 for t in totaux if t >= 100),
            "n200": sum(1 for t in totaux if t >= 200),
            "pluviometres": pluvios,
        },
        "temperatures": {"tx_max": tx_max, "tn_min": tn_min},
        "rivieres": riv,
        "radar_communes": rad,
        "foudre": fo,
        "calcule_le": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def resume(e: dict) -> dict:
    crue = next((r for r in e["rivieres"] if (r.get("part_ref") or 0) >= 0.2), None)
    pm = e["pluie"]["max"]
    rad = e.get("radar_communes") or {}
    return {
        "id": e["id"], "debut": e["debut"], "fin": e["fin"], "jours": e["jours"], "titre": e["titre"], "couleur": e["couleur"],
        "phenomenes": e["phenomenes"],
        "pluie_max": {"nom": pm["nom"], "mm": pm["total"]} if pm else None,
        "n100": e["pluie"]["n100"],
        "crue": {"riviere": crue["riviere"], "nom": crue["nom"], "h": crue["h_max"], "part_ref": crue["part_ref"]} if crue else None,
        "foudre": sum(v for k, v in (e["foudre"] or {}).items() if not k.startswith("_")) if e.get("foudre") else None,
        "radar": bool(rad),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--climato", required=True, help="clone partiel de la branche data du dépôt climato (stations/66*)")
    p.add_argument("--out-dir", default="build/episodes")
    p.add_argument("--tout", action="store_true", help="recalculer tous les épisodes")
    a = p.parse_args()

    vigi = get_json(VIGILANCE)
    if not vigi:
        print("Historique des vigilances indisponible", file=sys.stderr)
        return 1
    postes, climato = lire_climato(Path(a.climato))
    hydro = get_json(HYDRO)
    radar_jours = {d["date"] for d in (get_json(RADAR + "days.json") or {}).get("days", [])}
    ancien_index = {} if a.tout else (get_json(PUBLIE + "index.json") or {})
    ancien = {e["id"]: e for e in ancien_index.get("episodes", [])}
    anciennes_medianes = ancien_index.get("medianes_rivieres") or {}
    RECORDS.update(ancien_index.get("records_rivieres") or {})

    out = Path(a.out_dir)
    (out / "episodes").mkdir(parents=True, exist_ok=True)
    limite = (date.today() - timedelta(days=FIGE_APRES_JOURS)).isoformat()
    episodes = detecter(vigi, climato)
    caches: dict[str, dict] = {}
    for debut, fin in episodes:
        if debut in ancien and ancien[debut]["fin"] == fin and fin < limite:
            e = get_json(f"{PUBLIE}episodes/{debut}.json")
            if e is not None:
                caches[debut] = e
    a_calculer = [(d, f) for d, f in episodes if d not in caches]
    hix: dict[str, dict[str, float]] = {}
    if a_calculer:
        depuis = min(d for d, _ in a_calculer) if anciennes_medianes else "2001-10-01"
        hix = charger_hix(hydro, depuis, anciennes_medianes or None)

    index = []
    recalcules = 0
    for debut, fin in episodes:
        e = caches.get(debut)
        if e is None:
            e = calculer(debut, fin, vigi, postes, climato, hydro, hix, radar_jours)
            recalcules += 1
            if recalcules % 20 == 0:
                print(f"  {recalcules} épisodes calculés…", flush=True)
        (out / "episodes" / f"{debut}.json").write_text(json.dumps(e, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        index.append(resume(e))
    index.sort(key=lambda x: x["debut"], reverse=True)
    (out / "index.json").write_text(json.dumps({
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "departement": "66",
        "criteres": f"vigilance orange ou rouge, ou au moins {SEUIL_PLUIE_MM:.0f} mm en un jour à un pluviomètre du département",
        "episodes": index,
        "medianes_rivieres": {k: round(v, 3) for k, v in (MEDIANES or anciennes_medianes).items()},
        "records_rivieres": RECORDS,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(index)} épisodes ({recalcules} recalculés) ; dernier : {index[0]['titre']} du {index[0]['debut']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

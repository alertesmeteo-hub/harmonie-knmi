#!/usr/bin/env python3
"""Stations en direct : Pyrénées-Orientales (Météo-France) et Catalogne nord (Meteocat, réseau XEMA).

Meteocat publie ses stations automatiques en données ouvertes (analisi.transparenciacatalunya.cat,
jeu nzvn-apee, pas de 30 min, ~20 min de retard, sans clé). On garde les stations en service à moins
de RAYON_KM d'une commune du 66 : les orages et les pluies qui arrivent par le sud y passent 30 à 60 min
avant d'atteindre le département. Les stations du 66 viennent des fichiers déjà publiés sur la branche
observations (température, rafales, pluie horaire Météo-France).

Publie stations-66.json : dernière température, ressenti au vent, humidité, point de rosée, vent moyen et
direction, pression ramenée au niveau de la mer, maxi et mini du jour (minuit UTC), variation sur 24 h,
pluie sur 30 min à 72 h, rafale maximale de la dernière heure ; pour Meteocat aussi le rayonnement solaire
et la neige au sol, et les 12 dernières heures au pas de 30 min.

Publie aussi stations-vent.json (page « Vent en direct » du SaaS) : vent moyen, direction et rafale des stations
Météo-France des Pyrénées-Orientales et de l'Aude, et des stations Meteocat de la province de Gérone jusqu'à Blanes
(ZONE_VENT), avec les 12 dernières heures de vent au pas de 30 min pour Meteocat.
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
# Lues sur 25 h seulement (dernière valeur, maxi et mini du jour) pour rester loin de la limite de lignes de l'API.
VARIABLES_25H = {"33": "hr", "34": "p", "36": "rs", "38": "neige", "40": "tx", "42": "tn", "48": "vent6", "46": "vent2"}
FENETRES_H = [0.5, 1, 3, 6, 24, 48, 72]
# Page « Vent en direct » : Pyrénées-Orientales, Aude, Gérone et Costa Brava jusqu'à Blanes (sud, ouest, nord, est).
ZONE_VENT = (41.62, 1.65, 43.47, 3.45)
DEPARTEMENTS_VENT = ("66", "11")
VARIABLES_VENT = {"30": "vent", "31": "dir", "50": "rafale10", "53": "rafale6", "56": "rafale2"}
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


def point_de_rosee(t: float | None, hr: float | None) -> float | None:
    """Formule de Magnus (°C)."""
    if t is None or hr is None or hr <= 0:
        return None
    g = math.log(min(hr, 100) / 100) + 17.62 * t / (243.12 + t)
    return round(243.12 * g / (17.62 - g), 1)


def ressenti_vent(t: float | None, vent_kmh: float | None) -> float | None:
    """Refroidissement éolien, sinon la température réelle : même calcul que le ressenti des stations
    Météo-France (generer_temperature_records.calc_wind_chill)."""
    if t is None:
        return None
    if vent_kmh is not None and t <= 10.0 and vent_kmh >= 4.8:
        q = vent_kmh ** 0.16
        return round(13.12 + 0.6215 * t - 11.37 * q + 0.3965 * t * q, 1)
    return round(t, 1)


def humidex(t: float | None, td: float | None) -> float | None:
    """Indice de chaleur canadien, seulement par temps chaud (même règle que les stations Météo-France)."""
    if t is None or td is None or t < 20.0:
        return None
    e = 6.11 * math.exp(5417.7530 * (1 / 273.15 - 1 / (td + 273.15)))
    h = t + 0.5555 * (e - 10.0)
    return round(h, 1) if h >= t + 1.0 else None


def pression_mer(p: float | None, alt: float | None, t: float | None) -> float | None:
    """Pression de la station ramenée au niveau de la mer (nivellement barométrique)."""
    if p is None or alt is None or t is None or not 500 < p < 1100:
        return None
    return round(p * (1 - 0.0065 * alt / (t + 0.0065 * alt + 273.15)) ** -5.257, 1)


def series_xema(codes: list[str], variables: dict[str, str], debut: datetime, limite: int) -> dict[str, dict[str, dict[datetime, float]]]:
    rows = get(DONNEES_XEMA, {
        "$where": f"codi_estacio in({','.join(repr(c) for c in codes)}) AND codi_variable in({','.join(repr(v) for v in variables)}) "
                  f"AND data_lectura >= '{debut.strftime('%Y-%m-%dT%H:%M:%S')}'",
        "$select": "codi_estacio,codi_variable,data_lectura,valor_lectura",
        "$limit": limite,
    })
    if len(rows) >= limite:
        print(f"Attention : {len(rows)} lignes XEMA, limite atteinte", flush=True)
    series: dict[str, dict[str, dict[datetime, float]]] = {}
    for r in rows:
        try:
            v = float(r["valor_lectura"])
        except (TypeError, ValueError):
            continue
        # la lecture de hh:00 couvre hh:00-hh:30 (UTC) : on la date à la fin de la période
        t = datetime.fromisoformat(r["data_lectura"]).replace(tzinfo=timezone.utc) + timedelta(minutes=30)
        series.setdefault(r["codi_estacio"], {}).setdefault(variables[r["codi_variable"]], {})[t] = v
    return series


def derniere(serie: dict[datetime, float] | None, fin: datetime, max_age_h: float = 1.5) -> float | None:
    """Dernière valeur d'une série, si elle n'a pas plus de max_age_h heures de retard sur la station."""
    if not serie:
        return None
    t = max(serie)
    return serie[t] if t >= fin - timedelta(hours=max_age_h) else None


def du_jour(serie: dict[datetime, float] | None, fin: datetime) -> list[float]:
    """Valeurs des périodes de la journée UTC de `fin` (une période finissant à 00:00 appartient à la veille)."""
    return [v for t, v in (serie or {}).items() if (t - timedelta(seconds=1)).date() == (fin - timedelta(seconds=1)).date()]


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
    series = series_xema(list(proches), VARIABLES, maintenant - timedelta(hours=73), 100000)
    derniere_mesure = max((t for d in series.values() for x in d.values() for t in x), default=None)
    try:
        for code, d25 in series_xema(list(proches), VARIABLES_25H, maintenant - timedelta(hours=25), 50000).items():
            series.setdefault(code, {}).update(d25)
    except RuntimeError as e:  # les paramètres complémentaires ne doivent pas bloquer la pluie et les rafales
        print(f"Paramètres complémentaires Meteocat indisponibles : {e}", flush=True)
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
        t = derniere(d.get("t"), fin)
        hr = derniere(d.get("hr"), fin)
        vent_ms = derniere(d.get("vent") or d.get("vent6") or d.get("vent2"), fin)
        vent = round(vent_ms * 3.6) if vent_ms is not None else None
        td = point_de_rosee(t, hr)
        # maxi et mini du jour UTC, comme les stations Météo-France (tx_today / tn_today)
        tx = du_jour(d.get("tx"), fin) or du_jour(d.get("t"), fin)
        tn = du_jour(d.get("tn"), fin) or du_jour(d.get("t"), fin)
        t_24 = d["t"].get(t_last - timedelta(hours=24)) if t_last else None
        rs = derniere(d.get("rs"), fin)
        neige = derniere(d.get("neige"), fin, 3)
        pas = [fin - timedelta(minutes=30 * k) for k in range(23, -1, -1)]
        out.append({
            **s, "source": "meteocat",
            "a": iso(fin),
            "t": d["t"][t_last] if t_last else None,
            "ressenti": ressenti_vent(t, vent),
            "humidex": humidex(t, td),
            "hr": round(hr) if hr is not None else None,
            "td": td,
            "pression": pression_mer(derniere(d.get("p"), fin), s["alt"], t),
            "tx": max(tx) if tx else None,
            "tn": min(tn) if tn else None,
            "var_24h": round(d["t"][t_last] - t_24, 1) if t_24 is not None else None,
            "rs": round(rs) if rs is not None else None,
            "neige": round(neige) if neige is not None and neige >= 0 else None,
            "pluie": pluie,
            "couverture": couverture,
            "rafale_1h": round(max(raf_1h) * 3.6) if raf_1h else None,
            "vent": vent,
            "dir": derniere(d.get("dir"), fin),
            "serie": {
                "pas_min": 30,
                "pluie": [ppt.get(t) for t in pas],
                "rafale": [round(raf[t] * 3.6) if t in raf else None for t in pas],
            },
        })
    return out, iso(derniere_mesure) if derniere_mesure else None


def meteo_france() -> tuple[list[dict], str | None]:
    pluie = get(OBS + "observations_pluie.json")
    temp = get(OBS + "observations_temperature.json")
    raf = get(OBS + "observations_rafales.json")
    try:
        pres = get(OBS + "observations_pression.json")["stations"]
    except RuntimeError as e:
        print(f"Pression Météo-France indisponible : {e}", flush=True)
        pres = []
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
            e["ressenti"], e["humidex"] = s.get("wind_feels_like"), s.get("humidex")
            e["hr"] = round(s["humidity"]) if s.get("humidity") is not None else None
            e["td"] = s.get("dewpoint")
            e["tx"], e["tn"] = s.get("tx_today"), s.get("tn_today")
            e["var_24h"] = s.get("variation_24h")
    for s in raf["stations"]:
        if s.get("department_code") == "66":
            e = st.setdefault(s["id"], {"id": s["id"], "nom": s["name"].title(), "lat": s["lat"], "lon": s["lon"], "source": "meteofrance", "pluie": {}})
            e["rafale_1h"] = round(s["latest_gust_kmh"]) if s.get("latest_gust_kmh") is not None else None
            e["rafale_a"] = s.get("latest_gust_time")
            e["vent"] = round(s["latest_mean_wind_kmh"]) if s.get("latest_mean_wind_kmh") is not None else None
            e["dir"] = s.get("latest_direction_deg")
            e["alt"] = s.get("altitude_m")
    for s in pres:
        e = st.get(s.get("id"))
        if e is not None and s.get("pressure") is not None:
            e["pression"], e["pression_a"], e["pression_3h"] = s["pressure"], s.get("time"), s.get("var3")
    latest = max((x.get("a") or x.get("pluie_a") or "" for x in st.values()), default=None)
    return list(st.values()), latest


def dans_zone(lat: float, lon: float) -> bool:
    return ZONE_VENT[0] <= lat <= ZONE_VENT[2] and ZONE_VENT[1] <= lon <= ZONE_VENT[3]


def vent_meteocat(maintenant: datetime) -> list[dict]:
    """Stations Meteocat de la zone : dernier vent moyen (10 m), direction, rafale, et 12 h de vent au pas de 30 min."""
    meta = get(STATIONS_XEMA, {"$limit": 2000})
    zone = {}
    for st in meta:
        if st.get("codi_estat_ema") != "2":
            continue
        la, lo = float(st["latitud"]), float(st["longitud"])
        if dans_zone(la, lo):
            zone[st["codi_estacio"]] = {"id": st["codi_estacio"], "nom": st["nom_estacio"], "lat": la, "lon": lo,
                                        "alt": float(st.get("altitud") or 0), "comarca": st.get("nom_comarca", "")}
    if not zone:
        return []
    debut = (maintenant - timedelta(hours=13)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = get(DONNEES_XEMA, {
        "$where": f"codi_estacio in({','.join(repr(c) for c in zone)}) AND codi_variable in({','.join(repr(v) for v in VARIABLES_VENT)}) AND data_lectura >= '{debut}'",
        "$select": "codi_estacio,codi_variable,data_lectura,valor_lectura",
        "$limit": 50000,
    })
    series: dict[str, dict[str, dict[datetime, float]]] = {}
    for r in rows:
        try:
            v = float(r["valor_lectura"])
        except (TypeError, ValueError):
            continue
        t = datetime.fromisoformat(r["data_lectura"]).replace(tzinfo=timezone.utc) + timedelta(minutes=30)
        series.setdefault(r["codi_estacio"], {}).setdefault(VARIABLES_VENT[r["codi_variable"]], {})[t] = v
    out = []
    for code, info in zone.items():
        d = series.get(code, {})
        vent = d.get("vent", {})
        raf = d.get("rafale10") or d.get("rafale6") or d.get("rafale2") or {}
        if not vent and not raf:
            continue  # station sans anémomètre
        fin = max([*vent, *raf])
        if fin < maintenant - timedelta(hours=3):
            continue  # station muette
        pas = [fin - timedelta(minutes=30 * k) for k in range(23, -1, -1)]
        dernier_vent = max(vent) if vent else None
        out.append({
            **info, "source": "meteocat", "a": iso(fin),
            # m/s → km/h ; direction d'où vient le vent (degrés)
            "vent": round(vent[dernier_vent] * 3.6) if dernier_vent else None,
            "dir": d.get("dir", {}).get(dernier_vent) if dernier_vent else None,
            "rafale": round(raf[max(raf)] * 3.6) if raf else None,
            "serie": {"pas_min": 30, "vent": [round(vent[t] * 3.6) if t in vent else None for t in pas],
                      "rafale": [round(raf[t] * 3.6) if t in raf else None for t in pas]},
        })
    return out


def vent_meteo_france() -> list[dict]:
    """Stations Météo-France du 66 et de l'Aude : dernier vent moyen, direction, rafale, maxima sur 24 h."""
    raf = get(OBS + "observations_rafales.json")
    out = []
    for st in raf["stations"]:
        if st.get("department_code") not in DEPARTEMENTS_VENT or st.get("latest_mean_wind_kmh") is None:
            continue
        arrondi = lambda v: round(v) if v is not None else None  # noqa: E731
        out.append({
            "id": st["id"], "nom": st["name"].title(), "lat": st["lat"], "lon": st["lon"], "alt": st.get("altitude_m"),
            "departement": st["department_code"], "source": "meteofrance",
            "a": st.get("latest_mean_wind_time") or st.get("latest_gust_time"),
            "vent": arrondi(st.get("latest_mean_wind_kmh")),
            "dir": st.get("latest_mean_wind_direction_deg") if st.get("latest_mean_wind_direction_deg") is not None else st.get("latest_direction_deg"),
            "rafale": arrondi(st.get("latest_gust_kmh")),
            "vent_max_24h": arrondi(st.get("mean_wind_24h_max_kmh")),
            "rafale_max_24h": arrondi(st.get("gust_24h_kmh")),
        })
    return out


def ecrire_vent(od: Path, maintenant: datetime) -> None:
    """stations-vent.json ; une source en panne n'empêche pas l'autre, et rien n'empêche stations-66.json."""
    stations, sources_ok = [], []
    for nom, f in (("meteofrance", vent_meteo_france), ("meteocat", lambda: vent_meteocat(maintenant))):
        try:
            stations += f()
            sources_ok.append(nom)
        except Exception as e:  # noqa: BLE001 - une source de vent ne doit pas faire échouer les stations du 66
            print(f"Vent {nom} indisponible : {e}", flush=True)
    if not stations:
        print("Aucune station de vent : stations-vent.json non publié", flush=True)
        return
    (od / "stations-vent.json").write_text(json.dumps({
        "schema_version": 1,
        "generated_at": iso(maintenant),
        "zone": {"sud": ZONE_VENT[0], "ouest": ZONE_VENT[1], "nord": ZONE_VENT[2], "est": ZONE_VENT[3]},
        "unite": "km/h ; dir = direction d'où vient le vent (degrés)",
        "sources_ok": sources_ok,
        "stations": stations,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    fort = max(stations, key=lambda x: x.get("vent") or 0)
    print(f"Vent : {len(stations)} stations ({', '.join(sources_ok)}), plus fort {fort['nom']} {fort.get('vent')} km/h")


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
    seul_mf = False
    if len(cat) < 10:
        if cat_at:
            latest = datetime.fromisoformat(cat_at.replace("Z", "+00:00"))
            age_h = (maintenant - latest).total_seconds() / 3600
            if age_h > 6:
                if len(mf) < 10:
                    print(
                        f"Flux Meteocat retardé ({cat_at}, {age_h:.1f} h) : pas de publication",
                        file=sys.stderr,
                    )
                    return 2
                # Meteocat en retard (panne côté Generalitat) mais Météo-France à jour : on publie les seules stations
                # Météo-France plutôt que de figer toute la page ; les stations catalanes réapparaîtront au retour du flux.
                print(
                    f"Flux Meteocat retardé ({cat_at}, {age_h:.1f} h) : publication des seules stations Météo-France",
                    flush=True,
                )
                cat = []
                seul_mf = True
        if not seul_mf:
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
    ecrire_vent(od, maintenant)
    fortes = sorted(((x["pluie"].get("1") or 0, x["nom"]) for x in cat), reverse=True)[:3]
    print(f"{len(cat)} stations Meteocat (dernière mesure {cat_at}), {len(mf)} Météo-France ; pluie 1 h maxi : {fortes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tests de generer_records_menaces.py : point de grille, correction d'altitude, TX/TN, classement des records menacés,
mémoire des écarts, correction de biais et passage complet. Aucun appel réseau.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import gzip
import importlib.util
import json
import math
import os
import tempfile

MODULE_PATH = Path(__file__).resolve().parents[1] / "generer_records_menaces.py"
SPEC = importlib.util.spec_from_file_location("records_menaces", MODULE_PATH)
g = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(g)

NOW = datetime(2026, 10, 13, 8, 30, tzinfo=timezone.utc)
AUJ = date(2026, 10, 13)
DEMAIN = date(2026, 10, 14)
APRES = date(2026, 10, 15)

# --- Fichiers des modèles d'essai : cycle diurne horaire, TN à 3 h UTC, TX à 15 h UTC -------------------------------------
def iso_h(h):
    return datetime.fromtimestamp(h * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def doc_modele(points, base, amp, debut=g.heure(AUJ), fin=g.heure(AUJ) + 24 * 4, pas=1, generated="2026-10-13T06:00:00Z", decalage=0.0):
    """points : [[model_index, lat, lon, altitude], ...] ; base : température moyenne au niveau de chaque point (liste)."""
    pas_liste = []
    for h in range(debut, fin + 1, pas):
        hh = h % 24
        cycle = amp * math.cos(2 * math.pi * (hh - 15) / 24)  # +amp à 15 h, −amp à 3 h
        pas_liste.append([iso_h(h), [[round(b + cycle + decalage, 2), 50] for b in base]])
    return {"schema_version": 3, "status": "ok", "generated_at": generated, "department": "66",
            "columns": {"points": ["model_index", "latitude", "longitude", "altitude_m"], "values": ["temperature_c", "humidity_pct"]},
            "points": points, "communes": [], "forecast": pas_liste}


# --- Choix du point : compromis distance + écart d'altitude / 100 ---------------------------------------------------------
pts = [[1, 42.50, 2.50, 100], [2, 42.55, 2.55, 900], [3, 43.50, 3.50, 50]]
doc = doc_modele(pts, [15, 8, 14], 4)
assert g.fichiers_departement("66") == ["66"] and g.fichiers_departement("20") == ["2A", "2B"]
# station de montagne à 850 m : le point à 900 m, un peu plus loin, vaut mieux que le point à 100 m
c = g.choisir_point(doc, 42.51, 2.51, 850)
assert c and c[0] == 1 and c[1] == 900, c
# station de plaine à 120 m : le point à 100 m
c = g.choisir_point(doc, 42.51, 2.51, 120)
assert c and c[0] == 0 and c[1] == 100, c
# aucun point à moins de 40 km : pas de prévision
assert g.choisir_point(doc, 47.0, 6.0, 300) is None
# sans altitude de station, on prend le plus proche
assert g.choisir_point(doc, 42.5001, 2.5001, None)[0] == 0
assert g.choisir_point({"columns": {}, "points": []}, 42, 2, 100) is None

# --- Températures brutes d'un point ; correction d'altitude sur les extrêmes : 6,5 °C par km le jour, 1 °C par km la nuit -----------------------
brut = g.echantillons(doc, 0)
h15 = g.heure(AUJ, 15)
assert abs(brut[h15] - 19.0) < 1e-9, brut[h15]  # 15 °C de base + 4 °C de cycle à 15 h
assert g.echantillons({"columns": {"values": ["x"]}, "forecast": []}, 0) == {}
sans = g.prevoir_station({"arome": [doc_modele(pts, [10, 10, 10], 5)]}, 42.5001, 2.5001, 100, [DEMAIN])["arome"]["2026-10-14"]
haut = g.prevoir_station({"arome": [doc_modele(pts, [10, 10, 10], 5)]}, 42.5001, 2.5001, 600, [DEMAIN])["arome"]["2026-10-14"]  # 500 m plus haut que le point de grille
assert abs((sans[1] - haut[1]) - 3.25) < 1e-9, (sans, haut)  # TX : 6,5 °C par km
assert abs((sans[0] - haut[0]) - 0.5) < 1e-9, (sans, haut)  # TN : 1 °C par km seulement
inconnue = g.prevoir_station({"arome": [doc_modele(pts, [10, 10, 10], 5)]}, 42.5001, 2.5001, None, [DEMAIN])["arome"]["2026-10-14"]
assert abs(inconnue[1] - sans[1]) < 1e-9 and abs(inconnue[0] - sans[0]) < 1e-9  # altitude inconnue : aucune correction

# --- TN et TX : fenêtres 18 h → 18 h UTC et 6 h → 6 h UTC -----------------------------------------------------------------------
serie = g.echantillons(doc_modele(pts, [10, 10, 10], 5), 0)
tn, tx = g.extremes_jour(serie, DEMAIN)
assert tn is not None and tx is not None and abs(tn - 5.0) < 0.2 and abs(tx - 15.0) < 0.2, (tn, tx)
# TX du dernier jour de la série : la fenêtre s'arrête après la fin des échantillons -> non couverte
assert g.extremes_jour(serie, date(2026, 10, 17))[1] is None
# TN du jour 1 : une série qui démarre à 12 h UTC la veille couvre la fenêtre (tolérance 12 h au début) ; à 12 h UTC le jour même, non
court = {h: v for h, v in serie.items() if h >= g.heure(DEMAIN, 12)}
assert g.extremes_jour(court, DEMAIN)[0] is None
# trou de 6 h dans la fenêtre : non couverte
troue = {h: v for h, v in serie.items() if not (g.heure(DEMAIN, 9) < h < g.heure(DEMAIN, 15))}
assert g.extremes_jour(troue, DEMAIN)[1] is None
# échantillons toutes les 3 h : couverts
trois = {h: v for h, v in serie.items() if h % 3 == 0}
tn3, tx3 = g.extremes_jour(trois, DEMAIN)
assert tn3 is not None and tx3 is not None and abs(tx3 - tx) < 0.5

# --- Prévision d'une station : plusieurs modèles, Corse sur deux fichiers ------------------------------------------------------
docs = {"arome": [doc_modele(pts, [10, 10, 10], 5)], "icon": [doc_modele(pts, [11, 11, 11], 5, pas=3)], "cep": []}
prev = g.prevoir_station(docs, 42.51, 2.51, 100, [DEMAIN, APRES])
assert set(prev) == {"arome", "icon"}, prev.keys()
assert abs(prev["arome"]["2026-10-14"][1] - 15.0) < 0.2 and abs(prev["icon"]["2026-10-14"][1] - 16.0) < 0.5
nord = doc_modele([[9, 42.0, 9.0, 10]], [20], 3)
sud = doc_modele([[8, 41.5, 9.2, 10]], [18], 3)
corse = g.prevoir_station({"arome": [nord, sud]}, 41.52, 9.18, 10, [DEMAIN])  # plus près du fichier « sud »
assert abs(corse["arome"]["2026-10-14"][1] - 21.0) < 0.2, corse

# --- Références d'essai : une station de plaine (froid, chaleur, nuit tropicale) -------------------------------------------------
def station_ref(**kw):
    s = {"name": "ESSAI", "lat": 42.51, "lon": 2.51, "altitude": 100.0, "opening_year": 1950, "principal": True,
         "department_code": "66", "region": "Occitanie",
         "records": {
             "heat": {"absolute": [38.0, "2003-08-04"], "days": {"2026-10-14": {"monthly": [26.0, "2023-10-02"], "fortnight": [26.0, "2023-10-02"], "daily": [14.0, "2011-10-14"]},
                                                                  "2026-10-15": {"monthly": [26.0, "2023-10-02"], "fortnight": [24.0, "2005-10-20"], "daily": [25.0, "2022-10-15"]}}},
             "cold": {"absolute": [-10.0, "1956-02-10"], "days": {"2026-10-14": {"monthly": [0.0, "1974-10-28"], "fortnight": [0.0, "1974-10-28"], "daily": [6.0, "1993-10-14"]},
                                                                  "2026-10-15": {"monthly": [0.0, "1974-10-28"], "fortnight": [-1.0, "1962-10-17"], "daily": [2.0, "1988-10-15"]}}},
             "tropical": {"absolute": [30.0, "2018-08-04"], "days": {"2026-10-14": {"monthly": [22.0, "1997-10-11"], "fortnight": [22.0, "1997-10-11"], "daily": [19.0, "2018-10-14"]}}},
         }}
    s.update(kw)
    return s


par_modele = g.prevoir_station({"arome": [doc_modele(pts, [10, 10, 10], 5)], "arpege": [doc_modele(pts, [10, 10, 10], 5, decalage=1.0)],
                                "icon": [doc_modele(pts, [10, 10, 10], 5, decalage=-1.0)]}, 42.51, 2.51, 100, [DEMAIN, APRES])
# TX prévue ≈ 15 °C (14 à 16 selon le modèle), TN prévue ≈ 5 °C
evts = g.classer_station("S1", station_ref(), par_modele, DEMAIN, 1.8, {}, {})
assert set(evts) == {"heat", "cold"}, evts.keys()
chaud = evts["heat"]
assert chaud["flags"] == ["daily"] and chaud["value"] == 15.0 and chaud["forecast"]["n"] == 3 and chaud["forecast"]["min"] == 14.0 and chaud["forecast"]["max"] == 16.0, chaud
assert chaud["deltas"]["daily"] == -1.0 and chaud["deltas"]["monthly"] == 11.0, chaud["deltas"]
assert chaud["records"]["daily"] == {"value": 14.0, "date": "2011-10-14"} and chaud["near"] == [], chaud
froid = evts["cold"]
assert froid["flags"] == ["daily"] and froid["value"] == 5.0 and froid["deltas"]["daily"] == -1.0, froid  # 5,0 °C ≤ 6,0 °C du record du jour
# un modèle seul atteint le record du mois : « possible » même si la médiane en est loin
par2 = g.prevoir_station({"arome": [doc_modele(pts, [10, 10, 10], 5, decalage=11.0)], "arpege": [doc_modele(pts, [10, 10, 10], 5)],
                          "icon": [doc_modele(pts, [10, 10, 10], 5)]}, 42.51, 2.51, 100, [DEMAIN])
ev2 = g.classer_station("S1", station_ref(), par2, DEMAIN, 1.8, {}, {})["heat"]
assert "monthly" in ev2["near"] and "monthly" not in ev2["flags"], ev2  # 26 °C atteints par AROME (15 + 11), médiane 15 °C
# la médiane à 1 °C du record : possible ; à 3 °C : rien
r = station_ref()
r["records"]["heat"]["days"]["2026-10-14"]["daily"] = [16.0, "2011-10-14"]
assert g.classer_station("S1", r, par_modele, DEMAIN, 1.8, {}, {})["heat"]["near"] == ["daily"]
r["records"]["heat"]["days"]["2026-10-14"]["daily"] = [18.5, "2011-10-14"]
assert "heat" not in g.classer_station("S1", r, par_modele, DEMAIN, 1.8, {}, {})
# un seul modèle : pas d'annonce
un = {"arome": par_modele["arome"]}
assert g.classer_station("S1", station_ref(), un, DEMAIN, 1.8, {}, {}) == {}
# nuit tropicale : seulement si un modèle dépasse 19 °C
chaude = g.prevoir_station({"arome": [doc_modele(pts, [20, 20, 20], 3)], "arpege": [doc_modele(pts, [20, 20, 20], 3)]}, 42.51, 2.51, 100, [DEMAIN])
tr = g.classer_station("S1", station_ref(), chaude, DEMAIN, 1.8, {}, {})
assert "tropical" not in tr  # TN = 20 − 3 = 17 °C : sous le seuil de 19 °C, pas d'annonce
chaude2 = g.prevoir_station({"arome": [doc_modele(pts, [24, 24, 24], 3)], "arpege": [doc_modele(pts, [24, 24, 24], 3)]}, 42.51, 2.51, 100, [DEMAIN])
tr2 = g.classer_station("S1", station_ref(), chaude2, DEMAIN, 1.8, {}, {})["tropical"]
assert tr2["value"] == 21.0 and tr2["flags"] == ["daily"] and tr2["near"] == ["monthly"], tr2  # 21 °C ≥ 19 °C (record du jour) et à 1 °C de 22 °C (record du mois)
# nuit tropicale : la médiane doit atteindre 20 °C pour un record « probable » ; entre 19 et 20 °C, « possible » seulement
chaude3 = g.prevoir_station({"arome": [doc_modele(pts, [22.3, 22.3, 22.3], 3)], "arpege": [doc_modele(pts, [22.3, 22.3, 22.3], 3)]}, 42.51, 2.51, 100, [DEMAIN])  # TN = 22,3 − 3 = 19,3 °C
tr3 = g.classer_station("S1", station_ref(), chaude3, DEMAIN, 1.8, {}, {})["tropical"]
assert tr3["flags"] == [] and "daily" in tr3["near"] and tr3["value"] == 19.3, tr3  # record du jour (19,0) dépassé, mais la nuit n'est pas tropicale
# un modèle à plus de 20 °C suffit à garder la station en « possible », même avec une médiane plus basse
mixte = g.prevoir_station({"arome": [doc_modele(pts, [23.5, 23.5, 23.5], 3)], "arpege": [doc_modele(pts, [20.0, 20.0, 20.0], 3)], "icon": [doc_modele(pts, [20.0, 20.0, 20.0], 3)]}, 42.51, 2.51, 100, [DEMAIN])
tr4 = g.classer_station("S1", station_ref(), mixte, DEMAIN, 0.5, {}, {})  # sigma faible : seule la règle « un modèle atteint 20 °C » peut garder la station
assert tr4["tropical"]["flags"] == [] and tr4["tropical"]["near"] == ["daily"], tr4.get("tropical")
# jour sans références : rien
assert g.classer_station("S1", station_ref(), par_modele, date(2026, 10, 20), 1.8, {}, {}) == {}

# --- Correction de biais : général par modèle, propre à la station ------------------------------------------------------------------
glob = {(0, 0): 1.0, (0, 1): -0.5}
station = {("S1", 0, 0): 0.5}
v, b = g.corriger(10.0, 0, 0, "S1", glob, station)
assert abs(v - 8.5) < 1e-9 and abs(b - 1.5) < 1e-9
v, b = g.corriger(10.0, 0, 1, "S2", glob, station)
assert abs(v - 10.5) < 1e-9  # TX : modèle trop froid de 0,5 °C -> on ajoute
v, b = g.corriger(10.0, 1, 0, "S1", glob, station)
assert v == 10.0  # aucune correction pour un modèle sans historique
v, b = g.corriger(10.0, 0, 0, "S3", {(0, 0): 9.0}, {})
assert b == g.BIAIS_MAX and abs(v - (10.0 - g.BIAIS_MAX)) < 1e-9  # plafonnée

# --- Mémoire des écarts ---------------------------------------------------------------------------------------------------------------
etat = g.lire_etat(None)
assert etat["previsions"] == {} and etat["ecarts"] == {} and etat["modeles"] == ["arome", "arpege", "icon", "cep"]
assert g.lire_etat({"schema_version": 99}) == g.etat_vide()  # état écrit par une autre version ou avec d'autres modèles : on repart de zéro
assert g.lire_etat({"schema_version": g.SCHEMA_VERSION, "modeles": ["x"]}) == g.etat_vide()
ancien_calcul = dict(g.etat_vide(), version_calcul=g.ETAT_VERSION - 1, ecarts={"2026-10-12": {"S1": [[1.0, None]] * 4}})
assert g.lire_etat(ancien_calcul) == g.etat_vide()  # écarts issus d'une ancienne méthode de correction : abandonnés
assert g.lire_etat(dict(g.etat_vide(), ecarts={"2026-10-12": {}}))["ecarts"] == {"2026-10-12": {}}  # même méthode : conservés

N = 150  # stations d'essai (au moins MIN_STATIONS_BIAIS)
sids = [f"6600{i:04d}" for i in range(N)]
prev_reseau = {sid: {"arome": {"2026-10-14": (5.0 + i * 0.01, 15.0), "2026-10-15": (4.0, 16.0)},
                     "icon": {"2026-10-14": (6.0 + i * 0.01, 14.0)}} for i, sid in enumerate(sids)}
g.memoriser_previsions(etat, prev_reseau, [DEMAIN, APRES])
assert set(etat["previsions"]) == {"2026-10-14", "2026-10-15"}
ligne = etat["previsions"]["2026-10-14"][sids[0]]
assert ligne[0] == [5.0, 15.0] and ligne[1] == [None, None] and ligne[2] == [6.0, 14.0] and ligne[3] == [None, None], ligne
# le 14 n'est mesuré qu'à partir du 15 octobre à 7 h UTC
assert g.jour_mesure("2026-10-14", datetime(2026, 10, 15, 6, 59, tzinfo=timezone.utc)) is False
assert g.jour_mesure("2026-10-14", datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc)) is True
mesures = {"2026-10-14": {sid: [14.0, 4.0] for sid in sids}}
assert g.bilan_des_jours(etat, mesures, datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)) == []  # trop tôt
assert "2026-10-14" in etat["previsions"] and not etat["ecarts"]
assert g.bilan_des_jours(etat, mesures, datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)) == ["2026-10-14"]
assert "2026-10-14" not in etat["previsions"] and "2026-10-15" in etat["previsions"]
e0 = etat["ecarts"]["2026-10-14"][sids[0]]
assert e0[0] == [1.0, 1.0] and e0[1] == [None, None] and e0[2] == [2.0, 0.0], e0  # prévu − mesuré
# mesure absente pour une partie des stations : jour conservé tant que trop peu de stations sont mesurées
etat2 = g.lire_etat(None)
g.memoriser_previsions(etat2, prev_reseau, [DEMAIN])
assert g.bilan_des_jours(etat2, {"2026-10-14": {sids[0]: [14.0, 4.0]}}, datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)) == []
assert "2026-10-14" in etat2["previsions"]
g.bilan_des_jours(etat2, {}, datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc))
assert "2026-10-14" not in etat2["previsions"]  # abandonné après quelques jours
# écart aberrant (relevé faux) : ignoré
assert g.ecart(5.0, -13.5) is None and g.ecart(5.0, 4.0) == 1.0 and g.ecart(None, 3.0) is None

# --- Biais : général (médiane du jour, pondérée) et propre à la station (atténué) ---------------------------------------------------
etat3 = g.etat_vide()
for k, jour in enumerate(["2026-10-10", "2026-10-11", "2026-10-12"]):
    # AROME trop chaud de 1 °C la nuit sur toutes les stations, et de 4 °C à S0 ; ICON sans écart
    etat3["ecarts"][jour] = {sid: [[(1.0 + (3.0 if i == 0 else 0.0)), None], [None, None], [0.0, None], [None, None]] for i, sid in enumerate(sids)}
glob, st, njours = g.estimer_biais(etat3)
assert njours == 3
# biais général : médiane 1,0 chaque jour, moyenne pondérée atténuée (0,6^k ; retrait 0,4) -> (1+0,6+0,36) / (1,96+0,4)
attendu = (1.0 + 0.6 + 0.36) / (1.96 + 0.4)
assert abs(glob[(0, 0)] - attendu) < 1e-9, glob
assert abs(glob[(2, 0)]) < 1e-9 and (1, 0) not in glob
# écart propre : S0 est 3 °C au-dessus de la médiane chaque jour (3 jours) -> 3 × 3 / (3 + 3) = 1,5 ; les autres : 0
assert abs(st[(sids[0], 0, 0)] - 1.5) < 1e-9, st[(sids[0], 0, 0)]
assert abs(st[(sids[1], 0, 0)]) < 1e-9
# un seul jour de recul : atténué par 1 / (1 + 3)
etat4 = g.etat_vide()
etat4["ecarts"]["2026-10-12"] = etat3["ecarts"]["2026-10-12"]
glob4, st4, n4 = g.estimer_biais(etat4)
assert n4 == 1 and abs(st4[(sids[0], 0, 0)] - 3.0 / 4.0) < 1e-9 and abs(glob4[(0, 0)] - 1.0 / 1.4) < 1e-9
# moins de MIN_STATIONS_BIAIS stations : pas de biais général
petit = g.etat_vide()
petit["ecarts"]["2026-10-12"] = {sid: [[1.0, None], [None, None], [None, None], [None, None]] for sid in sids[:20]}
assert g.estimer_biais(petit)[0] == {}

# --- Qualité et incertitude --------------------------------------------------------------------------------------------------------------
q = g.qualite_des_ecarts(etat3)
assert q["jours"] == 3 and q["tn"]["modeles"]["arome"]["n"] == 3 * N and q["tn"]["modeles"]["icon"]["biais"] == 0.0
assert g.sigmas({"jours": 1}) == g.SIGMA_DEFAUT  # historique trop court
assert g.sigmas({"jours": 3, "tn": {"dispersion": 1.0}}) == {1: g.SIGMA_MIN, 2: max(g.SIGMA_MIN, 1.15 * 1.1)}  # bornée par le bas
s_mes = g.sigmas({"jours": 5, "tn": {"dispersion": 1.5}, "tx": {"dispersion": 1.7}})
assert abs(s_mes[1] - 1.15 * 1.7) < 1e-9 and abs(s_mes[2] - 1.15 * 1.7 * 1.1) < 1e-9
assert g.sigmas({"jours": 5, "tn": {"dispersion": 9.0}})[1] == g.SIGMA_MAX  # bornée

# --- Passage complet ------------------------------------------------------------------------------------------------------------------------
def references_essai(n=N):
    stations = {}
    for i in range(n):
        sid = f"6600{i:04d}"
        rec = station_ref(name=f"STATION {i}")
        if i % 3 == 0:  # un tiers des stations ont un record du jour de chaleur à battre demain
            rec["records"]["heat"]["days"]["2026-10-14"]["daily"] = [14.0, "2011-10-14"]
        else:
            rec["records"]["heat"]["days"]["2026-10-14"]["daily"] = [30.0, "2011-10-14"]
        stations[sid] = rec
    return {"schema_version": 1, "generated_at": "2026-10-13T08:00:00Z", "days": ["2026-10-14", "2026-10-15", "2026-10-16"], "stations": stations}


MODELES_ESSAI = {
    "arome-meteofrance": doc_modele(pts, [10, 10, 10], 5, decalage=0.0, generated="2026-10-13T06:03:00Z"),
    "ARPEGE-0.25": doc_modele(pts, [10, 10, 10], 5, decalage=0.5, generated="2026-10-13T04:30:00Z"),
    "ICON-GLOBAL-13-km": doc_modele(pts, [10, 10, 10], 5, decalage=-0.5, generated="2026-10-13T04:14:00Z"),
    "cep": doc_modele(pts, [10, 10, 10], 5, decalage=0.0, pas=3, generated="2026-10-12T21:30:00Z", debut=g.heure(AUJ) - 12),
}


def charger_essai(depot, dep):
    return MODELES_ESSAI.get(depot) if dep == "66" else None


refs = references_essai()
mesures_vides = {}
menaces, etat_sortie = g.generer(refs, mesures_vides, None, charger_essai, NOW)
assert menaces["status"] == "ok" and menaces["module_version"] == g.VERSION and menaces["schema_version"] == 1
assert [m["id"] for m in menaces["models"]] == ["arome", "arpege", "icon", "cep"], menaces["models"]
assert set(menaces["days"]) == {"2026-10-14", "2026-10-15"}
j14 = menaces["days"]["2026-10-14"]
assert j14["stations_with_values"] == N and j14["fortnight_label"] == "1re quinzaine" and j14["provisional"] is True
assert j14["counts"]["heat"] == N // 3 + (1 if N % 3 else 0) and j14["counts"]["heat_records"] == j14["counts"]["heat"], j14["counts"]
assert j14["counts"]["cold"] == N  # record du jour de froid à 6 °C, TN prévue 5 °C partout
assert menaces["correction"]["jours"] == 0 and menaces["correction"]["graine"] is False  # NOW = 8 h 30 mais l'état est vide et il n'y a pas de relevés
assert "2026-10-14" in etat_sortie["previsions"] and len(etat_sortie["previsions"]["2026-10-14"]) == N
# le fichier se sérialise (aucun NaN)
json.dumps(menaces, allow_nan=False)
# deuxième passage le lendemain, avec les relevés du 14 : écarts mesurés puis correction appliquée
demain_8h = datetime(2026, 10, 15, 8, 30, tzinfo=timezone.utc)
mesures14 = {"2026-10-14": {f"6600{i:04d}": [14.0, 3.0] for i in range(N)}}  # TN mesurée 3 °C : les modèles (5 °C) sont trop chauds de 2 °C
refs2 = dict(refs, days=["2026-10-15", "2026-10-16", "2026-10-17"])
refs2["stations"] = {sid: dict(s) for sid, s in refs["stations"].items()}
for s in refs2["stations"].values():
    s["records"] = json.loads(json.dumps(s["records"]))
    s["records"]["cold"]["days"]["2026-10-16"] = s["records"]["cold"]["days"]["2026-10-15"]
    s["records"]["heat"]["days"]["2026-10-16"] = s["records"]["heat"]["days"]["2026-10-15"]
J15 = g.heure(date(2026, 10, 15))
MODELES_J15 = {
    "arome-meteofrance": doc_modele(pts, [10, 10, 10], 5, debut=J15, fin=J15 + 96, generated="2026-10-15T06:03:00Z"),
    "ARPEGE-0.25": doc_modele(pts, [10, 10, 10], 5, debut=J15, fin=J15 + 96, decalage=0.5, generated="2026-10-15T04:30:00Z"),
    "ICON-GLOBAL-13-km": doc_modele(pts, [10, 10, 10], 5, debut=J15, fin=J15 + 96, decalage=-0.5, generated="2026-10-15T04:14:00Z"),
    "cep": doc_modele(pts, [10, 10, 10], 5, debut=J15 - 12, fin=J15 + 96, pas=3, generated="2026-10-14T21:30:00Z"),
}
menaces2, etat2 = g.generer(refs2, mesures14, etat_sortie, lambda depot, dep: MODELES_J15.get(depot) if dep == "66" else None, demain_8h)
assert "2026-10-14" in etat2["ecarts"] and "2026-10-14" not in etat2["previsions"], etat2.keys()
assert menaces2["correction"]["jours"] == 1
biais = menaces2["correction"]["biais_generaux"]
assert 1.0 < biais["arome_tn"] < 2.0, biais  # écart d'environ 2 °C, atténué à 1 / 1,4
assert set(menaces2["days"]) == {"2026-10-16", "2026-10-17"} or set(menaces2["days"]) == {"2026-10-16"}, menaces2["days"].keys()
# les modèles trop périmés sont écartés
vieux = dict(MODELES_ESSAI["ICON-GLOBAL-13-km"])
vieux["forecast"] = [[iso_h(g.heure(AUJ) - 100 + k), [[10, 50]] * 3] for k in range(0, 40)]
MODELES_PERIMES = dict(MODELES_ESSAI, **{"ICON-GLOBAL-13-km": vieux})
m3, _ = g.generer(refs, {}, None, lambda depot, dep: MODELES_PERIMES.get(depot) if dep == "66" else None, NOW)
assert [m["id"] for m in m3["models"]] == ["arome", "arpege", "cep"], m3["models"]
# aucun modèle : échec explicite, rien n'est publié
try:
    g.generer(refs, {}, None, lambda depot, dep: None, NOW)
    raise AssertionError("une absence de prévision doit être une erreur")
except RuntimeError:
    pass

# --- Amorçage de la correction le matin par l'écart des modèles à la TN du jour --------------------------------------------------------
mesures_auj = {"2026-10-13": {f"6600{i:04d}": [10.0, 1.0] for i in range(N)}}
m4, e4 = g.generer(refs, mesures_auj, None, charger_essai, NOW)
assert m4["correction"]["graine"] is True and "2026-10-13" in e4["ecarts"] and m4["correction"]["jours"] == 1
assert m4["correction"]["biais_generaux"]["arome_tn"] > 0.5  # TN du jour prévue ≈ 5 °C pour 1 °C mesuré
# pas d'amorçage l'après-midi, ni quand l'historique existe déjà
m5, _ = g.generer(refs, mesures_auj, None, charger_essai, datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc))
assert m5["correction"]["graine"] is False
m6, _ = g.generer(refs, mesures_auj, e4, charger_essai, NOW)
assert m6["correction"]["graine"] is False

# --- Lecture des relevés : contrôlés prioritaires sur les provisoires --------------------------------------------------------------------
fus = g.fusionner_mesures({"days": {"2026-10-10": {"A": [30.0, 10.0]}}}, {"days": {"2026-10-10": {"A": [99.0, 99.0], "B": [20.0, 5.0]}, "2026-10-11": {"A": [31.0, 11.0]}}})
assert fus["2026-10-10"]["A"] == [30.0, 10.0] and fus["2026-10-10"]["B"] == [20.0, 5.0] and "2026-10-11" in fus
assert g.fusionner_mesures(None, None) == {}

# --- Ligne de commande de bout en bout (dossiers locaux) -----------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "obs" / "cache").mkdir(parents=True)
    (tmp / "obs" / "references.json").write_text(json.dumps(refs), encoding="utf-8")
    (tmp / "obs" / "cache" / "recent.json").write_text(json.dumps({"days": {}}), encoding="utf-8")
    (tmp / "obs" / "cache" / "provisional.json").write_text(json.dumps({"days": mesures_auj}), encoding="utf-8")
    for depot, doc in MODELES_ESSAI.items():
        (tmp / "modeles" / depot).mkdir(parents=True)
        (tmp / "modeles" / depot / "66.json").write_text(json.dumps(doc), encoding="utf-8")
    ancien = os.getcwd()
    import sys
    argv = sys.argv
    try:
        sys.argv = ["generer_records_menaces.py", "--references", str(tmp / "obs"), "--modeles-dir", str(tmp / "modeles"),
                    "--sortie", str(tmp / "sortie"), "--maintenant", "2026-10-13T08:30:00Z"]
        assert g.main() == 0
        sortie = json.loads((tmp / "sortie" / "menaces.json").read_text(encoding="utf-8"))
        assert sortie["status"] == "ok" and set(sortie["days"]) == {"2026-10-14", "2026-10-15"}
        etat_fichier = json.loads((tmp / "sortie" / "etat.json").read_text(encoding="utf-8"))
        assert etat_fichier["schema_version"] == 1 and "ecarts" in etat_fichier
        # archive : une copie compressée de chaque fichier, nommée d'après l'heure du calcul, identique au fichier publié
        archives = sorted(p.name for p in (tmp / "sortie" / "archive").glob("*"))
        assert archives == ["20261013T083000Z_etat.json.gz", "20261013T083000Z_menaces.json.gz"], archives
        assert gzip.decompress((tmp / "sortie" / "archive" / archives[1]).read_bytes()) == (tmp / "sortie" / "menaces.json").read_bytes()
        assert gzip.decompress((tmp / "sortie" / "archive" / archives[0]).read_bytes()) == (tmp / "sortie" / "etat.json").read_bytes()
        # deuxième passage avec l'état écrit : relu sans erreur
        sys.argv += ["--etat", str(tmp / "sortie" / "etat.json")]
        assert g.main() == 0
    finally:
        sys.argv = argv
        os.chdir(ancien)

# --- Archive des annonces : copie compressée par calcul, conservée 45 jours, idempotente -----------------------------------------------------
with tempfile.TemporaryDirectory() as tmp_a:
    sortie_a = Path(tmp_a)
    (sortie_a / "menaces.json").write_text('{"a":1}', encoding="utf-8")
    (sortie_a / "etat.json").write_text('{"b":2}', encoding="utf-8")
    (sortie_a / "archive").mkdir()
    # limite = 13/10 à 08:30 moins 45 jours = 29/08 à 08:30 : les deux premiers sont périmés, le troisième (44 jours et 19 h) est conservé
    for prefixe in ("20260828T223000Z", "20260829T013000Z", "20260829T133000Z"):
        for base in ("menaces", "etat"):
            (sortie_a / "archive" / f"{prefixe}_{base}.json.gz").write_bytes(gzip.compress(b"{}"))
    (sortie_a / "archive" / "notes.json.gz").write_bytes(b"x")  # nom hors format : jamais supprimé
    ecrits = g.archiver(sortie_a, "2026-10-13T08:30:00Z", NOW)
    assert ecrits == ["20261013T083000Z_menaces.json.gz", "20261013T083000Z_etat.json.gz"], ecrits
    restants = sorted(p.name for p in (sortie_a / "archive").glob("*"))
    assert not any(x.startswith(("20260828T223000Z", "20260829T013000Z")) for x in restants), restants
    assert "20260829T133000Z_menaces.json.gz" in restants and "20260829T133000Z_etat.json.gz" in restants and "notes.json.gz" in restants, restants
    assert gzip.decompress((sortie_a / "archive" / ecrits[0]).read_bytes()) == b'{"a":1}'
    # même calcul archivé deux fois : mêmes fichiers, mêmes octets
    avant = (sortie_a / "archive" / ecrits[0]).read_bytes()
    assert g.archiver(sortie_a, "2026-10-13T08:30:00Z", NOW) == ecrits and (sortie_a / "archive" / ecrits[0]).read_bytes() == avant
    assert len(list((sortie_a / "archive").glob("*.tmp"))) == 0

print("tests generer_records_menaces : OK")

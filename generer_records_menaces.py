#!/usr/bin/env python3
"""Records de température menacés : prévisions de nos modèles ramenées aux stations, comparées aux records.

Pour chaque station suivie par le pipeline des records (records à battre publiés dans references.json par
generer_records_temperature.py), on calcule la température maximale (TX) et minimale (TN) prévues les jours à venir par
AROME, ARPEGE, ICON-GLOBAL et ECMWF IFS. Les fichiers par département des dépôts alertesmeteo-hub/<modèle> (branche data,
departements/<département>.json) donnent la température de chaque point de grille ; aucun service extérieur.

Pour une station, on retient le point de grille qui minimise « distance + écart d'altitude / 100 » (au plus 15 km, sinon le
plus proche) et on corrige la température de l'écart d'altitude : 6,5 °C par km pour la maximale, 1 °C par km seulement pour la
minimale (la nuit, par temps clair et calme, la température dépend peu de l'altitude). TX = maximum de 6 h UTC à 6 h UTC le lendemain,
TN = minimum de 18 h UTC la veille à 18 h UTC (définition des relevés de Météo-France, comme pour les records).

Les modèles se trompent de façon durable sur une station (vallée froide, crête, littoral) et d'un jour à l'autre sur toutes à la
fois (nuits claires trop douces). On garde donc d'un passage à l'autre (etat.json) les prévisions faites pour les jours à venir ;
quand le jour est écoulé et mesuré, l'écart est conservé, et la correction appliquée aux prévisions suivantes est la somme d'un
biais général récent de chaque modèle et d'un écart propre à la station (atténué tant que l'historique est court).

Sorties (dossier --sortie) : menaces.json (stations dont un record est menacé, par jour) et etat.json (mémoire des écarts), plus
archive/ : une copie compressée de chacun à chaque calcul (voir `archiver`), pour comparer plus tard ce qui était annoncé à ce qui s'est produit.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "records-menaces-20261013"
ETAT_VERSION = 2  # à changer quand la méthode de prévision change : les écarts mémorisés ne valent que pour la méthode qui les a produits

RAW = "https://raw.githubusercontent.com/alertesmeteo-hub/"
OBS_BASE = RAW + "harmonie-knmi/observations/records_temperature/"
UA = "Mozilla/5.0 (compatible; AlertesMeteo-RecordsMenaces/1.0)"

# id, nom affiché, dépôt (branche data, departements/<département>.json)
MODELES: Tuple[Tuple[str, str, str], ...] = (
    ("arome", "AROME", "arome-meteofrance"),
    ("arpege", "ARPEGE", "ARPEGE-0.25"),
    ("icon", "ICON", "ICON-GLOBAL-13-km"),
    ("cep", "CEP", "cep"),
)
JOURS_PREVUS = (1, 2)  # lendemain et surlendemain (J+1, J+2)

# Correction de l'écart d'altitude entre le point de grille et la station (°C par mètre), différente le jour et la nuit :
GRADIENT_TX = 0.0065  # le jour l'air est brassé et la température suit l'altitude (mesuré le 12/10/2026 sur l'IFS : l'erreur croît de 6,7 °C par km)
GRADIENT_TN = 0.0010  # la nuit, claire et calme, elle n'en dépend presque pas (inversions) : le 13/10/2026 l'erreur des quatre modèles est
#                       minimale entre 0 et 1 °C par km, et dépasse de 0,4 °C avec 6,5 °C par km
RAYON_KM = 15.0  # au plus ce rayon : meilleur compromis distance + écart d'altitude / 100 ; au-delà, le point le plus proche
RAYON_MAX_KM = 40.0
PAS_MAX_H = 3  # écart maximal entre deux échantillons d'une fenêtre pour la juger couverte
AGE_MAX_H = 36  # une prévision dont le premier échantillon date de plus de 36 h est écartée
H = 3600.0
EQUAL_EPS = 0.05  # même tolérance d'égalité que le pipeline des records

MIN_MODELES = 2  # au moins deux modèles pour annoncer un record menacé
TROPICAL_C = 20.0  # nuit tropicale : TN d'au moins 20 °C (comme pour les records observés, où seules les valeurs d'au moins 20 °C comptent)
SIGMA_DEFAUT = {1: 1.8, 2: 2.0}  # °C : incertitude d'une prévision de TX ou de TN (médiane des modèles corrigée), J+1 et J+2
SIGMA_MIN, SIGMA_MAX = 1.2, 2.8
Z_POSSIBLE = 0.674  # « possible » : une chance sur quatre au moins (loi normale d'écart-type sigma)
JOURS_ECARTS = 14  # jours d'écarts conservés
BIAIS_JOURS = 10  # jours pris pour l'écart propre à une station
BIAIS_GLOBAL_JOURS = 3  # jours pris pour le biais général d'un modèle
BIAIS_RETRAIT = 3.0  # atténuation de l'écart propre à la station : n / (n + 3)
BIAIS_GLOBAL_RETRAIT = 0.4
BIAIS_MAX = 4.0
ECART_ABERRANT = 12.0  # un écart prévu − mesuré de plus de 12 °C vient d'un relevé ou d'une prévision aberrants : ignoré pour la correction
MIN_STATIONS_BIAIS = 100  # en dessous, la médiane d'un jour n'est pas retenue
HEURE_BILAN = 7  # heure UTC après laquelle la veille est considérée comme mesurée (fenêtre TX close à 6 h UTC)
ARCHIVE_DIR = "archive"  # sous le dossier de sortie
ARCHIVE_JOURS = 45  # durée de conservation des annonces archivées


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fnum(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = math.pi / 180
    a = math.sin((lat2 - lat1) * r / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def heure(jour: date, h: int = 0) -> int:
    """Heure UTC (heures depuis l'epoch) d'un jour à h heures."""
    return int(datetime(jour.year, jour.month, jour.day, tzinfo=timezone.utc).timestamp() // H) + h


def heure_iso(texte: str) -> int:
    return int(datetime.fromisoformat(texte.replace("Z", "+00:00")).timestamp() // H)


def fichiers_departement(dep: str) -> List[str]:
    """Fichier(s) département des modèles pour le département d'une station : la Corse (20) est coupée en 2A et 2B."""
    return ["2A", "2B"] if dep == "20" else [dep]


# ———— Prévision d'une station à partir des fichiers des modèles ————


def _colonne(doc: dict, nom: str) -> Optional[int]:
    try:
        return doc["columns"]["values"].index(nom)
    except (KeyError, ValueError):
        return None


def choisir_point(doc: dict, lat: float, lon: float, alti: Optional[float]) -> Optional[Tuple[int, float, float]]:
    """(indice du point, altitude du modèle, distance en km) : au plus RAYON_KM le meilleur compromis distance + écart d'altitude / 100,
    sinon le point le plus proche (à moins de RAYON_MAX_KM)."""
    try:
        cp = doc["columns"]["points"]
        i_lat, i_lon = cp.index("latitude"), cp.index("longitude")
        i_alt = cp.index("altitude_m") if "altitude_m" in cp else cp.index("model_altitude_m")
    except (KeyError, ValueError):
        return None
    meilleur = proche = None
    for k, p in enumerate(doc["points"]):
        d = distance_km(lat, lon, float(p[i_lat]), float(p[i_lon]))
        palt = float(p[i_alt]) if isinstance(p[i_alt], (int, float)) else 0.0
        if proche is None or d < proche[0]:
            proche = (d, k, palt)
        if d <= RAYON_KM:
            score = d + (abs(alti - palt) / 100.0 if alti is not None else 0.0)
            if meilleur is None or score < meilleur[0]:
                meilleur = (score, k, palt, d)
    if meilleur:
        return meilleur[1], meilleur[2], meilleur[3]
    if proche and proche[0] <= RAYON_MAX_KM:
        return proche[1], proche[2], proche[0]
    return None


def echantillons(doc: dict, point: int) -> Dict[int, float]:
    """Températures brutes (heure UTC -> °C) d'un point de grille ; la correction d'altitude se fait sur les extrêmes (voir `prevoir_station`)."""
    i_t = _colonne(doc, "temperature_c")
    if i_t is None:
        return {}
    out: Dict[int, float] = {}
    for t, rows in doc["forecast"]:
        try:
            v = fnum(rows[point][i_t])
            if v is not None:
                out[heure_iso(t)] = v
        except (IndexError, TypeError, ValueError):
            continue
    return out


def fenetre(ech: Dict[int, float], a: int, b: int, mode: str, debut_tolere: int) -> Optional[float]:
    """Extrême (« min » ou « max ») des échantillons de la fenêtre [a, b] (heures UTC), ou None si elle n'est pas couverte : un
    échantillon à moins de `debut_tolere` heures du début et de PAS_MAX_H heures de la fin, et aucun trou de plus de PAS_MAX_H heures."""
    h = sorted(x for x in ech if a <= x <= b)
    if not h or h[0] > a + debut_tolere or h[-1] < b - PAS_MAX_H:
        return None
    if any(y - x > PAS_MAX_H for x, y in zip(h, h[1:])):
        return None
    vals = [ech[x] for x in h]
    return max(vals) if mode == "max" else min(vals)


def extremes_jour(ech: Dict[int, float], jour: date) -> Tuple[Optional[float], Optional[float]]:
    """(TN, TX) du jour : TN = minimum de 18 h UTC la veille à 18 h UTC, TX = maximum de 6 h UTC à 6 h UTC le lendemain."""
    h0 = heure(jour)
    # TN : le soir de la veille (début de la fenêtre) compte peu, le minimum est à l'aube ; une prévision qui démarre à 6 h UTC suffit.
    return fenetre(ech, h0 - 6, h0 + 18, "min", 12), fenetre(ech, h0 + 6, h0 + 30, "max", PAS_MAX_H)


def meilleur_point(docs: List[dict], lat: float, lon: float, alti: Optional[float]) -> Optional[Tuple[dict, int, float]]:
    """(fichier, indice du point, altitude du modèle) le plus proche de la station parmi les fichiers du département (deux pour la Corse)."""
    choix = None
    for doc in docs:
        c = choisir_point(doc, lat, lon, alti)
        if c and (choix is None or c[2] < choix[3]):
            choix = (doc, c[0], c[1], c[2])
    return (choix[0], choix[1], choix[2]) if choix else None


def prevoir_station(docs_par_modele: Dict[str, List[dict]], lat: float, lon: float, alti: Optional[float],
                    jours: List[date]) -> Dict[str, Dict[str, Tuple[Optional[float], Optional[float]]]]:
    """{modèle: {jour: (TN, TX)}} d'une station ; un modèle sans point proche ou sans fenêtre couverte est omis."""
    out: Dict[str, Dict[str, Tuple[Optional[float], Optional[float]]]] = {}
    for mid, docs in docs_par_modele.items():
        choix = meilleur_point(docs, lat, lon, alti)
        if not choix:
            continue
        doc, point, alt_modele = choix
        ech = echantillons(doc, point)
        if not ech:
            continue
        ecart_alt = (alti - alt_modele) if alti is not None else 0.0  # > 0 : station plus haute que le point de grille
        par_jour = {}
        for j in jours:
            tn, tx = extremes_jour(ech, j)
            if tn is not None or tx is not None:
                par_jour[j.isoformat()] = (None if tn is None else tn - GRADIENT_TN * ecart_alt, None if tx is None else tx - GRADIENT_TX * ecart_alt)
        if par_jour:
            out[mid] = par_jour
    return out


# ———— Lecture réseau ————


def _requests():
    import requests  # noqa: PLC0415 — importé à la demande : les tests n'en ont pas besoin

    return requests


def telecharger_json(url: str, tentatives: int = 3) -> Optional[Any]:
    requests = _requests()
    for essai in range(tentatives):
        try:
            r = requests.get(url, timeout=(15, 120), headers={"User-Agent": UA})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            if essai + 1 == tentatives:
                print(f"  {url} indisponible : {exc}", flush=True)
                return None
            time.sleep(5 * 3 ** essai)
    return None


def lire_json(chemin: Path) -> Optional[Any]:
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def document_valide(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("status") in (None, "ok") and isinstance(doc.get("forecast"), list) and len(doc["forecast"]) > 0


def chargeur_modeles(dossier: Optional[str]) -> Callable[[str, str], Optional[dict]]:
    """Fonction (dépôt, fichier département) -> document, lue sur GitHub ou, pour les essais, dans `dossier`/<dépôt>/<département>.json."""

    def charger(depot: str, dep: str) -> Optional[dict]:
        doc = lire_json(Path(dossier) / depot / f"{dep}.json") if dossier else telecharger_json(f"{RAW}{depot}/data/departements/{dep}.json")
        return doc if document_valide(doc) else None

    return charger


def lire_source(base: str, chemin: str) -> Optional[Any]:
    """JSON d'un fichier du pipeline des records : URL (par défaut la branche observations) ou dossier local."""
    if base.startswith("http"):
        return telecharger_json(base.rstrip("/") + "/" + chemin)
    return lire_json(Path(base) / chemin)


# ———— Prévisions de tout le réseau ————


def prevoir_reseau(stations: Dict[str, dict], jours: List[date], charger: Callable[[str, str], Optional[dict]], now: datetime,
                   travailleurs: int = 6) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """(prévisions, modèles) : prévisions[station] = {modèle: {jour: (TN, TX)}} ; modèles[id] = informations sur le fichier lu."""
    par_dep: Dict[str, List[str]] = {}
    for sid, s in stations.items():
        par_dep.setdefault(str(s.get("department_code") or sid[:2]), []).append(sid)
    infos: Dict[str, dict] = {}
    perimes: Dict[str, bool] = {}

    def traiter(dep: str) -> Dict[str, dict]:
        docs: Dict[str, List[dict]] = {}
        for mid, nom, depot in MODELES:
            for f in fichiers_departement(dep):
                doc = charger(depot, f)
                if not doc:
                    continue
                premier = doc["forecast"][0][0]
                age = (now - datetime.fromisoformat(premier.replace("Z", "+00:00"))).total_seconds() / H
                if age > AGE_MAX_H:
                    perimes[mid] = True
                    continue
                docs.setdefault(mid, []).append(doc)
                infos.setdefault(mid, {"id": mid, "nom": nom, "depot": depot, "genere": doc.get("generated_at"), "debut": premier,
                                       "fin": doc["forecast"][-1][0]})
        out = {}
        for sid in par_dep[dep]:
            s = stations[sid]
            p = prevoir_station(docs, float(s["lat"]), float(s["lon"]), fnum(s.get("altitude")), jours)
            if p:
                out[sid] = p
        return out

    resultat: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=travailleurs) as pool:
        for part in pool.map(traiter, sorted(par_dep)):
            resultat.update(part)
    for mid in perimes:
        print(f"[WARN] modèle {mid} : prévisions trop anciennes (plus de {AGE_MAX_H} h), écartées pour certains départements")
    return resultat, infos


# ———— Mémoire des écarts et correction ————


def etat_vide() -> dict:
    return {"schema_version": SCHEMA_VERSION, "version_calcul": ETAT_VERSION, "modeles": [m[0] for m in MODELES], "previsions": {}, "ecarts": {}}


def lire_etat(brut: Any) -> dict:
    """État conservé d'un passage à l'autre ; vide si absent, illisible, écrit avec une autre liste de modèles ou par une autre méthode de calcul."""
    if (not isinstance(brut, dict) or brut.get("schema_version") != SCHEMA_VERSION or brut.get("version_calcul") != ETAT_VERSION
            or brut.get("modeles") != [m[0] for m in MODELES]):
        return etat_vide()
    for cle in ("previsions", "ecarts"):
        if not isinstance(brut.get(cle), dict):
            brut[cle] = {}
    return brut


def memoriser_previsions(etat: dict, previsions: Dict[str, dict], jours: List[date]) -> None:
    """Garde, pour chaque jour à venir, la dernière prévision brute de chaque modèle : [[TN, TX] par modèle, dans l'ordre de MODELES]."""
    ordre = [m[0] for m in MODELES]
    for j in jours:
        cle = j.isoformat()
        pour_jour = {}
        for sid, par_modele in previsions.items():
            lignes = []
            for mid in ordre:
                v = (par_modele.get(mid) or {}).get(cle)
                lignes.append([None if v is None or v[0] is None else round(v[0], 1), None if v is None or v[1] is None else round(v[1], 1)])
            if any(x is not None for ligne in lignes for x in ligne):
                pour_jour[sid] = lignes
        if pour_jour:
            etat["previsions"][cle] = pour_jour


def ecart(prevu: Optional[float], mesure: Optional[float]) -> Optional[float]:
    """Prévu − mesuré (°C) ; None si une valeur manque ou si l'écart dépasse ECART_ABERRANT (relevé ou prévision manifestement faux)."""
    if prevu is None or mesure is None:
        return None
    e = prevu - mesure
    return round(e, 2) if abs(e) <= ECART_ABERRANT else None


def jour_mesure(jour: str, now: datetime) -> bool:
    """Le jour est complètement mesuré : sa fenêtre TX (jusqu'à 6 h UTC le lendemain) est close depuis au moins une heure."""
    return now >= datetime.fromisoformat(jour).replace(tzinfo=timezone.utc) + timedelta(days=1, hours=HEURE_BILAN)


def bilan_des_jours(etat: dict, mesures: Dict[str, Dict[str, list]], now: datetime) -> List[str]:
    """Pour chaque jour mémorisé et désormais mesuré : écarts (prévu − mesuré) de chaque modèle, déplacés de `previsions` vers `ecarts`."""
    faits = []
    for jour in sorted(etat["previsions"]):
        if not jour_mesure(jour, now):
            continue
        vues = mesures.get(jour) or {}
        ecarts: Dict[str, list] = {}
        for sid, lignes in etat["previsions"][jour].items():
            obs = vues.get(sid)
            if not obs:
                continue
            tx, tn = fnum(obs[0]), fnum(obs[1])
            ligne_ecarts = []
            for prev_tn, prev_tx in lignes:
                ligne_ecarts.append([ecart(prev_tn, tn), ecart(prev_tx, tx)])
            if any(x is not None for e in ligne_ecarts for x in e):
                ecarts[sid] = ligne_ecarts
        if len(ecarts) >= MIN_STATIONS_BIAIS:
            etat["ecarts"][jour] = ecarts
            del etat["previsions"][jour]
            faits.append(jour)
        elif (now.date() - date.fromisoformat(jour)).days > 5:
            del etat["previsions"][jour]  # jamais mesuré par assez de stations : abandonné
    for jour in sorted(etat["ecarts"])[:-JOURS_ECARTS]:
        del etat["ecarts"][jour]
    return faits


def graine_de_l_aube(etat: dict, previsions: Dict[str, dict], mesures: Dict[str, Dict[str, list]], today: date, now: datetime) -> bool:
    """Premier passage, le matin : la TN du jour est quasi définitive, on compare les modèles aux relevés pour amorcer la correction.

    Les modèles qui couvrent le début de la journée (prévisions des passages de 0 h ou 6 h UTC) donnent la TN prévue du jour ;
    l'écart est celui d'une prévision très courte, c'est une première estimation du biais, remplacée par les écarts réels ensuite."""
    jour = today.isoformat()
    if etat["ecarts"] or jour in etat["ecarts"] or not (8 <= now.hour <= 12):
        return False
    vues = mesures.get(jour) or {}
    ordre = [m[0] for m in MODELES]
    ecarts: Dict[str, list] = {}
    for sid, par_modele in previsions.items():
        obs = vues.get(sid)
        tn = fnum(obs[1]) if obs else None
        if tn is None:
            continue
        ligne = []
        for mid in ordre:
            v = (par_modele.get(mid) or {}).get(jour)
            ligne.append([ecart(None if v is None else v[0], tn), None])
        if any(e[0] is not None for e in ligne):
            ecarts[sid] = ligne
    if len(ecarts) < MIN_STATIONS_BIAIS:
        return False
    etat["ecarts"][jour] = ecarts
    return True


def mediane_jour(ecarts_jour: Dict[str, list], i_modele: int, i_var: int) -> Optional[float]:
    vals = [e[i_modele][i_var] for e in ecarts_jour.values() if e[i_modele][i_var] is not None]
    return statistics.median(vals) if len(vals) >= MIN_STATIONS_BIAIS else None


def estimer_biais(etat: dict) -> Tuple[Dict[Tuple[int, int], float], Dict[Tuple[str, int, int], float], int]:
    """(biais général[(modèle, variable)], écart propre à la station[(station, modèle, variable)], nombre de jours) ; variable 0 = TN, 1 = TX.

    Biais général : moyenne pondérée (poids 0,6 par jour de recul) des médianes, sur les BIAIS_GLOBAL_JOURS derniers jours, atténuée
    quand l'historique est court. Écart de station : moyenne de (écart − médiane du jour) sur les BIAIS_JOURS derniers jours,
    multipliée par n / (n + 3)."""
    jours = sorted(etat["ecarts"], reverse=True)[:BIAIS_JOURS]
    glob: Dict[Tuple[int, int], float] = {}
    station: Dict[Tuple[str, int, int], float] = {}
    meds: Dict[Tuple[int, int], Dict[str, float]] = {}
    for i_m in range(len(MODELES)):
        for i_v in (0, 1):
            par_jour = {}
            for j in jours:
                m = mediane_jour(etat["ecarts"][j], i_m, i_v)
                if m is not None:
                    par_jour[j] = m
            meds[(i_m, i_v)] = par_jour
            recents = [par_jour[j] for j in jours if j in par_jour][:BIAIS_GLOBAL_JOURS]
            if recents:
                poids = [0.6 ** k for k in range(len(recents))]
                g = sum(p * m for p, m in zip(poids, recents)) / (sum(poids) + BIAIS_GLOBAL_RETRAIT)
                glob[(i_m, i_v)] = max(-BIAIS_MAX, min(BIAIS_MAX, g))
    sommes: Dict[Tuple[str, int, int], List[float]] = {}
    for j in jours:
        for sid, lignes in etat["ecarts"][j].items():
            for i_m, ligne in enumerate(lignes):
                for i_v in (0, 1):
                    e = ligne[i_v]
                    med = meds[(i_m, i_v)].get(j)
                    if e is not None and med is not None:
                        sommes.setdefault((sid, i_m, i_v), []).append(e - med)
    for cle, anomalies in sommes.items():
        n = len(anomalies)
        station[cle] = sum(anomalies) / (n + BIAIS_RETRAIT)
    return glob, station, len([j for j in jours if any(meds[k].get(j) is not None for k in meds)])


def qualite_des_ecarts(etat: dict) -> dict:
    """Écarts bruts constatés sur les derniers jours mesurés (information publiée avec les prévisions)."""
    jours = sorted(etat["ecarts"], reverse=True)[:BIAIS_GLOBAL_JOURS]
    out: Dict[str, Any] = {"jours": len(jours)}
    for i_v, nom in ((0, "tn"), (1, "tx")):
        par_modele = {}
        dispersions = []
        for i_m, (mid, _, _) in enumerate(MODELES):
            vals = [e[i_m][i_v] for j in jours for e in etat["ecarts"][j].values() if e[i_m][i_v] is not None]
            if len(vals) >= MIN_STATIONS_BIAIS:
                par_modele[mid] = {"biais": round(statistics.fmean(vals), 2), "rmse": round(math.sqrt(statistics.fmean(v * v for v in vals)), 2), "n": len(vals)}
        # dispersion de la médiane des modèles autour de la médiane du jour (le biais général est corrigé, pas la dispersion)
        for j in jours:
            med_j = []
            for e in etat["ecarts"][j].values():
                v = [e[i_m][i_v] for i_m in range(len(MODELES)) if e[i_m][i_v] is not None]
                if len(v) >= MIN_MODELES:
                    med_j.append(statistics.median(v))
            if len(med_j) >= MIN_STATIONS_BIAIS:
                centre = statistics.median(med_j)
                dispersions.extend(m - centre for m in med_j)
        out[nom] = {"modeles": par_modele}
        if len(dispersions) >= 3 * MIN_STATIONS_BIAIS:
            out[nom]["dispersion"] = round(math.sqrt(statistics.fmean(d * d for d in dispersions)), 2)
    return out


def sigmas(qualite: dict) -> Dict[int, float]:
    """Incertitude (°C) d'une prévision de J+1 et J+2 : celle mesurée sur les derniers jours si l'historique suffit (trois jours au moins),
    sinon les valeurs par défaut ; bornée."""
    dispersions = [qualite.get(nom, {}).get("dispersion") for nom in ("tn", "tx")]
    dispersions = [d for d in dispersions if d is not None]
    if qualite.get("jours", 0) >= 3 and dispersions:
        base = 1.15 * max(dispersions)
        return {1: max(SIGMA_MIN, min(SIGMA_MAX, base)), 2: max(SIGMA_MIN, min(SIGMA_MAX, base * 1.1))}
    return dict(SIGMA_DEFAUT)


# ———— Records menacés ————


def corriger(valeur: float, i_modele: int, i_var: int, sid: str, glob: dict, station: dict) -> Tuple[float, float]:
    """(valeur corrigée, correction retranchée)."""
    b = glob.get((i_modele, i_var), 0.0) + station.get((sid, i_modele, i_var), 0.0)
    b = max(-BIAIS_MAX, min(BIAIS_MAX, b))
    return valeur - b, b


def classer_station(sid: str, s: dict, par_modele: dict, jour: date, sigma: float, glob: dict, station: dict) -> Dict[str, dict]:
    """{famille: événement} des records menacés ce jour-là pour la station (au plus un par famille)."""
    cle = jour.isoformat()
    ordre = [m[0] for m in MODELES]
    out: Dict[str, dict] = {}
    for kind, i_var, mode in (("heat", 1, "max"), ("cold", 0, "min"), ("tropical", 0, "max")):
        bloc = (s.get("records") or {}).get(kind)
        if not bloc:
            continue
        corrigees: Dict[str, float] = {}
        biais_appliques = []
        for mid, par_jour in par_modele.items():
            v = (par_jour.get(cle) or (None, None))[i_var]
            if v is None:
                continue
            c, b = corriger(v, ordre.index(mid), i_var, sid, glob, station)
            corrigees[mid] = c
            biais_appliques.append(b)
        if len(corrigees) < MIN_MODELES:
            continue
        valeurs = list(corrigees.values())
        centre = statistics.median(valeurs)
        marge = Z_POSSIBLE * sigma
        meilleur = max(valeurs) if mode == "max" else min(valeurs)  # le modèle le plus favorable au record
        # Nuit tropicale : la minimale doit aussi atteindre 20 °C, sinon ce n'est pas une nuit tropicale (« probable » : médiane d'au moins 20 °C ;
        # « possible » : médiane à moins de 0,67 sigma de 20 °C, ou un modèle l'atteint)
        tropicale_probable = kind != "tropical" or centre >= TROPICAL_C - EQUAL_EPS
        if kind == "tropical" and not (tropicale_probable or centre + marge >= TROPICAL_C or meilleur >= TROPICAL_C):
            continue
        jour_refs = (bloc.get("days") or {}).get(cle) or {}
        refs: Dict[str, Optional[dict]] = {}
        for scope in ("absolute", "monthly", "fortnight", "daily"):
            r = bloc.get("absolute") if scope == "absolute" else jour_refs.get(scope)
            refs[scope] = {"value": r[0], "date": r[1]} if isinstance(r, list) and len(r) == 2 and fnum(r[0]) is not None else None
        flags, near, deltas = [], [], {}
        for scope, ref in refs.items():
            if not ref:
                continue
            rv = float(ref["value"])
            delta = (rv - centre) if mode == "max" else (centre - rv)
            delta_meilleur = (rv - meilleur) if mode == "max" else (meilleur - rv)
            deltas[scope] = round(delta, 2)
            if delta <= EQUAL_EPS and tropicale_probable:
                flags.append(scope)  # probable : la médiane des modèles atteint le record
            elif scope in ("absolute", "monthly", "daily") and (delta <= marge or delta_meilleur <= EQUAL_EPS):
                near.append(scope)  # possible : médiane à moins de 0,67 sigma du record, ou un modèle l'atteint
        if not flags and not near:
            continue
        out[kind] = {
            "id": sid, "name": s.get("name"), "lat": s.get("lat"), "lon": s.get("lon"), "altitude": s.get("altitude"),
            "opening_year": s.get("opening_year"), "principal": s.get("principal") is True,
            "department_code": s.get("department_code"), "region": s.get("region") or "France",
            "value": round(centre, 1), "kind": kind, "flags": flags, "near": near,
            "records": {k: v for k, v in refs.items()}, "deltas": deltas, "provisional": True,
            "forecast": {"models": {k: round(v, 1) for k, v in corrigees.items()}, "min": round(min(valeurs), 1), "max": round(max(valeurs), 1),
                         "n": len(valeurs), "bias": round(statistics.median(biais_appliques), 2)},
        }
    return out


def construire_jour(jour: date, references: dict, previsions: Dict[str, dict], sigma: float, glob: dict, station: dict) -> dict:
    heat, cold, tropical = [], [], []
    avec_prevision = 0
    for sid, s in references.get("stations", {}).items():
        par_modele = previsions.get(sid)
        if not par_modele:
            continue
        avec_prevision += 1
        evts = classer_station(sid, s, par_modele, jour, sigma, glob, station)
        if "heat" in evts:
            heat.append(evts["heat"])
        if "cold" in evts:
            cold.append(evts["cold"])
        if "tropical" in evts:
            tropical.append(evts["tropical"])
    ordre = {"absolute": 0, "monthly": 1, "fortnight": 2, "daily": 3}

    def cle(e: dict) -> tuple:
        meilleure = min((ordre[f] for f in e["flags"]), default=9)
        return (0 if e["flags"] else 1, meilleure, min(e["deltas"].values(), default=99), e.get("name") or "")

    for liste in (heat, cold, tropical):
        liste.sort(key=cle)
    return {
        "date": jour.isoformat(), "provisional": True, "source": "Prévisions AROME, ARPEGE, ICON, CEP ramenées aux stations",
        "fortnight_label": "1re quinzaine" if jour.day <= 14 else "2e quinzaine", "sigma": round(sigma, 2),
        "events": {"heat": heat, "cold": cold, "tropical": tropical},
        "counts": {"heat": len(heat), "cold": len(cold), "tropical": len(tropical),
                   "heat_records": sum(bool(e["flags"]) for e in heat), "cold_records": sum(bool(e["flags"]) for e in cold),
                   "tropical_records": sum(bool(e["flags"]) for e in tropical)},
        "stations_with_values": avec_prevision,
    }


def fusionner_mesures(controlees: Any, provisoires: Any) -> Dict[str, Dict[str, list]]:
    """{jour: {station: [TX, TN]}} : les valeurs contrôlées l'emportent sur les provisoires."""
    out: Dict[str, Dict[str, list]] = {}
    for src in (provisoires, controlees):
        jours = (src or {}).get("days") if isinstance(src, dict) else None
        for jour, vals in (jours or {}).items():
            out.setdefault(jour, {}).update(vals)
    return out


def generer(references: dict, mesures: Dict[str, Dict[str, list]], etat_brut: Any, charger: Callable[[str, str], Optional[dict]], now: datetime) -> Tuple[dict, dict]:
    """(menaces.json, etat.json) à partir des records à battre, des relevés récents, de l'état précédent et des fichiers des modèles."""
    today = now.date()
    etat = lire_etat(etat_brut)
    stations = references.get("stations") or {}
    cibles = [today + timedelta(days=k) for k in JOURS_PREVUS]
    cibles = [j for j in cibles if j.isoformat() in (references.get("days") or [])]
    jours_calcul = [today] + [today + timedelta(days=k) for k in JOURS_PREVUS]
    previsions, infos = prevoir_reseau(stations, jours_calcul, charger, now)
    if len(infos) == 0 or not previsions:
        raise RuntimeError("Aucune prévision de modèle disponible")
    faits = bilan_des_jours(etat, mesures, now)
    graine = graine_de_l_aube(etat, previsions, mesures, today, now)
    glob, station, jours_biais = estimer_biais(etat)
    qualite = qualite_des_ecarts(etat)
    sig = sigmas(qualite)
    memoriser_previsions(etat, previsions, [today + timedelta(days=k) for k in JOURS_PREVUS])
    jours_out = {}
    for k, jour in zip(JOURS_PREVUS, [today + timedelta(days=k) for k in JOURS_PREVUS]):
        if jour in cibles:
            jours_out[jour.isoformat()] = construire_jour(jour, references, previsions, sig[k], glob, station)
    menaces = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID, "status": "ok", "generated_at": iso(now),
        "references_at": references.get("generated_at"),
        "models": [dict(infos[m[0]]) for m in MODELES if m[0] in infos],
        "correction": {"jours": jours_biais, "graine": graine, "biais_generaux": {f"{MODELES[i][0]}_{'tn' if v == 0 else 'tx'}": round(b, 2) for (i, v), b in sorted(glob.items())},
                       "stations_corrigees": len({k[0] for k in station})},
        "qualite": qualite, "sigma": {str(k): round(v, 2) for k, v in sig.items()},
        "days": jours_out,
    }
    etat["generated_at"] = iso(now)
    etat["bilans_ajoutes"] = faits
    return menaces, etat


def ecrire_json(chemin: Path, data: Any) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(chemin.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    tmp.replace(chemin)


def archiver(sortie: Path, instant: str, now: datetime, jours: int = ARCHIVE_JOURS) -> List[str]:
    """Copie compressée de menaces.json et etat.json dans <sortie>/archive, nommée d'après l'heure du calcul
    (AAAAMMJJTHHMMSSZ_menaces.json.gz et AAAAMMJJTHHMMSSZ_etat.json.gz) ; supprime les copies de plus de `jours` jours. Renvoie les noms écrits.

    Les fichiers publiés sont réécrits à chaque calcul, sans historique : sans cette archive, la prévision faite la veille pour un jour
    serait perdue quand on voudrait la comparer à ce qui s'est produit. La compression est déterministe (même contenu, même fichier)."""
    dossier = sortie / ARCHIVE_DIR
    dossier.mkdir(parents=True, exist_ok=True)
    nom = datetime.fromisoformat(instant.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ecrits = []
    for base in ("menaces", "etat"):
        cible = dossier / f"{nom}_{base}.json.gz"
        tmp = cible.with_suffix(cible.suffix + ".tmp")
        tmp.write_bytes(gzip.compress((sortie / f"{base}.json").read_bytes(), compresslevel=9, mtime=0))
        tmp.replace(cible)
        ecrits.append(cible.name)
    limite = (now - timedelta(days=jours)).strftime("%Y%m%dT%H%M%SZ")
    for f in dossier.glob("*.json.gz"):
        if len(f.name) > 16 and f.name[:16] < limite and f.name[8] == "T" and f.name[15] == "Z":
            f.unlink()
    return ecrits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--references", default=OBS_BASE, help="URL ou dossier de références (references.json) et de relevés (cache/recent.json, cache/provisional.json)")
    ap.add_argument("--modeles-dir", help="dossier <dépôt>/<département>.json au lieu de GitHub (essais)")
    ap.add_argument("--etat", help="fichier etat.json du passage précédent")
    ap.add_argument("--sortie", default="records_menaces", help="dossier de sortie")
    ap.add_argument("--maintenant", help="instant ISO UTC (essais)")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.maintenant.replace("Z", "+00:00")) if args.maintenant else utcnow()
    references = lire_source(args.references, "references.json")
    if not isinstance(references, dict) or not references.get("stations"):
        raise RuntimeError("references.json introuvable ou vide")
    mesures = fusionner_mesures(lire_source(args.references, "cache/recent.json"), lire_source(args.references, "cache/provisional.json"))
    etat_brut = lire_json(Path(args.etat)) if args.etat else None
    menaces, etat = generer(references, mesures, etat_brut, chargeur_modeles(args.modeles_dir), now)
    sortie = Path(args.sortie)
    ecrire_json(sortie / "menaces.json", menaces)
    ecrire_json(sortie / "etat.json", etat)
    try:
        ecrits = archiver(sortie, menaces["generated_at"], now)
        print("Archive :", ", ".join(ecrits), "|", len(list((sortie / ARCHIVE_DIR).glob("*.json.gz"))), "fichiers conservés")
    except Exception as exc:  # l'archive ne doit jamais empêcher la publication de l'annonce
        print("[WARN] archive non écrite :", exc)
    for jour, d in menaces["days"].items():
        c = d["counts"]
        print(f"{jour} : chaleur {c['heat']} ({c['heat_records']} probables), froid {c['cold']} ({c['cold_records']} probables), "
              f"nuits tropicales {c['tropical']} ({c['tropical_records']} probables) sur {d['stations_with_values']} stations")
    print("Modèles :", ", ".join(f"{m['nom']} (début {m['debut']})" for m in menaces["models"]))
    print("Correction :", menaces["correction"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

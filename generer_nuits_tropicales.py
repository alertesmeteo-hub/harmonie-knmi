#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nuits tropicales (température minimale d'au moins 20 °C) : nombre par station et par année, indicateur national.

Source : dépôt alertesmeteo-hub/climato, branche data (relevés quotidiens contrôlés de Météo-France, « Données climatologiques de
base - quotidiennes », complétés par le SYNOP pour les derniers jours) :
  stations.json.gz                      catalogue des stations (poste, nom, département, coordonnées, altitude, années disponibles)
  stations/<poste>/<année>.json.gz      un fichier par station et par année : {"days": [{"date", "tx", "tn", ...}]}
Le script ne télécharge rien : il lit un dossier local (--climato-dir), préparé par le workflow (clone partiel de la branche data limité
aux stations retenues, voir --liste-stations).

Stations retenues : en activité et au moins ANNEES_MIN années de relevés. Une année n'est comptée que si au moins COUVERTURE_MIN des jours ont
une minimale (sinon le comptage serait sous-estimé).

Année en cours : les données contrôlées arrivent avec près de trois semaines de retard pour la plupart des stations, et seules les stations
principales sont complétées par le SYNOP jusqu'à la veille. Les stations sont donc comparées à une date commune (la date atteinte par au
moins 80 % d'entre elles : « courante »), et les stations à jour plus tard ont en plus un comptage « derniere » jusqu'à leur dernier relevé.

Indicateur national : moyenne, sur un panel de stations de basse altitude à série longue et complète (altitude, années valides depuis 1950),
du nombre de nuits tropicales de l'année ; référence 1991-2020 ; comparaison de l'année en cours avec la même période des années de référence.
Les séries ne sont pas homogénéisées (déplacements de poste, changements de capteur) : l'indicateur donne l'ordre de grandeur et la tendance.

Sorties (dossier --sortie) : stations.json (comptage par année pour chaque station retenue) et indicateur.json (indicateur national).
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "nuits-tropicales-20261013"

SEUIL_C = 20.0  # nuit tropicale : minimale d'au moins 20 °C
ANNEES_MIN = 30  # une station est retenue à partir de 30 années de relevés
COUVERTURE_MIN = 0.85  # part minimale de jours avec une minimale pour compter une année
ECART_FIN_MAX_J = 10  # une station dont le dernier relevé de l'année date de plus de 10 jours avant la fin des données est écartée pour l'année en cours
PANEL_ALTITUDE_MAX_M = 500.0
PANEL_DEPUIS = 1950
PANEL_PART_ANNEES = 0.90  # le panel ne garde que les stations dont 90 % des années depuis 1950 sont valides
PANEL_PART_STATIONS = 0.80  # une année de l'indicateur exige 80 % des stations du panel
COUPURE_PART_STATIONS = 0.80  # date commune de l'année en cours : atteinte par au moins 80 % des stations qui ont des relevés
REFERENCE = (1991, 2020)


def fnum(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if v == v and abs(v) != float("inf") else None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def lire_json_gz(chemin: Path) -> Any:
    try:
        return json.loads(gzip.decompress(chemin.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, EOFError):
        return None


# ———— Catalogue ————


def charger_catalogue(racine: Path) -> List[dict]:
    doc = lire_json_gz(racine / "stations.json.gz")
    stations = (doc or {}).get("stations") if isinstance(doc, dict) else None
    if not isinstance(stations, list) or not stations:
        raise RuntimeError("catalogue stations.json.gz introuvable ou vide")
    return stations


def stations_retenues(catalogue: Iterable[dict]) -> List[dict]:
    """En activité, au moins ANNEES_MIN années de relevés, coordonnées connues."""
    return [s for s in catalogue
            if s.get("active") and len(s.get("years") or []) >= ANNEES_MIN and fnum(s.get("lat")) is not None and fnum(s.get("lon")) is not None]


# ———— Comptage par station et par année ————


def resume_annee(jours: List[dict], detail: bool = False) -> dict:
    """{jours, valides, nuits, fin, tropicales : [mois * 100 + jour des nuits tropicales]} d'un fichier annuel ;
    `detail` ajoute valides_md (mois * 100 + jour de chaque jour avec une minimale), pour juger la couverture jusqu'à une date."""
    valides = nuits = 0
    tropicales: List[int] = []
    valides_md: List[int] = []
    fin = None
    for j in jours:
        d = j.get("date")
        bon = isinstance(d, str) and len(d) == 10
        if bon:
            fin = d if fin is None or d > fin else fin
        tn = fnum(j.get("tn"))
        if tn is None or not bon:
            continue
        valides += 1
        md = int(d[5:7]) * 100 + int(d[8:10])
        if detail:
            valides_md.append(md)
        if tn >= SEUIL_C:
            nuits += 1
            tropicales.append(md)
    out = {"jours": len(jours), "valides": valides, "nuits": nuits, "fin": fin, "tropicales": tropicales}
    if detail:
        out["valides_md"] = valides_md
    return out


def analyser_station(racine: Path, s: dict, annee_courante: int) -> Dict[int, dict]:
    par_an: Dict[int, dict] = {}
    for an in s.get("years") or []:
        doc = lire_json_gz(racine / "stations" / str(s["num_poste"]) / f"{an}.json.gz")
        jours = (doc or {}).get("days") if isinstance(doc, dict) else None
        if isinstance(jours, list) and jours:
            par_an[int(an)] = resume_annee(jours, detail=int(an) == annee_courante)
    return par_an


def annee_valide(r: Optional[dict]) -> bool:
    """Année complète d'une station : au moins COUVERTURE_MIN des jours du fichier ont une minimale."""
    return bool(r) and r["jours"] > 0 and r["valides"] >= COUVERTURE_MIN * r["jours"]


def nuits_jusqu_a(r: dict, md: int) -> int:
    return sum(1 for x in r["tropicales"] if x <= md)


def comptage_courant(r: Optional[dict], annee: int, limite: date) -> Optional[dict]:
    """Année en cours jusqu'à `limite` (incluse) : {nuits, jours, fin} ; None si les relevés s'arrêtent avant `limite` ou si moins de
    COUVERTURE_MIN des jours écoulés ont une minimale."""
    if not r or not r["fin"] or "valides_md" not in r or date.fromisoformat(r["fin"]) < limite:
        return None
    md = limite.month * 100 + limite.day
    ecoules = (limite - date(annee, 1, 1)).days + 1
    valides = sum(1 for x in r["valides_md"] if x <= md)
    if ecoules <= 0 or valides < COUVERTURE_MIN * ecoules:
        return None
    return {"nuits": nuits_jusqu_a(r, md), "jours": valides, "fin": limite.isoformat()}


# ———— Indicateur national ————


def moyenne(valeurs: List[float]) -> Optional[float]:
    return statistics.mean(valeurs) if valeurs else None


def construire(racine: Path, catalogue: List[dict], now: datetime) -> Tuple[dict, dict]:
    retenues = stations_retenues(catalogue)
    derniers = [s["last_date"] for s in retenues if isinstance(s.get("last_date"), str)]
    if not derniers:
        raise RuntimeError("catalogue sans date de dernier relevé")
    annee_courante = date.fromisoformat(max(derniers)).year
    derniere_complete = annee_courante - 1
    analyses: Dict[str, Dict[int, dict]] = {s["num_poste"]: analyser_station(racine, s, annee_courante) for s in retenues}

    # fin des données et date commune de comparaison de l'année en cours (atteinte par au moins 80 % des stations qui ont des relevés)
    fins = sorted(par_an[annee_courante]["fin"] for par_an in analyses.values() if par_an.get(annee_courante) and par_an[annee_courante]["fin"])
    if not fins:
        raise RuntimeError("aucun relevé de l'année en cours lu")
    fin_donnees = date.fromisoformat(fins[-1])
    coupure = date.fromisoformat(fins[int(len(fins) * (1 - COUPURE_PART_STATIONS))])
    md_coupure = coupure.month * 100 + coupure.day
    courants: Dict[str, Optional[dict]] = {}
    derniers_releves: Dict[str, dict] = {}
    for sid, par_an in analyses.items():
        r = par_an.get(annee_courante)
        courants[sid] = comptage_courant(r, annee_courante, coupure)
        if r and r["fin"]:
            fin = date.fromisoformat(r["fin"])
            if fin > coupure and (fin_donnees - fin).days <= ECART_FIN_MAX_J:
                dernier = comptage_courant(r, annee_courante, fin)
                if dernier:
                    derniers_releves[sid] = dernier

    # panel : stations de basse altitude dont presque toutes les années depuis PANEL_DEPUIS sont valides (aucun critère sur l'année en cours)
    annees_panel = list(range(PANEL_DEPUIS, derniere_complete + 1))
    panel: List[dict] = []
    for s in retenues:
        alt = fnum(s.get("alti"))
        if alt is None or alt > PANEL_ALTITUDE_MAX_M:
            continue
        par_an = analyses[s["num_poste"]]
        if sum(1 for a in annees_panel if annee_valide(par_an.get(a))) >= PANEL_PART_ANNEES * len(annees_panel):
            panel.append(s)
    ids_panel = {s["num_poste"] for s in panel}

    # moyenne annuelle du panel
    moyennes: Dict[int, float] = {}
    for a in annees_panel:
        valeurs = [analyses[s["num_poste"]][a]["nuits"] for s in panel if annee_valide(analyses[s["num_poste"]].get(a))]
        if panel and len(valeurs) >= PANEL_PART_STATIONS * len(panel):
            moyennes[a] = statistics.mean(valeurs)
    ref_annees = [a for a in range(REFERENCE[0], REFERENCE[1] + 1) if a in moyennes]
    reference = moyenne([moyennes[a] for a in ref_annees])
    annees_out: List[dict] = []
    for a in sorted(moyennes):
        annees_out.append({"annee": a, "moyenne": round(moyennes[a], 2),
                           "ecart": round(moyennes[a] - reference, 2) if reference is not None else None,
                           "stations": sum(1 for s in panel if annee_valide(analyses[s["num_poste"]].get(a)))})
    decennies = []
    for debut in range(PANEL_DEPUIS // 10 * 10, derniere_complete + 1, 10):
        valeurs = [moyennes[a] for a in range(debut, debut + 10) if a in moyennes]
        if valeurs:
            decennies.append({"debut": debut, "fin": min(debut + 9, derniere_complete), "annees": len(valeurs), "moyenne": round(statistics.mean(valeurs), 2)})

    # année en cours, comparée à la même période des années de référence
    courante: Dict[str, Any] = {"annee": annee_courante, "jusqu_au": coupure.isoformat(), "dernier_releve": fin_donnees.isoformat(),
                                "moyenne": None, "stations": None, "reference_a_ce_jour": None, "ecart": None}
    valeurs_courantes = [courants[s["num_poste"]]["nuits"] for s in panel if courants[s["num_poste"]]]
    if panel and len(valeurs_courantes) >= PANEL_PART_STATIONS * len(panel):
        ref_par_an = []
        for a in ref_annees:
            v = [nuits_jusqu_a(analyses[s["num_poste"]][a], md_coupure) for s in panel if annee_valide(analyses[s["num_poste"]].get(a))]
            if len(v) >= PANEL_PART_STATIONS * len(panel):
                ref_par_an.append(statistics.mean(v))
        ref_a_ce_jour = moyenne(ref_par_an)
        courante.update({
            "moyenne": round(statistics.mean(valeurs_courantes), 2), "stations": len(valeurs_courantes),
            "reference_a_ce_jour": round(ref_a_ce_jour, 2) if ref_a_ce_jour is not None else None,
            "ecart": round(statistics.mean(valeurs_courantes) - ref_a_ce_jour, 2) if ref_a_ce_jour is not None else None,
        })
    classement = sorted(({"annee": a, "moyenne": round(m, 2)} for a, m in moyennes.items()), key=lambda x: -x["moyenne"])[:10]

    indicateur = {
        "schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID, "status": "ok", "generated_at": iso(now),
        "seuil_c": SEUIL_C, "reference": {"debut": REFERENCE[0], "fin": REFERENCE[1], "moyenne": round(reference, 2) if reference is not None else None},
        "panel": {"stations": len(panel), "ids": sorted(ids_panel),
                  "criteres": {"altitude_max_m": PANEL_ALTITUDE_MAX_M, "depuis": PANEL_DEPUIS, "part_annees_valides": PANEL_PART_ANNEES,
                               "couverture_min": COUVERTURE_MIN, "part_stations_par_annee": PANEL_PART_STATIONS}},
        "annees": annees_out, "decennies": decennies, "plus_fortes_annees": classement, "courante": courante,
    }

    # stations : une ligne par station, comptage de chaque année (None si l'année n'est pas assez complète)
    lignes = []
    for s in sorted(retenues, key=lambda x: (x.get("departement") or "", x.get("nom") or "", x["num_poste"])):
        sid = s["num_poste"]
        par_an = analyses[sid]
        annees_ok = [a for a in par_an if a <= derniere_complete]
        if not annees_ok:
            continue
        y0 = min(annees_ok)
        comptes = [par_an[a]["nuits"] if a in par_an and annee_valide(par_an[a]) else None for a in range(y0, derniere_complete + 1)]
        if not any(c is not None for c in comptes):
            continue
        ligne: Dict[str, Any] = {"id": sid, "nom": s.get("nom"), "dep": s.get("departement"), "lat": round(float(s["lat"]), 4),
                                 "lon": round(float(s["lon"]), 4), "alt": fnum(s.get("alti")), "y0": y0, "n": comptes}
        if sid in ids_panel:
            ligne["panel"] = True
        if courants[sid]:
            ligne["courante"] = courants[sid]
        if sid in derniers_releves:
            ligne["derniere"] = derniers_releves[sid]
        lignes.append(ligne)
    stations = {"schema_version": SCHEMA_VERSION, "module_version": VERSION, "build_id": BUILD_ID, "status": "ok", "generated_at": iso(now),
                "seuil_c": SEUIL_C, "annee_courante": annee_courante, "derniere_annee_complete": derniere_complete, "jusqu_au": coupure.isoformat(),
                "dernier_releve": fin_donnees.isoformat(), "couverture_min": COUVERTURE_MIN, "stations": lignes}
    return stations, indicateur


def ecrire_json(chemin: Path, data: Any) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(chemin.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    tmp.replace(chemin)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--climato-dir", required=True, help="dossier local de la branche data du dépôt climato (stations.json.gz et stations/)")
    ap.add_argument("--sortie", default="nuits_tropicales", help="dossier de sortie")
    ap.add_argument("--liste-stations", action="store_true", help="affiche les postes retenus (un par ligne) pour préparer le clone partiel, sans rien calculer")
    ap.add_argument("--maintenant", help="instant ISO UTC (essais)")
    args = ap.parse_args(argv)
    racine = Path(args.climato_dir)
    catalogue = charger_catalogue(racine)
    if args.liste_stations:
        for s in stations_retenues(catalogue):
            print(s["num_poste"])
        return 0
    now = datetime.fromisoformat(args.maintenant.replace("Z", "+00:00")) if args.maintenant else datetime.now(timezone.utc)
    stations, indicateur = construire(racine, catalogue, now)
    sortie = Path(args.sortie)
    ecrire_json(sortie / "stations.json", stations)
    ecrire_json(sortie / "indicateur.json", indicateur)
    c = indicateur["courante"]
    print(f"Stations retenues : {len(stations['stations'])} | panel : {indicateur['panel']['stations']} | années de l'indicateur : {len(indicateur['annees'])}")
    print(f"Référence 1991-2020 : {indicateur['reference']['moyenne']} nuits par station et par an | décennies :",
          ", ".join(f"{d['debut']}s {d['moyenne']}" for d in indicateur["decennies"]))
    print(f"Année {c['annee']} au {c.get('jusqu_au')} (dernier relevé : {c.get('dernier_releve')}) : {c.get('moyenne')} nuits par station "
          f"(même période 1991-2020 : {c.get('reference_a_ce_jour')}, écart {c.get('ecart')}) sur {c.get('stations')} stations du panel")
    print("Stations comptées à la date commune :", sum(1 for x in stations["stations"] if x.get("courante")), "| avec un dernier relevé plus récent :",
          sum(1 for x in stations["stations"] if x.get("derniere")))
    print("Années les plus chaudes :", ", ".join(f"{x['annee']} ({x['moyenne']})" for x in indicateur["plus_fortes_annees"][:5]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERREUR FATALE :", exc, file=sys.stderr)
        raise

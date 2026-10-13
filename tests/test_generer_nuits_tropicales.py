#!/usr/bin/env python3
"""Tests de generer_nuits_tropicales.py : stations retenues, comptage par année, couverture, panel, indicateur, année en cours.
Aucun appel réseau : le dépôt climato est simulé dans un dossier temporaire.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import gzip
import importlib.util
import json
import tempfile

MODULE_PATH = Path(__file__).resolve().parents[1] / "generer_nuits_tropicales.py"
SPEC = importlib.util.spec_from_file_location("nuits_tropicales", MODULE_PATH)
nt = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(nt)

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)


def ecrire_gz(chemin: Path, doc) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(gzip.compress(json.dumps(doc).encode("utf-8")))


def jours_annee(annee, nuits, valides=None, fin=None, nuits_octobre=0):
    """Fichier annuel : `nuits` nuits tropicales (tn 21) au début de juillet, `nuits_octobre` au début d'octobre, le reste à 12 °C ;
    `valides` = nombre de jours avec minimale (les suivants sont vides) ; `fin` = dernier jour du fichier."""
    jours = []
    d = date(annee, 1, 1)
    dernier = fin or date(annee, 12, 31)
    k_juillet = k_octobre = 0
    while d <= dernier:
        tn = 12.0
        if d.month == 7 and k_juillet < nuits:
            tn = 21.0
            k_juillet += 1
        if d.month == 10 and k_octobre < nuits_octobre:
            tn = 21.0
            k_octobre += 1
        jours.append({"date": d.isoformat(), "tx": 25.0, "tn": tn})
        d += timedelta(days=1)
    if valides is not None:
        for j in jours[valides:]:
            j["tn"] = None
    return jours


def station(poste, nom, alt, annees, actif=True, dep="66"):
    return {"num_poste": poste, "nom": nom, "departement": dep, "lat": 42.7, "lon": 2.9, "alti": alt, "years": list(annees),
            "first_date": f"{min(annees)}-01-01", "last_date": f"{max(annees)}-12-31", "has_normales": False, "active": actif}


def compte(annee):
    """Nuits tropicales de l'année pour une station de basse altitude : 1 par an avant 1970, 4 jusqu'en 1999, 10 depuis 2000."""
    return 1 if annee < 1970 else 4 if annee < 2000 else 10


CONTROLE = date(2026, 9, 22)  # la plupart des stations s'arrêtent là (données contrôlées en retard) ; P1 (principale) va jusqu'au 12 octobre
FIN = date(2026, 10, 12)

with tempfile.TemporaryDirectory() as tmp:
    racine = Path(tmp)
    annees = range(1945, 2027)
    catalogue = [
        station("A1", "BASSE 1", 50, annees),
        station("A2", "BASSE 2", 300, annees),
        station("P1", "PRINCIPALE", 80, annees),  # à jour jusqu'au 12 octobre
        station("H1", "HAUTE", 900, annees),  # altitude > 500 m : retenue mais hors panel
        station("S1", "STOP", 700, annees),  # dernier relevé de 2026 le 1er août : pas de comptage à la date commune
        station("I1", "INACTIVE", 50, annees, actif=False),
        station("C1", "COURTE", 50, range(2010, 2027)),  # moins de 30 années
        station("T1", "TROUS", 50, annees),  # 1960-1969 : minimales manquantes (couverture insuffisante)
    ]
    ecrire_gz(racine / "stations.json.gz", {"generated_at": "2026-10-13T00:00:00Z", "stations": catalogue})
    fins_2026 = {"P1": FIN, "S1": date(2026, 8, 1)}
    for s in catalogue:
        for an in s["years"]:
            poste = s["num_poste"]
            if an == 2026:
                fin = fins_2026.get(poste, CONTROLE)
                if poste in ("A1", "A2", "P1"):
                    ecrire_gz(racine / "stations" / poste / f"{an}.json.gz",
                              {"num_poste": poste, "year": an, "days": jours_annee(an, 30, fin=fin, nuits_octobre=3 if poste == "P1" else 0)})
                else:
                    ecrire_gz(racine / "stations" / poste / f"{an}.json.gz", {"num_poste": poste, "year": an, "days": jours_annee(an, 5, fin=fin)})
                continue
            valides = 100 if (poste == "T1" and 1960 <= an <= 1969) else None  # 100 jours sur 365 : année écartée
            ecrire_gz(racine / "stations" / poste / f"{an}.json.gz",
                      {"num_poste": poste, "year": an, "days": jours_annee(an, compte(an) if poste != "H1" else 0, valides=valides)})

    # --- stations retenues ---------------------------------------------------------------------------------------------------------
    retenues = {s["num_poste"] for s in nt.stations_retenues(nt.charger_catalogue(racine))}
    assert retenues == {"A1", "A2", "P1", "H1", "S1", "T1"}, retenues  # ni l'inactive, ni la station de moins de 30 ans

    # --- résumé d'une année -----------------------------------------------------------------------------------------------------------
    r = nt.resume_annee(jours_annee(2010, 10))
    assert r["jours"] == 365 and r["valides"] == 365 and r["nuits"] == 10 and r["fin"] == "2010-12-31" and r["tropicales"][0] == 701 and "valides_md" not in r, r
    assert nt.annee_valide(r) and not nt.annee_valide(nt.resume_annee(jours_annee(2010, 10, valides=100)))
    assert nt.nuits_jusqu_a(r, 705) == 5 and nt.nuits_jusqu_a(r, 1231) == 10  # jusqu'au 5 juillet, puis toute l'année

    # --- année en cours : comptage jusqu'à une date, couverture et fin des relevés --------------------------------------------------------
    cour = nt.resume_annee(jours_annee(2026, 30, fin=FIN, nuits_octobre=3), detail=True)
    assert nt.comptage_courant(cour, 2026, CONTROLE) == {"nuits": 30, "jours": 265, "fin": "2026-09-22"}
    assert nt.comptage_courant(cour, 2026, FIN) == {"nuits": 33, "jours": 285, "fin": "2026-10-12"}
    assert nt.comptage_courant(cour, 2026, date(2026, 10, 13)) is None, "relevés arrêtés avant la date demandée"
    creux = nt.resume_annee(jours_annee(2026, 30, fin=FIN, valides=150), detail=True)  # 150 jours sur 285 : couverture insuffisante
    assert nt.comptage_courant(creux, 2026, FIN) is None
    assert nt.comptage_courant(nt.resume_annee(jours_annee(2026, 30, fin=FIN)), 2026, FIN) is None, "sans détail des jours valides : non comptée"

    # --- calcul complet ---------------------------------------------------------------------------------------------------------------
    stations, indicateur = nt.construire(racine, nt.charger_catalogue(racine), NOW)
    assert stations["annee_courante"] == 2026 and stations["derniere_annee_complete"] == 2025, stations
    assert stations["jusqu_au"] == "2026-09-22" and stations["dernier_releve"] == "2026-10-12", (stations["jusqu_au"], stations["dernier_releve"])
    par_id = {s["id"]: s for s in stations["stations"]}
    assert set(par_id) == {"A1", "A2", "P1", "H1", "S1", "T1"}
    a1 = par_id["A1"]
    assert a1["y0"] == 1945 and len(a1["n"]) == 2025 - 1945 + 1 and a1["n"][0] == 1 and a1["n"][2025 - 1945] == 10, (a1["y0"], a1["n"][:3], a1["n"][-2:])
    assert a1["courante"] == {"nuits": 30, "jours": 265, "fin": "2026-09-22"} and a1.get("panel") is True and "derniere" not in a1, a1
    p1 = par_id["P1"]
    assert p1["courante"] == {"nuits": 30, "jours": 265, "fin": "2026-09-22"} and p1["derniere"] == {"nuits": 33, "jours": 285, "fin": "2026-10-12"}, p1
    t1 = par_id["T1"]
    assert t1["n"][1960 - 1945] is None and t1["n"][1969 - 1945] is None and t1["n"][1970 - 1945] == 4, "années à couverture insuffisante : non comptées"
    assert "panel" not in t1 and "panel" not in par_id["H1"] and "panel" not in par_id["S1"], "T1 : trop d'années invalides ; H1 et S1 : trop hautes"
    assert "courante" not in par_id["S1"], "S1 : relevés arrêtés avant la date commune"
    assert par_id["H1"]["n"][2020 - 1945] == 0 and par_id["H1"]["courante"]["nuits"] == 5

    # --- panel et indicateur ---------------------------------------------------------------------------------------------------------
    assert indicateur["panel"]["ids"] == ["A1", "A2", "P1"], indicateur["panel"]  # basses, séries complètes depuis 1950 (le panel ne dépend pas de l'année en cours)
    ann = {x["annee"]: x for x in indicateur["annees"]}
    assert min(ann) == 1950 and max(ann) == 2025 and ann[1950]["moyenne"] == 1.0 and ann[1985]["moyenne"] == 4.0 and ann[2020]["moyenne"] == 10.0 and ann[2020]["stations"] == 3
    # référence 1991-2020 : 9 années à 4 nuits (1991-1999) et 21 à 10 nuits (2000-2020)
    assert indicateur["reference"]["moyenne"] == round((9 * 4 + 21 * 10) / 30, 2), indicateur["reference"]
    assert ann[2020]["ecart"] == round(10 - (9 * 4 + 21 * 10) / 30, 2)
    dec = {d["debut"]: d for d in indicateur["decennies"]}
    assert dec[1950]["moyenne"] == 1.0 and dec[1990]["moyenne"] == 4.0 and dec[2020]["annees"] == 6 and dec[2020]["moyenne"] == 10.0, dec
    assert indicateur["plus_fortes_annees"][0]["moyenne"] == 10.0 and len(indicateur["plus_fortes_annees"]) == 10

    # --- année en cours, comparée à la même période des années de référence ---------------------------------------------------------
    c = indicateur["courante"]
    # les nuits de l'essai tombent en juillet : au 22 septembre, 2026 en compte 30 et chaque année de référence son total de 4 ou 10
    assert c["annee"] == 2026 and c["jusqu_au"] == "2026-09-22" and c["dernier_releve"] == "2026-10-12" and c["moyenne"] == 30.0 and c["stations"] == 3, c
    assert c["reference_a_ce_jour"] == round((9 * 4 + 21 * 10) / 30, 2) and c["ecart"] == round(30 - (9 * 4 + 21 * 10) / 30, 2), c

    # --- sorties -------------------------------------------------------------------------------------------------------------------------
    sortie = Path(tmp) / "sortie"
    assert nt.main(["--climato-dir", str(racine), "--sortie", str(sortie), "--maintenant", "2026-10-13T08:00:00Z"]) == 0
    s2 = json.loads((sortie / "stations.json").read_text(encoding="utf-8"))
    i2 = json.loads((sortie / "indicateur.json").read_text(encoding="utf-8"))
    assert s2["status"] == "ok" and i2["status"] == "ok" and s2["generated_at"] == "2026-10-13T08:00:00Z" and len(s2["stations"]) == 6
    assert (sortie / "stations.json").stat().st_size < 200_000

print("tests generer_nuits_tropicales : OK")

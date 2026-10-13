#!/usr/bin/env python3
"""Tests de generer_records_temperature.py : fiches des stations, Corse, fenêtres horaires, chaînage des records.

Aucun appel réseau : le script est chargé avec un faux module `requests`.
"""

from datetime import date, datetime, timezone
from pathlib import Path
import importlib.util
import sys
import types


class DummySession:
    def __init__(self):
        self.headers = {}


if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(Session=DummySession, RequestException=Exception)

MODULE_PATH = Path(__file__).resolve().parents[1] / "generer_records_temperature.py"
SPEC = importlib.util.spec_from_file_location("records_temperature", MODULE_PATH)
rt = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(rt)

# --- Corse : le jeu quotidien la range sous « 20 » (pas 2A / 2B) --------------------------------------------------
assert len(rt.DEPARTMENTS) == 95, len(rt.DEPARTMENTS)
assert "20" in rt.DEPARTMENTS and "2A" not in rt.DEPARTMENTS and "2B" not in rt.DEPARTMENTS
assert rt.REGION_BY_DEP["20"] == "Corse"
assert rt.department_of_station("20004002") == "20"
assert rt.department_of_station("66136001") == "66"

# --- Fiches des stations (METADONNEES_STATION/fiches.json) : année d'ouverture, type de poste, réseau principal ----
FICHES = [
    {   # station ouverte, type 0, plusieurs positions : on garde celle sans date de fin
        "id": "66136001", "nom": "PERPIGNAN", "dateDebut": "1924-11-01 00:00:00", "dateFin": "",
        "typesPoste": [{"dateFin": "", "dateDebut": "1924-11-01 00:00:00", "type": 0}],
        "positions": [
            {"latitude": 42.737, "longitude": 2.873, "altitude": 42, "dateDebut": "1924-11-01 00:00:00", "dateFin": "1943-03-31 00:00:00"},
            {"latitude": 42.7371, "longitude": 2.8728, "altitude": 44, "dateDebut": "1944-01-01 00:00:00", "dateFin": ""},
        ],
    },
    {   # type de poste qui change : 4 -> 3 -> 2 ; le type en vigueur est le dernier, sans date de fin
        "id": "25254001", "nom": "LES FOURGS", "dateDebut": "1956-07-01 00:00:00", "dateFin": "",
        "typesPoste": [
            {"dateFin": "2021-07-08 00:00:00", "dateDebut": "1956-07-01 00:00:00", "type": 4},
            {"dateFin": "2021-09-13 00:00:00", "dateDebut": "2021-07-09 00:00:00", "type": 3},
            {"dateFin": "", "dateDebut": "2021-09-14 00:00:00", "type": 2},
        ],
        "positions": [{"latitude": 46.834, "longitude": 6.4365, "altitude": 1140, "dateDebut": "2021-07-09 00:00:00", "dateFin": ""}],
    },
    {   # station fermée, type 4 : hors réseau principal
        "id": "20002001", "nom": "AGHIONE", "dateDebut": "2009-10-20 00:00:00", "dateFin": "2020-01-01 00:00:00",
        "typesPoste": [{"dateFin": "2020-01-01 00:00:00", "dateDebut": "2009-10-20 00:00:00", "type": 4}],
        "positions": [],
    },
    {"id": "", "nom": "SANS IDENTIFIANT"},
    "pas une fiche",
]
meta = rt.parse_station_fiches(FICHES)
assert set(meta) == {"66136001", "25254001", "20002001"}, sorted(meta)
assert meta["66136001"]["opened"] == "1924-11-01" and meta["66136001"]["opening_year"] == 1924
assert meta["66136001"]["type_station"] == 0 and meta["66136001"]["principal"] is True
assert meta["66136001"]["altitude"] == 44 and meta["66136001"]["closed"] is None
assert meta["25254001"]["type_station"] == 2 and meta["25254001"]["principal"] is True
assert meta["20002001"]["type_station"] == 4 and meta["20002001"]["principal"] is False
assert meta["20002001"]["closed"] == "2020-01-01" and meta["20002001"]["altitude"] is None
assert rt.parse_station_fiches(None) == {} and rt.parse_station_fiches({"x": 1}) == {}

# --- Fenêtres horaires : TX de 6 h à 6 h UTC, TN de 18 h la veille à 18 h UTC ---------------------------------------
def h(day, hour):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)

assert rt.window_dates(h(13, 5)) == ("2026-10-12", "2026-10-13")  # avant 6 h : la TX de la veille est encore ouverte
assert rt.window_dates(h(13, 6)) == ("2026-10-13", "2026-10-13")
assert rt.window_dates(h(13, 17)) == ("2026-10-13", "2026-10-13")
assert rt.window_dates(h(13, 18)) == ("2026-10-13", "2026-10-14")  # à 18 h, la nuit suivante commence
assert rt.window_dates(h(13, 0)) == ("2026-10-12", "2026-10-13")

def row(sid, t=None, tx=None, tn=None):
    r = {"geo_id_insee": sid}
    if t is not None: r["t"] = round(t + 273.15, 2)  # DPPaquetObs : kelvins
    if tx is not None: r["tx"] = round(tx + 273.15, 2)
    if tn is not None: r["tn"] = round(tn + 273.15, 2)
    return r

days = {}
known = {"66136001", "11144001"}
rt.accumulate_hour(days, h(13, 4), [row("66136001", t=10.0, tx=10.5, tn=9.6), row("11144001", t=12.0), row("99999999", t=1.0)], known)
rt.accumulate_hour(days, h(13, 12), [row("66136001", t=21.0, tx=21.8, tn=20.9)], known)
rt.accumulate_hour(days, h(13, 20), [row("66136001", t=14.0)], known)
# 4 h : TX du 12 (10,5), TN du 13 (9,6) ; 12 h : TX du 13 (21,8), TN du 13 (min inchangé) ; 20 h : TX du 13 (max inchangé), TN du 14 (14,0)
assert days["2026-10-12"]["66136001"][0] == 10.5
assert days["2026-10-13"]["66136001"] == [21.8, 9.6], days["2026-10-13"]["66136001"]
assert days["2026-10-14"]["66136001"] == [None, 14.0]
assert days["2026-10-12"]["11144001"][0] == 12.0  # TX/TN absents : la température instantanée les remplace
assert "99999999" not in days["2026-10-12"] and "99999999" not in days.get("2026-10-13", {})

# --- Contrôle de plausibilité des relevés horaires -------------------------------------------------------------------------------
# Relevés réels du 12-13 octobre 2026 de La Chiappa (20247001), capteur défaillant : 0,0 °C en plein après-midi, -8,4 °C pour 22 °C la même heure.
for t, tx, tn in ((21.3, 21.5, 0.0), (17.7, 20.5, -0.7), (-8.4, 22.1, -8.5), (4.5, 22.6, -8.5)):
    rejet = rt.check_hour(t, tx, tn, 21.2)
    assert rejet[:3] == (None, None, None) and rejet[3], (t, tx, tn, rejet)
# relevés plausibles : conservés tels quels
assert rt.check_hour(21.3, 21.5, 21.0, 21.2) == (21.3, 21.5, 21.0, [])
assert rt.check_hour(12.3, 12.0, 10.0, 11.0) == (12.3, 12.0, 10.0, [])  # t à 0,3 °C de tx : tolérance d'arrondi
# réchauffement rapide en fin de nuit d'inversion : tn de 3,7 °C pour t de 11,0 °C, appuyé par l'heure précédente (3,8 °C)
assert rt.check_hour(11.0, 11.0, 3.7, 3.8) == (11.0, 11.0, 3.7, [])
# sans heure précédente, une seule température ne permet pas de juger : seul le contrôle interne de la ligne s'applique
assert rt.check_hour(11.0, 11.0, 3.7) == (11.0, 11.0, 3.7, [])
# refroidissement d'orage (37,5 °C puis 25,2 °C ; extrêmes de l'heure 38,0 et 24,0) : 14 °C d'écart dans l'heure, plausible
assert rt.check_hour(25.2, 38.0, 24.0, 37.5) == (25.2, 38.0, 24.0, [])
# lignes incohérentes : tx < tn, t hors de [tn, tx], valeurs hors bornes, plus de 15 °C d'écart dans l'heure
assert rt.check_hour(10.0, 8.0, 10.0)[:3] == (None, None, None)
assert rt.check_hour(15.0, 12.0, 10.0)[:3] == (None, None, None)
hors_bornes = rt.check_hour(80.0, 80.5, 79.0)
assert hors_bornes[:3] == (None, None, None) and len(hors_bornes[3]) == 3, hors_bornes
assert rt.check_hour(20.0, 28.0, 12.5)[:3] == (None, None, None)  # 15,5 °C d'écart
assert rt.check_hour(20.0, 27.0, 12.5)[:3] == (20.0, 27.0, 12.5)  # 14,5 °C d'écart
# saut de la température instantanée : plus de 15 °C d'une heure à la suivante
assert rt.check_hour(30.0, 30.5, 29.5, 10.0)[:3] == (None, None, None)
# extrême sans appui (à plus de 6 °C des deux températures voisines) : écartée seule, le reste de la ligne est conservé
res = rt.check_hour(15.0, 15.2, 4.0, 14.5)
assert res[:3] == (15.0, 15.2, None) and len(res[3]) == 1, res
res = rt.check_hour(15.0, 25.0, 14.8, 14.5)
assert res[:3] == (15.0, None, 14.8) and len(res[3]) == 1, res

# accumulation : la ligne aberrante n'altère pas les extrêmes du jour et ne sert pas de référence à l'heure suivante
jours, rejetes = {}, []
prec = rt.accumulate_hour(jours, h(13, 8), [row("66136001", t=20.0, tx=20.3, tn=19.8)], known, None, rejetes)
assert prec == {"66136001": 20.0} and not rejetes
prec = rt.accumulate_hour(jours, h(13, 9), [row("66136001", t=-8.4, tx=22.1, tn=-8.5)], known, prec, rejetes)
assert prec == {} and [r["id"] for r in rejetes] == ["66136001"] and rejetes[0]["raw"] == [-8.4, 22.1, -8.5], rejetes
assert jours["2026-10-13"]["66136001"] == [20.3, 19.8], jours
# extrême sans appui remplacée par t (valeur de secours)
jours, rejetes = {}, []
rt.accumulate_hour(jours, h(13, 8), [row("66136001", t=15.0, tx=15.2, tn=4.0)], known, {"66136001": 14.5}, rejetes)
assert jours["2026-10-13"]["66136001"] == [15.2, 15.0] and len(rejetes) == 1, jours

# plusieurs heures : la plus ancienne ne sert que de référence ; l'heure douteuse (7 h : tn 9,5 °C sous t, 9,7 °C d'écart dans l'heure) est corrigée
paquets = {h(13, 5): [row("66136001", t=8.0, tx=8.2, tn=7.8)], h(13, 6): [row("66136001", t=8.0, tx=8.3, tn=7.7)],
           h(13, 7): [row("66136001", t=8.5, tx=8.7, tn=-1.0)], h(13, 8): [row("66136001", t=9.0, tx=9.2, tn=8.6)]}
jours = {}
rej = rt.accumulate_packets(jours, paquets, known, h(13, 6))
assert jours["2026-10-13"]["66136001"] == [9.2, 7.7], jours  # le -1,0 °C n'y est pas
assert "2026-10-12" not in jours, jours  # 5 h : référence seule
assert len(rej) == 1 and rej[0]["hour"] == "2026-10-13T07:00:00Z", rej
# passage suivant, la fenêtre a glissé : l'heure douteuse est la plus ancienne lue, elle ne sert que de référence (même verdict, aucune fuite)
jours = {}
rt.accumulate_packets(jours, {k: v for k, v in paquets.items() if k >= h(13, 7)}, known, h(13, 6))
assert jours["2026-10-13"]["66136001"] == [9.2, 8.6], jours
# heure manquante : la chaîne de références est interrompue, l'heure suivante est retenue avec le seul contrôle interne
jours = {}
rt.accumulate_packets(jours, {h(13, 6): paquets[h(13, 6)], h(13, 8): paquets[h(13, 8)]}, known, h(13, 6))
assert jours["2026-10-13"]["66136001"] == [9.2, 8.6], jours

# état antérieur au contrôle : le jour en cours est reconstruit, les jours précédents sont gardés
etat_ancien = {"days": {"2026-10-13": {"20247001": [22.1, -8.5]}, "2026-10-12": {"20247001": [21.5, 17.0]}}}
jp = {d: dict(v) for d, v in etat_ancien["days"].items()}
assert rt.reset_unchecked_day(etat_ancien, jp, date(2026, 10, 13)) is True and set(jp) == {"2026-10-12"}, jp
etat_ancien["qc_version"] = rt.QC_VERSION
jp = {d: dict(v) for d, v in etat_ancien["days"].items()}
assert rt.reset_unchecked_day(etat_ancien, jp, date(2026, 10, 13)) is False and set(jp) == {"2026-10-12", "2026-10-13"}, jp
assert rt.reset_unchecked_day(None, {}, date(2026, 10, 13)) is False
assert rt.quality_summary([{"id": "B"}, {"id": "A"}, {"id": "A"}]) == {"version": rt.QC_VERSION, "rejected_rows": 3, "rejected_stations": ["A", "B"]}

# --- Couche récente : les records d'un même mois s'enchaînent ----------------------------------------------------------
def station():
    return {
        "id": "S1", "name": "STATION", "department_code": "66", "region": "Occitanie", "lat": 42.0, "lon": 2.0, "altitude": 100.0,
        "opened": None, "opening_year": None, "type_station": None, "principal": False,
        "heat": {"absolute": [{"date": "2019-06-28", "value": 42.0}],
                 "monthly": {"10": [{"date": "1985-10-02", "value": 33.0}]},
                 "fortnight": {"10-1": [{"date": "1985-10-02", "value": 33.0}]},
                 "daily": {"10-05": [{"date": "1990-10-05", "value": 30.0}]}},
        "cold": {"absolute": [{"date": "1956-02-10", "value": -15.0}],
                 "monthly": {"10": [{"date": "1974-10-28", "value": -2.0}]},
                 "fortnight": {"10-2": [{"date": "1974-10-28", "value": -2.0}]},
                 "daily": {"10-20": [{"date": "1980-10-20", "value": 1.0}]}},
        "tropical": {"absolute": [], "monthly": {}, "fortnight": {}, "daily": {}},
    }

st = station()
d5 = date(2026, 10, 5)
sans = rt.classify(st, d5, 34.0, "heat")
assert "monthly" in sans["flags"] and "fortnight" in sans["flags"] and "daily" in sans["flags"], sans  # ce que faisait l'ancienne version
layer = rt.RecentLayer({"2026-10-03": {"S1": [35.0, 12.0]}, "2026-10-04": {"S1": [31.0, 11.0]}})
avec = rt.classify(st, d5, 34.0, "heat", layer)
assert "monthly" not in avec["flags"] and "fortnight" not in avec["flags"], avec  # 35,0 relevés le 3 octobre : 34,0 n'est plus un record du mois
assert "daily" in avec["flags"], avec  # le record du 5 octobre se joue entre années, pas entre jours
assert avec["records"]["monthly"] == {"value": 35.0, "date": "2026-10-03"}, avec["records"]
assert "monthly" in avec["near"], avec  # à 1 °C du (nouveau) record du mois
# valeur supérieure au record récent : record battu, ancien record = celui du 3 octobre
sup = rt.classify(st, d5, 35.6, "heat", layer)
assert "monthly" in sup["flags"] and sup["records"]["monthly"]["date"] == "2026-10-03"
# jours à partir de la date évaluée : ignorés (une archive ne regarde jamais le futur)
tard = rt.RecentLayer({"2026-10-06": {"S1": [40.0, 12.0]}})
assert "monthly" in rt.classify(st, d5, 34.0, "heat", tard)["flags"]
# égalité : le record le plus ancien est conservé
assert rt.better_ref({"value": 33.0, "date": "1985-10-02"}, {"value": 33.0, "date": "2026-10-03"}, "max")["date"] == "1985-10-02"
assert rt.better_ref({"value": -2.0, "date": "1974-10-28"}, {"value": -3.1, "date": "2026-10-10"}, "min")["date"] == "2026-10-10"
assert rt.better_ref(None, {"value": 1.0, "date": "2026-10-01"}, "max")["value"] == 1.0
# froid : la TN la plus basse du mois compte, la deuxième quinzaine a sa propre série
cold_layer = rt.RecentLayer({"2026-10-16": {"S1": [20.0, -3.0]}, "2026-10-04": {"S1": [20.0, -9.0]}})
froid = rt.classify(st, date(2026, 10, 25), -2.5, "cold", cold_layer)
assert "monthly" not in froid["flags"], froid  # -9,0 relevés le 4 octobre
assert "fortnight" not in froid["flags"], froid  # -3,0 relevés le 16 (même quinzaine)
assert froid["records"]["fortnight"] == {"value": -3.0, "date": "2026-10-16"}, froid["records"]
assert froid["records"]["monthly"] == {"value": -9.0, "date": "2026-10-04"}, froid["records"]

# --- Valeurs contrôlées prioritaires sur les valeurs provisoires ---------------------------------------------------------
fusion = rt.merge_days({"2026-10-10": {"S1": [30.0, 10.0]}}, {"2026-10-10": {"S1": [99.0, 99.0], "S2": [20.0, 5.0]}, "2026-10-11": {"S1": [31.0, 11.0]}})
assert fusion["2026-10-10"]["S1"] == [30.0, 10.0] and fusion["2026-10-10"]["S2"] == [20.0, 5.0] and "2026-10-11" in fusion

# --- Métadonnées à jour appliquées aux événements (les caches, figés, n'ont ni réseau principal ni année d'ouverture) ----
stations = {"S1": station()}
meta_live = {"S1": {"opened": "1956-07-01", "opening_year": 1956, "type_station": 2, "principal": True, "altitude": None, "lat": None, "lon": None}}
payload = rt.make_day_payload(d5, {"S1": {"tx": 34.0, "tn": 5.0}}, stations, provisional=False, source="test", meta=meta_live, recent=layer)
ev = payload["events"]["heat"][0]
assert ev["principal"] is True and ev["opening_year"] == 1956 and ev["type_station"] == 2 and ev["opened"] == "1956-07-01"
assert ev["altitude"] == 100.0  # l'altitude du cache n'est pas écrasée
payload_sans_meta = rt.make_day_payload(d5, {"S1": {"tx": 34.0, "tn": 5.0}}, stations, provisional=False, source="test")
assert payload_sans_meta["events"]["heat"][0]["principal"] is False and payload_sans_meta["events"]["heat"][0]["opening_year"] is None
assert payload["counts"]["heat"] == 1 and payload["stations_with_values"] == 1 and payload["fortnight_label"] == "1re quinzaine"

# --- Nuits tropicales : TN d'au moins 20 °C seulement --------------------------------------------------------------------
trop = station()
trop["tropical"] = {"absolute": [{"date": "2003-08-12", "value": 25.0}], "monthly": {"10": [{"date": "1997-10-11", "value": 23.0}]},
                    "fortnight": {"10-1": [{"date": "1997-10-11", "value": 23.0}]}, "daily": {"10-13": [{"date": "2003-10-13", "value": 20.0}]}}
assert rt.event_from_value(trop, date(2026, 10, 13), 19.9, "tropical") is None
e = rt.event_from_value(trop, date(2026, 10, 13), 20.5, "tropical")
assert e and "daily" in e["flags"] and e["records"]["daily"]["value"] == 20.0

# --- Dernier jour contrôlé : au moins la moitié des stations du jour le mieux fourni --------------------------------------
jours = {"2026-10-09": {str(i): [1, 1] for i in range(100)}, "2026-10-10": {str(i): [1, 1] for i in range(98)}, "2026-10-11": {"1": [1, 1], "2": [1, 1]}}
assert rt.last_complete_day(jours) == "2026-10-10"
assert rt.last_complete_day({}) is None

# --- Références pour les records menacés : seules les stations qui ont une valeur récente sont suivies ----------------------------
cache_ref = {"S1": dict(station(), id="S1"), "S2": dict(station(), id="S2", lat=None, lon=None)}
frais = rt.RecentLayer({"2026-10-12": {"S1": [20.0, 8.0], "S2": [20.0, 8.0]}})
ancien_layer = rt.RecentLayer({"2026-10-01": {"S1": [20.0, 8.0]}})
r1 = rt.build_references(cache_ref, None, frais, date(2026, 10, 13))
assert r1["days"] == ["2026-10-14", "2026-10-15", "2026-10-16"] and list(r1["stations"]) == ["S1"], r1  # S2 : sans coordonnées
assert rt.build_references(cache_ref, None, ancien_layer, date(2026, 10, 13))["stations"] == {}  # plus de valeur depuis plus de 3 jours
s1 = r1["stations"]["S1"]
assert s1["records"]["heat"]["absolute"] == [42.0, "2019-06-28"] and s1["name"] == "STATION" and s1["altitude"] == 100.0
assert s1["records"]["heat"]["days"]["2026-10-14"]["monthly"] == [33.0, "1985-10-02"]
assert s1["records"]["cold"]["days"]["2026-10-14"]["monthly"] == [-2.0, "1974-10-28"]
assert s1["records"]["cold"]["days"]["2026-10-14"]["fortnight"] == [8.0, "2026-10-12"]  # pas de record de 1re quinzaine dans le cache d'essai : la valeur récente en tient lieu
assert s1["records"]["cold"]["days"]["2026-10-16"]["fortnight"] == [-2.0, "1974-10-28"]
# une valeur récente plus forte devient le record à battre (mois, quinzaine) mais pas l'absolu ; le record du jour du calendrier est celui du jour visé
fort = rt.RecentLayer({"2026-10-13": {"S1": [36.0, 8.0]}})
r2 = rt.build_references(cache_ref, None, fort, date(2026, 10, 13))["stations"]["S1"]["records"]
assert r2["heat"]["days"]["2026-10-14"]["monthly"] == [36.0, "2026-10-13"], r2
assert r2["heat"]["days"]["2026-10-14"]["fortnight"] == [36.0, "2026-10-13"]  # le 13 est dans la même quinzaine que le 14
assert "fortnight" not in r2["heat"]["days"]["2026-10-15"]  # la 2e quinzaine n'a pas de record dans le cache d'essai
assert r2["heat"]["absolute"] == [42.0, "2019-06-28"]
assert "daily" not in r2["heat"]["days"]["2026-10-14"]  # le record du 14 octobre ne se joue pas avec une valeur du 13
# métadonnées à jour : réseau principal et année d'ouverture
meta_ref = {"S1": {"opened": "1956-07-01", "opening_year": 1956, "type_station": 2, "principal": True, "altitude": None, "lat": None, "lon": None}}
ref_meta = rt.build_references(cache_ref, meta_ref, frais, date(2026, 10, 13))["stations"]["S1"]
assert ref_meta["principal"] is True and ref_meta["opening_year"] == 1956

# --- LIVE de bout en bout (paquets horaires simulés) : jours provisoires, état conservé, couche récente ---------------
import os
import tempfile

import json as _json


def csv_hour(rows):
    lignes = ["geo_id_insee;t;tx;tn"] + [f"{sid};{t + 273.15:.2f};{tx + 273.15:.2f};{tn + 273.15:.2f}" for sid, t, tx, tn in rows]
    return ("\n".join(lignes) + "\n").encode()


def lancer_live(workdir, latest, through, forcer=None):
    os.chdir(workdir)
    out = Path("records_temperature")
    (out / "cache").mkdir(parents=True, exist_ok=True)
    cache = {"schema_version": 1, "module_version": "1.1.0", "build_id": "t", "department": "66", "generated_at": "2026-08-20T00:00:00Z",
             "stations": {"66136001": dict(station(), id="66136001", name="PERPIGNAN")}, "stations_total": 1, "rows_read": 0, "last_date": None}
    (out / "cache" / "dep-66.json").write_text(_json.dumps(cache), encoding="utf-8")
    (out / "cache" / "recent.json").write_text(_json.dumps({"schema_version": 1, "through": through, "days": {"2026-10-03": {"66136001": [35.0, 12.0]}}}), encoding="utf-8")

    def paquet(key, h):
        # le 12 octobre, pic de 34,0 °C à 14 h UTC (montée et descente de 4 °C par heure, comme dans la réalité) ; sinon 15 °C
        t = max(15.0, 34.0 - 4.0 * abs(h.hour - 14)) if h.day == 12 else 15.0
        return csv_hour([("66136001", *((forcer(h) if forcer else None) or (t, t + 0.5, t - 0.5)))])

    rt.PACKAGE_DELAY = 0
    rt.MIN_STATIONS_JOUR = 1  # une seule station dans ce jeu d'essai
    rt.api_key = lambda: "cle-de-test"
    rt.find_latest_hour = lambda key: (latest, paquet(key, latest))
    rt.package_request = paquet
    rt.generate_live()
    return out


with tempfile.TemporaryDirectory() as tmp:
    ancien = os.getcwd()
    try:
        out = lancer_live(tmp, datetime(2026, 10, 13, 5, tzinfo=timezone.utc), "2026-10-11")
        live = _json.loads((out / "live.json").read_text(encoding="utf-8"))
        assert live["day"]["date"] == "2026-10-13" and live["recent_through"] == "2026-10-11" and live["provisional_days"] == ["2026-10-12"], live["provisional_days"]
        assert live["coverage_target"] == 95 and live["coverage_departments"] == 1
        assert live["quality_control"] == {"version": rt.QC_VERSION, "rejected_rows": 0, "rejected_stations": []}, live["quality_control"]
        prov = _json.loads((out / "provisional" / "2026-10-12.json").read_text(encoding="utf-8"))
        chaud = prov["day"]["events"]["heat"]
        assert len(chaud) == 1 and chaud[0]["value"] == 34.5 and "monthly" not in chaud[0]["flags"], chaud  # 35,0 relevés le 3 octobre : pas un record du mois
        assert "daily" in chaud[0]["flags"] or "near" in chaud[0], chaud
        assert chaud[0]["provisional"] is True
        etat = _json.loads((out / "cache" / "provisional.json").read_text(encoding="utf-8"))
        assert etat["qc_version"] == rt.QC_VERSION
        assert set(etat["days"]) >= {"2026-10-12", "2026-10-13"}, etat["days"].keys()
        assert etat["days"]["2026-10-12"]["66136001"][0] == 34.5  # TX du 12 (fenêtre 6 h → 6 h UTC)
        index = _json.loads((out / "index.json").read_text(encoding="utf-8"))
        assert index["provisional_days"] == ["2026-10-12"] and index["latest_date"] == "2026-10-13" and index["recent_through"] == "2026-10-11"
        refs = _json.loads((out / "references.json").read_text(encoding="utf-8"))
        assert refs["days"] == ["2026-10-14", "2026-10-15", "2026-10-16"] and list(refs["stations"]) == ["66136001"], refs["days"]
        assert refs["stations"]["66136001"]["records"]["heat"]["days"]["2026-10-14"]["monthly"] == [35.0, "2026-10-03"]  # relevé du 3 octobre (couche récente)
        assert refs["stations"]["66136001"]["records"]["cold"]["absolute"] == [-15.0, "1956-02-10"]
        assert index["references_at"] == refs["generated_at"]
        premieres_refs = refs["generated_at"]
        # deuxième passage identique : le fichier du jour provisoire n'est pas réécrit
        premiere = prov["generated_at"]
        out = lancer_live(tmp, datetime(2026, 10, 13, 5, tzinfo=timezone.utc), "2026-10-11")
        assert _json.loads((out / "provisional" / "2026-10-12.json").read_text(encoding="utf-8"))["generated_at"] == premiere
        assert _json.loads((out / "references.json").read_text(encoding="utf-8"))["generated_at"] == premieres_refs  # contenu identique : pas réécrit
        # données contrôlées jusqu'au 12 : le jour provisoire disparaît (fichier et état)
        out = lancer_live(tmp, datetime(2026, 10, 13, 5, tzinfo=timezone.utc), "2026-10-12")
        assert not list((out / "provisional").glob("*.json"))
        assert "2026-10-12" not in _json.loads((out / "cache" / "provisional.json").read_text(encoding="utf-8"))["days"]
    finally:
        os.chdir(ancien)

# --- LIVE de bout en bout : capteur défaillant (tn de -3,0 °C pour 15 °C = record du mois si on le croyait) et état antérieur au contrôle ----------
with tempfile.TemporaryDirectory() as tmp:
    ancien = os.getcwd()
    try:
        latest_glitch = datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
        defaillant = lambda hh: (15.0, 15.5, -3.0) if (hh.day == 13 and hh.hour == 3) else None  # noqa: E731
        out = lancer_live(tmp, latest_glitch, "2026-10-11")  # premier passage : fichiers de base
        # état laissé par une version sans contrôle : tn de -3,0 °C déjà enregistrée pour le 13, et une autre pour le 12 (non relisible)
        (out / "cache" / "provisional.json").write_text(_json.dumps({"schema_version": 1, "generated_at": "2026-10-13T08:00:00Z", "days": {
            "2026-10-13": {"66136001": [15.5, -3.0]}, "2026-10-12": {"66136001": [34.5, 14.5]}}}), encoding="utf-8")
        out = lancer_live(tmp, latest_glitch, "2026-10-11", forcer=defaillant)
        live = _json.loads((out / "live.json").read_text(encoding="utf-8"))
        assert live["day"]["events"]["cold"] == [], live["day"]["events"]["cold"]  # ni l'ancienne valeur, ni la nouvelle ne deviennent un record
        assert live["quality_control"]["rejected_stations"] == ["66136001"] and live["quality_control"]["rejected_rows"] == 1, live["quality_control"]
        etat = _json.loads((out / "cache" / "provisional.json").read_text(encoding="utf-8"))
        assert etat["qc_version"] == rt.QC_VERSION
        assert etat["days"]["2026-10-13"]["66136001"][1] == 14.5, etat["days"]["2026-10-13"]  # jour reconstruit sans la valeur aberrante
        assert etat["days"]["2026-10-12"]["66136001"][0] == 34.5  # veille : conservée
        # les passages suivants n'effacent plus le jour en cours (état déjà contrôlé) mais gardent le verdict
        out = lancer_live(tmp, latest_glitch, "2026-10-11", forcer=defaillant)
        etat = _json.loads((out / "cache" / "provisional.json").read_text(encoding="utf-8"))
        assert etat["days"]["2026-10-13"]["66136001"][1] == 14.5, etat["days"]["2026-10-13"]
    finally:
        os.chdir(ancien)

# --- Reprise des heures manquantes : jours récents sans relevés horaires conservés ---------------------------------------------
rt.MIN_STATIONS_JOUR = 1  # une seule station dans ce jeu d'essai
assert rt.BACKFILL_MAX_HOURS == 30  # l'API ne garde qu'environ 24 h de paquets horaires
assert rt.uncovered_days({}, "2026-10-10", date(2026, 10, 13)) == ["2026-10-11", "2026-10-12"]
assert rt.uncovered_days({"2026-10-11": {"S1": [1, 1]}}, "2026-10-10", date(2026, 10, 13)) == ["2026-10-12"]
assert rt.uncovered_days({}, None, date(2026, 10, 13)) == ["2026-10-11", "2026-10-12"]
assert rt.uncovered_days({}, "2026-10-12", date(2026, 10, 13)) == []

with tempfile.TemporaryDirectory() as tmp:
    ancien = os.getcwd()
    try:
        os.chdir(tmp)
        out = Path("records_temperature")
        (out / "cache").mkdir(parents=True, exist_ok=True)
        cache = {"schema_version": 1, "module_version": "1.1.0", "build_id": "t", "department": "66", "generated_at": "2026-08-20T00:00:00Z",
                 "stations": {"66136001": dict(station(), id="66136001", name="PERPIGNAN")}, "stations_total": 1, "rows_read": 0, "last_date": None}
        (out / "cache" / "dep-66.json").write_text(_json.dumps(cache), encoding="utf-8")
        (out / "cache" / "recent.json").write_text(_json.dumps({"schema_version": 1, "through": "2026-10-10", "days": {}}), encoding="utf-8")
        appeles = []

        def paquet_reprise(key, h):
            appeles.append(h)
            t = max(15.0, 36.0 - 4.0 * abs(h.hour - 14)) if h.day == 11 else 15.0  # pic de 36,0 °C le 11 à 14 h UTC
            return csv_hour([("66136001", t, t + 0.5, t - 0.5)])

        rt.PACKAGE_DELAY = 0
        rt.BACKFILL_MAX_HOURS = 72  # pour tester la logique de reprise sur plusieurs jours
        rt.api_key = lambda: "cle-de-test"
        latest = datetime(2026, 10, 13, 5, tzinfo=timezone.utc)
        rt.find_latest_hour = lambda key: (latest, paquet_reprise(key, latest))
        rt.package_request = paquet_reprise
        rt.generate_live()
        # premier jour manquant : le 11 ; sa TN commence le 10 à 18 h UTC (une heure de plus est lue : référence du contrôle de plausibilité)
        assert min(appeles) == datetime(2026, 10, 10, 17, tzinfo=timezone.utc), min(appeles)
        assert sorted(p.stem for p in (out / "provisional").glob("*.json")) == ["2026-10-11", "2026-10-12"]
        j11 = _json.loads((out / "provisional" / "2026-10-11.json").read_text(encoding="utf-8"))["day"]
        assert [e["value"] for e in j11["events"]["heat"]] == [36.5], j11["events"]["heat"]
        # deuxième passage : les jours sont couverts, la reprise n'a plus lieu (seules les heures courantes sont relues)
        appeles.clear()
        rt.generate_live()
        # (avant 6 h UTC, la fenêtre TX de la veille, ouverte depuis le 12 à 6 h UTC, est relue ; rien avant, hormis l'heure de référence)
        assert min(appeles) >= datetime(2026, 10, 12, 5, tzinfo=timezone.utc), min(appeles)
        # quota dépassé en cours de reprise : on garde ce qui a été lu, sans échec
        (out / "cache" / "provisional.json").unlink()
        compteur = {"n": 0}

        def paquet_quota(key, h):
            compteur["n"] += 1
            if compteur["n"] > 5:
                raise RuntimeError("Météo-France HTTP 429 — quota dépassé")
            return paquet_reprise(key, h)

        rt.package_request = paquet_quota
        rt.generate_live()
        assert (out / "live.json").exists()
    finally:
        os.chdir(ancien)

# --- Début de la couche récente : plus ancienne construction de cache moins une marge, plafonné ----------------------------
from datetime import timedelta

with tempfile.TemporaryDirectory() as tmp:
    ancien = os.getcwd()
    try:
        os.chdir(tmp)
        cache_dir = Path("records_temperature") / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        recent = (rt.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (cache_dir / "dep-66.json").write_text('{"schema_version":1,"department":"66","generated_at":"%s","stations":{}}' % recent, encoding="utf-8")
        assert rt.recent_since_date() == (rt.utcnow() - timedelta(days=30 + rt.RECENT_MARGIN_DAYS)).date()
        vieux = (rt.utcnow() - timedelta(days=900)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (cache_dir / "dep-11.json").write_text('{"schema_version":1,"department":"11","generated_at":"%s","stations":{}}' % vieux, encoding="utf-8")
        assert rt.recent_since_date() == (rt.utcnow() - timedelta(days=rt.RECENT_MAX_DAYS)).date()  # plafonné
    finally:
        os.chdir(ancien)

print("tests generer_records_temperature : OK")

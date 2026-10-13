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

# --- LIVE de bout en bout (paquets horaires simulés) : jours provisoires, état conservé, couche récente ---------------
import os
import tempfile

import json as _json


def csv_hour(rows):
    lignes = ["geo_id_insee;t;tx;tn"] + [f"{sid};{t + 273.15:.2f};{tx + 273.15:.2f};{tn + 273.15:.2f}" for sid, t, tx, tn in rows]
    return ("\n".join(lignes) + "\n").encode()


def lancer_live(workdir, latest, through):
    os.chdir(workdir)
    out = Path("records_temperature")
    (out / "cache").mkdir(parents=True, exist_ok=True)
    cache = {"schema_version": 1, "module_version": "1.1.0", "build_id": "t", "department": "66", "generated_at": "2026-08-20T00:00:00Z",
             "stations": {"66136001": dict(station(), id="66136001", name="PERPIGNAN")}, "stations_total": 1, "rows_read": 0, "last_date": None}
    (out / "cache" / "dep-66.json").write_text(_json.dumps(cache), encoding="utf-8")
    (out / "cache" / "recent.json").write_text(_json.dumps({"schema_version": 1, "through": through, "days": {"2026-10-03": {"66136001": [35.0, 12.0]}}}), encoding="utf-8")

    def paquet(key, h):
        # le 12 octobre, 34,5 °C à 14 h UTC ; sinon 15 °C
        chaud = h.day == 12 and h.hour == 14
        return csv_hour([("66136001", 34.0 if chaud else 15.0, 34.5 if chaud else 15.5, 14.5 if not chaud else 33.5)])

    rt.PACKAGE_DELAY = 0
    rt.api_key = lambda: "cle-de-test"
    rt.find_latest_hour = lambda key: (latest, paquet(key, latest))
    rt.package_request = paquet
    rt.generate_live()
    return out


with tempfile.TemporaryDirectory() as tmp:
    ancien = os.getcwd()
    try:
        out = lancer_live(tmp, datetime(2026, 10, 13, 5, tzinfo=timezone.utc), "2026-10-10")
        live = _json.loads((out / "live.json").read_text(encoding="utf-8"))
        assert live["day"]["date"] == "2026-10-13" and live["recent_through"] == "2026-10-10" and live["provisional_days"] == ["2026-10-12"], live["provisional_days"]
        assert live["coverage_target"] == 95 and live["coverage_departments"] == 1
        prov = _json.loads((out / "provisional" / "2026-10-12.json").read_text(encoding="utf-8"))
        chaud = prov["day"]["events"]["heat"]
        assert len(chaud) == 1 and chaud[0]["value"] == 34.5 and "monthly" not in chaud[0]["flags"], chaud  # 35,0 relevés le 3 octobre : pas un record du mois
        assert "daily" in chaud[0]["flags"] or "near" in chaud[0], chaud
        assert chaud[0]["provisional"] is True
        etat = _json.loads((out / "cache" / "provisional.json").read_text(encoding="utf-8"))
        assert set(etat["days"]) >= {"2026-10-12", "2026-10-13"}, etat["days"].keys()
        assert etat["days"]["2026-10-12"]["66136001"][0] == 34.5  # TX du 12 (fenêtre 6 h → 6 h UTC)
        index = _json.loads((out / "index.json").read_text(encoding="utf-8"))
        assert index["provisional_days"] == ["2026-10-12"] and index["latest_date"] == "2026-10-13" and index["recent_through"] == "2026-10-10"
        # deuxième passage identique : le fichier du jour provisoire n'est pas réécrit
        premiere = prov["generated_at"]
        out = lancer_live(tmp, datetime(2026, 10, 13, 5, tzinfo=timezone.utc), "2026-10-10")
        assert _json.loads((out / "provisional" / "2026-10-12.json").read_text(encoding="utf-8"))["generated_at"] == premiere
        # données contrôlées jusqu'au 12 : le jour provisoire disparaît (fichier et état)
        out = lancer_live(tmp, datetime(2026, 10, 13, 5, tzinfo=timezone.utc), "2026-10-12")
        assert not list((out / "provisional").glob("*.json"))
        assert "2026-10-12" not in _json.loads((out / "cache" / "provisional.json").read_text(encoding="utf-8"))["days"]
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

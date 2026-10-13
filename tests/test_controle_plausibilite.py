#!/usr/bin/env python3
"""Tests du contrôle de plausibilité des relevés horaires (controle_plausibilite.py) et de son branchement sur les classements de
températures, les températures en direct et les températures de la veille. Aucun appel réseau : les paquets sont simulés.
"""

from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import io
import sys

try:
    import requests  # noqa: F401
except ImportError:  # les scripts importent requests : sans lui, rien à tester ici
    print("tests controle_plausibilite : ignorés (requests absent)")
    raise SystemExit(0)

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
import controle_plausibilite as cp  # noqa: E402


def charger(nom):
    spec = importlib.util.spec_from_file_location(nom, RACINE / f"{nom}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def h(jour, heure):
    return datetime(2026, 10, jour, heure, tzinfo=timezone.utc)


# --- Règles d'une heure (les cas détaillés sont aussi dans test_generer_records_temperature.py) ---------------------------------------------
assert cp.check_hour(21.3, 21.5, 0.0, 21.2)[:3] == (None, None, None)  # La Chiappa, 12/10 18 h UTC : tn 0,0 °C pour t 21,3 °C
assert cp.check_hour(11.0, 11.0, 3.7) == (11.0, 11.0, 3.7, [])  # sans heure précédente : seul le contrôle interne de la ligne
assert cp.check_hour(15.0, 15.2, 4.0, 14.5)[:3] == (15.0, 15.2, None)  # tn sans appui : écartée seule

# --- Chronologie d'une station : la référence est l'heure précédente exacte, si elle a été retenue ------------------------------------------
serie = [(h(13, 6), 8.0, 8.3, 7.7), (h(13, 7), 8.5, 8.7, -1.0), (h(13, 8), 9.0, 9.2, 8.6)]
res = cp.check_series(serie)
assert res[0][3] == [] and res[2][3] == [], res
assert res[1][:3] == (8.5, 8.7, None) and len(res[1][3]) == 1, res[1]  # 7 h : tn à 9,5 °C sous les températures voisines, écartée seule
# heure manquante (7 h absente) : 8 h n'a plus de référence, elle n'est contrôlée que sur elle-même
res = cp.check_series([serie[0], serie[2]])
assert res[1] == (9.0, 9.2, 8.6, []), res
# ligne rejetée en entier : la suivante n'a pas de référence non plus
res = cp.check_series([(h(13, 8), 21.3, 21.7, 20.9), (h(13, 9), -8.4, 22.1, -8.5), (h(13, 10), 4.5, 22.6, -8.5)])
assert res[1][:3] == (None, None, None) and res[2][:3] == (None, None, None), res  # 9 h et 10 h : étendue de plus de 15 °C

ecartes = []
retenus = cp.controler_station("20247001", [(h(13, 8), 21.3, 21.7, 20.9), (h(13, 9), -8.4, 22.1, -8.5)], ecartes)
assert retenus == [(21.3, 21.7, 20.9), (None, None, None)], retenus
assert len(ecartes) == 1 and ecartes[0]["id"] == "20247001" and ecartes[0]["hour"] == "2026-10-13T09:00:00Z" and ecartes[0]["raw"] == [-8.4, 22.1, -8.5], ecartes
assert cp.quality_summary(ecartes) == {"version": cp.QC_VERSION, "rejected_rows": 1, "rejected_stations": ["20247001"]}
sortie = io.StringIO()
with redirect_stdout(sortie):
    cp.log_rejections(ecartes, {"20247001": {"name": "LA CHIAPPA"}})
assert "[QC] 20247001 LA CHIAPPA 2026-10-13T09:00:00Z" in sortie.getvalue() and "1 relevés écartés" in sortie.getvalue(), sortie.getvalue()


# --- Paquets simulés ---------------------------------------------------------------------------------------------------------------------------
def k(v):
    return "" if v is None else f"{v + 273.15:.2f}"


def csv_paquet(heure, lignes):
    """lignes : (id, lat, lon, t, tx, tn) en °C -> CSV DPPaquetObs (kelvins)."""
    corps = [f"{sid};{lat};{lon};{heure.strftime('%Y-%m-%dT%H:%M:%SZ')};{k(t)};{k(None if t is None else t - 2)};{k(tx)};{k(tn)};80;2.0"
             for sid, lat, lon, t, tx, tn in lignes]
    return ("\n".join(["geo_id_insee;lat;lon;validity_time;t;td;tx;tn;u;ff"] + corps) + "\n").encode()


def releve(sid, heure):
    """Trois stations : A normale ; B au capteur défaillant à 9 h et 10 h (derniers relevés) ; C défaillante à 9 h seulement."""
    base = 14.0 + 6.0 * (1 if 6 <= heure.hour <= 18 else 0) + (heure.day - 12) * 0.0
    if sid == "11111111":
        return (sid, 43.0, 2.0, base, base + 0.4, base - 0.4)
    if sid == "22222222" and heure == h(13, 9):
        return (sid, 41.6, 9.3, -8.4, 22.1, -8.5)
    if sid == "22222222" and heure == h(13, 10):
        return (sid, 41.6, 9.3, 4.5, 22.6, -8.5)
    if sid == "33333333" and heure == h(13, 9):
        return (sid, 42.0, 3.0, -8.4, 22.1, -8.5)
    return (sid, 41.6 if sid == "22222222" else 42.0, 9.3 if sid == "22222222" else 3.0, 21.0, 21.4, 20.6)


STATIONS = ("11111111", "22222222", "33333333")


def paquet(heure):
    return SimpleNamespace(content=csv_paquet(heure, [releve(sid, heure) for sid in STATIONS]))


# --- Classements de températures --------------------------------------------------------------------------------------------------------------
gc = charger("generer_classements_temperature")
heures = {h(13, hh): [dict(id=sid, validity_time=gc.iso(h(13, hh)), t=r[3], tx=r[4], tn=r[5], lat=r[1], lon=r[2], humidity=80.0)
                       for sid in STATIONS for r in [releve(sid, h(13, hh))]] for hh in range(0, 11)}
propres, ecartes = gc.controler_heures(heures)
assert {e["id"] for e in ecartes} == {"22222222", "33333333"} and len(ecartes) == 3, ecartes  # B : 9 h et 10 h ; C : 9 h
ligne_b = next(o for o in propres[h(13, 9)] if o["id"] == "22222222")
assert ligne_b["t"] is None and ligne_b["tx"] is None and ligne_b["tn"] is None and ligne_b["humidity"] == 80.0, ligne_b  # autres champs conservés
ligne_a = next(o for o in propres[h(13, 9)] if o["id"] == "11111111")
assert ligne_a is next(o for o in heures[h(13, 9)] if o["id"] == "11111111")  # ligne saine : inchangée
tn_prov = gc.build_extreme_table(propres, {}, h(12, 18), h(13, 10), "min", "TN", True)
assert min(r["value"] for r in tn_prov["rows"]) > 10, tn_prov["rows"][:3]  # ni -8,5 °C ni 0 °C en tête
tn_brut = gc.build_extreme_table(heures, {}, h(12, 18), h(13, 10), "min", "TN", True)
assert tn_brut["rows"][0]["value"] == -8.5  # sans contrôle, le capteur défaillant serait premier
tables, horaire, ecartes2 = gc.build_tables({"hours": {gc.iso(dt): rows for dt, rows in heures.items()}}, {}, h(13, 10))
assert len(ecartes2) == 3 and tables["tn_provisoire"]["rows"][0]["value"] > 10, tables["tn_provisoire"]["rows"][:2]

# --- Températures en direct ----------------------------------------------------------------------------------------------------------------------
tr = charger("generer_temperature_records")
tr.PACKAGE_DELAY = 0
tr.find_latest_package = lambda key: (h(13, 10), paquet(h(13, 10)))
tr.package_request = lambda key, cible: paquet(cible)
latest, live, ecartes3 = tr.load_live_history("cle")
assert latest == h(13, 10)
assert "11111111" in live and "22222222" not in live, sorted(live)  # B : dernier relevé rejeté, la station est retirée de cette édition
assert "33333333" in live and live["33333333"]["temperature"] == 21.0, live.get("33333333")
assert live["33333333"]["min_12h"] > 20 and live["33333333"]["min_24h"] > 20, live["33333333"]  # le -8,4 °C de 9 h est écarté
assert live["33333333"]["tn_today"] > 20, live["33333333"]
assert len(ecartes3) == 3 and {e["id"] for e in ecartes3} == {"22222222", "33333333"}, ecartes3

# --- Températures de la veille ---------------------------------------------------------------------------------------------------------------------
vl = charger("generer_temperature_veille")
vl.PACKAGE_DELAY = 0
jour = date(2026, 10, 12)
debut, fin = datetime(2026, 10, 11, 22, tzinfo=timezone.utc), datetime(2026, 10, 12, 22, tzinfo=timezone.utc)
vl.previous_local_day_window = lambda now_utc=None: (jour, debut, fin)


def releve_veille(sid, heure):
    if sid == "22222222" and heure == datetime(2026, 10, 12, 18, tzinfo=timezone.utc):
        return (sid, 41.6, 9.3, 21.3, 21.5, 0.0)  # tn 0,0 °C pour t 21,3 °C (La Chiappa, 12/10 18 h UTC)
    base = 14.0 + 6.0 * (1 if 6 <= heure.hour <= 18 else 0)
    return (sid, 41.6 if sid == "22222222" else 42.0, 9.3 if sid == "22222222" else 3.0, base, base + 0.4, base - 0.4)


vl.package_request = lambda key, heure: SimpleNamespace(content=csv_paquet(heure, [releve_veille(sid, heure) for sid in STATIONS]))
_, _, _, attendues, disponibles, brut, ecartes4 = vl.load_yesterday("cle")
assert attendues == 24 and disponibles == 24
assert brut["22222222"]["tmin_yesterday"] == 13.6, brut["22222222"]["tmin_yesterday"]  # et non 0,0 °C
assert brut["11111111"]["tmin_yesterday"] == 13.6 and brut["22222222"]["tmax_yesterday"] == 20.4
assert len(ecartes4) == 1 and ecartes4[0]["id"] == "22222222" and ecartes4[0]["raw"] == [21.3, 21.5, 0.0], ecartes4
assert "series" not in brut["22222222"]  # le brouillon de chronologie n'est pas laissé dans les données agrégées

# --- Les scripts publient le bilan du contrôle -------------------------------------------------------------------------------------------------------
for src in ("generer_classements_temperature.py", "generer_temperature_records.py", "generer_temperature_veille.py"):
    assert '"quality_control": quality_summary(' in (RACINE / src).read_text(encoding="utf-8"), src

print("tests controle_plausibilite : OK")

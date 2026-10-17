#!/usr/bin/env python3
"""Tests de generer_neige_massif.py : cadre en mailles de la grille, lecture des grilles des modèles, précipitation totale (planchers),
quantification, format du fichier (écriture et relecture), un modèle complet et un passage avec reprise de l'état précédent.
Aucun appel réseau : les fichiers des modèles sont fabriqués ici.
"""

from datetime import datetime, timezone
from pathlib import Path
import gzip
import importlib.util
import json
import struct
import tempfile
import unittest

import numpy as np

MODULE_PATH = Path(__file__).resolve().parents[1] / "generer_neige_massif.py"
SPEC = importlib.util.spec_from_file_location("neige_massif", MODULE_PATH)
g = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(g)

BORNES_FRANCE = {"south": 38.0, "west": -12.0, "north": 57.0, "east": 18.0}


def sonde(valeurs, vmin, vmax, signature=b"HKV1"):
    """Une grille « maps/values/…hkv.gz » : valeurs en clair (NaN = manquant) quantifiées entre vmin et vmax sur 16 bits."""
    v = np.asarray(valeurs, dtype=np.float64)
    h, w = v.shape
    brut = np.where(np.isfinite(v), np.clip(np.rint((np.nan_to_num(v) - vmin) / (vmax - vmin) * 65534), 0, 65534), 65535).astype("<u2")
    return gzip.compress(signature + struct.pack("<HHff", w, h, vmin, vmax) + brut.tobytes())


class SourceFictive:
    """Remplace le réseau : `fichiers` associe un chemin (sans ?r=…) au contenu ; un chemin absent est un 404."""

    def __init__(self, fichiers):
        self.fichiers = fichiers
        self.demandes = []

    def octets(self, url):
        self.demandes.append(url)
        return self.fichiers.get(url.split("?")[0].replace(g.RAW, ""))


class Cadre(unittest.TestCase):
    def test_mailles_de_la_grille_des_modeles(self):
        # AROME, ARPEGE, ECMWF, AIFS : 1100 × 820 sur 38-57 °N, 12 °O-18 °E ; mêmes chiffres que la carte
        b = g.bloc_de_grille(1100, 820, BORNES_FRANCE)
        self.assertEqual((b["col0"], b["col1"]), (440, 572))
        self.assertEqual(b["row1"] - b["row0"], 63)
        self.assertAlmostEqual(b["bounds"]["west"], 0.0, places=6)
        self.assertAlmostEqual(b["bounds"]["east"], 3.6, places=6)

    def test_le_bloc_recouvre_le_cadre_de_moins_d_une_maille(self):
        for w, h in ((1100, 820), (1050, 1000)):  # GFS : 1050 × 1000
            b = g.bloc_de_grille(w, h, BORNES_FRANCE)["bounds"]
            dlon = 30.0 / w
            self.assertLessEqual(b["west"], g.BOITE["west"] + 1e-9)
            self.assertGreaterEqual(b["east"], g.BOITE["east"] - 1e-9)
            self.assertLess(b["west"] - g.BOITE["west"], dlon)
            self.assertLess(b["east"] - g.BOITE["east"], dlon + 1e-9)
            self.assertLessEqual(b["south"], g.BOITE["south"] + 1e-9)
            self.assertGreaterEqual(b["north"], g.BOITE["north"] - 1e-9)
            self.assertLess(g.BOITE["south"] - b["south"], 0.06)  # une maille fait environ 0,02 à 0,03° de latitude ici
            self.assertLess(b["north"] - g.BOITE["north"], 0.06)

    def test_les_lignes_suivent_l_ordonnee_web_mercator(self):
        # une grille de 100 lignes sur 38-57 °N : la ligne du milieu n'est pas à 47,5 °N mais plus au nord (Mercator étire vers le pôle)
        milieu = g.inv_merc((g.merc(57.0) + g.merc(38.0)) / 2)
        self.assertGreater(milieu, 47.5)
        b = g.bloc_de_grille(100, 100, BORNES_FRANCE, {"west": -12.0, "east": 18.0, "south": milieu, "north": 57.0})
        self.assertEqual((b["row0"], b["row1"]), (0, 50))
        self.assertAlmostEqual(b["bounds"]["south"], milieu, places=5)

    def test_refuse_un_cadre_hors_de_la_grille(self):
        with self.assertRaises(ValueError):
            g.bloc_de_grille(100, 100, BORNES_FRANCE, {"west": 100.0, "east": 101.0, "south": 0.0, "north": 1.0})


class LectureDesGrilles(unittest.TestCase):
    def test_decode_les_valeurs_et_les_manquants(self):
        octets = sonde([[0.1, 100.0, np.nan], [50.0, 0.1, 25.0]], 0.1, 100.0)
        v = g.lire_sonde(octets)
        self.assertEqual(v.shape, (2, 3))
        self.assertAlmostEqual(float(v[0, 0]), 0.1, places=4)
        self.assertAlmostEqual(float(v[0, 1]), 100.0, places=3)
        self.assertTrue(np.isnan(v[0, 2]))
        self.assertAlmostEqual(float(v[1, 0]), 50.0, delta=0.01)

    def test_lit_les_trois_signatures_des_modeles(self):
        for signature in (b"HKV1", b"CEV1", b"AIV1"):
            v = g.lire_sonde(sonde([[1.0, 2.0]], -25.0, 45.0, signature))
            self.assertAlmostEqual(float(v[0, 1]), 2.0, delta=0.01, msg=signature)

    def test_refuse_une_grille_tronquee(self):
        with self.assertRaises(ValueError):
            g.lire_sonde(gzip.compress(b"HKV1" + struct.pack("<HHff", 4, 4, 0.0, 1.0) + b"\x00" * 10))
        with self.assertRaises(ValueError):
            g.lire_sonde(gzip.compress(b"HKV1"))


class Quantification(unittest.TestCase):
    def test_precipitation_totale_compte_pour_zero_les_planchers(self):
        pluie = np.array([[0.1, 0.1, 2.0, np.nan, np.nan]], dtype=np.float32)
        neige = np.array([[0.1, 0.4, 0.5, 3.0, np.nan]], dtype=np.float32)
        total = g.precipitation_totale([pluie, neige])
        self.assertEqual(float(total[0, 0]), 0.0)  # deux planchers : pas de 0,2 mm fantôme
        self.assertAlmostEqual(float(total[0, 1]), 0.4, places=5)
        self.assertAlmostEqual(float(total[0, 2]), 2.5, places=5)
        self.assertAlmostEqual(float(total[0, 3]), 3.0, places=5)
        self.assertTrue(np.isnan(total[0, 4]))
        # un seul composant (ECMWF, GFS, AIFS) : le plancher 0 de la grille reste 0
        self.assertEqual(float(g.precipitation_totale([np.array([[0.0, 0.3]], dtype=np.float32)])[0, 0]), 0.0)

    def test_temperature_en_entiers(self):
        t = np.array([[-30.0, 0.0, 1.04, 2.26, 40.0, -50.0, 400.0, np.nan]], dtype=np.float32)
        o = g.temperature_en_entiers(t)
        self.assertEqual(o.dtype, np.int16)
        self.assertEqual(list(o[0]), [-300, 0, 10, 23, 400, -500, 3000, -32768])
        retour = o[0][:4].astype(float) / g.T_ECHELLE
        for vrai, lu in zip([-30.0, 0.0, 1.04, 2.26], retour):
            self.assertLessEqual(abs(vrai - lu), 0.05 + 1e-6)  # un vingtième de degré au plus

    def test_precipitation_en_entiers(self):
        p = np.array([[0.0, 0.05, 1.0, 12.34, 1e5, np.nan]], dtype=np.float32)
        o = g.precipitation_en_entiers(p)
        self.assertEqual(o.dtype, np.uint16)
        self.assertEqual(list(o[0]), [0, 1, 20, 247, 65534, 65535])


def exemple_fichier(n_etapes=3, w=5, h=4):
    rng = np.random.default_rng(7)
    relief = rng.integers(-5, 2900, size=(h, w)).astype(np.float64)
    pluies = [rng.integers(0, 2000, size=(h, w)).astype(np.uint16) for _ in range(n_etapes)]
    temps = [rng.integers(-400, 450, size=(h, w)).astype(np.int16) for _ in range(n_etapes)]
    entete = {
        "schema_version": 1,
        "modele": "essai",
        "run_time": "2026-10-17T03:00:00Z",
        "bounds": {"west": 0.0, "east": 3.6, "north": 43.4, "south": 41.8},
        "etapes": [{"valid_time": f"2026-10-17T{3 + k:02d}:00:00Z", "lead_hour": k} for k in range(n_etapes)],
    }
    return entete, relief, pluies, temps


class Fichier(unittest.TestCase):
    def test_ecriture_puis_relecture(self):
        entete, relief, pluies, temps = exemple_fichier()
        lu = g.lire_extrait(g.ecrire_extrait(entete, relief, pluies, temps))
        self.assertEqual((lu["w"], lu["h"]), (5, 4))
        self.assertEqual(lu["modele"], "essai")
        self.assertEqual([e["lead_hour"] for e in lu["etapes"]], [0, 1, 2])
        np.testing.assert_array_equal(lu["relief"], relief.astype(np.int16))
        for k in range(3):
            np.testing.assert_array_equal(lu["pluie"][k], pluies[k])
            np.testing.assert_array_equal(lu["temperature"][k], temps[k])

    def test_le_corps_est_aligne_sur_quatre_octets_et_le_fichier_est_reproductible(self):
        for modele in ("a", "un-nom-un-peu-plus-long", "xyz"):
            entete, relief, pluies, temps = exemple_fichier()
            entete["modele"] = modele
            octets = g.ecrire_extrait(entete, relief, pluies, temps)
            corps = gzip.decompress(octets)
            self.assertEqual(corps[:4], b"NSM1")
            (longueur,) = struct.unpack_from("<I", corps, 4)
            self.assertEqual((8 + longueur) % 4, 0)
            json.loads(corps[8 : 8 + longueur].decode("utf-8"))  # l'en-tête reste du JSON valide malgré le remplissage
            self.assertEqual(octets, g.ecrire_extrait(entete, relief, pluies, temps))  # pas de date dans la compression

    def test_refuse_un_nombre_de_grilles_different_du_nombre_d_echeances(self):
        entete, relief, pluies, temps = exemple_fichier()
        with self.assertRaises(ValueError):
            g.ecrire_extrait(entete, relief, pluies[:2], temps[:2])
        with self.assertRaises(ValueError):
            g.ecrire_extrait(entete, relief, pluies, temps[:2])

    def test_refuse_un_fichier_abime(self):
        entete, relief, pluies, temps = exemple_fichier()
        corps = gzip.decompress(g.ecrire_extrait(entete, relief, pluies, temps))
        with self.assertRaises(ValueError):
            g.lire_extrait(gzip.compress(b"XXXX" + corps[4:]))
        with self.assertRaises(ValueError):
            g.lire_extrait(gzip.compress(corps + b"\x00"))


# --- Un modèle d'essai : grille de 20 × 10 mailles sur le cadre, 4 échéances horaires --------------------------------------
BORNES_ESSAI = {"south": 41.0, "west": -1.0, "north": 44.0, "east": 5.0}
W0, H0 = 20, 10


def index_essai(run="2026-10-17T03:00:00Z", sans_neige=(), n=4, composition=("pluie_1h", "neige"), genere="2026-10-17T06:00:00Z"):
    steps = []
    for k in range(n):
        sondes = {"temperature": f"maps/values/temperature/{k:03d}.hkv.gz"}
        for c in composition:
            if (k, c) not in sans_neige:
                sondes[c] = f"maps/values/{c}/{k:03d}.hkv.gz"
        if k == 0:
            sondes["altitude"] = "maps/values/altitude/000.hkv.gz"
        steps.append({"lead_hour": k, "valid_time": f"2026-10-17T{3 + k:02d}:00:00Z", "probes": sondes, "files": {}})
    return {"run_time": run, "generated_at": genere, "bounds": BORNES_ESSAI, "steps": steps}


def fichiers_essai(depot, n=4, composition=("pluie_1h", "neige"), manquants=()):
    base = f"{depot}/data/"
    f = {base + "maps/values/altitude/000.hkv.gz": sonde(np.full((H0, W0), 1500.0), -50.0, 4500.0)}
    for k in range(n):
        f[base + f"maps/values/temperature/{k:03d}.hkv.gz"] = sonde(np.full((H0, W0), -2.0 + k), -25.0, 45.0)
        for c in composition:
            if (k, c) in manquants:
                continue
            valeurs = np.full((H0, W0), 0.1)  # plancher d'AROME : « rien »
            valeurs[:, 5:8] = 2.0 if c == "pluie_1h" else 0.5
            f[base + f"maps/values/{c}/{k:03d}.hkv.gz"] = sonde(valeurs, 0.1, 100.0)
    return f


class UnModele(unittest.TestCase):
    def test_construit_le_fichier_et_l_entree_du_manifeste(self):
        index = index_essai()
        source = SourceFictive(fichiers_essai("arome-meteofrance"))
        octets, entree = g.construire_modele(source, "arome", "arome-meteofrance", ("pluie_1h", "neige"), index, fils=2)
        lu = g.lire_extrait(octets)
        self.assertEqual((lu["w"], lu["h"]), (entree["w"], entree["h"]))
        self.assertEqual(entree["run_time"], "2026-10-17T03:00:00Z")
        self.assertEqual(entree["fichier"], "arome.nsm.gz")
        self.assertEqual(entree["octets"], len(octets))
        self.assertEqual(entree["etapes"], [s["valid_time"] for s in index["steps"]])
        self.assertEqual(lu["pluie"].shape, (4, entree["h"], entree["w"]))
        # le cadre (0-3,6 °E, 41,8-43,4 °N) sur une grille de 20 × 10 mailles de 0,3° : colonnes 3 à 15, bien dans la grille d'origine
        self.assertLessEqual(entree["bounds"]["west"], 0.0)
        self.assertGreaterEqual(entree["bounds"]["east"], 3.6)
        # relief : 1 500 m partout ; température : -2, -1, 0, 1 °C selon l'échéance ; précipitation : 2 + 0,5 mm là où il y en a, rien ailleurs
        self.assertTrue((lu["relief"] == 1500).all())
        for k in range(4):
            self.assertTrue((lu["temperature"][k] == g.T_ECHELLE * (-2 + k)).all(), k)
        bloc = lu["pluie"][0]
        self.assertEqual(int(bloc.max()), int(round(2.5 * g.P_ECHELLE)))
        self.assertEqual(int(bloc.min()), 0)  # deux planchers (0,1 mm + 0,1 mm) : zéro

    def test_demande_les_grilles_avec_le_run_dans_l_adresse(self):
        source = SourceFictive(fichiers_essai("gfs", composition=("pluie_1h",)))
        g.construire_modele(source, "gfs", "gfs", ("pluie_1h",), index_essai(run="2026-10-17T00:00:00Z", composition=("pluie_1h",)), fils=2)
        self.assertTrue(source.demandes)
        self.assertTrue(all(u.endswith("?r=2026-10-17T00:00:00Z") for u in source.demandes))

    def test_ignore_une_echeance_sans_grille_mais_garde_les_autres(self):
        index = index_essai(sans_neige={(2, "neige")})
        source = SourceFictive(fichiers_essai("arome-meteofrance", manquants={(2, "neige")}))
        octets, entree = g.construire_modele(source, "arome", "arome-meteofrance", ("pluie_1h", "neige"), index, fils=2)
        self.assertEqual(len(entree["etapes"]), 3)
        self.assertNotIn("2026-10-17T05:00:00Z", entree["etapes"])
        self.assertEqual(g.lire_extrait(octets)["pluie"].shape[0], 3)

    def test_ignore_une_echeance_dont_le_fichier_n_existe_pas(self):
        index = index_essai()
        source = SourceFictive(fichiers_essai("arome-meteofrance", manquants={(1, "pluie_1h")}))  # annoncée dans l'index, 404 sur le serveur
        _, entree = g.construire_modele(source, "arome", "arome-meteofrance", ("pluie_1h", "neige"), index, fils=2)
        self.assertEqual(len(entree["etapes"]), 3)

    def test_refuse_un_modele_dont_il_manque_plus_de_la_moitie(self):
        index = index_essai(sans_neige={(1, "neige"), (2, "neige"), (3, "neige")})
        source = SourceFictive(fichiers_essai("arome-meteofrance", manquants={(1, "neige"), (2, "neige"), (3, "neige")}))
        with self.assertRaises(ValueError):
            g.construire_modele(source, "arome", "arome-meteofrance", ("pluie_1h", "neige"), index, fils=2)

    def test_refuse_un_modele_sans_grille_d_altitude(self):
        index = index_essai()
        del index["steps"][0]["probes"]["altitude"]
        with self.assertRaises(ValueError):
            g.construire_modele(SourceFictive(fichiers_essai("arome-meteofrance")), "arome", "arome-meteofrance", ("pluie_1h", "neige"), index, fils=2)

    def test_les_echeances_calculables_sont_celles_qui_ont_toutes_les_grilles(self):
        index = index_essai(sans_neige={(1, "neige")})
        self.assertEqual([e["lead_hour"] for e in g.etapes_calculables(index, ("pluie_1h", "neige"))], [0, 2, 3])
        self.assertEqual(len(g.etapes_calculables(index, ("pluie_1h",))), 4)


class Passage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self.tmp.name)
        self.maintenant = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
        self.runs = {"arome-meteofrance": "2026-10-17T03:00:00Z", "gfs": "2026-10-17T00:00:00Z"}
        self.generes = {"arome-meteofrance": "2026-10-17T06:00:00Z", "gfs": "2026-10-17T06:00:00Z"}  # heure de génération de l'index de chaque modèle
        self.etapes = {"arome-meteofrance": 4, "gfs": 4}  # échéances que l'index annonce (un run se publie au fil du calcul)
        self.pannes = set()

    def tearDown(self):
        self.tmp.cleanup()

    def lire_index(self, depot):
        if depot in self.pannes:
            raise RuntimeError("panne du réseau")
        composition = ("pluie_1h", "neige") if depot == "arome-meteofrance" else ("pluie_1h",)
        return index_essai(run=self.runs[depot], composition=composition, n=self.etapes[depot], genere=self.generes[depot])

    def source(self):
        f = fichiers_essai("arome-meteofrance", n=8)  # les grilles de toutes les échéances que l'index pourra annoncer
        f.update(fichiers_essai("gfs", n=8, composition=("pluie_1h",)))
        return SourceFictive(f)

    def passer(self, sortie, etat=None):
        return g.passer(sortie, etat, source=self.source(), modeles=("arome", "gfs"), fils=2, maintenant=self.maintenant, lire_index=self.lire_index)

    def test_premier_passage_ecrit_les_fichiers_et_le_manifeste(self):
        sortie = self.racine / "a"
        manifeste = self.passer(sortie)
        self.assertEqual(sorted(manifeste["modeles"]), ["arome", "gfs"])
        self.assertTrue((sortie / "arome.nsm.gz").is_file() and (sortie / "gfs.nsm.gz").is_file())
        ecrit = json.loads((sortie / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(ecrit["generated_at"], "2026-10-17T09:00:00Z")
        self.assertEqual(ecrit["boite"], g.BOITE)
        self.assertEqual(ecrit["modeles"]["gfs"]["run_time"], "2026-10-17T00:00:00Z")

    def test_reprend_tel_quel_un_modele_dont_le_run_n_a_pas_change(self):
        premier = self.racine / "a"
        self.passer(premier)
        self.runs["gfs"] = "2026-10-17T06:00:00Z"  # seul GFS a un nouveau run
        second = self.racine / "b"
        manifeste = self.passer(second, premier)
        self.assertEqual(manifeste["modeles"]["gfs"]["run_time"], "2026-10-17T06:00:00Z")
        self.assertEqual(manifeste["modeles"]["arome"]["run_time"], "2026-10-17T03:00:00Z")
        self.assertEqual((second / "arome.nsm.gz").read_bytes(), (premier / "arome.nsm.gz").read_bytes())
        self.assertNotEqual((second / "gfs.nsm.gz").read_bytes(), (premier / "gfs.nsm.gz").read_bytes())

    def test_reprend_un_modele_dont_l_index_n_a_pas_bouge_et_reconstruit_celui_dont_le_run_a_avance(self):
        # AROME et GFS publient un run au fil du calcul : même run, mais l'index annonce de plus en plus d'échéances
        premier = self.racine / "a"
        self.passer(premier)
        self.assertEqual(len(json.loads((premier / "index.json").read_text(encoding="utf-8"))["modeles"]["arome"]["etapes"]), 4)
        self.etapes["arome-meteofrance"] = 7
        self.generes["arome-meteofrance"] = "2026-10-17T06:12:00Z"
        second = self.racine / "b"
        manifeste = self.passer(second, premier)
        self.assertEqual(len(manifeste["modeles"]["arome"]["etapes"]), 7)  # même run (03Z), échéances ajoutées : reconstruit
        self.assertEqual(manifeste["modeles"]["arome"]["run_time"], "2026-10-17T03:00:00Z")
        self.assertEqual(manifeste["modeles"]["arome"]["index_etapes"], 7)
        self.assertEqual((second / "gfs.nsm.gz").read_bytes(), (premier / "gfs.nsm.gz").read_bytes())  # GFS n'a pas bougé : repris
        # troisième passage sans rien de neuf : tout est repris
        troisieme = self.racine / "c"
        manifeste = self.passer(troisieme, second)
        self.assertEqual((troisieme / "arome.nsm.gz").read_bytes(), (second / "arome.nsm.gz").read_bytes())
        self.assertEqual(manifeste["modeles"]["arome"], json.loads((second / "index.json").read_text(encoding="utf-8"))["modeles"]["arome"])

    def test_reconstruit_quand_l_index_a_ete_regenere_avec_le_meme_nombre_d_echeances(self):
        premier = self.racine / "a"
        self.passer(premier)
        self.generes["gfs"] = "2026-10-17T07:30:00Z"
        second = self.racine / "b"
        manifeste = self.passer(second, premier)
        self.assertEqual(manifeste["modeles"]["gfs"]["index_generated_at"], "2026-10-17T07:30:00Z")

    def test_reconstruit_un_modele_publie_par_une_version_sans_etat_d_index(self):
        etat = self.racine / "etat"
        etat.mkdir()
        (etat / "index.json").write_text(json.dumps({"schema_version": 1, "modeles": {"gfs": {"run_time": "2026-10-17T00:00:00Z", "fichier": "gfs.nsm.gz"}}}), encoding="utf-8")
        (etat / "gfs.nsm.gz").write_bytes(b"ancien")
        manifeste = self.passer(self.racine / "a", etat)
        self.assertGreater(manifeste["modeles"]["gfs"]["octets"], 100)  # pas repris : l'entrée ne dit pas de quel état de l'index elle vient

    def test_un_modele_en_panne_garde_sa_derniere_version(self):
        premier = self.racine / "a"
        self.passer(premier)
        self.pannes.add("gfs")
        second = self.racine / "b"
        manifeste = self.passer(second, premier)
        self.assertEqual(sorted(manifeste["modeles"]), ["arome", "gfs"])
        self.assertEqual((second / "gfs.nsm.gz").read_bytes(), (premier / "gfs.nsm.gz").read_bytes())

    def test_un_modele_en_panne_sans_version_precedente_est_absent(self):
        self.pannes.add("gfs")
        manifeste = self.passer(self.racine / "a")
        self.assertEqual(sorted(manifeste["modeles"]), ["arome"])

    def test_ne_publie_pas_un_melange_quand_des_echeances_sont_ajoutees_pendant_la_lecture(self):
        appels = {"n": 0}

        def index_qui_grandit(depot):
            appels["n"] += 1
            return index_essai(run="2026-10-17T00:00:00Z", composition=("pluie_1h",), n=4 if appels["n"] == 1 else 6, genere="2026-10-17T06:00:00Z" if appels["n"] == 1 else "2026-10-17T06:03:00Z")

        manifeste = g.passer(self.racine / "a", None, source=SourceFictive(fichiers_essai("gfs", n=8, composition=("pluie_1h",))), modeles=("gfs",), fils=2, maintenant=self.maintenant, lire_index=index_qui_grandit)
        self.assertEqual(manifeste["modeles"], {})

    def test_ne_publie_pas_un_melange_quand_un_nouveau_run_sort_pendant_la_lecture(self):
        appels = {"n": 0}

        def index_changeant(depot):
            appels["n"] += 1
            run = "2026-10-17T03:00:00Z" if appels["n"] == 1 else "2026-10-17T06:00:00Z"  # le run change entre la lecture de l'index et la fin du calcul
            return index_essai(run=run, composition=("pluie_1h",))

        manifeste = g.passer(self.racine / "a", None, source=SourceFictive(fichiers_essai("gfs", composition=("pluie_1h",))), modeles=("gfs",), fils=2, maintenant=self.maintenant, lire_index=index_changeant)
        self.assertEqual(manifeste["modeles"], {})
        self.assertFalse((self.racine / "a" / "gfs.nsm.gz").exists())

    def test_ignore_un_etat_d_un_autre_schema(self):
        etat = self.racine / "etat"
        etat.mkdir()
        (etat / "index.json").write_text(json.dumps({"schema_version": 99, "modeles": {"gfs": {"run_time": "2026-10-17T00:00:00Z", "fichier": "gfs.nsm.gz"}}}), encoding="utf-8")
        (etat / "gfs.nsm.gz").write_bytes(b"pas un extrait")
        manifeste = self.passer(self.racine / "a", etat)
        self.assertGreater(manifeste["modeles"]["gfs"]["octets"], 100)  # reconstruit, pas repris


if __name__ == "__main__":
    unittest.main()

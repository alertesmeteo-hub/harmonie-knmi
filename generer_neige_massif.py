#!/usr/bin/env python3
"""Neige du massif : l'extrait des prévisions de nos modèles dont la carte /meteo-montagne tire la neige cumulée à l'altitude choisie.

Les modèles décrivent un relief lissé (AROME place le Canigou, 2 784 m, à 2 151 m ; ECMWF à 1 329 m) : à ces altitudes trop basses il pleut
quand, sur le vrai sommet, il neige, et leurs cartes de neige restent vides. Pour estimer la neige à une altitude voulue il faut, pour chaque
échéance, la température et les précipitations du modèle et le relief de sa maille ; pour la CUMULER depuis le début de la prévision il faut cela
pour TOUTES les échéances. Un navigateur ne peut pas lire 20 à 45 Mo de grilles par modèle : ce script le fait une fois pour toutes et publie,
pour chaque modèle, un petit fichier (quelques centaines de Ko) que la carte lit en une fois et avec lequel elle calcule la neige à n'importe
quelle altitude (le partage pluie-neige et le rapport cm par mm sont calculés dans la carte : lib/neige-altitude.ts du dépôt meteo-saas).

Sources : les grilles de valeurs « maps/values/<grandeur>/<échéance>.hkv.gz » de la branche data de chaque dépôt de modèle (celles que lit la
carte ; même grille, quelle que soit la maille du modèle : 1100 × 820 sur 38-57 °N et 12 °O-18 °E, GFS 1050 × 1000). Aucun service extérieur.
  - température à 2 m (°C) ;
  - précipitations totales de l'échéance (mm d'eau) : chez AROME et ARPEGE la grille « pluie_1h » ne compte que la pluie (la neige est à part,
    « neige » : là où AROME fait neiger sa « pluie » est nulle), on additionne donc les deux ; chez ECMWF, GFS et AIFS « pluie_1h » est le total.
    Une composante au plancher de sa grille (0,1 mm : « rien ») compte pour zéro ;
  - altitude du relief du modèle, maille par maille.
On ne garde que le cadre BOITE (Pyrénées et Languedoc-Roussillon), en mailles entières de la grille d'origine (même géométrie, mêmes lignes
Web Mercator : la carte dessine le bloc tel quel).

Sorties (dossier --sortie) : index.json (pour chaque modèle : run, fichier, cadre, échéances) et un fichier <modèle>.nsm.gz par modèle.
Format du fichier (avant compression gzip, petit-boutiste) :
    4 octets « NSM1 » · 4 octets : longueur L de l'en-tête (multiple de 4) · L octets : en-tête JSON UTF-8 (complété d'espaces) ·
    relief  : w × h entiers 16 bits signés (mètres) ·
    pluie   : pour chaque échéance, w × h entiers 16 bits non signés (précipitation totale en 1/20 mm ; 65535 = manquant) ·
    température : pour chaque échéance, w × h entiers 16 bits signés (dixièmes de degré ; -32768 = manquant).
Les lignes se suivent du nord au sud, les colonnes de l'ouest à l'est.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import shutil
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "1.0.0"
SCHEMA_VERSION = 1
FORMAT = b"NSM1"
BUILD_ID = "neige-massif-20261017"

RAW = "https://raw.githubusercontent.com/alertesmeteo-hub/"
UA = "Mozilla/5.0 (compatible; AlertesMeteo-NeigeMassif/1.0)"

# id (celui de la carte), dépôt (branche data, maps/index.json), grilles dont la somme donne la précipitation totale de l'échéance
MODELES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("arome", "arome-meteofrance", ("pluie_1h", "neige")),
    ("arpege", "arpege-meteo-france", ("pluie_1h", "neige")),
    ("cep", "cep", ("pluie_1h",)),
    ("gfs", "gfs", ("pluie_1h",)),
    ("aifs", "aifs", ("pluie_1h",)),
)

# Cadre gardé : toute la chaîne des Pyrénées françaises et le Languedoc-Roussillon (la carte montre le massif du Canigou, du Carlit et du Puigmal).
BOITE = {"west": 0.0, "south": 41.8, "east": 3.6, "north": 43.4}

PLANCHER_MM = 0.1  # « rien » dans les grilles de pluie et de neige d'AROME et d'ARPEGE (0 chez les autres)
T_ECHELLE = 10  # température en dixièmes de degré (16 bits signés) : le rapport cm par mm change par marches (0 °C, 1,5 °C…), une température arrondie au quart de degré faussait de 4 % la neige cumulée d'une maille d'altitude
T_MANQUANT = -32768
P_ECHELLE = 20  # précipitations en 1/20 mm ; 6 553 mm au plus
P_MANQUANT = 65535
ETAPES_MIN = 0.5  # on ne publie pas un modèle dont moins de la moitié des échéances ont pu être lues


# ---------------------------------------------------------------------------------------------------------------------
# Géométrie : le cadre en mailles de la grille d'origine
# ---------------------------------------------------------------------------------------------------------------------
def merc(lat: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def inv_merc(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def bloc_de_grille(w: int, h: int, bornes: Dict[str, float], boite: Dict[str, float] = BOITE) -> Dict[str, Any]:
    """Les mailles entières d'une grille w × h (bornes : south, west, north, east ; colonnes régulières en longitude, lignes régulières en
    ordonnée Web Mercator, du nord au sud) qui recouvrent le cadre. Rend les rangs (col0, col1, row0, row1 ; fin exclue) et les bornes
    exactes du bloc, avec lesquelles la carte le pose."""
    marge = 1e-9
    col0 = max(0, math.floor((boite["west"] - bornes["west"]) * w / (bornes["east"] - bornes["west"]) + marge))
    col1 = min(w, math.ceil((boite["east"] - bornes["west"]) * w / (bornes["east"] - bornes["west"]) - marge))
    y_nord, y_sud = merc(bornes["north"]), merc(bornes["south"])

    def ligne(lat: float) -> float:
        return (y_nord - merc(lat)) / (y_nord - y_sud) * h

    row0 = max(0, math.floor(ligne(boite["north"]) + marge))
    row1 = min(h, math.ceil(ligne(boite["south"]) - marge))
    if col1 <= col0 or row1 <= row0:
        raise ValueError("cadre hors de la grille")
    dlon = (bornes["east"] - bornes["west"]) / w
    dy = (y_nord - y_sud) / h
    return {
        "col0": col0,
        "col1": col1,
        "row0": row0,
        "row1": row1,
        "bounds": {
            "west": round(bornes["west"] + col0 * dlon, 6),
            "east": round(bornes["west"] + col1 * dlon, 6),
            "north": round(inv_merc(y_nord - row0 * dy), 6),
            "south": round(inv_merc(y_nord - row1 * dy), 6),
        },
    }


# ---------------------------------------------------------------------------------------------------------------------
# Lecture des grilles des modèles (HKV1)
# ---------------------------------------------------------------------------------------------------------------------
def lire_sonde(octets: bytes) -> np.ndarray:
    """Grille « maps/values/…hkv.gz » en flottants (lignes × colonnes), NaN là où la valeur manque. Format : gzip, quatre octets de signature (« HKV1 »
    chez AROME et ARPEGE, « CEV1 » chez ECMWF et GFS, « AIV1 » chez AIFS : la même mise en forme), largeur et hauteur (16 bits), minimum et maximum
    (32 bits flottants), puis des entiers 16 bits : 65535 = manquant, sinon minimum + valeur / 65534 × (maximum - minimum)."""
    corps = gzip.decompress(octets)
    if len(corps) < 16:
        raise ValueError("grille illisible (trop courte)")
    w, h = struct.unpack_from("<HH", corps, 4)
    if w == 0 or h == 0 or len(corps) != 16 + 2 * w * h:
        raise ValueError(f"grille illisible ({len(corps)} octets pour {w} × {h})")
    vmin, vmax = struct.unpack_from("<ff", corps, 8)
    brut = np.frombuffer(corps, dtype="<u2", count=w * h, offset=16).reshape(h, w)
    valeurs = np.float32(vmin) + brut.astype(np.float32) * np.float32((vmax - vmin) / 65534.0)
    valeurs[brut == 65535] = np.nan
    return valeurs


def precipitation_totale(composantes: Sequence[np.ndarray]) -> np.ndarray:
    """Somme des composantes (mm), chacune au plancher de sa grille (0,1 mm : « rien ») comptant pour zéro ; NaN là où aucune n'est lue."""
    somme = np.zeros_like(composantes[0], dtype=np.float32)
    lue = np.zeros(composantes[0].shape, dtype=bool)
    for c in composantes:
        valide = np.isfinite(c)
        lue |= valide
        somme += np.where(valide & (c > PLANCHER_MM + 0.001), c, np.float32(0.0))
    somme[~lue] = np.nan
    return somme


def temperature_en_entiers(t: np.ndarray) -> np.ndarray:
    """Température (°C) en dixièmes de degré sur 16 bits signés, bornée à ±300 °C (les grilles vont de -25 à +45 °C) ; -32768 = manquant."""
    brut = np.clip(np.rint(np.nan_to_num(t, nan=0.0) * T_ECHELLE), -3000, 3000)
    return np.where(np.isfinite(t), brut, T_MANQUANT).astype(np.int16)


def precipitation_en_entiers(p: np.ndarray) -> np.ndarray:
    """Précipitation totale (mm) en 1/20 mm sur 16 bits ; 65535 = manquant."""
    brut = np.clip(np.rint(np.nan_to_num(p, nan=0.0) * P_ECHELLE), 0, 65534)
    return np.where(np.isfinite(p), brut, P_MANQUANT).astype(np.uint16)


# ---------------------------------------------------------------------------------------------------------------------
# Le fichier d'un modèle
# ---------------------------------------------------------------------------------------------------------------------
def ecrire_extrait(entete: Dict[str, Any], relief: np.ndarray, pluies: Sequence[np.ndarray], temperatures: Sequence[np.ndarray]) -> bytes:
    """Fichier d'un modèle, compressé (voir l'en-tête du module pour le format). `relief` : mètres (h × w) ; `pluies` : une grille uint16 par échéance ;
    `temperatures` : une grille int16 par échéance. La compression est fixée (pas de date) : le même contenu donne le même fichier."""
    h, w = relief.shape
    if len(pluies) != len(temperatures) or len(pluies) != len(entete["etapes"]):
        raise ValueError("autant de grilles que d'échéances")
    texte = json.dumps({**entete, "w": w, "h": h}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    texte += b" " * ((-(len(texte) + 8)) % 4)  # corps aligné sur 4 octets : la carte lit des entiers 16 bits sur place
    corps = bytearray(FORMAT + struct.pack("<I", len(texte)) + texte)
    corps += np.clip(np.rint(relief), -32768, 32767).astype("<i2").tobytes()
    for p in pluies:
        corps += p.astype("<u2").tobytes()
    for t in temperatures:
        corps += t.astype("<i2").tobytes()
    return gzip.compress(bytes(corps), compresslevel=9, mtime=0)


def lire_extrait(octets: bytes) -> Dict[str, Any]:
    """Relecture d'un fichier écrit par `ecrire_extrait` : l'en-tête, `relief` (h × w), `pluie` et `temperature` (échéances × h × w, valeurs entières)."""
    corps = gzip.decompress(octets)
    if corps[:4] != FORMAT:
        raise ValueError("pas un fichier « NSM1 »")
    (longueur,) = struct.unpack_from("<I", corps, 4)
    entete = json.loads(corps[8 : 8 + longueur].decode("utf-8"))
    w, h, n = entete["w"], entete["h"], len(entete["etapes"])
    pos = 8 + longueur
    relief = np.frombuffer(corps, dtype="<i2", count=w * h, offset=pos).reshape(h, w)
    pos += 2 * w * h
    pluie = np.frombuffer(corps, dtype="<u2", count=n * w * h, offset=pos).reshape(n, h, w)
    pos += 2 * n * w * h
    temperature = np.frombuffer(corps, dtype="<i2", count=n * w * h, offset=pos).reshape(n, h, w)
    pos += 2 * n * w * h
    if pos != len(corps):
        raise ValueError(f"fichier de {len(corps)} octets, {pos} attendus")
    return {**entete, "relief": relief, "pluie": pluie, "temperature": temperature}


# ---------------------------------------------------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------------------------------------------------
class Source:
    """Lecture des fichiers publiés des modèles, avec reprises sur les erreurs passagères (429, 5xx, coupures)."""

    def __init__(self, fils: int = 8) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        reprise = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
        self.session.mount("https://", HTTPAdapter(max_retries=reprise, pool_connections=fils, pool_maxsize=fils))

    def octets(self, url: str) -> Optional[bytes]:
        """Le contenu, ou None si le fichier n'existe pas (404) ; toute autre erreur lève une exception."""
        r = self.session.get(url, timeout=90)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content

    def index(self, depot: str) -> Dict[str, Any]:
        # le run fait partie de l'adresse des grilles ; l'index, lui, change d'un passage à l'autre : on contourne le cache du CDN (5 min)
        url = f"{RAW}{depot}/data/maps/index.json?t={int(time.time() // 60)}"
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------------------------------------------------
# Un modèle
# ---------------------------------------------------------------------------------------------------------------------
def etapes_calculables(index: Dict[str, Any], composition: Sequence[str]) -> List[Dict[str, Any]]:
    """Les échéances de l'index qui publient la température et toutes les composantes de la précipitation."""
    gardees = []
    for e in index.get("steps", []):
        sondes = e.get("probes") or {}
        if sondes.get("temperature") and all(sondes.get(c) for c in composition) and e.get("valid_time"):
            gardees.append(e)
    return gardees


def construire_modele(
    source: Any,
    modele: str,
    depot: str,
    composition: Sequence[str],
    index: Dict[str, Any],
    fils: int = 8,
    boite: Dict[str, float] = BOITE,
) -> Tuple[bytes, Dict[str, Any]]:
    """Le fichier d'un modèle et son entrée du manifeste, à partir de son index. `source.octets(url)` rend le contenu d'un fichier (None s'il manque)."""
    run = index["run_time"]
    base = f"{RAW}{depot}/data/"
    suffixe = f"?r={run}"  # le run dans l'adresse : jamais la grille du run précédent servie par un cache
    chemin_relief = ((index.get("steps") or [{}])[0].get("probes") or {}).get("altitude")
    if not chemin_relief:
        raise ValueError(f"{modele} : pas de grille d'altitude")
    brut = source.octets(base + chemin_relief + suffixe)
    if brut is None:
        raise ValueError(f"{modele} : grille d'altitude introuvable")
    relief_entier = lire_sonde(brut)
    h0, w0 = relief_entier.shape
    bloc = bloc_de_grille(w0, h0, index["bounds"], boite)
    fenetre = (slice(bloc["row0"], bloc["row1"]), slice(bloc["col0"], bloc["col1"]))
    relief = relief_entier[fenetre]
    if not np.isfinite(relief).all():
        relief = np.nan_to_num(relief, nan=0.0)

    candidates = etapes_calculables(index, composition)

    def lire_etape(e: Dict[str, Any]) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        sondes = e["probes"]
        t = source.octets(base + sondes["temperature"] + suffixe)
        composantes = [source.octets(base + sondes[c] + suffixe) for c in composition]
        if t is None or any(c is None for c in composantes):
            return None
        grille_t = lire_sonde(t)
        grilles_p = [lire_sonde(c) for c in composantes]  # type: ignore[arg-type]
        if grille_t.shape != (h0, w0) or any(g.shape != (h0, w0) for g in grilles_p):
            return None
        total = precipitation_totale([g[fenetre] for g in grilles_p])
        return precipitation_en_entiers(total), temperature_en_entiers(grille_t[fenetre])

    with ThreadPoolExecutor(max_workers=fils) as pool:
        lues = list(pool.map(lire_etape, candidates))
    retenues = [(e, r) for e, r in zip(candidates, lues) if r is not None]
    if not index.get("steps") or len(retenues) < ETAPES_MIN * len(index["steps"]):
        raise ValueError(f"{modele} : {len(retenues)} échéances lues sur {len(index.get('steps', []))}")
    manquantes = len(index["steps"]) - len(retenues)
    if manquantes:
        print(f"  {modele} : {manquantes} échéance(s) sans grilles, ignorée(s)")

    entete = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "modele": modele,
        "run_time": run,
        "generated_at": index.get("generated_at"),
        "bounds": bloc["bounds"],
        "source": {"w": w0, "h": h0},
        "t": {"echelle": T_ECHELLE},
        "p": {"echelle": P_ECHELLE},
        "etapes": [{"valid_time": e["valid_time"], "lead_hour": e.get("lead_hour")} for e, _ in retenues],
    }
    octets = ecrire_extrait(entete, relief, [r[0] for _, r in retenues], [r[1] for _, r in retenues])
    h, w = relief.shape
    entree = {
        "run_time": run,
        "fichier": f"{modele}.nsm.gz",
        "octets": len(octets),
        "w": w,
        "h": h,
        "bounds": bloc["bounds"],
        "etapes": [e["valid_time"] for e, _ in retenues],
    }
    return octets, entree


# ---------------------------------------------------------------------------------------------------------------------
# Passage complet
# ---------------------------------------------------------------------------------------------------------------------
def lire_manifeste(dossier: Optional[Path]) -> Dict[str, Any]:
    if dossier is None:
        return {}
    chemin = dossier / "index.json"
    if not chemin.is_file():
        return {}
    try:
        manifeste = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return manifeste if manifeste.get("schema_version") == SCHEMA_VERSION else {}


def passer(
    sortie: Path,
    etat: Optional[Path] = None,
    source: Optional[Any] = None,
    modeles: Optional[Sequence[str]] = None,
    fils: int = 8,
    maintenant: Optional[datetime] = None,
    lire_index: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Un passage : pour chaque modèle dont le run a changé depuis le passage précédent (`etat`, dossier de la dernière publication), lit ses grilles
    et écrit son fichier ; les autres sont repris tels quels. Rend le manifeste écrit dans `sortie`/index.json."""
    source = source or Source(fils)
    lire_index = lire_index or source.index
    sortie.mkdir(parents=True, exist_ok=True)
    precedent = lire_manifeste(etat)
    entrees: Dict[str, Any] = {}
    for modele, depot, composition in MODELES:
        if modeles and modele not in modeles:
            continue
        ancien = (precedent.get("modeles") or {}).get(modele)
        try:
            index = lire_index(depot)
            run = index.get("run_time")
            if ancien and etat and ancien.get("run_time") == run and (etat / ancien["fichier"]).is_file():
                shutil.copyfile(etat / ancien["fichier"], sortie / ancien["fichier"])
                entrees[modele] = ancien
                print(f"{modele} : run {run} déjà publié")
                continue
            debut = time.time()
            octets, entree = construire_modele(source, modele, depot, composition, index, fils)
            # les grilles ont pu changer pendant la lecture (nouveau run publié) : on ne publie pas un mélange
            if lire_index(depot).get("run_time") != run:
                raise ValueError(f"{modele} : nouveau run publié pendant la lecture")
            (sortie / entree["fichier"]).write_bytes(octets)
            entrees[modele] = entree
            print(f"{modele} : run {run}, {len(entree['etapes'])} échéances, {len(octets) / 1e3:.0f} Ko, {time.time() - debut:.0f} s")
        except Exception as erreur:  # un modèle en panne ne prive pas les autres : on garde sa dernière version publiée
            print(f"::warning::{modele} : {erreur}")
            if ancien and etat and (etat / ancien["fichier"]).is_file():
                shutil.copyfile(etat / ancien["fichier"], sortie / ancien["fichier"])
                entrees[modele] = ancien
    manifeste = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "generated_at": (maintenant or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "boite": BOITE,
        "modeles": entrees,
    }
    (sortie / "index.json").write_text(json.dumps(manifeste, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return manifeste


def main(argv: Optional[Sequence[str]] = None) -> int:
    parseur = argparse.ArgumentParser(description="Extrait de la neige du massif (température, précipitations, relief) de nos modèles.")
    parseur.add_argument("--sortie", required=True, help="dossier des fichiers publiés")
    parseur.add_argument("--etat", help="dossier de la publication précédente : les modèles dont le run n'a pas changé en sont repris")
    parseur.add_argument("--modeles", help="liste d'identifiants séparés par des virgules (par défaut tous)")
    parseur.add_argument("--fils", type=int, default=8, help="lectures simultanées")
    args = parseur.parse_args(argv)
    modeles = [m.strip() for m in args.modeles.split(",")] if args.modeles else None
    manifeste = passer(Path(args.sortie), Path(args.etat) if args.etat else None, modeles=modeles, fils=args.fils)
    if not manifeste["modeles"]:
        print("::error::aucun modèle publié")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

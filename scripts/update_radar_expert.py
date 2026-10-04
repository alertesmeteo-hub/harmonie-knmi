#!/usr/bin/env python3
"""Radar Expert : données brutes polaires d'une station radar Météo-France -> images géoréférencées.

Source : API Paquet Radar Météo-France (DPPaquetRadar/v1/station/paquet), produits PAG
(réflectivité, vitesse radiale) et PAM (réflectivité, corrélation rhoHV, ZDR, phase PhiDP)
au format BUFR. Le paquet d'une station contient les 15 dernières minutes (3 pas de 5 min x
tours d'antenne A à F). Chaque produit est projeté en grille Web Mercator (compatible
Leaflet) et écrit en PNG indexé : <sortie>/<station>/<param>/<élévation>/<AAAAMMJJhhmm>.png.
Un manifeste <sortie>/<station>/index.json liste les paramètres, élévations et horodatages.

Codages (descriptifs techniques Météo-France) :
  Zh    : code N -> dBZ = N - 11 (0/1 : sous le bruit, 255 : manquant)
  V     : code N -> m/s = -60.25 + 0.5 N (positif vers le radar, 255 : manquant)
  rhoHV : code N -> 0.30 + N/100 (255 : manquant)
  ZDR   : code N -> -10 + N/10 dB (255 : manquant)
  PhiDP : code N (16 bits) -> degrés (65535 : manquant)
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import math
import os
import re
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import radar_bufr as rb  # noqa: E402

LOG = logging.getLogger("radar-expert")
API = "https://public-api.meteofrance.fr/public/DPPaquetRadar/v1/station/paquet"
EARTH_KM = 6371.0
RADIUS_KM = 256.0
SIZE = 640

STATION_NAMES = {
    36: "NOYAL", 37: "AJACCIO", 38: "ST-REMY", 40: "ABBEVILLE", 41: "BORDEAUX", 42: "BOURGES",
    43: "MOUCHEROTTE", 44: "BRIVE GREZES", 45: "FALAISE CAEN", 47: "NANCY", 49: "NIMES",
    50: "TOULOUSE", 51: "TRAPPES", 52: "ARCIS TROYES", 53: "SEMBADEL", 54: "TREILLIERES",
    55: "BOLLENE", 56: "PLABENNEC", 57: "OPOUL", 58: "ST.NIZIER", 59: "COLLOBRIERES",
    60: "VARS", 61: "ALERIA", 62: "MONTCLAR", 63: "L'AVESNOIS", 64: "CHERVES",
    65: "BLAISY-HAUT", 66: "MOMUY", 67: "MONTANCY", 68: "MAUREL", 69: "COLOMBIS",
}


def hexrgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# --- Paramètres : étendue des valeurs affichées, palette et légende -----------------------------
PARAMS: dict[str, dict] = {
    "reflectivity": {
        "label": "Réflectivité", "unit": "dBZ", "vmin": 0.0, "vmax": 70.0,
        "stops": [(0, "#4b6f8f"), (5, "#6ec6f5"), (15, "#1e88e5"), (25, "#2e7d32"), (30, "#66bb6a"), (35, "#f7e83a"),
                  (40, "#fdb827"), (45, "#f4511e"), (50, "#d50000"), (55, "#ad1457"), (60, "#8e24aa"), (65, "#e1bee7"), (70, "#ffffff")],
        "legend": [0, 10, 20, 30, 40, 50, 60, 70],
    },
    "velocity": {
        "label": "Vitesse radiale", "unit": "km/h", "vmin": -180.0, "vmax": 180.0,
        "stops": [(-180, "#ff2fd6"), (-135, "#3a1fd1"), (-105, "#1e9bff"), (-75, "#19e6d2"), (-45, "#12b83a"), (-15, "#0a5a1c"),
                  (0, "#808080"), (15, "#5a1010"), (45, "#d41414"), (75, "#ff6a00"), (105, "#ffa500"), (135, "#ffd21f"), (180, "#ffff66")],
        "legend": [-180, -135, -90, -45, 0, 45, 90, 135, 180],
    },
    "rhohv": {
        "label": "Coefficient de corrélation", "unit": "", "vmin": 0.3, "vmax": 1.05,
        "stops": [(0.3, "#5e2750"), (0.7, "#c0392b"), (0.85, "#f39c12"), (0.93, "#f1e63a"), (0.97, "#4cbf4c"), (1.0, "#1f7a3a"), (1.05, "#0b4f2a")],
        "legend": [0.3, 0.5, 0.7, 0.85, 0.93, 0.97, 1.0],
    },
    "zdr": {
        "label": "ZDR", "unit": "dB", "vmin": -4.0, "vmax": 8.0,
        "stops": [(-4, "#3b1f8f"), (-1, "#2f7fff"), (0, "#9ad8ff"), (1, "#33b34a"), (2, "#f5e630"), (3, "#f39c12"), (5, "#d50000"), (8, "#ff4fd8")],
        "legend": [-4, -2, 0, 1, 2, 3, 5, 8],
    },
    "phidp": {
        "label": "PhiDP", "unit": "°", "vmin": 0.0, "vmax": 360.0,
        "stops": [(0, "#3b4cc0"), (90, "#3cb44b"), (180, "#f5e630"), (270, "#e8590c"), (360, "#b0004a")],
        "legend": [0, 60, 120, 180, 240, 300, 360],
    },
}


def palette(param: str) -> tuple[bytes, bytes]:
    """Palette indexée de 256 entrées (index 0 = transparent) et table d'opacité."""
    spec = PARAMS[param]
    xs = [s[0] for s in spec["stops"]]
    cols = np.array([hexrgb(s[1]) for s in spec["stops"]], dtype=float)
    pal = np.zeros((256, 3), dtype=np.uint8)
    alpha = np.full(256, 235, dtype=np.uint8)
    alpha[0] = 0
    for i in range(1, 255):
        v = spec["vmin"] + (i - 1) / 253 * (spec["vmax"] - spec["vmin"])
        pal[i] = [int(np.interp(v, xs, cols[:, c])) for c in range(3)]
    if param == "reflectivity":
        for i in range(1, 255):
            v = spec["vmin"] + (i - 1) / 253 * (spec["vmax"] - spec["vmin"])
            alpha[i] = int(np.clip(v / 8.0, 0.25, 1.0) * 235)
    return pal.tobytes(), alpha.tobytes()


def to_index(values: np.ndarray, valid: np.ndarray, param: str) -> np.ndarray:
    spec = PARAMS[param]
    idx = np.rint((values - spec["vmin"]) / (spec["vmax"] - spec["vmin"]) * 253).astype(np.int32) + 1
    idx = np.clip(idx, 1, 254)
    idx[~valid] = 0
    return idx.astype(np.uint8)


# --- Grille de sortie (Web Mercator) ------------------------------------------------------------
def merc_y(lat: np.ndarray | float) -> np.ndarray | float:
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def inv_merc_y(y: np.ndarray) -> np.ndarray:
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


class Grid:
    def __init__(self, lat0: float, lon0: float):
        dlat = math.degrees(RADIUS_KM / EARTH_KM)
        dlon = dlat / math.cos(math.radians(lat0))
        self.south, self.north = lat0 - dlat, lat0 + dlat
        self.west, self.east = lon0 - dlon, lon0 + dlon
        ys = np.linspace(merc_y(self.north), merc_y(self.south), SIZE)
        lats = inv_merc_y(ys)
        lons = np.linspace(self.west, self.east, SIZE)
        lon_g, lat_g = np.meshgrid(lons, lats)
        north = np.radians(lat_g - lat0) * EARTH_KM
        east = np.radians(lon_g - lon0) * EARTH_KM * np.cos(np.radians((lat_g + lat0) / 2))
        self.ground_km = np.hypot(east, north)
        self.azimuth = np.degrees(np.arctan2(east, north)) % 360.0

    def lookup(self, elevation: float, n_az: int, gate_km: float, n_gates: int):
        slant = self.ground_km / max(math.cos(math.radians(min(elevation, 80.0))), 0.15)
        ia = np.clip((self.azimuth / (360.0 / n_az)).astype(np.int32), 0, n_az - 1)
        ig = (slant / gate_km).astype(np.int32)
        inside = (ig < n_gates) & (self.ground_km <= RADIUS_KM)
        return ia, np.clip(ig, 0, n_gates - 1), inside


def station_position(msg: bytes) -> tuple[float, float]:
    lat = lon = None
    for label, _n, value in rb.parse_message(msg, stop_label="006001"):
        if label == "005001":
            lat = value
        elif label == "006001":
            lon = value
    if lat is None or lon is None:
        raise RuntimeError("position du radar introuvable dans le BUFR")
    return float(lat), float(lon)


# --- Téléchargement -----------------------------------------------------------------------------
def download_packet(station: int, token: str) -> bytes:
    failures: list[str] = []
    for label, headers in (("apikey", {"apikey": token}), ("Bearer", {"Authorization": f"Bearer {token}"})):
        r = requests.get(API, params={"id_station": station}, headers={**headers, "Accept": "*/*"}, timeout=(20, 120))
        LOG.info("Paquet station %s (%s) -> HTTP %s, %.1f Mo", station, label, r.status_code, len(r.content) / 1e6)
        if r.status_code == 200 and len(r.content) > 10_000:
            return r.content
        failures.append(f"{label}: HTTP {r.status_code}")
    raise RuntimeError("téléchargement du paquet refusé : " + "; ".join(failures))


def read_packet(blob: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.isfile():
                data = tar.extractfile(member)
                if data is not None:
                    files[Path(member.name).name] = data.read()
    return files


# --- Traitement ---------------------------------------------------------------------------------
NAME_RE = re.compile(r"^T_(PAG|PAM)([A-H])(\d{2})_C_\w+_(\d{14})\.bufr(?:\.gz)?$")


def save_png(path: Path, index: np.ndarray, param: str) -> None:
    img = Image.fromarray(index, mode="P")
    pal, alpha = palette(param)
    img.putpalette(pal)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True, transparency=alpha)


def process(files: dict[str, bytes], station: int, out_dir: Path) -> tuple[Grid, int]:
    groups: dict[tuple[str, str], dict[str, bytes]] = {}
    for name, blob in files.items():
        m = NAME_RE.match(name)
        if not m:
            continue
        product, tour, _sid, stamp = m.groups()
        groups.setdefault((tour, stamp[:12]), {})[product] = rb.gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    if not groups:
        raise RuntimeError("aucun fichier PAG/PAM dans le paquet")

    first = next(iter(groups.values()))
    first_msg = next(rb.split_messages(first.get("PAG") or first["PAM"]))
    lat0, lon0 = station_position(first_msg)
    grid = Grid(lat0, lon0)
    written = 0

    for (tour, stamp), prods in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        pag = list(rb.split_messages(prods["PAG"])) if "PAG" in prods else []
        pam = list(rb.split_messages(prods["PAM"])) if "PAM" in prods else []
        # le 1er message PAM utilise une table locale (version 20) absente des tables publiées
        ref_msg = pam[1] if len(pam) > 1 else (pag[0] if pag else None)
        if ref_msg is None:
            continue
        elev = rb.elevation(ref_msg)
        if elev is None:
            continue
        elev_key = f"{elev:.1f}"

        def emit(param: str, values: np.ndarray, valid: np.ndarray, spec: tuple[int, float, int]) -> None:
            nonlocal written
            n_az, gate_km, n_gates = spec
            ia, ig, inside = grid.lookup(elev, n_az, gate_km, n_gates)
            vals = values[ia, ig]
            ok = valid[ia, ig] & inside
            target = out_dir / str(station) / param / elev_key / f"{stamp}.png"
            if target.exists():
                return
            save_png(target, to_index(vals, ok, param), param)
            written += 1

        zh_valid = None
        if len(pam) >= 4:
            n = 720 * 1066
            zh_raw = rb.tail_matrix(pam[0], n).reshape(720, 1066)
            dbz = zh_raw.astype(np.float32) - 11.0
            zh_valid = (zh_raw >= 11) & (zh_raw < 255)
            spec = (720, 0.24, 1066)
            emit("reflectivity", dbz, zh_valid, spec)
            rho_raw = rb.tail_matrix(pam[1], n).reshape(720, 1066)
            emit("rhohv", 0.30 + rho_raw.astype(np.float32) / 100.0, zh_valid & (rho_raw < 255), spec)
            zdr_raw = rb.tail_matrix(pam[2], n).reshape(720, 1066)
            emit("zdr", -10.0 + zdr_raw.astype(np.float32) / 10.0, zh_valid & (zdr_raw < 255), spec)
            phi_raw = rb.tail_matrix(pam[3], n, ">u2").reshape(720, 1066)
            emit("phidp", phi_raw.astype(np.float32), zh_valid & (phi_raw < 65535), spec)
        if len(pag) >= 3:
            zh = rb.tail_matrix(pag[0], 720 * 256).reshape(720, 256)
            v_raw = rb.tail_matrix(pag[2], 360 * 256).reshape(360, 256)
            # masque : réflectivité PAG ré-échantillonnée sur la grille 1 deg (valeur max des deux demi-degrés)
            zh_pairs = zh.reshape(360, 2, 256)
            zmask = ((zh_pairs >= 11) & (zh_pairs < 255)).any(axis=1)
            vel_kmh = -(-60.25 + 0.5 * v_raw.astype(np.float32)) * 3.6  # signe inversé : positif = s'éloigne
            emit("velocity", vel_kmh, zmask & (v_raw < 242), (360, 1.0, 256))
            if zh_valid is None:
                emit("reflectivity", zh.astype(np.float32) - 11.0, (zh >= 11) & (zh < 255), (720, 1.0, 256))
    return grid, written


def build_manifest(out_dir: Path, station: int, grid: Grid, keep_minutes: int) -> dict:
    root = out_dir / str(station)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=keep_minutes)
    params: dict[str, dict] = {}
    for param, spec in PARAMS.items():
        elevations: dict[str, list[str]] = {}
        for edir in sorted((root / param).glob("*")) if (root / param).exists() else []:
            stamps = []
            for f in sorted(edir.glob("*.png")):
                ts = datetime.strptime(f.stem, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    f.unlink()
                else:
                    stamps.append(f.stem)
            if stamps:
                elevations[edir.name] = stamps
            else:
                try:
                    edir.rmdir()
                except OSError:
                    pass
        stops = [{"v": s[0], "c": s[1]} for s in spec["stops"]]
        params[param] = {
            "label": spec["label"], "unit": spec["unit"], "vmin": spec["vmin"], "vmax": spec["vmax"],
            "stops": stops, "legend": spec["legend"], "elevations": elevations,
        }
    lat0 = (grid.north + grid.south) / 2
    manifest = {
        "schema_version": 1,
        "station": station,
        "name": STATION_NAMES.get(station, str(station)),
        "lat": round(lat0, 5),
        "lon": round((grid.east + grid.west) / 2, 5),
        "radius_km": RADIUS_KM,
        "size": SIZE,
        "bounds": [[round(grid.south, 5), round(grid.west, 5)], [round(grid.north, 5), round(grid.east, 5)]],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keep_minutes": keep_minutes,
        "source": "Météo-France — données radar polaires (PAG/PAM)",
        "params": params,
    }
    (root / "index.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--station", type=int, default=57)
    ap.add_argument("--output-dir", default="build/radar-expert")
    ap.add_argument("--packet", help="paquet .tar.gz local (test hors API)")
    ap.add_argument("--keep-minutes", type=int, default=75)
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")

    if args.packet:
        blob = Path(args.packet).read_bytes()
    else:
        token = os.environ.get("METEOFRANCE_RADAR_TOKEN") or os.environ.get("METEOFRANCE_PAQUET_API_KEY")
        if not token:
            LOG.error("METEOFRANCE_RADAR_TOKEN absent")
            return 2
        blob = download_packet(args.station, token)

    out_dir = Path(args.output_dir)
    grid, written = process(read_packet(blob), args.station, out_dir)
    manifest = build_manifest(out_dir, args.station, grid, args.keep_minutes)
    total = sum(len(v) for p in manifest["params"].values() for v in p["elevations"].values())
    LOG.info("Radar Expert %s : %d images écrites, %d au total", manifest["name"], written, total)
    return 0


if __name__ == "__main__":
    sys.exit(main())

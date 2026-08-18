#!/usr/bin/env python3
"""Construit un nowcast pluie +0/+15/+30/+45/+60 min depuis les mosaïques radar Météo-France.

Source : API Paquet Radar Météo-France (DPPaquetRadar), produit IPRN20_C_LFPW
(mosaïque nationale lame d'eau 5 min, 500 m, HDF5).

Le script :
- télécharge le paquet mosaïque du dernier quart d'heure ;
- extrait les 3 dernières mosaïques 5 min ;
- estime un champ de déplacement par flux optique ;
- advecte la dernière lame d'eau jusqu'à +60 min ;
- échantillonne les 34k+ communes du catalogue HARMONIE ;
- publie un index + un petit JSON par département.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import h5py
import numpy as np
import pyproj
import requests

LOG = logging.getLogger("radar_nowcast")
API_URL = "https://public-api.meteofrance.fr/public/DPPaquetRadar/v1/mosaique/paquet"
PRODUCT = "IPRN20_C_LFPW"
SCHEMA_VERSION = 1
LEADS = (0, 15, 30, 45, 60)
FILE_RE = re.compile(r"T_IPRN20_C_LFPW_(\d{14})\.h5$", re.I)


@dataclass(frozen=True)
class Grid:
    crs: pyproj.CRS
    transformer: pyproj.Transformer
    width: int
    height: int
    x_left: float
    x_step: float
    y_top: float
    y_step: float

    def pixel(self, lon: float, lat: float) -> tuple[float, float]:
        x, y = self.transformer.transform(lon, lat)
        col = (x - self.x_left) / self.x_step - 0.5
        row = (y - self.y_top) / self.y_step - 0.5
        return row, col


@dataclass
class RadarFrame:
    timestamp: datetime
    rain_rate: np.ndarray
    quality: np.ndarray
    grid: Grid
    source: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", default="config/communes-france.json")
    p.add_argument("--output-dir", default="build/radar")
    p.add_argument("--package", help="Archive paquetradar locale pour test hors API")
    p.add_argument("--downsample", type=int, default=4)
    p.add_argument("--self-test", action="store_true")
    return p.parse_args()


def text_attr(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.ndarray) and value.size == 1:
        return text_attr(value.reshape(-1)[0])
    return str(value)


def scalar_attr(value: Any, default: float | None = None) -> float | None:
    try:
        if isinstance(value, np.ndarray):
            value = value.reshape(-1)[0]
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError, IndexError):
        return default


def attrs_dict(group: h5py.Group | h5py.Dataset | None) -> dict[str, Any]:
    if group is None:
        return {}
    return {str(k).lower(): v for k, v in group.attrs.items()}


def recursive_datasets(group: h5py.Group) -> list[h5py.Dataset]:
    found: list[h5py.Dataset] = []
    def visit(_name: str, obj: Any) -> None:
        if isinstance(obj, h5py.Dataset) and obj.ndim == 2:
            found.append(obj)
    group.visititems(visit)
    return found


def quantity_of(ds: h5py.Dataset) -> str:
    groups: list[Any] = [ds]
    parent = ds.parent
    if parent is not None:
        groups.append(parent)
        if "what" in parent and isinstance(parent["what"], h5py.Group):
            groups.append(parent["what"])
    for g in groups:
        attrs = attrs_dict(g)
        if "quantity" in attrs:
            return text_attr(attrs["quantity"]).upper()
    return ""


def data_calibration(ds: h5py.Dataset) -> tuple[float, float, float | None, float | None]:
    candidates: list[Any] = [ds]
    parent = ds.parent
    if parent is not None:
        candidates.append(parent)
        if "what" in parent and isinstance(parent["what"], h5py.Group):
            candidates.insert(0, parent["what"])
    merged: dict[str, Any] = {}
    for g in candidates:
        merged.update(attrs_dict(g))
    gain = scalar_attr(merged.get("gain"), 0.01) or 0.01
    offset = scalar_attr(merged.get("offset"), 0.0) or 0.0
    nodata = scalar_attr(merged.get("nodata"), 65535.0)
    undetect = scalar_attr(merged.get("undetect"), None)
    return gain, offset, nodata, undetect


def pick_rain_dataset(h5: h5py.File) -> h5py.Dataset:
    datasets = recursive_datasets(h5)
    if not datasets:
        raise RuntimeError("aucune matrice 2D dans le HDF5 radar")
    scored = []
    for ds in datasets:
        if min(ds.shape) < 1000:
            continue
        q = quantity_of(ds)
        path = ds.name.lower()
        score = 0
        if q in {"ACRR", "RATE", "RR", "DBZH"}: score += 100
        if q == "ACRR": score += 100
        if "data" in path: score += 10
        if "quality" in path or q in {"QIND", "QUALITY"}: score -= 100
        scored.append((score, ds))
    if not scored:
        raise RuntimeError("matrice radar nationale introuvable")
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def pick_quality_dataset(h5: h5py.File, shape: tuple[int, int]) -> h5py.Dataset | None:
    candidates = []
    for ds in recursive_datasets(h5):
        if ds.shape != shape:
            continue
        q = quantity_of(ds)
        path = ds.name.lower()
        score = 0
        if q in {"QIND", "QUALITY"}: score += 100
        if "quality" in path: score += 50
        if q == "ACRR": score -= 100
        candidates.append((score, ds))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates[0][0] > 0 else None


def attr_from_groups(h5: h5py.File, key: str) -> Any | None:
    key = key.lower()
    groups: list[Any] = [h5]
    for name in ("where", "what", "how"):
        if name in h5 and isinstance(h5[name], h5py.Group):
            groups.append(h5[name])
    for g in groups:
        attrs = attrs_dict(g)
        if key in attrs:
            return attrs[key]
    return None


def build_grid(h5: h5py.File, shape: tuple[int, int]) -> Grid:
    projdef = attr_from_groups(h5, "projdef")
    if projdef is None:
        raise RuntimeError("projection radar absente du HDF5")
    crs = pyproj.CRS.from_user_input(text_attr(projdef))
    transformer = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def corner(prefix: str) -> tuple[float, float] | None:
        lon = scalar_attr(attr_from_groups(h5, prefix + "_lon"))
        lat = scalar_attr(attr_from_groups(h5, prefix + "_lat"))
        if lon is None or lat is None:
            return None
        return transformer.transform(lon, lat)

    ll, ul, ur, lr = corner("ll"), corner("ul"), corner("ur"), corner("lr")
    if not all((ll, ul, ur, lr)):
        raise RuntimeError("coins géographiques radar absents (LL/UL/UR/LR)")

    height, width = shape
    x_left = (ll[0] + ul[0]) / 2.0
    x_right = (lr[0] + ur[0]) / 2.0
    y_top = (ul[1] + ur[1]) / 2.0
    y_bottom = (ll[1] + lr[1]) / 2.0
    x_step = (x_right - x_left) / width
    y_step = (y_bottom - y_top) / height
    if abs(x_step) < 1 or abs(y_step) < 1:
        raise RuntimeError("géométrie radar incohérente")
    return Grid(crs, transformer, width, height, x_left, x_step, y_top, y_step)


def load_frame(path: Path) -> RadarFrame:
    match = FILE_RE.search(path.name)
    if not match:
        raise RuntimeError(f"nom de mosaïque inattendu: {path.name}")
    timestamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    with h5py.File(path, "r") as h5:
        ds = pick_rain_dataset(h5)
        raw = ds[...]
        gain, offset, nodata, undetect = data_calibration(ds)
        rain = raw.astype(np.float32)
        invalid = ~np.isfinite(rain)
        if nodata is not None:
            invalid |= raw == nodata
        if undetect is not None:
            rain[raw == undetect] = 0.0
        rain = rain * gain + offset
        rain[invalid] = 0.0
        rain = np.clip(rain, 0.0, 100.0) * 12.0  # cumul 5 min -> mm/h

        qds = pick_quality_dataset(h5, ds.shape)
        if qds is None:
            quality = np.where(invalid, 0.0, 100.0).astype(np.float32)
        else:
            qraw = qds[...]
            qgain, qoffset, qnodata, _ = data_calibration(qds)
            quality = qraw.astype(np.float32) * qgain + qoffset
            if qnodata is not None:
                quality[qraw == qnodata] = 0.0
            if np.nanmax(quality) <= 1.5:
                quality *= 100.0
            quality = np.clip(np.nan_to_num(quality, nan=0.0), 0.0, 100.0)
        grid = build_grid(h5, ds.shape)
    return RadarFrame(timestamp, rain, quality, grid, path.name)


def download_package(token: str, target: Path) -> None:
    headers = {"Accept": "*/*", "Authorization": f"Bearer {token}", "User-Agent": "alertesmeteo-radar-nowcast/1.0"}
    with requests.get(API_URL, headers=headers, timeout=(20, 90), stream=True) as r:
        if r.status_code == 401:
            raise RuntimeError("token Météo-France refusé (401) : vérifiez l'abonnement API Radar et le secret")
        if r.status_code == 403:
            raise RuntimeError("accès API Radar interdit (403) : vérifiez l'abonnement DPPaquetRadar")
        r.raise_for_status()
        with target.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk: f.write(chunk)


def safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise RuntimeError("archive radar contient un chemin dangereux")
        tar.extractall(destination, filter="data")


def resize_mean(field: np.ndarray, factor: int) -> np.ndarray:
    h, w = field.shape
    size = (max(1, w // factor), max(1, h // factor))
    return cv2.resize(field.astype(np.float32), size, interpolation=cv2.INTER_AREA)


def normalized_for_flow(rate: np.ndarray) -> np.ndarray:
    x = np.log1p(np.clip(rate, 0.0, 30.0)) / np.log1p(30.0)
    x[rate < 0.05] = 0.0
    return cv2.GaussianBlur(x.astype(np.float32), (0, 0), 1.2)


def calc_flow(prev: np.ndarray, curr: np.ndarray) -> np.ndarray:
    # Une translation globale (phase correlation) stabilise le calcul sur les
    # grandes bandes pluvieuses, puis Farneback ajoute les déformations locales.
    p = normalized_for_flow(prev)
    c = normalized_for_flow(curr)
    try:
        (dx, dy), response = cv2.phaseCorrelate(p, c)
    except cv2.error:
        dx, dy, response = 0.0, 0.0, 0.0
    if not (math.isfinite(dx) and math.isfinite(dy)) or response < 0.03:
        dx, dy = 0.0, 0.0
    global_flow = np.zeros((*p.shape, 2), dtype=np.float32)
    global_flow[..., 0] = float(np.clip(dx, -80.0, 80.0))
    global_flow[..., 1] = float(np.clip(dy, -80.0, 80.0))
    shifted = warp(p, global_flow, 1.0)
    residual = cv2.calcOpticalFlowFarneback(
        shifted, c, None, pyr_scale=0.5, levels=4, winsize=25, iterations=4,
        poly_n=7, poly_sigma=1.5, flags=0,
    )
    return global_flow + residual


def warp(field: np.ndarray, flow: np.ndarray, multiplier: float) -> np.ndarray:
    h, w = field.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    map_x = gx - flow[..., 0] * multiplier
    map_y = gy - flow[..., 1] * multiplier
    return cv2.remap(field.astype(np.float32), map_x, map_y, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)


def flow_confidence(prev: np.ndarray, curr: np.ndarray, flow: np.ndarray) -> float:
    rainy = (prev > 0.1) | (curr > 0.1)
    if rainy.sum() < 200:
        return 0.82  # champ essentiellement sec : confiance raisonnable dans le "sec"
    predicted = warp(prev, flow, 1.0)
    mae = float(np.mean(np.abs(np.sqrt(predicted[rainy]) - np.sqrt(curr[rainy]))))
    return float(np.clip(math.exp(-mae / 1.2), 0.25, 0.95))


def bilinear(field: np.ndarray, row: float, col: float) -> float | None:
    h, w = field.shape
    if row < 0 or col < 0 or row > h - 1 or col > w - 1:
        return None
    r0, c0 = int(math.floor(row)), int(math.floor(col))
    r1, c1 = min(r0 + 1, h - 1), min(c0 + 1, w - 1)
    fr, fc = row - r0, col - c0
    return float(
        field[r0, c0] * (1-fr) * (1-fc) +
        field[r0, c1] * (1-fr) * fc +
        field[r1, c0] * fr * (1-fc) +
        field[r1, c1] * fr * fc
    )


def load_communes(path: Path) -> dict[str, list[list[Any]]]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    communes = payload.get("communes") or []
    if len(communes) < 34000:
        raise RuntimeError("catalogue communal incomplet")
    by_dep: dict[str, list[list[Any]]] = {}
    for c in communes:
        if not isinstance(c, list) or len(c) < 7:
            continue
        by_dep.setdefault(str(c[2]), []).append(c)
    return by_dep


def compute(frames: list[RadarFrame], factor: int) -> tuple[list[np.ndarray], float, np.ndarray]:
    frames = sorted(frames, key=lambda f: f.timestamp)
    if len(frames) < 2:
        raise RuntimeError("au moins 2 mosaïques radar sont nécessaires")
    frames = frames[-3:]
    shape = frames[-1].rain_rate.shape
    if any(f.rain_rate.shape != shape for f in frames):
        raise RuntimeError("les mosaïques radar n'ont pas la même grille")

    low = [resize_mean(f.rain_rate, factor) for f in frames]
    flows = []
    for a, b in zip(low[:-1], low[1:]):
        flows.append(calc_flow(a, b))
    flow = flows[-1]
    if len(flows) >= 2:
        flow = 0.35 * flows[-2] + 0.65 * flows[-1]
    flow = cv2.GaussianBlur(flow, (0, 0), 1.1)
    confidence = flow_confidence(low[-2], low[-1], flow)

    gap = max(1.0, (frames[-1].timestamp - frames[-2].timestamp).total_seconds() / 60.0)
    nowcasts = []
    for lead in LEADS:
        if lead == 0:
            field = low[-1].copy()
        else:
            field = warp(low[-1], flow, lead / gap)
            # légère décroissance avec l'échéance pour limiter les extrapolations trop agressives
            field *= math.exp(-lead / 240.0)
        nowcasts.append(np.clip(field, 0.0, 300.0).astype(np.float32))
    quality = resize_mean(frames[-1].quality, factor)
    return nowcasts, confidence, quality


def write_output(output_dir: Path, by_dep: dict[str, list[list[Any]]], frame: RadarFrame,
                 fields: list[np.ndarray], motion_conf: float, quality: np.ndarray, factor: int) -> None:
    out = output_dir.resolve()
    if out.exists(): shutil.rmtree(out)
    dep_dir = out / "departements"
    dep_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc)
    age_minutes = max(0.0, (generated - frame.timestamp).total_seconds() / 60.0)
    dep_index: dict[str, Any] = {}
    total_communes = 0

    for dep, communes in sorted(by_dep.items()):
        rows: dict[str, list[Any]] = {}
        for c in communes:
            code = str(c[0])
            lat, lon = float(c[5]), float(c[6])
            row, col = frame.grid.pixel(lon, lat)
            row /= factor; col /= factor
            rates = [bilinear(field, row, col) for field in fields]
            q = bilinear(quality, row, col)
            if any(v is None for v in rates) or q is None:
                continue
            q = float(np.clip(q, 0.0, 100.0))
            conf = motion_conf * (q / 100.0)
            vals = [round(max(0.0, float(v)), 1) for v in rates]
            rows[code] = vals + [round(conf, 2), int(round(q))]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "generated_at": generated.isoformat().replace("+00:00", "Z"),
            "radar_time": frame.timestamp.isoformat().replace("+00:00", "Z"),
            "department": dep,
            "columns": ["rate_0", "rate_15", "rate_30", "rate_45", "rate_60", "confidence", "quality"],
            "communes": rows,
        }
        path = dep_dir / f"{dep}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        dep_index[dep] = {"file": f"departements/{dep}.json", "communes": len(rows), "size_bytes": path.stat().st_size}
        total_communes += len(rows)

    index = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "radar_time": frame.timestamp.isoformat().replace("+00:00", "Z"),
        "radar_age_minutes_at_generation": round(age_minutes, 1),
        "source": {
            "provider": "Météo-France",
            "api": "DPPaquetRadar/v1/mosaique/paquet",
            "product": PRODUCT,
            "label": "Mosaïque nationale lame d'eau 5 min",
            "native_resolution_m": 500,
            "frequency_minutes": 5,
        },
        "nowcast": {
            "algorithm": "dense optical flow (Farneback) + advection",
            "leads_minutes": list(LEADS),
            "downsample_factor": factor,
            "motion_confidence": round(motion_conf, 2),
            "note": "Extrapolation radar à très courte échéance ; HARMONIE est fusionné dans le plugin WordPress.",
        },
        "coverage": {"communes": total_communes, "departments": len(dep_index)},
        "departments": dep_index,
    }
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    LOG.info("Radar nowcast publié : %s communes, %s départements", total_communes, len(dep_index))


def self_test() -> None:
    # Test purement algorithmique du flux/advection, sans API ni HDF5.
    a = np.zeros((256, 256), np.float32)
    cv2.circle(a, (95, 130), 18, 8.0, -1)
    b = np.zeros_like(a)
    cv2.circle(b, (105, 130), 18, 8.0, -1)
    flow = calc_flow(a, b)
    c = warp(b, flow, 1.0)
    x = np.unravel_index(np.argmax(c), c.shape)[1]
    if x < 108:
        raise RuntimeError(f"self-test advection échoué (x={x})")
    print("SELF-TEST OK")


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
                        format="%(asctime)s | %(levelname)s | %(message)s")
    if args.self_test:
        self_test(); return 0
    if args.downsample not in (2, 3, 4, 5, 6, 8):
        raise ValueError("--downsample doit être 2,3,4,5,6 ou 8")

    by_dep = load_communes(Path(args.catalog))
    with tempfile.TemporaryDirectory(prefix="radar-mf-") as td:
        temp = Path(td)
        if args.package:
            archive = Path(args.package).resolve()
        else:
            token = os.getenv("METEOFRANCE_RADAR_TOKEN", "").strip()
            if not token:
                raise RuntimeError("secret METEOFRANCE_RADAR_TOKEN absent")
            archive = temp / "radar.tar.gz"
            download_package(token, archive)
        extracted = temp / "extract"
        extracted.mkdir()
        safe_extract(archive, extracted)
        files = sorted([p for p in extracted.rglob("*.h5") if FILE_RE.search(p.name)],
                       key=lambda p: FILE_RE.search(p.name).group(1))
        if len(files) < 2:
            names = ", ".join(p.name for p in extracted.iterdir())
            raise RuntimeError(f"moins de 2 mosaïques {PRODUCT} trouvées dans le paquet. Contenu: {names[:1000]}")
        frames = [load_frame(p) for p in files[-3:]]
        LOG.info("Mosaïques radar : %s", ", ".join(f.timestamp.isoformat() for f in frames))
        fields, conf, quality = compute(frames, args.downsample)
        write_output(Path(args.output_dir), by_dep, frames[-1], fields, conf, quality, args.downsample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

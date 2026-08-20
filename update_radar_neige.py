#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alertes-Meteo.com — Radar neige et limite pluie/neige
Version 1.0.0

Fusionne :
- mosaïque radar Météo-France IPRN20_C_LFPW (500 m / 5 min) ;
- AROME-PI WCS pour les quantités de précipitations neige/liquide ;
- AROME-PI WCS pour l'altitude de la température humide proche de 0/1 °C.

Sorties :
- index.json
- frames/radar-neige-YYYYMMDDHHMM.png
- limite-pluie-neige.png

Le script conserve les images précédentes fournies via --previous-dir pour obtenir
une animation glissante d'environ une heure malgré un workflow lancé toutes les 15 min.
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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
import pyproj
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine, from_bounds
from rasterio.warp import reproject
import requests

VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "radar-neige-lpn-mf-aromepi-20260820"

RADAR_API = "https://public-api.meteofrance.fr/public/DPPaquetRadar/v1/mosaique/paquet"
RADAR_PRODUCT = "IPRN20_C_LFPW"
RADAR_FILE_RE = re.compile(r"T_IPRN20_C_LFPW_(\d{14})\.h5$", re.I)

AROMEPI_CAPABILITIES = (
    "https://public-api.meteofrance.fr/public/aromepi/1.0/wcs/"
    "MF-NWP-HIGHRES-AROMEPI-001-FRANCE-WCS/GetCapabilities"
)
AROMEPI_COVERAGE = (
    "https://public-api.meteofrance.fr/public/aromepi/1.0/wcs/"
    "MF-NWP-HIGHRES-AROMEPI-001-FRANCE-WCS/GetCoverage"
)

# Emprise commune aux rasters AROME-PI et à la carte Leaflet.
WEST, SOUTH, EAST, NORTH = -5.6, 40.8, 10.2, 52.0
OUT_WIDTH = 1100
OUT_HEIGHT = 780
OUT_TRANSFORM = from_bounds(WEST, SOUTH, EAST, NORTH, OUT_WIDTH, OUT_HEIGHT)
OUT_CRS = "EPSG:4326"
BOUNDS_LEAFLET = [[SOUTH, WEST], [NORTH, EAST]]

HISTORY_MINUTES = 75
RAIN_THRESHOLD = 0.05
MODEL_PHASE_THRESHOLD = 0.01
HTTP_TIMEOUT = (20, 120)

LOG = logging.getLogger("radar_neige")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"alertes-meteo-radar-neige/{VERSION}"})


@dataclass(frozen=True)
class Grid:
    crs: pyproj.CRS
    width: int
    height: int
    x_left: float
    x_step: float
    y_top: float
    y_step: float


@dataclass
class RadarFrame:
    timestamp: datetime
    rain_rate: np.ndarray
    quality: np.ndarray
    grid: Grid
    source: str


@dataclass(frozen=True)
class CoverageSummary:
    coverage_id: str
    title: str
    run: Optional[datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="build/radar-neige")
    parser.add_argument("--previous-dir", default=None)
    parser.add_argument("--radar-package", default=None, help="archive radar locale pour test")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def normalize_credential(value: str) -> str:
    value = (value or "").strip().strip('"').strip("'")
    for prefix in ("Bearer ", "bearer ", "apikey: ", "apiKey: "):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    if not value:
        raise RuntimeError("credential Météo-France vide")
    return value


def auth_headers(credential: str) -> list[tuple[str, dict[str, str]]]:
    token = normalize_credential(credential)
    common = {"Accept": "*/*", "User-Agent": f"alertes-meteo-radar-neige/{VERSION}"}
    return [
        ("apikey", {**common, "apikey": token}),
        ("Bearer", {**common, "Authorization": f"Bearer {token}"}),
    ]


def request_with_auth(method: str, url: str, credential: str, **kwargs: Any) -> requests.Response:
    failures: list[str] = []
    for label, headers in auth_headers(credential):
        merged_headers = dict(kwargs.pop("headers", {}) or {})
        merged_headers.update(headers)
        try:
            response = SESSION.request(method, url, headers=merged_headers, **kwargs)
        except requests.RequestException as exc:
            failures.append(f"{label}:{type(exc).__name__}")
            continue
        if response.status_code in (401, 403):
            failures.append(f"{label}:HTTP{response.status_code}")
            response.close()
            continue
        return response
    raise RuntimeError("authentification Météo-France refusée : " + ", ".join(failures))


def safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise RuntimeError("archive radar contenant un chemin dangereux")
        tar.extractall(destination, filter="data")


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
    for group in groups:
        attrs = attrs_dict(group)
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
    for group in candidates:
        merged.update(attrs_dict(group))
    gain = scalar_attr(merged.get("gain"), 0.01) or 0.01
    offset = scalar_attr(merged.get("offset"), 0.0) or 0.0
    nodata = scalar_attr(merged.get("nodata"), 65535.0)
    undetect = scalar_attr(merged.get("undetect"), None)
    return gain, offset, nodata, undetect


def pick_rain_dataset(h5: h5py.File) -> h5py.Dataset:
    scored: list[tuple[int, h5py.Dataset]] = []
    for ds in recursive_datasets(h5):
        if min(ds.shape) < 1000:
            continue
        quantity = quantity_of(ds)
        path = ds.name.lower()
        score = 0
        if quantity in {"ACRR", "RATE", "RR", "DBZH"}:
            score += 100
        if quantity == "ACRR":
            score += 100
        if "data" in path:
            score += 10
        if "quality" in path or quantity in {"QIND", "QUALITY"}:
            score -= 100
        scored.append((score, ds))
    if not scored:
        raise RuntimeError("matrice radar nationale introuvable")
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def pick_quality_dataset(h5: h5py.File, shape: tuple[int, int]) -> h5py.Dataset | None:
    candidates: list[tuple[int, h5py.Dataset]] = []
    for ds in recursive_datasets(h5):
        if ds.shape != shape:
            continue
        quantity = quantity_of(ds)
        path = ds.name.lower()
        score = 0
        if quantity in {"QIND", "QUALITY"}:
            score += 100
        if "quality" in path:
            score += 50
        if quantity == "ACRR":
            score -= 100
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
    for group in groups:
        attrs = attrs_dict(group)
        if key in attrs:
            return attrs[key]
    return None


def build_grid(h5: h5py.File, shape: tuple[int, int]) -> Grid:
    projdef = attr_from_groups(h5, "projdef")
    if projdef is None:
        raise RuntimeError("projection radar absente")
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
        raise RuntimeError("coins géographiques radar absents")
    height, width = shape
    x_left = (ll[0] + ul[0]) / 2.0
    x_right = (lr[0] + ur[0]) / 2.0
    y_top = (ul[1] + ur[1]) / 2.0
    y_bottom = (ll[1] + lr[1]) / 2.0
    x_step = (x_right - x_left) / width
    y_step = (y_bottom - y_top) / height
    return Grid(crs, width, height, x_left, x_step, y_top, y_step)


def load_radar_frame(path: Path) -> RadarFrame:
    match = RADAR_FILE_RE.search(path.name)
    if not match:
        raise RuntimeError(f"nom radar inattendu : {path.name}")
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
        # IPRN20 est une lame d'eau 5 min : conversion en intensité mm/h.
        rain = np.clip(rain, 0.0, 100.0) * 12.0

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


def download_radar_package(credential: str, target: Path) -> None:
    response = request_with_auth("GET", RADAR_API, credential, timeout=HTTP_TIMEOUT, stream=True, allow_redirects=True)
    try:
        response.raise_for_status()
        target.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with target.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    handle.write(chunk)
                    total += len(chunk)
        if total < 1024:
            raise RuntimeError("paquet radar anormalement petit")
        LOG.info("Paquet radar téléchargé : %.1f Mo", total / 1_000_000)
    finally:
        response.close()


def radar_to_output(frame: RadarFrame) -> tuple[np.ndarray, np.ndarray]:
    src_transform = Affine(frame.grid.x_step, 0.0, frame.grid.x_left, 0.0, frame.grid.y_step, frame.grid.y_top)
    rate = np.zeros((OUT_HEIGHT, OUT_WIDTH), dtype=np.float32)
    quality = np.zeros((OUT_HEIGHT, OUT_WIDTH), dtype=np.float32)
    reproject(
        source=frame.rain_rate,
        destination=rate,
        src_transform=src_transform,
        src_crs=frame.grid.crs.to_wkt(),
        dst_transform=OUT_TRANSFORM,
        dst_crs=OUT_CRS,
        resampling=Resampling.bilinear,
        dst_nodata=0.0,
    )
    reproject(
        source=frame.quality,
        destination=quality,
        src_transform=src_transform,
        src_crs=frame.grid.crs.to_wkt(),
        dst_transform=OUT_TRANSFORM,
        dst_crs=OUT_CRS,
        resampling=Resampling.bilinear,
        dst_nodata=0.0,
    )
    return np.clip(rate, 0.0, 250.0), np.clip(quality, 0.0, 100.0)


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_run_from_coverage_id(coverage_id: str) -> Optional[datetime]:
    match = re.search(r"___(\d{4}-\d{2}-\d{2}T\d{2}\.\d{2}\.\d{2}Z)", coverage_id)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H.%M.%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_capabilities(xml_bytes: bytes) -> list[CoverageSummary]:
    root = ET.fromstring(xml_bytes)
    summaries: list[CoverageSummary] = []
    for elem in root.iter():
        children = list(elem)
        if not children:
            continue
        title = None
        coverage_id = None
        for child in children:
            name = localname(child.tag).lower()
            text = (child.text or "").strip()
            if name == "title" and text:
                title = text
            if name in {"coverageid", "identifier"} and text:
                coverage_id = text
        if title and coverage_id:
            summaries.append(CoverageSummary(coverage_id, title, parse_run_from_coverage_id(coverage_id)))
    # dédoublonnage
    unique: dict[str, CoverageSummary] = {item.coverage_id: item for item in summaries}
    return list(unique.values())


def normalize_label(text: str) -> str:
    return (
        text.lower()
        .replace("’", "'")
        .replace("°", "")
        .replace("é", "e").replace("è", "e").replace("ê", "e")
        .replace("à", "a").replace("ù", "u").replace("ô", "o")
        .replace("ï", "i").replace("î", "i")
    )


def choose_coverage(items: Iterable[CoverageSummary], kind: str) -> Optional[CoverageSummary]:
    scored: list[tuple[int, datetime, CoverageSummary]] = []
    for item in items:
        label = normalize_label(item.title + " " + item.coverage_id)
        score = 0
        if kind == "snow":
            if "precip" in label and "neige" in label:
                score += 100
            if "snow" in label:
                score += 80
            if "15min" in label or "pt15m" in label:
                score += 15
        elif kind == "liquid":
            if "precip" in label and "liquide" in label:
                score += 100
            if "liquid" in label:
                score += 80
            if "15min" in label or "pt15m" in label:
                score += 15
        elif kind == "lpn":
            if "altitude" in label and ("t'w" in label or "wet" in label or "wetbt" in label):
                score += 80
            if "1c" in label or "27415" in label:
                score += 30
            elif "0c" in label or "27315" in label:
                score += 20
        if score <= 0:
            continue
        run = item.run or datetime(1970, 1, 1, tzinfo=timezone.utc)
        scored.append((score, run, item))
    if not scored:
        return None
    # priorité au meilleur libellé puis au run le plus récent
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]


def get_aromepi_capabilities(credential: str) -> tuple[list[CoverageSummary], str]:
    params = {"service": "WCS", "version": "2.0.1", "language": "fre"}
    response = request_with_auth("GET", AROMEPI_CAPABILITIES, credential, params=params, timeout=HTTP_TIMEOUT)
    try:
        response.raise_for_status()
        items = parse_capabilities(response.content)
        if not items:
            raise RuntimeError("GetCapabilities AROME-PI sans couverture")
        return items, response.url
    finally:
        response.close()


def round_quarter(dt: datetime) -> datetime:
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def forecast_time_candidates(run: Optional[datetime], now: datetime) -> list[datetime]:
    base = round_quarter(now)
    candidates = [base, base - timedelta(minutes=15), base + timedelta(minutes=15), base - timedelta(minutes=30), base + timedelta(minutes=30)]
    if run is not None:
        candidates.extend([run, run + timedelta(minutes=15), run + timedelta(minutes=30), run + timedelta(hours=1)])
        lo, hi = run, run + timedelta(hours=6)
        candidates = [min(max(item, lo), hi) for item in candidates]
    unique: list[datetime] = []
    seen: set[str] = set()
    for item in candidates:
        key = iso(item) or ""
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


def get_coverage_tiff(credential: str, coverage: CoverageSummary, target_dir: Path) -> tuple[Path, datetime]:
    errors: list[str] = []
    for target_time in forecast_time_candidates(coverage.run, utcnow()):
        params: list[tuple[str, str]] = [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", coverage.coverage_id),
            ("format", "image/tiff"),
            ("subset", f"long({WEST},{EAST})"),
            ("subset", f"lat({SOUTH},{NORTH})"),
            ("subset", f"time({iso(target_time)})"),
        ]
        response = request_with_auth("GET", AROMEPI_COVERAGE, credential, params=params, timeout=HTTP_TIMEOUT, stream=True)
        try:
            if response.status_code != 200:
                errors.append(f"{iso(target_time)}=HTTP{response.status_code}")
                continue
            content_type = (response.headers.get("content-type") or "").lower()
            if "tiff" not in content_type and "octet-stream" not in content_type:
                errors.append(f"{iso(target_time)}={content_type or 'type-inconnu'}")
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / (re.sub(r"[^A-Za-z0-9_.-]+", "_", coverage.coverage_id)[-110:] + ".tif")
            with path.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
            if path.stat().st_size < 1000:
                errors.append(f"{iso(target_time)}=fichier-vide")
                path.unlink(missing_ok=True)
                continue
            # validation raster immédiate
            with rasterio.open(path) as src:
                if src.width < 10 or src.height < 10:
                    raise RuntimeError("GeoTIFF AROME-PI trop petit")
            return path, target_time
        except Exception as exc:
            errors.append(f"{iso(target_time)}={type(exc).__name__}")
        finally:
            response.close()
    raise RuntimeError("GetCoverage impossible : " + "; ".join(errors[-8:]))


def read_geotiff_to_output(path: Path, resampling: Resampling = Resampling.bilinear) -> np.ndarray:
    destination = np.full((OUT_HEIGHT, OUT_WIDTH), np.nan, dtype=np.float32)
    with rasterio.open(path) as src:
        source = src.read(1).astype(np.float32)
        if src.nodata is not None:
            source[source == src.nodata] = np.nan
        source[~np.isfinite(source)] = np.nan
        reproject(
            source=source,
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=np.nan,
            dst_transform=OUT_TRANSFORM,
            dst_crs=OUT_CRS,
            dst_nodata=np.nan,
            resampling=resampling,
        )
    return destination


def normalize_precip_model(field: np.ndarray) -> np.ndarray:
    x = np.nan_to_num(field.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    x[x < 0] = 0.0
    return x


def normalize_lpn(field: np.ndarray) -> np.ndarray:
    x = field.astype(np.float32).copy()
    finite = x[np.isfinite(x)]
    if finite.size and np.nanmax(finite) < 20.0:
        x *= 1000.0  # sécurité si un fournisseur exprime exceptionnellement l'altitude en km
    x[(x < -200) | (x > 6500)] = np.nan
    return x


def classify_phase(snow: Optional[np.ndarray], liquid: Optional[np.ndarray]) -> np.ndarray:
    # 0 inconnu/pluie par défaut, 1 pluie, 2 neige, 3 mixte.
    phase = np.ones((OUT_HEIGHT, OUT_WIDTH), dtype=np.uint8)
    if snow is None or liquid is None:
        return phase
    snow_present = normalize_precip_model(snow) > MODEL_PHASE_THRESHOLD
    liquid_present = normalize_precip_model(liquid) > MODEL_PHASE_THRESHOLD
    phase[snow_present & ~liquid_present] = 2
    phase[snow_present & liquid_present] = 3
    phase[~snow_present & liquid_present] = 1
    return phase


def rgba_from_rate(rate: np.ndarray, quality: np.ndarray, phase: np.ndarray) -> np.ndarray:
    rgba = np.zeros((rate.shape[0], rate.shape[1], 4), dtype=np.uint8)
    active = rate >= RAIN_THRESHOLD
    # Couleurs pluie selon intensité.
    rain_bins = [
        (0.05, 0.3, (70, 170, 255)),
        (0.3, 1.0, (65, 210, 175)),
        (1.0, 3.0, (80, 205, 85)),
        (3.0, 7.0, (245, 220, 65)),
        (7.0, 15.0, (245, 145, 45)),
        (15.0, 35.0, (225, 65, 75)),
        (35.0, 999.0, (150, 55, 180)),
    ]
    for lo, hi, color in rain_bins:
        mask = active & (phase == 1) & (rate >= lo) & (rate < hi)
        rgba[mask, :3] = color

    # Neige : bleu très clair -> bleu soutenu.
    snow_colors = [
        (0.05, 0.5, (215, 248, 255)),
        (0.5, 2.0, (125, 225, 250)),
        (2.0, 6.0, (65, 170, 235)),
        (6.0, 15.0, (45, 105, 205)),
        (15.0, 999.0, (50, 55, 160)),
    ]
    for lo, hi, color in snow_colors:
        mask = active & (phase == 2) & (rate >= lo) & (rate < hi)
        rgba[mask, :3] = color

    # Mélange pluie/neige : violet/rose selon intensité.
    mix_colors = [
        (0.05, 1.0, (220, 165, 245)),
        (1.0, 5.0, (190, 100, 225)),
        (5.0, 15.0, (165, 60, 190)),
        (15.0, 999.0, (125, 35, 145)),
    ]
    for lo, hi, color in mix_colors:
        mask = active & (phase == 3) & (rate >= lo) & (rate < hi)
        rgba[mask, :3] = color

    # Si phase non renseignée, pluie radar neutre.
    unknown = active & np.all(rgba[:, :, :3] == 0, axis=2)
    rgba[unknown, :3] = (80, 150, 230)

    intensity_alpha = np.clip(115 + np.log1p(rate) * 38, 120, 240)
    quality_factor = np.clip(0.45 + quality / 180.0, 0.45, 1.0)
    alpha = np.clip(intensity_alpha * quality_factor, 80, 240).astype(np.uint8)
    rgba[active, 3] = alpha[active]
    return rgba


def save_lpn_overlay(lpn: np.ndarray, path: Path) -> dict[str, Any]:
    valid = np.isfinite(lpn)
    if valid.sum() < 200:
        raise RuntimeError("champ limite pluie/neige trop incomplet")

    # Niveaux lisibles et identiques d'un run à l'autre.
    levels = [0, 300, 600, 900, 1200, 1500, 1800, 2200, 2600, 3000, 3500, 4000]
    xs = np.linspace(WEST, EAST, OUT_WIDTH)
    ys = np.linspace(NORTH, SOUTH, OUT_HEIGHT)

    fig = plt.figure(figsize=(OUT_WIDTH / 100, OUT_HEIGHT / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(WEST, EAST)
    ax.set_ylim(SOUTH, NORTH)
    ax.axis("off")
    contours = ax.contour(xs, ys, lpn, levels=levels, colors="#ff3b9d", linewidths=1.15, alpha=0.9)
    labels = ax.clabel(contours, inline=True, fontsize=7, fmt=lambda value: f"{int(value)} m", colors="#ffffff")
    for label in labels:
        label.set_bbox(dict(facecolor="#7b145b", edgecolor="none", alpha=0.82, pad=1.4))
    fig.savefig(path, transparent=True, dpi=100)
    plt.close(fig)

    vals = lpn[valid]
    return {
        "min_m": int(round(float(np.nanmin(vals)))),
        "median_m": int(round(float(np.nanmedian(vals)))),
        "max_m": int(round(float(np.nanmax(vals)))),
        "levels_m": levels,
    }


def copy_previous_frames(previous_dir: Optional[Path], output_dir: Path) -> list[dict[str, Any]]:
    if previous_dir is None:
        return []
    old_index_path = previous_dir / "index.json"
    if not old_index_path.exists():
        # accepte aussi une extraction contenant un sous-dossier radar-neige-data
        candidates = list(previous_dir.glob("**/index.json"))
        old_index_path = candidates[0] if candidates else old_index_path
    if not old_index_path.exists():
        return []
    try:
        old = json.loads(old_index_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    old_base = old_index_path.parent
    now = utcnow()
    copied: list[dict[str, Any]] = []
    for item in old.get("frames") or []:
        when_text = str(item.get("time") or "")
        try:
            when = datetime.fromisoformat(when_text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        if now - when > timedelta(minutes=HISTORY_MINUTES + 20):
            continue
        rel = str(item.get("file") or "")
        src = old_base / rel
        if not src.exists():
            continue
        copied_item = {"time": iso(when), "file": rel}
        for key in ("file", "precip_file", "snow_file"):
            rel_key = str(item.get(key) or "")
            if not rel_key:
                continue
            src_key = old_base / rel_key
            if not src_key.exists():
                continue
            dst_key = output_dir / rel_key
            dst_key.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_key, dst_key)
            copied_item[key] = rel_key
        copied.append(copied_item)

    old_lpn = old_base / str((old.get("lpn") or {}).get("file") or "")
    if old_lpn.exists():
        shutil.copy2(old_lpn, output_dir / "limite-pluie-neige.png")
    return copied


def dedupe_and_prune_frames(frames: list[dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for item in frames:
        if item.get("time") and item.get("file"):
            unique[str(item["time"])] = item
    rows = sorted(unique.values(), key=lambda item: str(item["time"]))
    if not rows:
        return []
    latest = datetime.fromisoformat(str(rows[-1]["time"]).replace("Z", "+00:00"))
    cutoff = latest - timedelta(minutes=HISTORY_MINUTES)
    kept: list[dict[str, Any]] = []
    for item in rows:
        when = datetime.fromisoformat(str(item["time"]).replace("Z", "+00:00"))
        if when >= cutoff:
            kept.append(item)
        else:
            for key in ("file", "precip_file", "snow_file"):
                rel = item.get(key)
                if rel:
                    (output_dir / str(rel)).unlink(missing_ok=True)
    return kept


def self_test() -> None:
    rate = np.zeros((OUT_HEIGHT, OUT_WIDTH), np.float32)
    quality = np.full_like(rate, 100.0)
    snow = np.zeros_like(rate)
    liquid = np.zeros_like(rate)
    rate[100:200, 100:200] = 4.0
    snow[100:150, 100:200] = 1.0
    liquid[150:200, 100:200] = 1.0
    phase = classify_phase(snow, liquid)
    rgba = rgba_from_rate(rate, quality, phase)
    assert rgba[120, 120, 3] > 0
    assert phase[120, 120] == 2
    assert phase[170, 120] == 1
    # zone mixte
    snow[160:170, 110:120] = 1.0
    phase2 = classify_phase(snow, liquid)
    assert phase2[165, 115] == 3
    print("SELF-TEST OK")


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "frames").mkdir(parents=True, exist_ok=True)
    previous = Path(args.previous_dir) if args.previous_dir else None
    frame_index = copy_previous_frames(previous, out)

    radar_token = os.getenv("METEOFRANCE_RADAR_TOKEN", "").strip()
    if not radar_token and not args.radar_package:
        raise RuntimeError("secret METEOFRANCE_RADAR_TOKEN absent")

    arome_token = (
        os.getenv("METEOFRANCE_AROME_PI_TOKEN", "").strip()
        or os.getenv("METEOFRANCE_AROME_TOKEN", "").strip()
    )

    phase_status = "unavailable"
    phase_note = "AROME-PI indisponible : échos radar affichés sans typage neige/pluie."
    snow_field: Optional[np.ndarray] = None
    liquid_field: Optional[np.ndarray] = None
    lpn_field: Optional[np.ndarray] = None
    arome_meta: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="am-radar-neige-") as tmp_name:
        tmp = Path(tmp_name)

        # AROME-PI est volontairement non bloquant : le radar doit rester disponible.
        if arome_token:
            try:
                summaries, _caps_url = get_aromepi_capabilities(arome_token)
                chosen = {
                    "snow": choose_coverage(summaries, "snow"),
                    "liquid": choose_coverage(summaries, "liquid"),
                    "lpn": choose_coverage(summaries, "lpn"),
                }
                LOG.info("AROME-PI couvertures : %s", {k: (v.title if v else None) for k, v in chosen.items()})
                times: dict[str, str] = {}
                ids: dict[str, str] = {}
                for kind in ("snow", "liquid", "lpn"):
                    coverage = chosen[kind]
                    if coverage is None:
                        continue
                    tif, target_time = get_coverage_tiff(arome_token, coverage, tmp / "aromepi")
                    field = read_geotiff_to_output(tif, Resampling.bilinear)
                    if kind == "snow":
                        snow_field = field
                    elif kind == "liquid":
                        liquid_field = field
                    else:
                        lpn_field = normalize_lpn(field)
                    times[kind] = iso(target_time) or ""
                    ids[kind] = coverage.coverage_id
                if snow_field is not None and liquid_field is not None:
                    phase_status = "ok"
                    phase_note = "Nature des précipitations croisée radar réel + AROME-PI neige/liquide."
                elif lpn_field is not None:
                    phase_status = "partial"
                    phase_note = "Limite pluie/neige disponible ; typage pluie/neige partiel."
                arome_meta = {"coverage_ids": ids, "forecast_times": times}
            except Exception as exc:
                LOG.warning("AROME-PI indisponible : %s", exc)
                phase_note = f"AROME-PI indisponible ({type(exc).__name__}) : radar brut maintenu."
        else:
            phase_note = "Secret METEOFRANCE_AROME_PI_TOKEN absent : radar brut maintenu."

        phase = classify_phase(snow_field, liquid_field)

        package = Path(args.radar_package) if args.radar_package else tmp / "radar.tar.gz"
        if not args.radar_package:
            download_radar_package(radar_token, package)
        extract = tmp / "radar"
        extract.mkdir(parents=True, exist_ok=True)
        safe_extract(package, extract)
        h5_files = sorted([p for p in extract.rglob("*.h5") if RADAR_FILE_RE.search(p.name)])
        if not h5_files:
            raise RuntimeError("aucune mosaïque IPRN20 dans le paquet radar")

        loaded: list[RadarFrame] = []
        for path in h5_files:
            try:
                loaded.append(load_radar_frame(path))
            except Exception as exc:
                LOG.warning("Mosaïque ignorée %s : %s", path.name, exc)
        loaded.sort(key=lambda item: item.timestamp)
        loaded = loaded[-4:]
        if not loaded:
            raise RuntimeError("aucune mosaïque radar exploitable")

        for frame in loaded:
            rate, quality = radar_to_output(frame)

            # Vue fusionnée pluie / neige / mêlée.
            rgba = rgba_from_rate(rate, quality, phase)
            rel = f"frames/radar-neige-{frame.timestamp:%Y%m%d%H%M}.png"
            Image.fromarray(rgba, mode="RGBA").save(out / rel, optimize=True)

            # Vue précipitations classique, indépendante du diagnostic de phase.
            precip_phase = np.ones_like(phase, dtype=np.uint8)
            precip_rgba = rgba_from_rate(rate, quality, precip_phase)
            precip_rel = f"frames/precip-{frame.timestamp:%Y%m%d%H%M}.png"
            Image.fromarray(precip_rgba, mode="RGBA").save(out / precip_rel, optimize=True)

            # Vue neige / pluie-neige seule.
            snow_rgba = rgba.copy()
            snow_keep = (phase == 2) | (phase == 3)
            snow_rgba[~snow_keep, 3] = 0
            snow_rel = f"frames/neige-{frame.timestamp:%Y%m%d%H%M}.png"
            Image.fromarray(snow_rgba, mode="RGBA").save(out / snow_rel, optimize=True)

            frame_index.append({
                "time": iso(frame.timestamp),
                "file": rel,
                "precip_file": precip_rel,
                "snow_file": snow_rel,
            })

        frame_index = dedupe_and_prune_frames(frame_index, out)
        latest_radar = loaded[-1].timestamp

        lpn_meta: dict[str, Any] = {"available": False, "file": None}
        if lpn_field is not None:
            try:
                stats = save_lpn_overlay(lpn_field, out / "limite-pluie-neige.png")
                lpn_meta = {"available": True, "file": "limite-pluie-neige.png", **stats}
            except Exception as exc:
                LOG.warning("Overlay LPN impossible : %s", exc)
        elif (out / "limite-pluie-neige.png").exists():
            # Fallback visuel de la publication précédente ; clairement signalé comme ancien.
            lpn_meta = {"available": True, "file": "limite-pluie-neige.png", "stale": True}

    index = {
        "schema_version": SCHEMA_VERSION,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "status": "ok",
        "generated_at": iso(utcnow()),
        "radar_time": iso(latest_radar),
        "bounds": BOUNDS_LEAFLET,
        "frames": frame_index,
        "animation": {
            "history_minutes": HISTORY_MINUTES,
            "native_radar_frequency_minutes": 5,
            "workflow_frequency_minutes": 15,
        },
        "phase": {
            "status": phase_status,
            "note": phase_note,
            "classes": {"rain": 1, "snow": 2, "mixed": 3},
        },
        "lpn": lpn_meta,
        "arome_pi": arome_meta,
        "source": {
            "radar": {
                "provider": "Météo-France",
                "api": "DPPaquetRadar/v1/mosaique/paquet",
                "product": RADAR_PRODUCT,
                "label": "Mosaïque nationale lame d'eau 5 min",
                "native_resolution_m": 500,
                "frequency_minutes": 5,
            },
            "phase_and_lpn": {
                "provider": "Météo-France",
                "model": "AROME-PI",
                "service": "WCS",
                "forecast_step_minutes": 15,
            },
        },
    }
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    LOG.info("Radar neige publié : %d images ; dernier radar %s ; phase=%s", len(frame_index), iso(latest_radar), phase_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

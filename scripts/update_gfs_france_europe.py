#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alertes-Meteo.com — GFS France / Europe
Version 1.2.0

Télécharge via le filtre NOMADS uniquement les champs GFS 0,25° utiles à la carte :
- PRMSL : pression au niveau moyen de la mer
- TMP : température 2 m
- APCP : précipitations accumulées sur la période portée par le GRIB
- UGRD/VGRD : vent 10 m
- GUST : rafales

La zone téléchargée couvre l'Europe (25°W–45°E, 34–72°N), puis la grille est
rééchantillonnée à 0,5° pour produire des JSON web légers.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import numpy as np
import requests

VERSION = "1.2.0"
BUILD_ID = "gfs-france-europe-pression-temperature-pluie-vent-20260821"
NOMADS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
LEFT, RIGHT, BOTTOM, TOP = -25.0, 45.0, 34.0, 72.0
DOWNSAMPLE = 2  # 0,25° -> 0,50°
FORECAST_HOURS = [0, 3, 6, 9, 12, 18, 24, 30, 36, 42, 48, 60, 72, 84, 96, 120, 144, 168]
HTTP_TIMEOUT = (20, 120)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"alertes-meteo-gfs/{VERSION} (+https://alertes-meteo.com/)"})


@dataclass(frozen=True)
class Run:
    date: str
    cycle: str
    dt: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def candidates(now: datetime) -> Iterable[Run]:
    base = now.replace(minute=0, second=0, microsecond=0)
    # Teste jusqu'à ~48 h en arrière, utile si NOMADS est momentanément en retard.
    for back in range(0, 49, 6):
        dt = base - timedelta(hours=back)
        cycle_hour = (dt.hour // 6) * 6
        run_dt = dt.replace(hour=cycle_hour)
        yield Run(run_dt.strftime("%Y%m%d"), f"{cycle_hour:02d}", run_dt)


def nomads_url(run: Run, fhr: int, tiny: bool = False) -> str:
    params = {
        "file": f"gfs.t{run.cycle}z.pgrb2.0p25.f{fhr:03d}",
        "lev_mean_sea_level": "on",
        "lev_2_m_above_ground": "on",
        "lev_10_m_above_ground": "on",
        "lev_surface": "on",
        "var_PRMSL": "on",
        "var_TMP": "on",
        "var_APCP": "on",
        "var_UGRD": "on",
        "var_VGRD": "on",
        "var_GUST": "on",
        "subregion": "",
        "leftlon": 1 if tiny else LEFT,
        "rightlon": 2 if tiny else RIGHT,
        "toplat": 47 if tiny else TOP,
        "bottomlat": 46 if tiny else BOTTOM,
        "dir": f"/gfs.{run.date}/{run.cycle}/atmos",
    }
    return NOMADS_FILTER + "?" + urlencode(params)


def looks_like_grib(response: requests.Response) -> bool:
    if response.status_code != 200 or len(response.content) < 80:
        return False
    ctype = (response.headers.get("content-type") or "").lower()
    if "text/html" in ctype:
        return False
    return b"GRIB" in response.content[:64] or len(response.content) > 1000


def detect_latest_run() -> Run:
    now = utcnow()
    errors: list[str] = []
    for run in candidates(now):
        try:
            response = SESSION.get(nomads_url(run, 0, tiny=True), timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            errors.append(str(exc))
            continue
        if looks_like_grib(response):
            print("Run GFS détecté :", iso(run.dt))
            return run
        errors.append(f"{run.date}/{run.cycle}: HTTP {response.status_code}, {len(response.content)} o")
        time.sleep(0.25)
    raise RuntimeError("Aucun run GFS disponible. " + " | ".join(errors[-5:]))


def download_frame(run: Run, fhr: int, target: Path) -> str:
    url = nomads_url(run, fhr)
    print(f"Téléchargement GFS +{fhr:03d} h")
    last_error = None
    for attempt in range(1, 4):
        try:
            response = SESSION.get(url, timeout=HTTP_TIMEOUT)
            if looks_like_grib(response):
                target.write_bytes(response.content)
                print("  ->", len(response.content), "octets")
                return url
            last_error = f"HTTP {response.status_code}, {len(response.content)} octets"
        except requests.RequestException as exc:
            last_error = str(exc)
        if attempt < 3:
            time.sleep(2 * attempt)
    raise RuntimeError(f"Échéance +{fhr:03d} h indisponible: {last_error}")


def attr_text(da: Any, key: str) -> str:
    return str(da.attrs.get(key, "") or "").strip()


def var_score(da: Any, kind: str) -> int:
    short = attr_text(da, "GRIB_shortName").lower()
    name = attr_text(da, "GRIB_name").lower()
    level_type = attr_text(da, "GRIB_typeOfLevel").lower()
    level = da.attrs.get("GRIB_level")
    try:
        level_num = float(level)
    except (TypeError, ValueError):
        level_num = None

    score = 0
    if kind == "pressure":
        if short == "prmsl": score += 100
        if "mean sea level" in name and "pressure" in name: score += 80
        if "meansea" in level_type: score += 30
    elif kind == "temperature":
        if short in {"2t", "t"}: score += 50
        if "temperature" in name: score += 40
        if "heightaboveground" in level_type and level_num == 2: score += 60
    elif kind == "u":
        if short in {"10u", "u"}: score += 50
        if "u component" in name: score += 40
        if "heightaboveground" in level_type and level_num == 10: score += 60
    elif kind == "v":
        if short in {"10v", "v"}: score += 50
        if "v component" in name: score += 40
        if "heightaboveground" in level_type and level_num == 10: score += 60
    elif kind == "gust":
        if short == "gust": score += 100
        if "gust" in name: score += 80
        if "surface" in level_type: score += 20
    elif kind == "precip":
        if short in {"tp", "apcp"}: score += 100
        if "total precipitation" in name or "precipitation" in name: score += 70
        if "surface" in level_type: score += 20
    return score


def all_dataarrays(path: Path) -> list[Any]:
    try:
        import cfgrib
    except Exception as exc:
        raise RuntimeError(f"cfgrib indisponible: {exc}") from exc
    datasets = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
    arrays = []
    for ds in datasets:
        for name in ds.data_vars:
            da = ds[name]
            if any(k in da.dims for k in ("latitude", "lat")) and any(k in da.dims for k in ("longitude", "lon")):
                arrays.append(da)
    return arrays


def choose_array(arrays: list[Any], kind: str) -> Any | None:
    ranked = sorted(((var_score(da, kind), da) for da in arrays), key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] <= 0:
        return None
    return ranked[0][1]


def extract_2d(da: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lat_name = "latitude" if "latitude" in da.dims else "lat"
    lon_name = "longitude" if "longitude" in da.dims else "lon"
    # Réduit toutes les dimensions scalaires restantes (time, step, heightAboveGround...).
    for dim in list(da.dims):
        if dim not in (lat_name, lon_name):
            da = da.isel({dim: 0})
    da = da.transpose(lat_name, lon_name)
    lat = np.asarray(da[lat_name].values, dtype=float)
    lon = np.asarray(da[lon_name].values, dtype=float)
    arr = np.asarray(da.values, dtype=float)
    if arr.ndim != 2:
        arr = np.squeeze(arr)
    if arr.ndim != 2:
        raise RuntimeError(f"Champ {getattr(da, 'name', '?')} non 2D: {arr.shape}")

    lon = np.where(lon > 180.0, lon - 360.0, lon)
    lat_order = np.argsort(lat)
    lon_order = np.argsort(lon)
    lat = lat[lat_order]
    lon = lon[lon_order]
    arr = arr[np.ix_(lat_order, lon_order)]

    lat_mask = (lat >= BOTTOM - 0.01) & (lat <= TOP + 0.01)
    lon_mask = (lon >= LEFT - 0.01) & (lon <= RIGHT + 0.01)
    lat_idx = np.flatnonzero(lat_mask)
    lon_idx = np.flatnonzero(lon_mask)
    if not len(lat_idx) or not len(lon_idx):
        raise RuntimeError("Grille GFS hors emprise Europe après normalisation des coordonnées")
    lat = lat[lat_idx]
    lon = lon[lon_idx]
    arr = arr[np.ix_(lat_idx, lon_idx)]

    lat = lat[::DOWNSAMPLE]
    lon = lon[::DOWNSAMPLE]
    arr = arr[::DOWNSAMPLE, ::DOWNSAMPLE]
    return lat, lon, arr


def harmonize(base_lat: np.ndarray, base_lon: np.ndarray, da: Any | None) -> np.ndarray | None:
    if da is None:
        return None
    lat, lon, arr = extract_2d(da)
    if lat.shape == base_lat.shape and lon.shape == base_lon.shape and np.allclose(lat, base_lat) and np.allclose(lon, base_lon):
        return arr
    # Les champs demandés sont sur la même grille GFS; si ce n'est pas le cas, on préfère
    # ne pas inventer de rééchantillonnage approximatif dans ce pipeline.
    raise RuntimeError(f"Grille différente pour {getattr(da, 'name', '?')}: {arr.shape} vs {(len(base_lat), len(base_lon))}")


def round_matrix(arr: np.ndarray | None, decimals: int) -> list[list[float | int | None]] | None:
    if arr is None:
        return None
    out: list[list[float | int | None]] = []
    for row in arr:
        values = []
        for value in row:
            if not math.isfinite(float(value)):
                values.append(None)
            else:
                v = round(float(value), decimals)
                values.append(int(v) if decimals == 0 else v)
        out.append(values)
    return out


def precip_period_label(da: Any | None, fhr: int) -> str:
    if da is None:
        return "Précipitations indisponibles à cette échéance"
    step_range = attr_text(da, "GRIB_stepRange")
    step_type = attr_text(da, "GRIB_stepType")
    if step_range:
        return f"Accumulation GFS {step_range} h" + (f" ({step_type})" if step_type else "")
    return f"Accumulation associée à l’échéance +{fhr:03d} h"


def parse_frame(path: Path, run: Run, fhr: int, source_url: str) -> dict[str, Any]:
    arrays = all_dataarrays(path)
    pressure_da = choose_array(arrays, "pressure")
    if pressure_da is None:
        raise RuntimeError("Champ PRMSL absent du GRIB filtré")
    temp_da = choose_array(arrays, "temperature")
    u_da = choose_array(arrays, "u")
    v_da = choose_array(arrays, "v")
    gust_da = choose_array(arrays, "gust")
    precip_da = choose_array(arrays, "precip")

    lat, lon, pressure = extract_2d(pressure_da)
    temp = harmonize(lat, lon, temp_da)
    u = harmonize(lat, lon, u_da)
    v = harmonize(lat, lon, v_da)
    gust = harmonize(lat, lon, gust_da)
    precip = harmonize(lat, lon, precip_da)

    if np.nanmedian(pressure) > 2000:
        pressure = pressure / 100.0
    if temp is not None and np.nanmedian(temp) > 100:
        temp = temp - 273.15

    wind_speed = None
    wind_dir = None
    if u is not None and v is not None:
        wind_speed = np.sqrt(u * u + v * v) * 3.6
        wind_dir = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    if gust is not None:
        gust = gust * 3.6

    # APCP en kg/m² équivaut numériquement à mm d'eau.
    if precip is not None:
        precip = np.where(precip < -0.01, np.nan, np.maximum(precip, 0.0))

    valid = run.dt + timedelta(hours=fhr)
    return {
        "status": "ok",
        "schema_version": 1,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "model": "NOAA/NCEP GFS 0.25 degree",
        "run_utc": iso(run.dt),
        "forecast_hour": fhr,
        "valid_utc": iso(valid),
        "generated_at": iso(utcnow()),
        "source": {
            "provider": "NOAA / NCEP NOMADS",
            "dataset": "GFS 0.25 degree",
            "filter": "filter_gfs_0p25.pl",
            "url": source_url,
        },
        "grid": {
            "spacing_deg": 0.5,
            "native_spacing_deg": 0.25,
            "latitudes": [round(float(x), 2) for x in lat],
            "longitudes": [round(float(x), 2) for x in lon],
        },
        "precipitation_period_label": precip_period_label(precip_da, fhr),
        "fields": {
            "pressure_hpa": round_matrix(pressure, 1),
            "temperature_2m_c": round_matrix(temp, 1),
            "precipitation_mm": round_matrix(precip, 1),
            "wind_speed_kmh": round_matrix(wind_speed, 0),
            "wind_direction_deg": round_matrix(wind_dir, 0),
            "gust_kmh": round_matrix(gust, 0),
        },
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def self_test() -> int:
    # Tests sans réseau des conversions et de la direction du vent.
    p = np.array([[101325.0]]) / 100.0
    assert round(float(p[0, 0]), 1) == 1013.2
    u = np.array([[10.0]])
    v = np.array([[0.0]])
    speed = np.sqrt(u * u + v * v) * 3.6
    direction = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    assert round(float(speed[0, 0])) == 36
    assert round(float(direction[0, 0])) == 270  # vent d'ouest
    print("Self-test GFS OK")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", default="build/gfs")
    p.add_argument("--hours", default=",".join(str(x) for x in FORECAST_HOURS))
    p.add_argument("--self-test", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        return self_test()

    hours = sorted({int(x.strip()) for x in args.hours.split(",") if x.strip()})
    output_dir = Path(args.output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run = detect_latest_run()
    frames = []
    failures = []

    with tempfile.TemporaryDirectory(prefix="am-gfs-") as tmpdir:
        tmp = Path(tmpdir)
        for fhr in hours:
            grib = tmp / f"gfs_f{fhr:03d}.grib2"
            try:
                source_url = download_frame(run, fhr, grib)
                payload = parse_frame(grib, run, fhr, source_url)
                filename = f"gfs_f{fhr:03d}.json"
                write_json(output_dir / filename, payload)
                frames.append({
                    "forecast_hour": fhr,
                    "valid_utc": payload["valid_utc"],
                    "file": filename,
                    "precipitation_period_label": payload["precipitation_period_label"],
                })
            except Exception as exc:
                print(f"::warning::GFS +{fhr:03d} h ignoré: {exc}")
                failures.append({"forecast_hour": fhr, "error": str(exc)})

    if not frames:
        raise RuntimeError("Aucune échéance GFS n'a pu être produite")

    index = {
        "status": "ok",
        "schema_version": 1,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "model": "NOAA/NCEP GFS 0.25 degree",
        "run_utc": iso(run.dt),
        "generated_at": iso(utcnow()),
        "coverage": {"west": LEFT, "east": RIGHT, "south": BOTTOM, "north": TOP},
        "native_spacing_deg": 0.25,
        "web_spacing_deg": 0.5,
        "variables": ["pressure_hpa", "temperature_2m_c", "precipitation_mm", "wind_speed_kmh", "wind_direction_deg", "gust_kmh"],
        "frames": frames,
        "failures": failures,
        "source": "NOAA/NCEP NOMADS GFS 0.25°",
    }
    write_json(output_dir / "index.json", index)
    print("Index :", output_dir / "index.json")
    print("Échéances :", len(frames), "/", len(hours))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERREUR FATALE GFS : {exc}", file=sys.stderr)
        raise

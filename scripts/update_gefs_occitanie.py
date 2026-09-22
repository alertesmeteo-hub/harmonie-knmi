#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Produit la pluie ensembliste GEFS pour la région Occitanie uniquement.

Le générateur traite le dernier cycle complet parmi 00/06/12/18 UTC. Il lit les
31 membres GEFS (c00 + p01-p30), calcule les statistiques de l'intervalle et du
cumul depuis le début du run, puis publie un petit JSON par échéance. La
conservation des quatre derniers cycles est assurée par le workflow GitHub.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import numpy as np
import requests

VERSION = "1.2.0"
BUILD_ID = "gefs-occitanie-rain-four-cycles-v120-20260922"
NOMADS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p50a.pl"
BOUNDARY_URLS = {
    "76": "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/regions/occitanie/region-occitanie.geojson",
}

# Marge d'un demi-point autour de la région. Les points hors contour sont masqués.
LEFT, RIGHT, BOTTOM, TOP = -0.5, 4.9, 42.0, 45.0
MEMBERS = ("c00",) + tuple(f"p{i:02d}" for i in range(1, 31))
FORECAST_HOURS = tuple(range(6, 385, 6))
THRESHOLDS_MM = (1.0, 10.0, 30.0, 50.0)
HTTP_TIMEOUT = (20, 120)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"alertes-meteo-gefs/{VERSION} (+https://alertes-meteo.com/)"})
REQUEST_INTERVAL_SECONDS = 0.6  # ~100 requêtes/minute, cadence respectueuse de NOMADS.
_REQUEST_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


@dataclass(frozen=True)
class Run:
    date: str
    cycle: str
    dt: datetime

    @property
    def run_id(self) -> str:
        return f"{self.date}T{self.cycle}Z"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def candidates(now: datetime) -> Iterable[Run]:
    base = now.replace(minute=0, second=0, microsecond=0)
    seen: set[datetime] = set()
    for back in range(0, 55, 6):
        dt = base - timedelta(hours=back)
        run_dt = dt.replace(hour=(dt.hour // 6) * 6)
        if run_dt in seen:
            continue
        seen.add(run_dt)
        yield Run(run_dt.strftime("%Y%m%d"), f"{run_dt.hour:02d}", run_dt)


def member_file(run: Run, member: str, fhr: int) -> str:
    return f"ge{member}.t{run.cycle}z.pgrb2a.0p50.f{fhr:03d}"


def nomads_url(run: Run, member: str, fhr: int, tiny: bool = False) -> str:
    params = {
        "file": member_file(run, member, fhr),
        "lev_surface": "on",
        "var_APCP": "on",
        "subregion": "",
        "leftlon": 1 if tiny else LEFT,
        "rightlon": 2 if tiny else RIGHT,
        "toplat": 44 if tiny else TOP,
        "bottomlat": 43 if tiny else BOTTOM,
        "dir": f"/gefs.{run.date}/{run.cycle}/atmos/pgrb2ap5",
    }
    return f"{NOMADS_FILTER}?{urlencode(params)}"


def get_bytes(url: str, attempts: int = 4) -> bytes:
    global _NEXT_REQUEST_AT
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with _REQUEST_LOCK:
                now = time.monotonic()
                wait = max(0.0, _NEXT_REQUEST_AT - now)
                _NEXT_REQUEST_AT = max(now, _NEXT_REQUEST_AT) + REQUEST_INTERVAL_SECONDS
            if wait:
                time.sleep(wait)
            response = SESSION.get(url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            data = response.content
            if len(data) < 16 or data[:4] != b"GRIB":
                raise RuntimeError(f"réponse non-GRIB ({len(data)} octets)")
            return data
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"échec NOMADS: {last}")


def detect_latest_run(now: datetime | None = None) -> Run:
    # Sonde plusieurs lots de membres à +384 h : un seul membre disponible ne
    # suffit pas, car NOMADS publie parfois les fichiers par vagues.
    for run in candidates(now or utcnow()):
        try:
            for member in ("c00", "p01", "p15", "p30"):
                get_bytes(nomads_url(run, member, 384, tiny=True), attempts=1)
            print("Run GEFS complet détecté :", iso(run.dt))
            return run
        except Exception as exc:
            print(f"Cycle {run.run_id} incomplet: {exc}")
    raise RuntimeError("Aucun cycle GEFS complet jusqu'à +384 h dans les 54 dernières heures")


def attr_text(da: Any, key: str) -> str:
    value = da.attrs.get(key, "")
    return "" if value is None else str(value)


def parse_grib(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, str, str]:
    try:
        import cfgrib
    except Exception as exc:
        raise RuntimeError(f"cfgrib indisponible: {exc}") from exc

    best = None
    best_score = -1
    for dataset in cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""}):
        for name in dataset.data_vars:
            da = dataset[name]
            short = attr_text(da, "GRIB_shortName").lower()
            long_name = attr_text(da, "GRIB_name").lower()
            score = (100 if short in {"tp", "apcp"} else 0) + (50 if "precipitation" in long_name else 0)
            if score > best_score:
                best, best_score = da, score
    if best is None or best_score <= 0:
        raise RuntimeError("champ APCP absent")

    da = best
    lat_name = "latitude" if "latitude" in da.dims else "lat"
    lon_name = "longitude" if "longitude" in da.dims else "lon"
    for dim in list(da.dims):
        if dim not in (lat_name, lon_name):
            da = da.isel({dim: 0})
    da = da.transpose(lat_name, lon_name)
    lat = np.asarray(da[lat_name].values, dtype=float)
    lon = np.asarray(da[lon_name].values, dtype=float)
    values = np.squeeze(np.asarray(da.values, dtype=float))
    lon = np.where(lon > 180.0, lon - 360.0, lon)
    lat_order, lon_order = np.argsort(lat), np.argsort(lon)
    lat, lon = lat[lat_order], lon[lon_order]
    values = values[np.ix_(lat_order, lon_order)]
    values = np.where(values < -0.01, np.nan, np.maximum(values, 0.0))
    return lat, lon, values, attr_text(da, "GRIB_stepRange"), attr_text(da, "GRIB_stepType")


def parse_step_range(value: str, fhr: int) -> tuple[int, int]:
    nums = [int(x) for x in re.findall(r"\d+", value or "")]
    return (nums[0], nums[-1]) if len(nums) >= 2 else (max(0, fhr - 6), fhr)


def interval_from_raw(
    raw: np.ndarray,
    step_range: str,
    step_type: str,
    fhr: int,
    previous_raw: np.ndarray | None,
    previous_fhr: int | None,
) -> np.ndarray:
    start_h, end_h = parse_step_range(step_range, fhr)
    cumulative = start_h == 0 and end_h == fhr and ("accum" in step_type.lower() or fhr > 6)
    if cumulative and previous_raw is not None and previous_fhr is not None and previous_fhr < fhr:
        return np.maximum(raw - previous_raw, 0.0)
    return np.maximum(raw, 0.0)


def fetch_boundary() -> tuple[dict[str, Any] | None, str]:
    polygons: list[Any] = []
    loaded: list[str] = []
    for code, url in BOUNDARY_URLS.items():
        try:
            response = SESSION.get(url, timeout=(10, 30))
            response.raise_for_status()
            body = response.json()
            geometry = body.get("geometry") if body.get("type") == "Feature" else body
            if not geometry or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
                raise RuntimeError("géométrie absente ou invalide")
            coordinates = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
            polygons.extend(coordinates)
            loaded.append(code)
        except Exception as exc:
            print(f"::warning::Contour région {code} indisponible: {exc}")
    if polygons:
        return {"type": "MultiPolygon", "coordinates": polygons}, f"france-geojson — contours IGN/INSEE (régions {','.join(loaded)})"
    raise RuntimeError("Contour Occitanie indisponible : publication annulée pour éviter d'inclure PACA")


def point_in_ring(x: float, y: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][:2]
        xj, yj = ring[j][:2]
        if (yi > y) != (yj > y):
            crossing = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < crossing:
                inside = not inside
        j = i
    return inside


def point_in_polygon(x: float, y: float, polygon: list[list[list[float]]]) -> bool:
    return bool(polygon and point_in_ring(x, y, polygon[0]) and not any(point_in_ring(x, y, hole) for hole in polygon[1:]))


def build_mask(lat: np.ndarray, lon: np.ndarray, geometry: dict[str, Any] | None) -> np.ndarray:
    if geometry is None:
        return np.ones((len(lat), len(lon)), dtype=bool)
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    return np.asarray([[any(point_in_polygon(float(x), float(y), poly) for poly in polygons) for x in lon] for y in lat], dtype=bool)


def masked(arr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return np.where(mask, arr, np.nan)


def matrix(arr: np.ndarray, decimals: int = 1) -> list[list[float | None]]:
    result: list[list[float | None]] = []
    for row in arr:
        result.append([None if not math.isfinite(float(v)) else round(float(v), decimals) for v in row])
    return result


def ensemble_stats(stack: np.ndarray, mask: np.ndarray, thresholds: tuple[float, ...]) -> dict[str, np.ndarray]:
    with np.errstate(invalid="ignore"):
        stats = {
            "mean": np.nanmean(stack, axis=0),
            "median": np.nanmedian(stack, axis=0),
            "p10": np.nanpercentile(stack, 10, axis=0),
            "p90": np.nanpercentile(stack, 90, axis=0),
        }
        for threshold in thresholds:
            stats[f"prob_ge_{int(threshold)}mm_pct"] = np.mean(stack >= threshold, axis=0) * 100.0
    return {key: masked(value, mask) for key, value in stats.items()}


def regional_summary(stack: np.ndarray, lat: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    weights = np.cos(np.deg2rad(lat))[:, None] * mask
    denominator = float(np.sum(weights))
    member_means = np.nansum(stack * weights[None, :, :], axis=(1, 2)) / denominator
    return {
        "mean_mm": round(float(np.mean(member_means)), 1),
        "median_mm": round(float(np.median(member_means)), 1),
        "p10_mm": round(float(np.percentile(member_means, 10)), 1),
        "p90_mm": round(float(np.percentile(member_means, 90)), 1),
        **{f"prob_ge_{int(t)}mm_pct": round(float(np.mean(member_means >= t) * 100), 1) for t in THRESHOLDS_MM},
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def download_member(run: Run, member: str, fhr: int, directory: Path) -> tuple[str, Path]:
    path = directory / f"{member}_f{fhr:03d}.grib2"
    path.write_bytes(get_bytes(nomads_url(run, member, fhr)))
    return member, path


def process_run(run: Run, output_dir: Path, hours: list[int], workers: int) -> dict[str, Any]:
    geometry, boundary_source = fetch_boundary()
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_raw: dict[str, np.ndarray] = {}
    previous_fhr: dict[str, int] = {}
    cumulative: dict[str, np.ndarray] = {}
    frames: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    base_lat: np.ndarray | None = None
    base_lon: np.ndarray | None = None
    region_mask: np.ndarray | None = None

    with tempfile.TemporaryDirectory(prefix="am-gefs-") as tmpname:
        tmp = Path(tmpname)
        for fhr in hours:
            print(f"GEFS {run.run_id} +{fhr:03d} h : {len(MEMBERS)} membres")
            paths: dict[str, Path] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                jobs = {pool.submit(download_member, run, member, fhr, tmp): member for member in MEMBERS}
                for future in as_completed(jobs):
                    member = jobs[future]
                    try:
                        name, path = future.result()
                        paths[name] = path
                    except Exception as exc:
                        failures.append({"forecast_hour": fhr, "member": member, "error": str(exc)})

            interval_arrays: list[np.ndarray] = []
            cumulative_arrays: list[np.ndarray] = []
            used_members: list[str] = []
            for member in MEMBERS:
                path = paths.get(member)
                if path is None:
                    continue
                try:
                    lat, lon, raw, step_range, step_type = parse_grib(path)
                    if base_lat is None:
                        base_lat, base_lon = lat, lon
                        region_mask = build_mask(lat, lon, geometry)
                        if not np.any(region_mask):
                            raise RuntimeError("le masque Occitanie ne contient aucun point GEFS")
                    elif not (np.array_equal(lat, base_lat) and np.array_equal(lon, base_lon)):
                        raise RuntimeError("grille différente de la grille de référence")
                    inc = interval_from_raw(raw, step_range, step_type, fhr, previous_raw.get(member), previous_fhr.get(member))
                    previous_raw[member], previous_fhr[member] = raw, fhr
                    cumulative[member] = cumulative.get(member, np.zeros_like(inc)) + inc
                    interval_arrays.append(inc)
                    cumulative_arrays.append(cumulative[member].copy())
                    used_members.append(member)
                except Exception as exc:
                    failures.append({"forecast_hour": fhr, "member": member, "error": str(exc)})
                finally:
                    path.unlink(missing_ok=True)

            if len(used_members) < 25 or base_lat is None or base_lon is None or region_mask is None:
                print(f"::warning::Échéance +{fhr:03d} h ignorée: {len(used_members)}/31 membres")
                continue

            interval_stack = np.stack(interval_arrays)
            cumulative_stack = np.stack(cumulative_arrays)
            interval_stats = ensemble_stats(interval_stack, region_mask, ())
            cumulative_stats = ensemble_stats(cumulative_stack, region_mask, THRESHOLDS_MM)
            fields = {
                "precipitation_6h_mean_mm": matrix(interval_stats["mean"]),
                "precipitation_6h_median_mm": matrix(interval_stats["median"]),
                "precipitation_cumulative_mean_mm": matrix(cumulative_stats["mean"]),
                "precipitation_cumulative_median_mm": matrix(cumulative_stats["median"]),
                "precipitation_cumulative_p10_mm": matrix(cumulative_stats["p10"]),
                "precipitation_cumulative_p90_mm": matrix(cumulative_stats["p90"]),
            }
            for threshold in THRESHOLDS_MM:
                key = f"prob_ge_{int(threshold)}mm_pct"
                fields[f"precipitation_cumulative_{key}"] = matrix(cumulative_stats[key], 0)

            filename = f"f{fhr:03d}.json"
            payload = {
                "status": "ok",
                "schema_version": 1,
                "module_version": VERSION,
                "model": "NOAA/NCEP GEFS 0.5 degree",
                "area": {"codes": ["76"], "name": "Occitanie", "boundary_source": boundary_source},
                "run_utc": iso(run.dt),
                "forecast_hour": fhr,
                "valid_utc": iso(run.dt + timedelta(hours=fhr)),
                "member_count": len(used_members),
                "members": used_members,
                "grid": {
                    "spacing_deg": 0.5,
                    "latitudes": [round(float(v), 2) for v in base_lat],
                    "longitudes": [round(float(v), 2) for v in base_lon],
                    "mask": matrix(region_mask.astype(float), 0),
                },
                "regional_summary_cumulative": regional_summary(cumulative_stack, base_lat, region_mask),
                "fields": fields,
            }
            write_json(output_dir / filename, payload)
            frames.append({
                "forecast_hour": fhr,
                "valid_utc": payload["valid_utc"],
                "member_count": len(used_members),
                "file": filename,
                "regional_summary_cumulative": payload["regional_summary_cumulative"],
            })

    if not frames:
        raise RuntimeError("aucune échéance GEFS produite")
    index = {
        "status": "ok",
        "schema_version": 1,
        "module_version": VERSION,
        "build_id": BUILD_ID,
        "model": "NOAA/NCEP GEFS 0.5 degree",
        "run_id": run.run_id,
        "run_utc": iso(run.dt),
        "generated_at": iso(utcnow()),
        "area": {"codes": ["76"], "name": "Occitanie", "coverage": {"west": LEFT, "east": RIGHT, "south": BOTTOM, "north": TOP}, "boundary_source": boundary_source},
        "members_expected": list(MEMBERS),
        "member_count_expected": len(MEMBERS),
        "thresholds_mm": list(THRESHOLDS_MM),
        "frames": frames,
        "failures": failures,
        "source": "NOAA/NCEP NOMADS GEFS 0.5°",
    }
    write_json(output_dir / "index.json", index)
    return index


def self_test() -> int:
    run = Run("20260921", "00", datetime(2026, 9, 21, tzinfo=timezone.utc))
    assert member_file(run, "p30", 384) == "gep30.t00z.pgrb2a.0p50.f384"
    assert len(MEMBERS) == 31 and MEMBERS[0] == "c00" and MEMBERS[-1] == "p30"
    assert parse_step_range("6-12", 12) == (6, 12)
    raw1 = np.array([[2.0, 4.0]])
    raw2 = np.array([[5.0, 9.0]])
    assert np.array_equal(interval_from_raw(raw2, "0-12", "accum", 12, raw1, 6), np.array([[3.0, 5.0]]))
    assert np.array_equal(interval_from_raw(raw2, "6-12", "accum", 12, raw1, 6), raw2)
    square = [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]
    assert point_in_polygon(1, 1, square) and not point_in_polygon(3, 1, square)
    stack = np.asarray([[[0.0]], [[10.0]], [[20.0]]])
    stats = ensemble_stats(stack, np.asarray([[True]]), (10.0,))
    assert float(stats["median"][0, 0]) == 10.0
    assert round(float(stats["prob_ge_10mm_pct"][0, 0]), 1) == 66.7
    print("Self-test GEFS Méditerranée OK")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="build/gefs-occitanie/current")
    parser.add_argument("--hours", default=",".join(str(v) for v in FORECAST_HOURS))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--request-interval", type=float, default=REQUEST_INTERVAL_SECONDS)
    parser.add_argument("--run", help="Cycle imposé au format YYYYMMDDHH")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    global REQUEST_INTERVAL_SECONDS
    args = parse_args()
    if args.self_test:
        return self_test()
    REQUEST_INTERVAL_SECONDS = max(0.1, float(args.request_interval))
    hours = sorted({int(v.strip()) for v in args.hours.split(",") if v.strip()})
    if args.run:
        dt = datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        run = Run(dt.strftime("%Y%m%d"), f"{dt.hour:02d}", dt)
    else:
        run = detect_latest_run()
    output_dir = Path(args.output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    index = process_run(run, output_dir, hours, max(1, min(args.workers, 12)))
    print("Index :", output_dir / "index.json")
    print("Run :", index["run_id"], "— échéances :", len(index["frames"]), "/", len(hours))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERREUR FATALE GEFS : {exc}", file=sys.stderr)
        raise

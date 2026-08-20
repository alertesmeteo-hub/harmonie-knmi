#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — SST France continue
Version 1.0.0

Source :
NASA/JPL MUR SST v4.1 via NOAA CoastWatch ERDDAP
Dataset : jplMURSST41
Résolution native : 0.01°
Rendu France : stride 2 => ~0.02° (~2 km)

Sorties :
- sst_france.png
- sst_france_metadata.json

Le PNG est un raster transparent géoréférencé par les bounds JSON.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np
import requests
from netCDF4 import Dataset, num2date
from matplotlib import image as mpimg
from matplotlib.colors import LinearSegmentedColormap, Normalize


VERSION = "1.0.0"
SCHEMA_VERSION = 1
BUILD_ID = "sst-france-mur-2km-20260820"

ERDDAP_BASE = (
    "https://coastwatch.pfeg.noaa.gov/"
    "erddap/griddap/jplMURSST41"
)

DATASET_ID = "jplMURSST41"
VARIABLE = "analysed_sst"

# France + mers proches :
# Manche, mer du Nord, golfe de Gascogne, Méditerranée, Corse.
SOUTH = 39.0
NORTH = 55.0
WEST = -10.0
EAST = 15.0

# MUR = 0.01°. Stride 2 -> environ 0.02°.
STRIDE = 2

OUTPUT_PNG = Path("sst_france.png")
OUTPUT_JSON = Path("sst_france_metadata.json")
TEMP_NC = Path("sst_france_latest.nc")

HTTP_TIMEOUT = 180

session = requests.Session()
session.headers.update({
    "User-Agent": f"alertes-meteo-sst-france/{VERSION}",
})


def utcnow_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def build_nc_url() -> str:
    query = (
        f"{VARIABLE}"
        f"[(last)]"
        f"[({SOUTH}):{STRIDE}:({NORTH})]"
        f"[({WEST}):{STRIDE}:({EAST})]"
    )

    encoded = quote(
        query,
        safe="[]():,.-"
    )

    return f"{ERDDAP_BASE}.nc?{encoded}"


def download_latest() -> None:
    url = build_nc_url()

    print("Téléchargement MUR SST :")
    print(url)

    response = session.get(
        url,
        timeout=HTTP_TIMEOUT,
    )

    response.raise_for_status()

    content_type = (
        response.headers.get("content-type")
        or ""
    ).lower()

    if (
        "text/html" in content_type
        or "text/plain" in content_type
    ):
        preview = response.text[:500]
        raise RuntimeError(
            "Réponse ERDDAP inattendue : "
            + preview
        )

    TEMP_NC.write_bytes(
        response.content
    )

    print(
        "NetCDF :",
        len(response.content),
        "octets",
    )


def analysis_time_iso(
    dataset: Dataset,
) -> str:
    var = dataset.variables["time"]

    dt = num2date(
        var[0],
        units=var.units,
        calendar=getattr(
            var,
            "calendar",
            "standard",
        ),
    )

    return dt.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def build_colormap():
    """
    Palette SST lisible :
    violet/bleu -> cyan -> vert -> jaune -> orange -> rouge.
    Echelle fixe -2 à 32 °C.
    """

    colors = [
        "#4527a0",
        "#283593",
        "#1565c0",
        "#039be5",
        "#00acc1",
        "#00bfa5",
        "#43a047",
        "#c0ca33",
        "#fdd835",
        "#fb8c00",
        "#f4511e",
        "#d32f2f",
        "#880e4f",
    ]

    return LinearSegmentedColormap.from_list(
        "alertes_meteo_sst",
        colors,
        N=256,
    )


def render_png(
    values: np.ndarray,
) -> None:
    data = np.ma.asarray(
        values,
        dtype=float,
    )

    if data.ndim == 3:
        data = data[0]

    if data.ndim != 2:
        raise RuntimeError(
            f"Forme SST inattendue : {data.shape}"
        )

    invalid = (
        np.ma.getmaskarray(data)
        | ~np.isfinite(
            np.ma.filled(data, np.nan)
        )
    )

    raw = np.ma.filled(
        data,
        np.nan,
    )

    # Valeurs physiquement aberrantes masquées.
    invalid |= (raw < -3.0)
    invalid |= (raw > 45.0)

    cmap = build_colormap()
    norm = Normalize(
        vmin=-2.0,
        vmax=32.0,
        clip=True,
    )

    rgba = cmap(
        norm(
            np.nan_to_num(
                raw,
                nan=-2.0,
            )
        )
    )

    rgba[..., 3] = np.where(
        invalid,
        0.0,
        0.90,
    )

    # NetCDF latitude sud -> nord.
    # Image Leaflet : première ligne = nord.
    rgba = np.flipud(rgba)

    mpimg.imsave(
        OUTPUT_PNG,
        rgba,
    )


def finite_stats(
    values: np.ndarray,
) -> tuple[float | None, float | None]:
    data = np.ma.asarray(
        values,
        dtype=float,
    )

    raw = np.ma.filled(
        data,
        np.nan,
    )

    valid = raw[
        np.isfinite(raw)
        & (raw >= -3.0)
        & (raw <= 45.0)
    ]

    if valid.size == 0:
        return None, None

    return (
        round(float(np.min(valid)), 1),
        round(float(np.max(valid)), 1),
    )


def main() -> int:
    print(
        f"=== SST France v{VERSION} ==="
    )
    print("Build :", BUILD_ID)

    download_latest()

    with Dataset(
        TEMP_NC,
        "r",
    ) as dataset:
        lat = np.asarray(
            dataset.variables["latitude"][:],
            dtype=float,
        )

        lon = np.asarray(
            dataset.variables["longitude"][:],
            dtype=float,
        )

        sst = dataset.variables[
            VARIABLE
        ][:]

        analysis_time = analysis_time_iso(
            dataset
        )

        render_png(sst)

        min_sst, max_sst = finite_stats(
            sst
        )

        south = round(
            float(np.min(lat)),
            4,
        )
        north = round(
            float(np.max(lat)),
            4,
        )
        west = round(
            float(np.min(lon)),
            4,
        )
        east = round(
            float(np.max(lon)),
            4,
        )

        lat_step = (
            abs(float(lat[1] - lat[0]))
            if len(lat) > 1
            else None
        )

        lon_step = (
            abs(float(lon[1] - lon[0]))
            if len(lon) > 1
            else None
        )

        resolution_deg = None

        if lat_step and lon_step:
            resolution_deg = round(
                max(
                    lat_step,
                    lon_step,
                ),
                4,
            )

        metadata = {
            "schema_version": SCHEMA_VERSION,
            "module_version": VERSION,
            "build_id": BUILD_ID,
            "status": "ok",

            "generated_at": utcnow_iso(),
            "analysis_time": analysis_time,

            "title": (
                "Température de surface "
                "de la mer — SST France"
            ),

            "source": {
                "provider": "NASA/JPL",
                "distribution": (
                    "NOAA CoastWatch ERDDAP"
                ),
                "dataset_id": DATASET_ID,
                "product": (
                    "MUR SST Analysis fv04.1"
                ),
                "variable": VARIABLE,
                "native_resolution_deg": 0.01,
                "native_resolution_label": (
                    "~1 km"
                ),
            },

            "render": {
                "resolution_deg": (
                    resolution_deg
                ),
                "resolution_label": (
                    "~2 km"
                ),
                "stride": STRIDE,
                "bounds": {
                    "south": south,
                    "north": north,
                    "west": west,
                    "east": east,
                },
                "width_cells": int(
                    len(lon)
                ),
                "height_cells": int(
                    len(lat)
                ),
                "color_scale_min_c": -2.0,
                "color_scale_max_c": 32.0,
                "opacity": 0.90,
                "png": "sst_france.png",
            },

            "stats": {
                "min_c": min_sst,
                "max_c": max_sst,
            },

            "point_query": {
                "dataset_id": DATASET_ID,
                "variable": VARIABLE,
                "base_url": ERDDAP_BASE,
                "note": (
                    "Valeur native 0.01° "
                    "interrogée au clic."
                ),
            },
        }

    OUTPUT_JSON.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    if TEMP_NC.exists():
        TEMP_NC.unlink()

    print()
    print("=== CONTROLE ===")
    print("Version :", VERSION)
    print("Schema :", SCHEMA_VERSION)
    print("Analyse :", analysis_time)
    print(
        "Grille :",
        metadata["render"]["width_cells"],
        "x",
        metadata["render"]["height_cells"],
    )
    print(
        "Résolution :",
        metadata["render"]["resolution_label"],
    )
    print(
        "Min/Max :",
        min_sst,
        "/",
        max_sst,
        "°C",
    )
    print(
        "PNG :",
        OUTPUT_PNG.stat().st_size,
        "octets",
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(
            main()
        )
    except Exception as exc:
        print(
            "ERREUR FATALE :",
            exc,
            file=sys.stderr,
        )
        raise

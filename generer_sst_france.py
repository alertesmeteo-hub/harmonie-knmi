#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Alertes-Meteo.com — SST France continue
Version 1.0.3

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


VERSION = "1.0.3"
SCHEMA_VERSION = 1
BUILD_ID = "sst-france-mur-webmercator-hover-v103-20260820"

ERDDAP_BASE = (
    "https://coastwatch.pfeg.noaa.gov/"
    "erddap/griddap/jplMURSST41"
)

DATASET_ID = "jplMURSST41"
VARIABLE = "analysed_sst"
MASK_VARIABLE = "mask"

# France + mers proches :
# Manche, mer du Nord, golfe de Gascogne, Méditerranée, Corse.
SOUTH = 39.0
NORTH = 55.0
WEST = -10.0
EAST = 15.0

# MUR = 0.01°. Stride 2 -> environ 0.02°.
STRIDE = 2

OUTPUT_PNG = Path("sst_france.png")
OUTPUT_VALUES_PNG = Path("sst_france_values.png")
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
    selection = (
        f"[(last)]"
        f"[({SOUTH}):{STRIDE}:({NORTH})]"
        f"[({WEST}):{STRIDE}:({EAST})]"
    )

    # IMPORTANT : MUR fournit analysed_sst et un masque terre/mer séparé.
    # analysed_sst peut contenir une analyse interpolée sur la terre ; le
    # raster public doit donc utiliser explicitement la variable mask.
    query = (
        f"{VARIABLE}{selection},"
        f"{MASK_VARIABLE}{selection}"
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


def sea_mask_2d(mask_values: np.ndarray) -> np.ndarray:
    mask_data = np.ma.asarray(mask_values)

    if mask_data.ndim == 3:
        mask_data = mask_data[0]

    if mask_data.ndim != 2:
        raise RuntimeError(
            f"Forme masque MUR inattendue : {mask_data.shape}"
        )

    mask_raw = np.ma.filled(
        mask_data,
        0,
    ).astype(np.int16)

    # Classes officielles MUR :
    # 1  = open_sea
    # 2  = land
    # 4  = open_lake
    # 8  = open_sea_with_ice_in_the_grid
    # 16 = open_lake_with_ice_in_the_grid
    #
    # IMPORTANT :
    # on compare ici les VALEURS de classe strictement.
    # La v1.0.1 utilisait un test bit-à-bit, trop permissif,
    # qui pouvait laisser passer des lacs intérieurs.
    return np.isin(
        mask_raw,
        (1, 8),
    )



def mercator_y(lat_deg: np.ndarray | float) -> np.ndarray | float:
    """
    Coordonnée Y Web Mercator sans facteur R.
    Leaflet utilise cette géométrie pour positionner imageOverlay.
    """
    lat = np.clip(
        np.asarray(lat_deg, dtype=float),
        -85.05112878,
        85.05112878,
    )
    rad = np.radians(lat)
    return np.log(
        np.tan(np.pi / 4.0 + rad / 2.0)
    )


def inverse_mercator_y(y: np.ndarray) -> np.ndarray:
    return np.degrees(
        np.arctan(
            np.sinh(y)
        )
    )


def source_2d(values: np.ndarray) -> np.ndarray:
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

    return np.ma.filled(
        data,
        np.nan,
    )


def mercator_resample(
    values: np.ndarray,
    mask_values: np.ndarray,
    latitudes: np.ndarray,
    south_edge: float,
    north_edge: float,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Rééchantillonne les lignes de la grille régulière en latitude
    vers une image régulière en Y Web Mercator.

    C'est indispensable pour superposer précisément le raster à
    Leaflet/OSM : une image régulière en latitude étirée directement
    dans imageOverlay produit un petit décalage nord-sud.
    """

    raw = source_2d(values)
    sea = sea_mask_2d(mask_values)

    if sea.shape != raw.shape:
        raise RuntimeError(
            f"Masque MUR {sea.shape} incompatible avec SST {raw.shape}"
        )

    lat = np.asarray(
        latitudes,
        dtype=float,
    )

    if lat.ndim != 1 or len(lat) != raw.shape[0]:
        raise RuntimeError(
            "Latitudes incompatibles avec la grille SST"
        )

    height = raw.shape[0]

    y_north = float(
        mercator_y(north_edge)
    )
    y_south = float(
        mercator_y(south_edge)
    )

    # Centres des pixels de l'image finale, du nord vers le sud.
    target_y = (
        y_north
        - (
            np.arange(height, dtype=float)
            + 0.5
        )
        * (
            (y_north - y_south)
            / height
        )
    )

    target_lat = inverse_mercator_y(
        target_y
    )

    # La grille source est régulièrement espacée en latitude.
    if len(lat) > 1:
        step = float(
            np.median(
                np.diff(lat)
            )
        )
    else:
        step = 1.0

    if step == 0:
        raise RuntimeError(
            "Pas latitude MUR nul"
        )

    source_index = np.rint(
        (target_lat - lat[0]) / step
    ).astype(np.int64)

    source_index = np.clip(
        source_index,
        0,
        len(lat) - 1,
    )

    # target_y est nord -> sud, donc l'image est directement
    # dans l'ordre attendu par Leaflet : pas de flipud ensuite.
    return (
        raw[source_index, :],
        sea[source_index, :],
    )


def valid_ocean(
    raw: np.ndarray,
    sea: np.ndarray,
) -> np.ndarray:
    return (
        sea
        & np.isfinite(raw)
        & (raw >= -3.0)
        & (raw <= 45.0)
    )


def render_values_png(
    raw: np.ndarray,
    sea: np.ndarray,
) -> None:
    """
    PNG invisible de données pour le survol.

    Température encodée au dixième :
        code = round((T + 10) * 10)
        R = octet fort
        G = octet faible
        A = 255 uniquement sur océan valide

    Cela permet au navigateur de lire instantanément la SST
    sous la souris, sans requête réseau par mouvement.
    """
    valid = valid_ocean(
        raw,
        sea,
    )

    code = np.zeros(
        raw.shape,
        dtype=np.uint16,
    )

    code[valid] = np.clip(
        np.rint(
            (raw[valid] + 10.0)
            * 10.0
        ),
        0,
        65535,
    ).astype(np.uint16)

    rgba = np.zeros(
        raw.shape + (4,),
        dtype=np.uint8,
    )

    rgba[..., 0] = (
        code >> 8
    ).astype(np.uint8)

    rgba[..., 1] = (
        code & 255
    ).astype(np.uint8)

    rgba[..., 2] = 0
    rgba[..., 3] = np.where(
        valid,
        255,
        0,
    ).astype(np.uint8)

    mpimg.imsave(
        OUTPUT_VALUES_PNG,
        rgba,
    )


def render_png(
    raw: np.ndarray,
    sea: np.ndarray,
) -> None:
    if raw.ndim != 2:
        raise RuntimeError(
            f"Forme SST rendue inattendue : {raw.shape}"
        )

    if sea.shape != raw.shape:
        raise RuntimeError(
            "Masque et SST rendus incompatibles"
        )

    invalid = ~valid_ocean(
        raw,
        sea,
    )

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

    # raw est déjà rééchantillonné nord -> sud en Web Mercator.
    mpimg.imsave(
        OUTPUT_PNG,
        rgba,
    )



def finite_stats(
    raw: np.ndarray,
    sea: np.ndarray,
) -> tuple[float | None, float | None]:
    valid = raw[
        valid_ocean(
            raw,
            sea,
        )
    ]

    if valid.size == 0:
        return None, None

    return (
        round(
            float(np.min(valid)),
            1,
        ),
        round(
            float(np.max(valid)),
            1,
        ),
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

        if MASK_VARIABLE not in dataset.variables:
            raise RuntimeError(
                "Variable mask absente du NetCDF MUR"
            )

        mur_mask = dataset.variables[
            MASK_VARIABLE
        ][:]

        # Diagnostic explicite des classes présentes dans le sous-domaine.
        mask_diag = np.ma.asarray(mur_mask)
        if mask_diag.ndim == 3:
            mask_diag = mask_diag[0]
        mask_diag_raw = np.ma.filled(
            mask_diag,
            0,
        ).astype(np.int16)

        mask_values, mask_counts = np.unique(
            mask_diag_raw,
            return_counts=True,
        )

        print(
            "Classes masque MUR :",
            {
                int(v): int(c)
                for v, c in zip(mask_values, mask_counts)
            },
        )

        analysis_time = analysis_time_iso(
            dataset
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

        # Les coordonnées ERDDAP sont les CENTRES des cellules.
        # Leaflet imageOverlay attend les BORDS extérieurs de l'image.
        # On ajoute donc un demi-pas sur chaque côté.
        half_lat = (
            lat_step / 2.0
            if lat_step
            else 0.0
        )
        half_lon = (
            lon_step / 2.0
            if lon_step
            else 0.0
        )

        south = round(
            float(np.min(lat)) - half_lat,
            5,
        )
        north = round(
            float(np.max(lat)) + half_lat,
            5,
        )
        west = round(
            float(np.min(lon)) - half_lon,
            5,
        )
        east = round(
            float(np.max(lon)) + half_lon,
            5,
        )

        mercator_raw, mercator_sea = mercator_resample(
            sst,
            mur_mask,
            lat,
            south,
            north,
        )

        render_png(
            mercator_raw,
            mercator_sea,
        )

        render_values_png(
            mercator_raw,
            mercator_sea,
        )

        min_sst, max_sst = finite_stats(
            mercator_raw,
            mercator_sea,
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
                "mask_variable": MASK_VARIABLE,
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
                "land_mask_applied": True,
                "ocean_only_classes": [1, 8],
                "cell_edge_bounds": True,
                "web_mercator_resampled": True,
                "projection": "EPSG:3857-compatible",
                "sea_flags_kept": [1, 8],
                "png": "sst_france.png",
                "values_png": "sst_france_values.png",
                "values_encoding": {
                    "offset_c": 10.0,
                    "scale": 10.0,
                    "red": "high_byte",
                    "green": "low_byte",
                    "alpha_zero": "no_ocean_data"
                },
            },

            "stats": {
                "min_c": min_sst,
                "max_c": max_sst,
            },

            "point_query": {
                "dataset_id": DATASET_ID,
                "variable": VARIABLE,
                "mask_variable": MASK_VARIABLE,
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
        "PNG couleur :",
        OUTPUT_PNG.stat().st_size,
        "octets",
    )
    print(
        "PNG valeurs survol :",
        OUTPUT_VALUES_PNG.stat().st_size,
        "octets",
    )
    print(
        "Projection raster : Web Mercator / Leaflet"
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

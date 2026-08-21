#!/usr/bin/env python3
"""Génère des cartes WebP légères à partir des points HARMONIE France.

Le rendu est volontairement produit côté GitHub Actions. WordPress ne reçoit
ainsi qu'une image par paramètre et par échéance, au lieu de plusieurs millions
de valeurs brutes. La grille d'affichage suit la projection Web Mercator afin
que les cartes restent correctement positionnées dans le lecteur interactif.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from scipy.spatial import cKDTree


MAP_SCHEMA_VERSION = 1
DEFAULT_BOUNDS = {
    "south": 41.0,
    "west": -5.8,
    "north": 51.6,
    "east": 10.1,
}


@dataclass(frozen=True)
class LayerSpec:
    key: str
    label: str
    unit: str
    field: str
    stops: tuple[tuple[float, str], ...]
    decimals: int = 0
    transparent_below: float | None = None
    opacity: int = 224


LAYER_SPECS = (
    LayerSpec(
        "temperature",
        "Température à 2 m",
        "°C",
        "temperature_c",
        (
            (-20, "#5b2a86"),
            (-10, "#3158b7"),
            (0, "#4db7e5"),
            (10, "#50c878"),
            (20, "#f2dc4d"),
            (30, "#f28e2b"),
            (35, "#d83b32"),
            (40, "#9d174d"),
            (45, "#5b1037"),
        ),
        decimals=1,
    ),
    LayerSpec(
        "pluie_1h",
        "Précipitations sur 1 h",
        "mm",
        "precipitation_mm",
        (
            (0.1, "#b8f1ff"),
            (0.5, "#66d6ff"),
            (1, "#3b9eea"),
            (2, "#2d6ecf"),
            (5, "#3fb950"),
            (10, "#f1d648"),
            (20, "#f08a24"),
            (40, "#d9363e"),
            (70, "#8b1a68"),
        ),
        decimals=1,
        transparent_below=0.1,
        opacity=238,
    ),
    LayerSpec(
        "pluie_cumul",
        "Précipitations cumulées depuis le run",
        "mm",
        "precipitation_total_mm",
        (
            (0.1, "#d8f7ff"),
            (1, "#78d9fa"),
            (3, "#3f9ee8"),
            (5, "#2e6fc5"),
            (10, "#38b76a"),
            (20, "#b8d84c"),
            (30, "#f2d347"),
            (50, "#f08b2b"),
            (80, "#d73a43"),
            (120, "#922a78"),
            (200, "#54204f"),
        ),
        decimals=1,
        transparent_below=0.1,
        opacity=238,
    ),
    LayerSpec(
        "vent",
        "Vent moyen à 10 m",
        "km/h",
        "wind_speed_kmh",
        (
            (0, "#e8f5e9"),
            (10, "#9bd68f"),
            (20, "#55bd78"),
            (30, "#36a6a1"),
            (40, "#347ac1"),
            (50, "#6257b5"),
            (60, "#a43d91"),
            (80, "#d63b56"),
            (100, "#7e1636"),
        ),
    ),
    LayerSpec(
        "rafales",
        "Rafales à 10 m",
        "km/h",
        "wind_gust_kmh",
        (
            (0, "#edf7e8"),
            (20, "#a9d77d"),
            (40, "#f0cf46"),
            (60, "#ef8b2c"),
            (80, "#db3d3d"),
            (100, "#9e235d"),
            (130, "#4d1647"),
            (160, "#25152e"),
        ),
    ),
    LayerSpec(
        "pression",
        "Pression",
        "hPa",
        "pressure_hpa",
        (
            (960, "#562a7c"),
            (975, "#315ab4"),
            (990, "#2f98c5"),
            (1000, "#48b983"),
            (1010, "#c6d64f"),
            (1020, "#f0c646"),
            (1030, "#e57a34"),
            (1045, "#b52f43"),
        ),
    ),
    LayerSpec(
        "nebulosite",
        "Nébulosité totale",
        "%",
        "cloud_cover_pct",
        (
            (0, "#d8f1ff"),
            (20, "#c4e5ef"),
            (40, "#aebfcc"),
            (60, "#8999a7"),
            (80, "#65717c"),
            (100, "#39434c"),
        ),
    ),
    LayerSpec(
        "humidite",
        "Humidité relative à 2 m",
        "%",
        "humidity_pct",
        (
            (0, "#9a5429"),
            (20, "#d19a52"),
            (40, "#e3d16b"),
            (60, "#83ca82"),
            (80, "#48a6b6"),
            (100, "#28569f"),
        ),
    ),
    LayerSpec(
        "visibilite",
        "Visibilité",
        "km",
        "visibility_km",
        (
            (0, "#7b1f1f"),
            (1, "#cf3d35"),
            (2, "#ed8b33"),
            (5, "#e6ce4f"),
            (10, "#88c681"),
            (20, "#67b8d0"),
            (50, "#d8f1ff"),
        ),
        decimals=1,
    ),
)


MAJOR_CITIES = (
    ("Lille", 50.6292, 3.0573),
    ("Paris", 48.8566, 2.3522),
    ("Strasbourg", 48.5734, 7.7521),
    ("Brest", 48.3904, -4.4861),
    ("Nantes", 47.2184, -1.5536),
    ("Bordeaux", 44.8378, -0.5792),
    ("Toulouse", 43.6047, 1.4442),
    ("Lyon", 45.7640, 4.8357),
    ("Clermont-Fd", 45.7772, 3.0870),
    ("Marseille", 43.2965, 5.3698),
    ("Nice", 43.7102, 7.2620),
    ("Ajaccio", 41.9192, 8.7386),
)


def _hex_to_rgb(value: str) -> np.ndarray:
    value = value.lstrip("#")
    return np.asarray(tuple(int(value[index : index + 2], 16) for index in (0, 2, 4)))


def _mercator(latitude: np.ndarray | float) -> np.ndarray | float:
    radians = np.radians(np.clip(latitude, -85.0, 85.0))
    return np.log(np.tan(np.pi / 4.0 + radians / 2.0))


def _inverse_mercator(value: np.ndarray) -> np.ndarray:
    return np.degrees(2.0 * np.arctan(np.exp(value)) - np.pi / 2.0)


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


class HarmonieMapRenderer:
    """Rend les champs horaires HARMONIE en superpositions WebP."""

    def __init__(
        self,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
        output_directory: Path,
        *,
        width: int = 960,
        height: int = 720,
        bounds: dict[str, float] | None = None,
        mask_radius_degrees: float = 0.18,
    ) -> None:
        self.latitudes = np.asarray(latitudes, dtype=np.float64)
        self.longitudes = np.asarray(longitudes, dtype=np.float64)
        if self.latitudes.shape != self.longitudes.shape or self.latitudes.ndim != 1:
            raise ValueError("Coordonnées cartographiques invalides")
        if len(self.latitudes) < 4:
            raise ValueError("Au moins quatre points sont nécessaires")

        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.width = int(width)
        self.height = int(height)
        self.bounds = dict(bounds or DEFAULT_BOUNDS)
        self.mask_radius_degrees = float(mask_radius_degrees)
        self.steps: list[dict[str, Any]] = []

        self._prepare_interpolation()
        self._write_static_maps()

    def _prepare_interpolation(self) -> None:
        south = float(self.bounds["south"])
        north = float(self.bounds["north"])
        west = float(self.bounds["west"])
        east = float(self.bounds["east"])
        mercator_rows = np.linspace(_mercator(north), _mercator(south), self.height)
        grid_latitudes = _inverse_mercator(mercator_rows)
        grid_longitudes = np.linspace(west, east, self.width)
        longitude_grid, latitude_grid = np.meshgrid(grid_longitudes, grid_latitudes)

        latitude_midpoint = (south + north) / 2.0
        longitude_scale = math.cos(math.radians(latitude_midpoint))
        source = np.column_stack(
            (self.longitudes * longitude_scale, self.latitudes)
        )
        target = np.column_stack(
            (
                longitude_grid.ravel() * longitude_scale,
                latitude_grid.ravel(),
            )
        )
        neighbour_count = min(4, len(source))
        distances, indexes = cKDTree(source).query(
            target,
            k=neighbour_count,
            workers=-1,
        )
        if neighbour_count == 1:
            distances = distances[:, None]
            indexes = indexes[:, None]
        weights = 1.0 / np.maximum(distances, 1.0e-4) ** 2
        self._indexes = indexes.astype(np.int32, copy=False)
        self._weights = weights.astype(np.float32, copy=False)
        self._land_mask = (
            distances[:, 0].reshape(self.height, self.width)
            <= self.mask_radius_degrees
        )

    def _interpolate(self, values: np.ndarray) -> np.ndarray:
        source = np.asarray(values, dtype=np.float64)
        if source.shape != self.latitudes.shape:
            raise ValueError("Le champ ne correspond pas au catalogue national")
        selected = source[self._indexes]
        finite = np.isfinite(selected)
        weights = self._weights * finite
        denominator = np.sum(weights, axis=1)
        numerator = np.sum(np.where(finite, selected, 0.0) * weights, axis=1)
        result = np.full(len(denominator), np.nan, dtype=np.float32)
        valid = denominator > 0
        result[valid] = numerator[valid] / denominator[valid]
        return result.reshape(self.height, self.width)

    def _image_from_field(self, field: np.ndarray, spec: LayerSpec) -> Image.Image:
        stop_values = np.asarray([item[0] for item in spec.stops], dtype=np.float32)
        stop_colours = np.asarray([_hex_to_rgb(item[1]) for item in spec.stops])
        finite_field = np.isfinite(field)
        clipped = np.clip(
            np.where(finite_field, field, stop_values[0]),
            stop_values[0],
            stop_values[-1],
        )
        upper = np.searchsorted(stop_values, clipped, side="right")
        upper = np.clip(upper, 1, len(stop_values) - 1)
        lower = upper - 1
        low_values = stop_values[lower]
        high_values = stop_values[upper]
        fraction = np.divide(
            clipped - low_values,
            high_values - low_values,
            out=np.zeros_like(clipped),
            where=(high_values != low_values),
        )
        rgb = (
            stop_colours[lower] * (1.0 - fraction[..., None])
            + stop_colours[upper] * fraction[..., None]
        ).astype(np.uint8)
        alpha = np.full(field.shape, spec.opacity, dtype=np.uint8)
        valid = self._land_mask & finite_field
        if spec.transparent_below is not None:
            valid &= field >= spec.transparent_below
        alpha[~valid] = 0
        rgba = np.dstack((rgb, alpha))
        image = Image.fromarray(rgba, mode="RGBA")
        image = image.filter(ImageFilter.GaussianBlur(radius=0.75))
        smoothed = np.asarray(image).copy()
        smoothed[..., 3] = alpha
        return Image.fromarray(smoothed, mode="RGBA")

    def _pixel(self, latitude: float, longitude: float) -> tuple[int, int]:
        west = float(self.bounds["west"])
        east = float(self.bounds["east"])
        north_y = float(_mercator(float(self.bounds["north"])))
        south_y = float(_mercator(float(self.bounds["south"])))
        x = (longitude - west) / (east - west) * (self.width - 1)
        y = (north_y - float(_mercator(latitude))) / (north_y - south_y)
        y *= self.height - 1
        return int(round(x)), int(round(y))

    def _write_static_maps(self) -> None:
        mask = Image.fromarray((self._land_mask * 255).astype(np.uint8), mode="L")
        sea = Image.new("RGB", (self.width, self.height), "#dceef5")
        sea_draw = ImageDraw.Draw(sea)
        grid_colour = "#bdd7e2"
        for longitude in range(-5, 11, 2):
            x, _ = self._pixel(46.0, float(longitude))
            sea_draw.line((x, 0, x, self.height), fill=grid_colour, width=1)
        for latitude in range(42, 52, 2):
            _, y = self._pixel(float(latitude), 2.0)
            sea_draw.line((0, y, self.width, y), fill=grid_colour, width=1)
        land = Image.new("RGB", (self.width, self.height), "#f5f2e9")
        base = Image.composite(land, sea, mask)
        expanded = mask.filter(ImageFilter.MaxFilter(5))
        contracted = mask.filter(ImageFilter.MinFilter(5))
        edge = ImageChops.difference(expanded, contracted)
        border = Image.new("RGB", (self.width, self.height), "#476472")
        base.paste(border, mask=edge)
        base.save(
            self.output_directory / "fond.webp",
            "WEBP",
            quality=84,
            method=4,
        )

        labels = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(labels)
        font = _font(max(11, self.width // 80), bold=True)
        for name, latitude, longitude in MAJOR_CITIES:
            x, y = self._pixel(latitude, longitude)
            if not (0 <= x < self.width and 0 <= y < self.height):
                continue
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill="#102f44", outline="white")
            draw.text(
                (x + 6, y - 7),
                name,
                font=font,
                fill="#102f44",
                stroke_width=3,
                stroke_fill="white",
            )
        labels.save(
            self.output_directory / "villes.webp",
            "WEBP",
            quality=90,
            method=4,
        )

    def render_step(
        self,
        *,
        lead_hour: int,
        valid_time: datetime,
        fields: dict[str, np.ndarray],
    ) -> None:
        files: dict[str, str] = {}
        for spec in LAYER_SPECS:
            values = fields.get(spec.field)
            if values is None:
                continue
            destination_directory = self.output_directory / spec.key
            destination_directory.mkdir(parents=True, exist_ok=True)
            destination = destination_directory / f"{lead_hour:03d}.webp"
            image = self._image_from_field(self._interpolate(values), spec)
            image.save(destination, "WEBP", quality=80, method=4)
            files[spec.key] = f"maps/{spec.key}/{destination.name}"

        self.steps.append(
            {
                "lead_hour": int(lead_hour),
                "valid_time": valid_time.isoformat().replace("+00:00", "Z"),
                "files": files,
            }
        )

    def write_manifest(
        self,
        *,
        generated_at: str,
        run_time: str | None,
    ) -> dict[str, Any]:
        layers = {
            spec.key: {
                "label": spec.label,
                "unit": spec.unit,
                "decimals": spec.decimals,
                "transparent_below": spec.transparent_below,
                "stops": [
                    {"value": value, "color": colour}
                    for value, colour in spec.stops
                ],
            }
            for spec in LAYER_SPECS
        }
        manifest = {
            "schema_version": MAP_SCHEMA_VERSION,
            "status": "ok",
            "generated_at": generated_at,
            "run_time": run_time,
            "projection": "EPSG:3857",
            "bounds": self.bounds,
            "width": self.width,
            "height": self.height,
            "background": "maps/fond.webp",
            "labels": "maps/villes.webp",
            "layers": layers,
            "steps": self.steps,
        }
        destination = self.output_directory / "index.json"
        with destination.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        return manifest

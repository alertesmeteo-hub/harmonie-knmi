#!/usr/bin/env python3
"""Crée une petite archive GRIB1 de test compatible avec update_harmonie.py."""

from __future__ import annotations

import argparse
import tarfile
import tempfile
from pathlib import Path

import numpy as np
from eccodes import (
    codes_get_long,
    codes_grib_new_from_samples,
    codes_release,
    codes_set,
    codes_set_values,
    codes_write,
)


PARAMETERS = (
    # code, type de niveau, niveau, TRI, valeur au run, évolution par heure
    (1, 103, 0, 0, 101_300.0, -25.0),
    (11, 105, 2, 0, 283.15, 0.5),
    (17, 105, 2, 0, 280.15, 0.3),
    (20, 105, 0, 0, 12_000.0, -100.0),
    (33, 105, 10, 0, 4.0, 0.2),
    (34, 105, 10, 0, -2.0, -0.1),
    (52, 105, 2, 0, 0.78, -0.01),
    (71, 105, 0, 0, 0.65, 0.04),
    (73, 105, 0, 0, 0.35, 0.02),
    (74, 105, 0, 0, 0.45, 0.01),
    (75, 105, 0, 0, 0.20, 0.01),
    (162, 105, 10, 2, 7.0, 0.3),
    (163, 105, 10, 2, -3.0, -0.2),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="tests/sample_harmonie.tar")
    parser.add_argument("--hours", type=int, default=4)
    return parser.parse_args()


def add_message(
    handle,
    *,
    code: int,
    level_type: int,
    level: int,
    tri: int,
    lead: int,
    value: float,
) -> None:
    gid = codes_grib_new_from_samples("regular_ll_sfc_grib1")
    try:
        codes_set(gid, "dataDate", 20260816)
        codes_set(gid, "dataTime", 0)
        codes_set(gid, "indicatorOfParameter", code)
        codes_set(gid, "indicatorOfTypeOfLevel", level_type)
        codes_set(gid, "level", level)
        codes_set(gid, "timeRangeIndicator", tri)
        if tri in (2, 4):
            codes_set(gid, "P1", 0)
            codes_set(gid, "P2", lead)
        else:
            codes_set(gid, "P1", lead)
            codes_set(gid, "P2", 0)

        count = codes_get_long(gid, "numberOfValues")
        # Un léger gradient spatial permet aussi de vérifier la recherche du
        # point de grille le plus proche, au lieu d'utiliser un champ constant.
        values = np.linspace(value, value + 0.2, count, dtype=np.float64)
        codes_set_values(gid, values)
        codes_write(gid, handle)
    finally:
        codes_release(gid)


def make_archive(output: Path, hours: int) -> None:
    if not 1 <= hours <= 12:
        raise ValueError("--hours doit être compris entre 1 et 12")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sample-harmonie-") as temporary:
        temporary_path = Path(temporary)
        members: list[Path] = []
        for lead in range(hours):
            member = temporary_path / f"HA43_N55_202608160000_{lead:03d}00_GB"
            with member.open("wb") as handle:
                for code, level_type, level, tri, base, change in PARAMETERS:
                    add_message(
                        handle,
                        code=code,
                        level_type=level_type,
                        level=level,
                        tri=tri,
                        lead=lead,
                        value=base + change * lead,
                    )

                # Précipitations cumulées depuis le run (TRI 4).
                add_message(
                    handle,
                    code=61,
                    level_type=105,
                    level=0,
                    tri=4,
                    lead=lead,
                    value=0.4 * lead,
                )
            members.append(member)

        with tarfile.open(output, "w") as archive:
            for member in members:
                archive.add(member, arcname=member.name)


def main() -> None:
    args = parse_args()
    make_archive(Path(args.output).resolve(), args.hours)
    print(Path(args.output).resolve())


if __name__ == "__main__":
    main()

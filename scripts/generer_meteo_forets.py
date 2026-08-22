#!/usr/bin/env python3
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

DATASET_API = "https://www.data.gouv.fr/api/1/datasets/archives-de-la-meteo-des-forets/"
CSV_FALLBACK_PATTERN = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/BULLETIN/MDF/mdf_{year}.csv.gz"
GEOJSON_FALLBACK_URL = "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/departements-version-simplifiee.geojson"

DATA_DIR = Path(os.environ.get("MDF_DATA_DIR", "data"))
LATEST_JSON = DATA_DIR / "meteo_forets_latest.json"
HISTORY_JSON = DATA_DIR / "meteo_forets_history.json"
ARCHIVE_JSON = DATA_DIR / "meteo_forets_archive.json"
GEOJSON = DATA_DIR / "departements.geojson"

HEADERS = {
    "User-Agent": "alertes-meteo-github-meteo-forets/1.2.2",
    "Accept": "*/*",
}

TIMEOUT = 45
PARIS = ZoneInfo("Europe/Paris")


def get_response(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    if not response.content:
        raise RuntimeError(f"Réponse vide: {url}")
    return response


def get_json(url: str) -> dict[str, Any]:
    response = get_response(url)
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON inattendu: {url}")
    return payload


def norm_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.strip().lower()
    return re.sub(r"[\s.\-]+", "_", value)


def normalize_dep_code(value: Any) -> str:
    code = str(value or "").strip().upper()

    if code in {"2A", "2B"}:
        return code

    if code.isdigit():
        number = int(code)
        if 1 <= number <= 95:
            return f"{number:02d}"
        return str(number)

    return code


def parse_reference_time(value: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("Reference_time vide")

    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"

    if " " in candidate and "T" not in candidate:
        candidate = candidate.replace(" ", "T", 1)

    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PARIS)
        return dt.astimezone(PARIS)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=PARIS)
        except ValueError:
            continue

    raise ValueError(f"Date de référence illisible: {value!r}")


def choose_geojson_urls(dataset: dict[str, Any] | None) -> list[str]:
    urls: list[str] = []
    resources = dataset.get("resources", []) if isinstance(dataset, dict) else []

    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue

            url = str(resource.get("url") or "").strip()
            if not url:
                continue

            searchable = " ".join(
                str(resource.get(key) or "")
                for key in ("title", "description", "format")
            ).lower() + " " + url.lower()

            if "geojson" in searchable and url not in urls:
                urls.append(url)

    if GEOJSON_FALLBACK_URL not in urls:
        urls.append(GEOJSON_FALLBACK_URL)

    return urls


def download_geojson(urls: list[str]) -> tuple[dict[str, Any], str]:
    errors: list[str] = []

    for url in urls:
        try:
            response = get_response(url)
            payload = response.json()
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue

        if (
            isinstance(payload, dict) and
            isinstance(payload.get("features"), list) and
            payload["features"]
        ):
            return payload, url

        errors.append(f"{url}: GeoJSON invalide")

    raise RuntimeError(
        "Aucun fond départemental téléchargeable.\n" +
        "\n".join(errors[-5:])
    )


def choose_csv_urls(dataset: dict[str, Any] | None, now_year: int | None = None) -> list[str]:
    year = now_year or datetime.now(PARIS).year
    candidates: list[tuple[int, str, str]] = []

    resources = dataset.get("resources", []) if isinstance(dataset, dict) else []

    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue

            url = str(resource.get("url") or "").strip()
            if not url:
                continue

            searchable = " ".join(
                str(resource.get(key) or "")
                for key in ("title", "description", "format")
            ).lower() + " " + url.lower()

            if "csv" not in searchable and ".csv.gz" not in searchable:
                continue

            score = 0

            if f"mdf_{year}" in searchable or str(year) in searchable:
                score += 100

            if ".csv.gz" in searchable:
                score += 10

            modified = str(
                resource.get("last_modified")
                or resource.get("modified")
                or ""
            )

            candidates.append((score, modified, url))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    urls: list[str] = []

    for _, _, url in candidates:
        if url not in urls:
            urls.append(url)

    for fallback_year in (year, year - 1):
        if fallback_year >= 2024:
            url = CSV_FALLBACK_PATTERN.format(year=fallback_year)
            if url not in urls:
                urls.append(url)

    return urls


def decode_text_content(content: bytes) -> str:
    """Décode un CSV Météo-France sans supposer un encodage unique."""
    if content.startswith(b"\xff\xfe"):
        return content.decode("utf-16-le")
    if content.startswith(b"\xfe\xff"):
        return content.decode("utf-16-be")

    for encoding in (
        "utf-8-sig",
        "utf-8",
        "cp1252",
        "iso-8859-15",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
    ):
        try:
            text = content.decode(encoding)
            if text.count("\x00") <= max(2, len(text) // 100):
                return text
        except UnicodeDecodeError:
            continue

    return content.decode("utf-8", errors="replace")


def download_csv_text(urls: list[str]) -> tuple[str, str]:
    """
    Télécharge la première archive réellement exploitable.
    Le schéma CSV est validé après normalisation par parse_archive().
    """
    errors: list[str] = []

    for url in urls:
        try:
            response = get_response(url)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue

        content = response.content

        # Décompresser uniquement si la signature gzip est réellement présente.
        # requests peut avoir déjà décompressé un Content-Encoding gzip.
        if content[:2] == b"\x1f\x8b":
            try:
                content = gzip.decompress(content)
            except OSError as exc:
                errors.append(f"{url}: gzip invalide ({exc})")
                continue

        text = decode_text_content(content)

        if not text.strip():
            errors.append(f"{url}: fichier vide après décodage")
            continue

        try:
            parse_archive(text)
        except Exception as exc:
            lines = text.splitlines()
            first_line = lines[0][:220] if lines else "<vide>"
            errors.append(
                f"{url}: CSV non reconnu ({exc}); première ligne={first_line!r}"
            )
            continue

        return text, url

    raise RuntimeError(
        "Aucune archive Météo des forêts téléchargeable.\n" +
        "\n".join(errors[-5:])
    )

def detect_delimiter(first_line: str) -> str:
    candidates = {
        ";": first_line.count(";"),
        ",": first_line.count(","),
        "\t": first_line.count("\t"),
        "|": first_line.count("|"),
    }
    return max(candidates, key=candidates.get)


def pick(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key, "")
        if value != "":
            return value
    return ""


def parse_archive(text: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    lines = [line for line in text.splitlines() if line.strip()]

    if len(lines) < 2:
        raise RuntimeError("Archive CSV vide ou incomplète.")

    delimiter = detect_delimiter(lines[0])

    reader = csv.DictReader(
        io.StringIO("\n".join(lines)),
        delimiter=delimiter
    )

    if not reader.fieldnames:
        raise RuntimeError("Entête CSV absente.")

    headers = {norm_key(name or "") for name in reader.fieldnames}
    aliases = {
        "reference_time": {"reference_time", "referencetime", "date_reference", "date"},
        "dep_code": {"dep_code", "code_dep", "code_departement", "departement", "department"},
        "niveau_j1": {"niveau_j1", "j1", "level_j1", "niveauj1"},
        "niveau_j2": {"niveau_j2", "j2", "level_j2", "niveauj2"},
        "nom_dep": {"nom_dep", "dep_nom", "nom_departement", "name", "nom"},
    }

    missing = [
        canonical for canonical, choices in aliases.items()
        if not (headers & choices)
    ]
    if missing:
        raise RuntimeError(
            "Colonnes Météo-France manquantes: " + ", ".join(missing) +
            " ; en-têtes reçus: " + ", ".join(sorted(headers))
        )

    grouped: dict[str, dict[str, Any]] = {}

    for raw in reader:
        if not raw:
            continue

        row = {
            norm_key(str(key or "")): str(value or "").strip()
            for key, value in raw.items()
        }

        ref = pick(row, "reference_time", "referencetime", "date_reference", "date")
        code = normalize_dep_code(
            pick(row, "dep_code", "code_dep", "code_departement", "departement", "department")
        )
        name = pick(row, "nom_dep", "dep_nom", "nom_departement", "name", "nom")

        try:
            j1 = int(float(pick(row, "niveau_j1", "j1", "level_j1", "niveauj1")))
            j2 = int(float(pick(row, "niveau_j2", "j2", "level_j2", "niveauj2")))
        except (TypeError, ValueError):
            continue

        if (
            not ref or
            not code or
            not name or
            j1 not in {1, 2, 3, 4} or
            j2 not in {1, 2, 3, 4}
        ):
            continue

        try:
            dt = parse_reference_time(ref)
        except ValueError:
            continue

        key = dt.isoformat()
        record = grouped.setdefault(
            key,
            {
                "reference_time": dt.isoformat(),
                "_stamp": dt.timestamp(),
                "date": dt.strftime("%Y-%m-%d"),
                "departments": {},
            }
        )

        record["departments"][code] = {
            "code": code,
            "name": name,
            "j1": j1,
            "j2": j2,
        }

    if not grouped:
        raise RuntimeError("Aucun enregistrement Météo des forêts exploitable.")

    records = sorted(
        grouped.values(),
        key=lambda record: record["_stamp"]
    )

    # Une réémission peut exister le même jour : on conserve la plus récente
    # pour l'historique quotidien.
    latest_by_date: dict[str, dict[str, Any]] = {}

    for record in records:
        latest_by_date[record["date"]] = record

    daily_records = [
        latest_by_date[date]
        for date in sorted(latest_by_date)
    ]

    history: list[dict[str, Any]] = []
    archive: list[dict[str, Any]] = []

    for record in daily_records:
        counts_j1 = {str(level): 0 for level in range(1, 5)}
        counts_j2 = {str(level): 0 for level in range(1, 5)}
        max_level = 1

        departments = sorted(
            record["departments"].values(),
            key=lambda department: department["name"].casefold()
        )

        for department in departments:
            counts_j1[str(department["j1"])] += 1
            counts_j2[str(department["j2"])] += 1
            max_level = max(
                max_level,
                department["j1"],
                department["j2"]
            )

        history.append(
            {
                "date": record["date"],
                "reference_time": record["reference_time"],
                "max_level": max_level,
                "counts_j1": counts_j1,
                "counts_j2": counts_j2,
            }
        )

        archive.append(
            {
                "date": record["date"],
                "reference_time": record["reference_time"],
                "counts": {
                    "j1": counts_j1,
                    "j2": counts_j2,
                },
                "departments": departments,
            }
        )

    latest = archive[-1]

    latest_payload = {
        "ok": True,
        "source": "Météo-France / data.gouv.fr — Licence Ouverte ETALAB 2.0",
        "reference_time": latest["reference_time"],
        "bulletin_date": latest["date"],
        "counts": latest["counts"],
        "departments": latest["departments"],
    }

    return latest_payload, history, archive


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )


def fetch_dataset_metadata() -> dict[str, Any] | None:
    try:
        return get_json(DATASET_API)
    except Exception as exc:
        print(
            f"AVERTISSEMENT: catalogue data.gouv.fr indisponible ({exc}). "
            "Utilisation des URL de secours.",
            file=sys.stderr
        )
        return None


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    dataset = fetch_dataset_metadata()
    csv_urls = choose_csv_urls(dataset)
    csv_text, csv_url = download_csv_text(csv_urls)

    latest, history, archive = parse_archive(csv_text)

    geo, geo_url = download_geojson(
        choose_geojson_urls(dataset)
    )

    write_json(LATEST_JSON, latest)
    write_json(HISTORY_JSON, history)
    write_json(ARCHIVE_JSON, archive)
    write_json(GEOJSON, geo)

    print(f"Source CSV : {csv_url}")
    print(f"Dernier bulletin : {latest['reference_time']}")
    print(f"Départements : {len(latest['departments'])}")
    print(f"Jours archivés : {len(archive)}")
    print(f"Source GeoJSON : {geo_url}")
    print(f"GeoJSON : {len(geo['features'])} objets")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        raise

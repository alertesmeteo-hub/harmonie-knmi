#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chaîne complète : PDF Météo-France -> data/vigilance-jours-suivants.json

Règle de sécurité : si une seule étape échoue ou si le contrôle de structure
n'est pas satisfait, le fichier précédent est conservé tel quel et le script
sort en erreur. On ne publie jamais de données partielles ou inventées.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from parse_vjs_pdf import ParseError, parse  # noqa: E402

DATA_FILE = ROOT / "data" / "vigilance-jours-suivants.json"
CALIBRATION = ROOT / "data" / "geo" / "france-map-calibration.json"
PDF_FILE = ROOT / "build" / "prochains-jours.pdf"

EXPECTED_LABELS = ["J+2", "J+3", "J+4", "J+5", "J+6 et J+7"]
EXPECTED_GRANULARITY = ["department", "department", "quadrant", "quadrant", "national"]
LEVELS = {"none", "low", "medium", "high"}
QUADRANTS = {"NO", "NE", "SO", "SE"}


class ValidationError(RuntimeError):
    pass


def validate(data: dict) -> None:
    if data.get("schema_version") != 3:
        raise ValidationError("schema_version attendu 3, reçu %r" % data.get("schema_version"))

    cards = data.get("cards") or []
    labels = [c.get("label") for c in cards]
    if labels != EXPECTED_LABELS:
        raise ValidationError("échéances attendues %s, reçues %s" % (EXPECTED_LABELS, labels))

    for card, expected in zip(cards, EXPECTED_GRANULARITY):
        if card.get("granularity") != expected:
            raise ValidationError("%s : granularité %r attendue, %r reçue"
                                  % (card["label"], expected, card.get("granularity")))
        if not card.get("date_label"):
            raise ValidationError("%s : libellé de date manquant" % card["label"])

        if expected == "department":
            departments = card.get("departments") or {}
            if len(departments) != 96:
                raise ValidationError("%s : %d départements au lieu de 96"
                                      % (card["label"], len(departments)))
            for code, info in departments.items():
                if info.get("level") not in LEVELS:
                    raise ValidationError("%s / %s : niveau inconnu %r"
                                          % (card["label"], code, info.get("level")))
                if not info.get("name"):
                    raise ValidationError("%s / %s : nom de département manquant" % (card["label"], code))
        elif expected == "quadrant":
            zones = card.get("zones") or {}
            if set(zones) != QUADRANTS:
                raise ValidationError("%s : quarts attendus %s, reçus %s"
                                      % (card["label"], sorted(QUADRANTS), sorted(zones)))
            for name, zone in zones.items():
                if zone.get("level") not in LEVELS:
                    raise ValidationError("%s / %s : niveau inconnu %r"
                                          % (card["label"], name, zone.get("level")))
        else:
            national = card.get("national") or {}
            if national.get("level") not in LEVELS:
                raise ValidationError("%s : niveau national inconnu %r"
                                      % (card["label"], national.get("level")))

    legend = (data.get("legend") or {}).get("levels") or []
    colours = {entry["key"]: entry.get("colour") for entry in legend}
    for key in ("low", "medium", "high"):
        if not colours.get(key) or not colours[key].startswith("#"):
            raise ValidationError("couleur de légende manquante pour %r" % key)

    if not (data.get("summaries") or {}).get("j2_j3"):
        raise ValidationError("synthèse J+2/J+3 vide")
    if not (data.get("summaries") or {}).get("j4_j7"):
        raise ValidationError("synthèse J+4→J+7 vide")

    issued = (data.get("bulletin") or {}).get("issued_at")
    if not issued:
        raise ValidationError("date d'émission absente")
    age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(issued)).total_seconds() / 3600.0
    if age_hours > 36:
        raise ValidationError("bulletin vieux de %.1f h : source probablement figée" % age_hours)
    if age_hours < -2:
        raise ValidationError("bulletin daté dans le futur (%.1f h)" % age_hours)


def _comparable(data: dict) -> str:
    clone = json.loads(json.dumps(data))
    clone.pop("generated_at", None)
    return json.dumps(clone, ensure_ascii=False, sort_keys=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=None,
                        help="utiliser un PDF déjà téléchargé au lieu d'aller le chercher")
    parser.add_argument("--out", type=Path, default=DATA_FILE)
    parser.add_argument("--keep-pdf", action="store_true")
    args = parser.parse_args(argv)

    if args.pdf:
        pdf_path = args.pdf
    else:
        from fetch_vjs_pdf import fetch
        attempts = int(os.environ.get("VJS_RETRIES", "2"))
        last = None
        pdf_path = None
        for attempt in range(1, attempts + 1):
            try:
                pdf_path = fetch(PDF_FILE)
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                print("Tentative %d/%d échouée : %s" % (attempt, attempts, exc), file=sys.stderr)
        if pdf_path is None:
            print("Échec de récupération du PDF : %s" % last, file=sys.stderr)
            print("Le JSON précédent est conservé.", file=sys.stderr)
            return 1

    try:
        data = parse(pdf_path, CALIBRATION)
        validate(data)
    except (ParseError, ValidationError) as exc:
        print("Bulletin non exploitable : %s" % exc, file=sys.stderr)
        print("Le JSON précédent est conservé.", file=sys.stderr)
        return 2

    previous = None
    if args.out.exists():
        try:
            previous = json.loads(args.out.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            previous = None

    if previous and _comparable(previous) == _comparable(data):
        print("Situation inchangée : aucun commit nécessaire.")
        if not args.keep_pdf and pdf_path == PDF_FILE and pdf_path.exists():
            pdf_path.unlink()
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    summary = []
    for card in data["cards"]:
        summary.append("%s=%s" % (card["label"], card["max_level"]))
    print("JSON mis à jour (%s) — %s" % (data["bulletin"]["issued_label"], ", ".join(summary)))

    if not args.keep_pdf and pdf_path == PDF_FILE and pdf_path.exists():
        pdf_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

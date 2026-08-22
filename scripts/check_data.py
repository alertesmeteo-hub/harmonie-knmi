#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contrôle autonome du JSON publié (utilisé par le workflow GitHub)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_vjs import ValidationError, validate  # noqa: E402


def main(argv):
    if len(argv) != 2:
        print("usage: check_data.py <fichier.json>", file=sys.stderr)
        return 64
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    try:
        validate(data)
    except ValidationError as exc:
        print("CONTRÔLE ÉCHOUÉ : %s" % exc, file=sys.stderr)
        return 1

    cards = {c["label"]: c for c in data["cards"]}
    print("Contrôle OK.")
    print("  bulletin      : %s" % data["bulletin"]["issued_label"])
    for label, card in cards.items():
        if card["granularity"] == "department":
            counts = {}
            for info in card["departments"].values():
                counts[info["level"]] = counts.get(info["level"], 0) + 1
            detail = ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()))
            print("  %-11s : départements (%s)" % (label, detail))
        elif card["granularity"] == "quadrant":
            detail = ", ".join("%s=%s" % (z, v["level"]) for z, v in sorted(card["zones"].items()))
            print("  %-11s : quarts (%s)" % (label, detail))
        else:
            print("  %-11s : national (%s)" % (label, card["national"]["level"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

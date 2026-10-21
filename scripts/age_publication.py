#!/usr/bin/env python3
"""Âge en heures (entier) d'un JSON publié, d'après son champ generated_at. Usage : age_publication.py URL"""
import json
import sys
import urllib.request
from datetime import datetime, timezone

with urllib.request.urlopen(sys.argv[1], timeout=30) as r:
    genere = json.load(r)["generated_at"]
age = datetime.now(timezone.utc) - datetime.fromisoformat(genere.replace("Z", "+00:00"))
print(int(age.total_seconds() // 3600))

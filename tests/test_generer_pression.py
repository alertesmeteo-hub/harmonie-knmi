#!/usr/bin/env python3

from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util
import sys
import types


class DummySession:
    def __init__(self):
        self.headers = {}


# Le test porte sur le décodage et les calculs ; aucun appel réseau n'est fait.
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(
        Session=DummySession,
        RequestException=Exception,
    )


MODULE_PATH = Path(__file__).resolve().parents[1] / "generer_pression_meteofrance.py"
SPEC = importlib.util.spec_from_file_location("pression", MODULE_PATH)
pression = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(pression)


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content


# --- hPa conversion : pascals bruts (ex. 101300 Pa -> 1013.0 hPa). ---
assert pression.hpa("101300") == 1013.0
assert pression.hpa("100800") == 1008.0
assert pression.hpa("1013.0") == 1013.0  # déjà en hPa : gardé tel quel.
assert pression.hpa("5") is None  # hors plage plausible.
assert pression.hpa(None) is None

hour = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)
csv_bytes = (
    "geo_id_insee;lat;lon;validity_time;pmer;pres\n"
    "59001001;50.30;3.10;2026-08-28T08:00:00Z;101300;100800\n"
    "62002001;50.90;1.80;2026-08-28T08:00:00Z;99800;99500\n"
).encode("utf-8")

cache = {"schema_version": 1, "samples": []}
added = pression.add_latest_package_to_cache(cache, FakeResponse(csv_bytes), hour)
assert added == 2
assert len(cache["samples"]) == 2

first_sample = cache["samples"][0]
assert first_sample["id"] == "59001001"
assert first_sample["pressure_msl_hpa"] == 1013.0
assert first_sample["pressure_station_hpa"] == 1008.0
assert pression.department_code(first_sample["id"]) == "59"

# --- Variation : échantillon le plus proche de l'échéance visée. ---
cache["samples"].append({
    "id": "59001001",
    "time": "2026-08-28T05:00:00Z",  # 3 h avant le dernier paquet.
    "lat": 50.30,
    "lon": 3.10,
    "pressure_msl_hpa": 1015.0,
    "pressure_station_hpa": 1010.0,
})
station_samples = [s for s in cache["samples"] if s["id"] == "59001001"]
closest = pression.closest_sample_to(
    station_samples, hour - timedelta(hours=3), 40
)
assert closest is not None
assert closest["pressure_msl_hpa"] == 1015.0

print("Tests pression API Météo-France : OK")

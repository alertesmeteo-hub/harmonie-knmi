#!/usr/bin/env python3

from datetime import datetime, timezone
from pathlib import Path
import gzip
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


MODULE_PATH = Path(__file__).resolve().parents[1] / "generer_rafales_meteofrance.py"
SPEC = importlib.util.spec_from_file_location("rafales", MODULE_PATH)
rafales = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(rafales)


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content


hour = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)
csv_bytes = (
    "geo_id_insee;lat;lon;validity_time;raf;ddraf;ff;dd\n"
    "59001001;50.30;3.10;2026-08-21T08:00:00Z;10;270;5;180\n"
    "62002001;50.90;1.80;2026-08-21T08:00:00Z;15;250;7;200\n"
).encode("utf-8")

cache = {"schema_version": 1, "samples": []}
added = rafales.add_latest_package_to_cache(cache, FakeResponse(csv_bytes), hour)
assert added == 2
assert len(cache["samples"]) == 2

first = cache["samples"][0]
assert first["id"] == "59001001"
assert first["gust_kmh"] == 36.0
assert first["mean_wind_kmh"] == 18.0
assert first["gust_direction_deg"] == 270
assert rafales.department_code(first["id"]) == "59"

compressed_rows = rafales.parse_csv(gzip.compress(csv_bytes))
assert len(compressed_rows) == 2

cache["samples"].append({
    "id": "59001001",
    "time": "2026-08-21T07:00:00Z",
    "gust_kmh": 52.0,
    "gust_direction_deg": 300,
})
station_samples = [item for item in cache["samples"] if item["id"] == "59001001"]
value, direction, observed_at = rafales.choose_max(
    station_samples,
    "gust_kmh",
    "gust_direction_deg",
)
assert value == 52.0
assert direction == 300
assert observed_at == "2026-08-21T07:00:00Z"

print("Tests rafales API Météo-France : OK")

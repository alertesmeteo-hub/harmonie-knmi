#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


class DummySession:
    def __init__(self) -> None:
        self.headers = {}


fake_requests = types.SimpleNamespace(
    Session=DummySession,
    Response=object,
    RequestException=Exception,
)
sys.modules.setdefault("requests", fake_requests)

script = Path(__file__).resolve().parents[1] / "generer_classements_temperature.py"
spec = importlib.util.spec_from_file_location("classements_observations_mf", script)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_hourly_fields() -> None:
    hour = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)
    rows = module.simplify_package([
        {
            "geo_id_insee": "06088001",
            "validity_time": "2026-08-21T08:00:00Z",
            "t": "300.15",
            "tn": "299.15",
            "tx": "301.15",
            "td": "295.15",
            "u": "72",
            "pmer": "101325",
            "pres": "100825",
            "ff": "5",
            "dd": "240",
            "vv": "10000",
            "ht_neige": "0.05",
            "insolh": "0.5",
            "lat": "43.7",
            "lon": "7.2",
        }
    ], hour)
    assert len(rows) == 1
    row = rows[0]
    assert row["t"] == 27.0
    assert row["dew_point"] == 22.0
    assert row["humidity"] == 72.0
    assert row["pressure_msl"] == 1013.2
    assert row["pressure"] == 1008.2
    assert row["wind_speed"] == 18.0
    assert row["wind_direction"] == 240.0
    assert row["visibility_km"] == 10.0
    assert row["snow_depth_cm"] == 5.0
    assert row["sunshine_1h_minutes"] == 30.0


def test_climate_data() -> None:
    values = ";".join(str(value) for value in range(1, 14))
    text = f"""
La température la plus élevée (°C);
;{values};
Température maximale (Moyenne en °C);
;{values};
Température minimale (Moyenne en °C);
;{values};
La température la plus basse (°C);
;{values};
"""
    data = module.parse_climate_data(text)
    assert data["record_tmax_monthly"][7] == 8.0
    assert data["normal_tmax_monthly"][7] == 8.0
    assert data["normal_tmin_monthly"][7] == 8.0
    assert data["record_tmin_absolute"] == 13.0


def test_site_quality() -> None:
    text = """
QUALITE DU SITE
Paramètre Classe Réf. Début Fin
Humidite 3 Nr35B 21/02/2019
Pluie 2 Nr35B 21/02/2019
Temperature 1 Nr35B 21/02/2019
Vent 4 Nr35B 21/02/2019
CLASSE MESURES
Temperature B NR37
"""
    assert module.parse_quality_text(text) == {
        "humidity": 3,
        "rain": 2,
        "temperature": 1,
        "wind": 4,
    }


if __name__ == "__main__":
    test_hourly_fields()
    test_climate_data()
    test_site_quality()
    print("Tests observations Météo-France v1.1.0 : OK")

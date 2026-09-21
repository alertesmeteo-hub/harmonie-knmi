import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_gefs_occitanie.py"
SPEC = importlib.util.spec_from_file_location("update_gefs_occitanie", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_members_and_cycles():
    assert len(MODULE.MEMBERS) == 31
    assert MODULE.MEMBERS == ("c00",) + tuple(f"p{i:02d}" for i in range(1, 31))
    cycles = [run.cycle for run in list(MODULE.candidates(MODULE.datetime(2026, 9, 21, 17, tzinfo=MODULE.timezone.utc)))[:4]]
    assert cycles == ["12", "06", "00", "18"]


def test_cumulative_and_interval_apcp():
    previous = MODULE.np.array([[2.0, 4.0]])
    raw = MODULE.np.array([[5.0, 9.0]])
    cumulative_increment = MODULE.interval_from_raw(raw, "0-12", "accum", 12, previous, 6)
    interval = MODULE.interval_from_raw(raw, "6-12", "accum", 12, previous, 6)
    assert MODULE.np.array_equal(cumulative_increment, MODULE.np.array([[3.0, 5.0]]))
    assert MODULE.np.array_equal(interval, raw)


def test_probabilities_and_quantiles():
    stack = MODULE.np.asarray([[[0.0]], [[10.0]], [[20.0]]])
    stats = MODULE.ensemble_stats(stack, MODULE.np.asarray([[True]]), (10.0,))
    assert float(stats["median"][0, 0]) == 10.0
    assert round(float(stats["prob_ge_10mm_pct"][0, 0]), 1) == 66.7


def test_polygon_mask():
    polygon = [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]
    assert MODULE.point_in_polygon(1, 1, polygon)
    assert not MODULE.point_in_polygon(3, 1, polygon)

"""Tests for the file-based history store (NDJSON hot + Parquet cold + DuckDB reads).

The NDJSON path is dependency-free and always exercised. The Parquet/compaction
path needs DuckDB, so those tests `importorskip("duckdb")` — the suite still passes
on an environment that hasn't installed it yet, which mirrors how the store degrades
in production (no DuckDB -> never compacts -> pure NDJSON, nothing lost).
"""
import json
from datetime import date
from pathlib import Path

import pytest

from lib import history_store as hs


def _write_day(hist_dir, iso, records):
    p = Path(hist_dir) / f"ess-{iso}.ndjson"
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return p


# --- NDJSON hot path (no DuckDB required) ----------------------------------

def test_read_day_parses_ndjson_and_skips_blank_and_torn_lines(tmp_path):
    iso = "2026-06-15"
    p = _write_day(tmp_path, iso, [{"kind": "cycle", "ts": f"{iso}T00:00:00", "soc": 0.5}])
    # Simulate a crash mid-write: a blank line and a truncated trailing JSON object.
    with p.open("a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write('{"kind": "cycle", "ts": "2026-06-15T00:15:00", "soc":')
    recs = hs.read_day(iso, str(tmp_path))
    assert len(recs) == 1
    assert recs[0]["soc"] == 0.5


def test_read_day_missing_returns_empty(tmp_path):
    assert hs.read_day("2026-06-15", str(tmp_path)) == []


def test_read_day_accepts_date_objects(tmp_path):
    _write_day(tmp_path, "2026-06-15", [{"kind": "cycle", "ts": "x"}])
    assert len(hs.read_day(date(2026, 6, 15), str(tmp_path))) == 1


def test_available_days_lists_ndjson_sorted(tmp_path):
    _write_day(tmp_path, "2026-06-16", [{"ts": "b"}])
    _write_day(tmp_path, "2026-06-14", [{"ts": "a"}])
    assert hs.available_days(str(tmp_path)) == ["2026-06-14", "2026-06-16"]


def test_append_writes_ndjson_line(tmp_path):
    hs.append("2026-06-15", {"kind": "cycle", "ts": "t", "soc": 1}, str(tmp_path))
    hs.append("2026-06-15", {"kind": "settlement", "ts": "t2"}, str(tmp_path))
    recs = hs.read_day("2026-06-15", str(tmp_path))
    assert [r.get("kind") for r in recs] == ["cycle", "settlement"]


# --- Parquet cold path + compaction (DuckDB) -------------------------------

def test_compact_month_roundtrips_and_lists_days(tmp_path):
    pytest.importorskip("duckdb")
    days = {
        "2026-05-01": [
            {"kind": "cycle", "ts": "2026-05-01T00:00:00", "soc": 0.1, "price_buy": 0.3},
            {"kind": "settlement", "ts": "2026-05-01T00:00:05", "actual_net_eur": None},
        ],
        "2026-05-02": [{"kind": "cycle", "ts": "2026-05-02T00:00:00", "soc": 0.2}],
    }
    for iso, recs in days.items():
        _write_day(tmp_path, iso, recs)

    out = hs.compact_month(2026, 5, str(tmp_path), remove_ndjson=True)
    assert out is not None and Path(out).exists()
    assert not list(Path(tmp_path).glob("ess-2026-05-0*.ndjson"))   # sources removed
    # Byte-faithful round-trip, including the explicit null in the settlement record.
    assert hs.read_day("2026-05-01", str(tmp_path)) == days["2026-05-01"]
    assert hs.read_day("2026-05-02", str(tmp_path)) == days["2026-05-02"]
    assert set(hs.available_days(str(tmp_path))) == {"2026-05-01", "2026-05-02"}


def test_read_day_prefers_ndjson_over_parquet(tmp_path):
    pytest.importorskip("duckdb")
    iso = "2026-05-03"
    _write_day(tmp_path, iso, [{"kind": "cycle", "ts": "old", "soc": 0.0}])
    hs.compact_month(2026, 5, str(tmp_path), remove_ndjson=False)   # parquet now exists too
    _write_day(tmp_path, iso, [{"kind": "cycle", "ts": "new", "soc": 0.9}])   # fresher hot file
    recs = hs.read_day(iso, str(tmp_path))
    assert len(recs) == 1 and recs[0]["ts"] == "new" and recs[0]["soc"] == 0.9


def test_compact_month_no_files_returns_none(tmp_path):
    pytest.importorskip("duckdb")
    assert hs.compact_month(2020, 1, str(tmp_path)) is None


def test_compact_month_is_idempotent_and_preserves_prior_parquet(tmp_path):
    pytest.importorskip("duckdb")
    _write_day(tmp_path, "2026-05-10", [{"kind": "cycle", "ts": "2026-05-10T00:00:00", "soc": 0.4}])
    hs.compact_month(2026, 5, str(tmp_path), remove_ndjson=True)     # day1 -> parquet, ndjson gone
    # A late-arriving second day for the same month is compacted without dropping day1.
    _write_day(tmp_path, "2026-05-11", [{"kind": "cycle", "ts": "2026-05-11T00:00:00", "soc": 0.6}])
    hs.compact_month(2026, 5, str(tmp_path), remove_ndjson=True)
    assert set(hs.available_days(str(tmp_path))) == {"2026-05-10", "2026-05-11"}
    assert hs.read_day("2026-05-10", str(tmp_path))[0]["soc"] == 0.4


def test_backfill_compacts_past_months_only(tmp_path):
    pytest.importorskip("duckdb")
    _write_day(tmp_path, "2026-04-10", [{"kind": "cycle", "ts": "2026-04-10T00:00:00"}])
    _write_day(tmp_path, "2026-05-10", [{"kind": "cycle", "ts": "2026-05-10T00:00:00"}])
    _write_day(tmp_path, "2026-06-05", [{"kind": "cycle", "ts": "2026-06-05T00:00:00"}])

    hs.backfill_cold_months(str(tmp_path), before=date(2026, 6, 1), remove_ndjson=True)

    assert Path(tmp_path, "ess-2026-04.parquet").exists()
    assert Path(tmp_path, "ess-2026-05.parquet").exists()
    assert not Path(tmp_path, "ess-2026-06.parquet").exists()          # current month untouched
    assert Path(tmp_path, "ess-2026-06-05.ndjson").exists()            # stays hot
    assert not list(Path(tmp_path).glob("ess-2026-04-*.ndjson"))
    assert set(hs.available_days(str(tmp_path))) == {"2026-04-10", "2026-05-10", "2026-06-05"}


def test_latest_ts_across_formats(tmp_path):
    pytest.importorskip("duckdb")
    _write_day(tmp_path, "2026-05-20", [{"ts": "2026-05-20T08:00:00"}, {"ts": "2026-05-20T09:00:00"}])
    hs.compact_month(2026, 5, str(tmp_path), remove_ndjson=True)
    _write_day(tmp_path, "2026-06-01", [{"ts": "2026-06-01T07:00:00"}])
    assert hs.latest_ts(str(tmp_path)) == "2026-06-01T07:00:00"


def test_store_status_reports_formats(tmp_path):
    pytest.importorskip("duckdb")
    _write_day(tmp_path, "2026-05-20", [{"ts": "2026-05-20T08:00:00"}])
    hs.compact_month(2026, 5, str(tmp_path), remove_ndjson=True)
    _write_day(tmp_path, "2026-06-01", [{"ts": "2026-06-01T07:00:00"}])
    st = hs.store_status(str(tmp_path))
    assert st["parquet_months"] == ["2026-05"]
    assert st["ndjson_days"] == ["2026-06-01"]
    assert st["earliest"] == "2026-05-20" and st["latest"] == "2026-06-01"

"""File-based ESS history store: NDJSON hot, Parquet cold, DuckDB as the read/query engine.

Durable data is only ever one of two shapes, both friendly to network / hostpath
(Gluster) storage and needing no daemon:

  * **Append-only NDJSON** for the hot/current month. A crash mid-write can damage at
    most the trailing line, which :func:`read_day` simply skips — there is no shared
    mutable index to corrupt.
  * **Write-once, immutable Parquet** for cold months. Compaction writes to a temp file,
    verifies the row count, then atomically renames it into place; the file is never
    mutated afterwards.

Because every file is immutable-after-publish and date-named, cross-zone migration is a
plain file copy and "which zone has the newest data" is answered by :func:`latest_ts` /
:func:`store_status` — there is no live database state to reconcile or tear.

DuckDB is used ONLY as an in-process engine to write Parquet during compaction and to
read/query it back. It is never the durable store, so it never sits as a mutable file on
the network FS. If DuckDB is not installed the store runs as pure NDJSON and compaction
is simply unavailable — nothing is lost, and reads of any not-yet-compacted day still work.
"""
import os
import re
import glob
import json
import logging
import tempfile
from datetime import date, datetime

try:
    import duckdb
    _HAVE_DUCKDB = True
except Exception:                       # pragma: no cover - environments without duckdb
    duckdb = None
    _HAVE_DUCKDB = False

DEFAULT_HISTORY_DIR = "data/history"

_DAY_RE = re.compile(r"ess-(\d{4})-(\d{2})-(\d{2})\.ndjson$")
_MONTH_RE = re.compile(r"ess-(\d{4})-(\d{2})\.parquet$")


def duckdb_available() -> bool:
    """True when the Parquet/compaction path is usable."""
    return _HAVE_DUCKDB


def resolve_history_dir(explicit=None) -> str:
    if explicit:
        return explicit
    return os.environ.get("HISTORY_DIR") or DEFAULT_HISTORY_DIR


def _iso(day) -> str:
    if isinstance(day, str):
        return day
    if isinstance(day, (date, datetime)):
        return day.strftime("%Y-%m-%d")
    raise TypeError(f"unsupported day type: {type(day)!r}")


def day_ndjson_path(day, hist_dir=None) -> str:
    return os.path.join(resolve_history_dir(hist_dir), f"ess-{_iso(day)}.ndjson")


def month_parquet_path(year, month, hist_dir=None) -> str:
    return os.path.join(resolve_history_dir(hist_dir),
                        f"ess-{int(year):04d}-{int(month):02d}.parquet")


def _month_parquet_for_day(iso, hist_dir) -> str:
    y, m, _d = iso.split("-")
    return month_parquet_path(int(y), int(m), hist_dir)


# --- writes ----------------------------------------------------------------

def append(day, record: dict, hist_dir=None) -> None:
    """Append one record to the day's NDJSON file (the hot path). Single writer."""
    path = day_ndjson_path(day, hist_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# --- reads -----------------------------------------------------------------

def _parse_ndjson(path) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue    # tolerate a torn/partial trailing line
    except (FileNotFoundError, OSError):
        return []
    return out


def _read_parquet_day(parquet_path, iso) -> list:
    esc = parquet_path.replace("'", "''")
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT line FROM read_parquet('{esc}') WHERE day = ? ORDER BY ts",
            [iso],
        ).fetchall()
    finally:
        con.close()
    out = []
    for (line,) in rows:
        try:
            out.append(json.loads(line))
        except (TypeError, json.JSONDecodeError):
            continue
    return out


def read_day(day, hist_dir=None) -> list:
    """All records for a day, oldest-first.

    The hot NDJSON file wins when present (freshest, still being appended); otherwise
    the day is served from its month's Parquet. Missing day -> ``[]``.
    """
    hist_dir = resolve_history_dir(hist_dir)
    iso = _iso(day)
    ndjson = os.path.join(hist_dir, f"ess-{iso}.ndjson")
    if os.path.exists(ndjson):
        return _parse_ndjson(ndjson)
    parquet = _month_parquet_for_day(iso, hist_dir)
    if _HAVE_DUCKDB and os.path.exists(parquet):
        return _read_parquet_day(parquet, iso)
    return []


def _ndjson_days(hist_dir) -> set:
    days = set()
    for p in glob.glob(os.path.join(hist_dir, "ess-*.ndjson")):
        m = _DAY_RE.search(os.path.basename(p))
        if m:
            days.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return days


def _parquet_months(hist_dir) -> list:
    months = []
    for p in glob.glob(os.path.join(hist_dir, "ess-*.parquet")):
        m = _MONTH_RE.search(os.path.basename(p))
        if m:
            months.append(f"{m.group(1)}-{m.group(2)}")
    return sorted(months)


def available_days(hist_dir=None) -> list:
    """Every day present in the store (NDJSON files + days inside Parquet months), sorted."""
    hist_dir = resolve_history_dir(hist_dir)
    days = _ndjson_days(hist_dir)
    parquets = glob.glob(os.path.join(hist_dir, "ess-*.parquet"))
    if parquets and _HAVE_DUCKDB:
        esc = os.path.join(hist_dir, "ess-*.parquet").replace("'", "''")
        con = duckdb.connect()
        try:
            for (d,) in con.execute(f"SELECT DISTINCT day FROM read_parquet('{esc}')").fetchall():
                if d:
                    days.add(d)
        except Exception as e:          # pragma: no cover
            logging.debug("history_store: parquet day scan failed: %s", e)
        finally:
            con.close()
    return sorted(days)


def latest_ts(hist_dir=None):
    """The newest record timestamp in the store, or None. Used to pick the freshest zone."""
    hist_dir = resolve_history_dir(hist_dir)
    days = available_days(hist_dir)
    if not days:
        return None
    stamps = [r.get("ts") or r.get("slot_start")
              for r in read_day(days[-1], hist_dir)]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def store_status(hist_dir=None) -> dict:
    """Summary of what's stored and in which format — handy for migration/zone checks."""
    hist_dir = resolve_history_dir(hist_dir)
    days = available_days(hist_dir)
    return {
        "dir": hist_dir,
        "duckdb": _HAVE_DUCKDB,
        "ndjson_days": sorted(_ndjson_days(hist_dir)),
        "parquet_months": _parquet_months(hist_dir),
        "days": len(days),
        "earliest": days[0] if days else None,
        "latest": days[-1] if days else None,
        "latest_ts": latest_ts(hist_dir),
    }


# --- compaction (cold-month rollup) ----------------------------------------

def _read_parquet_rows(parquet_path) -> list:
    esc = parquet_path.replace("'", "''")
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT day, ts, kind, line FROM read_parquet('{esc}')"
        ).fetchall()
    finally:
        con.close()


def compact_month(year, month, hist_dir=None, *, remove_ndjson=False):
    """Roll a month's NDJSON day files into a single immutable Parquet, atomically.

    Stores the raw JSON line per record (columns day/ts/kind/line) so read-back is
    byte-faithful — no schema drift or type coercion. Merges any existing Parquet for
    the month first, so re-runs and crash-partial states never drop data. Returns the
    Parquet path, or None when there is nothing to compact. Requires DuckDB.
    """
    hist_dir = resolve_history_dir(hist_dir)
    if not _HAVE_DUCKDB:
        raise RuntimeError("history_store.compact_month requires duckdb")

    prefix = f"ess-{int(year):04d}-{int(month):02d}-"
    src_files = sorted(
        p for p in glob.glob(os.path.join(hist_dir, f"{prefix}*.ndjson"))
        if _DAY_RE.search(os.path.basename(p))
    )
    parquet_path = month_parquet_path(year, month, hist_dir)
    if not src_files and not os.path.exists(parquet_path):
        return None

    # (day, ts, line) key -> row, so identical records dedupe and re-runs are idempotent.
    rows = {}
    if os.path.exists(parquet_path):
        for r in _read_parquet_rows(parquet_path):
            rows[(r[0], r[1], r[3])] = tuple(r)

    for path in src_files:
        m = _DAY_RE.search(os.path.basename(path))
        iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    s = raw.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                    except json.JSONDecodeError:
                        continue    # skip a torn line rather than abort the whole month
                    ts = str(rec.get("ts") or rec.get("slot_start") or "")
                    kind = str(rec.get("kind") or "cycle")
                    rows[(iso, ts, s)] = (iso, ts, kind, s)
        except OSError as e:
            logging.warning("history_store: cannot read %s: %s", path, e)
            return None

    row_list = sorted(rows.values(), key=lambda r: (r[0], r[1]))

    fd, tmp_path = tempfile.mkstemp(prefix=".compact-", suffix=".parquet", dir=hist_dir)
    os.close(fd)
    os.remove(tmp_path)     # let DuckDB create the file fresh
    try:
        con = duckdb.connect()
        try:
            con.execute("CREATE TABLE t(day VARCHAR, ts VARCHAR, kind VARCHAR, line VARCHAR)")
            if row_list:
                con.executemany("INSERT INTO t VALUES (?,?,?,?)", row_list)
            esc = tmp_path.replace("'", "''")
            con.execute(
                f"COPY (SELECT * FROM t ORDER BY day, ts) TO '{esc}' "
                "(FORMAT PARQUET, COMPRESSION 'zstd')"
            )
            written = con.execute(f"SELECT count(*) FROM read_parquet('{esc}')").fetchone()[0]
        finally:
            con.close()
        if written != len(row_list):
            raise RuntimeError(
                f"compaction row mismatch for {year}-{month}: wrote {written}, expected {len(row_list)}")
        os.replace(tmp_path, parquet_path)      # atomic publish
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    if remove_ndjson:
        for path in src_files:
            try:
                os.remove(path)
            except OSError as e:                # pragma: no cover
                logging.warning("history_store: could not remove %s after compaction: %s", path, e)
    return parquet_path


def backfill_cold_months(hist_dir=None, *, remove_ndjson=True, before=None) -> list:
    """Compact every complete past month (strictly before ``before``'s month).

    ``before`` defaults to the first of the current month, so the current month stays as
    hot NDJSON. Returns the list of Parquet paths written.
    """
    hist_dir = resolve_history_dir(hist_dir)
    if not _HAVE_DUCKDB:
        raise RuntimeError("history_store.backfill_cold_months requires duckdb")
    if before is None:
        before = date.today().replace(day=1)
    cutoff = (before.year, before.month)

    months = set()
    for iso in _ndjson_days(hist_dir):
        y, m, _d = iso.split("-")
        ym = (int(y), int(m))
        if ym < cutoff:
            months.add(ym)

    done = []
    for (y, mo) in sorted(months):
        out = compact_month(y, mo, hist_dir, remove_ndjson=remove_ndjson)
        if out:
            done.append(out)
    return done

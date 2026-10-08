"""Battery backtester: Phase 1, the data foundation.

One file, standard library plus pandas and numpy. Run from the terminal:

    python bess.py init            # create folders and database
    python bess.py fetch           # download missing months into raw/ (slowly)
    python bess.py ingest raw/     # load new or changed raw files into power.db
    python bess.py validate        # run checks, write findings to issues
    python bess.py health          # write reports/health.html
    python bess.py firstlook       # write reports/firstlook.csv and a chart
    python bess.py export --market DAM --from 2025-04-01 --to 2026-03-31   # CSV
    python bess.py selftest        # optimiser, look-ahead and information checks
    python bess.py baselines       # every baseline in docs/baselines.md -> reports/baselines/
    python bess.py baseline-report --from 2025-10-01   # same reports for a date window

Not built yet: a refresh routine (add new months with fetch, ingest, validate).

Raw data. `fetch` saves each month's IEX market-snapshot page exactly as
received (raw/<market>/<market>_YYYY-MM.html) and appends one line per request
to raw/manifest.csv: url, fetched_at, HTTP status, bytes, sha256. The page
embeds the month as JSON records (date, period, purchase_bid, sell_bid, mcv,
final_scheduled_volume, mcp, congestion) plus 4 summary rows per day. Prices
are stored as published; nothing is capped or clipped.

Refresh routine (to be written in task 10).

All times are Indian Standard Time (UTC+05:30), stored as naive ISO strings
"YYYY-MM-DD HH:MM:SS". India has no daylight saving, so every delivery day has
exactly 96 blocks; block 1 is 00:00-00:15 and block 96 is 23:45-24:00.
"""

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import ssl
import sys
import time as clock
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

# ---------------------------------------------------------------- settings

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "power.db"
RAW_DIR = ROOT / "raw"
REPORTS_DIR = ROOT / "reports"
MARKETS = ("DAM", "RTM")
RAW_SUBDIRS = {"DAM": RAW_DIR / "dam", "RTM": RAW_DIR / "rtm"}

HISTORY_START = date(2022, 4, 1)  # first delivery day IEX publishes in 15-min blocks
BLOCKS_PER_DAY = 96

SNAPSHOT_URLS = {
    "DAM": "https://www.iexindia.com/market-data/day-ahead-market/market-snapshot",
    "RTM": "https://www.iexindia.com/market-data/real-time-market/market-snapshot",
}
FETCH_DELAY_SECONDS = 10  # pause between requests; keep it slow and polite
USER_AGENT = "bess-backtester/0.1 (research; one request per 10 s)"
MANIFEST = RAW_DIR / "manifest.csv"
try:  # python.org builds on macOS ship without CA certificates
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()
RECORD_RE = re.compile(r'\{"date":"\d\d-\d\d-\d{4}"[^{}]*\}')

# Price cap in force from each delivery date (Rs/MWh), DAM and RTM. Used only to
# flag prices; stored prices are always as published, never clipped or changed.
# - Rs 20/kWh until CERC's order of 1 Apr 2022 (immediate effect); the last price
#   above Rs 12,000 is on delivery day 2 Apr 2022 in both markets.
# - Rs 10/kWh from 4 Apr 2023 (CERC order 04/SM/2023); the last price above
#   Rs 10,000 is on 29 Mar 2023, so the exact switch-over day changes no flag.
PRICE_CAPS = [
    (date(2022, 4, 1), 20000.0),
    (date(2022, 4, 3), 12000.0),
    (date(2023, 4, 4), 10000.0),
]

# Validation findings with a known cause: (market, delivery_date, check) -> why.
# validate copies the reason into issues.explanation; anything else is unexplained.
KNOWN_ISSUES = {
    ("RTM", "2024-07-17", "blocks_per_day"):
        "IEX publishes only blocks 1-94 for this day; its own daily summary rows cover the same 94",
}

# Whole checks accepted as IEX publishing quirks (reviewed 2026-10-01). Findings
# are still listed every run; the stored values are kept exactly as published.
ACCEPTED_CHECKS = {
    "no_trade_block": "IEX quirk: block published as all zeros (nothing traded); "
                      "IEX's daily Avg excludes it. Treat as no market, never as a Rs 0 price",
    "bids_missing": "IEX quirk: volume cleared but bids published as 0; kept as published",
    "mcv_above_bids": "IEX quirk: cleared volume slightly above a published bid; kept as published",
}

# Point-in-time rules (conservative; tighten once publication times are checked).
DAM_KNOWN_AT = time(15, 0)  # DAM prices for day D are public by 15:00 on D-1

# Published reference values used as validation checks.
# The IEX snapshot "Avg" of MCP is the simple mean of the 96 block prices
# (confirmed 2026-10-01: simple mean 7448.55, volume-weighted 4922.51).
REFERENCE_DAY_AVERAGES = [
    ("DAM", date(2026, 9, 29), 7448.55),
]


def price_cap(d):
    cap = None
    for start, value in PRICE_CAPS:
        if d >= start:
            cap = value
    return cap


def block_start(d, block):
    return datetime.combine(d, time()) + timedelta(minutes=15 * (block - 1))


def known_at(market, d, block):
    if market == "DAM":
        return datetime.combine(d - timedelta(days=1), DAM_KNOWN_AT)
    return block_start(d, block)  # RTM: the start of the delivery block


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id          INTEGER PRIMARY KEY,
    path             TEXT NOT NULL,
    source           TEXT NOT NULL,          -- page the file was exported from
    market           TEXT NOT NULL CHECK (market IN ('DAM', 'RTM')),
    first_date       TEXT,                   -- first delivery date in the file
    last_date        TEXT,                   -- last delivery date in the file
    downloaded_at    TEXT NOT NULL,          -- fetched_at from raw/manifest.csv, else file mtime
    checksum         TEXT NOT NULL UNIQUE,   -- sha256; the same bytes are never loaded twice
    loaded_at        TEXT NOT NULL
);

-- One row per market, delivery day, block and file. A re-downloaded file with
-- different values adds new rows; blocks_latest picks the newest version.
CREATE TABLE IF NOT EXISTS blocks (
    market                     TEXT NOT NULL CHECK (market IN ('DAM', 'RTM')),
    delivery_date              TEXT NOT NULL,
    block                      INTEGER NOT NULL CHECK (block BETWEEN 1 AND 96),
    block_start                TEXT NOT NULL,
    purchase_bid_mw            REAL,
    sell_bid_mw                REAL,
    mcv_mw                     REAL,
    final_scheduled_volume_mw  REAL,
    mcp_rs_mwh                 REAL,
    congestion                 TEXT,         -- 'YES'/'NO' as published
    session_id                 INTEGER,      -- RTM only, 1-48 (two blocks per session)
    file_id                    INTEGER NOT NULL REFERENCES files(file_id),
    known_at                   TEXT NOT NULL,
    loaded_at                  TEXT NOT NULL,
    PRIMARY KEY (market, delivery_date, block, file_id)
);

CREATE INDEX IF NOT EXISTS blocks_by_known_at ON blocks (market, known_at);

CREATE TABLE IF NOT EXISTS issues (
    issue_id       INTEGER PRIMARY KEY,
    found_at       TEXT NOT NULL,
    market         TEXT,
    delivery_date  TEXT,
    block          INTEGER,
    check_name     TEXT NOT NULL,
    detail         TEXT NOT NULL,
    explanation    TEXT              -- known cause, from KNOWN_ISSUES; NULL = unexplained
);

-- Latest version of each block: the row from the most recently downloaded file.
CREATE VIEW IF NOT EXISTS blocks_latest AS
SELECT b.*
FROM blocks b
JOIN files f USING (file_id)
WHERE NOT EXISTS (
    SELECT 1
    FROM blocks b2
    JOIN files f2 ON f2.file_id = b2.file_id
    WHERE b2.market = b.market
      AND b2.delivery_date = b.delivery_date
      AND b2.block = b.block
      AND (f2.downloaded_at, f2.file_id) > (f.downloaded_at, f.file_id)
);

-- One backtest run: a strategy with fixed battery and cost settings. Re-running
-- the same run_id replaces it.
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,     -- e.g. DA-B4_c1_w300_solar
    strategy       TEXT NOT NULL,        -- e.g. DA-B4
    market         TEXT NOT NULL,        -- DAM, RTM or BOTH
    settings       TEXT NOT NULL,        -- JSON: battery, costs, scenario
    first_date     TEXT NOT NULL,
    last_date      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    code_version   TEXT NOT NULL         -- git commit, "+dirty" if uncommitted changes
);

-- One row per run and delivery day. Energy in MWh at the grid connection, money in Rs.
CREATE TABLE IF NOT EXISTS results (
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    delivery_date  TEXT NOT NULL,
    bought_mwh     REAL NOT NULL,
    sold_mwh       REAL NOT NULL,
    buy_cost_rs    REAL NOT NULL,        -- energy bought x clearing price
    sell_revenue_rs REAL NOT NULL,       -- energy sold x clearing price
    fees_rs        REAL NOT NULL,        -- exchange fees, both sides
    wear_rs        REAL NOT NULL,
    profit_rs      REAL NOT NULL,        -- revenue - cost - fees - wear
    opt_gap_rs     REAL NOT NULL,        -- proven bound on optimiser shortfall vs the best plan on the
                                         -- prices planned on (for ceilings: exact optimum <= profit + gap)
    cycles         REAL NOT NULL,        -- stored energy discharged / usable energy
    unfilled_mwh   REAL NOT NULL,        -- planned in no-trade or missing blocks
    cut_mwh        REAL NOT NULL,        -- cut by the participation limit
    stranded_mwh   REAL NOT NULL,        -- stored energy left above the floor at day end (valued at 0)
    PRIMARY KEY (run_id, delivery_date)
);
"""


def connect(path=DB_PATH):
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    return con


# ---------------------------------------------------------------- fetch

def month_starts(first, last):
    d = date(first.year, first.month, 1)
    while d <= last:
        yield d
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)


def month_end(d):
    nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def raw_path(market, month):
    return RAW_SUBDIRS[market] / f"{market.lower()}_{month:%Y-%m}.html"


def snapshot_url(market, first, last):
    return (f"{SNAPSHOT_URLS[market]}?interval=ONE_FOURTH_HOUR&dp=SELECT_RANGE"
            f"&showGraph=false&fromDate={first:%d-%m-%Y}&toDate={last:%d-%m-%Y}")


def page_records(text):
    """The JSON records embedded in a saved snapshot page, summary rows included."""
    return [json.loads(r) for r in RECORD_RE.findall(text.replace('\\"', '"'))]


def log_fetch(row):
    new = not MANIFEST.exists()
    with MANIFEST.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["path", "url", "fetched_at", "status", "bytes", "sha256"])
        w.writerow(row)


def cmd_fetch(args):
    """Download every complete month not already in raw/. Stops on the first problem."""
    last_complete = date.today().replace(day=1) - timedelta(days=1)
    first = date.fromisoformat(args.start) if args.start else HISTORY_START
    last = min(date.fromisoformat(args.end), last_complete) if args.end else last_complete
    todo = [(m, s) for s in month_starts(first, last) for m in args.markets
            if not raw_path(m, s).exists()]
    print(f"{len(todo)} month files to fetch, about {len(todo) * FETCH_DELAY_SECONDS // 60 + 1} min")
    for i, (market, start) in enumerate(todo):
        if i:
            clock.sleep(FETCH_DELAY_SECONDS)
        end = month_end(start)
        url = snapshot_url(market, start, end)
        path = raw_path(market, start)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
                status, body = resp.status, resp.read()
        except Exception as e:  # network error, 403, 429...: stop rather than hammer
            log_fetch([str(path.relative_to(ROOT)), url, datetime.now().isoformat(timespec="seconds"),
                       getattr(e, "code", "error"), 0, ""])
            sys.exit(f"stopped at {market} {start:%Y-%m}: {e}")
        expected = (end - start).days + 1
        days = {r["date"] for r in page_records(body.decode("utf-8", "replace"))}
        if status != 200 or len(days) != expected:
            log_fetch([str(path.relative_to(ROOT)), url, datetime.now().isoformat(timespec="seconds"),
                       status, len(body), ""])
            sys.exit(f"stopped at {market} {start:%Y-%m}: status {status}, "
                     f"{len(days)} of {expected} days in page; not saved")
        path.write_bytes(body)
        log_fetch([str(path.relative_to(ROOT)), url, datetime.now().isoformat(timespec="seconds"),
                   status, len(body), hashlib.sha256(body).hexdigest()])
        print(f"  {path.relative_to(ROOT)}  {len(days)} days  {len(body) // 1024} KB")


# ---------------------------------------------------------------- ingest

# File contract (docs/file_contracts.md). A file that breaks it stops ingest.
BLOCK_KEYS = {"date", "period", "purchase_bid", "sell_bid", "mcv",
              "final_scheduled_volume", "mcp", "congestion"}
SUMMARY_PERIODS = {"Total (MWh)", "Max (MW)", "Min (MW)", "Avg (MW)"}
# DAM writes "00:00 - 00:15", RTM "00:00-00:15"; RTM 2026-09-09 has 12 blocks in
# the DAM style, so either is accepted.
PERIOD_RE = re.compile(r"^(\d\d):(\d\d) ?- ?(\d\d):(\d\d)$")
NUMBER_FIELDS = [("purchase_bid", "purchase_bid_mw"), ("sell_bid", "sell_bid_mw"),
                 ("mcv", "mcv_mw"), ("final_scheduled_volume", "final_scheduled_volume_mw"),
                 ("mcp", "mcp_rs_mwh")]
VALUE_COLUMNS = [col for _, col in NUMBER_FIELDS] + ["congestion"]


class ContractError(Exception):
    pass


def number(text):
    text = (text or "").strip().replace(",", "")
    return float(text) if text not in ("", "-") else None


def parse_snapshot(market, text):
    """Turn one saved snapshot page into block rows (dicts). Summary rows are dropped."""
    rows, seen = [], set()
    for rec in page_records(text):
        if rec.get("period") in SUMMARY_PERIODS:
            continue
        if set(rec) != BLOCK_KEYS:
            raise ContractError(f"unexpected keys {sorted(set(rec) ^ BLOCK_KEYS)} in {rec}")
        m = PERIOD_RE.match(rec["period"])
        if not m:
            raise ContractError(f"unexpected period {rec['period']!r} for {market}")
        h1, m1, h2, m2 = map(int, m.groups())
        if m1 % 15 or (h2 * 60 + m2) - (h1 * 60 + m1) != 15:
            raise ContractError(f"period is not a 15-minute block: {rec['period']!r}")
        d = datetime.strptime(rec["date"], "%d-%m-%Y").date()
        block = (h1 * 60 + m1) // 15 + 1
        if (d, block) in seen:
            raise ContractError(f"duplicate block {d} {block}")
        seen.add((d, block))
        row = {"market": market, "delivery_date": d.isoformat(), "block": block,
               "block_start": block_start(d, block).isoformat(sep=" "),
               "congestion": rec["congestion"],
               "session_id": (block + 1) // 2 if market == "RTM" else None,
               "known_at": known_at(market, d, block).isoformat(sep=" ")}
        for key, col in NUMBER_FIELDS:
            row[col] = number(rec[key])
        rows.append(row)
    if not rows:
        raise ContractError("no block records found")
    return rows


def manifest_entries():
    if not MANIFEST.exists():
        return {}
    with MANIFEST.open(newline="") as f:
        return {r["sha256"]: r for r in csv.DictReader(f) if r["sha256"]}


def ingest_file(con, path, market, manifest, now):
    """Load one raw file. Returns the number of rows loaded (0 if already loaded)."""
    data = path.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    if con.execute("SELECT 1 FROM files WHERE checksum = ?", (checksum,)).fetchone():
        return 0
    try:
        rows = parse_snapshot(market, data.decode("utf-8"))
    except ContractError as e:
        raise ContractError(f"{path.relative_to(ROOT)}: {e}") from None
    entry = manifest.get(checksum, {})
    downloaded_at = entry.get("fetched_at") or datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    dates = [r["delivery_date"] for r in rows]
    file_id = con.execute(
        "INSERT INTO files (path, source, market, first_date, last_date, downloaded_at, checksum, loaded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(path.relative_to(ROOT)), entry.get("url") or SNAPSHOT_URLS[market], market,
         min(dates), max(dates), downloaded_at.replace("T", " "), checksum, now)).lastrowid
    cols = ["market", "delivery_date", "block", "block_start", *VALUE_COLUMNS,
            "session_id", "known_at"]
    con.executemany(
        f"INSERT INTO blocks ({', '.join(cols)}, file_id, loaded_at)"
        f" VALUES ({', '.join('?' * len(cols))}, ?, ?)",
        [[r[c] for c in cols] + [file_id, now] for r in rows])
    # Revisions: the same block already loaded from another file with different values.
    differs = " OR ".join(f"a.{c} IS NOT b.{c}" for c in VALUE_COLUMNS)
    con.execute(
        f"""INSERT INTO issues (found_at, market, delivery_date, block, check_name, detail)
            SELECT ?, a.market, a.delivery_date, a.block, 'revision',
                   'file ' || b.file_id || ' differs from new file ' || a.file_id
            FROM blocks a JOIN blocks b
              ON a.market = b.market AND a.delivery_date = b.delivery_date
             AND a.block = b.block AND b.file_id <> a.file_id
            WHERE a.file_id = ? AND ({differs})""", (now, file_id))
    return len(rows)


def cmd_ingest(args):
    folder = Path(args.folder).resolve()
    manifest = manifest_entries()
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    loaded = skipped = 0
    with connect() as con:
        con.executescript(SCHEMA)
        for market, sub in RAW_SUBDIRS.items():
            for path in sorted(sub.glob("*.html")):
                if folder not in (path, *path.parents):
                    continue
                n = ingest_file(con, path, market, manifest, now)
                con.commit()
                if n:
                    loaded += 1
                    print(f"  {path.relative_to(ROOT)}  {n} rows")
                else:
                    skipped += 1
        totals = con.execute(
            "SELECT market, COUNT(*), COUNT(DISTINCT delivery_date), MIN(delivery_date), MAX(delivery_date)"
            " FROM blocks_latest GROUP BY market").fetchall()
        revisions = con.execute(
            "SELECT COUNT(*) FROM issues WHERE check_name = 'revision' AND found_at = ?", (now,)).fetchone()[0]
    print(f"{loaded} files loaded, {skipped} already loaded, {revisions} revised blocks")
    for market, n, days, first, last in totals:
        print(f"  {market}: {n} blocks, {days} days, {first} to {last}")


# ---------------------------------------------------------------- validation
#
# Validation only reads blocks and writes findings to issues; it never changes
# a stored value. Each run replaces the previous run's findings (revision
# findings from ingest are kept).

def daterange(first, last):
    for i in range((last - first).days + 1):
        yield first + timedelta(days=i)


def published_summaries(con, market):
    """IEX's own daily Avg MCP and Total MCV, read from the raw files behind blocks_latest."""
    out = {}
    paths = con.execute(
        "SELECT DISTINCT f.path FROM files f JOIN blocks_latest b USING (file_id) WHERE b.market = ?",
        (market,)).fetchall()
    for (path,) in paths:
        for rec in page_records((ROOT / path).read_text("utf-8")):
            if rec["period"] in ("Avg (MW)", "Total (MWh)"):
                d = datetime.strptime(rec["date"], "%d-%m-%Y").date().isoformat()
                key = "avg_mcp" if rec["period"] == "Avg (MW)" else "total_mcv_mwh"
                value = rec["mcp"] if key == "avg_mcp" else rec["mcv"]
                out.setdefault(d, {})[key] = number(value)
    return out


def run_checks(con, market, first, last):
    """Yield (delivery_date, block, check_name, detail) for one market and date range."""
    q = lambda sql, *p: con.execute(sql, (market, first.isoformat(), last.isoformat(), *p)).fetchall()
    where = "market = ? AND delivery_date BETWEEN ? AND ?"

    counts = dict(q(f"SELECT delivery_date, COUNT(*) FROM blocks_latest WHERE {where} GROUP BY 1"))
    for d in daterange(first, last):
        n = counts.get(d.isoformat(), 0)
        if n == 0:
            yield d.isoformat(), None, "missing_day", "no blocks for this day"
        elif n != BLOCKS_PER_DAY:
            have = {b for (b,) in con.execute(
                "SELECT block FROM blocks_latest WHERE market = ? AND delivery_date = ?", (market, d.isoformat()))}
            missing = [b for b in range(1, BLOCKS_PER_DAY + 1) if b not in have]
            yield d.isoformat(), None, "blocks_per_day", f"{n} blocks; missing {missing}"

    for col in VALUE_COLUMNS:
        for d, b in q(f"SELECT delivery_date, block FROM blocks_latest WHERE {where} AND {col} IS NULL"):
            yield d, b, "missing_value", f"{col} is empty"

    for d, b, p in q(f"SELECT delivery_date, block, mcp_rs_mwh FROM blocks_latest WHERE {where}"):
        cap = price_cap(date.fromisoformat(d))
        if p is not None and (p < 0 or p > cap):
            yield d, b, "price_range", f"MCP {p:.2f} outside 0 to {cap:.0f} (cap in force that day)"

    # A block published with zero bids, zero volume and zero price: nothing traded.
    # It is not a Rs 0 price, and IEX leaves it out of its daily average.
    no_trade = "purchase_bid_mw = 0 AND sell_bid_mw = 0 AND mcv_mw = 0 AND mcp_rs_mwh = 0"
    for d, b in q(f"SELECT delivery_date, block FROM blocks_latest WHERE {where} AND {no_trade}"):
        yield d, b, "no_trade_block", "bids, volume and price all published as 0"

    for d, b, buy, sell, mcv in q(
            f"SELECT delivery_date, block, purchase_bid_mw, sell_bid_mw, mcv_mw FROM blocks_latest"
            f" WHERE {where} AND (mcv_mw > purchase_bid_mw + 0.005 OR mcv_mw > sell_bid_mw + 0.005)"):
        if buy == 0 and sell == 0:
            yield d, b, "bids_missing", f"MCV {mcv:.2f} MW cleared but both bids published as 0"
        else:
            yield d, b, "mcv_above_bids", f"MCV {mcv:.2f} MW > purchase bid {buy:.2f} or sell bid {sell:.2f}"

    # IEX's daily Avg is the mean over traded blocks, so no-trade blocks are left out.
    published = published_summaries(con, market)
    ours = q(f"SELECT delivery_date, AVG(CASE WHEN NOT ({no_trade}) THEN mcp_rs_mwh END),"
             f" SUM(mcv_mw) * 0.25 FROM blocks_latest WHERE {where} GROUP BY 1")
    for d, avg_mcp, total_mcv in ours:
        pub = published.get(d, {})
        if pub.get("avg_mcp") is not None and abs(avg_mcp - pub["avg_mcp"]) > 0.011:
            yield d, None, "summary_mismatch", f"mean MCP {avg_mcp:.2f} vs IEX daily Avg {pub['avg_mcp']:.2f}"
        if pub.get("total_mcv_mwh") is not None and abs(total_mcv - pub["total_mcv_mwh"]) > 0.5:
            yield d, None, "summary_mismatch", f"MCV total {total_mcv:.2f} MWh vs IEX Total {pub['total_mcv_mwh']:.2f}"

    for ref_market, d, expected in REFERENCE_DAY_AVERAGES:
        if ref_market == market and first <= d <= last:
            (got,) = con.execute("SELECT AVG(mcp_rs_mwh) FROM blocks_latest WHERE market = ? AND delivery_date = ?",
                                 (market, d.isoformat())).fetchone()
            if got is None or abs(got - expected) > 0.005:
                yield d.isoformat(), None, "reference_day", f"mean MCP {got} vs published {expected:.2f}"


def cmd_validate(args):
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with connect() as con:
        con.executescript(SCHEMA)
        (db_last,) = con.execute("SELECT MAX(delivery_date) FROM blocks").fetchone()
        if db_last is None:
            sys.exit("no data; run ingest first")
        first = date.fromisoformat(args.start) if args.start else HISTORY_START
        last = date.fromisoformat(args.end) if args.end else date.fromisoformat(db_last)
        con.execute("DELETE FROM issues WHERE check_name <> 'revision' AND delivery_date BETWEEN ? AND ?",
                    (first.isoformat(), last.isoformat()))
        rows = []
        for market in MARKETS:
            for d, b, check, detail in run_checks(con, market, first, last):
                rows.append((now, market, d, b, check, detail, KNOWN_ISSUES.get((market, d, check)) or ACCEPTED_CHECKS.get(check)))
        con.executemany(
            "INSERT INTO issues (found_at, market, delivery_date, block, check_name, detail, explanation)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        summary = con.execute(
            "SELECT check_name, market, COUNT(*), COUNT(DISTINCT delivery_date), SUM(explanation IS NULL)"
            " FROM issues WHERE delivery_date BETWEEN ? AND ? GROUP BY 1, 2 ORDER BY 1, 2",
            (first.isoformat(), last.isoformat())).fetchall()
    print(f"validated {first} to {last}")
    if not summary:
        print("  no issues")
    for check, market, n, days, unexplained in summary:
        print(f"  {check:16} {market}  {n:5} findings on {days:4} days, {unexplained} unexplained")
    print("details: SELECT * FROM issues")


# ---------------------------------------------------------------- health report

NO_TRADE_SQL = "purchase_bid_mw = 0 AND sell_bid_mw = 0 AND mcv_mw = 0 AND mcp_rs_mwh = 0"

HEALTH_CSS = """
:root { --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a64; --line:#e4e1d8; --card:#ffffff;
  --ok:#4f9a6a; --note:#d9a441; --bad:#c8553d; --none:#ebe8e0; --pass:#2f7a4a; --fail:#b23a26; }
@media (prefers-color-scheme: dark) { :root { --bg:#161614; --fg:#ecebe6; --muted:#9a988f;
  --line:#2e2d29; --card:#1f1e1b; --ok:#5fae7c; --note:#e0b25a; --bad:#e06a52; --none:#2a2925;
  --pass:#6cc48d; --fail:#f08068; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:32px 16px 64px; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:18px; margin:36px 0 10px; }
.sub { color:var(--muted); margin:0 0 20px; }
table { border-collapse:collapse; width:100%; background:var(--card); font-variant-numeric:tabular-nums; }
th, td { padding:6px 10px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }
th:first-child, td:first-child { text-align:left; } th { color:var(--muted); font-weight:600; }
.wrap { overflow-x:auto; }
.pass { color:var(--pass); font-weight:600; } .fail { color:var(--fail); font-weight:600; }
.cal { display:grid; grid-template-columns:64px repeat(31, 1fr); gap:2px; font-size:11px;
  color:var(--muted); min-width:560px; }
.cal span { height:13px; border-radius:2px; }
.c-ok { background:var(--ok); } .c-note { background:var(--note); }
.c-bad { background:var(--bad); } .c-none { background:var(--none); }
.legend { display:flex; gap:16px; flex-wrap:wrap; color:var(--muted); font-size:13px; margin:8px 0 0; }
.legend i { display:inline-block; width:11px; height:11px; border-radius:2px; margin-right:5px; vertical-align:-1px; }
.cols { display:grid; grid-template-columns:repeat(auto-fit, minmax(480px, 1fr)); gap:24px; }
@media (max-width:560px) { .cols { grid-template-columns:1fr; } }
.note { color:var(--muted); font-size:13px; margin-top:6px; }
"""


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def html_table(headers, rows):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><tr>{head}</tr>{body}</table></div>'


def coverage_calendar(con, market, first, last):
    counts = dict(con.execute(
        "SELECT delivery_date, COUNT(*) FROM blocks_latest WHERE market = ? GROUP BY 1", (market,)))
    flagged = {d for (d,) in con.execute(
        "SELECT DISTINCT delivery_date FROM issues WHERE market = ?", (market,))}
    cells = []
    for month in month_starts(first, last):
        cells.append(f"<div>{month:%b %Y}</div>")
        for day in range(1, 32):
            try:
                d = month.replace(day=day)
            except ValueError:
                cells.append("<span></span>")
                continue
            if d < first or d > last:
                cells.append("<span></span>")
                continue
            n = counts.get(d.isoformat(), 0)
            if n == 0:
                cls, tip = "c-bad", "missing"
            elif n != BLOCKS_PER_DAY:
                cls, tip = "c-bad", f"{n} of 96 blocks"
            elif d.isoformat() in flagged:
                cls, tip = "c-note", "96 blocks, has findings"
            else:
                cls, tip = "c-ok", "96 blocks"
            cells.append(f'<span class="{cls}" title="{d} · {tip}"></span>')
    return f'<div class="wrap"><div class="cal">{"".join(cells)}</div></div>'


def yearly_prices(con, market):
    import pandas as pd
    df = pd.read_sql_query(
        f"SELECT delivery_date, mcp_rs_mwh AS p, ({NO_TRADE_SQL}) AS no_trade"
        f" FROM blocks_latest WHERE market = ?", con, params=(market,))
    df["year"] = df["delivery_date"].str[:4]
    df["cap"] = [price_cap(date.fromisoformat(d)) for d in df["delivery_date"]]
    df["at_cap"] = df["p"] >= df["cap"]
    rows = []
    for year, g in df.groupby("year"):
        traded = g[g["no_trade"] == 0]
        rows.append([year, f"{len(g):,}", f"{traded.p.min():,.0f}", f"{traded.p.quantile(0.05):,.0f}",
                     f"{traded.p.mean():,.0f}", f"{traded.p.median():,.0f}",
                     f"{traded.p.quantile(0.95):,.0f}", f"{traded.p.max():,.0f}",
                     f"{traded.at_cap.mean():.1%}", f"{int(g.no_trade.sum())}"])
    return rows


def cmd_health(args):
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / "health.html"
    with connect() as con:
        con.executescript(SCHEMA)
        (first_s, last_s) = con.execute("SELECT MIN(delivery_date), MAX(delivery_date) FROM blocks").fetchone()
        if first_s is None:
            sys.exit("no data; run ingest first")
        first, last = HISTORY_START, date.fromisoformat(last_s)
        n_days = (last - first).days + 1
        (n_files,) = con.execute("SELECT COUNT(*) FROM files").fetchone()
        (last_validated,) = con.execute("SELECT MAX(found_at) FROM issues").fetchone()

        # Done criteria that can be read from the database.
        checks = []
        for market in MARKETS:
            (present,) = con.execute(
                "SELECT COUNT(DISTINCT delivery_date) FROM blocks_latest WHERE market = ?"
                " AND delivery_date BETWEEN ? AND ?", (market, first.isoformat(), last.isoformat())).fetchone()
            (short,) = con.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM blocks_latest WHERE market = ? GROUP BY delivery_date"
                " HAVING COUNT(*) <> 96)", (market,)).fetchone()
            ok = present == n_days
            checks.append((ok, f"{market}: {present:,} of {n_days:,} days present"
                               + (f"; {short} day(s) short of 96 blocks, listed in issues" if short else "")))
        (unexplained,) = con.execute("SELECT COUNT(*) FROM issues WHERE explanation IS NULL").fetchone()
        checks.append((unexplained == 0, f"{unexplained} unexplained validation findings"
                       + ("" if last_validated else " (validate has not been run)")))
        for market, d, expected in REFERENCE_DAY_AVERAGES:
            (got,) = con.execute("SELECT AVG(mcp_rs_mwh) FROM blocks_latest WHERE market = ? AND delivery_date = ?",
                                 (market, d.isoformat())).fetchone()
            ok = got is not None and abs(got - expected) <= 0.005
            got_s = "no data" if got is None else f"Rs {got:,.2f}"
            checks.append((ok, f"{market} {d:%d %b %Y} average {got_s} vs published Rs {expected:,.2f}"))

        issue_rows = [[esc(c), m, f"{n:,}", f"{d:,}", f"{n - u:,}",
                       f'<span class="{"pass" if u == 0 else "fail"}">{u:,}</span>']
                      for c, m, n, d, u in con.execute(
                          "SELECT check_name, market, COUNT(*), COUNT(DISTINCT delivery_date),"
                          " SUM(explanation IS NULL) FROM issues GROUP BY 1, 2 ORDER BY 1, 2")]
        reasons = con.execute(
            "SELECT check_name, explanation, COUNT(*) FROM issues WHERE explanation IS NOT NULL"
            " GROUP BY 1, 2 ORDER BY 1").fetchall()
        calendars = {m: coverage_calendar(con, m, first, last) for m in MARKETS}
        prices = {m: yearly_prices(con, m) for m in MARKETS}

    criteria = "".join(
        f'<tr><td><span class="{"pass" if ok else "fail"}">{"PASS" if ok else "CHECK"}</span></td>'
        f"<td style='text-align:left;white-space:normal'>{esc(text)}</td></tr>" for ok, text in checks)
    caps = ", ".join(f"Rs {cap:,.0f} from {d:%d %b %Y}" for d, cap in PRICE_CAPS)
    price_headers = ["Year", "Blocks", "Min", "P5", "Mean", "Median", "P95", "Max", "At cap", "No-trade"]
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IEX Data Health</title><style>{HEALTH_CSS}</style></head><body><main>
<h1>IEX data health</h1>
<p class="sub">Delivery days {first:%d %b %Y} to {last:%d %b %Y} · {n_files} raw files ·
generated {datetime.now():%d %b %Y %H:%M} · last validated {esc(last_validated or "never")}</p>

<h2>Done criteria</h2>
<div class="wrap"><table>{criteria}</table></div>
<p class="note">Re-ingest idempotence and refresh time are checked by running the commands, not from this report.</p>

<h2>Coverage</h2>
<div class="cols">{"".join(f"<div><h3>{m}</h3>{calendars[m]}</div>" for m in MARKETS)}</div>
<div class="legend"><span><i class="c-ok"></i>96 blocks</span><span><i class="c-note"></i>96 blocks, with findings</span>
<span><i class="c-bad"></i>missing or short</span></div>

<h2>Validation findings</h2>
{html_table(["Check", "Market", "Findings", "Days", "Explained", "Unexplained"], issue_rows)
 if issue_rows else "<p>No findings.</p>"}
{html_table(["Check", "Explanation", "Findings"], [[esc(c), f"<span style='white-space:normal'>{esc(e)}</span>", f"{n:,}"] for c, e, n in reasons]) if reasons else ""}

<h2>Prices by year (Rs/MWh)</h2>
{"".join(f"<h3>{m}</h3>" + html_table(price_headers, prices[m]) for m in MARKETS)}
<p class="note">Prices as published, never clipped. Statistics exclude no-trade blocks (all values 0),
as IEX's own daily averages do. "At cap" is the share of traded blocks at the cap in force that day: {caps}.</p>
</main></body></html>"""
    out.write_text(html, "utf-8")
    print(f"wrote {out.relative_to(ROOT)}")
    for ok, text in checks:
        print(f"  {'PASS ' if ok else 'CHECK'} {text}")


# ---------------------------------------------------------------- first look
#
# For every day: the lowest and highest block price, the gap between them, and
# the best 2-hour buy window followed by the best later 2-hour sell window (the
# pair with the largest difference in average price). No battery logic: no
# losses, fees or limits. No-trade blocks and missing blocks are left out, and
# a window that touches one is not used.

WINDOW_BLOCKS = 8  # 2 hours of 15-minute blocks


def block_label(block):
    minutes = 15 * (block - 1)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def best_window_pair(prices):
    """prices: 96 floats (NaN = unusable). Returns (buy_start, buy_avg, sell_start, sell_avg) or None."""
    import numpy as np
    w = np.lib.stride_tricks.sliding_window_view(prices, WINDOW_BLOCKS).mean(axis=1)  # NaN if any NaN
    best = None
    best_buy = None  # index of the cheapest window that ends before the current sell window starts
    for sell in range(WINDOW_BLOCKS, len(w)):
        cand = sell - WINDOW_BLOCKS
        if not np.isnan(w[cand]) and (best_buy is None or w[cand] < w[best_buy]):
            best_buy = cand
        if best_buy is None or np.isnan(w[sell]):
            continue
        if best is None or w[sell] - w[best_buy] > best[3] - best[1]:
            best = (best_buy + 1, float(w[best_buy]), sell + 1, float(w[sell]))
    return best


def first_look_daily(con):
    import numpy as np
    import pandas as pd
    df = pd.read_sql_query(
        f"SELECT market, delivery_date, block, mcp_rs_mwh AS p, ({NO_TRADE_SQL}) AS no_trade"
        f" FROM blocks_latest ORDER BY market, delivery_date, block", con)
    df.loc[df.no_trade == 1, "p"] = np.nan
    rows = []
    for (market, d), g in df.groupby(["market", "delivery_date"], sort=True):
        prices = np.full(BLOCKS_PER_DAY, np.nan)
        prices[g.block.to_numpy() - 1] = g.p.to_numpy()
        traded = prices[~np.isnan(prices)]
        pair = best_window_pair(prices)
        row = {"market": market, "delivery_date": d,
               "blocks_used": int(len(traded)),
               "min_price": traded.min(), "min_block_start": block_label(int(np.nanargmin(prices)) + 1),
               "max_price": traded.max(), "max_block_start": block_label(int(np.nanargmax(prices)) + 1),
               "max_min_gap": traded.max() - traded.min(),
               "buy_window_start": None, "buy_avg": np.nan,
               "sell_window_start": None, "sell_avg": np.nan, "best_2h_spread": np.nan}
        if pair:
            b, buy_avg, s, sell_avg = pair
            row.update(buy_window_start=block_label(b), buy_avg=buy_avg,
                       sell_window_start=block_label(s), sell_avg=sell_avg,
                       best_2h_spread=sell_avg - buy_avg)
        rows.append(row)
    return pd.DataFrame(rows)


def first_look_chart(monthly, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import pandas as pd
    surface, ink, muted, grid = "#fcfcfb", "#1d1d1b", "#6b6a64", "#e4e1d8"
    colors = {"DAM": "#2a78d6", "RTM": "#eb6834"}
    names = {"DAM": "Day-ahead", "RTM": "Real-time"}
    panels = [("best_2h_spread", "Best 2-hour buy → later 2-hour sell spread"),
              ("max_min_gap", "Highest minus lowest block price")]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), sharey=True, facecolor=surface)
    for ax, (col, title) in zip(axes, panels):
        ax.set_facecolor(surface)
        ends = {}
        for market in MARKETS:
            m = monthly[monthly.market == market]
            x = pd.to_datetime(m.month + "-01")
            ax.plot(x, m[col], color=colors[market], lw=2, label=names[market])
            ends[market] = (x.iloc[-1], m[col].iloc[-1])
        # Direct labels at the line ends, pushed apart when the ends are close.
        lo, hi = sorted(MARKETS, key=lambda k: ends[k][1])
        nudge = 7 if abs(ends[hi][1] - ends[lo][1]) < 400 else 0
        for market, dy in ((lo, -nudge), (hi, nudge)):
            ax.annotate(names[market], ends[market], xytext=(6, dy), textcoords="offset points",
                        va="center", fontsize=9, color=ink)
        ax.set_title(title, loc="left", fontsize=11, color=ink, pad=10)
        ax.grid(axis="y", color=grid, lw=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(grid)
        ax.tick_params(colors=muted, labelsize=9, length=0)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"₹{v:,.0f}"))
        ax.set_ylim(0, 10500)
        ax.xaxis.set_major_locator(matplotlib.dates.YearLocator())
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y"))
        ax.margins(x=0.02)
    axes[0].set_ylabel("Monthly average, ₹/MWh", color=muted, fontsize=9)
    axes[0].legend(frameon=False, fontsize=9, loc="upper left", labelcolor=ink)
    fig.suptitle("IEX daily price spreads, monthly averages (prices as published, uncapped)",
                 x=0.01, ha="left", fontsize=12, color=ink, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=surface)
    plt.close(fig)


def cmd_firstlook(args):
    REPORTS_DIR.mkdir(exist_ok=True)
    with connect() as con:
        daily = first_look_daily(con)
    if daily.empty:
        sys.exit("no data; run ingest first")
    daily["month"] = daily.delivery_date.str[:7]
    value_cols = ["min_price", "max_price", "max_min_gap", "buy_avg", "sell_avg", "best_2h_spread"]
    monthly = (daily.groupby(["market", "month"])[value_cols].mean().round(2)
               .join(daily.groupby(["market", "month"]).size().rename("days")).reset_index())
    daily_path, monthly_path, chart_path = (REPORTS_DIR / "firstlook.csv", REPORTS_DIR / "firstlook_monthly.csv",
                                            REPORTS_DIR / "firstlook.png")
    daily.drop(columns="month").round(2).to_csv(daily_path, index=False)
    monthly.to_csv(monthly_path, index=False)
    first_look_chart(monthly, chart_path)
    for p in (daily_path, monthly_path, chart_path):
        print(f"wrote {p.relative_to(ROOT)}")
    for market in MARKETS:
        d = daily[daily.market == market]
        print(f"  {market}: best 2h spread mean Rs {d.best_2h_spread.mean():,.0f}, median "
              f"Rs {d.best_2h_spread.median():,.0f}; max-min gap mean Rs {d.max_min_gap.mean():,.0f}")


# ---------------------------------------------------------------- export

EXPORT_COLUMNS = ["market", "delivery_date", "block", "block_start", "session_id",
                  "purchase_bid_mw", "sell_bid_mw", "mcv_mw", "final_scheduled_volume_mw",
                  "mcp_rs_mwh", "congestion", "known_at", "file_id"]


def cmd_export(args):
    """Write the latest version of each block to CSV, values exactly as stored."""
    first = date.fromisoformat(args.start) if args.start else HISTORY_START
    last = date.fromisoformat(args.end) if args.end else date.today()
    markets = MARKETS if args.market == "ALL" else (args.market,)
    out = Path(args.out) if args.out else (
        REPORTS_DIR / f"export_{args.market}_{first}_{last if args.end else 'latest'}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        cur = con.execute(
            f"SELECT {', '.join(EXPORT_COLUMNS)}, ({NO_TRADE_SQL}) AS no_trade FROM blocks_latest"
            f" WHERE market IN ({', '.join('?' * len(markets))}) AND delivery_date BETWEEN ? AND ?"
            f" ORDER BY market, delivery_date, block",
            (*markets, first.isoformat(), last.isoformat()))
        with out.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([c[0] for c in cur.description])
            n = 0
            for row in cur:
                w.writerow(row)
                n += 1
    if n == 0:
        out.unlink()
        sys.exit(f"no blocks for {args.market} between {first} and {last}; nothing written")
    print(f"wrote {out}  {n:,} blocks")
    print("  no_trade = 1 marks blocks published as all zeros (nothing traded, not a Rs 0 price)")


# ---------------------------------------------------------------- battery, optimiser, settlement
#
# Phase 2 core (spec: docs/baselines.md). Prices are (days x 96) arrays with NaN
# where nothing can be traded. A plan is an integer array of state-of-charge
# changes per block, in SoC steps (1% of energy): positive charges, negative
# discharges. Strategies make plans from the information they may see; settle()
# then applies the plan to the actual prices.

@dataclass(frozen=True)
class Battery:
    power_mw: float = 100.0
    energy_mwh: float = 200.0
    rte: float = 0.87               # round trip; split evenly between charging and discharging
    soc_min: float = 0.10
    soc_max: float = 0.90           # each day starts at soc_min and must end there
    soc_step: float = 0.01          # optimiser resolution, as a share of energy
    fee_rs_mwh: float = 20.0        # exchange fee, each side (2 paise/kWh)
    wear_rs_mwh: float = 0.0        # per MWh discharged
    participation: float = 0.10     # max share of a block's cleared volume
    max_cycles: int = 1             # discharged energy <= max_cycles x usable energy
    charge_blocks: tuple = None     # (first, last) block in which charging is allowed; None = any

    @property
    def eta(self):
        return self.rte ** 0.5

    @property
    def step_mwh(self):
        return self.energy_mwh * self.soc_step

    @property
    def lo(self):
        return round(self.soc_min / self.soc_step)

    @property
    def hi(self):
        return round(self.soc_max / self.soc_step)

    @property
    def usable_steps(self):
        return self.hi - self.lo


def charge_allowed(bat):
    import numpy as np
    mask = np.ones(BLOCKS_PER_DAY, dtype=bool)
    if bat.charge_blocks:
        first, last = bat.charge_blocks
        mask[:] = False
        mask[first - 1:last] = True
    return mask


def step_limits(bat, mcv):
    """Max charge and discharge steps per block from power and the participation limit.
    mcv: (days x 96) cleared volume in MW, NaN = unknown (power limit only)."""
    import numpy as np
    grid_mwh = np.minimum(bat.power_mw, bat.participation * np.nan_to_num(mcv, nan=np.inf)) * 0.25
    ch = np.floor(grid_mwh * bat.eta / bat.step_mwh + 1e-9)
    dis = np.floor(grid_mwh / bat.eta / bat.step_mwh + 1e-9)
    return ch.astype(int), dis.astype(int)


def grid_mwh(bat, steps):
    """Energy at the grid connection for SoC changes (charging draws more, discharging delivers less)."""
    import numpy as np
    steps = np.asarray(steps, dtype=float)
    return np.where(steps > 0, steps * bat.step_mwh / bat.eta, -steps * bat.step_mwh * bat.eta)


def optimise(bat, buy, sell, ch_lim, dis_lim, lam):
    """Dynamic programme over state of charge for each day (row) at once.
    buy/sell: price paths (NaN = that side not tradable); ch_lim/dis_lim: max steps per block;
    lam: per-day penalty per discharged step (used to enforce the cycle limit).
    Returns the plan (days x 96) that maximises profit and ends at soc_min."""
    import numpy as np
    n, T = buy.shape
    S, lo = bat.hi + 1, bat.lo
    NEG = -1e30
    V = np.full((n, S), NEG)
    V[:, lo] = 0.0
    pol = np.zeros((T, n, S), dtype=np.int8)
    can_charge = charge_allowed(bat)
    max_ch, max_dis = int(ch_lim.max(initial=0)), int(dis_lim.max(initial=0))
    for t in range(T - 1, -1, -1):
        best = V.copy()
        arg = np.zeros((n, S), dtype=np.int8)
        bp, sp = buy[:, t], sell[:, t]
        if can_charge[t]:
            ok = ~np.isnan(bp)
            cost = np.where(ok, (bp + bat.fee_rs_mwh) * bat.step_mwh / bat.eta, 0.0)
            for d in range(1, max_ch + 1):
                valid = ok & (ch_lim[:, t] >= d)
                if not valid.any():
                    break
                cand = np.full((n, S), NEG)
                cand[:, lo:S - d] = V[:, lo + d:S] - (d * cost)[:, None]
                cand[~valid] = NEG
                better = cand > best
                best[better] = cand[better]
                arg[better] = d
        ok = ~np.isnan(sp)
        gain = np.where(ok, (sp - bat.fee_rs_mwh - bat.wear_rs_mwh) * bat.step_mwh * bat.eta - lam, 0.0)
        for d in range(1, max_dis + 1):
            valid = ok & (dis_lim[:, t] >= d)
            if not valid.any():
                break
            cand = np.full((n, S), NEG)
            cand[:, lo + d:S] = V[:, lo:S - d] + (d * gain)[:, None]
            cand[~valid] = NEG
            better = cand > best
            best[better] = cand[better]
            arg[better] = -d
        V = best
        pol[t] = arg
    plan = np.zeros((n, T), dtype=np.int16)
    s = np.full(n, lo)
    rows = np.arange(n)
    for t in range(T):
        a = pol[t, rows, s]
        plan[:, t] = a
        s = s + a
    return plan


def discharged_steps(plan):
    return -plan.clip(max=0).sum(axis=1)


def optimise_exact(bat, buy, sell, ch_lim, dis_lim, limit, chunk=40):
    """Exact optimum under the cycle limit: the DP state also counts steps discharged so far.
    Slower and memory-heavy, so plan_days only uses it on days that need it."""
    import numpy as np
    n, T = buy.shape
    S, lo, K = bat.hi + 1, bat.lo, limit + 1
    NEG = -1e30
    can_charge = charge_allowed(bat)
    plans = np.zeros((n, T), dtype=np.int16)
    for start in range(0, n, chunk):
        idx = slice(start, min(n, start + chunk))
        b, s_, cl, dl = buy[idx], sell[idx], ch_lim[idx], dis_lim[idx]
        m = b.shape[0]
        V = np.full((m, S, K), NEG)
        V[:, lo, :] = 0.0
        pol = np.zeros((T, m, S, K), dtype=np.int8)
        for t in range(T - 1, -1, -1):
            best = V.copy()
            arg = np.zeros((m, S, K), dtype=np.int8)
            if can_charge[t]:
                ok = ~np.isnan(b[:, t])
                cost = np.where(ok, (b[:, t] + bat.fee_rs_mwh) * bat.step_mwh / bat.eta, 0.0)
                for d in range(1, int(cl[:, t].max(initial=0)) + 1):
                    rows = np.where(ok & (cl[:, t] >= d))[0]
                    if not len(rows):
                        break
                    cand = V[rows, lo + d:S, :] - (d * cost[rows])[:, None, None]
                    cur = best[rows, lo:S - d, :]
                    better = cand > cur
                    best[rows, lo:S - d, :] = np.where(better, cand, cur)
                    arg[rows, lo:S - d, :] = np.where(better, d, arg[rows, lo:S - d, :])
            ok = ~np.isnan(s_[:, t])
            gain = np.where(ok, (s_[:, t] - bat.fee_rs_mwh - bat.wear_rs_mwh) * bat.step_mwh * bat.eta, 0.0)
            for d in range(1, min(int(dl[:, t].max(initial=0)), limit) + 1):
                rows = np.where(ok & (dl[:, t] >= d))[0]
                if not len(rows):
                    break
                cand = V[rows, lo:S - d, d:] + (d * gain[rows])[:, None, None]
                cur = best[rows, lo + d:S, :K - d]
                better = cand > cur
                best[rows, lo + d:S, :K - d] = np.where(better, cand, cur)
                arg[rows, lo + d:S, :K - d] = np.where(better, -d, arg[rows, lo + d:S, :K - d])
            V = best
            pol[t] = arg
        s, k, rows = np.full(m, lo), np.zeros(m, dtype=int), np.arange(m)
        for t in range(T):
            a = pol[t, rows, s, k]
            plans[idx][:, t] = a
            s, k = s + a, k + np.where(a < 0, -a, 0)
    return plans


def plan_value(bat, plan, buy, sell):
    """Profit of a plan on the prices it was planned on (no limits or fills applied)."""
    import numpy as np
    ch, dis = plan.clip(min=0), (-plan).clip(min=0)
    return ((dis * bat.step_mwh * bat.eta * (np.nan_to_num(sell) - bat.fee_rs_mwh - bat.wear_rs_mwh)).sum(1)
            - (ch * bat.step_mwh / bat.eta * (np.nan_to_num(buy) + bat.fee_rs_mwh)).sum(1))


def plan_days(bat, buy, sell, ch_lim, dis_lim, iterations=18, rel_tolerance=0.01):
    """Plans under the cycle limit for each day (row). Returns (plan, gap): gap is a
    proven bound on how much more the best possible plan could earn (Rs, on the prices
    planned on), so optimum <= plan profit + gap.
    Days whose best plan cycles too much get a penalty per discharged step, found by
    bisection (smallest penalty that fits). Each penalised solve also gives an upper bound
    on the best profit (profit - penalty x (used - limit)). Days still more than
    rel_tolerance below that bound are re-solved exactly (gap 0). Exact solving of every
    day is too slow; in practice the remaining gap is a fraction of a percent."""
    import numpy as np
    n = buy.shape[0]
    limit = bat.max_cycles * bat.usable_steps
    plan = optimise(bat, buy, sell, ch_lim, dis_lim, np.zeros(n))
    gap = np.zeros(n)
    over = np.where(discharged_steps(plan) > limit)[0]
    if len(over):
        sub = lambda a: a[over]
        b, s, cl, dl = sub(buy), sub(sell), sub(ch_lim), sub(dis_lim)
        lo_l, hi_l = np.zeros(len(over)), np.full(len(over), 1e5)
        bound = plan_value(bat, plan[over], b, s)       # the unconstrained optimum is an upper bound
        best = np.zeros((len(over), plan.shape[1]), dtype=plan.dtype)  # idle always fits
        for _ in range(iterations):
            mid = (lo_l + hi_l) / 2
            p = optimise(bat, b, s, cl, dl, mid)
            used = discharged_steps(p)
            bound = np.minimum(bound, plan_value(bat, p, b, s) - mid * (used - limit))
            fits = used <= limit
            best[fits] = p[fits]
            hi_l = np.where(fits, mid, hi_l)
            lo_l = np.where(fits, lo_l, mid)
        g = np.maximum(bound - plan_value(bat, best, b, s), 0.0)
        redo = g > np.maximum(1.0, rel_tolerance * np.abs(bound))
        if redo.any():
            best[redo] = optimise_exact(bat, b[redo], s[redo], cl[redo], dl[redo], limit)
            g[redo] = 0.0
        plan[over] = best
        gap[over] = g
    return plan, gap


SETTLE_FIELDS = ["bought_mwh", "sold_mwh", "buy_cost_rs", "sell_revenue_rs", "fees_rs", "wear_rs",
                 "profit_rs", "opt_gap_rs", "cycles", "unfilled_mwh", "cut_mwh", "stranded_mwh"]


def settle(bat, plan, buy, sell, buy_mcv, sell_mcv):
    """Apply plans to actual prices. Orders in no-trade blocks don't fill; the participation
    limit cuts volume; anything then impossible (selling energy never bought) is clipped
    by the SoC band. Energy left at day end is valued at 0. Returns a dict of per-day arrays."""
    import numpy as np
    n, T = plan.shape
    ch_lim, _ = step_limits(bat, buy_mcv)
    _, dis_lim = step_limits(bat, sell_mcv)
    out = {k: np.zeros(n) for k in SETTLE_FIELDS if k != "opt_gap_rs"}
    steps_out = np.zeros(n)
    s = np.full(n, bat.lo)
    for t in range(T):
        want = plan[:, t].astype(int)
        blocked = ((want > 0) & np.isnan(buy[:, t])) | ((want < 0) & np.isnan(sell[:, t]))
        out["unfilled_mwh"] += np.where(blocked, grid_mwh(bat, want), 0.0)
        a = np.where(blocked, 0, want)
        capped = np.clip(a, -dis_lim[:, t], ch_lim[:, t])
        out["cut_mwh"] += grid_mwh(bat, a) - grid_mwh(bat, capped)
        a = np.clip(capped, bat.lo - s, bat.hi - s)
        cg = np.where(a > 0, grid_mwh(bat, a), 0.0)
        dg = np.where(a < 0, grid_mwh(bat, a), 0.0)
        out["bought_mwh"] += cg
        out["sold_mwh"] += dg
        out["buy_cost_rs"] += cg * np.nan_to_num(buy[:, t])
        out["sell_revenue_rs"] += dg * np.nan_to_num(sell[:, t])
        out["fees_rs"] += (cg + dg) * bat.fee_rs_mwh
        out["wear_rs"] += dg * bat.wear_rs_mwh
        steps_out += np.where(a < 0, -a, 0)
        s = s + a
    out["stranded_mwh"] = (s - bat.lo) * bat.step_mwh
    out["profit_rs"] = out["sell_revenue_rs"] - out["buy_cost_rs"] - out["fees_rs"] - out["wear_rs"]
    out["cycles"] = steps_out / bat.usable_steps
    return out


# ---------------------------------------------------------------- baselines
#
# Information rules (docs/baselines.md). Day-ahead plans at 12:00 on D-1 and sees
# DAM prices through D-1. Real-time plans once at 22:00 on D-1 and sees RTM blocks
# 1-88 of D-1 (block 89 starts at 22:00, so it is not known yet). selftest checks
# both against known_at in the database.

EVAL_START = date(2022, 5, 1)       # first evaluated day; April 2022 is warm-up history
RTM_KNOWN_BLOCKS = 88               # RTM blocks of D-1 known at the 22:00 decision
BASELINE_STRATEGIES = ["B0", "B1", "B2", "B3", "B4", "PH"]
FIXED_WINDOWS = (45, 73)            # B0: charge from block 45 (11:00), discharge from block 73 (18:00)
B1_LOOKBACK_DAYS = 90
REGIMES = [("Rs 12k cap", date(2022, 4, 3), date(2023, 4, 3)),
           ("Rs 10k cap", date(2023, 4, 4), date(2025, 12, 31)),
           ("Rs 10k cap + DAM coupling", date(2026, 1, 1), date(2099, 12, 31))]
BASELINE_DIR = REPORTS_DIR / "baselines"


def load_market_arrays(con):
    """Actual prices and cleared volumes as (days x 96) arrays per market; NaN = no trade or missing."""
    import numpy as np
    import pandas as pd
    df = pd.read_sql_query(
        f"SELECT market, delivery_date, block, mcp_rs_mwh AS p, mcv_mw AS v, ({NO_TRADE_SQL}) AS nt"
        f" FROM blocks_latest", con)
    df.loc[df.nt == 1, ["p", "v"]] = np.nan
    last = date.fromisoformat(df.delivery_date.max())
    days = [d.isoformat() for d in daterange(HISTORY_START, last)]
    P, V = {}, {}
    for market in MARKETS:
        g = df[df.market == market]
        P[market] = g.pivot(index="delivery_date", columns="block", values="p").reindex(
            index=days, columns=range(1, 97)).to_numpy()
        V[market] = g.pivot(index="delivery_date", columns="block", values="v").reindex(
            index=days, columns=range(1, 97)).to_numpy()
    return days, P, V


def shift_days(A, k):
    import numpy as np
    out = np.full_like(A, np.nan)
    out[k:] = A[:-k]
    return out


def known_value(A, market, k):
    """For each delivery day, the k-th most recent known value of each block at decision time."""
    out = shift_days(A, k)
    if market == "RTM":
        out[:, RTM_KNOWN_BLOCKS:] = shift_days(A, k + 1)[:, RTM_KNOWN_BLOCKS:]
    return out


def recent_mean(A, market, n_days):
    import numpy as np
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks stay NaN
        return np.nanmean(np.stack([known_value(A, market, k) for k in range(1, n_days + 1)]), axis=0)


def profile(rule, market, A):
    """Price (or volume) profile a baseline plans on. Gaps are filled from the 7-day mean."""
    import numpy as np
    if rule == "B2":
        return recent_mean(A, market, 7)
    out = known_value(A, market, 1) if rule == "B4" else shift_days(A, 7)  # B3: D-7 is fully known
    fill = recent_mean(A, market, 7)
    return np.where(np.isnan(out), fill, out)


def best_windows(prices, buy_allowed):
    """Cheapest 2-hour window and the dearest 2-hour window starting after it ends.
    buy_allowed: bool per block where charging is allowed. Returns (buy_start, sell_start) blocks or None."""
    import numpy as np
    w = np.lib.stride_tricks.sliding_window_view(prices, WINDOW_BLOCKS).mean(axis=1)
    ok_buy = np.lib.stride_tricks.sliding_window_view(buy_allowed, WINDOW_BLOCKS).all(axis=1) & ~np.isnan(w)
    best, best_buy = None, None
    for sell in range(WINDOW_BLOCKS, len(w)):
        cand = sell - WINDOW_BLOCKS
        if ok_buy[cand] and (best_buy is None or w[cand] < w[best_buy]):
            best_buy = cand
        if best_buy is not None and not np.isnan(w[sell]) and (best is None or w[sell] - w[best_buy] > best[2]):
            best = (best_buy + 1, sell + 1, w[sell] - w[best_buy])
    return best[:2] if best else None


def window_plan(bat, starts):
    """Fixed-window plans: one full cycle at even power. starts: per-day (buy_start, sell_start) or None."""
    import numpy as np
    per_block = bat.usable_steps // WINDOW_BLOCKS
    plan = np.zeros((len(starts), BLOCKS_PER_DAY), dtype=np.int16)
    for i, st in enumerate(starts):
        if st:
            b, s = st
            plan[i, b - 1:b - 1 + WINDOW_BLOCKS] = per_block
            plan[i, s - 1:s - 1 + WINDOW_BLOCKS] = -per_block
    return plan


def monthly_windows(bat, market, A, days):
    """B1: on each month's first day, pick windows from the last 90 days' known average profile."""
    import numpy as np
    import warnings
    allowed = charge_allowed(bat)
    starts, cache = [], {}
    for i, d in enumerate(days):
        month = d[:7]
        if month not in cache:
            rows = [known_value(A[max(0, i - B1_LOOKBACK_DAYS - 2):i + 1], market, k)[-1]
                    for k in range(1, min(B1_LOOKBACK_DAYS, i) + 1)]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                prof = np.nanmean(np.stack(rows), axis=0) if rows else np.full(BLOCKS_PER_DAY, np.nan)
            cache[month] = best_windows(prof, allowed)
        starts.append(cache[month])
    return starts


def run_baseline(strategy, market, bat, days, P, V):
    """Plan and settle one baseline over all days. Returns the settlement dict."""
    import numpy as np
    if market == "BOTH":  # X-PH: buy at the cheaper market, sell at the dearer, with hindsight
        dam, rtm = P["DAM"], P["RTM"]
        buy, sell = np.fmin(dam, rtm), np.fmax(dam, rtm)
        buy_v = np.where(np.isnan(rtm) | (dam <= rtm), V["DAM"], V["RTM"])
        sell_v = np.where(np.isnan(rtm) | (dam >= rtm), V["DAM"], V["RTM"])
        ch, _ = step_limits(bat, buy_v)
        _, dis = step_limits(bat, sell_v)
        plan, gap = plan_days(bat, buy, sell, ch, dis)
        return {**settle(bat, plan, buy, sell, buy_v, sell_v), "opt_gap_rs": gap}
    A, Vm = P[market], V[market]
    if strategy == "B0":
        b, s = FIXED_WINDOWS
        plan = window_plan(bat, [(b, s)] * len(days))
    elif strategy == "B1":
        plan = window_plan(bat, monthly_windows(bat, market, A, days))
    else:
        prices = A if strategy == "PH" else profile(strategy, market, A)
        vols = Vm if strategy == "PH" else profile(strategy, market, Vm)
        ch, dis = step_limits(bat, vols)
        plan, gap = plan_days(bat, prices, prices, ch, dis)
        return {**settle(bat, plan, A, A, Vm, Vm), "opt_gap_rs": gap}
    return {**settle(bat, plan, A, A, Vm, Vm), "opt_gap_rs": np.zeros(len(days))}


def run_id_for(strategy, market, bat):
    prefix = {"DAM": "DA", "RTM": "RT", "BOTH": "X"}[market]
    sid = f"{prefix}-{strategy}_c{bat.max_cycles}_w{bat.wear_rs_mwh:g}"
    return sid + ("_solar" if bat.charge_blocks else "")


def code_version():
    import subprocess
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "bess.py"], cwd=ROOT, capture_output=True,
                               text=True).stdout.strip()
        if dirty:  # uncommitted edits: fingerprint the file so stale stored runs aren't reused
            head += "+dirty." + hashlib.sha256((ROOT / "bess.py").read_bytes()).hexdigest()[:8]
        return head
    except Exception:
        return "unknown"


def save_run(con, run_id, strategy, market, bat, days, res, version):
    import pandas as pd
    daily = pd.DataFrame({"delivery_date": days, **{k: res[k] for k in SETTLE_FIELDS}})
    daily = daily[daily.delivery_date >= EVAL_START.isoformat()].reset_index(drop=True)
    settings = json.dumps({**asdict(bat), "eval_start": EVAL_START.isoformat()})
    con.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
    con.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    con.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, strategy, market, settings, daily.delivery_date.iloc[0],
                 daily.delivery_date.iloc[-1], datetime.now().isoformat(sep=" ", timespec="seconds"), version))
    con.executemany(f"INSERT INTO results VALUES ({', '.join('?' * (len(SETTLE_FIELDS) + 2))})",
                    [(run_id, *row) for row in daily[["delivery_date", *SETTLE_FIELDS]].itertuples(index=False)])
    return daily


# ---------------------------------------------------------------- backtest reports

def lakh_per_mw_year(profit_rs, n_days, bat):
    return profit_rs / 1e5 / bat.power_mw / (n_days / 365.25)


def run_metrics(daily, ceiling, bat):
    """Headline numbers for one run. ceiling: the matching perfect-hindsight daily frame."""
    n = len(daily)
    profit, gross = daily.profit_rs.sum(), daily.profit_rs.sum() + daily.wear_rs.sum()
    month = daily.groupby(daily.delivery_date.str[:7]).profit_rs.sum()
    worst = daily.loc[daily.profit_rs.idxmin()]
    return {
        "days": n,
        "profit_lakh_mw_yr": lakh_per_mw_year(profit, n, bat),
        "before_wear_lakh_mw_yr": lakh_per_mw_year(gross, n, bat),
        "capture": profit / ceiling.profit_rs.sum() if ceiling is not None else float("nan"),
        "losing_days": (daily.profit_rs < 0).mean(),
        "worst_day": worst.delivery_date,
        "worst_day_lakh_mw": worst.profit_rs / 1e5 / bat.power_mw,
        "worst_month": month.idxmin(),
        "worst_month_lakh_mw": month.min() / 1e5 / bat.power_mw,
        "cycles_per_day": daily.cycles.mean(),
        "unfilled_mwh": daily.unfilled_mwh.sum(),
        "cut_mwh": daily.cut_mwh.sum(),
        "stranded_mwh": daily.stranded_mwh.sum(),
        "opt_gap_share": daily.opt_gap_rs.sum() / max(abs(profit), 1.0),
    }


def period_rows(daily, ceiling, bat):
    """Profit and capture by calendar year and by regime."""
    rows = []
    groups = [(y, daily.delivery_date.str[:4] == y) for y in sorted(daily.delivery_date.str[:4].unique())]
    groups += [(name, (daily.delivery_date >= a.isoformat()) & (daily.delivery_date <= b.isoformat()))
               for name, a, b in REGIMES]
    for name, mask in groups:
        d = daily[mask]
        if d.empty:
            continue
        cap = d.profit_rs.sum() / ceiling[mask].profit_rs.sum() if ceiling is not None else float("nan")
        rows.append([esc(name), f"{len(d):,}", f"{lakh_per_mw_year(d.profit_rs.sum(), len(d), bat):.1f}",
                     "–" if cap != cap else f"{cap:.0%}", f"{(d.profit_rs < 0).mean():.1%}",
                     f"{d.cycles.mean():.2f}"])
    return rows


# IMD seasons, by calendar month
SEASONS = [("Winter", "Dec–Feb", (12, 1, 2)), ("Summer", "Mar–May", (3, 4, 5)),
           ("Monsoon", "Jun–Sep", (6, 7, 8, 9)), ("Post-monsoon", "Oct–Nov", (10, 11))]


def season_of(delivery_dates):
    """Season name for each YYYY-MM-DD string (pandas Series)."""
    month = delivery_dates.str[5:7].astype(int)
    out = month.map({m: name for name, _, months in SEASONS for m in months})
    return out


def season_metrics(daily, ceiling, bat):
    """Per-season profit, capture, losing days and cycles. Returns a list of dicts."""
    seasons = season_of(daily.delivery_date)
    rows = []
    for name, months, _ in SEASONS:
        mask = (seasons == name).to_numpy()
        d = daily[mask]
        if d.empty:
            continue
        cap = d.profit_rs.sum() / ceiling[mask].profit_rs.sum() if ceiling is not None else float("nan")
        rows.append({"season": name, "months": months, "days": len(d),
                     "profit_k_mw_day": d.profit_rs.mean() / 1e3 / bat.power_mw,
                     "lakh_mw_yr_rate": lakh_per_mw_year(d.profit_rs.sum(), len(d), bat),
                     "capture": cap, "losing_days": (d.profit_rs < 0).mean(),
                     "cycles_per_day": d.cycles.mean()})
    return rows


def png_base64(fig):
    import base64
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
    return base64.b64encode(buf.getvalue()).decode()


def monthly_chart(daily, ceiling, bat, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    surface, ink, muted, grid = "#fcfcfb", "#1d1d1b", "#6b6a64", "#e4e1d8"
    fig, ax = plt.subplots(figsize=(10, 3.6), facecolor=surface)
    ax.set_facecolor(surface)
    series = [(daily, "#2a78d6", "This run")]
    if ceiling is not None:
        series.append((ceiling, "#eb6834", "Perfect hindsight"))
    for df, color, label in series:
        m = df.groupby(df.delivery_date.str[:7]).profit_rs.sum() / 1e5 / bat.power_mw
        x = pd.to_datetime(m.index + "-01")
        ax.plot(x, m.values, color=color, lw=2, label=label)
    ax.set_title(title, loc="left", fontsize=11, color=ink)
    ax.set_ylabel("₹ lakh per MW per month", color=muted, fontsize=9)
    ax.grid(axis="y", color=grid, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(grid)
    ax.tick_params(colors=muted, labelsize=9, length=0)
    ax.axhline(0, color=muted, lw=0.8)
    if len(series) > 1:
        ax.legend(frameon=False, fontsize=9, labelcolor=ink, loc="upper left")
    fig.tight_layout()
    data = png_base64(fig)
    plt.close(fig)
    return data


def write_run_report(run_id, strategy, market, bat, daily, ceiling, version, out_dir=BASELINE_DIR):
    folder = out_dir / run_id
    folder.mkdir(parents=True, exist_ok=True)
    daily.round(2).to_csv(folder / "daily.csv", index=False)
    m = run_metrics(daily, ceiling, bat)
    tiles = [("Profit, ₹ lakh/MW/yr", f"{m['profit_lakh_mw_yr']:.1f}"),
             ("Before wear", f"{m['before_wear_lakh_mw_yr']:.1f}"),
             ("Capture of ceiling", "–" if m["capture"] != m["capture"] else f"{m['capture']:.0%}"),
             ("Losing days", f"{m['losing_days']:.1%}"),
             ("Cycles per day", f"{m['cycles_per_day']:.2f}")]
    month_of_year = daily.groupby(daily.delivery_date.str[5:7]).profit_rs.mean() / 1e3 / bat.power_mw
    charge = "any time" if not bat.charge_blocks else \
        f"{block_label(bat.charge_blocks[0])}–{block_label(bat.charge_blocks[1] + 1)} only"
    settings = (f"{bat.power_mw:g} MW / {bat.energy_mwh:g} MWh · RTE {bat.rte:.0%} · SoC {bat.soc_min:.0%}–"
                f"{bat.soc_max:.0%} · {bat.max_cycles} cycle(s)/day · fee ₹{bat.fee_rs_mwh:g}/MWh each side · "
                f"wear ₹{bat.wear_rs_mwh:g}/MWh · participation ≤ {bat.participation:.0%} of cleared volume · "
                f"charging {charge}")
    chart = monthly_chart(daily, ceiling, bat, "Monthly profit")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(run_id)}</title><style>{HEALTH_CSS}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }}
.tile b {{ display:block; font-size:22px; }} .tile span {{ color:var(--muted); font-size:13px; }}
img {{ max-width:100%; height:auto; border:1px solid var(--line); border-radius:8px; }}
</style></head><body><main>
<p class="sub"><a href="../summary.html">← all baselines</a></p>
<h1>{esc(run_id)}</h1>
<p class="sub">{esc(BASELINE_DESCRIPTIONS.get(strategy, strategy))} · market {market} ·
{daily.delivery_date.iloc[0]} to {daily.delivery_date.iloc[-1]} ({len(daily):,} days) · code {esc(version)}</p>
<p class="note">{esc(settings)}</p>
<div class="tiles">{"".join(f'<div class="tile"><b>{v}</b><span>{esc(k)}</span></div>' for k, v in tiles)}</div>
<h2>Monthly profit</h2><img alt="Monthly profit" src="data:image/png;base64,{chart}">
<h2>By year and regime</h2>
{html_table(["Period", "Days", "₹ lakh/MW/yr", "Capture", "Losing days", "Cycles/day"], period_rows(daily, ceiling, bat))}
<h2>By season</h2>
{html_table(["Season", "Months", "Days", "₹ thousand/MW/day", "Annual rate, ₹ lakh/MW/yr", "Capture",
             "Losing days", "Cycles/day"],
            [[r["season"], r["months"], f'{r["days"]:,}', f'{r["profit_k_mw_day"]:.1f}', f'{r["lakh_mw_yr_rate"]:.1f}',
              "–" if r["capture"] != r["capture"] else f'{r["capture"]:.0%}', f'{r["losing_days"]:.1%}',
              f'{r["cycles_per_day"]:.2f}'] for r in season_metrics(daily, ceiling, bat)])}
<p class="note">Annual rate = the season's average daily profit × 365, for comparing seasons on the same scale.</p>
<h2>Average profit per day by calendar month (₹ thousand per MW)</h2>
{html_table(["Month", *[date(2000, int(k), 1).strftime("%b") for k in month_of_year.index]],
            [["Avg", *[f"{v:,.1f}" for v in month_of_year.values]]])}
<h2>Operations</h2>
{html_table(["Measure", "Value"], [
    ["Worst day", f"{m['worst_day']} (₹{m['worst_day_lakh_mw'] * 1e5:,.0f} per MW)"],
    ["Worst month", f"{m['worst_month']} (₹{m['worst_month_lakh_mw']:.2f} lakh per MW)"],
    ["Energy bought / sold", f"{daily.bought_mwh.sum():,.0f} / {daily.sold_mwh.sum():,.0f} MWh"],
    ["Unfilled (no-trade blocks)", f"{m['unfilled_mwh']:,.0f} MWh"],
    ["Cut by participation limit", f"{m['cut_mwh']:,.0f} MWh"],
    ["Left in battery at day end (valued 0)", f"{m['stranded_mwh']:,.0f} MWh"],
    ["Optimiser gap bound (best possible plan on the planned prices could earn at most this much more)",
     f"{m['opt_gap_share']:.2%} of profit"]])}
<p class="note">Daily results: daily.csv in this folder, and the results table in power.db (run_id {esc(run_id)}).</p>
</main></body></html>"""
    (folder / "report.html").write_text(html, "utf-8")
    return m


BASELINE_DESCRIPTIONS = {
    "B0": "Fixed windows: charge 11:00–13:00, discharge 18:00–20:00 every day",
    "B1": "Monthly windows: best 2-hour buy/sell windows from the last 90 days, re-picked each month",
    "B2": "7-day average profile, then optimiser",
    "B3": "Same weekday last week (D–7), then optimiser",
    "B4": "Yesterday's prices, then optimiser",
    "PH": "Perfect hindsight (ceiling): optimiser on the actual prices",
}

SCENARIOS = [  # (label, wear Rs/MWh, charge_blocks)
    ("base, no wear", 0.0, None),
    ("base, wear ₹300/MWh", 300.0, None),
    ("charging 10:00–15:00 only, wear ₹300/MWh", 300.0, (41, 60)),
]


def baseline_checks(table):
    """Spec checks 1, 2, 3 and 6 on the finished runs. table: {run_id: daily frame}."""
    import numpy as np
    findings = []

    def daily_profit(rid):
        return table[rid].profit_rs.to_numpy() if rid in table else None

    for rid, df in table.items():
        prefix, rest = rid.split("-", 1)
        strat, tail = rest.split("_", 1)
        if prefix == "X" or strat == "PH":
            continue
        ph = table.get(f"{prefix}-PH_{tail}")
        if ph is not None:  # the exact ceiling is at most PH profit + its proven optimiser gap
            bad = int((df.profit_rs.to_numpy() > ph.profit_rs.to_numpy() + ph.opt_gap_rs.to_numpy() + 1.0).sum())
            findings.append(("1 ceiling >= baseline", rid, bad))
    # Ceiling comparisons allow for each ceiling's proven optimiser gap: exact optimum
    # lies in [profit, profit + opt_gap_rs].
    upper = lambda rid: table[rid].profit_rs.to_numpy() + table[rid].opt_gap_rs.to_numpy()
    for rid in table:
        if rid.startswith("X-PH"):
            tail = rid.split("_", 1)[1]
            for m in ("DA", "RT"):
                other = daily_profit(f"{m}-PH_{tail}")
                if other is not None:
                    findings.append(("2 X-PH >= single-market PH", f"{rid} vs {m}",
                                     int((upper(rid) < other - 1.0).sum())))
        if "_c2_" in rid and rid.split("-")[1].startswith("PH"):
            one = daily_profit(rid.replace("_c2_", "_c1_"))
            if one is not None:
                findings.append(("6 2-cycle ceiling >= 1-cycle", rid, int((upper(rid) < one - 1.0).sum())))
        # Only for ceilings: a forecast-based plan made more cautious by wear can earn more.
        if "_w0" in rid and "PH_" in rid:
            heavy = rid.replace("_w0", "_w300")
            if heavy in table:
                worse = table[heavy].profit_rs.sum() > upper(rid).sum() + 1.0
                findings.append(("3 more wear never raises the ceiling's profit", rid, int(worse)))
    return findings


def write_summary(rows, checks, bat_default, out_dir=BASELINE_DIR, title="Baseline backtests"):
    import pandas as pd
    summary = pd.DataFrame(rows)
    summary.to_csv(out_dir / "summary.csv", index=False)
    parts = []
    for label, group in summary.groupby("scenario", sort=False):
        table = [[f'<a href="{r.run_id}/report.html">{esc(r.run_id)}</a>',
                  esc(BASELINE_DESCRIPTIONS.get(r.strategy, r.strategy) if r.market != "BOTH"
                      else "Perfect hindsight, best market per block"),
                  r.market, str(r.cycles), f"{r.profit_lakh_mw_yr:.1f}", f"{r.before_wear_lakh_mw_yr:.1f}",
                  "–" if r.capture != r.capture else f"{r.capture:.0%}", f"{r.losing_days:.1%}",
                  f"{r.cycles_per_day:.2f}"] for r in group.itertuples()]
        parts.append(f"<h2>{esc(label)}</h2>" + html_table(
            ["Run", "Strategy", "Market", "Cycle limit", "₹ lakh/MW/yr", "Before wear", "Capture",
             "Losing days", "Cycles/day"], table))
    check_rows = [[esc(c), esc(r), f'<span class="{"pass" if n == 0 else "fail"}">{n}</span>']
                  for c, r, n in checks]
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{HEALTH_CSS}</style></head><body><main>
<h1>{esc(title)}</h1>
<p class="sub">{summary.first_date.min()} to {summary.last_date.max()} · {bat_default.power_mw:g} MW / {bat_default.energy_mwh:g} MWh ·
spec: docs/baselines.md · generated {datetime.now():%d %b %Y %H:%M}</p>
<p class="sub"><a href="seasons.html">Results by season →</a></p>
<p class="note">Capture = profit ÷ perfect-hindsight profit in the same market, cycle limit and scenario.
Profit is after fees and wear, before transmission charges and operating costs.</p>
{"".join(parts)}
<h2>Result checks (findings per check; 0 = pass)</h2>
{html_table(["Check", "Runs", "Days or cases failing"], check_rows)}
</main></body></html>"""
    (out_dir / "summary.html").write_text(html, "utf-8")


def write_season_summary(table, out_dir, title):
    """seasons.csv (one row per run and season) and seasons.html (runs x seasons)."""
    import pandas as pd
    rows = []
    for rid, daily in table.items():
        prefix, rest = rid.split("-", 1)
        strategy, tail = rest.split("_", 1)
        cycles, wear = int(tail.split("_")[0][1:]), float(tail.split("_")[1][1:])
        charge = (41, 60) if tail.endswith("_solar") else None
        bat = Battery(wear_rs_mwh=wear, max_cycles=cycles, charge_blocks=charge)
        ceiling = table.get(f"{prefix}-PH_{tail}") if strategy != "PH" else None
        scenario = next(lbl for lbl, w, c in SCENARIOS if w == wear and c == charge)
        for r in season_metrics(daily, ceiling, bat):
            rows.append({"run_id": rid, "scenario": scenario, "strategy": strategy, "cycles": cycles, **r})
    df = pd.DataFrame(rows)
    df.round(4).to_csv(out_dir / "seasons.csv", index=False)
    names = [name for name, _, _ in SEASONS if name in set(df.season)]
    parts = []
    for scenario, g in df.groupby("scenario", sort=False):
        body = []
        for rid, r in g.groupby("run_id", sort=False):
            r = r.set_index("season")
            cells = []
            for name in names:
                if name not in r.index:
                    cells.append("–")
                    continue
                x = r.loc[name]
                cap = "" if x.capture != x.capture else f" · {x.capture:.0%}"
                cells.append(f"{x.profit_k_mw_day:.1f}{cap}")
            body.append([f'<a href="{rid}/report.html">{esc(rid)}</a>', *cells])
        parts.append(f"<h2>{esc(scenario)}</h2>" + html_table(
            ["Run", *[f"{n} ({m})" for n, m, _ in SEASONS if n in names]], body))
    days = df.drop_duplicates("season").set_index("season").days
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}: seasons</title><style>{HEALTH_CSS}</style></head><body><main>
<p class="sub"><a href="summary.html">← summary</a></p>
<h1>{esc(title)}: by season</h1>
<p class="sub">Each cell: average profit in ₹ thousand per MW per day · capture of the perfect-hindsight
ceiling in the same season. Days per season: {", ".join(f"{n} {days[n]:,}" for n in names)}.</p>
{"".join(parts)}
<p class="note">Seasons follow IMD: winter Dec–Feb, summer Mar–May, monsoon Jun–Sep, post-monsoon Oct–Nov.
Data in seasons.csv, with the annual-rate equivalent (daily average × 365).</p>
</main></body></html>"""
    (out_dir / "seasons.html").write_text(html, "utf-8")


def stored_run(run_id, bat, version, last_day):
    """A previously stored run with the same settings, code and data range, or None."""
    import pandas as pd
    settings = json.dumps({**asdict(bat), "eval_start": EVAL_START.isoformat()})
    with connect() as con:
        hit = con.execute("SELECT 1 FROM runs WHERE run_id = ? AND settings = ? AND code_version = ?"
                          " AND last_date = ?", (run_id, settings, version, last_day)).fetchone()
        if not hit:
            return None
        return pd.read_sql_query(f"SELECT delivery_date, {', '.join(SETTLE_FIELDS)} FROM results"
                                 f" WHERE run_id = ? ORDER BY delivery_date", con, params=(run_id,))


def cmd_baselines(args):
    import time as _t
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    version = code_version()
    with connect() as con:
        con.executescript(SCHEMA)
        days, P, V = load_market_arrays(con)
    table = {}
    for label, wear, charge in SCENARIOS:
        print(f"{label}:")
        for market in ("DAM", "RTM", "BOTH"):
            for strategy in (["PH"] if market == "BOTH" else BASELINE_STRATEGIES):
                for cycles in ((1,) if strategy in ("B0", "B1") else (1, 2)):
                    bat = Battery(wear_rs_mwh=wear, max_cycles=cycles, charge_blocks=charge)
                    rid = run_id_for(strategy, market, bat)
                    t0 = _t.time()
                    daily = None if args.force else stored_run(rid, bat, version, days[-1])
                    if daily is None:
                        res = run_baseline(strategy, market, bat, days, P, V)
                        with connect() as con:
                            daily = save_run(con, rid, strategy, market, bat, days, res, version)
                    else:
                        print(f"  {rid:28} already stored for this code and data")
                        table[rid] = daily
                        continue
                    table[rid] = daily
                    print(f"  {rid:28} {lakh_per_mw_year(daily.profit_rs.sum(), len(daily), bat):6.1f} "
                          f"lakh/MW/yr  ({_t.time() - t0:.0f}s)")
    write_baseline_reports(table, {rid: version for rid in table}, BASELINE_DIR, "Baseline backtests")


def write_baseline_reports(table, versions, out_dir, title):
    """Per-run reports, summary and result checks for {run_id: daily frame} into out_dir.
    Reports need each run's ceiling, so they are written once all runs are in the table."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for rid, daily in table.items():
        prefix, rest = rid.split("-", 1)
        strategy, tail = rest.split("_", 1)
        market = {"DA": "DAM", "RT": "RTM", "X": "BOTH"}[prefix]
        cycles = int(tail.split("_")[0][1:])
        wear = float(tail.split("_")[1][1:])
        charge = (41, 60) if tail.endswith("_solar") else None
        bat = Battery(wear_rs_mwh=wear, max_cycles=cycles, charge_blocks=charge)
        ceiling = table.get(f"{prefix}-PH_{tail}") if strategy != "PH" else None
        m = write_run_report(rid, strategy, market, bat, daily, ceiling, versions[rid], out_dir)
        label = next(lbl for lbl, w, c in SCENARIOS if w == wear and c == charge)
        rows.append({"run_id": rid, "scenario": label, "strategy": strategy, "market": market,
                     "cycles": cycles, "wear_rs_mwh": wear, "charging": "10-15h" if charge else "any",
                     "first_date": daily.delivery_date.iloc[0], "last_date": daily.delivery_date.iloc[-1],
                     **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}})
    checks = baseline_checks(table)
    write_summary(rows, checks, Battery(), out_dir, title)
    write_season_summary(table, out_dir, title)
    failed = [c for c in checks if c[2]]
    print(f"wrote {out_dir.relative_to(ROOT)}/summary.html, summary.csv and {len(table)} run folders")
    print(f"result checks: {len(checks) - len(failed)} passed, {len(failed)} with findings")
    for c, r, n in failed:
        print(f"  {c}: {r}: {n}")


def cmd_baseline_report(args):
    """Rebuild baseline reports for a date window from the stored daily results.
    Each day is simulated independently (start and end at soc_min, planning only on earlier
    prices), so a window's results are exactly the stored days in it; nothing is re-simulated."""
    import pandas as pd
    first = args.start or EVAL_START.isoformat()
    with connect() as con:
        runs = pd.read_sql_query("SELECT run_id, code_version FROM runs ORDER BY rowid", con)
        if runs.empty:
            sys.exit("no stored runs; run `python bess.py baselines` first")
        last = args.end or con.execute("SELECT MAX(delivery_date) FROM results").fetchone()[0]
        res = pd.read_sql_query(
            f"SELECT run_id, delivery_date, {', '.join(SETTLE_FIELDS)} FROM results"
            f" WHERE delivery_date BETWEEN ? AND ? ORDER BY run_id, delivery_date", con, params=(first, last))
    table = {rid: g.drop(columns="run_id").reset_index(drop=True)
             for rid, g in res.groupby("run_id", sort=False)}
    table = {rid: table[rid] for rid in runs.run_id if rid in table}
    out_dir = Path(args.out).resolve() if args.out else REPORTS_DIR / f"baselines_{first}_{last}"
    title = args.title or f"Baseline backtests, {first} to {last}"
    write_baseline_reports(table, dict(zip(runs.run_id, runs.code_version)), out_dir, title)


# ---------------------------------------------------------------- self-tests

def cmd_selftest(args):
    """Checks that don't depend on results: optimiser, look-ahead, known_at, a hand-computed day."""
    import numpy as np
    failures = []

    def check(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail and not ok else ''}")
        if not ok:
            failures.append(name)

    bat = Battery()
    # 1. Optimiser on a day with one obvious cycle
    p = np.full((1, 96), 5000.0)
    p[0, 40:48], p[0, 72:80] = 1000.0, 10000.0
    ch, dis = step_limits(bat, np.full((1, 96), np.nan))
    plan, _ = plan_days(bat, p, p, ch, dis)
    res = settle(bat, plan, p, p, np.full((1, 96), np.nan), np.full((1, 96), np.nan))
    usable = bat.usable_steps * bat.step_mwh
    expected = usable * bat.eta * (10000 - 20) - usable / bat.eta * (1000 + 20)
    check("optimiser finds the obvious cycle", abs(res["profit_rs"][0] - expected) < 1, f"{res['profit_rs'][0]:.0f} vs {expected:.0f}")
    check("plan ends at the SoC floor", int(plan.sum()) == 0)
    # 2. Cycle limit holds, and 2 cycles >= 1 cycle
    rng = np.random.default_rng(0)
    p = rng.uniform(1000, 10000, (20, 96))
    ch, dis = step_limits(bat, np.full_like(p, np.nan))
    one, gap1 = plan_days(bat, p, p, ch, dis)
    two, _ = plan_days(replace(bat, max_cycles=2), p, p, ch, dis)
    check("cycle limit respected", (discharged_steps(one) <= bat.usable_steps).all()
          and (discharged_steps(two) <= 2 * bat.usable_steps).all())
    nan = np.full_like(p, np.nan)
    profit = lambda plan: settle(bat, plan, p, p, nan, nan)["profit_rs"]
    exact = profit(optimise_exact(bat, p, p, ch, dis, bat.usable_steps))
    short = exact - profit(one)
    check("optimiser shortfall vs exact is within its reported bound", (short <= gap1 + 1).all() and (short >= -1).all(),
          f"max excess Rs {(short - gap1).max():.0f}")
    check("optimiser is within 1% of exact on every day", (short <= 0.01 * np.abs(exact) + 1).all(),
          f"max shortfall {(short / exact).max():.2%}")
    check("2-cycle profit >= 1-cycle profit", (settle(bat, two, p, p, nan, nan)["profit_rs"]
                                                >= settle(bat, one, p, p, nan, nan)["profit_rs"] - 1).all())
    # 3. Look-ahead: corrupting data from day i on never changes what day i's baselines see
    with connect() as con:
        days, P, V = load_market_arrays(con)
    for market in MARKETS:
        A = P[market]
        for i in (100, 800, len(days) - 5):
            bad = A.copy()
            bad[i:] = rng.uniform(0, 20000, bad[i:].shape)
            same = all(np.array_equal(profile(r, market, A)[i], profile(r, market, bad)[i], equal_nan=True)
                       for r in ("B2", "B3", "B4"))
            w_good = monthly_windows(bat, market, A[:i + 1], days[:i + 1])[-1]
            w_bad = monthly_windows(bat, market, bad[:i + 1], days[:i + 1])[-1]
            check(f"look-ahead: {market} day {days[i]} unaffected by its own and later prices",
                  same and w_good == w_bad)
    # 4. Information rules agree with known_at in the database
    with connect() as con:
        q = lambda sql: con.execute(sql).fetchone()[0]
        check("DAM day D is known before 12:00 on D (decision for D+1)",
              q("SELECT COUNT(*) FROM blocks WHERE market='DAM' AND known_at >= delivery_date || ' 12:00:00'") == 0)
        check(f"RTM blocks 1-{RTM_KNOWN_BLOCKS} of D are known before 22:00 on D",
              q(f"SELECT COUNT(*) FROM blocks WHERE market='RTM' AND block <= {RTM_KNOWN_BLOCKS}"
                f" AND known_at >= delivery_date || ' 22:00:00'") == 0)
        check(f"RTM blocks {RTM_KNOWN_BLOCKS + 1}-96 of D are not known at 22:00 on D (so D-2 is used)",
              q(f"SELECT COUNT(*) FROM blocks WHERE market='RTM' AND block > {RTM_KNOWN_BLOCKS}"
                f" AND known_at < delivery_date || ' 22:00:00'") == 0)
    # 5. DA-B0 on 29 Sep 2026, computed by hand
    i = days.index("2026-09-29")
    A, Vm = P["DAM"][i:i + 1], V["DAM"][i:i + 1]
    res = settle(bat, window_plan(bat, [FIXED_WINDOWS]), A, A, Vm, Vm)
    per_block = bat.usable_steps // WINDOW_BLOCKS * bat.step_mwh  # stored MWh per block
    b, s = FIXED_WINDOWS
    buy = sum(per_block / bat.eta * (A[0, k] + 20) for k in range(b - 1, b + 7))
    sell = sum(per_block * bat.eta * (A[0, k] - 20) for k in range(s - 1, s + 7))
    check("DA-B0 on 2026-09-29 matches a hand calculation", abs(res["profit_rs"][0] - (sell - buy)) < 1,
          f"{res['profit_rs'][0]:.0f} vs {sell - buy:.0f}")
    print(f"{'all checks passed' if not failures else f'{len(failures)} check(s) failed'}")
    if failures:
        sys.exit(1)


# ---------------------------------------------------------------- commands

def cmd_init(args):
    for d in [*RAW_SUBDIRS.values(), REPORTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
    print(f"ready: {DB_PATH.name}, raw/dam/, raw/rtm/, reports/")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bess.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create folders and database").set_defaults(func=cmd_init)
    p = sub.add_parser("fetch", help="download missing months into raw/")
    p.add_argument("--markets", nargs="+", default=list(MARKETS), choices=MARKETS)
    p.add_argument("--from", dest="start", help="YYYY-MM-DD, default 2022-04-01")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD, default end of last month")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("ingest", help="load new or changed raw files into power.db")
    p.add_argument("folder", nargs="?", default=str(RAW_DIR))
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("validate", help="run data checks and write findings to issues")
    p.add_argument("--from", dest="start", help="YYYY-MM-DD, default 2022-04-01")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD, default last day loaded")
    p.set_defaults(func=cmd_validate)
    sub.add_parser("health", help="write reports/health.html").set_defaults(func=cmd_health)
    sub.add_parser("firstlook", help="write reports/firstlook.csv, firstlook_monthly.csv and firstlook.png"
                   ).set_defaults(func=cmd_firstlook)
    p = sub.add_parser("export", help="write blocks to CSV for your own analysis")
    p.add_argument("--market", default="ALL", type=str.upper, choices=[*MARKETS, "ALL"])
    p.add_argument("--from", dest="start", help="YYYY-MM-DD, default 2022-04-01")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD, default last day loaded")
    p.add_argument("--out", help="CSV path, default reports/export_<market>_<from>_<to>.csv")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("baselines", help="run every baseline (docs/baselines.md), write reports/baselines/")
    p.add_argument("--force", action="store_true", help="recompute runs already stored for this code")
    p.set_defaults(func=cmd_baselines)
    p = sub.add_parser("baseline-report", help="rebuild baseline reports for a date window from stored results")
    p.add_argument("--from", dest="start", help="YYYY-MM-DD, default 2022-05-01")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD, default last day stored")
    p.add_argument("--out", help="folder, default reports/baselines_<from>_<to>")
    p.add_argument("--title", help="report title")
    p.set_defaults(func=cmd_baseline_report)
    sub.add_parser("selftest", help="optimiser, look-ahead and information checks").set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())

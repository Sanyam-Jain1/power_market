"""Battery backtester: Phase 1, the data foundation.

One file, standard library plus pandas and numpy. Run from the terminal:

    python bess.py init            # create folders and database
    python bess.py fetch           # download missing months into raw/ (slowly)
    python bess.py ingest raw/     # load new or changed raw files into power.db
    python bess.py validate        # run checks, write findings to issues
    python bess.py health          # write reports/health.html
    python bess.py firstlook       # write reports/firstlook.csv and a chart
    python bess.py export --market DAM --from 2025-04-01 --to 2026-03-31   # CSV

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
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())

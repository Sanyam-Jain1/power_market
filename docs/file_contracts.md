# File contracts

## What we store
`python bess.py fetch` saves each month's IEX market-snapshot page exactly as
received:

- `raw/dam/dam_YYYY-MM.html`, `raw/rtm/rtm_YYYY-MM.html`
- `raw/manifest.csv`: one line per request (path, url, fetched_at, status, bytes, sha256)

URL pattern (the page caps a range at 31 days, so one request per calendar month):

    https://www.iexindia.com/market-data/{day-ahead-market|real-time-market}/market-snapshot
      ?interval=ONE_FOURTH_HOUR&dp=SELECT_RANGE&showGraph=false&fromDate=DD-MM-YYYY&toDate=DD-MM-YYYY

## Embedded records (both markets)
The page embeds the whole range as JSON objects (with escaped quotes inside the
Next.js payload). `bess.page_records()` extracts them. Each day has 96 block
records followed by 4 summary records:

| key | example | notes |
|---|---|---|
| date | `01-08-2026` | DD-MM-YYYY |
| period | DAM `00:00 - 00:15`, RTM `00:00-00:15`; summary rows: `Total (MWh)`, `Max (MW)`, `Min (MW)`, `Avg (MW)` | last block is `23:45 - 24:00` |
| purchase_bid | `"16703.20"` | MW, string |
| sell_bid | `"4911.50"` | MW, string |
| mcv | `"4645.10"` | MW, market cleared volume (unconstrained) |
| final_scheduled_volume | `"4645.10"` | MW, after real-time curtailment (published T+2) |
| mcp | `"10000.00"` | Rs/MWh, unconstrained clearing price |
| congestion | `"NO"` | absent on summary rows |

- RTM 09-09-2026 has 12 blocks written in the DAM style (`12:30 - 12:45`); the parser accepts either.
- RTM has no session ID in the records; session = ceil(block / 2), 48 per day.
- DAM also has final_scheduled_volume (the design doc listed it as RTM only).

## Checks and notes
- The snapshot's daily "Avg" of MCP is the simple mean of the 96 block prices
  (DAM 29-09-2026: simple 7448.55 = published; volume-weighted 4922.51).
- DAM April 2022 shows prices of Rs 20,000/MWh: the Rs 12 cap was not yet in force.
- Final scheduled volume can change at T+2 (curtailment). Re-fetch a month at
  least 3 days after it ends; a changed file becomes a new version.
- The current day is partial; `fetch` only fetches complete months.
- No-trade blocks: some RTM blocks are published with bids, volume and price all 0
  (28 blocks on 7 days to Sep 2026). Nothing traded; this is not a Rs 0 price. IEX's
  daily "Avg" leaves them out. Genuine Rs 0 clearings (with volume) do occur and count.
- With no-trade blocks excluded, every day's mean MCP and total MCV match IEX's own
  daily summary rows (checked by `validate` for all days).

# ACME battery fleet vs an IEX-only backtest, 1 Apr – 30 Jun 2026

Script: `analysis/acme_q1fy27.py`. Outputs in `reports/acme_q1fy27/`: `results.csv`,
`monthly.csv`, `daily.csv` and `market_depth.csv`. Run on 2026-10-07.

**What the inputs are:**
- **ACME's figures and the capacity schedule:** taken as given in the request. I haven't
  verified them independently.
- **Prices:** IEX market data in `power.db`.
- **Settings:** 1 cycle/day, grid charging, 89% round trip, 93% of capacity used per cycle,
  exchange fee ₹20/MWh each side, no wear cost (ACME's operating profit is before
  depreciation).
- **Capacity convention:** capacity is energy deliverable to the grid. Each cycle sells
  0.93 MWh per MWh of capacity and buys 0.93 ÷ 0.89 = 1.045 MWh.
- **Fleet mix:** 4.5-hour and 2-hour batteries are run separately and blended 80/20. Each day
  is weighted by the capacity running that day (average 2,474 MWh; 225,135 MWh-days in the
  quarter).
- **Price-taker assumption:** results are per MWh of capacity, with no limit on market share.
  Market depth is checked separately in section 4.

## 1. Results (₹ per MWh of capacity per day, fleet-weighted, 80/20 blend)

| Strategy | Revenue | Revenue − charging | Avg sell price | Avg buy price | Quarter revenue | Quarter revenue − charging |
|---|---|---|---|---|---|---|
| Day-ahead, perfect hindsight (ceiling) | 8,682 | 7,375 | 9,200 | 1,241 | ₹195 cr | ₹166 cr |
| **Day-ahead, 7-day average (realistic)** | **8,648** | **7,313** | 9,146 | 1,263 | **₹195 cr** | **₹165 cr** |
| Day-ahead, yesterday's prices | 8,657 | 7,321 | 9,160 | 1,269 | ₹195 cr | ₹165 cr |
| Real-time, perfect hindsight | 7,415 | 6,032 | 7,891 | 1,337 | ₹167 cr | ₹136 cr |
| Real-time, 7-day average | 7,156 | 5,579 | 7,581 | 1,505 | ₹161 cr | ₹126 cr |
| Best of both markets per block, hindsight | 8,779 | 7,787 | 9,327 | 960 | ₹198 cr | ₹175 cr |
| **ACME reported** | **≈10,050** | **8,400–10,050** | | | **₹226 cr** | |

Your quick estimate holds: the best 2-hour day-ahead windows averaged ₹1,120 buy and
₹9,390 sell, with the sell window at the cap on 73 of 91 days. That gives ≈₹8,730 revenue
and ≈₹7,560 after charging for a 2-hour battery. The full optimiser gives 8,736 and 7,502.

**By duration** (day-ahead, 7-day average, simple daily mean):

| Duration | Revenue | Revenue − charging | Avg sell price | Avg buy price |
|---|---|---|---|---|
| 4.5-hour | 8,448 | 7,107 | 9,084 | 1,283 |
| 2-hour | 8,736 | 7,502 | 9,394 | 1,181 |

**By month** (day-ahead, 7-day average, blend):

| Month | Revenue | Revenue − charging | Avg fleet capacity |
|---|---|---|---|
| April | 7,959 | 6,621 | 1,830 MWh |
| May | 8,627 | 7,343 | 2,543 MWh |
| June | 8,927 | 7,588 | 3,047 MWh |

## 2. The gap

- **Revenue:**
  - Day-ahead alone comes out **14% below ACME** (8,648 vs 10,050).
  - That's ₹195 cr against ₹226 cr over the quarter, ₹31 cr short.
  - It's within your expected 15–25%, at the low end.
- **The strategy doesn't explain it.**
  - Perfect hindsight on day-ahead earns only 0.4% more than the 7-day-average rule.
  - Even perfect hindsight across both markets is still 13% below ACME.
  - The gap is about price or volume, not planning.
- **Revenue minus charging:**
  - Our charging cost is ≈₹1,335/MWh-day, about ₹30 cr for the quarter.
  - ACME's revenue minus operating profit is ₹37 cr. That has to cover charging plus O&M and
    other costs, so a ~₹30 cr charging bill fits.
  - That puts ACME's revenue minus charging at roughly ₹196 cr, or **≈₹8,700/MWh-day**,
    towards the low end of your 8,400–10,050 range.
  - Day-ahead alone (7,313) is about **16% below** that.

## 3. One cycle at 93% can't reach ₹10,050, even at the cap

- Selling 0.93 MWh per MWh of capacity per day, at ≤₹10/unit (the exchange cap and the top of
  ACME's contract range), the most revenue possible is **0.93 × ₹10,000 = ₹9,300/MWh-day**.
- ACME's ≈₹10,050 is 8% above that ceiling. The 8 May figure (≈₹9,570 per MWh/day of "net
  realization") is also above it.
- **So contract prices alone can't close the gap.** At ₹8–10/unit, contracts pay no more per
  MWh than day-ahead already did (₹9,146 average sell price). At least one of these must hold:
  1. **More capacity was running than the 2,474 MWh estimate.** At a ₹9,000 average realised
     price, ₹226 cr needs about **2,970 MWh** on average, about 20% more.
  2. **More energy per MWh per day.**
     - That could be deeper discharge than 93% of rated capacity, or capacity quoted on a
       different basis (for example DC or nameplate).
     - Or occasional extra partial cycles.
     - The required volume is ≈1.12 MWh sold per MWh of capacity at ₹9,000.
  3. **Revenue that isn't energy sales:** capacity or availability payments in the contracts,
     ancillary services, or another accounting item in "battery revenue".
- The disclosure doesn't let us tell these apart. The cleanest check would be ACME's
  MWh sold in the quarter, if it's published anywhere.

## 4. Could ACME have sold it all on the exchange?

| Measure | Value |
|---|---|
| Fleet discharge power, 1 Apr → 30 Jun | ≈370 → 860 MW |
| Day-ahead cleared volume, 18:00–23:00 (median) | 3,796 MW |
| Day-ahead cleared volume, 18:00–23:00 (10th percentile) | 1,449 MW |
| Fleet power as a share of evening day-ahead volume (median, quarter) | 20% |
| Fleet power as a share of evening day-ahead volume (median, June) | 23% |

- Selling the whole fleet into the day-ahead evening would have meant supplying about a
  fifth of cleared volume, and more on thin days.
- Many of those blocks clear at the cap with large unmet demand, so price impact may be
  modest there. On the remaining blocks it wouldn't be, and the ₹195 cr price-taker figure
  overstates what an exchange-only fleet of this size would earn.
- **Selling 85% through bilateral contracts is consistent with avoiding that price impact,**
  not only with seeking a better price.

## 5. Bottom line

- A price-taking battery trading only in the day-ahead market, with ACME's settings and
  capacity schedule, earns about **₹8,650 revenue and ₹7,300 after charging per MWh per day**.
  That's ₹195 cr of revenue for the quarter, about **14% below ACME's reported revenue** and
  ≈16% below its implied revenue minus charging.
- The realistic 7-day-average rule gets 99.6% of the day-ahead hindsight ceiling. Better
  trading wouldn't have closed the gap.
- The gap is too large to come from contract *prices* (₹8–10/unit is about what day-ahead
  paid). It most likely reflects **more delivered energy or capacity than assumed, or
  non-energy contract revenue**. Contracts also reduce the fleet's price impact, at about 20%
  of evening day-ahead volume.
- **Caveats:**
  - The 4.5-hour battery is simulated in 0.5% charge steps, so charge power comes out about
    5% below rated. That understates 4.5-hour revenue very slightly.
  - Fees are included only in the "after fees" columns of `results.csv`. All figures in this
    note are before exchange fees, to match "revenue minus charging".

# Baselines (draft for review)

Baselines are strategies that use **no forecasting model**. They give fixed reference
points, so any smarter strategy is measured as "how much better than X, and how close
to the ceiling". Every baseline runs on the same battery, costs and settlement rules
(section 1). Only the information each baseline uses is different.

## 0. How the baselines are organised

They form a ladder by **how fresh the information is**, run separately per market:

| Rung | Information used | Day-ahead | Real-time |
|---|---|---|---|
| 0 | none: the same schedule every day | DA-B0 | RT-B0 |
| 1 | stale: windows re-picked once a month | DA-B1 | RT-B1 |
| 2 | recent average: last 7 days | DA-B2 | RT-B2 |
| 3 | same weekday last week | DA-B3 | RT-B3 |
| 4 | most recent day | DA-B4 | RT-B4 |
| ceiling | perfect hindsight: the actual prices | DA-PH | RT-PH |
| ceiling | perfect hindsight, best market per block | X-PH | |

Rungs 2–4 run at 1 and at 2 cycles/day. Rungs 0–1 are 1 cycle only. The ceilings run at both.

**Reference baseline.** The baseline a new strategy must beat is the **best-performing
rung 2–4 baseline in the same market and cycle setting**. The prototype suggests this is
DA-B2 or DA-B4 for day-ahead. It's chosen once, from the baseline results, and then kept fixed.

## 1. Common setup (identical for every baseline)

### Battery (defaults from the design doc; all are parameters)
| Setting | Default |
|---|---|
| Power | 100 MW |
| Energy | 200 MWh (2-hour) |
| Round-trip efficiency | 87%, split evenly: 93.3% charging, 93.3% discharging |
| State-of-charge band | 10–90% (160 MWh usable) |
| Day start / end | 10% at 00:00 and back to 10% by 24:00, so each day is independent |
| Cycle limit | 1 or 2 per day, counted as discharged energy ≤ cycles × usable energy |
| Wear cost | ₹0 and ₹300 per MWh discharged (≈ ₹1.7M/MWh capex over ~6,000 cycles) |

### Costs and settlement
- Exchange fee: ₹20/MWh (2 paise/kWh), charged on both buying and selling.
- Transmission charges: ₹0 by default (co-located case). A standalone case comes later.
- Every block settles at the **actual clearing price** of the market traded.
- **No-trade or missing blocks:** orders there don't fill. Any later discharge that would
  need the missing energy is cut to what is actually stored, and the shortfall is logged.
- Participation limit: buy or sell at most 10% of the block's cleared volume. The amount
  cut is logged.
- Prices are used as published, never clipped.

### The optimiser (used by rungs 2–4 and the ceilings)
- Exact dynamic programming over state of charge in 1% steps, maximising profit for
  a given 96-block price profile.
- It respects power, the SoC band, the end-of-day level, the cycle limit, efficiency,
  fees and wear cost.
- It can take separate buy and sell price paths. X-PH needs this.
- Blocks marked unusable in the input profile get no trade.

### Information timing (enforced through `known_at`)
| Market | Decision time | May use |
|---|---|---|
| Day-ahead, delivery day D | 12:00 on D–1 | DAM rows with `known_at` < 12:00 D–1, i.e. DAM prices up to and including D–1 |
| Real-time, delivery day D | 22:00 on D–1 (one plan for the whole day) | RTM rows with `known_at` < 22:00 D–1, i.e. RTM up to block 88 of D–1 |

Real-time baselines plan once, the evening before. Re-planning during the day is the
first real-time *strategy*, not a baseline.

### Filling gaps in an input profile
If a profile block is missing or no-trade, fill it with the mean of the same block over
the last 7 days that do have it. If none have it, the block is not traded.

## 2. The baselines

### DA-B0 · Fixed windows
- **Rule:** charge 11:00–13:00 and discharge 18:00–20:00, every day, at the power needed
  for one full cycle.
- **Uses:** nothing.
- **Tests:** the "no software" floor from the design doc.

### DA-B1 · Monthly re-picked windows
- **Rule:** on the 1st of each month, take the block-wise average price profile of the
  previous 90 days. Find the cheapest 2-hour window and the most expensive later 2-hour
  window in it. Use those two windows every day of the month.
- **Uses:** prices that are up to 4 months old, refreshed monthly.
- **Tests:** how much of the value is just knowing the season's shape.

### DA-B2 · 7-day average profile
- **Rule:** the profile is the block-wise mean of the last 7 known days (D–1 … D–7). The
  optimiser plans on it.
- **Tests:** a smoothed recent shape. It is robust to one odd day.

### DA-B3 · Same weekday last week
- **Rule:** the profile is day D–7. The optimiser plans on it.
- **Tests:** whether weekday effects (Sundays, holidays) matter.

### DA-B4 · Yesterday
- **Rule:** the profile is day D–1. The optimiser plans on it.
- **Tests:** the freshest single day. In the prototype it captured about 95% of the 1-cycle ceiling.

### DA-PH · Perfect hindsight (ceiling)
- **Rule:** the optimiser plans on the actual prices of day D.
- **Tests:** the most any day-ahead strategy can earn with this battery and these costs.

### RT-B0 … RT-B4, RT-PH
These are the same rules applied to real-time prices, with the real-time decision time.
"Yesterday" (RT-B4) means the most recent known price for each block of the day: blocks
1–88 from D–1 and blocks 89–96 from D–2.

### X-PH · Best of both markets (ceiling)
- **Rule:** perfect hindsight where each block may buy at min(DAM, RTM) and sell at
  max(DAM, RTM).
- **Tests:** the most that using both markets could add. It's an upper bound, not
  achievable: real strategies must commit in day-ahead before seeing real-time.

## 3. What is reported for every baseline

- Profit in ₹ lakh per MW per year: gross, and after wear cost.
- **Capture** = profit ÷ the ceiling profit (same market, same cycle setting).
- Profit by year, by calendar month and by regime period (below).
- Share of losing days, worst day, worst month.
- Average cycles per day.
- Energy not filled (no-trade blocks) and energy cut by the participation limit.

### Regime periods (always reported separately)
| Period | Dates |
|---|---|
| ₹20k cap | 1–2 Apr 2022 (2 days; listed, not interpreted) |
| ₹12k cap | 3 Apr 2022 – 3 Apr 2023 |
| ₹10k cap | 4 Apr 2023 – 31 Dec 2025 |
| ₹10k cap + day-ahead market coupling | 1 Jan 2026 onward |

## 4. Checks the baselines must pass before any result is trusted
1. On every day, each ceiling ≥ every baseline in its market and cycle setting.
2. X-PH ≥ DA-PH and X-PH ≥ RT-PH on every day.
3. For the ceilings, a higher wear cost never increases cycles or profit. (Not required of
   forecast-based baselines: wear can make a plan more cautious and avoid forecast losses.)
4. Look-ahead test: corrupting prices after the decision time changes no baseline decision.
5. DA-B0 profit can be checked by hand on a sample day.
6. Results for the 2 cycles/day setting ≥ the 1-cycle results for the ceilings.

## 5. Open choices (defaults above apply unless changed)
- Battery size and duration of the target customer (default 100 MW / 200 MWh).
- Wear cost (default: report at ₹0 and ₹300/MWh).
- Whether to also run every baseline with charging allowed only 10:00–15:00 (the emerging
  grid-charging restriction). Recommended as a scenario from the start.

# Baseline results: recent windows (last 12 and 24 months)

Built on 2026-10-07 with `python bess.py baseline-report` from the stored daily results of
the full baseline run ([baseline_results.md](baseline_results.md)). Reports:
- `reports/baselines_last_1y/`: 1 Oct 2025 – 30 Sep 2026, 365 days
- `reports/baselines_last_2y/`: 1 Oct 2024 – 30 Sep 2026, 730 days

Each has `summary.html`, `summary.csv` and one folder per run. All 75 result checks pass in both.

**Why nothing was re-simulated.** Every baseline plans each day separately, starting and
ending at 10% charge, using only prices from before that day. A day's result is therefore
the same whatever window it's reported in. The window reports are exactly the stored days
in that window, re-aggregated.

Figures: ₹ lakh per MW per year, 100 MW / 200 MWh, wear ₹300/MWh, after fees.

## Profit by window

| Run | Full (May 2022 –) | Last 24 months | Last 12 months | Capture, last 12 months |
|---|---|---|---|---|
| DA-B0 fixed windows | 20.7 | 25.4 | 27.4 | 73% |
| DA-B1 monthly windows | 22.8 | 25.9 | 29.2 | 78% |
| DA-B3 last week | 30.1 | 33.2 | 34.7 | 93% |
| DA-B4 yesterday | 31.9 | 34.8 | 36.0 | 96% |
| **DA-B2 7-day average, 1 cycle** | **32.6** | **35.5** | **36.3** | **97%** |
| **DA-B2 7-day average, 2 cycles** | **39.5** | **43.0** | **43.7** | **95%** |
| DA ceiling, 1 cycle | 34.0 | 36.5 | 37.4 | |
| DA ceiling, 2 cycles | 41.9 | 45.0 | 46.0 | |
| RT-B2 7-day average, 1 cycle | 27.2 | 29.2 | 31.0 | 89% |
| RT-B2 7-day average, 2 cycles | 33.0 | 35.7 | 36.9 | 84% |
| RT ceiling, 2 cycles | 41.0 | 42.4 | 44.2 | |
| Best of both, hindsight, 2 cycles | 55.7 | 57.4 | 59.1 | |
| DA-B2, charging 10:00–15:00 only | 31.3 | 34.9 | 35.9 | 98% |

## What changes, and what doesn't

1. **Recent years are more profitable.** B2 day-ahead at 1 cycle earns 36.3 in the last 12
   months, against 32.6 over the full period (+11%). The ceilings rose by a similar amount
   (34.0 → 37.4). The market itself has paid more recently, as midday prices fell and the
   evening stayed near the cap. The strategies aren't doing anything different.
2. **The earlier years don't change the conclusions.**
   - The ranking of baselines is the same in every window: B2 > B4 > B3 > B1 > B0, in both
     markets.
   - B2 remains the reference baseline.
3. **Simple rules have improved most.**
   - Fixed windows rose from 61% to 73% of the ceiling as the midday-trough / evening-peak
     shape became more regular.
   - B2 still leaves only 3–5% of the day-ahead ceiling unclaimed.
4. **The big opportunity is still using both markets.** The gap between DA-B2 at 2 cycles and
   the best-of-both ceiling is 15.4 lakh/MW/yr in the last 12 months (43.7 vs 59.1), close to
   the full-period 16.2.
5. **The charging restriction still removes the second cycle** (43.7 → 35.9 for B2), but it
   costs less than before, because midday charging is now nearly always cheapest.
6. **No losing days in the last 12 months** for any day-ahead baseline. The worst months are
   February 2026 at 1 cycle and October 2025 at 2 cycles.

## Caution on short windows

- Twelve months is one cycle of seasons. It includes the first 9 months of day-ahead market
  coupling (from January 2026), which have been the most profitable on record.
- The strategy rankings are stable enough to trust. The profit level isn't: one more good or
  bad monsoon or winter could move it several lakh either way.
- For business cases, report full-period and last-12-month figures side by side rather than
  only the higher one.

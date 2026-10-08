# Baseline results by season

Built on 2026-10-07 from the stored baseline results. There's a season page in each report folder:
- `reports/baselines/seasons.html`: full period, May 2022 – Sep 2026
- `reports/baselines_last_2y/seasons.html`: Oct 2024 – Sep 2026
- `reports/baselines_last_1y/seasons.html`: Oct 2025 – Sep 2026

Each folder also has `seasons.csv`, and every run report now has a "By season" table.

**Seasons (IMD):**

| Season | Months | Days in the full period | Days in the last 12 months |
|---|---|---|---|
| Winter | Dec–Feb | 361 | 90 |
| Summer | Mar–May | 399 | 92 |
| Monsoon | Jun–Sep | 610 | 122 |
| Post-monsoon | Oct–Nov | 244 | 61 |

**Units:** average profit in ₹ thousand per MW per day. Multiply by 3.65 for ₹ lakh per MW
per year; for example, 9.0 is about 33 lakh/MW/yr. The battery is 100 MW / 200 MWh, with
wear at ₹300/MWh.

## Full period (May 2022 – Sep 2026)

| Run | Winter | Summer | Monsoon | Post-monsoon |
|---|---|---|---|---|
| DA-B0 fixed windows | 5.0 (53%) | 5.4 (62%) | 6.1 (61%) | 5.9 (72%) |
| **DA-B2 7-day avg, 1 cycle** | 9.0 (95%) | 8.2 (95%) | **9.8** (97%) | 7.9 (96%) |
| **DA-B2 7-day avg, 2 cycles** | **15.3** (97%) | 8.9 (92%) | 10.4 (93%) | 8.4 (93%) |
| RT-B2 7-day avg, 1 cycle | 8.1 (88%) | 6.7 (82%) | 7.9 (85%) | 6.6 (88%) |
| RT-B2 7-day avg, 2 cycles | 13.2 (91%) | 7.5 (73%) | 8.3 (76%) | 7.2 (82%) |
| Best of both, hindsight, 2 cycles | 17.5 | 14.8 | 15.6 | 11.7 |
| DA-B2, charging 10:00–15:00 only | 7.6 | 8.2 | 9.7 | 7.7 |

The percentage in brackets is capture of the ceiling in the same season.

## Last 12 months (Oct 2025 – Sep 2026)

| Run | Winter | Summer | Monsoon | Post-monsoon |
|---|---|---|---|---|
| DA-B0 fixed windows | 5.9 (69%) | 8.3 (79%) | 8.2 (70%) | 7.5 (78%) |
| **DA-B2 7-day avg, 1 cycle** | 8.3 (96%) | 10.2 (97%) | **11.3** (97%) | 9.3 (97%) |
| **DA-B2 7-day avg, 2 cycles** | **13.9** (99%) | 10.5 (92%) | 12.6 (93%) | 10.0 (96%) |
| RT-B2 7-day avg, 2 cycles | 12.5 (93%) | 7.9 (76%) | 10.5 (79%) | 9.0 (88%) |
| Best of both, hindsight, 2 cycles | 16.1 | 16.1 | 17.9 | 12.8 |
| DA-B2, charging 10:00–15:00 only | 7.8 | 10.2 | 11.3 | 9.3 |

## Day-ahead B2 by season and year (1 cycle / 2 cycles)

Winter is labelled by the year in which it ends; Dec 2024 – Feb 2025 is "2025".

| Season | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|
| Winter | – | 9.8 / 16.1 | 8.6 / 15.0 | 9.1 / 16.1 | 8.3 / 13.9 |
| Summer | 6.4 / 6.4 (May only) | 6.5 / 7.4 | 7.1 / 7.5 | 9.9 / 11.1 | 10.2 / 10.5 |
| Monsoon | 9.8 / 11.2 | 7.6 / 7.9 | 10.0 / 10.1 | 10.1 / 10.1 | 11.3 / 12.6 |
| Post-monsoon | 7.7 / 8.6 | 6.2 / 6.3 | 8.2 / 8.6 | 9.3 / 10.0 | – |

## What the seasons show

1. **Winter is the two-cycle season.**
   - In winter there's a morning peak as well as the evening one, so the battery can run a
     night-to-morning cycle as well as the midday-to-evening one.
   - Allowed 2 cycles, day-ahead B2 runs 1.99 cycles/day in winter, against 1.4–1.6 in
     other seasons.
   - The second cycle adds about 6.3 thousand/MW/day in winter (15.3 vs 9.0) and under 1
     thousand in every other season.
   - So almost all of the second cycle's value comes from December to February.
2. **The charging restriction mostly hurts in winter.** Charging only between 10:00 and 15:00
   rules out the night charge for the morning peak. Winter falls from 15.3 to 7.6, below even
   the 1-cycle figure (9.0). Other seasons barely change.
3. **The monsoon is the best 1-cycle season, and steady.** Cloudy days make the evening short of
   power, while midday prices stay low. Day-ahead B2 earns 9.8 over the full period and 11.3 in
   the last 12 months, at 97% capture.
4. **Summer has improved the most.** It was the weakest season in 2023–24 (about 6.5–7.1),
   because high daytime demand kept midday prices up. It has been strong since 2025
   (about 10). Midday prices in summer have fallen as solar capacity grew.
5. **Post-monsoon (Oct–Nov) is the weakest season** in most years.
6. **The real-time market is hardest to plan in summer and monsoon.**
   - RT-B2 at 2 cycles captures only 73–76% in those seasons, against about 90% in winter.
   - Those are also the seasons where the best-of-both ceiling is far above what the
     day-ahead baselines earn: about 5–6 thousand/MW/day.
   - So that's where a two-stage day-ahead + real-time strategy has the most to gain.
7. **Day-ahead B2 captures 92–99% in every season.** No season stands out as one where a
   better day-ahead forecast would pay off.

## What this means

- **Two cycles per day is effectively a winter strategy.** A battery limited to 1 cycle or to
  10:00–15:00 charging gives up about 6 thousand/MW/day for 3 months a year, roughly 5.5 lakh
  per MW per year.
- **The two-stage strategy (Phase 3) should be judged in summer and monsoon.** That's where the
  cross-market gap and the real-time planning shortfall are largest.
- **Seasonal swings set cash flow.** Day-ahead 1-cycle profit varies from about 6 to 11
  thousand/MW/day between seasons and years. Monthly financing and lease structures should
  allow for weak October–November and summer periods.

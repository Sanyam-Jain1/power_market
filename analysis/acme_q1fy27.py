"""Backtest IEX-only battery earnings for 1 Apr - 30 Jun 2026 against ACME's disclosed quarter.

Run from the project folder:  python3 analysis/acme_q1fy27.py
Writes reports/acme_q1fy27/ (results.csv, daily.csv, report.md).

ACME settings (as given): one cycle a day, grid charging, round trip 89%, 93% of capacity
used per cycle. Capacity (MWh) is read as energy deliverable at the grid, so each cycle
sells 0.93 MWh and buys 0.93 / 0.89 MWh per MWh of capacity. Fleet: about 80% 4.5-hour
and 20% 2-hour, weighted day by day with the commissioning schedule below.

Every result is per MWh of capacity, as a price-taker (no participation limit). Fleet
scale versus market depth is checked separately at the end.
"""
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bess  # noqa: E402

FIRST, LAST = "2026-04-01", "2026-06-30"
HISTORY_FROM = "2026-03-01"           # warm-up for the 7-day baselines
OUT = ROOT / "reports" / "acme_q1fy27"

# Capacity running from each date (MWh), as given.
SCHEDULE = [("2026-04-01", 1324), ("2026-04-03", 1485), ("2026-04-08", 1645), ("2026-04-10", 1806),
            ("2026-04-15", 2031), ("2026-05-01", 2192), ("2026-05-05", 2352), ("2026-05-13", 2673),
            ("2026-05-29", 2829), ("2026-06-03", 2989), ("2026-06-14", 3110)]
MIX = {4.5: 0.80, 2.0: 0.20}          # share of fleet MWh by duration (hours)

ACME = {"revenue_rs_cr": 226.0, "ebitda_rs_cr": 189.0, "rev_per_mwh_day": 10050, "ebitda_per_mwh_day": 8400,
        "may8_net_per_mwh_day": 9570}

RTE, DEPTH = 0.89, 0.93
RATING = 100.0                        # MWh of capacity simulated; results are divided by it


def battery(hours):
    eta = RTE ** 0.5
    # Internal energy so that stored energy x eta delivered = rating; 93% of it is used.
    return bess.Battery(power_mw=RATING / hours, energy_mwh=RATING / eta, rte=RTE,
                        soc_min=0.035, soc_max=0.965, soc_step=0.005, fee_rs_mwh=20.0,
                        wear_rs_mwh=0.0, participation=1e9, max_cycles=1)


def capacity_by_day(days):
    cap, out = 0, []
    sched = dict(SCHEDULE)
    for d in days:
        cap = sched.get(d, cap)
        out.append(cap)
    return np.array(out, dtype=float)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with bess.connect() as con:
        days_all, P, V = bess.load_market_arrays(con)
    i0, i1 = days_all.index(HISTORY_FROM), days_all.index(LAST) + 1
    days = days_all[i0:i1]
    P = {m: a[i0:i1] for m, a in P.items()}
    V = {m: a[i0:i1] for m, a in V.items()}
    window = np.array([FIRST <= d <= LAST for d in days])
    qdays = [d for d, w in zip(days, window) if w]
    cap = capacity_by_day(qdays)

    runs = [("DAM", "PH", "Day-ahead, perfect hindsight (ceiling)"),
            ("DAM", "B2", "Day-ahead, 7-day average then optimiser"),
            ("DAM", "B4", "Day-ahead, yesterday's prices then optimiser"),
            ("RTM", "PH", "Real-time, perfect hindsight"),
            ("RTM", "B2", "Real-time, 7-day average then optimiser"),
            ("BOTH", "PH", "Best of both markets per block, hindsight")]
    rows, daily_rows = [], []
    for market, strategy, label in runs:
        per = {}
        for hours in MIX:
            bat = battery(hours)
            res = bess.run_baseline(strategy, market, bat, days, P, V)
            # money and energy per MWh of capacity; cycles stay as they are
            per[hours] = {k: np.asarray(v)[window] / (1 if k == "cycles" else RATING) for k, v in res.items()}
        blend = {k: sum(MIX[h] * per[h][k] for h in MIX) for k in per[4.5]}
        for name, r in [("4.5h", per[4.5]), ("2h", per[2.0]), ("blend 80/20", blend)]:
            rev, chg, fees = r["sell_revenue_rs"], r["buy_cost_rs"], r["fees_rs"]
            rows.append({
                "market": market, "strategy": strategy, "label": label, "duration": name,
                "revenue_per_mwh_day": rev.mean(),
                "revenue_minus_charging_per_mwh_day": (rev - chg).mean(),
                "after_fees_per_mwh_day": (rev - chg - fees).mean(),
                "fleet_weighted_revenue_per_mwh_day": (rev * cap).sum() / cap.sum(),
                "fleet_weighted_net_per_mwh_day": ((rev - chg) * cap).sum() / cap.sum(),
                "quarter_revenue_rs_cr": (rev * cap).sum() / 1e7,
                "quarter_net_rs_cr": ((rev - chg) * cap).sum() / 1e7,
                "sold_mwh_per_mwh_day": r["sold_mwh"].mean(),
                "avg_sell_price": rev.sum() / r["sold_mwh"].sum(),
                "avg_buy_price": chg.sum() / r["bought_mwh"].sum(),
                "cycles_per_day": r["cycles"].mean(),
            })
        for j, d in enumerate(qdays):
            daily_rows.append({"date": d, "market": market, "strategy": strategy, "capacity_mwh": cap[j],
                               **{f"{h}h_revenue": per[h]["sell_revenue_rs"][j] for h in MIX},
                               **{f"{h}h_charging": per[h]["buy_cost_rs"][j] for h in MIX}})
    res = pd.DataFrame(rows)
    res.round(1).to_csv(OUT / "results.csv", index=False)
    pd.DataFrame(daily_rows).round(1).to_csv(OUT / "daily.csv", index=False)

    # Monthly view for the realistic day-ahead strategy, blended
    monthly = []
    for market, strategy in [("DAM", "B2"), ("DAM", "PH")]:
        dd = pd.DataFrame(daily_rows)
        dd = dd[(dd.market == market) & (dd.strategy == strategy)].copy()
        dd["rev"] = sum(MIX[h] * dd[f"{h}h_revenue"] for h in MIX)
        dd["net"] = dd["rev"] - sum(MIX[h] * dd[f"{h}h_charging"] for h in MIX)
        for month, g in dd.groupby(dd.date.str[:7]):
            monthly.append({"strategy": f"{market}-{strategy}", "month": month,
                            "revenue_per_mwh_day": g.rev.mean(), "net_per_mwh_day": g.net.mean(),
                            "avg_capacity_mwh": g.capacity_mwh.mean()})
    monthly = pd.DataFrame(monthly)
    monthly.round(0).to_csv(OUT / "monthly.csv", index=False)

    # Market depth: fleet power vs day-ahead cleared volume in the evening selling hours
    dam_v = V["DAM"][window]
    evening = dam_v[:, 72:92]            # 18:00-23:00
    fleet_mw = cap * sum(MIX[h] / h for h in MIX)
    share = fleet_mw[:, None] / evening
    depth = {"fleet_power_mw_start": fleet_mw[0], "fleet_power_mw_end": fleet_mw[-1],
             "evening_dam_mcv_median_mw": float(np.nanmedian(evening)),
             "evening_dam_mcv_p10_mw": float(np.nanpercentile(evening, 10)),
             "fleet_share_of_evening_mcv_median": float(np.nanmedian(share)),
             "fleet_share_of_evening_mcv_june_median": float(np.nanmedian(share[-30:]))}
    pd.Series(depth).round(3).to_csv(OUT / "market_depth.csv", header=["value"])

    pd.set_option("display.width", 250)
    print(res[res.duration == "blend 80/20"][["market", "strategy", "revenue_per_mwh_day",
          "revenue_minus_charging_per_mwh_day", "fleet_weighted_revenue_per_mwh_day",
          "fleet_weighted_net_per_mwh_day", "quarter_revenue_rs_cr", "quarter_net_rs_cr",
          "avg_sell_price", "avg_buy_price"]].round(0).to_string(index=False))
    print(res[res.duration != "blend 80/20"][["market", "strategy", "duration", "revenue_per_mwh_day",
          "revenue_minus_charging_per_mwh_day", "sold_mwh_per_mwh_day", "avg_sell_price",
          "avg_buy_price", "cycles_per_day"]].round(2).to_string(index=False))
    print(monthly.round(0).to_string(index=False))
    print(pd.Series(depth).round(3).to_string())
    print("capacity-days:", cap.sum(), "avg capacity", round(cap.mean()))


if __name__ == "__main__":
    main()

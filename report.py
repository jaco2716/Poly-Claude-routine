"""
Stats summary over the resolved-trade ledger.

Computes:
  - Cumulative P&L, win rate, hit rate by category
  - Brier score (calibration of claude_prob vs realised outcome)
  - Calibration buckets — when Claude said X%, what % actually paid out?
  - Edge realised vs predicted

Usage:
    python report.py
    python report.py --json   # machine-readable
"""
import argparse
import json
import os
from collections import defaultdict
from statistics import mean

import ledger

STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "1000"))


def _outcome_int(r: dict) -> int:
    """1 if YES resolved, 0 if NO resolved — independent of the bet direction."""
    return 1 if r["outcome"] == "yes" else 0


def _brier(records: list[dict]) -> float | None:
    """Mean squared error between Claude's claimed P(YES) and the actual binary outcome."""
    if not records:
        return None
    return mean((r["claude_prob"] - _outcome_int(r)) ** 2 for r in records)


def _market_brier(records: list[dict]) -> float | None:
    """Same metric for the market-implied probability — apples-to-apples baseline.

    market-implied P(YES) is recoverable from entry_price + direction.
    """
    if not records:
        return None
    vals = []
    for r in records:
        market_yes = r["entry_price"] if r["direction"] == "yes" else 1 - r["entry_price"]
        vals.append((market_yes - _outcome_int(r)) ** 2)
    return mean(vals)


def _calibration_buckets(records: list[dict]) -> list[dict]:
    """Bucket trades by claude_prob in 10% bins, report actual YES rate per bin."""
    buckets = defaultdict(list)
    for r in records:
        bucket = int(r["claude_prob"] * 10) / 10  # 0.0, 0.1, ..., 0.9
        if bucket >= 1.0:
            bucket = 0.9
        buckets[bucket].append(r)
    out = []
    for low in sorted(buckets):
        rs = buckets[low]
        actual_yes = sum(_outcome_int(r) for r in rs) / len(rs)
        out.append({
            "claude_prob_bucket": f"{low:.1f}–{low + 0.1:.1f}",
            "n":                  len(rs),
            "claude_avg":         round(mean(r["claude_prob"] for r in rs), 3),
            "actual_yes_rate":    round(actual_yes, 3),
            "well_calibrated":    abs(mean(r["claude_prob"] for r in rs) - actual_yes) < 0.1,
        })
    return out


def compute() -> dict:
    open_trades = ledger.currently_open()
    resolved = ledger.load_resolved()

    n_open = len(open_trades)
    n_resolved = len(resolved)
    total_pnl = sum(r["pnl_usdc"] for r in resolved)
    locked = sum(t["size_usdc"] for t in open_trades)
    realised_bankroll = STARTING_BANKROLL + total_pnl

    wins = [r for r in resolved if r["won"]]
    losses = [r for r in resolved if not r["won"]]

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in resolved:
        by_cat[r.get("category") or "unknown"].append(r)

    cat_stats = []
    for cat, rs in sorted(by_cat.items()):
        cat_pnl = sum(r["pnl_usdc"] for r in rs)
        cat_wins = sum(1 for r in rs if r["won"])
        cat_stats.append({
            "category":  cat,
            "n":         len(rs),
            "win_rate":  round(cat_wins / len(rs), 3),
            "pnl_usdc":  round(cat_pnl, 2),
            "avg_pnl":   round(cat_pnl / len(rs), 2),
        })

    return {
        "starting_bankroll": STARTING_BANKROLL,
        "open_positions": n_open,
        "capital_locked":  round(locked, 2),
        "resolved_trades": n_resolved,
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(len(wins) / n_resolved, 3) if n_resolved else None,
        "total_pnl":       round(total_pnl, 2),
        "realised_bankroll": round(realised_bankroll, 2),
        "return_pct":      round(total_pnl / STARTING_BANKROLL, 4) if STARTING_BANKROLL else None,
        "avg_win":         round(mean(r["pnl_usdc"] for r in wins), 2) if wins else None,
        "avg_loss":        round(mean(r["pnl_usdc"] for r in losses), 2) if losses else None,
        "brier_claude":    round(_brier(resolved), 4) if resolved else None,
        "brier_market":    round(_market_brier(resolved), 4) if resolved else None,
        "by_category":     cat_stats,
        "calibration":     _calibration_buckets(resolved),
    }


def _print_human(s: dict) -> None:
    print(f"\nPaper-trading ledger — {s['resolved_trades']} resolved, {s['open_positions']} open\n")
    print(f"  Bankroll: ${s['starting_bankroll']:.2f} → ${s['realised_bankroll']:.2f}  "
          f"({s['return_pct'] * 100 if s['return_pct'] is not None else 0:+.1f}%)")
    print(f"  Capital locked in open trades: ${s['capital_locked']:.2f}")
    if s['resolved_trades']:
        print(f"  W/L: {s['wins']}/{s['losses']}  win rate {s['win_rate']:.1%}  "
              f"avg win ${s['avg_win']}  avg loss ${s['avg_loss']}")
        print(f"  Brier score — Claude: {s['brier_claude']}   Market: {s['brier_market']}")
        if s['brier_claude'] is not None and s['brier_market'] is not None:
            verdict = "Claude beats market" if s['brier_claude'] < s['brier_market'] else "Market beats Claude"
            print(f"    → {verdict} (lower is better)")

    if s['by_category']:
        print("\n  By category:")
        for c in s['by_category']:
            print(f"    {c['category']:14s}  n={c['n']:3d}  "
                  f"win_rate={c['win_rate']:.1%}  P&L ${c['pnl_usdc']:+.2f}  "
                  f"avg ${c['avg_pnl']:+.2f}")

    if s['calibration']:
        print("\n  Calibration (Claude said X% → Y% actually resolved YES):")
        for b in s['calibration']:
            mark = "✓" if b['well_calibrated'] else "✗"
            print(f"    {mark} {b['claude_prob_bucket']}   n={b['n']:3d}  "
                  f"claude_avg={b['claude_avg']:.2f}  actual={b['actual_yes_rate']:.2f}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args()
    s = compute()
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        _print_human(s)


if __name__ == "__main__":
    main()

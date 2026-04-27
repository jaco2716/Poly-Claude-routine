"""
Per-category performance analyzer for the routine.

Walks reports/ and trades/ to surface where the routine actually finds edges
and makes (or loses) money. Use this to decide whether crypto_price markets
are worth their slot or should be capped.

Usage:
    python analyze_performance.py
    python analyze_performance.py --since 2026-04-01
"""
import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import ledger

REPORTS_DIR = Path(__file__).parent / "reports"


def _load_reports(since: datetime | None) -> list[dict]:
    out = []
    for p in sorted(REPORTS_DIR.glob("*.json")):
        try:
            r = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if since:
            try:
                gen = datetime.fromisoformat(r["generated_at"].replace("Z", "+00:00"))
                if gen.replace(tzinfo=None) < since:
                    continue
            except (KeyError, ValueError):
                pass
        out.append(r)
    return out


def _category_of_report(r: dict) -> str:
    return (r.get("market") or {}).get("category") or "unknown"


def _category_of_trade(t: dict) -> str:
    return t.get("category") or "unknown"


def _bucket(price: float) -> str:
    if price < 0.10: return "<0.10"
    if price < 0.20: return "0.10-0.20"
    if price < 0.40: return "0.20-0.40"
    if price < 0.60: return "0.40-0.60"
    if price < 0.80: return "0.60-0.80"
    if price < 0.90: return "0.80-0.90"
    return ">=0.90"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None,
                        help="Only include reports generated on/after this date (YYYY-MM-DD)")
    args = parser.parse_args()

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)

    reports = _load_reports(since)
    open_trades = ledger.load_open()
    resolved = ledger.load_resolved()

    if not reports:
        print("No reports found.")
        return

    by_cat = defaultdict(lambda: {
        "markets_seen": 0,
        "edges": [],          # |delta|
        "confidences": [],
        "had_direction": 0,   # |delta| >= 0.05 in _build_report
        "yes_prices": [],
    })
    by_bucket = defaultdict(lambda: {"seen": 0, "had_direction": 0, "edges": []})

    for r in reports:
        cat = _category_of_report(r)
        m = r.get("market", {})
        a = r.get("assessment", {})
        e = r.get("edge", {})
        s = by_cat[cat]
        s["markets_seen"] += 1
        if e.get("delta") is not None:
            s["edges"].append(abs(e["delta"]))
            if e.get("suggested_direction"):
                s["had_direction"] += 1
        if a.get("confidence") is not None:
            s["confidences"].append(a["confidence"])
        yp = m.get("yes_price")
        if yp is not None:
            s["yes_prices"].append(yp)
            b = by_bucket[_bucket(yp)]
            b["seen"] += 1
            if e.get("delta") is not None:
                b["edges"].append(abs(e["delta"]))
                if e.get("suggested_direction"):
                    b["had_direction"] += 1

    trades_by_cat = defaultdict(lambda: {"opened": 0, "open_now": 0})
    for t in open_trades:
        trades_by_cat[_category_of_trade(t)]["opened"] += 1
    open_ids = {t["trade_id"] for t in ledger.currently_open()}
    for t in open_trades:
        if t["trade_id"] in open_ids:
            trades_by_cat[_category_of_trade(t)]["open_now"] += 1

    pnl_by_cat = defaultdict(lambda: {"resolved": 0, "wins": 0, "pnl": 0.0, "pnl_net": 0.0})
    for r in resolved:
        c = pnl_by_cat[_category_of_trade(r)]
        c["resolved"] += 1
        c["wins"] += 1 if r.get("won") else 0
        pnl = r.get("pnl_usdc", 0.0)
        c["pnl"] += pnl
        # Fee = 2% of the winning $1/share payout, applied only on wins.
        if r.get("won") and r.get("entry_price"):
            shares = r.get("size_usdc", 0.0) / r["entry_price"]
            c["pnl_net"] += pnl - 0.02 * shares
        else:
            c["pnl_net"] += pnl

    print(f"\n{'='*100}")
    print(f"REPORTS ANALYZED: {len(reports)}    OPEN TRADES: {len(open_ids)}    RESOLVED: {len(resolved)}")
    if since:
        print(f"Filter: since {args.since}")
    print(f"{'='*100}\n")

    print("PER-CATEGORY (research funnel)")
    print(f"  {'category':<18}{'seen':>6}{'avg|Δ|':>10}{'avg conf':>10}{'%dir':>8}{'opened':>8}{'open':>6}{'resolved':>10}{'wins':>6}{'P&L':>10}{'P&L net':>10}")
    print(f"  {'-'*102}")
    cats = sorted(by_cat.keys(), key=lambda c: -by_cat[c]["markets_seen"])
    for cat in cats:
        s = by_cat[cat]
        t = trades_by_cat.get(cat, {"opened": 0, "open_now": 0})
        p = pnl_by_cat.get(cat, {"resolved": 0, "wins": 0, "pnl": 0.0, "pnl_net": 0.0})
        avg_e = statistics.mean(s["edges"]) if s["edges"] else 0.0
        avg_c = statistics.mean(s["confidences"]) if s["confidences"] else 0.0
        dir_pct = (100 * s["had_direction"] / s["markets_seen"]) if s["markets_seen"] else 0
        print(f"  {cat:<18}{s['markets_seen']:>6}{avg_e:>10.3f}{avg_c:>10.2f}{dir_pct:>7.0f}%"
              f"{t['opened']:>8}{t['open_now']:>6}{p['resolved']:>10}{p['wins']:>6}{p['pnl']:>+10.2f}{p['pnl_net']:>+10.2f}")

    print("\nPER-PRICE-BUCKET (where do the edges live?)")
    print(f"  {'bucket':<14}{'seen':>6}{'%dir':>8}{'avg|Δ|':>10}")
    print(f"  {'-'*40}")
    for b in ["<0.10", "0.10-0.20", "0.20-0.40", "0.40-0.60", "0.60-0.80", "0.80-0.90", ">=0.90"]:
        if b not in by_bucket:
            continue
        d = by_bucket[b]
        avg_e = statistics.mean(d["edges"]) if d["edges"] else 0.0
        dir_pct = (100 * d["had_direction"] / d["seen"]) if d["seen"] else 0
        print(f"  {b:<14}{d['seen']:>6}{dir_pct:>7.0f}%{avg_e:>10.3f}")

    print("\nLEGEND")
    print("  seen         markets researched in this category")
    print("  avg|Δ|       mean absolute distance between Claude's prob and market price")
    print("  %dir         share of markets where |Δ| ≥ 0.05 (a directional edge candidate)")
    print("  opened       paper trades opened in this category (all-time)")
    print("  open         currently unresolved")
    print("  resolved     trades that have a final outcome recorded")
    print("  wins / P&L   resolved hit-rate and net USDC")
    print("  P&L net      P&L after a 2% fee on winning payouts (loss legs unchanged)")
    print()


if __name__ == "__main__":
    main()

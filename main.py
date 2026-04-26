"""
Orchestrator: fetch active markets → filter to verifiable categories →
ask Claude for an independent probability → write a JSON report per market →
open paper trades when edge + confidence + liquidity thresholds pass.

No real trades. The paper ledger lives in trades/*.jsonl.

Usage:
    python main.py                    # default: analyse 5 markets, may open trades
    python main.py --limit 10
    python main.py --category weather
    python main.py --no-trade         # analysis only, don't open positions
"""
import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from polymarket_api import fetch_active_markets
from market_filter import filter_weather_and_commodity
from claude_analyst import analyse, build_report
import ledger

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

REPORTS_DIR = Path(__file__).parent / "reports"

# Trade trigger thresholds — tune these as you collect data.
EDGE_THRESHOLD       = float(os.getenv("EDGE_THRESHOLD",       "0.07"))   # |claude - market| ≥ 7%
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))   # Claude self-confidence ≥ 65%
MIN_LIQUIDITY        = float(os.getenv("MIN_LIQUIDITY",        "10000")) # $10k book minimum
STARTING_BANKROLL    = float(os.getenv("STARTING_BANKROLL",    "1000"))   # paper $
TRADE_SIZE_PCT       = float(os.getenv("TRADE_SIZE_PCT",       "0.02"))   # 2% per trade
MIN_TRADE_USDC       = float(os.getenv("MIN_TRADE_USDC",       "5"))


def _slug(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:max_len] or "market"


def _save_report(report: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{ts}_{_slug(report['market']['question'])}.json"
    path = REPORTS_DIR / name
    with path.open("w") as f:
        json.dump(report, f, indent=2)
    return path


def _should_trade(report: dict) -> tuple[bool, str]:
    """Decide if the report meets all trigger thresholds. Returns (trade?, reason)."""
    a = report["assessment"]
    e = report["edge"]
    m = report["market"]
    if not a.get("verifiable"):
        return False, "marked unverifiable by Claude"
    if e["delta"] is None or e["suggested_direction"] is None:
        return False, "no directional edge"
    if abs(e["delta"]) < EDGE_THRESHOLD:
        return False, f"edge {e['delta']:+.3f} < {EDGE_THRESHOLD}"
    if (a["confidence"] or 0) < CONFIDENCE_THRESHOLD:
        return False, f"confidence {a['confidence']:.2f} < {CONFIDENCE_THRESHOLD}"
    if (m["liquidity"] or 0) < MIN_LIQUIDITY:
        return False, f"liquidity ${m['liquidity']:,.0f} < ${MIN_LIQUIDITY:,.0f}"
    if ledger.has_open_position(m["condition_id"] or m["id"]):
        return False, "already have an open position on this market"
    return True, "all thresholds met"


def _open_trade(market: dict, report: dict) -> dict | None:
    bankroll = ledger.bankroll_realised(STARTING_BANKROLL) - ledger.capital_locked()
    if bankroll < MIN_TRADE_USDC:
        log.warning(f"Available bankroll ${bankroll:.2f} below minimum trade size — skipping")
        return None
    size = max(MIN_TRADE_USDC, round(bankroll * TRADE_SIZE_PCT, 2))
    size = min(size, bankroll)

    a = report["assessment"]
    e = report["edge"]
    direction = e["suggested_direction"]
    entry_price = market["yes_price"] if direction == "yes" else market["no_price"]

    rec = ledger.record_open(
        market=market,
        direction=direction,
        entry_price=entry_price,
        size_usdc=size,
        claude_prob=a["probability_yes"],
        confidence=a["confidence"],
        edge=e["delta"],
        reasoning=a["reasoning"],
        data_sources=a.get("data_sources", []),
        key_uncertainty=a.get("key_uncertainty", ""),
        model=a.get("model", ""),
    )
    return rec


def run(limit: int, pool: int, category: str | None, allow_trade: bool) -> tuple[list[dict], list[dict]]:
    log.info(f"Fetching up to {pool} active markets per ordering from Gamma API...")
    seen: dict[str, dict] = {}
    for order in ("volume24hr", "liquidity", "endDate"):
        for m in fetch_active_markets(limit=pool, order=order):
            if m["id"] not in seen:
                seen[m["id"]] = m
    raw = list(seen.values())
    log.info(f"Got {len(raw)} unique markets across orderings")

    candidates = filter_weather_and_commodity(raw)
    if category:
        candidates = [m for m in candidates if m["_category"] == category]
    log.info(f"After filtering to objectively verifiable: {len(candidates)}")

    candidates = [m for m in candidates if 0.05 <= m["yes_price"] <= 0.95]
    candidates.sort(key=lambda m: -m.get("liquidity", 0))

    if not candidates:
        log.error("No verifiable markets found in the candidate pool")
        return [], []

    targets = candidates[:limit]
    log.info(f"Analysing {len(targets)} markets:")
    for m in targets:
        log.info(f"  [{m['_category']:14s}] {m['question'][:80]}")

    open_count = len(ledger.currently_open())
    locked = ledger.capital_locked()
    realised = ledger.bankroll_realised(STARTING_BANKROLL) - STARTING_BANKROLL
    log.info(
        f"Ledger state: {open_count} open positions, "
        f"${locked:.2f} locked, realised P&L ${realised:+.2f}"
    )

    reports, trades = [], []
    for i, market in enumerate(targets, 1):
        log.info(f"\n[{i}/{len(targets)}] {market['question'][:90]}")
        log.info(f"  yes_price={market['yes_price']:.3f}  liquidity=${market['liquidity']:,.0f}")
        if i > 1:
            time.sleep(20)  # rate-limit guard for Claude web_search
        assessment = analyse(market)
        if assessment is None:
            log.warning("  → analysis failed, skipping")
            continue
        report = build_report(market, assessment)
        path = _save_report(report)
        reports.append(report)
        a = report["assessment"]
        e = report["edge"]
        log.info(
            f"  → claude_prob={a['probability_yes']}  conf={a['confidence']}  "
            f"delta={e['delta']:+.3f}  → {path.name}"
        )

        will_trade, reason = _should_trade(report)
        if not allow_trade:
            log.info(f"  trade gate: skipped (--no-trade)")
            continue
        if not will_trade:
            log.info(f"  trade gate: NO ({reason})")
            continue
        rec = _open_trade(market, report)
        if rec:
            trades.append(rec)
            log.info(
                f"  📝 PAPER TRADE OPENED  trade_id={rec['trade_id']}  "
                f"{rec['direction'].upper()} @ {rec['entry_price']:.3f}  size=${rec['size_usdc']}"
            )
    return reports, trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5, help="markets to analyse")
    parser.add_argument("--pool",  type=int, default=200, help="candidate pool size per ordering")
    parser.add_argument("--category", default=None,
                        help="restrict to one of: weather, temperature, commodity, crypto_price, equity_index")
    parser.add_argument("--no-trade", action="store_true",
                        help="analysis only — do not open paper trades")
    args = parser.parse_args()
    reports, trades = run(args.limit, args.pool, args.category, allow_trade=not args.no_trade)
    print(f"\n{'─' * 70}")
    print(f"Wrote {len(reports)} report(s) to {REPORTS_DIR}")
    print(f"Opened {len(trades)} paper trade(s)")
    if reports:
        print("\nSummary:")
        for r in reports:
            m = r["market"]
            a = r["assessment"]
            e = r["edge"]
            print(
                f"  [{m['category']:14s}] {m['question'][:55]:55s}  "
                f"market={m['yes_price']:.2f}  claude={a['probability_yes']:.2f}  "
                f"Δ={e['delta']:+.2f}  conf={a['confidence']:.2f}"
            )


if __name__ == "__main__":
    main()

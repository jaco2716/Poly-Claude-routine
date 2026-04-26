"""
Apply trade gates and record paper positions.

Input: a JSON array of {market: {...}, assessment: {...}} produced by the
Claude Routine agent after analysing each candidate from fetch_markets.py.

Reads from stdin by default, or from --input <file>.

Schema (per item):
{
  "market": { ...fields from fetch_markets.py output... },
  "assessment": {
    "verifiable":      true,
    "probability_yes": 0.42,
    "confidence":      0.72,
    "reasoning":       "...",
    "data_sources":    ["...","..."],
    "key_uncertainty": "..."
  }
}

Writes:
  - One reports/<ts>_<slug>.json per market (full audit record)
  - One line in trades/open.jsonl per opened paper trade
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import ledger

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("record_trades")

REPORTS_DIR = Path(__file__).parent / "reports"

EDGE_THRESHOLD       = float(os.getenv("EDGE_THRESHOLD",       "0.07"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))
MIN_LIQUIDITY        = float(os.getenv("MIN_LIQUIDITY",        "10000"))
STARTING_BANKROLL    = float(os.getenv("STARTING_BANKROLL",    "1000"))
TRADE_SIZE_PCT       = float(os.getenv("TRADE_SIZE_PCT",       "0.02"))
MIN_TRADE_USDC       = float(os.getenv("MIN_TRADE_USDC",       "5"))


def _slug(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:max_len] or "market"


def _build_report(market: dict, assessment: dict) -> dict:
    yes_price = market.get("yes_price", 0.0)
    prob = assessment.get("probability_yes")
    delta = round(prob - yes_price, 4) if prob is not None else None
    direction = None
    if delta is not None and abs(delta) >= 0.05:
        direction = "yes" if delta > 0 else "no"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": {
            "id":           market.get("market_id") or market.get("id"),
            "condition_id": market.get("condition_id"),
            "question":     market.get("question"),
            "category":     market.get("category"),
            "slug":         market.get("slug"),
            "end_date":     market.get("end_date"),
            "yes_price":    yes_price,
            "no_price":     market.get("no_price"),
            "liquidity":    market.get("liquidity"),
            "volume_24h":   market.get("volume_24h"),
        },
        "assessment": {
            "verifiable":      bool(assessment.get("verifiable", True)),
            "probability_yes": prob,
            "confidence":      assessment.get("confidence"),
            "reasoning":       assessment.get("reasoning"),
            "data_sources":    assessment.get("data_sources", []),
            "key_uncertainty": assessment.get("key_uncertainty"),
            "model":           assessment.get("model", "claude-routine-agent"),
        },
        "edge": {
            "market_implied_prob": yes_price,
            "claude_prob":         prob,
            "delta":               delta,
            "suggested_direction": direction,
        },
    }


def _save_report(report: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{ts}_{_slug(report['market']['question'])}.json"
    path = REPORTS_DIR / name
    with path.open("w") as f:
        json.dump(report, f, indent=2)
    return path


def _should_trade(report: dict) -> tuple[bool, str]:
    a = report["assessment"]
    e = report["edge"]
    m = report["market"]
    cid = m.get("condition_id") or m.get("id")
    if not a.get("verifiable"):
        return False, "marked unverifiable"
    if e["delta"] is None or e["suggested_direction"] is None:
        return False, "no directional edge"
    if abs(e["delta"]) < EDGE_THRESHOLD:
        return False, f"edge {e['delta']:+.3f} < {EDGE_THRESHOLD}"
    if (a["confidence"] or 0) < CONFIDENCE_THRESHOLD:
        return False, f"confidence {a['confidence']:.2f} < {CONFIDENCE_THRESHOLD}"
    if (m["liquidity"] or 0) < MIN_LIQUIDITY:
        return False, f"liquidity ${m['liquidity']:,.0f} < ${MIN_LIQUIDITY:,.0f}"
    if cid and ledger.has_open_position(cid):
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

    return ledger.record_open(
        market=market,
        direction=direction,
        entry_price=entry_price,
        size_usdc=size,
        claude_prob=a["probability_yes"],
        confidence=a["confidence"],
        edge=e["delta"],
        reasoning=a["reasoning"] or "",
        data_sources=a.get("data_sources", []),
        key_uncertainty=a.get("key_uncertainty") or "",
        model=a.get("model", "claude-routine-agent"),
        mode="paper",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None,
                        help="path to JSON file with analyses (default: read stdin)")
    parser.add_argument("--no-trade", action="store_true",
                        help="write reports but don't open positions")
    args = parser.parse_args()

    raw = Path(args.input).read_text() if args.input else sys.stdin.read()
    if not raw.strip():
        log.error("No input provided")
        sys.exit(2)
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as ex:
        log.error(f"Input is not valid JSON: {ex}")
        sys.exit(2)
    if not isinstance(items, list):
        log.error("Input must be a JSON array")
        sys.exit(2)

    open_count = len(ledger.currently_open())
    locked = ledger.capital_locked()
    realised = ledger.bankroll_realised(STARTING_BANKROLL) - STARTING_BANKROLL
    log.info(
        f"Ledger state: {open_count} open, ${locked:.2f} locked, realised ${realised:+.2f}"
    )

    reports, trades, skipped = [], [], []
    for item in items:
        if "market" not in item or "assessment" not in item:
            log.warning(f"Skipping malformed item (missing market or assessment): {str(item)[:120]}")
            continue
        market = item["market"]
        assessment = item["assessment"]
        report = _build_report(market, assessment)
        path = _save_report(report)
        reports.append(report)

        a = report["assessment"]
        e = report["edge"]
        delta_s = f"{e['delta']:+.2f}" if e["delta"] is not None else "  N/A"
        prob_s  = f"{a['probability_yes']:.2f}" if a["probability_yes"] is not None else "N/A"
        conf_s  = f"{a['confidence']:.2f}" if a["confidence"] is not None else "N/A"
        log.info(
            f"  {market.get('question','')[:60]:60s}  "
            f"market={market.get('yes_price', 0):.2f} claude={prob_s} "
            f"Δ={delta_s} conf={conf_s}  → {path.name}"
        )

        if args.no_trade:
            skipped.append("--no-trade")
            continue
        will, reason = _should_trade(report)
        if not will:
            skipped.append(reason)
            log.info(f"    gate: NO ({reason})")
            continue
        rec = _open_trade(market, report)
        if rec:
            trades.append(rec)
            log.info(
                f"    📝 PAPER TRADE  trade_id={rec['trade_id']}  "
                f"{rec['direction'].upper()} @ {rec['entry_price']:.3f}  ${rec['size_usdc']}"
            )

    print(f"\n{'─' * 70}")
    print(f"Wrote {len(reports)} report(s) to {REPORTS_DIR}")
    print(f"Opened {len(trades)} paper trade(s); skipped {len(skipped)}")


if __name__ == "__main__":
    main()

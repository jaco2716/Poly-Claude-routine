"""
Settlement loop. For every currently-open paper trade:
  - look up the market via CLOB (condition_id) or Gamma (numeric id)
  - if it's closed/resolved, determine winner ('yes' or 'no')
  - append a resolution row to trades/resolved.jsonl with P&L

Idempotent and append-only: re-running won't double-resolve a trade
because currently_open() filters by trade_ids already in resolved.jsonl.

Usage:
    python resolve.py
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from polymarket_api import _get, CLOB_URL, GAMMA_URL
import ledger

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("resolve")


def get_market_winner(market_id: str, condition_id: Optional[str]) -> Optional[str]:
    """Return 'yes', 'no', or None if not yet resolved.

    Numeric ids → Gamma API. 0x... hashes → CLOB API.
    Falls back to CLOB by condition_id if Gamma is ambiguous.
    """
    cid = condition_id or market_id
    is_hash = str(cid).startswith("0x")

    if is_hash:
        return _winner_from_clob(cid)

    data = _get(f"{GAMMA_URL}/markets/{market_id}")
    if not data:
        return None
    m = data[0] if isinstance(data, list) else data
    if not (m.get("closed") or m.get("resolved")):
        return None
    if m.get("winner"):
        return str(m["winner"]).lower()
    res = m.get("resolution")
    if res is not None:
        s = str(res).lower()
        if s in ("1", "true", "yes"):
            return "yes"
        if s in ("0", "false", "no"):
            return "no"
    # Final outcome prices ≈ 1.0 are also a resolution signal
    prices = m.get("outcomePrices")
    if prices and len(prices) >= 2:
        try:
            yes_p, no_p = float(prices[0]), float(prices[1])
            if yes_p >= 0.99:
                return "yes"
            if no_p >= 0.99:
                return "no"
        except (ValueError, TypeError):
            pass
    cid2 = m.get("conditionId") or m.get("condition_id")
    if cid2:
        return _winner_from_clob(cid2)
    return None


def _winner_from_clob(condition_id: str) -> Optional[str]:
    data = _get(f"{CLOB_URL}/markets/{condition_id}")
    if not data or not isinstance(data, dict) or not data.get("closed"):
        return None
    tokens = data.get("tokens") or []
    for token in tokens:
        if token.get("winner"):
            outcome = str(token.get("outcome", "")).lower()
            if outcome in ("yes", "no"):
                return outcome
            return "yes" if tokens.index(token) == 0 else "no"
    return None


def main() -> int:
    open_trades = ledger.currently_open()
    if not open_trades:
        log.info("No open paper trades to resolve.")
        return 0

    log.info(f"Checking {len(open_trades)} open trades for settlement...")
    resolved_count = 0
    total_pnl = 0.0

    for t in open_trades:
        winner = get_market_winner(t.get("market_id"), t.get("condition_id"))
        if winner is None:
            log.debug(f"  still open: {t['question'][:70]}")
            continue
        rec = ledger.record_resolution(
            trade=t,
            outcome=winner,
            resolved_at=datetime.now(timezone.utc).isoformat(),
        )
        resolved_count += 1
        total_pnl += rec["pnl_usdc"]
        result = "WON" if rec["won"] else "LOST"
        log.info(
            f"  {result:4s}  {t['direction'].upper()} @ {t['entry_price']:.3f}  "
            f"outcome={winner.upper()}  P&L ${rec['pnl_usdc']:+.2f}  "
            f"({t['question'][:60]})"
        )

    realised_total = sum(r["pnl_usdc"] for r in ledger.load_resolved())
    log.info(
        f"\nSettled {resolved_count} trade(s) this run, "
        f"P&L ${total_pnl:+.2f}  |  cumulative realised P&L ${realised_total:+.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Fetch and filter Polymarket candidates. Pure data, NO analysis.

The Claude Routine agent calls this, reads the JSON it prints, and does
the actual probability assessment itself using its own web search and
reasoning (which bills against the user's Claude Pro plan, not the API).

Usage:
    python fetch_markets.py                    # 5 candidates as JSON to stdout
    python fetch_markets.py --limit 20
    python fetch_markets.py --category weather
    python fetch_markets.py --pool 1000
    python fetch_markets.py --skip-open        # exclude markets we already hold
"""
import argparse
import json
import logging
import os
import sys

from polymarket_api import fetch_active_markets
from market_filter import filter_weather_and_commodity
import ledger

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "WARNING").upper(),  # quiet by default — JSON goes to stdout
    stream=sys.stderr,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_markets")


PRICE_MIN = float(os.getenv("PRICE_MIN", "0.15"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "0.85"))
CATEGORY_CAP = int(os.getenv("CATEGORY_CAP", "6"))


def _diversify(candidates: list[dict], limit: int, cap: int) -> list[dict]:
    """Take up to `cap` per category in the input order, then top up from leftovers
    if we're short of `limit`. Input is assumed pre-sorted by desirability."""
    picked: list[dict] = []
    leftover: list[dict] = []
    counts: dict[str, int] = {}
    for m in candidates:
        cat = m.get("_category") or "unknown"
        if counts.get(cat, 0) < cap:
            picked.append(m)
            counts[cat] = counts.get(cat, 0) + 1
            if len(picked) >= limit:
                return picked
        else:
            leftover.append(m)
    for m in leftover:
        if len(picked) >= limit:
            break
        picked.append(m)
    return picked


def fetch_candidates(limit: int, pool: int, category: str | None, skip_open: bool) -> list[dict]:
    seen: dict[str, dict] = {}
    for order in ("volume24hr", "liquidity", "endDate"):
        for m in fetch_active_markets(limit=pool, order=order):
            if m["id"] not in seen:
                seen[m["id"]] = m
    raw = list(seen.values())
    log.info(f"Fetched {len(raw)} unique markets across orderings")

    candidates = filter_weather_and_commodity(raw)
    if category:
        candidates = [m for m in candidates if m["_category"] == category]
    before = len(candidates)
    candidates = [m for m in candidates if PRICE_MIN <= m["yes_price"] <= PRICE_MAX]
    log.info(f"Price band [{PRICE_MIN:.2f},{PRICE_MAX:.2f}]: {before} -> {len(candidates)}")

    if skip_open:
        held = {t["condition_id"] for t in ledger.currently_open()}
        candidates = [m for m in candidates if m.get("condition_id") not in held]

    candidates.sort(key=lambda m: -m.get("liquidity", 0))
    if category:
        return candidates[:limit]
    picked = _diversify(candidates, limit, CATEGORY_CAP)
    from collections import Counter
    log.info(f"Category cap {CATEGORY_CAP}: picked {len(picked)} -> {dict(Counter(m['_category'] for m in picked))}")
    return picked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--pool",  type=int, default=500)
    parser.add_argument("--category", default=None,
                        help="weather | temperature | commodity | crypto_price | equity_index | fed_rate | economic")
    parser.add_argument("--skip-open", action="store_true",
                        help="exclude markets where we already have an open position")
    args = parser.parse_args()

    cands = fetch_candidates(args.limit, args.pool, args.category, args.skip_open)

    out = []
    for m in cands:
        out.append({
            "market_id":    m.get("id"),
            "condition_id": m.get("condition_id"),
            "question":     m.get("question"),
            "description":  (m.get("description") or "")[:1500],
            "category":     m.get("_category"),
            "slug":         m.get("slug"),
            "end_date":     m.get("end_date"),
            "yes_price":    m.get("yes_price"),
            "no_price":     m.get("no_price"),
            "liquidity":    m.get("liquidity"),
            "volume_24h":   m.get("volume_24h"),
            "tags":         m.get("tags"),
        })

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

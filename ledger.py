"""
Append-only JSONL ledger for paper trades.

Two files in trades/:
  open.jsonl       — one line per opened paper trade (never edited)
  resolved.jsonl   — one line per settled trade with P&L (never edited)

"Currently open" = entries in open.jsonl whose trade_id is NOT in resolved.jsonl.
Append-only is git-friendly: no merge conflicts, full history preserved.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

TRADES_DIR = Path(__file__).parent / "trades"
OPEN_FILE = TRADES_DIR / "open.jsonl"
RESOLVED_FILE = TRADES_DIR / "resolved.jsonl"


def _ensure_dir() -> None:
    TRADES_DIR.mkdir(exist_ok=True)


def _read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _append_jsonl(path: Path, record: dict) -> None:
    _ensure_dir()
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def make_trade_id(condition_id: str, direction: str, ts: str) -> str:
    """Stable id from market + direction + open ts. Short prefix for readability."""
    h = hashlib.sha256(f"{condition_id}|{direction}|{ts}".encode()).hexdigest()
    return h[:16]


def load_open() -> list[dict]:
    return list(_read_jsonl(OPEN_FILE))


def load_resolved() -> list[dict]:
    return list(_read_jsonl(RESOLVED_FILE))


def currently_open() -> list[dict]:
    """Open trades minus those that already have a resolution row."""
    resolved_ids = {r["trade_id"] for r in load_resolved()}
    return [t for t in load_open() if t["trade_id"] not in resolved_ids]


def has_open_position(condition_id: str) -> bool:
    """Don't double-enter a market we're already holding."""
    return any(t["condition_id"] == condition_id for t in currently_open())


def record_open(
    *,
    market: dict,
    direction: str,
    entry_price: float,
    size_usdc: float,
    claude_prob: float,
    confidence: float,
    edge: float,
    reasoning: str,
    data_sources: list[str],
    key_uncertainty: str,
    model: str,
) -> dict:
    """Append a new paper trade. Returns the record for logging."""
    ts = datetime.now(timezone.utc).isoformat()
    trade_id = make_trade_id(market["condition_id"] or market["id"], direction, ts)
    record = {
        "trade_id":           trade_id,
        "ts":                 ts,
        "market_id":          market.get("id"),
        "condition_id":       market.get("condition_id"),
        "question":           market.get("question"),
        "category":           market.get("_category") or market.get("category"),
        "slug":               market.get("slug"),
        "end_date":           market.get("end_date"),
        "direction":          direction,
        "entry_price":        round(entry_price, 4),
        "size_usdc":          round(size_usdc, 2),
        "claude_prob":        round(claude_prob, 4),
        "confidence":         round(confidence, 4),
        "edge":               round(edge, 4),
        "liquidity_at_entry": market.get("liquidity"),
        "volume_24h_at_entry": market.get("volume_24h"),
        "reasoning":          reasoning,
        "data_sources":       data_sources,
        "key_uncertainty":    key_uncertainty,
        "model":              model,
    }
    _append_jsonl(OPEN_FILE, record)
    return record


def record_resolution(
    *,
    trade: dict,
    outcome: str,
    resolved_at: str,
) -> dict:
    """Compute P&L and append to resolved.jsonl. Idempotent — caller should check first."""
    direction = trade["direction"]
    entry_price = trade["entry_price"]
    size_usdc = trade["size_usdc"]
    won = (direction == outcome)

    # Paper-trade P&L: entry buys size_usdc/entry_price shares at entry_price.
    # On YES win: each share pays $1, so payout = size / entry_price.
    # P&L = payout - size on win, P&L = -size on loss.
    if won:
        shares = size_usdc / entry_price
        pnl = round(shares - size_usdc, 4)
    else:
        pnl = round(-size_usdc, 4)
    pnl_pct = round(pnl / size_usdc, 4) if size_usdc else 0.0

    try:
        opened = datetime.fromisoformat(trade["ts"].replace("Z", "+00:00"))
        closed = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
        hold_hours = round((closed - opened).total_seconds() / 3600, 2)
    except (ValueError, TypeError):
        hold_hours = None

    record = {
        "trade_id":    trade["trade_id"],
        "condition_id": trade["condition_id"],
        "question":    trade["question"],
        "category":    trade.get("category"),
        "direction":   direction,
        "entry_price": entry_price,
        "size_usdc":   size_usdc,
        "claude_prob": trade["claude_prob"],
        "confidence":  trade["confidence"],
        "outcome":     outcome,
        "won":         won,
        "pnl_usdc":    pnl,
        "pnl_pct":     pnl_pct,
        "resolved_at": resolved_at,
        "hold_hours":  hold_hours,
    }
    _append_jsonl(RESOLVED_FILE, record)
    return record


def bankroll_realised(starting: float) -> float:
    """Starting bankroll plus all realised P&L."""
    return starting + sum(r["pnl_usdc"] for r in load_resolved())


def capital_locked() -> float:
    """USDC currently tied up in open positions."""
    return sum(t["size_usdc"] for t in currently_open())

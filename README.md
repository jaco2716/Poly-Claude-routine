# Poly-Claude-routine

Paper-trading framework for Polymarket. Every run:

1. Fetches active markets from Polymarket
2. Filters to objectively verifiable categories (weather, temperature, commodities, crypto, indices, fed/economic)
3. Asks Claude (with web search) for an independent probability estimate
4. Opens a paper trade when edge + confidence + liquidity thresholds are all met
5. A separate resolver settles trades when their markets close and computes P&L
6. A reporter rolls up calibration, Brier score, and P&L stats

No real trades are executed. The trade-execution functions in `polymarket_api.py` exist but are unused.

## Layout

```
polymarket_api.py   API layer: auth, market data, trade execution stubs
market_filter.py    Drops subjective markets, tags survivors by category
claude_analyst.py   Claude Sonnet 4.6 + web_search → probability JSON
ledger.py           Append-only JSONL ledger (open.jsonl + resolved.jsonl)
main.py             Analyse markets and open paper trades that meet thresholds
resolve.py          Settle closed markets, append P&L
report.py           Stats: P&L, win rate, Brier score, calibration buckets
reports/            One JSON file per analysed market (audit trail)
trades/
  open.jsonl        One line per opened paper trade
  resolved.jsonl    One line per settled trade with outcome + P&L
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# add ANTHROPIC_API_KEY
```

`POLYMARKET_PRIVATE_KEY` / `POLYMARKET_FUNDER_ADDRESS` are only needed if you call the trade-execution functions in `polymarket_api.py`. The paper pipeline does not.

## Run

```bash
python main.py                  # analyse 5 markets, may open paper trades
python main.py --limit 10
python main.py --no-trade       # analysis only, no positions opened
python main.py --category weather

python resolve.py               # settle any closed markets, append P&L
python report.py                # stats summary
python report.py --json         # machine-readable
```

## Trade gate

A paper trade is opened only when ALL of these pass:

| condition | default | env var |
|---|---|---|
| `\|claude_prob - market_yes_price\|` ≥ X | 0.07 | `EDGE_THRESHOLD` |
| Claude's `confidence` ≥ X | 0.65 | `CONFIDENCE_THRESHOLD` |
| Market `liquidity` ≥ $X | 10000 | `MIN_LIQUIDITY` |
| Claude flagged `verifiable: true` | — | — |
| No existing open position on this `condition_id` | — | — |

Sizing: `2%` of available bankroll per trade (`TRADE_SIZE_PCT`), `MIN_TRADE_USDC = $5`, starting bankroll `STARTING_BANKROLL = $1000`.

## Persistence model (Claude Routines)

Claude Routines run in an ephemeral container. The DB strategy is **append-only JSONL committed back to git** so the ledger survives between runs without external infrastructure.

Each routine run ends by committing `trades/` and `reports/` back to the repo. JSONL is text → diffs are sane, no merge conflicts as long as runs don't overlap, and the file *is* the history.

When you outgrow this (probably never for a personal experiment), swap `ledger.py`'s read/write functions for Turso/Postgres without changing anything else.

## Routine schedule (recommendation)

```
every 4-6 hours    python main.py --limit 20  →  git add reports/ trades/ && git commit && git push
once a day         python resolve.py          →  git add trades/ && git commit && git push
weekly             python report.py           →  digest output (optional)
```

## Report format

`reports/*.json` (one per market analysed):

```json
{
  "market":     { "id": "...", "question": "...", "category": "crypto_price", "yes_price": 0.51, ... },
  "assessment": { "verifiable": true, "probability_yes": 0.42, "confidence": 0.72,
                  "reasoning": "...", "data_sources": [...], "key_uncertainty": "...",
                  "model": "claude-sonnet-4-6" },
  "edge":       { "market_implied_prob": 0.51, "claude_prob": 0.42, "delta": -0.085,
                  "suggested_direction": "no" }
}
```

`trades/open.jsonl` (one line each — full snapshot for later analysis):

```
{"trade_id": "...", "ts": "...", "condition_id": "...", "question": "...",
 "direction": "no", "entry_price": 0.495, "size_usdc": 20.0,
 "claude_prob": 0.42, "confidence": 0.72, "edge": -0.085,
 "liquidity_at_entry": 119113, "reasoning": "...", "data_sources": [...], ...}
```

`trades/resolved.jsonl` (one line each):

```
{"trade_id": "...", "outcome": "yes", "won": false, "pnl_usdc": -20.0,
 "pnl_pct": -1.0, "resolved_at": "...", "hold_hours": 96.5, ...}
```

## Stats

`python report.py` prints:

- Bankroll: starting → current
- W/L count, win rate, avg win, avg loss
- **Brier score** for Claude's probabilities vs the market's — lower is better. The ratio tells you whether Claude is actually adding signal over the market price
- P&L by category (weather vs commodity vs crypto…)
- Calibration buckets — "when Claude said 70%, what % actually paid YES?" The killer metric for whether the model is well-calibrated

## Notes

- Web search costs ~10k tokens of context per call → main.py sleeps 20s between markets to stay under the 30k input-tokens/min Anthropic rate limit
- `main.py` widens the candidate pool by fetching across three Gamma orderings (`volume24hr`, `liquidity`, `endDate`) and dedupes — typically ~1400 unique active markets
- Markets with `yes_price < 0.05` or `> 0.95` are skipped (no actionable edge)
- `STARTING_BANKROLL` controls displayed P&L only — the ledger has no notion of an account, just a sum of trades. Bumping it later doesn't break old data

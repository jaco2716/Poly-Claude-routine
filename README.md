# Poly-Claude-routine

Paper-trading framework for Polymarket, designed to run on **Claude Routines** using your **Claude Pro/Max subscription** — no Anthropic API key, no per-token billing.

The Claude Code agent does the analysis itself (web search, reasoning) inside the routine. Python scripts handle everything else: fetching markets, applying trade gates, recording the ledger, settling resolutions, computing stats.

## Architecture

```
Routine (every 6h) ── Claude Code agent ──┐
                                          │
   1. python fetch_markets.py  ───────────┼──► JSON candidates
   2. agent analyses each market with web_search    (Pro plan, no API key)
   3. python record_trades.py  ◄──────────┘  ──► reports/, trades/open.jsonl
   4. git commit + push                          (persistence)

Routine (daily) ── Claude Code agent ──┐
                                       │
   1. python resolve.py  ──────────────┴──► trades/resolved.jsonl + P&L
   2. python report.py
   3. git commit + push
```

No `anthropic` SDK, no `ANTHROPIC_API_KEY`. The intelligence runs as part of the routine itself.

## Layout

```
polymarket_api.py    API layer: market data + (unused) trade execution stubs
market_filter.py     Drops subjective markets, tags survivors by category
fetch_markets.py     Outputs candidate markets as JSON to stdout
record_trades.py     Reads agent analyses, applies trade gates, writes ledger
resolve.py           Settles closed markets, appends P&L
report.py            P&L + Brier + calibration stats
ledger.py            Append-only JSONL store

reports/             One JSON per analysed market (audit trail)
trades/
  open.jsonl         One line per opened paper trade
  resolved.jsonl     One line per settled trade with outcome + P&L

ROUTINE.md           Exact prompts to paste into Claude Routine schedules
```

## Local setup

```bash
pip install -r requirements.txt
cp .env.example .env
# .env is optional — only needed for local threshold tweaks
```

## Routine setup

See **`ROUTINE.md`**. Two routines:

- **analyse** every 6 hours — fetches candidates, agent assesses each, opens paper trades that pass gates
- **resolve** daily — checks open trades for settlement, appends P&L

The persistence model is **append-only JSONL committed back to git** — the routine pushes `reports/` and `trades/` after each run, so the ledger survives between ephemeral container starts.

## Local commands (manual run / debugging)

```bash
# Print candidate markets as JSON (no analysis):
python fetch_markets.py --limit 12 --pool 800 --skip-open

# Apply gates given an analyses file (e.g. one you produced manually):
python record_trades.py --input analyses.json
python record_trades.py --input analyses.json --no-trade   # reports only

# Settle anything that closed:
python resolve.py

# Stats:
python report.py
python report.py --json
```

## Trade gate

A paper trade opens only when ALL of these pass:

| condition | default | env var |
|---|---|---|
| `\|claude_prob - market_yes_price\|` ≥ X | 0.07 | `EDGE_THRESHOLD` |
| Claude's `confidence` ≥ X | 0.65 | `CONFIDENCE_THRESHOLD` |
| Market `liquidity` ≥ $X | 10000 | `MIN_LIQUIDITY` |
| Claude flagged `verifiable: true` | — | — |
| No existing open position on this `condition_id` | — | — |

Sizing: `2%` of available bankroll per trade (`TRADE_SIZE_PCT`), `MIN_TRADE_USDC = $5`, starting bankroll `STARTING_BANKROLL = $1000`.

## Analysis schema

`record_trades.py` expects a JSON array, each item:

```json
{
  "market": { ...full object from fetch_markets.py output... },
  "assessment": {
    "verifiable":      true,
    "probability_yes": 0.42,
    "confidence":      0.72,
    "reasoning":       "2–4 sentences citing the data used",
    "data_sources":    ["short labels per source"],
    "key_uncertainty": "the biggest thing that could move the estimate"
  }
}
```

The Routine prompt in `ROUTINE.md` tells the agent to produce exactly this shape.

## Stats (`python report.py`)

- Bankroll: starting → realised
- W/L count, win rate, avg win/loss
- **Brier score** for Claude's probabilities vs the market's — lower is better. The ratio tells you whether Claude is actually adding signal
- P&L by category (weather vs commodity vs crypto…)
- Calibration buckets — "when Claude said 70%, what % actually resolved YES?"

## When to enable live trading

After ~50+ resolved trades:

- `brier_claude < brier_market` AND positive cumulative P&L → live trading has a basis
- Otherwise tune thresholds or kill the strategy

The live path is not built yet. When you're ready, `polymarket_api.py` already has working `place_market_order` + `get_order_fill` — you'll add a `mode: "live"` branch in `record_trades.py` that calls them and adds `POLYMARKET_PRIVATE_KEY` to the routine's secrets.

## Notes

- Markets with `yes_price < 0.05` or `> 0.95` are skipped (no actionable edge)
- `fetch_markets.py` widens the candidate pool by querying three Gamma orderings (`volume24hr`, `liquidity`, `endDate`) and dedupes — typically ~1400 unique active markets
- `STARTING_BANKROLL` controls displayed P&L only — the ledger has no notion of an account, just a sum of trades. Bumping it later doesn't break old data
- JSONL is read by `pandas.read_json(..., lines=True)` and `duckdb` natively, so a dashboard later is one query away

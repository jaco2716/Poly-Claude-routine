# Routine setup

Two scheduled Claude Routines power this project. Both run a Claude Code agent on your Pro/Max plan — no Anthropic API key needed.

---

## Routine 1 — Analyse + open paper trades

**Schedule**: every 6 hours (`0 */6 * * *`)

**Prompt**: paste the block below verbatim into the routine's instructions field.

```
You are running an automated paper-trading routine for Polymarket.

Working directory: this repo. Use the existing tools.

Steps:

1. Run `pip install -q -r requirements.txt`.

2. Run `python fetch_markets.py --limit 12 --pool 800 --skip-open` and capture
   the JSON array it prints to stdout. This is the candidate list.

3. For EACH candidate market in the array, do an independent probability
   assessment using your web_search tool. Read the question carefully, find
   the resolution criteria, then ground your estimate in current data:
   - Weather/temperature: official forecasts (NWS, ECMWF, weather.com,
     national met services) for the resolution location and date.
   - Commodity: front-month futures price + recent move + analyst targets.
   - Crypto: spot price across 2-3 sources + remaining time + base-rate move.
   - Equity index: current level + days remaining + implied vol.
   - Fed/economic: official release calendar + consensus + recent data.

   Reason from base rates first, then adjust for current conditions.
   Do NOT consider the market's current `yes_price` when forming your
   estimate — we want an independent number.

   If the question is genuinely subjective or you cannot find enough current
   data to estimate well, set verifiable=false or lower confidence. Do NOT
   guess.

4. Build a single JSON array where each entry has the shape:

   {
     "market": <the full market object from fetch_markets.py, unmodified>,
     "assessment": {
       "verifiable":      true | false,
       "probability_yes": 0.0-1.0,
       "confidence":      0.0-1.0,
       "reasoning":       "2-4 sentences citing the specific data you used",
       "data_sources":    ["short labels of each source"],
       "key_uncertainty": "the single biggest thing that could move the estimate"
     }
   }

   Save this array to /tmp/analyses.json.

5. Run `python record_trades.py --input /tmp/analyses.json` to apply trade
   gates and append paper trades to the ledger.

6. Commit and push:

       git config user.email "routine@bot"
       git config user.name "poly-routine"
       git add reports/ trades/
       git diff --cached --quiet || git commit -m "routine analyse $(date -u +%FT%TZ)"
       git push

Important:
- Do not invent market data. If web_search doesn't return useful info for a
  market, lower confidence on that market and move on.
- Do not modify the market object passed back into the analyses array.
- Keep reasoning concise (2-4 sentences) — this is logged for audit.
- If any step fails, print the error and exit non-zero. Do not "fix" the
  pipeline by editing files; report the problem so it can be addressed.
```

---

## Routine 2 — Resolve settled markets

**Schedule**: once a day (`0 7 * * *`)

**Prompt**:

```
You are running the daily settlement routine for the Polymarket paper trader.

Steps:
1. Run `pip install -q -r requirements.txt`
2. Run `python resolve.py` — it polls the CLOB/Gamma APIs for any open paper
   trades whose markets have closed, computes P&L, and appends to
   trades/resolved.jsonl.
3. Run `python report.py` and include its output in your final reply (so it
   shows up in the routine notification).
4. Commit + push:

       git config user.email "routine@bot"
       git config user.name "poly-routine"
       git add trades/
       git diff --cached --quiet || git commit -m "resolve $(date -u +%FT%TZ)"
       git push
```

---

## Things to verify on the first run

1. The agent successfully writes to `reports/` and `trades/open.jsonl`.
2. `git push` works (Routine has push access to the repo).
3. After 24h, `trades/open.jsonl` has lines and `trades/resolved.jsonl` is empty
   or filling in slowly as markets close.
4. `python report.py` locally (after `git pull`) shows the trades.

## Tuning

Trade-gate thresholds are env vars on the routine. Defaults are deliberately
strict; loosen after you have data. See `record_trades.py` for the list:

```
EDGE_THRESHOLD=0.07           # |claude - market| ≥ 7%
CONFIDENCE_THRESHOLD=0.65     # claude self-confidence ≥ 65%
MIN_LIQUIDITY=10000           # market book ≥ $10k
TRADE_SIZE_PCT=0.02           # 2% of bankroll per trade
STARTING_BANKROLL=1000
```

## When you have ~50+ resolved trades

Compare the two Brier scores in `python report.py`:

- `brier_claude` < `brier_market`  → Claude is adding signal, paper-trade gate is justified
- `brier_claude` ≈ `brier_market`  → no edge yet, do not enable live
- `brier_claude` > `brier_market`  → Claude is worse than the market, kill it

Also check the calibration table — when Claude says "70%", do markets actually
resolve YES ~70% of the time? Mis-calibration in one direction means your
gate's edge calculation is biased.

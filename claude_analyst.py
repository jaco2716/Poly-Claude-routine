"""
Claude-driven probability assessment for objectively verifiable markets.

Given a market dict (with `_category` from market_filter), produces a
structured assessment: probability, reasoning, confidence, and a note
on what data sources Claude grounded its estimate in.

Web search is enabled so Claude can pull current forecasts, commodity
quotes, and historical base rates rather than guessing from training data.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1500

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=key)
    return _client


SYSTEM_PROMPT = """You are a quantitative analyst evaluating prediction markets that have OBJECTIVELY VERIFIABLE outcomes — weather events, temperatures, commodity prices, index levels, crypto prices, economic releases.

Your job: estimate the true probability of the YES outcome, grounded in real-world data.

Approach:
1. Identify what's measurable in the question (a specific threshold, date, source).
2. Pull current state from the web: forecasts, prices, historical base rates, recent trends.
3. Reason from base rates first, then adjust for current conditions.
4. State your probability and how confident you are in that estimate (these are different — a probability of 0.5 with high confidence means "genuinely a coin flip", not "I don't know").

Rules:
- If the question is subjective or unmeasurable, set verifiable=false and explain.
- If you cannot find enough current data to estimate well, lower confidence — do not guess.
- Cite the specific data points you used (e.g. "NWS forecast 60% chance of measurable snow Friday", "WTI front-month at $76.40").
- Do NOT consider the current market price when forming your estimate — we want an independent probability, not a market-following one.

Reply ONLY with valid JSON, no prose, no markdown fences:
{
  "verifiable": true | false,
  "probability_yes": 0.0-1.0,
  "confidence": 0.0-1.0,
  "reasoning": "2-4 sentences citing the specific data used",
  "data_sources": ["short label of each source you grounded the estimate in"],
  "key_uncertainty": "the single biggest thing that could move the estimate"
}"""


def _build_user_message(market: dict) -> str:
    end_date = market.get("end_date") or "unknown"
    return (
        f"Category: {market.get('_category', 'unknown')}\n"
        f"Resolves: {end_date}\n"
        f"Question: {market.get('question', '')}\n"
        f"Description: {market.get('description', '')[:1500]}\n"
        f"Current YES price: {market.get('yes_price', 0):.3f}  "
        f"(market-implied probability — IGNORE when forming your own estimate)\n\n"
        f"Estimate the true probability of YES."
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first {...} JSON object out of a text blob."""
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: find the largest balanced {...} block
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def analyse(market: dict, use_web_search: bool = True) -> Optional[dict]:
    """Ask Claude for a probability assessment.

    Returns a dict shaped like the JSON in SYSTEM_PROMPT, plus the raw text
    response under `_raw` for auditing. Returns None on hard failure.
    """
    client = _get_client()

    tools = []
    if use_web_search:
        tools.append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 4,
        })

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tools or None,
            messages=[{"role": "user", "content": _build_user_message(market)}],
        )
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return None

    # Concatenate all text blocks (web search may interleave search results)
    text_parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    raw = "\n".join(text_parts).strip()

    parsed = _extract_json(raw)
    if parsed is None:
        log.warning(f"Could not parse JSON from Claude response: {raw[:200]}")
        return None

    # Validate / clamp
    try:
        if "probability_yes" in parsed:
            parsed["probability_yes"] = max(0.0, min(1.0, float(parsed["probability_yes"])))
        if "confidence" in parsed:
            parsed["confidence"] = max(0.0, min(1.0, float(parsed["confidence"])))
        parsed["verifiable"] = bool(parsed.get("verifiable", True))
    except (TypeError, ValueError) as e:
        log.warning(f"Invalid types in Claude response: {e}")
        return None

    parsed["_raw"] = raw
    parsed["_model"] = MODEL
    parsed["_timestamp"] = datetime.now(timezone.utc).isoformat()
    return parsed


def build_report(market: dict, assessment: dict) -> dict:
    """Assemble the structured report for a single market."""
    yes_price = market.get("yes_price", 0.0)
    prob = assessment.get("probability_yes")
    edge = None
    direction = None
    if prob is not None:
        edge = round(prob - yes_price, 4)
        # 'yes' edge if our prob > market; 'no' edge if our prob < market
        if abs(edge) >= 0.05:
            direction = "yes" if edge > 0 else "no"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": {
            "id":           market.get("id"),
            "condition_id": market.get("condition_id"),
            "question":     market.get("question"),
            "category":     market.get("_category"),
            "slug":         market.get("slug"),
            "end_date":     market.get("end_date"),
            "yes_price":    yes_price,
            "no_price":     market.get("no_price"),
            "liquidity":    market.get("liquidity"),
            "volume_24h":   market.get("volume_24h"),
        },
        "assessment": {
            "verifiable":      assessment.get("verifiable"),
            "probability_yes": prob,
            "confidence":      assessment.get("confidence"),
            "reasoning":       assessment.get("reasoning"),
            "data_sources":    assessment.get("data_sources", []),
            "key_uncertainty": assessment.get("key_uncertainty"),
            "model":           assessment.get("_model"),
        },
        "edge": {
            "market_implied_prob": yes_price,
            "claude_prob":         prob,
            "delta":               edge,
            "suggested_direction": direction,
        },
    }

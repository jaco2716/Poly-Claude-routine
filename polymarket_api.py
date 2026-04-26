"""
Clean Polymarket API wrapper.

Extracted from the existing trading bot — only the connectivity layer:
  * authentication (CLOB client init via private key)
  * market data fetching (Gamma + CLOB read endpoints)
  * trade execution (FAK market order placement + fill check)

NO trading logic, NO strategy, NO scoring/ranking lives here. Callers
decide what to do with the data.
"""
import json
import logging
import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "poly-claude-routine/1.0"})

_clob_client = None


def _get(url: str, params: dict = None, timeout: int = 10, retries: int = 3) -> Optional[dict]:
    """GET with exponential backoff on 429/5xx. Returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            log.warning(f"GET {url} failed after {retries} attempts: {e}")
            return None
        except json.JSONDecodeError:
            return None
    return None


# ─── Authentication ──────────────────────────────────────────────────────────

def get_clob_client():
    """Lazy-init the authenticated CLOB client (singleton).

    Requires POLYMARKET_PRIVATE_KEY env var. Funder address is optional
    (defaults to the wallet derived from the private key).
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set — cannot authenticate")
    from py_clob_client.client import ClobClient
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS") or None
    _clob_client = ClobClient(
        CLOB_URL,
        key=private_key,
        chain_id=137,
        signature_type=1,
        funder=funder,
    )
    _clob_client.set_api_creds(_clob_client.create_or_derive_api_creds())
    log.info("CLOB client initialised (Polygon mainnet)")
    return _clob_client


def get_usdc_balance() -> float:
    """Authenticated USDC balance query. Returns balance in dollars."""
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    client = get_clob_client()
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    client.update_balance_allowance(params=params)
    resp = client.get_balance_allowance(params=params)
    raw = float(resp.get("balance") or 0)
    return raw / 1_000_000


# ─── Market data fetching ────────────────────────────────────────────────────

def fetch_active_markets(limit: int = 100, order: str = "volume24hr") -> list[dict]:
    """Fetch raw active markets from the Gamma API. No filtering, no ranking.

    Returns a list of normalised market dicts with the fields we care about.
    Caller decides what to keep.
    """
    data = _get(f"{GAMMA_URL}/markets", params={
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": order,
        "ascending": "false",
    })
    if not data:
        return []
    raw = data if isinstance(data, list) else data.get("markets", [])
    return [_normalise_market(m) for m in raw if _normalise_market(m) is not None]


def _normalise_market(m: dict) -> Optional[dict]:
    """Pull the fields we use into a flat dict. Skip malformed entries."""
    try:
        prices_raw = m.get("outcomePrices")
        yes_price = None
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
            yes_price = float(prices[0]) if prices else None
        elif isinstance(prices_raw, list) and prices_raw:
            yes_price = float(prices_raw[0])
        if yes_price is None:
            return None

        tokens = m.get("clobTokenIds") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except (ValueError, TypeError):
                tokens = []
        yes_token_id = tokens[0] if tokens else None
        no_token_id = tokens[1] if len(tokens) > 1 else None

        return {
            "id":           m.get("id") or m.get("conditionId", ""),
            "condition_id": m.get("conditionId"),
            "question":     m.get("question") or m.get("title", ""),
            "description":  m.get("description", ""),
            "slug":         m.get("slug", ""),
            "yes_price":    yes_price,
            "no_price":     round(1 - yes_price, 4),
            "liquidity":    float(m.get("liquidity") or 0),
            "volume_24h":   float(m.get("volume24hr") or m.get("volume") or 0),
            "yes_token_id": yes_token_id,
            "no_token_id":  no_token_id,
            "end_date":     m.get("endDate") or m.get("endDateIso"),
            "tags":         [t.get("label") if isinstance(t, dict) else t for t in (m.get("tags") or [])],
            "category":     m.get("category", ""),
        }
    except (TypeError, ValueError, KeyError):
        return None


def fetch_market_by_condition_id(condition_id: str) -> Optional[dict]:
    """Fetch a single market by conditionId from the CLOB API."""
    data = _get(f"{CLOB_URL}/markets/{condition_id}")
    if not data or not isinstance(data, dict):
        return None
    if data.get("closed") or not data.get("active", True):
        return None
    tokens = data.get("tokens") or []
    yes_token = next((t for t in tokens if str(t.get("outcome", "")).lower() == "yes"), None)
    no_token = next((t for t in tokens if str(t.get("outcome", "")).lower() == "no"), None)
    if not yes_token:
        return None
    yes_price = float(yes_token.get("price") or 0)
    return {
        "id":           condition_id,
        "condition_id": condition_id,
        "question":     data.get("question", ""),
        "description":  data.get("description", ""),
        "slug":         data.get("market_slug", ""),
        "yes_price":    yes_price,
        "no_price":     round(1 - yes_price, 4),
        "liquidity":    0.0,
        "volume_24h":   0.0,
        "yes_token_id": yes_token.get("token_id"),
        "no_token_id":  no_token.get("token_id") if no_token else None,
        "end_date":     data.get("end_date_iso"),
        "tags":         data.get("tags") or [],
        "category":     data.get("category", ""),
    }


def fetch_token_price(token_id: str) -> Optional[float]:
    """Live mid-price for a CLOB token id."""
    data = _get(f"{CLOB_URL}/midpoint", params={"token_id": token_id})
    if not data:
        return None
    try:
        return float(data.get("mid"))
    except (TypeError, ValueError):
        return None


# ─── Trade execution (built but unused — analysis-only mode) ─────────────────

def place_market_order(token_id: str, amount_usdc: float, limit_price: float) -> dict:
    """Place a FAK (fill-and-kill) BUY market order on the CLOB.

    Available for future use. The current analysis framework does NOT call
    this — it only produces probability reports.
    """
    from py_clob_client.clob_types import (
        MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType,
    )
    from py_clob_client.order_builder.constants import BUY

    client = get_clob_client()
    client.update_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    mo = MarketOrderArgs(
        token_id=token_id,
        amount=amount_usdc,
        side=BUY,
        price=limit_price if limit_price > 0 else None,
    )
    signed = client.create_market_order(mo)
    resp = client.post_order(signed, OrderType.FAK)
    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    return {"order_id": order_id, "response": resp}


def get_order_fill(order_id: str, attempts: int = 5) -> tuple[bool, float]:
    """Poll the CLOB for fill status. Returns (filled, shares_matched)."""
    client = get_clob_client()
    wait = 1.0
    for attempt in range(attempts):
        if attempt > 0:
            time.sleep(wait)
            wait = min(wait * 2, 4.0)
        try:
            order = client.get_order(order_id)
            if not isinstance(order, dict):
                continue
            size_matched = float(
                order.get("size_matched") or order.get("sizeMatched") or 0
            )
            if size_matched > 0:
                return True, size_matched
            status = order.get("status") or order.get("order_status") or ""
            if status.upper() in ("CANCELLED", "UNMATCHED"):
                break
        except Exception as e:
            log.debug(f"fill check failed: {e}")
    return False, 0.0

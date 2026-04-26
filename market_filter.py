"""
Filter Polymarket markets to objectively verifiable categories only:
weather, temperature forecasts, and measurable outcome markets
(commodity prices, indices, crypto levels, sports scores, election counts).

Subjective markets — celebrity behavior, political opinion, "will X say Y" —
are excluded because Claude cannot ground them in measurable data.
"""
import re
from typing import Iterable

# Strong-signal keywords. Each tuple: (category, regex). Order matters: the
# first matching category wins.
CATEGORY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("weather",    re.compile(r"\b(snow|rain|hurricane|tornado|storm|blizzard|cyclone|typhoon|wildfire)\b", re.I)),
    ("temperature", re.compile(r"\b(temperature|temp|hottest|coldest|degrees?|fahrenheit|celsius|heatwave|cold snap|freeze)\b", re.I)),
    ("commodity",  re.compile(r"\b(oil|wti|brent|natural gas|gasoline|gold|silver|copper|wheat|corn|soybean|coffee|sugar|cotton|lumber)\b", re.I)),
    ("crypto_price", re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|dogecoin|cardano)\b.*\b(reach|hit|above|below|close|price|\$\d)", re.I)),
    ("equity_index", re.compile(r"\b(s&p|sp500|nasdaq|dow jones|djia|russell)\b.*\b(close|reach|hit|above|below|\d)", re.I)),
    ("fed_rate",   re.compile(r"\b(fed|federal reserve|fomc|rate cut|rate hike|interest rate)\b", re.I)),
    ("economic",   re.compile(r"\b(cpi|inflation|unemployment|jobs report|gdp|payroll|retail sales)\b", re.I)),
]

# Markets we explicitly skip even if they contain measurable-sounding words.
SUBJECTIVE_BLOCKLIST = re.compile(
    r"\b(say|tweet|post|wear|appear|kiss|date|marry|divorce|fired|resign|"
    r"endorse|cabinet|nominee|approval rating|popularity|"
    r"miss universe|grammy|oscar|emmy|tony award)\b",
    re.I,
)


def categorise(market: dict) -> str | None:
    """Return the verification category for a market, or None if subjective.

    The category tells the analyst module what kind of grounding to look for
    (forecasts, commodity quotes, crypto APIs, etc.).
    """
    text = " ".join([
        market.get("question", ""),
        market.get("description", "")[:500],
        " ".join(str(t) for t in market.get("tags", []) or []),
    ])
    if SUBJECTIVE_BLOCKLIST.search(text):
        return None
    for category, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return category
    return None


def filter_objective(markets: Iterable[dict]) -> list[dict]:
    """Tag each market with `_category`, drop those without one."""
    out = []
    for m in markets:
        cat = categorise(m)
        if cat is None:
            continue
        m = {**m, "_category": cat}
        out.append(m)
    return out


def filter_weather_and_commodity(markets: Iterable[dict]) -> list[dict]:
    """Subset for the initial validation set: weather, temperature, commodity, crypto, equity."""
    target = {"weather", "temperature", "commodity", "crypto_price", "equity_index"}
    return [m for m in filter_objective(markets) if m["_category"] in target]

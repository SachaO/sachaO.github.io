"""Bounded, best-effort market discovery for ATLAS paper trading.

Sources are public and may be delayed or rate-limited. Discovery never guarantees
coverage of every listed asset. Crypto assets use CoinGecko IDs to avoid ticker
symbol collisions; equities/ETFs use Yahoo Finance symbols.
"""
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)
TIMEOUT = 8
MAX_ASSETS = max(8, min(40, int(os.getenv("ATLAS_DISCOVERY_MAX_ASSETS", "20"))))
CRYPTO_MARKET_CAP_MIN = float(os.getenv("ATLAS_CRYPTO_MIN_MARKET_CAP_USD", "20000000"))
CRYPTO_VOLUME_MIN = float(os.getenv("ATLAS_CRYPTO_MIN_VOLUME_USD", "5000000"))
_CACHE = {"at": 0.0, "assets": []}
CACHE_SECONDS = 1800

# Diversified fallback universe: US/global stocks and ETFs. Existing portfolio
# watchlist is always retained and appears first in engine.py.
FALLBACK_EQUITIES = {
    "ASML": "ASML", "SAP": "SAP", "TM": "Toyota", "SONY": "Sony",
    "BABA": "Alibaba", "TSM": "TSMC", "SHOP": "Shopify", "AMD": "AMD",
    "AVGO": "Broadcom", "MSFT": "Microsoft", "AAPL": "Apple",
    "AMZN": "Amazon", "GOOGL": "Alphabet", "META": "Meta",
    "LLY": "Eli Lilly", "JPM": "JPMorgan", "XOM": "Exxon Mobil",
    "GLD": "Gold ETF", "TLT": "US Treasury ETF", "EFA": "Developed Markets ETF",
    "EEM": "Emerging Markets ETF", "IWM": "Russell 2000 ETF",
}

def _yahoo_screen(screen_id, count=12):
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    response = requests.get(
        url, params={"scrIds": screen_id, "count": count, "start": 0},
        headers={"User-Agent": "Mozilla/5.0 ATLAS-paper-trading"}, timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("finance", {}).get("result") or []
    quotes = results[0].get("quotes", []) if results else []
    assets = []
    for item in quotes:
        symbol = str(item.get("symbol", "")).strip()
        name = str(item.get("shortName") or item.get("longName") or symbol).strip()
        # Prevent unexpected ticker strings from entering the query path.
        if symbol and re.fullmatch(r"[A-Za-z0-9.^=_-]{1,24}", symbol):
            assets.append((symbol, name))
    return assets

def discover_assets():
    """Return a cached, bounded set of newly discovered candidates."""
    now = time.time()
    if now - _CACHE["at"] < CACHE_SECONDS:
        return list(_CACHE["assets"])
    found = {}
    # Yahoo's public predefined screeners are best-effort and can be throttled.
    for screen_id in ("most_actives", "day_gainers", "day_losers"):
        try:
            for symbol, name in _yahoo_screen(screen_id, count=12):
                found.setdefault(symbol, {"symbol": symbol, "name": name, "kind": "equity_or_etf", "source": "Yahoo Finance screener"})
        except Exception as exc:
            log.warning("ATLAS discovery Yahoo screener unavailable (%s): %s", screen_id, str(exc)[:140])

    # CoinGecko IDs are used rather than ambiguous SYMBOL-USD tickers.
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 100, "page": 1,
                    "sparkline": "false"},
            headers={"User-Agent": "ATLAS-paper-trading/1.0"}, timeout=TIMEOUT,
        )
        response.raise_for_status()
        for coin in response.json():
            coin_id = str(coin.get("id", "")).strip()
            name = str(coin.get("name") or coin_id).strip()
            market_cap = float(coin.get("market_cap") or 0)
            volume = float(coin.get("total_volume") or 0)
            if (coin_id and re.fullmatch(r"[a-z0-9-]{1,80}", coin_id)
                    and market_cap >= CRYPTO_MARKET_CAP_MIN and volume >= CRYPTO_VOLUME_MIN):
                symbol = "CG:" + coin_id
                found.setdefault(symbol, {
                    "symbol": symbol, "name": name, "kind": "crypto",
                    "source": "CoinGecko", "market_cap_usd": market_cap, "volume_24h_usd": volume,
                })
    except Exception as exc:
        log.warning("ATLAS discovery CoinGecko unavailable: %s", str(exc)[:160])

    for symbol, name in FALLBACK_EQUITIES.items():
        found.setdefault(symbol, {"symbol": symbol, "name": name, "kind": "equity_or_etf", "source": "fallback universe"})

    # Keep the first tranche from screeners and add a bounded number of crypto assets.
    equities = [v for v in found.values() if v["kind"] == "equity_or_etf"]
    cryptos = [v for v in found.values() if v["kind"] == "crypto"]
    result = equities[:max(5, MAX_ASSETS // 2)] + cryptos[:max(3, MAX_ASSETS // 2)]
    _CACHE.update({"at": now, "assets": result})
    log.info("ATLAS discovery completed: %s candidates (%s equity/ETF, %s crypto)",
             len(result), sum(a["kind"] == "equity_or_etf" for a in result),
             sum(a["kind"] == "crypto" for a in result))
    return list(result)

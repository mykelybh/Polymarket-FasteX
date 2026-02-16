#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles. Agents can customize signal source.

Usage:
    python fast_trader.py              # Dry run (show opportunities, no trades)
    python fast_trader.py --live       # Execute real trades
    python fast_trader.py --positions  # Show current fast market positions
    python fast_trader.py --quiet      # Only output on trades/errors

Requires:
    SIMMER_API_KEY environment variable (get from simmer.markets/dashboard)
"""

import os
import sys
import json
import math
import argparse
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote

# Force line-buffered stdout for non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# Optional: Trade Journal integration
try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False
        def log_trade(*args, **kwargs):
            pass

# =============================================================================
# Configuration (config.json > env vars > defaults)
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "env": "SIMMER_SPRINT_ENTRY", "type": float,
                        "help": "Min price divergence from 50¢ to trigger trade"},
    "min_momentum_pct": {"default": 0.5, "env": "SIMMER_SPRINT_MOMENTUM", "type": float,
                         "help": "Min BTC % move in lookback window to trigger"},
    "max_position": {"default": 5.0, "env": "SIMMER_SPRINT_MAX_POSITION", "type": float,
                     "help": "Max $ per trade"},
    "signal_source": {"default": "binance", "env": "SIMMER_SPRINT_SIGNAL", "type": str,
                      "help": "Price feed source (binance, coingecko)"},
    "lookback_minutes": {"default": 5, "env": "SIMMER_SPRINT_LOOKBACK", "type": int,
                         "help": "Minutes of price history for momentum calc"},
    "min_time_remaining": {"default": 60, "env": "SIMMER_SPRINT_MIN_TIME", "type": int,
                           "help": "Skip fast_markets with less than this many seconds remaining"},
    "asset": {"default": "BTC", "env": "SIMMER_SPRINT_ASSET", "type": str,
              "help": "Asset to trade (BTC, ETH, SOL)"},
    "window": {"default": "5m", "env": "SIMMER_SPRINT_WINDOW", "type": str,
               "help": "Market window duration (5m or 15m)"},
    "volume_confidence": {"default": True, "env": "SIMMER_SPRINT_VOL_CONF", "type": bool,
                          "help": "Weight signal by volume (higher volume = more confident)"},
}

TRADE_SOURCE = "sdk:fastloop"
SMART_SIZING_PCT = 0.05  # 5% of balance per trade
MIN_SHARES_PER_ORDER = 5  # Polymarket minimum

# Asset → Binance symbol mapping
ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

# Asset → Gamma API search patterns
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}


def _load_config(schema, skill_file, config_filename="config.json"):
    """Load config with priority: config.json > env vars > defaults."""
    from pathlib import Path
    config_path = Path(skill_file).parent / config_filename
    file_cfg = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                file_cfg = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    result = {}
    for key, spec in schema.items():
        if key in file_cfg:
            result[key] = file_cfg[key]
        elif spec.get("env") and os.environ.get(spec["env"]):
            val = os.environ.get(spec["env"])
            type_fn = spec.get("type", str)
            try:
                if type_fn == bool:
                    result[key] = val.lower() in ("true", "1", "yes")
                else:
                    result[key] = type_fn(val)
            except (ValueError, TypeError):
                result[key] = spec.get("default")
        else:
            result[key] = spec.get("default")
    return result


def _get_config_path(skill_file, config_filename="config.json"):
    from pathlib import Path
    return Path(skill_file).parent / config_filename


def _update_config(updates, skill_file, config_filename="config.json"):
    """Update config.json with new values."""
    from pathlib import Path
    config_path = Path(skill_file).parent / config_filename
    existing = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    existing.update(updates)
    with open(config_path, "w") as f:
        json.dump(existing, f, indent=2)
    return existing


# Load config
cfg = _load_config(CONFIG_SCHEMA, __file__)
ENTRY_THRESHOLD = cfg["entry_threshold"]
MIN_MOMENTUM_PCT = cfg["min_momentum_pct"]
MAX_POSITION_USD = cfg["max_position"]
SIGNAL_SOURCE = cfg["signal_source"]
LOOKBACK_MINUTES = cfg["lookback_minutes"]
MIN_TIME_REMAINING = cfg["min_time_remaining"]
ASSET = cfg["asset"].upper()
WINDOW = cfg["window"]  # "5m" or "15m"
VOLUME_CONFIDENCE = cfg["volume_confidence"]


# =============================================================================
# API Helpers
# =============================================================================

SIMMER_BASE = os.environ.get("SIMMER_API_BASE", "https://api.simmer.markets")


def get_api_key():
    key = os.environ.get("SIMMER_API_KEY")
    if not key:
        print("Error: SIMMER_API_KEY environment variable not set")
        print("Get your API key from: simmer.markets/dashboard → SDK tab")
        sys.exit(1)
    return key


def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    """Make an HTTP request. Returns parsed JSON or None on error."""
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "simmer-fastloop_market/1.0"
        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            return {"error": error_body.get("detail", str(e)), "status_code": e.code}
        except Exception:
            return {"error": str(e), "status_code": e.code}
    except URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def simmer_request(path, method="GET", data=None, api_key=None):
    """Make a Simmer API request."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return _api_request(f"{SIMMER_BASE}{path}", method=method, data=data, headers=headers)


# =============================================================================
# Sprint Market Discovery
# =============================================================================

def discover_fast_market_markets(asset="BTC", window="5m"):
    """Find active fast markets on Polymarket via Gamma API."""
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=20&closed=false&tag=crypto&order=createdAt&ascending=false"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        matches_window = f"-{window}-" in slug
        if any(p in q for p in patterns) and matches_window:
            condition_id = m.get("conditionId", "")
            closed = m.get("closed", False)
            if not closed and slug:
                # Parse end time from question (e.g., "5:30AM-5:35AM ET")
                end_time = _parse_fast_market_end_time(m.get("question", ""))
                markets.append({
                    "question": m.get("question", ""),
                    "slug": slug,
                    "condition_id": condition_id,
                    "end_time": end_time,
                    "outcomes": m.get("outcomes", []),
                    "outcome_prices": m.get("outcomePrices", "[]"),
                    "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                })
    return markets


def _parse_fast_market_end_time(question):
    """Parse end time from fast market question.
    e.g., 'Bitcoin Up or Down - February 15, 5:30AM-5:35AM ET' → datetime
    """
    import re
    # Match pattern: "Month Day, StartTime-EndTime ET"
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None
    try:
        date_str = match.group(1)
        time_str = match.group(2)
        year = datetime.now(timezone.utc).year
        dt_str = f"{date_str} {year} {time_str}"
        # Parse as ET (UTC-5)
        dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p")
        # Convert ET to UTC (+5 hours)
        dt = dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)
        return dt
    except Exception:
        return None


def find_best_fast_market(markets):
    """Pick the best fast_market to trade: soonest expiring with enough time remaining."""
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if not end_time:
            continue
        remaining = (end_time - now).total_seconds()
        if remaining > MIN_TIME_REMAINING:
            candidates.append((remaining, m))

    if not candidates:
        return None
    # Sort by soonest expiring
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# =============================================================================
# CEX Price Signal (Patched Binance)
# =============================================================================

def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    """Get price momentum from Binance public API (Railpack-safe)."""
    import requests
    import time

    base_urls = [
        "https://api.binance.com",
        "https://api1.binance.com",
        "https://api3.binance.com",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    for base in base_urls:
        url = f"{base}/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": "1m",
            "limit": lookback_minutes
        }

        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=10
            )

            if response.status_code == 429:
                time.sleep(2)
                continue

            response.raise_for_status()
            candles = response.json()

            if not candles or len(candles) < 2:
                continue

            price_then = float(candles[0][1])
            price_now = float(candles[-1][4])

            momentum_pct = ((price_now - price_then) / price_then) * 100
            direction = "up" if momentum_pct > 0 else "down"

            volumes = [float(c[5]) for c in candles]
            avg_volume = sum(volumes) / len(volumes)
            latest_volume = volumes[-1]
            volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

            return {
                "momentum_pct": momentum_pct,
                "direction": direction,
                "price_now": price_now,
                "price_then": price_then,
                "avg_volume": avg_volume,
                "latest_volume": latest_volume,
                "volume_ratio": volume_ratio,
                "candles": len(candles),
            }

        except Exception:
            time.sleep(1)
            continue

    return None


def get_coingecko_momentum(asset="bitcoin", lookback_minutes=5):
    """Fallback: get price from CoinGecko (less accurate, ~1-2 min lag)."""
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={asset}&vs_currencies=usd"
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return None
    price_now = result.get(asset, {}).get("usd")
    if not price_now:
        return None
    # CoinGecko doesn't give candle data on free tier, so just return current price
    # Agent would need to track history across calls for momentum
    return {
        "momentum_pct": 0,  # Can't calculate without history
        "direction": "neutral",
        "price_now": price_now,
        "price_then": price_now,
        "avg_volume": 0,
        "latest_volume": 0,
        "volume_ratio": 1.0,
        "candles": 0,
    }


COINGECKO_ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


def get_momentum(asset="BTC", source="binance", lookback=5):
    """Get price momentum from configured source."""
    if source == "binance":
        symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
        return get_binance_momentum(symbol, lookback)
    elif source == "coingecko":
        cg_id = COINGECKO_ASSETS.get(asset, "bitcoin")
        return get_coingecko_momentum(cg_id, lookback)
    else:
        return None


# =============================================================================
# Import & Trade
# =============================================================================

# ... rest of your file remains exactly the same ...


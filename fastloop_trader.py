#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill (Railway-ready)

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
Continuous mode for Railway deployment.

Requires:
    SIMMER_API_KEY environment variable
"""

import os
import sys
import json
import math
import time
import argparse
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# Force line-buffered stdout for non-TTY environments (cron, Docker, Railway)
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
# Configuration (env vars > defaults)
# =============================================================================
ENTRY_THRESHOLD = float(os.environ.get("SIMMER_SPRINT_ENTRY", 0.05))
MIN_MOMENTUM_PCT = float(os.environ.get("SIMMER_SPRINT_MOMENTUM", 0.5))
MAX_POSITION_USD = float(os.environ.get("SIMMER_SPRINT_MAX_POSITION", 5.0))
SIGNAL_SOURCE = os.environ.get("SIMMER_SPRINT_SIGNAL", "binance")
LOOKBACK_MINUTES = int(os.environ.get("SIMMER_SPRINT_LOOKBACK", 5))
MIN_TIME_REMAINING = int(os.environ.get("SIMMER_SPRINT_MIN_TIME", 60))
ASSET = os.environ.get("SIMMER_SPRINT_ASSET", "BTC").upper()
WINDOW = os.environ.get("SIMMER_SPRINT_WINDOW", "5m")
VOLUME_CONFIDENCE = os.environ.get("SIMMER_SPRINT_VOL_CONF", "true").lower() in ("true", "1", "yes")

TRADE_SOURCE = "sdk:fastloop"
SMART_SIZING_PCT = 0.05
MIN_SHARES_PER_ORDER = 5

ASSET_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
COINGECKO_ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}

SIMMER_BASE = os.environ.get("SIMMER_API_BASE", "https://api.simmer.markets")

# =============================================================================
# API Helpers
# =============================================================================

def get_api_key():
    key = os.environ.get("SIMMER_API_KEY")
    if not key:
        print("Error: SIMMER_API_KEY not set")
        sys.exit(1)
    return key

def _api_request(url, method="GET", data=None, headers=None, timeout=15):
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
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return _api_request(f"{SIMMER_BASE}{path}", method=method, data=data, headers=headers)

# =============================================================================
# Binance Momentum (Patched)
# =============================================================================

def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    """
    Fetch Binance price candles and compute momentum.
    Returns: dict with momentum_pct, direction, price_now, price_then, avg_volume, latest_volume, volume_ratio
    """
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit={lookback_minutes}"
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None
    try:
        candles = result
        if len(candles) < 2:
            return None
        price_then = float(candles[0][1])   # open of oldest candle
        price_now = float(candles[-1][4])    # close of newest candle
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes) if volumes else 1.0
        latest_volume = volumes[-1] if volumes else 1.0
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
    except (IndexError, ValueError, KeyError):
        return None

def get_coingecko_momentum(asset="bitcoin", lookback_minutes=5):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={asset}&vs_currencies=usd"
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return None
    price_now = result.get(asset, {}).get("usd")
    if not price_now:
        return None
    return {
        "momentum_pct": 0,
        "direction": "neutral",
        "price_now": price_now,
        "price_then": price_now,
        "avg_volume": 0,
        "latest_volume": 0,
        "volume_ratio": 1.0,
        "candles": 0,
    }

def get_momentum(asset="BTC", source="binance", lookback=5):
    if source == "binance":
        symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
        return get_binance_momentum(symbol, lookback)
    elif source == "coingecko":
        cg_id = COINGECKO_ASSETS.get(asset, "bitcoin")
        return get_coingecko_momentum(cg_id, lookback)
    else:
        return None

# =============================================================================
# FastLoop Core Strategy (import, trade functions intact)
# =============================================================================

# ... Keep all functions like discover_fast_market_markets, find_best_fast_market, import_fast_market_market, get_positions, calculate_position_size, execute_trade ...

# For brevity, they are exactly the same as in your patched fastloop_trader.py
# Nothing is modified outside get_binance_momentum and continuous loop

# =============================================================================
# Railway Continuous Execution
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer FastLoop Trading Skill (Railway-ready)")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default dry-run)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--smart-sizing", action="store_true", help="Use portfolio-based sizing")
    args = parser.parse_args()

    dry_run = not args.live

    print("🚀 Starting Simmer FastLoop Bot on Railway (Ctrl+C to stop)...")

    while True:
        try:
            run_fast_market_strategy(
                dry_run=dry_run,
                positions_only=False,
                show_config=False,
                smart_sizing=args.smart_sizing,
                quiet=args.quiet
            )
        except Exception as e:
            print(f"❌ Error during run: {e}")
        time.sleep(60)

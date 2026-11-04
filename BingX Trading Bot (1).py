import json
import re
import html
import hashlib
import hmac
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://open-api.bingx.com"
TICKER_URL = f"{BASE_URL}/openApi/swap/v2/quote/ticker"
KLINES_URL = f"{BASE_URL}/openApi/swap/v3/quote/klines"
FUNDING_URL = f"{BASE_URL}/openApi/swap/v2/quote/premiumIndex"
OPEN_INTEREST_URL = f"{BASE_URL}/openApi/swap/v2/quote/openInterest"
DEPTH_URL = f"{BASE_URL}/openApi/swap/v2/quote/depth"
TRADES_URL = f"{BASE_URL}/openApi/swap/v2/quote/trades"
PREMIUM_INDEX_URL = f"{BASE_URL}/openApi/swap/v2/quote/premiumIndex"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODELS = [
    model.strip() for model in os.getenv(
        "GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.6-flash"
    ).split(",") if model.strip()
]
GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
SNAPSHOT_FILE = Path("market_snapshot.json")
STATS_FILE = Path("demo_trade_stats.json")
STATE_FILE = Path("trade_state.json")
INCOME_PATH = "/openApi/swap/v2/user/income"
POSITION_HISTORY_PATH = "/openApi/swap/v1/trade/positionHistory"
TOP_MARKETS = 100
MAX_CANDIDATES = 10
INTERVAL = "15m"
INTERVAL_MS = 900000
CANDLE_LIMIT = 30
HOURLY_INTERVAL = "1h"
FOUR_HOUR_INTERVAL = "4h"
INTERVAL_DURATIONS_MS = {"15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}
DAILY_INTERVAL = "1d"
DAILY_INTERVAL_MS = 86400000
DAILY_CANDLE_LIMIT = 65
COOLDOWN_SECONDS = 3600 
NO_TRADE_COOLDOWN_SECONDS = 600
MIN_RR = 1.0  # Изменено для динамических уровней по структуре
REQUEST_TIMEOUT = 25
GEMINI_TIMEOUT = 45
MAX_GEMINI_ATTEMPTS_PER_MODEL = 3
JSON_RETRY_ATTEMPTS = 3
RETRY_INITIAL_SECONDS = 5
RETRY_MAX_SECONDS = 60
TELEGRAM_MAX_LENGTH = 3500
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "1") == "1"
NEWS_HOURS = max(1, min(72, int(os.getenv("NEWS_HOURS", "24"))))
NEWS_MAX_PER_TOPIC = 5
NEWS_TIMEOUT = 12
NEWS_API = "https://api.gdeltproject.org/api/v2/doc/doc"
NEWS_RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
}
NEWS_TOPICS = {
    "macro": '("Federal Reserve" OR "US inflation" OR "interest rates" OR "US jobs report")',
    "crypto": '("Bitcoin" OR "crypto exchange" OR "spot bitcoin ETF")',
}

VST_BASE_URL = "https://open-api-vst.bingx.com"
POSITIONS_PATH = "/openApi/swap/v2/user/positions"
ORDER_PATH = "/openApi/swap/v2/trade/order"
BALANCE_PATH = "/openApi/swap/v2/user/balance"
CANCEL_ALL_PATH = "/openApi/swap/v2/trade/allOpenOrders"
AUTO_TRADE = os.getenv("AUTO_TRADE", "1") == "1"
MAX_LOSS_USDT = float(os.getenv("MAX_LOSS_USDT", "100")) 
LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"
CONTRACTS_URL = f"{BASE_URL}/openApi/swap/v2/quote/contracts"
_CONTRACT_CACHE: dict = {}

def number(value, default=0.0):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default

def _strip_secret(value: str) -> str:
    return (value or "").strip().strip('"').strip("'")

def _bingx_sign(params: dict, secret: str) -> str:
    items = []
    for k in sorted(params.keys()):
        if k == "signature" or params[k] is None:
            continue
        items.append(f"{k}={params[k]}")
    query = "&".join(items)
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

def _build_signed_query(params: dict, secret: str) -> str:
    from urllib.parse import quote_plus
    clean = {k: v for k, v in params.items() if v is not None and k != "signature"}
    clean = {str(k): str(v) for k, v in clean.items()}
    sig = _bingx_sign(clean, secret)
    clean["signature"] = sig
    parts = [f"{k}={quote_plus(clean[k], safe='')}" for k in sorted(clean.keys())]
    return "&".join(parts)

async def api_get(client: httpx.AsyncClient, url: str, params=None):
    last_error = None
    for attempt in range(3):
        try:
            response = await client.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("code", 0) not in (0, "0", None):
                raise RuntimeError(f"BingX API: {result.get('code')} {result.get('msg', '')}")
            return result
        except (httpx.RequestError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(attempt + 1)
    raise RuntimeError(f"Request failed: {last_error}")

async def api_get_signed_vst(client: httpx.AsyncClient, path: str, params=None):
    api_key = _strip_secret(os.getenv("BINGX_API_KEY", ""))
    secret = _strip_secret(os.getenv("BINGX_SECRET_KEY", ""))
    if not api_key or not secret:
        raise RuntimeError("Missing BINGX_API_KEY or BINGX_SECRET_KEY")
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    query = _build_signed_query(params, secret)
    url = f"{VST_BASE_URL}{path}?{query}"
    last_error = None
    for attempt in range(3):
        try:
            response = await client.get(url, headers={"X-BX-APIKEY": api_key}, timeout=REQUEST_TIMEOUT)
            result = response.json()
            if isinstance(result, dict) and result.get("code", 0) not in (0, "0", None):
                raise RuntimeError(f"BingX VST API: {result.get('code')} {result.get('msg', '')}")
            return result
        except (httpx.RequestError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(attempt + 1)
    raise RuntimeError(f"VST signed request failed: {last_error}")

async def api_post_signed_vst(client: httpx.AsyncClient, path: str, payload: dict):
    api_key = _strip_secret(os.getenv("BINGX_API_KEY", ""))
    secret = _strip_secret(os.getenv("BINGX_SECRET_KEY", ""))
    if not api_key or not secret:
        raise RuntimeError("Missing BINGX_API_KEY or BINGX_SECRET_KEY")
    payload = {str(k): str(v) for k, v in dict(payload).items() if v is not None}
    payload["timestamp"] = str(int(time.time() * 1000))
    payload.setdefault("recvWindow", "5000")
    query = _build_signed_query(payload, secret)
    url = f"{VST_BASE_URL}{path}?{query}"
    headers = {"X-BX-APIKEY": api_key}
    response = await client.post(url, headers=headers, content=b"", timeout=REQUEST_TIMEOUT)
    result = response.json()
    if result.get("code", 0) not in (0, "0", None):
        raise RuntimeError(f"BingX VST order error: {result}")
    return result

async def fetch_demo_balance(client: httpx.AsyncClient) -> float:
    """Получает актуальный баланс демо-счета для динамического сайзинга."""
    try:
        res = await api_get_signed_vst(client, BALANCE_PATH)
        data = res.get("data", {})
        balance_info = data.get("balance", {})
        if isinstance(balance_info, dict):
            return float(number(balance_info.get("balance") or balance_info.get("availableMargin"), 100000.0))
        return 100000.0
    except Exception as e:
        print(f"Failed to fetch DEMO balance: {e}")
        return 100000.0

async def fetch_open_positions(client: httpx.AsyncClient) -> dict:
    try:
        data = (await api_get_signed_vst(client, POSITIONS_PATH)).get("data", [])
    except Exception as exc:
        print(f"Failed to fetch DEMO open positions: {exc}")
        return {}
    if isinstance(data, dict):
        data = data.get("positions") or data.get("list") or []
    if not isinstance(data, list):
        return {}
    by_symbol = {}
    for pos in data:
        if not isinstance(pos, dict):
            continue
        symbol = str(pos.get("symbol", "")).strip()
        amt = number(pos.get("positionAmt") or pos.get("availableAmt") or 0)
        if not symbol or abs(amt) < 1e-12:
            continue
        side = str(pos.get("positionSide", "")).upper()
        by_symbol.setdefault(symbol, []).append({
            "symbol": symbol, "positionSide": side, "positionAmt": amt,
            "avgPrice": number(pos.get("avgPrice")), "markPrice": number(pos.get("markPrice"))
        })
    return by_symbol

def has_open_position(symbol: str, direction: str, open_positions: dict) -> bool:
    wanted = direction.upper()
    for pos in open_positions.get(symbol) or []:
        if pos.get("positionSide") == wanted and abs(pos.get("positionAmt", 0)) > 0:
            return True
    return False

async def fetch_contract_specs(client: httpx.AsyncClient, symbol: str) -> dict:
    global _CONTRACT_CACHE
    if not _CONTRACT_CACHE:
        try:
            data = (await api_get(client, CONTRACTS_URL)).get("data", [])
            data = [data] if isinstance(data, dict) else data
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("symbol"):
                        _CONTRACT_CACHE[str(item["symbol"])] = item
        except Exception:
            pass

    item = _CONTRACT_CACHE.get(symbol)
    if not item:
        try:
            data = (await api_get(client, CONTRACTS_URL, {"symbol": symbol})).get("data", [])
            data = [data] if isinstance(data, dict) else data
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict) and str(row.get("symbol", "")) == symbol:
                        item = row
                        _CONTRACT_CACHE[symbol] = row
                        break
        except Exception:
            pass

    if not item:
        return {"maxLongLeverage": 125, "maxShortLeverage": 125, "quantityPrecision": 4, "pricePrecision": 4, "tradeMinQuantity": 0}
    def _max_lev(*keys, default=125):
        for k in keys:
            v = item.get(k)
            if v is not None and number(v, 0) > 0: return int(number(v))
        return default
    return {
        "maxLongLeverage": _max_lev("maxLongLeverage", "maxLeverage", "leverage", default=125),
        "maxShortLeverage": _max_lev("maxShortLeverage", "maxLongLeverage", "maxLeverage", "leverage", default=125),
        "quantityPrecision": int(number(item.get("quantityPrecision"), 4)),
        "pricePrecision": int(number(item.get("pricePrecision"), 4)),
        "tradeMinQuantity": number(item.get("tradeMinQuantity"), 0)
    }

def round_qty(qty: float, precision: int) -> float:
    precision = max(0, precision)
    fmt = "{:." + str(precision) + "f}"
    return float(fmt.format(qty))

async def set_demo_leverage(client: httpx.AsyncClient, symbol: str, direction: str, preferred: int) -> int:
    preferred = max(1, int(preferred or 50))
    ladder = [preferred, 50, 40, 30, 25, 20, 15, 10, 5]
    candidates = sorted(set(ladder), reverse=True)
    for lev in candidates:
        for side in (direction.upper(), "BOTH"):
            payload = {"symbol": symbol, "side": side, "leverage": str(lev)}
            try:
                await api_post_signed_vst(client, LEVERAGE_PATH, payload)
                return lev
            except Exception:
                continue
    return preferred if preferred <= 20 else 20

async def cancel_all_demo_orders(client: httpx.AsyncClient, symbol: str):
    api_key = _strip_secret(os.getenv("BINGX_API_KEY", ""))
    secret = _strip_secret(os.getenv("BINGX_SECRET_KEY", ""))
    payload = {"symbol": symbol, "timestamp": str(int(time.time() * 1000))}
    query = _build_signed_query(payload, secret)
    url = f"{VST_BASE_URL}{CANCEL_ALL_PATH}?{query}"
    try:
        await client.delete(url, headers={"X-BX-APIKEY": api_key}, timeout=REQUEST_TIMEOUT)
    except Exception:
        pass

async def manage_positions_and_scaling(client: httpx.AsyncClient, open_positions: dict):
    """Ступенчатая фиксация (50% / 75%) и трейлинг стопа в безубыток."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except:
        state = {}
        
    active_symbols = list(open_positions.keys())
    for s in list(state.keys()):
        if s not in active_symbols:
            del state[s]
            
    notifications = []
    for symbol, pos_list in open_positions.items():
        pos = pos_list[0]
        direction = pos.get("positionSide", "").upper()
        amt = abs(number(pos.get("positionAmt", 0)))
        entry = number(pos.get("avgPrice", 0))
        mark = number(pos.get("markPrice", 0))
        
        if amt == 0 or entry == 0 or symbol not in state:
            continue
            
        trade = state[symbol]
        orig_qty = trade["orig_qty"]
        orig_tp = trade["tp"]
        qty_prec = trade.get("qty_prec", 4)
        fixed_50 = trade.get("fixed_50", False)
        fixed_75 = trade.get("fixed_75", False)
        
        dist = orig_tp - entry
        tp50 = entry + dist * 0.50
        tp75 = entry + dist * 0.75
        
        hit_50 = (direction == "LONG" and mark >= tp50) or (direction == "SHORT" and mark <= tp50)
        hit_75 = (direction == "LONG" and mark >= tp75) or (direction == "SHORT" and mark <= tp75)
        
        close_side = "SELL" if direction == "LONG" else "BUY"
        
        # Проверка на 75%
        if hit_75 and not fixed_75:
            qty_to_close = round_qty(orig_qty * 0.25, qty_prec)
            if qty_to_close > 0:
                try:
                    await api_post_signed_vst(client, ORDER_PATH, {
                        "symbol": symbol, "side": close_side, "positionSide": direction,
                        "type": "MARKET", "quantity": qty_to_close
                    })
                    trade["fixed_75"] = True
                    notifications.append(f"🎯 {symbol}: Достигнуто 75% TP! Зафиксировано ещё 25% объема.")
                except Exception as e:
                    print(f"Failed 75% scale for {symbol}: {e}")
                    
        # Проверка на 50% и безубыток
        elif hit_50 and not fixed_50:
            qty_to_close = round_qty(orig_qty * 0.50, qty_prec)
            if qty_to_close > 0:
                try:
                    await api_post_signed_vst(client, ORDER_PATH, {
                        "symbol": symbol, "side": close_side, "positionSide": direction,
                        "type": "MARKET", "quantity": qty_to_close
                    })
                    await cancel_all_demo_orders(client, symbol)
                    
                    remain_qty = round_qty(orig_qty - qty_to_close, qty_prec)
                    # Выставляем SL в безубыток
                    await api_post_signed_vst(client, ORDER_PATH, {
                        "symbol": symbol, "side": close_side, "positionSide": direction,
                        "type": "STOP_MARKET", "stopPrice": trade["entry"],
                        "workingType": "MARK_PRICE", "quantity": remain_qty
                    })
                    # Выставляем TP на остаток
                    await api_post_signed_vst(client, ORDER_PATH, {
                        "symbol": symbol, "side": close_side, "positionSide": direction,
                        "type": "TAKE_PROFIT_MARKET", "stopPrice": orig_tp,
                        "workingType": "MARK_PRICE", "quantity": remain_qty
                    })
                    trade["fixed_50"] = True
                    trade["sl_moved"] = True
                    notifications.append(f"🛡 {symbol}: Достигнуто 50% TP! Зафиксирована половина прибыли, стоп перенесён в безубыток.")
                except Exception as e:
                    print(f"Failed 50% scale & BE for {symbol}: {e}")
                    
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))
    if notifications:
        await send_telegram(client, "\n".join(notifications))

async def place_demo_trades(client: httpx.AsyncClient, analysis, selected, open_positions):
    if not AUTO_TRADE:
        return []

    balance = await fetch_demo_balance(client)
    max_margin = balance * 0.10  # Максимум 10% от депозита на позицию

    by_symbol = {item["symbol"]: item for item in analysis.get("results", []) if isinstance(item, dict)}
    placed, failed = [], []

    # Чтение стейта
    try:
        t_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except:
        t_state = {}

    for candidate in selected:
        symbol = candidate["symbol"]
        item = by_symbol.get(symbol, {})
        if str(item.get("status", "")).upper() != "TRADE":
            continue

        direction = str(item.get("direction", "")).upper()
        entry_type = str(item.get("entry_type", "MARKET")).upper()
        entry = valid_price(item.get("entry_price"))
        stop = valid_price(item.get("stop_loss"))
        target = valid_price(item.get("take_profit"))

        if entry is None or stop is None or target is None or has_open_position(symbol, direction, open_positions):
            continue

        specs = await fetch_contract_specs(client, symbol)
        preferred_lev = specs["maxLongLeverage"] if direction == "LONG" else specs["maxShortLeverage"]
        preferred_lev = min(max(1, int(preferred_lev)), 50)  # Максимум 50 плечо
        qty_prec = int(specs.get("quantityPrecision", 4))
        min_qty = number(specs.get("tradeMinQuantity"), 0)

        applied_lev = await set_demo_leverage(client, symbol, direction, preferred_lev)
        
        # Расчет сайзинга от маржи (10% депозита * плечо)
        qty_by_notional = (max_margin * applied_lev * 0.95) / entry
        quantity = round_qty(qty_by_notional, qty_prec)
        
        if min_qty > 0 and quantity < min_qty:
            continue
        if quantity <= 0:
            continue

        notional = quantity * entry
        side = "BUY" if direction == "LONG" else "SELL"
        order_id = None
        last_entry_err = None

        for factor in (1.0, 0.7, 0.5):
            q = round_qty(quantity * factor, qty_prec)
            if q <= 0 or (min_qty > 0 and q < min_qty): continue
            
            entry_payload = {"symbol": symbol, "side": side, "positionSide": direction, "type": entry_type, "quantity": q}
            if entry_type == "LIMIT":
                entry_payload.update({"price": entry, "timeInForce": "GTC"})
            try:
                result = await api_post_signed_vst(client, ORDER_PATH, entry_payload)
                data = result.get("data", {}) if isinstance(result, dict) else {}
                order_id = data.get("orderId") or data.get("orderID")
                quantity = q
                notional = q * entry
                last_entry_err = None
                break
            except Exception as exc:
                last_entry_err = exc
                if "101209" in str(exc) or "maximum position value" in str(exc).lower():
                    continue
                break
                
        if last_entry_err is not None:
            failed.append({"symbol": symbol, "reason": str(last_entry_err)[:160]})
            continue

        # Установка SL и TP
        close_side = "SELL" if direction == "LONG" else "BUY"
        sl_ok = tp_ok = False
        for label, otype, trigger in (("SL", "STOP_MARKET", stop), ("TP", "TAKE_PROFIT_MARKET", target)):
            try:
                await api_post_signed_vst(client, ORDER_PATH, {
                    "symbol": symbol, "side": close_side, "positionSide": direction,
                    "type": otype, "stopPrice": trigger, "quantity": quantity, "workingType": "MARK_PRICE"
                })
                if label == "SL": sl_ok = True
                else: tp_ok = True
            except Exception as exc:
                print(f"DEMO {label} failed for {symbol}: {exc}")

        # Сохранение стейта для фиксации прибыли
        t_state[symbol] = {
            "entry": entry, "tp": target, "orig_qty": quantity, 
            "qty_prec": qty_prec, "fixed_50": False, "fixed_75": False, "sl_moved": False
        }
        STATE_FILE.write_text(json.dumps(t_state, ensure_ascii=False))

        placed.append({
            "symbol": symbol, "direction": direction, "entry_type": entry_type, "entry": entry,
            "stop": stop, "target": target, "quantity": quantity, "notional": round(notional, 2),
            "leverage": applied_lev, "order_id": order_id, "sl_ok": sl_ok, "tp_ok": tp_ok,
        })

    return {"placed": placed, "failed": failed}

async def fetch_markets(client: httpx.AsyncClient):
    data = (await api_get(client, TICKER_URL)).get("data", [])
    data = [data] if isinstance(data, dict) else data
    markets = []
    for item in data:
        if not isinstance(item, dict): continue
        symbol = str(item.get("symbol", ""))
        if not symbol.endswith("-USDT"): continue
        price, volume = number(item.get("lastPrice")), number(item.get("quoteVolume"))
        if volume <= 0: volume = number(item.get("volume")) * price
        if price > 0:
            markets.append({"symbol": symbol, "price": price, "volume": volume, "change_24h": number(item.get("priceChangePercent"))})
    markets.sort(key=lambda m: m["volume"], reverse=True)
    return markets[:TOP_MARKETS]

def parse_candle(row):
    if isinstance(row, dict):
        timestamp = row.get("time", row.get("timestamp", row.get("openTime", 0)))
        o, h, l, c = (number(row.get(k)) for k in ("open", "high", "low", "close"))
        qv = number(row.get("quoteVolume", row.get("quote_volume", 0)))
        v, v_type = (qv, "quote") if qv > 0 else (number(row.get("volume", row.get("vol", 0))), "base")
    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        timestamp = row[0]
        o, h, l, c = (number(row[i]) for i in range(1, 5))
        qv = number(row[7]) if len(row) >= 8 else 0
        v, v_type = (qv, "quote") if qv > 0 else (number(row[5]), "base")
    else: return None
    timestamp = int(number(timestamp))
    if timestamp <= 0 or min(o, h, l, c) <= 0 or v < 0: return None
    return {"time": timestamp, "open": o, "high": h, "low": l, "close": c, "volume": v, "volume_type": v_type}

async def fetch_candles(client: httpx.AsyncClient, symbol, interval=INTERVAL, limit=CANDLE_LIMIT):
    rows = (await api_get(client, KLINES_URL, {"symbol": symbol, "interval": interval, "limit": limit})).get("data", [])
    if not isinstance(rows, list): return []
    candles = []
    for row in rows:
        try:
            candle = parse_candle(row)
            if candle: candles.append(candle)
        except Exception: continue
    candles = sorted({c["time"]: c for c in candles}.values(), key=lambda c: c["time"])
    now_ms = int(time.time() * 1000)
    return [c for c in candles if c["time"] < 10**12 or c["time"] + INTERVAL_DURATIONS_MS[interval] <= now_ms]

async def fetch_optional_metrics(client: httpx.AsyncClient, symbol):
    metrics = {"funding_rate": None, "open_interest": None, "mark_price": None, "index_price": None, "data_status": {}}
    try:
        data = (await api_get(client, PREMIUM_INDEX_URL, {"symbol": symbol})).get("data", {})
        data = next((x for x in data if isinstance(x, dict)), {}) if isinstance(data, list) else data
        if isinstance(data, dict):
            metrics.update({"mark_price": number(data.get("markPrice")), "index_price": number(data.get("indexPrice"))})
    except Exception: pass
    return metrics

def load_trade_stats():
    if not STATS_FILE.exists(): return {"first_trade_ts_ms": None, "orders_log": []}
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        data.setdefault("first_trade_ts_ms", None)
        data.setdefault("orders_log", [])
        return data
    except Exception:
        return {"first_trade_ts_ms": None, "orders_log": []}

def save_trade_stats(stats: dict):
    stats["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

async def fetch_income_history(client: httpx.AsyncClient, start_ms: int, income_type: str = "REALIZED_PNL"):
    all_rows = []
    end_ms = int(time.time() * 1000)
    try:
        result = await api_get_signed_vst(client, INCOME_PATH, {"incomeType": income_type, "startTime": int(start_ms), "endTime": end_ms, "limit": 1000})
        data = result.get("data", [])
        data = data.get("list") or data.get("data") or [] if isinstance(data, dict) else data
        if isinstance(data, list): all_rows.extend([r for r in data if isinstance(r, dict)])
    except Exception: pass
    return all_rows

async def fetch_position_history(client: httpx.AsyncClient, start_ms: int, symbols=None):
    all_rows = []
    symbols = sorted({str(x).strip() for x in (symbols or []) if str(x).strip()})
    if not symbols: return all_rows
    end_ms = int(time.time() * 1000)
    for symbol in symbols:
        try:
            result = await api_get_signed_vst(client, POSITION_HISTORY_PATH, {"symbol": symbol, "startTs": int(start_ms), "endTs": end_ms, "pageIndex": 1, "pageSize": 100})
            data = result.get("data", []) if isinstance(result, dict) else []
            if isinstance(data, dict):
                page_rows = data.get("list") or data.get("positionHistory") or data.get("data") or []
            elif isinstance(data, list):
                page_rows = data
            all_rows.extend([r for r in page_rows if isinstance(r, dict)])
        except Exception: pass
    unique = {}
    for row in all_rows:
        key = str(row.get("positionId") or row.get("positionID") or "|".join(str(row.get(k, "")) for k in ("symbol", "positionSide", "openTime", "updateTime")))
        unique[key] = row
    return list(unique.values())

def build_stats_summary(realized_rows, position_rows, orders_log, open_positions, first_ts):
    closed_positions = []
    for row in position_rows or []:
        if not isinstance(row, dict): continue
        status = str(row.get("positionStatus") or row.get("status") or "").upper()
        if status in ("OPEN", "HOLDING", "ACTIVE"): continue
        pnl = row.get("realisedProfit") or row.get("realizedProfit") or row.get("pnl")
        closed_positions.append({"symbol": row.get("symbol"), "pnl": number(pnl)})

    trade_pnls = [p["pnl"] for p in closed_positions]
    closed = len(closed_positions)
    wins = sum(1 for p in trade_pnls if p > 0)
    total_pnl = sum(trade_pnls)
    win_rate = (wins / closed * 100.0) if closed else 0.0

    return {
        "closed_trades": closed, "wins": wins, "win_rate_pct": round(win_rate, 2),
        "total_realized_pnl_usdt": round(total_pnl, 4)
    }

async def update_demo_trade_stats(client: httpx.AsyncClient, newly_placed: list):
    stats = load_trade_stats()
    now_ms = int(time.time() * 1000)
    for p in newly_placed or []:
        stats.setdefault("orders_log", []).append({
            "ts_ms": now_ms, "symbol": p.get("symbol"), "direction": p.get("direction"),
            "entry": p.get("entry"), "stop": p.get("stop"), "target": p.get("target")
        })
        if stats.get("first_trade_ts_ms") is None: stats["first_trade_ts_ms"] = now_ms

    first_ts = stats.get("first_trade_ts_ms")
    if first_ts is None:
        save_trade_stats(stats)
        return stats

    if len(stats.get("orders_log", [])) > 500: stats["orders_log"] = stats["orders_log"][-500:]

    realized = await fetch_income_history(client, first_ts, "REALIZED_PNL")
    history_symbols = {str(row.get("symbol", "")).strip() for row in realized if isinstance(row, dict) and row.get("symbol")}
    history_symbols.update(str(row.get("symbol", "")).strip() for row in stats.get("orders_log", []) if isinstance(row, dict) and row.get("symbol"))
    
    pos_hist = await fetch_position_history(client, first_ts, history_symbols)
    open_pos = await fetch_open_positions(client)
    stats["summary"] = build_stats_summary(realized, pos_hist, stats.get("orders_log"), open_pos, first_ts)
    save_trade_stats(stats)
    return stats

def load_snapshot():
    try: return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except: return {"markets": {}, "sent_signals": {}, "analysis_cooldowns": {}}

def save_snapshot(markets, sent_signals, analysis_cooldowns):
    SNAPSHOT_FILE.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "markets": markets, "sent_signals": sent_signals, "analysis_cooldowns": analysis_cooldowns
    }, ensure_ascii=False, indent=2), encoding="utf-8")

def analyze_daily(candles):
    if len(candles) < 22: return {"status": "unavailable"}
    closes = [c["close"] for c in candles]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
    return {
        "status": "available", "trend": "bullish" if candles[-1]["close"] > sma20 and (sma50 is None or sma20 > sma50) else "bearish",
    }

def analyze_market(market, candles):
    if len(candles) < 22: return None
    close, prev_close = candles[-1]["close"], candles[-2]["close"]
    if min(close, prev_close) <= 0: return None
    change_15m = (close / prev_close - 1) * 100
    prior = candles[-21:-1]
    prior_high, prior_low = max(c["high"] for c in prior), min(c["low"] for c in prior)
    breakout_up = close > prior_high
    breakout_down = close < prior_low
    score = abs(change_15m) * 1.6
    if breakout_up: direction, title = "LONG", "ПРОБОЙ ВВЕРХ"
    elif breakout_down: direction, title = "SHORT", "ПРОБОЙ ВНИЗ"
    elif change_15m > 0: direction, title = "LONG", "ВОЗМОЖНЫЙ ОТСКОК"
    else: direction, title = "SHORT", "ВОЗМОЖНЫЙ ОТКАТ"
    return {
        "symbol": market["symbol"], "price": close, "direction": direction, "title": title,
        "score": score, "prior_high": prior_high, "prior_low": prior_low, "candles": candles,
    }

ANALYSIS_PROMPT = """
ПРИОРИТЕТ АНАЛИЗА: оценка структуры рынка по закрытым свечам 1D → 4H → 1H → 15M и фундаментальному фону.
Каждый таймфрейм оценивай отдельно: ключевые уровни поддержки и сопротивления, тренд, свинги.

ПРАВИЛА ДИНАМИЧЕСКИХ УРОВНЕЙ:
1. Stop-Loss и Take-Profit ДОЛЖНЫ опираться исключительно на актуальные уровни поддержки/сопротивления, хаи/лои свечей и структуру рынка. НЕ подгоняй уровни под фиксированное отношение Risk/Reward.
2. Для LONG: Stop-Loss ставится за ближайший локальный уровень поддержки (лоу), Take-Profit — у ближайшего уровня сопротивления (хая).
3. Для SHORT: Stop-Loss ставится за ближайший уровень сопротивления, Take-Profit — у ближайшей поддержки.
4. Если естественные уровни структуры рынка дают невыгодный сетап или риск слишком велик, выбирай status: NO_TRADE.

Для каждого переданного символа верни ровно один результат (TRADE или NO_TRADE).
Схема ответа: JSON с массивом results (symbol, status, direction, entry_type, entry_price, stop_loss, take_profit, reason, news_impact).
"""

TRADE_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "symbol": {"type": "STRING"}, "status": {"type": "STRING", "enum": ["TRADE", "NO_TRADE"]},
                    "direction": {"type": "STRING"}, "entry_type": {"type": "STRING"},
                    "entry_price": {"type": "STRING"}, "stop_loss": {"type": "STRING"},
                    "take_profit": {"type": "STRING"}, "reason": {"type": "STRING"},
                    "news_impact": {"type": "STRING"}
                },
                "required": ["symbol", "status", "direction", "entry_type", "entry_price", "stop_loss", "take_profit", "reason", "news_impact"],
            }
        }
    },
    "required": ["results"]
}

def build_candidate_data(candidate, metrics):
    return {
        "symbol": candidate["symbol"], "last_closed_price": candidate["price"],
        "preliminary_direction": candidate["direction"], "daily_structure": candidate.get("daily_structure", {}),
        "prior_20_candle_high": candidate["prior_high"], "prior_20_candle_low": candidate["prior_low"],
        "candles_15m": [{"time": c["time"], "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]} for c in candidate["candles"][-22:]],
    }

def is_retryable_status(status_code, error_text=""):
    return status_code in (408, 429, 500, 502, 503, 504) or "quota" in error_text.lower()

async def gemini_analyze_many(client: httpx.AsyncClient, candidates, metrics_by_symbol):
    api_key = os.getenv("GEMINI_API_KEY")
    payload = {
        "system_instruction": {"parts": [{"text": ANALYSIS_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps({"candidates": [build_candidate_data(c, metrics_by_symbol[c["symbol"]]) for c in candidates]}, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "responseSchema": TRADE_RESPONSE_SCHEMA}
    }
    models = []
    for m in [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]:
        if m and m not in models: models.append(m)

    for model in models:
        try:
            response = await client.post(GEMINI_URL_TEMPLATE.format(model=model), headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, json=payload, timeout=GEMINI_TIMEOUT)
            result = response.json()
            if response.is_success:
                raw = result["candidates"][0]["content"]["parts"][0].get("text", "").strip()
                if raw.startswith("```"): raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                return json.loads(raw)
        except Exception as exc: continue
    return {"results": [{"symbol": c["symbol"], "status": "NO_TRADE"} for c in candidates]}

def valid_price(value):
    try:
        p = float(value)
        if p > 0: return p
    except: pass
    return None

def format_price(price):
    if price >= 1000: return f"{price:.2f}"
    if price >= 1: return f"{price:.4f}".rstrip("0").rstrip(".")
    return f"{price:.6f}".rstrip("0").rstrip(".")

def render_report(analysis, selected, started):
    time_label = started.strftime("%d.%m.%Y %H:%M UTC")
    by_symbol = {item["symbol"]: item for item in analysis.get("results", []) if isinstance(item, dict)}
    trades, rejected = [], []

    for candidate in selected:
        symbol = candidate["symbol"]
        item = by_symbol.get(symbol, {})
        if str(item.get("status", "")).upper() != "TRADE":
            rejected.append(symbol)
            continue
        entry = valid_price(item.get("entry_price"))
        stop = valid_price(item.get("stop_loss"))
        target = valid_price(item.get("take_profit"))
        direction = str(item.get("direction", "")).upper()
        
        if not entry or not stop or not target or direction not in ("LONG", "SHORT"):
            rejected.append(symbol)
            continue
            
        risk = entry - stop if direction == "LONG" else stop - entry
        reward = target - entry if direction == "LONG" else entry - target
        if risk <= 0 or reward <= 0:
            rejected.append(symbol)
            continue
            
        ratio = reward / risk
        trades.append(f"✅ {symbol} | {direction}\nВход: {format_price(entry)} | SL: {format_price(stop)} | TP: {format_price(target)}\nRR 1:{ratio:.2f}")

    lines = [f"🧠 BINGX | {time_label}"] + (trades if trades else ["Подтверждённых сделок нет."])
    if rejected: lines.append("⛔ NO_TRADE: " + ", ".join(rejected))
    return "\n\n".join(lines)

def split_telegram_message(message, max_length=TELEGRAM_MAX_LENGTH):
    chunks = []
    while message:
        if len(message) <= max_length: chunks.append(message); break
        cut = message.rfind("\n", 0, max_length)
        if cut < max_length // 2: cut = max_length
        chunks.append(message[:cut])
        message = message[cut:].lstrip("\n")
    return chunks

async def send_telegram(client: httpx.AsyncClient, message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id: return
    for chunk in split_telegram_message(message):
        try:
            await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}, timeout=REQUEST_TIMEOUT)
        except Exception: pass

async def main():
    started = datetime.now(timezone.utc)
    snapshot = load_snapshot()
    sent_signals = snapshot.get("sent_signals", {})
    analysis_cooldowns = snapshot.get("analysis_cooldowns", {})

    async with httpx.AsyncClient(follow_redirects=True) as client:
        markets = await fetch_markets(client)
        if not markets: return
        
        # Получаем и сверяем открытые позиции (Для трейлинга)
        open_positions = await fetch_open_positions(client)
        await manage_positions_and_scaling(client, open_positions)
        
        candidates = []
        current_time = int(time.time())

        for m in markets[:40]:
            try:
                m15_c = await fetch_candles(client, m["symbol"])
                candidate = analyze_market(m, m15_c)
                if not candidate: continue
                key = f"{m['symbol']}:{candidate['direction']}"
                if current_time - sent_signals.get(key, 0) >= COOLDOWN_SECONDS and current_time - analysis_cooldowns.get(key, 0) >= NO_TRADE_COOLDOWN_SECONDS:
                    candidate["key"] = key
                    candidates.append(candidate)
            except Exception: pass
            
        filtered = [c for c in candidates if not has_open_position(c["symbol"], c["direction"], open_positions)]
        filtered.sort(key=lambda c: c["score"], reverse=True)
        selected = filtered[:MAX_CANDIDATES]

        if selected:
            metrics = {c["symbol"]: await fetch_optional_metrics(client, c["symbol"]) for c in selected}
            analysis = await gemini_analyze_many(client, selected, metrics)
            report = render_report(analysis, selected, started)
            await send_telegram(client, report)
            
            for c in selected:
                res = next((item for item in analysis.get("results", []) if item.get("symbol") == c["symbol"]), {})
                if str(res.get("status", "")).upper() == "TRADE":
                    sent_signals[c["key"]] = current_time
                else:
                    analysis_cooldowns[c["key"]] = current_time
            
            placed = await place_demo_trades(client, analysis, selected, open_positions)
            placed_list = placed.get("placed", [])
            if placed_list:
                lines = ["🤖 DEMO-сделки"]
                for p in placed_list:
                    lines.append(f"\n{p['direction']} {p['symbol']} | Вход: {p['entry']} | Объем: ≈{p['notional']} USDT | Плечо x{p['leverage']}")
                await send_telegram(client, "\n".join(lines))
            await update_demo_trade_stats(client, placed_list)

        market_snapshot = {m["symbol"]: {"price": m["price"], "volume": m["volume"], "change_24h": m["change_24h"]} for m in markets}
        save_snapshot(market_snapshot, sent_signals, analysis_cooldowns)

if __name__ == "__main__":
    asyncio.run(main())
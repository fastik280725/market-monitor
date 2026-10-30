import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE_URL = "https://open-api.bingx.com"
TICKER_URL = f"{BASE_URL}/openApi/swap/v2/quote/ticker"
KLINES_URL = f"{BASE_URL}/openApi/swap/v3/quote/klines"
FUNDING_URL = f"{BASE_URL}/openApi/swap/v2/quote/premiumIndex"
OPEN_INTEREST_URL = f"{BASE_URL}/openApi/swap/v2/quote/openInterest"
DEPTH_URL = f"{BASE_URL}/openApi/swap/v2/quote/depth"
TRADES_URL = f"{BASE_URL}/openApi/swap/v2/quote/trades"
PREMIUM_INDEX_URL = f"{BASE_URL}/openApi/swap/v2/quote/premiumIndex"
CONTRACTS_URL = f"{BASE_URL}/openApi/swap/v2/quote/contracts"

# Private requests are pinned to BingX VST demo host. Never change this to BASE_URL.
VST_BASE_URL = "https://open-api-vst.bingx.com"
ORDER_PATH = "/openApi/swap/v2/trade/order"
LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"
POSITIONS_PATH = "/openApi/swap/v2/user/positions"
BALANCE_PATH = "/openApi/swap/v3/user/balance"
POSITION_HISTORY_PATH = "/openApi/swap/v1/trade/positionHistory"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Fallbacks can be overridden in GitHub Actions with GEMINI_FALLBACK_MODELS.
GEMINI_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.1-flash-lite,gemini-2.5-flash-lite",
    ).split(",")
    if model.strip()
]
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
SNAPSHOT_FILE = Path("market_snapshot.json")
TRADES_FILE = Path("trades_history.json")
TOP_MARKETS = 100
MAX_CANDIDATES = 5
INTERVAL = "15m"
INTERVAL_MS = 900000
CANDLE_LIMIT = 30
COOLDOWN_SECONDS = 3600
REQUEST_TIMEOUT = 25
GEMINI_TIMEOUT = 90
REQUEST_PAUSE = 0.12
RETRY_INITIAL_SECONDS = 5
RETRY_MAX_SECONDS = 60
JSON_RETRY_ATTEMPTS = 3
MAX_GEMINI_ATTEMPTS = 5
MIN_REWARD_RISK_RATIO = 2.0
TELEGRAM_MAX_LENGTH = 3500
TARGET_NOTIONAL_USDT = max(0.0, float(os.getenv("TARGET_NOTIONAL_USDT", "5000")))
# Fail closed: demo order placement must be explicitly enabled in the environment.
ENABLE_AUTO_TRADE = os.getenv("ENABLE_AUTO_TRADE", "0").strip().lower() in ("1", "true", "yes")
MAX_DEMO_LEVERAGE = max(1, min(int(os.getenv("MAX_DEMO_LEVERAGE", "3")), 10))
MAX_ENTRY_DEVIATION_PCT = max(0.05, min(float(os.getenv("MAX_ENTRY_DEVIATION_PCT", "0.5")), 2.0))
session = requests.Session()

# Make diagnostics appear in GitHub Actions as soon as they are printed.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass


def number(value, default=0.0):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def api_get(url, params=None):
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("code", 0) not in (0, "0", None):
                raise RuntimeError(f"BingX API: {result.get('code')} {result.get('msg', '')}")
            return result
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"Request failed: {last_error}")


def fetch_markets():
    response = api_get(TICKER_URL)
    data = response.get("data", []) if isinstance(response, dict) else []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise RuntimeError("BingX ticker returned an unexpected data format")
    markets = []
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", ""))
        if not symbol.endswith("-USDT") or not symbol:
            continue
        price = number(item.get("lastPrice"))
        volume = number(item.get("quoteVolume"))
        if volume <= 0:
            volume = number(item.get("volume")) * price
        if price <= 0:
            continue
        markets.append({
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "change_24h": number(item.get("priceChangePercent")),
        })
    markets.sort(key=lambda m: m["volume"], reverse=True)
    return markets[:TOP_MARKETS]


def parse_candle(row):
    """Parse a BingX kline and normalize its opening timestamp to milliseconds."""
    if isinstance(row, dict):
        timestamp = row.get("time", row.get("timestamp", row.get("openTime", 0)))
        o, h, l, c = (number(row.get(k)) for k in ("open", "high", "low", "close"))
        quote_volume = number(row.get("quoteVolume", row.get("quote_volume", 0)))
        raw_volume = number(row.get("volume", row.get("vol", 0)))
    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        timestamp = row[0]
        o, h, l, c = (number(row[i]) for i in range(1, 5))
        raw_volume = number(row[5])
        quote_volume = number(row[7]) if len(row) >= 8 else 0.0
    else:
        return None

    timestamp = int(number(timestamp))
    if timestamp <= 0 or min(o, h, l, c) <= 0:
        return None
    if h < max(o, c, l) or l > min(o, c, h) or h < l:
        return None

    # BingX timestamps are usually milliseconds, but normalize seconds too.
    if timestamp < 10**12:
        timestamp *= 1000

    volume = quote_volume if quote_volume > 0 else raw_volume
    volume_type = "quote" if quote_volume > 0 else "base"
    if volume < 0:
        return None

    return {
        "time": timestamp,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": volume,
        "volume_type": volume_type,
    }


def fetch_candles(symbol):
    response = api_get(
        KLINES_URL,
        {"symbol": symbol, "interval": INTERVAL, "limit": CANDLE_LIMIT},
    )
    rows = response.get("data", []) if isinstance(response, dict) else []
    if not isinstance(rows, list):
        return []

    candles = []
    for row in rows:
        try:
            candle = parse_candle(row)
            if candle:
                candles.append(candle)
        except (IndexError, TypeError, ValueError, OverflowError):
            continue

    # Deduplicate by normalized opening time and sort oldest to newest.
    candles = sorted(
        {c["time"]: c for c in candles}.values(),
        key=lambda candle: candle["time"],
    )
    now_ms = int(time.time() * 1000)
    # Keep only fully closed candles; don't assume second-based timestamps are closed.
    return [
        candle for candle in candles
        if candle["time"] + INTERVAL_MS <= now_ms
    ]


def fetch_optional_metrics(symbol):
    """Collect live public futures metrics; unavailable fields remain None."""
    metrics = {
        "funding_rate": None,
        "open_interest": None,
        "mark_price": None,
        "index_price": None,
        "next_funding_time": None,
        "premium_index_time": None,
        "order_book": None,
        "recent_trades": None,
        "data_status": {},
    }

    # Mark price, index price and funding rate.
    try:
        data = api_get(PREMIUM_INDEX_URL, {"symbol": symbol}).get("data", {})
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict)), {})
        if isinstance(data, dict):
            if data.get("markPrice") is not None:
                metrics["mark_price"] = number(data["markPrice"])
            if data.get("indexPrice") is not None:
                metrics["index_price"] = number(data["indexPrice"])
            if data.get("lastFundingRate") is not None:
                metrics["funding_rate"] = number(data["lastFundingRate"])
            if data.get("nextFundingTime") is not None:
                metrics["next_funding_time"] = int(number(data["nextFundingTime"]))
            if data.get("time") is not None:
                metrics["premium_index_time"] = int(number(data["time"]))
        metrics["data_status"]["premium_index"] = "ok" if metrics["mark_price"] else "missing"
    except Exception as exc:
        metrics["data_status"]["premium_index"] = f"unavailable: {str(exc)[:100]}"
        print(f"Premium index unavailable for {symbol}: {exc}")

    # Open interest.
    try:
        data = api_get(OPEN_INTEREST_URL, {"symbol": symbol}).get("data", {})
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict)), {})
        if isinstance(data, dict) and data.get("openInterest") is not None:
            metrics["open_interest"] = number(data["openInterest"])
        metrics["data_status"]["open_interest"] = "ok" if metrics["open_interest"] is not None else "missing"
    except Exception as exc:
        metrics["data_status"]["open_interest"] = f"unavailable: {str(exc)[:100]}"
        print(f"Open interest unavailable for {symbol}: {exc}")

    # Current order book (top 20 levels). Values are summarized, not sent raw.
    try:
        data = api_get(DEPTH_URL, {"symbol": symbol, "limit": 20}).get("data", {})
        if isinstance(data, dict):
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            bids = [
                (number(x[0]), number(x[1])) for x in bids
                if isinstance(x, (list, tuple)) and len(x) >= 2
                and number(x[0]) > 0 and number(x[1]) > 0
            ]
            asks = [
                (number(x[0]), number(x[1])) for x in asks
                if isinstance(x, (list, tuple)) and len(x) >= 2
                and number(x[0]) > 0 and number(x[1]) > 0
            ]
            if bids and asks:
                best_bid = max(price for price, _ in bids)
                best_ask = min(price for price, _ in asks)
                mid = (best_bid + best_ask) / 2
                bid_notional = sum(price * qty for price, qty in bids)
                ask_notional = sum(price * qty for price, qty in asks)
                total_notional = bid_notional + ask_notional
                metrics["order_book"] = {
                    "levels_per_side": min(len(bids), len(asks)),
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread_pct": ((best_ask - best_bid) / mid * 100) if mid > 0 else None,
                    "bid_notional_top20_usdt": bid_notional,
                    "ask_notional_top20_usdt": ask_notional,
                    "bid_ask_notional_imbalance": (
                        (bid_notional - ask_notional) / total_notional
                        if total_notional > 0 else None
                    ),
                    "exchange_timestamp_ms": data.get("T"),
                }
        metrics["data_status"]["order_book"] = "ok" if metrics["order_book"] else "missing"
    except Exception as exc:
        metrics["data_status"]["order_book"] = f"unavailable: {str(exc)[:100]}"
        print(f"Order book unavailable for {symbol}: {exc}")

    # Recent public trades. buyerMaker=true means the buyer was maker, so the
    # seller was the taker/aggressor. If the field is absent, do not infer flow.
    try:
        rows = api_get(TRADES_URL, {"symbol": symbol, "limit": 100}).get("data", [])
        if isinstance(rows, list):
            buy_quote = 0.0
            sell_quote = 0.0
            classified = 0
            latest_time = None
            for trade in rows:
                if not isinstance(trade, dict) or trade.get("buyerMaker") is None:
                    continue
                price = number(trade.get("price"))
                qty = number(trade.get("qty"))
                quote_qty = number(trade.get("quoteQty"))
                if quote_qty <= 0:
                    quote_qty = price * qty
                if quote_qty <= 0:
                    continue
                if trade.get("buyerMaker") is True or str(trade.get("buyerMaker")).lower() == "true":
                    sell_quote += quote_qty
                else:
                    buy_quote += quote_qty
                classified += 1
                timestamp = int(number(trade.get("time")))
                if timestamp and (latest_time is None or timestamp > latest_time):
                    latest_time = timestamp
            total = buy_quote + sell_quote
            if classified:
                metrics["recent_trades"] = {
                    "trades_requested": min(len(rows), 100),
                    "trades_classified": classified,
                    "aggressive_buy_quote_usdt": buy_quote,
                    "aggressive_sell_quote_usdt": sell_quote,
                    "aggressive_buy_sell_ratio": buy_quote / sell_quote if sell_quote > 0 else None,
                    "buy_sell_imbalance": (buy_quote - sell_quote) / total if total > 0 else None,
                    "latest_trade_time_ms": latest_time,
                    "interpretation_note": "Recent sample only; not cumulative delta or proof of trader intent.",
                }
        metrics["data_status"]["recent_trades"] = "ok" if metrics["recent_trades"] else "unavailable or aggressor flag missing"
    except Exception as exc:
        metrics["data_status"]["recent_trades"] = f"unavailable: {str(exc)[:100]}"
        print(f"Recent trades unavailable for {symbol}: {exc}")

    return metrics


def load_snapshot():
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    markets = data.get("markets", {})
    sent = data.get("sent_signals", {})
    open_trades = data.get("open_trades", {})
    return {
        "markets": markets if isinstance(markets, dict) else {},
        "sent_signals": sent if isinstance(sent, dict) else {},
        "open_trades": open_trades if isinstance(open_trades, dict) else {},
    }


def save_snapshot(markets, sent_signals, open_trades=None):
    SNAPSHOT_FILE.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "markets": markets,
        "sent_signals": sent_signals,
        "open_trades": open_trades or {},
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def load_trades_history():
    try:
        data = json.loads(TRADES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"closed": [], "stats": {}}
    if not isinstance(data, dict):
        data = {"closed": [], "stats": {}}
    closed = data.get("closed", [])
    stats = data.get("stats", {})
    return {"closed": closed if isinstance(closed, list) else [],
            "stats": stats if isinstance(stats, dict) else {}}


def save_trades_history(history):
    temp = TRADES_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(TRADES_FILE)


def analyze_market(market, candles):
    if len(candles) < 22:
        return None
    last, previous, hour_ago = candles[-1], candles[-2], candles[-5]
    close, prev_close, hour_close = last["close"], previous["close"], hour_ago["close"]
    if min(close, prev_close, hour_close) <= 0:
        return None
    change_15m = (close / prev_close - 1) * 100
    change_1h = (close / hour_close - 1) * 100
    change_24h = market["change_24h"]
    prior = candles[-21:-1]
    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)
    consistent = len({c["volume_type"] for c in candles[-21:]}) == 1
    avg_volume = sum(c["volume"] for c in prior) / len(prior)
    volume_valid = consistent and avg_volume > 0 and last["volume"] > 0
    volume_ratio = last["volume"] / avg_volume if volume_valid else 0.0
    breakout_up = volume_valid and close > prior_high and volume_ratio >= 1.2
    breakout_down = volume_valid and close < prior_low and volume_ratio >= 1.2
    score = (abs(change_15m) * 1.6 + abs(change_1h) * 0.8
             + min(volume_ratio, 5.0) * 1.2 + min(abs(change_24h), 20.0) * 0.12)
    if breakout_up or breakout_down:
        score += 4.0
    if change_15m * change_1h > 0:
        score += 0.8
    interesting = (score >= 2.7 or
                   (volume_valid and abs(change_15m) >= 0.6 and volume_ratio >= 1.1) or
                   (volume_valid and abs(change_1h) >= 1.5 and volume_ratio >= 1.1) or
                   breakout_up or breakout_down)
    if not interesting:
        return None
    if breakout_up:
        direction, title = "LONG", "ПРОБОЙ ВВЕРХ"
    elif breakout_down:
        direction, title = "SHORT", "ПРОБОЙ ВНИЗ"
    elif change_15m >= 0.6 and change_1h > 0:
        direction, title = "LONG", "ИМПУЛЬС ВВЕРХ"
    elif change_15m <= -0.6 and change_1h < 0:
        direction, title = "SHORT", "ИМПУЛЬС ВНИЗ"
    elif change_15m > 0:
        direction, title = "LONG", "ВОЗМОЖНЫЙ ОТСКОК"
    else:
        direction, title = "SHORT", "ВОЗМОЖНЫЙ ОТКАТ"
    strong = (breakout_up or breakout_down or
              (volume_valid and abs(change_15m) >= 1.0 and volume_ratio >= 1.4) or
              (volume_valid and abs(change_1h) >= 2.0 and volume_ratio >= 1.3
               and change_15m * change_1h > 0))
    return {
        "symbol": market["symbol"], "price": close,
        "direction": direction, "title": title,
        "strength": "СИЛЬНЫЙ" if strong else "НАБЛЮДЕНИЕ",
        "change_15m": change_15m, "change_1h": change_1h,
        "change_24h": change_24h, "volume_ratio": volume_ratio,
        "volume_valid": volume_valid, "score": score,
        "prior_high": prior_high, "prior_low": prior_low,
        "candles": candles,
    }


ANALYSIS_PROMPT = """
Ты анализируешь до пяти USDT-M perpetual фьючерсов BingX.
Сравни их внутренне по структуре свечей, объёму, импульсу,
риску запоздалого входа, funding, open interest, mark/index price,
спреду и дисбалансу доступных уровней стакана, а также по недавним
агрессивным покупкам/продажам, если соответствующие поля доступны.
Эти live-метрики являются снимком момента запроса, а не гарантией.
Дисбаланс стакана не доказывает намерения участников; выборка сделок
не является полной дельтой; open interest без истории не показывает
направление изменения. Учитывай время данных и их доступность.
Выбери только ОДНУ обоснованную сделку или NO_TRADE.
Не показывай сравнение монет. Не выдумывай ликвидации, полную дельту,
действия крупных игроков или отсутствующие данные. Если ключевые
данные недоступны или противоречат сетапу, снижай уверенность и предпочитай
NO_TRADE вместо выдумывания подтверждений.

Ответ должен соответствовать переданной JSON-схеме.
status: TRADE либо NO_TRADE.
Для TRADE: symbol — точный символ из входных данных;
direction — LONG или SHORT; entry_type — MARKET или LIMIT;
entry_price, stop_loss, take_profit — положительные числовые цены
в виде строк; reason — одно короткое предложение по-русски.
Для NO_TRADE: status NO_TRADE, остальные поля — пустые строки,
reason — краткая причина отказа от сделки.

MARKET допустим только если вход по последней закрытой свече
не требует дополнительного подтверждения. LIMIT — конкретная цена.
Определи уровни по фактической структуре свечей: локальные swing high/low,
недавние поддержки и сопротивления, а также границы диапазона. Не ставь
тейк-профит за ближайшим сильным препятствием только ради высокого R:R.
Для LONG стоп размещай за уровнем, пробой которого отменяет сетап; TP —
перед ближайшим значимым сопротивлением. Для SHORT используй зеркальную
логику с поддержкой. Не ставь стоп произвольно близко к входу ради красивого
соотношения. Рассчитывай R:R по расстоянию от входа до SL и TP.
Минимальное допустимое соотношение потенциальная прибыль/риск — 2:1.
Если ближайшие уровни не дают минимум 2:1, если стоп получается слишком
далеко, или уровни нельзя обосновать свечами, верни NO_TRADE.
Для LONG: stop_loss < entry_price < take_profit.
Для SHORT: take_profit < entry_price < stop_loss.
Не обещай прибыль и не придумывай уровни ради заполнения полей.
"""

TRADE_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "status": {"type": "STRING", "enum": ["TRADE", "NO_TRADE"]},
        "symbol": {"type": "STRING"},
        "direction": {"type": "STRING"},
        "entry_type": {"type": "STRING"},
        "entry_price": {"type": "STRING"},
        "stop_loss": {"type": "STRING"},
        "take_profit": {"type": "STRING"},
        "reason": {"type": "STRING"},
    },
    "required": [
        "status", "symbol", "direction", "entry_type",
        "entry_price", "stop_loss", "take_profit", "reason",
    ],
    "propertyOrdering": [
        "status", "symbol", "direction", "entry_type",
        "entry_price", "stop_loss", "take_profit", "reason",
    ],
}


def build_candidate_data(candidate, metrics):
    candles = candidate["candles"][-22:]
    # Pivot levels are objective reference points; Gemini still judges their relevance.
    pivot_highs = []
    pivot_lows = []
    for i in range(1, len(candles) - 1):
        prev_c, cur_c, next_c = candles[i - 1], candles[i], candles[i + 1]
        if cur_c["high"] >= prev_c["high"] and cur_c["high"] > next_c["high"]:
            pivot_highs.append(cur_c["high"])
        if cur_c["low"] <= prev_c["low"] and cur_c["low"] < next_c["low"]:
            pivot_lows.append(cur_c["low"])
    current_price = candidate["price"]
    resistances = sorted({round(x, 12) for x in pivot_highs if x > current_price})[:5]
    supports = sorted({round(x, 12) for x in pivot_lows if x < current_price}, reverse=True)[:5]
    return {
        "symbol": candidate["symbol"],
        "last_closed_price": candidate["price"],
        "preliminary_direction": candidate["direction"],
        "preliminary_label": candidate["title"],
        "preliminary_strength": candidate["strength"],
        "preliminary_score": round(candidate["score"], 2),
        "change_15m_pct": round(candidate["change_15m"], 3),
        "change_1h_pct": round(candidate["change_1h"], 3),
        "change_24h_pct": round(candidate["change_24h"], 3),
        "volume_ratio": round(candidate["volume_ratio"], 3) if candidate["volume_valid"] else None,
        "prior_20_candle_high": candidate["prior_high"],
        "prior_20_candle_low": candidate["prior_low"],
        "recent_15m_levels": {
            "pivot_resistances_above_price_nearest_first": resistances,
            "pivot_supports_below_price_nearest_first": supports,
            "note": "Calculated from local swing highs/lows among the supplied recent candles; confirm against candle structure, not standalone signals."
        },
        "live_market_data": {
            "funding_rate": metrics.get("funding_rate"),
            "open_interest": metrics.get("open_interest"),
            "mark_price": metrics.get("mark_price"),
            "index_price": metrics.get("index_price"),
            "next_funding_time_ms": metrics.get("next_funding_time"),
            "premium_index_timestamp_ms": metrics.get("premium_index_time"),
            "order_book_top20": metrics.get("order_book"),
            "recent_trades_sample": metrics.get("recent_trades"),
            "data_status": metrics.get("data_status", {}),
        },
        "candles_15m_utc": [{
            "time": datetime.fromtimestamp(
                c["time"] / 1000 if c["time"] >= 10**12 else c["time"],
                tz=timezone.utc,
            ).isoformat(),
            "open": c["open"], "high": c["high"], "low": c["low"],
            "close": c["close"], "volume": c["volume"],
            "volume_type": c["volume_type"],
        } for c in candles],
        "unavailable_data": ["liquidations feed", "full trade-level delta history", "full volume profile", "historical open-interest series"],
    }


def is_retryable_status(status_code, error_text=""):
    if status_code in (408, 429, 500, 502, 503, 504):
        return True
    text = error_text.lower()
    return any(s in text for s in (
        "overloaded", "high demand", "temporarily unavailable",
        "resource_exhausted", "rate limit", "too many requests",
        "try again later", "service unavailable", "internal error",
    ))


def gemini_analyze_many(candidates, metrics_by_symbol, on_attempt=None):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Не задан секрет GEMINI_API_KEY")
    data = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "timeframe": INTERVAL,
        "candidates": [build_candidate_data(c, metrics_by_symbol[c["symbol"]]) for c in candidates],
    }
    payload = {
        "system_instruction": {"parts": [{"text": ANALYSIS_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(data, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1500,
            "responseMimeType": "application/json",
            "responseSchema": TRADE_RESPONSE_SCHEMA,
        },
    }
    model_queue = list(dict.fromkeys([GEMINI_MODEL] + [
        model for model in GEMINI_FALLBACK_MODELS if model != GEMINI_MODEL
    ]))
    model_index = 0
    current_model = model_queue[model_index]
    delay = RETRY_INITIAL_SECONDS
    attempt = 0
    model_attempt = 0
    while True:
        attempt += 1
        model_attempt += 1
        if on_attempt is not None:
            try:
                on_attempt(attempt)
            except Exception as exc:
                print(f"Telegram status update failed: {exc}")
        print(
            f"Gemini combined analysis attempt {attempt}; model={current_model}; "
            f"candidates={len(candidates)}"
        )
        model_url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent"
        try:
            response = session.post(
                model_url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=GEMINI_TIMEOUT,
            )
        except requests.RequestException as exc:
            if model_attempt >= MAX_GEMINI_ATTEMPTS:
                raise RuntimeError(
                    f"Gemini ({current_model}): сетевая ошибка после "
                    f"{model_attempt} попыток: {type(exc).__name__}: {str(exc)[:400]}"
                ) from exc
            print(
                f"Gemini network error on {current_model}, attempt "
                f"{model_attempt}/{MAX_GEMINI_ATTEMPTS}: {exc}; retry in {delay}s"
            )
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
            continue
        try:
            result = response.json()
        except ValueError:
            result = {"error": {"message": response.text[:1000]}}
        if not response.ok:
            error = result.get("error", {})
            error_message = error.get("message", str(result)) if isinstance(error, dict) else str(error)
            error_lower = error_message.lower()
            daily_quota_exhausted = response.status_code == 429 and any(
                marker in error_lower for marker in (
                    "requests per day", "per day limit", "daily limit",
                    "free_tier_requests", "quota exceeded", "generate_content_free_tier_requests",
                )
            )
            if daily_quota_exhausted and model_index + 1 < len(model_queue):
                old_model = current_model
                model_index += 1
                current_model = model_queue[model_index]
                model_attempt = 0
                delay = RETRY_INITIAL_SECONDS
                print(
                    f"Daily quota exhausted for {old_model}; switching to fallback "
                    f"model {current_model} (attempts remain bounded)."
                )
                continue
            if daily_quota_exhausted:
                raise RuntimeError(
                    f"Gemini: дневная квота исчерпана для всех моделей "
                    f"({', '.join(model_queue)}). Последняя ошибка HTTP 429: "
                    f"{error_message[:500]}"
                )
            if is_retryable_status(response.status_code, error_message):
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = max(delay, float(retry_after or 0))
                except ValueError:
                    wait_seconds = delay
                wait_seconds = min(wait_seconds, RETRY_MAX_SECONDS)
                if model_attempt >= MAX_GEMINI_ATTEMPTS:
                    raise RuntimeError(
                        f"Gemini HTTP {response.status_code} ({current_model}) после "
                        f"{model_attempt} попыток: {error_message[:500]}"
                    )
                print(
                    f"Temporary Gemini HTTP {response.status_code} on {current_model}, "
                    f"attempt {model_attempt}/{MAX_GEMINI_ATTEMPTS}: "
                    f"{error_message[:250]}; retry in {wait_seconds}s"
                )
                time.sleep(wait_seconds)
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                continue
            raise RuntimeError(
                f"Gemini HTTP {response.status_code} ({current_model}): {error_message[:700]}"
            )
        try:
            parts = result["candidates"][0]["content"]["parts"]
            raw = "\n".join(
                part.get("text", "") for part in parts if isinstance(part, dict)
            ).strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            analysis = json.loads(raw)
            if not isinstance(analysis, dict):
                raise ValueError("Ответ не является JSON-объектом")
            if analysis.get("status") not in ("TRADE", "NO_TRADE"):
                raise ValueError("Отсутствует корректный status")
            print(f"Gemini returned valid JSON on attempt {attempt}")
            return analysis
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            print(f"Gemini malformed response, attempt {attempt}: {exc}")
            if attempt >= JSON_RETRY_ATTEMPTS:
                print("JSON retry limit reached. Returning NO_TRADE safely.")
                return {
                    "status": "NO_TRADE",
                    "reason": "Не удалось получить корректный анализ от Gemini.",
                }
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
            continue


def _api_credentials():
    key = os.getenv("BINGX_API_KEY", "").strip()
    secret = os.getenv("BINGX_SECRET_KEY", "").strip()
    return (key, secret) if key and secret else (None, None)


def signed_request(method, path, params=None):
    """Signed request pinned to the VST host. Never retries order POSTs: that can duplicate orders."""
    api_key, api_secret = _api_credentials()
    if not api_key or not api_secret:
        raise RuntimeError("Missing BINGX_API_KEY / BINGX_SECRET_KEY")
    params = dict(params or {})
    params.setdefault("recvWindow", 5000)
    params["timestamp"] = int(time.time() * 1000)
    ordered = sorted((str(k), str(v)) for k, v in params.items())
    query = urlencode(ordered)
    signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    signed_query = f"{query}&signature={signature}"
    headers = {"X-BX-APIKEY": api_key}
    url = f"{VST_BASE_URL}{path}"
    method = method.upper()
    try:
        if method == "GET":
            response = session.get(f"{url}?{signed_query}", headers=headers, timeout=REQUEST_TIMEOUT)
        elif method == "DELETE":
            response = session.delete(f"{url}?{signed_query}", headers=headers, timeout=REQUEST_TIMEOUT)
        elif method == "POST":
            # BingX accepts signed params in query string for POST (matches CCXT / working clients).
            # Putting them only in the body caused 109400 "field is required" on leverage/order.
            response = session.post(
                f"{url}?{signed_query}",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        else:
            raise ValueError(f"Unsupported signed method: {method}")
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(f"BingX returned non-JSON HTTP {response.status_code}") from exc
        if not response.ok:
            raise RuntimeError(f"BingX HTTP {response.status_code}: {str(result)[:500]}")
        if isinstance(result, dict) and result.get("code", 0) not in (0, "0", None):
            raise RuntimeError(f"BingX {result.get('code')}: {result.get('msg', '')}")
        return result
    except requests.RequestException as exc:
        raise RuntimeError(f"Signed request transport error ({method} {path}); verify order state before retrying: {exc}") from exc


_contracts_cache = None


def load_contracts():
    global _contracts_cache
    if _contracts_cache is not None:
        return _contracts_cache
    data = api_get(CONTRACTS_URL).get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("Unexpected BingX contracts response")
    by_symbol = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", ""))
        if symbol:
            by_symbol[symbol] = {
                "symbol": symbol,
                "quantityPrecision": int(number(item.get("quantityPrecision"), 4)),
                "pricePrecision": int(number(item.get("pricePrecision"), 4)),
                "tradeMinQuantity": number(item.get("tradeMinQuantity")),
                "tradeMinUSDT": number(item.get("tradeMinUSDT")),
                "maxLongLeverage": int(number(item.get("maxLongLeverage"), 3)),
                "maxShortLeverage": int(number(item.get("maxShortLeverage"), 3)),
            }
    _contracts_cache = by_symbol
    return by_symbol


def round_quantity(qty, precision):
    # Round down to the contract's declared quantity precision; do not treat `size` as a step.
    scale = 10 ** max(0, min(int(precision), 12))
    return int(qty * scale) / scale


def set_leverage(symbol, position_side, leverage):
    return signed_request("POST", LEVERAGE_PATH, {
        "symbol": symbol, "side": position_side, "leverage": int(leverage),
    })


def validate_trade_analysis(analysis, selected):
    """Validate the model output before any private/order API request."""
    if not isinstance(analysis, dict) or str(analysis.get("status", "")).upper() != "TRADE":
        raise ValueError("Analysis is not a TRADE")
    symbol = str(analysis.get("symbol", ""))
    if symbol not in {c["symbol"] for c in selected}:
        raise ValueError("Gemini selected a symbol outside the candidate list")
    direction = str(analysis.get("direction", "")).upper()
    entry_type = str(analysis.get("entry_type", "")).upper()
    if direction not in ("LONG", "SHORT") or entry_type not in ("MARKET", "LIMIT"):
        raise ValueError("Invalid direction or entry type")
    entry = valid_price(analysis.get("entry_price"))
    stop = valid_price(analysis.get("stop_loss"))
    target = valid_price(analysis.get("take_profit"))
    if None in (entry, stop, target):
        raise ValueError("Entry, stop loss and take profit must be positive prices")
    if direction == "LONG":
        risk, reward = entry - stop, target - entry
    else:
        risk, reward = stop - entry, entry - target
    if risk <= 0 or reward <= 0:
        raise ValueError("SL/TP are on the wrong side of the entry")
    ratio = reward / risk
    if ratio < MIN_REWARD_RISK_RATIO:
        raise ValueError(f"R:R {ratio:.2f} below required {MIN_REWARD_RISK_RATIO:.2f}")
    return symbol, direction, entry_type, entry, stop, target, ratio


def place_demo_order(symbol, direction, entry_type, entry_price, stop_loss, take_profit, reason, current_price):
    if not ENABLE_AUTO_TRADE:
        raise RuntimeError("Demo order placement is disabled; set ENABLE_AUTO_TRADE=1 explicitly")
    if not VST_BASE_URL.startswith("https://open-api-vst.bingx.com"):
        raise RuntimeError("Safety stop: private API host is not the VST demo host")
    if TARGET_NOTIONAL_USDT <= 0:
        raise RuntimeError("TARGET_NOTIONAL_USDT must be positive")
    if entry_type == "MARKET":
        if current_price <= 0:
            raise RuntimeError("No valid current market price for MARKET order")
        deviation = abs(current_price - entry_price) / entry_price * 100
        if deviation > MAX_ENTRY_DEVIATION_PCT:
            raise RuntimeError(f"Market moved {deviation:.2f}% from model entry; refusing stale signal")
        sizing_price = current_price
    else:
        sizing_price = entry_price
    contracts = load_contracts()
    spec = contracts.get(symbol)
    if not spec:
        raise RuntimeError(f"No contract specification for {symbol}")
    lev_cap = spec["maxLongLeverage"] if direction == "LONG" else spec["maxShortLeverage"]
    leverage = max(1, min(MAX_DEMO_LEVERAGE, lev_cap or MAX_DEMO_LEVERAGE))
    qty = round_quantity(TARGET_NOTIONAL_USDT / sizing_price, spec["quantityPrecision"])
    if qty <= 0 or (spec["tradeMinQuantity"] > 0 and qty < spec["tradeMinQuantity"]):
        raise RuntimeError(f"Quantity {qty} is below the contract minimum")
    notional = qty * sizing_price
    min_notional = spec["tradeMinUSDT"]
    if min_notional > 0 and notional < min_notional:
        raise RuntimeError(f"Notional {notional:.4f} is below exchange minimum {min_notional:.4f}")

    position_side = direction
    side = "BUY" if direction == "LONG" else "SELL"
    set_leverage(symbol, position_side, leverage)
    params = {
        "symbol": symbol, "side": side, "positionSide": position_side,
        "type": "MARKET" if entry_type == "MARKET" else "LIMIT",
        "quantity": f"{qty:.{spec['quantityPrecision']}f}",
        "stopLoss": json.dumps({"type": "STOP_MARKET", "stopPrice": format_price(stop_loss), "workingType": "MARK_PRICE"}, separators=(",", ":")),
        "takeProfit": json.dumps({"type": "TAKE_PROFIT_MARKET", "stopPrice": format_price(take_profit), "workingType": "MARK_PRICE"}, separators=(",", ":")),
    }
    if entry_type == "LIMIT":
        params["price"] = format_price(entry_price)
        params["timeInForce"] = "GTC"
    # Do not retry this call automatically. If transport fails, verify the demo order/position first.
    result = signed_request("POST", ORDER_PATH, params)
    data = result.get("data", result) if isinstance(result, dict) else {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected order response: {str(result)[:500]}")
    order_id = str(data.get("orderID") or data.get("orderId") or data.get("clientOrderId") or f"local_{int(time.time())}")
    record = {
        "id": order_id, "symbol": symbol, "direction": direction, "entry_type": entry_type,
        "entry_price": sizing_price if entry_type == "MARKET" else entry_price,
        "signal_entry_price": entry_price, "stop_loss": stop_loss, "take_profit": take_profit,
        "quantity": qty, "notional_usdt": round(notional, 2), "leverage": leverage,
        "margin_approx_usdt": round(notional / leverage, 2), "reason": reason,
        "opened_at": datetime.now(timezone.utc).isoformat(), "status": "OPEN",
        "order_response": {k: data.get(k) for k in ("orderId", "orderID", "status", "clientOrderId") if data.get(k) is not None},
        "pnl_source": "exchange position history only; do not infer PnL from estimated stop price",
    }
    return record


def get_positions():
    result = signed_request("GET", POSITIONS_PATH, {})
    data = result.get("data", []) if isinstance(result, dict) else []
    if isinstance(data, dict):
        data = data.get("positions", [data])
    if not isinstance(data, list):
        raise RuntimeError("Unexpected positions response; refusing to infer closures")
    positions = []
    for p in data:
        if not isinstance(p, dict):
            continue
        amt = number(p.get("positionAmt", p.get("availableAmt", 0)))
        if abs(amt) < 1e-12:
            continue
        positions.append({"symbol": str(p.get("symbol", "")),
                          "positionSide": str(p.get("positionSide", "")).upper(),
                          "positionAmt": amt})
    return positions


def get_balance_vst():
    result = signed_request("GET", BALANCE_PATH, {})
    data = result.get("data", {}) if isinstance(result, dict) else {}
    if isinstance(data, dict) and isinstance(data.get("balance"), dict):
        bal = data["balance"]
        return {"equity": number(bal.get("balance", bal.get("equity", 0))),
                "available": number(bal.get("availableMargin", bal.get("available", 0))),
                "unrealized": number(bal.get("unrealizedProfit", 0))}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("asset", "")).upper() in ("VST", "USDT"):
                return {"equity": number(item.get("balance", item.get("equity", 0))),
                        "available": number(item.get("availableMargin", item.get("available", 0))),
                        "unrealized": number(item.get("unrealizedProfit", 0))}
    return None


def compute_stats(closed_trades):
    pnls = [number(t.get("realized_pnl", 0)) for t in closed_trades]
    if not pnls:
        return {"total_trades": 0, "wins": 0, "losses": 0, "winrate_pct": 0.0,
                "total_pnl": 0.0, "avg_pnl": 0.0, "max_drawdown": 0.0,
                "best_trade": 0.0, "worst_trade": 0.0, "profit_factor": 0.0}
    wins = sum(p > 0 for p in pnls)
    losses = sum(p < 0 for p in pnls)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    equity = peak = max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {"total_trades": len(pnls), "wins": wins, "losses": losses,
            "winrate_pct": round(100 * wins / len(pnls), 1),
            "total_pnl": round(sum(pnls), 2), "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "max_drawdown": round(max_dd, 2), "best_trade": round(max(pnls), 2),
            "worst_trade": round(min(pnls), 2),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (999.0 if gross_profit else 0.0)}


def format_stats_message(stats, balance=None):
    lines = ["STATS DEMO (VST)", f"Trades: {stats.get('total_trades', 0)}",
             f"Wins / losses: {stats.get('wins', 0)} / {stats.get('losses', 0)}",
             f"Winrate: {stats.get('winrate_pct', 0)}%",
             f"Total PnL: {stats.get('total_pnl', 0):+.2f} VST",
             f"Avg PnL: {stats.get('avg_pnl', 0):+.2f} VST",
             f"Max drawdown: {stats.get('max_drawdown', 0):.2f} VST",
             f"Best / worst: {stats.get('best_trade', 0):+.2f} / {stats.get('worst_trade', 0):+.2f} VST",
             f"Profit factor: {stats.get('profit_factor', 0)}"]
    if balance:
        lines.extend([f"Equity: {balance.get('equity', 0):.2f} VST",
                      f"Available: {balance.get('available', 0):.2f} VST"])
    return "\n".join(lines)


def sync_open_trades(open_trades):
    """Only record a closure when exchange history confirms realized PnL; otherwise keep it pending."""
    if not open_trades:
        return {}, []
    try:
        positions = get_positions()
    except Exception as exc:
        print(f"Cannot sync positions; retaining local trades: {exc}")
        return open_trades, []
    live_keys = {f"{p['symbol']}:{p['positionSide']}" for p in positions}
    still_open, newly_closed = {}, []
    for key, trade in open_trades.items():
        if f"{trade.get('symbol', '')}:{trade.get('direction', '')}" in live_keys:
            still_open[key] = trade
            continue
        # Missing from open positions is not enough: order may be pending/endpoint response incomplete.
        try:
            hist = signed_request("GET", POSITION_HISTORY_PATH, {"symbol": trade.get("symbol", ""), "limit": 20})
            rows = hist.get("data", []) if isinstance(hist, dict) else []
            if isinstance(rows, dict):
                rows = rows.get("list", rows.get("rows", []))
            if not isinstance(rows, list):
                still_open[key] = trade
                continue
            opened = trade.get("opened_at", "")
            opened_ts = 0
            try:
                opened_ts = datetime.fromisoformat(opened.replace("Z", "+00:00")).timestamp() * 1000
            except Exception:
                pass
            match = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                side = str(row.get("positionSide", row.get("side", ""))).upper()
                if side and side not in (trade.get("direction", "").upper(), "LONG" if trade.get("direction") == "LONG" else "SHORT"):
                    continue
                close_time = number(row.get("closeTime", row.get("time", row.get("updateTime", 0))))
                if 0 < close_time < 10**12:
                    close_time *= 1000
                if opened_ts and close_time and close_time < opened_ts:
                    continue
                if any(k in row for k in ("realizedPnl", "realizedProfit", "profit")):
                    match = row
                    break
            if match is None:
                still_open[key] = trade
                print(f"No matching confirmed closed-history record for {trade.get('symbol')}; retain for next sync")
                continue
            pnl_value = match.get("realizedPnl", match.get("realizedProfit", match.get("profit")))
            exit_price = number(match.get("avgPrice", match.get("closePrice", match.get("price", trade.get("entry_price", 0)))))
            closed = dict(trade)
            closed.update({"status": "CLOSED", "closed_at": datetime.now(timezone.utc).isoformat(),
                           "exit_price": exit_price, "realized_pnl": round(number(pnl_value), 4),
                           "close_reason": "confirmed exchange position history", "pnl_confirmed": True})
            newly_closed.append(closed)
        except Exception as exc:
            still_open[key] = trade
            print(f"Position history unavailable for {trade.get('symbol')}; retain trade: {exc}")
    return still_open, newly_closed


def valid_price(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        price = float(value)
        if price > 0 and price == price and price != float("inf"):
            return price
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def format_price(price):
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"{price:.6f}".rstrip("0").rstrip(".")
    return f"{price:.10f}".rstrip("0").rstrip(".")


def render_report(analysis, selected, started, trade_record=None):
    time_label = started.strftime("%d.%m.%Y %H:%M UTC")
    status = str(analysis.get("status", "")).upper()
    reason = str(analysis.get("reason") or "Недостаточно подтверждений для обоснованного входа.").strip()[:200]
    if status == "NO_TRADE":
        return f"🧠 BINGX | {time_label}\n⛔ НЕТ СДЕЛКИ\n💡 Причина: {reason}"
    if status != "TRADE":
        raise RuntimeError(f"Неизвестный статус Gemini: {status}")
    symbol = str(analysis.get("symbol", ""))
    direction = str(analysis.get("direction", "")).upper()
    entry_type = str(analysis.get("entry_type", "")).upper()
    if symbol not in {c["symbol"] for c in selected}:
        raise RuntimeError("Gemini выбрал монету не из переданных кандидатов")
    if direction not in ("LONG", "SHORT") or entry_type not in ("MARKET", "LIMIT"):
        raise RuntimeError("Gemini вернул неверное направление или тип входа")
    entry = valid_price(analysis.get("entry_price"))
    stop = valid_price(analysis.get("stop_loss"))
    target = valid_price(analysis.get("take_profit"))
    if entry is None or stop is None or target is None:
        raise RuntimeError("Gemini не указал корректные цены входа, SL и TP")
    if direction == "LONG":
        risk, reward = entry - stop, target - entry
    else:
        risk, reward = stop - entry, entry - target
    if risk <= 0 or reward <= 0:
        raise RuntimeError("Gemini указал нелогичные уровни SL/TP")
    ratio = reward / risk
    if ratio < MIN_REWARD_RISK_RATIO:
        return (
            f"🧠 BINGX | {time_label}\n"
            f"⛔ НЕТ СДЕЛКИ — R:R НИЖЕ МИНИМУМА\n"
            f"🪙 Монета: {symbol} ({direction})\n"
            f"⚖️ Получилось 1:{ratio:.2f}, требуется минимум 1:{MIN_REWARD_RISK_RATIO:.2f}.\n"
            f"💡 Ближайшие уровни не дают достаточного потенциала относительно стопа."
        )
    entry_label = "РЫНОК" if entry_type == "MARKET" else "ЛИМИТКА"
    return (
        f"🏆 BINGX | ЛУЧШАЯ СДЕЛКА\n"
        f"🕒 {time_label}\n\n"
        f"🪙 Монета: {symbol}\n"
        f"📈 Направление: {direction}\n"
        f"💵 Вход: {format_price(entry)} ({entry_label})\n"
        f"🛑 Stop Loss: {format_price(stop)}\n"
        f"🎯 Take Profit: {format_price(target)}\n"
        f"⚖️ SL:TP (риск/прибыль): 1:{ratio:.2f}\n\n"
        f"💡 {reason}\n"
        f"⚠️ План основан на закрытых свечах; перед входом проверь актуальную цену."
        + ("\n\nDEMO ORDER PLACED (VST)\n"
           f"Qty: {trade_record.get('quantity')}\n"
           f"Notional \~ {trade_record.get('notional_usdt')} USDT\n"
           f"Leverage: x{trade_record.get('leverage')}\n"
           f"Order ID: {trade_record.get('id')}" if trade_record else "")
    )


def split_telegram_message(message, max_length=TELEGRAM_MAX_LENGTH):
    chunks = []
    while message:
        if len(message) <= max_length:
            chunks.append(message)
            break
        cut = message.rfind("\n", 0, max_length)
        if cut < max_length // 2:
            cut = max_length
        chunks.append(message[:cut])
        message = message[cut:].lstrip("\n")
    return chunks


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    message_ids = []
    for index, chunk in enumerate(split_telegram_message(message), start=1):
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=REQUEST_TIMEOUT,
        )
        try:
            result = response.json()
        except ValueError:
            result = {"description": response.text}
        if not response.ok or not result.get("ok"):
            raise RuntimeError(f"Telegram error part {index}: {result}")
        message_ids.append(result["result"]["message_id"])
        print(f"Telegram message part {index} sent")
    return message_ids


def telegram_message_action(method, message_id, text=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    payload = {"chat_id": chat_id, "message_id": message_id}
    if text is not None:
        payload["text"] = text
    response = session.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    try:
        result = response.json()
    except ValueError:
        result = {"description": response.text[:500]}
    if not response.ok or not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Telegram {method}: {result}")


def remove_waiting_message(message_id):
    if message_id is None:
        return
    try:
        telegram_message_action("deleteMessage", message_id)
    except Exception as exc:
        print(f"Could not delete waiting message: {exc}")


def main():
    started = datetime.now(timezone.utc)
    print("BingX market scanner started:", started.isoformat())
    snapshot = load_snapshot()
    sent_signals = snapshot["sent_signals"]
    open_trades = snapshot.get("open_trades", {})
    history = load_trades_history()

    if ENABLE_AUTO_TRADE:
        if not _api_credentials()[0]:
            print("Auto-trade requested but BingX credentials are missing; no orders will be placed.")
        else:
            open_trades, newly_closed = sync_open_trades(open_trades)
            if newly_closed:
                history["closed"].extend(newly_closed)
                history["stats"] = compute_stats(history["closed"])
                save_trades_history(history)
                for t in newly_closed:
                    try:
                        send_telegram(
                            f"TRADE CLOSED (demo; PnL confirmed)\n{t.get('symbol')} {t.get('direction')}\n"
                            f"Entry: {format_price(number(t.get('entry_price')))}\n"
                            f"Exit: {format_price(number(t.get('exit_price')))}\n"
                            f"PnL: {number(t.get('realized_pnl')):+.2f} VST\n"
                            f"Reason: {t.get('close_reason', '')}\n\n{format_stats_message(history['stats'])}"
                        )
                    except Exception as te:
                        print(f"Telegram close notify failed: {te}")

    markets = fetch_markets()
    if not markets:
        raise RuntimeError("BingX returned no markets")
    print(f"Scanning {len(markets)} markets")
    candidates = []
    current_time = int(time.time())
    for index, market in enumerate(markets, start=1):
        symbol = market["symbol"]
        try:
            candidate = analyze_market(market, fetch_candles(symbol))
            if candidate:
                key = f"{symbol}:{candidate['direction']}:{candidate['strength']}"
                last_sent = int(sent_signals.get(key, 0) or 0)
                if current_time - last_sent >= COOLDOWN_SECONDS:
                    candidate["key"] = key
                    candidate["current_market_price"] = market["price"]
                    candidates.append(candidate)
        except Exception as exc:
            print(f"[{index}/{len(markets)}] {symbol}: {exc}")
        if index < len(markets):
            time.sleep(REQUEST_PAUSE)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected = candidates[:MAX_CANDIDATES]
    print(f"Qualifying candidates: {len(candidates)}; sent to Gemini: {len(selected)}")
    trade_record = None
    if selected:
        metrics = {c["symbol"]: fetch_optional_metrics(c["symbol"]) for c in selected}
        waiting_id = None
        try:
            waiting_id = send_telegram("⏳ Жду ответ от Gemini... Попытка 1")[0]
        except Exception as exc:
            print(f"Could not send waiting message: {exc}")

        def update_attempt(attempt):
            if waiting_id is not None and attempt > 1:
                telegram_message_action(
                    "editMessageText",
                    waiting_id,
                    f"⏳ Жду ответ от Gemini... Попытка {attempt}",
                )

        try:
            analysis = gemini_analyze_many(selected, metrics, on_attempt=update_attempt)
            # Validate before any order request; report validation also runs if auto-trade is disabled.
            if str(analysis.get("status", "")).upper() == "TRADE":
                validated = validate_trade_analysis(analysis, selected)
                symbol, direction, entry_type, entry, stop, target, ratio = validated
                if ENABLE_AUTO_TRADE:
                    if not _api_credentials()[0]:
                        raise RuntimeError("Auto-trade enabled but API credentials are missing")
                    current_price = next((c.get("current_market_price", 0) for c in selected if c["symbol"] == symbol), 0)
                    trade_record = place_demo_order(
                        symbol, direction, entry_type, entry, stop, target,
                        str(analysis.get("reason", "")), current_price,
                    )
                    open_key = f"{symbol}:{direction}:{trade_record['id']}"
                    open_trades[open_key] = trade_record
                    # Persist immediately after the exchange acknowledges the order.
                    save_snapshot(snapshot.get("markets", {}), sent_signals, open_trades)

            report = render_report(analysis, selected, started, trade_record)
            send_telegram(report)
            remove_waiting_message(waiting_id)
            for candidate in selected:
                sent_signals[candidate["key"]] = current_time
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {str(exc)}"
            print(f"Combined analysis/order/report failed: {error_text}")
            try:
                send_telegram(
                    "BINGX ANALYSIS / DEMO ORDER ERROR\n"
                    f"Reason: {error_text[:2500]}\n\n"
                    "If a private order request timed out, verify demo order/position state before retrying manually."
                )
            except Exception as telegram_exc:
                print("Telegram error report failed:", telegram_exc)
            remove_waiting_message(waiting_id)
    else:
        print("No interesting new candidates")

    market_snapshot = {
        m["symbol"]: {"price": m["price"], "volume": m["volume"], "change_24h": m["change_24h"]}
        for m in markets
    }
    save_snapshot(market_snapshot, sent_signals, open_trades)
    print("Snapshot saved")

    if history.get("closed"):
        history["stats"] = compute_stats(history["closed"])
        bal = None
        if ENABLE_AUTO_TRADE and _api_credentials()[0]:
            try:
                bal = get_balance_vst()
            except Exception as exc:
                print(f"Balance query failed: {exc}")
        print(format_stats_message(history["stats"], bal))


if __name__ == "__main__":
    main()

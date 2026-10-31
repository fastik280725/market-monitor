import json
from io import BytesIO
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://open-api.bingx.com"
TICKER_URL = f"{BASE_URL}/openApi/swap/v2/quote/ticker"
KLINES_URL = f"{BASE_URL}/openApi/swap/v3/quote/klines"
FUNDING_URL = f"{BASE_URL}/openApi/swap/v2/quote/premiumIndex"
OPEN_INTEREST_URL = f"{BASE_URL}/openApi/swap/v2/quote/openInterest"
DEPTH_URL = f"{BASE_URL}/openApi/swap/v2/quote/depth"
TRADES_URL = f"{BASE_URL}/openApi/swap/v2/quote/trades"
PREMIUM_INDEX_URL = f"{BASE_URL}/openApi/swap/v2/quote/premiumIndex"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
SNAPSHOT_FILE = Path("market_snapshot.json")
TOP_MARKETS = 100
MAX_CANDIDATES = 5
MIN_RR = 1.4
INTERVAL = "15m"
TIMEFRAMES = {
    "15m": {"interval_ms": 15 * 60 * 1000, "bars_per_hour": 4},
    "1h": {"interval_ms": 60 * 60 * 1000, "bars_per_hour": 1},
    "4h": {"interval_ms": 4 * 60 * 60 * 1000, "bars_per_hour": 0.25},
}
CANDLE_LIMIT = 30
COOLDOWN_SECONDS = 3600
REQUEST_TIMEOUT = 25
GEMINI_TIMEOUT = 90
REQUEST_PAUSE = 0.12
RETRY_INITIAL_SECONDS = 5
RETRY_MAX_SECONDS = 60
JSON_RETRY_ATTEMPTS = 3
TELEGRAM_MAX_LENGTH = 3500
session = requests.Session()


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
    data = api_get(TICKER_URL).get("data", [])
    if isinstance(data, dict):
        data = [data]
    markets = []
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", ""))
        if not symbol.endswith("-USDT"):
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
    return markets


def parse_candle(row):
    if isinstance(row, dict):
        timestamp = row.get("time", row.get("timestamp", row.get("openTime", 0)))
        o, h, l, c = (number(row.get(k)) for k in ("open", "high", "low", "close"))
        quote_volume = number(row.get("quoteVolume", row.get("quote_volume", 0)))
        volume = quote_volume if quote_volume > 0 else number(row.get("volume", row.get("vol", 0)))
        volume_type = "quote" if quote_volume > 0 else "base"
    elif isinstance(row, (list, tuple)) and len(row) >= 6:
        timestamp = row[0]
        o, h, l, c = (number(row[i]) for i in range(1, 5))
        quote_volume = number(row[7]) if len(row) >= 8 else 0
        volume = quote_volume if quote_volume > 0 else number(row[5])
        volume_type = "quote" if quote_volume > 0 else "base"
    else:
        return None
    timestamp = int(number(timestamp))
    if timestamp <= 0 or min(o, h, l, c) <= 0 or volume < 0:
        return None
    return {
        "time": timestamp, "open": o, "high": h, "low": l,
        "close": c, "volume": volume, "volume_type": volume_type,
    }


def fetch_candles(symbol, interval=INTERVAL):
    """Fetch only closed candles for the requested timeframe."""
    if interval not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {interval}")
    rows = api_get(
        KLINES_URL,
        {"symbol": symbol, "interval": interval, "limit": CANDLE_LIMIT},
    ).get("data", [])
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
    candles = sorted({c["time"]: c for c in candles}.values(), key=lambda c: c["time"])
    now_ms = int(time.time() * 1000)
    interval_ms = TIMEFRAMES[interval]["interval_ms"]
    return [
        c for c in candles
        if c["time"] < 10**12 or c["time"] + interval_ms <= now_ms
    ]


def summarize_timeframe(candles):
    """Compact, deterministic trend/volume/breakout summary for one timeframe."""
    if len(candles) < 22:
        return {
            "available": False,
            "direction": "UNKNOWN",
            "reason": f"Недостаточно закрытых свечей: {len(candles)}/22",
        }

    last = candles[-1]
    previous = candles[-2]
    window_ref = candles[-5]
    close = last["close"]
    if min(close, previous["close"], window_ref["close"]) <= 0:
        return {"available": False, "direction": "UNKNOWN", "reason": "Некорректные цены свечей"}

    change_last_pct = (close / previous["close"] - 1) * 100
    change_window_pct = (close / window_ref["close"] - 1) * 100
    prior = candles[-21:-1]
    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)
    same_volume_units = len({c["volume_type"] for c in candles[-21:]}) == 1
    avg_volume = sum(c["volume"] for c in prior) / len(prior)
    volume_valid = same_volume_units and avg_volume > 0 and last["volume"] > 0
    volume_ratio = last["volume"] / avg_volume if volume_valid else None
    breakout_up = bool(volume_valid and close > prior_high and volume_ratio >= 1.2)
    breakout_down = bool(volume_valid and close < prior_low and volume_ratio >= 1.2)

    if breakout_up or (change_last_pct > 0 and change_window_pct > 0):
        direction = "LONG"
    elif breakout_down or (change_last_pct < 0 and change_window_pct < 0):
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    return {
        "available": True,
        "direction": direction,
        "last_closed_price": close,
        "last_candle_change_pct": round(change_last_pct, 4),
        "four_candle_change_pct": round(change_window_pct, 4),
        "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
        "volume_comparable": volume_valid,
        "breakout_up": breakout_up,
        "breakout_down": breakout_down,
        "prior_20_candle_high": prior_high,
        "prior_20_candle_low": prior_low,
        "last_closed_candle_utc": datetime.fromtimestamp(
            last["time"] / 1000 if last["time"] >= 10**12 else last["time"],
            tz=timezone.utc,
        ).isoformat(),
    }


def compare_timeframes(candidate, timeframe_candles):
    """Require 15m strength and 1h agreement; reject a direct 4h conflict."""
    summaries = {
        tf: summarize_timeframe(candles)
        for tf, candles in timeframe_candles.items()
    }
    candidate_direction = candidate["direction"]
    h1 = summaries["1h"]
    h4 = summaries["4h"]

    if not h1.get("available"):
        return None
    if h1["direction"] != candidate_direction:
        return None
    if not h4.get("available"):
        return None
    if h4["direction"] not in (candidate_direction, "NEUTRAL"):
        return None

    # Higher score for three-timeframe agreement; neutral 4h is allowed but marked.
    confluence = "15m + 1h + 4h" if h4["direction"] == candidate_direction else "15m + 1h; 4h нейтральный"
    candidate["timeframe_analysis"] = summaries
    candidate["timeframe_confluence"] = confluence
    candidate["score"] += 3.0 if h4["direction"] == candidate_direction else 0.5
    candidate["candles_by_timeframe"] = timeframe_candles
    return candidate


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
    cursor = data.get("scan_cursor", 0)
    try:
        cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        cursor = 0
    return {
        "markets": markets if isinstance(markets, dict) else {},
        "sent_signals": sent if isinstance(sent, dict) else {},
        "scan_cursor": cursor,
    }


def save_snapshot(markets, sent_signals, scan_cursor=0):
    SNAPSHOT_FILE.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "markets": markets,
        "sent_signals": sent_signals,
        "scan_cursor": int(scan_cursor),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


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
На вход поступают только кандидаты, прошедшие технический фильтр силы и
сравнение 15m/1h/4h. Сначала проверь, согласованы ли таймфреймы:
15m — точка краткосрочного импульса; 1h — подтверждение движения;
4h — старший контекст. Не считай нейтральный 4h подтверждением.
Если 4h противоречит направлению, не открывай сигнал. Выбери только ОДИН
сигнал с сильным подтверждением или NO_TRADE.
Если подтверждения недостаточно, верни NO_TRADE. Не показывай сравнение монет. Не выдумывай ликвидации, полную дельту,
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
Если уровни нельзя обосновать или подтверждение ещё не получено,
верни NO_TRADE. Для LONG: stop_loss < entry_price < take_profit.
Для SHORT: take_profit < entry_price < stop_loss.
Минимальное соотношение потенциальной прибыли к риску — 1.4.
Если обоснованные уровни не обеспечивают RR >= 1.4, верни NO_TRADE.
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
    return {
        "symbol": candidate["symbol"],
        "last_closed_price": candidate["price"],
        "preliminary_direction": candidate["direction"],
        "preliminary_label": candidate["title"],
        "preliminary_strength": candidate["strength"],
        "preliminary_score": round(candidate["score"], 2),
        "timeframe_confluence": candidate.get("timeframe_confluence"),
        "timeframe_analysis": candidate.get("timeframe_analysis", {}),
        "change_15m_pct": round(candidate["change_15m"], 3),
        "change_1h_pct": round(candidate["change_1h"], 3),
        "change_24h_pct": round(candidate["change_24h"], 3),
        "volume_ratio": round(candidate["volume_ratio"], 3) if candidate["volume_valid"] else None,
        "prior_20_candle_high": candidate["prior_high"],
        "prior_20_candle_low": candidate["prior_low"],
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
        "candles_by_timeframe_utc": {
            tf: [{
                "time": datetime.fromtimestamp(
                    c["time"] / 1000 if c["time"] >= 10**12 else c["time"],
                    tz=timezone.utc,
                ).isoformat(),
                "open": c["open"], "high": c["high"], "low": c["low"],
                "close": c["close"], "volume": c["volume"],
                "volume_type": c["volume_type"],
            } for c in timeframe_candles[-22:]]
            for tf, timeframe_candles in candidate.get("candles_by_timeframe", {"15m": candles}).items()
        },
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
        "timeframes_compared": ["15m", "1h", "4h"],
        "timeframe_rules": {
            "15m": "краткосрочный импульс и кандидат на сигнал",
            "1h": "обязательное подтверждение направления",
            "4h": "старший контекст; противоположное направление запрещает сигнал",
        },
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
    delay = RETRY_INITIAL_SECONDS
    attempt = 0
    max_attempts = 4
    while attempt < max_attempts:
        attempt += 1
        if on_attempt is not None:
            try:
                on_attempt(attempt)
            except Exception as exc:
                print(f"Telegram status update failed: {exc}")
        print(f"Gemini combined analysis attempt {attempt} for {len(candidates)} candidates")
        try:
            response = session.post(
                GEMINI_URL,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=GEMINI_TIMEOUT,
            )
        except requests.RequestException as exc:
            print(f"Gemini network error on attempt {attempt}: {exc}")
            if attempt >= max_attempts:
                return {
                    "status": "NO_TRADE",
                    "reason": "Сеть Gemini недоступна после повторных попыток.",
                }
            print(f"Retrying Gemini in {delay}s")
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
            if is_retryable_status(response.status_code, error_message):
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = max(delay, float(retry_after or 0))
                except ValueError:
                    wait_seconds = delay
                wait_seconds = min(wait_seconds, RETRY_MAX_SECONDS)
                if attempt >= max_attempts:
                    print(f"Gemini retry limit reached after HTTP {response.status_code}; no alert will be sent")
                    return {
                        "status": "NO_TRADE",
                        "reason": "Временная ошибка Gemini; повторите анализ при следующем сканировании.",
                    }
                print(f"Temporary Gemini HTTP {response.status_code}: {error_message[:200]}; retry in {wait_seconds}s")
                time.sleep(wait_seconds)
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                continue
            print(f"Non-retryable Gemini HTTP {response.status_code}: {error_message[:300]}")
            return {
                "status": "NO_TRADE",
                "reason": "Gemini временно не смог выполнить анализ.",
            }
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
            if attempt >= min(JSON_RETRY_ATTEMPTS, max_attempts):
                print("JSON retry limit reached. Returning NO_TRADE safely.")
                return {
                    "status": "NO_TRADE",
                    "reason": "Не удалось получить корректный анализ от Gemini.",
                }
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
            continue


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


def render_report(analysis, selected, started):
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
    if ratio + 1e-9 < MIN_RR:
        raise RuntimeError(f"RR {ratio:.2f} ниже минимума {MIN_RR:.2f}")
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
    payload = {"chat_id": chat_id, "message_id": message_id}
    if text is not None:
        payload["text"] = text
    response = session.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    result = response.json()
    if not response.ok or not result.get("ok"):
        raise RuntimeError(f"Telegram {method}: {result}")


def remove_waiting_message(message_id):
    if message_id is None:
        return
    try:
        telegram_message_action("deleteMessage", message_id)
    except Exception as exc:
        print(f"Could not delete waiting message: {exc}")


def send_telegram_document(filename, content):
    """Send a UTF-8 text report as a Telegram document."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    data = BytesIO(content.encode("utf-8-sig"))
    response = session.post(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data={"chat_id": chat_id},
        files={"document": (filename, data, "text/plain; charset=utf-8")},
        timeout=REQUEST_TIMEOUT,
    )
    try:
        result = response.json()
    except ValueError:
        result = {"description": response.text[:500]}
    if not response.ok or not result.get("ok"):
        raise RuntimeError(f"Telegram document error: {result}")
    print(f"Telegram statistics file sent: {filename}")


def send_scan_statistics(stats, started, cursor, total, finished=False):
    elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
    lines = [
        "📊 Отчёт сканера BingX",
        f"Проверено за запуск: {stats['checked']} из {total}",
        f"Без сильного кандидата 15m: {stats['weak_15m']}",
        f"Отклонено на 1h/4h: {stats['tf_rejected']}",
        f"Прошли все ТФ: {stats['tf_passed']}",
        f"Повторные сигналы (cooldown): {stats['cooldown']}",
        f"Передано Gemini: {stats['gemini_candidates']}",
        f"Ответов NO_TRADE: {stats['no_trade']}",
        f"Подтверждено: {stats['confirmed']}",
        f"Ошибок данных/API/анализа: {stats['errors']}",
        f"Продолжение с позиции: {cursor + 1 if cursor < total else 1} из {total}",
        f"Время работы: {elapsed} сек.",
        f"RR минимум: {MIN_RR}",
    ]
    if finished:
        lines.append("Список рынков пройден полностью; следующий цикл начнётся сначала.")
    report = "\n".join(lines) + "\n"
    filename = f"bingx_scan_stats_{started.strftime('%Y%m%d_%H%M%S')}_UTC.txt"
    try:
        send_telegram_document(filename, report)
    except Exception as exc:
        print(f"Could not send statistics file: {type(exc).__name__}: {exc}")
    print(" | ".join(lines))


def main():
    started = datetime.now(timezone.utc)
    print("BingX multi-timeframe market scanner started:", started.isoformat())
    snapshot = load_snapshot()
    sent_signals = snapshot["sent_signals"]
    stats = {k: 0 for k in (
        "checked", "weak_15m", "tf_rejected", "tf_passed", "cooldown",
        "gemini_candidates", "no_trade", "confirmed", "errors"
    )}
    markets = fetch_markets()
    if not markets:
        raise RuntimeError("BingX returned no USDT markets")

    # Search the entire volume-ranked market list in batches of 100. Persist the
    # cursor so a time-limited GitHub Actions run can continue in the next run.
    batch_size = TOP_MARKETS
    cursor = snapshot.get("scan_cursor", 0)
    if cursor >= len(markets):
        cursor = 0
    deadline = time.monotonic() + 330  # leave margin under the workflow's 8-minute limit
    market_snapshot = {
        m["symbol"]: {
            "price": m["price"],
            "volume": m["volume"],
            "change_24h": m["change_24h"],
        }
        for m in markets
    }

    while cursor < len(markets) and time.monotonic() < deadline:
        batch_start = cursor
        batch_end = min(batch_start + batch_size, len(markets))
        print(f"Scanning market batch {batch_start + 1}-{batch_end} of {len(markets)}")
        candidates = []
        current_time = int(time.time())
        index = batch_start

        while index < batch_end and time.monotonic() < deadline - 25:
            market = markets[index]
            symbol = market["symbol"]
            stats["checked"] += 1
            try:
                candles_by_timeframe = {
                    tf: fetch_candles(symbol, tf)
                    for tf in ("15m", "1h", "4h")
                }
                candidate = analyze_market(market, candles_by_timeframe["15m"])
                if candidate and candidate.get("strength") == "СИЛЬНЫЙ":
                    candidate = compare_timeframes(candidate, candles_by_timeframe)
                    if candidate:
                        stats["tf_passed"] += 1
                        key = f"{symbol}:{candidate['direction']}:{candidate['strength']}"
                        last_sent = int(sent_signals.get(key, 0) or 0)
                        if current_time - last_sent >= COOLDOWN_SECONDS:
                            candidate["key"] = key
                            candidates.append(candidate)
                        else:
                            stats["cooldown"] += 1
                            print(f"[{index + 1}/{len(markets)}] {symbol}: suppressed by cooldown")
                    else:
                        stats["tf_rejected"] += 1
                        print(f"[{index + 1}/{len(markets)}] {symbol}: timeframe conflict or insufficient 1h/4h data")
                else:
                    stats["weak_15m"] += 1
                    print(f"[{index + 1}/{len(markets)}] {symbol}: no strong 15m candidate")
            except Exception as exc:
                stats["errors"] += 1
                print(f"[{index + 1}/{len(markets)}] {symbol}: {exc}")
            index += 1
            if index < batch_end:
                time.sleep(REQUEST_PAUSE)

        # Advance only over markets actually visited; the next run resumes here.
        cursor = index
        save_snapshot(market_snapshot, sent_signals, cursor)
        print(f"Saved scan cursor at market {cursor} of {len(markets)}")

        candidates.sort(key=lambda c: c["score"], reverse=True)
        selected = candidates[:MAX_CANDIDATES]
        stats["gemini_candidates"] += len(selected)
        print(f"Strong multi-timeframe candidates in this batch: {len(candidates)}; sent to Gemini: {len(selected)}")

        if selected and time.monotonic() >= deadline - 20:
            # Do not skip a promising batch if there is not enough time to validate it.
            cursor = batch_start
            save_snapshot(market_snapshot, sent_signals, cursor)
            print("Not enough time to validate candidates; this batch will be retried next run.")
            break

        if selected:
            metrics = {}
            for candidate in selected:
                try:
                    metrics[candidate["symbol"]] = fetch_optional_metrics(candidate["symbol"])
                except Exception as exc:
                    print(f"Optional metrics unavailable for {candidate['symbol']}: {exc}")
                    metrics[candidate["symbol"]] = {"data_status": {"all": "unavailable"}}

            waiting_id = None
            try:
                waiting_id = send_telegram("⏳ Сравниваю сильные кандидаты по 15m / 1h / 4h…")[0]
            except Exception as exc:
                print(f"Could not send temporary status message: {exc}")

            try:
                analysis = gemini_analyze_many(selected, metrics)
                status = str(analysis.get("status", "")).upper()
                if status == "TRADE":
                    report = render_report(analysis, selected, started)
                    # Include the objective timeframe agreement in the alert.
                    chosen_symbol = str(analysis.get("symbol", ""))
                    chosen = next((c for c in selected if c["symbol"] == chosen_symbol), None)
                    if chosen and chosen.get("timeframe_confluence"):
                        report += f"\n🧭 Подтверждение ТФ: {chosen['timeframe_confluence']}"
                    send_telegram(report)
                    if chosen:
                        sent_signals[chosen["key"]] = current_time
                    stats["confirmed"] += 1
                    print(f"Strong multi-timeframe signal alert sent for {chosen_symbol}")
                    save_snapshot(market_snapshot, sent_signals, cursor)
                    send_scan_statistics(stats, started, cursor, len(markets))
                    return
                stats["no_trade"] += 1
                print("Gemini returned NO_TRADE; continuing with the next market batch")
            except Exception as exc:
                stats["errors"] += 1
                print(f"Combined analysis failed; no alert sent: {type(exc).__name__}: {exc}")
            finally:
                remove_waiting_message(waiting_id)

        if cursor >= len(markets):
            print("Reached the end of the available BingX USDT market list without a confirmed signal.")
            cursor = 0
            save_snapshot(market_snapshot, sent_signals, cursor)
            break

    if time.monotonic() >= deadline:
        print("Time budget reached; saved cursor will resume the search on the next workflow run.")
    elif cursor < len(markets):
        print("Search paused; saved cursor will resume on the next workflow run.")

    save_snapshot(market_snapshot, sent_signals, cursor)
    send_scan_statistics(stats, started, cursor, len(markets), finished=(cursor == 0))
    print("Snapshot saved; no confirmed signal was sent in this run.")


if __name__ == "__main__":
    main()

# bot.py — Bybit TradingView Webhook Bot (Mainnet) with Enhanced Telegram Messages — Long-only, 3× Leverage
import os
import time
import json
import hmac
import hashlib
import logging
import requests
from flask import Flask, request, jsonify
from math import floor
from datetime import datetime

# ——— Configuration ———
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BASE_URL         = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")
RECV_WINDOW      = "5000"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

LONG_LEVERAGE    = 3  # leverage for long positions

# ——— Logging Setup ———
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(funcName)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ——— Signature Helper ———
def sign_request(payload_str: str = "", query: str = ""):
    ts = str(int(time.time() * 1000))
    to_sign = ts + BYBIT_API_KEY + RECV_WINDOW + (payload_str or query)
    signature = hmac.new(BYBIT_API_SECRET.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    return ts, signature

# ——— HTTP Helpers ———
def http_get(path: str, params: dict = None):
    url = f"{BASE_URL}/{path}"
    query = '&'.join(f"{k}={v}" for k, v in (params or {}).items())
    ts, sign = sign_request(query=query)
    headers = {
        "Content-Type":       "application/json",
        "X-BAPI-API-KEY":     BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN":        sign
    }
    resp = requests.get(url, headers=headers, params=params)
    logger.debug(f"GET {path}?{query} → {resp.status_code} {resp.text}")
    return resp

def http_post(path: str, body: dict):
    url = f"{BASE_URL}/{path}"
    payload_str = json.dumps(body, separators=(',', ':'), sort_keys=True)
    ts, sign = sign_request(payload_str=payload_str)
    headers = {
        "Content-Type":       "application/json",
        "X-BAPI-API-KEY":     BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN":        sign
    }
    resp = requests.post(url, headers=headers, data=payload_str)
    logger.debug(f"POST {path} {payload_str} → {resp.status_code} {resp.text}")
    return resp

# ——— Bybit Utilities ———
def get_wallet_balance() -> float:
    data = http_get("v5/account/wallet-balance", {"coin": "USDT", "accountType": "UNIFIED"}).json()
    if data.get("retCode") == 0:
        return float(data["result"]["list"][0].get("totalAvailableBalance", 0))
    else:
        logger.error(f"Failed to fetch wallet balance: {data}")
        return 0.0

def get_symbol_info(symbol: str):
    data = http_get("v5/market/instruments-info", {"category": "linear", "symbol": symbol}).json()
    if data.get("retCode") == 0:
        filt = data["result"]["list"][0]["lotSizeFilter"]
        return float(filt["minOrderQty"]), float(filt["qtyStep"])
    else:
        logger.error(f"Failed to fetch symbol info for {symbol}: {data}")
        return 0.0, 0.0

def get_ticker_price(symbol: str) -> float:
    data = http_get("v5/market/tickers", {"category": "linear", "symbol": symbol}).json()
    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except Exception as e:
        logger.error(f"Failed to fetch ticker price for {symbol}: {e}")
        return 0.0

def get_positions(symbol: str):
    data = http_get("v5/position/list", {"category": "linear", "symbol": symbol}).json()
    return data.get("result", {}).get("list", [])

def get_executions(symbol: str, order_id: str):
    data = http_get("v5/execution/list", {"category": "linear", "symbol": symbol, "orderId": order_id}).json()
    return data.get("result", {}).get("list", [])

# ——— Send Telegram Message ———
def send_telegram(text: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text}
            )
            logger.debug(f"Telegram send → {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
    else:
        logger.warning("Telegram token or chat ID not set; skipping Telegram notification.")

# ——— Flask App ———
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    logger.info(f"▶ Получен Webhook: {json.dumps(data)}")

    symbol   = data.get('symbol')
    side_cmd = data.get('side', '').lower()

    if not symbol or not side_cmd:
        logger.warning(f"Ignoring webhook with missing symbol or side: {data}")
        return jsonify({"status": "ignored", "reason": "missing symbol or side"}), 200

    # ——— Open Long ———
    if side_cmd == 'buy':
        # Set leverage
        logger.info(f"Setting leverage {LONG_LEVERAGE}× for {symbol}")
        # Only send buy_leverage, remove sell_leverage entirely
        http_post("v5/position/set-leverage", {
            "category":    "linear",
            "symbol":      symbol,
            "buy_leverage": LONG_LEVERAGE,
            "position_idx": 0
        })

        balance = get_wallet_balance()
        min_q, step = get_symbol_info(symbol)
        price = get_ticker_price(symbol)
        if price <= 0 or step <= 0:
            logger.error(f"Invalid price or step for {symbol}: price={price}, step={step}")
            return jsonify({"status": "error", "reason": "invalid price or step"}), 200

        qty = max(min_q, step * floor((balance * LONG_LEVERAGE) / (price * step)))
        logger.info(f"Calculated order quantity for {symbol}: {qty}")

        res = http_post("v5/order/create", {
            "category":    "linear",
            "symbol":      symbol,
            "side":        "Buy",
            "orderType":   "Market",
            "qty":         str(qty),
            "timeInForce": "ImmediateOrCancel"
        })
        resp_data = res.json()
        if resp_data.get("retCode") != 0:
            logger.error(f"Failed to create long order: {resp_data}")
            return jsonify({"status": "error", "reason": resp_data}), 200

        order_id = resp_data["result"].get("orderId", "")
        execs = get_executions(symbol, order_id)
        if execs:
            avg_price = sum(float(e['execPrice']) * float(e['execQty']) for e in execs) / sum(float(e['execQty']) for e in execs)
        else:
            avg_price = price

        pct = (qty * avg_price / LONG_LEVERAGE) / balance * 100 if balance > 0 else 0
        msg = (
            f"🔹 Лонг открыт: {symbol}\n"
            f"• Цена входа: {avg_price:.4f}\n"
            f"• Риск: {pct:.2f}% от депозита\n"
            f"• Плечо: {LONG_LEVERAGE}×"
        )
        logger.info(msg)
        send_telegram(msg)
        return jsonify({"status": "ok"}), 200

    # ——— Close Long ———
    if side_cmd == 'exit':
        positions = get_positions(symbol)
        original = next((p for p in positions if p['side'] == 'Buy' and float(p['size']) > 0), None)
        if not original:
            logger.warning(f"No open long position to close for {symbol}")
            return jsonify({"status": "no_position"}), 200

        qty = float(original['size'])
        entry_price = float(original['avgPrice'])
        balance_before = get_wallet_balance()
        res = http_post("v5/order/create", {
            "category":    "linear",
            "symbol":      symbol,
            "side":        "Sell",
            "orderType":   "Market",
            "qty":         str(qty),
            "timeInForce": "ImmediateOrCancel",
            "reduce_only": True
        })
        resp_data = res.json()
        if resp_data.get("retCode") != 0:
            logger.error(f"Failed to create close order: {resp_data}")
            return jsonify({"status": "error", "reason": resp_data}), 200

        order_id = resp_data["result"].get("orderId", "")
        execs = get_executions(symbol, order_id)
        if execs:
            exit_price = sum(float(e['execPrice']) * float(e['execQty']) for e in execs) / sum(float(e['execQty']) for e in execs)
        else:
            exit_price = entry_price

        pnl = (exit_price - entry_price) * qty
        fee = sum(float(e.get('execFee', 0)) for e in execs)
        net_pnl = pnl - fee
        pct_change = net_pnl / balance_before * 100 if balance_before > 0 else 0
        msg = (
            f"🔹 Лонг закрыт: {symbol}\n"
            f"• PnL: {net_pnl:.4f} USDT ({pct_change:+.2f}%)\n"
            f"• Цена выхода: {exit_price:.4f}"
        )
        logger.info(msg)
        send_telegram(msg)
        return jsonify({"status": "ok"}), 200

    logger.info(f"Ignored webhook with side='{side_cmd}' for {symbol}")
    return jsonify({"status": "ignored"}), 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port)

# bot.py — Bybit TradingView Webhook Bot (Mainnet) with Enhanced Telegram Messages and Accurate PnL
import os
import time
import json
import hmac
import hashlib
import logging
import requests
from flask import Flask, request
from math import floor

# ——— Configuration ———
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BASE_URL         = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")
RECV_WINDOW      = "5000"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

LONG_LEVERAGE  = 3  # leverage для лонга
SHORT_LEVERAGE = 1  # leverage для шорта

# ——— Logging Setup ———
logging.basicConfig(
    level=logging.DEBUG,
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
        "Content-Type":      "application/json",
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
        "Content-Type":      "application/json",
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
    data = http_get("v5/account/wallet-balance", {"coin":"USDT","accountType":"UNIFIED"}).json()
    return float(data["result"]["list"][0].get("totalAvailableBalance", 0)) if data.get("retCode")==0 else 0.0


def get_symbol_info(symbol: str):
    lst = http_get("v5/market/instruments-info", {"category":"linear","symbol":symbol}).json()["result"]["list"]
    filt = lst[0]["lotSizeFilter"]
    return float(filt["minOrderQty"]), float(filt["qtyStep"])


def get_ticker_price(symbol: str) -> float:
    return float(http_get("v5/market/tickers", {"category":"linear","symbol":symbol}).json()["result"]["list"][0]["lastPrice"])


def get_positions(symbol: str):
    return http_get("v5/position/list", {"category":"linear","symbol":symbol}).json()["result"]["list"]


def set_leverage(symbol: str):
    body={"category":"linear","symbol":symbol,"buy_leverage":LONG_LEVERAGE,
          "sell_leverage":SHORT_LEVERAGE,"position_idx":0}
    return http_post("v5/position/set-leverage", body)


def get_executions(symbol: str, order_id: str):
    return http_get("v5/execution/list", {"category":"linear","symbol":symbol,"orderId":order_id}).json()["result"]["list"]

# ——— Send Telegram ———
def send_telegram(text: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id":TELEGRAM_CHAT_ID,"text":text})

# ——— Flask App ———
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    symbol = data.get('symbol')
    side_cmd = data.get('side','').lower()

    # Открытие лонга/шорта
    if side_cmd in ('buy','sell'):
        side = 'Buy' if side_cmd=='buy' else 'Sell'
        set_leverage(symbol)
        balance = get_wallet_balance()
        min_q, step = get_symbol_info(symbol)
        price = get_ticker_price(symbol)
        lev = LONG_LEVERAGE if side=='Buy' else SHORT_LEVERAGE
        qty = max(min_q, step * floor((balance * lev) / (price * step)))
        res = http_post("v5/order/create", {"category":"linear","symbol":symbol,
                                              "side":side,"orderType":"Market",
                                              "qty":str(qty),"timeInForce":"ImmediateOrCancel"})
        order_id = res.json().get("result",{}).get("orderId","")
        execs = get_executions(symbol, order_id)
        avg_price = (sum(float(e['execPrice']) * float(e['execQty']) for e in execs) /
                     sum(float(e['execQty']) for e in execs)) if execs else price
        pct = (qty * avg_price / lev) / balance * 100 if balance>0 else 0
        txt = (f"{'Лонг' if side=='Buy' else 'Шорт'}: {symbol}\n"
               f"Цена входа: {avg_price:.4f}\n"
               f"Процент от депозита: {pct:.2f}%\n"
               f"Плечо: {lev}x")
        send_telegram(txt)
        return {"status":"ok"}

    # Закрытие позиции
    if side_cmd == 'exit':
        # Получаем открытую позицию
        positions = get_positions(symbol)
        original = None
        for p in positions:
            size = float(p['size'])
            if size > 0 and p['side'] == 'Buy':
                original = ('Buy', size, float(p['avgPrice']))
                break
            if size > 0 and p['side'] == 'Sell':
                original = ('Sell', size, float(p['avgPrice']))
                break
        if not original:
            return {"status":"no_position"}
        orig_side, qty, entry_price = original
        balance_before = get_wallet_balance()
        # Закрывающий side
        close_side = 'Sell' if orig_side=='Buy' else 'Buy'
        res = http_post("v5/order/create", {"category":"linear","symbol":symbol,
                                              "side":close_side,"orderType":"Market",
                                              "qty":str(qty),"timeInForce":"ImmediateOrCancel",
                                              "reduce_only":True})
        order_id = res.json().get("result",{}).get("orderId","")
        execs = get_executions(symbol, order_id)
        # Средняя цена выхода
        exit_price = (sum(float(e['execPrice']) * float(e['execQty']) for e in execs) /
                      sum(float(e['execQty']) for e in execs)) if execs else entry_price
        # PnL
        if orig_side == 'Buy':
            pnl = (exit_price - entry_price) * qty
        else:
            pnl = (entry_price - exit_price) * qty
        # Комиссия
        fee = sum(float(e.get('execFee',0)) for e in execs)
        net_pnl = pnl - fee
        pct_change = net_pnl / balance_before * 100 if balance_before>0 else 0
        side_text = 'Лонг закрыт' if orig_side=='Buy' else 'Шорт закрыт'
        txt = (f"{side_text}: {symbol}\n"
               f"Изменение баланса: {pct_change:+.2f}%")
        send_telegram(txt)
        return {"status":"ok"}

    return {"status":"ignored"}

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT',10000)))

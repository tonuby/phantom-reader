"""
PHANTOM TRADER v1.0
===================
TradingView webhook sinyallerini alir, Binance Futures'da otomatik islem acar.

Sistem:
1. TV sinyal gonderir -> Bu bot webhook alir
2. Bot Binance'de market emri ile girer
3. Stop Loss ve TP1 emirleri koyar
4. TP1 gelince %50 kapatir, stop'u BE'ye ceker
5. Hedef gelince kalanı kapatir
6. Her adimda Telegram'a bildirim gider

Kurulum:
    pip install flask requests hmac hashlib

Calistirma:
    python phantom_trader.py
"""

from flask import Flask, request, jsonify
import requests
import hmac
import hashlib
import time
import json
import logging
import threading
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

app = Flask(__name__)

# =========================================================================
# AYARLAR — Environment variables olarak yukle
# =========================================================================
import os

BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
TG_BOT_URL         = "https://api.telegram.org/bot8730809758:AAH5pxgy3PWA4cd0_m6N1Jb5-QOTQcPJJ6Q/sendMessage"
TG_CHAT_ID         = "811792517"
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "phantom2026")

# Risk ayarlari
RISK_USDT    = float(os.environ.get("RISK_USDT", "10"))     # her islemde 10 USDT risk
LEVERAGE     = int(os.environ.get("LEVERAGE", "10"))         # kaldirac

BINANCE_URL  = "https://fapi.binance.com"

# Aktif islemler: {symbol: {ep, sl, tp1, tp2, qty, dir, tp1_hit}}
aktif_islemler = {}

# =========================================================================
# TELEGRAM
# =========================================================================
def send_tg(text):
    try:
        requests.post(TG_BOT_URL, json={
            "chat_id": TG_CHAT_ID,
            "text":    text
        }, timeout=10)
    except Exception as e:
        log.error(f"Telegram hatasi: {e}")

# =========================================================================
# BINANCE API
# =========================================================================
def binance_sign(params):
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()
    return query + "&signature=" + signature

def binance_request(method, endpoint, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    signed = binance_sign(params)
    url    = f"{BINANCE_URL}{endpoint}?{signed}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            r = requests.post(url, headers=headers, timeout=10)
        elif method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"Binance API hatasi: {e}")
        return {"error": str(e)}

def set_leverage(symbol, leverage):
    return binance_request("POST", "/fapi/v1/leverage", {
        "symbol":   symbol,
        "leverage": leverage
    })

def get_symbol_info(symbol):
    """Sembol bilgisi al - precision icin"""
    try:
        r = requests.get(f"{BINANCE_URL}/fapi/v1/exchangeInfo", timeout=10)
        data = r.json()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                return s
    except:
        pass
    return None

def round_qty(symbol, qty):
    """Miktar hassasiyetini ayarla"""
    info = get_symbol_info(symbol)
    if info:
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                precision = len(str(step).rstrip("0").split(".")[-1])
                return round(qty, precision)
    return round(qty, 3)

def round_price(symbol, price):
    """Fiyat hassasiyetini ayarla"""
    info = get_symbol_info(symbol)
    if info:
        tick = float(info.get("filters", [{}])[0].get("tickSize", "0.01"))
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
                precision = len(str(tick).rstrip("0").split(".")[-1])
                return round(price, precision)
    return round(price, 2)

def place_market_order(symbol, side, qty):
    """Market emri"""
    return binance_request("POST", "/fapi/v1/order", {
        "symbol":   symbol,
        "side":     side,
        "type":     "MARKET",
        "quantity": qty
    })

def place_limit_order(symbol, side, qty, price, reduce_only=False):
    """Limit emri"""
    params = {
        "symbol":           symbol,
        "side":             side,
        "type":             "LIMIT",
        "timeInForce":      "GTC",
        "quantity":         qty,
        "price":            price,
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return binance_request("POST", "/fapi/v1/order", params)

def place_stop_order(symbol, side, qty, stop_price, reduce_only=True):
    """Stop Market emri"""
    params = {
        "symbol":       symbol,
        "side":         side,
        "type":         "STOP_MARKET",
        "quantity":     qty,
        "stopPrice":    stop_price,
        "reduceOnly":   "true" if reduce_only else "false"
    }
    return binance_request("POST", "/fapi/v1/order", params)

def cancel_all_orders(symbol):
    """Tum açik emirleri iptal et"""
    return binance_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

def get_position(symbol):
    """Mevcut pozisyonu al"""
    result = binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if isinstance(result, list):
        for p in result:
            if p["symbol"] == symbol:
                return p
    return None

# =========================================================================
# ISLEM AC
# =========================================================================
def islem_ac(symbol, yon, giris, stop, tp1, hedef, miktar_usdt):
    """
    Yeni islem ac:
    1. Leverage ayarla
    2. Market emri ile gir
    3. Stop Loss emri koy
    4. TP1 emri koy (%50)
    5. TP2 emri koy (%50)
    """
    try:
        # Sembol duzelt (BTCUSDT.P -> BTCUSDT)
        clean_symbol = symbol.replace(".P", "")

        # Kaldirac ayarla
        set_leverage(clean_symbol, LEVERAGE)
        time.sleep(0.1)

        # Pozisyon boyutu hesapla
        sl_dist  = abs(giris - stop)
        qty_coin = miktar_usdt / sl_dist
        qty_coin = round_qty(clean_symbol, qty_coin)

        # Fiyatlari yuvarla
        tp1_r    = round_price(clean_symbol, tp1)
        hedef_r  = round_price(clean_symbol, hedef)
        stop_r   = round_price(clean_symbol, stop)

        # Yon belirle
        if yon == "LONG":
            entry_side = "BUY"
            close_side = "SELL"
        else:
            entry_side = "SELL"
            close_side = "BUY"

        # Market emri ile gir
        log.info(f"Giriş: {clean_symbol} {yon} {qty_coin}")
        entry_result = place_market_order(clean_symbol, entry_side, qty_coin)
        log.info(f"Giriş emri: {entry_result}")

        if "orderId" not in entry_result:
            send_tg(f"HATA: {clean_symbol} giris emri basarisiz!\n{entry_result}")
            return False

        time.sleep(0.5)

        # Stop Loss (%100 pozisyon)
        sl_result = place_stop_order(clean_symbol, close_side, qty_coin, stop_r)
        log.info(f"Stop emri: {sl_result}")

        # TP1 (%50 pozisyon)
        qty_half = round_qty(clean_symbol, qty_coin / 2)
        tp1_result = place_limit_order(clean_symbol, close_side, qty_half, tp1_r, reduce_only=True)
        log.info(f"TP1 emri: {tp1_result}")

        # TP2 (%50 kalan)
        tp2_result = place_limit_order(clean_symbol, close_side, qty_half, hedef_r, reduce_only=True)
        log.info(f"TP2 emri: {tp2_result}")

        # Aktif islem kaydet
        aktif_islemler[clean_symbol] = {
            "symbol":   clean_symbol,
            "yon":      yon,
            "ep":       giris,
            "sl":       stop_r,
            "tp1":      tp1_r,
            "tp2":      hedef_r,
            "qty":      qty_coin,
            "tp1_hit":  False,
            "sl_order": sl_result.get("orderId"),
        }

        send_tg(
            f"ISLEM ACILDI\n"
            f"Parite: {clean_symbol}\n"
            f"Yon: {yon}\n"
            f"Giris: {giris}\n"
            f"Stop: {stop_r}\n"
            f"TP1: {tp1_r}\n"
            f"Hedef: {hedef_r}\n"
            f"Miktar: {qty_coin}\n"
            f"Risk: {miktar_usdt} USDT"
        )
        return True

    except Exception as e:
        log.error(f"islem_ac hatasi: {e}")
        send_tg(f"HATA: {symbol} islem acilamadi: {str(e)}")
        return False

# =========================================================================
# POZISYON TAKIP (arka planda calisir)
# =========================================================================
def pozisyon_takip():
    """Her 5 saniyede pozisyonları kontrol et"""
    while True:
        try:
            for symbol, islem in list(aktif_islemler.items()):
                if islem["tp1_hit"]:
                    continue

                pos = get_position(symbol)
                if pos is None:
                    continue

                pos_amt = float(pos.get("positionAmt", 0))

                # Pozisyon yarıya düştü = TP1 vuruldu
                if abs(pos_amt) < abs(islem["qty"]) * 0.6 and abs(pos_amt) > 0:
                    log.info(f"TP1 vuruldu: {symbol}")
                    islem["tp1_hit"] = True

                    # Eski stop'u iptal et
                    cancel_all_orders(symbol)
                    time.sleep(0.3)

                    # Yeni stop = BE (giris fiyati)
                    yon     = islem["yon"]
                    ep      = round_price(symbol, islem["ep"])
                    hedef   = islem["tp2"]
                    qty_rem = round_qty(symbol, abs(pos_amt))
                    close_side = "SELL" if yon == "LONG" else "BUY"

                    # BE stop
                    place_stop_order(symbol, close_side, qty_rem, ep)
                    # TP2 tekrar koy
                    place_limit_order(symbol, close_side, qty_rem, hedef, reduce_only=True)

                    send_tg(
                        f"TP1 ALINDI\n"
                        f"Parite: {symbol}\n"
                        f"Stop BE'ye cekildi: {ep}\n"
                        f"Hedef bekleniyor: {hedef}"
                    )

                # Pozisyon kapandı
                elif abs(pos_amt) == 0 and symbol in aktif_islemler:
                    log.info(f"Pozisyon kapandı: {symbol}")
                    if islem["tp1_hit"]:
                        send_tg(f"ISLEM KAPANDI\nParite: {symbol}\nSonuc: Full Win veya BE")
                    else:
                        send_tg(f"STOP\nParite: {symbol}\nSonuc: -1R")
                    del aktif_islemler[symbol]

        except Exception as e:
            log.error(f"Takip hatasi: {e}")

        time.sleep(5)

# =========================================================================
# WEBHOOK ENDPOINT
# =========================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    """TradingView'dan gelen webhook"""
    try:
        # Secret kontrol
        secret = request.headers.get("X-Webhook-Secret", "")
        if secret != WEBHOOK_SECRET:
            # Secret header yoksa body'den kontrol et
            pass

        data = request.get_json(force=True)
        log.info(f"Webhook alindi: {data}")

        if not data:
            # String olarak geldi mi?
            raw = request.data.decode("utf-8")
            try:
                data = json.loads(raw)
            except:
                log.error(f"Parse hatasi: {raw}")
                return jsonify({"status": "error", "msg": "parse failed"}), 400

        # Gerekli alanlar
        symbol = data.get("symbol", "").replace(".P", "")
        yon    = data.get("yon", "").upper()
        giris  = float(data.get("giris", 0))
        stop   = float(data.get("stop", 0))
        tp1    = float(data.get("tp1", 0))
        hedef  = float(data.get("hedef", 0))

        if not all([symbol, yon, giris, stop, tp1, hedef]):
            log.error(f"Eksik veri: {data}")
            return jsonify({"status": "error", "msg": "eksik veri"}), 400

        if yon not in ["LONG", "SHORT"]:
            return jsonify({"status": "error", "msg": "gecersiz yon"}), 400

        # Zaten acik islem var mi?
        if symbol in aktif_islemler:
            log.warning(f"Zaten acik islem var: {symbol}")
            return jsonify({"status": "skip", "msg": "zaten acik"}), 200

        # Islemi ac (ayri thread'de)
        threading.Thread(
            target=islem_ac,
            args=(symbol, yon, giris, stop, tp1, hedef, RISK_USDT),
            daemon=True
        ).start()

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log.error(f"Webhook hatasi: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "aktif_islemler": list(aktif_islemler.keys())
    }), 200

# =========================================================================
# ANA PROGRAM
# =========================================================================
if __name__ == "__main__":
    log.info("PHANTOM TRADER v1.0 baslatildi.")
    log.info(f"Risk per islem: {RISK_USDT} USDT")
    log.info(f"Leverage: {LEVERAGE}x")

    if not BINANCE_API_KEY:
        log.error("BINANCE_API_KEY eksik!")
        send_tg("HATA: BINANCE_API_KEY ayarlanmamis!")
    else:
        send_tg(
            f"PHANTOM TRADER v1.0 aktiv\n"
            f"Risk: {RISK_USDT} USDT\n"
            f"Leverage: {LEVERAGE}x\n"
            f"Webhook: /webhook"
        )

    # Pozisyon takip thread'i
    threading.Thread(target=pozisyon_takip, daemon=True).start()

    # Flask sunucusu
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

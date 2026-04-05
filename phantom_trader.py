"""
PHANTOM BOT v1.0
================
Birlesik sistem:
1. TV webhook alir
2. FVGPatron_bot uzerinden Telegram'a mesaj gonderir
3. Istatistik tutar, gece UTC 00:00 (Baku 04:00) rapor gonderir
4. Binance Futures'da otomatik islem acar/yonetir

TV Alarm Ayarlari:
- Kosul: PHANTOM_B13 -> alert() fonksiyonu cagrılari
- Mesaj: {{strategy.order.alert_message}}
- Bildirimler: Web kancasi -> https://phantom-bot.onrender.com/webhook
"""

from flask import Flask, request, jsonify
import requests
import hmac
import hashlib
import time
import json
import logging
import threading
import schedule
import os
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

app = Flask(__name__)

# =========================================================================
# AYARLAR
# =========================================================================
# FVGPatron_bot — mesaj gondermek icin
FVG_BOT_TOKEN = "8573057660:AAFrNwYM3A2IthWDfZFdKcD4T0O2zyIEcTI"
FVG_API       = f"https://api.telegram.org/bot{FVG_BOT_TOKEN}"
TG_CHAT_ID    = "811792517"

# Binance API (Environment variables)
BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_URL        = "https://fapi.binance.com"

# Risk ayarlari
RISK_USDT = float(os.environ.get("RISK_USDT", "10"))
LEVERAGE  = int(os.environ.get("LEVERAGE", "10"))

# Trade aktif mi?
TRADE_ACTIVE = os.environ.get("TRADE_ACTIVE", "false").lower() == "true"

# =========================================================================
# GUNLUK ISTATISTIK
# =========================================================================
class GunlukIstat:
    def __init__(self):
        self.reset()

    def reset(self):
        self.toplam   = 0
        self.full_win = 0
        self.be       = 0
        self.stop     = 0
        self.net_r    = 0.0

    def rapor_olustur(self):
        tarih  = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        kapanan = self.full_win + self.be + self.stop
        if kapanan == 0:
            return None
        ugur   = (self.full_win + self.be) * 100.0 / kapanan
        durum  = "QAZANCLI" if self.net_r >= 0 else "ZIYANLI"
        r_str  = ("+" if self.net_r >= 0 else "") + f"{self.net_r:.2f}"
        usd    = self.net_r * RISK_USDT
        usd_s  = ("+" if usd >= 0 else "") + f"{usd:.2f}"

        return (
            f"GUN SONU HESABATI\n"
            f"{tarih}\n\n"
            f"Veziyyet: {durum}\n\n"
            f"Umumi Emeliyyat: {kapanan}\n"
            f"Full Win:        {self.full_win}\n"
            f"Risksiz BE:      {self.be}\n"
            f"Stop:            {self.stop}\n\n"
            f"Ugur: %{ugur:.1f}\n"
            f"Net R: {r_str}R\n"
            f"Net USD: {usd_s}$"
        )

istat = GunlukIstat()
aktif_islemler = {}

# =========================================================================
# TELEGRAM
# =========================================================================
def send_tg(text):
    """FVGPatron_bot uzerinden mesaj gonder"""
    try:
        r = requests.post(f"{FVG_API}/sendMessage", json={
            "chat_id": TG_CHAT_ID,
            "text":    text
        }, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram hatasi: {r.text}")
    except Exception as e:
        log.error(f"Telegram baglanti hatasi: {e}")

# =========================================================================
# BINANCE API
# =========================================================================
def binance_sign(params):
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    sig = hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()
    return query + "&signature=" + sig

def binance_request(method, endpoint, params=None):
    if not BINANCE_API_KEY:
        return {"error": "API key yok"}
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    signed  = binance_sign(params)
    url     = f"{BINANCE_URL}{endpoint}?{signed}"
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
        return {"error": str(e)}

def get_symbol_info(symbol):
    try:
        r = requests.get(f"{BINANCE_URL}/fapi/v1/exchangeInfo", timeout=10)
        for s in r.json().get("symbols", []):
            if s["symbol"] == symbol:
                return s
    except:
        pass
    return None

def round_qty(symbol, qty):
    info = get_symbol_info(symbol)
    if info:
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                dec  = len(str(step).rstrip("0").split(".")[-1])
                return round(qty, dec)
    return round(qty, 3)

def round_price(symbol, price):
    info = get_symbol_info(symbol)
    if info:
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
                dec  = len(str(tick).rstrip("0").split(".")[-1])
                return round(price, dec)
    return round(price, 2)

def place_market_order(symbol, side, qty):
    return binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side,
        "type": "MARKET", "quantity": qty
    })

def place_limit_order(symbol, side, qty, price):
    return binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side,
        "type": "LIMIT", "timeInForce": "GTC",
        "quantity": qty, "price": price,
        "reduceOnly": "true"
    })

def place_stop_order(symbol, side, qty, stop_price):
    return binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side,
        "type": "STOP_MARKET",
        "quantity": qty, "stopPrice": stop_price,
        "reduceOnly": "true"
    })

def cancel_all_orders(symbol):
    return binance_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

def get_position(symbol):
    result = binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if isinstance(result, list):
        for p in result:
            if p["symbol"] == symbol:
                return p
    return None

def set_leverage(symbol, lev):
    return binance_request("POST", "/fapi/v1/leverage", {
        "symbol": symbol, "leverage": lev
    })

# =========================================================================
# ISLEM AC
# =========================================================================
def islem_ac(symbol, yon, giris, stop, tp1, hedef):
    try:
        clean = symbol.replace(".P", "")
        set_leverage(clean, LEVERAGE)
        time.sleep(0.1)

        sl_dist  = abs(giris - stop)
        qty      = round_qty(clean, RISK_USDT / sl_dist)
        tp1_r    = round_price(clean, tp1)
        hedef_r  = round_price(clean, hedef)
        stop_r   = round_price(clean, stop)
        qty_half = round_qty(clean, qty / 2)

        entry_side = "BUY"  if yon == "LONG" else "SELL"
        close_side = "SELL" if yon == "LONG" else "BUY"

        # Market emri
        entry = place_market_order(clean, entry_side, qty)
        if "orderId" not in entry:
            send_tg(f"HATA: {clean} giris emri basarisiz!\n{entry}")
            return
        time.sleep(0.5)

        # Stop Loss
        place_stop_order(clean, close_side, qty, stop_r)
        # TP1 %50
        place_limit_order(clean, close_side, qty_half, tp1_r)
        # TP2 %50
        place_limit_order(clean, close_side, qty_half, hedef_r)

        aktif_islemler[clean] = {
            "yon": yon, "ep": giris, "sl": stop_r,
            "tp1": tp1_r, "tp2": hedef_r,
            "qty": qty, "tp1_hit": False
        }
        log.info(f"Islem acildi: {clean} {yon} {qty}")

    except Exception as e:
        log.error(f"islem_ac hatasi: {e}")
        send_tg(f"HATA: {symbol} islem acilamadi: {str(e)}")

# =========================================================================
# POZISYON TAKIP
# =========================================================================
def pozisyon_takip():
    while True:
        try:
            for symbol, ism in list(aktif_islemler.items()):
                if ism["tp1_hit"]:
                    continue
                pos = get_position(symbol)
                if not pos:
                    continue
                pos_amt = float(pos.get("positionAmt", 0))

                # TP1 vuruldu (miktar yarıya düştü)
                if 0 < abs(pos_amt) < ism["qty"] * 0.6:
                    ism["tp1_hit"] = True
                    cancel_all_orders(symbol)
                    time.sleep(0.3)
                    ep  = round_price(symbol, ism["ep"])
                    rem = round_qty(symbol, abs(pos_amt))
                    cs  = "SELL" if ism["yon"] == "LONG" else "BUY"
                    place_stop_order(symbol, cs, rem, ep)
                    place_limit_order(symbol, cs, rem, ism["tp2"])
                    log.info(f"TP1 vuruldu: {symbol}, stop BE'ye cekildi")

                # Pozisyon kapandı
                elif abs(pos_amt) == 0:
                    del aktif_islemler[symbol]
                    log.info(f"Pozisyon kapandı: {symbol}")

        except Exception as e:
            log.error(f"Takip hatasi: {e}")
        time.sleep(5)

# =========================================================================
# WEBHOOK — TV'den gelen JSON mesaji isle
# =========================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.data.decode("utf-8").strip()
        log.info(f"Webhook alindi: {raw[:200]}")

        # JSON parse et
        try:
            data = json.loads(raw)
        except:
            log.error(f"JSON parse hatasi: {raw}")
            return jsonify({"status": "error"}), 400

        # chat_id ve text alanlari var mi?
        chat_id = data.get("chat_id", TG_CHAT_ID)
        text    = data.get("text", "")

        if not text:
            return jsonify({"status": "empty"}), 200

        # Telegram'a mesaj gonder
        send_tg(text)

        # Istatistik guncelle
        text_upper = text.upper()
        if "EMELLIYYATA GIR" in text_upper or "ISLEME GIR" in text_upper:
            istat.toplam += 1
            log.info("Istatistik: Giris kaydedildi")

            # Trade aktifse Binance'de islem ac
            if TRADE_ACTIVE and BINANCE_API_KEY:
                parite = _parse_field(text, "Cut:", "Parite:")
                yon    = _parse_field(text, "Yon:")
                giris  = _parse_float(text, "Giris:")
                stop   = _parse_float(text, "Stop:")
                tp1    = _parse_float(text, "TP1:")
                hedef  = _parse_float(text, "Hedef:")
                if all([parite, yon, giris, stop, tp1, hedef]):
                    threading.Thread(
                        target=islem_ac,
                        args=(parite, yon, giris, stop, tp1, hedef),
                        daemon=True
                    ).start()

        elif "FULL WIN" in text_upper:
            istat.full_win += 1
            rr = _parse_rr(text)
            istat.net_r += rr
            log.info(f"Istatistik: Full Win +{rr}R")

        elif "RISKSIZ" in text_upper:
            istat.be += 1
            istat.net_r += 1.0
            log.info("Istatistik: Risksiz BE +1R")

        elif "STOP" in text_upper and "STOP VURULDU" in text_upper:
            istat.stop += 1
            istat.net_r -= 1.0
            log.info("Istatistik: Stop -1R")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log.error(f"Webhook hatasi: {e}")
        return jsonify({"status": "error"}), 500

def _parse_field(text, *keys):
    for line in text.split("\n"):
        for key in keys:
            if key in line:
                val = line.split(key, 1)[-1].strip()
                return val.split()[0] if val else None
    return None

def _parse_float(text, key):
    val = _parse_field(text, key)
    if val:
        try:
            return float(val.replace(",", "."))
        except:
            pass
    return None

def _parse_rr(text):
    import re
    match = re.search(r'[+]?(\d+\.?\d*)R', text)
    if match:
        try:
            return float(match.group(1))
        except:
            pass
    return 0.0

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":   "ok",
        "istat":    {"toplam": istat.toplam, "net_r": istat.net_r},
        "aktif":    list(aktif_islemler.keys()),
        "trade":    TRADE_ACTIVE
    }), 200

# =========================================================================
# GUNLUK RAPOR — Her gece UTC 00:00 (Baku 04:00)
# =========================================================================
def gunluk_rapor():
    rapor = istat.rapor_olustur()
    if rapor:
        send_tg(rapor)
        log.info("Gunluk rapor gonderildi")
    else:
        log.info("Bugun hic islem yok, rapor gonderilmedi")
    istat.reset()

def zamanlayici():
    schedule.every().day.at("00:00").do(gunluk_rapor)
    while True:
        schedule.run_pending()
        time.sleep(30)

# =========================================================================
# ANA PROGRAM
# =========================================================================
if __name__ == "__main__":
    log.info("PHANTOM BOT v1.0 baslatildi.")
    log.info(f"Trade aktif: {TRADE_ACTIVE}")
    log.info(f"Risk: {RISK_USDT} USDT | Leverage: {LEVERAGE}x")

    send_tg(
        f"PHANTOM BOT v1.0 aktiv\n"
        f"Webhook hazir\n"
        f"Gunluk rapor: UTC 00:00 (Baku 04:00)\n"
        f"Trade: {'AKTIV' if TRADE_ACTIVE else 'PASIV'}"
    )

    # Pozisyon takip thread
    if TRADE_ACTIVE:
        threading.Thread(target=pozisyon_takip, daemon=True).start()

    # Zamanlayici thread
    threading.Thread(target=zamanlayici, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

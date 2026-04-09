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
import re
import unicodedata
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger()
app = Flask(__name__)

FVG_BOT_TOKEN = "8573057660:AAFrNwYM3A2IthWDfZFdKcD4T0O2zyIEcTI"
FVG_API       = f"https://api.telegram.org/bot{FVG_BOT_TOKEN}"
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "811792517")

BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_URL        = "https://fapi.binance.com"

RISK_USDT    = float(os.environ.get("RISK_USDT", "10"))
LEVERAGE     = int(os.environ.get("LEVERAGE", "10"))
TRADE_ACTIVE = os.environ.get("TRADE_ACTIVE", "false").lower() == "true"

# =========================================================================
# YARDIMCI
# =========================================================================
def strip_emojis(text):
    cleaned = ""
    for ch in text:
        cat = unicodedata.category(ch)
        if cat not in ("So", "Mn") and ord(ch) < 0x10000:
            cleaned += ch
        elif ch in (" ", "\t"):
            cleaned += ch
    return cleaned.strip()

def normalize_text(text):
    return text.replace("\\n", "\n")

def _parse_field(text, *keys):
    text = normalize_text(text)
    for line in text.split("\n"):
        line_clean = strip_emojis(line).strip()
        for key in keys:
            if key.strip() in line_clean:
                val = line_clean.split(key.strip(), 1)[-1].strip()
                val = val.split()[0] if val else None
                if val:
                    return val
    return None

def _parse_float(text, *keys):
    val = _parse_field(text, *keys)
    if val:
        try:
            return float(val.replace(",", "."))
        except:
            pass
    return None

def _parse_yon(text):
    text = normalize_text(text)
    for line in text.split("\n"):
        up = line.upper()
        if "EMELLIYYATA GIR" in up or "ISLEME GIR" in up:
            if "LONG" in up:
                return "LONG"
            elif "SHORT" in up:
                return "SHORT"
    return None

def _parse_rr(text):
    match = re.search(r'[+]?(\d+\.?\d*)R', text)
    if match:
        try:
            return float(match.group(1))
        except:
            pass
    return 0.0

# =========================================================================
# ISTATISTIK
# =========================================================================
class GunlukIstat:
    def __init__(self):
        self.reset()

    def reset(self):
        self.toplam = 0
        self.full_win = 0
        self.be = 0
        self.stop = 0
        self.net_r = 0.0

    def rapor_olustur(self):
        tarih = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        kapanan = self.full_win + self.be + self.stop
        if kapanan == 0:
            return None
        ugur = (self.full_win + self.be) * 100.0 / kapanan
        durum = "QAZANCLI" if self.net_r >= 0 else "ZIYANLI"
        r_str = ("+" if self.net_r >= 0 else "") + f"{self.net_r:.2f}"
        usd = self.net_r * RISK_USDT
        usd_s = ("+" if usd >= 0 else "") + f"{usd:.2f}"
        return (
            f"GUN SONU HESABATI\n{tarih}\n\n"
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
def send_tg(text, chat_id=None):
    cid = chat_id or TG_CHAT_ID
    try:
        r = requests.post(f"{FVG_API}/sendMessage", json={"chat_id": cid, "text": text}, timeout=10)
        if r.status_code != 200:
            log.error(f"TG hatasi: {r.text}")
    except Exception as e:
        log.error(f"TG baglanti hatasi: {e}")

# =========================================================================
# BINANCE API
# =========================================================================
def binance_sign(params):
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    sig = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig

def binance_request(method, endpoint, params=None):
    if not BINANCE_API_KEY:
        return {"error": "API key yok"}
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    signed = binance_sign(params)
    url = f"{BINANCE_URL}{endpoint}?{signed}"
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
                dec = len(str(step).rstrip("0").split(".")[-1])
                return round(qty, dec)
    return round(qty, 3)

def round_price(symbol, price):
    info = get_symbol_info(symbol)
    if info:
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
                dec = len(str(tick).rstrip("0").split(".")[-1])
                return round(price, dec)
    return round(price, 2)

def get_min_qty(symbol):
    info = get_symbol_info(symbol)
    if info:
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                return float(f["minQty"])
    return 0.0

def get_mark_price(symbol):
    try:
        r = requests.get(f"{BINANCE_URL}/fapi/v1/premiumIndex?symbol={symbol}", timeout=5)
        return float(r.json().get("markPrice", 0))
    except:
        return 0.0

def place_market_order(symbol, side, qty):
    return binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty
    })

def place_tp_order(symbol, side, qty, price):
    return binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": price,
        "closePosition": "false",
        "quantity": qty,
        "reduceOnly": "true",
        "timeInForce": "GTC",
        "workingType": "MARK_PRICE"
    })

def close_position_market(symbol, side, qty):
    return binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side,
        "type": "MARKET", "quantity": qty,
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
    return binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})

# =========================================================================
# ISLEM AC
# =========================================================================
def islem_ac(symbol, yon, giris, stop, tp1, hedef):
    try:
        clean = symbol.replace(".P", "").upper()
        log.info(f"Islem aciliyor: {clean} {yon} giris={giris} stop={stop} tp1={tp1} hedef={hedef}")

        set_leverage(clean, LEVERAGE)
        time.sleep(0.2)

        sl_dist = abs(giris - stop)
        if sl_dist == 0:
            send_tg(f"HATA: {clean} SL mesafesi sifir!")
            return

        qty_raw = RISK_USDT / sl_dist
        qty = round_qty(clean, qty_raw)
        min_qty = get_min_qty(clean)

        if qty < min_qty:
            send_tg(f"HATA: {clean} miktar cok kucuk! qty={qty} min={min_qty}")
            return

        qty_half = round_qty(clean, qty / 2)
        tp1_r = round_price(clean, tp1)
        hedef_r = round_price(clean, hedef)
        stop_r = round_price(clean, stop)

        entry_side = "BUY" if yon == "LONG" else "SELL"
        close_side = "SELL" if yon == "LONG" else "BUY"

        # 1. Market giris
        entry = place_market_order(clean, entry_side, qty)
        if "orderId" not in entry:
            send_tg(f"HATA: {clean} giris basarisiz!\n{json.dumps(entry)}")
            return
        time.sleep(0.5)

        # 2. TP1 - TAKE_PROFIT_MARKET trigger
        tp1_result = place_tp_order(clean, close_side, qty_half, tp1_r)
        log.info(f"TP1 emri: {tp1_result}")
        time.sleep(0.3)

        # 3. TP2 - TAKE_PROFIT_MARKET trigger
        tp2_result = place_tp_order(clean, close_side, qty_half, hedef_r)
        log.info(f"TP2 emri: {tp2_result}")

        # 4. SL - bot tarafindan izlenir, stop seviyesine gelince market ile kapatilir
        aktif_islemler[clean] = {
            "yon": yon,
            "ep": giris,
            "sl": stop_r,
            "tp1": tp1_r,
            "tp2": hedef_r,
            "qty": qty,
            "tp1_hit": False,
            "sl_active": True
        }

        log.info(f"Islem acildi: {clean} {yon} qty={qty} SL={stop_r}")
        send_tg(
            f"BINANCE: {clean} {yon} acildi\n"
            f"Qty: {qty} | Risk: {RISK_USDT}$\n"
            f"SL: {stop_r} (bot izliyor)\n"
            f"TP1: {tp1_r} | TP2: {hedef_r}"
        )

    except Exception as e:
        log.error(f"islem_ac hatasi: {e}")
        send_tg(f"HATA: {symbol} islem acilamadi: {str(e)}")

# =========================================================================
# POZISYON TAKIP - SL + TP1 sonrasi BE
# =========================================================================
def pozisyon_takip():
    while True:
        try:
            for symbol, ism in list(aktif_islemler.items()):
                pos = get_position(symbol)
                if not pos:
                    continue

                pos_amt = float(pos.get("positionAmt", 0))

                # Pozisyon kapandi (elle veya TP ile)
                if abs(pos_amt) == 0:
                    del aktif_islemler[symbol]
                    log.info(f"Pozisyon kapandi: {symbol}")
                    continue

                mark_price = get_mark_price(symbol)
                if mark_price == 0:
                    continue

                yon = ism["yon"]
                sl = ism["sl"]
                ep = ism["ep"]

                # TP1 kontrol - miktar yariya dustuyse
                if not ism["tp1_hit"] and 0 < abs(pos_amt) < ism["qty"] * 0.6:
                    ism["tp1_hit"] = True
                    # SL'yi girise cek
                    ism["sl"] = round_price(symbol, ep)
                    log.info(f"TP1 vuruldu: {symbol}, SL girisce cekildi: {ism['sl']}")
                    send_tg(f"TP1 ALINDI: {symbol}\nSL girisce cekildi: {ism['sl']}")

                # SL kontrol - fiyat stop seviyesine geldi mi?
                if ism["sl_active"]:
                    sl_tetiklendi = False
                    if yon == "LONG" and mark_price <= ism["sl"]:
                        sl_tetiklendi = True
                    elif yon == "SHORT" and mark_price >= ism["sl"]:
                        sl_tetiklendi = True

                    if sl_tetiklendi:
                        ism["sl_active"] = False
                        rem = round_qty(symbol, abs(pos_amt))
                        close_side = "SELL" if yon == "LONG" else "BUY"
                        # Once TP emirlerini iptal et
                        cancel_all_orders(symbol)
                        time.sleep(0.3)
                        # Market ile kapat
                        result = close_position_market(symbol, close_side, rem)
                        log.info(f"SL tetiklendi: {symbol} @ {mark_price} -> {result}")
                        send_tg(f"SL TETIKLENDI: {symbol}\nFiyat: {mark_price} | SL: {ism['sl']}\nPozisyon kapatildi!")
                        del aktif_islemler[symbol]

        except Exception as e:
            log.error(f"Takip hatasi: {e}")
        time.sleep(3)

# =========================================================================
# BASLANGICTA POZISYONLARI YUKLE
# =========================================================================
def pozisyonlari_yukle():
    try:
        result = binance_request("GET", "/fapi/v2/positionRisk", {})
        if isinstance(result, list):
            yuklenen = 0
            for p in result:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    symbol = p["symbol"]
                    ep = float(p["entryPrice"])
                    yon = "LONG" if amt > 0 else "SHORT"
                    # Acik pozisyonlar icin SL bilinmiyor, tp1_hit=True yapiyoruz
                    aktif_islemler[symbol] = {
                        "yon": yon, "ep": ep, "sl": 0,
                        "tp1": 0, "tp2": 0,
                        "qty": abs(amt), "tp1_hit": True,
                        "sl_active": False
                    }
                    yuklenen += 1
                    log.info(f"Pozisyon yuklendi: {symbol} {yon} qty={abs(amt)}")
            if yuklenen > 0:
                send_tg(f"Acik pozisyonlar yuklendi: {yuklenen} adet\nNot: Restart sonrasi SL manuel kontrol edin!")
    except Exception as e:
        log.error(f"Pozisyon yukleme hatasi: {e}")

# =========================================================================
# WEBHOOK
# =========================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.data.decode("utf-8").strip()
        log.info(f"Webhook alindi: {raw[:300]}")

        try:
            data = json.loads(raw)
        except:
            log.error(f"JSON parse hatasi: {raw}")
            return jsonify({"status": "error"}), 400

        chat_id = data.get("chat_id", TG_CHAT_ID)
        text = data.get("text", "")

        if not text:
            return jsonify({"status": "empty"}), 200

        send_tg(text, chat_id)
        text_upper = text.upper()

        if "EMELLIYYATA GIR" in text_upper or "ISLEME GIR" in text_upper:
            istat.toplam += 1
            log.info("Istatistik: Giris kaydedildi")

            if TRADE_ACTIVE and BINANCE_API_KEY:
                parite = _parse_field(text, "Cut:", "Parite:")
                yon = _parse_yon(text)
                giris = _parse_float(text, "Giris:")
                stop = _parse_float(text, "Stop:")
                tp1 = _parse_float(text, "TP1:")
                hedef = _parse_float(text, "Hedef:")

                log.info(f"Parse: parite={parite} yon={yon} giris={giris} stop={stop} tp1={tp1} hedef={hedef}")

                if all([parite, yon, giris, stop, tp1, hedef]):
                    threading.Thread(
                        target=islem_ac,
                        args=(parite, yon, giris, stop, tp1, hedef),
                        daemon=True
                    ).start()
                else:
                    log.warning("Parse eksik!")
                    send_tg(f"UYARI: Parse eksik!\nParite:{parite} Yon:{yon} Giris:{giris} Stop:{stop} TP1:{tp1} Hedef:{hedef}")

        elif "FULL WIN" in text_upper:
            istat.full_win += 1
            rr = _parse_rr(text)
            istat.net_r += rr
            log.info(f"Istatistik: Full Win +{rr}R")

        elif "RISKSIZ" in text_upper:
            istat.be += 1
            istat.net_r += 1.0
            log.info("Istatistik: Risksiz BE +1R")

        elif "STOP VURULDU" in text_upper:
            istat.stop += 1
            istat.net_r -= 1.0
            log.info("Istatistik: Stop -1R")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log.error(f"Webhook hatasi: {e}")
        return jsonify({"status": "error"}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "istat": {"toplam": istat.toplam, "net_r": istat.net_r},
        "aktif": list(aktif_islemler.keys()),
        "trade": TRADE_ACTIVE
    }), 200

# =========================================================================
# GUNLUK RAPOR
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
    log.info("PHANTOM BOT v1.5 baslatildi.")
    log.info(f"Trade aktif: {TRADE_ACTIVE}")
    log.info(f"Risk: {RISK_USDT} USDT | Leverage: {LEVERAGE}x")
    send_tg(
        f"PHANTOM BOT v1.5 aktiv\n"
        f"Webhook hazir\n"
        f"SL: Bot izleme modu\n"
        f"TP: TAKE_PROFIT_MARKET trigger\n"
        f"Trade: {'AKTIV' if TRADE_ACTIVE else 'PASIV'}"
    )
    if TRADE_ACTIVE:
        pozisyonlari_yukle()
        threading.Thread(target=pozisyon_takip, daemon=True).start()
    threading.Thread(target=zamanlayici, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

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

KOMISYON_ORAN = 0.0004
LIMIT_TIMEOUT = 45

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

def get_order_status(symbol, order_id):
    return binance_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

def cancel_order(symbol, order_id):
    return binance_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

def cancel_all_orders(symbol):
    r1 = binance_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    log.info(f"Normal orderlar iptal: {r1}")
    r2 = binance_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})
    log.info(f"Algo orderlar iptal: {r2}")

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
# ALGO ORDER — TP veya SL
# Cozum: algotype=CONDITIONAL + timeInForce=GTC
# =========================================================================
def place_algo_order(symbol, side, qty, price, order_type):
    """
    order_type: 'TAKE_PROFIT_MARKET' veya 'STOP_MARKET'
    algotype=CONDITIONAL zorunlu (TAKE_PROFIT_MARKET/STOP_MARKET gecersiz)
    timeInForce=GTC (GTE_GTC gecersiz)
    """
    result = binance_request("POST", "/fapi/v1/algoOrder", {
        "symbol":       symbol,
        "side":         side,
        "type":         order_type,
        "algotype":     "CONDITIONAL",   # ✅ tek gecerli deger
        "triggerPrice": price,
        "quantity":     qty,
        "reduceOnly":   "true",
        "timeInForce":  "GTC",           # ✅ GTE_GTC degil GTC
        "workingType":  "MARK_PRICE"
    })
    log.info(f"Algo order ({order_type}) @ {price}: {result}")
    return result

def close_position_market(symbol, side, qty):
    return binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side,
        "type": "MARKET", "quantity": qty,
        "reduceOnly": "true"
    })

# =========================================================================
# LIMIT GIRIS — 45 saniye bekle, dolmazsa iptal
# =========================================================================
def place_limit_entry(symbol, side, qty, price):
    result = binance_request("POST", "/fapi/v1/order", {
        "symbol":      symbol,
        "side":        side,
        "type":        "LIMIT",
        "price":       price,
        "quantity":    qty,
        "timeInForce": "GTC"
    })

    if "orderId" not in result:
        log.error(f"Limit emir acilamadi: {result}")
        return False, 0.0

    order_id = result["orderId"]
    log.info(f"Limit emir acildi: {order_id} @ {price}")

    for _ in range(LIMIT_TIMEOUT // 3):
        time.sleep(3)
        status = get_order_status(symbol, order_id)
        durum = status.get("status", "")
        log.info(f"Limit emir durumu: {durum}")

        if durum == "FILLED":
            gercek_fiyat = float(status.get("avgPrice", price))
            return True, gercek_fiyat

        if durum in ("CANCELED", "EXPIRED", "REJECTED"):
            return False, 0.0

    cancel_order(symbol, order_id)
    log.warning(f"Limit emir {LIMIT_TIMEOUT}sn dolmadi, iptal.")
    return False, 0.0

# =========================================================================
# ISLEM AC
# =========================================================================
def islem_ac(symbol, yon, giris, stop, tp1, hedef, risk_usdt, pine_qty):
    try:
        clean = symbol.replace(".P", "").upper()
        log.info(f"Islem: {clean} {yon} giris={giris} sl={stop} tp1={tp1} tp2={hedef} risk={risk_usdt} pine_qty={pine_qty}")

        set_leverage(clean, LEVERAGE)
        time.sleep(0.2)

        sl_dist  = abs(giris - stop)
        tp1_dist = abs(tp1 - giris)

        if sl_dist == 0:
            send_tg(f"HATA: {clean} SL mesafesi sifir!")
            return
        if tp1_dist == 0:
            send_tg(f"HATA: {clean} TP1 mesafesi sifir!")
            return

        min_qty = get_min_qty(clean)

        # Pine'dan miktar geldiyse onu kullan, yoksa komisyon dahil hesapla
        if pine_qty and pine_qty > 0:
            qty = round_qty(clean, pine_qty)
            log.info(f"Pine miktari: {qty}")
        else:
            komisyon_per_unit = giris * 2 * KOMISYON_ORAN
            qty = round_qty(clean, risk_usdt / (sl_dist + komisyon_per_unit))
            log.info(f"Bot miktari: {qty}")

        if qty < min_qty:
            send_tg(f"HATA: {clean} miktar cok kucuk! qty={qty} min={min_qty}")
            return

        # TP1 miktari: tam 0.5R kazanmak icin
        tp1_qty = round_qty(clean, min((risk_usdt * 0.5) / tp1_dist, qty))
        tp2_qty = round_qty(clean, qty - tp1_qty)

        giris_r = round_price(clean, giris)
        tp1_r   = round_price(clean, tp1)
        hedef_r = round_price(clean, hedef)
        stop_r  = round_price(clean, stop)

        entry_side = "BUY"  if yon == "LONG" else "SELL"
        close_side = "SELL" if yon == "LONG" else "BUY"

        send_tg(
            f"LIMIT EMIR GONDERILDI\n"
            f"{clean} {yon} @ {giris_r}\n"
            f"Qty: {qty} | Risk: {risk_usdt}$\n"
            f"45 saniye bekleniyor..."
        )

        # 1. LIMIT GIRIS
        doldu, gercek_giris = place_limit_entry(clean, entry_side, qty, giris_r)
        if not doldu:
            send_tg(f"EMIR DOLMADI: {clean}\nFiyat {giris_r} seviyesine gelmedi.")
            return

        time.sleep(0.5)

        # 2. TP1 algo order (0.5R)
        tp1_result = place_algo_order(clean, close_side, tp1_qty, tp1_r, "TAKE_PROFIT_MARKET")
        time.sleep(0.3)

        # 3. TP2 algo order (kalan)
        if tp2_qty >= min_qty:
            place_algo_order(clean, close_side, tp2_qty, hedef_r, "TAKE_PROFIT_MARKET")
        time.sleep(0.3)

        # 4. SL algo order (Binance'e direkt)
        place_algo_order(clean, close_side, qty, stop_r, "STOP_MARKET")

        # 5. Bot izlemesi — sadece TP1 sonrasi BE icin
        aktif_islemler[clean] = {
            "yon":       yon,
            "ep":        gercek_giris,
            "sl":        stop_r,
            "tp1":       tp1_r,
            "tp2":       hedef_r,
            "qty":       qty,
            "risk_usdt": risk_usdt,
            "tp1_hit":   False,
        }

        send_tg(
            f"ISLEM ACILDI\n"
            f"{clean} {yon}\n"
            f"Giris: {gercek_giris} | Qty: {qty}\n"
            f"1R = {risk_usdt}$\n"
            f"TP1: {tp1_r} | {tp1_qty} adet (0.5R)\n"
            f"TP2: {hedef_r} | {tp2_qty} adet\n"
            f"SL: {stop_r} (Binance algo)"
        )

    except Exception as e:
        log.error(f"islem_ac hatasi: {e}")
        send_tg(f"HATA: {symbol} islem acilamadi: {str(e)}")

# =========================================================================
# POZISYON TAKIP — sadece TP1 sonrasi SL'yi BE'ye cek
# =========================================================================
def pozisyon_takip():
    while True:
        try:
            for symbol, ism in list(aktif_islemler.items()):
                pos = get_position(symbol)
                if not pos:
                    continue

                pos_amt = float(pos.get("positionAmt", 0))

                if abs(pos_amt) == 0:
                    del aktif_islemler[symbol]
                    log.info(f"Pozisyon kapandi: {symbol}")
                    continue

                # TP1 vuruldu mu? Miktar yaridan azsa
                if not ism["tp1_hit"] and 0 < abs(pos_amt) < ism["qty"] * 0.6:
                    ism["tp1_hit"] = True
                    ep = ism["ep"]
                    be_sl = round_price(symbol, ep)
                    yon = ism["yon"]
                    close_side = "SELL" if yon == "LONG" else "BUY"
                    rem = round_qty(symbol, abs(pos_amt))

                    log.info(f"TP1 vuruldu: {symbol}, BE SL konuyor: {be_sl}")
                    send_tg(f"TP1 ALINDI: {symbol}\nSL BE'ye cekilecek: {be_sl}")

                    # Mevcut SL'yi iptal et, yeni BE SL koy
                    cancel_all_orders(symbol)
                    time.sleep(0.3)
                    sl_result = place_algo_order(symbol, close_side, rem, be_sl, "STOP_MARKET")
                    log.info(f"BE SL kondu: {sl_result}")
                    send_tg(f"BE SL KONDU: {symbol} @ {be_sl}")
                    ism["sl"] = be_sl

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
                    aktif_islemler[symbol] = {
                        "yon": yon, "ep": ep, "sl": 0,
                        "tp1": 0, "tp2": 0,
                        "qty": abs(amt), "risk_usdt": RISK_USDT,
                        "tp1_hit": True
                    }
                    yuklenen += 1
                    log.info(f"Pozisyon yuklendi: {symbol} {yon} qty={abs(amt)}")
            if yuklenen > 0:
                send_tg(f"Acik pozisyonlar yuklendi: {yuklenen} adet\nNot: Restart sonrasi SL manuel kontrol!")
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

            if TRADE_ACTIVE and BINANCE_API_KEY:
                parite = _parse_field(text, "Cut:", "Parite:")
                yon    = _parse_yon(text)
                giris  = _parse_float(text, "Giris:")
                stop   = _parse_float(text, "Stop:")
                tp1    = _parse_float(text, "TP1:")
                hedef  = _parse_float(text, "Hedef:")
                risk   = _parse_float(text, "Risk:") or RISK_USDT
                miqdar = _parse_float(text, "Miqdar:")

                log.info(f"Parse: {parite} {yon} g={giris} sl={stop} tp1={tp1} h={hedef} r={risk} qty={miqdar}")

                if all([parite, yon, giris, stop, tp1, hedef]):
                    threading.Thread(
                        target=islem_ac,
                        args=(parite, yon, giris, stop, tp1, hedef, risk, miqdar),
                        daemon=True
                    ).start()
                else:
                    send_tg(
                        f"UYARI: Parse eksik!\n"
                        f"Parite:{parite} Yon:{yon} Giris:{giris} "
                        f"Stop:{stop} TP1:{tp1} Hedef:{hedef} Risk:{risk}"
                    )

        elif "FULL WIN" in text_upper:
            istat.full_win += 1
            istat.net_r += _parse_rr(text)

        elif "RISKSIZ" in text_upper:
            istat.be += 1
            istat.net_r += 1.0

        elif "STOP VURULDU" in text_upper:
            istat.stop += 1
            istat.net_r -= 1.0

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
    log.info("PHANTOM BOT v1.8 baslatildi.")
    log.info(f"Trade aktif: {TRADE_ACTIVE} | Risk: {RISK_USDT}$ | Leverage: {LEVERAGE}x")
    send_tg(
        f"PHANTOM BOT v1.8 aktiv\n"
        f"Giris: LIMIT (45sn timeout)\n"
        f"TP1: 0.5R | TP2: kalan\n"
        f"SL: Algo (CONDITIONAL+GTC)\n"
        f"BE: TP1 sonrasi otomatik\n"
        f"Trade: {'AKTIV' if TRADE_ACTIVE else 'PASIV'}"
    )
    if TRADE_ACTIVE:
        pozisyonlari_yukle()
        threading.Thread(target=pozisyon_takip, daemon=True).start()
    threading.Thread(target=zamanlayici, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

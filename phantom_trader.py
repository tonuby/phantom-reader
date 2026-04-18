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
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger()
app = Flask(__name__)

FVG_BOT_TOKEN = "8573057660:AAFrNwYM3A2IthWDfZFdKcD4T0O2zyIEcTI"
FVG_API       = f"https://api.telegram.org/bot{FVG_BOT_TOKEN}"
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "811792517")

BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_URL        = "https://fapi.binance.com"

RISK_USDT      = float(os.environ.get("RISK_USDT", "10"))
LEVERAGE       = int(os.environ.get("LEVERAGE", "10"))
TRADE_ACTIVE   = os.environ.get("TRADE_ACTIVE", "false").lower() == "true"

KOMISYON_ORAN  = 0.0004
MAX_LOSS_TRADE = 15.0
MAX_LOSS_DAILY = 100.0
TRAILING_ATR   = 1.5
BAKU_TZ        = timezone(timedelta(hours=4))

# Cikis yapisi:
#   TP  @ Hedef → %90 kapat (limit order, slippage sifir)
#   Trailing    → %10 (TP sonrasi baslar)
TP_PCT       = 0.90
TRAILING_PCT = 0.10

# =========================================================================
# GUNLUK ZARAR TAKIBI
# =========================================================================
class GunlukZarar:
    def __init__(self):
        self.gun_zarari = 0.0
        self.durduruldu = False

    def ekle(self, zarar):
        self.gun_zarari += abs(zarar)
        if self.gun_zarari >= MAX_LOSS_DAILY and not self.durduruldu:
            self.durduruldu = True
            send_tg(
                "GUNLUK LIMIT ASILDI!\n"
                "Zarar: -" + str(round(self.gun_zarari, 2)) + "$\n"
                "Limit: " + str(MAX_LOSS_DAILY) + "$\n"
                "Baku 00:00'da sifirlanir."
            )

    def sifirla(self):
        self.gun_zarari = 0.0
        self.durduruldu = False
        send_tg("Yeni gun (Baku)\nSayac sifirlandi. Bot hazir!")

    def acilabilir_mi(self):
        return not self.durduruldu

gun_zarar = GunlukZarar()

# =========================================================================
# ISTATISTIK
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
        tarih   = datetime.now(BAKU_TZ).strftime("%d/%m/%Y")
        kapanan = self.full_win + self.be + self.stop
        if kapanan == 0:
            return None
        ugur  = (self.full_win + self.be) * 100.0 / kapanan
        durum = "QAZANCLI" if self.net_r >= 0 else "ZIYANLI"
        r_str = ("+" if self.net_r >= 0 else "") + str(round(self.net_r, 2)) + "R"
        usd   = self.net_r * RISK_USDT
        usd_s = ("+" if usd >= 0 else "") + str(round(usd, 2)) + "$"
        return (
            "GUN SONU HESABATI\n" + tarih + "\n\n"
            "Veziyyet: " + durum + "\n\n"
            "Umumi: " + str(kapanan) + "\n"
            "Full Win: " + str(self.full_win) + "\n"
            "BE:       " + str(self.be) + "\n"
            "Stop:     " + str(self.stop) + "\n\n"
            "Ugur: %" + str(round(ugur, 1)) + "\n"
            "Net R: " + r_str + "\n"
            "Net USD: " + usd_s + "\n"
            "Gunluk Zarar: -" + str(round(gun_zarar.gun_zarari, 2)) + "$"
        )

istat          = GunlukIstat()
aktif_islemler = {}

# =========================================================================
# TELEGRAM
# =========================================================================
def send_tg(text, chat_id=None):
    cid = chat_id or TG_CHAT_ID
    try:
        r = requests.post(
            FVG_API + "/sendMessage",
            json={"chat_id": cid, "text": text},
            timeout=10
        )
        if r.status_code != 200:
            log.error("TG hatasi: " + r.text)
    except Exception as e:
        log.error("TG hatasi: " + str(e))

# =========================================================================
# BINANCE API
# =========================================================================
def binance_sign(params):
    query = "&".join([k + "=" + str(v) for k, v in params.items()])
    sig   = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig

def binance_request(method, endpoint, params=None):
    if not BINANCE_API_KEY:
        return {"error": "API key yok"}
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    signed  = binance_sign(params)
    url     = BINANCE_URL + endpoint + "?" + signed
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
        r = requests.get(BINANCE_URL + "/fapi/v1/exchangeInfo", timeout=10)
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
    return round(price, 5)

def get_min_qty(symbol):
    info = get_symbol_info(symbol)
    if info:
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                return float(f["minQty"])
    return 0.0

def get_mark_price(symbol):
    try:
        r = requests.get(BINANCE_URL + "/fapi/v1/premiumIndex?symbol=" + symbol, timeout=5)
        return float(r.json().get("markPrice", 0))
    except:
        return 0.0

def get_atr(symbol):
    try:
        r      = requests.get(BINANCE_URL + "/fapi/v1/klines?symbol=" + symbol + "&interval=5m&limit=15", timeout=5)
        klines = r.json()
        if len(klines) < 2:
            return 0.0
        trs = []
        for i in range(1, len(klines)):
            high       = float(klines[i][2])
            low        = float(klines[i][3])
            prev_close = float(klines[i-1][4])
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return sum(trs) / len(trs)
    except:
        return 0.0

def get_order_status(symbol, order_id):
    return binance_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

def cancel_all_orders(symbol):
    binance_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    binance_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

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
# EMIR FONKSIYONLARI
# =========================================================================
def place_limit_tp(symbol, side, qty, price):
    """TP icin limit order — slippage sifir"""
    result = binance_request("POST", "/fapi/v1/order", {
        "symbol":      symbol,
        "side":        side,
        "type":        "LIMIT",
        "price":       price,
        "quantity":    qty,
        "timeInForce": "GTC",
        "reduceOnly":  "true"
    })
    log.info("Limit TP @ " + str(price) + ": " + str(result))
    return result

def place_algo_sl(symbol, side, qty, price):
    """SL icin algo order"""
    result = binance_request("POST", "/fapi/v1/algoOrder", {
        "symbol":       symbol,
        "side":         side,
        "type":         "STOP_MARKET",
        "algotype":     "CONDITIONAL",
        "triggerPrice": price,
        "quantity":     qty,
        "reduceOnly":   "true",
        "timeInForce":  "GTC",
        "workingType":  "MARK_PRICE"
    })
    log.info("Algo SL @ " + str(price) + ": " + str(result))
    return result

def close_position_market(symbol, side, qty, sebep=""):
    result = binance_request("POST", "/fapi/v1/order", {
        "symbol":     symbol,
        "side":       side,
        "type":       "MARKET",
        "quantity":   qty,
        "reduceOnly": "true"
    })
    log.info("Market kapat (" + sebep + "): " + str(result))
    return result

# =========================================================================
# LIMIT GIRIS — suresiz bekle, sadece elle iptal edilir
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
        log.error("Limit acilamadi: " + str(result))
        return False, 0.0

    order_id = result["orderId"]
    log.info("Limit acildi: " + str(order_id) + " @ " + str(price))

    # Suresiz bekle — sadece FILLED veya elle CANCELED olunca cik
    while True:
        time.sleep(5)
        status = get_order_status(symbol, order_id)
        durum  = status.get("status", "")
        log.info("Limit durumu: " + durum)

        if durum == "FILLED":
            gercek = float(status.get("avgPrice", price))
            log.info("Limit doldu @ " + str(gercek))
            return True, gercek

        if durum in ("CANCELED", "EXPIRED", "REJECTED"):
            log.warning("Limit iptal edildi (elle veya sistem): " + durum)
            return False, 0.0

# =========================================================================
# ISLEM AC
#
# Cikis:
#   TP  @ Hedef → %90 limit order (slippage sifir)
#   Trailing    → %10 (TP vurulunca baslar, 1.5x ATR)
#   SL          → Algo order
# =========================================================================
def islem_ac(symbol, yon, giris, stop, hedef, risk_usdt, pine_qty):
    try:
        clean = symbol.replace(".P", "").upper()
        log.info("Islem: " + clean + " " + yon)

        if not gun_zarar.acilabilir_mi():
            send_tg("BLOKE: " + clean + "\nGunluk limit asildi!")
            return

        set_leverage(clean, LEVERAGE)
        time.sleep(0.2)

        sl_dist = abs(giris - stop)
        if sl_dist == 0:
            send_tg("HATA: " + clean + " SL sifir!")
            return

        min_qty = get_min_qty(clean)

        # Miktar
        if pine_qty and pine_qty > 0:
            qty = round_qty(clean, pine_qty)
        else:
            qty = round_qty(clean, risk_usdt / (sl_dist + giris * 2 * KOMISYON_ORAN))

        if qty < min_qty:
            send_tg("HATA: " + clean + " qty=" + str(qty) + " < min=" + str(min_qty))
            return

        # %90 TP, %10 trailing
        tp_qty       = round_qty(clean, qty * TP_PCT)
        trailing_qty = round_qty(clean, qty - tp_qty)

        giris_r = round_price(clean, giris)
        stop_r  = round_price(clean, stop)
        hedef_r = round_price(clean, hedef)

        entry_side = "BUY"  if yon == "LONG" else "SELL"
        close_side = "SELL" if yon == "LONG" else "BUY"
        fee        = qty * giris * 4 * KOMISYON_ORAN

        send_tg(
            "LIMIT EMIR ACILDI\n"
            + clean + " " + yon + " @ " + str(giris_r) + "\n"
            "Qty: " + str(qty) + " | Risk: " + str(risk_usdt) + "$ + fee ~" + str(round(fee, 2)) + "$\n"
            "Fiyat gelene kadar bekliyor... (elle iptal edebilirsin)"
        )

        # GIRIS — suresiz limit, sadece elle iptal
        doldu, gercek_giris = place_limit_entry(clean, entry_side, qty, giris_r)

        if not doldu:
            send_tg("EMIR IPTAL EDILDI: " + clean + "\nPozisyon acilmadi.")
            return

        time.sleep(0.5)
        atr = get_atr(clean)

        # TP — %90 Limit @ Hedef (slippage sifir)
        if tp_qty >= min_qty:
            place_limit_tp(clean, close_side, tp_qty, hedef_r)
        time.sleep(0.3)

        # SL — Algo order
        place_algo_sl(clean, close_side, qty, stop_r)

        aktif_islemler[clean] = {
            "yon":          yon,
            "ep":           gercek_giris,
            "sl":           stop_r,
            "tp":           hedef_r,
            "qty":          qty,
            "tp_qty":       tp_qty,
            "trailing_qty": trailing_qty,
            "risk_usdt":    risk_usdt,
            "tp_hit":       False,
            "trailing_sl":  None,
            "best_price":   gercek_giris,
            "atr":          atr if atr > 0 else sl_dist,
        }

        send_tg(
            "ISLEM ACILDI\n"
            + clean + " " + yon + "\n"
            "Giris: " + str(gercek_giris) + " | Qty: " + str(qty) + "\n"
            "Risk: " + str(risk_usdt) + "$ | Max Zarar: " + str(MAX_LOSS_TRADE) + "$\n\n"
            "TP  @ " + str(hedef_r) + " | " + str(tp_qty) + " adet (%90)\n"
            "Trailing: " + str(trailing_qty) + " adet (%10) → TP sonrasi\n"
            "SL: " + str(stop_r) + " (Binance algo)"
        )

    except Exception as e:
        log.error("islem_ac hatasi: " + str(e))
        send_tg("HATA: " + symbol + " - " + str(e))

# =========================================================================
# POZISYON TAKIP
# =========================================================================
def pozisyon_takip():
    while True:
        try:
            for symbol, ism in list(aktif_islemler.items()):
                pos = get_position(symbol)
                if not pos:
                    continue

                pos_amt    = float(pos.get("positionAmt", 0))
                unreal_pnl = float(pos.get("unRealizedProfit", 0))

                if abs(pos_amt) == 0:
                    if unreal_pnl < 0:
                        gun_zarar.ekle(abs(unreal_pnl))
                    del aktif_islemler[symbol]
                    continue

                mark_price = get_mark_price(symbol)
                if mark_price == 0:
                    continue

                yon        = ism["yon"]
                ep         = ism["ep"]
                atr        = ism["atr"]
                close_side = "SELL" if yon == "LONG" else "BUY"
                rem        = round_qty(symbol, abs(pos_amt))

                # MAX ZARAR — -15$ olunca hemen kapat
                if unreal_pnl <= -MAX_LOSS_TRADE:
                    cancel_all_orders(symbol)
                    time.sleep(0.2)
                    close_position_market(symbol, close_side, rem, "MAX ZARAR")
                    gun_zarar.ekle(abs(unreal_pnl))
                    send_tg(
                        "MAX ZARAR: " + symbol + "\n"
                        "Zarar: " + str(round(unreal_pnl, 2)) + "$\n"
                        "Pozisyon kapatildi!\n"
                        "Gunluk: -" + str(round(gun_zarar.gun_zarari, 2)) + "$"
                    )
                    if symbol in aktif_islemler:
                        del aktif_islemler[symbol]
                    continue

                # TP VURULDU? — miktar %90 azaldiysa
                if not ism["tp_hit"] and abs(pos_amt) < ism["qty"] * 0.20:
                    ism["tp_hit"] = True
                    log.info("TP vuruldu: " + symbol + " - Trailing basladi")

                    # Trailing SL baslat
                    trailing_mesafe = atr * TRAILING_ATR
                    if yon == "LONG":
                        ism["trailing_sl"] = round_price(symbol, mark_price - trailing_mesafe)
                    else:
                        ism["trailing_sl"] = round_price(symbol, mark_price + trailing_mesafe)
                    ism["best_price"] = mark_price

                    send_tg(
                        "TP ALINDI: " + symbol + " @ " + str(ism["tp"]) + "\n"
                        "%90 kapandi!\n"
                        "Trailing SL basladi: " + str(ism["trailing_sl"]) + "\n"
                        "Kalan %10 trailing ile devam ediyor!"
                    )

                # TRAILING SL — TP sonrasi aktif
                if ism["tp_hit"] and ism["trailing_sl"] is not None:
                    trailing_mesafe = atr * TRAILING_ATR

                    if yon == "LONG":
                        if mark_price > ism["best_price"]:
                            ism["best_price"] = mark_price
                            yeni_sl = round_price(symbol, mark_price - trailing_mesafe)
                            if yeni_sl > ism["trailing_sl"]:
                                ism["trailing_sl"] = yeni_sl
                    elif yon == "SHORT":
                        if mark_price < ism["best_price"]:
                            ism["best_price"] = mark_price
                            yeni_sl = round_price(symbol, mark_price + trailing_mesafe)
                            if yeni_sl < ism["trailing_sl"]:
                                ism["trailing_sl"] = yeni_sl

                    tetiklendi = (
                        (yon == "LONG"  and mark_price <= ism["trailing_sl"]) or
                        (yon == "SHORT" and mark_price >= ism["trailing_sl"])
                    )

                    if tetiklendi:
                        cancel_all_orders(symbol)
                        time.sleep(0.2)
                        close_position_market(symbol, close_side, rem, "TRAILING")
                        pnl = (mark_price - ep) * rem if yon == "LONG" else (ep - mark_price) * rem
                        if pnl < 0:
                            gun_zarar.ekle(abs(pnl))
                        send_tg(
                            "TRAILING SL: " + symbol + "\n"
                            "Fiyat: " + str(mark_price) + "\n"
                            "Kalan %10 kapatildi!\n"
                            "PNL: " + ("+" if pnl >= 0 else "") + str(round(pnl, 2)) + "$"
                        )
                        if symbol in aktif_islemler:
                            del aktif_islemler[symbol]

        except Exception as e:
            log.error("Takip hatasi: " + str(e))
        time.sleep(3)

# =========================================================================
# POZISYON SENKRONIZASYON — her 60sn
# =========================================================================
def pozisyon_senkronize():
    while True:
        try:
            result = binance_request("GET", "/fapi/v2/positionRisk", {})
            if isinstance(result, list):
                for p in result:
                    amt    = float(p.get("positionAmt", 0))
                    symbol = p["symbol"]
                    if amt != 0 and symbol not in aktif_islemler:
                        ep  = float(p["entryPrice"])
                        yon = "LONG" if amt > 0 else "SHORT"
                        atr = get_atr(symbol)
                        aktif_islemler[symbol] = {
                            "yon": yon, "ep": ep,
                            "sl": 0, "tp": 0,
                            "qty": abs(amt),
                            "tp_qty": 0, "trailing_qty": abs(amt),
                            "risk_usdt": RISK_USDT,
                            "tp_hit": True,
                            "trailing_sl": None, "best_price": ep,
                            "atr": atr if atr > 0 else 0.001,
                        }
                        send_tg("SENKRONIZE: " + symbol + "\nBot izlemeye basladi. SL manuel kontrol!")
                    elif amt == 0 and symbol in aktif_islemler:
                        del aktif_islemler[symbol]
        except Exception as e:
            log.error("Senkron hatasi: " + str(e))
        time.sleep(60)

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
                    ep     = float(p["entryPrice"])
                    yon    = "LONG" if amt > 0 else "SHORT"
                    atr    = get_atr(symbol)
                    aktif_islemler[symbol] = {
                        "yon": yon, "ep": ep,
                        "sl": 0, "tp": 0,
                        "qty": abs(amt),
                        "tp_qty": 0, "trailing_qty": abs(amt),
                        "risk_usdt": RISK_USDT,
                        "tp_hit": True,
                        "trailing_sl": None, "best_price": ep,
                        "atr": atr if atr > 0 else 0.001,
                    }
                    yuklenen += 1
            if yuklenen > 0:
                send_tg("Acik pozisyon yuklendi: " + str(yuklenen) + " adet\nSL manuel kontrol!")
    except Exception as e:
        log.error("Yukle hatasi: " + str(e))

# =========================================================================
# PARSE YARDIMCILARI
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
            return float(val.replace(",", ".").replace("+", ""))
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

# =========================================================================
# WEBHOOK
# =========================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.data.decode("utf-8").strip()
        log.info("Webhook: " + raw[:200])

        try:
            data = json.loads(raw)
        except:
            return jsonify({"status": "error"}), 400

        chat_id = data.get("chat_id", TG_CHAT_ID)
        text    = data.get("text", "")
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
                hedef  = _parse_float(text, "Hedef:")
                risk   = _parse_float(text, "Risk:") or RISK_USDT
                miqdar = _parse_float(text, "Miqdar:")

                if all([parite, yon, giris, stop, hedef]):
                    clean_parite = parite.replace(".P", "").upper()
                    if clean_parite in aktif_islemler:
                        send_tg("BLOKE: " + clean_parite + " zaten aktif!")
                    else:
                        threading.Thread(
                            target=islem_ac,
                            args=(parite, yon, giris, stop, hedef, risk, miqdar),
                            daemon=True
                        ).start()
                else:
                    send_tg(
                        "PARSE EKSIK!\n"
                        "Parite:" + str(parite) + " Yon:" + str(yon) + "\n"
                        "Giris:" + str(giris) + " Stop:" + str(stop) + " Hedef:" + str(hedef)
                    )

        elif any(k in text_upper for k in ["FULL WIN", "RISKSIZ", "STOP VURULDU"]):
            log.info("Pine mesaji, istatistik guncellenmedi.")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log.error("Webhook hatasi: " + str(e))
        return jsonify({"status": "error"}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":       "ok",
        "aktif":        list(aktif_islemler.keys()),
        "trade":        TRADE_ACTIVE,
        "gunluk_zarar": gun_zarar.gun_zarari,
        "durduruldu":   gun_zarar.durduruldu,
        "istat":        {"toplam": istat.toplam, "net_r": istat.net_r}
    }), 200

# =========================================================================
# GUNLUK RAPOR — BAKU 00:00 (UTC 20:00)
# =========================================================================
def gunluk_rapor():
    rapor = istat.rapor_olustur()
    if rapor:
        send_tg(rapor)
    istat.reset()
    gun_zarar.sifirla()

def zamanlayici():
    schedule.every().day.at("20:00").do(gunluk_rapor)
    while True:
        schedule.run_pending()
        time.sleep(30)

# =========================================================================
# ANA PROGRAM
# =========================================================================
if __name__ == "__main__":
    log.info("PHANTOM BOT v2.4 baslatildi.")
    send_tg(
        "PHANTOM BOT v2.4 aktiv\n\n"
        "Giris: LIMIT (suresiz, elle iptal)\n\n"
        "Cikis:\n"
        "  TP  @ Hedef → %90 (limit, slippage sifir)\n"
        "  Trailing    → %10 (TP sonrasi, " + str(TRAILING_ATR) + "x ATR)\n\n"
        "Max Zarar/Islem: " + str(MAX_LOSS_TRADE) + "$\n"
        "Max Zarar/Gun:   " + str(MAX_LOSS_DAILY) + "$\n"
        "Trade: " + ("AKTIV" if TRADE_ACTIVE else "PASIV")
    )
    if TRADE_ACTIVE:
        pozisyonlari_yukle()
        threading.Thread(target=pozisyon_takip, daemon=True).start()
        threading.Thread(target=pozisyon_senkronize, daemon=True).start()
    threading.Thread(target=zamanlayici, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

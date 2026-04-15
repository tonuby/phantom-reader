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

RISK_USDT       = float(os.environ.get("RISK_USDT", "10"))
LEVERAGE        = int(os.environ.get("LEVERAGE", "10"))
TRADE_ACTIVE    = os.environ.get("TRADE_ACTIVE", "false").lower() == "true"

KOMISYON_ORAN   = 0.0004
LIMIT_TIMEOUT   = 900        # 15 dakika
MAX_LOSS_TRADE  = 15.0       # Islem basina max zarar ($)
MAX_LOSS_DAILY  = 100.0      # Gunluk max zarar ($) — Baku 00:00'da sifirlanir
TRAILING_ATR    = 1.5        # Trailing SL ATR carpani

# Baku UTC+4
BAKU_TZ = timezone(timedelta(hours=4))

# =========================================================================
# GUNLUK ZARAR TAKIBI
# =========================================================================
class GunlukZarar:
    def __init__(self):
        self.gun_zarari = 0.0
        self.durduruldu = False

    def ekle(self, zarar_usdt):
        self.gun_zarari += zarar_usdt
        if self.gun_zarari >= MAX_LOSS_DAILY and not self.durduruldu:
            self.durduruldu = True
            send_tg(
                f"GUNLUK LIMIT ASILD!\n"
                f"Gunluk zarar: -{self.gun_zarari:.2f}$\n"
                f"Limit: {MAX_LOSS_DAILY}$\n"
                f"Bot bugun yeni islem ACMAYACAK!\n"
                f"Baku 00:00'da sifirlanacak."
            )
            log.warning(f"Gunluk zarar limiti asildi: {self.gun_zarari:.2f}$")

    def sifirla(self):
        self.gun_zarari = 0.0
        self.durduruldu = False
        log.info("Gunluk zarar sayaci sifirlandi (Baku 00:00)")
        send_tg(f"Yeni gun basladi (Baku)\nGunluk zarar sayaci sifirlandi.\nBot yeni islemlere hazir!")

    def islem_acilabilir_mi(self):
        return not self.durduruldu

gun_zarar = GunlukZarar()

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
        tarih = datetime.now(BAKU_TZ).strftime("%d/%m/%Y")
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
            f"Net USD: {usd_s}$\n"
            f"Gunluk Zarar: -{gun_zarar.gun_zarari:.2f}$"
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
        r = requests.get(f"{BINANCE_URL}/fapi/v1/premiumIndex?symbol={symbol}", timeout=5)
        return float(r.json().get("markPrice", 0))
    except:
        return 0.0

def get_atr(symbol):
    """Son 14 mumun ATR'sini hesapla"""
    try:
        r = requests.get(
            f"{BINANCE_URL}/fapi/v1/klines?symbol={symbol}&interval=5m&limit=15",
            timeout=5
        )
        klines = r.json()
        if len(klines) < 2:
            return 0.0
        trs = []
        for i in range(1, len(klines)):
            high = float(klines[i][2])
            low  = float(klines[i][3])
            prev_close = float(klines[i-1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        return sum(trs) / len(trs)
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

def place_algo_order(symbol, side, qty, price, order_type):
    result = binance_request("POST", "/fapi/v1/algoOrder", {
        "symbol":       symbol,
        "side":         side,
        "type":         order_type,
        "algotype":     "CONDITIONAL",
        "triggerPrice": price,
        "quantity":     qty,
        "reduceOnly":   "true",
        "timeInForce":  "GTC",
        "workingType":  "MARK_PRICE"
    })
    log.info(f"Algo order ({order_type}) @ {price}: {result}")
    return result

def close_position_market(symbol, side, qty, sebep=""):
    result = binance_request("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side,
        "type": "MARKET", "quantity": qty,
        "reduceOnly": "true"
    })
    log.info(f"Market kapatis ({sebep}): {result}")
    return result

# =========================================================================
# LIMIT GIRIS — 15 dakika bekle
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

    # Suresiz bekle — fiyat gelene kadar iptal etme
    while True:
        time.sleep(5)
        status = get_order_status(symbol, order_id)
        durum = status.get("status", "")
        log.info(f"Limit emir durumu: {durum}")

        if durum == "FILLED":
            gercek_fiyat = float(status.get("avgPrice", price))
            log.info(f"Limit emir doldu @ {gercek_fiyat}")
            return True, gercek_fiyat

        if durum in ("CANCELED", "EXPIRED", "REJECTED"):
            log.warning(f"Limit emir iptal/red: {durum}")
            return False, 0.0

# =========================================================================
# ISLEM AC
# Cikis Yapisi:
#   %25 → TP1 (ilk likidite — Pine TP1 fiyati)
#   %25 → TP2 (ikinci likidite — Pine Hedef fiyati)
#   %50 → Trailing SL (1.5x ATR ile takip)
# =========================================================================
def islem_ac(symbol, yon, giris, stop, tp1, hedef, risk_usdt, pine_qty):
    try:
        clean = symbol.replace(".P", "").upper()
        log.info(f"Islem: {clean} {yon} g={giris} sl={stop} tp1={tp1} tp2={hedef} risk={risk_usdt}")

        # Gunluk limit kontrol
        if not gun_zarar.islem_acilabilir_mi():
            send_tg(f"SINYAL BLOKE: {clean}\nGunluk zarar limiti ({MAX_LOSS_DAILY}$) asildi!\nBaku 00:00'a kadar yeni islem yok.")
            return

        set_leverage(clean, LEVERAGE)
        time.sleep(0.2)

        sl_dist = abs(giris - stop)
        if sl_dist == 0:
            send_tg(f"HATA: {clean} SL mesafesi sifir!")
            return

        min_qty = get_min_qty(clean)

        # Miktar: Pine'dan geldiyse aynen kullan
        if pine_qty and pine_qty > 0:
            qty = round_qty(clean, pine_qty)
        else:
            komisyon_per_unit = giris * 2 * KOMISYON_ORAN
            qty = round_qty(clean, risk_usdt / (sl_dist + komisyon_per_unit))

        if qty < min_qty:
            send_tg(f"HATA: {clean} miktar cok kucuk! qty={qty} min={min_qty}")
            return

        # Cikis yapisi: %25 / %25 / %50
        tp1_qty      = round_qty(clean, qty * 0.25)
        tp2_qty      = round_qty(clean, qty * 0.25)
        trailing_qty = round_qty(clean, qty - tp1_qty - tp2_qty)

        giris_r = round_price(clean, giris)
        tp1_r   = round_price(clean, tp1)
        hedef_r = round_price(clean, hedef)
        stop_r  = round_price(clean, stop)

        entry_side = "BUY"  if yon == "LONG" else "SELL"
        close_side = "SELL" if yon == "LONG" else "BUY"

        fee = qty * giris * 4 * KOMISYON_ORAN

        send_tg(
            f"LIMIT EMIR GONDERILDI\n"
            f"{clean} {yon} @ {giris_r}\n"
            f"Qty: {qty} | Risk: {risk_usdt}$ + fee ~{fee:.2f}$\n"
            f"Fiyat gelene kadar bekliyor..."
        )

        # 1. LIMIT GIRIS
        doldu, gercek_giris = place_limit_entry(clean, entry_side, qty, giris_r)
        if not doldu:
            send_tg(f"EMIR DOLMADI: {clean}\nFiyat {giris_r} seviyesine gelmedi.")
            return

        time.sleep(0.5)

        # ATR hesapla (trailing icin)
        atr = get_atr(clean)
        trailing_mesafe = atr * TRAILING_ATR
        log.info(f"ATR: {atr} | Trailing mesafe: {trailing_mesafe}")

        # 2. TP1 — %25
        if tp1_qty >= min_qty:
            place_algo_order(clean, close_side, tp1_qty, tp1_r, "TAKE_PROFIT_MARKET")
        time.sleep(0.3)

        # 3. TP2 — %25
        if tp2_qty >= min_qty:
            place_algo_order(clean, close_side, tp2_qty, hedef_r, "TAKE_PROFIT_MARKET")
        time.sleep(0.3)

        # 4. SL — tam qty (tum pozisyon korunur)
        place_algo_order(clean, close_side, qty, stop_r, "STOP_MARKET")

        # 5. Bot takibi — trailing SL + max zarar
        # Trailing SL baslangici None — TP1 vurulunca aktif olur
        trailing_sl_baslangic = None

        aktif_islemler[clean] = {
            "yon":          yon,
            "ep":           gercek_giris,
            "sl":           stop_r,
            "tp1":          tp1_r,
            "tp2":          hedef_r,
            "qty":          qty,
            "tp1_qty":      tp1_qty,
            "tp2_qty":      tp2_qty,
            "trailing_qty": trailing_qty,
            "risk_usdt":    risk_usdt,
            "tp1_hit":      False,
            "tp2_hit":      False,
            "trailing_sl":  trailing_sl_baslangic,
            "best_price":   gercek_giris,
            "atr":          atr,
        }

        send_tg(
            f"ISLEM ACILDI\n"
            f"{clean} {yon}\n"
            f"Giris: {gercek_giris} | Qty: {qty}\n"
            f"Risk: {risk_usdt}$ | Max Zarar: {MAX_LOSS_TRADE}$\n\n"
            f"TP1: {tp1_r} | {tp1_qty} adet (%25)\n"
            f"TP2: {hedef_r} | {tp2_qty} adet (%25)\n"
            f"Trailing: {trailing_qty} adet (%50) @ ATR*{TRAILING_ATR}\n"
            f"SL: {stop_r} (Binance algo)"
        )

    except Exception as e:
        log.error(f"islem_ac hatasi: {e}")
        send_tg(f"HATA: {symbol} islem acilamadi: {str(e)}")

# =========================================================================
# POZISYON TAKIP
# - Trailing SL guncelle (%50 kisim)
# - Max zarar kontrolu (-15$)
# - TP1/TP2 vurulunca trailing devam eder
# =========================================================================
def pozisyon_senkronize():
    """Her 60sn Binance'den acik pozisyonlari cek, aktif_islemler ile senkronize et"""
    while True:
        try:
            result = binance_request("GET", "/fapi/v2/positionRisk", {})
            if isinstance(result, list):
                for p in result:
                    amt = float(p.get("positionAmt", 0))
                    symbol = p["symbol"]
                    if amt != 0 and symbol not in aktif_islemler:
                        ep = float(p["entryPrice"])
                        yon = "LONG" if amt > 0 else "SHORT"
                        atr = get_atr(symbol)
                        aktif_islemler[symbol] = {
                            "yon": yon, "ep": ep,
                            "sl": 0, "tp1": 0, "tp2": 0,
                            "qty": abs(amt),
                            "tp1_qty": 0, "tp2_qty": 0,
                            "trailing_qty": abs(amt),
                            "risk_usdt": RISK_USDT,
                            "tp1_hit": True,
                            "tp2_hit": True,
                            "trailing_sl": None,
                            "best_price": ep,
                            "atr": atr if atr > 0 else 0.001,
                        }
                        log.info(f"Senkronize edildi: {symbol} {yon} qty={abs(amt)}")
                        send_tg("POZISYON SENKRONIZE EDILDI: " + symbol + "\nBot yeniden izlemeye basladi.\nNot: SL ve Trailing manuel kontrol et!")
                    elif amt == 0 and symbol in aktif_islemler:
                        del aktif_islemler[symbol]
                        log.info(f"Kapali pozisyon temizlendi: {symbol}")
        except Exception as e:
            log.error(f"Senkronizasyon hatasi: {e}")
        time.sleep(60)

def pozisyon_takip():
    while True:
        try:
            for symbol, ism in list(aktif_islemler.items()):
                pos = get_position(symbol)
                if not pos:
                    continue

                pos_amt   = float(pos.get("positionAmt", 0))
                unreal_pnl = float(pos.get("unRealizedProfit", 0))

                # Pozisyon kapandi
                if abs(pos_amt) == 0:
                    # Zarar hesapla
                    if unreal_pnl < 0:
                        gun_zarar.ekle(abs(unreal_pnl))
                    del aktif_islemler[symbol]
                    log.info(f"Pozisyon kapandi: {symbol} PNL: {unreal_pnl:.2f}$")
                    continue

                mark_price = get_mark_price(symbol)
                if mark_price == 0:
                    continue

                yon        = ism["yon"]
                ep         = ism["ep"]
                atr        = ism["atr"]
                close_side = "SELL" if yon == "LONG" else "BUY"
                rem        = round_qty(symbol, abs(pos_amt))

                # ---------------------------------------------------------
                # MAX ZARAR KONTROLU — -15$ olunca hemen kapat
                # ---------------------------------------------------------
                if unreal_pnl <= -MAX_LOSS_TRADE:
                    log.warning(f"MAX ZARAR ASILD: {symbol} PNL={unreal_pnl:.2f}$")
                    cancel_all_orders(symbol)
                    time.sleep(0.2)
                    close_position_market(symbol, close_side, rem, "MAX ZARAR")
                    gun_zarar.ekle(abs(unreal_pnl))
                    send_tg(
                        f"MAX ZARAR TETIKLENDI: {symbol}\n"
                        f"Zarar: {unreal_pnl:.2f}$ / Limit: -{MAX_LOSS_TRADE}$\n"
                        f"Pozisyon market ile kapatildi!\n"
                        f"Gunluk toplam zarar: -{gun_zarar.gun_zarari:.2f}$"
                    )
                    del aktif_islemler[symbol]
                    continue

                # ---------------------------------------------------------
                # TP1 VURULDU MU?
                # ---------------------------------------------------------
                if not ism["tp1_hit"] and abs(pos_amt) < ism["qty"] * 0.85:
                    ism["tp1_hit"] = True
                    log.info(f"TP1 vuruldu: {symbol}, Trailing SL aktif oldu")

                    # Trailing SL baslat — TP1 fiyatindan
                    atr = ism["atr"]
                    trailing_mesafe = atr * TRAILING_ATR
                    if yon == "LONG":
                        ism["trailing_sl"] = round_price(symbol, mark_price - trailing_mesafe)
                        ism["best_price"]  = mark_price
                    else:
                        ism["trailing_sl"] = round_price(symbol, mark_price + trailing_mesafe)
                        ism["best_price"]  = mark_price

                    # 1. Eski algo SL iptal et
                    algo_orders = binance_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
                    if isinstance(algo_orders, dict) and "orders" in algo_orders:
                        for order in algo_orders["orders"]:
                            if order.get("orderType") == "STOP_MARKET":
                                algo_id = order.get("algoId")
                                if algo_id:
                                    binance_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
                                    log.info(f"Algo SL iptal edildi: {algo_id}")
                    time.sleep(0.3)

                    # 2. BE SL koy — giris fiyatina (kalan tum miktar)
                    be_price = round_price(symbol, ism["ep"])
                    close_side_be = "SELL" if yon == "LONG" else "BUY"
                    kalan_qty = round_qty(symbol, abs(pos_amt))
                    be_result = place_algo_order(symbol, close_side_be, kalan_qty, be_price, "STOP_MARKET")
                    log.info(f"BE SL kondu @ {be_price}: {be_result}")

                    send_tg(
                        f"TP1 ALINDI: {symbol} @ {ism['tp1']}\n"
                        f"%25 pozisyon kapandi.\n"
                        f"BE SL kondu: {be_price} (giris fiyati)\n"
                        f"Trailing SL aktif: {ism['trailing_sl']}\n"
                        f"Kalan %75 trailing + BE ile devam ediyor."
                    )

                # ---------------------------------------------------------
                # TP2 VURULDU MU?
                # ---------------------------------------------------------
                if ism["tp1_hit"] and not ism["tp2_hit"] and abs(pos_amt) < ism["qty"] * 0.60:
                    ism["tp2_hit"] = True
                    log.info(f"TP2 vuruldu: {symbol}")
                    send_tg(f"TP2 ALINDI: {symbol} @ {ism['tp2']}\n%25 pozisyon daha kapandi.\nTrailing SL devam ediyor (%50).")

                # ---------------------------------------------------------
                # TRAILING SL — sadece TP1 vurulduktan sonra aktif
                # TP1 oncesi Binance algo SL korur
                # ---------------------------------------------------------
                trailing_tetiklendi = False

                if ism["tp1_hit"] and ism["trailing_sl"] is not None:
                    trailing_mesafe = atr * TRAILING_ATR

                    if yon == "LONG":
                        if mark_price > ism["best_price"]:
                            ism["best_price"] = mark_price
                            yeni_sl = round_price(symbol, mark_price - trailing_mesafe)
                            if yeni_sl > ism["trailing_sl"]:
                                ism["trailing_sl"] = yeni_sl
                                log.info(f"Trailing SL guncellendi: {symbol} -> {yeni_sl}")

                    elif yon == "SHORT":
                        if mark_price < ism["best_price"]:
                            ism["best_price"] = mark_price
                            yeni_sl = round_price(symbol, mark_price + trailing_mesafe)
                            if yeni_sl < ism["trailing_sl"]:
                                ism["trailing_sl"] = yeni_sl
                                log.info(f"Trailing SL guncellendi: {symbol} -> {yeni_sl}")

                    trailing_tetiklendi = (
                        (yon == "LONG"  and mark_price <= ism["trailing_sl"]) or
                        (yon == "SHORT" and mark_price >= ism["trailing_sl"])
                    )

                if trailing_tetiklendi:
                    trailing_rem = round_qty(symbol, abs(pos_amt))
                    if trailing_rem > 0:
                        cancel_all_orders(symbol)
                        time.sleep(0.2)
                        close_position_market(symbol, close_side, trailing_rem, "TRAILING SL")

                        # Kar/zarar hesapla
                        if yon == "LONG":
                            pnl = (mark_price - ep) * trailing_rem
                        else:
                            pnl = (ep - mark_price) * trailing_rem

                        if pnl < 0:
                            gun_zarar.ekle(abs(pnl))

                        send_tg(
                            f"TRAILING SL TETIKLENDI: {symbol}\n"
                            f"Fiyat: {mark_price} | Trailing SL: {ism['trailing_sl']}\n"
                            f"Kalan pozisyon kapatildi!\n"
                            f"Tahmini PNL: {'+' if pnl >= 0 else ''}{pnl:.2f}$"
                        )
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
                    atr = get_atr(symbol)
                    aktif_islemler[symbol] = {
                        "yon": yon, "ep": ep,
                        "sl": 0, "tp1": 0, "tp2": 0,
                        "qty": abs(amt),
                        "tp1_qty": 0, "tp2_qty": 0,
                        "trailing_qty": abs(amt),
                        "risk_usdt": RISK_USDT,
                        "tp1_hit": True, "tp2_hit": True,
                        "trailing_sl": ep,
                        "best_price": ep,
                        "atr": atr if atr > 0 else 0.001,
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
                    # Ayni paritede aktif islem varsa bloke et
                    clean_parite = parite.replace(".P", "").upper()
                    if clean_parite in aktif_islemler:
                        send_tg(f"SINYAL BLOKE: {clean_parite}\nBu paritede zaten aktif islem var!")
                    else:
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
        "trade": TRADE_ACTIVE,
        "gunluk_zarar": gun_zarar.gun_zarari,
        "durduruldu": gun_zarar.durduruldu
    }), 200

# =========================================================================
# GUNLUK RAPOR + SIFIRLAMA — BAKU 00:00
# =========================================================================
def gunluk_rapor():
    rapor = istat.rapor_olustur()
    if rapor:
        send_tg(rapor)
    istat.reset()
    gun_zarar.sifirla()

def zamanlayici():
    # Baku UTC+4 => UTC 20:00'da calis (Baku 00:00)
    schedule.every().day.at("20:00").do(gunluk_rapor)
    while True:
        schedule.run_pending()
        time.sleep(30)

# =========================================================================
# ANA PROGRAM
# =========================================================================
if __name__ == "__main__":
    log.info("PHANTOM BOT v2.0 baslatildi.")
    log.info(f"Trade aktif: {TRADE_ACTIVE} | Risk: {RISK_USDT}$ | Leverage: {LEVERAGE}x")
    send_tg(
        f"PHANTOM BOT v2.0 aktiv\n\n"
        f"Giris: LIMIT (suresiz, manuel iptal)\n"
        f"Cikis: %25 TP1 | %25 TP2 | %50 Trailing\n"
        f"Trailing: {TRAILING_ATR}x ATR\n"
        f"Max Zarar/Islem: {MAX_LOSS_TRADE}$\n"
        f"Max Zarar/Gun: {MAX_LOSS_DAILY}$ (Baku 00:00 sifirlanir)\n"
        f"Trade: {'AKTIV' if TRADE_ACTIVE else 'PASIV'}"
    )
    if TRADE_ACTIVE:
        pozisyonlari_yukle()
        threading.Thread(target=pozisyon_takip, daemon=True).start()
        threading.Thread(target=pozisyon_senkronize, daemon=True).start()
    threading.Thread(target=zamanlayici, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

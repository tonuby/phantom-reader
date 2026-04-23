# =========================================================================
# PHANTOM BOT v2.6
# B15 ile birebir eslesir:
#   - Giris: MARKET (aninda)
#   - TP1: +1R @ %30 kapat → SL break-even'e cekilir
#   - TP2: Kalan %70 → tam hedef limit order
#   - Risk: Komisyon dahil hesaplanir (gercek risk = RISK_USDT)
# =========================================================================

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

RISK_USDT      = float(os.environ.get("RISK_USDT", "6"))      # 300$ x %2
LEVERAGE       = int(os.environ.get("LEVERAGE", "10"))
TRADE_ACTIVE   = os.environ.get("TRADE_ACTIVE", "false").lower() == "true"

KOMISYON_ORAN  = 0.0004   # %0.04 taker — her iki taraf x2 = x4 toplam
MAX_LOSS_TRADE = float(os.environ.get("MAX_LOSS_TRADE", "8"))  # 300$ x %2 + biraz buffer
MAX_LOSS_DAILY = float(os.environ.get("MAX_LOSS_DAILY", "60")) # Gunluk max zarar
BAKU_TZ        = timezone(timedelta(hours=4))

# Cikis yapisi (B15):
TP1_PCT = 0.20   # TP1'de %20 kapat
TP2_PCT = 0.80   # Kalan %80 hedefe

# =========================================================================
# GUNLUK ZARAR TAKIBI
# =========================================================================
class GunlukZarar:
    def __init__(self):
        self.gun_kazanc = 0.0
        self.gun_kayip  = 0.0
        self.durduruldu = False

    @property
    def gun_zarari(self):
        return self.gun_kayip

    def ekle(self, zarar):
        self.gun_kayip += abs(zarar)
        net = self.gun_kazanc - self.gun_kayip
        if net <= -MAX_LOSS_DAILY and not self.durduruldu:
            self.durduruldu = True
            send_tg(
                "🚨 GUNLUK LIMIT ASILDI!\n"
                "💚 Kazanc: +" + str(round(self.gun_kazanc, 2)) + "$\n"
                "🔴 Kayip:  -" + str(round(self.gun_kayip, 2)) + "$\n"
                "📊 Net: " + str(round(net, 2)) + "$\n"
                "🛑 Limit: -" + str(MAX_LOSS_DAILY) + "$\n"
                "🕛 Baku 00:00'da sifirlanir."
            )

    def ekle_kazanc(self, kazanc):
        self.gun_kazanc += abs(kazanc)
        net = self.gun_kazanc - self.gun_kayip
        log.info("Gun net PnL: " + str(round(net, 2)) + "$")

    def net_pnl(self):
        return self.gun_kazanc - self.gun_kayip

    def sifirla(self):
        self.gun_kazanc = 0.0
        self.gun_kayip  = 0.0
        self.durduruldu = False
        send_tg("🌅 Yeni gun (Baku)\n✅ Sayac sifirlandi. Bot hazir!")

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
            "📋 GUN SONU HESABATI\n" + tarih + "\n\n"
            "📊 Veziyyet: " + durum + "\n\n"
            "🔢 Umumi: " + str(kapanan) + "\n"
            "🏆 Full Win: " + str(self.full_win) + "\n"
            "⚖️ BE +1R:   " + str(self.be) + "\n"
            "💥 Stop:     " + str(self.stop) + "\n\n"
            "✅ Ugur: %" + str(round(ugur, 1)) + "\n"
            "📈 Net R: " + r_str + "\n"
            "💵 Net USD: " + usd_s + "\n\n"
            "💚 Gunluk Kazanc: +" + str(round(gun_zarar.gun_kazanc, 2)) + "$\n"
            "🔴 Gunluk Kayip:  -" + str(round(gun_zarar.gun_kayip, 2)) + "$\n"
            "📊 Net PnL: " + str(round(gun_zarar.net_pnl(), 2)) + "$"
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

def get_order_status(symbol, order_id):
    return binance_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

def get_available_balance():
    """Kullanilabilir USDT bakiyesini dondur"""
    try:
        result = binance_request("GET", "/fapi/v2/balance", {})
        if isinstance(result, list):
            for b in result:
                if b.get("asset") == "USDT":
                    return float(b.get("availableBalance", 0))
    except Exception as e:
        log.error("Bakiye hatasi: " + str(e))
    return 0.0

def max_alinabilir_qty(symbol, fiyat, leverage):
    """
    Mevcut bakiyeye gore max alinabilir miktari hesapla.
    %90 buffer birakir — margin hatasini onler.
    """
    try:
        bakiye       = get_available_balance()
        max_notional = bakiye * leverage * 0.90
        max_qty      = max_notional / fiyat
        return round_qty(symbol, max_qty)
    except Exception as e:
        log.error("Max qty hatasi: " + str(e))
        return 0.0

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

def get_max_leverage(symbol):
    """Binance'in sembole izin verdigi max kaldiraci al"""
    try:
        result = binance_request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol})
        if isinstance(result, list) and len(result) > 0:
            brackets = result[0].get("brackets", [])
            if brackets:
                return int(brackets[0].get("initialLeverage", 20))
        elif isinstance(result, dict):
            brackets = result.get("brackets", [])
            if brackets:
                return int(brackets[0].get("initialLeverage", 20))
    except Exception as e:
        log.error("Max leverage hatasi: " + str(e))
    return 20

def set_leverage(symbol, lev):
    """Kaldiraci ayarla — Binance sembol limitini asma"""
    max_lev    = get_max_leverage(symbol)
    gercek_lev = min(lev, max_lev)
    if gercek_lev != lev:
        log.info(symbol + " max leverage: " + str(max_lev) + " — " + str(lev) + "x yerine " + str(gercek_lev) + "x kullanildi")
        send_tg("ℹ️ " + symbol + " max kaldıraç: " + str(max_lev) + "x\n" + str(lev) + "x yerine " + str(gercek_lev) + "x kullanıldı")
    return binance_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": gercek_lev})

# =========================================================================
# EMIR FONKSIYONLARI
# =========================================================================
def place_market_entry(symbol, side, qty):
    result = binance_request("POST", "/fapi/v1/order", {
        "symbol":   symbol,
        "side":     side,
        "type":     "MARKET",
        "quantity": qty
    })
    log.info("Market giris: " + str(result))
    return result

def place_limit_tp(symbol, side, qty, price):
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
    """SL icin once algo order dene, basarisiz olursa normal stop market koy"""
    # Once algo order dene
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
    if "algoId" in result or "orderId" in result:
        log.info("Algo SL @ " + str(price) + ": OK")
        return result

    # Algo basarisiz — normal STOP_MARKET ile dene
    log.warning("Algo SL basarisiz, normal STOP_MARKET deneniyor: " + str(result))
    result2 = binance_request("POST", "/fapi/v1/order", {
        "symbol":      symbol,
        "side":        side,
        "type":        "STOP_MARKET",
        "stopPrice":   price,
        "quantity":    qty,
        "reduceOnly":  "true",
        "workingType": "MARK_PRICE",
        "timeInForce": "GTC"
    })
    log.info("Normal STOP_MARKET @ " + str(price) + ": " + str(result2))
    return result2

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
# ISLEM AC — v2.6 / B15
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

        # Pine'dan gelen miktari kullan (komisyon zaten dahil hesaplandi)
        if pine_qty and pine_qty > 0:
            qty = round_qty(clean, pine_qty)
        else:
            # Fallback: komisyon dahil hesapla
            kom_pay = giris * KOMISYON_ORAN * 4.0
            qty = round_qty(clean, risk_usdt / (sl_dist + kom_pay))

        # BAKIYE KONTROLU — Pine miktari fazlaysa otomatik küçült
        mark = get_mark_price(clean)
        if mark == 0:
            mark = giris
        max_qty = max_alinabilir_qty(clean, mark, LEVERAGE)
        if max_qty > 0 and qty > max_qty:
            log.warning("Miktar dusuruldu: " + str(qty) + " → " + str(max_qty) + " (bakiye limiti)")
            send_tg(
                "⚠️ MİKTAR AYARLANDI: " + clean + "\n"
                "Pine: " + str(qty) + " adet\n"
                "Bakiye limiti: " + str(max_qty) + " adet\n"
                "Giris devam ediyor..."
            )
            qty = max_qty

        if qty < min_qty:
            send_tg("HATA: " + clean + " qty=" + str(qty) + " < min=" + str(min_qty))
            return

        # TP1: %30, TP2: %70
        tp1_qty = round_qty(clean, qty * TP1_PCT)
        tp2_qty = round_qty(clean, qty - tp1_qty)

        stop_r  = round_price(clean, stop)
        hedef_r = round_price(clean, hedef)

        # TP1 = giris ± sl_dist (tam +1R)
        if yon == "LONG":
            tp1_price = round_price(clean, giris + sl_dist)
        else:
            tp1_price = round_price(clean, giris - sl_dist)

        entry_side = "BUY"  if yon == "LONG" else "SELL"
        close_side = "SELL" if yon == "LONG" else "BUY"

        send_tg(
            "🔄 ISLEM ACILIYOR — v2.6\n"
            + clean + " " + yon + "\n"
            "📥 Market girisi yapiliyor..."
        )

        # GIRIS — MARKET
        entry_result = place_market_entry(clean, entry_side, qty)
        if "orderId" not in entry_result:
            send_tg("GIRIS HATASI: " + clean + "\n" + str(entry_result))
            return

        time.sleep(0.5)
        entry_status = get_order_status(clean, entry_result["orderId"])
        gercek_giris = float(entry_status.get("avgPrice", giris))
        if gercek_giris == 0:
            gercek_giris = get_mark_price(clean)

        # Gercek giristen TP1 hesapla
        gercek_sl_dist = abs(gercek_giris - stop_r)
        if yon == "LONG":
            gercek_tp1 = round_price(clean, gercek_giris + gercek_sl_dist)
        else:
            gercek_tp1 = round_price(clean, gercek_giris - gercek_sl_dist)

        # TP1 limit order (%30)
        if tp1_qty >= min_qty:
            place_limit_tp(clean, close_side, tp1_qty, gercek_tp1)
        time.sleep(0.3)

        # TP2 limit order (%70)
        if tp2_qty >= min_qty:
            place_limit_tp(clean, close_side, tp2_qty, hedef_r)
        time.sleep(0.3)

        # SL algo order
        place_algo_sl(clean, close_side, qty, stop_r)

        aktif_islemler[clean] = {
            "yon":       yon,
            "ep":        gercek_giris,
            "sl":        stop_r,
            "tp1":       gercek_tp1,
            "tp2":       hedef_r,
            "qty":       qty,
            "tp1_qty":   tp1_qty,
            "tp2_qty":   tp2_qty,
            "risk_usdt": risk_usdt,
            "tp1_hit":   False,
            "be_set":    False,
        }

        send_tg(
            "✅ ISLEM ACILDI — v2.6\n"
            + clean + " " + yon + "\n"
            "📥 Giris: " + str(gercek_giris) + " (MARKET)\n"
            "📦 Qty: " + str(qty) + "\n\n"
            "💰 TP1: " + str(gercek_tp1) + " (+1R | %30 = " + str(tp1_qty) + " adet)\n"
            "🎯 TP2: " + str(hedef_r) + " (Hedef | %70 = " + str(tp2_qty) + " adet)\n"
            "🛑 SL:  " + str(stop_r) + " (Algo)\n\n"
            "🛡 TP1 vurulunca → SL girişe cekilir (BE)"
        )

    except Exception as e:
        log.error("islem_ac hatasi: " + str(e))
        send_tg("HATA: " + symbol + " - " + str(e))

# =========================================================================
# POZISYON TAKIP
# TP1 vurulunca → SL BE'ye cekilir
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
                    # Pozisyon kapandi — nedeni bul
                    ep        = ism["ep"]
                    tp2       = ism["tp2"]
                    sl        = ism["sl"]
                    tp1_hit   = ism["tp1_hit"]

                    # Son kapanma fiyatini bulmak icin trade history bak
                    try:
                        trades = binance_request("GET", "/fapi/v1/userTrades", {
                            "symbol": symbol, "limit": 5
                        })
                        son_fiyat = float(trades[-1]["price"]) if isinstance(trades, list) and trades else mark_price
                    except:
                        son_fiyat = mark_price

                    # Kapanma nedenini tespit et
                    if tp1_hit:
                        # TP1 alindi — Full Win mi BE mi?
                        if yon == "LONG":
                            full_win = son_fiyat >= tp2 * 0.998
                        else:
                            full_win = son_fiyat <= tp2 * 1.002

                        if full_win:
                            # Full Win
                            gun_zarar.ekle_kazanc(abs(unreal_pnl) if unreal_pnl > 0 else 0)
                            istat.full_win += 1
                            send_tg(
                                "🏆 FULL WIN: " + symbol + "\n"
                                "🎯 Hedef vuruldu: " + str(son_fiyat) + "\n"
                                "📊 TP2 kapandi!\n"
                                "💵 PNL: +" + str(round(abs(unreal_pnl), 2)) + "$"
                            )
                        else:
                            # Risksiz BE — TP1 alindi ama TP2 gelmedi
                            istat.be += 1
                            send_tg(
                                "⚖️ RISKSIZ KAPANDI: " + symbol + "\n"
                                "🛡 BE stop tetiklendi: " + str(son_fiyat) + "\n"
                                "✅ TP1 kazanci korundu (+1R)\n"
                                "📊 Net: 0 zarar"
                            )
                    else:
                        # TP1 gelmeden kapandi — Stop
                        gun_zarar.ekle(abs(unreal_pnl))
                        istat.stop += 1
                        send_tg(
                            "💥 STOP: " + symbol + "\n"
                            "🛑 Stop tetiklendi: " + str(son_fiyat) + "\n"
                            "📊 Zarar: -" + str(round(abs(unreal_pnl), 2)) + "$\n"
                            "📉 Gunluk Net: " + str(round(gun_zarar.net_pnl(), 2)) + "$"
                        )

                    del aktif_islemler[symbol]
                    continue

                mark_price = get_mark_price(symbol)
                if mark_price == 0:
                    continue

                yon        = ism["yon"]
                close_side = "SELL" if yon == "LONG" else "BUY"
                rem        = round_qty(symbol, abs(pos_amt))

                # MAX ZARAR
                if unreal_pnl <= -MAX_LOSS_TRADE:
                    cancel_all_orders(symbol)
                    time.sleep(0.2)
                    close_position_market(symbol, close_side, rem, "MAX ZARAR")
                    gun_zarar.ekle(abs(unreal_pnl))
                    send_tg(
                        "🚨 MAX ZARAR: " + symbol + "\n"
                        "💸 Zarar: " + str(round(unreal_pnl, 2)) + "$\n"
                        "❌ Pozisyon kapatildi!\n"
                        "📊 Gunluk Net: " + str(round(gun_zarar.net_pnl(), 2)) + "$"
                    )
                    if symbol in aktif_islemler:
                        del aktif_islemler[symbol]
                    continue

                # TP1 VURULDU MU?
                # Hem fiyat hem miktar kontrolu — ikisinden biri yeterliyse TP1 sayilir
                if not ism["tp1_hit"]:
                    tp1_fiyat_ok = (
                        (yon == "LONG"  and mark_price >= ism["tp1"]) or
                        (yon == "SHORT" and mark_price <= ism["tp1"])
                    )
                    tp1_miktar_ok = abs(pos_amt) < ism["qty"] * (TP2_PCT + 0.05)

                    if tp1_fiyat_ok or tp1_miktar_ok:
                        ism["tp1_hit"] = True
                        log.info("TP1 vuruldu: " + symbol + " (fiyat=" + str(tp1_fiyat_ok) + " miktar=" + str(tp1_miktar_ok) + ")")

                    if not ism["be_set"]:
                        ism["be_set"] = True
                        cancel_all_orders(symbol)
                        time.sleep(0.3)

                        # TP2 limit'i yeniden koy
                        tp2_rem = round_qty(symbol, abs(pos_amt))
                        if tp2_rem > 0:
                            place_limit_tp(symbol, close_side, tp2_rem, ism["tp2"])
                            time.sleep(0.2)

                        # SL → BE (giris fiyati)
                        sl_result = place_algo_sl(symbol, close_side, tp2_rem, ism["ep"])
                        
                        # SL basarili mi kontrol et
                        if "algoId" not in sl_result and "orderId" not in sl_result:
                            send_tg(
                                "⚠️ SL KURULAMADI: " + symbol + "\n"
                                "BE fiyati: " + str(ism["ep"]) + "\n"
                                "❗ MANUEL SL KOY!"
                            )
                        else:
                            log.info("BE SL basariyla kuruldu: " + symbol + " @ " + str(ism["ep"]))

                        send_tg(
                            "💰 TP1 ALINDI: " + symbol + "\n"
                            "📍 +1R @ " + str(mark_price) + "\n"
                            "📦 %30 kapatildi!\n\n"
                            "🛡 SL → BE: " + str(ism["ep"]) + "\n"
                            "🎯 Kalan %70 hedefe gidiyor\n"
                            "📍 Hedef: " + str(ism["tp2"])
                        )

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
                        aktif_islemler[symbol] = {
                            "yon": yon, "ep": ep,
                            "sl": 0, "tp1": 0, "tp2": 0,
                            "qty": abs(amt),
                            "tp1_qty": 0, "tp2_qty": abs(amt),
                            "risk_usdt": RISK_USDT,
                            "tp1_hit": True,
                            "be_set": True,
                        }
                        send_tg("🔄 SENKRONIZE: " + symbol + "\n📊 Bot izlemeye basladi. SL manuel kontrol!")
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
                    aktif_islemler[symbol] = {
                        "yon": yon, "ep": ep,
                        "sl": 0, "tp1": 0, "tp2": 0,
                        "qty": abs(amt),
                        "tp1_qty": 0, "tp2_qty": abs(amt),
                        "risk_usdt": RISK_USDT,
                        "tp1_hit": True,
                        "be_set": True,
                    }
                    yuklenen += 1
            if yuklenen > 0:
                send_tg("📂 Acik pozisyon yuklendi: " + str(yuklenen) + " adet\n🛑 SL manuel kontrol!")
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
    return text.replace("\n", "\n")

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

        text_upper = text.upper()

        # Pine simulasyon mesajlarini filtrele — Telegram'a iletme
        PINE_MESAJLARI = ["FULL WIN", "RISKSIZ BAGLI", "STOP VURULDU", "TP1 ALINDI", "NETICE:"]
        is_pine_sim = any(k in text_upper for k in PINE_MESAJLARI) and "EMELLIYYATA GIR" not in text_upper

        if is_pine_sim:
            log.info("Pine simulasyon mesaji, yok sayildi: " + text[:60])
            return jsonify({"status": "ok"}), 200

        send_tg(text, chat_id)

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

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log.error("Webhook hatasi: " + str(e))
        return jsonify({"status": "error"}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":       "ok",
        "version":      "v2.6",
        "aktif":        list(aktif_islemler.keys()),
        "trade":        TRADE_ACTIVE,
        "gunluk_zarar": gun_zarar.gun_zarari,
        "durduruldu":   gun_zarar.durduruldu,
        "istat":        {"toplam": istat.toplam, "net_r": istat.net_r},
        "cikis":        "TP1=+1R/%" + str(int(TP1_PCT*100)) + "→BE | TP2=Hedef/%" + str(int(TP2_PCT*100))
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
    log.info("PHANTOM BOT v2.6 baslatildi.")
    send_tg(
        "👻 PHANTOM BOT v2.6 aktiv\n\n"
        "📥 GIRIS: MARKET (aninda)\n\n"
        "📤 CIKIS:\n"
        "  💰 TP1 → +1R @ %" + str(int(TP1_PCT*100)) + " kapat\n"
        "  🛡 BE  → TP1 sonrasi SL girişe cekilir\n"
        "  🎯 TP2 → Tam hedef @ %" + str(int(TP2_PCT*100)) + " kapat\n\n"
        "🚨 Max Zarar/Islem: " + str(MAX_LOSS_TRADE) + "$\n"
        "📊 Max Zarar/Gun:   " + str(MAX_LOSS_DAILY) + "$\n"
        "⚡ Trade: " + ("AKTIV" if TRADE_ACTIVE else "PASIV")
    )
    if TRADE_ACTIVE:
        pozisyonlari_yukle()
        threading.Thread(target=pozisyon_takip, daemon=True).start()
        threading.Thread(target=pozisyon_senkronize, daemon=True).start()
        threading.Thread(target=zamanlayici, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

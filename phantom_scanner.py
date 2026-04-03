"""
PHANTOM SCANNER v1.0
====================
Binance USDS-M Futures USDT.P paritelerini SFP stratejisiyle tarar.
- Ilk calistirmada: son 90 gunluk veri, tek seferlik rapor
- Her Pazartesi 08:00 Baku (04:00 UTC): haftalik guncelleme raporu

Kurulum:
    pip install requests pandas schedule

Calistirma:
    python phantom_scanner.py
"""

import requests
import pandas as pd
import schedule
import time
import logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

# =========================================================================
# AYARLAR
# =========================================================================
# Rapor gidecek yeni bot
TG_BOT_URL  = "https://api.telegram.org/bot8730809758:AAH5pxgy3PWA4cd0_m6N1Jb5-QOTQcPJJ6Q/sendMessage"
TG_CHAT_ID  = "811792517"

TIMEFRAMES   = ["5m", "15m", "1h"]
DAYS_BACK    = 90

# Filtreler
MIN_TRADES   = 20
MIN_NET_R    = 25.0
MIN_KAR_FAKT = 2.5
TOP_N        = 15   # max gosterilecek parite sayisi

# SFP parametreleri — PHANTOM_V3 ile ayni
WICK_MULT   = 1.5
P_LEFT      = 15
P_RIGHT     = 5
ATR_LEN     = 14
ATR_MULT    = 1.0
MIN_RR      = 1.95
OB_LOOKBACK = 20
FVG_BARS    = 10
HTF_EMA_LEN = 50

# =========================================================================
# BINANCE API
# =========================================================================
BINANCE_URL = "https://fapi.binance.com"

def get_all_usdt_pairs():
    """Binance USDS-M Futures'taki tum USDT sonu biten aktif pariteleri al"""
    try:
        r = requests.get(f"{BINANCE_URL}/fapi/v1/exchangeInfo", timeout=15)
        data = r.json()
        pairs = []
        for s in data["symbols"]:
            if (s["quoteAsset"] == "USDT" and
                s["status"] == "TRADING" and
                s["contractType"] == "PERPETUAL"):
                pairs.append(s["symbol"])
        log.info(f"Toplam {len(pairs)} USDT.P paritesi bulundu")
        return pairs
    except Exception as e:
        log.error(f"Parite listesi alinamadi: {e}")
        return []

def get_klines(symbol, interval, days):
    """Belirtilen parite ve zaman dilimi icin OHLCV verisi al"""
    try:
        end_ms   = int(datetime.utcnow().timestamp() * 1000)
        start_ms = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)

        all_klines = []
        limit = 1500

        while start_ms < end_ms:
            r = requests.get(f"{BINANCE_URL}/fapi/v1/klines", params={
                "symbol":    symbol,
                "interval":  interval,
                "startTime": start_ms,
                "endTime":   end_ms,
                "limit":     limit
            }, timeout=15)

            if r.status_code != 200:
                break

            klines = r.json()
            if not klines:
                break

            all_klines.extend(klines)
            start_ms = klines[-1][0] + 1

            if len(klines) < limit:
                break

            time.sleep(0.1)

        if not all_klines:
            return None

        df = pd.DataFrame(all_klines, columns=[
            "time","open","high","low","close","volume",
            "close_time","quote_vol","trades","taker_base","taker_quote","ignore"
        ])
        df["time"]   = pd.to_datetime(df["time"], unit="ms")
        df["open"]   = df["open"].astype(float)
        df["high"]   = df["high"].astype(float)
        df["low"]    = df["low"].astype(float)
        df["close"]  = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        return df.reset_index(drop=True)

    except Exception as e:
        log.error(f"{symbol} {interval} veri hatasi: {e}")
        return None

# =========================================================================
# INDIKATÖRLER
# =========================================================================
def calc_atr(df, period=14):
    """ATR hesapla"""
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def find_pivots(df, left, right):
    """Pivot high ve low bul"""
    ph = pd.Series(index=df.index, dtype=float)
    pl = pd.Series(index=df.index, dtype=float)

    for i in range(left, len(df) - right):
        # Pivot high
        if df["high"].iloc[i] == df["high"].iloc[i-left:i+right+1].max():
            ph.iloc[i] = df["high"].iloc[i]
        # Pivot low
        if df["low"].iloc[i] == df["low"].iloc[i-left:i+right+1].min():
            pl.iloc[i] = df["low"].iloc[i]

    return ph, pl

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# =========================================================================
# SFP STRATEJİSİ BACKTEST
# =========================================================================
def backtest_sfp(df):
    """
    SFP stratejisini simule et.
    Her islem 3 kategoriden birine girer:
    - full_win: TP2 hedefine ulasti
    - risksiz_be: TP1 sonrasi breakeven
    - stop: TP1 gelmeden stop
    TP1 ayrıca sayılmaz.
    """
    if df is None or len(df) < 100:
        return None

    atr   = calc_atr(df, ATR_LEN)
    ph, pl = find_pivots(df, P_LEFT, P_RIGHT)
    ema50 = calc_ema(df["close"], HTF_EMA_LEN)

    results = {
        "full_win":   0,
        "risksiz_be": 0,
        "stop":       0,
        "r_full_win": 0.0,
        "r_be":       0.0,
        "r_stop":     0.0,
    }

    last_ph   = None
    last_pl   = None
    swept_ph  = None
    swept_pl  = None
    in_trade  = False
    ep = sl = tp1 = tp2 = sl_dist = 0.0
    trade_dir = 0
    tp1_hit   = False
    start_qty = 1.0

    for i in range(P_LEFT + P_RIGHT + ATR_LEN, len(df)):
        row   = df.iloc[i]
        atr_v = atr.iloc[i]

        if pd.notna(ph.iloc[i]):
            last_ph = ph.iloc[i]
        if pd.notna(pl.iloc[i]):
            last_pl = pl.iloc[i]

        if pd.isna(atr_v) or atr_v == 0:
            continue

        body    = abs(row["close"] - row["open"])
        body    = max(body, 1e-10)
        wick_up = row["high"] - max(row["close"], row["open"])
        wick_dn = min(row["close"], row["open"]) - row["low"]

        # Aktif işlem takibi
        if in_trade:
            # TP1 kontrolu
            if not tp1_hit:
                if trade_dir == 1 and row["high"] >= tp1:
                    tp1_hit = True
                    sl = ep  # breakeven
                elif trade_dir == -1 and row["low"] <= tp1:
                    tp1_hit = True
                    sl = ep

            # Kapanis kontrolu
            if trade_dir == 1:
                if row["low"] <= sl:
                    # Stop vuruldu
                    if tp1_hit:
                        results["risksiz_be"] += 1
                        results["r_be"] += 1.0
                    else:
                        results["stop"] += 1
                        results["r_stop"] -= 1.0
                    in_trade = False
                    tp1_hit = False
                elif row["high"] >= tp2:
                    # TP2 hedef vuruldu — Full Win
                    rr_val = abs(tp2 - ep) / sl_dist
                    results["full_win"] += 1
                    results["r_full_win"] += rr_val
                    in_trade = False
                    tp1_hit = False
            else:
                if row["high"] >= sl:
                    if tp1_hit:
                        results["risksiz_be"] += 1
                        results["r_be"] += 1.0
                    else:
                        results["stop"] += 1
                        results["r_stop"] -= 1.0
                    in_trade = False
                    tp1_hit = False
                elif row["low"] <= tp2:
                    rr_val = abs(ep - tp2) / sl_dist
                    results["full_win"] += 1
                    results["r_full_win"] += rr_val
                    in_trade = False
                    tp1_hit = False
            continue

        # Yeni işlem sinyali ara
        if in_trade:
            continue

        # LONG SFP
        if (last_pl is not None and
            row["low"] < last_pl and
            row["close"] > last_pl and
            wick_dn >= body * WICK_MULT and
            (swept_pl is None or last_pl != swept_pl)):

            ep_val   = row["close"]
            sl_val   = round(row["low"] - atr_v * ATR_MULT, 10)
            sld      = max(abs(ep_val - sl_val), 1e-10)
            tp1_val  = round(ep_val + sld, 10)
            tp2_val  = round(ep_val + sld * max(MIN_RR, 1.95), 10)
            rr_check = abs(tp2_val - ep_val) / sld

            if rr_check >= MIN_RR:
                in_trade  = True
                ep        = ep_val
                sl        = sl_val
                tp1       = tp1_val
                tp2       = tp2_val
                sl_dist   = sld
                trade_dir = 1
                tp1_hit   = False
                swept_pl  = last_pl

        # SHORT SFP
        elif (last_ph is not None and
              row["high"] > last_ph and
              row["close"] < last_ph and
              wick_up >= body * WICK_MULT and
              (swept_ph is None or last_ph != swept_ph)):

            ep_val   = row["close"]
            sl_val   = round(row["high"] + atr_v * ATR_MULT, 10)
            sld      = max(abs(sl_val - ep_val), 1e-10)
            tp1_val  = round(ep_val - sld, 10)
            tp2_val  = round(ep_val - sld * max(MIN_RR, 1.95), 10)
            rr_check = abs(ep_val - tp2_val) / sld

            if rr_check >= MIN_RR:
                in_trade  = True
                ep        = ep_val
                sl        = sl_val
                tp1       = tp1_val
                tp2       = tp2_val
                sl_dist   = sld
                trade_dir = -1
                tp1_hit   = False
                swept_ph  = last_ph

    # Istatistik hesapla
    total_trades = results["full_win"] + results["risksiz_be"] + results["stop"]
    if total_trades < MIN_TRADES:
        return None

    net_r     = results["r_full_win"] + results["r_be"] + results["r_stop"]
    kazanan_r = results["r_full_win"] + results["r_be"]
    kaybeden_r = abs(results["r_stop"]) if results["r_stop"] != 0 else 0.001
    kar_fakt  = kazanan_r / kaybeden_r
    basari    = (results["full_win"] + results["risksiz_be"]) * 100.0 / total_trades

    return {
        "total":      total_trades,
        "full_win":   results["full_win"],
        "risksiz_be": results["risksiz_be"],
        "stop":       results["stop"],
        "r_full_win": round(results["r_full_win"], 2),
        "r_be":       round(results["r_be"], 2),
        "net_r":      round(net_r, 2),
        "kar_fakt":   round(kar_fakt, 2),
        "basari":     round(basari, 1),
    }

# =========================================================================
# TELEGRAM
# =========================================================================
def send_tg(text):
    try:
        r = requests.post(TG_BOT_URL, json={
            "chat_id": TG_CHAT_ID,
            "text":    text
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hatasi: {e}")
        return False

def tf_label(tf):
    return {"5m":"5M","15m":"15M","1h":"1H","4h":"4H"}.get(tf, tf.upper())

# =========================================================================
# ANA TARAMA
# =========================================================================
def run_scan(baslik):
    log.info(f"Tarama basladi: {baslik}")
    pairs = get_all_usdt_pairs()
    if not pairs:
        log.error("Parite listesi bos, tarama iptal")
        return

    results = []
    toplam = len(pairs) * len(TIMEFRAMES)
    islenen = 0

    for symbol in pairs:
        for tf in TIMEFRAMES:
            islenen += 1
            if islenen % 50 == 0:
                log.info(f"  {islenen}/{toplam} islendi...")

            df = get_klines(symbol, tf, DAYS_BACK)
            if df is None:
                continue

            stat = backtest_sfp(df)
            if stat is None:
                continue

            # Filtrele
            if (stat["net_r"]    >= MIN_NET_R and
                stat["kar_fakt"] >= MIN_KAR_FAKT and
                stat["total"]    >= MIN_TRADES):
                results.append({
                    "symbol": symbol,
                    "tf":     tf,
                    **stat
                })

            time.sleep(0.05)

    # Net R'ye gore sirala
    results.sort(key=lambda x: x["net_r"], reverse=True)
    top = results[:TOP_N]

    log.info(f"Tarama bitti. {len(results)} parite filtreyi gecti.")

    # Rapor olustur
    if not top:
        send_tg(f"{baslik}\n\nFiltreyi gecen parite bulunamadi.\n"
                f"(Min Net R: +{MIN_NET_R}R | Min KF: {MIN_KAR_FAKT} | Min Islem: {MIN_TRADES})")
        return

    tarih = datetime.utcnow().strftime("%d/%m/%Y")
    msg   = f"{baslik}\n{tarih} | Son {DAYS_BACK} gun\n"
    msg  += f"Filtre: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}\n"
    msg  += f"Gececen: {len(results)} parite | En iyi {len(top)} gosteriliyor\n"
    msg  += "=" * 32 + "\n\n"

    for idx, r in enumerate(top, 1):
        r_fw  = f"+{r['r_full_win']}R" if r['r_full_win'] >= 0 else f"{r['r_full_win']}R"
        r_be  = f"+{r['r_be']}R"
        r_net = f"+{r['net_r']}R" if r['net_r'] >= 0 else f"{r['net_r']}R"

        msg += f"{idx}. {r['symbol']} — {tf_label(r['tf'])}\n"
        msg += f"   Islem: {r['total']}\n"
        msg += f"   Full Win: {r['full_win']}  ({r_fw})\n"
        msg += f"   Risksiz BE: {r['risksiz_be']}  ({r_be})\n"
        msg += f"   Stop: {r['stop']}\n"
        msg += f"   Net R: {r_net}\n"
        msg += f"   Kar Faktoru: {r['kar_fakt']}\n"
        msg += f"   Basari: %{r['basari']}\n"
        msg += "\n"

    # Telegram'a gonder (4096 karakter siniri var)
    if len(msg) <= 4096:
        send_tg(msg)
    else:
        # Parcalara bol
        chunks = []
        lines  = msg.split("\n")
        chunk  = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                chunks.append(chunk)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            chunks.append(chunk)
        for c in chunks:
            send_tg(c)
            time.sleep(0.5)

    log.info("Rapor gonderildi.")

# =========================================================================
# ZAMANLAYICI
# =========================================================================
ilk_calistirma_yapildi = False

def haftalik_tarama():
    run_scan("HAFTALIK TARAMA RAPORU")

def main():
    global ilk_calistirma_yapildi

    log.info("PHANTOM SCANNER baslatildi.")
    log.info(f"Zaman dilimleri: {TIMEFRAMES}")
    log.info(f"Filtreler: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}")
    log.info(f"Haftalik rapor: Her Pazartesi 04:00 UTC (Baku 08:00)")
    log.info("")

    # Ilk calistirmada hemen tara
    if not ilk_calistirma_yapildi:
        ilk_calistirma_yapildi = True
        log.info("Ilk tarama baslıyor (tek seferlik)...")
        run_scan("ILK TARAMA RAPORU — Son 90 Gun")

    # Her Pazartesi 04:00 UTC
    schedule.every().monday.at("04:00").do(haftalik_tarama)

    log.info("Zamanlayici aktif. Haftalik raporlar bekleniyor...")

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
